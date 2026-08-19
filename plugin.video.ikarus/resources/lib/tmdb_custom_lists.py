# -*- coding: utf-8 -*-
import hashlib
import os
import re
import time
from datetime import datetime

import xbmc
import xbmcaddon
import xbmcvfs

from resources.lib import github_gist_sync, i18n, json_storage


AUTO_SYNC_INTERVAL_SECONDS = 90.0
AUTO_SYNC_LOCK_TIMEOUT_SECONDS = 45.0
AUTO_SYNC_SUPPRESS_AFTER_LOCAL_CHANGE_SECONDS = 12.0
SERVICE_SYNC_INTERVAL_SECONDS = 60.0
SERVICE_SYNC_LOCAL_CHANGE_DELAY_SECONDS = 15.0
LIST_TRAKT_META_FIELDS = (
    "trakt_slug",
    "trakt_id",
    "trakt_username",
    "trakt_privacy",
    "trakt_description",
    "trakt_item_count",
    "sync_state",
    "last_trakt_sync_at",
)
DELETED_LIST_META_FIELDS = (
    "trakt_slug",
    "trakt_id",
    "trakt_username",
    "trakt_privacy",
    "trakt_description",
    "sync_state",
)


def _log(msg, level=xbmc.LOGINFO):
    try:
        xbmc.log(f"[IKARUS][TMDB LISTS] {msg}", level)
    except Exception:
        pass


def _addon():
    try:
        return xbmcaddon.Addon("plugin.video.ikarus")
    except Exception:
        return xbmcaddon.Addon()


def _profile_dir():
    addon = _addon()
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


def custom_lists_path():
    return os.path.join(_profile_dir(), "tmdb_custom_lists.json")


def _device_meta_path():
    return os.path.join(_profile_dir(), "tmdb_device.json")


def _custom_sync_meta_path():
    return os.path.join(_profile_dir(), "tmdb_custom_lists_auto_sync.json")


def _device_name():
    try:
        name = xbmc.getInfoLabel("System.FriendlyName") or ""
    except Exception:
        name = ""
    name = str(name or "").strip()
    return name or i18n.T(31958)


def _device_identity():
    path = _device_meta_path()
    if xbmcvfs.exists(path):
        try:
            data = json_storage.read_json(path, {}, expected_type=dict)
            if isinstance(data, dict):
                device_id = str(data.get("device_id") or "").strip()
                device_name = str(data.get("device_name") or "").strip()
                if device_id:
                    return {
                        "device_id": device_id,
                        "device_name": device_name or _device_name(),
                    }
        except Exception:
            pass

    try:
        seed = f"{_device_name()}|{datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')}|{os.urandom(8).hex()}"
    except Exception:
        seed = f"{_device_name()}|{datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')}"
    device = {
        "device_id": hashlib.md5(seed.encode("utf-8")).hexdigest()[:16],
        "device_name": _device_name(),
    }
    try:
        json_storage.write_json(path, device, indent=2, keep_backup=True)
    except Exception:
        pass
    return device


def _normalize_data(data):
    if not isinstance(data, dict):
        data = {}
    lists = data.get("lists")
    if not isinstance(lists, list):
        lists = []
    deleted_lists = data.get("deleted_lists")
    if not isinstance(deleted_lists, list):
        deleted_lists = []
    return {
        "version": 1,
        "lists": lists,
        "deleted_lists": deleted_lists,
    }


def _addon_setting_enabled(key: str) -> bool:
    if key == "gist_sync_custom_lists":
        return False
    try:
        return (_addon().getSetting(key) or "").strip().lower() == "true"
    except Exception:
        return False


def _sync_meta_load():
    path = _custom_sync_meta_path()
    if not xbmcvfs.exists(path):
        return {}
    try:
        return json_storage.read_json(path, {}, expected_type=dict)
    except Exception:
        return {}


def _sync_meta_save(data: dict):
    path = _custom_sync_meta_path()
    try:
        json_storage.write_json(
            path,
            data if isinstance(data, dict) else {},
            indent=2,
            keep_backup=True,
        )
    except Exception:
        pass


def _sync_meta_get(key: str, default=None):
    return _sync_meta_load().get(key, default)


