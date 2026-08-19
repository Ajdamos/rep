# -*- coding: utf-8 -*-
import json
import os
import re
import time

import xbmc
import xbmcaddon
import xbmcgui
import xbmcplugin
import xbmcvfs

from resources.lib import i18n, source_common


_SENSITIVE_KEYS = {
    "password",
    "token",
    "wst",
    "trakt_access_token",
    "trakt_refresh_token",
    "client_secret",
    "cookie",
    "direct",
    "filehash",
    "final_url",
    "gist_token",
    "headers",
    "hash",
    "resolved_url",
    "video_hash",
    "ws_token",
}

_URL_KEYS = {"url", "page_url", "play_url", "stream_url"}


def _safe_text(value, limit=120):
    text = str(value if value is not None else "")
    text = text.replace("\r", " ").replace("\n", " ").strip()
    if len(text) > limit:
        return text[:limit] + "..."
    return text


def safe_params(params, limit=8):
    if not isinstance(params, dict):
        return ""
    parts = []
    for key in sorted(params.keys()):
        if str(key).lower() == "action":
            continue
        if len(parts) >= limit:
            parts.append("...")
            break
        lowered = str(key).lower()
        if lowered in _SENSITIVE_KEYS or "token" in lowered or "secret" in lowered:
            value = "***"
        elif lowered in _URL_KEYS:
            value = source_common.redact_url_for_log(params.get(key))
        else:
            value = _safe_text(params.get(key))
        parts.append(f"{key}={value}")
    return " ".join(parts)


class ActionTimer:
    def __init__(self, area, action="", params=None):
        self.area = _safe_text(area or "router", 40)
        self.action = _safe_text(action or "", 80)
        self.params = params or {}
        self.started = 0.0

    def __enter__(self):
        self.started = time.time()
        xbmc.log(
            f"[IKARUS][ACTION][START] area={self.area} action={self.action} {safe_params(self.params)}",
            xbmc.LOGINFO,
        )
        return self

    def __exit__(self, exc_type, exc, tb):
        elapsed_ms = int((time.time() - self.started) * 1000) if self.started else 0
        if exc is None:
            level = xbmc.LOGWARNING if elapsed_ms >= 5000 else xbmc.LOGINFO
            xbmc.log(
                f"[IKARUS][ACTION][END] area={self.area} action={self.action} ms={elapsed_ms}",
                level,
            )
            return False
        xbmc.log(
            f"[IKARUS][ACTION][ERROR] area={self.area} action={self.action} ms={elapsed_ms} error={repr(exc)}",
            xbmc.LOGERROR,
        )
        return False


def action_scope(area, action="", params=None):
    return ActionTimer(area, action, params)


def is_playback_action(action: str) -> bool:
    action = str(action or "").strip().lower()
    if not action:
        return False
    if action.endswith(".play") or ".play." in action:
        return True
    return action in {
        "film.play",
        "tmdb.movie.detail.open",
        "tmdb.tv.detail.open",
        "tmdb.movie.source.play",
        "tmdb.tv.source.play",
        "other.prehrajto_item",
        "other.hellspy_item",
        "other.sktonline_item",
    }


def finish_failed_action(addon_handle, action: str = ""):
    try:
        handle = int(addon_handle or 0)
    except Exception:
        handle = 0
    if handle <= 0:
        return
    try:
        if is_playback_action(action):
            xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())
        else:
            xbmcplugin.endOfDirectory(
                handle,
                succeeded=False,
                updateListing=False,
                cacheToDisc=False,
            )
    except Exception:
        pass


def _addon():
    try:
        return xbmcaddon.Addon("plugin.video.ikarus")
    except Exception:
        return xbmcaddon.Addon()


def _translate_path(path: str) -> str:
    try:
        return xbmcvfs.translatePath(path)
    except Exception:
        try:
            return xbmc.translatePath(path)
        except Exception:
            return path


def _profile_dir() -> str:
    try:
        return _translate_path(_addon().getAddonInfo("profile")).rstrip("/\\")
    except Exception:
        return ""


def _exists(path: str) -> bool:
    if not path:
        return False
    checks = [path, _translate_path(path)]
    for value in list(checks):
        if value and not str(value).endswith(("/", "\\")):
            checks.append(str(value) + os.sep)
            checks.append(str(value) + "/")
    try:
        for value in checks:
            if value and xbmcvfs.exists(value):
                return True
    except Exception:
        pass
    try:
        for value in checks:
            if value and os.path.exists(value):
                return True
    except Exception:
        pass
    return False


