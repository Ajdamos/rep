# -*- coding: utf-8 -*-
import hashlib

import xbmc

from resources.lib import i18n
from resources.lib import tmdb_custom_lists
from resources.lib import trakt_client

_TRAKT_LIST_OVERVIEW_TTL = 300.0


def _log(msg, level=xbmc.LOGINFO):
    try:
        xbmc.log(f"[IKARUS][TRAKT LIST SYNC] {msg}", level)
    except Exception:
        pass


def _clean(text) -> str:
    return str(text or "").strip()


_LOCAL_LATIN_CHARS = (
    "\u00c1\u00e1\u010c\u010d\u010e\u010f\u00c9\u00e9\u011a\u011b"
    "\u00cd\u00ed\u0147\u0148\u00d3\u00f3\u0158\u0159\u0160\u0161"
    "\u0164\u0165\u00da\u00fa\u016e\u016f\u00dd\u00fd\u017d\u017e"
    "\u013d\u013e\u0139\u013a\u0154\u0155\u00d4\u00f4"
    "\u00c4\u00e4\u00d6\u00f6\u00dc\u00fc"
)


def _has_local_latin_chars(text: str) -> bool:
    return any(ch in _LOCAL_LATIN_CHARS for ch in _clean(text))


def _has_protected_local_title(item: dict) -> bool:
    if not isinstance(item, dict) or not item.get("_title_is_local"):
        return False
    title = _clean(item.get("title"))
    if not title:
        return False
    original = _clean(item.get("original_title"))
    if original and title.casefold() != original.casefold():
        if _has_local_latin_chars(original) and not _has_local_latin_chars(title):
            return False
        if not item.get("_title_source_tmdb_detail") and not _has_local_latin_chars(title) and not _has_local_latin_chars(original):
            return False
    return True


def _name_key(name: str) -> str:
    return _clean(name).casefold()


def _list_id_for_remote(slug: str, name: str) -> str:
    slug = _clean(slug)
    name = _clean(name) or i18n.T(31724, "Trakt.TV list")
    if slug:
        digest = hashlib.md5(slug.encode("utf-8")).hexdigest()[:8]
        return f"trakt-{slug[:40]}-{digest}"
    return tmdb_custom_lists._make_list_id(name)


def _first_image_url(value) -> str:
    if isinstance(value, str):
        text = value.strip()
        return text if text.lower().startswith(("http://", "https://")) else ""
    if isinstance(value, list):
        for item in value:
            found = _first_image_url(item)
            if found:
                return found
        return ""
    if isinstance(value, dict):
        for key in ("full", "medium", "thumb", "url", "poster", "posters", "fanart", "background", "backdrop", "backdrops"):
            found = _first_image_url(value.get(key))
            if found:
                return found
        for item in value.values():
            found = _first_image_url(item)
            if found:
                return found
    return ""