def _sync_meta_set(key: str, value):
    data = _sync_meta_load()
    data[key] = value
    _sync_meta_save(data)


def _sync_lock_acquire() -> bool:
    now = time.time()
    lock_ts = 0.0
    try:
        lock_ts = float(_sync_meta_get("sync_lock_ts", 0.0) or 0.0)
    except Exception:
        lock_ts = 0.0
    if lock_ts > 0 and (now - lock_ts) < AUTO_SYNC_LOCK_TIMEOUT_SECONDS:
        return False
    _sync_meta_set("sync_lock_ts", now)
    return True


def _sync_lock_release():
    _sync_meta_set("sync_lock_ts", 0.0)


def _local_file_hash():
    path = custom_lists_path()
    if not xbmcvfs.exists(path):
        return ""
    try:
        fh = xbmcvfs.File(path)
        raw = fh.read()
        fh.close()
        if isinstance(raw, bytes):
            payload = raw
        else:
            payload = str(raw or "").encode("utf-8")
        return hashlib.sha1(payload).hexdigest()
    except Exception:
        return ""


def _mark_local_mutation():
    now = time.time()
    _sync_meta_set("last_local_change_ts", now)
    _sync_meta_set("auto_sync_suppress_until", now + AUTO_SYNC_SUPPRESS_AFTER_LOCAL_CHANGE_SECONDS)
    _sync_meta_set("dirty", True)
    try:
        github_gist_sync.mark_dirty(github_gist_sync.CUSTOM_LISTS_FILE)
    except Exception:
        pass


def _write_local_lists(data):
    normalized = _normalize_data(data)
    clean = {"version": 1, "lists": [], "deleted_lists": []}
    seen_lists = set()

    for row in (data or {}).get("lists") or []:
        if not isinstance(row, dict):
            continue
        list_id = str(row.get("id") or "").strip()
        name = str(row.get("name") or "").strip()
        if not list_id or not name or list_id in seen_lists:
            continue
        seen_lists.add(list_id)

        out = {
            "id": list_id,
            "name": name,
            "created_at": row.get("created_at") or _now_iso(),
            "updated_at": row.get("updated_at") or _now_iso(),
            "items": [],
            "deleted_items": [],
        }
        owner_device_id = str(row.get("owner_device_id") or "").strip()
        owner_device_name = str(row.get("owner_device_name") or "").strip()
        if owner_device_id:
            out["owner_device_id"] = owner_device_id
            if owner_device_name:
                out["owner_device_name"] = owner_device_name
        for key in LIST_TRAKT_META_FIELDS:
            if key in row and row.get(key) not in (None, ""):
                out[key] = row.get(key)
        seen_items = set()
        for item in row.get("items") or []:
            if not isinstance(item, dict):
                continue
            media_type = str(item.get("media_type") or "").strip().lower()
            tmdb_id = str(item.get("tmdb_id") or item.get("id") or "").strip()
            if media_type not in ("movie", "tv") or not tmdb_id:
                continue
            key = _item_key(media_type, tmdb_id)
            if key in seen_items:
                continue
            seen_items.add(key)
            saved = dict(item)
            saved["media_type"] = media_type
            saved["tmdb_id"] = tmdb_id
            added_by_device_id = str(item.get("added_by_device_id") or "").strip()
            added_by_device_name = str(item.get("added_by_device_name") or "").strip()
            if added_by_device_id:
                saved["added_by_device_id"] = added_by_device_id
                if added_by_device_name:
                    saved["added_by_device_name"] = added_by_device_name
            out["items"].append(saved)

        seen_deleted_items = set()
        for item in row.get("deleted_items") or []:
            if not isinstance(item, dict):
                continue
            media_type = str(item.get("media_type") or "").strip().lower()
            tmdb_id = str(item.get("tmdb_id") or item.get("id") or "").strip()
            deleted_at = str(item.get("deleted_at") or "").strip()
            if media_type not in ("movie", "tv") or not tmdb_id or not deleted_at:
                continue
            key = _item_key(media_type, tmdb_id)
            if key in seen_deleted_items:
                continue
            seen_deleted_items.add(key)
            out["deleted_items"].append({
                "media_type": media_type,
                "tmdb_id": tmdb_id,
                "title": str(item.get("title") or "").strip(),
                "deleted_at": deleted_at,
                "sync_state": str(item.get("sync_state") or "pending").strip() or "pending",
            })

        clean["lists"].append(out)

    seen_deleted = set()
    for row in normalized.get("deleted_lists") or []:
        if not isinstance(row, dict):
            continue
        list_id = str(row.get("id") or "").strip()
        deleted_at = str(row.get("deleted_at") or "").strip()
        if not list_id or not deleted_at or list_id in seen_deleted:
            continue
        seen_deleted.add(list_id)
        clean["deleted_lists"].append({
            "id": list_id,
            "name": str(row.get("name") or "").strip(),
            "deleted_at": deleted_at,
        })
        clean_deleted = clean["deleted_lists"][-1]
        for key in DELETED_LIST_META_FIELDS:
            if key in row and row.get(key) not in (None, ""):
                clean_deleted[key] = row.get(key)

    json_storage.write_json(custom_lists_path(), clean, indent=2, keep_backup=True)
    return clean


