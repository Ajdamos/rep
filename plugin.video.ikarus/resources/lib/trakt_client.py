# -*- coding: utf-8 -*-
from concurrent.futures import ThreadPoolExecutor
import threading
import time
import urllib.parse

import requests
import xbmc
import xbmcaddon
import xbmcgui

from resources.lib import i18n


API_BASE = "https://api.trakt.tv"
API_VERSION = "2"
TIMEOUT = 20
MENU_TIMEOUT = 8
_POST_LOGIN_SYNC_RUNNING = False

# Az bude mit Ikarus vlastni Trakt aplikaci, staci vyplnit tyto dve hodnoty
# a uzivatel uvidi jen tlacitko "Aktivovat Trakt.TV".
IKARUS_TRAKT_CLIENT_ID = "5ab1b07d111335658edecc59fcff117b0336cb27192ff1f2e5eb89dd9bb40c1f"
IKARUS_TRAKT_CLIENT_SECRET = "d98c24448cd13317f08db5300687c8512c74688bbcc3255583a51adad064bf86"


def _addon():
    try:
        return xbmcaddon.Addon("plugin.video.ikarus")
    except Exception:
        return xbmcaddon.Addon()


def _log(msg, level=xbmc.LOGINFO):
    try:
        xbmc.log(f"[IKARUS][TRAKT] {msg}", level)
    except Exception:
        pass


def _get(key: str, default: str = "") -> str:
    try:
        value = _addon().getSetting(key) or default
    except Exception:
        value = default
    if key == "trakt_client_id" and not value:
        return IKARUS_TRAKT_CLIENT_ID
    if key == "trakt_client_secret" and not value:
        return IKARUS_TRAKT_CLIENT_SECRET
    return value


def _set(key: str, value):
    try:
        _addon().setSetting(key, "" if value is None else str(value))
    except Exception:
        pass


def _headers(client_id: str = "", access_token: str = "") -> dict:
    client_id = client_id or _get("trakt_client_id")
    headers = {
        "Content-Type": "application/json",
        "trakt-api-version": API_VERSION,
    }
    if client_id:
        headers["trakt-api-key"] = client_id
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    return headers


def _quote_path(value) -> str:
    return urllib.parse.quote(str(value or "").strip(), safe="")


def _response_text(response) -> str:
    try:
        text = response.text or ""
    except Exception:
        text = ""
    text = str(text).replace("\r", " ").replace("\n", " ").strip()
    if len(text) > 240:
        text = text[:240] + "..."
    return text


def _raise_for_status(response, action: str = ""):
    try:
        response.raise_for_status()
        return
    except Exception:
        try:
            status = int(getattr(response, "status_code", 0) or 0)
        except Exception:
            status = 0
        detail = _response_text(response)
        label = f"{action} " if action else ""
        _log(f"{label}HTTP error status={status} detail={detail}", xbmc.LOGWARNING)
        if status in (401, 403):
            _clear_auth(keep_client_keys=True)
        raise


def _set_status(text: str):
    _set("trakt_status", text or "")


def _clear_auth(keep_client_keys: bool = True):
    keys = [
        "trakt_access_token",
        "trakt_refresh_token",
        "trakt_token_type",
        "trakt_expires_in",
        "trakt_created_at",
        "trakt_username",
    ]
    if not keep_client_keys:
        keys.extend(["trakt_client_id", "trakt_client_secret"])
    for key in keys:
        _set(key, "")
    _set_status("nepripojeno")


def _save_token(data: dict):
    if not isinstance(data, dict):
        return False
    access_token = str(data.get("access_token") or "").strip()
    refresh_token = str(data.get("refresh_token") or "").strip()
    if not access_token or not refresh_token:
        return False
    _set("trakt_access_token", access_token)
    _set("trakt_refresh_token", refresh_token)
    _set("trakt_token_type", data.get("token_type") or "bearer")
    _set("trakt_expires_in", data.get("expires_in") or "")
    _set("trakt_created_at", data.get("created_at") or int(time.time()))
    return True


def _post_login_sync_background():
    global _POST_LOGIN_SYNC_RUNNING
    if _POST_LOGIN_SYNC_RUNNING:
        _log("post-login sync already running")
        return

    _POST_LOGIN_SYNC_RUNNING = True
    try:
        _log("post-login sync started")
        try:
            from resources.lib import tmdb_playback_sync
            tmdb_playback_sync.sync_local_with_trakt(force=True, push_local=True)
            _log("post-login playback sync done")
        except Exception as e:
            _log(f"post-login playback sync failed: {e}", xbmc.LOGWARNING)

        try:
            from resources.lib import tmdb_trakt_list_sync
            result = tmdb_trakt_list_sync.sync_now(fetch_items=True, push_items=True)
            _log(
                "post-login custom list sync done "
                f"linked={int(result.get('linked') or 0)} "
                f"created={int(result.get('created') or 0)} "
                f"pulled_items={int(result.get('pulled_items') or 0)} "
                f"pushed_items={int(result.get('pushed_items') or 0)}"
            )
        except Exception as e:
            _log(f"post-login custom list sync failed: {e}", xbmc.LOGWARNING)
    finally:
        _POST_LOGIN_SYNC_RUNNING = False


def start_post_login_sync():
    try:
        thread = threading.Thread(target=_post_login_sync_background, name="IkarusTraktPostLoginSync")
        thread.daemon = True
        thread.start()
        return True
    except Exception as e:
        _log(f"post-login sync start failed: {e}", xbmc.LOGWARNING)
        return False


