# -*- coding: utf-8 -*-
import json
import os
import time

import requests
import xbmc
import xbmcaddon
import xbmcvfs


GIST_API_URL = "https://api.github.com/gists"
CUSTOM_LISTS_FILE = "tmdb_custom_lists.json"
PLAYBACK_STATE_FILE = "tmdb_playback_state.json"
CUSTOM_LISTS_CACHE_TTL = 120.0
PLAYBACK_STATE_CACHE_TTL = 300.0


def _addon():
    try:
        return xbmcaddon.Addon("plugin.video.ikarus")
    except Exception:
        return xbmcaddon.Addon()


def _get(key, default=""):
    try:
        return _addon().getSetting(key) or default
    except Exception:
        return default


def _set(key, value):
    try:
        _addon().setSetting(key, "" if value is None else str(value))
    except Exception:
        pass


def _log(msg, level=xbmc.LOGINFO):
    try:
        xbmc.log(f"[IKARUS][GIST SYNC] {msg}", level)
    except Exception:
        pass


def _profile_dir():
    addon = _addon()
    try:
        base = xbmcvfs.translatePath(addon.getAddonInfo("profile"))
    except Exception:
        base = ""
    path = os.path.join(base, "tmdb_state") if base else ""
    if path:
        try:
            xbmcvfs.mkdirs(path)
        except Exception:
            try:
                os.makedirs(path, exist_ok=True)
            except Exception:
                pass
    return path


def _cache_file_path(filename: str) -> str:
    return os.path.join(_profile_dir(), f"gist_cache_{filename}")


def _meta_path() -> str:
    return os.path.join(_profile_dir(), "gist_sync_meta.json")