def _tmdb_enrich_item(item: dict) -> dict:
    item = dict(item or {})
    media_type = _clean(item.get("media_type")).lower()
    tmdb_id = _clean(item.get("tmdb_id"))
    if media_type not in ("movie", "tv") or not tmdb_id:
        return item

    try:
        from resources.lib import TMDB_knihovna as tmdb_lib

        if media_type == "movie":
            detail = tmdb_lib._tmdb_movie_listing_detail(tmdb_id)
            title = (
                tmdb_lib._tmdb_display_title(detail, "movie", item.get("title") or i18n.T(30748, "Untitled"))
                or item.get("title")
                or i18n.T(30748, "Untitled")
            )
            original = _clean((detail or {}).get("original_title") or item.get("original_title"))
            premiered = _clean((detail or {}).get("release_date"))
            fallback_icon = "DefaultMovies.png"
        else:
            detail = tmdb_lib._tmdb_tv_listing_detail(tmdb_id)
            title = (
                tmdb_lib._tmdb_display_title(detail, "tv", item.get("title") or i18n.T(30748, "Untitled"))
                or item.get("title")
                or i18n.T(30748, "Untitled")
            )
            original = _clean((detail or {}).get("original_name") or item.get("original_title"))
            premiered = _clean((detail or {}).get("first_air_date"))
            fallback_icon = "DefaultTVShows.png"

        if not isinstance(detail, dict) or not detail:
            return item

        art = tmdb_lib._tmdb_media_art(detail)
        year = premiered[:4] if len(premiered) >= 4 and premiered[:4].isdigit() else _clean(item.get("year"))
        trakt_title = _clean(item.get("trakt_title") or item.get("title"))

        item["title"] = _clean(title) or item.get("title") or i18n.T(30748, "Untitled")
        if trakt_title and trakt_title != item["title"]:
            item["trakt_title"] = trakt_title
        item["original_title"] = original
        item["year"] = year
        item["poster"] = _clean((art or {}).get("poster") or item.get("poster")) or fallback_icon
        item["plot"] = (detail or {}).get("overview") or item.get("plot") or ""
        item["rating"] = (detail or {}).get("vote_average", item.get("rating") or 0)
        item["_title_is_local"] = True
        item["_title_source_tmdb_detail"] = True
        item["_local_origin"] = "tmdb"
    except Exception as e:
        _log(f"tmdb enrich failed type={media_type} id={tmdb_id}: {e}", xbmc.LOGWARNING)
    return item


def _trakt_meta_from_list(row: dict) -> dict:
    if not isinstance(row, dict):
        return {}
    ids = row.get("ids") if isinstance(row.get("ids"), dict) else {}
    user = row.get("user") if isinstance(row.get("user"), dict) else {}
    slug = _clean(ids.get("slug"))
    name = _clean(row.get("name")) or i18n.T(31724, "Trakt.TV list")
    if not slug:
        return {}
    return {
        "name": name,
        "trakt_slug": slug,
        "trakt_id": _clean(ids.get("trakt")),
        "trakt_username": _clean(user.get("username")) or "me",
        "trakt_privacy": _clean(row.get("privacy")) or "private",
        "trakt_description": _clean(row.get("description")),
        "trakt_item_count": row.get("item_count", 0),
    }


def _apply_trakt_meta(local_row: dict, meta: dict):
    if not isinstance(local_row, dict) or not meta:
        return
    remote_name = _clean(meta.get("name"))
    if remote_name:
        local_row["name"] = remote_name
    for key in tmdb_custom_lists.LIST_TRAKT_META_FIELDS:
        if key in meta:
            local_row[key] = meta[key]
    local_row["sync_state"] = "synced"
    local_row["last_trakt_sync_at"] = tmdb_custom_lists._now_iso()


def _item_key(item: dict) -> str:
    return tmdb_custom_lists._item_key(item.get("media_type"), item.get("tmdb_id"))


def _local_item_from_trakt(row: dict) -> dict:
    if not isinstance(row, dict):
        return {}
    media_type = _clean(row.get("type")).lower()
    if media_type == "movie":
        media = row.get("movie") if isinstance(row.get("movie"), dict) else {}
        local_type = "movie"
        original_key = "original_title"
        date_key = "released"
    elif media_type in ("show", "tv", "series"):
        media = row.get("show") if isinstance(row.get("show"), dict) else {}
        local_type = "tv"
        original_key = "original_name"
        date_key = "first_aired"
    else:
        return {}

    ids = media.get("ids") if isinstance(media.get("ids"), dict) else {}
    tmdb_id = _clean(ids.get("tmdb"))
    if not tmdb_id:
        return {}

    year = _clean(media.get("year"))
    if not year:
        premiered = _clean(media.get(date_key) or media.get("released") or media.get("first_aired"))
        year = premiered[:4] if len(premiered) >= 4 and premiered[:4].isdigit() else ""

    item = {
        "media_type": local_type,
        "tmdb_id": tmdb_id,
            "title": _clean(media.get("title")) or i18n.T(30748, "Untitled"),
        "trakt_title": _clean(media.get("title")) or "",
        "year": year,
        "plot": _clean(media.get("overview")),
        "rating": media.get("rating") or 0,
        "trakt_id": _clean(ids.get("trakt")),
        "added_at": _clean(row.get("listed_at")) or tmdb_custom_lists._now_iso(),
    }
    original = _clean(media.get(original_key))
    if original:
        item["original_title"] = original
    poster = _first_image_url(media.get("images") or row.get("images") or {})
    if poster:
        item["poster"] = poster
    return _tmdb_enrich_item(item)


