# -*- coding: utf-8 -*-
import copy
import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs

from resources.lib import auto_next_policy, i18n, json_storage, playback_utils

_CACHE_LOCK = None
_STATE_CACHE = None
_STATE_CACHE_TS = 0.0
_STATE_CACHE_TTL = 2.0
_TRAKT_SCROBBLE_LAST = {}
try:
    import threading
    _CACHE_LOCK = threading.RLock()
    _TRAKT_SCROBBLE_LOCK = threading.RLock()
except Exception:
    threading = None
    _TRAKT_SCROBBLE_LOCK = None


class _DummyLock:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


if _CACHE_LOCK is None:
    _CACHE_LOCK = _DummyLock()
if _TRAKT_SCROBBLE_LOCK is None:
    _TRAKT_SCROBBLE_LOCK = _DummyLock()


def _log(msg, level=xbmc.LOGINFO):
    try:
        xbmc.log(f"[IKARUS][TMDB PLAYBACK] {msg}", level)
    except Exception:
        pass


def _refresh_container_soon():
    try:
        xbmc.executebuiltin("AlarmClock(IkarusPlaybackStateRefresh,Container.Refresh,00:01,silent)")
    except Exception:
        try:
            xbmc.executebuiltin("Container.Refresh")
        except Exception:
            pass


def _profile_dir():
    addon = xbmcaddon.Addon()
    base = xbmcvfs.translatePath(addon.getAddonInfo("profile"))
    path = os.path.join(base, "tmdb_state")
    try:
        xbmcvfs.mkdirs(path)
    except Exception:
        try:
            os.makedirs(path, exist_ok=True)
        except Exception:
            pass
    return path


def playback_state_path():
    return os.path.join(_profile_dir(), "tmdb_playback_state.json")


def playback_trakt_snapshot_path():
    return os.path.join(_profile_dir(), "tmdb_playback_trakt_snapshot.json")


def _sanitize_local_source(src):
    if not isinstance(src, dict):
        return {}
    cleaned = copy.deepcopy(src)
    for key in ("direct", "play_url", "resolved_url"):
        cleaned.pop(key, None)
    url = str(cleaned.get("url") or "").strip()
    if url and not url.startswith(("tmdb://", "trakt://")):
        cleaned.pop("url", None)
    return cleaned


def _clean_local_state(state):
    cleaned = {}
    for tmdb_id, row in (state or {}).items():
        if not isinstance(row, dict):
            continue
        row_copy = copy.deepcopy(row)
        sources = row_copy.get("sources") or {}
        if not isinstance(sources, dict):
            continue
        local_sources = {}
        for source_key, src in sources.items():
            if not isinstance(src, dict):
                continue
            if str(src.get("provider") or "").strip().lower() == "trakt":
                continue
            clean_src = _sanitize_local_source(src)
            if clean_src:
                local_sources[str(source_key)] = clean_src
        if not local_sources:
            continue
        row_copy["sources"] = local_sources
        _normalize_row_title_from_sources(row_copy)
        cleaned[str(tmdb_id)] = row_copy
    return cleaned


def _split_legacy_state(state):
    local_state = {}
    trakt_state = {}
    for tmdb_id, row in (state or {}).items():
        if not isinstance(row, dict):
            continue
        sources = row.get("sources") if isinstance(row.get("sources"), dict) else {}
        local_sources = {}
        trakt_sources = {}
        for source_key, src in sources.items():
            if not isinstance(src, dict):
                continue
            if str(src.get("provider") or "").strip().lower() == "trakt":
                trakt_sources[str(source_key)] = copy.deepcopy(src)
            else:
                local_sources[str(source_key)] = copy.deepcopy(src)
        if local_sources:
            local_row = copy.deepcopy(row)
            local_row["sources"] = local_sources
            local_state[str(tmdb_id)] = local_row
        if trakt_sources:
            trakt_row = copy.deepcopy(row)
            trakt_row["sources"] = trakt_sources
            trakt_state[str(tmdb_id)] = trakt_row
    return local_state, trakt_state


def _merge_snapshot_state(existing, incoming):
    merged = copy.deepcopy(existing if isinstance(existing, dict) else {})
    for tmdb_id, incoming_row in (incoming or {}).items():
        if not isinstance(incoming_row, dict):
            continue
        current = merged.get(str(tmdb_id))
        current = copy.deepcopy(current) if isinstance(current, dict) else {}
        for key, value in incoming_row.items():
            if key == "sources":
                continue
            if value not in (None, ""):
                current[key] = copy.deepcopy(value)
        sources = current.get("sources") if isinstance(current.get("sources"), dict) else {}
        incoming_sources = incoming_row.get("sources") if isinstance(incoming_row.get("sources"), dict) else {}
        sources.update(copy.deepcopy(incoming_sources))
        current["sources"] = sources
        merged[str(tmdb_id)] = current
    return merged


def _persist_legacy_trakt_state(trakt_state):
    if not trakt_state:
        return
    path = playback_trakt_snapshot_path()

    def _update(existing):
        return _merge_snapshot_state(existing, trakt_state)

    json_storage.update_json(path, {}, _update, expected_type=dict, indent=2)


def _write_local_state(state):
    cleaned = _clean_local_state(state)

    json_storage.write_json(playback_state_path(), cleaned, indent=2, keep_backup=True)
    return cleaned


def _mutate_local_state(mutator):
    path = playback_state_path()
    with json_storage.file_lock(path):
        raw_state = json_storage.read_json(
            path,
            {},
            expected_type=dict,
            logger=lambda message: _log(message, xbmc.LOGWARNING),
        )
        local_state, legacy_trakt_state = _split_legacy_state(raw_state)
        if legacy_trakt_state:
            _persist_legacy_trakt_state(legacy_trakt_state)
        result = mutator(local_state)
        cleaned = _write_local_state(local_state)
        if legacy_trakt_state:
            cleaned = _write_local_state(cleaned)
        _cache_set(cleaned)
    return result


def _cache_get():
    with _CACHE_LOCK:
        if _STATE_CACHE is None:
            return None
        if (time.time() - _STATE_CACHE_TS) > _STATE_CACHE_TTL:
            return None
        try:
            return json.loads(json.dumps(_STATE_CACHE))
        except Exception:
            return dict(_STATE_CACHE)


def _cache_set(data):
    with _CACHE_LOCK:
        global _STATE_CACHE, _STATE_CACHE_TS
        try:
            _STATE_CACHE = json.loads(json.dumps(data if isinstance(data, dict) else {}))
        except Exception:
            _STATE_CACHE = dict(data if isinstance(data, dict) else {})
        _STATE_CACHE_TS = time.time()


def invalidate_cache():
    with _CACHE_LOCK:
        global _STATE_CACHE, _STATE_CACHE_TS
        _STATE_CACHE = None
        _STATE_CACHE_TS = 0.0


def flush_remote_if_due(force: bool = False):
    return


def playback_state_load(force_remote: bool = False):
    if force_remote:
        invalidate_cache()
    cached = _cache_get()
    if cached is not None:
        return cached

    path = playback_state_path()
    try:
        raw_data = json_storage.read_json(
            path,
            {},
            expected_type=dict,
            logger=lambda message: _log(message, xbmc.LOGWARNING),
        )
        local_data, legacy_trakt_state = _split_legacy_state(raw_data)
        if legacy_trakt_state:
            with json_storage.file_lock(path):
                locked_data = json_storage.read_json(
                    path,
                    {},
                    expected_type=dict,
                    logger=lambda message: _log(message, xbmc.LOGWARNING),
                )
                local_data, legacy_trakt_state = _split_legacy_state(locked_data)
                if legacy_trakt_state:
                    _persist_legacy_trakt_state(legacy_trakt_state)
                    local_data = _write_local_state(local_data)
                    local_data = _write_local_state(local_data)
                    _log(
                        f"migrated Trakt sources to snapshot items={len(legacy_trakt_state)}",
                        xbmc.LOGINFO,
                    )
        else:
            local_data = _clean_local_state(local_data)
        _cache_set(local_data)
        return local_data
    except Exception as e:
        _log(f"load error: {e}", xbmc.LOGERROR)
        _cache_set({})
        return {}


def refresh_from_remote():
    return playback_state_load(force_remote=True)


def playback_state_save(state, sync_remote: bool = True):
    try:
        path = playback_state_path()
        local_state, legacy_trakt_state = _split_legacy_state(state if isinstance(state, dict) else {})
        with json_storage.file_lock(path):
            if legacy_trakt_state:
                _persist_legacy_trakt_state(legacy_trakt_state)
            cleaned = _write_local_state(local_state)
            if legacy_trakt_state:
                cleaned = _write_local_state(cleaned)
            _cache_set(cleaned)
        return True
    except Exception as e:
        _log(f"save error: {e}", xbmc.LOGERROR)
        return False


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean_text(value) -> str:
    return str(value or "").strip()


def _has_local_latin_chars(text: str) -> bool:
    return bool(re.search(r"[ÁáČčĎďÉéĚěÍíŇňÓóŘřŠšŤťÚúŮůÝýŽž]", str(text or "")))