def _meta_load() -> dict:
    path = _meta_path()
    if not path or not xbmcvfs.exists(path):
        return {}
    try:
        fh = xbmcvfs.File(path)
        raw = fh.read()
        fh.close()
        data = json.loads(raw) if raw else {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _meta_save(data: dict):
    path = _meta_path()
    if not path:
        return
    try:
        fh = xbmcvfs.File(path, "w")
        fh.write(json.dumps(data if isinstance(data, dict) else {}, ensure_ascii=False, indent=2))
        fh.close()
    except Exception:
        pass


def _meta_get(key: str, default=None):
    return _meta_load().get(key, default)


def _meta_set(key: str, value):
    data = _meta_load()
    data[key] = value
    _meta_save(data)


def _cache_ttl(filename: str) -> float:
    if filename == PLAYBACK_STATE_FILE:
        return PLAYBACK_STATE_CACHE_TTL
    return CUSTOM_LISTS_CACHE_TTL


def _uses_cache(filename: str) -> bool:
    return filename != CUSTOM_LISTS_FILE


def _cache_read(filename: str):
    path = _cache_file_path(filename)
    if not path or not xbmcvfs.exists(path):
        return None
    try:
        fh = xbmcvfs.File(path)
        raw = fh.read()
        fh.close()
        return raw if raw else None
    except Exception:
        return None


def _cache_write(filename: str, content: str):
    path = _cache_file_path(filename)
    if not path:
        return
    try:
        fh = xbmcvfs.File(path, "w")
        fh.write(content or "")
        fh.close()
    except Exception:
        pass




def _etag_key(filename: str) -> str:
    return f"etag::{filename}"


def _store_response_etag(filename: str, response):
    try:
        etag = response.headers.get("ETag")
    except Exception:
        etag = ""
    if etag:
        _meta_set(_etag_key(filename), etag)


def invalidate_remote_cache(filename: str):
    path = _cache_file_path(filename)
    try:
        if path and xbmcvfs.exists(path):
            xbmcvfs.delete(path)
    except Exception:
        pass
    _meta_set(f"cache_ts::{filename}", 0.0)


def cleanup_custom_lists_cache():
    invalidate_remote_cache(CUSTOM_LISTS_FILE)
    _meta_set(f"dirty::{CUSTOM_LISTS_FILE}", False)
    _meta_set(f"due::{CUSTOM_LISTS_FILE}", 0.0)


def _token():
    return (_get("gist_token") or "").strip()


def _gist_id():
    return _normalize_gist_id((_get("gist_id") or "").strip())


def _enabled():
    return False


def _enabled_playback_state():
    return (_get("gist_sync_playback_state") or "").strip().lower() == "true"


def _has_lists(data) -> bool:
    return isinstance(data, dict) and bool(data.get("lists"))


def _has_playback_state(data) -> bool:
    if not isinstance(data, dict):
        return False
    for row in data.values():
        if not isinstance(row, dict):
            continue
        sources = row.get("sources") or {}
        if isinstance(sources, dict) and sources:
            return True
    return False


def _normalize_gist_id(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if "/" in text:
        text = text.rstrip("/").split("/")[-1]
    if "?" in text:
        text = text.split("?", 1)[0]
    return text.strip()


def _headers():
    token = _token()
    gist_id = _gist_id()
    if not token or not gist_id:
        _set("gist_status", "nepripojeno")
        return None
    _set("gist_status", "pripraveno")
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def mark_dirty(filename: str):
    _meta_set(f"dirty::{filename}", True)


def clear_dirty(filename: str):
    _meta_set(f"dirty::{filename}", False)
    _meta_set(f"due::{filename}", 0.0)


def is_dirty(filename: str) -> bool:
    return bool(_meta_get(f"dirty::{filename}", False))




def get_due_time(filename: str) -> float:
    try:
        return float(_meta_get(f"due::{filename}", 0.0) or 0.0)
    except Exception:
        return 0.0


def set_last_push_time(filename: str, ts: float):
    _meta_set(f"last_push::{filename}", float(ts or 0.0))




def import_settings_from_file():
    path = (_get("gist_config_path") or "").strip()
    if not path:
        _set("gist_status", "chybi soubor")
        return False, "Neni nastavena cesta k souboru."
    if not os.path.isfile(path):
        _set("gist_status", "soubor nenalezen")
        return False, f"Soubor nebyl nalezen:\n{path}"

    token = ""
    gist_id = ""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip().lower()
                value = value.strip()
                if key == "gist_token":
                    token = value
                elif key == "gist_id":
                    gist_id = _normalize_gist_id(value)
    except Exception as e:
        _set("gist_status", "chyba souboru")
        return False, f"Nacteni souboru selhalo:\n{e}"

    if not token or not gist_id:
        _set("gist_status", "neuplny soubor")
        return False, "Soubor musi obsahovat gist_token= a gist_id=."

    _set("gist_token", token)
    _set("gist_id", gist_id)
    _set("gist_status", "nacteno ze souboru")
    return True, "GitHub token a Gist ID byly nacteny ze souboru."


def sync_custom_lists_load(local_data, force_remote: bool = True):
    try:
        cleanup_custom_lists_cache()
        content = _gist_file_content(CUSTOM_LISTS_FILE, enabled=_enabled(), force_remote=force_remote)
        if not content:
            _set("gist_status", "soubor prazdny")
            if _has_lists(local_data):
                sync_custom_lists_save(local_data)
            return local_data
        remote_data = json.loads(content)
        if isinstance(remote_data, dict) and isinstance(remote_data.get("lists"), list):
            if not remote_data.get("lists") and _has_lists(local_data):
                _set("gist_status", "gist prazdny, ponechavam lokal")
                sync_custom_lists_save(local_data)
                return local_data
            merged = _merge_custom_lists_data(local_data, remote_data)
            _set("gist_status", "pripojeno")
            return merged
    except Exception as e:
        _log(f"load error: {e}", xbmc.LOGWARNING)
        _set("gist_status", "chyba cteni")
    return local_data


def sync_custom_lists_save(clean_data):
    if not _enabled():
        return
    try:
        cleanup_custom_lists_cache()
        _gist_file_save(CUSTOM_LISTS_FILE, clean_data)
        cleanup_custom_lists_cache()
        _set("gist_status", "pripojeno")
    except Exception as e:
        _log(f"save error: {e}", xbmc.LOGWARNING)
        _set("gist_status", "chyba zapisu")


def _list_item_key(item: dict) -> str:
    if not isinstance(item, dict):
        return ""
    media_type = str(item.get("media_type") or "").strip().lower()
    tmdb_id = str(item.get("tmdb_id") or item.get("id") or "").strip()
    if media_type not in ("movie", "tv") or not tmdb_id:
        return ""
    return f"{media_type}:{tmdb_id}"


def _merge_custom_list_rows(local_row, remote_row):
    local_row = dict(local_row or {})
    remote_row = dict(remote_row or {})

    local_updated = _iso_key(local_row.get("updated_at"))
    remote_updated = _iso_key(remote_row.get("updated_at"))
    merged = dict(remote_row if remote_updated > local_updated else local_row)

    for key in ("id", "name", "created_at", "updated_at"):
        if merged.get(key) in (None, ""):
            merged[key] = local_row.get(key) or remote_row.get(key) or ""

    local_items = local_row.get("items") if isinstance(local_row.get("items"), list) else []
    remote_items = remote_row.get("items") if isinstance(remote_row.get("items"), list) else []
    local_deleted_items = local_row.get("deleted_items") if isinstance(local_row.get("deleted_items"), list) else []
    remote_deleted_items = remote_row.get("deleted_items") if isinstance(remote_row.get("deleted_items"), list) else []
    item_map = {}
    deleted_item_map = {}

    for item in local_items:
        key = _list_item_key(item)
        if not key:
            continue
        item_map[key] = dict(item)

    for item in remote_items:
        key = _list_item_key(item)
        if not key:
            continue
        existing = item_map.get(key)
        if not isinstance(existing, dict):
            item_map[key] = dict(item)
            continue
        existing_added = _iso_key(existing.get("added_at"))
        incoming_added = _iso_key(item.get("added_at"))
        chosen = dict(item if incoming_added > existing_added else existing)
        for field, value in existing.items():
            if chosen.get(field) in (None, "") and value not in (None, ""):
                chosen[field] = value
        for field, value in item.items():
            if chosen.get(field) in (None, "") and value not in (None, ""):
                chosen[field] = value
        item_map[key] = chosen

    for item in list(local_deleted_items) + list(remote_deleted_items):
        key = _list_item_key(item)
        if not key:
            continue
        deleted_at = _iso_key((item or {}).get("deleted_at"))
        if not deleted_at:
            continue
        existing = deleted_item_map.get(key)
        if not existing or deleted_at >= _iso_key(existing.get("deleted_at")):
            deleted_item_map[key] = dict(item)

    for key, tomb in list(deleted_item_map.items()):
        item = item_map.get(key)
        item_added = _iso_key((item or {}).get("added_at")) if isinstance(item, dict) else ""
        deleted_at = _iso_key(tomb.get("deleted_at"))
        if item and item_added > deleted_at:
            deleted_item_map.pop(key, None)
            continue
        item_map.pop(key, None)

    merged["items"] = list(item_map.values())
    merged["deleted_items"] = list(deleted_item_map.values())
    merged["updated_at"] = max(local_updated, remote_updated, _iso_key(merged.get("updated_at")))
    return merged


def _merge_custom_lists_data(local_data, remote_data):
    local_lists = (local_data or {}).get("lists") if isinstance((local_data or {}).get("lists"), list) else []
    remote_lists = (remote_data or {}).get("lists") if isinstance((remote_data or {}).get("lists"), list) else []
    local_deleted = (local_data or {}).get("deleted_lists") if isinstance((local_data or {}).get("deleted_lists"), list) else []
    remote_deleted = (remote_data or {}).get("deleted_lists") if isinstance((remote_data or {}).get("deleted_lists"), list) else []

    merged_map = {}
    name_index = {}

    def _row_key(row: dict) -> str:
        row_id = str((row or {}).get("id") or "").strip()
        if row_id:
            return row_id
        return str((row or {}).get("name") or "").strip().casefold()

    for row in local_lists:
        if not isinstance(row, dict):
            continue
        key = _row_key(row)
        if not key:
            continue
        merged_map[key] = dict(row)
        row_name = str(row.get("name") or "").strip().casefold()
        if row_name:
            name_index[row_name] = key

    for row in remote_lists:
        if not isinstance(row, dict):
            continue
        key = _row_key(row)
        row_name = str(row.get("name") or "").strip().casefold()
        if row_name and row_name in name_index:
            key = name_index[row_name]
        if key in merged_map:
            merged_map[key] = _merge_custom_list_rows(merged_map.get(key), row)
        else:
            merged_map[key] = dict(row)
        if row_name:
            name_index[row_name] = key

    tombstones = {}

    def _merge_tombstones(rows):
        for row in rows:
            if not isinstance(row, dict):
                continue
            list_id = str(row.get("id") or "").strip()
            deleted_at = _iso_key(row.get("deleted_at"))
            if not list_id or not deleted_at:
                continue
            existing = tombstones.get(list_id) or {}
            if deleted_at > _iso_key(existing.get("deleted_at")):
                tombstones[list_id] = {
                    "id": list_id,
                    "name": str(row.get("name") or "").strip(),
                    "deleted_at": deleted_at,
                }

    _merge_tombstones(local_deleted)
    _merge_tombstones(remote_deleted)

    for tomb_id, tomb in list(tombstones.items()):
        row = merged_map.get(tomb_id)
        if not isinstance(row, dict):
            continue
        if _iso_key(tomb.get("deleted_at")) >= _iso_key(row.get("updated_at")):
            merged_map.pop(tomb_id, None)

    return {
        "version": 1,
        "lists": list(merged_map.values()),
        "deleted_lists": list(tombstones.values()),
    }


def _gist_file_content(filename: str, enabled: bool = True, force_remote: bool = False):
    if not enabled:
        return None
    cache_ts = float(_meta_get(f"cache_ts::{filename}", 0.0) or 0.0)
    dirty = is_dirty(filename)
    cached = _cache_read(filename) if _uses_cache(filename) else None
    if cached and (not force_remote) and (not dirty) and cache_ts > 0 and (time.time() - cache_ts) <= _cache_ttl(filename):
        return cached
    headers = _headers()
    gist_id = _gist_id()
    if not headers or not gist_id:
        return None
    etag = _meta_get(_etag_key(filename), "")
    if cached and etag and (not force_remote) and (not dirty):
        headers["If-None-Match"] = etag
    r = requests.get(f"{GIST_API_URL}/{gist_id}", headers=headers, timeout=20)
    if r.status_code == 304:
        _meta_set(f"cache_ts::{filename}", time.time())
        clear_dirty(filename)
        return cached
    r.raise_for_status()
    _store_response_etag(filename, r)
    payload = r.json() or {}
    files = payload.get("files") or {}
    row = files.get(filename) or {}
    content = row.get("content")
    if row.get("truncated"):
        raw_url = row.get("raw_url")
        if not raw_url:
            return None
        raw_headers = dict(headers)
        raw_headers.pop("If-None-Match", None)
        rr = requests.get(raw_url, headers=raw_headers, timeout=20)
        rr.raise_for_status()
        content = rr.text
    if content is not None:
        if _uses_cache(filename):
            _cache_write(filename, content)
            _meta_set(f"cache_ts::{filename}", time.time())
        else:
            invalidate_remote_cache(filename)
        clear_dirty(filename)
    return content


def _gist_file_save(filename: str, data):
    headers = _headers()
    gist_id = _gist_id()
    if not headers or not gist_id:
        return
    content = json.dumps(data, ensure_ascii=False, indent=2)
    body = {
        "files": {
            filename: {
                "content": content
            }
        }
    }
    r = requests.patch(f"{GIST_API_URL}/{gist_id}", headers=headers, json=body, timeout=20)
    r.raise_for_status()
    _store_response_etag(filename, r)
    if _uses_cache(filename):
        _cache_write(filename, content)
        _meta_set(f"cache_ts::{filename}", time.time())
    else:
        invalidate_remote_cache(filename)
    set_last_push_time(filename, time.time())
    clear_dirty(filename)


def _iso_key(value):
    return str(value or "").strip()


def _merge_playback_row(local_row, remote_row):
    local_row = dict(local_row or {})
    remote_row = dict(remote_row or {})

    local_sources = local_row.get("sources") if isinstance(local_row.get("sources"), dict) else {}
    remote_sources = remote_row.get("sources") if isinstance(remote_row.get("sources"), dict) else {}

    merged = dict(local_row)
    local_updated = _iso_key(local_row.get("updated_at"))
    remote_updated = _iso_key(remote_row.get("updated_at"))
    if remote_updated > local_updated:
        merged = dict(remote_row)
    else:
        for key, value in remote_row.items():
            if key == "sources":
                continue
            if key not in merged or merged.get(key) in (None, ""):
                merged[key] = value

    merged_sources = {}
    source_keys = set(local_sources.keys()) | set(remote_sources.keys())
    for source_key in source_keys:
        local_src = local_sources.get(source_key)
        remote_src = remote_sources.get(source_key)
        if not isinstance(local_src, dict):
            merged_sources[source_key] = remote_src
            continue
        if not isinstance(remote_src, dict):
            merged_sources[source_key] = local_src
            continue
        local_src_updated = max(
            _iso_key(local_src.get("last_started_at")),
            _iso_key(local_src.get("last_finished_at")),
            _iso_key(local_src.get("last_deleted_at")),
        )
        remote_src_updated = max(
            _iso_key(remote_src.get("last_started_at")),
            _iso_key(remote_src.get("last_finished_at")),
            _iso_key(remote_src.get("last_deleted_at")),
        )
        merged_sources[source_key] = dict(remote_src if remote_src_updated > local_src_updated else local_src)

    merged["sources"] = {k: v for k, v in merged_sources.items() if isinstance(v, dict)}
    merged["updated_at"] = max(
        _iso_key(merged.get("updated_at")),
        _iso_key(local_row.get("updated_at")),
        _iso_key(remote_row.get("updated_at")),
    )
    return merged


def sync_playback_state_load(local_data, force_remote: bool = False):
    if not _enabled_playback_state():
        return local_data
    try:
        content = _gist_file_content(PLAYBACK_STATE_FILE, enabled=True, force_remote=force_remote)
        if not content:
            if _has_playback_state(local_data):
                sync_playback_state_save(local_data)
            return local_data
        remote_data = json.loads(content)
        if not isinstance(remote_data, dict):
            return local_data
        if not _has_playback_state(remote_data) and _has_playback_state(local_data):
            sync_playback_state_save(local_data)
            return local_data

        merged = {}
        item_keys = set((local_data or {}).keys()) | set(remote_data.keys())
        for item_key in item_keys:
            local_row = (local_data or {}).get(item_key)
            remote_row = remote_data.get(item_key)
            if not isinstance(local_row, dict):
                if isinstance(remote_row, dict):
                    merged[item_key] = remote_row
                continue
            if not isinstance(remote_row, dict):
                merged[item_key] = local_row
                continue
            merged[item_key] = _merge_playback_row(local_row, remote_row)

        _set("gist_status", "pripojeno")
        return merged
    except Exception as e:
        _log(f"playback load error: {e}", xbmc.LOGWARNING)
        _set("gist_status", "chyba cteni")
        return local_data


def sync_playback_state_save(clean_data):
    if not _enabled_playback_state():
        return
    try:
        if not isinstance(clean_data, dict):
            return
        mark_dirty(PLAYBACK_STATE_FILE)
        _gist_file_save(PLAYBACK_STATE_FILE, clean_data)
        _set("gist_status", "pripojeno")
    except Exception as e:
        _log(f"playback save error: {e}", xbmc.LOGWARNING)
        _set("gist_status", "chyba zapisu")
