# -*- coding: utf-8 -*-
import copy
import hashlib
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import xbmc

from resources.lib import json_storage, tmdb_playback_queue, tmdb_playback_state
from resources.lib import trakt_client


_TRAKT_STATE_CACHE = None
_TRAKT_STATE_CACHE_TS = 0.0
_TRAKT_STATE_CACHE_TTL = 60.0
_TRAKT_STATE_CACHE_LOCK = threading.RLock()
_LOCAL_TRAKT_SYNC_TTL = 300.0
_LOCAL_TRAKT_SYNC_RUNNING = False
_OUTBOX_FLUSH_LOCK = threading.RLock()
_HISTORY_SYNC_CHUNK_SIZE = 100
_REMOTE_HISTORY_FULL_SYNC_INTERVAL = 24 * 60 * 60
_REMOTE_HISTORY_MAX_PAGES = 500
_STATUS_INDEX_SCHEMA = 1
_STATUS_INDEX_STATUS_KEY = "_playback_status"
_STATUS_INDEX_UPDATED_KEY = "_playback_status_at"


def _log(msg, level=xbmc.LOGINFO):
    try:
        xbmc.log(f"[IKARUS][TMDB PLAYBACK SYNC] {msg}", level)
    except Exception:
        pass


def _str(value):
    return str(value or "").strip()


def _to_int(value):
    try:
        text = _str(value)
        if not text:
            return None
        return int(text)
    except Exception:
        return None


def _to_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default


def _sync_meta_path() -> str:
    return os.path.join(os.path.dirname(tmdb_playback_state.playback_state_path()), "tmdb_playback_trakt_sync.json")


def _snapshot_path() -> str:
    return tmdb_playback_state.playback_trakt_snapshot_path()


def _outbox_path() -> str:
    return os.path.join(os.path.dirname(tmdb_playback_state.playback_state_path()), "tmdb_playback_trakt_outbox.json")


def _status_index_path() -> str:
    return os.path.join(os.path.dirname(tmdb_playback_state.playback_state_path()), "tmdb_playback_trakt_index.json")


def _file_signature(path: str):
    try:
        stat = os.stat(path)
        return {
            "size": int(stat.st_size),
            "mtime_ns": int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1000000000))),
        }
    except OSError:
        return None


def _status_index_dependencies() -> dict:
    return {
        "snapshot": _file_signature(_snapshot_path()),
        "outbox": _file_signature(_outbox_path()),
    }


def _status_row_updated_at(row: dict) -> str:
    values = [
        _str((row or {}).get("last_played_at")),
        _str((row or {}).get("updated_at")),
    ]
    sources = (row or {}).get("sources")
    if isinstance(sources, dict):
        for src in sources.values():
            if not isinstance(src, dict):
                continue
            values.extend((
                _str(src.get("last_started_at")),
                _str(src.get("last_finished_at")),
                _str(src.get("updated_at")),
            ))
    return max(values or [""])


def _compact_status_row(tmdb_id: str, row: dict, status: str = "") -> dict:
    if not isinstance(row, dict):
        return {}
    status = _str(status or tmdb_playback_state.get_status_bucket(row)).lower()
    if status not in ("started", "finished"):
        return {}
    compact = {
        key: copy.deepcopy(value)
        for key, value in row.items()
        if key != "sources" and key not in (_STATUS_INDEX_STATUS_KEY, _STATUS_INDEX_UPDATED_KEY)
    }
    compact["tmdb_id"] = _str(compact.get("tmdb_id") or tmdb_id)
    if not _str(compact.get("media_type")):
        media_type = ""
        sources = row.get("sources") if isinstance(row.get("sources"), dict) else {}
        for src in sources.values():
            if not isinstance(src, dict):
                continue
            mode = _str(src.get("mode")).lower()
            if mode in ("tv", "tvshow", "show", "series", "serial", "episode", "serial_episode"):
                media_type = "tv"
                break
            if src.get("season") not in (None, "") or src.get("episode") not in (None, ""):
                media_type = "tv"
                break
            if mode == "movie":
                media_type = "movie"
        if media_type:
            compact["media_type"] = media_type
    compact[_STATUS_INDEX_STATUS_KEY] = status
    compact[_STATUS_INDEX_UPDATED_KEY] = _status_row_updated_at(row)
    return compact


def _build_status_index_rows(state: dict) -> dict:
    rows = {}
    for tmdb_id, row in (state or {}).items():
        compact = _compact_status_row(_str(tmdb_id), row)
        if compact:
            rows[_str(tmdb_id)] = compact
    return rows


def _status_index_valid(data: dict, dependencies: dict) -> bool:
    return bool(
        isinstance(data, dict)
        and data.get("schema") == _STATUS_INDEX_SCHEMA
        and data.get("dependencies") == dependencies
        and isinstance(data.get("rows"), dict)
    )


def _write_status_index(rows: dict, dependencies: dict) -> bool:
    path = _status_index_path()
    try:
        with json_storage.file_lock(path):
            if _status_index_dependencies() != dependencies:
                return False
            json_storage.write_json(
                path,
                {
                    "schema": _STATUS_INDEX_SCHEMA,
                    "dependencies": dependencies,
                    "rows": rows if isinstance(rows, dict) else {},
                },
                keep_backup=False,
            )
        return True
    except Exception as e:
        _log(f"status index save error: {e}", xbmc.LOGWARNING)
        return False


def _rebuild_status_index() -> dict:
    rows = {}
    for _ in range(3):
        dependencies = _status_index_dependencies()
        state = _snapshot_load()
        rows = _build_status_index_rows(state)
        if dependencies != _status_index_dependencies():
            continue
        if _write_status_index(rows, dependencies):
            return rows
    return rows


def _status_index_rows(force_rebuild: bool = False) -> dict:
    dependencies = _status_index_dependencies()
    if not force_rebuild:
        try:
            data = json_storage.read_json(_status_index_path(), {}, expected_type=dict)
            if _status_index_valid(data, dependencies):
                return data.get("rows") or {}
        except Exception as e:
            _log(f"status index load error: {e}", xbmc.LOGWARNING)
    return _rebuild_status_index()


def _sync_meta_load() -> dict:
    path = _sync_meta_path()
    try:
        return json_storage.read_json(path, {}, expected_type=dict)
    except Exception:
        return {}


def _sync_meta_save(data: dict):
    try:
        path = _sync_meta_path()

        def _update(existing):
            merged = dict(existing) if isinstance(existing, dict) else {}
            merged.update(data if isinstance(data, dict) else {})
            return merged

        json_storage.update_json(path, {}, _update, expected_type=dict, indent=2)
    except Exception as e:
        _log(f"sync meta save error: {e}", xbmc.LOGWARNING)


def _snapshot_load() -> dict:
    try:
        state = json_storage.read_json(_snapshot_path(), {}, expected_type=dict)
    except Exception as e:
        _log(f"snapshot load error: {e}", xbmc.LOGWARNING)
        state = {}
    try:
        return tmdb_playback_queue.filter_remote_state(state, tmdb_playback_queue.load(_outbox_path()))
    except Exception as e:
        _log(f"snapshot tombstone filter error: {e}", xbmc.LOGWARNING)
        return state if isinstance(state, dict) else {}