def _fetch_all_user_list_items(slug: str, username: str = "me") -> list:
    rows = []
    page = 1
    username = _clean(username) or "me"
    while page <= 50:
        try:
            page_rows = trakt_client.fetch_list_items(username, slug, limit=100, page=page)
        except Exception as e:
            _log(f"list item page fetch failed slug={slug} username={username} page={page}: {e}", xbmc.LOGWARNING)
            break
        if not page_rows:
            break
        rows.extend(page_rows)
        if len(page_rows) < 100:
            break
        page += 1
    return rows


def _deleted_item_keys(row: dict) -> set:
    keys = set()
    for item in (row or {}).get("deleted_items") or []:
        if not isinstance(item, dict):
            continue
        key = tmdb_custom_lists._item_key(item.get("media_type"), item.get("tmdb_id"))
        if key:
            keys.add(key)
    return keys


def _clear_deleted_item(row: dict, media_type: str, tmdb_id: str) -> bool:
    if not isinstance(row, dict):
        return False
    key = tmdb_custom_lists._item_key(media_type, tmdb_id)
    old = [item for item in (row.get("deleted_items") or []) if isinstance(item, dict)]
    row["deleted_items"] = [
        item for item in old
        if tmdb_custom_lists._item_key(item.get("media_type"), item.get("tmdb_id")) != key
    ]
    return len(row["deleted_items"]) != len(old)


def _merge_item_metadata(current: dict, incoming: dict) -> dict:
    current = dict(current or {})
    incoming = dict(incoming or {})
    if not incoming:
        return current

    protected_title = _has_protected_local_title(current)
    current_title = _clean(current.get("title"))
    incoming_title = _clean(incoming.get("title"))
    incoming_is_tmdb = bool(incoming.get("_title_source_tmdb_detail") or incoming.get("_title_is_local"))

    for field, value in incoming.items():
        if value in (None, ""):
            continue
        if field == "title":
            if not current_title:
                current[field] = value
            elif protected_title:
                continue
            elif _has_local_latin_chars(current_title) and not _has_local_latin_chars(incoming_title):
                continue
            elif incoming_is_tmdb:
                current[field] = value
            continue
        if field == "original_title":
            if protected_title and _has_local_latin_chars(current.get("original_title")):
                continue
            if incoming_is_tmdb or not current.get(field):
                current[field] = value
            continue
        if field in ("_title_is_local", "_title_source_tmdb_detail", "_local_origin"):
            if value:
                current[field] = value
            continue
        if incoming_is_tmdb and field in ("poster", "plot", "rating", "year"):
            current[field] = value
            continue
        if not current.get(field):
            current[field] = value

    return current


def _merge_items(local_row: dict, remote_rows: list) -> int:
    local_items = local_row.setdefault("items", []) if isinstance(local_row, dict) else []
    deleted_keys = _deleted_item_keys(local_row)
    items = [item for item in (local_items or []) if isinstance(item, dict)]
    by_key = {}
    for item in items:
        key = _item_key(item)
        if key:
            by_key[key] = item

    added = 0
    for row in remote_rows or []:
        item = _local_item_from_trakt(row)
        key = _item_key(item)
        if not key:
            continue
        if key in deleted_keys:
            continue
        if key in by_key:
            by_key[key] = _merge_item_metadata(by_key[key], item)
        else:
            by_key[key] = item
            added += 1

    local_items[:] = list(by_key.values())
    return added