def _looks_like_file_title(title: str) -> bool:
    title = _clean_text(title)
    lowered = title.lower()
    if lowered.endswith((".mkv", ".mp4", ".avi", ".mov", ".ts", ".m2ts", ".wmv")):
        return True
    return bool(re.search(r"\b(1080p|2160p|720p|bluray|web[-_. ]?dl|webrip|x264|x265|h\.?264|h\.?265)\b", lowered))


def _repair_protected_local_title(row: dict):
    if not isinstance(row, dict) or not row.get("_title_is_local"):
        return
    title = _clean_text(row.get("title"))
    original = _clean_text(row.get("original_title"))
    if title and original and title.casefold() != original.casefold():
        if _has_local_latin_chars(original) and not _has_local_latin_chars(title):
            row["title"] = original


def _has_protected_local_title(row: dict) -> bool:
    if not isinstance(row, dict) or not row.get("_title_is_local"):
        return False
    title = _clean_text(row.get("title"))
    if not title:
        return False
    original = _clean_text(row.get("original_title"))
    if original and title.casefold() != original.casefold():
        if _has_local_latin_chars(original) and not _has_local_latin_chars(title):
            return False
        if not row.get("_title_source_tmdb_detail") and not _has_local_latin_chars(title) and not _has_local_latin_chars(original):
            return False
    return True


def _source_display_title(source_meta: dict) -> str:
    title = _clean_text((source_meta or {}).get("display_title"))
    if not title:
        return ""
    if title.lower() in ("trakt.tv", "zdroj"):
        return ""
    if _looks_like_file_title(title):
        return ""
    return title

def _episode_display_title(show_title: str, season: str = "", episode: str = "", episode_title: str = "") -> str:
    show_title = _clean_text(show_title) or "Zdroj"
    parts = [show_title]

    season_i = _to_int(season)
    episode_i = _to_int(episode)
    if season_i is not None and episode_i is not None:
        parts.append(f"S{season_i:02d}E{episode_i:02d}")
    else:
        season = _clean_text(season)
        episode = _clean_text(episode)
        if season and episode:
            parts.append(f"S{season}E{episode}")

    episode_title = _clean_text(episode_title)
    if episode_title:
        parts.append(episode_title)

    return " - ".join(parts)


def _is_movie_playback_meta(row: dict, meta: dict = None, source_meta: dict = None) -> bool:
    mode = _clean_text((source_meta or {}).get("mode")).lower()
    season = _clean_text((source_meta or {}).get("season"))
    episode = _clean_text((source_meta or {}).get("episode"))
    if mode == "serial_episode" or season or episode:
        return False

    media_type = _clean_text((meta or {}).get("media_type") or (row or {}).get("media_type")).lower()
    if media_type in ("tv", "tvshow", "show", "series", "serial"):
        return False
    return media_type == "movie" or not media_type


def _normalize_row_title_from_source(row: dict, meta: dict = None, source_meta: dict = None):
    # Playback source names are file/provider labels, not movie titles.
    # Keep persisted TMDB titles authoritative and never rewrite them from a source.
    return


def _normalize_row_title_from_sources(row: dict):
    # Do not let old saved source/file names rewrite the visible movie title on save.
    return


def _trakt_sync_playback_status(status: str, tmdb_id: str, pos_sec=None, total_sec=None,
                                ratio=None, meta: dict = None, source_meta: dict = None,
                                event_at: str = ""):
    try:
        from resources.lib import tmdb_playback_sync
    except Exception:
        try:
            import tmdb_playback_sync
        except Exception:
            return

    try:
        if str((source_meta or {}).get("provider") or "").strip().lower() == "trakt":
            return

        media_type = str((meta or {}).get("media_type") or "").strip().lower()
        season = (source_meta or {}).get("season")
        episode = (source_meta or {}).get("episode")
        mode = str((source_meta or {}).get("mode") or "").strip().lower()

        if mode == "serial_episode" or (season not in (None, "") and episode not in (None, "")):
            media_type = "episode"
        elif media_type in ("tv", "tvshow", "show", "series", "serial"):
            return
        else:
            media_type = "movie"

        try:
            progress = float(ratio) * 100.0
        except Exception:
            progress = 0.0
        if progress <= 0:
            try:
                total = float(total_sec or 0.0)
                pos = float(pos_sec or 0.0)
                progress = (pos / total) * 100.0 if total > 0 else 0.0
            except Exception:
                progress = 0.0
        if status == "finished":
            progress = max(progress, 100.0)
            trakt_action = "history"
        else:
            progress = max(1.0, min(95.0, progress))
            trakt_action = "pause"

        source_key = str((source_meta or {}).get("source_id") or (source_meta or {}).get("url") or "").strip()
        throttle_key = "|".join([
            str(tmdb_id),
            str(media_type),
            str(season or ""),
            str(episode or ""),
            source_key,
            str(trakt_action),
        ])
        now_ts = time.time()
        with _TRAKT_SCROBBLE_LOCK:
            last = _TRAKT_SCROBBLE_LAST.get(throttle_key) or {}
            last_ts = float(last.get("ts") or 0.0)
            last_progress = float(last.get("progress") or 0.0)
            if trakt_action == "history" and last.get("sent"):
                return
            if trakt_action == "pause" and last_ts > 0 and (now_ts - last_ts) < 300 and abs(progress - last_progress) < 5.0:
                return
        if trakt_action == "history":
            operation_id = tmdb_playback_sync.queue_history_status(
                media_type=media_type,
                tmdb_id=tmdb_id,
                season=season,
                episode=episode,
                watched_at=str(event_at or "").strip() or _now_iso(),
            )
        else:
            operation_id = tmdb_playback_sync.queue_progress_status(
                media_type=media_type,
                tmdb_id=tmdb_id,
                progress=progress,
                season=season,
                episode=episode,
                event_at=str(event_at or "").strip() or _now_iso(),
            )
        if not operation_id:
            return

        with _TRAKT_SCROBBLE_LOCK:
            _TRAKT_SCROBBLE_LAST[throttle_key] = {
                "ts": now_ts,
                "progress": progress,
                "sent": True,
            }

        def _send():
            try:
                result = tmdb_playback_sync.flush_pending_operations(limit=20)
                _log(
                    f"trakt queue flush sent={int(result.get('sent') or 0)} "
                    f"pending={int(result.get('pending') or 0)}",
                    xbmc.LOGINFO,
                )
            except Exception as e:
                _log(f"trakt sync {status} error: {e}", xbmc.LOGWARNING)

        if threading is not None:
            thread = threading.Thread(target=_send)
            try:
                thread.daemon = True
            except Exception:
                pass
            thread.start()
        else:
            _send()
    except Exception as e:
        _log(f"trakt sync {status} error: {e}", xbmc.LOGWARNING)


def make_source_key(provider: str, url: str = "", direct: str = "", source_id: str = "", file_hash: str = "") -> str:
    provider_norm = (provider or "").strip().lower()
    source_id_norm = (source_id or "").strip()
    file_hash_norm = (file_hash or "").strip()
    direct_norm = (direct or "").strip()
    url_norm = (url or "").strip()

    if source_id_norm or file_hash_norm:
        base = "|".join([provider_norm, source_id_norm, file_hash_norm])
    else:
        base = "|".join([provider_norm, direct_norm, url_norm])

    if not base.strip("|"):
        base = "unknown"
    return hashlib.md5(base.encode("utf-8")).hexdigest()


def update_item_meta(tmdb_id: str, meta: dict = None):
    tmdb_id = str(tmdb_id or "").strip()
    if not tmdb_id:
        return

    meta = dict(meta or {})
    def _update(state):
        row = state.get(tmdb_id) or {}
        row["tmdb_id"] = tmdb_id
        for key in ("title", "original_title", "year", "poster", "plot", "rating", "media_type", "total_episodes", "total_seasons", "_title_is_local", "_title_source_tmdb_detail", "_local_origin"):
            val = meta.get(key)
            if val not in (None, ""):
                row[key] = val
        row["updated_at"] = _now_iso()
        state[tmdb_id] = row
        return True

    return bool(_mutate_local_state(_update))


def update_items_meta(updates):
    updates = list(updates or [])
    if not updates:
        return 0

    def _update(state):
        changed = 0
        for tmdb_id, meta in updates:
            tmdb_id = str(tmdb_id or "").strip()
            if not tmdb_id or not isinstance(meta, dict):
                continue

            row = state.get(tmdb_id) or {}
            row["tmdb_id"] = tmdb_id
            row_changed = False
            incoming_title_is_local = bool(meta.get("_title_is_local"))
            for key in ("title", "original_title", "year", "poster", "plot", "rating", "media_type", "total_episodes", "total_seasons", "_title_is_local", "_title_source_tmdb_detail", "_local_origin"):
                val = meta.get(key)
                if val in (None, ""):
                    continue
                if key in ("title", "original_title"):
                    current = _clean_text(row.get(key))
                    incoming = _clean_text(val)
                    if current and not _looks_like_file_title(current) and not incoming_title_is_local and not _has_local_latin_chars(incoming):
                        continue
                if row.get(key) != val:
                    row[key] = val
                    changed += 1
                    row_changed = True

            if row_changed:
                row["updated_at"] = _now_iso()
            state[tmdb_id] = row
        return changed

    return int(_mutate_local_state(_update) or 0)