def _token_expired(buffer_seconds: int = 300) -> bool:
    try:
        created_at = int(float(_get("trakt_created_at") or 0))
        expires_in = int(float(_get("trakt_expires_in") or 0))
    except Exception:
        return True
    if created_at <= 0 or expires_in <= 0:
        return True
    return time.time() >= (created_at + expires_in - buffer_seconds)


def refresh_token_if_needed(force: bool = False) -> bool:
    client_id = _get("trakt_client_id").strip()
    client_secret = _get("trakt_client_secret").strip()
    refresh_token = _get("trakt_refresh_token").strip()
    if not client_id or not client_secret or not refresh_token:
        return False
    if not force and not _token_expired():
        return True

    try:
        response = requests.post(
            f"{API_BASE}/oauth/token",
            headers={"Content-Type": "application/json"},
            json={
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": "urn:ietf:wg:oauth:2.0:oob",
                "grant_type": "refresh_token",
            },
            timeout=TIMEOUT,
        )
        _raise_for_status(response, "refresh")
        if _save_token(response.json()):
            _set_status("pripojeno")
            return True
    except Exception as e:
        _log(f"refresh error: {e}", xbmc.LOGWARNING)
    return False


def access_token() -> str:
    if _token_expired() and not refresh_token_if_needed(force=True):
        return ""
    return _get("trakt_access_token").strip()


def _optional_access_token(refresh_if_expired: bool = False) -> str:
    token = _get("trakt_access_token").strip()
    if not token:
        return ""
    if _token_expired():
        if refresh_if_expired and refresh_token_if_needed(force=True):
            return _get("trakt_access_token").strip()
        return ""
    return token


def is_connected() -> bool:
    if not (_get("trakt_access_token").strip() and _get("trakt_refresh_token").strip()):
        return False
    if _token_expired():
        return refresh_token_if_needed(force=True)
    return True


def fetch_user_settings() -> dict:
    client_id = _get("trakt_client_id").strip()
    token = access_token()
    if not client_id or not token:
        return {}
    response = requests.get(
        f"{API_BASE}/users/settings",
        headers=_headers(client_id=client_id, access_token=token),
        timeout=TIMEOUT,
    )
    _raise_for_status(response, "fetch user settings")
    data = response.json()
    return data if isinstance(data, dict) else {}


def fetch_user_lists() -> list:
    client_id = _get("trakt_client_id").strip()
    token = access_token()
    if not client_id or not token:
        return []
    response = requests.get(
        f"{API_BASE}/users/me/lists",
        headers=_headers(client_id=client_id, access_token=token),
        timeout=MENU_TIMEOUT,
    )
    _raise_for_status(response, "fetch user lists")
    data = response.json()
    return data if isinstance(data, list) else []


def fetch_saved_filters(section: str, page: int = 1) -> list:
    client_id = _get("trakt_client_id").strip()
    token = access_token()
    section = str(section or "").strip().lower()
    if section not in ("movies", "shows", "search"):
        return []
    if not client_id or not token:
        return []
    try:
        page = max(1, int(page))
    except Exception:
        page = 1
    response = requests.get(
        f"{API_BASE}/users/saved_filters/{section}",
        headers=_headers(client_id=client_id, access_token=token),
        params={"page": page},
        timeout=MENU_TIMEOUT,
    )
    _raise_for_status(response, "fetch saved filters")
    data = response.json()
    return data if isinstance(data, list) else []


def _normalize_saved_filter_url(url: str) -> str:
    raw_url = str(url or "").strip()
    if not raw_url:
        return ""

    parsed = urllib.parse.urlparse(raw_url)
    if parsed.scheme and parsed.netloc:
        raw_url = parsed.path or ""
        if parsed.query:
            raw_url += "?" + parsed.query

    raw_url = str(raw_url or "").strip()
    if not raw_url:
        return ""
    if not raw_url.startswith("/"):
        raw_url = "/" + raw_url.lstrip("/")
    allowed_prefixes = (
        "/lists/smart/create",
        "/movies",
        "/shows",
        "/search",
        "/calendars",
    )
    if not raw_url.startswith(allowed_prefixes):
        return ""
    return raw_url


def is_supported_saved_filter_url(url: str) -> bool:
    return bool(_normalize_saved_filter_url(url))


def create_saved_filter(name: str, url: str) -> dict:
    client_id = _get("trakt_client_id").strip()
    token = access_token()
    name = str(name or "").strip()
    url = _normalize_saved_filter_url(url)
    if not client_id or not token or not name or not url:
        return {}

    response = requests.post(
        f"{API_BASE}/users/saved_filters",
        headers=_headers(client_id=client_id, access_token=token),
        json=[{
            "name": name,
            "url": url,
        }],
        timeout=TIMEOUT,
    )
    try:
        _raise_for_status(response, "create saved filter")
    except Exception as e:
        detail = ""
        try:
            detail = (response.text or "").strip()
        except Exception:
            detail = ""
        _log(f"create saved filter failed url={url} status={getattr(response, 'status_code', '?')} detail={detail}", xbmc.LOGWARNING)
        if detail:
            raise RuntimeError(detail)
        raise e
    data = response.json()
    return data if isinstance(data, dict) else {}


def delete_saved_filter(filter_id) -> bool:
    client_id = _get("trakt_client_id").strip()
    token = access_token()
    filter_id = str(filter_id or "").strip()
    if not client_id or not token or not filter_id:
        return False

    response = requests.delete(
        f"{API_BASE}/users/saved_filters/{filter_id}",
        headers=_headers(client_id=client_id, access_token=token),
        timeout=TIMEOUT,
    )
    _raise_for_status(response, "delete saved filter")
    return True