def sync_local_list_items_from_trakt(list_id: str = "", slug: str = "", username: str = "") -> dict:
    if not trakt_client.is_connected():
        return {"ok": False, "mode": "not_connected"}

    list_id = _clean(list_id)
    slug = _clean(slug)
    username = _clean(username) or "me"
    if not list_id and not slug:
        return {"ok": False, "mode": "missing"}

    data = tmdb_custom_lists.load(sync_remote=False)
    target = None
    for row in data.get("lists") or []:
        if not isinstance(row, dict):
            continue
        if list_id and _clean(row.get("id")) == list_id:
            target = row
            break
        if slug and _clean(row.get("trakt_slug")) == slug:
            target = row
            break
    if not target:
        return {"ok": False, "mode": "list_not_found"}

    slug = _clean(target.get("trakt_slug")) or slug
    username = _clean(target.get("trakt_username")) or username or "me"
    if not slug:
        return {"ok": False, "mode": "missing_slug"}

    remote_items = _fetch_all_user_list_items(slug, username=username)
    if not remote_items and username != "me":
        remote_items = _fetch_all_user_list_items(slug, username="me")

    pulled = _merge_items(target, remote_items)
    target["trakt_item_count"] = len(remote_items or [])
    target["last_trakt_sync_at"] = tmdb_custom_lists._now_iso()
    tmdb_custom_lists.save(data, sync_remote=False)
    _log(
        f"single list items pulled slug={slug} username={username} remote={len(remote_items or [])} "
        f"local={len(target.get('items') or [])} pulled={pulled}",
        xbmc.LOGINFO,
    )
    return {
        "ok": True,
        "mode": "synced",
        "slug": slug,
        "pulled_items": pulled,
        "remote_items": len(remote_items or []),
        "local_items": len(target.get("items") or []),
    }


def _find_local_row(data: dict, meta: dict):
    slug = _clean(meta.get("trakt_slug"))
    name = _clean(meta.get("name"))
    for row in data.get("lists") or []:
        if slug and _clean((row or {}).get("trakt_slug")) == slug:
            return row
    for row in data.get("lists") or []:
        if name and _name_key((row or {}).get("name")) == _name_key(name):
            return row
    return None


def _deleted_list_matches(meta: dict, tomb: dict) -> bool:
    if not isinstance(meta, dict) or not isinstance(tomb, dict):
        return False
    meta_slug = _clean(meta.get("trakt_slug"))
    tomb_slug = _clean(tomb.get("trakt_slug"))
    if meta_slug and tomb_slug and meta_slug == tomb_slug:
        return True
    meta_id = _clean(meta.get("trakt_id"))
    tomb_id = _clean(tomb.get("trakt_id"))
    if meta_id and tomb_id and meta_id == tomb_id:
        return True
    meta_name = _name_key(meta.get("name"))
    tomb_name = _name_key(tomb.get("name"))
    return bool(meta_name and tomb_name and meta_name == tomb_name)


def _find_deleted_list_tombstone(data: dict, list_id: str = "", slug: str = "", name: str = ""):
    list_id = _clean(list_id)
    slug = _clean(slug)
    name_key = _name_key(name)
    for tomb in (data.get("deleted_lists") or []):
        if not isinstance(tomb, dict):
            continue
        if list_id and _clean(tomb.get("id")) == list_id:
            return tomb
        if slug and _clean(tomb.get("trakt_slug")) == slug:
            return tomb
        if name_key and _name_key(tomb.get("name")) == name_key:
            return tomb
    return None


def _push_items_to_trakt(row: dict) -> int:
    slug = _clean((row or {}).get("trakt_slug"))
    if not slug:
        return 0
    pushed = 0
    for item in (row or {}).get("items") or []:
        media_type = _clean((item or {}).get("media_type")).lower()
        tmdb_id = _clean((item or {}).get("tmdb_id"))
        if media_type not in ("movie", "tv") or not tmdb_id:
            continue
        try:
            trakt_client.add_item_to_list(slug, media_type, tmdb_id)
            pushed += 1
        except Exception as e:
            _log(f"item push failed slug={slug} type={media_type} id={tmdb_id}: {e}", xbmc.LOGWARNING)
    return pushed