def _ensure_row(state: dict, tmdb_id: str, meta: dict = None):
    row = state.get(tmdb_id) or {}
    row["tmdb_id"] = tmdb_id
    row["sources"] = row.get("sources") or {}
    _repair_protected_local_title(row)
    protected_title = _has_protected_local_title(row)
    for key in ("title", "original_title", "year", "poster", "plot", "rating", "media_type", "total_episodes", "total_seasons", "_title_is_local", "_title_source_tmdb_detail", "_local_origin"):
        val = (meta or {}).get(key)
        if val in (None, ""):
            continue
        if key == "title" and protected_title:
            continue
        if key == "original_title" and protected_title and _has_local_latin_chars(row.get("original_title")):
            continue
        if key in ("_title_is_local", "_title_source_tmdb_detail") and not val:
            continue
        row[key] = val
    return row


def mark_started(tmdb_id: str, source_key: str, pos_sec=None, total_sec=None, ratio=None,
                 meta: dict = None, source_meta: dict = None, sync_remote: bool = True,
                 sync_trakt: bool = True, refresh_container: bool = True):
    tmdb_id = str(tmdb_id or "").strip()
    source_key = str(source_key or "").strip()
    if not tmdb_id or not source_key:
        return

    now = _now_iso()

    def _update(state):
        row = _ensure_row(state, tmdb_id, meta)
        src = row["sources"].get(source_key) or {}
        src["status"] = "started"
        src["last_started_at"] = now
        src["last_position_sec"] = float(pos_sec or 0.0)
        src["last_duration_sec"] = float(total_sec or 0.0)
        src["last_ratio"] = float(ratio or 0.0)
        for key in ("provider", "source_id", "file_hash", "direct", "url", "play_url", "title", "display_title", "mode", "season", "episode", "episode_title", "season_name"):
            val = (source_meta or {}).get(key)
            if val not in (None, ""):
                src[key] = val
        row["sources"][source_key] = src
        _normalize_row_title_from_source(row, meta=meta, source_meta=source_meta)
        row["last_played_at"] = now
        row["updated_at"] = now
        state[tmdb_id] = row
        return True

    saved = bool(_mutate_local_state(_update))
    if refresh_container:
        _refresh_container_soon()
    if saved and sync_trakt:
        _trakt_sync_playback_status("started", tmdb_id, pos_sec=pos_sec, total_sec=total_sec,
                                    ratio=ratio, meta=meta, source_meta=source_meta,
                                    event_at=now)
    return saved


def mark_finished(tmdb_id: str, source_key: str, pos_sec=None, total_sec=None, ratio=None,
                  meta: dict = None, source_meta: dict = None, sync_remote: bool = True,
                  sync_trakt: bool = True, refresh_container: bool = True,
                  finished_at: str = ""):
    tmdb_id = str(tmdb_id or "").strip()
    source_key = str(source_key or "").strip()
    if not tmdb_id or not source_key:
        return

    now = str(finished_at or "").strip() or _now_iso()

    def _update(state):
        row = _ensure_row(state, tmdb_id, meta)
        src = row["sources"].get(source_key) or {}
        src["status"] = "finished"
        src["last_finished_at"] = now
        src["last_position_sec"] = float(pos_sec or 0.0)
        src["last_duration_sec"] = float(total_sec or 0.0)
        src["last_ratio"] = float(ratio or 1.0)
        for key in ("provider", "source_id", "file_hash", "direct", "url", "play_url", "title", "display_title", "mode", "season", "episode", "episode_title", "season_name"):
            val = (source_meta or {}).get(key)
            if val not in (None, ""):
                src[key] = val
        row["sources"][source_key] = src
        _normalize_row_title_from_source(row, meta=meta, source_meta=source_meta)
        row["last_played_at"] = now
        row["updated_at"] = now
        state[tmdb_id] = row
        return True

    saved = bool(_mutate_local_state(_update))
    if refresh_container:
        _refresh_container_soon()
    if saved and sync_trakt:
        _trakt_sync_playback_status("finished", tmdb_id, pos_sec=pos_sec, total_sec=total_sec,
                                    ratio=ratio, meta=meta, source_meta=source_meta,
                                    event_at=now)
    return saved


def checkpoint_playback(tmdb_id: str, source_key: str, pos_sec=None, total_sec=None, ratio=None,
                        meta: dict = None, source_meta: dict = None):
    try:
        ratio_val = float(ratio or 0.0)
    except Exception:
        ratio_val = 0.0

    try:
        if ratio_val >= 0.8:
            return mark_finished(
                tmdb_id,
                source_key,
                pos_sec=pos_sec,
                total_sec=total_sec,
                ratio=ratio_val,
                meta=meta,
                source_meta=source_meta,
                sync_remote=False,
                sync_trakt=True,
                refresh_container=False,
            )
        if ratio_val >= 0.1:
            return mark_started(
                tmdb_id,
                source_key,
                pos_sec=pos_sec,
                total_sec=total_sec,
                ratio=ratio_val,
                meta=meta,
                source_meta=source_meta,
                sync_remote=False,
                sync_trakt=True,
                refresh_container=False,
            )
    except Exception as e:
        _log(f"checkpoint save error: {e}", xbmc.LOGERROR)
    return False


def mark_deleted(tmdb_id: str, source_key: str, meta: dict = None, source_meta: dict = None):
    tmdb_id = str(tmdb_id or "").strip()
    source_key = str(source_key or "").strip()
    if not tmdb_id or not source_key:
        return

    now = _now_iso()

    def _update(state):
        row = _ensure_row(state, tmdb_id, meta)
        src = row["sources"].get(source_key) or {}
        src["status"] = "delete"
        src["last_deleted_at"] = now
        for key in ("provider", "source_id", "file_hash", "direct", "url", "play_url", "title", "display_title", "mode", "season", "episode", "episode_title", "season_name"):
            val = (source_meta or {}).get(key)
            if val not in (None, ""):
                src[key] = val
        row["sources"][source_key] = src
        row["last_played_at"] = now
        row["updated_at"] = now
        state[tmdb_id] = row
        return True

    saved = bool(_mutate_local_state(_update))
    _refresh_container_soon()
    return saved


def mark_movie_finished(tmdb_id: str, meta: dict = None, watched_at: str = ""):
    tmdb_id = str(tmdb_id or "").strip()
    if not tmdb_id:
        return

    source_key = make_source_key(
        provider="manual",
        url=f"tmdb://movie/{tmdb_id}",
        source_id="manual_finished",
    )
    source_meta = {
        "provider": "manual",
        "source_id": "manual_finished",
        "url": f"tmdb://movie/{tmdb_id}",
        "title": "Ru\u010dn\u00ed ozna\u010den\u00ed jako zhl\u00e9dnut\u00e9",
    }
    return mark_finished(
        tmdb_id,
        source_key,
        pos_sec=1.0,
        total_sec=1.0,
        ratio=1.0,
        meta=meta,
        source_meta=source_meta,
        sync_trakt=False,
        finished_at=watched_at,
    )


def _manual_episode_source_parts(tmdb_id: str, season_i: int, episode_i: int,
                                 episode_title: str = "", season_name: str = ""):
    source_id = f"manual_finished_s{season_i:03d}e{episode_i:04d}"
    url = f"tmdb://tv/{tmdb_id}/season/{season_i}/episode/{episode_i}"
    source_key = make_source_key(
        provider="manual",
        url=url,
        source_id=source_id,
    )
    source_meta = {
        "provider": "manual",
        "source_id": source_id,
        "url": url,
        "title": episode_title or f"Ru\u010dn\u00ed ozna\u010den\u00ed S{season_i:02d}E{episode_i:02d}",
        "mode": "serial_episode",
        "season": str(season_i),
        "episode": str(episode_i),
        "episode_title": episode_title,
        "season_name": season_name,
    }
    return source_key, source_meta


def mark_episode_finished(tmdb_id: str, season, episode, meta: dict = None,
                          episode_title: str = "", season_name: str = "",
                          watched_at: str = ""):
    tmdb_id = str(tmdb_id or "").strip()
    season_i = _to_int(season)
    episode_i = _to_int(episode)
    if not tmdb_id or season_i is None or episode_i is None:
        return

    source_key, source_meta = _manual_episode_source_parts(
        tmdb_id,
        season_i,
        episode_i,
        episode_title=episode_title,
        season_name=season_name,
    )
    return mark_finished(
        tmdb_id,
        source_key,
        pos_sec=1.0,
        total_sec=1.0,
        ratio=1.0,
        meta=meta,
        source_meta=source_meta,
        sync_trakt=False,
        finished_at=watched_at,
    )