def _snapshot_save(state: dict):
    path = _snapshot_path()
    with json_storage.file_lock(path):
        json_storage.write_json(
            path,
            state if isinstance(state, dict) else {},
            indent=2,
            keep_backup=True,
        )
    try:
        _status_index_rows(force_rebuild=True)
    except Exception as e:
        _log(f"status index rebuild error: {e}", xbmc.LOGWARNING)


def queue_progress_status(media_type: str, tmdb_id, progress, season=None, episode=None,
                          event_at: str = "") -> str:
    return tmdb_playback_queue.enqueue_progress(
        _outbox_path(),
        media_type,
        tmdb_id,
        progress,
        season=season,
        episode=episode,
        event_at=event_at,
    )


def queue_history_status(media_type: str, tmdb_id, season=None, episode=None,
                         watched_at: str = "") -> str:
    return tmdb_playback_queue.enqueue_history(
        _outbox_path(),
        media_type,
        tmdb_id,
        season=season,
        episode=episode,
        event_at=watched_at,
    )


def queue_history_items(movies=None, episodes=None) -> list:
    return tmdb_playback_queue.enqueue_history_items(
        _outbox_path(),
        movies=movies,
        episodes=episodes,
    )


def queue_clear_status(media_type: str, tmdb_id, season=None, episode=None) -> str:
    global _TRAKT_STATE_CACHE, _TRAKT_STATE_CACHE_TS
    operation_id = tmdb_playback_queue.enqueue_clear(
        _outbox_path(),
        media_type,
        tmdb_id,
        season=season,
        episode=episode,
    )
    with _TRAKT_STATE_CACHE_LOCK:
        _TRAKT_STATE_CACHE = None
        _TRAKT_STATE_CACHE_TS = 0.0
    return operation_id


def operation_pending(operation_id: str) -> bool:
    return tmdb_playback_queue.operation_pending(_outbox_path(), operation_id)


def pending_operation_count() -> int:
    return tmdb_playback_queue.pending_count(_outbox_path())


def _operation_history_payload(row: dict) -> dict:
    payload = {
        "tmdb_id": _str(row.get("tmdb_id")),
        "watched_at": _str(row.get("event_at")),
    }
    if _str(row.get("media_type")).lower() == "episode":
        payload["season"] = _to_int(row.get("season"))
        payload["episode"] = _to_int(row.get("episode"))
    return payload


def _flush_pending_operations_inner(limit: int = 200) -> dict:
    result = {"sent": 0, "pending": pending_operation_count(), "errors": []}
    try:
        if not trakt_client.is_connected():
            return result
    except Exception as e:
        result["errors"].append(repr(e))
        return result

    with _OUTBOX_FLUSH_LOCK:
        operations = tmdb_playback_queue.due_operations(_outbox_path(), limit=max(1, int(limit or 1)))
        if not operations:
            result["pending"] = pending_operation_count()
            return result

        clear_rows = [row for row in operations if row.get("kind") == "clear"]
        history_rows = [row for row in operations if row.get("kind") == "history"]
        progress_rows = [row for row in operations if row.get("kind") == "progress"]

        for row in clear_rows[:20]:
            operation_id = _str(row.get("id"))
            try:
                clear_result = trakt_client.clear_sync_item(
                    row.get("media_type"),
                    row.get("tmdb_id"),
                    season=row.get("season"),
                    episode=row.get("episode"),
                )
                errors = clear_result.get("errors") if isinstance(clear_result, dict) else ["invalid clear response"]
                if errors:
                    raise RuntimeError("; ".join(str(value) for value in errors))
                tmdb_playback_queue.mark_clears_sent(_outbox_path(), [operation_id])
                result["sent"] += 1
            except Exception as e:
                tmdb_playback_queue.mark_failed(_outbox_path(), [operation_id], repr(e))
                result["errors"].append(repr(e))
                _log(f"queued clear failed id={operation_id}: {e}", xbmc.LOGWARNING)

        for start in range(0, len(history_rows), _HISTORY_SYNC_CHUNK_SIZE):
            chunk = history_rows[start:start + _HISTORY_SYNC_CHUNK_SIZE]
            movie_rows = []
            episode_rows = []
            for row in chunk:
                payload = _operation_history_payload(row)
                if _str(row.get("media_type")).lower() == "movie":
                    movie_rows.append(payload)
                elif _str(row.get("media_type")).lower() == "episode":
                    episode_rows.append(payload)
            operation_ids = [_str(row.get("id")) for row in chunk if _str(row.get("id"))]
            if not operation_ids:
                continue
            try:
                trakt_client.add_history_items(
                    movies=movie_rows,
                    episodes=episode_rows,
                    replace_existing=True,
                )
                tmdb_playback_queue.mark_completed(_outbox_path(), operation_ids)
                result["sent"] += len(operation_ids)
            except Exception as e:
                tmdb_playback_queue.mark_failed(_outbox_path(), operation_ids, repr(e))
                result["errors"].append(repr(e))
                _log(f"queued history batch failed count={len(operation_ids)}: {e}", xbmc.LOGWARNING)
                break

        for row in progress_rows[:10]:
            operation_id = _str(row.get("id"))
            try:
                trakt_client.scrobble_status(
                    media_type=row.get("media_type"),
                    tmdb_id=row.get("tmdb_id"),
                    progress=_to_float(row.get("progress"), default=0.0),
                    status="pause",
                    season=row.get("season"),
                    episode=row.get("episode"),
                )
                tmdb_playback_queue.mark_completed(_outbox_path(), [operation_id])
                result["sent"] += 1
            except Exception as e:
                tmdb_playback_queue.mark_failed(_outbox_path(), [operation_id], repr(e))
                result["errors"].append(repr(e))
                _log(f"queued progress failed id={operation_id}: {e}", xbmc.LOGWARNING)

        result["pending"] = pending_operation_count()
        return result


def flush_pending_operations(limit: int = 200) -> dict:
    try:
        with json_storage.file_lock(
            _outbox_path() + ".flush",
            timeout=1.0,
            stale_after=120.0,
        ):
            return _flush_pending_operations_inner(limit=limit)
    except TimeoutError:
        return {
            "sent": 0,
            "pending": pending_operation_count(),
            "errors": [],
            "busy": True,
        }


def flush_pending_and_refresh(limit: int = 200) -> dict:
    first = flush_pending_operations(limit=limit)
    try:
        if not trakt_client.is_connected():
            return first
    except Exception:
        return first

    trakt_state(force=True)
    second = flush_pending_operations(limit=limit)
    if int(second.get("sent") or 0) > 0:
        trakt_state(force=True)
    return {
        "sent": int(first.get("sent") or 0) + int(second.get("sent") or 0),
        "pending": pending_operation_count(),
        "errors": list(first.get("errors") or []) + list(second.get("errors") or []),
        "busy": bool(first.get("busy") or second.get("busy")),
    }