def _read_text_file(path: str) -> str:
    if not _exists(path):
        return ""
    try:
        fh = xbmcvfs.File(path, "r")
        raw = fh.read()
        fh.close()
        return raw or ""
    except Exception:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return fh.read()
        except Exception:
            return ""


def _read_json(path: str, default):
    raw = _read_text_file(path)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def _file_size(path: str) -> int:
    try:
        translated = _translate_path(path)
        if translated and os.path.exists(translated):
            return int(os.path.getsize(translated))
    except Exception:
        pass
    return 0


def _setting_bool(addon, key: str, default=False) -> bool:
    try:
        value = addon.getSettingBool(key)
        return bool(value)
    except Exception:
        try:
            value = (addon.getSetting(key) or "").strip().lower()
            if not value:
                return bool(default)
            return value in ("true", "1", "yes", "on")
        except Exception:
            return bool(default)


def _temp_source_cache_files():
    try:
        _dirs, files = xbmcvfs.listdir("special://temp/")
        out = []
        for filename in files or []:
            filename = str(filename or "")
            if filename.startswith("ikarus_sources_") and filename.endswith(".json"):
                out.append("special://temp/" + filename)
        return out
    except Exception:
        try:
            temp_dir = _translate_path("special://temp/")
            out = []
            for filename in os.listdir(temp_dir):
                if filename.startswith("ikarus_sources_") and filename.endswith(".json"):
                    out.append(os.path.join(temp_dir, filename))
            return out
        except Exception:
            return []


def _count_temp_source_cache_files() -> int:
    return len(_temp_source_cache_files())


def _delete_temp_source_cache_files(files):
    deleted = 0
    failed = 0
    for path in files or []:
        try:
            if xbmcvfs.delete(path):
                deleted += 1
                continue
        except Exception:
            pass
        try:
            translated = _translate_path(path)
            if translated and os.path.exists(translated):
                os.remove(translated)
                deleted += 1
                continue
        except Exception:
            pass
        failed += 1
    return deleted, failed


def cleanup_source_cache_silent():
    files = _temp_source_cache_files()
    if not files:
        return 0, 0
    deleted, failed = _delete_temp_source_cache_files(files)
    if deleted or failed:
        xbmc.log(f"[IKARUS][DIAG] cleanup_source_cache_silent deleted={deleted} failed={failed}", xbmc.LOGWARNING)
    return deleted, failed


def cleanup_source_cache(addon_handle=0):
    files = _temp_source_cache_files()
    if not files:
        xbmcgui.Dialog().notification(
            i18n.T(30900, "Ikarus"),
            i18n.T(30901, "Source cache is empty."),
            xbmcgui.NOTIFICATION_INFO,
            3000,
        )
        _finish_directory(addon_handle)
        return

    if not xbmcgui.Dialog().yesno(
        i18n.T(30900, "Ikarus"),
        i18n.T(30902, "Delete temporary source cache?\n\nFiles: {count}").format(count=len(files)),
    ):
        _finish_directory(addon_handle)
        return

    deleted, failed = _delete_temp_source_cache_files(files)
    xbmc.log(f"[IKARUS][DIAG] cleanup_source_cache deleted={deleted} failed={failed}", xbmc.LOGWARNING)
    xbmcgui.Dialog().notification(
        i18n.T(30900, "Ikarus"),
        i18n.T(30903, "Source cache: deleted {deleted}, failed {failed}").format(
            deleted=deleted,
            failed=failed,
        ),
        xbmcgui.NOTIFICATION_INFO if failed == 0 else xbmcgui.NOTIFICATION_WARNING,
        4000,
    )
    _finish_directory(addon_handle)


def _finish_directory(addon_handle=0):
    try:
        handle = int(addon_handle or 0)
        if handle > 0:
            xbmcplugin.endOfDirectory(handle, succeeded=True, updateListing=False, cacheToDisc=False)
    except Exception:
        pass