def mark_episodes_finished_bulk(tmdb_id: str, episodes, meta: dict = None,
                                watched_at: str = "") -> int:
    tmdb_id = str(tmdb_id or "").strip()
    if not tmdb_id:
        return 0

    now = str(watched_at or "").strip() or _now_iso()

    def _update(state):
        row = _ensure_row(state, tmdb_id, meta)
        sources = row.get("sources") or {}
        marked = 0
        for item in (episodes or []):
            if not isinstance(item, dict):
                continue
            season_i = _to_int(item.get("season"))
            episode_i = _to_int(item.get("episode"))
            if season_i is None or episode_i is None:
                continue

            source_key, source_meta = _manual_episode_source_parts(
                tmdb_id,
                season_i,
                episode_i,
                episode_title=str(item.get("episode_title") or "").strip(),
                season_name=str(item.get("season_name") or "").strip(),
            )
            src = sources.get(source_key) or {}
            src["status"] = "finished"
            src["last_finished_at"] = now
            src["last_position_sec"] = 1.0
            src["last_duration_sec"] = 1.0
            src["last_ratio"] = 1.0
            for key in ("provider", "source_id", "file_hash", "direct", "url", "play_url", "title", "display_title", "mode", "season", "episode", "episode_title", "season_name"):
                val = source_meta.get(key)
                if val not in (None, ""):
                    src[key] = val
            sources[source_key] = src
            marked += 1

        if not marked:
            return 0
        row["sources"] = sources
        row["last_played_at"] = now
        row["updated_at"] = now
        state[tmdb_id] = row
        return marked

    return int(_mutate_local_state(_update) or 0)


def remove_episode(tmdb_id: str, season, episode) -> bool:
    tmdb_id = str(tmdb_id or "").strip()
    season_i = _to_int(season)
    episode_i = _to_int(episode)
    if not tmdb_id or season_i is None or episode_i is None:
        return False

    def _update(state):
        row = state.get(tmdb_id)
        if not isinstance(row, dict):
            return False
        sources = row.get("sources") or {}
        changed = False
        for key, src in list(sources.items()):
            src_season, src_episode = _source_season_episode(src if isinstance(src, dict) else {})
            if src_season == season_i and src_episode == episode_i:
                sources.pop(key, None)
                changed = True
        if not changed:
            return False
        row["sources"] = sources
        row["updated_at"] = _now_iso()
        if sources:
            state[tmdb_id] = row
        else:
            state.pop(tmdb_id, None)
        return True

    return bool(_mutate_local_state(_update))


def remove_season(tmdb_id: str, season) -> bool:
    tmdb_id = str(tmdb_id or "").strip()
    season_i = _to_int(season)
    if not tmdb_id or season_i is None:
        return False

    def _update(state):
        row = state.get(tmdb_id)
        if not isinstance(row, dict):
            return False
        sources = row.get("sources") or {}
        changed = False
        for key, src in list(sources.items()):
            src_season, _ = _source_season_episode(src if isinstance(src, dict) else {})
            if src_season == season_i:
                sources.pop(key, None)
                changed = True
        if not changed:
            return False
        row["sources"] = sources
        row["updated_at"] = _now_iso()
        if sources:
            state[tmdb_id] = row
        else:
            state.pop(tmdb_id, None)
        return True

    return bool(_mutate_local_state(_update))


def remove_item(tmdb_id: str):
    tmdb_id = str(tmdb_id or "").strip()
    if not tmdb_id:
        return False

    def _update(state):
        if tmdb_id not in state:
            return False
        state.pop(tmdb_id, None)
        return True

    return bool(_mutate_local_state(_update))


def get_resume_seconds(tmdb_id: str, source_key: str):
    state = playback_state_load()
    row = state.get(str(tmdb_id or "").strip()) or {}
    src = (row.get("sources") or {}).get(str(source_key or "").strip()) or {}
    try:
        pos = float(src.get("last_position_sec") or 0.0)
    except Exception:
        pos = 0.0
    try:
        total = float(src.get("last_duration_sec") or 0.0)
    except Exception:
        total = 0.0
    try:
        ratio = float(src.get("last_ratio") or 0.0)
    except Exception:
        ratio = pos / total if total > 0 else 0.0

    status = str(src.get("status") or "").strip().lower()
    if status == "started" and total > 0 and pos > 0 and 0.1 <= ratio < 0.8:
        return pos
    return None


def _to_int(val):
    try:
        s = str(val).strip()
        if s == "":
            return None
        return int(s)
    except Exception:
        return None


def _source_season_episode(src: dict):
    season = _to_int((src or {}).get("season"))
    episode = _to_int((src or {}).get("episode"))
    if season is not None and episode is not None:
        return season, episode

    haystacks = [
        str((src or {}).get("title") or ""),
        str((src or {}).get("url") or ""),
        str((src or {}).get("play_url") or ""),
        str((src or {}).get("direct") or ""),
    ]

    for text in haystacks:
        if not text:
            continue
        m = re.search(r"[Ss]\s*(\d{1,3})\s*[Ee]\s*(\d{1,4})", text)
        if m:
            return int(m.group(1)), int(m.group(2))

    return season, episode


def _source_episode_keys(src: dict):
    season = _to_int((src or {}).get("season"))
    episode = _to_int((src or {}).get("episode"))
    if season is not None and episode is not None:
        return {(season, episode)}

    return set()


def _simple_status_bucket(row: dict):
    sources = (row or {}).get("sources") or {}
    latest_started = ""
    latest_finished = ""

    for src in sources.values():
        if not isinstance(src, dict):
            continue
        st = (src.get("status") or "").lower()
        if st == "finished":
            latest_finished = max(latest_finished, str(src.get("last_finished_at") or src.get("updated_at") or ""))
        if st == "started":
            latest_started = max(latest_started, str(src.get("last_started_at") or src.get("updated_at") or ""))

    if latest_started and latest_started > latest_finished:
        return "started"
    if latest_finished:
        return "finished"
    if latest_started:
        return "started"
    return ""


def _iter_filtered_sources(row: dict, provider: str = "", season=None, episode=None):
    provider = (provider or "").strip().lower()
    season = _to_int(season)
    episode = _to_int(episode)

    for src in ((row or {}).get("sources") or {}).values():
        if not isinstance(src, dict):
            continue
        st = (src.get("status") or "").strip().lower()
        if st not in ("started", "finished"):
            continue
        if provider and (src.get("provider") or "").strip().lower() != provider:
            continue
        if season is not None:
            keys = _source_episode_keys(src)
            if not keys or not any(src_key_season == season for src_key_season, _ in keys):
                continue
        if episode is not None:
            keys = _source_episode_keys(src)
            if not keys or (season, episode) not in keys:
                continue
        yield src


def _status_from_sources(sources) -> str:
    latest_started = ""
    latest_finished = ""

    for src in sources:
        st = (src.get("status") or "").strip().lower()
        if st == "finished":
            latest_finished = max(latest_finished, str(src.get("last_finished_at") or src.get("updated_at") or ""))
        if st == "started":
            latest_started = max(latest_started, str(src.get("last_started_at") or src.get("updated_at") or ""))

    if latest_started and latest_started > latest_finished:
        return "started"
    if latest_finished:
        return "finished"
    if latest_started:
        return "started"
    return ""


def get_provider_status_in_row(row: dict, provider: str, season=None, episode=None) -> str:
    if not isinstance(row, dict):
        return ""
    return _status_from_sources(_iter_filtered_sources(row, provider=provider, season=season, episode=episode))


def get_provider_status(tmdb_id: str, provider: str, season=None, episode=None) -> str:
    row = (playback_state_load() or {}).get(str(tmdb_id or "").strip())
    return get_provider_status_in_row(row, provider=provider, season=season, episode=episode)


def get_episode_status(tmdb_id: str, season, episode) -> str:
    row = (playback_state_load() or {}).get(str(tmdb_id or "").strip())
    if not isinstance(row, dict):
        return ""
    return _status_from_sources(_iter_filtered_sources(row, season=season, episode=episode))


def get_season_status(tmdb_id: str, season, total_episodes=None) -> str:
    row = (playback_state_load() or {}).get(str(tmdb_id or "").strip())
    if not isinstance(row, dict):
        return ""

    season_int = _to_int(season)
    total_episodes = _to_int(total_episodes)
    if season_int is None:
        return ""

    touched = set()
    finished_dates = {}
    started_dates = {}
    for src in _iter_filtered_sources(row, season=season_int):
        keys = {
            ep
            for src_season, ep in _source_episode_keys(src)
            if src_season == season_int
        }
        if not keys:
            continue
        touched.update(keys)
        if (src.get("status") or "").strip().lower() == "finished":
            when = str(src.get("last_finished_at") or src.get("updated_at") or "")
            for ep in keys:
                finished_dates[ep] = max(finished_dates.get(ep, ""), when)
        elif (src.get("status") or "").strip().lower() == "started":
            when = str(src.get("last_started_at") or src.get("updated_at") or "")
            for ep in keys:
                started_dates[ep] = max(started_dates.get(ep, ""), when)

    active_started = any(started_at > finished_dates.get(ep, "") for ep, started_at in started_dates.items())
    if total_episodes and len(finished_dates) >= total_episodes and not active_started:
        return "finished"
    if touched or active_started:
        return "started"
    return ""