def _hash_json(data) -> str:
    try:
        raw = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        raw = repr(data)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _date_from_row(row: dict, fallback: str = "") -> str:
    for key in ("paused_at", "last_watched_at", "watched_at", "listed_at", "updated_at"):
        value = _str((row or {}).get(key))
        if value:
            return value
    return fallback


def _source_updated_at(src: dict) -> str:
    return max(
        _str((src or {}).get("last_started_at")),
        _str((src or {}).get("last_finished_at")),
        _str((src or {}).get("last_deleted_at")),
    )


def _source_progress(src: dict, status: str) -> float:
    if status == "finished":
        return 100.0
    ratio = _to_float((src or {}).get("last_ratio"), default=0.0)
    if ratio <= 0.0:
        pos = _to_float((src or {}).get("last_position_sec"), default=0.0)
        total = _to_float((src or {}).get("last_duration_sec"), default=0.0)
        ratio = (pos / total) if total > 0 else 0.0
    progress = max(0.0, min(95.0, ratio * 100.0))
    return progress if progress > 0 else 1.0


def _media_meta(media: dict, media_type: str) -> dict:
    ids = media.get("ids") if isinstance(media.get("ids"), dict) else {}
    tmdb_id = _str(ids.get("tmdb"))
    title = _str(media.get("title"))
    year = _str(media.get("year"))
    rating = media.get("rating")
    meta = {
        "tmdb_id": tmdb_id,
        "title": title,
        "original_title": _str(media.get("original_title")),
        "year": year,
        "rating": rating,
        "media_type": media_type,
    }
    if media_type in ("tv", "show", "series"):
        meta["media_type"] = "tv"
        total = media.get("aired_episodes") or media.get("episode_count") or media.get("episodes")
        total_i = _to_int(total)
        if total_i is not None:
            meta["total_episodes"] = total_i
    return meta


_LOCAL_LATIN_CHARS = (
    "\u00c1\u00e1\u010c\u010d\u010e\u010f\u00c9\u00e9\u011a\u011b"
    "\u00cd\u00ed\u0147\u0148\u00d3\u00f3\u0158\u0159\u0160\u0161"
    "\u0164\u0165\u00da\u00fa\u016e\u016f\u00dd\u00fd\u017d\u017e"
    "\u013d\u013e\u0139\u013a\u0154\u0155\u00d4\u00f4"
    "\u00c4\u00e4\u00d6\u00f6\u00dc\u00fc"
)


def _has_local_latin_chars(text: str) -> bool:
    return any(ch in _LOCAL_LATIN_CHARS for ch in _str(text))


def _repair_protected_local_title(row: dict):
    if not isinstance(row, dict) or not row.get("_title_is_local"):
        return
    title = _str(row.get("title"))
    original = _str(row.get("original_title"))
    if title and original and title.casefold() != original.casefold():
        if _has_local_latin_chars(original) and not _has_local_latin_chars(title):
            row["title"] = original


def _has_protected_local_title(row: dict) -> bool:
    if not isinstance(row, dict) or not row.get("_title_is_local"):
        return False
    title = _str(row.get("title"))
    if not title:
        return False
    original = _str(row.get("original_title"))
    if original and title.casefold() != original.casefold():
        if _has_local_latin_chars(original) and not _has_local_latin_chars(title):
            return False
        if not row.get("_title_source_tmdb_detail") and not _has_local_latin_chars(title) and not _has_local_latin_chars(original):
            return False
    return True


def _ensure_row(state: dict, tmdb_id: str, meta: dict) -> dict:
    tmdb_id = _str(tmdb_id)
    row = state.get(tmdb_id)
    if not isinstance(row, dict):
        row = {}
    row["tmdb_id"] = tmdb_id
    row["sources"] = row.get("sources") if isinstance(row.get("sources"), dict) else {}
    _repair_protected_local_title(row)
    protected_title = _has_protected_local_title(row)
    for key in ("title", "original_title", "year", "poster", "plot", "rating", "media_type", "total_episodes", "total_seasons", "_title_is_local", "_title_source_tmdb_detail", "_local_origin"):
        value = (meta or {}).get(key)
        if value in (None, ""):
            continue
        if key == "title" and protected_title:
            continue
        if key == "original_title" and protected_title and _has_local_latin_chars(row.get("original_title")):
            continue
        if key in ("_title_is_local", "_title_source_tmdb_detail") and not value:
            continue
        row[key] = value
    state[tmdb_id] = row
    return row


def _set_row_dates(row: dict, when: str):
    when = _str(when)
    if not when:
        return
    if not _str(row.get("last_played_at")) or when > _str(row.get("last_played_at")):
        row["last_played_at"] = when
    if not _str(row.get("updated_at")) or when > _str(row.get("updated_at")):
        row["updated_at"] = when


def _add_movie_source(state: dict, movie: dict, status: str, when: str, progress: float = 100.0):
    meta = _media_meta(movie or {}, "movie")
    tmdb_id = _str(meta.get("tmdb_id"))
    if not tmdb_id:
        return
    row = _ensure_row(state, tmdb_id, meta)
    ratio = max(0.0, min(1.0, float(progress or 0.0) / 100.0))
    source_key = tmdb_playback_state.make_source_key(
        provider="trakt",
        url=f"trakt://movie/{tmdb_id}",
        source_id=f"trakt_{status}",
    )
    row["sources"][source_key] = {
        "provider": "trakt",
        "source_id": f"trakt_{status}",
        "url": f"trakt://movie/{tmdb_id}",
        "title": "Trakt.TV",
        "mode": "movie",
        "status": status,
        "last_position_sec": ratio,
        "last_duration_sec": 1.0,
        "last_ratio": ratio,
    }
    if status == "finished":
        row["sources"][source_key]["last_finished_at"] = when
    else:
        row["sources"][source_key]["last_started_at"] = when
    _set_row_dates(row, when)


def _add_episode_source(state: dict, show: dict, season, episode, status: str, when: str,
                        progress: float = 100.0, episode_title: str = ""):
    meta = _media_meta(show or {}, "tv")
    tmdb_id = _str(meta.get("tmdb_id"))
    season_i = _to_int(season)
    episode_i = _to_int(episode)
    if not tmdb_id or season_i is None or episode_i is None:
        return
    row = _ensure_row(state, tmdb_id, meta)
    ratio = max(0.0, min(1.0, float(progress or 0.0) / 100.0))
    source_id = f"trakt_{status}_s{season_i:03d}e{episode_i:04d}"
    url = f"trakt://show/{tmdb_id}/season/{season_i}/episode/{episode_i}"
    source_key = tmdb_playback_state.make_source_key(
        provider="trakt",
        url=url,
        source_id=source_id,
    )
    row["sources"][source_key] = {
        "provider": "trakt",
        "source_id": source_id,
        "url": url,
        "title": episode_title or f"Trakt.TV S{season_i:02d}E{episode_i:02d}",
        "mode": "serial_episode",
        "season": str(season_i),
        "episode": str(episode_i),
        "episode_title": episode_title,
        "status": status,
        "last_position_sec": ratio,
        "last_duration_sec": 1.0,
        "last_ratio": ratio,
    }
    if status == "finished":
        row["sources"][source_key]["last_finished_at"] = when
    else:
        row["sources"][source_key]["last_started_at"] = when
    _set_row_dates(row, when)