def _push_deleted_items_to_trakt(row: dict) -> int:
    slug = _clean((row or {}).get("trakt_slug"))
    if not slug:
        return 0
    deleted_items = [item for item in (row or {}).get("deleted_items") or [] if isinstance(item, dict)]
    pushed = 0
    keep = []
    for item in deleted_items:
        media_type = _clean(item.get("media_type")).lower()
        tmdb_id = _clean(item.get("tmdb_id"))
        if media_type not in ("movie", "tv") or not tmdb_id:
            continue
        try:
            trakt_client.remove_item_from_list(slug, media_type, tmdb_id)
            pushed += 1
        except Exception as e:
            item["sync_state"] = "pending"
            keep.append(item)
            _log(f"item delete push failed slug={slug} type={media_type} id={tmdb_id}: {e}", xbmc.LOGWARNING)
    row["deleted_items"] = keep
    if pushed:
        row["last_trakt_sync_at"] = tmdb_custom_lists._now_iso()
    return pushed


def _push_deleted_lists_to_trakt(data: dict) -> int:
    deleted_lists = [row for row in (data or {}).get("deleted_lists") or [] if isinstance(row, dict)]
    pushed = 0
    keep = []
    for row in deleted_lists:
        slug = _clean(row.get("trakt_slug"))
        if not slug:
            continue
        try:
            trakt_client.delete_user_list(slug)
            pushed += 1
        except Exception as e:
            row["sync_state"] = "pending"
            keep.append(row)
            _log(f"list delete push failed slug={slug}: {e}", xbmc.LOGWARNING)

    data["deleted_lists"] = [
        row for row in deleted_lists
        if not _clean(row.get("trakt_slug"))
    ] + keep
    return pushed


def _remote_meta_by_name(name: str) -> dict:
    wanted = _name_key(name)
    if not wanted:
        return {}
    try:
        for remote_row in trakt_client.fetch_user_lists() or []:
            meta = _trakt_meta_from_list(remote_row)
            if meta and _name_key(meta.get("name")) == wanted:
                return meta
    except Exception as e:
        _log(f"remote list lookup failed name={name}: {e}", xbmc.LOGWARNING)
    return {}


def _ensure_remote_list(row: dict) -> bool:
    if not isinstance(row, dict):
        return False
    if _clean(row.get("trakt_slug")):
        return True

    name = _clean(row.get("name"))
    if not name:
        return False

    meta = _remote_meta_by_name(name)
    if not meta:
        created_row = trakt_client.create_user_list(
            name,
            description=_clean(row.get("trakt_description")),
            privacy=_clean(row.get("trakt_privacy")) or "private",
        )
        meta = _trakt_meta_from_list(created_row)
    if not meta:
        meta = _remote_meta_by_name(name)
    if not meta:
        return False
    _apply_trakt_meta(row, meta)
    return True