def get_status_bucket(row: dict, total_episodes=None):
    media_type = str((row or {}).get("media_type") or "").strip().lower()
    tv_like = media_type in ("tv", "tvshow", "show", "series", "serial")
    if not tv_like:
        for src in ((row or {}).get("sources") or {}).values():
            if not isinstance(src, dict):
                continue
            keys = _source_episode_keys(src)
            src_season, src_episode = _source_season_episode(src)
            if keys or src_season is not None or src_episode is not None:
                tv_like = True
                break

    if tv_like:
        touched = set()
        finished_dates = {}
        started_dates = {}
        for src in _iter_filtered_sources(row):
            keys = _source_episode_keys(src)
            if not keys:
                continue
            touched.update(keys)
            if (src.get("status") or "").strip().lower() == "finished":
                when = str(src.get("last_finished_at") or src.get("updated_at") or "")
                for key in keys:
                    finished_dates[key] = max(finished_dates.get(key, ""), when)
            elif (src.get("status") or "").strip().lower() == "started":
                when = str(src.get("last_started_at") or src.get("updated_at") or "")
                for key in keys:
                    started_dates[key] = max(started_dates.get(key, ""), when)

        total = _to_int(total_episodes)
        if total is None:
            total = _to_int((row or {}).get("total_episodes"))

        active_started = any(started_at > finished_dates.get(key, "") for key, started_at in started_dates.items())
        if total and len(finished_dates) >= total and not active_started:
            return "finished"
        if touched or active_started:
            return "started"

    return _simple_status_bucket(row)


def get_item_status(tmdb_id: str) -> str:
    row = (playback_state_load() or {}).get(str(tmdb_id or "").strip())
    if not isinstance(row, dict):
        return ""
    return get_status_bucket(row)


def apply_status(li, status: str):
    status = (status or "").strip().lower()
    if not status:
        return None, 0

    try:
        icon_watched = xbmcgui.ICON_OVERLAY_WATCHED
        icon_partial = xbmcgui.ICON_OVERLAY_PARTIAL
    except Exception:
        icon_watched = 7
        icon_partial = 8

    if status == "finished":
        return icon_watched, 1

    if li is not None and status == "started":
        try:
            vit = li.getVideoInfoTag()
            vit.setResumePoint(1.0, 100.0)
        except Exception:
            try:
                li.setProperty("resumetime", "1")
                li.setProperty("totaltime", "100")
            except Exception:
                pass

    return icon_partial, 0


def apply_watch_state(li, tmdb_id):
    state = playback_state_load()
    row = state.get(str(tmdb_id or "").strip())
    if not isinstance(row, dict):
        return None, 0

    status = get_status_bucket(row)
    return apply_status(li, status)





def find_source_status_in_row(row: dict, provider: str = "", url: str = "", direct: str = "",
                              source_id: str = "", file_hash: str = "", title: str = "") -> str:
    row = row if isinstance(row, dict) else {}
    sources = row.get("sources") or {}

    exact_key = make_source_key(provider=provider, url=url, direct=direct, source_id=source_id, file_hash=file_hash)
    exact = sources.get(exact_key) or {}
    status = (exact.get("status") or "").strip().lower()
    if status:
        return status

    provider = (provider or "").strip().lower()
    source_id = (source_id or "").strip()
    file_hash = (file_hash or "").strip()
    url = (url or "").strip()
    direct = (direct or "").strip()
    title = (title or "").strip().lower()

    for src in sources.values():
        if not isinstance(src, dict):
            continue
        st = (src.get("status") or "").strip().lower()
        if not st:
            continue
        if provider and (src.get("provider") or "").strip().lower() != provider:
            continue
        if source_id and (src.get("source_id") or "").strip() == source_id:
            return st
        if file_hash and (src.get("file_hash") or "").strip() == file_hash:
            return st
        if direct and (src.get("direct") or "").strip() == direct:
            return st
        if url and (src.get("url") or "").strip() == url:
            return st
        if url and (src.get("play_url") or "").strip() == url:
            return st
        src_title = (src.get("title") or "").strip().lower()
        if title and src_title and title == src_title:
            return st

    return ""


def find_source_status(tmdb_id: str, provider: str = "", url: str = "", direct: str = "",
                       source_id: str = "", file_hash: str = "", title: str = "") -> str:
    state = playback_state_load() or {}
    row = state.get(str(tmdb_id or "").strip()) or {}
    return find_source_status_in_row(
        row,
        provider=provider,
        url=url,
        direct=direct,
        source_id=source_id,
        file_hash=file_hash,
        title=title,
    )


def iter_items_by_status(target_status: str):
    target_status = (target_status or "").strip().lower()
    out = []
    for tmdb_id, row in (playback_state_load() or {}).items():
        if not isinstance(row, dict):
            continue
        if get_status_bucket(row) != target_status:
            continue
        out.append((row.get("last_played_at") or "", tmdb_id, row))
    out.sort(key=lambda x: x[0], reverse=True)
    return out


def _kodi_url_with_headers(url: str, headers: dict = None) -> str:
    return playback_utils.kodi_url_with_headers(url, headers)

def _guess_stream_mimetype(url: str) -> str:
    return playback_utils.guess_stream_mimetype(url)


def _fmt_time_hms(seconds):
    try:
        s = int(seconds or 0)
    except Exception:
        s = 0
    if s < 0:
        s = 0
    h = s // 3600
    m = (s % 3600) // 60
    s = s % 60
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def ask_resume(tmdb_id: str, source_key: str):
    pos = get_resume_seconds(tmdb_id, source_key)
    if not pos or pos <= 0:
        return None

    choice = xbmcgui.Dialog().contextmenu([
        i18n.T(31878, "Continue from {time}").format(time=_fmt_time_hms(pos)),
        i18n.T(31879, "Play from beginning"),
    ])
    if choice == 0:
        return pos
    if choice == 1:
        return None
    return "__cancel__"

def _addon_bool(setting_id: str, default: bool = False) -> bool:
    try:
        val = (xbmcaddon.Addon().getSetting(setting_id) or "").strip().lower()
    except Exception:
        val = ""
    if not val:
        return default
    return val == "true"


def _addon_enum_index(setting_id: str, default: int = 0) -> int:
    try:
        raw = (xbmcaddon.Addon().getSetting(setting_id) or "").strip()
        return int(raw) if raw != "" else int(default)
    except Exception:
        return int(default)

def _addon_lang_choice(setting_id: str, default: str = "CZ") -> str:
    langs = ("CZ", "SK", "EN")
    default = (default or "CZ").strip().upper()
    if default not in langs:
        default = "CZ"
    idx = _addon_enum_index(setting_id, default=langs.index(default))
    if idx < 0 or idx >= len(langs):
        return default
    return langs[idx]

def _jsonrpc(method: str, params: dict = None):
    try:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params or {},
        }
        raw = xbmc.executeJSONRPC(json.dumps(payload))
        data = json.loads(raw or "{}")
        if data.get("error"):
            _log(f"jsonrpc {method} error: {data.get('error')}", xbmc.LOGWARNING)
            return None
        return data.get("result")
    except Exception as e:
        _log(f"jsonrpc {method} exception: {e}", xbmc.LOGWARNING)
        return None

def _active_video_player_id():
    players = _jsonrpc("Player.GetActivePlayers") or []
    for row in players:
        try:
            if str(row.get("type") or "").lower() == "video":
                return int(row.get("playerid"))
        except Exception:
            pass
    return None

def _stream_text(row: dict) -> str:
    if not isinstance(row, dict):
        return ""
    parts = []
    for key in ("language", "name", "title", "label"):
        value = row.get(key)
        if value not in (None, "", [], {}):
            parts.append(str(value))
    return " ".join(parts)

def _norm_stream_lang(text: str) -> str:
    try:
        import unicodedata
        text = "".join(
            ch for ch in unicodedata.normalize("NFKD", text or "")
            if not unicodedata.combining(ch)
        )
    except Exception:
        text = text or ""
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()

_STREAM_LANG_ALIASES = {
    "CZ": ("cz", "cs", "cze", "ces", "czech", "cesky", "cestina"),
    "SK": ("sk", "slo", "slk", "svk", "slovak", "slovensky", "slovencina"),
    "EN": ("en", "eng", "english"),
}

def _stream_has_lang(row: dict, lang: str) -> bool:
    text = _stream_text(row)
    if not text:
        return False
    lang = (lang or "").strip().upper()
    aliases = _STREAM_LANG_ALIASES.get(lang) or ()
    if not aliases:
        return False
    norm = _norm_stream_lang(text)
    compact = norm.replace(" ", "")
    if compact in aliases:
        return True
    return any(re.search(r"\b" + re.escape(alias) + r"\b", norm) for alias in aliases)

def _stream_lang_code(row: dict) -> str:
    for lang in ("CZ", "SK", "EN"):
        if _stream_has_lang(row, lang):
            return lang
    return ""

def _stream_index(row: dict):
    for key in ("index", "id"):
        try:
            value = row.get(key)
            if value not in (None, ""):
                return int(value)
        except Exception:
            pass
    return None