def _now_iso():
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _make_list_id(name: str) -> str:
    clean = re.sub(r"[^0-9A-Za-z_-]+", "-", (name or "").strip()).strip("-").lower()
    if not clean:
        clean = "seznam"
    digest = hashlib.md5((name + _now_iso()).encode("utf-8")).hexdigest()[:8]
    return f"{clean[:40]}-{digest}"


def _item_key(media_type: str, tmdb_id) -> str:
    return f"{(media_type or '').strip().lower()}:{str(tmdb_id or '').strip()}"


def load(sync_remote: bool = False):
    path = custom_lists_path()
    if not xbmcvfs.exists(path):
        data = {"version": 1, "lists": [], "deleted_lists": []}
    else:
        try:
            data = json_storage.read_json(
                path,
                {"version": 1, "lists": [], "deleted_lists": []},
                expected_type=dict,
                logger=lambda message: _log(message, xbmc.LOGWARNING),
            )
            data = _normalize_data(data)
        except Exception as e:
            _log(f"load error: {e}", xbmc.LOGERROR)
            data = {"version": 1, "lists": [], "deleted_lists": []}

    if not sync_remote:
        return data

    merged = github_gist_sync.sync_custom_lists_load(data)
    if merged != data:
        try:
            _write_local_lists(merged)
        except Exception as e:
            _log(f"persist merged load error: {e}", xbmc.LOGWARNING)
    return merged


def save(data, sync_remote: bool = False):
    try:
        clean = _write_local_lists(data)
        if sync_remote:
            github_gist_sync.sync_custom_lists_save(clean)
    except Exception as e:
        _log(f"save error: {e}", xbmc.LOGERROR)


def all_lists():
    rows = load(sync_remote=False).get("lists") or []
    rows.sort(key=lambda x: str(x.get("name") or "").casefold())
    return rows


def get_list(list_id: str):
    list_id = str(list_id or "").strip()
    for row in load(sync_remote=False).get("lists") or []:
        if str(row.get("id") or "") == list_id:
            return row
    return None


def replace_list_items_metadata(list_id: str, items: list):
    list_id = str(list_id or "").strip()
    if not list_id or not isinstance(items, list):
        return False

    data = load(sync_remote=False)
    target = None
    for row in data.get("lists") or []:
        if str(row.get("id") or "") == list_id:
            target = row
            break
    if not target:
        return False

    current = target.get("items") if isinstance(target.get("items"), list) else []
    if current == items:
        return True
    target["items"] = items
    save(data, sync_remote=False)
    return True


def can_delete_list(row: dict):
    if not isinstance(row, dict):
        return False, "list_not_found"
    owner_device_id = str(row.get("owner_device_id") or "").strip()
    if not owner_device_id:
        return True, "allowed"
    current_device_id = _device_identity().get("device_id") or ""
    if owner_device_id == current_device_id:
        return True, "allowed"
    return False, "owner_only"


def can_rename_list(row: dict):
    return can_delete_list(row)


def delete_owner_name(row: dict) -> str:
    if not isinstance(row, dict):
        return ""
    return str(row.get("owner_device_name") or "").strip()


def rename_owner_name(row: dict) -> str:
    return delete_owner_name(row)