def fetch_saved_filter_items(path: str, query: str = "", section: str = "", limit: int = 100, page: int = 1) -> list:
    client_id = _get("trakt_client_id").strip()
    token = access_token()
    raw_path = str(path or "").strip()
    raw_query = str(query or "").strip()
    section = str(section or "").strip().lower()
    if not client_id or not token or not raw_path:
        return []
    try:
        limit = max(1, min(int(limit), 100))
    except Exception:
        limit = 100
    try:
        page = max(1, int(page))
    except Exception:
        page = 1

    parsed = urllib.parse.urlparse(raw_path)
    endpoint_path = parsed.path or raw_path.split("?", 1)[0]
    if not endpoint_path.startswith("/"):
        endpoint_path = "/" + endpoint_path

    allowed_prefixes = ("/movies", "/shows", "/search")
    if section and section not in ("movies", "shows", "search"):
        return []
    if not endpoint_path.startswith(allowed_prefixes):
        return []

    params = {}
    for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        params[key] = value
    for key, value in urllib.parse.parse_qsl(raw_query, keep_blank_values=True):
        params[key] = value
    params["extended"] = "full,images"
    params["limit"] = limit
    params["page"] = page

    response = requests.get(
        f"{API_BASE}{endpoint_path}",
        headers=_headers(client_id=client_id, access_token=token),
        params=params,
        timeout=MENU_TIMEOUT,
    )
    _raise_for_status(response, "fetch saved filter items")
    data = response.json()
    return data if isinstance(data, list) else []


def create_user_list(name: str, description: str = "", privacy: str = "private") -> dict:
    client_id = _get("trakt_client_id").strip()
    token = access_token()
    name = str(name or "").strip()
    description = str(description or "").strip()
    privacy = str(privacy or "private").strip().lower()
    if privacy not in ("private", "friends", "public"):
        privacy = "private"
    if not client_id or not token or not name:
        return {}
    response = requests.post(
        f"{API_BASE}/users/me/lists",
        headers=_headers(client_id=client_id, access_token=token),
        json={
            "name": name,
            "description": description,
            "privacy": privacy,
            "display_numbers": False,
            "allow_comments": True,
            "sort_by": "rank",
            "sort_how": "asc",
        },
        timeout=TIMEOUT,
    )
    _raise_for_status(response, "create user list")
    data = response.json()
    return data if isinstance(data, dict) else {}


def update_user_list(slug: str, name: str, description: str = "", privacy: str = "private") -> dict:
    client_id = _get("trakt_client_id").strip()
    token = access_token()
    slug = str(slug or "").strip()
    name = str(name or "").strip()
    description = str(description or "").strip()
    privacy = str(privacy or "private").strip().lower()
    if privacy not in ("private", "friends", "public"):
        privacy = "private"
    if not client_id or not token or not slug or not name:
        return {}
    response = requests.put(
        f"{API_BASE}/users/me/lists/{_quote_path(slug)}",
        headers=_headers(client_id=client_id, access_token=token),
        json={
            "name": name,
            "description": description,
            "privacy": privacy,
        },
        timeout=TIMEOUT,
    )
    _raise_for_status(response, "update user list")
    data = response.json()
    return data if isinstance(data, dict) else {}


def delete_user_list(slug: str) -> bool:
    client_id = _get("trakt_client_id").strip()
    token = access_token()
    slug = str(slug or "").strip()
    if not client_id or not token or not slug:
        return False
    response = requests.delete(
        f"{API_BASE}/users/me/lists/{_quote_path(slug)}",
        headers=_headers(client_id=client_id, access_token=token),
        timeout=TIMEOUT,
    )
    _raise_for_status(response, "delete user list")
    return True


def fetch_public_lists(limit: int = 50, page: int = 1, mode: str = "popular") -> list:
    client_id = _get("trakt_client_id").strip()
    token = _optional_access_token(refresh_if_expired=False)
    if not client_id:
        return []
    try:
        limit = max(1, min(int(limit), 100))
    except Exception:
        limit = 50
    try:
        page = max(1, int(page))
    except Exception:
        page = 1
    mode = (mode or "popular").strip().lower()
    if mode not in ("popular", "trending"):
        mode = "popular"
    response = requests.get(
        f"{API_BASE}/lists/{mode}",
        headers=_headers(client_id=client_id, access_token=token),
        params={"limit": limit, "page": page},
        timeout=TIMEOUT,
    )
    _raise_for_status(response, "fetch public lists")
    data = response.json()
    return data if isinstance(data, list) else []


def search_lists(query: str, limit: int = 50, page: int = 1) -> list:
    client_id = _get("trakt_client_id").strip()
    token = _optional_access_token(refresh_if_expired=False)
    query = str(query or "").strip()
    if not client_id or not query:
        return []
    try:
        limit = max(1, min(int(limit), 100))
    except Exception:
        limit = 50
    try:
        page = max(1, int(page))
    except Exception:
        page = 1
    response = requests.get(
        f"{API_BASE}/search/list",
        headers=_headers(client_id=client_id, access_token=token),
        params={"query": query, "limit": limit, "page": page},
        timeout=TIMEOUT,
    )
    _raise_for_status(response, "search lists")
    data = response.json()
    return data if isinstance(data, list) else []