def sync_local_list_to_trakt(list_id: str, only_item: dict = None) -> dict:
    if not trakt_client.is_connected():
        return {"ok": False, "mode": "not_connected"}

    list_id = _clean(list_id)
    if not list_id:
        return {"ok": False, "mode": "missing"}

    data = tmdb_custom_lists.load(sync_remote=False)
    target = None
    for row in data.get("lists") or []:
        if _clean((row or {}).get("id")) == list_id:
            target = row
            break
    if not target:
        return {"ok": False, "mode": "list_not_found"}

    try:
        had_remote = bool(_clean(target.get("trakt_slug")))
        if not _ensure_remote_list(target):
            target["sync_state"] = "pending"
            tmdb_custom_lists.save(data, sync_remote=False)
            return {"ok": False, "mode": "remote_list_failed"}

        pushed = 0
        if isinstance(only_item, dict) and had_remote:
            slug = _clean(target.get("trakt_slug"))
            media_type = _clean(only_item.get("media_type")).lower()
            tmdb_id = _clean(only_item.get("tmdb_id"))
            if slug and media_type in ("movie", "tv") and tmdb_id:
                trakt_client.add_item_to_list(slug, media_type, tmdb_id)
                pushed = 1
        else:
            pushed = _push_items_to_trakt(target)

        target["sync_state"] = "synced"
        target["last_trakt_sync_at"] = tmdb_custom_lists._now_iso()
        tmdb_custom_lists.save(data, sync_remote=False)
        return {"ok": True, "mode": "synced", "pushed_items": pushed}
    except Exception as e:
        target["sync_state"] = "pending"
        tmdb_custom_lists.save(data, sync_remote=False)
        _log(f"local list push failed list_id={list_id}: {e}", xbmc.LOGWARNING)
        return {"ok": False, "mode": "error"}


def sync_local_list_metadata_to_trakt(list_id: str) -> dict:
    if not trakt_client.is_connected():
        return {"ok": False, "mode": "not_connected"}

    list_id = _clean(list_id)
    if not list_id:
        return {"ok": False, "mode": "missing"}

    data = tmdb_custom_lists.load(sync_remote=False)
    target = None
    for row in data.get("lists") or []:
        if _clean((row or {}).get("id")) == list_id:
            target = row
            break
    if not target:
        return {"ok": False, "mode": "list_not_found"}

    try:
        if not _ensure_remote_list(target):
            target["sync_state"] = "pending"
            tmdb_custom_lists.save(data, sync_remote=False)
            return {"ok": False, "mode": "remote_list_failed"}

        slug = _clean(target.get("trakt_slug"))
        if not slug:
            target["sync_state"] = "pending"
            tmdb_custom_lists.save(data, sync_remote=False)
            return {"ok": False, "mode": "missing_slug"}

        row = trakt_client.update_user_list(
            slug,
            _clean(target.get("name")),
            description=_clean(target.get("trakt_description")),
            privacy=_clean(target.get("trakt_privacy")) or "private",
        )
        meta = _trakt_meta_from_list(row)
        if not meta:
            meta = _remote_meta_by_name(target.get("name"))
        if meta:
            _apply_trakt_meta(target, meta)
        else:
            target["sync_state"] = "synced"
            target["last_trakt_sync_at"] = tmdb_custom_lists._now_iso()
        tmdb_custom_lists.save(data, sync_remote=False)
        return {"ok": True, "mode": "synced"}
    except Exception as e:
        target["sync_state"] = "pending"
        tmdb_custom_lists.save(data, sync_remote=False)
        _log(f"local list metadata push failed list_id={list_id}: {e}", xbmc.LOGWARNING)
        return {"ok": False, "mode": "error"}


def sync_local_item_delete_to_trakt(list_id: str, media_type: str, tmdb_id: str) -> dict:
    if not trakt_client.is_connected():
        return {"ok": False, "mode": "not_connected"}

    list_id = _clean(list_id)
    media_type = _clean(media_type).lower()
    tmdb_id = _clean(tmdb_id)
    if not list_id or media_type not in ("movie", "tv") or not tmdb_id:
        return {"ok": False, "mode": "missing"}

    data = tmdb_custom_lists.load(sync_remote=False)
    target = None
    for row in data.get("lists") or []:
        if _clean((row or {}).get("id")) == list_id:
            target = row
            break
    if not target:
        return {"ok": False, "mode": "list_not_found"}

    slug = _clean(target.get("trakt_slug"))
    if not slug:
        try:
            _ensure_remote_list(target)
        except Exception as e:
            _log(f"delete remote list ensure failed list_id={list_id}: {e}", xbmc.LOGWARNING)
        slug = _clean(target.get("trakt_slug"))
    if not slug:
        _clear_deleted_item(target, media_type, tmdb_id)
        target["last_trakt_sync_at"] = tmdb_custom_lists._now_iso()
        tmdb_custom_lists.save(data, sync_remote=False)
        return {"ok": True, "mode": "no_remote"}

    try:
        trakt_client.remove_item_from_list(slug, media_type, tmdb_id)
        _clear_deleted_item(target, media_type, tmdb_id)
        target["sync_state"] = "synced"
        target["last_trakt_sync_at"] = tmdb_custom_lists._now_iso()
        tmdb_custom_lists.save(data, sync_remote=False)
        return {"ok": True, "mode": "synced"}
    except Exception as e:
        target["sync_state"] = "pending"
        tmdb_custom_lists.save(data, sync_remote=False)
        _log(f"local item delete push failed list_id={list_id} type={media_type} id={tmdb_id}: {e}", xbmc.LOGWARNING)
        return {"ok": False, "mode": "error"}


