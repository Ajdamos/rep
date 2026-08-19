# -*- coding: utf-8 -*-
import os
import time

import xbmc
import xbmcaddon
import xbmcvfs

from resources.lib import i18n, json_storage


def _addon():
    try:
        return xbmcaddon.Addon("plugin.video.ikarus")
    except Exception:
        return xbmcaddon.Addon()


def _profile_dir():
    base = xbmcvfs.translatePath(_addon().getAddonInfo("profile"))
    path = os.path.join(base, "tmdb_state")
    try:
        xbmcvfs.mkdirs(path)
    except Exception:
        try:
            os.makedirs(path, exist_ok=True)
        except Exception:
            pass
    return path


def favorites_path():
    return os.path.join(_profile_dir(), "trakt_public_favorites.json")


def _load_data():
    path = favorites_path()
    if not xbmcvfs.exists(path):
        return {"version": 1, "lists": []}
    try:
        data = json_storage.read_json(path, {}, expected_type=dict)
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    rows = data.get("lists")
    if not isinstance(rows, list):
        rows = []
    return {"version": 1, "lists": rows}


def _save_data(data):
    rows = data.get("lists") if isinstance(data, dict) else []
    if not isinstance(rows, list):
        rows = []
    clean = {"version": 1, "lists": rows}
    try:
        json_storage.write_json(favorites_path(), clean, indent=2, keep_backup=True)
        return True
    except Exception as e:
        try:
            xbmc.log(f"[IKARUS][TRAKT FAV] save error: {e}", xbmc.LOGWARNING)
        except Exception:
            pass
        return False


def _key(username: str, slug: str) -> str:
    return f"{str(username or '').strip().lower()}::{str(slug or '').strip().lower()}"


def all_lists():
    rows = []
    seen = set()
    for row in _load_data().get("lists") or []:
        if not isinstance(row, dict):
            continue
        username = str(row.get("username") or "").strip()
        slug = str(row.get("slug") or "").strip()
        name = str(row.get("name") or "").strip()
        key = _key(username, slug)
        if not username or not slug or key in seen:
            continue
        seen.add(key)
        out = dict(row)
        out["username"] = username
        out["slug"] = slug
        out["name"] = name or i18n.T(31724, "Trakt.TV list")
        rows.append(out)
    rows.sort(key=lambda item: (str(item.get("name") or "").lower(), str(item.get("username") or "").lower()))
    return rows


def is_favorite(username: str, slug: str) -> bool:
    key = _key(username, slug)
    return any(_key(row.get("username"), row.get("slug")) == key for row in all_lists())


def add_favorite(row: dict) -> bool:
    if not isinstance(row, dict):
        return False
    username = str(row.get("username") or "").strip()
    slug = str(row.get("slug") or "").strip()
    name = str(row.get("name") or i18n.T(31724, "Trakt.TV list")).strip() or i18n.T(31724, "Trakt.TV list")
    if not username or not slug:
        return False
    data = _load_data()
    rows = [item for item in data.get("lists") or [] if _key(item.get("username"), item.get("slug")) != _key(username, slug)]
    rows.append({
        "username": username,
        "slug": slug,
        "name": name,
        "item_count": row.get("item_count"),
        "type": str(row.get("type") or "").strip().lower(),
        "description": str(row.get("description") or "").strip(),
        "likes": row.get("likes"),
        "comments": row.get("comments"),
        "added_at": int(time.time()),
    })
    data["lists"] = rows
    return _save_data(data)


def remove_favorite(username: str, slug: str) -> bool:
    username = str(username or "").strip()
    slug = str(slug or "").strip()
    if not username or not slug:
        return False
    data = _load_data()
    before = len(data.get("lists") or [])
    data["lists"] = [
        row for row in data.get("lists") or []
        if _key(row.get("username"), row.get("slug")) != _key(username, slug)
    ]
    if len(data["lists"]) == before:
        return False
    return _save_data(data)