def fetch_lists_by_user(username: str, limit: int = 50, page: int = 1) -> list:
    client_id = _get("trakt_client_id").strip()
    username = str(username or "").strip()
    if not client_id or not username:
        return []
    token = access_token() if username.lower() == "me" else _optional_access_token(refresh_if_expired=False)
    if username.lower() == "me" and not token:
        return []
    try:
        limit = max(1, min(int(limit), 100))
    except Exception:
        limit = 50
    try:
        page = max(1, int(page))
    except Exception:
        page = 1
    response = requests.get(
        f"{API_BASE}/users/{_quote_path(username)}/lists",
        headers=_headers(client_id=client_id, access_token=token),
        params={"limit": limit, "page": page},
        timeout=TIMEOUT,
    )
    _raise_for_status(response, "fetch lists by user")
    data = response.json()
    return data if isinstance(data, list) else []


def search_list_users(query: str, limit: int = 20, page: int = 1) -> list:
    query = str(query or "").strip()
    if not query:
        return []
    try:
        limit = max(1, min(int(limit), 50))
    except Exception:
        limit = 20
    try:
        page = max(1, int(page))
    except Exception:
        page = 1

    candidates = {}
    q_lower = query.lower()
    search_pages = max(1, min(page + 2, 5))
    for search_page in range(1, search_pages + 1):
        rows = search_lists(query, limit=100, page=search_page)
        if not rows:
            break
        for row in rows:
            list_row = row.get("list") if isinstance(row, dict) and isinstance(row.get("list"), dict) else row
            if not isinstance(list_row, dict):
                continue
            user_row = list_row.get("user") if isinstance(list_row.get("user"), dict) else {}
            username = str(user_row.get("username") or "").strip()
            if not username:
                continue
            display_name = str(user_row.get("name") or "").strip()
            key = username.lower()
            item = candidates.setdefault(key, {
                "username": username,
                "name": display_name,
                "matched_lists": 0,
                "list_count": None,
            })
            item["matched_lists"] = int(item.get("matched_lists") or 0) + 1
            if display_name and not item.get("name"):
                item["name"] = display_name

    rows = list(candidates.values())
    filtered = [
        row for row in rows
        if q_lower in str(row.get("username") or "").lower()
        or q_lower in str(row.get("name") or "").lower()
    ]
    if filtered:
        rows = filtered

    rows.sort(key=lambda row: (
        0 if q_lower in str(row.get("username") or "").lower() else 1,
        -int(row.get("matched_lists") or 0),
        str(row.get("username") or "").lower(),
    ))

    start = (page - 1) * limit
    end = start + limit
    page_rows = rows[start:end]

    for row in page_rows:
        username = str(row.get("username") or "").strip()
        if not username:
            continue
        try:
            lists = fetch_lists_by_user(username, limit=100, page=1)
            count = len(lists or [])
            if count >= 100:
                row["list_count"] = "100+"
            else:
                row["list_count"] = count
        except Exception:
            row["list_count"] = None

    return page_rows


def fetch_list_items(username: str, slug: str, limit: int = 100, page: int = 1, item_type: str = "") -> list:
    client_id = _get("trakt_client_id").strip()
    username = str(username or "").strip()
    slug = str(slug or "").strip()
    if not client_id or not username or not slug:
        return []
    token = access_token() if username.lower() == "me" else _optional_access_token(refresh_if_expired=False)
    if username.lower() == "me" and not token:
        return []
    try:
        limit = max(1, min(int(limit), 100))
    except Exception:
        limit = 100
    try:
        page = max(1, int(page))
    except Exception:
        page = 1
    item_type = str(item_type or "").strip().lower()
    if item_type not in ("movies", "shows"):
        item_type = ""
    endpoint = f"{API_BASE}/users/{_quote_path(username)}/lists/{_quote_path(slug)}/items"
    if item_type:
        endpoint += f"/{item_type}"
    response = requests.get(
        endpoint,
        headers=_headers(client_id=client_id, access_token=token),
        params={"extended": "full,images", "limit": limit, "page": page},
        timeout=MENU_TIMEOUT,
    )
    _raise_for_status(response, "fetch list items")
    data = response.json()
    return data if isinstance(data, list) else []


def fetch_watched(media_type: str) -> list:
    client_id = _get("trakt_client_id").strip()
    token = access_token()
    media_type = str(media_type or "").strip().lower()
    if media_type not in ("movies", "shows"):
        return []
    if not client_id or not token:
        return []
    response = requests.get(
        f"{API_BASE}/sync/watched/{media_type}",
        headers=_headers(client_id=client_id, access_token=token),
        params={"extended": "full"},
        timeout=TIMEOUT,
    )
    _raise_for_status(response, "fetch watched")
    data = response.json()
    return data if isinstance(data, list) else []


def fetch_playback() -> list:
    client_id = _get("trakt_client_id").strip()
    token = access_token()
    if not client_id or not token:
        return []
    response = requests.get(
        f"{API_BASE}/sync/playback",
        headers=_headers(client_id=client_id, access_token=token),
        params={"extended": "full"},
        timeout=TIMEOUT,
    )
    _raise_for_status(response, "fetch playback")
    data = response.json()
    return data if isinstance(data, list) else []