def can_delete_item(list_row: dict, item: dict):
    if not isinstance(list_row, dict):
        return False, "list_not_found"
    if not isinstance(item, dict):
        return False, "item_not_found"

    can_delete_all, _ = can_delete_list(list_row)
    if can_delete_all:
        return True, "allowed"

    added_by_device_id = str(item.get("added_by_device_id") or "").strip()
    if not added_by_device_id:
        return False, "item_owner_only"
    current_device_id = _device_identity().get("device_id") or ""
    if added_by_device_id == current_device_id:
        return True, "allowed"
    return False, "item_owner_only"


def item_owner_name(item: dict) -> str:
    if not isinstance(item, dict):
        return ""
    return str(item.get("added_by_device_name") or "").strip()


def create_list(name: str, description: str = "", privacy: str = "private"):
    name = str(name or "").strip()
    description = str(description or "").strip()
    privacy = str(privacy or "private").strip().lower()
    if privacy not in ("private", "public", "friends"):
        privacy = "private"
    if not name:
        return None

    data = load(sync_remote=False)
    for row in data.get("lists") or []:
        if str(row.get("name") or "").strip().casefold() == name.casefold():
            changed = False
            if description and str(row.get("trakt_description") or "").strip() != description:
                row["trakt_description"] = description
                changed = True
            if str(row.get("trakt_privacy") or "private").strip().lower() != privacy:
                row["trakt_privacy"] = privacy
                changed = True
            if changed:
                row["updated_at"] = _now_iso()
                save(data, sync_remote=False)
                _mark_local_mutation()
            return row

    now = _now_iso()
    device = _device_identity()
    row = {
        "id": _make_list_id(name),
        "name": name,
        "created_at": now,
        "updated_at": now,
        "owner_device_id": device.get("device_id") or "",
        "owner_device_name": device.get("device_name") or "",
        "trakt_description": description,
        "trakt_privacy": privacy,
        "items": [],
    }
    data.setdefault("lists", []).append(row)
    data["deleted_lists"] = [
        tomb
        for tomb in (data.get("deleted_lists") or [])
        if str((tomb or {}).get("name") or "").strip().casefold() != name.casefold()
    ]
    save(data, sync_remote=False)
    _mark_local_mutation()
    return row


def rename_list(list_id: str, new_name: str, description: str = "", privacy: str = "private"):
    list_id = str(list_id or "").strip()
    new_name = str(new_name or "").strip()
    description = str(description or "").strip()
    privacy = str(privacy or "private").strip().lower()
    if privacy not in ("private", "public", "friends"):
        privacy = "private"
    if not list_id or not new_name:
        return False, "missing"

    data = load(sync_remote=False)
    target = None
    for row in data.get("lists") or []:
        if str(row.get("id") or "") == list_id:
            target = row
            continue
        if str(row.get("name") or "").strip().casefold() == new_name.casefold():
            return False, "duplicate"

    if not target:
        return False, "list_not_found"
    allowed, mode = can_rename_list(target)
    if not allowed:
        return False, mode

    target["name"] = new_name
    target["trakt_description"] = description
    target["trakt_privacy"] = privacy
    target["updated_at"] = _now_iso()
    save(data, sync_remote=False)
    _mark_local_mutation()
    return True, "renamed"


def add_item(list_id: str, item: dict):
    list_id = str(list_id or "").strip()
    media_type = str((item or {}).get("media_type") or "").strip().lower()
    tmdb_id = str((item or {}).get("tmdb_id") or (item or {}).get("id") or "").strip()
    if not list_id or media_type not in ("movie", "tv") or not tmdb_id:
        return False, "missing"

    data = load(sync_remote=False)
    target = None
    for row in data.get("lists") or []:
        if str(row.get("id") or "") == list_id:
            target = row
            break
    if not target:
        return False, "list_not_found"

    items = target.setdefault("items", [])
    key = _item_key(media_type, tmdb_id)
    now = _now_iso()
    device = _device_identity()
    clean_item = dict(item or {})
    clean_item["media_type"] = media_type
    clean_item["tmdb_id"] = tmdb_id
    clean_item["added_at"] = clean_item.get("added_at") or now
    clean_item["added_by_device_id"] = str(clean_item.get("added_by_device_id") or device.get("device_id") or "").strip()
    clean_item["added_by_device_name"] = str(clean_item.get("added_by_device_name") or device.get("device_name") or "").strip()
    target["deleted_items"] = [
        deleted for deleted in (target.get("deleted_items") or [])
        if _item_key((deleted or {}).get("media_type"), (deleted or {}).get("tmdb_id")) != key
    ]

    for idx, existing in enumerate(items):
        if _item_key(existing.get("media_type"), existing.get("tmdb_id")) == key:
            clean_item["added_at"] = existing.get("added_at") or now
            clean_item["added_by_device_id"] = str(existing.get("added_by_device_id") or clean_item.get("added_by_device_id") or "").strip()
            clean_item["added_by_device_name"] = str(existing.get("added_by_device_name") or clean_item.get("added_by_device_name") or "").strip()
            items[idx] = clean_item
            target["updated_at"] = now
            save(data, sync_remote=False)
            _mark_local_mutation()
            return True, "updated"

    items.append(clean_item)
    target["updated_at"] = now
    save(data, sync_remote=False)
    _mark_local_mutation()
    return True, "added"