def sync_local_list_delete_to_trakt(list_id: str) -> dict:
    if not trakt_client.is_connected():
        return {"ok": False, "mode": "not_connected"}

    list_id = _clean(list_id)
    if not list_id:
        return {"ok": False, "mode": "missing"}

    data = tmdb_custom_lists.load(sync_remote=False)
    tomb = _find_deleted_list_tombstone(data, list_id=list_id)
    if not tomb:
        return {"ok": False, "mode": "list_not_found"}

    slug = _clean(tomb.get("trakt_slug"))
    if not slug:
        data["deleted_lists"] = [
            row for row in (data.get("deleted_lists") or [])
            if not (isinstance(row, dict) and _clean(row.get("id")) == list_id)
        ]
        tmdb_custom_lists.save(data, sync_remote=False)
        return {"ok": True, "mode": "no_remote"}

    try:
        trakt_client.delete_user_list(slug)
        data["deleted_lists"] = [
            row for row in (data.get("deleted_lists") or [])
            if not (isinstance(row, dict) and _clean(row.get("id")) == list_id)
        ]
        tmdb_custom_lists.save(data, sync_remote=False)
        return {"ok": True, "mode": "synced"}
    except Exception as e:
        tomb["sync_state"] = "pending"
        tmdb_custom_lists.save(data, sync_remote=False)
        _log(f"local list delete push failed list_id={list_id} slug={slug}: {e}", xbmc.LOGWARNING)
        return {"ok": False, "mode": "error"}