def _same_media_lang_group(audio_lang: str, subtitle_lang: str) -> bool:
    audio_lang = (audio_lang or "").upper()
    subtitle_lang = (subtitle_lang or "").upper()
    if not audio_lang or not subtitle_lang:
        return False
    if audio_lang in ("CZ", "SK") and subtitle_lang in ("CZ", "SK"):
        return True
    return audio_lang == subtitle_lang

def _disable_subtitles(player_id, props: dict, reason: str) -> str:
    if props.get("subtitleenabled"):
        result = _jsonrpc("Player.SetSubtitle", {
            "playerid": player_id,
            "subtitle": "off",
        })
        _log(f"auto preferred media: disabled subtitles reason={reason} result={result}", xbmc.LOGINFO)
        return f"subtitle-disabled-{reason}"
    return f"subtitle-off-{reason}"

def _open_online_subtitle_search(reason: str) -> str:
    return ""

def _select_preferred_audio_or_subtitle(monitor=None) -> str:
    if not _addon_bool("tmdb_auto_cz_audio_subtitles", default=True):
        return "disabled"

    primary_audio = _addon_lang_choice("tmdb_primary_audio_lang", default="CZ")
    secondary_audio = _addon_lang_choice("tmdb_secondary_audio_lang", default="SK")
    subtitle_lang = _addon_lang_choice("tmdb_preferred_subtitle_lang", default="CZ")
    audio_preferences = [primary_audio]
    if secondary_audio != primary_audio:
        audio_preferences.append(secondary_audio)

    player_id = None
    props = None
    for _ in range(12):
        if monitor and monitor.abortRequested():
            return "aborted"
        player_id = _active_video_player_id()
        if player_id is not None:
            props = _jsonrpc("Player.GetProperties", {
                "playerid": player_id,
                "properties": [
                    "audiostreams",
                    "currentaudiostream",
                    "subtitles",
                    "currentsubtitle",
                    "subtitleenabled",
                ],
            }) or {}
            if props.get("audiostreams") or props.get("subtitles"):
                break
        try:
            if monitor:
                monitor.waitForAbort(0.5)
            else:
                xbmc.sleep(500)
        except Exception:
            pass

    if player_id is None or not isinstance(props, dict):
        return "no-player"

    audio_streams = props.get("audiostreams") or []
    if isinstance(audio_streams, dict):
        audio_streams = [audio_streams]

    current_audio = props.get("currentaudiostream") or {}
    current_audio_lang = _stream_lang_code(current_audio)
    selected_audio_lang = current_audio_lang
    audio_result = "audio-kept"
    preferred_audio_found = False

    for wanted_lang in audio_preferences:
        if current_audio_lang == wanted_lang:
            selected_audio_lang = wanted_lang
            audio_result = f"audio-already-{wanted_lang.lower()}"
            preferred_audio_found = True
            break

        wanted_stream = None
        for stream in audio_streams:
            if isinstance(stream, dict) and _stream_has_lang(stream, wanted_lang):
                wanted_stream = stream
                break

        if not wanted_stream:
            continue

        idx = _stream_index(wanted_stream)
        if idx is None:
            continue
        result = _jsonrpc("Player.SetAudioStream", {
            "playerid": player_id,
            "stream": idx,
        })
        selected_audio_lang = wanted_lang
        audio_result = f"audio-{wanted_lang.lower()}-selected"
        preferred_audio_found = True
        _log(f"auto preferred media: selected {wanted_lang} audio stream index={idx} result={result}", xbmc.LOGINFO)
        break

    subtitles = props.get("subtitles") or []
    if isinstance(subtitles, dict):
        subtitles = [subtitles]

    if preferred_audio_found and _same_media_lang_group(selected_audio_lang, subtitle_lang):
        subtitle_result = _disable_subtitles(
            player_id,
            props,
            f"matching-{selected_audio_lang.lower()}-{subtitle_lang.lower()}",
        )
        return f"{audio_result}; {subtitle_result}"

    current_subtitle = props.get("currentsubtitle") or {}
    if props.get("subtitleenabled") and _stream_has_lang(current_subtitle, subtitle_lang):
        return f"{audio_result}; subtitle-already-{subtitle_lang.lower()}"

    for stream in subtitles:
        if not isinstance(stream, dict) or not _stream_has_lang(stream, subtitle_lang):
            continue
        idx = _stream_index(stream)
        if idx is None:
            continue
        result = _jsonrpc("Player.SetSubtitle", {
            "playerid": player_id,
            "subtitle": idx,
            "enable": True,
        })
        _log(f"auto preferred media: selected {subtitle_lang} subtitle index={idx} result={result}", xbmc.LOGINFO)
        return f"{audio_result}; subtitle-{subtitle_lang.lower()}-selected"

    subtitle_result = _disable_subtitles(
        player_id,
        props,
        f"no-{subtitle_lang.lower()}",
    )
    online_result = _open_online_subtitle_search(f"no-{subtitle_lang.lower()}")
    return f"{audio_result}; {subtitle_result}{online_result}"

def _auto_next_debug_enabled() -> bool:
    return _addon_bool("tmdb_series_next_debug", default=False)

def _auto_next_notify(message: str, level=None):
    _log(f"auto next: {message}", xbmc.LOGINFO)
    if not _auto_next_debug_enabled():
        return
    try:
        xbmcgui.Dialog().notification(
            i18n.T(31834, "Next episode"),
            str(message),
            level or xbmcgui.NOTIFICATION_INFO,
            2500,
            sound=False,
        )
    except Exception:
        pass

def _is_tmdb_serial_episode_source(source_meta: dict) -> bool:
    return (source_meta or {}).get("mode", "").strip() == "serial_episode"


def _auto_next_prefetch_start_sec(total: float) -> float:
    return auto_next_policy.prefetch_start_sec(total)


class _PlaybackObserver(xbmc.Player):
    def __init__(self):
        super().__init__()
        self.playback_started = False
        self.playback_ended = False
        self.playback_stopped = False
        self.playback_error = False

    def onAVStarted(self):
        self.playback_started = True
        self.playback_ended = False
        self.playback_stopped = False
        self.playback_error = False

    def onPlayBackStarted(self):
        self.playback_started = True
        self.playback_ended = False
        self.playback_stopped = False
        self.playback_error = False

    def onPlayBackEnded(self):
        self.playback_ended = True

    def onPlayBackStopped(self):
        self.playback_stopped = True

    def onPlayBackError(self):
        self.playback_error = True


def _prepare_next_episode_sync(tmdb_id: str, source_meta: dict, prefer_same_provider: bool):
    try:
        from resources.lib import TMDB_knihovna
        return TMDB_knihovna.prepare_next_tv_episode_playback(
            tmdb_id=tmdb_id,
            source_meta=source_meta or {},
            prefer_same_provider=prefer_same_provider,
        ) or {}
    except Exception as e:
        _log(f"auto next sync prepare error: {e}", xbmc.LOGWARNING)
        return {}


def _ensure_auto_next_play_url(result: dict) -> dict:
    result = dict(result or {})
    if result.get("play_url"):
        return result

    source = result.get("source") or {}
    if not isinstance(source, dict) or not source:
        return result

    try:
        from resources.lib import Hledani_zdroje
        play_url = Hledani_zdroje.build_tmdb_episode_source_play_url(
            row=source,
            tmdb_id=result.get("tmdb_id") or "",
            title=result.get("title") or "",
            original_title=result.get("original_title") or "",
            year=str(result.get("year") or ""),
            season=str(result.get("season") or ""),
            episode=str(result.get("episode") or ""),
            episode_title=result.get("episode_title") or "",
            season_name=result.get("season_name") or "",
            auto_next=True,
            auto_trace=result.get("auto_trace") or "",
        )
        if play_url:
            result["play_url"] = play_url
            _log("auto next: reconstructed missing play_url from source row", xbmc.LOGINFO)
    except Exception as e:
        _log(f"auto next rebuild play_url error: {e}", xbmc.LOGWARNING)
    return result


def _auto_next_result_has_any_target(result: dict) -> bool:
    result = result or {}
    return bool(
        result.get("play_url")
        or result.get("provider_search_url")
        or result.get("search_url")
    )

def _start_next_episode_prefetch(tmdb_id: str, source_meta: dict, prefer_same_provider: bool):
    state = {"done": False, "result": {}, "error": "", "cancel": False}

    def _worker():
        try:
            if state.get("cancel"):
                return
            _auto_next_notify(i18n.T(31919))
            from resources.lib import TMDB_knihovna
            result = TMDB_knihovna.prepare_next_tv_episode_playback(
                tmdb_id=tmdb_id,
                source_meta=source_meta or {},
                prefer_same_provider=prefer_same_provider,
            )
            if state.get("cancel"):
                return
            result = _ensure_auto_next_play_url(result or {})
            state["result"] = result or {}
            if _auto_next_result_has_any_target(result):
                season = result.get("season") or "?"
                episode = result.get("episode") or "?"
                if str(season).isdigit() and str(episode).isdigit():
                    _auto_next_notify(i18n.T(31920).format(season=int(season), episode=int(episode)))
                else:
                    _auto_next_notify(i18n.T(31921))
            else:
                _auto_next_notify(i18n.T(31922), xbmcgui.NOTIFICATION_WARNING)
        except Exception as e:
            state["error"] = str(e)
            _log(f"auto next prefetch error: {e}", xbmc.LOGWARNING)
            _auto_next_notify(i18n.T(31923), xbmcgui.NOTIFICATION_WARNING)
        finally:
            state["done"] = True

    try:
        t = threading.Thread(target=_worker)
        t.daemon = True
        t.start()
        state["thread"] = t
    except Exception as e:
        state["done"] = True
        state["error"] = str(e)
        _log(f"auto next thread error: {e}", xbmc.LOGWARNING)

    return state