def _summarize_playback_state(data):
    if not isinstance(data, dict):
        return {
            "rows": 0,
            "sources": 0,
            "local_sources": 0,
            "started": 0,
            "finished": 0,
            "delete": 0,
            "missing_title": 0,
            "missing_poster": 0,
            "missing_plot": 0,
            "unreadable_title": 0,
            "file_like_title": 0,
            "volatile_urls": 0,
        }
    out = {
        "rows": len(data),
        "sources": 0,
        "local_sources": 0,
        "started": 0,
        "finished": 0,
        "delete": 0,
        "missing_title": 0,
        "missing_poster": 0,
        "missing_plot": 0,
        "unreadable_title": 0,
        "file_like_title": 0,
        "volatile_urls": 0,
    }
    for row in data.values():
        if not isinstance(row, dict):
            continue
        title = _safe_text(row.get("title") or "", 160)
        if not title:
            out["missing_title"] += 1
        elif _looks_unreadable_title(title):
            out["unreadable_title"] += 1
        elif _looks_like_file_title(title):
            out["file_like_title"] += 1
        if not _row_has_art(row):
            out["missing_poster"] += 1
        if not str(row.get("plot") or row.get("overview") or row.get("description") or "").strip():
            out["missing_plot"] += 1
        sources = row.get("sources") or {}
        if not isinstance(sources, dict):
            continue
        for source in sources.values():
            if not isinstance(source, dict):
                continue
            out["sources"] += 1
            provider = str(source.get("provider") or "").strip().lower()
            if provider and provider != "trakt":
                out["local_sources"] += 1
            status = str(source.get("status") or "").strip().lower()
            if status in ("started", "finished", "delete"):
                out[status] += 1
            for key in ("direct", "play_url", "resolved_url"):
                if str(source.get(key) or "").strip():
                    out["volatile_urls"] += 1
            url = str(source.get("url") or "").strip()
            if url and not url.startswith(("tmdb://", "trakt://")):
                out["volatile_urls"] += 1
    return out


def _looks_unreadable_title(title: str) -> bool:
    title = str(title or "").strip()
    if not title:
        return False
    if "\ufffd" in title or "\u25a1" in title or "\u25a0" in title:
        return True
    return not bool(re.search(r"[A-Za-z0-9Á-ž]", title))


def _looks_like_file_title(title: str) -> bool:
    title = str(title or "").strip().lower()
    if not title:
        return False
    if title.endswith((".mkv", ".mp4", ".avi", ".mov", ".ts", ".m2ts", ".wmv")):
        return True
    return bool(re.search(r"\b(1080p|2160p|720p|bluray|web[-_. ]?dl|webrip|x264|x265|h\.?264|h\.?265)\b", title))


def _row_has_art(row) -> bool:
    if not isinstance(row, dict):
        return False
    for key in ("poster", "thumb", "icon", "cover", "image"):
        if str(row.get(key) or "").strip():
            return True
    art = row.get("art") or row.get("artwork") or {}
    if isinstance(art, dict):
        return bool(str(art.get("poster") or art.get("thumb") or art.get("icon") or "").strip())
    return False


def _summarize_download_queue(data):
    jobs = []
    if isinstance(data, dict):
        jobs = data.get("jobs") or []
    if not isinstance(jobs, list):
        jobs = []
    out = {"all": len(jobs), "queued": 0, "downloading": 0, "done": 0, "error": 0}
    for job in jobs:
        if not isinstance(job, dict):
            continue
        status = str(job.get("status") or "").strip().lower()
        if status in out:
            out[status] += 1
    return out