def _merge_watched_movies(state: dict):
    for row in trakt_client.fetch_watched("movies"):
        if not isinstance(row, dict):
            continue
        movie = row.get("movie") if isinstance(row.get("movie"), dict) else {}
        when = _date_from_row(row)
        _add_movie_source(state, movie, "finished", when, progress=100.0)


def _merge_watched_shows(state: dict):
    for row in trakt_client.fetch_watched("shows"):
        if not isinstance(row, dict):
            continue
        show = row.get("show") if isinstance(row.get("show"), dict) else {}
        when = _date_from_row(row)
        seasons = row.get("seasons") if isinstance(row.get("seasons"), list) else []
        for season_row in seasons:
            if not isinstance(season_row, dict):
                continue
            season = season_row.get("number")
            episodes = season_row.get("episodes") if isinstance(season_row.get("episodes"), list) else []
            for episode_row in episodes:
                if not isinstance(episode_row, dict):
                    continue
                episode = episode_row.get("number")
                ep_when = _date_from_row(episode_row, fallback=when)
                _add_episode_source(state, show, season, episode, "finished", ep_when, progress=100.0)


def _watched_show_episode_rows() -> list:
    out = []
    for row in trakt_client.fetch_watched("shows"):
        if not isinstance(row, dict):
            continue
        show = row.get("show") if isinstance(row.get("show"), dict) else {}
        meta = _media_meta(show, "tv")
        tmdb_id = _str(meta.get("tmdb_id"))
        show_title = _str(meta.get("title"))
        when = _date_from_row(row)
        if not tmdb_id:
            continue
        seasons = row.get("seasons") if isinstance(row.get("seasons"), list) else []
        for season_row in seasons:
            if not isinstance(season_row, dict):
                continue
            season = _to_int(season_row.get("number"))
            episodes = season_row.get("episodes") if isinstance(season_row.get("episodes"), list) else []
            for episode_row in episodes:
                if not isinstance(episode_row, dict):
                    continue
                episode = _to_int(episode_row.get("number"))
                if season is None or episode is None:
                    continue
                ep_when = _date_from_row(episode_row, fallback=when)
                episode_title = _str(episode_row.get("title"))
                ep_code = f"S{season:02d}E{episode:02d}"
                display_title = f"{show_title} - {ep_code}"
                if episode_title:
                    display_title = f"{display_title} {episode_title}"
                item = dict(meta)
                item.update({
                    "tmdb_id": tmdb_id,
                    "media_type": "tv",
                    "title": display_title,
                    "display_title": display_title,
                    "show_title": show_title,
                    "season": str(season),
                    "episode": str(episode),
                    "episode_title": episode_title,
                    "last_played_at": ep_when,
                    "updated_at": ep_when,
                    "sources": {},
                })
                _add_episode_source(
                    {tmdb_id: item},
                    show,
                    season,
                    episode,
                    "finished",
                    ep_when,
                    progress=100.0,
                    episode_title=episode_title,
                )
                key = f"{tmdb_id}:s{season:03d}e{episode:04d}"
                out.append((ep_when, key, item))
    out.sort(key=lambda item: item[0], reverse=True)
    return out


def _merge_playback(state: dict):
    for row in trakt_client.fetch_playback():
        if not isinstance(row, dict):
            continue
        progress = _to_float(row.get("progress"), default=0.0)
        when = _date_from_row(row)
        media_type = _str(row.get("type")).lower()
        if media_type == "movie" and isinstance(row.get("movie"), dict):
            _add_movie_source(state, row.get("movie"), "started", when, progress=progress)
        elif media_type == "episode":
            show = row.get("show") if isinstance(row.get("show"), dict) else {}
            episode_data = row.get("episode") if isinstance(row.get("episode"), dict) else {}
            _add_episode_source(
                state,
                show,
                episode_data.get("season"),
                episode_data.get("number"),
                "started",
                when,
                progress=progress,
                episode_title=_str(episode_data.get("title")),
            )


def _utc_iso(timestamp=None) -> str:
    try:
        timestamp = float(timestamp if timestamp is not None else time.time())
    except Exception:
        timestamp = time.time()
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _remote_source_count(state: dict, status: str = "") -> int:
    status = _str(status).lower()
    count = 0
    for row in (state or {}).values():
        if not isinstance(row, dict):
            continue
        for source in (row.get("sources") or {}).values():
            if not isinstance(source, dict):
                continue
            if _str(source.get("provider")).lower() != "trakt":
                continue
            if status and _str(source.get("status")).lower() != status:
                continue
            count += 1
    return count


def _finished_remote_state(state: dict) -> dict:
    out = {}
    for tmdb_id, row in (state or {}).items():
        if not isinstance(row, dict):
            continue
        sources = {
            key: copy.deepcopy(source)
            for key, source in (row.get("sources") or {}).items()
            if isinstance(source, dict)
            and _str(source.get("provider")).lower() == "trakt"
            and _str(source.get("status")).lower() == "finished"
        }
        if not sources:
            continue
        saved_row = copy.deepcopy(row)
        saved_row["sources"] = sources
        out[_str(tmdb_id)] = saved_row
    return out


def _merge_history_rows(state: dict, rows: list) -> int:
    parsed = 0
    # Process oldest first so the latest play remains stored when an item has
    # multiple history entries or a retry returned pages in a different order.
    ordered_rows = sorted(
        list(rows or []),
        key=lambda history_row: _date_from_row(history_row) if isinstance(history_row, dict) else "",
    )
    for history_row in ordered_rows:
        if not isinstance(history_row, dict):
            continue
        media_type = _str(history_row.get("type")).lower()
        when = _date_from_row(history_row)
        movie = history_row.get("movie") if isinstance(history_row.get("movie"), dict) else {}
        episode = history_row.get("episode") if isinstance(history_row.get("episode"), dict) else {}
        show = history_row.get("show") if isinstance(history_row.get("show"), dict) else {}
        if media_type == "movie" or movie:
            ids = movie.get("ids") if isinstance(movie.get("ids"), dict) else {}
            if not _str(ids.get("tmdb")):
                continue
            _add_movie_source(state, movie, "finished", when, progress=100.0)
            parsed += 1
        elif media_type == "episode" or episode:
            ids = show.get("ids") if isinstance(show.get("ids"), dict) else {}
            if (
                not _str(ids.get("tmdb"))
                or _to_int(episode.get("season")) is None
                or _to_int(episode.get("number")) is None
            ):
                continue
            _add_episode_source(
                state,
                show,
                episode.get("season"),
                episode.get("number"),
                "finished",
                when,
                progress=100.0,
                episode_title=_str(episode.get("title")),
            )
            parsed += 1
    return parsed