def delete_list(list_id: str):
    list_id = str(list_id or "").strip()
    if not list_id:
        return False, "missing"
    data = load(sync_remote=False)
    removed_row = None
    for row in data.get("lists") or []:
        if str(row.get("id") or "") == list_id:
            removed_row = row
            break
    if not removed_row:
        return False, "list_not_found"
    allowed, mode = can_delete_list(removed_row)
    if not allowed:
        return False, mode
    old_len = len(data.get("lists") or [])
    data["lists"] = [row for row in data.get("lists") or [] if str(row.get("id") or "") != list_id]
    changed = len(data["lists"]) != old_len
    if changed:
        data.setdefault("deleted_lists", [])
        now = _now_iso()
        data["deleted_lists"] = [
            tomb
            for tomb in data.get("deleted_lists") or []
            if str((tomb or {}).get("id") or "") != list_id
        ]
        data["deleted_lists"].append({
            "id": list_id,
            "name": str((removed_row or {}).get("name") or "").strip(),
            "deleted_at": now,
        })
        deleted_row = data["deleted_lists"][-1]
        for key in DELETED_LIST_META_FIELDS:
            value = (removed_row or {}).get(key)
            if value not in (None, ""):
                deleted_row[key] = value
        if str((removed_row or {}).get("trakt_slug") or "").strip():
            deleted_row["sync_state"] = "pending"
        save(data, sync_remote=False)
        _mark_local_mutation()
        return True, "deleted"
    return False, "not_changed"


def delete_item(list_id: str, media_type: str, tmdb_id: str):
    list_id = str(list_id or "").strip()
    media_type = str(media_type or "").strip().lower()
    tmdb_id = str(tmdb_id or "").strip()
    if not list_id or media_type not in ("movie", "tv") or not tmdb_id:
        return False, "missing"

    data = load(sync_remote=False)
    target = None
    for row in data.get("lists") or []:
        if str(row.get("id") or "") == list_id:
            target = row
            break
    if not target:
        return False, "list_not_found"

    items = target.get("items") or []
    found_item = None
    for item in items:
        if _item_key(item.get("media_type"), item.get("tmdb_id")) == _item_key(media_type, tmdb_id):
            found_item = item
            break
    if not found_item:
        return False, "item_not_found"

    allowed, mode = can_delete_item(target, found_item)
    if not allowed:
        return False, mode

    old_len = len(items)
    target["items"] = [
        item for item in items
        if _item_key(item.get("media_type"), item.get("tmdb_id")) != _item_key(media_type, tmdb_id)
    ]
    if len(target["items"]) == old_len:
        return False, "not_changed"

    now = _now_iso()
    target["updated_at"] = now
    target.setdefault("deleted_items", [])
    target["deleted_items"] = [
        deleted for deleted in (target.get("deleted_items") or [])
        if _item_key((deleted or {}).get("media_type"), (deleted or {}).get("tmdb_id")) != _item_key(media_type, tmdb_id)
    ]
    target["deleted_items"].append({
        "media_type": media_type,
        "tmdb_id": tmdb_id,
        "title": str((found_item or {}).get("title") or "").strip(),
        "deleted_at": now,
        "sync_state": "pending",
    })
    save(data, sync_remote=False)
    _mark_local_mutation()
    return True, "deleted"