def run_self_test(addon_handle=0):
    addon = _addon()
    profile = _profile_dir()
    tmdb_state_dir = os.path.join(profile, "tmdb_state") if profile else ""
    playback_path = os.path.join(tmdb_state_dir, "tmdb_playback_state.json") if tmdb_state_dir else ""
    playback_snapshot_path = os.path.join(tmdb_state_dir, "tmdb_playback_trakt_snapshot.json") if tmdb_state_dir else ""
    playback_index_path = os.path.join(tmdb_state_dir, "tmdb_playback_trakt_index.json") if tmdb_state_dir else ""
    playback_outbox_path = os.path.join(tmdb_state_dir, "tmdb_playback_trakt_outbox.json") if tmdb_state_dir else ""
    custom_lists_path = os.path.join(tmdb_state_dir, "tmdb_custom_lists.json") if tmdb_state_dir else ""
    prefetch_path = os.path.join(tmdb_state_dir, "tmdb_custom_list_prefetch_queue.json") if tmdb_state_dir else ""
    download_queue_path = os.path.join(profile, "download_queue.json") if profile else ""

    playback = _summarize_playback_state(_read_json(playback_path, {}))
    playback_snapshot = _summarize_playback_state(_read_json(playback_snapshot_path, {}))
    playback_index = _read_json(playback_index_path, {})
    playback_index_rows = playback_index.get("rows") if isinstance(playback_index, dict) else {}
    playback_index_count = len(playback_index_rows) if isinstance(playback_index_rows, dict) else 0
    playback_index_schema = playback_index.get("schema") if isinstance(playback_index, dict) else "-"
    playback_outbox = _read_json(playback_outbox_path, {})
    playback_pending = len(playback_outbox.get("operations") or []) if isinstance(playback_outbox, dict) else 0
    playback_tombstones = len(playback_outbox.get("tombstones") or {}) if isinstance(playback_outbox, dict) else 0
    downloads = _summarize_download_queue(_read_json(download_queue_path, {}))
    custom_lists = _read_json(custom_lists_path, {})
    if isinstance(custom_lists, dict):
        custom_count = len(custom_lists.get("lists") or custom_lists.get("items") or custom_lists)
    elif isinstance(custom_lists, list):
        custom_count = len(custom_lists)
    else:
        custom_count = 0
    prefetch_rows = _read_json(prefetch_path, [])
    prefetch_count = len(prefetch_rows) if isinstance(prefetch_rows, list) else 0

    ws_user = bool((addon.getSetting("username") or "").strip())
    ws_token = bool((addon.getSetting("token") or "").strip())
    trakt_access = bool((addon.getSetting("trakt_access_token") or "").strip())
    trakt_refresh = bool((addon.getSetting("trakt_refresh_token") or "").strip())
    trakt_user = (addon.getSetting("trakt_username") or "").strip()
    providers = [
        name for name, key in (
            ("prehrajto", "show_prehrajto"),
            ("hellspy", "show_hellspy"),
            ("sktonline", "show_sktonline"),
            ("webshare", "show_webshare"),
        )
        if _setting_bool(addon, key, True)
    ]

    lines = [
        i18n.T(30904, "Ikarus diagnostics"),
        f"profile={'OK' if _exists(profile) else 'MISSING'} path={profile}",
        f"webshare user={'yes' if ws_user else 'no'} token={'yes' if ws_token else 'no'}",
        f"trakt connected={'yes' if (trakt_access and trakt_refresh) else 'no'} user={trakt_user or '-'}",
        f"providers enabled={','.join(providers) if providers else '-'}",
        (
            "playback_state "
            f"exists={'yes' if _exists(playback_path) else 'no'} "
            f"size={_file_size(playback_path)} "
            f"rows={playback['rows']} sources={playback['sources']} "
            f"local_sources={playback['local_sources']} "
            f"started={playback['started']} finished={playback['finished']} delete={playback['delete']}"
        ),
        (
            "trakt_snapshot "
            f"exists={'yes' if _exists(playback_snapshot_path) else 'no'} "
            f"size={_file_size(playback_snapshot_path)} "
            f"rows={playback_snapshot['rows']} sources={playback_snapshot['sources']} "
            f"pending={playback_pending} tombstones={playback_tombstones}"
        ),
        (
            "trakt_index "
            f"exists={'yes' if _exists(playback_index_path) else 'no'} "
            f"size={_file_size(playback_index_path)} "
            f"rows={playback_index_count} schema={playback_index_schema}"
        ),
        (
            "playback_quality "
            f"missing_title={playback['missing_title']} "
            f"missing_poster={playback['missing_poster']} "
            f"missing_plot={playback['missing_plot']} "
            f"unreadable_title={playback['unreadable_title']} "
            f"file_like_title={playback['file_like_title']}"
            f" volatile_urls={playback['volatile_urls']}"
        ),
        f"custom_lists exists={'yes' if _exists(custom_lists_path) else 'no'} count={custom_count}",
        f"prefetch_queue exists={'yes' if _exists(prefetch_path) else 'no'} count={prefetch_count}",
        (
            "download_queue "
            f"exists={'yes' if _exists(download_queue_path) else 'no'} "
            f"all={downloads['all']} queued={downloads['queued']} "
            f"downloading={downloads['downloading']} done={downloads['done']} error={downloads['error']}"
        ),
        f"source_cache temp_files={_count_temp_source_cache_files()}",
        f"download enabled={'yes' if _setting_bool(addon, 'download_enable', True) else 'no'} path={(addon.getSetting('download_path') or '').strip() or '-'}",
    ]

    for line in lines:
        try:
            xbmc.log(f"[IKARUS][DIAG] {line}", xbmc.LOGWARNING)
        except Exception:
            pass

    text = "\n".join(lines)
    try:
        xbmcgui.Dialog().textviewer(i18n.T(30904, "Ikarus diagnostics"), text)
    except Exception:
        xbmcgui.Dialog().ok(i18n.T(30904, "Ikarus diagnostics"), text)

    try:
        handle = int(addon_handle or 0)
        if handle > 0:
            xbmcplugin.endOfDirectory(handle, succeeded=True, updateListing=False, cacheToDisc=False)
    except Exception:
        pass