def _queue_has_sent_tombstones(queue_data: dict) -> bool:
    for tombstone in ((queue_data or {}).get("tombstones") or {}).values():
        if isinstance(tombstone, dict) and _to_float(tombstone.get("remote_sent_at"), 0.0) > 0:
            return True
    return False


def _validate_full_history(previous_state: dict, candidate_state: dict,
                           item_count: int, parsed_count: int):
    previous_finished = _remote_source_count(previous_state, "finished")
    candidate_finished = _remote_source_count(candidate_state, "finished")
    if item_count > 0 and parsed_count <= 0:
        raise RuntimeError("Trakt history contained no usable movie or episode data")
    if previous_finished >= 100 and candidate_finished < max(10, int(previous_finished * 0.25)):
        raise RuntimeError(
            "refusing suspicious Trakt snapshot shrink "
            f"from {previous_finished} to {candidate_finished} finished items"
        )


def _local_push_units(local_state: dict) -> dict:
    units = {}
    for tmdb_id, row in (local_state or {}).items():
        if not isinstance(row, dict):
            continue
        tmdb_id = _str(tmdb_id or row.get("tmdb_id"))
        if not tmdb_id:
            continue
        media_type = _str(row.get("media_type")).lower()
        for src in (row.get("sources") or {}).values():
            if not isinstance(src, dict):
                continue
            if _str(src.get("provider")).lower() == "trakt":
                continue
            status = _source_status(src)
            if status not in ("started", "finished"):
                continue
            pair = _source_episode_pair(src)
            if pair:
                key = ("episode", tmdb_id, pair[0], pair[1])
            elif media_type in ("", "movie"):
                key = ("movie", tmdb_id, None, None)
            else:
                continue

            unit = units.setdefault(key, {"finished": None, "started": None})
            current = unit.get(status)
            if current is None or _source_updated_at(src) > _source_updated_at(current):
                unit[status] = dict(src)
    return units


def _push_units_fingerprint(units: dict) -> str:
    clean = []
    for key, unit in sorted((units or {}).items(), key=lambda item: repr(item[0])):
        row = {"key": key}
        for status in ("finished", "started"):
            src = unit.get(status) if isinstance(unit, dict) else None
            if not isinstance(src, dict):
                continue
            row[status] = {
                "updated_at": _source_updated_at(src),
                "progress": round(_source_progress(src, status), 3),
            }
        clean.append(row)
    return _hash_json(clean)


def _history_time_key(value: str) -> str:
    value = _str(value)
    if not value:
        return ""
    if "." in value and value.endswith("Z"):
        return value.split(".", 1)[0] + "Z"
    return value


def _history_item_key(media_type: str, tmdb_id, season=None, episode=None, watched_at: str = ""):
    media_type = _str(media_type).lower()
    tmdb_id = _str(tmdb_id)
    if not media_type or not tmdb_id:
        return None
    if media_type == "movie":
        return ("movie", tmdb_id, None, None, _history_time_key(watched_at))
    return ("episode", tmdb_id, _to_int(season), _to_int(episode), _history_time_key(watched_at))


def _existing_history_keys(items: list) -> set:
    dates = sorted({_str(item.get("watched_at"))[:10] for item in items or [] if isinstance(item, dict) and _str(item.get("watched_at"))})
    if not dates:
        return set()
    keys = set()
    try:
        for date_key in dates:
            start_at = f"{date_key}T00:00:00.000Z"
            end_day = datetime.strptime(date_key, "%Y-%m-%d") + timedelta(days=1)
            end_at = end_day.strftime("%Y-%m-%dT00:00:00.000Z")
            for row in trakt_client.fetch_history_range(start_at=start_at, end_at=end_at, max_pages=10):
                if not isinstance(row, dict):
                    continue
                watched_at = _str(row.get("watched_at"))
                row_type = _str(row.get("type")).lower()
                if row_type == "movie":
                    movie = row.get("movie") if isinstance(row.get("movie"), dict) else {}
                    ids = movie.get("ids") if isinstance(movie.get("ids"), dict) else {}
                    key = _history_item_key("movie", ids.get("tmdb"), watched_at=watched_at)
                elif row_type == "episode":
                    show = row.get("show") if isinstance(row.get("show"), dict) else {}
                    episode_row = row.get("episode") if isinstance(row.get("episode"), dict) else {}
                    ids = show.get("ids") if isinstance(show.get("ids"), dict) else {}
                    key = _history_item_key(
                        "episode",
                        ids.get("tmdb"),
                        episode_row.get("season"),
                        episode_row.get("number"),
                        watched_at=watched_at,
                    )
                else:
                    key = None
                if key:
                    keys.add(key)
    except Exception as e:
        _log(f"existing history check failed: {e}", xbmc.LOGWARNING)
    return keys


def _push_local_units_to_trakt(units: dict) -> dict:
    result = {"pushed": 0, "skipped_existing": 0, "errors": [], "warnings": []}
    finished_movies = []
    finished_episodes = []
    started_units = []

    for key, unit in (units or {}).items():
        if not isinstance(unit, dict):
            continue
        media_type, tmdb_id, season, episode = key
        finished_src = unit.get("finished") if isinstance(unit.get("finished"), dict) else None
        started_src = unit.get("started") if isinstance(unit.get("started"), dict) else None

        if finished_src is not None:
            watched_at = _str(finished_src.get("last_finished_at")) or _str(finished_src.get("last_started_at")) or _str(finished_src.get("updated_at"))
            if media_type == "movie":
                finished_movies.append({
                    "tmdb_id": tmdb_id,
                    "watched_at": watched_at,
                })
            elif media_type == "episode":
                finished_episodes.append({
                    "tmdb_id": tmdb_id,
                    "season": season,
                    "episode": episode,
                    "watched_at": watched_at,
                })

        if started_src is not None and finished_src is None:
            started_units.append((key, started_src))

    finished_items = []
    for item in finished_movies:
        finished_items.append({"media_type": "movie", **item})
    for item in finished_episodes:
        finished_items.append({"media_type": "episode", **item})
    existing_history = _existing_history_keys(finished_items)
    if existing_history:
        filtered_movies = []
        for item in finished_movies:
            key = _history_item_key("movie", item.get("tmdb_id"), watched_at=item.get("watched_at"))
            if key in existing_history:
                result["skipped_existing"] += 1
            else:
                filtered_movies.append(item)
        filtered_episodes = []
        for item in finished_episodes:
            key = _history_item_key("episode", item.get("tmdb_id"), item.get("season"), item.get("episode"), item.get("watched_at"))
            if key in existing_history:
                result["skipped_existing"] += 1
            else:
                filtered_episodes.append(item)
        finished_movies = filtered_movies
        finished_episodes = filtered_episodes

    queued_ids = queue_history_items(movies=finished_movies, episodes=finished_episodes)

    # Playback progress has no practical bulk endpoint here, so keep it tiny and non-blocking.
    # Older local caches can contain many stale started rows; pushing all of them would freeze Kodi.
    started_units.sort(key=lambda item: _source_updated_at(item[1]), reverse=True)
    for key, started_src in started_units[:10]:
        media_type, tmdb_id, season, episode = key
        try:
            operation_id = queue_progress_status(
                media_type=media_type,
                tmdb_id=tmdb_id,
                progress=_source_progress(started_src, "started"),
                season=season,
                episode=episode,
                event_at=_source_updated_at(started_src),
            )
            if operation_id:
                queued_ids.append(operation_id)
            else:
                result["errors"].append(f"local started queue rejected: {key}")
        except Exception as e:
            result["errors"].append(repr(e))
            _log(f"local started queue failed {key}: {e}", xbmc.LOGWARNING)
            continue

    try:
        flush_result = flush_pending_operations(limit=max(200, len(queued_ids) + 20))
        result["pushed"] += int(flush_result.get("sent") or 0)
        result["errors"].extend(flush_result.get("errors") or [])
        pending_queued = sum(1 for operation_id in queued_ids if operation_pending(operation_id))
        if pending_queued:
            result["warnings"].append(f"queued for retry: {pending_queued}")
    except Exception as e:
        result["errors"].append(repr(e))
        _log(f"local playback queue flush failed: {e}", xbmc.LOGWARNING)

    return result


