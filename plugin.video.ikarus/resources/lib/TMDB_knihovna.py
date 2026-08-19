# -*- coding: utf-8 -*-

import sys
import json
import time
import ssl
import html
import gzip
import concurrent.futures
import threading
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime

import xbmc
import xbmcgui

import xbmcplugin

import xbmcaddon

from resources.lib import tmdb_playback_state
from resources.lib import tmdb_playback_sync
from resources.lib import tmdb_custom_lists
from resources.lib import tmdb_custom_prefetch
from resources.lib import tmdb_trakt_list_sync
from resources.lib import trakt_client
from resources.lib import trakt_public_favorites
from resources.lib import diagnostics
from resources.lib import source_common
from resources.lib import i18n
from resources.lib import auto_next_policy




addon = xbmcaddon.Addon()

addon_handle = int(sys.argv[1]) if len(sys.argv) > 1 else 0

BASE_PATH = sys.argv[0] if len(sys.argv) > 0 else ""



def _refresh_runtime():
    global addon_handle, BASE_PATH
    try:
        addon_handle = int(sys.argv[1]) if len(sys.argv) > 1 else addon_handle
    except Exception:
        pass
    try:
        BASE_PATH = sys.argv[0] if len(sys.argv) > 0 else BASE_PATH
    except Exception:
        pass


TMDB_API_KEY = "1f0150a5f78d4adc2407911989fdb66c".strip()
TMDB_BASE_URL = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w500"
TMDB_IMAGE_THUMB_BASE = "https://image.tmdb.org/t/p/w780"
TMDB_IMAGE_ORIGINAL_BASE = "https://image.tmdb.org/t/p/original"
TMDB_LANGUAGE = "cs-CZ"
TMDB_REGION = "CZ"
TMDB_MENU_TIMEOUT = 8
TMDB_DETAIL_TIMEOUT = 20
TMDB_IMAGE_TIMEOUT = 30
_TMDB_LOCAL_LATIN_CHARS = set(
    "\u00c1\u00e1\u010c\u010d\u010e\u010f\u00c9\u00e9\u011a\u011b"
    "\u00cd\u00ed\u0147\u0148\u00d3\u00f3\u0158\u0159\u0160\u0161"
    "\u0164\u0165\u00da\u00fa\u016e\u016f\u00dd\u00fd\u017d\u017e"
)
_TMDB_DISTINCT_LOCAL_LATIN_CHARS = set(
    "\u010c\u010d\u010e\u010f\u011a\u011b\u013d\u013e\u0147\u0148"
    "\u00d4\u00f4\u0154\u0155\u0158\u0159\u0160\u0161\u0164\u0165"
    "\u016e\u016f\u017d\u017e"
)


_CTX = ssl.create_default_context()



_TMDB_CACHE = {}
_TMDB_DETAIL_MISS_CACHE = {}
_TMDB_DETAIL_MISS_TTL = 1800
_TRAKT_STATUS_PREWARM_RUNNING = False
_TRAKT_STATUS_PREWARM_TS = 0.0
TMDB_MOVIE_HISTORY_FILE = "tmdb_movie_search_history.json"

TMDB_SERIES_HISTORY_FILE = "tmdb_series_search_history.json"

TMDB_PEOPLE_HISTORY_FILE = "tmdb_people_search_history.json"

TMDB_MULTI_HISTORY_FILE = "tmdb_multi_search_history.json"

TMDB_MOVIE_HISTORY_MAX = 25



# ---------------------------

# ROUTING HELPERS

# ---------------------------

def build_url(query: dict) -> str:

    return BASE_PATH + "?" + urllib.parse.urlencode(query)



def _get_param(name: str, default: str = "") -> str:
    if len(sys.argv) < 3 or not sys.argv[2]:
        return default
    params = urllib.parse.parse_qs(sys.argv[2][1:])
    return params.get(name, [default])[0]

def _tmdb_watch_source_param() -> str:
    value = _get_param("watch_source", "").strip().lower()
    return "trakt" if value == "trakt" else ""

def _tmdb_add_watch_source(query: dict, watch_source: str) -> dict:
    if watch_source:
        query["watch_source"] = watch_source
    return query

def _tmdb_tv_playback_status_row(tmdb_id, watch_source: str = "") -> dict:
    try:
        if str(watch_source or "").strip().lower() == "trakt":
            return tmdb_playback_sync.trakt_show_row(tmdb_id) or {}
        return tmdb_playback_sync.merged_row(tmdb_id, include_trakt=True) or {}
    except Exception:
        return {}

def _get_int_param(name: str, default: int = 0) -> int:
    try:
        return int(_get_param(name, str(default)))
    except Exception:

        return default



def _end(content="files", update_listing=None):
    try:
        xbmcplugin.setContent(addon_handle, content)
    except Exception:
        pass
    if update_listing is None:
        update_listing = _get_param("replace_listing", "").strip() == "1"
    xbmcplugin.endOfDirectory(addon_handle, True, bool(update_listing))


def _finish_command_action(succeeded=True):
    _refresh_runtime()
    try:
        xbmcplugin.endOfDirectory(
            addon_handle,
            succeeded=bool(succeeded),
            updateListing=False,
            cacheToDisc=False,
        )
    except Exception:
        pass


def _set_video_info(li, info: dict):
    info = dict(info or {})
    if not info:
        return

    try:
        tag = li.getVideoInfoTag()
    except Exception:
        tag = None

    if tag is None:
        try:
            li.setInfo("video", info)
        except Exception:
            pass
        return

    def _set(method, value, cast=None):
        if value in (None, ""):
            return
        func = getattr(tag, method, None)
        if not func:
            return
        try:
            func(cast(value) if cast else value)
        except Exception:
            pass

    def _list(value):
        if value in (None, ""):
            return []
        if isinstance(value, (list, tuple)):
            return [str(v) for v in value if str(v or "").strip()]
        return [part.strip() for part in str(value).split("/") if part.strip()]

    _set("setTitle", info.get("title"))
    _set("setSortTitle", info.get("sorttitle"))
    _set("setOriginalTitle", info.get("originaltitle") or info.get("original_title"))
    _set("setTvShowTitle", info.get("tvshowtitle"))
    _set("setPlot", info.get("plot"))
    _set("setPlotOutline", info.get("plotoutline"))
    _set("setTagLine", info.get("tagline"))
    _set("setMediaType", info.get("mediatype"))
    _set("setTrailer", info.get("trailer"))
    _set("setMpaa", info.get("mpaa"))
    _set("setPremiered", info.get("premiered") or info.get("releasedate") or info.get("firstaired"))
    _set("setYear", info.get("year"), int)
    _set("setRating", info.get("rating"), float)
    _set("setVotes", info.get("votes"), int)
    _set("setDuration", info.get("duration"), int)
    _set("setSeason", info.get("season"), int)
    _set("setEpisode", info.get("episode"), int)

    list_fields = (
        ("genre", "setGenres"),
        ("director", "setDirectors"),
        ("writer", "setWriters"),
        ("country", "setCountries"),
        ("studio", "setStudios"),
    )
    for key, method in list_fields:
        values = _list(info.get(key))
        if values:
            _set(method, values)


def _set_video_info_compact(li, info: dict):
    info = dict(info or {})
    if not info:
        return

    try:
        tag = li.getVideoInfoTag()
    except Exception:
        tag = None

    if tag is None:
        try:
            li.setInfo("video", {
                "title": info.get("title"),
                "plot": info.get("plot"),
                "mediatype": info.get("mediatype"),
                "year": info.get("year"),
                "rating": info.get("rating"),
            })
        except Exception:
            pass
        return

    def _set(method, value, cast=None):
        if value in (None, ""):
            return
        func = getattr(tag, method, None)
        if not func:
            return
        try:
            func(cast(value) if cast else value)
        except Exception:
            pass

    _set("setTitle", info.get("title"))
    _set("setTvShowTitle", info.get("tvshowtitle"))
    _set("setPlot", info.get("plot"))
    _set("setMediaType", info.get("mediatype"))
    _set("setPremiered", info.get("premiered") or info.get("releasedate") or info.get("firstaired"))
    _set("setYear", info.get("year"), int)
    _set("setRating", info.get("rating"), float)


def _add_dir(label: str, query: dict, icon: str = "DefaultFolder.png"):
    li = xbmcgui.ListItem(label=label)
    li.setArt({
        "icon": icon,
        "thumb": icon,
        "poster": icon

    })

    xbmcplugin.addDirectoryItem(

        handle=addon_handle,

        url=build_url(query),

        listitem=li,

        isFolder=True
    )


def _add_main_style_dir(label: str, query: dict, icon: str = "DefaultFolder.png",
                        title: str = "", plot: str = "", context_items=None):
    li = xbmcgui.ListItem(label=label)
    li.setArt({
        "icon": icon,
        "thumb": icon
    })
    if title or plot:
        try:
            _set_video_info(li, {"title": title or label, "plot": plot or ""})
        except Exception:
            pass
    if context_items:
        try:
            li.addContextMenuItems(list(context_items), replaceItems=False)
        except Exception:
            pass
    xbmcplugin.addDirectoryItem(
        handle=addon_handle,
        url=build_url(query or {}),
        listitem=li,
        isFolder=True
    )


def _add_main_style_action_item(label: str, query: dict, icon: str = "DefaultAddonProgram.png"):
    li = xbmcgui.ListItem(label=label)
    li.setArt({
        "icon": icon,
        "thumb": icon
    })
    li.setProperty("IsPlayable", "false")
    xbmcplugin.addDirectoryItem(
        handle=addon_handle,
        url=build_url(query or {}),
        listitem=li,
        isFolder=False
    )


def _add_action_item(label: str, query: dict, icon: str = "DefaultAddonProgram.png"):
    li = xbmcgui.ListItem(label=label)
    li.setArt({
        "icon": icon,
        "thumb": icon,
        "poster": icon

    })

    li.setProperty("IsPlayable", "false")

    xbmcplugin.addDirectoryItem(

        handle=addon_handle,

        url=build_url(query),

        listitem=li,

        isFolder=False

    )





_TMDB_WATCH_RETURN_ACTIONS = {

    "tmdb.finished",

    "tmdb.started",

    "tmdb.movies.finished",

    "tmdb.movies.started",

    "tmdb.series.finished",

    "tmdb.series.started",

}





def _tmdb_refresh_watch_container():

    return_action = _get_param("return_action", "").strip()

    if return_action in _TMDB_WATCH_RETURN_ACTIONS:

        try:

            target_url = build_url({"action": return_action})

            xbmc.executebuiltin(f'Container.Update("{target_url}",replace)')

            return

        except Exception:

            pass

    try:

        xbmc.executebuiltin("Container.Refresh")

    except Exception:

        pass





def _placeholder_dir(title: str, extra: str = ""):
    xbmcplugin.setPluginCategory(addon_handle, title)
    xbmcplugin.setContent(addon_handle, "files")


    text = i18n.T(31912, "This section is under construction.")

    if extra:

        text += " " + extra



    li = xbmcgui.ListItem(label=text)

    li.setArt({

        "icon": "DefaultAddonProgram.png",

        "thumb": "DefaultAddonProgram.png",

        "poster": "DefaultAddonProgram.png"

    })



    xbmcplugin.addDirectoryItem(

        handle=addon_handle,

        url="",

        listitem=li,

        isFolder=False

    )


    _end("files")


def _empty_result_dir(title: str, content: str = "files", text: str = None):
    text = text or i18n.T(31602, "No item found.")
    xbmcplugin.setPluginCategory(addon_handle, title)
    xbmcplugin.setContent(addon_handle, content)

    li = xbmcgui.ListItem(label=text)
    li.setArt({
        "icon": "DefaultFolder.png",
        "thumb": "DefaultFolder.png",
        "poster": "DefaultFolder.png"
    })
    li.setProperty("IsPlayable", "false")

    xbmcplugin.addDirectoryItem(
        handle=addon_handle,
        url="",
        listitem=li,
        isFolder=False
    )

    _end(content)


def _tmdb_my_lists_title() -> str:
    return i18n.T(31700, "TMDB / My lists")


def _tmdb_trakt_title() -> str:
    return i18n.T(31701, "TMDB / Trakt.TV")


def _tmdb_custom_lists_title() -> str:
    return i18n.T(31702, "TMDB / Custom lists")


def _tmdb_not_trakt_logged_text() -> str:
    return i18n.T(31720, "You are not signed in to Trakt.TV.")


def _tmdb_empty_not_logged_text() -> str:
    return i18n.T(31721, "The list is empty, you are not signed in.")


def _tmdb_empty_list_text() -> str:
    return i18n.T(31722, "The list is empty.")


def _empty_saved_items_dir(title: str):
    xbmcplugin.setPluginCategory(addon_handle, title)
    xbmcplugin.setContent(addon_handle, "files")


    li = xbmcgui.ListItem(label=i18n.T(30426, "V sekci nejsou prozat\u00edm ulo\u017eeny \u017e\u00e1dn\u00e9 polo\u017eky"))

    li.setArt({

        "icon": "DefaultFolder.png",

        "thumb": "DefaultFolder.png",

        "poster": "DefaultFolder.png"

    })



    xbmcplugin.addDirectoryItem(

        handle=addon_handle,

        url="",

        listitem=li,

        isFolder=False

    )



    _end("files")



# ---------------------------

# SETTINGS

# ---------------------------

def _get_page_limit(default: int = 25) -> int:

    try:

        raw = xbmcaddon.Addon().getSetting("search_limit")

        val = int(raw)

        return val if val > 0 else default

    except Exception:

        return default



def _profile_dir() -> str:

    try:

        from xbmcvfs import translatePath

        base = translatePath(addon.getAddonInfo("profile"))

    except Exception:

        base = ""

    if base:

        try:

            import os

            os.makedirs(base, exist_ok=True)

        except Exception:

            pass

    return base



def _tmdb_movie_history_path() -> str:

    import os

    return os.path.join(_profile_dir(), TMDB_MOVIE_HISTORY_FILE)



def _tmdb_movie_history_load() -> list:

    import os

    path = _tmdb_movie_history_path()

    if not path or not os.path.exists(path):

        return []

    try:

        with open(path, "r", encoding="utf-8") as f:

            data = json.load(f)

        if not isinstance(data, list):

            return []

        out = []

        seen = set()

        for item in data:

            q = str(item or "").strip()

            if not q:

                continue

            key = q.casefold()

            if key in seen:

                continue

            seen.add(key)

            out.append(q)

        return out[:TMDB_MOVIE_HISTORY_MAX]

    except Exception as e:

        xbmc.log(f"[IKARUS][TMDB] history load failed: {e}", xbmc.LOGWARNING)

        return []



def _tmdb_movie_history_save(items: list):

    path = _tmdb_movie_history_path()

    if not path:

        return

    clean = []

    seen = set()

    for item in items or []:

        q = str(item or "").strip()

        if not q:

            continue

        key = q.casefold()

        if key in seen:

            continue

        seen.add(key)

        clean.append(q)

    try:

        with open(path, "w", encoding="utf-8") as f:

            json.dump(clean[:TMDB_MOVIE_HISTORY_MAX], f, ensure_ascii=False, indent=2)

    except Exception as e:

        xbmc.log(f"[IKARUS][TMDB] history save failed: {e}", xbmc.LOGWARNING)



def _tmdb_movie_history_add(query: str):

    q = (query or "").strip()

    if not q:

        return

    history = [item for item in _tmdb_movie_history_load() if item.casefold() != q.casefold()]

    history.insert(0, q)

    _tmdb_movie_history_save(history)



def _tmdb_history_load(filename: str) -> list:

    import os

    path = os.path.join(_profile_dir(), filename)

    if not path or not os.path.exists(path):

        return []

    try:

        with open(path, "r", encoding="utf-8") as f:

            data = json.load(f)

        if not isinstance(data, list):

            return []

        out = []

        seen = set()

        for item in data:

            q = str(item or "").strip()

            if not q:

                continue

            key = q.casefold()

            if key in seen:

                continue

            seen.add(key)

            out.append(q)

        return out[:TMDB_MOVIE_HISTORY_MAX]

    except Exception as e:

        xbmc.log(f"[IKARUS][TMDB] history load failed for {filename}: {e}", xbmc.LOGWARNING)

        return []



def _tmdb_history_save(filename: str, items: list):

    import os

    path = os.path.join(_profile_dir(), filename)

    if not path:

        return

    clean = []

    seen = set()

    for item in items or []:

        q = str(item or "").strip()

        if not q:

            continue

        key = q.casefold()

        if key in seen:

            continue

        seen.add(key)

        clean.append(q)

    try:

        with open(path, "w", encoding="utf-8") as f:

            json.dump(clean[:TMDB_MOVIE_HISTORY_MAX], f, ensure_ascii=False, indent=2)

    except Exception as e:

        xbmc.log(f"[IKARUS][TMDB] history save failed for {filename}: {e}", xbmc.LOGWARNING)



def _tmdb_history_add(filename: str, query: str):

    q = (query or "").strip()

    if not q:

        return

    history = [item for item in _tmdb_history_load(filename) if item.casefold() != q.casefold()]

    history.insert(0, q)

    _tmdb_history_save(filename, history)



# ---------------------------

# HTTP / TMDB

# ---------------------------

_sanitize_url = source_common.sanitize_url

def _redact_url_for_log(url: str) -> str:
    try:
        p = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qsl(p.query, keep_blank_values=True)
        redacted = [("api_key", "***") if key == "api_key" else (key, value) for key, value in query]
        return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, urllib.parse.urlencode(redacted), p.fragment))
    except Exception:
        return url

def _http_get_json(url: str, timeout: int = 20, referer: str = None, origin: str = None) -> dict:
    url = _sanitize_url(url)


    headers = {

        "User-Agent": (

            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "

            "AppleWebKit/537.36 (KHTML, like Gecko) "

            "Chrome/122.0.0.0 Safari/537.36"

        ),

        "Accept": "application/json,*/*",

        "Accept-Language": "cs-CZ,cs;q=0.9,en;q=0.8",

        "Accept-Encoding": "identity",

        "Connection": "close",

    }

    if referer:

        headers["Referer"] = referer

    if origin:

        headers["Origin"] = origin



    req = urllib.request.Request(url, headers=headers, method="GET")


    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as resp:
            raw = resp.read()
            encoding = (resp.headers.get("Content-Encoding") or "").lower()
        if "gzip" in encoding or raw[:2] == b"\x1f\x8b":
            try:
                raw = gzip.decompress(raw)
            except OSError:
                pass
        try:
            txt = raw.decode("utf-8", errors="ignore")
        except Exception:
            txt = raw.decode(errors="ignore")
        return json.loads(txt) if txt else {}
    except Exception as e:
        xbmc.log(f"[IKARUS][TMDB] HTTP JSON FAILED: {repr(e)} | {_redact_url_for_log(url)}", xbmc.LOGERROR)
        return {}


def _http_get_json_retry(url: str, timeout: int = 20, referer: str = None,
                         origin: str = None, retries: int = 1) -> dict:
    try:
        retries = max(0, min(2, int(retries or 0)))
    except Exception:
        retries = 1

    for attempt in range(retries + 1):
        data = _http_get_json(url, timeout=timeout, referer=referer, origin=origin)
        is_error = bool(
            isinstance(data, dict)
            and (
                data.get("success") is False
                or (data.get("status_code") and data.get("status_message"))
            )
        )
        if isinstance(data, dict) and data and not is_error:
            return data
        if attempt < retries:
            xbmc.log(
                f"[IKARUS][TMDB] retry {attempt + 1}/{retries}",
                xbmc.LOGWARNING,
            )
            xbmc.sleep(350 * (attempt + 1))
    return {}


def _tmdb_get(path: str, params: dict = None, timeout: int = 20,
              force_refresh: bool = False, retries: int = 1) -> dict:

    if not TMDB_API_KEY:

        xbmc.log("[IKARUS][TMDB] Missing TMDB_API_KEY", xbmc.LOGERROR)

        return {}



    params = dict(params or {})

    params["api_key"] = TMDB_API_KEY



    url = TMDB_BASE_URL + path + "?" + urllib.parse.urlencode(params)

    cache_key = url



    now = time.time()

    cached = None if force_refresh else _TMDB_CACHE.get(cache_key)

    if cached:

        ts = cached.get("ts", 0)
        cached_data = cached.get("data") or {}

        if (now - ts) < 600 and cached_data:

            return cached_data

        if not cached_data:
            _TMDB_CACHE.pop(cache_key, None)



    data = _http_get_json_retry(

        url,

        timeout=timeout,

        referer="https://www.themoviedb.org/",

        origin="https://www.themoviedb.org",
        retries=retries,

    )



    if not data:
        _TMDB_CACHE.pop(cache_key, None)
        return {}

    _TMDB_CACHE[cache_key] = {"ts": time.time(), "data": data}

    return data or {}



# ---------------------------

# TMDB HELPERS

# ---------------------------

def _tmdb_media_art(item: dict) -> dict:
    poster = (item.get("poster_path") or "").strip()
    backdrop = (item.get("backdrop_path") or "").strip()


    art = {

        "icon": "DefaultMovies.png",

        "thumb": "DefaultMovies.png",

        "poster": "DefaultMovies.png",

    }


    if poster:
        full = poster if poster.lower().startswith(("http://", "https://")) else TMDB_IMAGE_BASE + poster
        art["icon"] = full
        art["thumb"] = full
        art["poster"] = full

    if backdrop:
        art["fanart"] = backdrop if backdrop.lower().startswith(("http://", "https://")) else TMDB_IMAGE_BASE + backdrop

    return art


def _tmdb_movie_info(item: dict) -> dict:
    title, title_is_local = _tmdb_display_title_info(item, "movie")
    original = _tmdb_hide_unreadable_text(item.get("original_title") or "")
    plot = item.get("overview") or ""

    rating = item.get("vote_average") or 0

    votes = item.get("vote_count") or 0

    premiered = item.get("release_date") or ""



    year = 0

    if len(premiered) >= 4 and premiered[:4].isdigit():

        year = int(premiered[:4])



    return {

        "title": title,

        "originaltitle": original,

        "plot": plot,

        "rating": float(rating) if rating else 0.0,

        "votes": str(votes),

        "year": year,

        "premiered": premiered,

        "mediatype": "movie",

    }



def _tmdb_movie_runtime_text(runtime) -> str:

    try:

        runtime = int(runtime or 0)

    except Exception:

        runtime = 0



    if runtime <= 0:

        return ""



    h = runtime // 60

    m = runtime % 60



    if h > 0 and m > 0:

        return f"{h} h {m} min"

    if h > 0:

        return f"{h} h"

    return f"{m} min"



def _tmdb_join_names(items, key="name", limit=8) -> str:
    out = []
    for it in (items or [])[:limit]:
        if isinstance(it, str):
            val = it.strip()
        elif key is None:

            val = str(it).strip()

        else:

            val = (it.get(key) or "").strip()

        if val:

            out.append(val)
    return ", ".join(out)

def _tmdb_latin_letter_ratio(text: str) -> float:
    letters = 0
    latin = 0
    for ch in str(text or ""):
        if not ch.isalpha():
            continue
        letters += 1
        if "LATIN" in unicodedata.name(ch, ""):
            latin += 1
    if letters <= 0:
        return 1.0
    return latin / float(letters)

def _tmdb_needs_readable_title(text: str) -> bool:
    text = str(text or "").strip()
    if not text:
        return False
    return _tmdb_latin_letter_ratio(text) < 0.5

def _tmdb_hide_unreadable_text(text: str) -> str:
    text = str(text or "").strip()
    if _tmdb_needs_readable_title(text):
        return ""
    return text

def _tmdb_alt_title_country_rank(country: str) -> int:
    country = str(country or "").strip().upper()
    priority = {
        "CZ": 0,
        "CS": 1,
        "SK": 2,
        "US": 3,
        "GB": 4,
        "CA": 5,
        "AU": 6,
        "": 20,
    }
    return priority.get(country, 10)


def _tmdb_title_candidate_rank(row) -> int:
    if not isinstance(row, dict):
        return _tmdb_alt_title_country_rank("")
    country = str(row.get("country") or "").strip().upper()
    language = str(row.get("language") or "").strip().lower()
    rank = _tmdb_alt_title_country_rank(country)
    if language == "en" and rank > 6:
        return 6
    return rank

def _tmdb_alt_titles_from_payload(payload: dict) -> list:
    rows = (payload or {}).get("titles")
    if rows is None:
        rows = (payload or {}).get("results")

    out = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or row.get("name") or "").strip()
        if not title:
            continue
        out.append({
            "title": title,
            "country": str(row.get("iso_3166_1") or "").strip().upper(),
            "type": str(row.get("type") or "").strip(),
        })
    return out

def _tmdb_has_local_latin_chars(text: str) -> bool:
    return any(ch in _TMDB_LOCAL_LATIN_CHARS for ch in str(text or ""))


def _tmdb_has_distinct_local_latin_chars(text: str) -> bool:
    return any(ch in _TMDB_DISTINCT_LOCAL_LATIN_CHARS for ch in str(text or ""))


def _tmdb_is_local_origin_item(item: dict) -> bool:
    codes = set()
    for code in (item or {}).get("origin_country") or []:
        code = str(code or "").strip().upper()
        if code:
            codes.add(code)
    for country in (item or {}).get("production_countries") or []:
        if not isinstance(country, dict):
            continue
        code = str(country.get("iso_3166_1") or "").strip().upper()
        if code:
            codes.add(code)
    return bool(codes.intersection({"CZ", "CS", "SK"}))


def _tmdb_alternative_titles(media_type: str, tmdb_id) -> list:
    tmdb_id = str(tmdb_id or "").strip()
    media_type = str(media_type or "").strip().lower()
    if not tmdb_id or media_type not in ("movie", "tv"):
        return []

    payload = _tmdb_get(f"/{media_type}/{tmdb_id}/alternative_titles", params={}, timeout=6)
    return _tmdb_alt_titles_from_payload(payload)


def _tmdb_translation_titles(media_type: str, tmdb_id) -> list:
    tmdb_id = str(tmdb_id or "").strip()
    media_type = str(media_type or "").strip().lower()
    if not tmdb_id or media_type not in ("movie", "tv"):
        return []

    payload = _tmdb_get(f"/{media_type}/{tmdb_id}/translations", params={}, timeout=6)
    out = []
    for row in (payload or {}).get("translations") or []:
        if not isinstance(row, dict):
            continue
        data = row.get("data") or {}
        if not isinstance(data, dict):
            data = {}
        title = str(data.get("title") or data.get("name") or "").strip()
        if not title:
            continue
        country = str(row.get("iso_3166_1") or "").strip().upper()
        language = str(row.get("iso_639_1") or "").strip().lower()
        if not country and language == "en":
            country = "US"
        out.append({
            "title": title,
            "country": country,
            "language": language,
            "type": "translation",
        })
    return out

def _tmdb_pick_readable_title(candidates, fallback: str = "") -> str:
    fallback = str(fallback or "").strip()
    rows = []
    for idx, row in enumerate(candidates or []):
        if isinstance(row, dict):
            title = str(row.get("title") or "").strip()
            country = str(row.get("country") or "").strip().upper()
        else:
            title = str(row or "").strip()
            country = ""
        if not title or _tmdb_needs_readable_title(title):
            continue
        rows.append((_tmdb_title_candidate_rank(row), idx, title))

    if rows:
        rows.sort(key=lambda item: (item[0], item[1], item[2].lower()))
        return rows[0][2]
    return fallback if fallback and not _tmdb_needs_readable_title(fallback) else ""


def _tmdb_untitled() -> str:
    return i18n.T(30748, "Untitled")


def _tmdb_detail_line(label_id: int, fallback_label: str, value) -> str:
    return i18n.T(31876, "{label}: {value}").format(
        label=i18n.T(label_id, fallback_label),
        value=value
    )


def _tmdb_rating_line(rating, vote_count) -> str:
    return i18n.T(31877, "{label}: {rating} ({count} votes)").format(
        label=i18n.T(31869, "TMDB rating"),
        rating=rating,
        count=vote_count
    )


def _tmdb_readable_fallback(text: str, default: str = None) -> str:
    if default is None:
        default = _tmdb_untitled()
    text = str(text or "").strip()
    if text and not _tmdb_needs_readable_title(text):
        return text
    return default

def _tmdb_pick_local_readable_title(candidates, fallback: str = "") -> str:
    local = []
    for row in candidates or []:
        if not isinstance(row, dict):
            continue
        country = str(row.get("country") or "").strip().upper()
        if country in ("CZ", "CS"):
            local.append(row)
    return _tmdb_pick_readable_title(local, fallback="")

def _tmdb_display_title_info(item: dict, media_type: str, fallback: str = "", fetch_alternatives: bool = True):
    item = item or {}
    media_type = str(media_type or "").strip().lower()
    title_key = "title" if media_type == "movie" else "name"
    original_key = "original_title" if media_type == "movie" else "original_name"

    title = str(item.get(title_key) or fallback or "").strip()
    original = str(item.get(original_key) or "").strip()
    local_origin = _tmdb_is_local_origin_item(item)
    payload_candidates = _tmdb_alt_titles_from_payload(item.get("alternative_titles") or {})
    if local_origin and original and not _tmdb_needs_readable_title(original):
        return original, True

    local_title = _tmdb_pick_local_readable_title(payload_candidates)
    if local_title:
        return local_title, True

    if title and not _tmdb_needs_readable_title(title):
        if original and title.casefold() != original.casefold():
            return title, True
        if _tmdb_has_distinct_local_latin_chars(title):
            return title, True

    candidates = []
    candidates.extend(payload_candidates)
    if fetch_alternatives:
        fetched_candidates = _tmdb_alternative_titles(media_type, item.get("id"))
        local_title = _tmdb_pick_local_readable_title(fetched_candidates)
        if local_title:
            return local_title, True
        candidates.extend(fetched_candidates)
        translation_candidates = _tmdb_translation_titles(media_type, item.get("id"))
        local_title = _tmdb_pick_local_readable_title(translation_candidates)
        if local_title:
            return local_title, True
        candidates.extend(translation_candidates)

    if title and not _tmdb_needs_readable_title(title):
        return title, False

    candidates.append(original)

    picked = _tmdb_pick_readable_title(candidates, fallback=_tmdb_readable_fallback(fallback, _tmdb_untitled()))
    return picked or _tmdb_readable_fallback(fallback, _tmdb_untitled()), False


def _tmdb_display_title(item: dict, media_type: str, fallback: str = "", fetch_alternatives: bool = True) -> str:
    title, _ = _tmdb_display_title_info(item, media_type, fallback=fallback, fetch_alternatives=fetch_alternatives)
    return title


def _tmdb_date_sort_key(item: dict, *keys) -> str:

    for key in keys:

        val = (item.get(key) or "").strip()

        if val:

            return val

    return ""



def _is_future_release_date(date_str: str) -> bool:

    date_str = (date_str or "").strip()

    if len(date_str) < 4 or not date_str[:4].isdigit():

        return False



    try:

        year = int(date_str[:4])

        current_year = time.localtime().tm_year

        return year > current_year

    except Exception:

        return False





def _filter_out_future_items(results, date_key: str, enabled: bool = True):

    results = list(results or [])

    if not enabled:

        return results



    out = []

    for item in results:

        date_str = (item.get(date_key) or "").strip()

        if _is_future_release_date(date_str):

            continue

        out.append(item)

    return out



def _tmdb_find_first_non_future_page(
    endpoint: str,
    params: dict,
    date_key: str,

    start_page: int = 1,

    max_lookahead: int = 10

):

    page = max(1, int(start_page or 1))

    looked = 0



    while looked < max_lookahead:

        cur_params = dict(params or {})

        cur_params["page"] = page



        payload = _tmdb_get(endpoint, params=cur_params, timeout=20)

        results = payload.get("results") or []

        total_pages = int(payload.get("total_pages") or 1)



        filtered = _filter_out_future_items(results, date_key, enabled=True)



        if filtered:

            return payload, filtered, page, total_pages



        if page >= total_pages:

            return payload, [], page, total_pages



        page += 1

        looked += 1



    payload = _tmdb_get(endpoint, params=dict(params or {}, page=page), timeout=20)

    total_pages = int(payload.get("total_pages") or 1)

    return payload, [], page, total_pages



def _tmdb_sort_movies_by_newest(results):

    results = list(results or [])

    results.sort(key=lambda x: _tmdb_date_sort_key(x, "release_date"), reverse=True)

    return results



def _tmdb_sort_tv_by_newest(results):

    results = list(results or [])

    results.sort(key=lambda x: _tmdb_date_sort_key(x, "first_air_date"), reverse=True)

    return results



def _tmdb_fetch_search_results(path: str, query: str, page: int = 1, language: str = None):

    if page < 1:

        page = 1



    params = {

        "query": query,

        "page": page,

        "include_adult": "false",

        "language": language or TMDB_LANGUAGE,

    }



    payload = _tmdb_get(path, params=params, timeout=20)

    results = payload.get("results") or []

    total_pages = int(payload.get("total_pages") or 1)



    return {

        "results": results,

        "total_pages": total_pages,

    }



def _tmdb_tag_results(results, media_type: str):

    out = []

    for item in (results or []):

        row = dict(item or {})

        row["media_type"] = media_type

        out.append(row)

    return out



def _tmdb_collect_search_results(path: str, query: str, media_type: str, needed_count: int, sort_mode: str = ""):

    collected = []

    page = 1

    total_pages = 1

    has_more = False



    if needed_count <= 0:

        return {

            "results": [],

            "has_more": False,

            "total_pages": 1,

            "fetched_pages": 0,

        }



    while True:

        data = _tmdb_fetch_search_results(path, query, page=page, language=TMDB_LANGUAGE)

        batch = data.get("results") or []

        total_pages = int(data.get("total_pages") or 1)



        if not batch:

            has_more = False

            break



        collected.extend(_tmdb_tag_results(batch, media_type))



        if len(collected) >= needed_count:

            has_more = page < total_pages

            break



        if page >= total_pages:

            has_more = False

            break



        page += 1



    if sort_mode == "newest":

        if media_type == "tv":

            collected = _tmdb_sort_tv_by_newest(collected)

        elif media_type == "movie":

            collected = _tmdb_sort_movies_by_newest(collected)



    return {

        "results": collected,

        "has_more": has_more,

        "total_pages": total_pages,

        "fetched_pages": page,

    }



def _tmdb_collect_all_search_results(path: str, query: str, media_type: str, sort_mode: str = "", max_pages: int = 0):

    collected = []



    first = _tmdb_fetch_search_results(path, query, page=1, language=TMDB_LANGUAGE)

    first_results = first.get("results") or []

    total_pages = int(first.get("total_pages") or 1)



    if first_results:

        collected.extend(_tmdb_tag_results(first_results, media_type))



    if max_pages and max_pages > 0:

        total_to_fetch = min(total_pages, max_pages)

    else:

        total_to_fetch = total_pages



    for page in range(2, total_to_fetch + 1):

        data = _tmdb_fetch_search_results(path, query, page=page, language=TMDB_LANGUAGE)

        batch = data.get("results") or []

        if not batch:

            break

        collected.extend(_tmdb_tag_results(batch, media_type))



    if sort_mode == "newest":

        if media_type == "tv":

            collected = _tmdb_sort_tv_by_newest(collected)

        elif media_type == "movie":

            collected = _tmdb_sort_movies_by_newest(collected)



    has_more = total_to_fetch < total_pages



    return {

        "results": collected,

        "has_more": has_more,

        "total_pages": total_pages,

        "fetched_pages": total_to_fetch,

    }



def tmdb_movie_sources_search_stub_menu():
    from resources.lib import Hledani_zdroje
    Hledani_zdroje.source_search_stub_menu()

def tmdb_movie_sources_webshare_group_menu():
    from resources.lib import Hledani_zdroje
    Hledani_zdroje.source_webshare_group_menu()

def tmdb_movie_sources_provider_group_menu():
    from resources.lib import Hledani_zdroje
    Hledani_zdroje.source_provider_group_menu()

def tmdb_movie_source_play():
    from resources.lib import Hledani_zdroje
    Hledani_zdroje.source_play()


def tmdb_movie_source_download():

    from resources.lib import Hledani_zdroje

    Hledani_zdroje.source_download()





def tmdb_movie_detail_open():
    tmdb_id = _get_param("id", "").strip()
    title = _get_param("title", "").strip()
    original_title = _get_param("original_title", "").strip()
    watch_source = _tmdb_watch_source_param()
    if not tmdb_id:
        _finish_command_action(False)
        return

    detail_url = build_url(_tmdb_add_watch_source({
        "action": "tmdb.movie.detail",
        "id": tmdb_id,
        "title": title,
        "original_title": original_title,
    }, watch_source))
    try:

        xbmc.executebuiltin(f"Container.Update({detail_url})")
        _finish_command_action(True)
    except Exception:

        _finish_command_action(False)




def tmdb_tv_detail_open():
    tmdb_id = _get_param("id", "").strip()
    title = _get_param("title", "").strip()
    original_title = _get_param("original_title", "").strip()
    watch_source = _tmdb_watch_source_param()
    if not tmdb_id:
        _finish_command_action(False)
        return

    detail_url = build_url(_tmdb_add_watch_source({
        "action": "tmdb.tv.detail",
        "id": tmdb_id,
        "title": title,
        "original_title": original_title,
    }, watch_source))
    try:

        xbmc.executebuiltin(f"Container.Update({detail_url})")
        _finish_command_action(True)
    except Exception:

        _finish_command_action(False)




def tmdb_tv_seasons_open():
    tmdb_id = _get_param("id", "").strip()
    title = _get_param("title", "").strip()
    original_title = _get_param("original_title", "").strip()
    watch_source = _tmdb_watch_source_param()
    if not tmdb_id:
        _finish_command_action(False)
        return

    detail_url = build_url(_tmdb_add_watch_source({
        "action": "tmdb.tv.seasons",
        "id": tmdb_id,
        "title": title,
        "original_title": original_title,
    }, watch_source))
    try:

        xbmc.executebuiltin(f"Container.Update({detail_url})")
        _finish_command_action(True)
    except Exception:

        _finish_command_action(False)




def tmdb_tv_season_episodes_open():
    tmdb_id = _get_param("id", "").strip()
    title = _get_param("title", "").strip()
    original_title = _get_param("original_title", "").strip()
    season_number = _get_param("season_number", "").strip()
    season_name = _get_param("season_name", "").strip()
    watch_source = _tmdb_watch_source_param()
    if not tmdb_id or season_number == "":
        _finish_command_action(False)
        return

    detail_url = build_url(_tmdb_add_watch_source({
        "action": "tmdb.tv.season.episodes",
        "id": tmdb_id,
        "title": title,
        "original_title": original_title,
        "season_number": season_number,
        "season_name": season_name,
    }, watch_source))
    try:

        xbmc.executebuiltin(f"Container.Update({detail_url})")
        _finish_command_action(True)
    except Exception:

        _finish_command_action(False)




def tmdb_tv_episode_sources_open():

    tmdb_id = _get_param("tmdb_id", "").strip() or _get_param("id", "").strip()

    title = _get_param("title", "").strip()

    original_title = _get_param("original_title", "").strip()

    year = _get_param("year", "").strip()

    season = _get_param("season", "").strip()

    episode = _get_param("episode", "").strip()

    episode_title = _get_param("episode_title", "").strip()

    season_name = _get_param("season_name", "").strip()

    query = _get_param("query", "").strip()

    if not tmdb_id:

        return



    detail_url = build_url({

        "action": "tmdb.movie.sources.search.stub",

        "tmdb_id": tmdb_id,

        "title": title,

        "original_title": original_title,

        "year": year,

        "mode": "serial_episode",

        "season": season,

        "episode": episode,

        "episode_title": episode_title,

        "season_name": season_name,

        "query": query,

    })

    try:

        xbmc.executebuiltin(f"Container.Update({detail_url})")

    except Exception:

        pass



def _tmdb_find_next_tv_episode(tmdb_id: str, season: str, episode: str):

    try:

        current_season = int(season)

        current_episode = int(episode)

    except Exception:

        return None

    today_iso = datetime.utcnow().strftime("%Y-%m-%d")



    current_detail = _tmdb_tv_season_detail(tmdb_id, str(current_season))

    current_episodes = current_detail.get("episodes") or []

    for ep in sorted(current_episodes, key=lambda x: int(x.get("episode_number") or 0)):

        if not auto_next_policy.episode_is_available(ep, today_iso):

            continue

        try:

            ep_num = int(ep.get("episode_number") or 0)

        except Exception:

            ep_num = 0

        if ep_num > current_episode:

            return {

                "season": str(current_season),

                "episode": str(ep_num),

                "episode_title": (ep.get("name") or "").strip() or f"Epizoda {ep_num}",

                "season_name": (current_detail.get("name") or "").strip() or f"Serie {current_season}",

            }



    tv_detail = _tmdb_tv_detail(tmdb_id)

    seasons = tv_detail.get("seasons") or []

    next_seasons = []

    for s in seasons:

        try:

            sn = int(s.get("season_number") or 0)

        except Exception:

            continue

        if sn > current_season and sn != 0 and int(s.get("episode_count") or 0) > 0:

            next_seasons.append((sn, s))



    for sn, season_obj in sorted(next_seasons, key=lambda item: item[0]):

        detail = _tmdb_tv_season_detail(tmdb_id, str(sn))

        episodes = detail.get("episodes") or []

        for ep in sorted(episodes, key=lambda x: int(x.get("episode_number") or 0)):

            if not auto_next_policy.episode_is_available(ep, today_iso):

                continue

            try:

                ep_num = int(ep.get("episode_number") or 0)

            except Exception:

                ep_num = 0

            if ep_num > 0:

                return {

                    "season": str(sn),

                    "episode": str(ep_num),

                    "episode_title": (ep.get("name") or "").strip() or f"Epizoda {ep_num}",

                    "season_name": (detail.get("name") or season_obj.get("name") or "").strip() or f"Serie {sn}",

                }



    return None



def prepare_next_tv_episode_playback(tmdb_id: str, source_meta: dict = None,

                                     prefer_same_provider: bool = True) -> dict:

    source_meta = source_meta or {}
    auto_trace = str(source_meta.get("_auto_next_trace") or "").strip()[:80]

    tmdb_id = str(tmdb_id or "").strip()

    if not tmdb_id:

        return {}



    if (source_meta.get("mode") or "").strip() != "serial_episode":

        return {}



    next_ep = _tmdb_find_next_tv_episode(

        tmdb_id,

        source_meta.get("season"),

        source_meta.get("episode"),

    )

    if not next_ep:

        return {}



    tv_meta = _tmdb_playback_tv_meta(tmdb_id)

    title = (tv_meta.get("title") or source_meta.get("media_title") or source_meta.get("title") or "").strip()

    original_title = (tv_meta.get("original_title") or source_meta.get("original_title") or "").strip()

    year = str(tv_meta.get("year") or source_meta.get("year") or "").strip()

    season = next_ep["season"]

    episode = next_ep["episode"]

    episode_title = next_ep["episode_title"]

    season_name = next_ep["season_name"]

    query = f"{title} S{int(season):02d}E{int(episode):02d}" if title else f"S{int(season):02d}E{int(episode):02d}"



    search_url = build_url({

        "action": "tmdb.movie.sources.search.stub",

        "tmdb_id": tmdb_id,

        "title": title,

        "original_title": original_title,

        "year": year,

        "mode": "serial_episode",

        "season": season,

        "episode": episode,

        "episode_title": episode_title,

        "season_name": season_name,

        "query": query,

    })



    row = None

    play_url = ""

    provider_search_url = ""

    try:

        from resources.lib import Hledani_zdroje

        preferred = (source_meta.get("provider") or "").strip() if prefer_same_provider else ""

        require_cz_dabing = False

        try:

            require_cz_dabing = (addon.getSetting("tmdb_series_next_require_cz") or "").strip().lower() == "true"

        except Exception:

            require_cz_dabing = True



        row = Hledani_zdroje.find_best_tmdb_episode_source(

            title=title,

            original_title=original_title,

            year=year,

            season=season,

            episode=episode,

            episode_title=episode_title,

            preferred_provider=preferred,

            require_cz_dabing=require_cz_dabing,

            season_name=season_name,

        )

        if row:

            provider_name = (row.get("provider") or "").strip()

            if provider_name:

                provider_search_url = build_url({

                    "action": "tmdb.movie.sources.search.stub",

                    "provider": provider_name,

                    "tmdb_id": tmdb_id,

                    "title": title,

                    "original_title": original_title,

                    "year": year,

                    "mode": "serial_episode",

                    "season": season,

                    "episode": episode,

                    "episode_title": episode_title,

                    "season_name": season_name,

                    "query": query,

                })

            play_url = Hledani_zdroje.build_tmdb_episode_source_play_url(
                row=row,
                tmdb_id=tmdb_id,
                title=title,
                original_title=original_title,
                year=year,
                season=season,
                episode=episode,
                episode_title=episode_title,
                season_name=season_name,
                auto_next=True,
                auto_trace=auto_trace,
            )
    except Exception as e:

        try:

            xbmc.log(f"[IKARUS][AUTO NEXT] prepare source failed: {e}", xbmc.LOGWARNING)

        except Exception:

            pass



    return {

        "tmdb_id": tmdb_id,

        "title": title,

        "original_title": original_title,

        "year": year,

        "season": season,

        "episode": episode,

        "episode_title": episode_title,

        "season_name": season_name,

        "query": query,

        "search_url": search_url,

        "provider_search_url": provider_search_url,

        "play_url": play_url,

        "auto_trace": auto_trace,

        "source": row or {},

    }



def tmdb_downloads_queue_menu():

    from resources.lib import Hledani_zdroje

    Hledani_zdroje.downloads_queue_menu()



def tmdb_downloads_queue_clear_done():

    from resources.lib import Hledani_zdroje

    Hledani_zdroje.downloads_queue_clear_done()



def tmdb_downloads_queue_clear_all():
    from resources.lib import Hledani_zdroje
    Hledani_zdroje.downloads_queue_clear_all()


def _tmdb_youtube_prepare_subtitles():
    try:
        yt_addon = xbmcaddon.Addon("plugin.video.youtube")
        features = (yt_addon.getSetting("kodion.mpd.stream.features") or "").split(",")
        features = [feature.strip() for feature in features if feature.strip()]
        features = [feature for feature in features if feature not in ("vtt", "ttml")]
        if features:
            yt_addon.setSetting("kodion.mpd.stream.features", ",".join(features))
        yt_addon.setSetting("kodion.subtitle.languages.num", "2")
        yt_addon.setSetting("kodion.subtitle.download", "false")
    except Exception as e:
        xbmc.log(f"[IKARUS][TMDB] YouTube subtitles prepare failed: {repr(e)}", xbmc.LOGWARNING)


def tmdb_video_play():
    site = (_get_param("site", "") or "").strip().lower()
    key = (_get_param("key", "") or "").strip()
    title = (_get_param("title", "Trailer") or "Trailer").strip()


    if not key:
        try:
            xbmcplugin.setResolvedUrl(addon_handle, False, xbmcgui.ListItem())
        except Exception:
            pass
        xbmcgui.Dialog().ok(i18n.T(31300, "TMDB / Trailer"), i18n.T(31301, "Missing video key."))

        return

    if site == "youtube":
        _tmdb_youtube_prepare_subtitles()
        yt_url = "plugin://plugin.video.youtube/play/?" + urllib.parse.urlencode({"video_id": key})

        try:
            li = xbmcgui.ListItem(label=title, path=yt_url)
        except TypeError:
            li = xbmcgui.ListItem(label=title)
            try:
                li.setPath(yt_url)
            except Exception:
                pass
        li.setProperty("IsPlayable", "true")
        _set_video_info(li, {
            "title": title,
            "mediatype": "video",
        })

        xbmcplugin.setResolvedUrl(addon_handle, True, li)
        return


    xbmcgui.Dialog().ok(

        i18n.T(31300, "TMDB / Trailer"),

        i18n.T(31302, "Unsupported video source: {source}").format(source=site or i18n.T(31303, "unknown"))

    )
    try:
        xbmcplugin.setResolvedUrl(addon_handle, False, xbmcgui.ListItem())
    except Exception:
        pass

def _tmdb_movie_detail(tmdb_id: str) -> dict:
    if not tmdb_id:
        return {}

    return _tmdb_get(f"/movie/{tmdb_id}", params={
        "language": TMDB_LANGUAGE,
        "append_to_response": "credits,videos,similar,alternative_titles",
    }, timeout=20)


def _tmdb_movie_listing_detail(tmdb_id: str) -> dict:
    if not tmdb_id:
        return {}
    return _tmdb_get(f"/movie/{tmdb_id}", params={
        "language": TMDB_LANGUAGE,
        "append_to_response": "alternative_titles",
    }, timeout=4)


def _tmdb_playback_movie_meta(tmdb_id: str) -> dict:
    tmdb_id = str(tmdb_id or "").strip()

    if not tmdb_id:

        return {}



    item = _tmdb_movie_detail(tmdb_id)

    if not item:

        return {}



    title, title_is_local = _tmdb_display_title_info(item, "movie")
    original_title = (item.get("original_title") or "").strip()

    release_date = (item.get("release_date") or "").strip()

    year = release_date[:4] if len(release_date) >= 4 and release_date[:4].isdigit() else ""

    poster_path = (item.get("poster_path") or "").strip()

    plot = (item.get("overview") or "").strip()

    rating = item.get("vote_average")



    meta = {

        "title": title,

        "original_title": original_title,

        "year": year,

        "plot": plot,

        "rating": rating,

        "media_type": "movie",

        "_title_is_local": True if title_is_local else "",
    }

    if poster_path:

        meta["poster"] = TMDB_IMAGE_BASE + poster_path

    return {k: v for k, v in meta.items() if v not in (None, "")}





def tmdb_movie_mark_finished():
    tmdb_id = _get_param("id", "").strip() or _get_param("tmdb_id", "").strip()

    title = _get_param("title", "").strip()

    if not tmdb_id:

        return



    meta = {}

    try:

        meta = _tmdb_playback_movie_meta(tmdb_id)

    except Exception:

        meta = {}

    if title and not meta.get("title"):

        meta["title"] = title

    meta["media_type"] = "movie"



    watched_at = _tmdb_watch_now_iso()
    try:
        tmdb_playback_state.mark_movie_finished(tmdb_id, meta, watched_at=watched_at)
        trakt_ok = _tmdb_push_trakt_finished_history(
            movies=[{"tmdb_id": tmdb_id, "watched_at": watched_at}],
        )
        xbmcgui.Dialog().notification(
            "TMDB",
            i18n.T(31792, "Movie marked as watched") if trakt_ok else i18n.T(31793, "Movie marked locally, Trakt will sync later"),
            xbmcgui.NOTIFICATION_INFO if trakt_ok else xbmcgui.NOTIFICATION_WARNING,
            2500,
            sound=False,
        )
    except Exception as e:

        xbmcgui.Dialog().notification(

            "TMDB",

            i18n.T(31794, "Marking failed: {error}").format(error=e),

            xbmcgui.NOTIFICATION_ERROR,

            3500,

            sound=False,

        )

        return



    _tmdb_refresh_watch_container()





def _tmdb_clear_trakt_watch_state(media_type: str, tmdb_id: str, season=None, episode=None):
    operation_id = ""
    try:
        operation_id = tmdb_playback_sync.queue_clear_status(
            media_type,
            tmdb_id,
            season=season,
            episode=episode,
        )
        if not operation_id:
            return False
        if not trakt_client.is_connected():
            return False
        result = tmdb_playback_sync.flush_pending_and_refresh(limit=50)
        if result.get("errors"):
            xbmc.log(f"[IKARUS][TRAKT] clear watch state warnings: {result.get('errors')}", xbmc.LOGWARNING)
        return not tmdb_playback_sync.operation_pending(operation_id)
    except Exception as e:
        try:
            xbmc.log(f"[IKARUS][TRAKT] clear watch state error: {e}", xbmc.LOGWARNING)
        except Exception:
            pass
        return False


def _tmdb_watch_now_iso() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _tmdb_push_trakt_finished_history(movies=None, episodes=None) -> bool:
    operation_ids = []
    try:
        operation_ids = tmdb_playback_sync.queue_history_items(
            movies=list(movies or []),
            episodes=list(episodes or []),
        )
        if not operation_ids:
            return True
        if not trakt_client.is_connected():
            return False
        result = tmdb_playback_sync.flush_pending_and_refresh(limit=max(200, len(operation_ids) + 20))
        if result.get("errors"):
            return False
        return not any(tmdb_playback_sync.operation_pending(value) for value in operation_ids)
    except Exception as e:
        try:
            xbmc.log(f"[IKARUS][TRAKT] manual watched push error: {e}", xbmc.LOGWARNING)
        except Exception:
            pass
        return False


def tmdb_movie_watch_clear():
    tmdb_id = _get_param("id", "").strip() or _get_param("tmdb_id", "").strip()

    if not tmdb_id:

        return



    try:
        tmdb_playback_state.remove_item(tmdb_id)
        trakt_ok = _tmdb_clear_trakt_watch_state("movie", tmdb_id)
        xbmcgui.Dialog().notification(
            "TMDB",
            i18n.T(31795, "Movie mark was cleared") if trakt_ok else i18n.T(31796, "Movie removed locally, Trakt will sync later"),
            xbmcgui.NOTIFICATION_INFO if trakt_ok else xbmcgui.NOTIFICATION_WARNING,
            2500,
            sound=False,
        )
    except Exception as e:

        xbmcgui.Dialog().notification(

            "TMDB",

            i18n.T(31797, "Clearing failed: {error}").format(error=e),

            xbmcgui.NOTIFICATION_ERROR,

            3500,

            sound=False,

        )

        return



    _tmdb_refresh_watch_container()





def _tmdb_tv_meta_with_title(tmdb_id: str, title: str = "") -> dict:

    meta = {}

    try:

        meta = _tmdb_playback_tv_meta(tmdb_id)

    except Exception:

        meta = {}

    if title and not meta.get("title"):

        meta["title"] = title

    meta["media_type"] = "tv"

    return meta





def tmdb_tv_episode_mark_finished():

    tmdb_id = _get_param("id", "").strip() or _get_param("tmdb_id", "").strip()

    title = _get_param("title", "").strip()

    season = _get_param("season", "").strip() or _get_param("season_number", "").strip()

    episode = _get_param("episode", "").strip()

    episode_title = _get_param("episode_title", "").strip()

    season_name = _get_param("season_name", "").strip()

    if not tmdb_id or season == "" or episode == "":

        return



    watched_at = _tmdb_watch_now_iso()
    try:
        tmdb_playback_state.mark_episode_finished(
            tmdb_id,
            season,
            episode,
            meta=_tmdb_tv_meta_with_title(tmdb_id, title),
            episode_title=episode_title,
            season_name=season_name,
            watched_at=watched_at,
        )
        trakt_ok = _tmdb_push_trakt_finished_history(episodes=[{
            "tmdb_id": tmdb_id,
            "season": season,
            "episode": episode,
            "watched_at": watched_at,
        }])
        xbmcgui.Dialog().notification(
            "TMDB",
            i18n.T(31798, "Episode marked as watched") if trakt_ok else i18n.T(31799, "Episode marked locally, Trakt will sync later"),
            xbmcgui.NOTIFICATION_INFO if trakt_ok else xbmcgui.NOTIFICATION_WARNING,
            2200,
            sound=False,
        )
    except Exception as e:

        xbmcgui.Dialog().notification("TMDB", i18n.T(31800, "Episode marking failed: {error}").format(error=e), xbmcgui.NOTIFICATION_ERROR, 3500, sound=False)

        return



    _tmdb_refresh_watch_container()





def tmdb_tv_episode_watch_clear():

    tmdb_id = _get_param("id", "").strip() or _get_param("tmdb_id", "").strip()

    season = _get_param("season", "").strip() or _get_param("season_number", "").strip()

    episode = _get_param("episode", "").strip()

    if not tmdb_id or season == "" or episode == "":

        return



    try:
        tmdb_playback_state.remove_episode(tmdb_id, season, episode)
        trakt_ok = _tmdb_clear_trakt_watch_state("episode", tmdb_id, season=season, episode=episode)
        xbmcgui.Dialog().notification(
            "TMDB",
            i18n.T(31801, "Episode mark was cleared") if trakt_ok else i18n.T(31802, "Episode removed locally, Trakt will sync later"),
            xbmcgui.NOTIFICATION_INFO if trakt_ok else xbmcgui.NOTIFICATION_WARNING,
            2200,
            sound=False,
        )
    except Exception as e:

        xbmcgui.Dialog().notification("TMDB", i18n.T(31803, "Episode clearing failed: {error}").format(error=e), xbmcgui.NOTIFICATION_ERROR, 3500, sound=False)

        return



    _tmdb_refresh_watch_container()





def tmdb_tv_season_mark_finished():

    tmdb_id = _get_param("id", "").strip() or _get_param("tmdb_id", "").strip()

    title = _get_param("title", "").strip()

    season = _get_param("season", "").strip() or _get_param("season_number", "").strip()

    season_name = _get_param("season_name", "").strip()

    if not tmdb_id or season == "":

        return



    data = _tmdb_tv_season_detail(tmdb_id, season)

    episodes = data.get("episodes") or []

    if not episodes:

        xbmcgui.Dialog().notification("TMDB", i18n.T(31323, "The series has no available episodes"), xbmcgui.NOTIFICATION_WARNING, 2500, sound=False)

        return



    meta = _tmdb_tv_meta_with_title(tmdb_id, title)
    watched_at = _tmdb_watch_now_iso()
    try:
        bulk_items = []
        trakt_episodes = []
        for ep in episodes:
            ep_num = ep.get("episode_number")
            if ep_num is None:
                continue
            ep_title = (ep.get("name") or "").strip()

            bulk_items.append({

                "season": season,

                "episode": ep_num,
                "episode_title": ep_title,
                "season_name": season_name,
            })
            trakt_episodes.append({
                "tmdb_id": tmdb_id,
                "season": season,
                "episode": ep_num,
                "watched_at": watched_at,
            })
        tmdb_playback_state.mark_episodes_finished_bulk(tmdb_id, bulk_items, meta=meta, watched_at=watched_at)
        trakt_ok = _tmdb_push_trakt_finished_history(episodes=trakt_episodes)
        xbmcgui.Dialog().notification(
            "TMDB",
            i18n.T(31804, "Season marked as watched") if trakt_ok else i18n.T(31805, "Season marked locally, Trakt will sync later"),
            xbmcgui.NOTIFICATION_INFO if trakt_ok else xbmcgui.NOTIFICATION_WARNING,
            2500,
            sound=False,
        )
    except Exception as e:

        xbmcgui.Dialog().notification("TMDB", i18n.T(31806, "Season marking failed: {error}").format(error=e), xbmcgui.NOTIFICATION_ERROR, 3500, sound=False)

        return



    _tmdb_refresh_watch_container()





def tmdb_tv_season_watch_clear():

    tmdb_id = _get_param("id", "").strip() or _get_param("tmdb_id", "").strip()

    season = _get_param("season", "").strip() or _get_param("season_number", "").strip()

    if not tmdb_id or season == "":

        return



    try:
        tmdb_playback_state.remove_season(tmdb_id, season)
        trakt_ok = _tmdb_clear_trakt_watch_state("show", tmdb_id, season=season)
        xbmcgui.Dialog().notification(
            "TMDB",
            i18n.T(31807, "Season mark was cleared") if trakt_ok else i18n.T(31808, "Season removed locally, Trakt will sync later"),
            xbmcgui.NOTIFICATION_INFO if trakt_ok else xbmcgui.NOTIFICATION_WARNING,
            2200,
            sound=False,
        )
    except Exception as e:

        xbmcgui.Dialog().notification("TMDB", i18n.T(31809, "Season clearing failed: {error}").format(error=e), xbmcgui.NOTIFICATION_ERROR, 3500, sound=False)

        return



    _tmdb_refresh_watch_container()





def tmdb_tv_mark_finished():

    tmdb_id = _get_param("id", "").strip() or _get_param("tmdb_id", "").strip()

    title = _get_param("title", "").strip()

    if not tmdb_id:

        return



    item = _tmdb_tv_detail(tmdb_id)

    seasons = item.get("seasons") if isinstance(item, dict) else []

    seasons = seasons or []

    meta = _tmdb_tv_meta_with_title(tmdb_id, title)



    marked = 0
    watched_at = _tmdb_watch_now_iso()
    try:
        bulk_items = []
        trakt_episodes = []
        for season in seasons:
            season_number = season.get("season_number")

            if season_number is None:

                continue

            data = _tmdb_tv_season_detail(tmdb_id, str(season_number))

            for ep in (data.get("episodes") or []):

                ep_num = ep.get("episode_number")

                if ep_num is None:

                    continue

                bulk_items.append({
                    "season": season_number,
                    "episode": ep_num,
                    "episode_title": (ep.get("name") or "").strip(),
                    "season_name": (season.get("name") or "").strip(),
                })
                trakt_episodes.append({
                    "tmdb_id": tmdb_id,
                    "season": season_number,
                    "episode": ep_num,
                    "watched_at": watched_at,
                })
        marked = tmdb_playback_state.mark_episodes_finished_bulk(tmdb_id, bulk_items, meta=meta, watched_at=watched_at)
        trakt_ok = _tmdb_push_trakt_finished_history(episodes=trakt_episodes)
        xbmcgui.Dialog().notification(
            "TMDB",
            i18n.T(31810, "Series marked as watched ({count} ep.)").format(count=marked) if trakt_ok else i18n.T(31811, "Series marked locally ({count} ep.), Trakt will sync later").format(count=marked),
            xbmcgui.NOTIFICATION_INFO if trakt_ok else xbmcgui.NOTIFICATION_WARNING,
            3000,
            sound=False,
        )
    except Exception as e:

        xbmcgui.Dialog().notification("TMDB", i18n.T(31812, "Series marking failed: {error}").format(error=e), xbmcgui.NOTIFICATION_ERROR, 3500, sound=False)

        return



    _tmdb_refresh_watch_container()





def tmdb_tv_watch_clear():

    tmdb_id = _get_param("id", "").strip() or _get_param("tmdb_id", "").strip()

    if not tmdb_id:

        return



    try:
        tmdb_playback_state.remove_item(tmdb_id)
        trakt_ok = _tmdb_clear_trakt_watch_state("show", tmdb_id)
        xbmcgui.Dialog().notification(
            "TMDB",
            i18n.T(31813, "Series mark was cleared") if trakt_ok else i18n.T(31814, "Series removed locally, Trakt will sync later"),
            xbmcgui.NOTIFICATION_INFO if trakt_ok else xbmcgui.NOTIFICATION_WARNING,
            2500,
            sound=False,
        )
    except Exception as e:

        xbmcgui.Dialog().notification("TMDB", i18n.T(31815, "Series clearing failed: {error}").format(error=e), xbmcgui.NOTIFICATION_ERROR, 3500, sound=False)

        return



    _tmdb_refresh_watch_container()





def _tmdb_playback_tv_meta(tmdb_id: str) -> dict:
    tmdb_id = str(tmdb_id or "").strip()

    if not tmdb_id:

        return {}



    item = _tmdb_tv_detail(tmdb_id)

    if not item:

        return {}



    title, title_is_local = _tmdb_display_title_info(item, "tv")
    original_title = (item.get("original_name") or "").strip()

    first_air_date = (item.get("first_air_date") or "").strip()

    year = first_air_date[:4] if len(first_air_date) >= 4 and first_air_date[:4].isdigit() else ""

    poster_path = (item.get("poster_path") or "").strip()

    plot = (item.get("overview") or "").strip()

    rating = item.get("vote_average")

    total_episodes = item.get("number_of_episodes")

    total_seasons = item.get("number_of_seasons")



    meta = {

        "title": title,

        "original_title": original_title,

        "year": year,

        "plot": plot,

        "rating": rating,

        "media_type": "tv",

        "_title_is_local": True if title_is_local else "",
        "total_episodes": total_episodes,

        "total_seasons": total_seasons,

    }

    if poster_path:

        meta["poster"] = TMDB_IMAGE_BASE + poster_path

    return {k: v for k, v in meta.items() if v not in (None, "")}



def _tmdb_tv_detail(tmdb_id: str) -> dict:
    if not tmdb_id:
        return {}

    return _tmdb_get(f"/tv/{tmdb_id}", params={
        "language": TMDB_LANGUAGE,
        "append_to_response": "credits,videos,similar,alternative_titles",
    }, timeout=20)


def _tmdb_tv_listing_detail(tmdb_id: str) -> dict:
    if not tmdb_id:
        return {}
    return _tmdb_get(f"/tv/{tmdb_id}", params={
        "language": TMDB_LANGUAGE,
        "append_to_response": "alternative_titles",
    }, timeout=4)


def _tmdb_tv_season_detail(tv_id: str, season_number: str) -> dict:
    if not tv_id or season_number == "":

        return {}



    return _tmdb_get(f"/tv/{tv_id}/season/{season_number}", params={

        "language": TMDB_LANGUAGE,

    }, timeout=20)



def tmdb_tv_seasons_menu():
    tmdb_id = _get_param("id", "").strip()
    fallback_title = _get_param("title", i18n.T(31913, "Series")).strip() or i18n.T(31913, "Series")
    original_title = _get_param("original_title", "").strip()
    watch_source = _tmdb_watch_source_param()


    if not tmdb_id:

        _placeholder_dir(i18n.T(31304, "TMDB / Series / Seasons"), i18n.T(31305, "(missing TMDB id)"))

        return



    item = _tmdb_tv_detail(tmdb_id)

    if not item:

        _placeholder_dir(i18n.T(31304, "TMDB / Series / Seasons"), i18n.T(31306, "({title}) - detail could not be loaded").format(title=fallback_title))

        return



    title = _tmdb_display_title(item, "tv", fallback_title) or fallback_title
    if not original_title:

        original_title = (item.get("original_name") or "").strip()

    seasons = item.get("seasons") or []



    xbmcplugin.setPluginCategory(addon_handle, i18n.T(31307, "TMDB / Series / {title} / Seasons").format(title=title))

    xbmcplugin.setContent(addon_handle, "seasons")



    try:

        tmdb_playback_state.refresh_from_remote()

    except Exception:

        pass



    try:
        tmdb_playback_state.update_item_meta(tmdb_id, _tmdb_playback_tv_meta(tmdb_id))
    except Exception:
        pass

    playback_row = _tmdb_tv_playback_status_row(tmdb_id, watch_source)

    added = 0
    for season in seasons:

        season_number = season.get("season_number")

        name = (season.get("name") or "").strip() or i18n.T(31308, "Season {number}").format(number=season_number)

        air_date = (season.get("air_date") or "").strip()

        episode_count = season.get("episode_count") or 0

        poster_path = (season.get("poster_path") or "").strip()

        overview = (season.get("overview") or "").strip()



        if season_number is None:

            continue



        label = name

        meta = []

        if air_date and len(air_date) >= 4:

            meta.append(air_date[:4])

        if episode_count:

            meta.append(i18n.T(31309, "{count} ep.").format(count=episode_count))

        if meta:

            label += "  [" + " | ".join(meta) + "]"



        season_status = ""
        try:
            season_status = tmdb_playback_sync.get_row_season_status(
                playback_row,
                season_number,
                total_episodes=episode_count,
            )
        except Exception:
            season_status = ""


        li = xbmcgui.ListItem(label=_tmdb_colorize_status_label(label, season_status))


        art = {

            "icon": "DefaultFolder.png",

            "thumb": "DefaultFolder.png",

            "poster": "DefaultFolder.png",

        }

        if poster_path:

            full = TMDB_IMAGE_BASE + poster_path

            art["icon"] = full

            art["thumb"] = full

            art["poster"] = full



        li.setArt(art)

        info = {

            "title": name,

            "tvshowtitle": title,

            "season": int(season_number) if str(season_number).isdigit() else 0,

            "plot": overview,

            "premiered": air_date,

            "mediatype": "season",

        }

        _set_video_info(li, _tmdb_apply_explicit_status(li, season_status, info))

        _tmdb_add_tv_season_watch_context(

            li,

            tmdb_id,

            title=title,

            season=season_number,

            season_name=name,

            status=season_status,

        )



        if season_status in ("started", "finished"):
            u = build_url(_tmdb_add_watch_source({
                "action": "tmdb.tv.season.episodes.open",
                "id": str(tmdb_id),
                "title": title,
                "season_number": str(season_number),
                "season_name": name,
                "original_title": original_title,
            }, watch_source))
            is_folder = False
        else:
            u = build_url(_tmdb_add_watch_source({
                "action": "tmdb.tv.season.episodes",
                "id": str(tmdb_id),
                "title": title,
                "season_number": str(season_number),
                "season_name": name,
                "original_title": original_title,
            }, watch_source))
            is_folder = True


        xbmcplugin.addDirectoryItem(

            handle=addon_handle,

            url=u,

            listitem=li,

            isFolder=is_folder

        )

        added += 1



    if added == 0:

        _placeholder_dir(i18n.T(31307, "TMDB / Series / {title} / Seasons").format(title=title), i18n.T(31310, "(no seasons)"))

        return



    _end("seasons")

    

def tmdb_tv_season_episodes_menu():
    tmdb_id = _get_param("id", "").strip()
    title = _get_param("title", i18n.T(31913, "Series")).strip() or i18n.T(31913, "Series")
    season_number = _get_param("season_number", "").strip()
    season_name = _get_param("season_name", "").strip() or i18n.T(31914, "Season {season}").format(season=season_number)
    watch_source = _tmdb_watch_source_param()


    if not tmdb_id or season_number == "":

        _placeholder_dir(i18n.T(31311, "TMDB / Series / Episodes"), i18n.T(31312, "(missing TMDB id or season_number)"))

        return



    data = _tmdb_tv_season_detail(tmdb_id, season_number)

    if not data:

        _placeholder_dir(i18n.T(31311, "TMDB / Series / Episodes"), i18n.T(31313, "({title} / {season}) - data could not be loaded").format(title=title, season=season_name))

        return



    episodes = data.get("episodes") or []



    xbmcplugin.setPluginCategory(addon_handle, i18n.T(31314, "TMDB / Series / {title} / {season}").format(title=title, season=season_name))

    xbmcplugin.setContent(addon_handle, "episodes")



    try:

        tmdb_playback_state.refresh_from_remote()

    except Exception:

        pass



    if not episodes:
        _placeholder_dir(i18n.T(31314, "TMDB / Series / {title} / {season}").format(title=title, season=season_name), i18n.T(31315, "(no episodes)"))
        return

    series_original_title = _get_param("original_title", "").strip()
    first_air_date = _get_param("year", "").strip()
    if not first_air_date:
        first_air_date = (_get_param("first_air_date", "") or "").strip()

    tv_detail_for_series = None
    if not series_original_title or not first_air_date:
        tv_detail_for_series = _tmdb_tv_detail(tmdb_id)

    if not series_original_title:
        series_original_title = (
            (tv_detail_for_series or {}).get("original_name")
            or title
        ).strip()

    if not first_air_date:
        first_air_date = (tv_detail_for_series or {}).get("first_air_date", "").strip()

    series_year = first_air_date[:4] if len(first_air_date) >= 4 and first_air_date[:4].isdigit() else ""

    playback_row = _tmdb_tv_playback_status_row(tmdb_id, watch_source)

    for ep in episodes:
        ep_num = ep.get("episode_number") or 0
        ep_name = (ep.get("name") or "").strip() or f"Epizoda {ep_num}"
        air_date = (ep.get("air_date") or "").strip()

        vote_average = ep.get("vote_average") or 0

        still_path = (ep.get("still_path") or "").strip()

        overview = (ep.get("overview") or "").strip()



        label = f"{ep_num}. {ep_name}" if ep_num else ep_name



        meta = []

        if air_date:

            meta.append(air_date)

        try:

            if float(vote_average) > 0:

                meta.append("TMDB %.1f" % float(vote_average))

        except Exception:

            pass

        if meta:

            label += "  [" + " | ".join(meta) + "]"



        episode_status = ""
        try:
            episode_status = tmdb_playback_sync.get_row_episode_status(
                playback_row,
                season_number,
                ep_num,
            )
        except Exception:
            episode_status = ""


        li = xbmcgui.ListItem(label=_tmdb_colorize_status_label(label, episode_status))


        art = {

            "icon": "DefaultVideo.png",

            "thumb": "DefaultVideo.png",

            "poster": "DefaultVideo.png",

        }

        if still_path:

            full = TMDB_IMAGE_BASE + still_path

            art["icon"] = full

            art["thumb"] = full

            art["poster"] = full



        li.setArt(art)

        info = {

            "title": ep_name,

            "tvshowtitle": title,

            "season": int(season_number) if str(season_number).isdigit() else 0,

            "episode": int(ep_num) if str(ep_num).isdigit() else 0,

            "plot": overview,

            "premiered": air_date,

            "rating": float(vote_average) if vote_average else 0.0,

            "mediatype": "episode",

        }

        _set_video_info(li, _tmdb_apply_explicit_status(li, episode_status, info))

        _tmdb_add_tv_episode_watch_context(

            li,

            tmdb_id,

            title=title,

            season=season_number,

            episode=ep_num,

            episode_title=ep_name,

            season_name=season_name,

            status=episode_status,
        )

        u = build_url({
            "action": "tmdb.movie.sources.search.stub",
            "tmdb_id": str(tmdb_id),
            "title": title,
            "original_title": series_original_title,
            "year": series_year,
            "mode": "serial_episode",
            "season": str(season_number),
            "episode": str(ep_num),
            "episode_title": ep_name,
            "season_name": season_name,
            "query": f"{title} S{int(season_number):02d}E{int(ep_num):02d}",
        })
        is_folder = True


        xbmcplugin.addDirectoryItem(

            handle=addon_handle,

            url=u,

            listitem=li,

            isFolder=is_folder

        )



    _end("episodes")



def _tmdb_label_movie(item: dict, prefix: str = "") -> str:
    title = (_tmdb_display_title(item, "movie", _tmdb_untitled()) or _tmdb_untitled()).strip()
    premiered = (item.get("release_date") or "").strip()

    rating = item.get("vote_average") or 0



    parts = []

    if len(premiered) >= 4 and premiered[:4].isdigit():

        parts.append(premiered[:4])



    try:

        if float(rating) > 0:

            parts.append("TMDB %.1f" % float(rating))

    except Exception:

        pass



    label = title

    if parts:

        label = f"{title}  [{' | '.join(parts)}]"



    if prefix:

        label = f"[{prefix}] {label}"



    return label



def _tmdb_merged_item_status(tmdb_id) -> str:
    try:
        return (tmdb_playback_sync.merged_item_status(tmdb_id, include_trakt=True) or "").strip().lower()
    except Exception:
        try:
            return (tmdb_playback_state.get_item_status(tmdb_id) or "").strip().lower()
        except Exception:
            return ""


def _tmdb_apply_watch_state(li, tmdb_id, info: dict = None):
    overlay = None
    playcount = 0
    try:
        overlay, playcount = tmdb_playback_state.apply_status(li, _tmdb_merged_item_status(tmdb_id))
    except Exception:
        overlay, playcount = None, 0


    if isinstance(info, dict):

        info["playcount"] = int(playcount or 0)

        if overlay is not None:

            info["overlay"] = overlay

    return info or {}





def _tmdb_apply_explicit_status(li, status: str, info: dict = None):
    overlay = None

    playcount = 0

    try:

        overlay, playcount = tmdb_playback_state.apply_status(li, status)

    except Exception:

        overlay, playcount = None, 0



    if isinstance(info, dict):

        info["playcount"] = int(playcount or 0)

        if overlay is not None:

            info["overlay"] = overlay

    return info or {}


def _tmdb_status_map_for_items(items: list) -> dict:
    try:
        state = tmdb_playback_sync.merged_state(include_trakt=True) or {}
    except Exception:
        state = {}

    status_map = {}
    for item in items or []:
        try:
            tmdb_id = str((item or {}).get("id") or "").strip()
        except Exception:
            tmdb_id = ""
        if not tmdb_id:
            continue
        row = state.get(tmdb_id)
        if not isinstance(row, dict):
            status_map[tmdb_id] = ""
            continue
        try:
            status_map[tmdb_id] = (tmdb_playback_state.get_status_bucket(row) or "").strip().lower()
        except Exception:
            status_map[tmdb_id] = ""
    return status_map


def _tmdb_status_symbol(status: str) -> str:
    status = (status or "").strip().lower()
    if status == "finished":
        return "\u2713"
    if status == "started":
        return "\u25D0"
    return ""


def _tmdb_prefix_status_symbol(label: str, status: str) -> str:
    symbol = _tmdb_status_symbol(status)
    label = str(label or "")
    if not symbol:
        return label
    stripped = label.lstrip()
    if stripped.startswith(("\u2713", "\u25D0")):
        return label
    return f"{symbol} {label}"


def _tmdb_colorize_status_label(label: str, status: str, with_symbol: bool = False) -> str:
    status = (status or "").strip().lower()
    if status == "finished":
        return f"[COLOR lime]{label}[/COLOR]"
    if status == "started":
        return f"[COLOR orange]{label}[/COLOR]"
    return label


def _tmdb_colorize_movie_label(label: str, tmdb_id, status_override=None) -> str:
    if status_override is None:
        try:
            status = _tmdb_merged_item_status(tmdb_id)
        except Exception:
            status = ""
    else:
        status = (status_override or "").strip().lower()
    return _tmdb_colorize_status_label(label, status)


def _tmdb_colorize_tv_label(label: str, tmdb_id, status_override=None) -> str:
    if status_override is None:
        try:
            status = _tmdb_merged_item_status(tmdb_id)
        except Exception:
            status = ""
    else:
        status = (status_override or "").strip().lower()
    return _tmdb_colorize_status_label(label, status)




def _tmdb_add_custom_list_context_item(items: list, media_type: str, tmdb_id, title: str = ""):
    tmdb_id = str(tmdb_id or "").strip()

    media_type = (media_type or "").strip().lower()

    if media_type not in ("movie", "tv") or not tmdb_id:

        return



    items.append((

        i18n.T(31835, "Add to custom list"),

        "RunPlugin(%s)" % build_url({

            "action": "tmdb.custom.add",

            "media_type": media_type,

            "id": tmdb_id,

            "title": (title or "").strip(),

        })

    ))


def _tmdb_add_trakt_list_context_item(items: list, media_type: str, tmdb_id, title: str = ""):
    tmdb_id = str(tmdb_id or "").strip()
    media_type = (media_type or "").strip().lower()
    if media_type not in ("movie", "tv") or not tmdb_id:
        return
    try:
        if not trakt_client.is_connected():
            return
    except Exception:
        return

    items.append((
        i18n.T(30428, "Vlo\u017eit do Trakt seznamu"),
        "RunPlugin(%s)" % build_url({
            "action": "tmdb.trakt.add",
            "media_type": media_type,
            "id": tmdb_id,
            "title": (title or "").strip(),
        })
    ))


def _tmdb_add_movie_watch_context(li, tmdb_id, title: str = "", status: str = "", return_action: str = ""):
    tmdb_id = str(tmdb_id or "").strip()

    if not tmdb_id:

        return



    title = (title or "").strip()

    return_action = (return_action or "").strip()

    mark_query = {

        "action": "tmdb.movie.mark.finished",

        "id": tmdb_id,

        "title": title,

    }

    if return_action:

        mark_query["return_action"] = return_action

    items = [

        (

            i18n.T(31836, "Mark as watched"),

            "RunPlugin(%s)" % build_url(mark_query)

        )

    ]
    _tmdb_add_custom_list_context_item(items, "movie", tmdb_id, title)
    status = (status or "").strip().lower()
    replace_items = status in ("started", "finished")



    if replace_items:

        clear_query = {

            "action": "tmdb.movie.watch.clear",

            "id": tmdb_id,

            "title": title,

        }

        if return_action:

            clear_query["return_action"] = return_action

        items.append((

            i18n.T(31837, "Unmark movie"),

            "RunPlugin(%s)" % build_url(clear_query)

        ))

        items.extend([

            (i18n.T(31838, "Information"), "Action(Info)"),

            (i18n.T(30427, "P\u0159idat do obl\u00edben\u00fdch"), "Action(ToggleFavorite)"),

        ])



    try:

        li.addContextMenuItems(items, replaceItems=replace_items)

    except Exception:

        pass





def _tmdb_add_tv_watch_context(li, tmdb_id, title: str = "", status: str = "", return_action: str = ""):

    tmdb_id = str(tmdb_id or "").strip()

    if not tmdb_id:

        return



    title = (title or "").strip()

    return_action = (return_action or "").strip()

    mark_query = {

        "action": "tmdb.tv.mark.finished",

        "id": tmdb_id,

        "title": title,

    }

    if return_action:

        mark_query["return_action"] = return_action

    items = [(

        i18n.T(31839, "Mark series as watched"),

        "RunPlugin(%s)" % build_url(mark_query)

    )]
    _tmdb_add_custom_list_context_item(items, "tv", tmdb_id, title)
    status = (status or "").strip().lower()
    replace_items = status in ("started", "finished")



    if replace_items:

        clear_query = {

            "action": "tmdb.tv.watch.clear",

            "id": tmdb_id,

            "title": title,

        }

        if return_action:

            clear_query["return_action"] = return_action

        items.append((

            i18n.T(31840, "Unmark series"),

            "RunPlugin(%s)" % build_url(clear_query)

        ))

        items.extend([

            (i18n.T(31838, "Information"), "Action(Info)"),

            (i18n.T(30427, "P\u0159idat do obl\u00edben\u00fdch"), "Action(ToggleFavorite)"),

        ])



    try:

        li.addContextMenuItems(items, replaceItems=replace_items)

    except Exception:

        pass





def _tmdb_add_tv_season_watch_context(li, tmdb_id, title: str = "", season=None,

                                      season_name: str = "", status: str = ""):

    tmdb_id = str(tmdb_id or "").strip()

    season = str(season if season is not None else "").strip()

    if not tmdb_id or season == "":

        return



    items = [(

        i18n.T(31841, "Mark season as watched"),

        "RunPlugin(%s)" % build_url({

            "action": "tmdb.tv.season.mark.finished",

            "id": tmdb_id,

            "title": title,

            "season": season,

            "season_number": season,

            "season_name": season_name,

        })

    )]

    status = (status or "").strip().lower()

    replace_items = status in ("started", "finished")



    if replace_items:

        items.append((

            i18n.T(31842, "Unmark season"),

            "RunPlugin(%s)" % build_url({

                "action": "tmdb.tv.season.watch.clear",

                "id": tmdb_id,

                "title": title,

                "season": season,

                "season_number": season,

                "season_name": season_name,

            })

        ))

        items.extend([

            (i18n.T(31838, "Information"), "Action(Info)"),

            (i18n.T(30427, "P\u0159idat do obl\u00edben\u00fdch"), "Action(ToggleFavorite)"),

        ])



    try:

        li.addContextMenuItems(items, replaceItems=replace_items)

    except Exception:

        pass





def _tmdb_add_tv_episode_watch_context(li, tmdb_id, title: str = "", season=None, episode=None,

                                       episode_title: str = "", season_name: str = "",

                                       status: str = ""):

    tmdb_id = str(tmdb_id or "").strip()

    season = str(season if season is not None else "").strip()

    episode = str(episode if episode is not None else "").strip()

    if not tmdb_id or season == "" or episode == "":

        return



    items = [(

        i18n.T(31843, "Mark episode as watched"),

        "RunPlugin(%s)" % build_url({

            "action": "tmdb.tv.episode.mark.finished",

            "id": tmdb_id,

            "title": title,

            "season": season,

            "episode": episode,

            "episode_title": episode_title,

            "season_name": season_name,

        })

    )]

    status = (status or "").strip().lower()

    replace_items = status in ("started", "finished")



    if replace_items:

        items.append((

            i18n.T(31844, "Unmark episode"),

            "RunPlugin(%s)" % build_url({

                "action": "tmdb.tv.episode.watch.clear",

                "id": tmdb_id,

                "title": title,

                "season": season,

                "episode": episode,

                "episode_title": episode_title,

                "season_name": season_name,

            })

        ))

        items.extend([

            (i18n.T(31838, "Information"), "Action(Info)"),

            (i18n.T(30427, "P\u0159idat do obl\u00edben\u00fdch"), "Action(ToggleFavorite)"),

        ])



    try:

        li.addContextMenuItems(items, replaceItems=replace_items)

    except Exception:

        pass





def _add_movie_result(item: dict, prefix: str = "", extra_context_items=None,
                      watch_source: str = "", status_override=None, return_item: bool = False,
                      compact_info: bool = False):
    tmdb_id = item.get("id")
    watch_source = "trakt" if str(watch_source or "").strip().lower() == "trakt" else ""
    if watch_source == "trakt":
        if status_override is not None:
            status = (status_override or "").strip().lower()
        else:
            status = (tmdb_playback_sync.trakt_item_status("movie", tmdb_id) or "").strip().lower()
    else:
        if status_override is not None:
            status = (status_override or "").strip().lower()
        else:
            status = ""
            try:
                status = _tmdb_merged_item_status(tmdb_id)
            except Exception:
                status = ""
    title = _tmdb_display_title(item, "movie", _tmdb_untitled()) or _tmdb_untitled()


    li = xbmcgui.ListItem(label=_tmdb_colorize_movie_label(
        _tmdb_label_movie(item, prefix=prefix),
        tmdb_id,
        status_override=status if (watch_source == "trakt" or status_override is not None) else None,
    ))
    li.setArt(_tmdb_media_art(item))
    info = _tmdb_movie_info(item)
    if watch_source == "trakt":
        info = _tmdb_apply_explicit_status(li, status, info)
    elif status_override is not None:
        info = _tmdb_apply_explicit_status(li, status, info)
    else:
        info = _tmdb_apply_watch_state(li, tmdb_id, info)
    if compact_info:
        _set_video_info_compact(li, info)
    else:
        _set_video_info(li, info)
    li.setProperty("IsPlayable", "false")
    _tmdb_add_movie_watch_context(li, tmdb_id, title, status)
    try:
        extra_items = list(extra_context_items or [])
        if extra_items:
            li.addContextMenuItems(extra_items, replaceItems=False)
    except Exception:
        pass

    if status == "started":
        u = build_url({
            "action": "tmdb.movie.detail.open",
            "id": str(tmdb_id or ""),
            "title": title,
            "original_title": item.get("original_title") or "",
            "watch_source": watch_source,
        })
        is_folder = False

    else:

        u = build_url(_tmdb_add_watch_source({
            "action": "tmdb.movie.detail",
            "id": str(tmdb_id or ""),
            "title": title
        }, watch_source))
        is_folder = True

    directory_item = (u, li, is_folder)
    if return_item:
        return directory_item
    xbmcplugin.addDirectoryItem(
        handle=addon_handle,
        url=directory_item[0],
        listitem=directory_item[1],
        isFolder=directory_item[2]
    )
    return True


def _add_movie_detail_header(item: dict, watch_source: str = ""):
    title = _tmdb_display_title(item, "movie", _tmdb_untitled()) or _tmdb_untitled()
    original = _tmdb_hide_unreadable_text(item.get("original_title") or "")
    overview = item.get("overview") or ""

    release_date = item.get("release_date") or ""

    vote_average = item.get("vote_average") or 0

    vote_count = item.get("vote_count") or 0

    runtime = item.get("runtime") or 0

    tmdb_id = item.get("id") or ""



    genres = _tmdb_join_names(item.get("genres") or [])

    countries = _tmdb_join_names(item.get("production_countries") or [], key="name", limit=5)



    credits = item.get("credits") or {}

    cast = credits.get("cast") or []

    cast_text = _tmdb_join_names(cast, key="name", limit=8)



    year = ""

    if len(release_date) >= 4 and release_date[:4].isdigit():

        year = release_date[:4]



    label_parts = [title]

    if year:

        label_parts.append(f"[{year}]")



    label = " ".join(label_parts)



    runtime_text = _tmdb_movie_runtime_text(runtime)



    lines = []

    if original and original != title:

        lines.append(_tmdb_detail_line(31864, "Original title", original))

    if genres:

        lines.append(_tmdb_detail_line(31865, "Genre", genres))

    if countries:

        lines.append(_tmdb_detail_line(31866, "Country", countries))

    if runtime_text:

        lines.append(_tmdb_detail_line(31867, "Runtime", runtime_text))

    if release_date:

        lines.append(_tmdb_detail_line(31868, "Premiere", release_date))

    if vote_average:

        try:

            lines.append(_tmdb_rating_line(f"{float(vote_average):.1f}", vote_count))

        except Exception:

            pass

    if cast_text:

        lines.append(_tmdb_detail_line(31870, "Cast", cast_text))

    if overview:

        lines.append("")

        lines.append(overview)



    plot = "\n".join(lines).strip()



    watch_source = "trakt" if str(watch_source or "").strip().lower() == "trakt" else ""
    if watch_source == "trakt":
        status = tmdb_playback_sync.trakt_item_status("movie", tmdb_id)
    else:
        try:
            status = _tmdb_merged_item_status(tmdb_id)
        except Exception:
            status = ""

    li = xbmcgui.ListItem(label=_tmdb_colorize_movie_label(label, tmdb_id, status_override=status if watch_source == "trakt" else None))
    li.setArt(_tmdb_media_art(item))

    info = {

        "title": title,

        "originaltitle": original,

        "plot": plot,

        "rating": float(vote_average) if vote_average else 0.0,

        "votes": str(vote_count),

        "year": int(year) if year.isdigit() else 0,

        "premiered": release_date,

        "duration": int(runtime) * 60 if runtime else 0,

        "mediatype": "movie",

    }

    if watch_source == "trakt":
        _set_video_info(li, _tmdb_apply_explicit_status(li, status, info))
    else:
        _set_video_info(li, _tmdb_apply_watch_state(li, tmdb_id, info))
    li.setProperty("IsPlayable", "false")
    _tmdb_add_movie_watch_context(li, tmdb_id, title, status)


    u = build_url({

        "action": "tmdb.movie.sources.search.stub",

        "id": str(tmdb_id),

        "title": title,

        "original_title": original,

        "year": year,

        "query": title,

    })



    xbmcplugin.addDirectoryItem(

        handle=addon_handle,

        url=u,

        listitem=li,

        isFolder=True

    )



def _add_tv_detail_header(item: dict, watch_source: str = ""):
    title = _tmdb_display_title(item, "tv", _tmdb_untitled()) or _tmdb_untitled()
    original = _tmdb_hide_unreadable_text(item.get("original_name") or "")
    overview = item.get("overview") or ""

    first_air_date = item.get("first_air_date") or ""

    last_air_date = item.get("last_air_date") or ""

    vote_average = item.get("vote_average") or 0

    vote_count = item.get("vote_count") or 0

    number_of_seasons = item.get("number_of_seasons") or 0

    number_of_episodes = item.get("number_of_episodes") or 0

    status = item.get("status") or ""



    genres = _tmdb_join_names(item.get("genres") or [])

    countries = _tmdb_join_names(item.get("origin_country") or [], key=None, limit=10)



    credits = item.get("credits") or {}

    cast = credits.get("cast") or []

    cast_text = _tmdb_join_names(cast, key="name", limit=8)



    year = ""

    if len(first_air_date) >= 4 and first_air_date[:4].isdigit():

        year = first_air_date[:4]



    label_parts = [title]

    if year:

        label_parts.append(f"[{year}]")



    label = " ".join(label_parts)



    lines = []

    if original and original != title:

        lines.append(_tmdb_detail_line(31864, "Original title", original))

    if genres:

        lines.append(_tmdb_detail_line(31865, "Genre", genres))

    if countries:

        lines.append(_tmdb_detail_line(31866, "Country", countries))

    if first_air_date:

        lines.append(_tmdb_detail_line(31871, "First air date", first_air_date))

    if last_air_date:

        lines.append(_tmdb_detail_line(31872, "Last air date", last_air_date))

    if status:

        lines.append(_tmdb_detail_line(31873, "Status", status))

    if number_of_seasons:

        lines.append(_tmdb_detail_line(31874, "Number of seasons", number_of_seasons))

    if number_of_episodes:

        lines.append(_tmdb_detail_line(31875, "Number of episodes", number_of_episodes))

    if vote_average:

        try:

            lines.append(_tmdb_rating_line(f"{float(vote_average):.1f}", vote_count))

        except Exception:

            pass

    if cast_text:

        lines.append(_tmdb_detail_line(31870, "Cast", cast_text))

    if overview:

        lines.append("")

        lines.append(overview)



    plot = "\n".join(lines).strip()

    tmdb_id = item.get("id") or ""
    watch_source = "trakt" if str(watch_source or "").strip().lower() == "trakt" else ""
    if watch_source == "trakt":
        status = tmdb_playback_sync.trakt_item_status("show", tmdb_id)
    else:
        status = ""
        try:
            status = _tmdb_merged_item_status(tmdb_id)
        except Exception:
            status = ""

    li = xbmcgui.ListItem(label=_tmdb_colorize_tv_label(label, tmdb_id, status_override=status if watch_source == "trakt" else None))
    li.setArt(_tmdb_media_art(item))

    info = {

        "title": title,

        "originaltitle": original,

        "tvshowtitle": title,

        "plot": plot,

        "rating": float(vote_average) if vote_average else 0.0,

        "votes": str(vote_count),

        "year": int(year) if year.isdigit() else 0,

        "premiered": first_air_date,

        "mediatype": "tvshow",

    }

    if watch_source == "trakt":
        _set_video_info(li, _tmdb_apply_explicit_status(li, status, info))
    else:
        _set_video_info(li, _tmdb_apply_watch_state(li, tmdb_id, info))
    _tmdb_add_tv_watch_context(li, tmdb_id, title, status)



    if status in ("started", "finished"):

        u = build_url(_tmdb_add_watch_source({
            "action": "tmdb.tv.seasons.open",
            "id": str(tmdb_id),
            "title": title,
            "original_title": original,
        }, watch_source))
        is_folder = False
    else:
        u = build_url(_tmdb_add_watch_source({
            "action": "tmdb.tv.seasons",
            "id": str(tmdb_id),
            "title": title,
            "original_title": original,
        }, watch_source))
        is_folder = True

    xbmcplugin.addDirectoryItem(
        handle=addon_handle,
        url=u,
        listitem=li,
        isFolder=is_folder
    )


def _add_next_page(label: str, action: str, page: int, extra: dict = None):

    query = {"action": action, "page": str(page)}

    if extra:

        query.update(extra)



    li = xbmcgui.ListItem(label=label)

    li.setArt({

        "icon": "DefaultFolder.png",

        "thumb": "DefaultFolder.png",

        "poster": "DefaultFolder.png"

    })



    xbmcplugin.addDirectoryItem(

        handle=addon_handle,

        url=build_url(query),

        listitem=li,

        isFolder=True

    )



def _add_tmdb_page_item(label: str, query: dict = None, icon: str = "DefaultFolder.png", is_folder: bool = True):
    li = xbmcgui.ListItem(label=label)
    li.setArt({
        "icon": icon,
        "thumb": icon,
        "poster": icon

    })

    xbmcplugin.addDirectoryItem(

        handle=addon_handle,

        url=build_url(query) if query else "",

        listitem=li,

        isFolder=is_folder
    )


def _add_tmdb_replace_page_item(label: str, query: dict, icon: str = "DefaultFolder.png"):
    target_url = build_url(query or {})
    li = xbmcgui.ListItem(label=label)
    li.setArt({
        "icon": icon,
        "thumb": icon,
        "poster": icon
    })
    li.setProperty("IsPlayable", "false")
    xbmcplugin.addDirectoryItem(
        handle=addon_handle,
        url=build_url({"action": "tmdb.nav.replace", "target": target_url}),
        listitem=li,
        isFolder=False
    )


def _add_tmdb_replace_with_parent_page_item(label: str, query: dict, parent_query: dict, icon: str = "DefaultFolder.png"):
    target_url = build_url(query or {})
    parent_url = build_url(parent_query or {})
    li = xbmcgui.ListItem(label=label)
    li.setArt({
        "icon": icon,
        "thumb": icon,
        "poster": icon
    })
    li.setProperty("IsPlayable", "false")
    xbmcplugin.addDirectoryItem(
        handle=addon_handle,
        url=build_url({"action": "tmdb.nav.replace.parent", "target": target_url, "parent": parent_url}),
        listitem=li,
        isFolder=False
    )


def _add_tmdb_page_navigation(
    action: str,
    page: int,
    total_pages: int,
    extra: dict = None,

    home_action: str = "",

    home_label: str = "Domu",

    placement: str = "top"

):

    try:

        page = max(1, int(page or 1))

    except Exception:

        page = 1

    try:

        total_pages = max(1, int(total_pages or 1))

    except Exception:

        total_pages = 1



    extra = dict(extra or {})

    placement = (placement or "top").strip().lower()



    if total_pages <= 2:

        if placement == "bottom" and page < total_pages:

            _add_next_page(

                label=i18n.T(31915, "Next page ({page}/{total_pages})").format(page=page + 1, total_pages=total_pages),

                action=action,

                page=page + 1,

                extra=extra

            )

        return



    if placement == "bottom":

        if page < total_pages:

            next_query = {"action": action, "page": str(page + 1)}

            next_query.update(extra)

            _add_tmdb_page_item(
                i18n.T(31830, "Next page ({page}/{total_pages}) >>").format(page=page + 1, total_pages=total_pages),
                next_query,
            )

        return



    if home_action:

        _add_tmdb_page_item(

            label=home_label,

            query={"action": "tmdb.nav.home", "target": home_action},

            icon="DefaultFolderBack.png",

            is_folder=False

        )



    if page > 1:

        first_query = {"action": action, "page": "1"}

        first_query.update(extra)

        _add_tmdb_page_item(i18n.T(31831, "<< First page"), first_query)



        prev_query = {"action": action, "page": str(page - 1)}

        prev_query.update(extra)

        _add_tmdb_page_item(
            i18n.T(31832, "<< Previous page ({page}/{total_pages})").format(page=page - 1, total_pages=total_pages),
            prev_query,
        )

def tmdb_nav_home():
    target = _get_param("target", "").strip()
    allowed = {
        "tmdb.movies",
        "tmdb.series",
        "tmdb.people",

        "tmdb.search",

        "tmdb.trending",

    }

    if target not in allowed:

        target = "tmdb.movies"



    try:

        home_url = build_url({"action": target})

        xbmc.executebuiltin(f'Container.Update("{home_url}",replace)')

    except Exception as e:
        xbmc.log(f"[Ikarus/TMDB] tmdb_nav_home failed: {e}", xbmc.LOGWARNING)


def tmdb_nav_replace():
    target_url = _get_param("target", "").strip()
    if not target_url:
        return

    if not _tmdb_is_safe_plugin_url(target_url):
        return

    try:
        safe_url = target_url.replace('"', "%22")
        xbmc.executebuiltin(f'Container.Update("{safe_url}",replace)')
    except Exception as e:
        xbmc.log(f"[Ikarus/TMDB] tmdb_nav_replace failed: {e}", xbmc.LOGWARNING)


def _tmdb_is_safe_plugin_url(target_url: str) -> bool:
    allowed = False
    try:
        allowed = target_url.startswith(BASE_PATH + "?")
    except Exception:
        allowed = False
    if not allowed:
        try:
            allowed = target_url.startswith("plugin://plugin.video.ikarus")
        except Exception:
            allowed = False
    return bool(allowed)


def tmdb_nav_replace_with_parent():
    parent_url = _get_param("parent", "").strip()
    target_url = _get_param("target", "").strip()
    if not parent_url or not target_url:
        return
    if not _tmdb_is_safe_plugin_url(parent_url) or not _tmdb_is_safe_plugin_url(target_url):
        return

    try:
        safe_parent = parent_url.replace('"', "%22")
        safe_target = target_url.replace('"', "%22")
        xbmc.executebuiltin("CancelAlarm(IkarusTraktFirstPageJump,silent)")
        xbmc.executebuiltin(f'Container.Update("{safe_parent}",replace)')
        xbmc.executebuiltin(f'AlarmClock(IkarusTraktFirstPageJump,Container.Update("{safe_target}"),00:01,silent)')
    except Exception as e:
        xbmc.log(f"[Ikarus/TMDB] tmdb_nav_replace_with_parent failed: {e}", xbmc.LOGWARNING)


def _tmdb_nav_home_for_media(media_type: str):
    if media_type == "movie":

        return "tmdb.movies", i18n.T(30424, "Dom\u016f: Film")

    if media_type == "tv":

        return "tmdb.series", i18n.T(30425, "Dom\u016f: Seri\u00e1ly")

    return "", i18n.T(30423, "Dom\u016f")



_TMDB_MOVIE_GENRE_LABEL_IDS = {
    28: 31200,
    12: 31201,
    16: 31202,
    35: 31203,
    80: 31204,
    99: 31205,
    18: 31206,
    10751: 31207,
    14: 31208,
    36: 31209,
    27: 31210,
    10402: 31211,
    9648: 31212,
    10749: 31213,
    878: 31214,
    10770: 31215,
    53: 31216,
    10752: 31217,
    37: 31218,
}


_TMDB_TV_GENRE_LABEL_IDS = {
    10759: 31219,
    16: 31202,
    35: 31203,
    80: 31204,
    99: 31205,
    18: 31206,
    10751: 31207,
    10762: 31220,
    9648: 31212,
    10763: 31221,
    10764: 31222,
    10765: 31223,
    10766: 31224,
    10767: 31225,
    10768: 31226,
    37: 31218,
}


def _tmdb_genre_display_name(media_type: str, genre: dict) -> str:
    genre = genre or {}
    try:
        genre_id = int(genre.get("id") or 0)
    except Exception:
        genre_id = 0
    fallback = str(genre.get("name") or "").strip()
    label_map = _TMDB_MOVIE_GENRE_LABEL_IDS if media_type == "movie" else _TMDB_TV_GENRE_LABEL_IDS
    label_id = label_map.get(genre_id)
    if label_id:
        return i18n.T(label_id, fallback or str(genre_id))
    return fallback



def _tmdb_movie_genres():

    payload = _tmdb_get("/genre/movie/list", params={

        "language": TMDB_LANGUAGE,

    }, timeout=20)



    genres = payload.get("genres") or []

    out = []



    for g in genres:

        gid = g.get("id")

        name = _tmdb_genre_display_name("movie", g)

        if gid and name:

            out.append({

                "id": int(gid),

                "name": name,

            })



    out.sort(key=lambda x: x["name"].lower())

    return out



def _tmdb_movie_years(start_year: int = 1900):

    current_year = time.localtime().tm_year

    years = []



    for y in range(current_year, start_year - 1, -1):

        years.append(y)



    return years



def _tmdb_origin_countries():

    payload = _tmdb_get("/configuration/countries", params={

        "language": TMDB_LANGUAGE,

    }, timeout=20)



    rows = payload

    if isinstance(payload, dict):

        rows = payload.get("countries") or payload.get("results") or []



    out = []

    seen = set()



    for c in rows or []:

        if not isinstance(c, dict):

            continue



        code = (c.get("iso_3166_1") or c.get("code") or "").strip().upper()

        name = (

            c.get("native_name")

            or c.get("english_name")

            or c.get("name")

            or code

        )

        name = str(name or "").strip()



        if not code or not name or code in seen:

            continue



        seen.add(code)

        out.append({

            "code": code,

            "name": name,

        })



    out.sort(key=lambda x: x["name"].casefold())

    return out



def _tmdb_movie_countries():

    # Zachováme název helperu kvůli minimálním zásahům,

    # ale fakticky už vracíme původní jazyky.

    return [

        {"code": "cs", "name": "čeština"},

        {"code": "sk", "name": "slovenština"},

        {"code": "en", "name": "angličtina"},

        {"code": "fr", "name": "francouzština"},

        {"code": "de", "name": "němčina"},

        {"code": "it", "name": "italština"},

        {"code": "es", "name": "španělština"},

        {"code": "ja", "name": "japonština"},

        {"code": "ko", "name": "korejština"},

        {"code": "hi", "name": "hindština"},

        {"code": "zh", "name": "čínština"},

        {"code": "ru", "name": "ruština"},

        {"code": "pl", "name": "polština"},

        {"code": "sv", "name": "švédština"},

        {"code": "da", "name": "dánština"},

        {"code": "no", "name": "norština"},

        {"code": "fi", "name": "finština"},

        {"code": "nl", "name": "nizozemština"},

        {"code": "tr", "name": "turečtina"},

    ]



def _tmdb_movie_origin_countries():

    return _tmdb_origin_countries()



def _tmdb_movie_watch_providers():

    payload = _tmdb_get("/watch/providers/movie", params={

        "language": TMDB_LANGUAGE,

        "watch_region": TMDB_REGION,

    }, timeout=20)



    results = payload.get("results") or []

    out = []



    for p in results:

        pid = p.get("provider_id")

        name = (p.get("provider_name") or "").strip()

        logo = (p.get("logo_path") or "").strip()



        if not pid or not name:

            continue



        art = "DefaultAddonVideo.png"

        if logo:

            art = TMDB_IMAGE_BASE + logo



        out.append({

            "id": int(pid),

            "name": name,

            "icon": art,

        })



    out.sort(key=lambda x: x["name"].lower())

    return out





def tmdb_movies_popular_menu():
    providers = _tmdb_movie_watch_providers()
    if not providers:
        providers = _tmdb_tv_watch_providers()

    xbmcplugin.setPluginCategory(addon_handle, i18n.T(31316, "TMDB / Movies / Popular"))
    xbmcplugin.setContent(addon_handle, "files")



    _add_dir(

        i18n.T(31111, "All"),

        {

            "action": "tmdb.movies.popular.list",

            "page": "1",

        },

        "DefaultMovies.png"

    )



    if providers:

        for p in providers:

            _add_dir(

                p["name"],

                {

                    "action": "tmdb.movies.popular.provider.list",

                    "provider_id": str(p["id"]),

                    "provider_name": p["name"],

                    "page": "1",

                },

                p.get("icon") or "DefaultAddonVideo.png"

            )



    _end("files")



def tmdb_movies_popular_provider_list():

    page = _get_int_param("page", 1)

    provider_id = _get_param("provider_id", "").strip()

    provider_name = _get_param("provider_name", i18n.T(31319, "Service")).strip() or i18n.T(31319, "Service")



    if not provider_id:

        _placeholder_dir(i18n.T(31316, "TMDB / Movies / Popular"), i18n.T(31317, "(missing provider_id)"))

        return



    _tmdb_movies_list_from_endpoint(

        endpoint="/discover/movie",

        category_title=i18n.T(31318, "TMDB / Movies / Popular / {provider}").format(provider=provider_name),

        page=page,

        extra_params={

            "sort_by": "popularity.desc",

            "include_adult": "false",

            "include_video": "false",

            "watch_region": TMDB_REGION,

            "with_watch_providers": provider_id,

        },

        next_action="tmdb.movies.popular.provider.list",

        persist_params={

            "provider_id": provider_id,

            "provider_name": provider_name,

        }

    )



def _tmdb_tv_watch_providers():

    payload = _tmdb_get("/watch/providers/tv", params={

        "language": TMDB_LANGUAGE,

        "watch_region": TMDB_REGION,

    }, timeout=20)



    results = payload.get("results") or []

    out = []



    for p in results:

        pid = p.get("provider_id")

        name = (p.get("provider_name") or "").strip()

        logo = (p.get("logo_path") or "").strip()



        if not pid or not name:

            continue



        art = "DefaultAddonVideo.png"

        if logo:

            art = TMDB_IMAGE_BASE + logo



        out.append({

            "id": int(pid),

            "name": name,

            "icon": art,

        })



    out.sort(key=lambda x: x["name"].lower())

    return out





def tmdb_series_popular_menu():

    providers = _tmdb_tv_watch_providers()



    xbmcplugin.setPluginCategory(addon_handle, i18n.T(31320, "TMDB / Series / Popular"))

    xbmcplugin.setContent(addon_handle, "files")



    _add_dir(

        i18n.T(31111, "All"),

        {

            "action": "tmdb.series.popular.list",

            "page": "1",

        },

        "DefaultTVShows.png"

    )



    if providers:

        for p in providers:

            _add_dir(

                p["name"],

                {

                    "action": "tmdb.series.popular.provider.list",

                    "provider_id": str(p["id"]),

                    "provider_name": p["name"],

                    "page": "1",

                },

                p.get("icon") or "DefaultAddonVideo.png"

            )



    _end("files")



def tmdb_series_popular_list():

    page = _get_int_param("page", 1)

    _tmdb_tv_list_from_endpoint(

        endpoint="/discover/tv",

        category_title=i18n.T(31321, "TMDB / Series / Popular / All"),

        page=page,

        extra_params={

            "sort_by": "first_air_date.desc",

            "include_adult": "false",

        },

        next_action="tmdb.series.popular.list"

    )



def tmdb_series_popular_provider_list():

    page = _get_int_param("page", 1)

    provider_id = _get_param("provider_id", "").strip()

    provider_name = _get_param("provider_name", i18n.T(31319, "Service")).strip() or i18n.T(31319, "Service")



    if not provider_id:

        _placeholder_dir(i18n.T(31320, "TMDB / Series / Popular"), i18n.T(31317, "(missing provider_id)"))

        return



    _tmdb_tv_list_from_endpoint(

        endpoint="/discover/tv",

        category_title=i18n.T(31322, "TMDB / Series / Popular / {provider}").format(provider=provider_name),

        page=page,

        extra_params={

            "sort_by": "first_air_date.desc",

            "include_adult": "false",

            "watch_region": TMDB_REGION,

            "with_watch_providers": provider_id,

        },

        next_action="tmdb.series.popular.provider.list",

        persist_params={

            "provider_id": provider_id,

            "provider_name": provider_name,

        }

    )



def _tmdb_tv_genres():

    payload = _tmdb_get("/genre/tv/list", params={

        "language": TMDB_LANGUAGE,

    }, timeout=20)



    genres = payload.get("genres") or []

    out = []



    for g in genres:

        gid = g.get("id")

        name = _tmdb_genre_display_name("tv", g)

        if gid and name:

            out.append({

                "id": int(gid),

                "name": name,

            })



    out.sort(key=lambda x: x["name"].lower())

    return out





def _tmdb_tv_years(start_year: int = 1900):

    current_year = time.localtime().tm_year

    years = []



    for y in range(current_year, start_year - 1, -1):

        years.append(y)



    return years





def _tmdb_tv_countries():

    # Zachováme název helperu kvůli minimálním zásahům,

    # ale fakticky už vracíme původní jazyky.

    return [

        {"code": "cs", "name": "čeština"},

        {"code": "sk", "name": "slovenština"},

        {"code": "en", "name": "angličtina"},

        {"code": "fr", "name": "francouzština"},

        {"code": "de", "name": "němčina"},

        {"code": "it", "name": "italština"},

        {"code": "es", "name": "španělština"},

        {"code": "ja", "name": "japonština"},

        {"code": "ko", "name": "korejština"},

        {"code": "hi", "name": "hindština"},

        {"code": "zh", "name": "čínština"},

        {"code": "ru", "name": "ruština"},

        {"code": "pl", "name": "polština"},

        {"code": "sv", "name": "švédština"},

        {"code": "da", "name": "dánština"},

        {"code": "no", "name": "norština"},

        {"code": "fi", "name": "finština"},

        {"code": "nl", "name": "nizozemština"},

        {"code": "tr", "name": "turečtina"},

    ]



def _tmdb_tv_origin_countries():

    return _tmdb_origin_countries()



def _tmdb_tv_info(item: dict) -> dict:
    title = _tmdb_display_title(item, "tv")
    original = _tmdb_hide_unreadable_text(item.get("original_name") or "")
    plot = item.get("overview") or ""

    rating = item.get("vote_average") or 0

    votes = item.get("vote_count") or 0

    premiered = item.get("first_air_date") or ""



    year = 0

    if len(premiered) >= 4 and premiered[:4].isdigit():

        year = int(premiered[:4])



    return {

        "title": title,

        "originaltitle": original,

        "tvshowtitle": title,

        "plot": plot,

        "rating": float(rating) if rating else 0.0,

        "votes": str(votes),

        "year": year,

        "premiered": premiered,

        "mediatype": "tvshow",

    }



def _tmdb_person_art(item: dict) -> dict:

    profile = (item.get("profile_path") or "").strip()



    art = {

        "icon": "DefaultActor.png",

        "thumb": "DefaultActor.png",

        "poster": "DefaultActor.png",

    }



    if profile:

        full = TMDB_IMAGE_BASE + profile

        art["icon"] = full

        art["thumb"] = full

        art["poster"] = full



    return art



def _tmdb_person_label(item: dict, prefix: str = "") -> str:

    name = (item.get("name") or i18n.T(31845, "Unknown name")).strip()



    label = name



    if prefix:

        label = f"[{prefix}] {label}"



    return label



def _tmdb_person_info(item: dict) -> dict:

    name = item.get("name") or ""

    known_for_department = item.get("known_for_department") or ""

    popularity = item.get("popularity") or 0



    known_for_titles = []

    for k in (item.get("known_for") or [])[:5]:

        t = (k.get("title") or k.get("name") or "").strip()

        if t:

            known_for_titles.append(t)



    plot_lines = []

    if known_for_department:

        plot_lines.append(f"Profese: {known_for_department}")

    if known_for_titles:

        plot_lines.append(_tmdb_detail_line(31917, "Known for", ", ".join(known_for_titles)))

    if popularity:

        try:

            plot_lines.append(f"Popularita: {float(popularity):.1f}")

        except Exception:

            pass



    return {

        "title": name,

        "plot": "\n".join(plot_lines).strip(),

        "mediatype": "video",

    }





def _tmdb_person_detail(person_id: str) -> dict:

    if not person_id:

        return {}



    return _tmdb_get(f"/person/{person_id}", params={

        "language": TMDB_LANGUAGE,

        "append_to_response": "combined_credits,images",

    }, timeout=20)





def _add_person_result(item: dict, prefix: str = ""):

    person_id = item.get("id")

    name = item.get("name") or i18n.T(31845, "Unknown name")



    li = xbmcgui.ListItem(label=_tmdb_person_label(item, prefix=prefix))

    li.setArt(_tmdb_person_art(item))

    _set_video_info(li, _tmdb_person_info(item))



    u = build_url({

        "action": "tmdb.person.detail",

        "id": str(person_id or ""),

        "name": name,

    })



    xbmcplugin.addDirectoryItem(

        handle=addon_handle,

        url=u,

        listitem=li,

        isFolder=True

    )



def _tmdb_label_tv(item: dict, prefix: str = "") -> str:
    title = (_tmdb_display_title(item, "tv", _tmdb_untitled()) or _tmdb_untitled()).strip()
    premiered = (item.get("first_air_date") or "").strip()

    rating = item.get("vote_average") or 0



    parts = []

    if len(premiered) >= 4 and premiered[:4].isdigit():

        parts.append(premiered[:4])



    try:

        if float(rating) > 0:

            parts.append("TMDB %.1f" % float(rating))

    except Exception:

        pass



    label = title

    if parts:

        label = f"{title}  [{' | '.join(parts)}]"



    if prefix:

        label = f"[{prefix}] {label}"



    return label





def _add_tv_result(item: dict, prefix: str = "", extra_context_items=None,
                   watch_source: str = "", status_override=None, return_item: bool = False,
                   compact_info: bool = False):
    tmdb_id = item.get("id")
    watch_source = "trakt" if str(watch_source or "").strip().lower() == "trakt" else ""
    if watch_source == "trakt":
        if status_override is not None:
            status = (status_override or "").strip().lower()
        else:
            status = (tmdb_playback_sync.trakt_item_status("show", tmdb_id) or "").strip().lower()
    else:
        if status_override is not None:
            status = (status_override or "").strip().lower()
        else:
            status = ""
            try:
                status = _tmdb_merged_item_status(tmdb_id)
            except Exception:
                status = ""
    title = _tmdb_display_title(item, "tv", _tmdb_untitled()) or _tmdb_untitled()


    li = xbmcgui.ListItem(label=_tmdb_colorize_tv_label(
        _tmdb_label_tv(item, prefix=prefix),
        tmdb_id,
        status_override=status if (watch_source == "trakt" or status_override is not None) else None,
    ))
    li.setArt(_tmdb_media_art(item))
    info = _tmdb_tv_info(item)
    if watch_source == "trakt":
        info = _tmdb_apply_explicit_status(li, status, info)
    elif status_override is not None:
        info = _tmdb_apply_explicit_status(li, status, info)
    else:
        info = _tmdb_apply_watch_state(li, tmdb_id, info)
    if compact_info:
        _set_video_info_compact(li, info)
    else:
        _set_video_info(li, info)
    li.setProperty("IsPlayable", "false")
    _tmdb_add_tv_watch_context(li, tmdb_id, title, status)
    try:
        extra_items = list(extra_context_items or [])
        if extra_items:
            li.addContextMenuItems(extra_items, replaceItems=False)
    except Exception:
        pass

    if status == "started":
        u = build_url({
            "action": "tmdb.tv.detail.open",
            "id": str(tmdb_id or ""),
            "title": title,
            "original_title": item.get("original_name") or "",
            "watch_source": watch_source,
        })
        is_folder = False

    else:

        u = build_url(_tmdb_add_watch_source({
            "action": "tmdb.tv.detail",
            "id": str(tmdb_id or ""),
            "title": title
        }, watch_source))
        is_folder = True
    directory_item = (u, li, is_folder)
    if return_item:
        return directory_item
    xbmcplugin.addDirectoryItem(
        handle=addon_handle,
        url=directory_item[0],
        listitem=directory_item[1],
        isFolder=directory_item[2]
    )
    return True


def _tmdb_tv_list_from_endpoint(

    endpoint: str,

    category_title: str,

    page: int = 1,

    extra_params: dict = None,

    next_action: str = None,

    persist_params: dict = None,

    sort_mode: str = "",

    filter_future_years: bool = True

):

    if page < 1:
        page = 1

    action = next_action or _get_param("action", "")
    params = {
        "language": TMDB_LANGUAGE,
        "page": page,
    }
    if extra_params:
        params.update(extra_params)

    with diagnostics.action_scope("tmdb.phase", f"{action or 'tmdb.tv.list'}.fetch", {"page": page}):
        if filter_future_years:
            payload, results, page, total_pages = _tmdb_find_first_non_future_page(
                endpoint=endpoint,
                params=params,
                date_key="first_air_date",
                start_page=page,
                max_lookahead=15
            )
        else:
            payload = _tmdb_get(endpoint, params=params, timeout=TMDB_MENU_TIMEOUT)
            results = payload.get("results") or []
            total_pages = int(payload.get("total_pages") or 1)


    if sort_mode == "newest":

        results = _tmdb_sort_tv_by_newest(results)

    page_limit = _get_page_limit(25)



    xbmcplugin.setPluginCategory(addon_handle, i18n.T(31601, "{title} / Page {page}").format(title=category_title, page=page))

    xbmcplugin.setContent(addon_handle, "tvshows")



    if not results:

        _placeholder_dir(category_title, i18n.T(31159, "(no TMDB data)"))

        return



    query = _get_param("query", "")
    genre_id = _get_param("genre_id", "")

    genre_name = _get_param("genre_name", "")

    year = _get_param("year", "")

    country_code = _get_param("country_code", "")

    country_name = _get_param("country_name", "")



    extra = {}

    if query:

        extra["query"] = query

    if genre_id:

        extra["genre_id"] = genre_id

    if genre_name:

        extra["genre_name"] = genre_name

    if year:

        extra["year"] = year

    if country_code:

        extra["country_code"] = country_code

    if country_name:

        extra["country_name"] = country_name



    if persist_params:
        extra.update(persist_params)

    home_action, home_label = _tmdb_nav_home_for_media("tv")
    with diagnostics.action_scope("tmdb.phase", f"{action or 'tmdb.tv.list'}.nav", {"page": page, "pages": total_pages}):
        _add_tmdb_page_navigation(action, page, total_pages, extra, home_action, home_label)

    page_items = list(results[:page_limit])
    with diagnostics.action_scope("tmdb.phase", f"{action or 'tmdb.tv.list'}.status", {"page": page, "count": len(page_items)}):
        status_map = _tmdb_status_map_for_items(page_items)

    directory_items = []
    with diagnostics.action_scope("tmdb.phase", f"{action or 'tmdb.tv.list'}.rows.build", {"page": page, "count": len(page_items)}):
        for item in page_items:
            tmdb_id = str((item or {}).get("id") or "").strip()
            directory_item = _add_tv_result(
                item,
                status_override=status_map.get(tmdb_id, ""),
                return_item=True,
                compact_info=True,
            )
            if directory_item:
                directory_items.append(directory_item)

    shown = len(directory_items)
    if shown:
        with diagnostics.action_scope("tmdb.phase", f"{action or 'tmdb.tv.list'}.rows.add", {"page": page, "count": shown}):
            try:
                xbmcplugin.addDirectoryItems(addon_handle, directory_items, shown)
            except Exception:
                for url, li, is_folder in directory_items:
                    xbmcplugin.addDirectoryItem(
                        handle=addon_handle,
                        url=url,
                        listitem=li,
                        isFolder=is_folder
                    )

    with diagnostics.action_scope("tmdb.phase", f"{action or 'tmdb.tv.list'}.next_nav", {"page": page, "pages": total_pages}):
        _add_tmdb_page_navigation(action, page, total_pages, extra, home_action, home_label, placement="bottom")

    _end("tvshows")


def _tmdb_people_list_from_endpoint(

    endpoint: str,

    category_title: str,

    page: int = 1,

    extra_params: dict = None,

    next_action: str = None,

    persist_params: dict = None

):

    if page < 1:

        page = 1



    params = {

        "language": TMDB_LANGUAGE,

        "page": page,

    }

    if extra_params:

        params.update(extra_params)



    payload = _tmdb_get(endpoint, params=params, timeout=TMDB_MENU_TIMEOUT)


    results = payload.get("results") or []

    total_pages = int(payload.get("total_pages") or 1)

    page_limit = _get_page_limit(25)



    xbmcplugin.setPluginCategory(addon_handle, i18n.T(31601, "{title} / Page {page}").format(title=category_title, page=page))

    xbmcplugin.setContent(addon_handle, "files")



    if not results:

        _placeholder_dir(category_title, i18n.T(31159, "(no TMDB data)"))

        return



    shown = 0

    for item in results:

        if shown >= page_limit:

            break

        _add_person_result(item)

        shown += 1



    if page < total_pages:

        action = next_action or _get_param("action", "")

        query = _get_param("query", "")



        extra = {}

        if query:

            extra["query"] = query



        if persist_params:

            extra.update(persist_params)



        _add_next_page(

            label=i18n.T(31915, "Next page ({page}/{total_pages})").format(page=page + 1, total_pages=total_pages),

            action=action,

            page=page + 1,

            extra=extra

        )



    _end("files")



def _tmdb_multi_stacked_search_list(

    category_title: str,

    query: str,

    page: int = 1,

    filter_future_years: bool = False

):

    query = (query or "").strip()

    if not query:

        _placeholder_dir(i18n.T(31115, "TMDB / Search / All"), i18n.T(31117, "(missing search query)"))

        return



    if page < 1:

        page = 1



    page_limit = _get_page_limit(25)

    start_idx = (page - 1) * page_limit

    end_idx = start_idx + page_limit



    # 1) nejdřív si zkusíme vystačit jen s osobami

    people_pack = _tmdb_collect_search_results(

        path="/search/person",

        query=query,

        media_type="person",

        needed_count=end_idx + 1,

        sort_mode=""

    )

    people_results = people_pack.get("results") or []

    people_count = len(people_results)



    # 2) pokud osoby nestačí pokrýt konec naší stránky, dotáhneme seriály

    tv_needed = max(0, (end_idx + 1) - people_count)

    tv_pack = _tmdb_collect_search_results(

        path="/search/tv",

        query=query,

        media_type="tv",

        needed_count=tv_needed,

        sort_mode="newest"

    )

    tv_results = tv_pack.get("results") or []

    tv_count = len(tv_results)



    # 3) pokud nestačí osoby + seriály, dotáhneme filmy

    movie_needed = max(0, (end_idx + 1) - people_count - tv_count)

    movie_pack = _tmdb_collect_search_results(

        path="/search/movie",

        query=query,

        media_type="movie",

        needed_count=movie_needed,

        sort_mode="newest"

    )

    movie_results = movie_pack.get("results") or []



    if filter_future_years:

        tv_results = _filter_out_future_items(tv_results, "first_air_date", enabled=True)

        movie_results = _filter_out_future_items(movie_results, "release_date", enabled=True)



    combined = people_results + tv_results + movie_results

    page_items = combined[start_idx:end_idx]



    xbmcplugin.setPluginCategory(addon_handle, i18n.T(31601, "{title} / Page {page}").format(title=category_title, page=page))

    xbmcplugin.setContent(addon_handle, "files")



    if not page_items:

        _placeholder_dir(category_title, i18n.T(31159, "(no TMDB data)"))

        return



    shown = 0

    for item in page_items:

        media_type = (item.get("media_type") or "").strip()



        if media_type == "person":

            _add_person_result(item, prefix=i18n.T(31938))

            shown += 1

            continue



        if media_type == "tv":

            _add_tv_result(item, prefix=i18n.T(31940))

            shown += 1

            continue



        if media_type == "movie":

            _add_movie_result(item, prefix=i18n.T(31939))

            shown += 1

            continue



    if shown == 0:

        _placeholder_dir(category_title, i18n.T(31600, "(no supported results)"))

        return



    has_more_people = bool(people_pack.get("has_more"))

    has_more_tv = bool(tv_pack.get("has_more"))

    has_more_movies = bool(movie_pack.get("has_more"))

    has_more_loaded = len(combined) > end_idx



    if has_more_people or has_more_tv or has_more_movies or has_more_loaded:

        _add_next_page(

            label=i18n.T(31916, "Next page ({page})").format(page=page + 1),

            action="tmdb.search.multi.results",

            page=page + 1,

            extra={

                "query": query,

            }

        )



    _end("files")



def _tmdb_single_stacked_search_list(

    category_title: str,

    query: str,

    media_type: str,

    page: int = 1,

    filter_future_years: bool = False

):

    query = (query or "").strip()

    if not query:

        _placeholder_dir(category_title, i18n.T(31117, "(missing search query)"))

        return



    if page < 1:

        page = 1



    page_limit = _get_page_limit(25)

    start_idx = (page - 1) * page_limit

    end_idx = start_idx + page_limit



    if media_type == "movie":

        path = "/search/movie"

        content_type = "movies"

        next_action = "tmdb.movies.search.results"

    elif media_type == "tv":

        path = "/search/tv"

        content_type = "tvshows"

        next_action = "tmdb.series.search.results"

    else:

        _placeholder_dir(category_title, i18n.T(31600, "(no supported results)"))

        return



    pack = _tmdb_collect_all_search_results(

        path=path,

        query=query,

        media_type=media_type,

        sort_mode="newest"

    )



    all_results = pack.get("results") or []



    if media_type == "movie":

        all_results = _filter_out_future_items(all_results, "release_date", enabled=filter_future_years)

    elif media_type == "tv":

        all_results = _filter_out_future_items(all_results, "first_air_date", enabled=filter_future_years)



    page_items = all_results[start_idx:end_idx]



    xbmcplugin.setPluginCategory(addon_handle, i18n.T(31601, "{title} / Page {page}").format(title=category_title, page=page))

    xbmcplugin.setContent(addon_handle, content_type)



    if not page_items:

        _placeholder_dir(category_title, i18n.T(31159, "(no TMDB data)"))

        return



    total_pages = max(1, (len(all_results) + page_limit - 1) // page_limit)

    home_action, home_label = _tmdb_nav_home_for_media(media_type)

    _add_tmdb_page_navigation(

        next_action,

        page,

        total_pages,

        {"query": query},

        home_action,

        home_label

    )



    shown = 0

    for item in page_items:

        if media_type == "movie":

            _add_movie_result(item)

            shown += 1

            continue



        if media_type == "tv":

            _add_tv_result(item)

            shown += 1

            continue



    if shown == 0:

        _placeholder_dir(category_title, i18n.T(31600, "(no supported results)"))

        return



    _add_tmdb_page_navigation(

        next_action,

        page,

        total_pages,

        {"query": query},

        home_action,

        home_label,

        placement="bottom"

    )





    _end(content_type)



# ---------------------------

# TMDB API SECTIONS

# ---------------------------



def _tmdb_movies_list_from_endpoint(

    endpoint: str,

    category_title: str,

    page: int = 1,

    extra_params: dict = None,

    next_action: str = None,

    persist_params: dict = None,

    sort_mode: str = "",

    filter_future_years: bool = True

):

    if page < 1:
        page = 1

    action = next_action or _get_param("action", "")
    params = {
        "language": TMDB_LANGUAGE,
        "region": TMDB_REGION,
        "page": page,
    }

    if extra_params:

        params.update(extra_params)



    with diagnostics.action_scope("tmdb.phase", f"{action or 'tmdb.movies.list'}.fetch", {"page": page}):
        if filter_future_years:
            payload, results, page, total_pages = _tmdb_find_first_non_future_page(
                endpoint=endpoint,
                params=params,
                date_key="release_date",
                start_page=page,
                max_lookahead=15
            )
        else:
            payload = _tmdb_get(endpoint, params=params, timeout=TMDB_MENU_TIMEOUT)
            results = payload.get("results") or []
            total_pages = int(payload.get("total_pages") or 1)


    if sort_mode == "newest":

        results = _tmdb_sort_movies_by_newest(results)

    page_limit = _get_page_limit(25)



    xbmcplugin.setPluginCategory(addon_handle, i18n.T(31601, "{title} / Page {page}").format(title=category_title, page=page))

    xbmcplugin.setContent(addon_handle, "movies")



    if not results:

        _placeholder_dir(category_title, i18n.T(31159, "(no TMDB data)"))

        return



    section = _get_param("section", "")
    genre_id = _get_param("genre_id", "")

    genre_name = _get_param("genre_name", "")

    year = _get_param("year", "")

    country_code = _get_param("country_code", "")

    country_name = _get_param("country_name", "")

    query = _get_param("query", "")



    extra = {}

    if section:

        extra["section"] = section

    if genre_id:

        extra["genre_id"] = genre_id

    if genre_name:

        extra["genre_name"] = genre_name

    if year:

        extra["year"] = year

    if country_code:

        extra["country_code"] = country_code

    if country_name:

        extra["country_name"] = country_name

    if query:

        extra["query"] = query



    if persist_params:
        extra.update(persist_params)

    home_action, home_label = _tmdb_nav_home_for_media("movie")
    with diagnostics.action_scope("tmdb.phase", f"{action or 'tmdb.movies.list'}.nav", {"page": page, "pages": total_pages}):
        _add_tmdb_page_navigation(action, page, total_pages, extra, home_action, home_label)

    page_items = list(results[:page_limit])
    with diagnostics.action_scope("tmdb.phase", f"{action or 'tmdb.movies.list'}.status", {"page": page, "count": len(page_items)}):
        status_map = _tmdb_status_map_for_items(page_items)

    directory_items = []
    with diagnostics.action_scope("tmdb.phase", f"{action or 'tmdb.movies.list'}.rows.build", {"page": page, "count": len(page_items)}):
        for item in page_items:
            tmdb_id = str((item or {}).get("id") or "").strip()
            directory_item = _add_movie_result(
                item,
                status_override=status_map.get(tmdb_id, ""),
                return_item=True,
                compact_info=True,
            )
            if directory_item:
                directory_items.append(directory_item)

    shown = len(directory_items)
    if shown:
        with diagnostics.action_scope("tmdb.phase", f"{action or 'tmdb.movies.list'}.rows.add", {"page": page, "count": shown}):
            try:
                xbmcplugin.addDirectoryItems(addon_handle, directory_items, shown)
            except Exception:
                for url, li, is_folder in directory_items:
                    xbmcplugin.addDirectoryItem(
                        handle=addon_handle,
                        url=url,
                        listitem=li,
                        isFolder=is_folder
                    )

    with diagnostics.action_scope("tmdb.phase", f"{action or 'tmdb.movies.list'}.next_nav", {"page": page, "pages": total_pages}):
        _add_tmdb_page_navigation(action, page, total_pages, extra, home_action, home_label, placement="bottom")

    _end("movies")




def tmdb_movies_popular_list():

    page = _get_int_param("page", 1)

    _tmdb_movies_list_from_endpoint(

        endpoint="/discover/movie",

        category_title=i18n.T(31128, "TMDB / Movies / Popular / All"),

        page=page,

        extra_params={
            "sort_by": "popularity.desc",
            "include_adult": "false",
            "include_video": "false",
        },
        next_action="tmdb.movies.popular.list",
        filter_future_years=False
    )




def tmdb_movies_top_rated_list():

    page = _get_int_param("page", 1)

    _tmdb_movies_list_from_endpoint(

        endpoint="/movie/top_rated",

        category_title=i18n.T(31129, "TMDB / Movies / Top rated"),

        page=page

    )





def tmdb_movies_now_playing_list():

    page = _get_int_param("page", 1)

    _tmdb_movies_list_from_endpoint(

        endpoint="/movie/now_playing",

        category_title=i18n.T(31126, "TMDB / Movies / News / Now playing"),

        page=page,

        filter_future_years=False

    )





def tmdb_movies_upcoming_list():

    page = _get_int_param("page", 1)

    _tmdb_movies_list_from_endpoint(

        endpoint="/movie/upcoming",

        category_title=i18n.T(31127, "TMDB / Movies / News / Upcoming"),

        page=page,

        filter_future_years=False

    )





def tmdb_movies_trending_day_list():

    page = _get_int_param("page", 1)

    _tmdb_movies_list_from_endpoint(

        endpoint="/trending/movie/day",

        category_title=i18n.T(31130, "TMDB / Movies / News / Trending today"),

        page=page,

        extra_params={

            "language": TMDB_LANGUAGE,

        },

        filter_future_years=False

    )





def tmdb_movies_trending_week_list():

    page = _get_int_param("page", 1)

    _tmdb_movies_list_from_endpoint(

        endpoint="/trending/movie/week",

        category_title=i18n.T(31131, "TMDB / Movies / News / Trending this week"),

        page=page,

        extra_params={

            "language": TMDB_LANGUAGE,

        },

        filter_future_years=False

    )





def tmdb_movies_genre_menu():

    genres = _tmdb_movie_genres()



    xbmcplugin.setPluginCategory(addon_handle, i18n.T(31132, "TMDB / Movies / By genre"))

    xbmcplugin.setContent(addon_handle, "files")



    if not genres:

        _placeholder_dir(i18n.T(31132, "TMDB / Movies / By genre"), i18n.T(31160, "(no genre data from TMDB)"))

        return



    for g in genres:

        _add_dir(

            g["name"],

            {

                "action": "tmdb.movies.genre.list",

                "genre_id": str(g["id"]),

                "genre_name": g["name"],

                "page": "1",

            },

            "DefaultGenre.png"

        )



    _end("files")





def tmdb_movies_by_genre_list():

    page = _get_int_param("page", 1)

    genre_id = _get_param("genre_id", "")

    genre_name = _get_param("genre_name", i18n.T(31941))



    if not genre_id:

        _placeholder_dir(i18n.T(31132, "TMDB / Movies / By genre"), i18n.T(31164, "(missing genre_id)"))

        return



    _tmdb_movies_list_from_endpoint(

        endpoint="/discover/movie",

        category_title=i18n.T(31133, "TMDB / Movies / By genre / {genre}").format(genre=genre_name),

        page=page,

        extra_params={

            "with_genres": genre_id,

            "sort_by": "popularity.desc",

            "include_adult": "false",

            "include_video": "false",

        }

    )





def tmdb_movies_year_menu():

    years = _tmdb_movie_years(1900)



    xbmcplugin.setPluginCategory(addon_handle, i18n.T(31134, "TMDB / Movies / By year"))

    xbmcplugin.setContent(addon_handle, "files")



    if not years:

        _placeholder_dir(i18n.T(31134, "TMDB / Movies / By year"), i18n.T(31161, "(no year data)"))

        return



    for year in years:

        _add_dir(

            str(year),

            {

                "action": "tmdb.movies.year.list",

                "year": str(year),

                "page": "1",

            },

            "DefaultYear.png"

        )



    _end("files")





def tmdb_movies_by_year_list():

    page = _get_int_param("page", 1)

    year = _get_param("year", "")



    if not year:

        _placeholder_dir(i18n.T(31134, "TMDB / Movies / By year"), i18n.T(31165, "(missing year)"))

        return



    _tmdb_movies_list_from_endpoint(

        endpoint="/discover/movie",

        category_title=i18n.T(31135, "TMDB / Movies / By year / {year}").format(year=year),

        page=page,

        extra_params={

            "primary_release_year": year,

            "sort_by": "popularity.desc",

            "include_adult": "false",

            "include_video": "false",

        }

    )





def tmdb_movies_country_menu():

    countries = _tmdb_movie_countries()



    xbmcplugin.setPluginCategory(addon_handle, i18n.T(31136, "TMDB / Movies / By original language"))

    xbmcplugin.setContent(addon_handle, "files")



    if not countries:

        _placeholder_dir(i18n.T(31136, "TMDB / Movies / By original language"), i18n.T(31162, "(no language data)"))

        return



    for c in countries:

        _add_dir(

            c["name"],

            {

                "action": "tmdb.movies.country.list",

                "country_code": c["code"],

                "country_name": c["name"],

                "page": "1",

            },

            "DefaultCountry.png"

        )



    _end("files")





def tmdb_movies_by_country_list():

    page = _get_int_param("page", 1)

    country_code = _get_param("country_code", "")

    country_name = _get_param("country_name", i18n.T(31993))



    if not country_code:

        _placeholder_dir(i18n.T(31136, "TMDB / Movies / By original language"), i18n.T(31166, "(missing country_code)"))

        return



    _tmdb_movies_list_from_endpoint(

        endpoint="/discover/movie",

        category_title=i18n.T(31137, "TMDB / Movies / By original language / {country}").format(country=country_name),

        page=page,

        extra_params={

            "with_original_language": country_code,

            "sort_by": "primary_release_date.desc",

            "include_adult": "false",

            "include_video": "false",

        }

    )





def tmdb_movies_origin_country_menu():

    countries = _tmdb_movie_origin_countries()



    xbmcplugin.setPluginCategory(addon_handle, i18n.T(31138, "TMDB / Movies / By country of origin"))

    xbmcplugin.setContent(addon_handle, "files")



    if not countries:

        _placeholder_dir(i18n.T(31138, "TMDB / Movies / By country of origin"), i18n.T(31163, "(no country data)"))

        return



    for c in countries:

        _add_dir(

            c["name"],

            {

                "action": "tmdb.movies.origin_country.list",

                "country_code": c["code"],

                "country_name": c["name"],

                "page": "1",

            },

            "DefaultCountry.png"

        )



    _end("files")





def tmdb_movies_by_origin_country_list():

    page = _get_int_param("page", 1)

    country_code = _get_param("country_code", "")

    country_name = _get_param("country_name", i18n.T(31942))



    if not country_code:

        _placeholder_dir(i18n.T(31138, "TMDB / Movies / By country of origin"), i18n.T(31166, "(missing country_code)"))

        return



    _tmdb_movies_list_from_endpoint(

        endpoint="/discover/movie",

        category_title=i18n.T(31139, "TMDB / Movies / By country of origin / {country}").format(country=country_name),

        page=page,

        extra_params={

            "with_origin_country": country_code,

            "sort_by": "primary_release_date.desc",

            "include_adult": "false",

            "include_video": "false",

        }

    )





def tmdb_movies_search_input():

    search_title = i18n.T(31112, "TMDB / Movies / Search")
    q = xbmcgui.Dialog().input(search_title, type=xbmcgui.INPUT_ALPHANUM)

    if not q or not q.strip():

        _placeholder_dir(search_title, i18n.T(31116, "(no search query entered)"))

        return



    q = q.strip()

    _tmdb_movie_history_add(q)



    _tmdb_single_stacked_search_list(

        category_title=i18n.T(31122, "TMDB / Movies / Search / {query}").format(query=q),

        query=q,

        media_type="movie",

        page=1

    )





def tmdb_movies_search_menu():

    xbmcplugin.setPluginCategory(addon_handle, i18n.T(31112, "TMDB / Movies / Search"))

    xbmcplugin.setContent(addon_handle, "files")



    _add_dir(i18n.T(30418, "Nov\u00e9 hled\u00e1n\u00ed"), {"action": "tmdb.movies.search.input"}, "DefaultAddSource.png")

    _add_dir(i18n.T(30419, "Historie hled\u00e1n\u00ed"), {"action": "tmdb.movies.search.history"}, "DefaultAddonsSearch.png")



    _end("files")





def tmdb_movies_search_history_menu():

    xbmcplugin.setPluginCategory(addon_handle, i18n.T(31118, "TMDB / Movies / Search history"))

    xbmcplugin.setContent(addon_handle, "files")



    _add_dir(i18n.T(30420, "[Vymazat historii]"), {"action": "tmdb.movies.search.history.clear"}, "DefaultAddonContextItem.png")



    history = _tmdb_movie_history_load()

    if not history:

        li = xbmcgui.ListItem(label=i18n.T(30421, "Historie hled\u00e1n\u00ed je pr\u00e1zdn\u00e1"))

        li.setArt({

            "icon": "DefaultAddonsSearch.png",

            "thumb": "DefaultAddonsSearch.png",

            "poster": "DefaultAddonsSearch.png",

        })

        xbmcplugin.addDirectoryItem(handle=addon_handle, url="", listitem=li, isFolder=False)

        _end("files")

        return



    for q in history:

        _add_dir(

            q,

            {"action": "tmdb.movies.search.results", "query": q, "page": 1},

            "DefaultAddonsSearch.png"

        )



    _end("files")





def tmdb_movies_search_history_clear():

    _tmdb_movie_history_save([])

    xbmcgui.Dialog().notification(

        i18n.T(31100, "TMDB / Movies"),

        i18n.T(30422, "Historie hled\u00e1n\u00ed byla vymaz\u00e1na."),

        xbmcgui.NOTIFICATION_INFO,

        2200

    )

    tmdb_movies_search_history_menu()





def tmdb_movies_search_results():

    page = _get_int_param("page", 1)

    q = _get_param("query", "").strip()



    if not q:

        _placeholder_dir(i18n.T(31112, "TMDB / Movies / Search"), i18n.T(31117, "(missing search query)"))

        return



    _tmdb_movie_history_add(q)

    _tmdb_single_stacked_search_list(

        category_title=i18n.T(31122, "TMDB / Movies / Search / {query}").format(query=q),

        query=q,

        media_type="movie",

        page=page

    )






def tmdb_movies_news_menu():

    xbmcplugin.setPluginCategory(addon_handle, i18n.T(31101, "TMDB / Movies / News"))

    xbmcplugin.setContent(addon_handle, "files")



    _add_dir(i18n.T(31102, "Now playing"), {"action": "tmdb.stub", "section": "movies_now_playing", "page": "1"}, "DefaultRecentlyAddedMovies.png")

    _add_dir(i18n.T(31103, "Upcoming"), {"action": "tmdb.stub", "section": "movies_upcoming", "page": "1"}, "DefaultRecentlyAddedMovies.png")

    _add_dir(i18n.T(30412, "Filmy / Dnes"), {"action": "tmdb.trending.movies.day", "page": "1"}, "DefaultRecentlyAddedMovies.png")

    _add_dir(i18n.T(30413, "Filmy / Tento týden"), {"action": "tmdb.trending.movies.week", "page": "1"}, "DefaultRecentlyAddedMovies.png")



    _end("files")








def tmdb_series_menu():

    xbmcplugin.setPluginCategory(addon_handle, i18n.T(31104, "TMDB / Series"))

    xbmcplugin.setContent(addon_handle, "files")



    _add_dir(i18n.T(30409, "Dnes v TV"), {"action": "tmdb.series.airing_today", "page": "1"}, "DefaultTVShows.png")

    _add_dir(i18n.T(30401, "Populární"), {"action": "tmdb.series.popular"}, "DefaultTVShows.png")

    _add_dir(i18n.T(30402, "Nejlépe hodnocené"), {"action": "tmdb.series.top_rated", "page": "1"}, "DefaultSets.png")

    _add_dir(i18n.T(30403, "Podle žánru"), {"action": "tmdb.series.genre"}, "DefaultGenre.png")

    _add_dir(i18n.T(30404, "Podle roku"), {"action": "tmdb.series.year"}, "DefaultYear.png")

    _add_dir(i18n.T(30405, "Podle země původu"), {"action": "tmdb.series.origin_country"}, "DefaultCountry.png")

    _add_dir(i18n.T(30406, "Podle původního jazyka"), {"action": "tmdb.series.country"}, "DefaultCountry.png")

    _add_dir(i18n.T(30410, "Seriály - zhlédnuté"), {"action": "tmdb.series.finished"}, "DefaultInProgressShows.png")

    _add_dir(i18n.T(30411, "Seriály - rozkoukané"), {"action": "tmdb.series.started"}, "DefaultInProgressShows.png")

    _add_dir(i18n.T(30307, "Vyhledat"), {"action": "tmdb.series.search"}, "DefaultAddonsSearch.png")



    _end("files")





def tmdb_people_menu():

    xbmcplugin.setPluginCategory(addon_handle, i18n.T(31105, "TMDB / People"))

    xbmcplugin.setContent(addon_handle, "files")



    _add_dir(i18n.T(30401, "Populární"), {"action": "tmdb.people.popular", "page": "1"}, "DefaultActor.png")

    _add_dir(i18n.T(30307, "Vyhledat"), {"action": "tmdb.people.search"}, "DefaultAddonsSearch.png")



    _end("files")



def tmdb_trending_menu():

    xbmcplugin.setPluginCategory(addon_handle, i18n.T(31106, "TMDB / Trending"))

    xbmcplugin.setContent(addon_handle, "files")



    _add_dir(i18n.T(30412, "Filmy / Dnes"), {"action": "tmdb.trending.movies.day", "page": "1"}, "DefaultMovies.png")

    _add_dir(i18n.T(30413, "Filmy / Tento týden"), {"action": "tmdb.trending.movies.week", "page": "1"}, "DefaultMovies.png")



    _add_dir(i18n.T(30414, "Seriály / Dnes"), {"action": "tmdb.trending.series.day", "page": "1"}, "DefaultTVShows.png")

    _add_dir(i18n.T(30415, "Seriály / Tento týden"), {"action": "tmdb.trending.series.week", "page": "1"}, "DefaultTVShows.png")



    _add_dir(i18n.T(30416, "Osoby / Dnes"), {"action": "tmdb.trending.people.day", "page": "1"}, "DefaultActor.png")

    _add_dir(i18n.T(30417, "Osoby / Tento týden"), {"action": "tmdb.trending.people.week", "page": "1"}, "DefaultActor.png")



    _end("files")



def tmdb_search_menu():
    xbmcplugin.setPluginCategory(addon_handle, i18n.T(31107, "TMDB / Search"))
    xbmcplugin.setContent(addon_handle, "files")

    _add_dir(i18n.T(31108, "Movie"), {"action": "tmdb.search.movies"}, "DefaultMovies.png")
    _add_dir(i18n.T(31109, "Series"), {"action": "tmdb.search.series"}, "DefaultTVShows.png")

    _add_dir(i18n.T(31110, "People"), {"action": "tmdb.search.people"}, "DefaultActor.png")

    _add_dir(i18n.T(31111, "All"), {"action": "tmdb.search.multi"}, "DefaultAddonsSearch.png")


    _end("files")


def _tmdb_media_detail(media_type: str, tmdb_id: str) -> dict:
    if media_type == "movie":
        return _tmdb_movie_detail(tmdb_id)
    if media_type == "tv":
        return _tmdb_tv_detail(tmdb_id)
    return {}


def _tmdb_media_images_payload(media_type: str, tmdb_id: str) -> dict:
    if not tmdb_id:
        return {}
    if media_type == "movie":
        return _tmdb_get(f"/movie/{tmdb_id}/images", params={}, timeout=20)
    if media_type == "tv":
        return _tmdb_get(f"/tv/{tmdb_id}/images", params={}, timeout=20)
    return {}


def _tmdb_media_detail_with_images(media_type: str, tmdb_id: str) -> dict:
    item = _tmdb_media_detail(media_type, tmdb_id)
    if not item:
        return {}
    images = _tmdb_media_images_payload(media_type, tmdb_id)
    if images:
        item = dict(item)
        item["images"] = images
    return item


def _tmdb_media_base_label(media_type: str) -> str:
    return i18n.T(31108, "Movie") if media_type == "movie" else i18n.T(31109, "Series")


def _tmdb_media_action_prefix(media_type: str) -> str:
    return "tmdb.movie" if media_type == "movie" else "tmdb.tv"


def _tmdb_media_images(item: dict, image_kind: str) -> list:
    images = item.get("images") or {}
    if image_kind == "backdrops":
        return list(images.get("backdrops") or [])
    if image_kind == "posters":
        return list(images.get("posters") or [])
    if image_kind == "popular":
        rows = []
        for row in images.get("backdrops") or []:
            r = dict(row)
            r["_tmdb_image_kind"] = "backdrop"
            rows.append(r)
        for row in images.get("posters") or []:
            r = dict(row)
            r["_tmdb_image_kind"] = "poster"
            rows.append(r)
        rows.sort(key=_tmdb_media_image_sort_key, reverse=True)
        return rows
    return []


def _tmdb_media_image_sort_key(item: dict):
    try:
        vote_average = float(item.get("vote_average") or 0)
    except Exception:
        vote_average = 0.0
    try:
        vote_count = int(item.get("vote_count") or 0)
    except Exception:
        vote_count = 0
    return (vote_average, vote_count)


def _tmdb_media_count_label(label: str, count: int) -> str:
    return f"{label} ({count})" if count > 0 else label


def _tmdb_media_image_section_title(image_kind: str) -> str:
    if image_kind == "popular":
        return i18n.T(31606, "Most popular")
    if image_kind == "backdrops":
        return i18n.T(31608, "Backdrops")
    if image_kind == "posters":
        return i18n.T(31609, "Posters")
    return i18n.T(31406, "Media")


def _tmdb_media_image_label(image_kind: str, item: dict, index: int) -> str:
    label = _tmdb_media_image_section_title(image_kind)
    if image_kind == "posters":
        label = i18n.T(31610, "Poster")
    if image_kind == "popular":
        kind = item.get("_tmdb_image_kind")
        if kind == "backdrop":
            label = i18n.T(31608, "Backdrops")
        elif kind == "poster":
            label = i18n.T(31610, "Poster")

    parts = [f"{label} {index}"]
    width = item.get("width") or 0
    height = item.get("height") or 0
    if width and height:
        parts.append(f"{width}x{height}")
    lang = (item.get("iso_639_1") or "").strip()
    if lang:
        parts.append(lang.upper())
    try:
        votes = int(item.get("vote_count") or 0)
    except Exception:
        votes = 0
    if votes:
        parts.append(i18n.T(31918, "{count} votes").format(count=votes))
    if len(parts) == 1:
        return parts[0]
    return parts[0] + "  [" + " | ".join(parts[1:]) + "]"


def _tmdb_get_setting_bool(setting_id: str, default: bool = False) -> bool:
    try:
        val = addon.getSetting(setting_id)
        if str(val).strip() == "":
            return default
        return str(val).strip().lower() == "true"
    except Exception:
        return default


def _tmdb_get_setting_text(setting_id: str, default: str = "") -> str:
    try:
        return (addon.getSetting(setting_id) or default).strip()
    except Exception:
        return default


def _tmdb_ensure_dir(path: str) -> bool:
    try:
        import xbmcvfs
        if not path:
            return False
        if xbmcvfs.exists(path):
            return True
        return xbmcvfs.mkdirs(path)
    except Exception:
        return False


def _tmdb_safe_filename(name: str, fallback: str = "obrazek") -> str:
    import re
    name = (name or "").strip() or fallback
    name = re.sub(r'[\\/:*?"<>|]+', "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:180] or fallback


def _tmdb_image_ext_from_url(url: str) -> str:
    path = urllib.parse.urlsplit(url or "").path.lower()
    for ext in (".jpg", ".jpeg", ".png", ".webp"):
        if path.endswith(ext):
            return ext
    return ".jpg"


def _tmdb_youtube_search_query(item: dict, media_type: str, title: str) -> str:
    title = (title or "").strip()
    original = ""
    if media_type == "movie":
        original = _tmdb_hide_unreadable_text(item.get("original_title") or "")
    elif media_type == "tv":
        original = _tmdb_hide_unreadable_text(item.get("original_name") or "")
    original = (original or "").strip()

    base = title or original or "trailer"
    if original and original.casefold() != base.casefold():
        base = f"{base} {original}"
    return f"{base} trailer".strip()


def _add_tmdb_youtube_search_item(item: dict, media_type: str, title: str):
    query = _tmdb_youtube_search_query(item, media_type, title)
    youtube_label = i18n.T(31846, "Search on YouTube")
    li = xbmcgui.ListItem(label=youtube_label)
    li.setArt({
        "icon": "DefaultAddonsSearch.png",
        "thumb": "DefaultAddonsSearch.png",
        "poster": "DefaultAddonsSearch.png",
    })
    li.setProperty("IsPlayable", "false")
    _set_video_info(li, {
        "title": youtube_label,
        "plot": query,
        "mediatype": "video",
    })
    xbmcplugin.addDirectoryItem(
        handle=addon_handle,
        url=build_url({
            "action": "tmdb.youtube.search",
            "q": query,
            "title": title,
        }),
        listitem=li,
        isFolder=True
    )


def _tmdb_json_after_marker(text: str, marker: str):
    pos = (text or "").find(marker)
    if pos < 0:
        return None
    pos += len(marker)
    while pos < len(text) and text[pos] in " \t\r\n=":
        pos += 1
    try:
        data, _end_pos = json.JSONDecoder().raw_decode(text[pos:])
        return data
    except Exception:
        return None


def _tmdb_youtube_initial_data(html_text: str):
    for marker in ("var ytInitialData =", "ytInitialData ="):
        data = _tmdb_json_after_marker(html_text, marker)
        if data:
            return data
    return None


def _tmdb_youtube_text(node) -> str:
    if not isinstance(node, dict):
        return ""
    text = node.get("simpleText")
    if text:
        return html.unescape(str(text)).strip()
    runs = node.get("runs") or []
    parts = []
    for run in runs:
        if isinstance(run, dict) and run.get("text"):
            parts.append(str(run.get("text")))
    return html.unescape("".join(parts)).strip()


def _tmdb_youtube_thumb(renderer: dict) -> str:
    thumbs = (((renderer or {}).get("thumbnail") or {}).get("thumbnails") or [])
    if not thumbs:
        return "DefaultAddonVideo.png"
    url = str((thumbs[-1] or {}).get("url") or "").strip()
    return html.unescape(url) if url else "DefaultAddonVideo.png"


def _tmdb_youtube_collect_renderers(node, out: list, seen: set):
    if isinstance(node, dict):
        renderer = node.get("videoRenderer")
        if isinstance(renderer, dict):
            video_id = str(renderer.get("videoId") or "").strip()
            if video_id and video_id not in seen:
                seen.add(video_id)
                title = _tmdb_youtube_text(renderer.get("title") or {}) or "YouTube video"
                channel = (
                    _tmdb_youtube_text(renderer.get("ownerText") or {})
                    or _tmdb_youtube_text(renderer.get("longBylineText") or {})
                )
                duration = _tmdb_youtube_text(renderer.get("lengthText") or {})
                out.append({
                    "id": video_id,
                    "title": title,
                    "channel": channel,
                    "duration": duration,
                    "thumb": _tmdb_youtube_thumb(renderer),
                })
        for value in node.values():
            _tmdb_youtube_collect_renderers(value, out, seen)
    elif isinstance(node, list):
        for value in node:
            _tmdb_youtube_collect_renderers(value, out, seen)


def _tmdb_youtube_web_search(query: str, limit: int = 25) -> list:
    query = (query or "").strip()
    if not query:
        return []

    url = "https://www.youtube.com/results?" + urllib.parse.urlencode({
        "search_query": query,
        "hl": "cs",
        "persist_hl": "1",
    })
    req = urllib.request.Request(
        _sanitize_url(url),
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "cs-CZ,cs;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=20, context=_CTX) as resp:
            raw = resp.read()
        text = raw.decode("utf-8", errors="ignore")
    except Exception as e:
        xbmc.log(f"[IKARUS][TMDB] YouTube web search failed: {repr(e)}", xbmc.LOGWARNING)
        return []

    data = _tmdb_youtube_initial_data(text)
    if not data:
        xbmc.log("[IKARUS][TMDB] YouTube web search: ytInitialData not found", xbmc.LOGWARNING)
        return []

    rows = []
    _tmdb_youtube_collect_renderers(data, rows, set())
    return rows[:max(1, int(limit or 25))]


def tmdb_youtube_search_menu():
    query = _get_param("q", "").strip()
    fallback_title = _get_param("title", "YouTube").strip() or "YouTube"

    xbmcplugin.setPluginCategory(addon_handle, f"TMDB / YouTube / {fallback_title}")
    xbmcplugin.setContent(addon_handle, "videos")

    if not query:
        _empty_result_dir(f"TMDB / YouTube / {fallback_title}", "videos")
        return

    rows = _tmdb_youtube_web_search(query)
    if not rows:
        _empty_result_dir(
            f"TMDB / YouTube / {fallback_title}",
            "videos",
            i18n.T(31602, "No item found.")
        )
        return

    for row in rows:
        title = row.get("title") or "YouTube video"
        channel = row.get("channel") or ""
        duration = row.get("duration") or ""
        video_id = row.get("id") or ""

        label = title
        meta = []
        if channel:
            meta.append(channel)
        if duration:
            meta.append(duration)
        if meta:
            label += "  [" + " | ".join(meta) + "]"

        thumb = row.get("thumb") or "DefaultAddonVideo.png"
        li = xbmcgui.ListItem(label=label)
        li.setArt({
            "icon": thumb,
            "thumb": thumb,
            "poster": thumb,
        })
        li.setProperty("IsPlayable", "true")
        _set_video_info(li, {
            "title": title,
            "plot": f"YouTube / {channel}" if channel else "YouTube",
            "mediatype": "video",
        })

        xbmcplugin.addDirectoryItem(
            handle=addon_handle,
            url=build_url({
                "action": "tmdb.video.play",
                "site": "youtube",
                "key": video_id,
                "title": title,
            }),
            listitem=li,
            isFolder=False
        )

    _end("videos")


def _tmdb_image_download():
    url = _get_param("url", "").strip()
    title = _get_param("title", i18n.T(31603, "Image")).strip() or i18n.T(31603, "Image")
    file_name = _get_param("filename", "").strip()

    if not url:
        xbmcgui.Dialog().ok(i18n.T(30723, "TMDB / Image"), i18n.T(30724, "Missing image URL."))
        return

    if not _tmdb_get_setting_bool("download_enable", True):
        xbmcgui.Dialog().ok(i18n.T(30700, "Downloads"), i18n.T(30701, "Downloads are disabled in settings."))
        return

    download_dir = _tmdb_get_setting_text("download_path", "")
    if not download_dir:
        xbmcgui.Dialog().ok(i18n.T(30700, "Downloads"), i18n.T(30702, "Download target folder is not set."))
        return

    if not _tmdb_ensure_dir(download_dir):
        xbmcgui.Dialog().ok(
            i18n.T(30700, "Downloads"),
            i18n.T(30703, "Could not create / open target folder:\n{path}").format(path=download_dir),
        )
        return

    ext = _tmdb_image_ext_from_url(url)
    if not file_name:
        file_name = _tmdb_safe_filename(title, "obrazek") + ext
    else:
        file_name = _tmdb_safe_filename(file_name, "obrazek")
        if "." not in file_name:
            file_name += ext

    if _tmdb_get_setting_bool("download_ask_name", True):
        input_type = getattr(xbmcgui, "INPUT_ALPHANUM", 0)
        custom = xbmcgui.Dialog().input(i18n.T(30706, "File name"), defaultt=file_name, type=input_type)
        if not custom:
            return
        custom = _tmdb_safe_filename(custom, "obrazek")
        if "." not in custom:
            custom += ext
        file_name = custom

    dst_path = download_dir.rstrip("/\\") + "/" + file_name

    try:
        import xbmcvfs
        if xbmcvfs.exists(dst_path) and not _tmdb_get_setting_bool("download_overwrite", False):
            yes = xbmcgui.Dialog().yesno(
                i18n.T(30700, "Downloads"),
                i18n.T(30707, "File already exists:\n{file}\n\nOverwrite it?").format(file=file_name)
            )
            if not yes:
                return

        req = urllib.request.Request(
            _sanitize_url(url),
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": "https://www.themoviedb.org/",
                "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            }
        )
        with urllib.request.urlopen(req, timeout=30, context=_CTX) as resp:
            dst = xbmcvfs.File(dst_path, "wb")
            try:
                while True:
                    chunk = resp.read(1024 * 128)
                    if not chunk:
                        break
                    dst.write(chunk)
            finally:
                dst.close()

        xbmcgui.Dialog().notification(
            i18n.T(30700, "Downloads"),
            i18n.T(30725, "Saved: {file}").format(file=file_name),
            xbmcgui.NOTIFICATION_INFO,
            3000,
            sound=False
        )
    except Exception as e:
        xbmc.log(f"[IKARUS][TMDB] image download failed: {repr(e)}", xbmc.LOGERROR)
        xbmcgui.Dialog().ok(
            i18n.T(30700, "Downloads"),
            i18n.T(30726, "Image could not be downloaded:\n{error}").format(error=e),
        )


def _tmdb_media_menu(media_type: str):
    tmdb_id = _get_param("id", "").strip()
    fallback_title = _get_param("title", _tmdb_media_base_label(media_type)).strip() or _tmdb_media_base_label(media_type)

    base_label = _tmdb_media_base_label(media_type)
    if not tmdb_id:
        _placeholder_dir(i18n.T(31604, "TMDB / {media} / Media").format(media=base_label), i18n.T(31305, "(missing TMDB id)"))
        return

    item = _tmdb_media_detail_with_images(media_type, tmdb_id)
    if not item:
        _placeholder_dir(i18n.T(31604, "TMDB / {media} / Media").format(media=base_label), i18n.T(31306, "({title}) - detail could not be loaded").format(title=fallback_title))
        return

    title = _tmdb_display_title(item, media_type, fallback_title) or fallback_title
    videos_count = len((item.get("videos") or {}).get("results") or [])
    backdrops_count = len(_tmdb_media_images(item, "backdrops"))
    posters_count = len(_tmdb_media_images(item, "posters"))

    xbmcplugin.setPluginCategory(addon_handle, i18n.T(31605, "TMDB / {media} / {title} / Media").format(media=base_label, title=title))
    xbmcplugin.setContent(addon_handle, "files")

    action_prefix = _tmdb_media_action_prefix(media_type)
    icon = "DefaultPicture.png"

    _add_dir(
        i18n.T(31606, "Most popular"),
        {
            "action": f"{action_prefix}.media.popular",
            "id": str(tmdb_id),
            "title": title,
        },
        icon
    )

    _add_dir(
        _tmdb_media_count_label(i18n.T(31607, "Videos"), videos_count),
        {
            "action": f"{action_prefix}.videos",
            "id": str(tmdb_id),
            "title": title,
            "section": "media",
        },
        "DefaultAddonVideo.png"
    )

    _add_dir(
        _tmdb_media_count_label(i18n.T(31608, "Backdrops"), backdrops_count),
        {
            "action": f"{action_prefix}.media.backdrops",
            "id": str(tmdb_id),
            "title": title,
        },
        icon
    )

    _add_dir(
        _tmdb_media_count_label(i18n.T(31609, "Posters"), posters_count),
        {
            "action": f"{action_prefix}.media.posters",
            "id": str(tmdb_id),
            "title": title,
        },
        icon
    )

    _end("files")


def _tmdb_media_images_menu(media_type: str, image_kind: str):
    tmdb_id = _get_param("id", "").strip()
    fallback_title = _get_param("title", _tmdb_media_base_label(media_type)).strip() or _tmdb_media_base_label(media_type)

    base_label = _tmdb_media_base_label(media_type)
    section_title = _tmdb_media_image_section_title(image_kind)
    if not tmdb_id:
        _placeholder_dir(i18n.T(31611, "TMDB / {media} / Media / {section}").format(media=base_label, section=section_title), i18n.T(31305, "(missing TMDB id)"))
        return

    item = _tmdb_media_detail_with_images(media_type, tmdb_id)
    if not item:
        _placeholder_dir(i18n.T(31611, "TMDB / {media} / Media / {section}").format(media=base_label, section=section_title), i18n.T(31306, "({title}) - detail could not be loaded").format(title=fallback_title))
        return

    title = _tmdb_display_title(item, media_type, fallback_title) or fallback_title
    rows = _tmdb_media_images(item, image_kind)

    xbmcplugin.setPluginCategory(addon_handle, i18n.T(31612, "TMDB / {media} / {title} / Media / {section}").format(media=base_label, title=title, section=section_title))
    xbmcplugin.setContent(addon_handle, "images")

    if not rows:
        _empty_result_dir(i18n.T(31612, "TMDB / {media} / {title} / Media / {section}").format(media=base_label, title=title, section=section_title), "images")
        return

    added = 0
    for idx, row in enumerate(rows, 1):
        file_path = (row.get("file_path") or "").strip()
        if not file_path:
            continue

        thumb = TMDB_IMAGE_THUMB_BASE + file_path
        original = TMDB_IMAGE_ORIGINAL_BASE + file_path
        row_kind = row.get("_tmdb_image_kind") or ("poster" if image_kind == "posters" else "backdrop")

        art = {
            "icon": thumb,
            "thumb": thumb,
            "poster": thumb,
            "fanart": original,
        }
        if row_kind == "poster":
            art["poster"] = thumb
        else:
            art["fanart"] = original

        image_label = _tmdb_media_image_label(image_kind, row, idx)
        li = xbmcgui.ListItem(label=image_label)
        li.setArt(art)
        li.setProperty("IsPlayable", "false")
        try:
            li.addContextMenuItems([
                (
                    i18n.T(31847, "Download image"),
                    "RunPlugin(%s)" % build_url({
                        "action": "tmdb.image.download",
                        "url": original,
                        "title": f"{title} - {image_label}",
                        "filename": f"{title} - {image_label}{_tmdb_image_ext_from_url(original)}",
                    })
                )
            ], replaceItems=False)
        except Exception:
            pass

        xbmcplugin.addDirectoryItem(
            handle=addon_handle,
            url=original,
            listitem=li,
            isFolder=False
        )
        added += 1

    if added == 0:
        _empty_result_dir(i18n.T(31612, "TMDB / {media} / {title} / Media / {section}").format(media=base_label, title=title, section=section_title), "images")
        return

    _end("images")


def tmdb_movie_media_menu():
    _tmdb_media_menu("movie")


def tmdb_movie_media_popular_menu():
    _tmdb_media_images_menu("movie", "popular")


def tmdb_movie_media_backdrops_menu():
    _tmdb_media_images_menu("movie", "backdrops")


def tmdb_movie_media_posters_menu():
    _tmdb_media_images_menu("movie", "posters")


def tmdb_tv_media_menu():
    _tmdb_media_menu("tv")


def tmdb_tv_media_popular_menu():
    _tmdb_media_images_menu("tv", "popular")


def tmdb_tv_media_backdrops_menu():
    _tmdb_media_images_menu("tv", "backdrops")


def tmdb_tv_media_posters_menu():
    _tmdb_media_images_menu("tv", "posters")


def tmdb_movie_detail_menu():
    tmdb_id = _get_param("id", "").strip()
    fallback_title = _get_param("title", "Film").strip() or "Film"
    watch_source = _tmdb_watch_source_param()


    if not tmdb_id:

        _placeholder_dir(i18n.T(31400, "TMDB / Movie / Detail"), i18n.T(31305, "(missing TMDB id)"))

        return



    item = _tmdb_movie_detail(tmdb_id)

    if not item:

        _placeholder_dir(i18n.T(31400, "TMDB / Movie / Detail"), i18n.T(31306, "({title}) - detail could not be loaded").format(title=fallback_title))

        return



    title = _tmdb_display_title(item, "movie", fallback_title) or fallback_title
    _tmdb_remember_detail_playback_meta(tmdb_id, item, "movie")


    xbmcplugin.setPluginCategory(addon_handle, i18n.T(31401, "TMDB / Movie / {title}").format(title=title))

    xbmcplugin.setContent(addon_handle, "movies")



    _add_movie_detail_header(item, watch_source=watch_source)


    _add_dir(

        i18n.T(31402, "Basic information"),

        {

            "action": "tmdb.movie.info",

            "id": str(tmdb_id),

            "title": title,

        },

        "DefaultAddonProgram.png"

    )



    release_date = (item.get("release_date") or "").strip()

    year = release_date[:4] if len(release_date) >= 4 and release_date[:4].isdigit() else ""

    original_title = (item.get("original_title") or "").strip()



    cast_count = _tmdb_media_cast_count(item)



    _add_dir(

        i18n.T(31404, "Cast ({count})").format(count=cast_count) if cast_count > 0 else i18n.T(31403, "Cast"),

        {

            "action": "tmdb.movie.cast",

            "id": str(tmdb_id),

            "title": title,

        },

        "DefaultActor.png"

    )



    for dep in _tmdb_media_crew_departments_with_counts(item):

        _add_dir(

            f"{dep['label']} ({dep['count']})",

            {

                "action": "tmdb.movie.department",

                "id": str(tmdb_id),

                "title": title,

                "department": dep["department"],

            },

            dep["icon"]

        )



    _add_dir(
        i18n.T(31405, "Similar movies"),
        {
            "action": "tmdb.movie.similar",
            "id": str(tmdb_id),
            "title": title,

            "page": "1",

        },

        "DefaultMovies.png"
    )

    _add_dir(
        i18n.T(31406, "Media"),
        {
            "action": "tmdb.movie.media",
            "id": str(tmdb_id),
            "title": title,
        },
        "DefaultPicture.png"
    )

    _end("movies")


def tmdb_movie_info_menu():

    tmdb_id = _get_param("id", "").strip()

    fallback_title = _get_param("title", "Film").strip() or "Film"



    if not tmdb_id:

        _placeholder_dir(i18n.T(31445, "TMDB / Movie / Basic information"), i18n.T(31305, "(missing TMDB id)"))

        return



    item = _tmdb_movie_detail(tmdb_id)

    if not item:

        _placeholder_dir(i18n.T(31407, "TMDB / Movie / {title} / Basic information").format(title=fallback_title), i18n.T(31306, "({title}) - detail could not be loaded").format(title=fallback_title))

        return



    title = _tmdb_display_title(item, "movie", fallback_title) or fallback_title
    xbmcplugin.setPluginCategory(addon_handle, i18n.T(31407, "TMDB / Movie / {title} / Basic information").format(title=title))

    xbmcplugin.setContent(addon_handle, "files")



    original = _tmdb_hide_unreadable_text(item.get("original_title") or "")
    tagline = item.get("tagline") or ""

    status = item.get("status") or ""

    budget = item.get("budget") or 0

    revenue = item.get("revenue") or 0

    runtime = item.get("runtime") or 0

    homepage = item.get("homepage") or ""

    imdb_id = item.get("imdb_id") or ""



    genres = _tmdb_join_names(item.get("genres") or [])

    countries = _tmdb_join_names(item.get("production_countries") or [], key="name", limit=10)

    companies = _tmdb_join_names(item.get("production_companies") or [], key="name", limit=10)



    rows = [

        (i18n.T(31430, "Title"), title),

        (i18n.T(31431, "Original title"), original),

        (i18n.T(31432, "Tagline"), tagline),

        (i18n.T(31433, "Status"), status),

        (i18n.T(31434, "Genres"), genres),

        (i18n.T(31435, "Country"), countries),

        (i18n.T(31436, "Production"), companies),

        (i18n.T(31437, "Runtime"), _tmdb_movie_runtime_text(runtime)),

        ("IMDb ID", imdb_id),

        ("Homepage", homepage),

        (i18n.T(31438, "Budget"), f"{budget:,} USD" if budget else ""),

        (i18n.T(31439, "Revenue"), f"{revenue:,} USD" if revenue else ""),

    ]



    added = 0

    for left, right in rows:

        right = (right or "").strip() if isinstance(right, str) else right

        if not right:

            continue



        li = xbmcgui.ListItem(label=f"{left}: {right}")

        li.setArt(_tmdb_media_art(item))

        xbmcplugin.addDirectoryItem(

            handle=addon_handle,

            url="",

            listitem=li,

            isFolder=False

        )

        added += 1



    if added == 0:

        _placeholder_dir(i18n.T(31407, "TMDB / Movie / {title} / Basic information").format(title=title), i18n.T(31408, "(no detailed metadata)"))

        return



    _end("files")



def tmdb_movie_cast_menu():

    tmdb_id = _get_param("id", "").strip()

    fallback_title = _get_param("title", "Film").strip() or "Film"



    if not tmdb_id:

        _placeholder_dir(i18n.T(31409, "TMDB / Movie / Cast"), i18n.T(31305, "(missing TMDB id)"))

        return



    item = _tmdb_movie_detail(tmdb_id)

    if not item:

        _placeholder_dir(i18n.T(31409, "TMDB / Movie / Cast"), i18n.T(31306, "({title}) - detail could not be loaded").format(title=fallback_title))

        return



    title = _tmdb_display_title(item, "movie", fallback_title) or fallback_title
    credits = item.get("credits") or {}

    cast = credits.get("cast") or []



    xbmcplugin.setPluginCategory(addon_handle, i18n.T(31410, "TMDB / Movie / {title} / Cast").format(title=title))

    xbmcplugin.setContent(addon_handle, "files")



    if not cast:

        _placeholder_dir(i18n.T(31410, "TMDB / Movie / {title} / Cast").format(title=title), i18n.T(31411, "(no cast)"))

        return



    for person in cast[:50]:

        person_id = person.get("id")

        name = (person.get("name") or "").strip()

        character = (person.get("character") or "").strip()

        profile_path = (person.get("profile_path") or "").strip()



        label = name or i18n.T(31845, "Unknown name")

        if character:

            label += f" — {character}"



        li = xbmcgui.ListItem(label=label)



        art = {

            "icon": "DefaultActor.png",

            "thumb": "DefaultActor.png",

            "poster": "DefaultActor.png",

        }

        if profile_path:

            full = TMDB_IMAGE_BASE + profile_path

            art["icon"] = full

            art["thumb"] = full

            art["poster"] = full



        li.setArt(art)

        li.setProperty("IsPlayable", "false")



        u = build_url({

            "action": "tmdb.person.detail",

            "id": str(person_id or ""),

            "name": name or "Osoba",

        })



        xbmcplugin.addDirectoryItem(

            handle=addon_handle,

            url=u,

            listitem=li,

            isFolder=True

        )



    _end("files")



def tmdb_movie_department_menu():

    tmdb_id = _get_param("id", "").strip()

    fallback_title = _get_param("title", "Film").strip() or "Film"

    department = _get_param("department", "").strip()



    if not tmdb_id:

        _placeholder_dir(i18n.T(31412, "TMDB / Movie / Crew"), i18n.T(31305, "(missing TMDB id)"))

        return



    if not department:

        _placeholder_dir(i18n.T(31412, "TMDB / Movie / Crew"), i18n.T(31414, "(missing department)"))

        return



    item = _tmdb_movie_detail(tmdb_id)

    if not item:

        _placeholder_dir(i18n.T(31412, "TMDB / Movie / Crew"), i18n.T(31306, "({title}) - detail could not be loaded").format(title=fallback_title))

        return



    title = _tmdb_display_title(item, "movie", fallback_title) or fallback_title
    credits = (item.get("credits") or {})

    crew = credits.get("crew") or []



    rows = []

    seen = {}



    for person in crew:

        dept = (person.get("department") or "").strip()

        if dept != department:

            continue



        key = str(person.get("id") or "")

        job = (person.get("job") or "").strip()



        if key not in seen:

            row = dict(person)

            row["_jobs"] = []

            if job:

                row["_jobs"].append(job)

            seen[key] = row

        else:

            if job and job not in seen[key]["_jobs"]:

                seen[key]["_jobs"].append(job)



    rows = list(seen.values())

    rows.sort(key=_tmdb_person_name_sort_key)



    dept_label = _tmdb_media_department_label(department)



    xbmcplugin.setPluginCategory(addon_handle, i18n.T(31413, "TMDB / Movie / {title} / {department}").format(title=title, department=dept_label))

    xbmcplugin.setContent(addon_handle, "files")



    if not rows:

        _placeholder_dir(i18n.T(31413, "TMDB / Movie / {title} / {department}").format(title=title, department=dept_label), i18n.T(31415, "(no data)"))

        return



    for person in rows:

        jobs = person.get("_jobs") or []

        role_text = ", ".join(jobs[:3]).strip()

        _add_tmdb_person_folder_item(person, role_text=role_text)



    _end("files")



def tmdb_movie_similar_list():

    tmdb_id = _get_param("id", "").strip()

    fallback_title = _get_param("title", "Film").strip() or "Film"

    page = _get_int_param("page", 1)



    if not tmdb_id:

        _placeholder_dir(i18n.T(31416, "TMDB / Movie / Similar movies"), i18n.T(31305, "(missing TMDB id)"))

        return



    _tmdb_movies_list_from_endpoint(

        endpoint=f"/movie/{tmdb_id}/similar",

        category_title=i18n.T(31417, "TMDB / Movie / {title} / Similar movies").format(title=fallback_title),

        page=page,

        next_action="tmdb.movie.similar",

        persist_params={

            "id": tmdb_id,

            "title": fallback_title,

        }

    )



def tmdb_movie_videos_menu():
    tmdb_id = _get_param("id", "").strip()
    fallback_title = _get_param("title", "Film").strip() or "Film"
    section = _get_param("section", "").strip()
    section_label = i18n.T(31613, "Media / Videos") if section == "media" else i18n.T(31614, "Trailers / videos")

    if not tmdb_id:
        _placeholder_dir(i18n.T(31615, "TMDB / Movie / {section}").format(section=section_label), i18n.T(31305, "(missing TMDB id)"))
        return

    item = _tmdb_movie_detail(tmdb_id)
    if not item:
        _placeholder_dir(i18n.T(31615, "TMDB / Movie / {section}").format(section=section_label), i18n.T(31306, "({title}) - detail could not be loaded").format(title=fallback_title))
        return

    title = _tmdb_display_title(item, "movie", fallback_title) or fallback_title
    videos = ((item.get("videos") or {}).get("results") or [])

    xbmcplugin.setPluginCategory(addon_handle, i18n.T(31616, "TMDB / Movie / {title} / {section}").format(title=title, section=section_label))
    xbmcplugin.setContent(addon_handle, "videos")

    _add_tmdb_youtube_search_item(item, "movie", title)

    if not videos:
        _end("videos")
        return

    added = 0


    for v in videos[:30]:

        name = (v.get("name") or "Video").strip()

        vtype = (v.get("type") or "").strip()

        site = (v.get("site") or "").strip()

        key = (v.get("key") or "").strip()



        if not key:

            continue



        label = name

        meta = []

        if vtype:

            meta.append(vtype)

        if site:

            meta.append(site)

        if meta:

            label += "  [" + " | ".join(meta) + "]"



        li = xbmcgui.ListItem(label=label)

        li.setArt({

            "icon": "DefaultAddonVideo.png",

            "thumb": "DefaultAddonVideo.png",

            "poster": "DefaultAddonVideo.png",

        })

        li.setProperty("IsPlayable", "true")

        _set_video_info(li, {

            "title": name,

            "plot": f"{title} / {vtype}" if vtype else title,

            "mediatype": "video",

        })



        u = build_url({

            "action": "tmdb.video.play",

            "site": site,

            "key": key,

            "title": name,

        })



        xbmcplugin.addDirectoryItem(

            handle=addon_handle,

            url=u,

            listitem=li,

            isFolder=False

        )

        added += 1


    if added == 0:
        _end("videos")
        return

    _end("videos")


def tmdb_tv_detail_menu():
    tmdb_id = _get_param("id", "").strip()
    fallback_title = _get_param("title", i18n.T(31913, "Series")).strip() or i18n.T(31913, "Series")
    original_title = _get_param("original_title", "").strip()
    watch_source = _tmdb_watch_source_param()
    if not tmdb_id:

        _placeholder_dir(i18n.T(31418, "TMDB / Series / Detail"), i18n.T(31305, "(missing TMDB id)"))

        return



    item = _tmdb_tv_detail(tmdb_id)

    if not item:

        _placeholder_dir(i18n.T(31418, "TMDB / Series / Detail"), i18n.T(31306, "({title}) - detail could not be loaded").format(title=fallback_title))

        return



    title = _tmdb_display_title(item, "tv", fallback_title) or fallback_title
    _tmdb_remember_detail_playback_meta(tmdb_id, item, "tv")


    xbmcplugin.setPluginCategory(addon_handle, i18n.T(31419, "TMDB / Series / {title}").format(title=title))

    xbmcplugin.setContent(addon_handle, "tvshows")



    _add_tv_detail_header(item, watch_source=watch_source)


    _add_dir(

        i18n.T(31402, "Basic information"),

        {

            "action": "tmdb.tv.info",

            "id": str(tmdb_id),

            "title": title,

        },

        "DefaultAddonProgram.png"

    )



    cast_count = _tmdb_media_cast_count(item)



    _add_dir(

        i18n.T(31404, "Cast ({count})").format(count=cast_count) if cast_count > 0 else i18n.T(31403, "Cast"),

        {

            "action": "tmdb.tv.cast",

            "id": str(tmdb_id),

            "title": title,

        },

        "DefaultActor.png"

    )



    for dep in _tmdb_media_crew_departments_with_counts(item):

        _add_dir(

            f"{dep['label']} ({dep['count']})",

            {

                "action": "tmdb.tv.department",

                "id": str(tmdb_id),

                "title": title,

                "department": dep["department"],

            },

            dep["icon"]

        )



    _add_dir(
        i18n.T(31420, "Similar series"),
        {
            "action": "tmdb.tv.similar",
            "id": str(tmdb_id),
            "title": title,

            "page": "1",

        },

        "DefaultTVShows.png"
    )

    _add_dir(
        i18n.T(31406, "Media"),
        {
            "action": "tmdb.tv.media",
            "id": str(tmdb_id),
            "title": title,
        },
        "DefaultPicture.png"
    )

    _end("tvshows")


def tmdb_tv_info_menu():

    tmdb_id = _get_param("id", "").strip()

    fallback_title = _get_param("title", i18n.T(31913, "Series")).strip() or i18n.T(31913, "Series")



    if not tmdb_id:

        _placeholder_dir(i18n.T(31446, "TMDB / Series / Basic information"), i18n.T(31305, "(missing TMDB id)"))

        return



    item = _tmdb_tv_detail(tmdb_id)

    if not item:

        _placeholder_dir(i18n.T(31446, "TMDB / Series / Basic information"), i18n.T(31306, "({title}) - detail could not be loaded").format(title=fallback_title))

        return



    title = _tmdb_display_title(item, "tv", fallback_title) or fallback_title
    xbmcplugin.setPluginCategory(addon_handle, i18n.T(31421, "TMDB / Series / {title} / Basic information").format(title=title))

    xbmcplugin.setContent(addon_handle, "files")



    original = _tmdb_hide_unreadable_text(item.get("original_name") or "")
    tagline = item.get("tagline") or ""

    status = item.get("status") or ""

    first_air_date = item.get("first_air_date") or ""

    last_air_date = item.get("last_air_date") or ""

    number_of_seasons = item.get("number_of_seasons") or 0

    number_of_episodes = item.get("number_of_episodes") or 0



    genres = _tmdb_join_names(item.get("genres") or [])

    countries = _tmdb_join_names(item.get("origin_country") or [], key=None, limit=10)

    networks = _tmdb_join_names(item.get("networks") or [], key="name", limit=10)

    companies = _tmdb_join_names(item.get("production_companies") or [], key="name", limit=10)



    rows = [

        (i18n.T(31430, "Title"), title),

        (i18n.T(31431, "Original title"), original),

        (i18n.T(31432, "Tagline"), tagline),

        (i18n.T(31433, "Status"), status),

        (i18n.T(31440, "First air date"), first_air_date),

        (i18n.T(31441, "Last air date"), last_air_date),

        (i18n.T(31442, "Number of seasons"), str(number_of_seasons) if number_of_seasons else ""),

        (i18n.T(31443, "Number of episodes"), str(number_of_episodes) if number_of_episodes else ""),

        (i18n.T(31434, "Genres"), genres),

        (i18n.T(31435, "Country"), countries),

        (i18n.T(31444, "Networks"), networks),

        (i18n.T(31436, "Production"), companies),

    ]



    added = 0

    for left, right in rows:

        right = (right or "").strip() if isinstance(right, str) else right

        if not right:

            continue



        li = xbmcgui.ListItem(label=f"{left}: {right}")

        li.setArt(_tmdb_media_art(item))

        xbmcplugin.addDirectoryItem(

            handle=addon_handle,

            url="",

            listitem=li,

            isFolder=False

        )

        added += 1



    if added == 0:

        _placeholder_dir(i18n.T(31421, "TMDB / Series / {title} / Basic information").format(title=title), i18n.T(31408, "(no detailed metadata)"))

        return



    _end("files")



def tmdb_tv_cast_menu():

    tmdb_id = _get_param("id", "").strip()

    fallback_title = _get_param("title", i18n.T(31913, "Series")).strip() or i18n.T(31913, "Series")



    if not tmdb_id:

        _placeholder_dir(i18n.T(31422, "TMDB / Series / Cast"), i18n.T(31305, "(missing TMDB id)"))

        return



    item = _tmdb_tv_detail(tmdb_id)

    if not item:

        _placeholder_dir(i18n.T(31422, "TMDB / Series / Cast"), i18n.T(31306, "({title}) - detail could not be loaded").format(title=fallback_title))

        return



    title = _tmdb_display_title(item, "tv", fallback_title) or fallback_title
    credits = item.get("credits") or {}

    cast = credits.get("cast") or []



    xbmcplugin.setPluginCategory(addon_handle, i18n.T(31423, "TMDB / Series / {title} / Cast").format(title=title))

    xbmcplugin.setContent(addon_handle, "files")



    if not cast:

        _placeholder_dir(i18n.T(31423, "TMDB / Series / {title} / Cast").format(title=title), i18n.T(31411, "(no cast)"))

        return



    for person in cast[:50]:

        person_id = person.get("id")

        name = (person.get("name") or "").strip()

        character = (person.get("character") or "").strip()

        profile_path = (person.get("profile_path") or "").strip()



        label = name or i18n.T(31845, "Unknown name")

        if character:

            label += f" — {character}"



        li = xbmcgui.ListItem(label=label)



        art = {

            "icon": "DefaultActor.png",

            "thumb": "DefaultActor.png",

            "poster": "DefaultActor.png",

        }

        if profile_path:

            full = TMDB_IMAGE_BASE + profile_path

            art["icon"] = full

            art["thumb"] = full

            art["poster"] = full



        li.setArt(art)

        li.setProperty("IsPlayable", "false")



        u = build_url({

            "action": "tmdb.person.detail",

            "id": str(person_id or ""),

            "name": name or "Osoba",

        })



        xbmcplugin.addDirectoryItem(

            handle=addon_handle,

            url=u,

            listitem=li,

            isFolder=True

        )



    _end("files")



def tmdb_tv_department_menu():

    tmdb_id = _get_param("id", "").strip()

    fallback_title = _get_param("title", i18n.T(31913, "Series")).strip() or i18n.T(31913, "Series")

    department = _get_param("department", "").strip()



    if not tmdb_id:

        _placeholder_dir(i18n.T(31424, "TMDB / Series / Crew"), i18n.T(31305, "(missing TMDB id)"))

        return



    if not department:

        _placeholder_dir(i18n.T(31424, "TMDB / Series / Crew"), i18n.T(31414, "(missing department)"))

        return



    item = _tmdb_tv_detail(tmdb_id)

    if not item:

        _placeholder_dir(i18n.T(31424, "TMDB / Series / Crew"), i18n.T(31306, "({title}) - detail could not be loaded").format(title=fallback_title))

        return



    title = _tmdb_display_title(item, "tv", fallback_title) or fallback_title
    credits = (item.get("credits") or {})

    crew = credits.get("crew") or []



    rows = []

    seen = {}



    for person in crew:

        dept = (person.get("department") or "").strip()

        if dept != department:

            continue



        key = str(person.get("id") or "")

        job = (person.get("job") or "").strip()



        if key not in seen:

            row = dict(person)

            row["_jobs"] = []

            if job:

                row["_jobs"].append(job)

            seen[key] = row

        else:

            if job and job not in seen[key]["_jobs"]:

                seen[key]["_jobs"].append(job)



    rows = list(seen.values())

    rows.sort(key=_tmdb_person_name_sort_key)



    dept_label = _tmdb_media_department_label(department)



    xbmcplugin.setPluginCategory(addon_handle, i18n.T(31425, "TMDB / Series / {title} / {department}").format(title=title, department=dept_label))

    xbmcplugin.setContent(addon_handle, "files")



    if not rows:

        _placeholder_dir(i18n.T(31425, "TMDB / Series / {title} / {department}").format(title=title, department=dept_label), i18n.T(31415, "(no data)"))

        return



    for person in rows:

        jobs = person.get("_jobs") or []

        role_text = ", ".join(jobs[:3]).strip()

        _add_tmdb_person_folder_item(person, role_text=role_text)



    _end("files")



def tmdb_tv_similar_list():

    tmdb_id = _get_param("id", "").strip()

    fallback_title = _get_param("title", i18n.T(31913, "Series")).strip() or i18n.T(31913, "Series")

    page = _get_int_param("page", 1)



    if not tmdb_id:

        _placeholder_dir(i18n.T(31426, "TMDB / Series / Similar series"), i18n.T(31305, "(missing TMDB id)"))

        return



    _tmdb_tv_list_from_endpoint(

        endpoint=f"/tv/{tmdb_id}/similar",

        category_title=i18n.T(31427, "TMDB / Series / {title} / Similar series").format(title=fallback_title),

        page=page,

        next_action="tmdb.tv.similar",

        persist_params={

            "id": tmdb_id,

            "title": fallback_title,

        }

    )



def tmdb_tv_videos_menu():
    tmdb_id = _get_param("id", "").strip()
    fallback_title = _get_param("title", i18n.T(31913, "Series")).strip() or i18n.T(31913, "Series")
    section = _get_param("section", "").strip()
    section_label = i18n.T(31613, "Media / Videos") if section == "media" else i18n.T(31614, "Trailers / videos")

    if not tmdb_id:
        _placeholder_dir(i18n.T(31617, "TMDB / Series / {section}").format(section=section_label), i18n.T(31305, "(missing TMDB id)"))
        return

    item = _tmdb_tv_detail(tmdb_id)
    if not item:
        _placeholder_dir(i18n.T(31617, "TMDB / Series / {section}").format(section=section_label), i18n.T(31306, "({title}) - detail could not be loaded").format(title=fallback_title))
        return

    title = _tmdb_display_title(item, "tv", fallback_title) or fallback_title
    videos = ((item.get("videos") or {}).get("results") or [])

    xbmcplugin.setPluginCategory(addon_handle, i18n.T(31618, "TMDB / Series / {title} / {section}").format(title=title, section=section_label))
    xbmcplugin.setContent(addon_handle, "videos")

    _add_tmdb_youtube_search_item(item, "tv", title)

    if not videos:
        _end("videos")
        return

    added = 0


    for v in videos[:30]:

        name = (v.get("name") or "Video").strip()

        vtype = (v.get("type") or "").strip()

        site = (v.get("site") or "").strip()

        key = (v.get("key") or "").strip()



        if not key:

            continue



        label = name

        meta = []

        if vtype:

            meta.append(vtype)

        if site:

            meta.append(site)

        if meta:

            label += "  [" + " | ".join(meta) + "]"



        li = xbmcgui.ListItem(label=label)

        li.setArt({

            "icon": "DefaultAddonVideo.png",

            "thumb": "DefaultAddonVideo.png",

            "poster": "DefaultAddonVideo.png",

        })

        li.setProperty("IsPlayable", "true")

        _set_video_info(li, {

            "title": name,

            "plot": f"{title} / {vtype}" if vtype else title,

            "mediatype": "video",

        })



        u = build_url({

            "action": "tmdb.video.play",

            "site": site,

            "key": key,

            "title": name,

        })



        xbmcplugin.addDirectoryItem(

            handle=addon_handle,

            url=u,

            listitem=li,

            isFolder=False

        )

        added += 1


    if added == 0:
        _end("videos")
        return

    _end("videos")


def tmdb_person_detail_menu():

    person_id = _get_param("id", "").strip()

    fallback_name = _get_param("name", i18n.T(31500, "Person")).strip() or i18n.T(31500, "Person")



    if not person_id:

        _placeholder_dir(i18n.T(31501, "TMDB / People / Detail"), i18n.T(31305, "(missing TMDB id)"))

        return



    item = _tmdb_person_detail(person_id)

    if not item:

        _placeholder_dir(i18n.T(31501, "TMDB / People / Detail"), i18n.T(31306, "({title}) - detail could not be loaded").format(title=fallback_name))

        return



    name = item.get("name") or fallback_name

    biography = (item.get("biography") or "").strip()

    birthday = (item.get("birthday") or "").strip()

    deathday = (item.get("deathday") or "").strip()

    place_of_birth = (item.get("place_of_birth") or "").strip()

    known_for_department = (item.get("known_for_department") or "").strip()



    known_titles = _tmdb_person_known_titles(item, limit=8)



    lines = []

    if known_for_department:

        lines.append(i18n.T(31503, "Profession: {value}").format(value=known_for_department))

    if birthday:

        lines.append(i18n.T(31504, "Born: {value}").format(value=birthday))

    if deathday:

        lines.append(i18n.T(31505, "Died: {value}").format(value=deathday))

    if place_of_birth:

        lines.append(i18n.T(31506, "Place of birth: {value}").format(value=place_of_birth))

    if known_titles:

        lines.append(i18n.T(31507, "Known for: {value}").format(value=", ".join(known_titles[:8])))

    if biography:

        lines.append("")

        lines.append(biography)



    plot = "\n".join(lines).strip()



    xbmcplugin.setPluginCategory(addon_handle, i18n.T(31502, "TMDB / People / {name}").format(name=name))

    xbmcplugin.setContent(addon_handle, "files")



    li = xbmcgui.ListItem(label=name)

    li.setArt(_tmdb_person_art(item))

    _set_video_info(li, {

        "title": name,

        "plot": plot,

        "mediatype": "video",

    })



    xbmcplugin.addDirectoryItem(

        handle=addon_handle,

        url="",

        listitem=li,

        isFolder=False

    )



    departments = _tmdb_person_departments_with_counts(item)



    if departments:

        for dep in departments:

            dept = dep.get("department") or ""

            label = dep.get("label") or dept or i18n.T(31508, "Filmography")

            count = int(dep.get("count") or 0)



            _add_dir(

                f"{label} ({count})",

                {

                    "action": "tmdb.person.credits.department",

                    "id": str(person_id),

                    "name": name,

                    "department": dept,

                },

                _tmdb_person_department_icon(dept)

            )

    else:

        _placeholder_dir(i18n.T(31502, "TMDB / People / {name}").format(name=name), i18n.T(31509, "(no filmography)"))

        return



    _end("files")



def tmdb_person_credits_menu():

    person_id = _get_param("id", "").strip()

    name = _get_param("name", i18n.T(31500, "Person")).strip() or i18n.T(31500, "Person")



    if not person_id:

        _placeholder_dir(i18n.T(31510, "TMDB / People / Filmography"), i18n.T(31305, "(missing TMDB id)"))

        return



    item = _tmdb_person_detail(person_id)

    if not item:

        _placeholder_dir(i18n.T(31510, "TMDB / People / Filmography"), i18n.T(31313, "({title} / {season}) - data could not be loaded").format(title=name, season=i18n.T(31508, "Filmography")))

        return



    xbmcplugin.setPluginCategory(addon_handle, i18n.T(31511, "TMDB / People / {name} / Filmography").format(name=name))

    xbmcplugin.setContent(addon_handle, "files")



    departments = _tmdb_person_departments_with_counts(item)

    if not departments:

        _placeholder_dir(i18n.T(31511, "TMDB / People / {name} / Filmography").format(name=name), i18n.T(31509, "(no filmography)"))

        return



    for dep in departments:

        dept = dep.get("department") or ""

        label = dep.get("label") or dept or i18n.T(31508, "Filmography")

        count = int(dep.get("count") or 0)



        _add_dir(

            f"{label} ({count})",

            {

                "action": "tmdb.person.credits.department",

                "id": str(person_id),

                "name": name,

                "department": dept,

            },

            _tmdb_person_department_icon(dept)

        )



    _end("files")



def tmdb_person_credits_department_menu():

    person_id = _get_param("id", "").strip()

    name = _get_param("name", i18n.T(31500, "Person")).strip() or i18n.T(31500, "Person")

    department = _get_param("department", "").strip()



    if not person_id:

        _placeholder_dir(i18n.T(31510, "TMDB / People / Filmography"), i18n.T(31305, "(missing TMDB id)"))

        return



    if not department:

        _placeholder_dir(i18n.T(31510, "TMDB / People / Filmography"), i18n.T(31414, "(missing department)"))

        return



    item = _tmdb_person_detail(person_id)

    if not item:

        _placeholder_dir(i18n.T(31510, "TMDB / People / Filmography"), i18n.T(31313, "({title} / {season}) - data could not be loaded").format(title=name, season=i18n.T(31508, "Filmography")))

        return



    credits = (item.get("combined_credits") or {})

    cast_items = credits.get("cast") or []

    crew_items = credits.get("crew") or []



    rows = []

    seen = {}



    if department == "Acting":

        for c in cast_items:

            media_type = (c.get("media_type") or "").strip()

            if media_type not in ("movie", "tv"):

                continue



            key = (media_type, str(c.get("id") or ""))

            role = (c.get("character") or "").strip()



            if key not in seen:

                row = dict(c)

                row["_roles"] = []

                if role:

                    row["_roles"].append(role)

                seen[key] = row

            else:

                if role and role not in seen[key]["_roles"]:

                    seen[key]["_roles"].append(role)



    else:

        for c in crew_items:

            dept = (c.get("department") or "").strip() or "Crew"

            if dept != department:

                continue



            media_type = (c.get("media_type") or "").strip()

            if media_type not in ("movie", "tv"):

                continue



            key = (media_type, str(c.get("id") or ""))

            role = (c.get("job") or "").strip()



            if key not in seen:

                row = dict(c)

                row["_roles"] = []

                if role:

                    row["_roles"].append(role)

                seen[key] = row

            else:

                if role and role not in seen[key]["_roles"]:

                    seen[key]["_roles"].append(role)



    rows = list(seen.values())

    rows.sort(key=_person_credit_sort_key, reverse=True)



    title = _tmdb_person_department_label(department)



    xbmcplugin.setPluginCategory(addon_handle, i18n.T(31512, "TMDB / People / {name} / {department}").format(name=name, department=title))

    xbmcplugin.setContent(addon_handle, "files")



    if not rows:

        _placeholder_dir(i18n.T(31512, "TMDB / People / {name} / {department}").format(name=name, department=title), i18n.T(31415, "(no data)"))

        return



    shown = 0

    for r in rows:

        roles = r.get("_roles") or []

        role_text = ", ".join(roles[:3]).strip()



        if _add_person_credit_result(r, role_text=role_text):

            shown += 1



    if shown == 0:

        _placeholder_dir(i18n.T(31512, "TMDB / People / {name} / {department}").format(name=name, department=title), i18n.T(31513, "(no supported items)"))

        return



    _end("files")



def _person_credit_sort_key(item: dict):

    date_str = (item.get("release_date") or item.get("first_air_date") or "").strip()

    if not date_str:

        return ""

    return date_str



def _tmdb_person_department_order():

    return [

        "Acting",

        "Directing",

        "Writing",

        "Production",

        "Camera",

        "Sound",

        "Editing",

        "Art",

        "Costume & Make-Up",

        "Visual Effects",

        "Lighting",

        "Creator",

        "Crew",

    ]





def _tmdb_person_department_label(department: str) -> str:

    mapping = {

        "Acting": i18n.T(31943),

        "Directing": i18n.T(31944),

        "Writing": i18n.T(31945),

        "Production": i18n.T(31946),

        "Camera": i18n.T(31947),

        "Sound": i18n.T(31948),

        "Editing": i18n.T(31949),

        "Art": i18n.T(31950),

        "Costume & Make-Up": i18n.T(31951),

        "Visual Effects": i18n.T(31952),

        "Lighting": i18n.T(31953),

        "Creator": i18n.T(31954),

        "Crew": i18n.T(31955),

    }

    return mapping.get((department or "").strip(), (department or "").strip() or "Další profese")





def _tmdb_person_department_icon(department: str) -> str:

    dept = (department or "").strip()



    if dept == "Acting":

        return "DefaultActor.png"

    if dept in ("Directing", "Writing", "Production", "Creator"):

        return "DefaultFolder.png"

    if dept in ("Camera", "Visual Effects", "Art", "Costume & Make-Up", "Lighting", "Editing", "Sound"):

        return "DefaultAddonsSearch.png"



    return "DefaultFolder.png"





def _tmdb_person_known_titles(item: dict, limit: int = 8):

    credits = (item.get("combined_credits") or {})

    cast_credits = credits.get("cast") or []

    crew_credits = credits.get("crew") or []



    titles = []



    for bucket in (cast_credits, crew_credits):

        for c in bucket:

            media_type = "movie" if c.get("title") else "tv"
            t = _tmdb_display_title(c, media_type, c.get("title") or c.get("name") or "").strip()

            if t and t not in titles:

                titles.append(t)

            if len(titles) >= limit:

                return titles



    return titles





def _tmdb_person_departments_with_counts(item: dict):

    credits = (item.get("combined_credits") or {})

    cast_credits = credits.get("cast") or []

    crew_credits = credits.get("crew") or []



    counts = {}



    if cast_credits:

        counts["Acting"] = len(cast_credits)



    for c in crew_credits:

        dept = (c.get("department") or "").strip() or "Crew"

        counts[dept] = counts.get(dept, 0) + 1



    order = _tmdb_person_department_order()



    def sort_key(k):

        try:

            idx = order.index(k)

        except ValueError:

            idx = 999

        return (idx, _tmdb_person_department_label(k).lower())



    out = []

    for dept in sorted(counts.keys(), key=sort_key):

        out.append({

            "department": dept,

            "label": _tmdb_person_department_label(dept),

            "count": counts.get(dept, 0),

        })



    return out



def _add_person_credit_result(item: dict, role_text: str = ""):

    media_type = (item.get("media_type") or "").strip()



    if media_type == "movie":

        tmdb_id = item.get("id")

        title = (_tmdb_display_title(item, "movie", _tmdb_untitled()) or _tmdb_untitled()).strip()


        label = _tmdb_label_movie(item, prefix=i18n.T(31939))

        if role_text:

            label += f" — {role_text}"



        li = xbmcgui.ListItem(label=label)

        li.setArt(_tmdb_media_art(item))

        _set_video_info(li, _tmdb_movie_info(item))

        li.setProperty("IsPlayable", "false")

        try:

            status = _tmdb_merged_item_status(tmdb_id)
        except Exception:

            status = ""

        _tmdb_add_movie_watch_context(li, tmdb_id, title, status)



        u = build_url({

            "action": "tmdb.movie.detail",

            "id": str(tmdb_id or ""),

            "title": title,

        })



        xbmcplugin.addDirectoryItem(

            handle=addon_handle,

            url=u,

            listitem=li,

            isFolder=True

        )

        return True



    if media_type == "tv":

        tmdb_id = item.get("id")

        title = (_tmdb_display_title(item, "tv", _tmdb_untitled()) or _tmdb_untitled()).strip()


        label = _tmdb_label_tv(item, prefix=i18n.T(31940))

        if role_text:

            label += f" — {role_text}"



        li = xbmcgui.ListItem(label=label)

        li.setArt(_tmdb_media_art(item))

        _set_video_info(li, _tmdb_tv_info(item))

        li.setProperty("IsPlayable", "false")

        try:

            status = _tmdb_merged_item_status(tmdb_id)
        except Exception:

            status = ""

        _tmdb_add_tv_watch_context(li, tmdb_id, title, status)



        u = build_url({

            "action": "tmdb.tv.detail",

            "id": str(tmdb_id or ""),

            "title": title,

        })



        xbmcplugin.addDirectoryItem(

            handle=addon_handle,

            url=u,

            listitem=li,

            isFolder=True

        )

        return True



    return False



def _tmdb_media_credit_departments():

    return [

        ("Directing", i18n.T(31944), "DefaultFolder.png"),

        ("Writing", i18n.T(31945), "DefaultFolder.png"),

        ("Production", i18n.T(31946), "DefaultFolder.png"),

        ("Camera", i18n.T(31947), "DefaultAddonsSearch.png"),

        ("Sound", i18n.T(31948), "DefaultAddonsSearch.png"),

    ]





def _tmdb_media_department_label(department: str) -> str:

    for dep, label, _icon in _tmdb_media_credit_departments():

        if dep == (department or "").strip():

            return label

    return (department or "").strip() or "Další"









def _tmdb_media_crew_departments_with_counts(item: dict):

    credits = (item.get("credits") or {})

    crew = credits.get("crew") or []



    counts = {}

    for person in crew:

        dept = (person.get("department") or "").strip()

        if dept not in ("Directing", "Writing", "Production", "Camera", "Sound"):

            continue

        counts[dept] = counts.get(dept, 0) + 1



    out = []

    for dep, label, icon in _tmdb_media_credit_departments():

        count = counts.get(dep, 0)

        if count > 0:

            out.append({

                "department": dep,

                "label": label,

                "icon": icon,

                "count": count,

            })



    return out



def _tmdb_media_cast_count(item: dict) -> int:

    credits = (item.get("credits") or {})

    cast = credits.get("cast") or []

    return len(cast)



def _tmdb_person_name_sort_key(item: dict):

    return (item.get("name") or "").strip().lower()





def _add_tmdb_person_folder_item(person: dict, role_text: str = ""):

    person_id = person.get("id")

    name = (person.get("name") or "").strip() or i18n.T(31845, "Unknown name")

    profile_path = (person.get("profile_path") or "").strip()



    label = name

    if role_text:

        label += f" — {role_text}"



    li = xbmcgui.ListItem(label=label)



    art = {

        "icon": "DefaultActor.png",

        "thumb": "DefaultActor.png",

        "poster": "DefaultActor.png",

    }

    if profile_path:

        full = TMDB_IMAGE_BASE + profile_path

        art["icon"] = full

        art["thumb"] = full

        art["poster"] = full



    li.setArt(art)

    li.setProperty("IsPlayable", "false")



    u = build_url({

        "action": "tmdb.person.detail",

        "id": str(person_id or ""),

        "name": name,

    })



    xbmcplugin.addDirectoryItem(

        handle=addon_handle,

        url=u,

        listitem=li,

        isFolder=True

    )



def tmdb_person_credits_movies_menu():

    person_id = _get_param("id", "").strip()

    name = _get_param("name", i18n.T(31500, "Person")).strip() or i18n.T(31500, "Person")



    if not person_id:

        _placeholder_dir(i18n.T(31514, "TMDB / People / Filmography / Movies"), i18n.T(31305, "(missing TMDB id)"))

        return



    item = _tmdb_person_detail(person_id)

    if not item:

        _placeholder_dir(i18n.T(31514, "TMDB / People / Filmography / Movies"), i18n.T(31313, "({title} / {season}) - data could not be loaded").format(title=name, season=i18n.T(31508, "Filmography")))

        return



    credits = (item.get("combined_credits") or {})

    cast_items = credits.get("cast") or []



    movies = []

    seen = set()



    for c in cast_items:

        if c.get("media_type") != "movie":

            continue



        cid = c.get("credit_id") or f"{c.get('id')}::{c.get('character','')}"

        if cid in seen:

            continue

        seen.add(cid)

        movies.append(c)



    movies.sort(key=_person_credit_sort_key, reverse=True)



    xbmcplugin.setPluginCategory(addon_handle, i18n.T(31515, "TMDB / People / {name} / Filmography / Movies").format(name=name))

    xbmcplugin.setContent(addon_handle, "movies")



    if not movies:

        _placeholder_dir(i18n.T(31515, "TMDB / People / {name} / Filmography / Movies").format(name=name), i18n.T(31516, "(no movie roles)"))

        return



    for m in movies:

        title = (_tmdb_display_title(m, "movie", _tmdb_untitled()) or _tmdb_untitled()).strip()

        release_date = (m.get("release_date") or "").strip()

        year = release_date[:4] if len(release_date) >= 4 else ""

        character = (m.get("character") or "").strip()

        rating = m.get("vote_average") or 0



        label = title

        meta = []

        if year:

            meta.append(year)

        if character:

            meta.append(character)

        try:

            if float(rating) > 0:

                meta.append("TMDB %.1f" % float(rating))

        except Exception:

            pass



        if meta:

            label += "  [" + " | ".join(meta) + "]"



        li = xbmcgui.ListItem(label=label)

        li.setArt(_tmdb_media_art(m))

        _set_video_info(li, _tmdb_movie_info(m))

        tmdb_id = m.get("id")

        try:

            status = _tmdb_merged_item_status(tmdb_id)
        except Exception:

            status = ""

        _tmdb_add_movie_watch_context(li, tmdb_id, title, status)



        u = build_url({

            "action": "tmdb.movie.detail",

            "id": str(tmdb_id or ""),

            "title": title,

        })



        xbmcplugin.addDirectoryItem(

            handle=addon_handle,

            url=u,

            listitem=li,

            isFolder=True

        )



    _end("movies")



def tmdb_person_credits_tv_menu():

    person_id = _get_param("id", "").strip()

    name = _get_param("name", i18n.T(31500, "Person")).strip() or i18n.T(31500, "Person")



    if not person_id:

        _placeholder_dir(i18n.T(31517, "TMDB / People / Filmography / Series"), i18n.T(31305, "(missing TMDB id)"))

        return



    item = _tmdb_person_detail(person_id)

    if not item:

        _placeholder_dir(i18n.T(31517, "TMDB / People / Filmography / Series"), i18n.T(31313, "({title} / {season}) - data could not be loaded").format(title=name, season=i18n.T(31508, "Filmography")))

        return



    credits = (item.get("combined_credits") or {})

    cast_items = credits.get("cast") or []



    tv_items = []

    seen = set()



    for c in cast_items:

        if c.get("media_type") != "tv":

            continue



        cid = c.get("credit_id") or f"{c.get('id')}::{c.get('character','')}"

        if cid in seen:

            continue

        seen.add(cid)

        tv_items.append(c)



    tv_items.sort(key=_person_credit_sort_key, reverse=True)



    xbmcplugin.setPluginCategory(addon_handle, i18n.T(31518, "TMDB / People / {name} / Filmography / Series").format(name=name))

    xbmcplugin.setContent(addon_handle, "tvshows")



    if not tv_items:

        _placeholder_dir(i18n.T(31518, "TMDB / People / {name} / Filmography / Series").format(name=name), i18n.T(31519, "(no series roles)"))

        return



    for t in tv_items:

        title = (_tmdb_display_title(t, "tv", _tmdb_untitled()) or _tmdb_untitled()).strip()

        first_air_date = (t.get("first_air_date") or "").strip()

        year = first_air_date[:4] if len(first_air_date) >= 4 else ""

        character = (t.get("character") or "").strip()

        rating = t.get("vote_average") or 0



        label = title

        meta = []

        if year:

            meta.append(year)

        if character:

            meta.append(character)

        try:

            if float(rating) > 0:

                meta.append("TMDB %.1f" % float(rating))

        except Exception:

            pass



        if meta:

            label += "  [" + " | ".join(meta) + "]"



        li = xbmcgui.ListItem(label=label)

        li.setArt(_tmdb_media_art(t))

        _set_video_info(li, _tmdb_tv_info(t))

        tmdb_id = t.get("id")

        try:

            status = _tmdb_merged_item_status(tmdb_id)
        except Exception:

            status = ""

        _tmdb_add_tv_watch_context(li, tmdb_id, title, status)



        u = build_url({

            "action": "tmdb.tv.detail",

            "id": str(tmdb_id or ""),

            "title": title,

        })



        xbmcplugin.addDirectoryItem(

            handle=addon_handle,

            url=u,

            listitem=li,

            isFolder=True

        )



    _end("tvshows")



def tmdb_stub():

    section = _get_param("section", "")

    limit = _get_page_limit()



    if section == "movies_now_playing":

        tmdb_movies_now_playing_list()

        return



    if section == "movies_upcoming":

        tmdb_movies_upcoming_list()

        return



    names = {

        "movies_now_playing": i18n.T(31126, "TMDB / Movies / News / Now playing"),

        "movies_upcoming": i18n.T(31127, "TMDB / Movies / News / Upcoming"),

    }



    title = names.get(section, "TMDB")

    _placeholder_dir(title, i18n.T(31619, "(page limit: {limit})").format(limit=limit))





def _tmdb_custom_item_from_params():

    media_type = _get_param("media_type", "").strip().lower()

    tmdb_id = _get_param("id", "").strip()

    title_param = _get_param("title", "").strip()

    if media_type not in ("movie", "tv") or not tmdb_id:

        return {}



    data = {}

    if media_type == "movie":

        data = _tmdb_get(f"/movie/{tmdb_id}", params={"language": TMDB_LANGUAGE}, timeout=20) or {}

        title = _tmdb_display_title(data, "movie", title_param or _tmdb_untitled()) or title_param or _tmdb_untitled()

        original = _tmdb_hide_unreadable_text(data.get("original_title") or "")

        premiered = data.get("release_date") or ""

        fallback_icon = "DefaultMovies.png"

    else:

        data = _tmdb_get(f"/tv/{tmdb_id}", params={"language": TMDB_LANGUAGE}, timeout=20) or {}

        title = _tmdb_display_title(data, "tv", title_param or _tmdb_untitled()) or title_param or _tmdb_untitled()

        original = _tmdb_hide_unreadable_text(data.get("original_name") or "")

        premiered = data.get("first_air_date") or ""

        fallback_icon = "DefaultTVShows.png"



    year = premiered[:4] if len(premiered) >= 4 and premiered[:4].isdigit() else ""

    art = _tmdb_media_art(data) if data else {}



    return {

        "media_type": media_type,

        "tmdb_id": tmdb_id,

        "title": title,

        "original_title": original,

        "year": year,

        "poster": art.get("poster") or fallback_icon,

        "plot": data.get("overview") or "",

        "rating": data.get("vote_average") or 0,

    }





def tmdb_custom_lists_menu():
    xbmcplugin.setPluginCategory(addon_handle, _tmdb_my_lists_title())
    xbmcplugin.setContent(addon_handle, "files")

    _add_action_item(i18n.T(31779, "New list"), {"action": "tmdb.custom.list.create"}, "DefaultAddSource.png")
    with diagnostics.action_scope("tmdb.phase", "tmdb.custom.lists.trakt.overview"):
        try:
            if trakt_client.is_connected():
                result = tmdb_trakt_list_sync.sync_now(fetch_items=False, push_items=False)
                try:
                    xbmc.log(
                        "[IKARUS][TRAKT LIST SYNC] custom lists menu overview "
                        f"mode={result.get('mode')} linked={result.get('linked')} "
                        f"removed={result.get('removed_remote_lists')}",
                        xbmc.LOGINFO,
                    )
                except Exception:
                    pass
        except Exception as e:
            try:
                xbmc.log(f"[IKARUS][TRAKT LIST SYNC] custom lists menu overview failed: {e}", xbmc.LOGWARNING)
            except Exception:
                pass
    with diagnostics.action_scope("tmdb.phase", "tmdb.custom.lists.load"):
        rows = tmdb_custom_lists.all_lists()
    if not rows:

        li = xbmcgui.ListItem(label=i18n.T(31880, "No custom lists have been created yet"))
        li.setArt({"icon": "DefaultFolder.png", "thumb": "DefaultFolder.png", "poster": "DefaultFolder.png"})

        xbmcplugin.addDirectoryItem(handle=addon_handle, url="", listitem=li, isFolder=False)

        _end("files")

        return



    for row in rows:
        list_id = str(row.get("id") or "").strip()
        name = str(row.get("name") or i18n.T(31723, "List")).strip()
        local_count = len(row.get("items") or [])
        count = local_count
        try:
            if str(row.get("trakt_slug") or "").strip() and row.get("trakt_item_count") not in (None, ""):
                count = int(row.get("trakt_item_count") or 0)
        except Exception:
            count = local_count
        li = xbmcgui.ListItem(label=f"{name} ({count})")
        li.setArt({"icon": "DefaultVideoPlaylists.png", "thumb": "DefaultVideoPlaylists.png", "poster": "DefaultVideoPlaylists.png"})
        try:
            plot = str(row.get("trakt_description") or "").strip()
            if plot:
                _set_video_info(li, {"title": name, "plot": plot})
        except Exception:
            pass
        try:
            menu_items = [
            ]
            can_rename, rename_mode = tmdb_custom_lists.can_rename_list(row)

            if can_rename or rename_mode != "owner_only":

                menu_items.append(

                    (

                        i18n.T(31853, "Rename list"),

                        "RunPlugin(%s)" % build_url({

                            "action": "tmdb.custom.list.rename",

                            "list_id": list_id,

                            "name": name,

                        })

                    )

                )

            can_delete, delete_mode = tmdb_custom_lists.can_delete_list(row)

            if can_delete or delete_mode != "owner_only":

                menu_items.append(

                    (

                        i18n.T(31854, "Delete list"),

                        "RunPlugin(%s)" % build_url({

                            "action": "tmdb.custom.list.delete",

                            "list_id": list_id,

                            "name": name,

                        })

                    )

                )

            li.addContextMenuItems(menu_items, replaceItems=False)

        except Exception:

            pass

        xbmcplugin.addDirectoryItem(

            handle=addon_handle,

            url=build_url({"action": "tmdb.custom.list", "list_id": list_id}),

            listitem=li,

            isFolder=True

        )



    _end("files")


def _trakt_public_list_from_row(row: dict) -> dict:
    if not isinstance(row, dict):
        return {}
    list_row = row.get("list") if isinstance(row.get("list"), dict) else row
    user_row = list_row.get("user") if isinstance(list_row.get("user"), dict) else {}
    ids = list_row.get("ids") if isinstance(list_row.get("ids"), dict) else {}
    return {
        "name": str(list_row.get("name") or i18n.T(31724, "Trakt.TV list")).strip(),
        "slug": str(ids.get("slug") or "").strip(),
        "trakt_id": str(ids.get("trakt") or "").strip(),
        "username": str(user_row.get("username") or "").strip(),
        "item_count": list_row.get("item_count"),
        "description": str(list_row.get("description") or "").strip(),
        "likes": row.get("like_count", row.get("likes", list_row.get("likes", ""))),
        "comments": row.get("comment_count", row.get("comments", list_row.get("comment_count", ""))),
        "type": str(list_row.get("type") or "").strip().lower(),
    }


def _trakt_first_image_url(value) -> str:
    if isinstance(value, str):
        text = value.strip()
        return text if text.lower().startswith(("http://", "https://")) else ""
    if isinstance(value, list):
        for item in value:
            found = _trakt_first_image_url(item)
            if found:
                return found
        return ""
    if isinstance(value, dict):
        priority = (
            "full", "medium", "thumb", "url", "poster", "posters",
            "fanart", "background", "backdrop", "backdrops"
        )
        for key in priority:
            found = _trakt_first_image_url(value.get(key))
            if found:
                return found
        for item in value.values():
            found = _trakt_first_image_url(item)
            if found:
                return found
    return ""


def tmdb_trakt_lists_menu():
    title = i18n.T(31703, "TMDB / Trakt.TV lists")
    xbmcplugin.setPluginCategory(addon_handle, title)

    if not trakt_client.is_connected():
        _empty_result_dir(title, text=_tmdb_empty_not_logged_text())
        return

    _add_main_style_dir(i18n.T(30430, "Ve\u0159ejn\u00e9 seznamy"), {"action": "tmdb.trakt.public.lists", "page": "1"}, "DefaultVideoPlaylists.png")
    _add_main_style_dir(i18n.T(30431, "Obl\u00edben\u00e9 ve\u0159ejn\u00e9 seznamy"), {"action": "tmdb.trakt.public.favorites"}, "DefaultFavourites.png")
    xbmcplugin.endOfDirectory(addon_handle)


def tmdb_playback_trakt_sync():
    title = i18n.T(31708, "TMDB / Watched and in progress")
    if not trakt_client.is_connected():
        xbmcgui.Dialog().notification(title, _tmdb_not_trakt_logged_text(), xbmcgui.NOTIFICATION_WARNING, 3000, sound=False)
        return

    ok = xbmcgui.Dialog().yesno(
        title,
        i18n.T(31780, "Upload locally saved watched/in-progress items to Trakt.TV and sync Trakt state back to Ikarus?"),
        nolabel=i18n.T(31848, "Back"),
        yeslabel=i18n.T(31858, "Synchronize"),
    )
    if not ok:
        return

    progress = xbmcgui.DialogProgress()
    try:
        progress.create(title, i18n.T(31788))
    except Exception:
        progress = None

    try:
        if progress:
            progress.update(10, i18n.T(31881, "Preparing local state..."))
        tmdb_playback_sync.sync_local_with_trakt(force=True, push_local=True)
        summary = tmdb_playback_sync.last_sync_summary()
        pushed = int(summary.get("last_pushed_count") or 0) if isinstance(summary, dict) else 0
        skipped = int(summary.get("last_skipped_existing_count") or 0) if isinstance(summary, dict) else 0
        errors = str((summary or {}).get("last_sync_error") or "").strip() if isinstance(summary, dict) else ""
        if progress:
            progress.update(100, i18n.T(31852, "Done."))
    except Exception as e:
        try:
            if progress:
                progress.close()
        except Exception:
            pass
        xbmcgui.Dialog().notification(title, i18n.T(31819, "Sync failed: {error}").format(error=e), xbmcgui.NOTIFICATION_ERROR, 4000, sound=False)
        return
    finally:
        try:
            if progress:
                progress.close()
        except Exception:
            pass

    if errors:
        xbmcgui.Dialog().notification(title, i18n.T(31781, "Some items could not be sent. We will try again next time."), xbmcgui.NOTIFICATION_WARNING, 4000, sound=False)
    else:
        xbmcgui.Dialog().notification(
            title,
            i18n.T(31820, "Sync completed. Sent: {pushed}, existing: {skipped}").format(pushed=pushed, skipped=skipped),
            xbmcgui.NOTIFICATION_INFO,
            3500,
            sound=False,
        )
    try:
        xbmc.executebuiltin("Container.Refresh")
    except Exception:
        pass


def _trakt_smart_section_label(section: str) -> str:
    section = str(section or "").strip().lower()
    if section == "movies":
        return "Filmy"
    if section == "shows":
        return "Seri\u00e1ly"
    if section == "search":
        return "Media"
    return "Filtr"


def _trakt_page_nav_label(label: str, target_page: int, total_pages: int = 0) -> str:
    try:
        target_page = max(1, int(target_page or 1))
    except Exception:
        target_page = 1
    try:
        total_pages = int(total_pages or 0)
    except Exception:
        total_pages = 0
    if total_pages > 0:
        return f"{label} ({target_page}/{total_pages})"
    return f"{label} ({target_page})"


def _trakt_first_page_parent_query(action: str, extra: dict = None) -> dict:
    action = str(action or "").strip()
    extra = dict(extra or {})

    if action in ("tmdb.trakt.watched", "tmdb.trakt.started"):
        return {"action": "tmdb.trakt.lists"}
    if action == "tmdb.trakt.smart.list":
        return {"action": "tmdb.trakt.smart.lists"}
    if action == "tmdb.trakt.public.lists":
        return {"action": "tmdb.trakt.public.lists"}
    if action == "tmdb.trakt.list":
        return_action = str(extra.get("return_action") or "").strip()
        if return_action == "tmdb.trakt.public.lists":
            parent = {"action": return_action}
            if extra.get("return_mode"):
                parent["mode"] = str(extra.get("return_mode") or "")
            if extra.get("return_query"):
                parent["query"] = str(extra.get("return_query") or "")
            if extra.get("return_page"):
                parent["page"] = str(extra.get("return_page") or "1")
            return parent
        if return_action == "tmdb.trakt.user.lists":
            return {"action": return_action}
        if return_action == "tmdb.trakt.public.favorites":
            return {"action": return_action}
        if str(extra.get("user") or "").strip().lower() == "me":
            return {"action": "tmdb.trakt.user.lists"}
        return {"action": "tmdb.trakt.public.lists"}
    return {}


def _trakt_add_top_page_nav(action: str, page: int, total_pages: int = 0, extra: dict = None):
    try:
        page = max(1, int(page or 1))
    except Exception:
        page = 1
    extra = dict(extra or {})

    if page >= 3:
        first_query = {"action": action, "page": "1"}
        first_query.update(extra)
        first_query["replace_listing"] = "1"
        _add_tmdb_page_item(_trakt_page_nav_label("Zp\u011bt na prvn\u00ed str\u00e1nku", 1, total_pages), first_query, "DefaultFolderBack.png")

    if page > 1:
        prev_query = {"action": action, "page": str(page - 1)}
        prev_query.update(extra)
        prev_query["replace_listing"] = "1"
        _add_tmdb_page_item(_trakt_page_nav_label("Predchozi stranka", page - 1, total_pages), prev_query, "DefaultFolderBack.png")


def _trakt_add_bottom_page_nav(action: str, next_page: int, total_pages: int = 0, extra: dict = None):
    try:
        next_page = max(1, int(next_page or 1))
    except Exception:
        next_page = 1
    extra = dict(extra or {})
    next_query = {"action": action, "page": str(next_page)}
    next_query.update(extra)
    next_query["replace_listing"] = "1"
    _add_tmdb_page_item(_trakt_page_nav_label("Dal\u0161\u00ed str\u00e1nka", next_page, total_pages), next_query, "DefaultFolder.png")


def _trakt_status_prewarm_async(min_interval: float = 45.0):
    global _TRAKT_STATUS_PREWARM_RUNNING, _TRAKT_STATUS_PREWARM_TS
    now = time.time()
    if _TRAKT_STATUS_PREWARM_RUNNING or (now - _TRAKT_STATUS_PREWARM_TS) < float(min_interval or 45.0):
        return
    _TRAKT_STATUS_PREWARM_RUNNING = True
    _TRAKT_STATUS_PREWARM_TS = now

    def _run():
        global _TRAKT_STATUS_PREWARM_RUNNING, _TRAKT_STATUS_PREWARM_TS
        try:
            if trakt_client.is_connected():
                with diagnostics.action_scope("tmdb.phase", "tmdb.trakt.status.prewarm"):
                    tmdb_playback_sync.trakt_state()
        except Exception as e:
            try:
                xbmc.log(f"[IKARUS][TRAKT] status prewarm failed: {e}", xbmc.LOGWARNING)
            except Exception:
                pass
        finally:
            _TRAKT_STATUS_PREWARM_TS = time.time()
            _TRAKT_STATUS_PREWARM_RUNNING = False

    try:
        thread = threading.Thread(target=_run, name="IkarusTraktStatusPrewarm")
        thread.daemon = True
        thread.start()
    except Exception:
        _TRAKT_STATUS_PREWARM_RUNNING = False


def tmdb_trakt_smart_lists_menu():
    title = i18n.T(31704, "TMDB / My lists / Smart lists")
    xbmcplugin.setPluginCategory(addon_handle, title)

    if not trakt_client.is_connected():
        _empty_result_dir(title, text=_tmdb_empty_not_logged_text())
        return

    _trakt_status_prewarm_async()
    _add_action_item(i18n.T(31833, "Creating a new smart list is currently only available on the Trakt.TV website"), {"action": "noop"}, "DefaultAddonProgram.png")

    rows = []
    for section in ("search", "movies", "shows"):
        with diagnostics.action_scope("tmdb.phase", "tmdb.trakt.smart.lists.fetch", {"section": section}):
            try:
                for row in trakt_client.fetch_saved_filters(section):
                    if isinstance(row, dict):
                        item = dict(row)
                        item["section"] = str(item.get("section") or section).strip().lower()
                        rows.append(item)
            except Exception as e:
                try:
                    xbmc.log(f"[IKARUS][TRAKT] saved filters load error section={section}: {e}", xbmc.LOGWARNING)
                except Exception:
                    pass

    if not rows:
        li = xbmcgui.ListItem(label=i18n.T(31882, "No smart lists are defined on Trakt.TV."))
        li.setArt({"icon": "DefaultFolder.png", "thumb": "DefaultFolder.png", "poster": "DefaultFolder.png"})
        xbmcplugin.addDirectoryItem(handle=addon_handle, url="", listitem=li, isFolder=False)
        _end("files")
        return

    def _sort_key(row):
        try:
            rank = int(row.get("rank") or 0)
        except Exception:
            rank = 0
        return (_trakt_smart_section_label(row.get("section")), rank, str(row.get("name") or "").lower())

    added = 0
    with diagnostics.action_scope("tmdb.phase", "tmdb.trakt.smart.lists.rows", {"count": len(rows)}):
        for row in sorted(rows, key=_sort_key):
            name = str(row.get("name") or i18n.T(31883, "Smart list")).strip()
            path = str(row.get("path") or "").strip()
            query = str(row.get("query") or "").strip()
            section = str(row.get("section") or "").strip().lower()
            if not name or not path:
                continue
            label = f"{name} [COLOR gray]({_trakt_smart_section_label(section)})[/COLOR]"
            _add_main_style_dir(
                label,
                {
                    "action": "tmdb.trakt.smart.list",
                    "name": name,
                    "section": section,
                    "path": path,
                    "query": query,
                    "page": "1",
                },
                "DefaultVideoPlaylists.png",
                title=name,
                plot=(f"path={path}\nquery={query}" if query else f"path={path}"),
            )
            added += 1

    if added == 0:
        _empty_result_dir(title, text=i18n.T(31884, "Smart lists do not have a valid address."))
        return

    xbmcplugin.endOfDirectory(addon_handle)


def tmdb_trakt_smart_list_create():
    if not trakt_client.is_connected():
        xbmcgui.Dialog().notification(i18n.T(31752, "TMDB / Smart lists"), _tmdb_not_trakt_logged_text(), xbmcgui.NOTIFICATION_WARNING, 3000)
        return

    name = xbmcgui.Dialog().input(i18n.T(31753, "TMDB / Smart lists / New list"), type=xbmcgui.INPUT_ALPHANUM)
    if not name or not name.strip():
        return
    name = name.strip()

    mode_options = ["Jednoduchy pruvodce", "Vlastni URL"]
    mode_choice = xbmcgui.Dialog().contextmenu(mode_options)
    if mode_choice < 0:
        return

    url = ""
    if mode_choice == 0:
        section_options = ["Filmy", "Seri\u00e1ly"]
        section_choice = xbmcgui.Dialog().contextmenu(section_options)
        if section_choice < 0:
            return

        target_options = [i18n.T(31742), i18n.T(31741), i18n.T(31980)]
        target_choice = xbmcgui.Dialog().contextmenu(target_options)
        if target_choice < 0:
            return

        section_prefix = "/movies" if section_choice == 0 else "/shows"
        target_suffix = {
            0: "/trending",
            1: "/popular",
            2: "/anticipated",
        }.get(target_choice, "/trending")
        base_path = f"{section_prefix}{target_suffix}"

        raw_query = xbmcgui.Dialog().input(
            i18n.T(31754, "Filter parameters after ? or the full builder URL from Trakt.TV"),
            type=xbmcgui.INPUT_ALPHANUM,
        )
        if raw_query is None:
            return
        raw_query = str(raw_query or "").strip()
        if "?" in raw_query:
            raw_query = raw_query.split("?", 1)[1].strip()
        raw_query = raw_query.lstrip("?&").strip()

        url = base_path + (f"?{raw_query}" if raw_query else "")
    else:
        url = xbmcgui.Dialog().input(i18n.T(31755, "URL from the Create smart list window on Trakt.TV"), type=xbmcgui.INPUT_ALPHANUM)
        if not url or not url.strip():
            return
        url = url.strip()
        if not trakt_client.is_supported_saved_filter_url(url):
            xbmcgui.Dialog().notification(
                i18n.T(31752, "TMDB / Smart lists"),
                i18n.T(31756, "Use the URL from the Create smart list window on Trakt.TV."),
                xbmcgui.NOTIFICATION_WARNING,
                4000,
            )
            return

    try:
        result = trakt_client.create_saved_filter(name, url)
    except Exception as e:
        try:
            xbmc.log(f"[IKARUS][TRAKT] create saved filter error: {e}", xbmc.LOGWARNING)
        except Exception:
            pass
        detail = str(e or "").strip()
        if detail:
            xbmcgui.Dialog().notification(i18n.T(31752, "TMDB / Smart lists"), i18n.T(31821, "Trakt.TV error: {error}").format(error=detail[:120]), xbmcgui.NOTIFICATION_ERROR, 4500)
        else:
            xbmcgui.Dialog().notification(i18n.T(31752, "TMDB / Smart lists"), i18n.T(31757, "The smart list could not be created."), xbmcgui.NOTIFICATION_ERROR, 3500)
        return

    added = result.get("added") if isinstance(result, dict) else []
    skipped = result.get("skipped") if isinstance(result, dict) else []
    if isinstance(added, list) and added:
        xbmcgui.Dialog().notification(i18n.T(31752, "TMDB / Smart lists"), i18n.T(31758, "The smart list was created."), xbmcgui.NOTIFICATION_INFO, 2500)
        try:
            xbmc.executebuiltin("Container.Refresh")
        except Exception:
            pass
        return

    if isinstance(skipped, list) and skipped:
        xbmcgui.Dialog().notification(i18n.T(31752, "TMDB / Smart lists"), i18n.T(31759, "Trakt skipped the smart list. Check the filter URL."), xbmcgui.NOTIFICATION_WARNING, 3500)
        return

    xbmcgui.Dialog().notification(i18n.T(31752, "TMDB / Smart lists"), i18n.T(31760, "Trakt did not create the smart list."), xbmcgui.NOTIFICATION_WARNING, 3000)


def tmdb_trakt_user_lists_menu():
    title = i18n.T(31705, "TMDB / Trakt.TV / My lists")
    xbmcplugin.setPluginCategory(addon_handle, title)

    if not trakt_client.is_connected():
        _empty_result_dir(title, text=_tmdb_empty_not_logged_text())
        return

    _add_main_style_action_item(i18n.T(31779, "New list"), {"action": "tmdb.trakt.list.create"}, "DefaultAddSource.png")

    try:
        rows = trakt_client.fetch_user_lists()
    except Exception as e:
        try:
            xbmc.log(f"[IKARUS][TRAKT] user list load error: {e}", xbmc.LOGWARNING)
        except Exception:
            pass
        _empty_result_dir(title, text=i18n.T(31979))
        return

    if not rows:
        _empty_result_dir(title, text=_tmdb_empty_list_text())
        return

    added = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        ids = row.get("ids") if isinstance(row.get("ids"), dict) else {}
        name = str(row.get("name") or i18n.T(31724, "Trakt.TV list")).strip()
        slug = str(ids.get("slug") or "").strip()
        list_type = str(row.get("type") or "").strip().lower()
        privacy = str(row.get("privacy") or "private").strip().lower()
        try:
            item_count = int(row.get("item_count") or 0)
        except Exception:
            item_count = 0
        if not slug:
            continue

        label = f"{name}  [COLOR gray]({i18n.T(31902).format(count=item_count)})[/COLOR]"
        plot = str(row.get("description") or "").strip()
        if item_count:
            plot = (plot + "\n" if plot else "") + i18n.T(31907).format(count=item_count)
        context_items = [
                (
                    i18n.T(31859, "Edit Trakt list"),
                    "RunPlugin(%s)" % build_url({
                        "action": "tmdb.trakt.list.edit",
                        "slug": slug,
                        "name": name,
                        "description": plot,
                        "privacy": privacy,
                    })
                ),
                (
                    i18n.T(31860, "Delete Trakt list"),
                    "RunPlugin(%s)" % build_url({
                        "action": "tmdb.trakt.list.delete",
                        "slug": slug,
                        "name": name,
                    })
                ),
            ]
        _add_main_style_dir(
            label,
            {
                "action": "tmdb.trakt.list",
                "user": "me",
                "slug": slug,
                "name": name,
                "count": str(item_count or ""),
                "media_type": list_type,
                "return_action": "tmdb.trakt.user.lists",
                "page": "1",
            },
            "DefaultVideoPlaylists.png",
            title=name,
            plot=plot,
            context_items=context_items,
        )
        added += 1

    if added == 0:
        _empty_result_dir(title, text=_tmdb_empty_list_text())
        return

    xbmcplugin.endOfDirectory(addon_handle)


def tmdb_trakt_list_create():
    if not trakt_client.is_connected():
        xbmcgui.Dialog().notification(_tmdb_trakt_title(), _tmdb_not_trakt_logged_text(), xbmcgui.NOTIFICATION_WARNING, 3000)
        return

    name = xbmcgui.Dialog().input(i18n.T(31711, "TMDB / Trakt.TV / New list"), type=xbmcgui.INPUT_ALPHANUM)
    if not name or not name.strip():
        return
    name = name.strip()

    description = xbmcgui.Dialog().input(i18n.T(31761, "List description (optional)"), type=xbmcgui.INPUT_ALPHANUM)
    description = (description or "").strip()

    privacy_options = ["Soukrom\u00fd", "Ve\u0159ejn\u00fd", "Pro p\u0159\u00e1tele"]
    privacy_values = ["private", "public", "friends"]
    privacy_choice = xbmcgui.Dialog().contextmenu(privacy_options)
    if privacy_choice < 0:
        return
    privacy = privacy_values[privacy_choice]

    try:
        row = trakt_client.create_user_list(name, description=description, privacy=privacy)
    except Exception as e:
        try:
            xbmc.log(f"[IKARUS][TRAKT] create list error: {e}", xbmc.LOGWARNING)
        except Exception:
            pass
        xbmcgui.Dialog().notification(_tmdb_trakt_title(), i18n.T(31725, "The list could not be created."), xbmcgui.NOTIFICATION_ERROR, 3500)
        return

    if row:
        xbmcgui.Dialog().notification(_tmdb_trakt_title(), i18n.T(31726, "The list was created."), xbmcgui.NOTIFICATION_INFO, 2500)
        try:
            xbmc.executebuiltin("Container.Refresh")
        except Exception:
            pass
    else:
        xbmcgui.Dialog().notification(_tmdb_trakt_title(), i18n.T(31727, "Trakt did not create the list."), xbmcgui.NOTIFICATION_WARNING, 3000)


def tmdb_trakt_list_edit():
    slug = _get_param("slug", "").strip()
    old_name = _get_param("name", i18n.T(31723, "List")).strip() or i18n.T(31723, "List")
    old_description = _get_param("description", "").strip()
    old_privacy = _get_param("privacy", "private").strip().lower()
    if old_privacy not in ("private", "public", "friends"):
        old_privacy = "private"
    if not slug:
        xbmcgui.Dialog().notification(_tmdb_trakt_title(), i18n.T(31728, "The list could not be edited."), xbmcgui.NOTIFICATION_ERROR, 3000)
        return
    if not trakt_client.is_connected():
        xbmcgui.Dialog().notification(_tmdb_trakt_title(), _tmdb_not_trakt_logged_text(), xbmcgui.NOTIFICATION_WARNING, 3000)
        return

    name = xbmcgui.Dialog().input(i18n.T(31712, "TMDB / Trakt.TV / Edit title"), defaultt=old_name, type=xbmcgui.INPUT_ALPHANUM)
    if not name or not name.strip():
        return
    name = name.strip()

    description = xbmcgui.Dialog().input(i18n.T(31762, "Edit description"), defaultt=old_description, type=xbmcgui.INPUT_ALPHANUM)
    description = (description or "").strip()

    all_options = [
        ("private", "Soukromy"),
        ("public", "Ve\u0159ejn\u00fd"),
        ("friends", "Pro pratele"),
    ]
    ordered = [item for item in all_options if item[0] == old_privacy]
    ordered.extend([item for item in all_options if item[0] != old_privacy])
    privacy_choice = xbmcgui.Dialog().contextmenu([item[1] for item in ordered])
    if privacy_choice < 0:
        return
    privacy = ordered[privacy_choice][0]

    try:
        row = trakt_client.update_user_list(slug, name, description=description, privacy=privacy)
    except Exception as e:
        try:
            xbmc.log(f"[IKARUS][TRAKT] edit list error: {e}", xbmc.LOGWARNING)
        except Exception:
            pass
        xbmcgui.Dialog().notification(_tmdb_trakt_title(), i18n.T(31729, "Editing the list failed."), xbmcgui.NOTIFICATION_ERROR, 3500)
        return

    if row:
        xbmcgui.Dialog().notification(_tmdb_trakt_title(), i18n.T(31730, "The list was edited."), xbmcgui.NOTIFICATION_INFO, 2500)
        try:
            xbmc.executebuiltin("Container.Refresh")
        except Exception:
            pass
    else:
        xbmcgui.Dialog().notification(_tmdb_trakt_title(), i18n.T(31731, "Trakt did not edit the list."), xbmcgui.NOTIFICATION_WARNING, 3000)


def tmdb_trakt_list_delete():
    slug = _get_param("slug", "").strip()
    name = _get_param("name", i18n.T(31723, "List")).strip() or i18n.T(31723, "List")
    if not slug:
        xbmcgui.Dialog().notification(_tmdb_trakt_title(), i18n.T(31732, "The list could not be deleted."), xbmcgui.NOTIFICATION_ERROR, 3000)
        return
    if not trakt_client.is_connected():
        xbmcgui.Dialog().notification(_tmdb_trakt_title(), _tmdb_not_trakt_logged_text(), xbmcgui.NOTIFICATION_WARNING, 3000)
        return

    ok = xbmcgui.Dialog().yesno(
        _tmdb_trakt_title(),
        i18n.T(31822, "Really delete list {name}?\\n\\nThis action deletes the whole list on Trakt.TV.").format(name=name),
        nolabel=i18n.T(31848, "Back"),
        yeslabel=i18n.T(31849, "Delete")
    )
    if not ok:
        return

    try:
        deleted = trakt_client.delete_user_list(slug)
    except Exception as e:
        try:
            xbmc.log(f"[IKARUS][TRAKT] delete list error: {e}", xbmc.LOGWARNING)
        except Exception:
            pass
        xbmcgui.Dialog().notification(_tmdb_trakt_title(), i18n.T(31733, "Deleting the list failed."), xbmcgui.NOTIFICATION_ERROR, 3500)
        return

    if deleted:
        xbmcgui.Dialog().notification(_tmdb_trakt_title(), i18n.T(31734, "The list was deleted."), xbmcgui.NOTIFICATION_INFO, 2500)
        try:
            xbmc.executebuiltin("Container.Refresh")
        except Exception:
            pass
    else:
        xbmcgui.Dialog().notification(_tmdb_trakt_title(), i18n.T(31735, "Trakt did not delete the list."), xbmcgui.NOTIFICATION_WARNING, 3000)


def _tmdb_trakt_public_input(mode: str, heading: str):
    value = xbmcgui.Dialog().input(heading, type=xbmcgui.INPUT_ALPHANUM)
    value = (value or "").strip()
    if not value:
        _empty_result_dir(heading, text=i18n.T(31750, "Missing search text."))
        return
    value = value[:80]
    _tmdb_trakt_public_lists_menu(mode_override=mode, query_override=value, page_override=1)


def tmdb_trakt_public_search_input():
    _tmdb_trakt_public_input("search", i18n.T(31713, "TMDB / Trakt.TV / Search list"))


def tmdb_trakt_public_user_input():
    _tmdb_trakt_public_input("user", i18n.T(31714, "TMDB / Trakt.TV / User lists"))


def tmdb_trakt_public_user_search_input():
    _tmdb_trakt_public_input("user_search", i18n.T(31715, "TMDB / Trakt.TV / Search user"))


def _trakt_public_favorite_context_items(list_row: dict, remove_only: bool = False):
    if not isinstance(list_row, dict):
        return []
    username = str(list_row.get("username") or "").strip()
    slug = str(list_row.get("slug") or "").strip()
    name = str(list_row.get("name") or i18n.T(31724, "Trakt.TV list")).strip() or i18n.T(31724, "Trakt.TV list")
    if not username or not slug:
        return []

    base_query = {
        "user": username,
        "slug": slug,
        "name": name,
    }
    if remove_only or trakt_public_favorites.is_favorite(username, slug):
        query = {"action": "tmdb.trakt.public.favorite.remove"}
        query.update(base_query)
        return [(i18n.T(31861, "Remove from favorite public lists"), "RunPlugin(%s)" % build_url(query))]

    query = {
        "action": "tmdb.trakt.public.favorite.add",
        "item_count": str(list_row.get("item_count") or ""),
        "media_type": str(list_row.get("type") or "").strip().lower(),
        "description": str(list_row.get("description") or "").strip(),
        "likes": str(list_row.get("likes") or ""),
        "comments": str(list_row.get("comments") or ""),
    }
    query.update(base_query)
    return [(i18n.T(31862, "Save to favorite public lists"), "RunPlugin(%s)" % build_url(query))]


def tmdb_trakt_public_favorite_add():
    row = {
        "username": (_get_param("user", "").strip() or _get_param("username", "").strip()),
        "slug": _get_param("slug", "").strip(),
        "name": _get_param("name", i18n.T(31724, "Trakt.TV list")).strip(),
        "item_count": _get_param("item_count", "").strip(),
        "type": _get_param("media_type", "").strip().lower(),
        "description": _get_param("description", "").strip(),
        "likes": _get_param("likes", "").strip(),
        "comments": _get_param("comments", "").strip(),
    }
    if trakt_public_favorites.add_favorite(row):
        xbmcgui.Dialog().notification(_tmdb_trakt_title(), i18n.T(31736, "Public list was saved."), xbmcgui.NOTIFICATION_INFO, 2500)
    else:
        xbmcgui.Dialog().notification(_tmdb_trakt_title(), i18n.T(31737, "The list could not be saved."), xbmcgui.NOTIFICATION_ERROR, 3000)


def tmdb_trakt_public_favorite_remove():
    username = _get_param("user", "").strip() or _get_param("username", "").strip()
    slug = _get_param("slug", "").strip()
    if trakt_public_favorites.remove_favorite(username, slug):
        xbmcgui.Dialog().notification(_tmdb_trakt_title(), i18n.T(31738, "Public list was removed."), xbmcgui.NOTIFICATION_INFO, 2200)
        try:
            xbmc.executebuiltin("Container.Refresh")
        except Exception:
            pass
    else:
        xbmcgui.Dialog().notification(_tmdb_trakt_title(), i18n.T(31739, "The list was not in favorites."), xbmcgui.NOTIFICATION_WARNING, 2600)


def tmdb_trakt_public_favorites_menu():
    title = i18n.T(31707, "TMDB / Trakt.TV / Favorite public lists")
    xbmcplugin.setPluginCategory(addon_handle, title)

    rows = trakt_public_favorites.all_lists()
    if not rows:
        _empty_result_dir(title, text=i18n.T(31740, "No favorite public lists are saved yet."))
        return

    for row in rows:
        list_row = dict(row)
        name = list_row.get("name") or i18n.T(31724, "Trakt.TV list")
        username = list_row.get("username") or ""
        slug = list_row.get("slug") or ""
        if not username or not slug:
            continue
        item_count = list_row.get("item_count")
        meta = []
        if item_count not in (None, ""):
            meta.append(i18n.T(31902, "{count} items").format(count=item_count))
        label = f"{name}  [COLOR gray]({username}{' | ' + ' | '.join(meta) if meta else ''})[/COLOR]"
        _add_main_style_dir(
            label,
            {
                "action": "tmdb.trakt.list",
                "user": username,
                "slug": slug,
                "name": name,
                "count": str(item_count or ""),
                "media_type": str(list_row.get("type") or "").strip().lower(),
                "return_action": "tmdb.trakt.public.favorites",
                "page": "1",
            },
            "DefaultVideoPlaylists.png",
            title=name,
            plot=str(list_row.get("description") or ""),
            context_items=_trakt_public_favorite_context_items(list_row, remove_only=True),
        )

    xbmcplugin.endOfDirectory(addon_handle)


def tmdb_trakt_public_lists_menu():
    return _tmdb_trakt_public_lists_menu()


def _tmdb_trakt_public_lists_menu(mode_override: str = "", query_override: str = "", page_override=None):
    page = int(page_override) if page_override is not None else _get_int_param("page", 1)
    if page < 1:
        page = 1
    mode = (mode_override or _get_param("mode", "")).strip().lower()
    query = (query_override or _get_param("query", "")).strip()
    if mode not in ("popular", "trending", "search", "user", "user_search"):
        title = i18n.T(31706, "TMDB / Trakt.TV / Public lists")
        xbmcplugin.setPluginCategory(addon_handle, title)
        if not trakt_client.is_connected():
            _empty_result_dir(title, text=_tmdb_empty_not_logged_text())
            return
        _add_main_style_dir(i18n.T(31741, "Popular"), {"action": "tmdb.trakt.public.lists", "mode": "popular", "page": "1"}, "DefaultVideoPlaylists.png")
        _add_main_style_dir(i18n.T(31742, "Trending"), {"action": "tmdb.trakt.public.lists", "mode": "trending", "page": "1"}, "DefaultVideoPlaylists.png")
        _add_main_style_dir(i18n.T(31743, "Search list"), {"action": "tmdb.trakt.public.search.input"}, "DefaultAddonsSearch.png")
        _add_main_style_dir(i18n.T(31744, "Search user"), {"action": "tmdb.trakt.public.user.search.input"}, "DefaultAddonsSearch.png")
        _add_main_style_dir(i18n.T(31745, "User lists"), {"action": "tmdb.trakt.public.user.input"}, "DefaultUser.png")
        xbmcplugin.endOfDirectory(addon_handle)
        return
    limit = 20
    if mode == "trending":
        mode_label = i18n.T(31742, "Trending")
    elif mode == "search":
        mode_label = i18n.T(31746, "Search / {query}").format(query=query) if query else i18n.T(31743, "Search list")
    elif mode == "user":
        mode_label = i18n.T(31747, "User / {query}").format(query=query) if query else i18n.T(31745, "User lists")
    elif mode == "user_search":
        mode_label = i18n.T(31748, "Search user / {query}").format(query=query) if query else i18n.T(31744, "Search user")
    else:
        mode_label = i18n.T(31741, "Popular")
    title = i18n.T(31601, "{title} / Page {page}").format(title=f"{i18n.T(31706, 'TMDB / Trakt.TV / Public lists')} / {mode_label}", page=page)
    xbmcplugin.setPluginCategory(addon_handle, title)

    if not trakt_client.is_connected():
        _empty_result_dir(title, text=_tmdb_empty_not_logged_text())
        return

    if mode in ("search", "user", "user_search") and not query:
        _empty_result_dir(title, text=i18n.T(31750, "Missing search text."))
        return

    try:
        with diagnostics.action_scope("tmdb.phase", "tmdb.trakt.public.lists.fetch", {"mode": mode, "page": page}):
            if mode == "search":
                rows = trakt_client.search_lists(query, limit=limit, page=page)
            elif mode == "user":
                rows = trakt_client.fetch_lists_by_user(query, limit=limit, page=page)
            elif mode == "user_search":
                rows = trakt_client.search_list_users(query, limit=limit, page=page)
            else:
                rows = trakt_client.fetch_public_lists(limit=limit, page=page, mode=mode)
    except Exception as e:
        try:
            xbmc.log(f"[IKARUS][TRAKT] public list load error: {e}", xbmc.LOGWARNING)
        except Exception:
            pass
        _empty_result_dir(title, text=i18n.T(31749, "Public Trakt.TV lists could not be loaded."))
        return

    if not rows:
        _empty_result_dir(title, text=_tmdb_empty_list_text())
        return

    if mode == "user_search":
        nav_extra = {"mode": mode, "query": query}
        _trakt_add_top_page_nav("tmdb.trakt.public.lists", page, extra=nav_extra)
        added = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            username = str(row.get("username") or "").strip()
            if not username:
                continue
            display_name = str(row.get("name") or "").strip()
            list_count = row.get("list_count")
            matched_lists = int(row.get("matched_lists") or 0)
            meta = []
            if list_count not in (None, ""):
                meta.append(i18n.T(31903, "{count} lists").format(count=list_count))
            if matched_lists:
                meta.append(i18n.T(31904, "{count} matches").format(count=matched_lists))
            label_name = f"{display_name} [{username}]" if display_name and display_name.lower() != username.lower() else username
            label = f"{label_name}  [COLOR gray]({' | '.join(meta)})[/COLOR]" if meta else label_name
            public_list_count = list_count if list_count not in (None, "") else i18n.T(31905, "unknown")
            _add_main_style_dir(
                label,
                {
                    "action": "tmdb.trakt.public.lists",
                    "mode": "user",
                    "query": username,
                    "page": "1",
                },
                "DefaultUser.png",
                title=label_name,
                plot=i18n.T(31906, "User: {username}\nPublic lists: {count}").format(
                    username=username,
                    count=public_list_count
                ),
            )
            added += 1
        if added == 0:
            _empty_result_dir(title, text=i18n.T(31751, "No users found."))
            return
        if len(rows) >= limit:
            _trakt_add_bottom_page_nav("tmdb.trakt.public.lists", page + 1, extra=nav_extra)
        xbmcplugin.endOfDirectory(addon_handle)
        return

    nav_extra = {"mode": mode}
    if query:
        nav_extra["query"] = query
    _trakt_add_top_page_nav("tmdb.trakt.public.lists", page, extra=nav_extra)

    added = 0
    for row in rows:
        list_row = _trakt_public_list_from_row(row)
        name = list_row.get("name") or i18n.T(31724, "Trakt.TV list")
        slug = list_row.get("slug") or ""
        username = list_row.get("username") or ""
        if mode == "user" and not username:
            username = query
        list_type = list_row.get("type") or ""
        if not slug or not username:
            continue

        item_count = list_row.get("item_count")
        try:
            item_count = int(item_count)
        except Exception:
            item_count = None
        meta = []
        if item_count is not None:
            meta.append(i18n.T(31902, "{count} items").format(count=item_count))
        try:
            likes_i = int(list_row.get("likes") or 0)
        except Exception:
            likes_i = 0
        try:
            comments_i = int(list_row.get("comments") or 0)
        except Exception:
            comments_i = 0
        if likes_i:
            meta.append(i18n.T(31908, "Likes: {count}").format(count=likes_i))
        if comments_i:
            meta.append(i18n.T(31909, "Comments: {count}").format(count=comments_i))
        label = f"{name}  [COLOR gray]({' | '.join(meta)})[/COLOR]" if meta else name

        plot_lines = []
        if list_row.get("description"):
            plot_lines.append(list_row.get("description") or "")
        if item_count is not None:
            plot_lines.append(i18n.T(31907, "Items: {count}").format(count=item_count))
        if likes_i:
            plot_lines.append(i18n.T(31908, "Likes: {count}").format(count=likes_i))
        if comments_i:
            plot_lines.append(i18n.T(31909, "Comments: {count}").format(count=comments_i))
        plot = "\n".join(plot_lines).strip()
        _add_main_style_dir(
            label,
            {
                "action": "tmdb.trakt.list",
                "user": username,
                "slug": slug,
                "name": name,
                "count": str(item_count or ""),
                "media_type": list_type,
                "return_action": "tmdb.trakt.public.lists",
                "return_mode": mode,
                "return_query": query,
                "return_page": str(page),
                "page": "1",
            },
            "DefaultVideoPlaylists.png",
            title=name,
            plot=plot,
            context_items=_trakt_public_favorite_context_items(list_row),
        )
        added += 1

    if added == 0:
        _empty_result_dir(title, text=_tmdb_empty_list_text())
        return

    if len(rows) >= limit:
        _trakt_add_bottom_page_nav("tmdb.trakt.public.lists", page + 1, extra=nav_extra)

    xbmcplugin.endOfDirectory(addon_handle)


def _trakt_movie_to_tmdb_item(row: dict, details: dict = None) -> dict:
    movie = (row or {}).get("movie") if isinstance((row or {}).get("movie"), dict) else {}
    ids = movie.get("ids") if isinstance(movie.get("ids"), dict) else {}
    tmdb_id = str(ids.get("tmdb") or "").strip()
    if tmdb_id:
        detail = (details or {}).get(f"movie:{tmdb_id}") if isinstance(details, dict) else None
        if isinstance(detail, dict) and detail:
            return detail
        try:
            item = _tmdb_movie_listing_detail(tmdb_id)
            if item:
                return item
        except Exception:
            pass
            title = movie.get("title") or i18n.T(30748, "Untitled")
    year = str(movie.get("year") or "")
    item = {
        "id": tmdb_id,
        "title": title,
        "original_title": title,
        "release_date": f"{year}-01-01" if year.isdigit() else "",
        "overview": movie.get("overview") or "",
        "vote_average": movie.get("rating") or 0,
    }
    poster = _trakt_first_image_url(movie.get("images") or (row or {}).get("images") or {})
    if poster:
        item["poster_path"] = poster
    return item


def _trakt_show_to_tmdb_item(row: dict, details: dict = None) -> dict:
    show = (row or {}).get("show") if isinstance((row or {}).get("show"), dict) else {}
    ids = show.get("ids") if isinstance(show.get("ids"), dict) else {}
    tmdb_id = str(ids.get("tmdb") or "").strip()
    if tmdb_id:
        detail = (details or {}).get(f"show:{tmdb_id}") if isinstance(details, dict) else None
        if isinstance(detail, dict) and detail:
            return detail
        try:
            item = _tmdb_tv_listing_detail(tmdb_id)
            if item:
                return item
        except Exception:
            pass
            title = show.get("title") or i18n.T(30748, "Untitled")
    year = str(show.get("year") or "")
    item = {
        "id": tmdb_id,
        "name": title,
        "original_name": title,
        "first_air_date": f"{year}-01-01" if year.isdigit() else "",
        "overview": show.get("overview") or "",
        "vote_average": show.get("rating") or 0,
    }
    poster = _trakt_first_image_url(show.get("images") or (row or {}).get("images") or {})
    if poster:
        item["poster_path"] = poster
    return item


def _trakt_row_supported(row: dict) -> bool:
    if not isinstance(row, dict):
        return False
    media_type = str(row.get("type") or "").strip().lower()
    if media_type == "movie":
        media = row.get("movie") if isinstance(row.get("movie"), dict) else {}
    elif media_type in ("show", "tv", "series"):
        media = row.get("show") if isinstance(row.get("show"), dict) else {}
    else:
        return False
    ids = media.get("ids") if isinstance(media.get("ids"), dict) else {}
    return bool(str(ids.get("tmdb") or "").strip())


def _trakt_row_key(row: dict) -> str:
    if not isinstance(row, dict):
        return ""
    media_type = str(row.get("type") or "").strip().lower()
    if media_type == "movie":
        media = row.get("movie") if isinstance(row.get("movie"), dict) else {}
    elif media_type in ("show", "tv", "series"):
        media = row.get("show") if isinstance(row.get("show"), dict) else {}
        media_type = "show"
    else:
        return ""
    ids = media.get("ids") if isinstance(media.get("ids"), dict) else {}
    tmdb_id = str(ids.get("tmdb") or "").strip()
    trakt_id = str(ids.get("trakt") or "").strip()
    return f"{media_type}:{tmdb_id or trakt_id}"


def _trakt_remove_context_items(slug: str, list_name: str, row: dict):
    slug = str(slug or "").strip()
    if not slug or not isinstance(row, dict):
        return []
    media_type = str(row.get("type") or "").strip().lower()
    if media_type == "movie":
        media = row.get("movie") if isinstance(row.get("movie"), dict) else {}
        action_media_type = "movie"
    elif media_type in ("show", "tv", "series"):
        media = row.get("show") if isinstance(row.get("show"), dict) else {}
        action_media_type = "tv"
    else:
        return []
    ids = media.get("ids") if isinstance(media.get("ids"), dict) else {}
    tmdb_id = str(ids.get("tmdb") or "").strip()
    title = str(media.get("title") or "").strip()
    if not tmdb_id:
        return []
    return [(
        i18n.T(31863, "Remove from Trakt list"),
        "RunPlugin(%s)" % build_url({
            "action": "tmdb.trakt.remove",
            "slug": slug,
            "list_name": str(list_name or "").strip(),
            "media_type": action_media_type,
            "id": tmdb_id,
            "title": title,
        })
    )]


def _trakt_prefetch_tmdb_listing_details(rows: list):
    jobs = []
    seen = set()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        media_type = str(row.get("type") or "").strip().lower()
        if media_type == "movie":
            media = row.get("movie") if isinstance(row.get("movie"), dict) else {}
            kind = "movie"
            key_kind = "movie"
        elif media_type in ("show", "tv", "series"):
            media = row.get("show") if isinstance(row.get("show"), dict) else {}
            kind = "tv"
            key_kind = "show"
        else:
            continue
        ids = media.get("ids") if isinstance(media.get("ids"), dict) else {}
        tmdb_id = str(ids.get("tmdb") or "").strip()
        key = f"{key_kind}:{tmdb_id}"
        if not tmdb_id or key in seen:
            continue
        seen.add(key)
        jobs.append((kind, tmdb_id, key))

    if not jobs:
        return {}

    def _load(job):
        kind, tmdb_id, key = job
        if kind == "movie":
            return key, _tmdb_movie_listing_detail(tmdb_id)
        return key, _tmdb_tv_listing_detail(tmdb_id)

    details = {}
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(6, len(jobs))) as executor:
            for key, detail in executor.map(_load, jobs):
                if isinstance(detail, dict) and detail:
                    details[key] = detail
    except Exception as e:
        try:
            xbmc.log(f"[IKARUS][TRAKT] tmdb prefetch error: {e}", xbmc.LOGWARNING)
        except Exception:
            pass
    return details


def _trakt_status_from_state(trakt_state: dict, media_type: str, tmdb_id) -> str:
    tmdb_id = str(tmdb_id or "").strip()
    if not tmdb_id:
        return ""
    try:
        if isinstance(trakt_state, dict):
            state_row = trakt_state.get(tmdb_id)
            if isinstance(state_row, dict):
                return (tmdb_playback_state.get_status_bucket(state_row) or "").strip().lower()
            return ""
    except Exception:
        pass
    try:
        return (tmdb_playback_sync.trakt_item_status(media_type, tmdb_id) or "").strip().lower()
    except Exception:
        return ""


def _trakt_add_supported_row(row: dict, extra_context_items=None, details: dict = None,
                            trakt_state: dict = None, compact_info: bool = False) -> bool:
    media_type = str((row or {}).get("type") or "").strip().lower()
    if media_type == "movie":
        item = _trakt_movie_to_tmdb_item(row, details=details)
        if item.get("id"):
            status = _trakt_status_from_state(trakt_state, "movie", item.get("id"))
            _add_movie_result(
                item,
                extra_context_items=extra_context_items,
                watch_source="trakt",
                status_override=status,
                compact_info=compact_info,
            )
            return True
        return False
    if media_type in ("show", "tv", "series"):
        item = _trakt_show_to_tmdb_item(row, details=details)
        if item.get("id"):
            status = _trakt_status_from_state(trakt_state, "show", item.get("id"))
            _add_tv_result(
                item,
                extra_context_items=extra_context_items,
                watch_source="trakt",
                status_override=status,
                compact_info=compact_info,
            )
            return True
    return False


def _trakt_smart_result_row(row: dict, section: str) -> dict:
    if not isinstance(row, dict):
        return {}
    media_type = str(row.get("type") or "").strip().lower()
    if media_type in ("movie", "show", "tv", "series"):
        return row
    if isinstance(row.get("movie"), dict):
        out = dict(row)
        out["type"] = "movie"
        return out
    if isinstance(row.get("show"), dict):
        out = dict(row)
        out["type"] = "show"
        return out

    section = str(section or "").strip().lower()
    ids = row.get("ids") if isinstance(row.get("ids"), dict) else {}
    if section == "movies":
        return {"type": "movie", "movie": row}
    if section == "shows":
        return {"type": "show", "show": row}
    if row.get("title"):
        return {"type": "movie", "movie": row}
    if row.get("name"):
        return {"type": "show", "show": row}
    if str(ids.get("tmdb") or ids.get("trakt") or "").strip():
        return {"type": "movie", "movie": row}
    return {}


def _trakt_build_smart_fast_row(row: dict, details: dict = None, trakt_state: dict = None):
    media_type = str((row or {}).get("type") or "").strip().lower()
    is_movie = media_type == "movie"
    is_show = media_type in ("show", "tv", "series")
    if is_movie:
        media = row.get("movie") if isinstance(row.get("movie"), dict) else {}
        media_kind = "movie"
        detail_key_kind = "movie"
        detail_action = "tmdb.movie.detail"
        default_icon = "DefaultMovies.png"
        prefix = "[F] "
    elif is_show:
        media = row.get("show") if isinstance(row.get("show"), dict) else {}
        media_kind = "show"
        detail_key_kind = "show"
        detail_action = "tmdb.tv.detail"
        default_icon = "DefaultTVShows.png"
        prefix = "[S] "
    else:
        return None

    ids = media.get("ids") if isinstance(media.get("ids"), dict) else {}
    tmdb_id = str(ids.get("tmdb") or "").strip()
    if not tmdb_id:
        return None

    title = str(media.get("title") or media.get("name") or _tmdb_untitled()).strip()
    year = str(media.get("year") or "").strip()
    rating = media.get("rating")
    poster = _trakt_first_image_url(media.get("images") or row.get("images") or {}) or default_icon
    detail = {}
    art = {}
    original_title = ""
    plot = str(media.get("overview") or "").strip()
    tmdb_rating = None
    details = details if isinstance(details, dict) else {}
    detail = details.get(f"{detail_key_kind}:{tmdb_id}") or {}
    if not detail:
        try:
            detail = _tmdb_movie_listing_detail(tmdb_id) if is_movie else _tmdb_tv_listing_detail(tmdb_id)
        except Exception:
            detail = {}
    if isinstance(detail, dict) and detail:
        detail_title = str(detail.get("title" if is_movie else "name") or "").strip()
        if detail_title:
            title = detail_title
        original_title = str(detail.get("original_title" if is_movie else "original_name") or "").strip()
        premiered = str(detail.get("release_date" if is_movie else "first_air_date") or "").strip()
        if len(premiered) >= 4 and premiered[:4].isdigit():
            year = premiered[:4]
        tmdb_rating = detail.get("vote_average")
        plot = str(detail.get("overview") or plot or "").strip()
        art = _tmdb_media_art(detail)
        poster = art.get("poster") or poster
    suffix = ""
    try:
        if tmdb_rating not in (None, ""):
            suffix = f" [TMDB {float(tmdb_rating):.1f}]"
        elif rating not in (None, ""):
            suffix = f" [Trakt {float(rating):.1f}]"
    except Exception:
        suffix = ""

    label = f"{prefix}{title} ({year}){suffix}" if year else f"{prefix}{title}{suffix}"
    try:
        state_row = (trakt_state or {}).get(tmdb_id) if isinstance(trakt_state, dict) else None
        if isinstance(state_row, dict):
            status = tmdb_playback_state.get_status_bucket(state_row)
        else:
            status = tmdb_playback_sync.trakt_item_status(media_kind, tmdb_id)
    except Exception:
        status = ""
    status = (status or "").strip().lower()
    if status:
        label = _tmdb_colorize_status_label(label, status)

    li = xbmcgui.ListItem(label=label)
    if not art:
        art = {"icon": poster, "thumb": poster, "poster": poster}
    else:
        for key in ("icon", "thumb", "poster"):
            if not art.get(key):
                art[key] = poster
    li.setArt(art)
    info = {
        "title": title,
        "originaltitle": original_title,
        "plot": plot,
        "mediatype": "movie" if is_movie else "tvshow",
    }
    if year.isdigit():
        info["year"] = int(year)
    try:
        info["rating"] = float(tmdb_rating if tmdb_rating not in (None, "") else rating or 0)
    except Exception:
        pass
    _set_video_info(li, info)
    li.setProperty("IsPlayable", "false")
    context_items = []
    try:
        _tmdb_add_custom_list_context_item(context_items, "movie" if is_movie else "tv", tmdb_id, title)
    except Exception:
        context_items = []
    if context_items:
        li.addContextMenuItems(context_items)

    return (
        build_url({
            "action": detail_action,
            "id": tmdb_id,
            "title": title,
            "original_title": original_title,
            "watch_source": "trakt",
        }),
        li,
        True
    )


def _trakt_add_smart_fast_row(row: dict, details: dict = None, trakt_state: dict = None) -> bool:
    item = _trakt_build_smart_fast_row(row, details=details, trakt_state=trakt_state)
    if not item:
        return False
    xbmcplugin.addDirectoryItem(
        handle=addon_handle,
        url=item[0],
        listitem=item[1],
        isFolder=item[2]
    )
    return True


def tmdb_trakt_smart_list_menu():
    name = _get_param("name", i18n.T(31883, "Smart list")).strip() or i18n.T(31883, "Smart list")
    section = _get_param("section", "").strip().lower()
    path = _get_param("path", "").strip()
    query = _get_param("query", "").strip()
    page = _get_int_param("page", 1)
    if page < 1:
        page = 1
    page_limit = 20
    title = i18n.T(31601, "{title} / Page {page}").format(
        title=i18n.T(31828, "TMDB / My lists / Smart lists / {name}").format(name=name),
        page=page,
    )
    xbmcplugin.setPluginCategory(addon_handle, title)
    xbmcplugin.setContent(addon_handle, "files")

    if not trakt_client.is_connected():
        _empty_result_dir(title, text=_tmdb_empty_not_logged_text())
        return
    if section not in ("movies", "shows", "search") or not path:
        _empty_result_dir(title, text=i18n.T(31885, "Smart list does not have a valid address."))
        return

    with diagnostics.action_scope("tmdb.phase", "tmdb.trakt.smart.list.fetch", {"page": page}):
        try:
            rows = trakt_client.fetch_saved_filter_items(path, query=query, section=section, limit=page_limit, page=page)
        except Exception as e:
            try:
                xbmc.log(f"[IKARUS][TRAKT] saved filter items load error: {e}", xbmc.LOGWARNING)
            except Exception:
                pass
            _empty_result_dir(title, text=i18n.T(31886, "Smart list items could not be loaded."))
            return

    if not rows:
        _empty_result_dir(title, text=_tmdb_empty_list_text())
        return

    nav_extra = {
        "name": name,
        "section": section,
        "path": path,
        "query": query,
    }
    with diagnostics.action_scope("tmdb.phase", "tmdb.trakt.smart.list.nav", {"page": page}):
        _trakt_add_top_page_nav("tmdb.trakt.smart.list", page, extra=nav_extra)

    with diagnostics.action_scope("tmdb.phase", "tmdb.trakt.smart.list.normalize", {"count": len(rows)}):
        smart_rows = []
        for row in rows:
            smart_row = _trakt_smart_result_row(row, section)
            if smart_row:
                smart_rows.append(smart_row)

    with diagnostics.action_scope("tmdb.phase", "tmdb.trakt.smart.list.prefetch", {"count": len(smart_rows)}):
        smart_details = _trakt_prefetch_tmdb_listing_details(smart_rows) or {}

    with diagnostics.action_scope("tmdb.phase", "tmdb.trakt.smart.list.status", {"count": len(smart_rows)}):
        try:
            smart_trakt_state = tmdb_playback_sync.trakt_state()
        except Exception:
            smart_trakt_state = {}

    directory_items = []
    with diagnostics.action_scope("tmdb.phase", "tmdb.trakt.smart.list.rows.build", {"count": len(smart_rows)}):
        for smart_row in smart_rows:
            try:
                item = _trakt_build_smart_fast_row(smart_row, details=smart_details, trakt_state=smart_trakt_state)
                if item:
                    directory_items.append(item)
            except Exception as e:
                try:
                    xbmc.log(f"[IKARUS][TRAKT] smart row render error: {e}", xbmc.LOGWARNING)
                except Exception:
                    pass

    added = len(directory_items)
    if added == 0:
        _empty_result_dir(title, text=i18n.T(31900, "The list contains no supported movies or series."))
        return

    with diagnostics.action_scope("tmdb.phase", "tmdb.trakt.smart.list.rows.add", {"count": added}):
        try:
            xbmcplugin.addDirectoryItems(addon_handle, directory_items, added)
        except Exception:
            for url, li, is_folder in directory_items:
                xbmcplugin.addDirectoryItem(
                    handle=addon_handle,
                    url=url,
                    listitem=li,
                    isFolder=is_folder
                )

    if len(rows) >= page_limit:
        with diagnostics.action_scope("tmdb.phase", "tmdb.trakt.smart.list.next_nav", {"page": page + 1}):
            _trakt_add_bottom_page_nav("tmdb.trakt.smart.list", page + 1, extra=nav_extra)

    _end("files")


def _trakt_fetch_supported_rows(username: str, slug: str, item_type: str) -> list:
    raw_limit = 100
    supported_rows = []
    seen_keys = set()
    raw_page = 1

    while raw_page <= 50:
        rows = trakt_client.fetch_list_items(username, slug, limit=raw_limit, page=raw_page, item_type=item_type)
        if not rows:
            break
        for row in rows:
            row_key = _trakt_row_key(row)
            if row_key and row_key not in seen_keys and _trakt_row_supported(row):
                seen_keys.add(row_key)
                supported_rows.append(row)
        if len(rows) < raw_limit:
            break
        raw_page += 1

    return supported_rows


def _trakt_fetch_supported_page(username: str, slug: str, item_type: str, page: int, page_limit: int):
    try:
        page = max(1, int(page))
    except Exception:
        page = 1
    try:
        page_limit = max(1, int(page_limit))
    except Exception:
        page_limit = 20

    start_idx = (page - 1) * page_limit
    end_idx = start_idx + page_limit
    need_count = end_idx + 1
    raw_limit = 100
    raw_page = 1
    supported_rows = []
    seen_keys = set()
    exhausted = False

    while raw_page <= 50 and len(supported_rows) < need_count:
        rows = trakt_client.fetch_list_items(username, slug, limit=raw_limit, page=raw_page, item_type=item_type)
        if not rows:
            exhausted = True
            break
        for row in rows:
            row_key = _trakt_row_key(row)
            if row_key and row_key not in seen_keys and _trakt_row_supported(row):
                seen_keys.add(row_key)
                supported_rows.append(row)
                if len(supported_rows) >= need_count:
                    break
        if len(rows) < raw_limit:
            exhausted = True
            break
        raw_page += 1

    page_rows = supported_rows[start_idx:end_idx]
    has_next = len(supported_rows) > end_idx or (not exhausted and len(supported_rows) >= end_idx)
    return page_rows, has_next


def _trakt_sync_date_key(row: dict) -> str:
    if not isinstance(row, dict):
        return ""
    for key in ("paused_at", "last_watched_at", "watched_at", "listed_at", "updated_at"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return ""


def _trakt_sync_row_key(row: dict) -> str:
    if not isinstance(row, dict):
        return ""
    if isinstance(row.get("movie"), dict):
        ids = row["movie"].get("ids") if isinstance(row["movie"].get("ids"), dict) else {}
        return "movie:%s" % (ids.get("tmdb") or ids.get("trakt") or "")
    if isinstance(row.get("show"), dict):
        ids = row["show"].get("ids") if isinstance(row["show"].get("ids"), dict) else {}
        return "show:%s" % (ids.get("tmdb") or ids.get("trakt") or "")
    return ""


def _trakt_add_sync_row(row: dict) -> bool:
    if not isinstance(row, dict):
        return False
    if isinstance(row.get("movie"), dict):
        item = _trakt_movie_to_tmdb_item({"movie": row.get("movie")})
        if item.get("id"):
            _add_movie_result(item)
            return True
        return False
    if isinstance(row.get("show"), dict):
        item = _trakt_show_to_tmdb_item({"show": row.get("show")})
        if item.get("id"):
            _add_tv_result(item)
            return True
    return False


def _trakt_paginated_sync_menu(title: str, action: str, rows: list, page: int):
    page_limit = 20
    if page < 1:
        page = 1
    rows = list(rows or [])
    rows.sort(key=_trakt_sync_date_key, reverse=True)

    if not rows:
        _empty_result_dir(title, text=_tmdb_empty_list_text())
        return

    total_pages = max(1, (len(rows) + page_limit - 1) // page_limit)
    if page > total_pages:
        page = total_pages
    xbmcplugin.setPluginCategory(addon_handle, i18n.T(31818, "{title} / Page {page}/{total_pages}").format(title=title, page=page, total_pages=total_pages))
    xbmcplugin.setContent(addon_handle, "files")

    start_idx = (page - 1) * page_limit
    end_idx = start_idx + page_limit

    _trakt_add_top_page_nav(action, page, total_pages)

    added = 0
    for row in rows[start_idx:end_idx]:
        if _trakt_add_sync_row(row):
            added += 1

    if added == 0:
        _empty_result_dir(title, text=_tmdb_empty_list_text())
        return

    if page < total_pages:
        _trakt_add_bottom_page_nav(action, page + 1, total_pages)

    _end("files")


def _tmdb_paginated_playback_rows(title: str, action: str, rows: list, page: int,
                                  status: str):
    with diagnostics.action_scope("tmdb.phase", f"{action}.prepare", {"count": len(rows or [])}):
        page_limit = 20
        if page < 1:
            page = 1
        rows = list(rows or [])

        total_pages = max(1, (len(rows) + page_limit - 1) // page_limit)
        if page > total_pages:
            page = total_pages

        xbmcplugin.setPluginCategory(addon_handle, i18n.T(31818, "{title} / Page {page}/{total_pages}").format(title=title, page=page, total_pages=total_pages))
        xbmcplugin.setContent(addon_handle, "files")

    if not rows:
        _empty_result_dir(title, text=_tmdb_empty_list_text())
        return

    with diagnostics.action_scope("tmdb.phase", f"{action}.nav", {"page": page, "pages": total_pages}):
        _trakt_add_top_page_nav(action, page, total_pages)

    color = "lime" if status == "finished" else "orange"
    start_idx = (page - 1) * page_limit
    end_idx = start_idx + page_limit
    added = 0

    page_rows = rows[start_idx:end_idx]
    with diagnostics.action_scope("tmdb.phase", f"{action}.prefetch", {"page": page, "count": len(page_rows)}):
        playback_details = _tmdb_prefetch_playback_listing_details(page_rows)

    directory_items = []
    metadata_updates = []
    with diagnostics.action_scope("tmdb.phase", f"{action}.rows.build", {"page": page, "count": len(page_rows)}):
        for _, _, row in page_rows:
            if _tmdb_is_movie_playback_row(row):
                needs_saved_meta = not _tmdb_playback_row_has_listing_meta(row)
                movie_row = _tmdb_enrich_playback_row(row, "movie", details=playback_details)
                title_repair_meta = _tmdb_playback_title_repair_meta(row, movie_row)
                if title_repair_meta:
                    metadata_updates.append((str(movie_row.get("tmdb_id") or "").strip(), title_repair_meta))
                if needs_saved_meta and _tmdb_playback_row_has_listing_meta(movie_row):
                    meta = _tmdb_playback_persistable_meta(movie_row)
                    if meta:
                        metadata_updates.append((str(movie_row.get("tmdb_id") or "").strip(), meta))
                movie_title = str(movie_row.get("title") or "").strip()
                movie_row["title"] = f"[F] {movie_title}" if movie_title else "[F]"
                item = _tmdb_add_playback_movie_row(movie_row, color=color, return_action=action, explicit_status=status, return_item=True)
                if item:
                    directory_items.append(item)
                continue

            needs_saved_meta = not _tmdb_playback_row_has_listing_meta(row)
            tv_row = _tmdb_enrich_playback_row(row, "tv", details=playback_details)
            title_repair_meta = _tmdb_playback_title_repair_meta(row, tv_row)
            if title_repair_meta:
                metadata_updates.append((str(tv_row.get("tmdb_id") or "").strip(), title_repair_meta))
            if needs_saved_meta and _tmdb_playback_row_has_listing_meta(tv_row):
                meta = _tmdb_playback_persistable_meta(tv_row)
                if meta:
                    metadata_updates.append((str(tv_row.get("tmdb_id") or "").strip(), meta))
            tv_title = str(tv_row.get("title") or "").strip()
            tv_row["title"] = f"[S] {tv_title}" if tv_title else "[S]"
            if str(action or "").startswith("tmdb.trakt."):
                tv_row["watch_source"] = "trakt"
            item = _tmdb_add_playback_tv_row(tv_row, color=color, return_action=action, explicit_status=status, return_item=True)
            if item:
                directory_items.append(item)

    if metadata_updates:
        with diagnostics.action_scope("tmdb.phase", f"{action}.meta.save", {"page": page, "count": len(metadata_updates)}):
            _tmdb_remember_enriched_playback_rows(metadata_updates)

    added = len(directory_items)
    if added:
        with diagnostics.action_scope("tmdb.phase", f"{action}.rows.add", {"page": page, "count": added}):
            try:
                xbmcplugin.addDirectoryItems(addon_handle, directory_items, added)
            except Exception:
                for url, li, is_folder in directory_items:
                    xbmcplugin.addDirectoryItem(
                        handle=addon_handle,
                        url=url,
                        listitem=li,
                        isFolder=is_folder
                    )

    if added == 0:
        _empty_result_dir(title, text=_tmdb_empty_list_text())
        return

    if page < total_pages:
        with diagnostics.action_scope("tmdb.phase", f"{action}.next_nav", {"page": page + 1, "pages": total_pages}):
            _trakt_add_bottom_page_nav(action, page + 1, total_pages)

    _end("files")


def _tmdb_paginated_playback_media_rows(title: str, action: str, rows: list, page: int,
                                        media_type: str, status: str, content_type: str):
    with diagnostics.action_scope("tmdb.phase", f"{action}.prepare", {"count": len(rows or [])}):
        page_limit = 20
        if page < 1:
            page = 1
        rows = list(rows or [])

        total_pages = max(1, (len(rows) + page_limit - 1) // page_limit)
        if page > total_pages:
            page = total_pages

        xbmcplugin.setPluginCategory(addon_handle, i18n.T(31818, "{title} / Page {page}/{total_pages}").format(title=title, page=page, total_pages=total_pages))
        xbmcplugin.setContent(addon_handle, content_type)

    if not rows:
        _empty_saved_items_dir(title)
        return

    with diagnostics.action_scope("tmdb.phase", f"{action}.nav", {"page": page, "pages": total_pages}):
        _trakt_add_top_page_nav(action, page, total_pages)

    media_type = "movie" if str(media_type or "").strip().lower() == "movie" else "tv"
    color = "lime" if status == "finished" else "orange"
    start_idx = (page - 1) * page_limit
    end_idx = start_idx + page_limit
    page_rows = rows[start_idx:end_idx]

    with diagnostics.action_scope("tmdb.phase", f"{action}.prefetch", {"page": page, "count": len(page_rows)}):
        playback_details = _tmdb_prefetch_playback_listing_details(page_rows)

    directory_items = []
    metadata_updates = []
    with diagnostics.action_scope("tmdb.phase", f"{action}.rows.build", {"page": page, "count": len(page_rows)}):
        for _, _, row in page_rows:
            needs_saved_meta = not _tmdb_playback_row_has_listing_meta(row)
            enriched = _tmdb_enrich_playback_row(row, media_type, details=playback_details)
            title_repair_meta = _tmdb_playback_title_repair_meta(row, enriched)
            if title_repair_meta:
                metadata_updates.append((str(enriched.get("tmdb_id") or "").strip(), title_repair_meta))
            if needs_saved_meta and _tmdb_playback_row_has_listing_meta(enriched):
                meta = _tmdb_playback_persistable_meta(enriched)
                if meta:
                    metadata_updates.append((str(enriched.get("tmdb_id") or "").strip(), meta))
            if media_type == "movie":
                item = _tmdb_add_playback_movie_row(
                    enriched,
                    color=color,
                    return_action=action,
                    explicit_status=status,
                    return_item=True
                )
            else:
                item = _tmdb_add_playback_tv_row(
                    enriched,
                    color=color,
                    return_action=action,
                    explicit_status=status,
                    return_item=True
                )
            if item:
                directory_items.append(item)

    if metadata_updates:
        with diagnostics.action_scope("tmdb.phase", f"{action}.meta.save", {"page": page, "count": len(metadata_updates)}):
            _tmdb_remember_enriched_playback_rows(metadata_updates)

    if directory_items:
        with diagnostics.action_scope("tmdb.phase", f"{action}.rows.add", {"page": page, "count": len(directory_items)}):
            try:
                xbmcplugin.addDirectoryItems(addon_handle, directory_items, len(directory_items))
            except Exception:
                for url, li, is_folder in directory_items:
                    xbmcplugin.addDirectoryItem(
                        handle=addon_handle,
                        url=url,
                        listitem=li,
                        isFolder=is_folder
                    )

    if not directory_items:
        _empty_saved_items_dir(title)
        return

    if page < total_pages:
        with diagnostics.action_scope("tmdb.phase", f"{action}.next_nav", {"page": page + 1, "pages": total_pages}):
            _trakt_add_bottom_page_nav(action, page + 1, total_pages)

    _end(content_type)


def _tmdb_playback_detail_key(media_type: str, tmdb_id) -> str:
    media_type = "movie" if str(media_type or "").strip().lower() == "movie" else "tv"
    tmdb_id = str(tmdb_id or "").strip()
    return f"{media_type}:{tmdb_id}" if tmdb_id else ""


def _tmdb_detail_miss_is_fresh(key: str) -> bool:
    key = str(key or "").strip()
    if not key:
        return False
    ts = float(_TMDB_DETAIL_MISS_CACHE.get(key) or 0.0)
    if ts <= 0:
        return False
    if (time.time() - ts) < _TMDB_DETAIL_MISS_TTL:
        return True
    try:
        _TMDB_DETAIL_MISS_CACHE.pop(key, None)
    except Exception:
        pass
    return False


def _tmdb_detail_miss_remember(key: str):
    key = str(key or "").strip()
    if key:
        _TMDB_DETAIL_MISS_CACHE[key] = time.time()


def _tmdb_detail_miss_clear(key: str):
    try:
        _TMDB_DETAIL_MISS_CACHE.pop(str(key or "").strip(), None)
    except Exception:
        pass


def _tmdb_source_display_title(row: dict) -> str:
    sources = (row or {}).get("sources")
    if not isinstance(sources, dict):
        return ""

    candidates = []
    for idx, src in enumerate(sources.values()):
        if not isinstance(src, dict):
            continue
        title = str(src.get("display_title") or "").strip()
        if (
            not title
            or _tmdb_needs_readable_title(title)
            or title.lower() in ("trakt.tv", "zdroj")
            or title.lower().endswith((".mkv", ".mp4", ".avi", ".ts"))
        ):
            continue

        provider = str(src.get("provider") or "").strip().lower()
        provider_rank = 5 if provider == "trakt" else 0
        updated = max(
            str(src.get("last_started_at") or ""),
            str(src.get("last_finished_at") or ""),
            str(src.get("updated_at") or ""),
        )
        candidates.append((provider_rank, updated, idx, title))

    if not candidates:
        return ""
    local_candidates = [item for item in candidates if item[0] == 0]
    picked_from = local_candidates or candidates
    picked_from.sort(key=lambda item: (item[1], item[2]))
    return picked_from[-1][3]


def _tmdb_playback_row_has_listing_meta(row: dict) -> bool:
    if not isinstance(row, dict):
        return False
    title = str(row.get("title") or "").strip()
    return bool(
        title
        and not _tmdb_needs_readable_title(title)
        and str(row.get("year") or "").strip()
        and str(row.get("poster") or "").strip()
        and str(row.get("plot") or "").strip()
    )


def _tmdb_listing_detail_to_playback_meta(item: dict, media_type: str) -> dict:
    item = item or {}
    media_type = "movie" if str(media_type or "").strip().lower() == "movie" else "tv"
    if media_type == "movie":
        title, title_is_local = _tmdb_display_title_info(item, "movie", fetch_alternatives=True)
        original_title = _tmdb_hide_unreadable_text(item.get("original_title") or "")
        premiered = str(item.get("release_date") or "").strip()
    else:
        title, title_is_local = _tmdb_display_title_info(item, "tv", fetch_alternatives=True)
        original_title = _tmdb_hide_unreadable_text(item.get("original_name") or "")
        premiered = str(item.get("first_air_date") or "").strip()
    year = premiered[:4] if len(premiered) >= 4 and premiered[:4].isdigit() else ""
    art = _tmdb_media_art(item) if item else {}
    meta = {
        "title": title,
        "original_title": original_title,
        "year": year,
        "plot": str(item.get("overview") or "").strip(),
        "rating": item.get("vote_average"),
        "media_type": "movie" if media_type == "movie" else "tv",
        "poster": art.get("poster") or "",
        "_title_source_tmdb_detail": True,
        "_local_origin": _tmdb_is_local_origin_item(item),
    }
    if title_is_local:
        meta["_title_is_local"] = True

    return {k: v for k, v in meta.items() if v not in (None, "")}


def _tmdb_playback_persistable_meta(row: dict) -> dict:
    if not isinstance(row, dict):
        return {}
    keys = (
        "original_title",
        "year",
        "poster",
        "plot",
        "rating",
        "media_type",
        "total_episodes",
        "total_seasons",
        "_title_source_tmdb_detail",
        "_local_origin",
    )
    meta = {key: row.get(key) for key in keys if row.get(key) not in (None, "")}
    title = str(row.get("title") or "").strip()
    if title and row.get("_title_is_local"):
        meta["title"] = title
        meta["_title_is_local"] = True
    return meta


def _tmdb_playback_title_repair_meta(before: dict, after: dict) -> dict:
    before_title = str((before or {}).get("title") or "").strip()
    after_title = str((after or {}).get("title") or "").strip()
    if after_title and after_title != before_title and (after or {}).get("_title_is_local"):
        meta = _tmdb_playback_persistable_meta(after or {})
        meta["title"] = after_title
        meta["_title_is_local"] = True
        if (after or {}).get("_title_source_tmdb_detail"):
            meta["_title_source_tmdb_detail"] = True
        if "_local_origin" in (after or {}):
            meta["_local_origin"] = (after or {}).get("_local_origin")
        return meta
    return {}


def _tmdb_remember_enriched_playback_rows(updates: list):
    if not updates:
        return
    try:
        changed = tmdb_playback_state.update_items_meta(updates)
        if changed:
            xbmc.log(f"[IKARUS][TMDB] playback metadata saved fields={changed} rows={len(updates)}", xbmc.LOGINFO)
    except Exception as e:
        try:
            xbmc.log(f"[IKARUS][TMDB] playback metadata save failed: {e}", xbmc.LOGWARNING)
        except Exception:
            pass


def _tmdb_remember_detail_playback_meta(tmdb_id: str, item: dict, media_type: str):
    tmdb_id = str(tmdb_id or "").strip()
    if not tmdb_id or not isinstance(item, dict) or not item:
        return
    try:
        state = tmdb_playback_state.playback_state_load() or {}
        if tmdb_id not in state:
            return
        meta = _tmdb_listing_detail_to_playback_meta(item, media_type)
        if not meta:
            return
        changed = tmdb_playback_state.update_items_meta([(tmdb_id, meta)])
        if changed:
            xbmc.log(f"[IKARUS][TMDB] playback detail metadata saved tmdb_id={tmdb_id} fields={changed}", xbmc.LOGINFO)
    except Exception as e:
        try:
            xbmc.log(f"[IKARUS][TMDB] playback detail metadata save failed tmdb_id={tmdb_id}: {e}", xbmc.LOGWARNING)
        except Exception:
            pass


def _tmdb_playback_preferred_local_title(row: dict) -> str:
    row = row or {}
    title = str(row.get("title") or "").strip()
    original = str(row.get("original_title") or "").strip()
    if title and original and title.casefold() != original.casefold():
        if row.get("_title_source_tmdb_detail"):
            return title
        if row.get("_title_is_local") and _tmdb_has_local_latin_chars(original) and not _tmdb_has_local_latin_chars(title):
            return ""
        if row.get("_title_is_local") and not _tmdb_has_local_latin_chars(title) and not _tmdb_has_local_latin_chars(original):
            return ""
    if title and row.get("_title_is_local") and not row.get("_title_source_tmdb_detail"):
        if _tmdb_has_distinct_local_latin_chars(title):
            return title
        return ""
    if title and row.get("_title_is_local") and not _tmdb_needs_readable_title(title):
        return title
    return ""


def _tmdb_playback_title_needs_local_lookup(row: dict) -> bool:
    row = row or {}
    if _tmdb_playback_preferred_local_title(row):
        return False
    title = str(row.get("title") or "").strip()
    if not title or _tmdb_needs_readable_title(title):
        return True
    return True


def _tmdb_prefetch_playback_listing_details(page_rows: list) -> dict:
    jobs = []
    seen = set()
    for row_tuple in page_rows or []:
        try:
            row = row_tuple[2]
        except Exception:
            continue
        if not isinstance(row, dict):
            continue
        needs_local_title_lookup = _tmdb_playback_title_needs_local_lookup(row)
        if _tmdb_playback_row_has_listing_meta(row) and not needs_local_title_lookup:
            continue
        media_type = "movie" if _tmdb_is_movie_playback_row(row) else "tv"
        tmdb_id = str(row.get("tmdb_id") or "").strip()
        key = _tmdb_playback_detail_key(media_type, tmdb_id)
        if not key or key in seen:
            continue
        if _tmdb_detail_miss_is_fresh(key) and not needs_local_title_lookup:
            continue
        seen.add(key)
        jobs.append((media_type, tmdb_id, key))
    if not jobs:
        return {}

    def _load(job):
        media_type, tmdb_id, key = job
        detail = _tmdb_movie_listing_detail(tmdb_id) if media_type == "movie" else _tmdb_tv_listing_detail(tmdb_id)
        return key, detail

    details = {}
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(6, len(jobs))) as executor:
            for key, detail in executor.map(_load, jobs):
                if isinstance(detail, dict) and detail:
                    details[key] = detail
                    _tmdb_detail_miss_clear(key)
                else:
                    details[key] = None
                    _tmdb_detail_miss_remember(key)
    except Exception as e:
        try:
            xbmc.log(f"[IKARUS][TMDB] playback prefetch error: {e}", xbmc.LOGWARNING)
        except Exception:
            pass
    return details


def _tmdb_enrich_playback_row(row: dict, media_type: str, details: dict = None) -> dict:
    out = dict(row or {})
    tmdb_id = str(out.get("tmdb_id") or "").strip()
    media_type = "movie" if str(media_type or "").strip().lower() == "movie" else "tv"
    local_title = _tmdb_playback_preferred_local_title(out)
    if local_title:
        out["title"] = local_title
    has_listing_meta = _tmdb_playback_row_has_listing_meta(out)
    needs_local_title_lookup = _tmdb_playback_title_needs_local_lookup(out)
    if not tmdb_id or (has_listing_meta and not needs_local_title_lookup):
        return out

    try:
        key = _tmdb_playback_detail_key(media_type, tmdb_id)
        has_prefetched_details = isinstance(details, dict)
        detail = details.get(key) if has_prefetched_details else None
        if isinstance(detail, dict) and detail:
            meta = _tmdb_listing_detail_to_playback_meta(detail, media_type)
            _tmdb_detail_miss_clear(key)
        elif has_prefetched_details and key in details and not needs_local_title_lookup:
            meta = {}
        elif _tmdb_detail_miss_is_fresh(key) and not needs_local_title_lookup:
            meta = {}
        elif media_type == "movie":
            detail = _tmdb_movie_listing_detail(tmdb_id)
            if isinstance(detail, dict) and detail:
                _tmdb_detail_miss_clear(key)
                meta = _tmdb_listing_detail_to_playback_meta(detail, "movie")
            else:
                _tmdb_detail_miss_remember(key)
                meta = {}
        else:
            detail = _tmdb_tv_listing_detail(tmdb_id)
            if isinstance(detail, dict) and detail:
                _tmdb_detail_miss_clear(key)
                meta = _tmdb_listing_detail_to_playback_meta(detail, "tv")
            else:
                _tmdb_detail_miss_remember(key)
                meta = {}

    except Exception as e:
        try:
            xbmc.log(f"[IKARUS][TRAKT] tmdb enrich error tmdb_id={tmdb_id}: {e}", xbmc.LOGWARNING)
        except Exception:
            pass
        meta = {}

    for key, value in (meta or {}).items():
        if value in (None, ""):
            continue
        if key == "title":
            continue
        out[key] = value
    meta_title = str((meta or {}).get("title") or "").strip()
    if meta_title and (
        (meta or {}).get("_title_is_local")
        or not str(out.get("title") or "").strip()
        or _tmdb_needs_readable_title(out.get("title"))
    ):
        before_title = str(out.get("title") or "").strip()
        out["title"] = meta_title
        if before_title and before_title != meta_title and (meta or {}).get("_title_is_local"):
            try:
                xbmc.log(
                    f"[IKARUS][TMDB] playback title repaired tmdb_id={tmdb_id} "
                    f"old={before_title} new={meta_title}",
                    xbmc.LOGINFO,
                )
            except Exception:
                pass
    if meta_title and (meta or {}).get("_title_is_local"):
        out["_title_is_local"] = True
    return out

def _tmdb_enrich_playback_rows(rows, media_type: str) -> list:
    out = []
    for row in (rows or []):
        if not isinstance(row, dict):
            continue
        out.append(_tmdb_enrich_playback_row(row, media_type))
    return out


def tmdb_trakt_watched_menu():
    page = _get_int_param("page", 1)
    title = "TMDB / Trakt.TV / Zhl\u00e9dnut\u00e9"
    if not trakt_client.is_connected():
        _empty_result_dir(title, text=_tmdb_empty_not_logged_text())
        return

    rows = list(tmdb_playback_sync.iter_trakt_watched_items())
    _tmdb_paginated_playback_rows(title, "tmdb.trakt.watched", rows, page, "finished")


def tmdb_trakt_started_menu():
    page = _get_int_param("page", 1)
    title = "TMDB / Trakt.TV / Rozkoukan\u00e9"
    if not trakt_client.is_connected():
        _empty_result_dir(title, text=_tmdb_empty_not_logged_text())
        return

    rows = list(tmdb_playback_sync.iter_trakt_playback_items())
    _tmdb_paginated_playback_rows(title, "tmdb.trakt.started", rows, page, "started")


def tmdb_trakt_list_menu():
    name = _get_param("name", i18n.T(31724, "Trakt.TV list")).strip() or i18n.T(31724, "Trakt.TV list")
    username = _get_param("user", "").strip() or _get_param("username", "").strip()
    slug = _get_param("slug", "").strip()
    page = _get_int_param("page", 1)
    if page < 1:
        page = 1
    media_type = _get_param("media_type", "").strip().lower()
    item_type = ""
    page_limit = 20
    total_count = _get_int_param("count", 0)
    has_total_count = total_count > 0
    total_pages = max(1, (total_count + page_limit - 1) // page_limit) if has_total_count else 0
    if has_total_count and page > total_pages:
        page = total_pages
    page_label = f"{page}/{total_pages}" if has_total_count else str(page)
    title = i18n.T(31911, "TMDB / Trakt.TV / {name} / Page {page}").format(name=name, page=page_label)
    xbmcplugin.setPluginCategory(addon_handle, title)
    xbmcplugin.setContent(addon_handle, "files")

    if not trakt_client.is_connected():
        _empty_result_dir(title, text=_tmdb_empty_not_logged_text())
        return
    if not username or not slug:
        _empty_result_dir(title, text=i18n.T(31910, "Trakt.TV list does not have a valid address."))
        return

    try:
        with diagnostics.action_scope("tmdb.phase", "tmdb.trakt.list.fetch", {"page": page, "limit": page_limit}):
            page_rows, has_next = _trakt_fetch_supported_page(username, slug, item_type, page, page_limit)
    except Exception as e:
        try:
            xbmc.log(f"[IKARUS][TRAKT] list items load error: {e}", xbmc.LOGWARNING)
        except Exception:
            pass
        _empty_result_dir(title, text=i18n.T(31901, "List items could not be loaded."))
        return

    if not page_rows:
        _empty_result_dir(title, text=_tmdb_empty_list_text())
        return

    nav_extra = {
        "user": username,
        "slug": slug,
        "name": name,
        "media_type": media_type or "all",
    }
    for key in ("return_action", "return_mode", "return_query", "return_page"):
        value = _get_param(key, "").strip()
        if value:
            nav_extra[key] = value
    if has_total_count:
        nav_extra["count"] = str(total_count)
    with diagnostics.action_scope("tmdb.phase", "tmdb.trakt.list.nav.top", {"page": page}):
        _trakt_add_top_page_nav("tmdb.trakt.list", page, total_pages if has_total_count else 0, nav_extra)

    added = 0
    can_remove = username.lower() == "me"
    with diagnostics.action_scope("tmdb.phase", "tmdb.trakt.list.prefetch", {"count": len(page_rows)}):
        trakt_details = _trakt_prefetch_tmdb_listing_details(page_rows) or {}
    with diagnostics.action_scope("tmdb.phase", "tmdb.trakt.list.status", {"count": len(page_rows)}):
        try:
            trakt_state = tmdb_playback_sync.trakt_state()
        except Exception as e:
            trakt_state = {}
            try:
                xbmc.log(f"[IKARUS][TRAKT] trakt status state load error: {e}", xbmc.LOGWARNING)
            except Exception:
                pass
    with diagnostics.action_scope("tmdb.phase", "tmdb.trakt.list.rows", {"count": len(page_rows)}):
        for row in page_rows:
            extra_context = _trakt_remove_context_items(slug, name, row) if can_remove else []
            if _trakt_add_supported_row(
                row,
                extra_context_items=extra_context,
                details=trakt_details,
                trakt_state=trakt_state,
                compact_info=True,
            ):
                added += 1

    if added == 0:
        _empty_result_dir(title, text=i18n.T(31900, "The list contains no supported movies or series."))
        return

    if has_total_count:
        has_next = page < total_pages

    if has_next:
        with diagnostics.action_scope("tmdb.phase", "tmdb.trakt.list.nav.bottom", {"page": page + 1}):
            _trakt_add_bottom_page_nav("tmdb.trakt.list", page + 1, total_pages if has_total_count else 0, nav_extra)

    _end("files")


def _tmdb_refresh_custom_lists_container():
    try:

        xbmc.executebuiltin('CancelAlarm(IkarusCustomListsRefresh,silent)')

    except Exception:

        pass

    try:

        xbmc.executebuiltin(

            'AlarmClock(IkarusCustomListsRefresh,Container.Refresh,00:01,silent)'

        )

        return True

    except Exception:

        return False





def _tmdb_refresh_custom_list_container(list_id: str):
    list_id = str(list_id or "").strip()
    if list_id:
        try:
            target_url = build_url({"action": "tmdb.custom.list", "list_id": list_id})
            xbmc.executebuiltin(f'Container.Update("{target_url}",replace)')
            return True
        except Exception:
            pass
    try:
        xbmc.executebuiltin('CancelAlarm(IkarusCustomListRefresh,silent)')
    except Exception:
        pass

    try:

        xbmc.executebuiltin(

            'AlarmClock(IkarusCustomListRefresh,Container.Refresh,00:01,silent)'

        )

        return True

    except Exception:

        return False









def _tmdb_finish_plugin_action():
    return


def _tmdb_custom_list_create_dialog(default_name: str = "", default_description: str = "", default_privacy: str = "private", heading: str = None):
    heading = heading or i18n.T(31716, "TMDB / My lists / New list")
    name = xbmcgui.Dialog().input(
        heading,
        defaultt=(default_name or "").strip(),
        type=xbmcgui.INPUT_ALPHANUM,
    )
    if not name or not name.strip():
        return None
    name = name.strip()

    description = xbmcgui.Dialog().input(
        i18n.T(31761, "List description (optional)"),
        defaultt=(default_description or "").strip(),
        type=xbmcgui.INPUT_ALPHANUM,
    )
    description = (description or "").strip()

    privacy_values = ["private", "public", "friends"]
    privacy_labels = [
        i18n.T(31855, "Private"),
        i18n.T(31856, "Public"),
        i18n.T(31857, "Friends"),
    ]
    try:
        default_index = privacy_values.index(str(default_privacy or "private").strip().lower())
    except Exception:
        default_index = 0
    ordered_values = [privacy_values[default_index]] + [value for idx, value in enumerate(privacy_values) if idx != default_index]
    ordered_labels = [privacy_labels[default_index]] + [label for idx, label in enumerate(privacy_labels) if idx != default_index]
    choice = xbmcgui.Dialog().contextmenu(ordered_labels)
    if choice < 0:
        return None

    return {
        "name": name,
        "description": description,
        "privacy": ordered_values[choice],
    }


def tmdb_custom_list_create():
    payload = _tmdb_custom_list_create_dialog()
    if not payload:
        return
    row = tmdb_custom_lists.create_list(
        payload.get("name"),
        description=payload.get("description"),
        privacy=payload.get("privacy"),
    )
    if row:
        trakt_result = {"ok": False, "mode": "not_connected"}
        try:
            if trakt_client.is_connected():
                trakt_result = tmdb_trakt_list_sync.sync_local_list_to_trakt(str(row.get("id") or ""))
        except Exception as e:
            try:
                xbmc.log(f"[IKARUS][TRAKT LIST SYNC] create list sync error: {e}", xbmc.LOGWARNING)
            except Exception:
                pass
            trakt_result = {"ok": False, "mode": "error"}

        if trakt_result.get("ok") and trakt_result.get("mode") == "synced":
            msg = i18n.T(31887, "List was created locally and on Trakt.TV.")
        elif trakt_result.get("mode") == "not_connected":
            msg = i18n.T(31888, "List was created.")
        else:
            msg = i18n.T(31889, "List was created locally. Trakt.TV will be synchronized later.")
        xbmcgui.Dialog().notification(_tmdb_custom_lists_title(), msg, xbmcgui.NOTIFICATION_INFO, 3000)
        _tmdb_finish_plugin_action()
    _tmdb_refresh_custom_lists_container()




def tmdb_custom_list_delete():

    list_id = _get_param("list_id", "").strip()

    name = _get_param("name", i18n.T(31723, "List")).strip() or i18n.T(31723, "List")

    if not list_id:

        return

    row = tmdb_custom_lists.get_list(list_id)

    if row:

        can_delete, mode = tmdb_custom_lists.can_delete_list(row)

        if not can_delete and mode == "owner_only":

            owner_name = tmdb_custom_lists.delete_owner_name(row)

            msg = i18n.T(31890, "The list can only be deleted on the device where it was created.")

            if owner_name:

                msg = i18n.T(31823, "The list can only be deleted on the device where it was created: {owner}.").format(owner=owner_name)

            xbmcgui.Dialog().notification(_tmdb_custom_lists_title(), msg, xbmcgui.NOTIFICATION_WARNING, 3500)

            _tmdb_refresh_custom_lists_container()

            return

    ok = xbmcgui.Dialog().yesno(
        _tmdb_custom_lists_title(),
        i18n.T(31824, "Delete list {name}?").format(name=name),
        nolabel=i18n.T(31848, "Back"),
        yeslabel=i18n.T(31849, "Delete")
    )
    if ok:
        deleted, mode = tmdb_custom_lists.delete_list(list_id)
        if deleted:
            trakt_result = {"ok": False, "mode": "not_connected"}
            try:
                if trakt_client.is_connected():
                    trakt_result = tmdb_trakt_list_sync.sync_local_list_delete_to_trakt(list_id)
            except Exception as e:
                try:
                    xbmc.log(f"[IKARUS][TRAKT LIST SYNC] delete list sync error: {e}", xbmc.LOGWARNING)
                except Exception:
                    pass
                trakt_result = {"ok": False, "mode": "error"}

            if trakt_result.get("ok") and trakt_result.get("mode") == "synced":
                msg = i18n.T(31892, "List was deleted locally and on Trakt.TV.")
            elif trakt_result.get("mode") == "not_connected":
                msg = i18n.T(31893, "List was deleted.")
            elif trakt_result.get("ok"):
                msg = i18n.T(31893, "List was deleted.")
            else:
                msg = i18n.T(31894, "List was deleted locally. Trakt.TV will be synchronized later.")
            xbmcgui.Dialog().notification(_tmdb_custom_lists_title(), msg, xbmcgui.NOTIFICATION_INFO, 3000)
        elif mode == "owner_only":
            owner_name = tmdb_custom_lists.delete_owner_name(row)
            msg = i18n.T(31890, "The list can only be deleted on the device where it was created.")
            if owner_name:

                msg = i18n.T(31891, "The list can only be deleted on device: {owner}.").format(owner=owner_name)

            xbmcgui.Dialog().notification(_tmdb_custom_lists_title(), msg, xbmcgui.NOTIFICATION_WARNING, 3500)

    _tmdb_refresh_custom_lists_container()





def tmdb_custom_list_rename():
    list_id = _get_param("list_id", "").strip()
    old_name = _get_param("name", i18n.T(31723, "List")).strip() or i18n.T(31723, "List")
    if not list_id:
        return
    row = tmdb_custom_lists.get_list(list_id)

    if row:

        can_rename, mode = tmdb_custom_lists.can_rename_list(row)

        if not can_rename and mode == "owner_only":

            owner_name = tmdb_custom_lists.rename_owner_name(row)

            msg = i18n.T(31895, "The list can only be renamed on the device where it was created.")

            if owner_name:

                msg = i18n.T(31896, "The list can only be renamed on device: {owner}.").format(owner=owner_name)

            xbmcgui.Dialog().notification(_tmdb_custom_lists_title(), msg, xbmcgui.NOTIFICATION_WARNING, 3500)
            _tmdb_refresh_custom_lists_container()
            return

    payload = _tmdb_custom_list_create_dialog(
        default_name=old_name,
        default_description=str((row or {}).get("trakt_description") or "").strip(),
        default_privacy=str((row or {}).get("trakt_privacy") or "private").strip().lower(),
        heading=i18n.T(31717, "TMDB / My lists / Edit list"),
    )
    if not payload:
        return

    ok, mode = tmdb_custom_lists.rename_list(
        list_id,
        payload.get("name"),
        description=payload.get("description"),
        privacy=payload.get("privacy"),
    )
    if ok:
        trakt_result = {"ok": False, "mode": "not_connected"}
        try:
            if trakt_client.is_connected():
                trakt_result = tmdb_trakt_list_sync.sync_local_list_metadata_to_trakt(list_id)
        except Exception as e:
            try:
                xbmc.log(f"[IKARUS][TRAKT LIST SYNC] rename list sync error: {e}", xbmc.LOGWARNING)
            except Exception:
                pass
            trakt_result = {"ok": False, "mode": "error"}

        if trakt_result.get("ok") and trakt_result.get("mode") == "synced":
            msg = i18n.T(31897, "List was updated locally and on Trakt.TV.")
        elif trakt_result.get("mode") == "not_connected":
            msg = i18n.T(31898, "List was updated.")
        else:
            msg = i18n.T(31899, "List was updated locally. Trakt.TV will be synchronized later.")
        xbmcgui.Dialog().notification(_tmdb_custom_lists_title(), msg, xbmcgui.NOTIFICATION_INFO, 3000)
    elif mode == "owner_only":
        owner_name = tmdb_custom_lists.rename_owner_name(row)
        msg = i18n.T(31895, "The list can only be renamed on the device where it was created.")
        if owner_name:

            msg = i18n.T(31896, "The list can only be renamed on device: {owner}.").format(owner=owner_name)

        xbmcgui.Dialog().notification(_tmdb_custom_lists_title(), msg, xbmcgui.NOTIFICATION_WARNING, 3500)

    elif mode == "duplicate":

        xbmcgui.Dialog().notification(_tmdb_custom_lists_title(), i18n.T(31776, "A list with this name already exists."), xbmcgui.NOTIFICATION_WARNING, 3000)

    else:

        xbmcgui.Dialog().notification(_tmdb_custom_lists_title(), i18n.T(31777, "Renaming failed."), xbmcgui.NOTIFICATION_ERROR, 3000)

    _tmdb_refresh_custom_lists_container()





def _tmdb_custom_list_year_sort_key(item: dict):

    try:

        return int(str((item or {}).get("year") or "0").strip()[:4] or 0)

    except Exception:

        return 0





def _tmdb_custom_item_delete_context_items(list_id: str, list_row: dict, item: dict):
    list_id = str(list_id or "").strip()

    if not list_id or not isinstance(list_row, dict) or not isinstance(item, dict):

        return []

    media_type = str((item or {}).get("media_type") or "").strip().lower()

    tmdb_id = str((item or {}).get("tmdb_id") or "").strip()

    if media_type not in ("movie", "tv") or not tmdb_id:

        return []

    allowed, mode = tmdb_custom_lists.can_delete_item(list_row, item)

    if not allowed and mode == "item_owner_only":

        return []

    return [(

        i18n.T(31829, "Remove from list"),

        "RunPlugin(%s)" % build_url({

            "action": "tmdb.custom.item.delete",

            "list_id": list_id,

            "media_type": media_type,

            "id": tmdb_id,

        })

    )]


def _tmdb_add_custom_list_fast_row(list_id: str, list_row: dict, item: dict) -> bool:
    display_item = dict(item or {})
    media_type = str(display_item.get("media_type") or "").strip().lower()
    tmdb_id = str(display_item.get("tmdb_id") or "").strip()
    if media_type not in ("movie", "tv") or not tmdb_id:
        return False

    title = str(display_item.get("title") or _tmdb_untitled()).strip()
    year = str(display_item.get("year") or "").strip()
    rating = display_item.get("rating")
    poster = str(display_item.get("poster") or "").strip()
    if not poster:
        poster = "DefaultTVShows.png" if media_type == "tv" else "DefaultMovies.png"
    original_title = str(display_item.get("original_title") or "").strip()

    suffix = ""
    try:
        if rating not in (None, ""):
            suffix = f" [TMDB {float(rating):.1f}]"
    except Exception:
        suffix = ""

    prefix = "[S] " if media_type == "tv" else "[F] "
    label = f"{prefix}{title} ({year}){suffix}" if year else f"{prefix}{title}{suffix}"
    try:
        status = _tmdb_merged_item_status(tmdb_id)
    except Exception:
        status = ""
    status = (status or "").strip().lower()
    if status:
        label = _tmdb_colorize_status_label(label, status)

    li = xbmcgui.ListItem(label=label)
    li.setArt({"icon": poster, "thumb": poster, "poster": poster})
    li.setProperty("IsPlayable", "false")

    extra_context = _tmdb_custom_item_delete_context_items(list_id, list_row, display_item)
    if extra_context:
        try:
            li.addContextMenuItems(extra_context, replaceItems=False)
        except Exception:
            pass

    action = "tmdb.tv.detail.open" if media_type == "tv" else "tmdb.movie.detail.open"
    watch_source = str(display_item.get("watch_source") or "").strip().lower()
    if watch_source != "trakt":
        watch_source = ""
    xbmcplugin.addDirectoryItem(
        handle=addon_handle,
        url=build_url(_tmdb_add_watch_source({
            "action": action,
            "id": tmdb_id,
            "title": title,
            "original_title": original_title,
        }, watch_source)),
        listitem=li,
        isFolder=False
    )
    return True


def _tmdb_custom_item_has_listing_meta(item: dict) -> bool:
    if not isinstance(item, dict):
        return False
    media_type = str(item.get("media_type") or "").strip().lower()
    tmdb_id = str(item.get("tmdb_id") or "").strip()
    if media_type not in ("movie", "tv") or not tmdb_id:
        return False

    title = str(item.get("title") or "").strip()
    poster = str(item.get("poster") or "").strip()
    plot = str(item.get("plot") or "").strip()
    year = str(item.get("year") or "").strip()
    rating = item.get("rating")
    has_rating = rating not in (None, "")
    has_poster = bool(poster and not poster.startswith("Default"))
    return bool(title and (has_poster or plot or year or has_rating))


def _tmdb_custom_item_display_row(item: dict) -> dict:
    row = dict(item or {})
    media_type = str(row.get("media_type") or "").strip().lower()
    tmdb_id = str(row.get("tmdb_id") or "").strip()
    if media_type not in ("movie", "tv") or not tmdb_id:
        return row

    if _tmdb_custom_item_has_listing_meta(row):
        return row

    try:
        detail = _tmdb_movie_listing_detail(tmdb_id) if media_type == "movie" else _tmdb_tv_listing_detail(tmdb_id)
    except Exception:
        detail = {}
    if not isinstance(detail, dict) or not detail:
        return row

    if media_type == "movie":
        title = _tmdb_display_title(detail, "movie", row.get("title") or _tmdb_untitled()) or row.get("title") or _tmdb_untitled()
        original = str(detail.get("original_title") or row.get("original_title") or "").strip()
        premiered = str(detail.get("release_date") or "").strip()
    else:
        title = _tmdb_display_title(detail, "tv", row.get("title") or _tmdb_untitled()) or row.get("title") or _tmdb_untitled()
        original = str(detail.get("original_name") or row.get("original_title") or "").strip()
        premiered = str(detail.get("first_air_date") or "").strip()

    art = _tmdb_media_art(detail)
    poster = str(art.get("poster") or row.get("poster") or "").strip()
    year = premiered[:4] if len(premiered) >= 4 and premiered[:4].isdigit() else str(row.get("year") or "").strip()

    row["title"] = title
    row["original_title"] = original
    row["year"] = year
    row["poster"] = poster or row.get("poster") or ("DefaultTVShows.png" if media_type == "tv" else "DefaultMovies.png")
    row["plot"] = detail.get("overview") or row.get("plot") or ""
    row["rating"] = detail.get("vote_average", row.get("rating") or 0)
    return row


def _tmdb_prefetch_custom_list_details(items: list):
    jobs = []
    seen = set()
    for item in items or []:
        if not isinstance(item, dict):
            continue
        if _tmdb_custom_item_has_listing_meta(item):
            continue
        media_type = str(item.get("media_type") or "").strip().lower()
        tmdb_id = str(item.get("tmdb_id") or "").strip()
        if media_type not in ("movie", "tv") or not tmdb_id:
            continue
        key = f"{media_type}:{tmdb_id}"
        if key in seen:
            continue
        seen.add(key)
        jobs.append((media_type, tmdb_id))

    if not jobs:
        return

    def _load(job):
        media_type, tmdb_id = job
        if media_type == "movie":
            return _tmdb_movie_listing_detail(tmdb_id)
        return _tmdb_tv_listing_detail(tmdb_id)

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(6, len(jobs))) as executor:
            list(executor.map(_load, jobs))
    except Exception as e:
        try:
            xbmc.log(f"[IKARUS][TMDB LISTS] custom list detail prefetch error: {e}", xbmc.LOGWARNING)
        except Exception:
            pass


def _tmdb_enrich_custom_list_items_for_storage(list_id: str, items: list) -> list:
    enriched = []
    changed = False
    for item in items or []:
        if not isinstance(item, dict):
            continue
        display_item = _tmdb_custom_item_display_row(item)
        enriched.append(display_item)
        if display_item != item:
            changed = True

    if changed:
        try:
            tmdb_custom_lists.replace_list_items_metadata(list_id, enriched)
        except Exception as e:
            try:
                xbmc.log(f"[IKARUS][TMDB LISTS] custom list metadata save error: {e}", xbmc.LOGWARNING)
            except Exception:
                pass
    return enriched


def _tmdb_enrich_custom_list_page_for_storage(list_id: str, all_items: list, page_items: list) -> list:
    enriched = []
    changed = {}

    for item in page_items or []:
        if not isinstance(item, dict):
            continue
        display_item = _tmdb_custom_item_display_row(item)
        enriched.append(display_item)
        if display_item != item:
            key = tmdb_custom_lists._item_key(display_item.get("media_type"), display_item.get("tmdb_id"))
            if key != ":":
                changed[key] = display_item

    if changed:
        try:
            merged = []
            for item in all_items or []:
                if not isinstance(item, dict):
                    continue
                key = tmdb_custom_lists._item_key(item.get("media_type"), item.get("tmdb_id"))
                merged.append(changed.get(key, item))
            tmdb_custom_lists.replace_list_items_metadata(list_id, merged)
        except Exception as e:
            try:
                xbmc.log(f"[IKARUS][TMDB LISTS] custom list page metadata save error: {e}", xbmc.LOGWARNING)
            except Exception:
                pass

    return enriched


def _tmdb_add_custom_list_row(list_id: str, list_row: dict, item: dict, enrich: bool = True):
    display_item = _tmdb_custom_item_display_row(item) if enrich else dict(item or {})
    media_type = str((display_item or {}).get("media_type") or "").strip().lower()
    tmdb_id = str((display_item or {}).get("tmdb_id") or "").strip()
    try:
        status = _tmdb_merged_item_status(tmdb_id)
    except Exception:
        status = ""
    color = "lime" if status == "finished" else "orange" if status == "started" else ""
    extra_context = _tmdb_custom_item_delete_context_items(list_id, list_row, display_item)

    if media_type == "movie":
        _tmdb_add_playback_movie_row(
            display_item,
            color=color,
            label_prefix="[F] ",
            extra_context_items=extra_context,
            explicit_status=status,
        )
        return True
    if media_type == "tv":
        return _tmdb_add_playback_tv_row(
            display_item,
            color=color,
            label_prefix="[S] ",
            extra_context_items=extra_context,
            explicit_status=status,
        )
    return False




def tmdb_custom_list_menu():
    list_id = _get_param("list_id", "").strip()
    page = _get_int_param("page", 1)
    page_limit = 20
    with diagnostics.action_scope("tmdb.phase", "tmdb.custom.list.load", {"list_id": list_id}):
        row = tmdb_custom_lists.get_list(list_id)
    if not row:
        _placeholder_dir(_tmdb_my_lists_title(), i18n.T(31778, "(list was not found)"))
        return


    try:
        remote_count = int(row.get("trakt_item_count") or 0)
    except Exception:
        remote_count = 0
    local_count = len(row.get("items") or [])
    if remote_count > local_count and str(row.get("trakt_slug") or "").strip():
        with diagnostics.action_scope("tmdb.phase", "tmdb.custom.list.trakt.items", {"list_id": list_id}):
            try:
                result = tmdb_trakt_list_sync.sync_local_list_items_from_trakt(list_id=list_id)
                try:
                    xbmc.log(
                        "[IKARUS][TRAKT LIST SYNC] custom list open item refresh "
                        f"mode={result.get('mode')} list_id={list_id} "
                        f"remote={result.get('remote_items')} local={result.get('local_items')}",
                        xbmc.LOGINFO,
                    )
                except Exception:
                    pass
                row = tmdb_custom_lists.get_list(list_id) or row
            except Exception as e:
                try:
                    xbmc.log(f"[IKARUS][TRAKT LIST SYNC] custom list open item refresh failed list_id={list_id}: {e}", xbmc.LOGWARNING)
                except Exception:
                    pass

    name = str(row.get("name") or i18n.T(31723, "List")).strip()
    xbmcplugin.setContent(addon_handle, "files")

    added = 0
    with diagnostics.action_scope("tmdb.phase", "tmdb.custom.list.prepare", {"page": page}):
        raw_items = [item for item in (row.get("items") or []) if isinstance(item, dict)]
        if page < 1:
            page = 1
        items = sorted(raw_items, key=_tmdb_custom_list_year_sort_key, reverse=True)
        total_pages = max(1, (len(items) + page_limit - 1) // page_limit)
        if page > total_pages:
            page = total_pages
        xbmcplugin.setPluginCategory(
            addon_handle,
            i18n.T(31818, "{title} / Page {page}/{total_pages}").format(
                title=i18n.T(31827, "TMDB / My lists / {name}").format(name=name),
                page=page,
                total_pages=total_pages,
            ),
        )

    if items:
        with diagnostics.action_scope("tmdb.phase", "tmdb.custom.list.nav", {"page": page, "pages": total_pages}):
            _trakt_add_top_page_nav("tmdb.custom.list", page, total_pages, {"list_id": list_id})

    start_idx = (page - 1) * page_limit
    end_idx = start_idx + page_limit
    page_items = items[start_idx:end_idx]
    prefetch_pages = []
    if page < total_pages:
        prefetch_pages.append(page + 1)
    if page > 1:
        prefetch_pages.append(page - 1)
    if prefetch_pages:
        with diagnostics.action_scope("tmdb.phase", "tmdb.custom.list.queue_prefetch", {"page": page, "pages": ",".join(str(p) for p in prefetch_pages)}):
            tmdb_custom_prefetch.enqueue_pages(list_id, prefetch_pages, page_limit)

    with diagnostics.action_scope("tmdb.phase", "tmdb.custom.list.prefetch", {"page": page, "count": len(page_items)}):
        _tmdb_prefetch_custom_list_details(page_items)
    with diagnostics.action_scope("tmdb.phase", "tmdb.custom.list.enrich", {"page": page, "count": len(page_items)}):
        page_items = _tmdb_enrich_custom_list_page_for_storage(list_id, raw_items, page_items)

    with diagnostics.action_scope("tmdb.phase", "tmdb.custom.list.rows", {"page": page, "count": len(page_items)}):
        for item in page_items:
            if _tmdb_add_custom_list_fast_row(list_id, row, item):
                added += 1


    if added == 0:

        li = xbmcgui.ListItem(label=_tmdb_empty_list_text())

        li.setArt({"icon": "DefaultFolder.png", "thumb": "DefaultFolder.png", "poster": "DefaultFolder.png"})
        xbmcplugin.addDirectoryItem(handle=addon_handle, url="", listitem=li, isFolder=False)

    if page < total_pages:
        with diagnostics.action_scope("tmdb.phase", "tmdb.custom.list.next_nav", {"page": page + 1, "pages": total_pages}):
            _trakt_add_bottom_page_nav("tmdb.custom.list", page + 1, total_pages, {"list_id": list_id})

    _end("files")




def tmdb_custom_add():

    item = _tmdb_custom_item_from_params()

    if not item:

        xbmcgui.Dialog().notification(_tmdb_custom_lists_title(), i18n.T(31763, "The item could not be loaded."), xbmcgui.NOTIFICATION_ERROR, 3000)

        return



    rows = tmdb_custom_lists.all_lists()

    options = [i18n.T(31779, "New list")] + [str(row.get("name") or i18n.T(31723, "List")) for row in rows]

    choice = xbmcgui.Dialog().contextmenu(options)

    if choice < 0:

        return



    if choice == 0:
        payload = _tmdb_custom_list_create_dialog()
        if not payload:
            return
        target = tmdb_custom_lists.create_list(
            payload.get("name"),
            description=payload.get("description"),
            privacy=payload.get("privacy"),
        )
    else:
        target = rows[choice - 1]


    if not target:

        xbmcgui.Dialog().notification(_tmdb_custom_lists_title(), i18n.T(31725, "The list could not be created."), xbmcgui.NOTIFICATION_ERROR, 3000)

        return



    target_name = str(target.get("name") or i18n.T(31723, "List")).strip()

    ok = xbmcgui.Dialog().yesno(

        _tmdb_my_lists_title(),
        i18n.T(31825, "Add item to list {name}?").format(name=target_name),

        nolabel=i18n.T(31848, "Back"),

        yeslabel=i18n.T(31850, "Save")

    )

    if not ok:

        return



    saved, mode = tmdb_custom_lists.add_item(str(target.get("id") or ""), item)

    if saved:
        trakt_result = {"ok": False, "mode": "not_connected"}
        try:
            if trakt_client.is_connected():
                trakt_result = tmdb_trakt_list_sync.sync_local_list_to_trakt(str(target.get("id") or ""), only_item=item)
        except Exception as e:
            try:
                xbmc.log(f"[IKARUS][TRAKT LIST SYNC] add item sync error: {e}", xbmc.LOGWARNING)
            except Exception:
                pass
            trakt_result = {"ok": False, "mode": "error"}

        if trakt_result.get("ok"):
            msg = i18n.T(31981)
        elif trakt_result.get("mode") == "not_connected":
            msg = i18n.T(31982) if mode == "updated" else i18n.T(31983)
        else:
            msg = i18n.T(31984)
        xbmcgui.Dialog().notification(_tmdb_custom_lists_title(), msg, xbmcgui.NOTIFICATION_INFO, 3000)
        return
    else:

        xbmcgui.Dialog().notification(_tmdb_custom_lists_title(), i18n.T(31764, "Save failed."), xbmcgui.NOTIFICATION_ERROR, 3000)





def tmdb_trakt_add():
    item = _tmdb_custom_item_from_params()
    if not item:
        xbmcgui.Dialog().notification(_tmdb_trakt_title(), i18n.T(31763, "The item could not be loaded."), xbmcgui.NOTIFICATION_ERROR, 3000)
        return
    if not trakt_client.is_connected():
        xbmcgui.Dialog().notification(_tmdb_trakt_title(), _tmdb_not_trakt_logged_text(), xbmcgui.NOTIFICATION_WARNING, 3000)
        return

    try:
        rows = trakt_client.fetch_user_lists()
    except Exception as e:
        try:
            xbmc.log(f"[IKARUS][TRAKT] add list selection error: {e}", xbmc.LOGWARNING)
        except Exception:
            pass
        xbmcgui.Dialog().notification(_tmdb_trakt_title(), i18n.T(31765, "Lists could not be loaded."), xbmcgui.NOTIFICATION_ERROR, 3000)
        return

    lists = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        ids = row.get("ids") if isinstance(row.get("ids"), dict) else {}
        slug = str(ids.get("slug") or "").strip()
        name = str(row.get("name") or i18n.T(31723, "List")).strip()
        if slug:
            lists.append({"slug": slug, "name": name})

    if not lists:
        xbmcgui.Dialog().notification(_tmdb_trakt_title(), i18n.T(31766, "You do not have any Trakt lists."), xbmcgui.NOTIFICATION_WARNING, 3000)
        return

    choice = xbmcgui.Dialog().contextmenu([row["name"] for row in lists])
    if choice < 0:
        return
    target = lists[choice]
    target_name = target.get("name") or i18n.T(31723, "List")

    ok = xbmcgui.Dialog().yesno(
        _tmdb_trakt_title(),
        i18n.T(31825, "Add item to list {name}?").format(name=target_name),
        nolabel=i18n.T(31848, "Back"),
        yeslabel=i18n.T(31850, "Save")
    )
    if not ok:
        return

    media_type = str(item.get("media_type") or "").strip().lower()
    tmdb_id = str(item.get("tmdb_id") or "").strip()
    try:
        data = trakt_client.add_item_to_list(target.get("slug") or "", media_type, tmdb_id)
    except Exception as e:
        try:
            xbmc.log(f"[IKARUS][TRAKT] add item error: {e}", xbmc.LOGWARNING)
        except Exception:
            pass
        xbmcgui.Dialog().notification(_tmdb_trakt_title(), i18n.T(31767, "Adding to Trakt list failed."), xbmcgui.NOTIFICATION_ERROR, 3500)
        return

    added = data.get("added") if isinstance(data.get("added"), dict) else {}
    existing = data.get("existing") if isinstance(data.get("existing"), dict) else {}
    media_key = "movies" if media_type == "movie" else "shows"
    if int((added or {}).get(media_key) or 0) > 0:
        xbmcgui.Dialog().notification(_tmdb_trakt_title(), i18n.T(31768, "The item was added to the list."), xbmcgui.NOTIFICATION_INFO, 2500)
    elif int((existing or {}).get(media_key) or 0) > 0:
        xbmcgui.Dialog().notification(_tmdb_trakt_title(), i18n.T(31769, "The item is already in the list."), xbmcgui.NOTIFICATION_INFO, 2500)
    else:
        xbmcgui.Dialog().notification(_tmdb_trakt_title(), i18n.T(31770, "Trakt did not add the item."), xbmcgui.NOTIFICATION_WARNING, 3000)


def tmdb_trakt_remove():
    slug = _get_param("slug", "").strip()
    list_name = _get_param("list_name", i18n.T(31723, "List")).strip() or i18n.T(31723, "List")
    media_type = _get_param("media_type", "").strip().lower()
    tmdb_id = _get_param("id", "").strip()
    title = _get_param("title", i18n.T(31784, "item")).strip() or i18n.T(31784, "item")

    if not slug or media_type not in ("movie", "tv") or not tmdb_id:
        xbmcgui.Dialog().notification(_tmdb_trakt_title(), i18n.T(31771, "The item could not be removed."), xbmcgui.NOTIFICATION_ERROR, 3000)
        return
    if not trakt_client.is_connected():
        xbmcgui.Dialog().notification(_tmdb_trakt_title(), _tmdb_not_trakt_logged_text(), xbmcgui.NOTIFICATION_WARNING, 3000)
        return

    ok = xbmcgui.Dialog().yesno(
        _tmdb_trakt_title(),
        i18n.T(31826, "Remove {title} from list {list_name}?").format(title=title, list_name=list_name),
        nolabel=i18n.T(31848, "Back"),
        yeslabel=i18n.T(31851, "Remove")
    )
    if not ok:
        return

    try:
        data = trakt_client.remove_item_from_list(slug, media_type, tmdb_id)
    except Exception as e:
        try:
            xbmc.log(f"[IKARUS][TRAKT] remove item error: {e}", xbmc.LOGWARNING)
        except Exception:
            pass
        xbmcgui.Dialog().notification(_tmdb_trakt_title(), i18n.T(31772, "Removing from Trakt list failed."), xbmcgui.NOTIFICATION_ERROR, 3500)
        return

    deleted = data.get("deleted") if isinstance(data.get("deleted"), dict) else {}
    not_found = data.get("not_found") if isinstance(data.get("not_found"), dict) else {}
    media_key = "movies" if media_type == "movie" else "shows"
    if int((deleted or {}).get(media_key) or 0) > 0:
        xbmcgui.Dialog().notification(_tmdb_trakt_title(), i18n.T(31773, "The item was removed from the list."), xbmcgui.NOTIFICATION_INFO, 2500)
        try:
            xbmc.executebuiltin("Container.Refresh")
        except Exception:
            pass
    elif int((not_found or {}).get(media_key) or 0) > 0:
        xbmcgui.Dialog().notification(_tmdb_trakt_title(), i18n.T(31774, "The item was not found in the list."), xbmcgui.NOTIFICATION_WARNING, 3000)
        try:
            xbmc.executebuiltin("Container.Refresh")
        except Exception:
            pass
    else:
        xbmcgui.Dialog().notification(_tmdb_trakt_title(), i18n.T(31775, "Trakt did not remove the item."), xbmcgui.NOTIFICATION_WARNING, 3000)


def tmdb_custom_item_delete():
    list_id = _get_param("list_id", "").strip()

    media_type = _get_param("media_type", "").strip().lower()

    tmdb_id = _get_param("id", "").strip()

    if not list_id or media_type not in ("movie", "tv") or not tmdb_id:

        _tmdb_refresh_custom_list_container(list_id)

        return



    row = tmdb_custom_lists.get_list(list_id)

    if not row:

        _tmdb_refresh_custom_list_container(list_id)

        return



    found_item = None

    for item in row.get("items") or []:

        if str(item.get("media_type") or "").strip().lower() == media_type and str(item.get("tmdb_id") or "").strip() == tmdb_id:

            found_item = item

            break



    if not found_item:

        _tmdb_refresh_custom_list_container(list_id)

        return



    allowed, mode = tmdb_custom_lists.can_delete_item(row, found_item)

    if not allowed and mode == "item_owner_only":

        owner_name = tmdb_custom_lists.item_owner_name(found_item)

        msg = i18n.T(31782, "Only the device that added this item to the list can remove it.")

        if owner_name:

            msg = i18n.T(31783, "Only this device can remove the item: {owner}.").format(owner=owner_name)

        xbmcgui.Dialog().notification(_tmdb_custom_lists_title(), msg, xbmcgui.NOTIFICATION_WARNING, 3500)

        _tmdb_refresh_custom_list_container(list_id)

        return



    display_item = _tmdb_custom_item_display_row(found_item)
    title = str(display_item.get("title") or found_item.get("title") or i18n.T(31784, "item")).strip() or i18n.T(31784, "item")
    ok = xbmcgui.Dialog().yesno(
        _tmdb_custom_lists_title(),
        i18n.T(31791, "Remove item {title} from the list?").format(title=title),
        nolabel=i18n.T(31848, "Back"),
        yeslabel=i18n.T(31851, "Remove"),
    )

    if ok:

        deleted, mode = tmdb_custom_lists.delete_item(list_id, media_type, tmdb_id)
        if deleted:
            trakt_result = {"ok": False, "mode": "not_connected"}
            try:
                if trakt_client.is_connected():
                    trakt_result = tmdb_trakt_list_sync.sync_local_item_delete_to_trakt(list_id, media_type, tmdb_id)
            except Exception as e:
                try:
                    xbmc.log(f"[IKARUS][TRAKT LIST SYNC] delete item sync error: {e}", xbmc.LOGWARNING)
                except Exception:
                    pass
                trakt_result = {"ok": False, "mode": "error"}

            if trakt_result.get("ok") and trakt_result.get("mode") == "synced":
                msg = i18n.T(31985)
            elif trakt_result.get("mode") == "not_connected":
                msg = i18n.T(31986)
            elif trakt_result.get("ok"):
                msg = i18n.T(31986)
            else:
                msg = i18n.T(31987)
            xbmcgui.Dialog().notification(_tmdb_custom_lists_title(), msg, xbmcgui.NOTIFICATION_INFO, 3000)
        elif mode == "item_owner_only":
            owner_name = tmdb_custom_lists.item_owner_name(found_item)

            msg = i18n.T(31782, "Only the device that added this item to the list can remove it.")

            if owner_name:

                msg = i18n.T(31783, "Only this device can remove the item: {owner}.").format(owner=owner_name)

            xbmcgui.Dialog().notification(_tmdb_custom_lists_title(), msg, xbmcgui.NOTIFICATION_WARNING, 3500)



    _tmdb_refresh_custom_list_container(list_id)





def tmdb_custom_sync():
    ok = True

    mode = "synced"

    try:

        ok, mode = tmdb_custom_lists.auto_sync_if_due(force=True)

    except Exception:

        ok = False

        mode = "error"



    if ok:

        message = i18n.T(31785, "Sync completed.")

    elif mode == "busy":

        message = i18n.T(31786, "Sync is already running.")

    else:

        message = i18n.T(31787, "Sync failed.")



    xbmcgui.Dialog().notification(

        _tmdb_my_lists_title(),
        message,

        xbmcgui.NOTIFICATION_INFO if ok else xbmcgui.NOTIFICATION_ERROR,

        2500

    )

    try:

        xbmc.executebuiltin("Container.Refresh")

    except Exception:
        pass


def tmdb_custom_trakt_sync():
    if not trakt_client.is_connected():
        xbmcgui.Dialog().notification(_tmdb_my_lists_title(), _tmdb_not_trakt_logged_text(), xbmcgui.NOTIFICATION_WARNING, 3000)
        return

    progress = xbmcgui.DialogProgress()
    try:
        progress.create(_tmdb_my_lists_title(), i18n.T(31788, "Syncing with Trakt.TV..."))
    except Exception:
        progress = None

    try:
        result = tmdb_trakt_list_sync.sync_now(fetch_items=True, push_items=True)
    except Exception as e:
        try:
            xbmc.log(f"[IKARUS][TRAKT LIST SYNC] manual sync error: {e}", xbmc.LOGWARNING)
        except Exception:
            pass
        result = {"ok": False, "mode": "error"}
    finally:
        try:
            if progress:
                progress.close()
        except Exception:
            pass

    if result.get("ok"):
        message = i18n.T(31789, "Done. Linked: {linked}, created on Trakt.TV: {created}.").format(
            linked=int(result.get("linked") or 0),
            created=int(result.get("created") or 0),
        )
        icon = xbmcgui.NOTIFICATION_INFO
    elif result.get("mode") == "not_connected":
        message = _tmdb_not_trakt_logged_text()
        icon = xbmcgui.NOTIFICATION_WARNING
    else:
        message = i18n.T(31790, "Trakt.TV sync failed.")
        icon = xbmcgui.NOTIFICATION_ERROR
    xbmcgui.Dialog().notification(_tmdb_my_lists_title(), message, icon, 3500)
    _tmdb_refresh_custom_lists_container()


def tmdb_finished_list():
    page = _get_int_param("page", 1)
    with diagnostics.action_scope("tmdb.phase", "tmdb.finished.load", {"page": page}):
        rows = list(tmdb_playback_sync.iter_items_by_status("finished", include_trakt=True))
    _tmdb_paginated_playback_rows(i18n.T(31718, "TMDB / Watched"), "tmdb.finished", rows, page, "finished")



def tmdb_started_list():
    page = _get_int_param("page", 1)
    with diagnostics.action_scope("tmdb.phase", "tmdb.started.load", {"page": page}):
        rows = list(tmdb_playback_sync.iter_items_by_status("started", include_trakt=True))
    _tmdb_paginated_playback_rows(i18n.T(31719, "TMDB / In progress"), "tmdb.started", rows, page, "started")



def _tmdb_add_playback_movie_row(row: dict, color: str = "", label_prefix: str = "", extra_context_items=None,
                                return_action: str = "", explicit_status=None, return_item: bool = False):
    tmdb_id = str((row or {}).get("tmdb_id") or "").strip()

    title = str((row or {}).get("title") or _tmdb_untitled()).strip()

    year = str((row or {}).get("year") or "").strip()

    rating = (row or {}).get("rating")

    poster = str((row or {}).get("poster") or "").strip() or "DefaultVideo.png"
    plot = str((row or {}).get("plot") or "").strip()
    original_title = str((row or {}).get("original_title") or "").strip()

    suffix = ""
    try:

        if rating not in (None, ""):

            suffix = f" [TMDB {float(rating):.1f}]"

    except Exception:

        suffix = ""



    label = f"{label_prefix}{title} ({year}){suffix}" if year else f"{label_prefix}{title}{suffix}"
    explicit_status = (explicit_status or "").strip().lower()
    if explicit_status:
        label = _tmdb_colorize_status_label(label, explicit_status)
    elif color:
        label = f"[COLOR {color}]{label}[/COLOR]"

    li = xbmcgui.ListItem(label=label)
    li.setArt({"icon": poster, "thumb": poster, "poster": poster})
    info = {
        "title": title,
        "originaltitle": original_title,
        "plot": plot,
        "year": int(year) if year.isdigit() else 0,
        "rating": float(rating) if rating not in (None, "") else 0.0,
        "mediatype": "movie",
    }
    if explicit_status:
        _set_video_info(li, _tmdb_apply_explicit_status(li, explicit_status, info))
    else:
        _set_video_info(li, _tmdb_apply_watch_state(li, tmdb_id, info))
    status = explicit_status or (_tmdb_merged_item_status(tmdb_id) if tmdb_id else "")
    _tmdb_add_movie_watch_context(li, tmdb_id, title, status, return_action=return_action)

    try:

        extra_items = list(extra_context_items or [])

        if extra_items:

            li.addContextMenuItems(extra_items, replaceItems=False)

    except Exception:

        pass



    item = (
        build_url(_tmdb_add_watch_source({
            "action": "tmdb.movie.detail.open",
            "id": tmdb_id,
            "title": title,
            "original_title": original_title,
        }, "trakt" if str(return_action or "").startswith("tmdb.trakt.") else "")),
        li,
        False
    )
    if return_item:
        return item
    xbmcplugin.addDirectoryItem(
        handle=addon_handle,
        url=item[0],
        listitem=item[1],
        isFolder=item[2]
    )




def _tmdb_is_movie_playback_row(row: dict) -> bool:

    if not isinstance(row, dict):

        return False

    media_type = str(row.get("media_type") or "").strip().lower()

    if media_type in ("tv", "tvshow", "show", "series", "serial"):

        return False

    if media_type == "movie":

        return True

    sources = (row.get("sources") or {}).values() if isinstance(row.get("sources"), dict) else []

    for src in sources:

        if not isinstance(src, dict):

            continue

        mode = str(src.get("mode") or "").strip().lower()

        if mode in ("tv", "episode", "series", "serial", "serial_episode"):

            return False

        if mode == "movie":

            return True

        if src.get("season") not in (None, "") or src.get("episode") not in (None, ""):

            return False

    return True





def _tmdb_add_playback_tv_row(row: dict, color: str = "", label_prefix: str = "", extra_context_items=None,
                              return_action: str = "", explicit_status=None, return_item: bool = False):
    tmdb_id = str((row or {}).get("tmdb_id") or "").strip()

    media_type = str((row or {}).get("media_type") or "").strip().lower()

    if media_type and media_type not in ("tv", "series", "show", "serial"):

        return False



    title = str((row or {}).get("title") or _tmdb_untitled()).strip()

    year = str((row or {}).get("year") or "").strip()

    rating = (row or {}).get("rating")

    poster = str((row or {}).get("poster") or "").strip() or "DefaultTVShows.png"

    plot = str((row or {}).get("plot") or "").strip()
    original_title = str((row or {}).get("original_title") or "").strip()
    watch_source = str((row or {}).get("watch_source") or "").strip().lower()
    if not watch_source and str(return_action or "").startswith("tmdb.trakt."):
        watch_source = "trakt"
    if watch_source != "trakt":
        watch_source = ""

    suffix = ""
    try:

        if rating not in (None, ""):

            suffix = f" [TMDB {float(rating):.1f}]"

    except Exception:

        suffix = ""



    label = f"{label_prefix}{title} ({year}){suffix}" if year else f"{label_prefix}{title}{suffix}"
    explicit_status = (explicit_status or "").strip().lower()
    if explicit_status:
        label = _tmdb_colorize_status_label(label, explicit_status)
    elif color:
        label = f"[COLOR {color}]{label}[/COLOR]"

    li = xbmcgui.ListItem(label=label)
    li.setArt({"icon": poster, "thumb": poster, "poster": poster})
    info = {
        "title": title,
        "originaltitle": original_title,
        "tvshowtitle": title,
        "plot": plot,
        "year": int(year) if year.isdigit() else 0,
        "rating": float(rating) if rating not in (None, "") else 0.0,
        "mediatype": "tvshow",
    }
    if explicit_status:
        _set_video_info(li, _tmdb_apply_explicit_status(li, explicit_status, info))
    else:
        _set_video_info(li, _tmdb_apply_watch_state(li, tmdb_id, info))
    status = explicit_status or (_tmdb_merged_item_status(tmdb_id) if tmdb_id else "")
    _tmdb_add_tv_watch_context(li, tmdb_id, title, status, return_action=return_action)

    try:

        extra_items = list(extra_context_items or [])

        if extra_items:

            li.addContextMenuItems(extra_items, replaceItems=False)

    except Exception:

        pass



    item = (
        build_url(_tmdb_add_watch_source({
            "action": "tmdb.tv.detail.open",
            "id": tmdb_id,
            "title": title,
            "original_title": original_title,
        }, watch_source)),
        li,
        False
    )
    if return_item:
        return item
    xbmcplugin.addDirectoryItem(
        handle=addon_handle,
        url=item[0],
        listitem=item[1],
        isFolder=item[2]
    )
    return True




def tmdb_movies_menu():

    xbmcplugin.setPluginCategory(addon_handle, i18n.T(31100, "TMDB / Movies"))

    xbmcplugin.setContent(addon_handle, "files")

    _add_dir(i18n.T(30400, "Novinky"), {"action": "tmdb.movies.news"}, "DefaultRecentlyAddedMovies.png")

    _add_dir(i18n.T(30401, "Populární"), {"action": "tmdb.movies.popular"}, "DefaultMovies.png")

    _add_dir(i18n.T(30402, "Nejlépe hodnocené"), {"action": "tmdb.movies.top_rated"}, "DefaultSets.png")

    _add_dir(i18n.T(30403, "Podle žánru"), {"action": "tmdb.movies.genre"}, "DefaultGenre.png")

    _add_dir(i18n.T(30404, "Podle roku"), {"action": "tmdb.movies.year"}, "DefaultYear.png")

    _add_dir(i18n.T(30405, "Podle země původu"), {"action": "tmdb.movies.origin_country"}, "DefaultCountry.png")

    _add_dir(i18n.T(30406, "Podle původního jazyka"), {"action": "tmdb.movies.country"}, "DefaultCountry.png")

    _add_dir(i18n.T(30407, "Filmy - zhlédnuté"), {"action": "tmdb.movies.finished"}, "DefaultInProgressShows.png")

    _add_dir(i18n.T(30408, "Filmy - rozkoukané"), {"action": "tmdb.movies.started"}, "DefaultInProgressShows.png")

    _add_dir(i18n.T(30307, "Vyhledat"), {"action": "tmdb.movies.search"}, "DefaultAddonsSearch.png")

    _end("files")










def tmdb_movies_finished_list():
    page = _get_int_param("page", 1)
    title = i18n.T(31140, "TMDB / Movies / Watched")
    rows = [
        item for item in tmdb_playback_sync.iter_items_by_status("finished", include_trakt=True)
        if _tmdb_is_movie_playback_row(item[2])
    ]
    _tmdb_paginated_playback_media_rows(
        title,
        "tmdb.movies.finished",
        rows,
        page,
        "movie",
        "finished",
        "movies"
    )


def tmdb_movies_started_list():
    page = _get_int_param("page", 1)
    title = i18n.T(31141, "TMDB / Movies / In progress")
    rows = [
        item for item in tmdb_playback_sync.iter_items_by_status("started", include_trakt=True)
        if _tmdb_is_movie_playback_row(item[2])
    ]
    _tmdb_paginated_playback_media_rows(
        title,
        "tmdb.movies.started",
        rows,
        page,
        "movie",
        "started",
        "movies"
    )


def tmdb_series_finished_list():
    page = _get_int_param("page", 1)
    title = i18n.T(31142, "TMDB / Series / Watched")
    rows = [
        item for item in tmdb_playback_sync.iter_items_by_status("finished", include_trakt=True)
        if not _tmdb_is_movie_playback_row(item[2])
    ]
    _tmdb_paginated_playback_media_rows(
        title,
        "tmdb.series.finished",
        rows,
        page,
        "tv",
        "finished",
        "tvshows"
    )


def tmdb_series_started_list():
    page = _get_int_param("page", 1)
    title = i18n.T(31143, "TMDB / Series / In progress")
    rows = [
        item for item in tmdb_playback_sync.iter_items_by_status("started", include_trakt=True)
        if not _tmdb_is_movie_playback_row(item[2])
    ]
    _tmdb_paginated_playback_media_rows(
        title,
        "tmdb.series.started",
        rows,
        page,
        "tv",
        "started",
        "tvshows"
    )


def tmdb_series_top_rated_list():
    _tmdb_tv_list_from_endpoint("/tv/top_rated", i18n.T(31144, "TMDB / Series / Top rated"), page=_get_int_param("page", 1))





def tmdb_series_airing_today_list():

    _tmdb_tv_list_from_endpoint("/tv/airing_today", i18n.T(31145, "TMDB / Series / Airing today"), page=_get_int_param("page", 1))





def tmdb_series_trending_day_list():

    _tmdb_tv_list_from_endpoint("/trending/tv/day", i18n.T(31146, "TMDB / Trending / Series / Today"), page=_get_int_param("page", 1), extra_params={"language": TMDB_LANGUAGE})





def tmdb_series_trending_week_list():

    _tmdb_tv_list_from_endpoint("/trending/tv/week", i18n.T(31147, "TMDB / Trending / Series / This week"), page=_get_int_param("page", 1), extra_params={"language": TMDB_LANGUAGE})





def tmdb_series_search_menu():

    xbmcplugin.setPluginCategory(addon_handle, i18n.T(31113, "TMDB / Series / Search"))

    xbmcplugin.setContent(addon_handle, "files")

    _add_dir(i18n.T(30418, "Nov\u00e9 hled\u00e1n\u00ed"), {"action": "tmdb.series.search.input"}, "DefaultAddSource.png")

    _add_dir(i18n.T(30419, "Historie hled\u00e1n\u00ed"), {"action": "tmdb.series.search.history"}, "DefaultAddonsSearch.png")

    _end("files")





def tmdb_series_search_input():

    search_title = i18n.T(31113, "TMDB / Series / Search")
    q = xbmcgui.Dialog().input(search_title, type=xbmcgui.INPUT_ALPHANUM)

    if not q or not q.strip():

        _placeholder_dir(search_title, i18n.T(31116, "(no search query entered)"))

        return

    q = q.strip()

    _tmdb_history_add(TMDB_SERIES_HISTORY_FILE, q)

    _tmdb_single_stacked_search_list(i18n.T(31123, "TMDB / Series / Search / {query}").format(query=q), q, "tv", page=1)





def tmdb_series_search_history_menu():

    xbmcplugin.setPluginCategory(addon_handle, i18n.T(31119, "TMDB / Series / Search history"))

    xbmcplugin.setContent(addon_handle, "files")

    _add_dir(i18n.T(30420, "[Vymazat historii]"), {"action": "tmdb.series.search.history.clear"}, "DefaultAddonContextItem.png")

    history = _tmdb_history_load(TMDB_SERIES_HISTORY_FILE)

    if not history:

        xbmcplugin.addDirectoryItem(handle=addon_handle, url="", listitem=xbmcgui.ListItem(label=i18n.T(30421, "Historie hled\u00e1n\u00ed je pr\u00e1zdn\u00e1")), isFolder=False)

        _end("files")

        return

    for q in history:

        _add_dir(q, {"action": "tmdb.series.search.results", "query": q, "page": 1}, "DefaultAddonsSearch.png")

    _end("files")





def tmdb_series_search_history_clear():

    _tmdb_history_save(TMDB_SERIES_HISTORY_FILE, [])

    xbmcgui.Dialog().notification(i18n.T(31104, "TMDB / Series"), i18n.T(30422, "Historie hled\u00e1n\u00ed byla vymaz\u00e1na."), xbmcgui.NOTIFICATION_INFO, 2200)

    tmdb_series_search_history_menu()





def tmdb_series_search_results():

    q = _get_param("query", "").strip()

    if not q:

        _placeholder_dir(i18n.T(31113, "TMDB / Series / Search"), i18n.T(31117, "(missing search query)"))

        return

    _tmdb_history_add(TMDB_SERIES_HISTORY_FILE, q)

    _tmdb_single_stacked_search_list(i18n.T(31123, "TMDB / Series / Search / {query}").format(query=q), q, "tv", page=_get_int_param("page", 1))





def tmdb_series_genre_menu():

    genres = _tmdb_tv_genres()

    xbmcplugin.setPluginCategory(addon_handle, i18n.T(31148, "TMDB / Series / By genre"))

    xbmcplugin.setContent(addon_handle, "files")

    if not genres:

        _placeholder_dir(i18n.T(31148, "TMDB / Series / By genre"), i18n.T(31160, "(no genre data from TMDB)"))

        return

    for g in genres:

        _add_dir(g["name"], {"action": "tmdb.series.genre.list", "genre_id": str(g["id"]), "genre_name": g["name"], "page": "1"}, "DefaultGenre.png")

    _end("files")





def tmdb_series_by_genre_list():

    genre_id = _get_param("genre_id", "")

    genre_name = _get_param("genre_name", i18n.T(31941))

    if not genre_id:

        _placeholder_dir(i18n.T(31148, "TMDB / Series / By genre"), i18n.T(31164, "(missing genre_id)"))

        return

    _tmdb_tv_list_from_endpoint("/discover/tv", i18n.T(31149, "TMDB / Series / By genre / {genre}").format(genre=genre_name), page=_get_int_param("page", 1), extra_params={"with_genres": genre_id, "sort_by": "popularity.desc", "include_adult": "false"})





def tmdb_series_year_menu():

    years = _tmdb_tv_years(1900)

    xbmcplugin.setPluginCategory(addon_handle, i18n.T(31150, "TMDB / Series / By year"))

    xbmcplugin.setContent(addon_handle, "files")

    for year in years:

        _add_dir(str(year), {"action": "tmdb.series.year.list", "year": str(year), "page": "1"}, "DefaultYear.png")

    _end("files")





def tmdb_series_by_year_list():

    year = _get_param("year", "")

    if not year:

        _placeholder_dir(i18n.T(31150, "TMDB / Series / By year"), i18n.T(31165, "(missing year)"))

        return

    _tmdb_tv_list_from_endpoint("/discover/tv", i18n.T(31151, "TMDB / Series / By year / {year}").format(year=year), page=_get_int_param("page", 1), extra_params={"first_air_date_year": year, "sort_by": "popularity.desc", "include_adult": "false"})





def tmdb_series_country_menu():

    countries = _tmdb_tv_countries()

    xbmcplugin.setPluginCategory(addon_handle, i18n.T(31152, "TMDB / Series / By original language"))

    xbmcplugin.setContent(addon_handle, "files")

    for c in countries:

        _add_dir(c["name"], {"action": "tmdb.series.country.list", "country_code": c["code"], "country_name": c["name"], "page": "1"}, "DefaultCountry.png")

    _end("files")





def tmdb_series_by_country_list():

    country_code = _get_param("country_code", "")

    country_name = _get_param("country_name", i18n.T(31993))

    if not country_code:

        _placeholder_dir(i18n.T(31152, "TMDB / Series / By original language"), i18n.T(31166, "(missing country_code)"))

        return

    _tmdb_tv_list_from_endpoint("/discover/tv", i18n.T(31153, "TMDB / Series / By original language / {country}").format(country=country_name), page=_get_int_param("page", 1), extra_params={"with_original_language": country_code, "sort_by": "first_air_date.desc", "include_adult": "false"})





def tmdb_series_origin_country_menu():

    countries = _tmdb_tv_origin_countries()

    xbmcplugin.setPluginCategory(addon_handle, i18n.T(31154, "TMDB / Series / By country of origin"))

    xbmcplugin.setContent(addon_handle, "files")

    for c in countries:

        _add_dir(c["name"], {"action": "tmdb.series.origin_country.list", "country_code": c["code"], "country_name": c["name"], "page": "1"}, "DefaultCountry.png")

    _end("files")





def tmdb_series_by_origin_country_list():

    country_code = _get_param("country_code", "")

    country_name = _get_param("country_name", i18n.T(31942))

    if not country_code:

        _placeholder_dir(i18n.T(31154, "TMDB / Series / By country of origin"), i18n.T(31166, "(missing country_code)"))

        return

    _tmdb_tv_list_from_endpoint("/discover/tv", i18n.T(31155, "TMDB / Series / By country of origin / {country}").format(country=country_name), page=_get_int_param("page", 1), extra_params={"with_origin_country": country_code, "sort_by": "first_air_date.desc", "include_adult": "false"}, next_action="tmdb.series.origin_country.list", persist_params={"country_code": country_code, "country_name": country_name})





def tmdb_people_popular_list():

    _tmdb_people_list_from_endpoint("/person/popular", i18n.T(31156, "TMDB / People / Popular"), page=_get_int_param("page", 1))





def tmdb_people_trending_day_list():

    _tmdb_people_list_from_endpoint("/trending/person/day", i18n.T(31157, "TMDB / Trending / People / Today"), page=_get_int_param("page", 1), extra_params={"language": TMDB_LANGUAGE})





def tmdb_people_trending_week_list():

    _tmdb_people_list_from_endpoint("/trending/person/week", i18n.T(31158, "TMDB / Trending / People / This week"), page=_get_int_param("page", 1), extra_params={"language": TMDB_LANGUAGE})





def tmdb_people_search_menu():

    xbmcplugin.setPluginCategory(addon_handle, i18n.T(31114, "TMDB / People / Search"))

    xbmcplugin.setContent(addon_handle, "files")

    _add_dir(i18n.T(30418, "Nov\u00e9 hled\u00e1n\u00ed"), {"action": "tmdb.people.search.input"}, "DefaultAddSource.png")

    _add_dir(i18n.T(30419, "Historie hled\u00e1n\u00ed"), {"action": "tmdb.people.search.history"}, "DefaultAddonsSearch.png")

    _end("files")





def tmdb_people_search_input():

    search_title = i18n.T(31114, "TMDB / People / Search")
    q = xbmcgui.Dialog().input(search_title, type=xbmcgui.INPUT_ALPHANUM)

    if not q or not q.strip():

        _placeholder_dir(search_title, i18n.T(31116, "(no search query entered)"))

        return

    q = q.strip()

    _tmdb_history_add(TMDB_PEOPLE_HISTORY_FILE, q)

    _tmdb_people_list_from_endpoint("/search/person", i18n.T(31124, "TMDB / People / Search / {query}").format(query=q), page=1, extra_params={"query": q}, next_action="tmdb.people.search.results", persist_params={"query": q})





def tmdb_people_search_history_menu():

    xbmcplugin.setPluginCategory(addon_handle, i18n.T(31120, "TMDB / People / Search history"))

    xbmcplugin.setContent(addon_handle, "files")

    _add_dir(i18n.T(30420, "[Vymazat historii]"), {"action": "tmdb.people.search.history.clear"}, "DefaultAddonContextItem.png")

    history = _tmdb_history_load(TMDB_PEOPLE_HISTORY_FILE)

    if not history:

        xbmcplugin.addDirectoryItem(handle=addon_handle, url="", listitem=xbmcgui.ListItem(label=i18n.T(30421, "Historie hled\u00e1n\u00ed je pr\u00e1zdn\u00e1")), isFolder=False)

        _end("files")

        return

    for q in history:

        _add_dir(q, {"action": "tmdb.people.search.results", "query": q, "page": 1}, "DefaultAddonsSearch.png")

    _end("files")





def tmdb_people_search_history_clear():

    _tmdb_history_save(TMDB_PEOPLE_HISTORY_FILE, [])

    xbmcgui.Dialog().notification(i18n.T(31105, "TMDB / People"), i18n.T(30422, "Historie hled\u00e1n\u00ed byla vymaz\u00e1na."), xbmcgui.NOTIFICATION_INFO, 2200)

    tmdb_people_search_history_menu()





def tmdb_people_search_results():

    q = _get_param("query", "").strip()

    if not q:

        _placeholder_dir(i18n.T(31114, "TMDB / People / Search"), i18n.T(31117, "(missing search query)"))

        return

    _tmdb_history_add(TMDB_PEOPLE_HISTORY_FILE, q)

    _tmdb_people_list_from_endpoint("/search/person", i18n.T(31124, "TMDB / People / Search / {query}").format(query=q), page=_get_int_param("page", 1), extra_params={"query": q}, next_action="tmdb.people.search.results", persist_params={"query": q})





def tmdb_multi_search_menu():

    xbmcplugin.setPluginCategory(addon_handle, i18n.T(31115, "TMDB / Search / All"))

    xbmcplugin.setContent(addon_handle, "files")

    _add_dir(i18n.T(30418, "Nov\u00e9 hled\u00e1n\u00ed"), {"action": "tmdb.search.multi.input"}, "DefaultAddSource.png")

    _add_dir(i18n.T(30419, "Historie hled\u00e1n\u00ed"), {"action": "tmdb.search.multi.history"}, "DefaultAddonsSearch.png")

    _end("files")





def tmdb_multi_search_input():

    search_title = i18n.T(31115, "TMDB / Search / All")
    q = xbmcgui.Dialog().input(search_title, type=xbmcgui.INPUT_ALPHANUM)

    if not q or not q.strip():

        _placeholder_dir(search_title, i18n.T(31116, "(no search query entered)"))

        return

    q = q.strip()

    _tmdb_history_add(TMDB_MULTI_HISTORY_FILE, q)

    _tmdb_multi_stacked_search_list(i18n.T(31125, "TMDB / Search / All / {query}").format(query=q), q, page=1)





def tmdb_multi_search_history_menu():

    xbmcplugin.setPluginCategory(addon_handle, i18n.T(31121, "TMDB / Search / All / Search history"))

    xbmcplugin.setContent(addon_handle, "files")

    _add_dir(i18n.T(30420, "[Vymazat historii]"), {"action": "tmdb.search.multi.history.clear"}, "DefaultAddonContextItem.png")

    history = _tmdb_history_load(TMDB_MULTI_HISTORY_FILE)

    if not history:

        xbmcplugin.addDirectoryItem(handle=addon_handle, url="", listitem=xbmcgui.ListItem(label=i18n.T(30421, "Historie hled\u00e1n\u00ed je pr\u00e1zdn\u00e1")), isFolder=False)

        _end("files")

        return

    for q in history:

        _add_dir(q, {"action": "tmdb.search.multi.results", "query": q, "page": 1}, "DefaultAddonsSearch.png")

    _end("files")





def tmdb_multi_search_history_clear():

    _tmdb_history_save(TMDB_MULTI_HISTORY_FILE, [])

    xbmcgui.Dialog().notification(i18n.T(31115, "TMDB / Search / All"), i18n.T(30422, "Historie hled\u00e1n\u00ed byla vymaz\u00e1na."), xbmcgui.NOTIFICATION_INFO, 2200)

    tmdb_multi_search_history_menu()





def tmdb_multi_search_results():

    q = _get_param("query", "").strip()

    if not q:

        _placeholder_dir(i18n.T(31115, "TMDB / Search / All"), i18n.T(31117, "(missing search query)"))

        return

    _tmdb_history_add(TMDB_MULTI_HISTORY_FILE, q)

    _tmdb_multi_stacked_search_list(i18n.T(31125, "TMDB / Search / All / {query}").format(query=q), q, page=_get_int_param("page", 1))





# ---------------------------

# ROUTER

# ---------------------------

def _router_impl():
    _refresh_runtime()
    action = _get_param("action", "")


    if not action:

        tmdb_movies_menu()

        return



    if action == "tmdb.nav.home":
        tmdb_nav_home()
        return

    if action == "tmdb.nav.replace":
        tmdb_nav_replace()
        return

    if action == "tmdb.nav.replace.parent":
        tmdb_nav_replace_with_parent()
        return

    if action == "tmdb.movies":
        tmdb_movies_menu()
        return


    if action == "tmdb.movies.news":

        tmdb_movies_news_menu()

        return



    if action == "tmdb.movies.popular":

        tmdb_movies_popular_menu()

        return



    if action == "tmdb.movies.popular.list":

        tmdb_movies_popular_list()

        return



    if action == "tmdb.movies.popular.provider.list":

        tmdb_movies_popular_provider_list()

        return



    if action == "tmdb.movies.top_rated":

        tmdb_movies_top_rated_list()

        return



    if action == "tmdb.movies.genre":

        tmdb_movies_genre_menu()

        return



    if action == "tmdb.movies.genre.list":

        tmdb_movies_by_genre_list()

        return



    if action == "tmdb.movies.year":

        tmdb_movies_year_menu()

        return



    if action == "tmdb.movies.year.list":

        tmdb_movies_by_year_list()

        return



    if action == "tmdb.movies.origin_country":

        tmdb_movies_origin_country_menu()

        return



    if action == "tmdb.movies.origin_country.list":

        tmdb_movies_by_origin_country_list()

        return



    if action == "tmdb.movies.country":

        tmdb_movies_country_menu()

        return



    if action == "tmdb.movies.country.list":

        tmdb_movies_by_country_list()

        return



    if action == "tmdb.movies.finished":

        tmdb_movies_finished_list()

        return



    if action == "tmdb.movies.started":

        tmdb_movies_started_list()

        return



    if action == "tmdb.movies.search":

        tmdb_movies_search_menu()

        return



    if action == "tmdb.movies.search.input":

        tmdb_movies_search_input()

        return



    if action == "tmdb.movies.search.history":

        tmdb_movies_search_history_menu()

        return



    if action == "tmdb.movies.search.history.clear":

        tmdb_movies_search_history_clear()

        return



    if action == "tmdb.movies.search.results":

        tmdb_movies_search_results()

        return



    if action == "tmdb.movie.detail":

        tmdb_movie_detail_menu()

        return



    if action == "tmdb.movie.detail.open":

        tmdb_movie_detail_open()

        return



    if action == "tmdb.movie.mark.finished":

        tmdb_movie_mark_finished()

        return



    if action == "tmdb.movie.watch.clear":

        tmdb_movie_watch_clear()

        return



    if action == "tmdb.tv.mark.finished":

        tmdb_tv_mark_finished()

        return



    if action == "tmdb.tv.watch.clear":

        tmdb_tv_watch_clear()

        return



    if action == "tmdb.tv.season.mark.finished":

        tmdb_tv_season_mark_finished()

        return



    if action == "tmdb.tv.season.watch.clear":

        tmdb_tv_season_watch_clear()

        return



    if action == "tmdb.tv.episode.mark.finished":

        tmdb_tv_episode_mark_finished()

        return



    if action == "tmdb.tv.episode.watch.clear":

        tmdb_tv_episode_watch_clear()

        return



    if action == "tmdb.tv.detail.open":

        tmdb_tv_detail_open()

        return



    if action == "tmdb.tv.seasons.open":

        tmdb_tv_seasons_open()

        return



    if action == "tmdb.tv.season.episodes.open":

        tmdb_tv_season_episodes_open()

        return



    if action == "tmdb.tv.episode.sources.open":

        tmdb_tv_episode_sources_open()

        return



    if action == "tmdb.movie.info":

        tmdb_movie_info_menu()

        return



    if action == "tmdb.movie.cast":

        tmdb_movie_cast_menu()

        return



    if action == "tmdb.movie.department":

        tmdb_movie_department_menu()

        return



    if action == "tmdb.movie.similar":
        tmdb_movie_similar_list()
        return

    if action == "tmdb.movie.media":
        tmdb_movie_media_menu()
        return

    if action == "tmdb.movie.media.popular":
        tmdb_movie_media_popular_menu()
        return

    if action == "tmdb.movie.media.backdrops":
        tmdb_movie_media_backdrops_menu()
        return

    if action == "tmdb.movie.media.posters":
        tmdb_movie_media_posters_menu()
        return

    if action == "tmdb.movie.sources.search.stub":
        tmdb_movie_sources_search_stub_menu()
        return

    if action == "tmdb.movie.sources.webshare.group":
        tmdb_movie_sources_webshare_group_menu()
        return

    if action == "tmdb.movie.sources.provider.group":
        tmdb_movie_sources_provider_group_menu()
        return

    if action == "tmdb.movie.source.play":
        tmdb_movie_source_play()
        return


    if action == "tmdb.movie.source.download":

        tmdb_movie_source_download()

        return



    if action == "tmdb.downloads.queue":

        tmdb_downloads_queue_menu()

        return



    if action == "tmdb.downloads.queue.clear_done":

        tmdb_downloads_queue_clear_done()

        return



    if action == "tmdb.downloads.queue.clear_all":

        tmdb_downloads_queue_clear_all()

        return



    if action == "tmdb.custom.lists":
        tmdb_custom_lists_menu()
        return

    if action == "tmdb.trakt.lists":
        tmdb_trakt_lists_menu()
        return

    if action == "tmdb.trakt.watched":
        tmdb_trakt_watched_menu()
        return

    if action == "tmdb.trakt.started":
        tmdb_trakt_started_menu()
        return

    if action == "tmdb.playback.trakt.sync":
        tmdb_playback_trakt_sync()
        return

    if action == "tmdb.trakt.smart.lists":
        tmdb_trakt_smart_lists_menu()
        return

    if action == "tmdb.trakt.smart.list.create":
        tmdb_trakt_smart_list_create()
        return

    if action == "tmdb.trakt.smart.list":
        tmdb_trakt_smart_list_menu()
        return

    if action == "tmdb.trakt.public.lists":
        tmdb_trakt_public_lists_menu()
        return

    if action == "tmdb.trakt.public.search.input":
        tmdb_trakt_public_search_input()
        return

    if action == "tmdb.trakt.public.user.input":
        tmdb_trakt_public_user_input()
        return

    if action == "tmdb.trakt.public.user.search.input":
        tmdb_trakt_public_user_search_input()
        return

    if action == "tmdb.trakt.public.favorites":
        tmdb_trakt_public_favorites_menu()
        return

    if action == "tmdb.trakt.public.favorite.add":
        tmdb_trakt_public_favorite_add()
        return

    if action == "tmdb.trakt.public.favorite.remove":
        tmdb_trakt_public_favorite_remove()
        return

    if action == "tmdb.trakt.user.lists":
        tmdb_trakt_user_lists_menu()
        return

    if action == "tmdb.trakt.list.create":
        tmdb_trakt_list_create()
        return

    if action == "tmdb.trakt.list.edit":
        tmdb_trakt_list_edit()
        return

    if action == "tmdb.trakt.list.delete":
        tmdb_trakt_list_delete()
        return

    if action == "tmdb.trakt.list":
        tmdb_trakt_list_menu()
        return

    if action == "tmdb.custom.list":
        tmdb_custom_list_menu()
        return


    if action == "tmdb.custom.list.create":

        tmdb_custom_list_create()

        return



    if action == "tmdb.custom.list.rename":

        tmdb_custom_list_rename()

        return



    if action == "tmdb.custom.list.delete":

        tmdb_custom_list_delete()

        return



    if action == "tmdb.custom.item.delete":

        tmdb_custom_item_delete()

        return



    if action == "tmdb.custom.sync":
        tmdb_custom_sync()
        return

    if action == "tmdb.custom.trakt.sync":
        tmdb_custom_trakt_sync()
        return

    if action == "tmdb.custom.add":
        tmdb_custom_add()
        return

    if action == "tmdb.trakt.add":
        tmdb_trakt_add()
        return

    if action == "tmdb.trakt.remove":
        tmdb_trakt_remove()
        return

    if action == "tmdb.video.play":
        tmdb_video_play()
        return

    if action == "tmdb.youtube.search":
        tmdb_youtube_search_menu()
        return

    if action == "tmdb.image.download":
        _tmdb_image_download()
        return

    if action == "tmdb.movie.videos":
        tmdb_movie_videos_menu()
        return


    if action == "tmdb.series":

        tmdb_series_menu()

        return



    if action == "tmdb.series.popular":

        tmdb_series_popular_menu()

        return



    if action == "tmdb.series.popular.list":

        tmdb_series_popular_list()

        return



    if action == "tmdb.series.popular.provider.list":

        tmdb_series_popular_provider_list()

        return



    if action == "tmdb.series.top_rated":

        tmdb_series_top_rated_list()

        return



    if action == "tmdb.series.airing_today":

        tmdb_series_airing_today_list()

        return



    if action == "tmdb.series.search":

        tmdb_series_search_menu()

        return



    if action == "tmdb.series.search.input":

        tmdb_series_search_input()

        return



    if action == "tmdb.series.search.history":

        tmdb_series_search_history_menu()

        return



    if action == "tmdb.series.search.history.clear":

        tmdb_series_search_history_clear()

        return



    if action == "tmdb.series.search.results":

        tmdb_series_search_results()

        return



    if action == "tmdb.tv.detail":

        tmdb_tv_detail_menu()

        return



    if action == "tmdb.tv.info":

        tmdb_tv_info_menu()

        return



    if action == "tmdb.tv.cast":

        tmdb_tv_cast_menu()

        return



    if action == "tmdb.tv.department":

        tmdb_tv_department_menu()

        return



    if action == "tmdb.tv.similar":
        tmdb_tv_similar_list()
        return

    if action == "tmdb.tv.media":
        tmdb_tv_media_menu()
        return

    if action == "tmdb.tv.media.popular":
        tmdb_tv_media_popular_menu()
        return

    if action == "tmdb.tv.media.backdrops":
        tmdb_tv_media_backdrops_menu()
        return

    if action == "tmdb.tv.media.posters":
        tmdb_tv_media_posters_menu()
        return

    if action == "tmdb.tv.videos":
        tmdb_tv_videos_menu()
        return


    if action == "tmdb.tv.seasons":

        tmdb_tv_seasons_menu()

        return



    if action == "tmdb.tv.season.episodes":

        tmdb_tv_season_episodes_menu()

        return

    

    if action == "tmdb.series.genre":

        tmdb_series_genre_menu()

        return



    if action == "tmdb.series.genre.list":

        tmdb_series_by_genre_list()

        return



    if action == "tmdb.series.year":

        tmdb_series_year_menu()

        return



    if action == "tmdb.series.year.list":

        tmdb_series_by_year_list()

        return



    if action == "tmdb.series.origin_country":

        tmdb_series_origin_country_menu()

        return



    if action == "tmdb.series.origin_country.list":

        tmdb_series_by_origin_country_list()

        return



    if action == "tmdb.series.country":

        tmdb_series_country_menu()

        return



    if action == "tmdb.series.country.list":

        tmdb_series_by_country_list()

        return



    if action == "tmdb.series.finished":

        tmdb_series_finished_list()

        return



    if action == "tmdb.series.started":

        tmdb_series_started_list()

        return

    

    if action == "tmdb.people":

        tmdb_people_menu()

        return



    if action == "tmdb.people.popular":

        tmdb_people_popular_list()

        return



    if action == "tmdb.people.search":

        tmdb_people_search_menu()

        return



    if action == "tmdb.people.search.input":

        tmdb_people_search_input()

        return



    if action == "tmdb.people.search.history":

        tmdb_people_search_history_menu()

        return



    if action == "tmdb.people.search.history.clear":

        tmdb_people_search_history_clear()

        return



    if action == "tmdb.people.search.results":

        tmdb_people_search_results()

        return



    if action == "tmdb.person.detail":

        tmdb_person_detail_menu()

        return



    if action == "tmdb.person.credits":

        tmdb_person_credits_menu()

        return



    if action == "tmdb.person.credits.department":

        tmdb_person_credits_department_menu()

        return



    if action == "tmdb.person.credits.movies":

        tmdb_person_credits_movies_menu()

        return



    if action == "tmdb.person.credits.tv":

        tmdb_person_credits_tv_menu()

        return



    if action == "tmdb.trending":

        tmdb_trending_menu()

        return



    if action == "tmdb.finished":

        tmdb_finished_list()

        return



    if action == "tmdb.started":

        tmdb_started_list()

        return



    if action == "tmdb.trending.movies.day":

        tmdb_movies_trending_day_list()

        return



    if action == "tmdb.trending.movies.week":

        tmdb_movies_trending_week_list()

        return



    if action == "tmdb.trending.series.day":

        tmdb_series_trending_day_list()

        return



    if action == "tmdb.trending.series.week":

        tmdb_series_trending_week_list()

        return



    if action == "tmdb.trending.people.day":

        tmdb_people_trending_day_list()

        return



    if action == "tmdb.trending.people.week":

        tmdb_people_trending_week_list()

        return



    if action == "tmdb.search":

        tmdb_search_menu()

        return

    

    if action == "tmdb.search.movies":

        tmdb_movies_search_menu()

        return

    

    if action == "tmdb.search.series":

        tmdb_series_search_menu()

        return



    if action == "tmdb.search.people":

        tmdb_people_search_menu()

        return



    if action == "tmdb.search.multi":

        tmdb_multi_search_menu()

        return



    if action == "tmdb.search.multi.input":

        tmdb_multi_search_input()

        return



    if action == "tmdb.search.multi.history":

        tmdb_multi_search_history_menu()

        return



    if action == "tmdb.search.multi.history.clear":

        tmdb_multi_search_history_clear()

        return



    if action == "tmdb.search.multi.results":

        tmdb_multi_search_results()

        return



    if action == "tmdb.stub":

        tmdb_stub()

        return



    _placeholder_dir("TMDB", i18n.T(31816, "(unknown action: {action})").format(action=action))


def _tmdb_param_value(*names):
    for name in names:
        value = _get_param(name, "").strip()
        if value:
            return value
    return ""


def _tmdb_missing_param(action: str, name: str):
    try:
        xbmc.log(f"[IKARUS][TMDB][PARAM] missing action={action} param={name}", xbmc.LOGWARNING)
    except Exception:
        pass
    _placeholder_dir("TMDB", i18n.T(31817, "(missing required parameter: {name})").format(name=name))


def _tmdb_validate_action_params(action: str) -> bool:
    action = (action or "").strip()

    if action in {
        "tmdb.movie.detail",
        "tmdb.movie.detail.open",
        "tmdb.movie.info",
        "tmdb.movie.cast",
        "tmdb.movie.department",
        "tmdb.movie.similar",
        "tmdb.movie.media",
        "tmdb.movie.media.popular",
        "tmdb.movie.media.backdrops",
        "tmdb.movie.media.posters",
        "tmdb.movie.videos",
        "tmdb.movie.mark.finished",
        "tmdb.movie.watch.clear",
        "tmdb.movie.sources.search.stub",
        "tmdb.movie.sources.webshare.group",
        "tmdb.movie.sources.provider.group",
        "tmdb.movie.source.play",
        "tmdb.movie.source.download",
    } and not _tmdb_param_value("id", "tmdb_id"):
        _tmdb_missing_param(action, "id")
        return False

    if action in {
        "tmdb.tv.detail",
        "tmdb.tv.detail.open",
        "tmdb.tv.info",
        "tmdb.tv.cast",
        "tmdb.tv.department",
        "tmdb.tv.similar",
        "tmdb.tv.media",
        "tmdb.tv.media.popular",
        "tmdb.tv.media.backdrops",
        "tmdb.tv.media.posters",
        "tmdb.tv.videos",
        "tmdb.tv.seasons",
        "tmdb.tv.seasons.open",
        "tmdb.tv.season.episodes",
        "tmdb.tv.season.episodes.open",
        "tmdb.tv.episode.sources.open",
        "tmdb.tv.mark.finished",
        "tmdb.tv.watch.clear",
        "tmdb.tv.season.mark.finished",
        "tmdb.tv.season.watch.clear",
        "tmdb.tv.episode.mark.finished",
        "tmdb.tv.episode.watch.clear",
    } and not _tmdb_param_value("id", "tmdb_id"):
        _tmdb_missing_param(action, "id")
        return False

    if action in {
        "tmdb.tv.season.episodes",
        "tmdb.tv.season.episodes.open",
        "tmdb.tv.episode.sources.open",
        "tmdb.tv.season.mark.finished",
        "tmdb.tv.season.watch.clear",
        "tmdb.tv.episode.mark.finished",
        "tmdb.tv.episode.watch.clear",
    } and not _tmdb_param_value("season", "season_number"):
        _tmdb_missing_param(action, "season")
        return False

    if action in {
        "tmdb.tv.episode.sources.open",
        "tmdb.tv.episode.mark.finished",
        "tmdb.tv.episode.watch.clear",
    } and not _tmdb_param_value("episode", "episode_number"):
        _tmdb_missing_param(action, "episode")
        return False

    if action in {
        "tmdb.person.detail",
        "tmdb.person.credits",
        "tmdb.person.credits.department",
        "tmdb.person.credits.movies",
        "tmdb.person.credits.tv",
    } and not _tmdb_param_value("id", "person_id"):
        _tmdb_missing_param(action, "id")
        return False

    if action in {
        "tmdb.custom.list",
        "tmdb.custom.list.rename",
        "tmdb.custom.list.delete",
        "tmdb.custom.item.delete",
    } and not _tmdb_param_value("list_id"):
        _tmdb_missing_param(action, "list_id")
        return False

    if action in {
        "tmdb.movies.popular.provider.list",
        "tmdb.series.popular.provider.list",
    } and not _tmdb_param_value("provider_id"):
        _tmdb_missing_param(action, "provider_id")
        return False

    if action in {
        "tmdb.movies.genre.list",
        "tmdb.series.genre.list",
    } and not _tmdb_param_value("genre_id"):
        _tmdb_missing_param(action, "genre_id")
        return False

    if action in {
        "tmdb.movies.year.list",
        "tmdb.series.year.list",
    } and not _tmdb_param_value("year"):
        _tmdb_missing_param(action, "year")
        return False

    if action in {
        "tmdb.movies.country.list",
        "tmdb.movies.origin_country.list",
        "tmdb.series.country.list",
        "tmdb.series.origin_country.list",
    } and not _tmdb_param_value("country_code"):
        _tmdb_missing_param(action, "country_code")
        return False

    if action in {
        "tmdb.trakt.list",
        "tmdb.trakt.public.favorite.add",
        "tmdb.trakt.public.favorite.remove",
    } and not _tmdb_param_value("user", "username"):
        _tmdb_missing_param(action, "user")
        return False

    if action in {
        "tmdb.trakt.list",
        "tmdb.trakt.list.edit",
        "tmdb.trakt.list.delete",
        "tmdb.trakt.public.favorite.add",
        "tmdb.trakt.public.favorite.remove",
    } and not _tmdb_param_value("slug"):
        _tmdb_missing_param(action, "slug")
        return False

    if action in {
        "tmdb.trakt.smart.list",
    } and not _tmdb_param_value("path"):
        _tmdb_missing_param(action, "path")
        return False

    return True


def router():
    _refresh_runtime()
    action = _get_param("action", "") or "tmdb.movies"
    with diagnostics.action_scope("tmdb", action):
        if not _tmdb_validate_action_params(action):
            return
        return _router_impl()