def sync_now(fetch_items: bool = True, push_items: bool = True) -> dict:
    if not trakt_client.is_connected():
        return {"ok": False, "mode": "not_connected"}

    data = tmdb_custom_lists.load(sync_remote=False)
    data.setdefault("lists", [])
    data.setdefault("deleted_lists", [])
    created = 0
    linked = 0
    pulled_items = 0
    pushed_items = 0
    pushed_deletes = 0
    pushed_deleted_lists = 0

    if push_items:
        pushed_deleted_lists += _push_deleted_lists_to_trakt(data)

    try:
        remote_rows = trakt_client.fetch_user_lists()
    except Exception as e:
        _log(f"user list overview fetch failed: {e}", xbmc.LOGWARNING)
        tmdb_custom_lists.save(data, sync_remote=False)
        return {
            "ok": False,
            "mode": "remote_error",
            "error": str(e),
            "linked": 0,
            "created": created,
            "pulled_items": 0,
            "pushed_items": 0,
            "pushed_deletes": 0,
            "pushed_deleted_lists": pushed_deleted_lists,
            "removed_remote_lists": 0,
        }
    remote_meta = []
    for remote_row in remote_rows or []:
        meta = _trakt_meta_from_list(remote_row)
        if meta:
            if _find_deleted_list_tombstone(data, slug=meta.get("trakt_slug"), name=meta.get("name")):
                continue
            remote_meta.append(meta)

    remote_slug_set = { _clean(meta.get("trakt_slug")) for meta in remote_meta if _clean(meta.get("trakt_slug")) }
    remote_id_set = { _clean(meta.get("trakt_id")) for meta in remote_meta if _clean(meta.get("trakt_id")) }
    kept_rows = []
    removed_remote_lists = 0
    for row in data.get("lists") or []:
        if not isinstance(row, dict):
            continue
        slug = _clean(row.get("trakt_slug"))
        trakt_id = _clean(row.get("trakt_id"))
        if slug and slug not in remote_slug_set and (not trakt_id or trakt_id not in remote_id_set):
            removed_remote_lists += 1
            continue
        kept_rows.append(row)
    data["lists"] = kept_rows

    for meta in remote_meta:
        row = _find_local_row(data, meta)
        if row is None:
            now = tmdb_custom_lists._now_iso()
            row = {
                "id": _list_id_for_remote(meta.get("trakt_slug"), meta.get("name")),
                "name": meta.get("name") or i18n.T(31724, "Trakt.TV list"),
                "created_at": now,
                "updated_at": now,
                "items": [],
            }
            data["lists"].append(row)
        _apply_trakt_meta(row, meta)
        linked += 1
        if fetch_items:
            try:
                slug = meta.get("trakt_slug")
                username = meta.get("trakt_username") or "me"
                remote_items = _fetch_all_user_list_items(slug, username=username)
                if not remote_items and username != "me":
                    remote_items = _fetch_all_user_list_items(slug, username="me")
                pulled_items += _merge_items(row, remote_items)
                _log(
                    f"items pulled slug={slug} username={username} remote={len(remote_items or [])} local={len(row.get('items') or [])}",
                    xbmc.LOGINFO,
                )
            except Exception as e:
                _log(f"item pull failed slug={meta.get('trakt_slug')}: {e}", xbmc.LOGWARNING)

    for row in data.get("lists") or []:
        if _clean((row or {}).get("trakt_slug")):
            continue
        name = _clean((row or {}).get("name"))
        if not name:
            continue
        try:
            created_row = trakt_client.create_user_list(
                name,
                description=_clean(row.get("trakt_description")),
                privacy=_clean(row.get("trakt_privacy")) or "private",
            )
            meta = _trakt_meta_from_list(created_row)
            if meta:
                _apply_trakt_meta(row, meta)
                created += 1
        except Exception as e:
            row["sync_state"] = "pending"
            _log(f"list create failed name={name}: {e}", xbmc.LOGWARNING)

    if push_items:
        for row in data.get("lists") or []:
            pushed_items += _push_items_to_trakt(row)
            pushed_deletes += _push_deleted_items_to_trakt(row)

    tmdb_custom_lists.save(data, sync_remote=False)
    return {
        "ok": True,
        "mode": "synced",
        "linked": linked,
        "created": created,
        "pulled_items": pulled_items,
        "pushed_items": pushed_items,
        "pushed_deletes": pushed_deletes,
        "pushed_deleted_lists": pushed_deleted_lists,
        "removed_remote_lists": removed_remote_lists,
    }


def sync_overview_if_due(force: bool = False) -> dict:
    if not trakt_client.is_connected():
        return {"ok": False, "mode": "not_connected"}

    now = time.time()
    try:
        last_ts = float(tmdb_custom_lists._sync_meta_get("trakt_lists_overview_sync_ts", 0.0) or 0.0)
    except Exception:
        last_ts = 0.0

    if not force and last_ts > 0 and (now - last_ts) < _TRAKT_LIST_OVERVIEW_TTL:
        return {"ok": True, "mode": "cached"}

    try:
        result = sync_now(fetch_items=False, push_items=False)
    except Exception as e:
        _log(f"overview sync failed: {e}", xbmc.LOGWARNING)
        return {"ok": False, "mode": "remote_error", "error": str(e)}
    if result.get("ok"):
        try:
            tmdb_custom_lists._sync_meta_set("trakt_lists_overview_sync_ts", time.time())
        except Exception:
            pass
    return result