def _cancel_next_episode_prefetch(prefetch_state: dict):
    if isinstance(prefetch_state, dict):
        prefetch_state["cancel"] = True

def _wait_for_prefetch(prefetch_state: dict, monitor, player, max_wait_sec: float = 8.0, require_playing: bool = True):
    waited = 0.0
    while (
        prefetch_state
        and not prefetch_state.get("cancel")
        and not prefetch_state.get("done")
        and waited < max_wait_sec
        and not monitor.abortRequested()
        and ((not require_playing) or player.isPlayingVideo())
    ):
        monitor.waitForAbort(0.5)
        waited += 0.5
    return _ensure_auto_next_play_url((prefetch_state or {}).get("result") or {})

class NextEpisodeDialog(xbmcgui.WindowXMLDialog):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.heading = kwargs.get("heading") or i18n.T(31924)
        self.message = kwargs.get("message") or ""
        self.timeout_sec = max(1, int(kwargs.get("timeout_sec") or 60))
        self.choice = False
        self.done = False

    def onInit(self):
        try:
            self.getControl(10).setLabel(self.heading)
        except Exception:
            pass
        try:
            self.getControl(41).setLabel(i18n.T(31994, "Continue"))
        except Exception:
            pass
        try:
            self.getControl(42).setLabel(i18n.T(31995, "Stop"))
        except Exception:
            pass
        try:
            self.getControl(20).setText(self.message)
        except Exception:
            try:
                self.getControl(20).setLabel(self.message)
            except Exception:
                pass

    def _update_countdown(self, seconds_left: int):
        try:
            self.getControl(30).setLabel(i18n.T(31925).format(time=_format_countdown(seconds_left)))
        except Exception:
            pass

    def onClick(self, controlId):
        if controlId == 41:
            self.choice = True
            self.done = True
        elif controlId == 42:
            self.choice = False
            self.done = True

    def onAction(self, action):
        try:
            action_id = action.getId()
        except Exception:
            action_id = None
        if action_id in (9, 10, 92, 216, 247, 257, 275, 61467):
            self.choice = False
            self.done = True
            return
        super().onAction(action)

def _format_countdown(seconds_left: int) -> str:
    total = max(0, int(seconds_left))
    minutes, seconds = divmod(total, 60)
    if minutes >= 60:
        hours, minutes = divmod(minutes, 60)
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def _auto_next_reached_end(cur: float, total: float) -> bool:
    return auto_next_policy.reached_end(cur, total)


def _confirm_next_episode(result: dict, timeout_sec: int) -> bool:
    season = result.get("season") or ""
    episode = result.get("episode") or ""
    episode_title = result.get("episode_title") or ""
    label = f"S{int(season):02d}E{int(episode):02d}" if str(season).isdigit() and str(episode).isdigit() else i18n.T(31924)
    if episode_title:
        label = f"{label} - {episode_title}"
    msg = label
    _auto_next_notify(i18n.T(31926).format(label=label))
    try:
        dlg = NextEpisodeDialog(
            "DialogNextEpisode.xml",
            xbmcaddon.Addon().getAddonInfo("path"),
            "Default",
            "720p",
            heading=i18n.T(31927),
            message=msg,
            timeout_sec=timeout_sec,
        )
        dlg.show()
        xbmc.sleep(150)

        monitor = xbmc.Monitor()
        remaining = max(1, int(timeout_sec))
        while remaining >= 0 and not monitor.abortRequested():
            try:
                dlg._update_countdown(remaining)
            except Exception:
                pass

            if getattr(dlg, "done", False):
                break

            if remaining == 0:
                break

            if monitor.waitForAbort(1.0):
                break
            remaining -= 1

        choice = bool(getattr(dlg, "choice", False)) if getattr(dlg, "done", False) else False
        try:
            dlg.close()
        except Exception:
            pass
        return choice
    except Exception as e:
        _log(f"auto next dialog error: {e}", xbmc.LOGWARNING)
        return False

def _run_plugin_delayed(plugin_url: str, delay_ms: int = 900):
    plugin_url = str(plugin_url or "").strip()
    if not plugin_url:
        return False

    try:
        delay_sec = max(0.0, float(delay_ms or 0) / 1000.0)
        monitor = xbmc.Monitor()
        if delay_sec and monitor.waitForAbort(delay_sec):
            return False
        xbmc.executebuiltin(f'RunPlugin("{plugin_url}")')
        return True
    except Exception as e:
        _log(f"auto next delayed run error: {e}", xbmc.LOGWARNING)
        return False


def _open_serial_episode_return_view_delayed(tmdb_id: str, meta: dict = None, source_meta: dict = None, delay_ms: int = 900):
    return False

def _open_next_episode_result(result: dict, auto_play: bool):
    play_url = (result or {}).get("play_url") or ""
    provider_search_url = (result or {}).get("provider_search_url") or ""
    search_url = (result or {}).get("search_url") or ""
    try:
        if play_url:
            if auto_play:
                return _run_plugin_delayed(play_url, delay_ms=250)
            xbmc.executebuiltin(f'RunPlugin("{play_url}")')
            return True
        if auto_play:
            _log("auto next: direct play_url not ready, not opening search listing", xbmc.LOGWARNING)
            return False
        if provider_search_url:
            xbmc.executebuiltin(f'Container.Update("{provider_search_url}")')
            return True
        if search_url:
            xbmc.executebuiltin(f'Container.Update("{search_url}")')
            return True
    except Exception as e:
        _log(f"auto next open error: {e}", xbmc.LOGWARNING)
    return False


def play_and_record_progress(tmdb_id: str, source_key: str, play_url: str, headers: dict = None,
                             meta: dict = None, resume_sec: float = None, source_meta: dict = None):
    tmdb_id = str(tmdb_id or "").strip()
    source_key = str(source_key or "").strip()
    if not tmdb_id or not source_key or not play_url:
        return False

    final_url = _kodi_url_with_headers(play_url, headers)
    display_title = (
        (source_meta or {}).get("display_title")
        or (meta or {}).get("title")
        or (source_meta or {}).get("title")
        or "Zdroj"
    )
    display_title = str(display_title or "Zdroj").strip()
    subtitle_title = str(
        (source_meta or {}).get("title")
        or (source_meta or {}).get("original_title")
        or (meta or {}).get("original_title")
        or ""
    ).strip()
    year = str((source_meta or {}).get("year") or (meta or {}).get("year") or "").strip()

    player = _PlaybackObserver()
    try:
        if str(play_url).startswith("plugin://"):
            xbmc.executebuiltin(f'PlayMedia("{final_url}")')
        else:
            li = playback_utils.make_video_list_item(
                display_title,
                final_url,
                subtitle_title=subtitle_title,
                year=year,
                mimetype=_guess_stream_mimetype(play_url),
            )
            player.play(final_url, li)
    except Exception as e:
        _log(f"play start exception: {e}", xbmc.LOGWARNING)
        try:
            xbmc.executebuiltin(f'PlayMedia("{final_url}")')
        except Exception as inner:
            _log(f"play start fallback exception: {inner}", xbmc.LOGERROR)
            return False

    return monitor_current_playback(
        tmdb_id=tmdb_id,
        source_key=source_key,
        meta=meta,
        resume_sec=resume_sec,
        source_meta=source_meta,
        player=player,
    )