def _fetch_history_page(start_at: str = "", end_at: str = "", page: int = 1,
                        limit: int = 100) -> dict:
    client_id = _get("trakt_client_id").strip()
    token = access_token()
    if not client_id or not token:
        raise RuntimeError("Trakt is not connected")
    try:
        page = max(1, int(page))
    except Exception:
        page = 1
    try:
        limit = max(1, min(int(limit), 100))
    except Exception:
        limit = 100

    params = {"extended": "full", "limit": limit, "page": page}
    start_at = str(start_at or "").strip()
    end_at = str(end_at or "").strip()
    if start_at:
        params["start_at"] = start_at
    if end_at:
        params["end_at"] = end_at

    response = requests.get(
        f"{API_BASE}/sync/history",
        headers=_headers(client_id=client_id, access_token=token),
        params=params,
        timeout=TIMEOUT,
    )
    _raise_for_status(response, "fetch history")
    data = response.json()
    if not isinstance(data, list):
        raise ValueError("unexpected Trakt history response")

    def _header_int(name, default=0):
        try:
            return max(0, int(response.headers.get(name) or default))
        except Exception:
            return max(0, int(default or 0))

    return {
        "rows": data,
        "page": _header_int("X-Pagination-Page", page),
        "page_count": _header_int("X-Pagination-Page-Count", 0),
        "item_count": _header_int("X-Pagination-Item-Count", 0),
    }


def fetch_history(start_at: str = "", end_at: str = "", page: int = 1, limit: int = 100) -> list:
    result = _fetch_history_page(
        start_at=start_at,
        end_at=end_at,
        page=page,
        limit=limit,
    )
    return list(result.get("rows") or [])


def fetch_history_complete(start_at: str = "", end_at: str = "", max_pages: int = 500,
                           workers: int = 6) -> dict:
    try:
        max_pages = max(1, int(max_pages))
    except Exception:
        max_pages = 500
    try:
        workers = max(1, min(int(workers), 8))
    except Exception:
        workers = 6

    limit = 100
    first = _fetch_history_page(
        start_at=start_at,
        end_at=end_at,
        page=1,
        limit=limit,
    )
    page_count = int(first.get("page_count") or 0)
    item_count = int(first.get("item_count") or 0)
    if page_count <= 0:
        page_count = 1 if first.get("rows") else 0
    if page_count > max_pages:
        raise RuntimeError(f"Trakt history has too many pages: {page_count}")

    batches = [list(first.get("rows") or [])]
    if page_count > 1:
        def _load_page(page):
            result = _fetch_history_page(
                start_at=start_at,
                end_at=end_at,
                page=page,
                limit=limit,
            )
            reported_pages = int(result.get("page_count") or page_count)
            if reported_pages != page_count:
                raise RuntimeError("Trakt history changed while it was being downloaded")
            page_rows = list(result.get("rows") or [])
            if page < page_count and len(page_rows) < limit:
                for _attempt in range(2):
                    retry = _fetch_history_page(
                        start_at=start_at,
                        end_at=end_at,
                        page=page,
                        limit=limit,
                    )
                    retry_rows = list(retry.get("rows") or [])
                    if len(retry_rows) > len(page_rows):
                        page_rows = retry_rows
                    if len(page_rows) >= limit:
                        break
            return page_rows

        with ThreadPoolExecutor(max_workers=min(workers, page_count - 1)) as executor:
            for batch in executor.map(_load_page, range(2, page_count + 1)):
                batches.append(batch)

    if page_count > 1:
        for page, batch in enumerate(batches[:-1], start=1):
            if len(batch) < (limit - 5):
                raise RuntimeError(
                    f"incomplete Trakt history page {page}: downloaded {len(batch)} of {limit} rows"
                )
        if item_count > 0 and not batches[-1]:
            raise RuntimeError("incomplete Trakt history: final page is empty")

    rows = []
    for batch in batches:
        rows.extend(batch)

    history_ids = {
        str(row.get("id"))
        for row in rows
        if isinstance(row, dict) and row.get("id") not in (None, "")
    }
    if page_count > 1 and item_count and history_ids and len(history_ids) < item_count:
        with ThreadPoolExecutor(max_workers=min(workers, page_count)) as executor:
            second_pass = executor.map(_load_page, range(1, page_count + 1))
            by_id = {
                str(row.get("id")): row
                for row in rows
                if isinstance(row, dict) and row.get("id") not in (None, "")
            }
            without_id = [
                row for row in rows
                if not isinstance(row, dict) or row.get("id") in (None, "")
            ]
            for batch in second_pass:
                for row in batch:
                    if isinstance(row, dict) and row.get("id") not in (None, ""):
                        by_id.setdefault(str(row.get("id")), row)
                    else:
                        without_id.append(row)
            rows = list(by_id.values()) + without_id

    if item_count and (item_count - len(rows)) >= limit:
        raise RuntimeError(
            f"incomplete Trakt history: downloaded {len(rows)} of {item_count} rows"
        )
    return {
        "rows": rows,
        "page_count": page_count,
        "item_count": item_count or len(rows),
    }


def fetch_history_range(start_at: str, end_at: str, max_pages: int = 30) -> list:
    rows = []
    limit = 100
    try:
        max_pages = max(1, int(max_pages))
    except Exception:
        max_pages = 30
    for page in range(1, max_pages + 1):
        batch = fetch_history(start_at=start_at, end_at=end_at, page=page, limit=limit)
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < limit:
            break
    return rows


def add_item_to_list(slug: str, media_type: str, tmdb_id) -> dict:
    client_id = _get("trakt_client_id").strip()
    token = access_token()
    slug = str(slug or "").strip()
    media_type = str(media_type or "").strip().lower()
    tmdb_id = str(tmdb_id or "").strip()
    if media_type == "tv":
        media_type = "show"
    if not client_id or not token or not slug or media_type not in ("movie", "show") or not tmdb_id:
        return {}

    key = "movies" if media_type == "movie" else "shows"
    response = requests.post(
        f"{API_BASE}/users/me/lists/{_quote_path(slug)}/items",
        headers=_headers(client_id=client_id, access_token=token),
        json={key: [{"ids": {"tmdb": int(tmdb_id) if tmdb_id.isdigit() else tmdb_id}}]},
        timeout=TIMEOUT,
    )
    _raise_for_status(response, "add item to list")
    data = response.json()
    return data if isinstance(data, dict) else {}