def last_sync_summary() -> dict:
    return _sync_meta_load()


def _merge_local_and_remote(local_state: dict, remote_state: dict) -> dict:
    try:
        state = copy.deepcopy(local_state if isinstance(local_state, dict) else {})
    except Exception:
        state = dict(local_state if isinstance(local_state, dict) else {})

    for tmdb_id, remote_row in (remote_state or {}).items():
        if not isinstance(remote_row, dict):
            continue
        local_row = state.get(tmdb_id) if isinstance(state.get(tmdb_id), dict) else {}
        merged_row = dict(local_row or {})
        for key, value in remote_row.items():
            if key == "sources":
                continue
            if value not in (None, "") and (key not in merged_row or merged_row.get(key) in (None, "")):
                merged_row[key] = copy.deepcopy(value)
        sources = merged_row.get("sources") if isinstance(merged_row.get("sources"), dict) else {}
        remote_sources = remote_row.get("sources") if isinstance(remote_row.get("sources"), dict) else {}
        sources.update(copy.deepcopy(remote_sources))
        merged_row["sources"] = sources
        _set_row_dates(merged_row, _str(remote_row.get("last_played_at")))
        _set_row_dates(merged_row, _str(remote_row.get("updated_at")))
        state[tmdb_id] = merged_row
    return state


def _merge_trakt_into_state(local_state: dict, force: bool = True) -> dict:
    return _merge_local_and_remote(local_state, trakt_state(force=force))


def sync_local_with_trakt(force: bool = False, push_local: bool = False) -> dict:
    global _LOCAL_TRAKT_SYNC_RUNNING
    local_state = tmdb_playback_state.playback_state_load(force_remote=force)
    if not trakt_client.is_connected():
        return local_state
    if _LOCAL_TRAKT_SYNC_RUNNING:
        return merged_state(force_remote=force, include_trakt=True)

    now = time.time()
    meta = _sync_meta_load()
    units = _local_push_units(local_state) if push_local else {}
    units_hash = _push_units_fingerprint(units) if push_local else _hash_json(local_state)
    last_units_hash = _str(meta.get("last_pushed_units_hash"))
    last_sync_ts = _to_float(meta.get("last_sync_ts"), default=0.0)
    last_read_ts = _to_float(meta.get("last_read_sync_ts"), default=0.0)
    has_pending_operations = pending_operation_count() > 0

    if not push_local and not force and not has_pending_operations and last_read_ts > 0 and (now - last_read_ts) < _LOCAL_TRAKT_SYNC_TTL:
        return merged_state(force_remote=False, include_trakt=True)
    if not force and not has_pending_operations and last_sync_ts > 0 and (now - last_sync_ts) < _LOCAL_TRAKT_SYNC_TTL and units_hash == last_units_hash:
        return merged_state(force_remote=False, include_trakt=True)

    _LOCAL_TRAKT_SYNC_RUNNING = True
    try:
        flush_result = flush_pending_operations(limit=200)
        push_result = {"pushed": 0, "errors": []}
        if push_local and units_hash != last_units_hash:
            push_result = _push_local_units_to_trakt(units)

        merged = _merge_trakt_into_state(local_state, force=True)
        post_flush_result = flush_pending_operations(limit=200)
        if int(post_flush_result.get("sent") or 0) > 0:
            merged = _merge_trakt_into_state(local_state, force=True)
        flush_result["sent"] = int(flush_result.get("sent") or 0) + int(post_flush_result.get("sent") or 0)
        flush_result["pending"] = pending_operation_count()
        flush_result["errors"] = list(flush_result.get("errors") or []) + list(post_flush_result.get("errors") or [])

        if push_local:
            meta["last_sync_ts"] = time.time()
        else:
            meta["last_read_sync_ts"] = time.time()
        sync_errors = list(flush_result.get("errors") or []) + list(push_result.get("errors") or [])
        meta["last_sync_ok"] = not bool(sync_errors)
        meta["last_sync_error"] = "; ".join(sync_errors)[:500]
        meta["last_sync_warning"] = "; ".join(push_result.get("warnings") or [])[:500]
        meta["last_pushed_count"] = int(flush_result.get("sent") or 0) + int(push_result.get("pushed") or 0)
        meta["last_skipped_existing_count"] = int(push_result.get("skipped_existing") or 0)
        meta["pending_operation_count"] = pending_operation_count()
        if push_local and not push_result.get("errors"):
            meta["last_pushed_units_hash"] = units_hash
        _sync_meta_save(meta)
        return merged
    except Exception as e:
        meta["last_sync_ts"] = time.time()
        meta["last_sync_ok"] = False
        meta["last_sync_error"] = repr(e)[:500]
        _sync_meta_save(meta)
        _log(f"local trakt sync error: {e}", xbmc.LOGWARNING)
        return merged_state(force_remote=False, include_trakt=True)
    finally:
        _LOCAL_TRAKT_SYNC_RUNNING = False