def monitor_current_playback(tmdb_id: str, source_key: str, meta: dict = None,
                             resume_sec: float = None,
                             source_meta: dict = None,
                             player=None):
    tmdb_id = str(tmdb_id or "").strip()
    source_key = str(source_key or "").strip()
    if not tmdb_id or not source_key:
        return False

    monitor = xbmc.Monitor()
    player = player or _PlaybackObserver()
    started = False
    total = 0.0
    cur = 0.0
    auto_next_enabled = (
        _addon_bool("tmdb_series_next_enabled", default=True)
        and _is_tmdb_serial_episode_source(source_meta)
    )
    auto_next_mode = _addon_enum_index("tmdb_series_next_mode", default=0)
    auto_play_next = auto_next_mode == 0
    prefer_same_provider = _addon_bool("tmdb_series_next_prefer_same_provider", default=True)
    next_prefetch = None
    next_result_to_open = None
    next_action_allowed = False
    next_dialog_shown = False
    next_dialog_accepted = False
    next_dialog_declined = False
    next_prefetch_result = {}
    last_playback_pos = 0.0
    auto_next_seek_cooldown = 0
    last_checkpoint_time = 0.0
    last_checkpoint_ratio = 0.0
    checkpoint_finished_saved = False

    auto_next_source = str((source_meta or {}).get("auto_next") or "").strip() == "1"
    auto_next_trace = hashlib.sha1(
        f"{tmdb_id}|{(source_meta or {}).get('season')}|{(source_meta or {}).get('episode')}|{time.time()}".encode("utf-8")
    ).hexdigest()[:10]
    next_prepare_meta = dict(source_meta or {})
    next_prepare_meta["_auto_next_trace"] = auto_next_trace
    startup_checks = 40

    for i in range(startup_checks):
        if monitor.abortRequested():
            return False
        if getattr(player, "playback_error", False):
            break
        if player.isPlayingVideo():
            started = True
            try:
                total = float(player.getTotalTime() or 0.0)
            except Exception:
                total = 0.0

            if resume_sec is not None and resume_sec > 0:
                try:
                    player.seekTime(float(resume_sec))
                except Exception as e:
                    _log(f"seek error: {e}", xbmc.LOGERROR)
            break

        monitor.waitForAbort(0.5)

    if not started:
        reason = "playback-error" if getattr(player, "playback_error", False) else "startup-timeout"
        _log(
            f"playback did not start reason={reason} auto_next={int(auto_next_source)} "
            f"provider={(source_meta or {}).get('provider') or '-'}",
            xbmc.LOGWARNING,
        )
        try:
            mark_deleted(tmdb_id, source_key, meta=meta, source_meta=source_meta)
        except Exception as e:
            _log(f"failed source state save error: {e}", xbmc.LOGERROR)
        return False

    preferred_media_result = _select_preferred_audio_or_subtitle(monitor=monitor)
    _log(f"auto preferred media result: {preferred_media_result}", xbmc.LOGINFO)

    lost_video_checks = 0
    while not monitor.abortRequested():
        if (
            getattr(player, "playback_ended", False)
            or getattr(player, "playback_stopped", False)
            or getattr(player, "playback_error", False)
        ):
            break
        try:
            playing_video = bool(player.isPlayingVideo())
        except Exception:
            playing_video = False
        if not playing_video:
            lost_video_checks += 1
            if lost_video_checks >= 4:
                break
            monitor.waitForAbort(0.25)
            continue
        lost_video_checks = 0

        try:
            cur_tmp = float(player.getTime() or 0.0)
            tot_tmp = float(player.getTotalTime() or 0.0)
            if tot_tmp > 0:
                total = tot_tmp
            if cur_tmp >= 0:
                if last_playback_pos > 0 and abs(cur_tmp - last_playback_pos) > 30.0:
                    auto_next_seek_cooldown = 8
                cur = cur_tmp
                last_playback_pos = cur_tmp
        except Exception:
            pass

        if auto_next_seek_cooldown > 0:
            auto_next_seek_cooldown -= 1

        if (
            auto_next_enabled
            and total > 0
            and cur >= _auto_next_prefetch_start_sec(total)
            and auto_next_seek_cooldown <= 0
        ):
            if next_prefetch is None:
                next_prefetch = _start_next_episode_prefetch(
                    tmdb_id=tmdb_id,
                    source_meta=next_prepare_meta,
                    prefer_same_provider=prefer_same_provider,
                )

        ratio_live = (cur / total) if total > 0 else 0.0
        now_ts = time.time()
        if (
            total > 0
            and cur >= 30.0
            and ratio_live >= 0.1
            and (
                now_ts - last_checkpoint_time >= 60.0
                or ratio_live - last_checkpoint_ratio >= 0.05
                or (ratio_live >= 0.8 and not checkpoint_finished_saved)
            )
        ):
            checkpoint_playback(
                tmdb_id,
                source_key,
                pos_sec=cur,
                total_sec=total,
                ratio=ratio_live,
                meta=meta,
                source_meta=source_meta,
            )
            last_checkpoint_time = now_ts
            last_checkpoint_ratio = ratio_live
            if ratio_live >= 0.8:
                checkpoint_finished_saved = True

        if (
            auto_next_enabled
            and not auto_play_next
            and not next_dialog_shown
            and not next_dialog_declined
            and total > 0
            and ratio_live >= 0.8
        ):
            result = next_prefetch_result or {}
            if not result and next_prefetch is not None:
                result = _wait_for_prefetch(
                    next_prefetch,
                    monitor=monitor,
                    player=player,
                    max_wait_sec=6.0,
                    require_playing=True,
                )
                if result:
                    next_prefetch_result = result

            if _auto_next_result_has_any_target(result):
                remaining = max(0.0, total - cur)
                timeout_sec = max(1, int(max(0.0, remaining - 3.0)))
                next_dialog_shown = True
                if _confirm_next_episode(result, timeout_sec=timeout_sec):
                    next_dialog_accepted = True
                    next_result_to_open = result
                    next_action_allowed = True
                else:
                    next_dialog_declined = True
            elif next_prefetch and next_prefetch.get("done"):
                next_dialog_shown = True
                next_dialog_declined = True

        monitor.waitForAbort(1.0)

    ratio = (cur / total) if total > 0 else 0.0
    ended_event = bool(getattr(player, "playback_ended", False))
    stopped_event = bool(getattr(player, "playback_stopped", False))
    error_event = bool(getattr(player, "playback_error", False))
    natural_end = auto_next_policy.is_natural_end(
        ended=ended_event,
        stopped=stopped_event,
        playback_error=error_event,
        aborted=monitor.abortRequested(),
        cur=cur,
        total=total,
    )
    end_reason = "ended" if ended_event else "stopped" if stopped_event else "error" if error_event else "lost"
    _log(
        f"playback exit reason={end_reason} natural={int(natural_end)} "
        f"cur={cur:.1f} total={total:.1f} ratio={ratio:.3f} trace={auto_next_trace}",
        xbmc.LOGINFO,
    )

    try:
        if natural_end or ratio >= 0.8:
            mark_finished(tmdb_id, source_key, pos_sec=cur, total_sec=total, ratio=ratio, meta=meta, source_meta=source_meta)
        elif ratio >= 0.1:
            mark_started(tmdb_id, source_key, pos_sec=cur, total_sec=total, ratio=ratio, meta=meta, source_meta=source_meta)
    except Exception as e:
        _log(f"final playback state save error: {e}", xbmc.LOGERROR)

    if auto_next_enabled:
        if natural_end:
            next_action_allowed = True
        else:
            _cancel_next_episode_prefetch(next_prefetch)
            if next_action_allowed or next_result_to_open:
                _auto_next_notify(i18n.T(31928))
            next_action_allowed = False
            next_result_to_open = None
            _open_serial_episode_return_view_delayed(tmdb_id, meta=meta, source_meta=source_meta, delay_ms=1200)
            _log(
                f"auto next: canceled reason={end_reason} cur={cur:.1f} "
                f"total={total:.1f} ratio={ratio:.3f} trace={auto_next_trace}",
                xbmc.LOGINFO,
            )

    if next_action_allowed:
        result = _ensure_auto_next_play_url(next_prefetch_result or {})
        if next_prefetch is not None:
            result = _wait_for_prefetch(
                next_prefetch,
                monitor=monitor,
                player=player,
                max_wait_sec=15.0,
                require_playing=False,
            )
            if result:
                next_prefetch_result = result
        needs_sync_prepare = (
            (auto_play_next and not (result and result.get("play_url")))
            or ((not auto_play_next) and not _auto_next_result_has_any_target(result))
        )
        prefetch_finished = next_prefetch is None or bool(next_prefetch.get("done"))
        if needs_sync_prepare and prefetch_finished:
            _log(
                f"auto next: missing ready result after prefetch, trying sync prepare "
                f"trace={auto_next_trace}",
                xbmc.LOGINFO,
            )
            sync_result = _ensure_auto_next_play_url(_prepare_next_episode_sync(
                tmdb_id=tmdb_id,
                source_meta=next_prepare_meta,
                prefer_same_provider=prefer_same_provider,
            ))
            if sync_result:
                result = sync_result
                next_prefetch_result = sync_result
        elif needs_sync_prepare:
            _log(
                f"auto next: prefetch still running after wait, duplicate sync skipped "
                f"trace={auto_next_trace}",
                xbmc.LOGWARNING,
            )
        if auto_play_next and not (result and result.get("play_url")):
            _cancel_next_episode_prefetch(next_prefetch)
            _auto_next_notify(i18n.T(31929), xbmcgui.NOTIFICATION_WARNING)
            result = {}
        if _auto_next_result_has_any_target(result):
            if auto_play_next:
                next_result_to_open = result
            else:
                if next_dialog_accepted:
                    next_result_to_open = result
                elif (not next_dialog_shown) and (not next_dialog_declined):
                    if _confirm_next_episode(result, timeout_sec=60):
                        next_result_to_open = result
        elif auto_next_enabled:
            _auto_next_notify(i18n.T(31930), xbmcgui.NOTIFICATION_WARNING)

    if next_result_to_open:
        opened = _open_next_episode_result(next_result_to_open, auto_play=auto_play_next)
        _log(
            f"auto next: transition scheduled={int(bool(opened))} trace={auto_next_trace}",
            xbmc.LOGINFO if opened else xbmc.LOGWARNING,
        )

    return True