def remove_item_from_list(slug: str, media_type: str, tmdb_id) -> dict:
    client_id = _get("trakt_client_id").strip()
    token = access_token()
    slug = str(slug or "").strip()
    media_type = str(media_type or "").strip().lower()
    tmdb_id = str(tmdb_id or "").strip()
    if media_type == "tv":
        media_type = "show"
    if not client_id or not token or not slug or media_type not in ("movie", "show") or not tmdb_id:
        return {}

    key = "movies" if media_type == "movie" else "shows"
    response = requests.post(
        f"{API_BASE}/users/me/lists/{_quote_path(slug)}/items/remove",
        headers=_headers(client_id=client_id, access_token=token),
        json={key: [{"ids": {"tmdb": int(tmdb_id) if tmdb_id.isdigit() else tmdb_id}}]},
        timeout=TIMEOUT,
    )
    _raise_for_status(response, "remove item from list")
    data = response.json()
    return data if isinstance(data, dict) else {}


def _ids_match(item: dict, tmdb_id) -> bool:
    if not isinstance(item, dict):
        return False
    ids = item.get("ids") if isinstance(item.get("ids"), dict) else {}
    return str(ids.get("tmdb") or "").strip() == str(tmdb_id or "").strip()


def _delete_playback_row(playback_id) -> bool:
    client_id = _get("trakt_client_id").strip()
    token = access_token()
    playback_id = str(playback_id or "").strip()
    if not client_id or not token or not playback_id:
        return False
    response = requests.delete(
        f"{API_BASE}/sync/playback/{_quote_path(playback_id)}",
        headers=_headers(client_id=client_id, access_token=token),
        timeout=TIMEOUT,
    )
    _raise_for_status(response, "delete playback row")
    return True


def remove_playback_item(media_type: str, tmdb_id, season=None, episode=None) -> int:
    media_type = str(media_type or "").strip().lower()
    if media_type == "tv":
        media_type = "show"
    tmdb_id = str(tmdb_id or "").strip()
    if media_type not in ("movie", "show", "series", "episode") or not tmdb_id:
        return 0

    season_i = None
    episode_i = None
    try:
        if season not in (None, ""):
            season_i = int(str(season).strip())
        if episode not in (None, ""):
            episode_i = int(str(episode).strip())
    except Exception:
        season_i = None
        episode_i = None

    deleted = 0
    for row in fetch_playback():
        if not isinstance(row, dict):
            continue
        playback_id = row.get("id")
        row_type = str(row.get("type") or "").strip().lower()

        if media_type == "movie":
            if row_type == "movie" and _ids_match(row.get("movie"), tmdb_id) and _delete_playback_row(playback_id):
                deleted += 1
            continue

        if row_type != "episode" or not _ids_match(row.get("show"), tmdb_id):
            continue
        episode_row = row.get("episode") if isinstance(row.get("episode"), dict) else {}
        if season_i is not None and int(episode_row.get("season") or -1) != season_i:
            continue
        if episode_i is not None and int(episode_row.get("number") or -1) != episode_i:
            continue
        if _delete_playback_row(playback_id):
            deleted += 1
    return deleted


def remove_history_item(media_type: str, tmdb_id, season=None, episode=None) -> dict:
    client_id = _get("trakt_client_id").strip()
    token = access_token()
    media_type = str(media_type or "").strip().lower()
    if media_type == "tv":
        media_type = "show"
    tmdb_id = str(tmdb_id or "").strip()
    if not client_id or not token or not tmdb_id:
        return {}

    ids = {"tmdb": int(tmdb_id) if tmdb_id.isdigit() else tmdb_id}
    if media_type == "movie":
        payload = {"movies": [{"ids": ids}]}
    elif media_type in ("show", "series", "episode"):
        show = {"ids": ids}
        try:
            season_i = int(str(season).strip()) if season not in (None, "") else None
            episode_i = int(str(episode).strip()) if episode not in (None, "") else None
        except Exception:
            season_i = None
            episode_i = None
        if season_i is not None:
            season_row = {"number": season_i}
            if episode_i is not None:
                season_row["episodes"] = [{"number": episode_i}]
            show["seasons"] = [season_row]
        payload = {"shows": [show]}
    else:
        return {}

    response = requests.post(
        f"{API_BASE}/sync/history/remove",
        headers=_headers(client_id=client_id, access_token=token),
        json=payload,
        timeout=TIMEOUT,
    )
    _raise_for_status(response, "remove history item")
    data = response.json()
    return data if isinstance(data, dict) else {}


def clear_sync_item(media_type: str, tmdb_id, season=None, episode=None) -> dict:
    result = {"playback_deleted": 0, "history": {}, "errors": []}
    if not is_connected():
        return result

    try:
        result["playback_deleted"] = remove_playback_item(media_type, tmdb_id, season=season, episode=episode)
    except Exception as e:
        result["errors"].append(f"playback: {e}")
        _log(f"playback clear error: {e}", xbmc.LOGWARNING)

    try:
        result["history"] = remove_history_item(media_type, tmdb_id, season=season, episode=episode)
    except Exception as e:
        result["errors"].append(f"history: {e}")
        _log(f"history clear error: {e}", xbmc.LOGWARNING)

    return result