def trakt_state(force: bool = False) -> dict:
    global _TRAKT_STATE_CACHE, _TRAKT_STATE_CACHE_TS
    try:
        connected = trakt_client.is_connected()
    except Exception:
        connected = False
    if not connected:
        fallback_state = _snapshot_load()
        with _TRAKT_STATE_CACHE_LOCK:
            _TRAKT_STATE_CACHE = copy.deepcopy(fallback_state)
            _TRAKT_STATE_CACHE_TS = time.time()
        return copy.deepcopy(fallback_state)

    with _TRAKT_STATE_CACHE_LOCK:
        now = time.time()
        if (
            not force
            and isinstance(_TRAKT_STATE_CACHE, dict)
            and (now - _TRAKT_STATE_CACHE_TS) < _TRAKT_STATE_CACHE_TTL
        ):
            return copy.deepcopy(_TRAKT_STATE_CACHE)

        fallback_state = _snapshot_load()
        fetch_started_ts = time.time()
        fetch_end_at = _utc_iso(fetch_started_ts)
        meta = _sync_meta_load()
        queue_data = tmdb_playback_queue.load(_outbox_path())
        last_full_sync_ts = _to_float(meta.get("remote_history_full_sync_ts"), 0.0)
        history_cursor = _str(meta.get("remote_history_cursor"))
        full_refresh = (
            not bool(meta.get("remote_history_complete"))
            or not history_cursor
            or last_full_sync_ts <= 0
            or (fetch_started_ts - last_full_sync_ts) >= _REMOTE_HISTORY_FULL_SYNC_INTERVAL
            or _queue_has_sent_tombstones(queue_data)
        )
        try:
            if full_refresh:
                history_result = trakt_client.fetch_history_complete(
                    end_at=fetch_end_at,
                    max_pages=_REMOTE_HISTORY_MAX_PAGES,
                )
                state = {}
            else:
                history_result = trakt_client.fetch_history_complete(
                    start_at=history_cursor,
                    end_at=fetch_end_at,
                    max_pages=_REMOTE_HISTORY_MAX_PAGES,
                )
                state = _finished_remote_state(fallback_state)

            history_rows = history_result.get("rows") if isinstance(history_result, dict) else []
            history_rows = history_rows if isinstance(history_rows, list) else []
            parsed_count = _merge_history_rows(state, history_rows)
            item_count = _to_int(
                history_result.get("item_count") if isinstance(history_result, dict) else len(history_rows)
            ) or 0
            if full_refresh:
                _validate_full_history(
                    fallback_state,
                    state,
                    item_count=item_count,
                    parsed_count=parsed_count,
                )

            _merge_playback(state)
            tmdb_playback_queue.acknowledge_clears(
                _outbox_path(),
                state,
                fetch_started_ts=fetch_started_ts,
            )
            state = tmdb_playback_queue.filter_remote_state(
                state,
                tmdb_playback_queue.load(_outbox_path()),
            )
            _snapshot_save(state)
            meta.update({
                "remote_history_complete": True,
                "remote_history_cursor": fetch_end_at,
                "remote_history_last_refresh_ts": time.time(),
                "remote_history_last_mode": "full" if full_refresh else "incremental",
                "remote_history_last_rows": len(history_rows),
                "remote_snapshot_finished_count": _remote_source_count(state, "finished"),
                "remote_snapshot_source_count": _remote_source_count(state),
            })
            if full_refresh:
                meta["remote_history_full_sync_ts"] = fetch_started_ts
                meta["remote_history_full_item_count"] = item_count
            _sync_meta_save(meta)
        except Exception as e:
            _log(f"trakt state load error: {e}", xbmc.LOGWARNING)
            state = fallback_state

        _TRAKT_STATE_CACHE = copy.deepcopy(state)
        _TRAKT_STATE_CACHE_TS = time.time()
        return copy.deepcopy(state)


def trakt_item_status(media_type: str, tmdb_id: str) -> str:
    tmdb_id = _str(tmdb_id)
    media_type = _str(media_type).lower()
    if media_type == "tv":
        media_type = "show"
    if not tmdb_id:
        return ""
    row = _trakt_cached_row(tmdb_id)
    if not isinstance(row, dict):
        return ""
    return tmdb_playback_state.get_status_bucket(row)


def _trakt_cached_row(tmdb_id: str, force_remote: bool = False) -> dict:
    tmdb_id = _str(tmdb_id)
    if not tmdb_id:
        return {}
    if not force_remote:
        with _TRAKT_STATE_CACHE_LOCK:
            if (
                isinstance(_TRAKT_STATE_CACHE, dict)
                and (time.time() - _TRAKT_STATE_CACHE_TS) < _TRAKT_STATE_CACHE_TTL
            ):
                row = _TRAKT_STATE_CACHE.get(tmdb_id)
                return copy.deepcopy(row) if isinstance(row, dict) else {}
    state = trakt_state(force=force_remote)
    row = state.get(tmdb_id) if isinstance(state, dict) else None
    return copy.deepcopy(row) if isinstance(row, dict) else {}


def merged_row(tmdb_id: str, force_remote: bool = False, include_trakt: bool = True) -> dict:
    tmdb_id = _str(tmdb_id)
    if not tmdb_id:
        return {}
    try:
        local_state = tmdb_playback_state.playback_state_load(force_remote=force_remote)
    except Exception:
        local_state = {}
    local_row = local_state.get(tmdb_id) if isinstance(local_state, dict) else None
    if not include_trakt:
        return copy.deepcopy(local_row) if isinstance(local_row, dict) else {}
    remote_row = _trakt_cached_row(tmdb_id, force_remote=force_remote)
    merged = _merge_local_and_remote(
        {tmdb_id: local_row} if isinstance(local_row, dict) else {},
        {tmdb_id: remote_row} if isinstance(remote_row, dict) else {},
    )
    row = merged.get(tmdb_id)
    return copy.deepcopy(row) if isinstance(row, dict) else {}


def merged_item_status(tmdb_id: str, force_remote: bool = False, include_trakt: bool = True) -> str:
    row = merged_row(tmdb_id, force_remote=force_remote, include_trakt=include_trakt)
    if not row:
        return ""
    return tmdb_playback_state.get_status_bucket(row)


def merged_state(force_remote: bool = False, include_trakt: bool = True) -> dict:
    try:
        local_state = tmdb_playback_state.playback_state_load(force_remote=force_remote)
    except Exception:
        local_state = {}

    if not include_trakt:
        return copy.deepcopy(local_state if isinstance(local_state, dict) else {})

    try:
        connected = trakt_client.is_connected()
    except Exception:
        connected = False
    if not connected:
        return _merge_local_and_remote(local_state, _snapshot_load())

    try:
        remote_state = trakt_state(force=force_remote)
        return _merge_local_and_remote(local_state, remote_state)
    except Exception as e:
        _log(f"trakt merge error: {e}", xbmc.LOGWARNING)
        return copy.deepcopy(local_state if isinstance(local_state, dict) else {})


def _status_index_row_status(row: dict) -> str:
    status = _str((row or {}).get(_STATUS_INDEX_STATUS_KEY)).lower()
    if status in ("started", "finished"):
        return status
    return tmdb_playback_state.get_status_bucket(row)