def sync_with_remote():
    local_data = load(sync_remote=False)
    try:
        github_gist_sync.cleanup_custom_lists_cache()
    except Exception:
        pass
    merged = github_gist_sync.sync_custom_lists_load(local_data, force_remote=True)
    if merged != local_data:
        try:
            _write_local_lists(merged)
        except Exception as e:
            _log(f"sync persist merge error: {e}", xbmc.LOGWARNING)
    try:
        github_gist_sync.sync_custom_lists_save(merged)
        github_gist_sync.cleanup_custom_lists_cache()
    except Exception as e:
        _log(f"sync push error: {e}", xbmc.LOGWARNING)
    return merged


def service_sync_once(force: bool = False):
    if not _addon_setting_enabled("gist_sync_custom_lists"):
        return False, "disabled"

    now = time.time()
    meta = _sync_meta_load()
    dirty = bool(meta.get("dirty", False))
    current_hash = _local_file_hash()
    last_synced_hash = str(meta.get("last_service_local_hash") or "").strip()
    if current_hash and current_hash != last_synced_hash:
        dirty = True

    try:
        last_local_change_ts = float(meta.get("last_local_change_ts", 0.0) or 0.0)
    except Exception:
        last_local_change_ts = 0.0
    try:
        last_service_sync_ts = float(meta.get("last_service_sync_ts", 0.0) or 0.0)
    except Exception:
        last_service_sync_ts = 0.0

    if not force:
        if dirty and last_local_change_ts > 0 and (now - last_local_change_ts) < SERVICE_SYNC_LOCAL_CHANGE_DELAY_SECONDS:
            return False, "waiting_local_change"
        if (not dirty) and last_service_sync_ts > 0 and (now - last_service_sync_ts) < SERVICE_SYNC_INTERVAL_SECONDS:
            return False, "not_due"

    if not _sync_lock_acquire():
        return False, "busy"

    try:
        _log(f"service sync start: force={force} dirty={dirty}")
        sync_with_remote()
        meta = _sync_meta_load()
        meta["dirty"] = False
        meta["last_service_sync_ts"] = time.time()
        meta["last_service_sync_ok"] = True
        meta["last_service_sync_error"] = ""
        meta["last_service_local_hash"] = _local_file_hash()
        _sync_meta_save(meta)
        _log("service sync finished")
        return True, "synced"
    except Exception as e:
        meta = _sync_meta_load()
        meta["last_service_sync_ok"] = False
        meta["last_service_sync_error"] = repr(e)
        meta["last_service_sync_ts"] = time.time()
        _sync_meta_save(meta)
        _log(f"service sync error: {e}", xbmc.LOGWARNING)
        return False, "error"
    finally:
        _sync_lock_release()


def auto_sync_if_due(force: bool = False):
    if not _addon_setting_enabled("gist_sync_custom_lists"):
        _log("auto sync skipped: disabled")
        return False, "disabled"

    now = time.time()
    try:
        last_sync_ts = float(_sync_meta_get("last_auto_sync_ts", 0.0) or 0.0)
    except Exception:
        last_sync_ts = 0.0
    try:
        suppress_until = float(_sync_meta_get("auto_sync_suppress_until", 0.0) or 0.0)
    except Exception:
        suppress_until = 0.0

    if (not force) and suppress_until > now:
        _log("auto sync skipped: suppressed")
        return False, "suppressed"

    if (not force) and last_sync_ts > 0 and (now - last_sync_ts) < AUTO_SYNC_INTERVAL_SECONDS:
        _log("auto sync skipped: not due")
        return False, "not_due"

    if not _sync_lock_acquire():
        _log("auto sync skipped: busy")
        return False, "busy"

    try:
        _log(f"auto sync start: force={force}")
        sync_with_remote()
        _sync_meta_set("last_auto_sync_ts", time.time())
        _sync_meta_set("last_auto_sync_ok", True)
        _sync_meta_set("dirty", False)
        _log("auto sync finished")
        return True, "synced"
    except Exception as e:
        _log(f"auto sync error: {e}", xbmc.LOGWARNING)
        _sync_meta_set("last_auto_sync_ok", False)
        return False, "error"
    finally:
        _sync_lock_release()