def add_history_items(movies=None, episodes=None, replace_existing: bool = False) -> dict:
    client_id = _get("trakt_client_id").strip()
    token = access_token()
    if not client_id or not token:
        return {}

    payload = {}
    movie_rows = []
    for movie in movies or []:
        watched_at = ""
        if isinstance(movie, dict):
            tmdb_id = str(movie.get("tmdb_id") or movie.get("id") or "").strip()
            watched_at = str(movie.get("watched_at") or "").strip()
        else:
            tmdb_id = str(movie or "").strip()
        if not tmdb_id:
            continue
        row = {"ids": {"tmdb": int(tmdb_id) if tmdb_id.isdigit() else tmdb_id}}
        if watched_at:
            row["watched_at"] = watched_at
        movie_rows.append(row)
    if movie_rows:
        payload["movies"] = movie_rows

    shows = {}
    for item in episodes or []:
        if not isinstance(item, dict):
            continue
        tmdb_id = str(item.get("tmdb_id") or "").strip()
        if not tmdb_id:
            continue
        try:
            season_i = int(str(item.get("season") or "").strip())
            episode_i = int(str(item.get("episode") or "").strip())
        except Exception:
            continue
        show = shows.setdefault(tmdb_id, {"ids": {"tmdb": int(tmdb_id) if tmdb_id.isdigit() else tmdb_id}, "seasons": {}})
        season_row = show["seasons"].setdefault(season_i, {"number": season_i, "episodes": []})
        episode_row = {"number": episode_i}
        watched_at = str(item.get("watched_at") or "").strip()
        if watched_at:
            episode_row["watched_at"] = watched_at
        season_row["episodes"].append(episode_row)

    if shows:
        payload["shows"] = []
        for show in shows.values():
            show["seasons"] = list(show.get("seasons", {}).values())
            payload["shows"].append(show)

    if not payload:
        return {}

    if replace_existing:
        try:
            remove_payload = {}
            if movie_rows:
                remove_payload["movies"] = [{"ids": row.get("ids")} for row in movie_rows if isinstance(row.get("ids"), dict)]
            if payload.get("shows"):
                remove_payload["shows"] = payload.get("shows")
            if remove_payload:
                response = requests.post(
                    f"{API_BASE}/sync/history/remove",
                    headers=_headers(client_id=client_id, access_token=token),
                    json=remove_payload,
                    timeout=TIMEOUT,
                )
                _raise_for_status(response, "replace history remove")
        except Exception as e:
            _log(f"history replace remove failed: {e}", xbmc.LOGWARNING)

    response = requests.post(
        f"{API_BASE}/sync/history",
        headers=_headers(client_id=client_id, access_token=token),
        json=payload,
        timeout=TIMEOUT,
    )
    _raise_for_status(response, "add history items")
    data = response.json()
    return data if isinstance(data, dict) else {}


def scrobble_status(media_type: str, tmdb_id, progress, status: str = "pause",
                    season=None, episode=None) -> dict:
    client_id = _get("trakt_client_id").strip()
    token = access_token()
    media_type = str(media_type or "").strip().lower()
    status = str(status or "pause").strip().lower()
    tmdb_id = str(tmdb_id or "").strip()
    if media_type == "tv":
        media_type = "show"
    if status not in ("start", "pause", "stop"):
        status = "pause"
    if not client_id or not token or not tmdb_id:
        return {}

    try:
        progress = float(progress or 0.0)
    except Exception:
        progress = 0.0
    if progress < 0:
        progress = 0.0
    if progress > 100:
        progress = 100.0

    ids = {"tmdb": int(tmdb_id) if tmdb_id.isdigit() else tmdb_id}
    payload = {
        "progress": progress,
        "app_version": "Ikarus",
        "app_date": "2026-05-05",
    }

    if media_type == "movie":
        payload["movie"] = {"ids": ids}
    elif media_type in ("episode", "show", "series"):
        try:
            season_i = int(str(season or "").strip())
            episode_i = int(str(episode or "").strip())
        except Exception:
            return {}
        payload["show"] = {"ids": ids}
        payload["episode"] = {"season": season_i, "number": episode_i}
    else:
        return {}

    response = requests.post(
        f"{API_BASE}/scrobble/{status}",
        headers=_headers(client_id=client_id, access_token=token),
        json=payload,
        timeout=10,
    )
    try:
        _raise_for_status(response, f"scrobble {status}")
    except requests.HTTPError as e:
        detail = ""
        try:
            detail = response.text[:300]
        except Exception:
            detail = ""
        raise requests.HTTPError(f"{e}; body={detail}", response=response)
    data = response.json()
    return data if isinstance(data, dict) else {}


def check_connection(show_notification: bool = True) -> bool:
    try:
        data = fetch_user_settings()
        user = (data.get("user") or {}) if isinstance(data, dict) else {}
        username = str(user.get("username") or "").strip()
        if username:
            _set("trakt_username", username)
            _set_status("pripojeno")
        else:
            _set_status("pripojeno")
        if show_notification:
            xbmcgui.Dialog().notification(
                i18n.T(30810, "Ikarus - Trakt.TV"),
                i18n.T(30811, "Connected as {username}").format(username=username)
                if username
                else i18n.T(30812, "Connected."),
                xbmcgui.NOTIFICATION_INFO,
                3000,
            )
        return True
    except Exception as e:
        _log(f"connection check error: {e}", xbmc.LOGWARNING)
        _set_status("nepripojeno")
        if show_notification:
            xbmcgui.Dialog().notification(
                i18n.T(30810, "Ikarus - Trakt.TV"),
                i18n.T(30813, "Connection check failed."),
                xbmcgui.NOTIFICATION_ERROR,
                3500,
            )
        return False