def _merge_compact_status_rows(local_row: dict, remote_row: dict, tmdb_id: str) -> dict:
    local_compact = _compact_status_row(tmdb_id, local_row)
    remote_compact = dict(remote_row) if isinstance(remote_row, dict) else {}
    if not local_compact:
        return remote_compact
    if not remote_compact:
        return local_compact

    local_status = _status_index_row_status(local_compact)
    remote_status = _status_index_row_status(remote_compact)
    local_updated = _str(local_compact.get(_STATUS_INDEX_UPDATED_KEY))
    remote_updated = _str(remote_compact.get(_STATUS_INDEX_UPDATED_KEY))

    use_local_status = bool(local_status) and (
        not remote_status
        or not remote_updated
        or (bool(local_updated) and local_updated >= remote_updated)
    )
    status = local_status if use_local_status else remote_status
    status_updated = local_updated if use_local_status else remote_updated

    merged = dict(local_compact)
    for key, value in remote_compact.items():
        if key in (_STATUS_INDEX_STATUS_KEY, _STATUS_INDEX_UPDATED_KEY):
            continue
        if value not in (None, "") and merged.get(key) in (None, ""):
            merged[key] = copy.deepcopy(value)
    for key in ("last_played_at", "updated_at"):
        merged[key] = max(_str(local_compact.get(key)), _str(remote_compact.get(key)))
    merged[_STATUS_INDEX_STATUS_KEY] = status
    merged[_STATUS_INDEX_UPDATED_KEY] = status_updated
    return merged


def _merged_status_index_state() -> dict:
    try:
        local_state = tmdb_playback_state.playback_state_load(force_remote=False)
    except Exception:
        local_state = {}
    remote_rows = _status_index_rows()
    state = dict(remote_rows if isinstance(remote_rows, dict) else {})
    for tmdb_id, local_row in (local_state or {}).items():
        if not isinstance(local_row, dict):
            continue
        key = _str(tmdb_id)
        merged = _merge_compact_status_rows(local_row, state.get(key), key)
        if merged:
            state[key] = merged
    return state


def _state_rows(state: dict) -> list:
    out = []
    for tmdb_id, row in (state or {}).items():
        if not isinstance(row, dict):
            continue
        out.append((_str(row.get("last_played_at")), tmdb_id, row))
    out.sort(key=lambda item: item[0], reverse=True)
    return out


def _state_rows_by_status(state: dict, status: str) -> list:
    status = _str(status).lower()
    out = []
    for tmdb_id, row in (state or {}).items():
        if not isinstance(row, dict):
            continue
        if _status_index_row_status(row) != status:
            continue
        out.append((_str(row.get("last_played_at")), tmdb_id, row))
    out.sort(key=lambda item: item[0], reverse=True)
    return out


def iter_trakt_watched_items():
    if not trakt_client.is_connected():
        return []
    try:
        state = _status_index_rows()
    except Exception as e:
        _log(f"trakt watched load error: {e}", xbmc.LOGWARNING)
        state = {}
    return _state_rows_by_status(state, "finished")


def iter_trakt_playback_items():
    if not trakt_client.is_connected():
        return []
    try:
        state = _status_index_rows()
    except Exception as e:
        _log(f"trakt playback load error: {e}", xbmc.LOGWARNING)
        state = {}
    return _state_rows_by_status(state, "started")


def trakt_show_row(tmdb_id: str) -> dict:
    tmdb_id = _str(tmdb_id)
    if not tmdb_id or not trakt_client.is_connected():
        return {}

    return _trakt_cached_row(tmdb_id)


def _row_sources(row: dict) -> list:
    sources = (row or {}).get("sources")
    if not isinstance(sources, dict):
        return []
    return [src for src in sources.values() if isinstance(src, dict)]


def _source_episode_pair(src: dict):
    season = _to_int((src or {}).get("season"))
    episode = _to_int((src or {}).get("episode"))
    if season is None or episode is None:
        return None
    return season, episode


def _source_status(src: dict) -> str:
    status = _str((src or {}).get("status")).lower()
    if status in ("finished", "watched", "complete", "completed"):
        return "finished"
    if status in ("started", "progress", "paused", "playing"):
        return "started"
    ratio = _to_float((src or {}).get("last_ratio"), default=0.0)
    if ratio >= 0.90:
        return "finished"
    if ratio > 0.0:
        return "started"
    return ""


def get_row_episode_status(row: dict, season, episode) -> str:
    season_i = _to_int(season)
    episode_i = _to_int(episode)
    if season_i is None or episode_i is None:
        return ""

    latest_finished = ""
    latest_started = ""
    for src in _row_sources(row):
        if _source_episode_pair(src) != (season_i, episode_i):
            continue
        status = _source_status(src)
        if status == "finished":
            latest_finished = max(latest_finished, _source_updated_at(src))
        if status == "started":
            latest_started = max(latest_started, _source_updated_at(src))
    if latest_started and latest_started > latest_finished:
        return "started"
    if latest_finished:
        return "finished"
    return "started" if latest_started else ""


def merged_episode_status(tmdb_id: str, season, episode, force_remote: bool = False,
                          include_trakt: bool = True) -> str:
    row = merged_row(tmdb_id, force_remote=force_remote, include_trakt=include_trakt)
    if not row:
        return ""
    return get_row_episode_status(row, season, episode)


def get_row_season_status(row: dict, season, total_episodes=None) -> str:
    season_i = _to_int(season)
    if season_i is None:
        return ""

    touched = set()
    finished_dates = {}
    started_dates = {}
    for src in _row_sources(row):
        pair = _source_episode_pair(src)
        if not pair or pair[0] != season_i:
            continue
        touched.add(pair[1])
        if _source_status(src) == "finished":
            finished_dates[pair[1]] = max(finished_dates.get(pair[1], ""), _source_updated_at(src))
        elif _source_status(src) == "started":
            started_dates[pair[1]] = max(started_dates.get(pair[1], ""), _source_updated_at(src))

    total_i = _to_int(total_episodes)
    active_started = any(started_at > finished_dates.get(ep, "") for ep, started_at in started_dates.items())
    if total_i and total_i > 0 and len(finished_dates) >= total_i and not active_started:
        return "finished"
    if touched or active_started:
        return "started"
    return ""


def merged_season_status(tmdb_id: str, season, total_episodes=None, force_remote: bool = False,
                         include_trakt: bool = True) -> str:
    row = merged_row(tmdb_id, force_remote=force_remote, include_trakt=include_trakt)
    if not row:
        return ""
    return get_row_season_status(row, season, total_episodes=total_episodes)


def iter_items_by_status(target_status: str, force_remote: bool = False, include_trakt: bool = True):
    target_status = _str(target_status).lower()
    if include_trakt:
        try:
            connected = bool(force_remote and trakt_client.is_connected())
        except Exception:
            connected = False
        if connected:
            state = sync_local_with_trakt(force=force_remote, push_local=False)
        else:
            state = _merged_status_index_state()
    else:
        try:
            state = tmdb_playback_state.playback_state_load(force_remote=force_remote)
        except Exception:
            state = {}

    out = []
    for tmdb_id, row in (state or {}).items():
        if not isinstance(row, dict):
            continue
        if _status_index_row_status(row) != target_status:
            continue
        out.append((_str(row.get("last_played_at")), tmdb_id, row))
    out.sort(key=lambda item: item[0], reverse=True)
    return out