def login_device_flow():
    client_id = _get("trakt_client_id").strip()
    client_secret = _get("trakt_client_secret").strip()
    if not client_id or not client_secret:
        _set_status("chyb\u00ed client id/secret")
        xbmcgui.Dialog().ok(
            i18n.T(30810, "Ikarus - Trakt.TV"),
            i18n.T(
                30814,
                "Trakt.TV username alone is not enough.\n\n"
                "The activation code can only be generated by a registered Trakt application. "
                "Once Ikarus application credentials are filled in, clicking the button will show the code for https://trakt.tv/activate.",
            ),
        )
        return False

    try:
        device_response = requests.post(
            f"{API_BASE}/oauth/device/code",
            headers={"Content-Type": "application/json"},
            json={"client_id": client_id},
            timeout=TIMEOUT,
        )
        _raise_for_status(device_response, "device code")
        device = device_response.json()
    except Exception as e:
        _log(f"device code error: {e}", xbmc.LOGERROR)
        _set_status("chyba p\u0159ihl\u00e1\u0161en\u00ed")
        xbmcgui.Dialog().notification(
            i18n.T(30810, "Ikarus - Trakt.TV"),
            i18n.T(30815, "Could not get login code."),
            xbmcgui.NOTIFICATION_ERROR,
            3500,
        )
        return False

    user_code = str(device.get("user_code") or "").strip()
    device_code = str(device.get("device_code") or "").strip()
    verification_url = str(
        device.get("verification_url")
        or device.get("verification_uri")
        or "https://trakt.tv/activate"
    ).strip()
    try:
        expires_in = int(device.get("expires_in") or 600)
    except Exception:
        expires_in = 600
    try:
        interval = int(device.get("interval") or 5)
    except Exception:
        interval = 5

    if not user_code or not device_code:
        _set_status("chyba kodu")
        xbmcgui.Dialog().notification(
            i18n.T(30810, "Ikarus - Trakt.TV"),
            i18n.T(30816, "Trakt did not return a login code."),
            xbmcgui.NOTIFICATION_ERROR,
            3500,
        )
        return False

    progress = xbmcgui.DialogProgress()
    progress.create(
        i18n.T(30817, "Ikarus - Trakt.TV / Device activation"),
        i18n.T(30818, "Open {url}\nEnter code: {code}\nWaiting for browser confirmation...").format(
            url=verification_url,
            code=user_code,
        ),
    )
    _set_status(f"\u010dek\u00e1 na potvrzen\u00ed: {user_code}")

    start = time.time()
    token_data = None
    try:
        while (time.time() - start) < expires_in:
            if progress.iscanceled():
                _set_status("p\u0159ihl\u00e1\u0161en\u00ed zru\u0161eno")
                return False

            elapsed = time.time() - start
            percent = max(0, min(99, int((elapsed / float(expires_in)) * 100)))
            progress.update(
                percent,
                i18n.T(30819, "Open {url}\nEnter code: {code}\nThe window will close after activation.").format(
                    url=verification_url,
                    code=user_code,
                ),
            )

            try:
                token_response = requests.post(
                    f"{API_BASE}/oauth/device/token",
                    headers={"Content-Type": "application/json"},
                    json={
                        "code": device_code,
                        "client_id": client_id,
                        "client_secret": client_secret,
                    },
                    timeout=TIMEOUT,
                )
                if token_response.status_code == 200:
                    token_data = token_response.json()
                    break

                err = {}
                try:
                    err = token_response.json()
                except Exception:
                    err = {}
                error_name = str(err.get("error") or "").strip()
                if error_name == "authorization_pending":
                    pass
                elif error_name == "slow_down":
                    interval += 5
                elif error_name in ("expired_token", "access_denied"):
                    _set_status("p\u0159ihl\u00e1\u0161en\u00ed odm\u00edtnuto")
                    return False
                else:
                    _log(f"device token polling status={token_response.status_code} body={token_response.text[:250]}", xbmc.LOGWARNING)
            except Exception as e:
                _log(f"device token polling error: {e}", xbmc.LOGWARNING)

            xbmc.sleep(max(1, interval) * 1000)
    finally:
        try:
            progress.close()
        except Exception:
            pass

    if not token_data:
        _set_status("k\u00f3d vypr\u0161el")
        xbmcgui.Dialog().notification(
            i18n.T(30810, "Ikarus - Trakt.TV"),
            i18n.T(30820, "Login code expired."),
            xbmcgui.NOTIFICATION_ERROR,
            3500,
        )
        return False

    if not _save_token(token_data):
        _set_status("chyba tokenu")
        xbmcgui.Dialog().notification(
            i18n.T(30810, "Ikarus - Trakt.TV"),
            i18n.T(30821, "Trakt did not return a valid token."),
            xbmcgui.NOTIFICATION_ERROR,
            3500,
        )
        return False

    check_connection(show_notification=False)
    username = _get("trakt_username").strip()
    xbmcgui.Dialog().notification(
        i18n.T(30810, "Ikarus - Trakt.TV"),
        i18n.T(30822, "Device connected as {username}").format(username=username)
        if username
        else i18n.T(30823, "Device was connected."),
        xbmcgui.NOTIFICATION_INFO,
        3500,
    )
    start_post_login_sync()
    return True


def logout():
    _clear_auth(keep_client_keys=True)
    xbmcgui.Dialog().notification(
        i18n.T(30810, "Ikarus - Trakt.TV"),
        i18n.T(30824, "Trakt account was disconnected."),
        xbmcgui.NOTIFICATION_INFO,
        2500,
    )
    return True
