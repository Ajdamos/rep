# -*- coding: utf-8 -*-
import sys
import urllib.parse
import re
import html
import requests
import xbmcgui
import xbmcplugin
import xbmcaddon
import xbmc
import xbmcvfs
import urllib.request
import urllib.error
import http.cookiejar
import json
import xml.etree.ElementTree as ET
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed

from bs4 import BeautifulSoup

from resources.lib import auto_next_policy, diagnostics, i18n, playback_utils, source_common, source_labels
from resources.lib.source_sktonline import (
    sktonline_abs as _sktonline_abs,
    sktonline_extract_download_links as _sktonline_extract_download_links,
    sktonline_extract_sources_from_scripts as _sktonline_extract_sources_from_scripts,
    sktonline_extract_video_sources as _sktonline_extract_video_sources,
    sktonline_find_embed_url_anywhere as _sktonline_find_embed_url_anywhere,
    sktonline_guess_label_from_url as _sktonline_guess_label_from_url,
    sktonline_merge_sources as _sktonline_merge_sources,
)
from resources.lib.source_hellspy import (
    hellspy_build_search_url as _hellspy_build_search_url,
    hellspy_extract_items_and_next as _hellspy_extract_items_and_next,
    hellspy_pick_direct_url as _hellspy_pick_direct_url,
)
from resources.lib.source_prehrajto import (
    prehrajto_build_search_url as _build_prehrajto_search_url,
    prehrajto_canonicalize_url as _canonicalize_prehrajto_url,
    prehrajto_extract_first_source_from_html as _try_extract_sources_from_html,
    prehrajto_extract_sources_from_html as _prehrajto_extract_sources_from_html,
)

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


_COOKIEJAR = http.cookiejar.CookieJar()
_OPENER = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(_COOKIEJAR)
)
_SKT_SESSION = requests.Session()
_SKT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/75.0.3770.142 Safari/537.36"
    ),
    "Referer": "https://online.sktorrent.eu",
    "Accept-Encoding": "identity",
}
_SOURCE_SEARCH_CACHE = {}
_SOURCE_SEARCH_CACHE_TTL = 20 * 60  # 20 minut
_SOURCE_SEARCH_FILTER_VERSION = "episode-filter-v13-auto-next-retry"
_WS_FILE_INFO_CACHE = {}
_WS_TOKEN_CACHE = {"token": "", "checked_at": 0.0}
_WS_TOKEN_CACHE_TTL = 10 * 60
_PREHRAJTO_DETAIL_CACHE = {}
_HELLSPY_DEBUG_DUMPED = set()
_SKTONLINE_WARMED = False
_HANDLED_REFRESH_TOKENS = {}
_HANDLED_REFRESH_TOKEN_TTL = 10 * 60  # 10 minut



def _setting_enabled(setting_id: str, default: bool = True) -> bool:
    value = (addon.getSetting(setting_id) or "").strip().lower()
    if not value:
        return default
    return value == "true"


def _webshare_connected() -> bool:
    status = (addon.getSetting("status") or "").strip().lower()
    return status in ("připojeno", "pripojeno", "přihlášen", "prihlasen")


_sanitize_url = source_common.sanitize_url

def _http_get(url: str, timeout: int = 25, referer: str = None) -> str:
    return source_common.http_get(url, _OPENER, timeout=timeout, referer=referer)


def _http_get_json(url: str, timeout: int = 25, referer: str = None, origin: str = None) -> dict:
    return source_common.http_get_json(url, _OPENER, timeout=timeout, referer=referer, origin=origin)

def _skt_http_get(url: str, referer: str = None, timeout: int = 25) -> str:
    headers = dict(_SKT_HEADERS)
    if referer:
        headers["Referer"] = referer

    resp = _SKT_SESSION.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp.text or ""

# -----------------------------------------------
#             STAHOVANI ZDROJE
# -----------------------------------------------

def _get_setting_bool(setting_id: str, default: bool = False) -> bool:
    try:
        val = addon.getSetting(setting_id)
        return str(val).strip().lower() == "true"
    except Exception:
        return default

def _get_setting_text(setting_id: str, default: str = "") -> str:
    try:
        return (addon.getSetting(setting_id) or default).strip()
    except Exception:
        return default

def _ensure_dir(path: str) -> bool:
    try:
        if not path:
            return False
        if xbmcvfs.exists(path):
            return True
        return xbmcvfs.mkdirs(path)
    except Exception:
        return False

_safe_filename = source_common.safe_filename

def _guess_ext_from_url(url: str) -> str:
    ul = (url or "").lower()
    for ext in (".m3u8", ".mp4", ".mkv", ".avi", ".ts", ".webm"):
        if ext in ul:
            return ext
    return ".mp4"


# ---------------------------
# PLAY HELPERS + RESOLVERS
# ---------------------------

_is_direct_media_url = source_common.is_direct_media_url

def _kodi_url_with_headers(url: str, headers: dict) -> str:
    return playback_utils.kodi_url_with_headers(url, headers)

def play_url(title: str, stream_url: str, headers: dict = None):
    _refresh_runtime()
    try:
        stream_url = _sanitize_url(stream_url)
    except Exception:
        pass

    playback_utils.set_resolved_playback(
        addon_handle,
        title,
        stream_url,
        headers=headers,
        mimetype="application/octet-stream",
    )
    return


def _fail_playback():
    _refresh_runtime()
    playback_utils.fail_playback(addon_handle)




def _prehrajto_build_headers(page_url: str, stream_url: str) -> dict:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Referer": _sanitize_url(page_url),
        "Origin": _PREHRAJTO_BASE,
        "Accept": "*/*",
    }
    ck = _cookie_header_for_url(stream_url)
    if ck:
        headers["Cookie"] = ck
    return headers

def prehrajto_try_resolve_and_play(title: str, page_url: str) -> bool:
    direct, headers = _prehrajto_resolve_direct_url(page_url)
    if not direct:
        return False
    play_url(title, direct, headers=headers)
    return True

_dump_text_to_temp = source_common.dump_text_to_temp







def _http_follow_to_final_url(url: str, referer: str = None, timeout: int = 25) -> str:
    return source_common.follow_to_final_url(url, _OPENER, referer=referer, timeout=timeout)


def _cookie_header_for_url(url: str) -> str:
    return source_common.cookie_header_for_url(_COOKIEJAR, url, relaxed_domain=True)

def _probe_stream(url: str, headers: dict, timeout: int = 15):
    return source_common.probe_stream(url, headers, _OPENER, timeout=timeout, log_prefix="[IKARUS][SKT][PROBE]")

def _hellspy_stream_is_available(url: str, headers: dict):
    result = source_common.probe_stream(
        url,
        headers,
        _OPENER,
        timeout=10,
        log_prefix="[IKARUS][HELLSPY][PROBE]",
    )
    if isinstance(result, dict) and result.get("ok") is False:
        return False, result.get("error") or "unknown probe error"
    return True, ""

def sktonline_try_resolve_and_play(title: str, page_url: str) -> bool:
    detail_html = ""
    embed_html = ""
    embed_url = ""

    try:
        detail_html = _http_get(page_url, referer=_SKTONLINE_BASE)
    except Exception as e:
        _dump_text_to_temp("skt_detail.html", detail_html)
        _dump_text_to_temp("skt_embed.html", embed_html)
        _dump_text_to_temp("skt_debug.txt", f"page_url={page_url}\nembed_url={embed_url}\nerror={repr(e)}\n")
        return False

    embed_url = _sktonline_find_embed_url_anywhere(detail_html, page_url)
    embed_html = ""
    if embed_url:
        try:
            embed_html = _http_get(embed_url, referer=page_url)
        except Exception as e:
            embed_html = ""
            _dump_text_to_temp("skt_debug.txt", f"page_url={page_url}\nembed_url={embed_url}\nembed_error={repr(e)}\n")

    sources_detail = _sktonline_extract_video_sources(detail_html, page_url)
    sources_embed = _sktonline_extract_video_sources(embed_html, embed_url) if embed_html and embed_url else []

    sources_script_detail = _sktonline_extract_sources_from_scripts(detail_html, page_url)
    sources_script_embed = _sktonline_extract_sources_from_scripts(embed_html, embed_url) if embed_html and embed_url else []

    sources = _sktonline_merge_sources(sources_detail, sources_embed, sources_script_detail, sources_script_embed)

    if not sources:
        dl = []
        dl += _sktonline_extract_download_links(detail_html, page_url)
        if embed_html and embed_url:
            dl += _sktonline_extract_download_links(embed_html, embed_url)

        for it in dl:
            durl = it.get("url")
            if not durl:
                continue
            final = _http_follow_to_final_url(durl, referer=embed_url or page_url)
            if final and _is_direct_media_url(final):
                sources.append({"label": it.get("label") or _sktonline_guess_label_from_url(final), "src": final})

    if not sources:
        return False

    labels = [(s.get("label") or i18n.T(30728, "Source")).strip() for s in sources]

    ref = _sanitize_url(page_url)

    base_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Referer": ref,
        "Origin": "https://online.sktorrent.eu",
        "Accept": "*/*",
    }

    if len(sources) == 1:
        src1 = (sources[0].get("src") or "").strip()
        headers = dict(base_headers)

        ck = _cookie_header_for_url(src1)
        if ck:
            headers["Cookie"] = ck

        xbmc.log(f"[IKARUS][SKT] PLAY: {source_common.redact_url_for_log(src1)}", xbmc.LOGWARNING)
        xbmc.log(f"[IKARUS][SKT] HDR: {source_common.redact_headers_for_log(headers)}", xbmc.LOGWARNING)
        _probe_stream(src1, headers)
        play_url(title, src1, headers=headers)
        return True

    idx = xbmcgui.Dialog().select(i18n.T(30727, "Select quality"), labels)
    if idx is None or idx < 0:
        return True

    picked = sources[idx]
    stream_url = (picked.get("src") or "").strip()
    if not stream_url:
        return False

    headers = dict(base_headers)
    ck = _cookie_header_for_url(stream_url)
    if ck:
        headers["Cookie"] = ck

    picked_label = (picked.get("label") or "").strip()
    play_title = f"{title} ({picked_label})" if picked_label else title

    xbmc.log(f"[IKARUS][SKT] PLAY: {source_common.redact_url_for_log(stream_url)}", xbmc.LOGWARNING)
    xbmc.log(f"[IKARUS][SKT] HDR: {source_common.redact_headers_for_log(headers)}", xbmc.LOGWARNING)
    _probe_stream(stream_url, headers)
    play_url(play_title, stream_url, headers=headers)
    return True

def _add_playable_result(label: str, action_query: dict, plot: str = "", icon: str = "DefaultVideo.png"):
    li = xbmcgui.ListItem(label=label)
    li.setArt({
        "icon": icon,
        "thumb": icon,
        "poster": icon
    })
    if plot:
        li.setInfo("video", {"title": label, "plot": plot})
    li.setProperty("IsPlayable", "true")

    download_query = dict(action_query or {})
    download_query["action"] = "tmdb.movie.source.download"

    li.addContextMenuItems([
        (
            i18n.T(30737, "Download source"),
            "RunPlugin(%s)" % build_url(download_query)
        )
    ])

    item_url = build_url(action_query)

    try:
        provider = ((action_query or {}).get("provider") or "").strip().lower()
    except Exception:
        provider = ""

    try:
        action_name = ((action_query or {}).get("action") or "").strip()
    except Exception:
        action_name = ""

    if action_name == "tmdb.movie.source.play":
        # This action starts playback itself and records TMDB progress. Marking
        # it as playable makes Kodi expect setResolvedUrl(True) from this item,
        # which can show a false "Playback failed" dialog while video is already playing.
        li.setProperty("IsPlayable", "false")

    try:
        raw_url = ((action_query or {}).get("url") or "").strip()
    except Exception:
        raw_url = ""

    if (
        provider == "webshare"
        and action_name != "tmdb.movie.source.play"
        and raw_url.startswith("plugin://plugin.video.ikarus/?action=film.play")
    ):
        item_url = raw_url
        xbmc.log(f"[IKARUS][WS] clickable item redirected directly to film.play: {raw_url}", xbmc.LOGWARNING)

    xbmcplugin.addDirectoryItem(
        handle=addon_handle,
        url=item_url,
        listitem=li,
        isFolder=False
    )

# ---------------------------
# WEBSHARE SEARCH
# ---------------------------

_WS_BASE = "https://webshare.cz"
_WS_API = _WS_BASE + "/api/"
_WS_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Referer": _WS_BASE,
}
_WS_TIMEOUT = 20
_WS_MENU_TIMEOUT = 8

def _ws_api(fnct: str, data: dict, timeout: int = None):
    resp = requests.post(
        _WS_API + fnct + "/",
        data=data,
        headers=_WS_HEADERS,
        timeout=timeout or _WS_TIMEOUT
    )
    resp.raise_for_status()
    return resp

def _ws_search_simple(query: str, limit: int = 40, offset: int = 0):
    """
    Minimalistické hledání na WebShare.
    Vrací:
      [{"id": ident, "name": name, "size": size_int}, ...]
    """
    query = " ".join((query or "").split()).strip()
    if not query:
        return []

    data = {
        "what": query,
        "offset": int(offset or 0),
        "limit": int(limit),
        "category": "video",
    }

    try:
        root = ET.fromstring(_ws_api("search", data, timeout=_WS_MENU_TIMEOUT).content)
        if (root.findtext("status") or "OK") != "OK":
            return []

        out = []
        for f in root.findall("file"):
            ident = (f.findtext("ident") or "").strip()
            name = (f.findtext("name") or "").strip()
            size_raw = (f.findtext("size") or "0").strip()
            img = (f.findtext("img") or "").strip()

            try:
                size_int = int(size_raw) if str(size_raw).isdigit() else 0
            except Exception:
                size_int = 0

            if not ident:
                continue

            row = {
                "id": ident,
                "name": name or ident,
                "size": size_int,
            }
            if img:
                row["img"] = img
            out.append(row)

        return out
    except Exception as e:
        xbmc.log(f"[IKARUS][WS] SEARCH FAILED: {repr(e)}", xbmc.LOGERROR)
        return []

_ws_xml_streams = source_common.xml_streams

def _ws_file_info(ident: str, token: str = ""):
    ident = (ident or "").strip()
    if not ident:
        return {}

    cached = _WS_FILE_INFO_CACHE.get(ident)
    if cached is not None:
        return dict(cached)

    token = (token or "").strip()
    if not token:
        token = _ws_login_token()
    if not token:
        _WS_FILE_INFO_CACHE[ident] = {}
        return {}

    root = None
    for maybe_removed in ("", "true"):
        data = {"ident": ident, "wst": token}
        if maybe_removed:
            data["maybe_removed"] = maybe_removed
        try:
            root = ET.fromstring(_ws_api("file_info", data, timeout=_WS_MENU_TIMEOUT).content or b"")
        except Exception as e:
            xbmc.log(f"[IKARUS][WS] file_info failed ident={ident}: {repr(e)}", xbmc.LOGWARNING)
            root = None

        if root is not None and (root.findtext("status") or "").strip().upper() == "OK":
            break
        root = None

    if root is None:
        _WS_FILE_INFO_CACHE[ident] = {}
        return {}

    def text(name: str) -> str:
        try:
            return (root.findtext(name) or "").strip()
        except Exception:
            return ""

    info = {
        "ws_file_info": True,
        "ws_info_name": text("name"),
        "file_type": text("type"),
        "width": text("width"),
        "height": text("height"),
        "format": text("format"),
        "fps": text("fps"),
        "bitrate": text("bitrate"),
        "available": text("available"),
        "password": text("password"),
        "removed": text("removed"),
        "removed_at": text("removed_at"),
        "removal_reason": text("removal_reason"),
        "copyrighted": text("copyrighted"),
        "video_streams": _ws_xml_streams(root, "video"),
        "audio_streams": _ws_xml_streams(root, "audio"),
    }

    size_bytes = _source_int_from_any(text("size"), 0)
    if size_bytes > 0:
        info["size_bytes"] = size_bytes

    _WS_FILE_INFO_CACHE[ident] = dict(info)
    return info

def _enrich_webshare_rows(rows):
    started_at = __import__("time").time()
    rows = list(rows or [])
    if not rows:
        return rows

    token = _ws_login_token()
    if not token:
        return rows

    missing_idents = []
    seen_missing = set()
    for row in rows:
        ident = str((row or {}).get("id") or "").strip()
        if not ident:
            continue
        if ident in _WS_FILE_INFO_CACHE:
            continue
        if ident in seen_missing:
            continue
        seen_missing.add(ident)
        missing_idents.append(ident)

    if len(missing_idents) > 1:
        max_workers = min(6, len(missing_idents))
        try:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {
                    executor.submit(_ws_file_info, ident, token): ident
                    for ident in missing_idents
                }
                for future in as_completed(futures):
                    ident = futures[future]
                    try:
                        future.result()
                    except Exception as e:
                        xbmc.log(f"[IKARUS][WS] file_info parallel failed ident={ident}: {repr(e)}", xbmc.LOGWARNING)
        except Exception as e:
            xbmc.log(f"[IKARUS][WS] parallel enrich failed, fallback to sequential: {repr(e)}", xbmc.LOGWARNING)
            for ident in missing_idents:
                try:
                    _ws_file_info(ident, token)
                except Exception:
                    pass

    out = []
    for row in rows:
        item = dict(row)
        ident = (item.get("id") or "").strip()
        if ident:
            info = _ws_file_info(ident, token)
            if info:
                original_size = item.get("size_bytes") or item.get("size") or 0
                item.update(info)
                if not item.get("size_bytes") and original_size:
                    item["size_bytes"] = original_size
        out.append(item)

    elapsed = __import__("time").time() - started_at
    xbmc.log(
        f"[IKARUS][WS] enrich rows={len(rows)} fetched={len(missing_idents)} sec={elapsed:.2f}",
        xbmc.LOGWARNING
    )
    return out





def _ws_device_uuid() -> str:
    duuid = _ws_get_setting("ws_device_uuid", "")
    if duuid:
        return duuid

    try:
        import uuid
        duuid = str(uuid.uuid4())
    except Exception:
        duuid = "ikarus-device"

    try:
        addon.setSetting("ws_device_uuid", duuid)
    except Exception:
        pass

    return duuid


def _ws_get_setting(setting_id: str, default: str = "") -> str:
    try:
        return (addon.getSetting(setting_id) or default).strip()
    except Exception:
        return default




def _ws_login_token():
    now_ts = __import__("time").time()
    cached_token = str(_WS_TOKEN_CACHE.get("token") or "").strip()
    checked_at = float(_WS_TOKEN_CACHE.get("checked_at") or 0.0)
    if cached_token and checked_at and (now_ts - checked_at) < _WS_TOKEN_CACHE_TTL:
        return cached_token

    try:
        from resources.lib import nastaveni
    except Exception:
        try:
            import nastaveni
        except Exception as e:
            xbmc.log(f"[IKARUS][WS][DL] import nastaveni failed: {repr(e)}", xbmc.LOGERROR)
            return ""

    # 1) zkus použít stejný token jako přehrávání
    try:
        token = (addon.getSetting("token") or "").strip()
    except Exception:
        token = ""

    try:
        if token and nastaveni.over_token(token):
            _WS_TOKEN_CACHE["token"] = token
            _WS_TOKEN_CACHE["checked_at"] = now_ts
            xbmc.log("[IKARUS][WS][DL] using existing shared token", xbmc.LOGWARNING)
            return token
    except Exception as e:
        xbmc.log(f"[IKARUS][WS][DL] over_token(existing) failed: {repr(e)}", xbmc.LOGERROR)

    # 2) vezmi stejné přihlašovací údaje jako playback
    username = ""
    password = ""
    try:
        username = (addon.getSetting("username") or "").strip()
    except Exception:
        pass
    try:
        password = (addon.getSetting("password") or "").strip()
    except Exception:
        pass

    try:
        token = (nastaveni.ziskej_token(username, password) or "").strip()
    except Exception as e:
        xbmc.log(f"[IKARUS][WS][DL] ziskej_token failed: {repr(e)}", xbmc.LOGERROR)
        token = ""

    if token:
        _WS_TOKEN_CACHE["token"] = token
        _WS_TOKEN_CACHE["checked_at"] = now_ts
        try:
            addon.setSetting("token", token)
        except Exception:
            pass
        try:
            addon.setSetting("ws_token", token)
        except Exception:
            pass
        xbmc.log("[IKARUS][WS][DL] shared token acquired via nastaveni.ziskej_token()", xbmc.LOGWARNING)

    return token

def _ws_file_link(ident: str, token: str, download_type: str = "video_stream"):
    if not ident or not token:
        return ""

    try:
        resp = _ws_api("file_link", {
            "ident": ident,
            "wst": token,
            "download_type": download_type,
            "device_uuid": _ws_device_uuid(),
        })
        raw = resp.content or b""
        xbmc.log(f"[IKARUS][WS] file_link raw ({download_type}): {raw[:500]!r}", xbmc.LOGWARNING)
        root = ET.fromstring(raw)
    except Exception as e:
        xbmc.log(f"[IKARUS][WS] file_link parse failed: {repr(e)}", xbmc.LOGERROR)
        return ""

    status = (root.findtext("status") or "").strip()
    xbmc.log(f"[IKARUS][WS] file_link ident={ident} type={download_type} status={status}", xbmc.LOGWARNING)

    if status.upper() != "OK":
        code = (root.findtext("code") or "").strip()
        message = (root.findtext("message") or "").strip()
        xbmc.log(f"[IKARUS][WS] file_link fail code={code} message={message}", xbmc.LOGERROR)
        return ""

    for tag in ("link", "url", "file_link", "download_link"):
        val = (root.findtext(tag) or "").strip()
        if val:
            xbmc.log(f"[IKARUS][WS] direct link found in <{tag}>: {val[:150]}", xbmc.LOGWARNING)
            return val

    for elem in root.iter():
        txt = (elem.text or "").strip()
        if txt.startswith("http://") or txt.startswith("https://"):
            xbmc.log(f"[IKARUS][WS] direct link found generic in <{elem.tag}>: {txt[:150]}", xbmc.LOGWARNING)
            return txt

    return ""


def _ws_resolve_direct_url(file_id: str):
    ident = (file_id or "").strip()
    xbmc.log(f"[IKARUS][WS][DL] resolve direct for ident={ident}", xbmc.LOGWARNING)

    if not ident:
        return "", {}

    token = _ws_login_token()
    if not token:
        return "", {}

    direct = _ws_file_link(ident, token, "file_download")
    if not direct:
        return "", {}

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "*/*",
        "Referer": _WS_BASE,
        "Cookie": "wst=" + token,
    }

    xbmc.log(f"[IKARUS][WS][DL] resolved direct: {'YES' if direct else 'NO'}", xbmc.LOGWARNING)
    return direct, headers




# ---------------------------
# PREHRAJ.TO SEARCH
# ---------------------------

_PREHRAJTO_BASE = "https://prehrajto.cz"
_PREHRAJTO_DETAIL_RE = re.compile(r"^/[^/]+/[0-9a-f]{10,}$", re.IGNORECASE)

def _extract_prehrajto_results(html_text: str):
    results = []
    seen = set()
    html_text = html_text or ""

    for m in re.finditer(r'<a[^>]+href="(?P<href>/[^"]+)"(?P<rest>[^>]*)>', html_text, re.IGNORECASE):
        href = (m.group("href") or "").strip()
        rest = m.group("rest") or ""

        if not href or href.startswith("#") or href.lower().startswith("javascript:"):
            continue
        if not _PREHRAJTO_DETAIL_RE.match(href):
            continue

        full = urllib.parse.urljoin(_PREHRAJTO_BASE, href)
        full = _canonicalize_prehrajto_url(full)

        if full in seen:
            continue
        seen.add(full)

        title = ""
        mt = re.search(r'title="([^"]+)"', rest, re.IGNORECASE)
        if mt:
            title = html.unescape(mt.group(1)).strip()

        if not title or len(title) < 2:
            seg = href.strip("/").split("/", 1)[0]
            title = seg

        title = re.sub(r"\s+", " ", title).strip()

        context_start = max(0, m.start() - 900)
        context_end = min(len(html_text), m.end() + 1800)
        context = html_text[context_start:context_end]
        size_bytes = _source_size_from_text(context)

        results.append({
            "title": title,
            "url": full,
            "size_bytes": size_bytes,
        })

    return results

def _prehrajto_param_values(detail_html: str) -> dict:
    detail_html = detail_html or ""
    if not detail_html:
        return {}

    try:
        soup = BeautifulSoup(detail_html, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        lines = [
            html.unescape(x).replace("\xa0", " ").strip()
            for x in soup.get_text("\n").splitlines()
            if html.unescape(x).replace("\xa0", " ").strip()
        ]
    except Exception:
        text = re.sub(r"<[^>]+>", "\n", detail_html)
        lines = [
            html.unescape(x).replace("\xa0", " ").strip()
            for x in text.splitlines()
            if html.unescape(x).replace("\xa0", " ").strip()
        ]

    wanted = {
        "nazev souboru": "name",
        "velikost": "size",
        "datum nahrani": "upload_date",
        "delka": "duration",
        "format": "file_type",
        "rozliseni": "resolution",
    }
    labels = set(wanted.keys())
    params = {}

    i = 0
    while i < len(lines):
        label_norm = _norm_text(lines[i]).strip(":")
        key = wanted.get(label_norm)
        if not key:
            i += 1
            continue

        values = []
        j = i + 1
        while j < len(lines):
            next_norm = _norm_text(lines[j]).strip(":")
            if next_norm in labels:
                break
            values.append(lines[j])
            if key != "name":
                break
            j += 1

        value = " ".join(values).strip()
        if value:
            params[key] = re.sub(r"\s+", " ", value).strip()
        i = max(j, i + 1)

    return params

def _prehrajto_metadata_from_detail(detail_html: str) -> dict:
    params = _prehrajto_param_values(detail_html)
    if not params:
        return {}

    meta = {}
    size_bytes = _source_size_from_text(params.get("size") or "")
    if size_bytes > 0:
        meta["size_bytes"] = size_bytes

    if params.get("duration"):
        meta["duration"] = params["duration"]
    if params.get("file_type"):
        meta["file_type"] = params["file_type"].lower().lstrip(".")
    if params.get("upload_date"):
        meta["upload_date"] = params["upload_date"]
    if params.get("name"):
        meta["source_name"] = params["name"]

    res = params.get("resolution") or ""
    m = re.search(r"(\d{3,5})\s*[x×]\s*(\d{3,5})", res, re.IGNORECASE)
    if m:
        meta["width"] = m.group(1)
        meta["height"] = m.group(2)

    return meta

def _prehrajto_detail_metadata(page_url: str) -> dict:
    page_url = (page_url or "").strip()
    if not page_url:
        return {}
    if page_url in _PREHRAJTO_DETAIL_CACHE:
        return dict(_PREHRAJTO_DETAIL_CACHE.get(page_url) or {})

    try:
        detail_html = _http_get(page_url, referer=_PREHRAJTO_BASE)
        meta = _prehrajto_metadata_from_detail(detail_html)
    except Exception as e:
        xbmc.log(f"[IKARUS][PREHRAJTO] detail metadata failed: {repr(e)}", xbmc.LOGWARNING)
        meta = {}

    _PREHRAJTO_DETAIL_CACHE[page_url] = dict(meta or {})
    return meta or {}

def _enrich_prehrajto_rows(rows, limit: int = 30):
    enriched = []
    count = 0

    for row in (rows or []):
        item = dict(row)
        if count < limit:
            meta = _prehrajto_detail_metadata(item.get("url") or "")
            if meta:
                item.update({k: v for k, v in meta.items() if v not in (None, "", [], {}, 0)})
            count += 1
        enriched.append(item)

    return enriched

def prehrajto_search_all_pages(query: str, max_pages: int = 8):
    all_results = []
    seen_urls = set()
    q = " ".join((query or "").split()).strip()
    has_episode_code = _query_has_episode_code(q)
    page_limit = max_pages
    if has_episode_code:
        page_limit = min(page_limit, 2)
    elif len(_title_tokens(q)) >= 2:
        page_limit = min(page_limit, 5)

    for page in range(1, page_limit + 1):
        url = _build_prehrajto_search_url(query, page)
        try:
            html_text = _http_get(url)
        except urllib.error.HTTPError as e:
            xbmc.log(
                f"[IKARUS][PREHRAJTO] query={q!r} page={page}/{page_limit} HTTP {getattr(e, 'code', '?')} - keeping {len(all_results)} results",
                xbmc.LOGWARNING
            )
            break
        except Exception as e:
            xbmc.log(
                f"[IKARUS][PREHRAJTO] query={q!r} page={page}/{page_limit} failed: {repr(e)} - keeping {len(all_results)} results",
                xbmc.LOGWARNING
            )
            break
        page_results = _extract_prehrajto_results(html_text)
        xbmc.log(
            f"[IKARUS][PREHRAJTO] query={q!r} page={page}/{page_limit} results={len(page_results)}",
            xbmc.LOGWARNING
        )
        if page == 1 and not page_results:
            qkey = _cache_safe_key_part(q) or "empty"
            _dump_text_to_temp(f"prehrajto_search_{qkey}_page1.html", html_text or "")

        if not page_results:
            break

        new_added = 0
        for it in page_results:
            if it["url"] in seen_urls:
                continue
            seen_urls.add(it["url"])
            all_results.append(it)
            new_added += 1

        if new_added == 0:
            break

    return all_results


# ---------------------------
# SKTONLINE SEARCH
# ---------------------------

_SKTONLINE_BASE = "https://online.sktorrent.eu/"


def _sktonline_result_matches_query(title: str, wanted_query: str = "") -> bool:
    tokens = [
        t for t in _norm_text(wanted_query).split()
        if len(t) >= 3 and not t.isdigit()
    ]
    if not tokens:
        return True

    title_norm = _norm_text(title)
    return all(t in title_norm for t in tokens)


def _sktonline_extract_results(results_html: str, base_url: str, wanted_query: str = ""):
    out = []
    seen = set()

    html_doc = BeautifulSoup(results_html or "", "html.parser")
    posts = html_doc.find_all("div", {"class": "well well-sm"}, True)

    for post in posts:
        links = post.select("a")
        spans = post.select("span")
        imgs = post.select("img")

        if not links:
            continue
        if not spans:
            continue
        if not imgs:
            continue

        href = (links[0].get("href") or "").strip()
        if not href:
            continue

        full = _sktonline_abs(href, base_url)
        u = urllib.parse.urlparse(full)

        if u.netloc and ("online.sktorrent.eu" not in u.netloc):
            continue

        path = (u.path or "").strip().lower()
        if not path or path == "/":
            continue

        bad_prefixes = (
            "/search",
            "/albums",
            "/blog",
            "/games",
            "/categories",
            "/community",
            "/signup",
            "/login",
            "/lost",
            "/confirm",
            "/upload",
            "/invite",
            "/notices",
            "/feedback",
            "/static",
        )
        if path.startswith(bad_prefixes):
            continue

        name = ""
        try:
            name = (spans[0].get_text() or "").strip()
        except Exception:
            name = ""

        name = re.sub(r"\s+", " ", name).strip()
        if not name:
            continue
        if not _sktonline_result_matches_query(name, wanted_query):
            continue

        key = full.lower()
        if key in seen:
            continue
        seen.add(key)

        size_bytes = _source_size_from_text(post.get_text(" ", strip=True))

        out.append({
            "title": name,
            "url": full,
            "size_bytes": size_bytes,
        })

    return out

def _sktonline_find_next_page_url(current_html: str, current_url: str):
    m = re.search(r'<a[^>]+rel=["\']next["\'][^>]+href=["\']([^"\']+)["\']', current_html or "", re.IGNORECASE)
    if m:
        return _sktonline_abs(m.group(1), current_url)

    pag = re.search(r'(<ul[^>]*class="[^"]*pagination[^"]*"[^>]*>[\s\S]*?</ul>)', current_html or "", re.IGNORECASE)
    if pag:
        ul = pag.group(1)
        mact = re.search(r'<li[^>]*class="[^"]*\bactive\b[^"]*"[^>]*>[\s\S]*?</li>\s*<li[^>]*>[\s\S]*?<a[^>]+href=["\']([^"\']+)["\']', ul, re.IGNORECASE)
        if mact:
            return _sktonline_abs(mact.group(1), current_url)

        mnext = re.search(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>\s*(?:»|&raquo;|next|další|dalsi|>)\s*</a>', ul, re.IGNORECASE)
        if mnext:
            return _sktonline_abs(mnext.group(1), current_url)

    return None

def sktonline_search_all_pages(query: str, max_pages: int = 50):
    global _SKTONLINE_WARMED
    q = " ".join((query or "").split()).strip()
    if not q:
        return []

    q_enc = urllib.parse.quote_plus(q)
    safe_q = _cache_safe_key_part(q)
    has_episode_code = _query_has_episode_code(q)
    page_limit = min(int(max_pages or 50), 2 if has_episode_code else 4)

    search_templates = [
        _SKTONLINE_BASE.rstrip("/") + "/search/videos?type=public&t=a&o=mr&search_query=" + q_enc + "&page={page}",
        _SKTONLINE_BASE.rstrip("/") + "/search/videos?search_query=" + q_enc + "&page={page}",
    ]

    out = []
    seen = set()

    if not _SKTONLINE_WARMED:
        try:
            _skt_http_get(_SKTONLINE_BASE.rstrip("/") + "/", referer=_SKTONLINE_BASE.rstrip("/") + "/")
        except Exception:
            pass

        try:
            _skt_http_get(_SKTONLINE_BASE.rstrip("/") + "/videos", referer=_SKTONLINE_BASE.rstrip("/") + "/")
        except Exception:
            pass
        _SKTONLINE_WARMED = True

    for tmpl_idx, tmpl in enumerate(search_templates, start=1):
        for page_no in range(1, page_limit + 1):
            cur_url = tmpl.format(page=page_no)

            try:
                cur_html = _skt_http_get(cur_url, referer=_SKTONLINE_BASE.rstrip("/") + "/videos")
            except Exception:
                break

            results = _sktonline_extract_results(cur_html, cur_url, wanted_query=q)
            if page_no == 1 and not results:
                try:
                    _dump_text_to_temp(
                        f"skt_search_{safe_q}_tmpl{tmpl_idx}_page{page_no}.html",
                        cur_html
                    )
                    _dump_text_to_temp(
                        f"skt_search_{safe_q}_tmpl{tmpl_idx}_page{page_no}.txt",
                        f"query={q}\nurl={cur_url}\n"
                    )
                except Exception:
                    pass

            new_added = 0
            for r in results:
                u = (r.get("url") or "").strip()
                if not u or u in seen:
                    continue
                seen.add(u)
                out.append(r)
                new_added += 1

            if new_added == 0:
                break

            if has_episode_code and out:
                break

        if has_episode_code and out:
            break

    return out

def build_url(query: dict) -> str:
    _refresh_runtime()
    return BASE_PATH + "?" + urllib.parse.urlencode(query)

def _cache_safe_key_part(s: str) -> str:
    s = _norm_text(s or "")
    s = s.replace(" ", "_")
    s = re.sub(r"[^a-z0-9_]+", "", s)
    return s[:80] or "x"

def _format_episode_code(season: str, episode: str) -> str:
    try:
        season_i = int(str(season or "").strip())
        episode_i = int(str(episode or "").strip())
        return f"S{season_i:02d}E{episode_i:02d}"
    except Exception:
        season = str(season or "").strip()
        episode = str(episode or "").strip()
        if season and episode:
            return f"S{season}E{episode}"
    return ""

def _build_playback_display_title(meta: dict, fallback_title: str, mode: str = "",
                                  season: str = "", episode: str = "", episode_title: str = "") -> str:
    media_title = str((meta or {}).get("title") or "").strip()
    original_title = str((meta or {}).get("original_title") or "").strip()
    media_title = media_title or original_title or str(fallback_title or "").strip() or i18n.T(30728, "Source")

    if str(mode or "").strip().lower() == "serial_episode":
        code = _format_episode_code(season, episode)
        parts = [media_title]
        if code:
            parts.append(code)
        episode_title = str(episode_title or "").strip()
        if episode_title:
            parts.append(episode_title)
        return " - ".join(parts)

    return media_title

def _with_playback_display_title(play_target: str, display_title: str,
                                 subtitle_title: str = "", subtitle_query: str = "",
                                 year: str = "") -> str:
    if not play_target or not display_title:
        return play_target
    if not str(play_target).startswith("plugin://"):
        return play_target

    try:
        parsed = urllib.parse.urlsplit(play_target)
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        action = (query.get("action") or [""])[0]
        if action not in ("film.play", "play"):
            return play_target

        query["name"] = [display_title]
        query["title"] = [display_title]
        query["play_title"] = [display_title]
        if subtitle_title:
            query["subtitle_title"] = [subtitle_title]
        if subtitle_query:
            query["subtitle_query"] = [subtitle_query]
        if year:
            query["year"] = [year]
        new_query = urllib.parse.urlencode(query, doseq=True)
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, new_query, parsed.fragment))
    except Exception:
        return play_target

def _subtitle_base_from_source_name(source_title: str) -> str:
    value = str(source_title or "").strip()
    if not value:
        return ""

    value = re.sub(r"\.(mkv|mp4|avi|mov|wmv|m4v|ts)$", "", value, flags=re.I)
    value = re.sub(r"[\[\(].*?[\]\)]", " ", value)
    value = re.sub(r"[._]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    if not value:
        return ""

    marker = re.search(
        r"\b(?:19\d{2}|20\d{2}|2160p|1080p|720p|480p|web|webrip|web-dl|bluray|brrip|hdrip|dvdrip|"
        r"uhd|hdr|dv|x264|x265|h264|h265|hevc|aac|ac3|eac3|ddp|dts)\b",
        value,
        flags=re.I,
    )
    if marker:
        value = value[:marker.start()].strip(" -._")

    return re.sub(r"\s+", " ", value).strip()

def _subtitle_search_query(source_title: str, media_title: str, original_title: str) -> str:
    source_title = str(source_title or "").strip()
    media_title = str(media_title or "").strip()
    original_title = str(original_title or "").strip()
    source_base = _subtitle_base_from_source_name(source_title)

    if original_title and original_title.lower() != media_title.lower():
        return original_title
    if source_base and source_base.lower() != media_title.lower():
        return source_base
    return original_title or media_title or source_base or source_title

def _source_cache_key(provider: str, title: str, original_title: str, year: str,
                      mode: str = "", season: str = "", episode: str = "") -> str:
    return "||".join([
        _SOURCE_SEARCH_FILTER_VERSION,
        (provider or "").strip().lower(),
        _norm_text(title or ""),
        _norm_text(original_title or ""),
        (year or "").strip(),
        (mode or "").strip().lower(),
        str(season or "").strip(),
        str(episode or "").strip(),
    ])

def _source_cache_filename(provider: str, title: str, original_title: str, year: str,
                           mode: str = "", season: str = "", episode: str = "") -> str:
    return (
        "ikarus_sources_"
        + _cache_safe_key_part(_SOURCE_SEARCH_FILTER_VERSION)
        + "_"
        + _cache_safe_key_part(provider)
        + "_"
        + _cache_safe_key_part(title)
        + "_"
        + _cache_safe_key_part(original_title)
        + "_"
        + _cache_safe_key_part(year)
        + "_"
        + _cache_safe_key_part(mode)
        + "_"
        + _cache_safe_key_part(season)
        + "_"
        + _cache_safe_key_part(episode)
        + ".json"
    )
def _source_cache_path(provider: str, title: str, original_title: str, year: str,
                       mode: str = "", season: str = "", episode: str = "") -> str:
    fname = _source_cache_filename(provider, title, original_title, year, mode, season, episode)
    return xbmcvfs.translatePath("special://temp/" + fname)

def _source_cache_get(provider: str, title: str, original_title: str, year: str,
                      mode: str = "", season: str = "", episode: str = ""):
    import time

    key = _source_cache_key(provider, title, original_title, year, mode, season, episode)
    now = time.time()

    mem = _SOURCE_SEARCH_CACHE.get(key)
    if mem:
        ts = float(mem.get("ts") or 0)
        if (now - ts) <= _SOURCE_SEARCH_CACHE_TTL:
            rows = mem.get("rows") or []
            if rows:
                return rows

    path = _source_cache_path(provider, title, original_title, year, mode, season, episode)
    if not xbmcvfs.exists(path):
        return None

    try:
        f = xbmcvfs.File(path, "r")
        raw = f.read()
        f.close()

        data = json.loads(raw or "{}")
        ts = float(data.get("ts") or 0)
        rows = data.get("rows") or []

        if (now - ts) > _SOURCE_SEARCH_CACHE_TTL:
            return None
        if not rows:
            return None

        _SOURCE_SEARCH_CACHE[key] = {
            "ts": ts,
            "rows": rows,
        }
        return rows
    except Exception as e:
        xbmc.log(f"[IKARUS][SRC CACHE] READ FAILED: {repr(e)}", xbmc.LOGERROR)
        return None
    
def _source_cache_set(provider: str, title: str, original_title: str, year: str, rows,
                      mode: str = "", season: str = "", episode: str = ""):
    import time

    key = _source_cache_key(provider, title, original_title, year, mode, season, episode)
    now = time.time()
    rows = list(rows or [])
    if not rows:
        _SOURCE_SEARCH_CACHE.pop(key, None)
        return

    _SOURCE_SEARCH_CACHE[key] = {
        "ts": now,
        "rows": rows,
    }

    path = _source_cache_path(provider, title, original_title, year, mode, season, episode)

    try:
        payload = {
            "ts": now,
            "provider": provider,
            "title": title,
            "original_title": original_title,
            "year": year,
            "mode": mode,
            "season": season,
            "episode": episode,
            "rows": rows,
        }
        f = xbmcvfs.File(path, "w")
        f.write(json.dumps(payload, ensure_ascii=False))
        f.close()
    except Exception as e:
        xbmc.log(f"[IKARUS][SRC CACHE] WRITE FAILED: {repr(e)}", xbmc.LOGERROR)

def _source_cache_clear(provider: str, title: str, original_title: str, year: str,
                        mode: str = "", season: str = "", episode: str = ""):
    key = _source_cache_key(provider, title, original_title, year, mode, season, episode)
    _SOURCE_SEARCH_CACHE.pop(key, None)

    path = _source_cache_path(provider, title, original_title, year, mode, season, episode)
    try:
        if xbmcvfs.exists(path):
            xbmcvfs.delete(path)
    except Exception:
        pass

def _refresh_token_already_handled(token: str) -> bool:
    import time

    token = (token or "").strip()
    if not token:
        return False

    now = time.time()

    # úklid starých tokenů
    stale = []
    for k, ts in _HANDLED_REFRESH_TOKENS.items():
        try:
            if (now - float(ts)) > _HANDLED_REFRESH_TOKEN_TTL:
                stale.append(k)
        except Exception:
            stale.append(k)

    for k in stale:
        _HANDLED_REFRESH_TOKENS.pop(k, None)

    ts = _HANDLED_REFRESH_TOKENS.get(token)
    if ts is None:
        return False

    try:
        return (now - float(ts)) <= _HANDLED_REFRESH_TOKEN_TTL
    except Exception:
        return False

def _mark_refresh_token_handled(token: str):
    import time
    token = (token or "").strip()
    if not token:
        return
    _HANDLED_REFRESH_TOKENS[token] = time.time()

def _get_param(name: str, default: str = "") -> str:
    if len(sys.argv) < 3 or not sys.argv[2]:
        return default
    params = urllib.parse.parse_qs(sys.argv[2][1:])
    return params.get(name, [default])[0]


def _end(content="files"):
    _refresh_runtime()
    try:
        xbmcplugin.setContent(addon_handle, content)
    except Exception:
        pass
    xbmcplugin.endOfDirectory(addon_handle)

def _add_result_dir(label: str, query: dict, plot: str = "", icon: str = "DefaultVideo.png"):
    li = xbmcgui.ListItem(label=label)
    li.setArt({
        "icon": icon,
        "thumb": icon,
        "poster": icon
    })
    if plot:
        li.setInfo("video", {"title": label, "plot": plot})
    li.setProperty("IsPlayable", "false")
    xbmcplugin.addDirectoryItem(
        handle=addon_handle,
        url=build_url(query),
        listitem=li,
        isFolder=True
    )

def _add_dir(label: str, query: dict, icon: str = "DefaultFolder.png"):
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
        isFolder=True
    )

def _add_command_item(label: str, query: dict, icon: str = "DefaultAddonProgram.png"):
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

def _add_file(label: str, plot: str = "", icon: str = "DefaultVideo.png"):
    li = xbmcgui.ListItem(label=label)
    li.setArt({
        "icon": icon,
        "thumb": icon,
        "poster": icon
    })
    if plot:
        li.setInfo("video", {"title": label, "plot": plot})
    li.setProperty("IsPlayable", "false")
    xbmcplugin.addDirectoryItem(
        handle=addon_handle,
        url=build_url({"action": "tmdb.noop"}),
        listitem=li,
        isFolder=False
    )

def _add_info_file(label: str, plot: str = "", color: str = "skyblue", icon: str = "DefaultAddonProgram.png"):
    label = str(label or "")
    if color:
        label = f"[COLOR {color}][B]{label}[/B][/COLOR]"
    _add_file(label=label, plot=plot, icon=icon)

def _refresh_current_container():
    try:
        xbmc.executebuiltin("Container.Refresh")
    except Exception:
        pass

def _finish_command_action():
    _refresh_runtime()
    try:
        xbmcplugin.endOfDirectory(
            addon_handle,
            succeeded=True,
            updateListing=False,
            cacheToDisc=False
        )
    except Exception:
        pass

def _placeholder_dir(title: str, extra: str = ""):
    xbmcplugin.setPluginCategory(addon_handle, title)
    xbmcplugin.setContent(addon_handle, "files")

    text = i18n.T(31912, "This section is under construction.")
    if extra:
        text += " " + extra

    _add_file(text, icon="DefaultAddonProgram.png")
    _end("files")

def _norm_text(s: str) -> str:
    s = (s or "").lower().strip()
    s = "".join(
        ch for ch in unicodedata.normalize("NFKD", s)
        if not unicodedata.combining(ch)
    )

    repl = {
        "á": "a", "ä": "a", "č": "c", "ď": "d", "é": "e", "ě": "e",
        "í": "i", "ľ": "l", "ĺ": "l", "ň": "n", "ó": "o", "ô": "o",
        "ř": "r", "ŕ": "r", "š": "s", "ť": "t", "ú": "u", "ů": "u",
        "ý": "y", "ž": "z",
    }
    for a, b in repl.items():
        s = s.replace(a, b)

    s = s.replace("&", " and ")
    s = s.replace("+", " ")
    s = s.replace("/", " ")
    s = s.replace("-", " ")
    s = s.replace("_", " ")

    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def _search_safe_text(s: str) -> str:
    s = _norm_text(s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def _is_generic_episode_title(s: str) -> bool:
    s_norm = _norm_text(s or "")
    if not s_norm:
        return False

    # sjednocení tvarů
    toks = [t for t in s_norm.split() if t]
    if not toks:
        return False

    generic_words = {
        "epizoda", "epizode", "epizody",
        "epizoda.", "epizode.", "epizody.",
        "epizoda:", "epizode:", "epizody:",
        "episode", "episodes",
        "dil", "dilu", "diel", "dielu",
        "cast", "část", "part", "sekce",
        # "Pilot" is shared by many S01E01 episodes, so it is not specific
        # enough to search or accept without the series title.
        "pilot", "pilotni"
    }

    # povolíme i tvary typu:
    # "1 epizoda", "epizoda 1", "1. epizoda", "díl 1", "epizóda 1"
    cleaned = []
    for t in toks:
        tt = t.strip(".:-_")
        if tt:
            cleaned.append(tt)

    if not cleaned:
        return False

    # když jsou tam jen čísla + obecná slova pro epizodu, bereme to jako generický název
    non_generic = []
    has_generic_word = False

    for t in cleaned:
        if t in generic_words:
            has_generic_word = True
            continue
        if re.fullmatch(r"\d+", t):
            continue
        non_generic.append(t)

    if has_generic_word and not non_generic:
        return True

    # ještě tvar čistě "1", "01" apod. samotný nebereme jako smysluplný název epizody
    if len(cleaned) == 1 and re.fullmatch(r"\d+", cleaned[0]):
        return True

    return False

def _episode_title_allowed_extra_tokens():
    return {
        "cz", "sk", "en", "dab", "czdab", "skdab", "dual", "multi",
        "tit", "titulky", "sub", "subs", "czsub", "sksub", "engsub", "forced",
        "hd", "fhd", "uhd", "sd", "hdr", "dv", "hevc", "x264", "x265", "h264", "h265",
        "aac", "ac3", "dd", "ddp", "ddp5", "atmos", "dts",
        "webrip", "webdl", "web", "dl", "bluray", "bdrip", "dvdrip", "hdtv", "tvrip", "tv", "rip", "dvb",
        "mkv", "mp4", "avi",
        "proper", "repack", "extended", "uncut",
        "nf", "netflix", "amzn", "amazon", "dsnp", "disney", "hbo", "max",
        "s01", "s02", "s03", "s04", "s05", "s06", "s07", "s08", "s09", "s10",
        "e01", "e02", "e03", "e04", "e05", "e06", "e07", "e08", "e09", "e10",
        "epizoda", "episode", "dil", "diel"
    }

def _is_clean_episode_title_match(candidate_title: str, episode_title: str) -> bool:
    cand_tokens = _title_tokens(candidate_title)
    ep_tokens = _title_tokens(episode_title)

    if not cand_tokens or not ep_tokens:
        return False

    # najdi souvislý blok tokenů epizody v kandidátu
    hit_pos = -1
    ep_len = len(ep_tokens)

    for i in range(0, len(cand_tokens) - ep_len + 1):
        if cand_tokens[i:i + ep_len] == ep_tokens:
            hit_pos = i
            break

    if hit_pos < 0:
        return False

    before = cand_tokens[:hit_pos]
    after = cand_tokens[hit_pos + ep_len:]
    extras = before + after

    allowed = _episode_title_allowed_extra_tokens()

    # povol:
    # - žádné extra tokeny
    # - jen pár technických tokenů
    # - čísla / rozlišení / SxxEyy / 1x02 apod.
    if not extras:
        return True

    if len(extras) > 5:
        return False

    for t in extras:
        if t in allowed:
            continue
        if re.fullmatch(r"\d{1,4}p?", t):
            continue
        if re.fullmatch(r"s\d{1,2}e\d{1,2}", t):
            continue
        if re.fullmatch(r"\d{1,2}[x×]\d{1,2}", t):
            continue
        if re.fullmatch(r"e\d{1,2}", t):
            continue
        if re.fullmatch(r"s\d{1,2}", t):
            continue
        return False

    return True

def _episode_title_prefix_match_info(candidate_title: str, episode_title: str):
    cand_tokens = _title_tokens(candidate_title)
    ep_tokens = _title_core_tokens(episode_title)

    if not cand_tokens or len(ep_tokens) < 2:
        return False, 0, []

    best = 0
    best_tokens = []

    for i in range(len(cand_tokens)):
        match = 0
        while (
            i + match < len(cand_tokens)
            and match < len(ep_tokens)
            and cand_tokens[i + match] == ep_tokens[match]
        ):
            match += 1
        if match > best:
            best = match
            best_tokens = ep_tokens[:match]

    return best >= 2, best, best_tokens

def _episode_title_overlap_match_info(candidate_title: str, episode_title: str):
    cand_tokens = set(_title_core_tokens(candidate_title))
    ep_tokens = _title_core_tokens(episode_title)

    if not cand_tokens or len(ep_tokens) < 2:
        return False, 0, []

    overlap = [t for t in ep_tokens if t in cand_tokens]
    if not overlap:
        return False, 0, []

    first_token_hit = ep_tokens[0] in cand_tokens
    needed = 3 if len(ep_tokens) >= 5 else 2

    return first_token_hit and len(overlap) >= needed, len(overlap), overlap

def _serial_episode_unknown_tokens(candidate_title: str, title: str, original_title: str,
                                   episode_title: str, season_name: str = ""):
    cand_tokens = _title_tokens(candidate_title)
    if not cand_tokens:
        return []

    allowed = set()

    allowed.update(_title_tokens(title))
    allowed.update(_title_tokens(original_title))
    allowed.update(_title_tokens(episode_title))
    allowed.update(_title_tokens(season_name))

    allowed.update(_episode_title_allowed_extra_tokens())

    out = []

    for t in cand_tokens:
        if t in allowed:
            continue
        if re.fullmatch(r"(19\d{2}|20\d{2}|21\d{2})", t):
            continue
        if re.fullmatch(r"\d{1,4}p?", t):
            continue
        if re.fullmatch(r"s\d{1,2}e\d{1,2}", t):
            continue
        if re.fullmatch(r"\d{1,2}[x×]\d{1,2}", t):
            continue
        if re.fullmatch(r"e\d{1,2}", t):
            continue
        if re.fullmatch(r"s\d{1,2}", t):
            continue
        if re.fullmatch(r"\d+", t):
            continue
        out.append(t)

    return out

def _title_connector_tokens():
    return {
        "a", "an", "and", "the", "of", "to", "in", "on", "for", "with",
        "o", "i", "v", "ve", "na", "do", "z", "ze", "se", "s",
    }

def _title_core_tokens(value: str):
    return [t for t in _title_tokens(value) if t not in _title_connector_tokens()]

def _title_dominant_core_tokens(value: str):
    tokens = _title_core_tokens(value)
    if not tokens:
        return []

    long_tokens = [t for t in tokens if len(t) >= 5]
    short_tokens = [t for t in tokens if len(t) <= 3]

    if len(tokens) == 2 and long_tokens and short_tokens:
        return long_tokens

    return []

def _title_search_variants(value: str):
    out = []
    seen = set()

    def add(text: str):
        text = " ".join(str(text or "").split()).strip()
        if not text:
            return
        key = text.lower()
        if key in seen:
            return
        seen.add(key)
        out.append(text)

    add(value)
    core = _title_core_tokens(value)
    if core:
        add(" ".join(core))
    for token in _title_dominant_core_tokens(value):
        add(token)
    return out

def _short_title_search_variants(value: str):
    tokens = _title_core_tokens(value)
    out = []
    seen = set()

    def add(parts):
        text = " ".join([p for p in (parts or []) if p]).strip()
        if not text:
            return
        key = text.lower()
        if key in seen:
            return
        seen.add(key)
        out.append(text)

    if tokens:
        add(tokens[:1])
    if len(tokens) >= 2:
        add(tokens[:2])
    if len(tokens) >= 3:
        add(tokens[:3])

    return out

def _serial_specific_title_tokens(title: str, original_title: str):
    local = _title_core_tokens(title)
    orig = _title_core_tokens(original_title)

    local_set = set(local)
    orig_set = set(orig)
    all_tokens = local_set | orig_set
    if not all_tokens:
        return set()

    shared = local_set & orig_set if local_set and orig_set and local_set != orig_set else set()
    specific = all_tokens - shared

    if specific:
        return specific

    tokens = local or orig
    if len(tokens) >= 4:
        return set(tokens[3:])

    return set()

def _serial_episode_identity_issue(candidate_title: str, title: str, original_title: str,
                                   episode_title: str, season_name: str = ""):
    unknown = _serial_episode_unknown_tokens(
        candidate_title=candidate_title,
        title=title,
        original_title=original_title,
        episode_title=episode_title,
        season_name=season_name
    )
    if not unknown:
        return False, []

    cand_norm = _norm_text(candidate_title)
    if (title and _norm_text(title) in cand_norm) or (original_title and _norm_text(original_title) in cand_norm):
        return False, unknown

    specific = _serial_specific_title_tokens(title, original_title)
    if not specific:
        return False, unknown

    cand_tokens = set(_title_tokens(candidate_title))
    specific_hit = len(specific & cand_tokens)
    dominant_hit = len(
        (set(_title_dominant_core_tokens(title)) | set(_title_dominant_core_tokens(original_title)))
        & cand_tokens
    )
    local_core = _title_core_tokens(title)
    orig_core = _title_core_tokens(original_title)

    if local_core and set(local_core).issubset(cand_tokens):
        return False, unknown
    if orig_core and set(orig_core).issubset(cand_tokens):
        return False, unknown

    if max(len(local_core), len(orig_core)) <= 1 and specific_hit >= 1:
        return False, unknown
    if dominant_hit >= 1:
        return False, unknown

    if specific_hit == 0:
        return True, unknown
    if len(specific) >= 2 and specific_hit < 2:
        return True, unknown

    return False, unknown

def _query_variants_for_search(title: str, original_title: str, year: str):
    title = (title or "").strip()
    original_title = (original_title or "").strip()
    year = (year or "").strip()

    out = []
    seen = set()

    def add(q):
        q = " ".join((q or "").split()).strip()
        if not q:
            return
        k = q.lower()
        if k in seen:
            return
        seen.add(k)
        out.append(q)

    # původní varianty
    add(title)
    add(original_title)
    if title and year:
        add(f"{title} {year}")
    if original_title and year and original_title.lower() != title.lower():
        add(f"{original_title} {year}")

    # bez diakritiky / normalizované varianty
    safe_title = _search_safe_text(title)
    safe_orig = _search_safe_text(original_title)

    add(safe_title)
    add(safe_orig)
    if safe_title and year:
        add(f"{safe_title} {year}")
    if safe_orig and year:
        add(f"{safe_orig} {year}")

    # kratší tokenové varianty
    title_tokens = _title_tokens(title)
    orig_tokens = _title_tokens(original_title)

    if len(title_tokens) >= 2:
        add(" ".join(title_tokens[:2]))
        add(" ".join(title_tokens[-2:]))

    if len(orig_tokens) >= 2:
        add(" ".join(orig_tokens[:2]))
        add(" ".join(orig_tokens[-2:]))

    # bez krátkých spojek/předložek – pro weby, které hledají lépe přes klíčová slova
    stop_words = {"a", "i", "v", "ve", "na", "do", "od", "z", "ze", "u", "s", "nad", "pod", "pro"}

    short_title_tokens = [t for t in title_tokens if t not in stop_words]
    short_orig_tokens = [t for t in orig_tokens if t not in stop_words]

    if len(short_title_tokens) >= 2:
        add(" ".join(short_title_tokens))
        add(" ".join(short_title_tokens[:3]))
        add(" ".join(short_title_tokens[-3:]))

    if len(short_orig_tokens) >= 2:
        add(" ".join(short_orig_tokens))
        add(" ".join(short_orig_tokens[:3]))
        add(" ".join(short_orig_tokens[-3:]))

    # kratší + rok
    if len(title_tokens) >= 2 and year:
        add(" ".join(title_tokens[:2]) + f" {year}")
        add(" ".join(title_tokens[-2:]) + f" {year}")

    if len(orig_tokens) >= 2 and year:
        add(" ".join(orig_tokens[:2]) + f" {year}")
        add(" ".join(orig_tokens[-2:]) + f" {year}")

    return out


def _season_episode_code(season: str, episode: str) -> str:
    try:
        s = int(season)
        e = int(episode)
        return f"S{s:02d}E{e:02d}"
    except Exception:
        return ""

def _episode_query_number_variants(season: str, episode: str):
    try:
        s = int(str(season or "").strip())
        e = int(str(episode or "").strip())
    except Exception:
        return []
    if s <= 0 or e <= 0:
        return []
    return [
        f"S{s:02d}E{e:02d}",
        f"{s:02d}x{e:02d}",
        f"{s}x{e:02d}",
        f"{s}.{e:02d}",
    ]

def _query_has_episode_code(query: str) -> bool:
    return bool(re.search(r"\b(?:s\d{1,2}e\d{1,2}|\d{1,2}x\d{1,2}|\d{1,2}[.]\d{1,2})\b", query or "", re.IGNORECASE))

def _episode_number_match_info(text: str, season: str, episode: str):
    try:
        s = int(str(season or "").strip())
        e = int(str(episode or "").strip())
    except Exception:
        return False, False, "", 0
    if s <= 0 or e <= 0:
        return False, False, "", 0

    raw = text or ""
    edge_left = r"(?<![A-Za-z0-9])"
    edge_right = r"(?![A-Za-z0-9])"
    strong_patterns = [
        (edge_left + r"[Ss]\s*0*%d\s*[._\-\s]*[Ee]\s*0*%d" % (s, e) + edge_right, "S%02dE%02d" % (s, e)),
        (edge_left + "0*%d\\s*[xX\\u00d7]\\s*0*%d" % (s, e) + edge_right, "%dx%02d" % (s, e)),
        (edge_left + r"0*%d[.]\s*0*%d" % (s, e) + edge_right, "%d.%02d" % (s, e)),
    ]
    for pattern, label in strong_patterns:
        if re.search(pattern, raw):
            return True, False, label, 2

    explicit_wrong = False
    for m in re.finditer(edge_left + r"[Ss]\s*0*(\d{1,2})\s*[._\-\s]*[Ee]\s*0*(\d{1,3})" + edge_right, raw):
        try:
            if int(m.group(1)) != s or int(m.group(2)) != e:
                explicit_wrong = True
        except Exception:
            pass
    for m in re.finditer(edge_left + "0*(\\d{1,2})\\s*[xX\\u00d7]\\s*0*(\\d{1,3})" + edge_right, raw):
        try:
            if int(m.group(1)) != s or int(m.group(2)) != e:
                explicit_wrong = True
        except Exception:
            pass
    for m in re.finditer(edge_left + r"0*(\d{1,2})[.]\s*0*(\d{1,3})" + edge_right, raw):
        try:
            if int(m.group(1)) != s or int(m.group(2)) != e:
                explicit_wrong = True
        except Exception:
            pass
    if explicit_wrong:
        return False, True, "", 0
    weak_patterns = [
        (r"\b[Ee][Pp]?\s*[.]?\s*0*%d\b" % e, "E%02d" % e),
        (r"\b[Ee]pisode\s*0*%d\b" % e, "Episode %d" % e),
        ("\\b[Dd][iI\\u00ed\\u00cd]l\\s*0*%d\\b" % e, "Dil %d" % e),
        (r"\b[Dd]iel\s*0*%d\b" % e, "Diel %d" % e),
        (r"\b[Pp]art[.]?\s*0*%d\b" % e, "Part %d" % e),
    ]
    if s == 1:
        weak_patterns.extend([
            ("\\b0*%d\\s*[._\\-]?\\s*[Dd][iI\\u00ed\\u00cd]l\\b" % e, "Dil %d" % e),
            (r"\b0*%d\s*[._\-]?\s*[Dd]iel\b" % e, "Diel %d" % e),
        ])
    for pattern, label in weak_patterns:
        if re.search(pattern, raw):
            return True, False, label, 1

    wrong = False
    for m in re.finditer(edge_left + r"[Ss]\s*0*(\d{1,2})\s*[._\-\s]*[Ee]\s*0*(\d{1,3})" + edge_right, raw):
        try:
            if int(m.group(1)) != s or int(m.group(2)) != e:
                wrong = True
        except Exception:
            pass
    for m in re.finditer(edge_left + "0*(\\d{1,2})\\s*[xX\\u00d7]\\s*0*(\\d{1,3})" + edge_right, raw):
        try:
            if int(m.group(1)) != s or int(m.group(2)) != e:
                wrong = True
        except Exception:
            pass
    for m in re.finditer(edge_left + r"0*(\d{1,2})[.]\s*0*(\d{1,3})" + edge_right, raw):
        try:
            if int(m.group(1)) != s or int(m.group(2)) != e:
                wrong = True
        except Exception:
            pass
    for m in re.finditer("\\b(?:[Ee][Pp]?|[Ee]pisode|[Dd][iI\\u00ed\\u00cd]l|[Dd]iel|[Pp]art[.]?)\\s*[.]?\\s*0*(\\d{1,3})\\b", raw):
        try:
            if int(m.group(1)) != e:
                wrong = True
        except Exception:
            pass

    return False, wrong, "", 0


def _extract_episode_codes(text: str):
    s = _norm_text(text or "")
    flat = s.replace(" ", "")

    out = set()

    for m in re.finditer(r"s(\d{1,2})e(\d{1,2})", flat, re.IGNORECASE):
        try:
            out.add(f"S{int(m.group(1)):02d}E{int(m.group(2)):02d}")
        except Exception:
            pass

    for m in re.finditer(r"\b[Ss]\s*0*(\d{1,2})\s*[._\-\s]*[Ee]\s*0*(\d{1,3})\b", text or ""):
        try:
            out.add(f"S{int(m.group(1)):02d}E{int(m.group(2)):02d}")
        except Exception:
            pass

    for m in re.finditer(r"(\d{1,2})x(\d{1,2})", flat, re.IGNORECASE):
        try:
            out.add(f"S{int(m.group(1)):02d}E{int(m.group(2)):02d}")
        except Exception:
            pass

    for m in re.finditer(r"\b0*(\d{1,2})\s*[xX×]\s*0*(\d{1,3})\b", text or ""):
        try:
            out.add(f"S{int(m.group(1)):02d}E{int(m.group(2)):02d}")
        except Exception:
            pass

    for m in re.finditer(r"\b0*(\d{1,2})[.]\s*0*(\d{1,3})\b", text or "", re.IGNORECASE):
        try:
            out.add(f"S{int(m.group(1)):02d}E{int(m.group(2)):02d}")
        except Exception:
            pass

    return sorted(out)

def _serial_name_match_strength(candidate_title: str, title: str, original_title: str,
                                season: str = "", episode: str = "") -> int:
    cand_norm = _norm_text(candidate_title)
    cand_tokens = set(_title_tokens(candidate_title))
    local_tokens = set(_title_core_tokens(title))
    orig_tokens = set(_title_core_tokens(original_title))
    local_dominant = set(_title_dominant_core_tokens(title))
    orig_dominant = set(_title_dominant_core_tokens(original_title))

    local_phrase = bool(title and _norm_text(title) in cand_norm)
    orig_phrase = bool(original_title and _norm_text(original_title) in cand_norm)
    if not local_phrase and len(local_tokens) >= 2 and " ".join(_title_core_tokens(title)) in cand_norm:
        local_phrase = True
    if not orig_phrase and len(orig_tokens) >= 2 and " ".join(_title_core_tokens(original_title)) in cand_norm:
        orig_phrase = True

    local_hit = len(local_tokens & cand_tokens) if local_tokens else 0
    orig_hit = len(orig_tokens & cand_tokens) if orig_tokens else 0

    strength = 0

    if local_phrase:
        if len(local_tokens) == 1 and season and episode:
            if _serial_episode_single_word_title_code_match(candidate_title, title, original_title, season, episode):
                strength = max(strength, 3)
        else:
            strength = max(strength, 3)
    elif len(local_tokens) == 1 and season and episode:
        if _serial_episode_single_word_title_code_match(candidate_title, title, original_title, season, episode):
            strength = max(strength, 3)
    elif len(local_tokens) >= 2 and local_hit >= 2:
        strength = max(strength, 2)
    elif season and episode and local_dominant and (local_dominant & cand_tokens):
        if _episode_number_match_info(candidate_title, season, episode)[0]:
            strength = max(strength, 2)
    elif len(local_tokens) == 1 and local_hit >= 1 and not (season and episode):
        strength = max(strength, 1)

    if orig_phrase:
        if len(orig_tokens) == 1 and season and episode:
            if _serial_episode_single_word_title_code_match(candidate_title, title, original_title, season, episode):
                strength = max(strength, 3)
        else:
            strength = max(strength, 3)
    elif len(orig_tokens) == 1 and season and episode:
        if _serial_episode_single_word_title_code_match(candidate_title, title, original_title, season, episode):
            strength = max(strength, 3)
    elif len(orig_tokens) >= 2 and orig_hit >= 2:
        strength = max(strength, 2)
    elif season and episode and orig_dominant and (orig_dominant & cand_tokens):
        if _episode_number_match_info(candidate_title, season, episode)[0]:
            strength = max(strength, 2)
    elif len(orig_tokens) == 1 and orig_hit >= 1 and not (season and episode):
        strength = max(strength, 1)

    return strength


def _filter_sktonline_serial_episode_rows(rows, title: str, original_title: str,
                                          season: str, episode: str, episode_title: str,
                                          season_name: str = ""):
    wanted_code = _season_episode_code(season, episode)
    ep_norm = _norm_text(episode_title or "")

    prepared = []

    for row in (rows or []):
        cand_title = (row.get("title") or "").strip()
        if not cand_title:
            continue

        ok, _why = _is_candidate_relevant(
            candidate_title=cand_title,
            title=title,
            original_title=original_title,
            year="",
            mode="serial_episode",
            season=season,
            episode=episode,
            episode_title=episode_title,
            season_name=season_name
        )
        if not ok:
            continue

        name_strength = _serial_name_match_strength(cand_title, title, original_title, season, episode)
        if name_strength < 2:
            continue

        cand_codes = _extract_episode_codes(cand_title)
        cand_norm = _norm_text(cand_title)

        wrong_episode = False
        exact_episode = False

        if wanted_code:
            if wanted_code in cand_codes:
                exact_episode = True
            elif cand_codes:
                wrong_episode = True

        ep_title_hit = bool(ep_norm and ep_norm in cand_norm)

        if wrong_episode:
            continue

        base_score, reasons = _score_candidate(
            candidate_title=cand_title,
            title=title,
            original_title=original_title,
            year="",
            mode="serial_episode",
            season=season,
            episode=episode,
            episode_title=episode_title,
            season_name=season_name
        )

        score = int(base_score)

        if exact_episode:
            score += 120
            reasons = list(reasons) + [f"přesná shoda epizody {wanted_code}"]

        if ep_title_hit:
            score += 40
            reasons = list(reasons) + ["shoda názvu epizody"]

        score += name_strength * 20
        reasons = list(reasons) + [f"shoda seriálu: {name_strength}"]

        item = dict(row)
        item["_score"] = score
        item["_why"] = reasons
        prepared.append(item)

    prepared.sort(key=lambda x: (x.get("_score", 0), x.get("title", "")), reverse=True)
    return prepared

def _title_tokens(s: str):
    s = _norm_text(s)
    if not s:
        return []
    return [x for x in s.split(" ") if x]

def _is_source_technical_token(token: str) -> bool:
    token = (token or "").strip()
    if not token:
        return True
    if token in _episode_title_allowed_extra_tokens():
        return True
    if re.fullmatch(r"s\d{1,2}e\d{1,2}", token):
        return True
    if re.fullmatch(r"s\d{1,2}", token):
        return True
    if re.fullmatch(r"e\d{1,2}", token):
        return True
    if re.fullmatch(r"\d{1,2}[x×]\d{1,2}", token):
        return True
    if re.fullmatch(r"\d{3,4}p?", token):
        return True
    if re.fullmatch(r"(19\d{2}|20\d{2}|21\d{2})", token):
        return True
    return False

def _serial_episode_single_word_title_required(title: str, original_title: str) -> bool:
    title_sets = []
    for value in (title, original_title):
        tokens = _title_core_tokens(value)
        if tokens:
            title_sets.append(tokens)
    return bool(title_sets) and all(len(tokens) == 1 for tokens in title_sets)

def _serial_episode_single_word_title_code_match(candidate_title: str, title: str, original_title: str,
                                                season: str, episode: str) -> bool:
    wanted_titles = []
    for value in (title, original_title):
        tokens = _title_core_tokens(value)
        if len(tokens) == 1 and tokens[0] not in wanted_titles:
            wanted_titles.append(tokens[0])
    if not wanted_titles:
        return False

    tokens = _title_tokens(candidate_title)
    if not tokens:
        return False

    for idx, token in enumerate(tokens):
        if token not in wanted_titles:
            continue
        has_only_technical_prefix = not any(
            not _is_source_technical_token(prefix) for prefix in tokens[:idx]
        )
        has_alt_title_variant = any(
            other != token and other in tokens for other in wanted_titles
        )

        suffix_parts = []
        for tail_token in tokens[idx + 1:idx + 7]:
            if not _is_source_technical_token(tail_token):
                break
            suffix_parts.append(tail_token)
            suffix_text = " ".join(suffix_parts)
            number_hit, wrong_number, _label, _strength = _episode_number_match_info(suffix_text, season, episode)
            if number_hit and not wrong_number:
                if has_only_technical_prefix:
                    return True
                if len(wanted_titles) >= 2 and has_alt_title_variant:
                    return True

    return False

def _extract_years(text: str):
    years = []
    for m in re.finditer(r"(19\d{2}|20\d{2}|21\d{2})", text or ""):
        years.append(m.group(1))
    return years

def _has_episode_marker(text: str) -> bool:
    s = _norm_text(text)

    if re.search(r"\bs\d{1,2}e\d{1,2}\b", s):
        return True
    if re.search(r"\b\d{1,2}x\d{1,2}\b", s):
        return True

    toks = set(_title_tokens(text))
    episode_words = {
        "season", "serie", "serial", "epizoda", "episode", "episodes",
        "dil", "diel", "s01", "s02", "s03", "s04", "s05", "s06", "s07", "s08",
    }
    return bool(toks & episode_words)

def _has_music_marker(text: str) -> bool:
    toks = set(_title_tokens(text))
    music_words = {
        "song", "audio", "album", "karaoke", "lyrics", "lyric", "remix",
        "mix", "live", "concert", "videoklip", "klip", "ost", "soundtrack",
        "music", "mp3", "flac",
    }
    return bool(toks & music_words)

def _is_candidate_relevant(candidate_title: str, title: str, original_title: str, year: str,
                           mode: str = "", season: str = "", episode: str = "",
                           episode_title: str = "", season_name: str = ""):
    cand_raw = (candidate_title or "").strip()
    cand = _norm_text(cand_raw)

    local_title = (title or "").strip()
    orig_title = (original_title or "").strip()
    wanted_year = (year or "").strip()

    if not cand:
        return False, "prázdný název"

    # tvrdé bloky – ale už bez dabingu
    noise_words = {
        "trailer", "ukazka", "sample", "cam", "ts", "workprint",
        "bonus", "soundtrack"
    }
    cand_tokens = set(_title_tokens(cand_raw))

    if noise_words & cand_tokens:
        return False, "obsahuje šum"

    if _has_music_marker(cand_raw):
        return False, "vypadá jako hudba"

    if (mode or "").strip() != "serial_episode":
        if _has_episode_marker(cand_raw):
            return False, "vypadá jako epizoda seriálu"

    local_tokens = set(_title_core_tokens(local_title))
    orig_tokens = set(_title_core_tokens(orig_title))

    local_hit = len(local_tokens & cand_tokens) if local_tokens else 0
    orig_hit = len(orig_tokens & cand_tokens) if orig_tokens else 0

    local_phrase = bool(local_title and _norm_text(local_title) in cand)
    orig_phrase = bool(orig_title and _norm_text(orig_title) in cand)
    if not local_phrase and len(local_tokens) >= 2 and " ".join(_title_core_tokens(local_title)) in cand:
        local_phrase = True
    if not orig_phrase and len(orig_tokens) >= 2 and " ".join(_title_core_tokens(orig_title)) in cand:
        orig_phrase = True

    cand_years = _extract_years(cand_raw)

    if wanted_year and cand_years and wanted_year not in cand_years:
        close_year = False
        try:
            wanted_year_i = int(wanted_year)
            close_year = any(abs(int(y) - wanted_year_i) <= 1 for y in cand_years if str(y).isdigit())
        except Exception:
            close_year = False

        if not close_year and (mode or "").strip() != "serial_episode":
            return False, "jin\u00fd rok"

    strong_local = False
    if local_tokens:
        if local_phrase:
            strong_local = True
        elif len(local_tokens) >= 2 and local_hit >= 2:
            strong_local = True
        elif len(local_tokens) == 1 and local_hit >= 1 and wanted_year and wanted_year in cand_years:
            strong_local = True

    strong_orig = False
    if orig_tokens:
        if len(orig_tokens) >= 2:
            if orig_phrase or orig_hit >= 2:
                strong_orig = True
        else:
            if orig_phrase and wanted_year and wanted_year in cand_years:
                strong_orig = True

    if (mode or "").strip() == "serial_episode":
        code = _season_episode_code(season, episode)
        cand_norm = _norm_text(cand_raw)
        ep_title_norm = _norm_text(episode_title or "")
        generic_ep_title = _is_generic_episode_title(episode_title)
        prefix_episode_match, prefix_episode_len, _prefix_episode_tokens = _episode_title_prefix_match_info(
            cand_raw, episode_title
        )
        overlap_episode_match, overlap_episode_len, _overlap_episode_tokens = _episode_title_overlap_match_info(
            cand_raw, episode_title
        )
        number_hit, wrong_number, number_label, number_strength = _episode_number_match_info(
            cand_raw, season, episode
        )
        number_with_ep_title = bool(number_hit and ep_title_norm and not generic_ep_title and ep_title_norm in cand_norm)

        if wrong_number and not number_hit:
            return False, "jiná epizoda"

        strong_episode = False
        exact_episode_code = False
        clean_episode_title_match = False

        if number_hit:
            strong_episode = True
            exact_episode_code = number_strength >= 2
        elif code and code.lower() in cand_norm.replace(" ", ""):
            strong_episode = True
            exact_episode_code = True
        elif ep_title_norm and not generic_ep_title and ep_title_norm in cand_norm:
            if _is_clean_episode_title_match(cand_raw, episode_title):
                strong_episode = True
                clean_episode_title_match = True
            elif (prefix_episode_match or overlap_episode_match) and (local_phrase or orig_phrase):
                strong_episode = True
            else:
                return False, "n\u00e1zev epizody je v ciz\u00edm kontextu"
        elif (prefix_episode_match or overlap_episode_match) and (local_phrase or orig_phrase):
            strong_episode = True

        if ep_title_norm and generic_ep_title and ep_title_norm in cand_norm and (local_phrase or orig_phrase):
            strong_episode = True

        if not strong_episode:
            return False, "slabá shoda epizody"

        if _serial_episode_single_word_title_required(title, original_title):
            if not _serial_episode_single_word_title_code_match(cand_raw, title, original_title, season, episode):
                if not (exact_episode_code and (number_with_ep_title or clean_episode_title_match)):
                    return False, "slab\u00e1 shoda jednoslovn\u00e9ho n\u00e1zvu seri\u00e1lu"

        if clean_episode_title_match:
            return True, ""

        if number_with_ep_title:
            return True, ""

        identity_issue, _identity_unknown = _serial_episode_identity_issue(
            candidate_title=cand_raw,
            title=title,
            original_title=original_title,
            episode_title=episode_title,
            season_name=season_name
        )
        if identity_issue:
            return False, "cizí název seriálu"

        if local_phrase or orig_phrase or _serial_name_match_strength(cand_raw, title, original_title, season, episode) >= 2:
            return True, ""

    if not strong_local and not strong_orig:
        return False, "slabá shoda názvu"

    return True, ""

def _score_candidate(candidate_title: str, title: str, original_title: str, year: str,
                     mode: str = "", season: str = "", episode: str = "", episode_title: str = "",
                     season_name: str = ""):
    cand_raw = (candidate_title or "").strip()
    cand = _norm_text(cand_raw)

    local_title = _norm_text(title)
    orig_title = _norm_text(original_title)
    wanted_year = (year or "").strip()

    score = 0
    reasons = []

    if not cand:
        return -999, ["prázdný název"]

    if local_title and cand == local_title:
        score += 100
        reasons.append("přesná shoda lokálního názvu")

    if orig_title and cand == orig_title:
        score += 100
        reasons.append("přesná shoda originálního názvu")

    if local_title and local_title in cand:
        score += 50
        reasons.append("obsahuje lokální název")

    orig_token_count = len(_title_tokens(original_title))
    if orig_title and orig_title in cand:
        if orig_token_count >= 2:
            score += 50
            reasons.append("obsahuje originální název")
        else:
            score += 15
            reasons.append("obsahuje jednoslovný originální název")

    cand_tokens = set(_title_tokens(cand_raw))
    local_tokens = set(_title_tokens(title))
    orig_tokens = set(_title_tokens(original_title))
    season_tokens = set(_title_tokens(season_name))

    if local_tokens:
        hit = len(local_tokens & cand_tokens)
        score += hit * 8
        if hit:
            reasons.append(f"shoda tokenů lokálního názvu: {hit}")

    if orig_tokens:
        hit = len(orig_tokens & cand_tokens)
        if len(orig_tokens) >= 2:
            score += hit * 8
            if hit:
                reasons.append(f"shoda tokenů originálního názvu: {hit}")
        else:
            score += hit * 3
            if hit:
                reasons.append(f"shoda tokenů jednoslovného originálního názvu: {hit}")

    if season_tokens:
        hit = len(season_tokens & cand_tokens)
        score += hit * 6
        if hit:
            reasons.append(f"shoda tokenů názvu série: {hit}")

    cand_years = _extract_years(cand_raw)
    if wanted_year:
        if wanted_year in cand_years:
            score += 40
            reasons.append("souhlas\u00ed rok")
        elif cand_years:
            close_year = False
            try:
                wanted_year_i = int(wanted_year)
                close_year = any(abs(int(y) - wanted_year_i) <= 1 for y in cand_years if str(y).isdigit())
            except Exception:
                close_year = False

            if close_year:
                score += 10
                reasons.append("rok v toleranci +-1")
            elif (mode or "").strip() == "serial_episode":
                reasons.append("jin\u00fd rok u epizody nepenalizuji")
            else:
                score -= 140
                reasons.append("jin\u00fd rok")

    noise_words = {
        "trailer", "ukazka", "sample", "cam", "ts", "workprint",
        "titulky", "bonus", "soundtrack"
    }
    noise_hit = len(noise_words & cand_tokens)
    if noise_hit:
        score -= noise_hit * 80
        reasons.append("obsahuje šum")

    if "czdab" in cand_tokens:
        score += 35
        reasons.append("obsahuje CZ dabing")
    elif "dabing" in cand_tokens or "dab" in cand_tokens:
        score += 20
        reasons.append("obsahuje dabing")

    if "skdab" in cand_tokens:
        score += 10
        reasons.append("obsahuje SK dabing")

    if (mode or "").strip() == "serial_episode":
        code = _season_episode_code(season, episode)
        cand_flat = cand.replace(" ", "")
        ep_norm = _norm_text(episode_title or "")
        generic_ep_title = _is_generic_episode_title(episode_title)
        clean_ep_match = _is_clean_episode_title_match(cand_raw, episode_title) if ep_norm else False
        prefix_ep_match, prefix_ep_len, _prefix_ep_tokens = _episode_title_prefix_match_info(cand_raw, episode_title)
        overlap_ep_match, overlap_ep_len, _overlap_ep_tokens = _episode_title_overlap_match_info(cand_raw, episode_title)
        number_hit, wrong_number, number_label, number_strength = _episode_number_match_info(
            cand_raw, season, episode
        )
        number_with_ep_title = bool(number_hit and ep_norm and not generic_ep_title and ep_norm in cand)

        if wrong_number and not number_hit:
            score -= 180
            reasons.append("jin\u00e1 epizoda")

        if number_hit:
            bonus = 90 if number_strength >= 2 else 60
            score += bonus
            reasons.append(f"obsahuje epizodu {number_label or code}")
        elif code and code.lower() in cand_flat:
            score += 80
            reasons.append(f"obsahuje epizodu {code}")

        if ep_norm and not generic_ep_title and ep_norm in cand:
            if clean_ep_match:
                score += 40
                reasons.append("obsahuje \u010dist\u00fd n\u00e1zev epizody")

                if cand == ep_norm:
                    score += 60
                    reasons.append("p\u0159esn\u00e1 shoda n\u00e1zvu epizody")
                elif cand.startswith(ep_norm):
                    score += 25
                    reasons.append("za\u010d\u00edn\u00e1 n\u00e1zvem epizody")
                elif cand.endswith(ep_norm):
                    score += 25
                    reasons.append("kon\u010d\u00ed n\u00e1zvem epizody")
            elif number_hit:
                score += 65
                reasons.append("obsahuje n\u00e1zev epizody a spr\u00e1vn\u00e9 \u010d\u00edslo")
            else:
                unknown = _serial_episode_unknown_tokens(
                    candidate_title=cand_raw,
                    title=title,
                    original_title=original_title,
                    episode_title=episode_title,
                    season_name=season_name
                )

                if len(unknown) <= 1:
                    score += 45
                    reasons.append("obsahuje n\u00e1zev epizody s mal\u00fdm p\u0159esahem")
                else:
                    score -= 120
                    reasons.append("n\u00e1zev epizody je v ciz\u00edm kontextu")
        elif prefix_ep_match and (local_title and local_title in cand or orig_title and orig_title in cand):
            score += 18 + min(prefix_ep_len, 4) * 8
            reasons.append("obsahuje za\u010d\u00e1tek n\u00e1zvu epizody")
        elif overlap_ep_match and (local_title and local_title in cand or orig_title and orig_title in cand):
            score += 12 + min(overlap_ep_len, 5) * 6
            reasons.append("obsahuje v\u00edce token\u016f n\u00e1zvu epizody")
        elif ep_norm and generic_ep_title and ep_norm in cand and (local_title and local_title in cand or orig_title and orig_title in cand):
            score += 25
            reasons.append("obsahuje obecn\u00fd n\u00e1zev epizody s n\u00e1zvem seri\u00e1lu")

        identity_issue, unknown = _serial_episode_identity_issue(
            candidate_title=cand_raw,
            title=title,
            original_title=original_title,
            episode_title=episode_title,
            season_name=season_name
        )

        if unknown and not number_with_ep_title:
            if identity_issue:
                score -= 220
                reasons.append("cizí název seriálu: " + ", ".join(unknown[:4]))
            elif number_hit and (local_title and local_title in cand or orig_title and orig_title in cand):
                pass
            elif prefix_ep_match and (local_title and local_title in cand or orig_title and orig_title in cand):
                penalty = min(len(unknown), 4) * 8
                score -= penalty
                reasons.append("volne pokracovani nazvu epizody: " + ", ".join(unknown[:4]))
            elif overlap_ep_match and (local_title and local_title in cand or orig_title and orig_title in cand):
                penalty = min(len(unknown), 4) * 10
                score -= penalty
                reasons.append("volne souvisi s nazvem epizody: " + ", ".join(unknown[:4]))
            else:
                penalty = min(len(unknown), 4) * 35
                score -= penalty
                reasons.append("cizí tokeny: " + ", ".join(unknown[:4]))

    return score, reasons

def _filter_candidates(rows, title: str, original_title: str, year: str, min_score: int = 35,
                       mode: str = "", season: str = "", episode: str = "", episode_title: str = "",
                       season_name: str = ""):
    scored = []

    for row in (rows or []):
        cand_title = (row.get("title") or "").strip()

        ok, why_not = _is_candidate_relevant(
            candidate_title=cand_title,
            title=title,
            original_title=original_title,
            year=year,
            mode=mode,
            season=season,
            episode=episode,
            episode_title=episode_title,
            season_name=season_name
        )
        if not ok:
            continue

        score, reasons = _score_candidate(
            candidate_title=cand_title,
            title=title,
            original_title=original_title,
            year=year,
            mode=mode,
            season=season,
            episode=episode,
            episode_title=episode_title,
            season_name=season_name
        )

        if score < min_score:
            continue

        item = dict(row)
        item["_score"] = score
        item["_why"] = reasons
        scored.append(item)

    scored.sort(key=lambda x: (x.get("_score", 0), x.get("title", "")), reverse=True)
    return scored

def _provider_result(title: str, url: str = "", provider: str = "", year: str = "", plot: str = "", **extra):
    row = {
        "title": (title or "").strip(),
        "url": (url or "").strip(),
        "provider": (provider or "").strip(),
        "year": (year or "").strip(),
        "plot": (plot or "").strip(),
    }

    for k, v in (extra or {}).items():
        row[k] = v

    return row

from resources.lib.source_labels import (
    source_collect_dicts_from_mapping as _source_collect_dicts_from_mapping,
    source_first_from_mapping as _source_first_from_mapping,
    source_fmt_bitrate as _source_fmt_bitrate,
    source_fmt_size as _source_fmt_size,
    source_int_from_any as _source_int_from_any,
    source_metadata as _source_metadata,
    source_quality_from_dimensions as _source_quality_from_dimensions,
    source_size_bytes as _source_size_bytes,
    source_size_from_mapping as _source_size_from_mapping,
    source_size_from_text as _source_size_from_text,
)

def _source_label_suffix(row: dict, score=None) -> str:
    meta = _source_metadata(row)
    tags = []

    for value in (meta.get("size"), meta.get("quality"), meta.get("audio")):
        if value and value not in tags:
            tags.append(value)

    if score is not None:
        tags.append(f"score {score}")

    if not tags:
        return ""
    return "  " + " ".join(f"[{x}]" for x in tags)

def _source_lang_tag_from_text(value: str) -> str:
    text = _norm_text(value or "")
    if not text:
        return ""

    mapping = (
        ("CZ", ("cz", "cs", "cze", "ces", "czech", "cesky")),
        ("SK", ("sk", "slo", "slk", "slovak", "slovensky")),
        ("EN", ("en", "eng", "english")),
    )
    hits = []
    tokens = set(text.replace("+", " ").replace(",", " ").split())
    compact = text.replace(" ", "")

    compact_markers = {
        "CZ": ("czdab", "czdabbing", "czdubbing", "cztit", "czsub", "czsubs"),
        "SK": ("skdab", "skdabbing", "skdubbing", "sktit", "sksub", "sksubs"),
        "EN": ("endab", "endubbing", "entit", "ensub", "ensubs"),
    }

    for code, aliases in mapping:
        if code in hits:
            continue
        if any(alias in tokens for alias in aliases):
            hits.append(code)
            continue
        if any(alias in compact for alias in aliases if len(alias) >= 3):
            hits.append(code)
            continue
        if any(marker in compact for marker in compact_markers.get(code, ())):
            hits.append(code)

    return ",".join(hits)

def _source_label_prefix(row: dict) -> str:
    meta = _source_metadata(row)
    tags = []

    lang = _source_lang_tag_from_text(
        " ".join(
            str(x or "")
            for x in (
                meta.get("audio"),
                meta.get("subtitles"),
                row.get("title") if isinstance(row, dict) else "",
            )
        )
    )
    for value in (lang, meta.get("quality"), meta.get("size")):
        value = str(value or "").strip()
        if value and value not in tags:
            tags.append(value)

    if not tags:
        return ""
    return "".join(f"[{x}]" for x in tags) + " "

def _source_score_suffix(score=None) -> str:
    if score is None:
        return ""
    return f"  [score {score}]"

def _webshare_source_name(row: dict) -> str:
    row = row or {}
    for key in ("ws_info_name", "title", "name", "id"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return "WebShare zdroj"

def _webshare_lang_code(value) -> str:
    text = _norm_text(str(value or ""))
    if not text:
        return ""

    compact = text.replace(" ", "")
    mapping = {
        "cz": "CZ", "cs": "CZ", "cze": "CZ", "ces": "CZ", "czech": "CZ", "cesky": "CZ",
        "sk": "SK", "slo": "SK", "slk": "SK", "slovak": "SK", "slovensky": "SK",
        "en": "EN", "eng": "EN", "english": "EN",
    }
    if compact in mapping:
        return mapping[compact]
    for token in text.split():
        if token in mapping:
            return mapping[token]
    return ""

def _webshare_langs_from_streams(streams) -> list:
    if isinstance(streams, dict):
        streams = [streams]
    out = []
    for stream in (streams or []):
        if not isinstance(stream, dict):
            continue
        for key in ("lang", "language", "language_code", "locale"):
            code = _webshare_lang_code(stream.get(key))
            if code and code not in out:
                out.append(code)
    return out

def _webshare_langs_from_text(value) -> list:
    out = []
    for chunk in re.split(r"[,;/+| ]+", str(value or "")):
        code = _webshare_lang_code(chunk)
        if code and code not in out:
            out.append(code)
    return out

def _webshare_language_tags(row: dict) -> list:
    row = row or {}
    audio = _webshare_langs_from_streams(row.get("audio_streams"))
    if not audio:
        audio = _webshare_langs_from_text(row.get("audio") or row.get("audio_lang"))

    subtitles = (
        _webshare_langs_from_streams(row.get("subtitle_streams"))
        or _webshare_langs_from_text(row.get("subtitles") or row.get("subs") or row.get("subs_langs"))
    )

    tags = []
    if audio:
        tags.append(",".join(audio[:3]))
    if subtitles:
        tags.append(",".join(subtitles[:3]))
    return tags

def _webshare_audio_langs(row: dict) -> list:
    row = row or {}
    audio = _webshare_langs_from_streams(row.get("audio_streams"))
    if not audio:
        audio = _webshare_langs_from_text(row.get("audio") or row.get("audio_lang"))
    return audio

def _webshare_audio_rank(row: dict) -> int:
    langs = _webshare_audio_langs(row)
    if "CZ" in langs:
        return 4
    if "SK" in langs:
        return 3
    if "EN" in langs:
        return 2
    return 1

def _webshare_quality_tag(row: dict) -> str:
    row = row or {}
    direct = str(row.get("quality") or row.get("resolution") or "").strip()
    if direct:
        return direct

    quality = _source_quality_from_dimensions(row.get("width"), row.get("height"))
    if quality:
        return quality

    best_w = 0
    best_h = 0
    streams = row.get("video_streams") or []
    if isinstance(streams, dict):
        streams = [streams]
    for stream in streams:
        if not isinstance(stream, dict):
            continue
        w = _source_int_from_any(stream.get("width"), 0)
        h = _source_int_from_any(stream.get("height"), 0)
        if h > best_h or (h == best_h and w > best_w):
            best_w, best_h = w, h
    return _source_quality_from_dimensions(best_w, best_h)

def _webshare_quality_rank(row: dict) -> int:
    quality = (_webshare_quality_tag(row) or "").strip().lower()
    if not quality:
        return 0
    if quality in ("8k", "4320p"):
        return 8000
    if quality in ("4k", "uhd", "2160p"):
        return 4000
    m = re.search(r"(\d{3,4})p", quality)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            return 0
    if quality == "hd":
        return 700
    if quality == "sd":
        return 400
    return 0

def _source_language_rank(row: dict) -> int:
    row = row or {}
    meta = _source_metadata(row)
    tags = _source_lang_tag_from_text(
        " ".join(
            str(x or "")
            for x in (meta.get("audio"), meta.get("subtitles"), row.get("title"))
        )
    ).split(",")
    if "CZ" in tags:
        return 4
    if "SK" in tags:
        return 3
    if "EN" in tags:
        return 2
    return 1

def _source_quality_rank(row: dict) -> int:
    quality = (_source_metadata(row).get("quality") or "").strip().lower()
    if not quality:
        return 0
    if quality in ("8k", "4320p"):
        return 8000
    if quality in ("4k", "uhd", "2160p"):
        return 4000
    m = re.search(r"(\d{3,4})p", quality)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            return 0
    if quality == "hd":
        return 700
    if quality == "sd":
        return 400
    return 0

def _provider_sort_key(row: dict):
    row = row or {}
    return (
        _source_language_rank(row),
        _source_quality_rank(row),
        _source_size_bytes(row),
        row.get("_score", 0),
        (row.get("title") or "").strip().lower(),
    )

def _sort_provider_rows(rows) -> list:
    return sorted(
        (row for row in (rows or []) if isinstance(row, dict)),
        key=_provider_sort_key,
        reverse=True,
    )

def _webshare_sort_key(row: dict):
    row = row or {}
    return (
        _webshare_audio_rank(row),
        _webshare_quality_rank(row),
        _source_size_bytes(row),
        row.get("_score", 0),
        _webshare_source_name(row).lower(),
    )

def _sort_webshare_rows(rows) -> list:
    return sorted((row for row in (rows or []) if isinstance(row, dict)),
                  key=_webshare_sort_key, reverse=True)

_WEBSHARE_EPISODE_TITLE_ONLY_GROUP = "__episode_title_only__"

def _webshare_episode_title_only_has_foreign_number(source_name: str, season: str, episode: str) -> bool:
    try:
        wanted_season = int(str(season or "").strip())
        wanted_episode = int(str(episode or "").strip())
    except Exception:
        return False
    if wanted_season <= 0 or wanted_episode <= 0:
        return False

    for token in _title_tokens(source_name):
        if not re.fullmatch(r"\d{1,3}", token):
            continue
        try:
            value = int(token)
        except Exception:
            continue
        if value not in (wanted_season, wanted_episode):
            return True
    return False

def _webshare_serial_episode_match_kind(row: dict, media_title: str, original_title: str,
                                        season: str, episode: str, episode_title: str,
                                        season_name: str = "") -> str:
    source_name = _webshare_source_name(row)
    if not source_name:
        return "reject"

    number_hit, wrong_number, _number_label, _number_strength = _episode_number_match_info(
        source_name,
        season,
        episode,
    )
    if wrong_number and not number_hit:
        return "reject"

    ep_norm = _norm_text(episode_title or "")
    generic_ep_title = _is_generic_episode_title(episode_title)
    source_norm = _norm_text(source_name)
    episode_title_hit = bool(ep_norm and not generic_ep_title and ep_norm in source_norm)
    serial_strength = _serial_name_match_strength(source_name, media_title, original_title, season, episode)

    if serial_strength >= 2 and (number_hit or episode_title_hit):
        return "strong"
    if number_hit and episode_title_hit:
        identity_issue, unknown = _serial_episode_identity_issue(
            candidate_title=source_name,
            title=media_title,
            original_title=original_title,
            episode_title=episode_title,
            season_name=season_name,
        )
        if identity_issue or len(unknown) >= 2:
            return "reject"
        return "strong"
    if episode_title_hit and serial_strength < 2:
        identity_issue, unknown = _serial_episode_identity_issue(
            candidate_title=source_name,
            title=media_title,
            original_title=original_title,
            episode_title=episode_title,
            season_name=season_name,
        )
        if identity_issue or len(unknown) >= 2:
            return "reject"
        if not number_hit and _webshare_episode_title_only_has_foreign_number(source_name, season, episode):
            return "reject"
        return "episode_title_only"
    if serial_strength >= 2:
        return "strong"
    return "reject"

def _split_webshare_episode_title_only_rows(rows, media_title: str, original_title: str,
                                           season: str, episode: str, episode_title: str,
                                           season_name: str = ""):
    classified = []
    has_strong = False

    for row in _sort_webshare_rows(rows):
        if not isinstance(row, dict):
            continue
        kind = _webshare_serial_episode_match_kind(
            row,
            media_title=media_title,
            original_title=original_title,
            season=season,
            episode=episode,
            episode_title=episode_title,
            season_name=season_name,
        )
        if kind == "strong":
            has_strong = True
        classified.append((kind, row))

    if not has_strong:
        return [row for kind, row in classified if kind != "reject"], []

    main_rows = []
    episode_title_only_rows = []
    for kind, row in classified:
        if kind == "reject":
            continue
        if kind == "episode_title_only":
            episode_title_only_rows.append(row)
        else:
            main_rows.append(row)

    if not main_rows:
        return [row for kind, row in classified if kind != "reject"], []
    return main_rows, episode_title_only_rows

def _webshare_media_display_name(media_title: str, year: str) -> str:
    title = str(media_title or "").strip()
    if not title:
        return ""

    year_text = str(year or "").strip()
    m = re.search(r"(19\d{2}|20\d{2}|21\d{2})", year_text)
    if m:
        year_text = m.group(1)
    else:
        year_text = ""

    if year_text and f"({year_text})" not in title:
        return f"{title} ({year_text})"
    return title

def _webshare_serial_episode_display_name(media_title: str, season: str, episode: str,
                                          episode_title: str = "") -> str:
    title = str(media_title or "").strip()
    if not title:
        return ""

    parts = [title]
    code = _format_episode_code(season, episode)
    if code:
        parts.append(code)

    episode_title = str(episode_title or "").strip()
    if episode_title:
        parts.append(episode_title)

    return " - ".join(parts)

def _webshare_display_media_name(media_title: str, year: str = "", mode: str = "",
                                 season: str = "", episode: str = "",
                                 episode_title: str = "") -> str:
    if str(mode or "").strip().lower() == "serial_episode":
        return _webshare_serial_episode_display_name(media_title, season, episode, episode_title)
    return _webshare_media_display_name(media_title, year)

def _webshare_label_name(row: dict, media_title: str = "", year: str = "", mode: str = "",
                         season: str = "", episode: str = "", episode_title: str = "") -> str:
    if row and row.get("ws_file_info") and "CZ" in _webshare_audio_langs(row):
        media_name = _webshare_display_media_name(
            media_title,
            year=year,
            mode=mode,
            season=season,
            episode=episode,
            episode_title=episode_title,
        )
        if media_name:
            return media_name
    return _webshare_source_name(row)

def _webshare_display_label(row: dict, media_title: str = "", year: str = "", mode: str = "",
                            season: str = "", episode: str = "", episode_title: str = "") -> str:
    row = row or {}
    name = _webshare_label_name(
        row,
        media_title=media_title,
        year=year,
        mode=mode,
        season=season,
        episode=episode,
        episode_title=episode_title,
    )
    if not row.get("ws_file_info"):
        return name

    tags = []

    for tag in _webshare_language_tags(row):
        if tag and tag not in tags:
            tags.append(tag)

    quality = _webshare_quality_tag(row)
    if quality and quality not in tags:
        tags.append(quality)

    size = _source_fmt_size(_source_size_bytes(row))
    if size and size not in tags:
        tags.append(size)

    if not tags:
        return name
    return "".join(f"[{tag}]" for tag in tags) + " " + name

def _webshare_group_key(row: dict, media_title: str = "", year: str = "", mode: str = "",
                        season: str = "", episode: str = "", episode_title: str = "") -> str:
    return _cache_safe_key_part(_webshare_display_label(
        row,
        media_title=media_title,
        year=year,
        mode=mode,
        season=season,
        episode=episode,
        episode_title=episode_title,
    ))

def _group_webshare_rows(rows, media_title: str = "", year: str = "", mode: str = "",
                         season: str = "", episode: str = "", episode_title: str = "") -> list:
    groups = []
    index = {}

    for row in _sort_webshare_rows(rows):
        if not isinstance(row, dict):
            continue
        key = _webshare_group_key(
            row,
            media_title=media_title,
            year=year,
            mode=mode,
            season=season,
            episode=episode,
            episode_title=episode_title,
        )
        if key not in index:
            group = {
                "key": key,
                "label": _webshare_display_label(
                    row,
                    media_title=media_title,
                    year=year,
                    mode=mode,
                    season=season,
                    episode=episode,
                    episode_title=episode_title,
                ),
                "rows": [],
            }
            index[key] = group
            groups.append(group)
        index[key]["rows"].append(row)

    return groups

def _source_stream_line(stream: dict, kind: str = "video") -> str:
    return source_labels.source_stream_line(stream, kind)

def _source_duration_text(value) -> str:
    return source_labels.source_duration_text(value)

def _source_detail_line(label_id: int, value) -> str:
    return i18n.T(31876).format(label=i18n.T(label_id), value=value)

def _source_detail_lines(row: dict, score=None):
    row = row or {}
    meta = _source_metadata(row)
    lines = []

    provider_name = (row.get("provider") or "").strip()
    if provider_name:
        lines.append(_source_detail_line(31959, provider_name))

    if meta.get("size"):
        lines.append(_source_detail_line(30612, meta["size"]))
    file_type = str(row.get("file_type") or row.get("type") or "").strip()
    if file_type:
        lines.append(_source_detail_line(31960, file_type))
    if meta.get("quality"):
        lines.append(_source_detail_line(31961, meta["quality"]))

    video_streams = row.get("video_streams") or []
    if isinstance(video_streams, dict):
        video_streams = [video_streams]
    has_video_stream = any(_source_stream_line(stream, "video") for stream in video_streams)

    if not has_video_stream and meta.get("codec"):
        lines.append(_source_detail_line(31962, meta["codec"]))
    width = str(row.get("width") or "").strip()
    height = str(row.get("height") or "").strip()
    if not has_video_stream and width and height:
        lines.append(_source_detail_line(31963, f"{width}x{height}"))
    fps = str(row.get("fps") or "").strip()
    if not has_video_stream and fps:
        lines.append(_source_detail_line(31989, fps))
    bitrate = _source_fmt_bitrate(row.get("bitrate"))
    if not has_video_stream and bitrate:
        lines.append(_source_detail_line(31990, bitrate))
    duration = _source_duration_text(row.get("duration"))
    if duration:
        lines.append(_source_detail_line(31867, duration))
    upload_date = str(row.get("upload_date") or "").strip()
    if upload_date:
        lines.append(_source_detail_line(31964, upload_date))
    if meta.get("audio"):
        lines.append(_source_detail_line(30015, meta["audio"]))
    if meta.get("subtitles"):
        lines.append(_source_detail_line(31965, meta["subtitles"]))

    for stream in video_streams:
        stream_line = _source_stream_line(stream, "video")
        if stream_line:
            lines.append(_source_detail_line(31991, stream_line))

    audio_streams = row.get("audio_streams") or []
    if isinstance(audio_streams, dict):
        audio_streams = [audio_streams]
    for stream in audio_streams:
        stream_line = _source_stream_line(stream, "audio")
        if stream_line:
            lines.append(_source_detail_line(31992, stream_line))

    available = str(row.get("available") or "").strip().lower()
    if available in ("0", "false", "no", "ne"):
        lines.append(_source_detail_line(31966, i18n.T(31974)))

    password = str(row.get("password") or "").strip().lower()
    if password in ("1", "true", "yes", "ano"):
        lines.append(_source_detail_line(31967, i18n.T(31973)))

    copyrighted = str(row.get("copyrighted") or "").strip().lower()
    if copyrighted in ("1", "true", "yes", "ano"):
        lines.append(_source_detail_line(31988, i18n.T(31973)))

    removed = str(row.get("removed") or "").strip().lower()
    if removed in ("1", "true", "yes", "ano"):
        lines.append(_source_detail_line(31968, i18n.T(31973)))
        removal_reason = str(row.get("removal_reason") or "").strip()
        if removal_reason:
            lines.append(_source_detail_line(31969, removal_reason))

    if score is not None:
        lines.append(_source_detail_line(31970, f"score {score}"))

    query = (row.get("query") or "").strip()
    if query:
        lines.append(_source_detail_line(31971, query))

    why = row.get("_why") or []
    if why:
        lines.append("")
        lines.append(i18n.T(31972) + ":")
        lines.extend(str(x) for x in why if str(x).strip())

    return lines


def _merge_provider_rows(*groups):
    out = []
    seen = set()

    for group in groups:
        for row in (group or []):
            title = (row.get("title") or "").strip()
            url = (row.get("url") or "").strip()
            key = (title.lower(), url.lower())

            if not title:
                continue
            if key in seen:
                continue
            seen.add(key)
            out.append(dict(row))

    return out

def _provider_prehrajto(query: str, title: str, original_title: str, year: str):
    q = (query or "").strip()
    if not q:
        return []

    found = prehrajto_search_all_pages(q, max_pages=3)

    rows = []
    for it in (found or []):
        rows.append(_provider_result(
            title=it.get("title") or "",
            url=it.get("url") or "",
            provider="prehrajto",
            year="",
            plot=f"Přehraj.to | dotaz: {q}",
            query=q,
            size_bytes=_source_size_bytes(it),
        ))

    return rows

def _provider_sktonline(query: str, title: str, original_title: str, year: str):
    q = " ".join((query or "").split()).strip()
    if not q:
        return []

    try:
        found = sktonline_search_all_pages(q, max_pages=5)
    except Exception:
        found = []

    rows = []
    seen_urls = set()

    for it in (found or []):
        url = (it.get("url") or "").strip()
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)

        row_title = (it.get("title") or "").strip()
        if not row_title:
            row_title = url

        rows.append(_provider_result(
            title=row_title,
            url=url,
            provider="sktonline",
            year="",
            plot=f"SkTonLine | dotaz: {q}",
            query=q,
            size_bytes=_source_size_bytes(it),
        ))

    return rows

def _provider_webshare(query: str, title: str, original_title: str, year: str,
                       limit: int = 40, max_pages: int = 1):
    q = " ".join((query or "").split()).strip()
    if not q:
        return []

    found = []
    try:
        limit = max(1, int(limit or 40))
    except Exception:
        limit = 40
    try:
        max_pages = max(1, int(max_pages or 1))
    except Exception:
        max_pages = 1

    for page in range(max_pages):
        offset = page * limit
        try:
            batch = _ws_search_simple(q, limit=limit, offset=offset) or []
        except Exception:
            batch = []
        if not batch:
            break
        found.extend(batch)
        if len(batch) < limit:
            break

    rows = []
    seen_ids = set()

    for it in (found or []):
        ident = (it.get("id") or "").strip()
        if not ident or ident in seen_ids:
            continue
        seen_ids.add(ident)

        row_title = (it.get("name") or "").strip() or ident
        size_int = int(it.get("size") or 0)

        play_url = (
            "plugin://plugin.video.ikarus/"
            "?action=film.play&id=" + urllib.parse.quote_plus(ident) +
            "&ident=" + urllib.parse.quote_plus(ident)
        )

        plot = f"WebShare | dotaz: {q}"

        rows.append(_provider_result(
            title=row_title,
            url=play_url,
            provider="webshare",
            year="",
            plot=plot,
            id=ident,
            query=q,
            size_bytes=size_int,
        ))

    return rows

_HELLSPY_API_BASE = "https://api.hellspy.to"
_HELLSPY_SEARCH_ENDPOINT = _HELLSPY_API_BASE + "/gw/search"
_HELLSPY_SITE_BASE = "https://www.hellspy.to/"
_HELLSPY_LIMIT = 64

def _cookie_header_for_url_basic(url: str) -> str:
    return source_common.cookie_header_for_url(_COOKIEJAR, url, relaxed_domain=False)




_hellspy_get_stream = source_common.hellspy_download_stream

def _hellspy_dump_first_item(query: str, item: dict):
    qkey = _cache_safe_key_part(query or "hellspy")
    if qkey in _HELLSPY_DEBUG_DUMPED:
        return
    _HELLSPY_DEBUG_DUMPED.add(qkey)

    try:
        payload = json.dumps(item or {}, ensure_ascii=False, indent=2, sort_keys=True)
        _dump_text_to_temp(f"ikarus_hellspy_first_item_{qkey}.json", payload)
    except Exception as e:
        xbmc.log(f"[IKARUS][HELLSPY] debug dump failed: {repr(e)}", xbmc.LOGWARNING)

def _hellspy_streams_from_item(item: dict, kind: str):
    if not isinstance(item, dict):
        return []

    if kind == "video":
        candidate_keys = (
            "video", "videos", "videoStream", "videoStreams",
            "stream", "streams", "media", "sources", "files"
        )
        width_keys = ("width", "w", "videoWidth")
        height_keys = ("height", "h", "videoHeight")
        format_keys = ("format", "codec", "videoCodec", "videoFormat")
        bitrate_keys = ("bitrate", "videoBitrate")
        fps_keys = ("fps", "frameRate", "framerate")
    else:
        candidate_keys = (
            "audio", "audios", "audioStream", "audioStreams",
            "audioTrack", "audioTracks", "tracks", "media", "streams"
        )
        format_keys = ("format", "codec", "audioCodec", "audioFormat")
        bitrate_keys = ("bitrate", "audioBitrate")
        channels_keys = ("channels", "channelCount", "audioChannels")
        lang_keys = ("lang", "language", "audioLanguage", "locale")

    out = []
    seen = set()

    candidates = _source_collect_dicts_from_mapping(item, candidate_keys)
    if kind == "video":
        candidates.append(item)

    for cand in candidates:
        if not isinstance(cand, dict):
            continue

        if kind == "video":
            stream = {
                "width": _source_first_from_mapping(cand, width_keys),
                "height": _source_first_from_mapping(cand, height_keys),
                "format": _source_first_from_mapping(cand, format_keys),
                "fps": _source_first_from_mapping(cand, fps_keys),
                "bitrate": _source_first_from_mapping(cand, bitrate_keys),
            }
        else:
            stream = {
                "format": _source_first_from_mapping(cand, format_keys),
                "channels": _source_first_from_mapping(cand, channels_keys),
                "lang": _source_first_from_mapping(cand, lang_keys),
                "bitrate": _source_first_from_mapping(cand, bitrate_keys),
            }

        stream = {k: str(v).strip() for k, v in stream.items() if v not in (None, "", [], {})}
        if not stream:
            continue

        key = tuple(sorted(stream.items()))
        if key in seen:
            continue
        seen.add(key)
        out.append(stream)

    return out

def _hellspy_metadata_from_item(item: dict):
    if not isinstance(item, dict):
        return {}

    meta = {
        "size_bytes": _source_size_from_mapping(item),
        "width": _source_first_from_mapping(item, ("width", "w", "videoWidth")),
        "height": _source_first_from_mapping(item, ("height", "h", "videoHeight")),
        "format": _source_first_from_mapping(item, ("format", "codec", "videoCodec", "videoFormat")),
        "fps": _source_first_from_mapping(item, ("fps", "frameRate", "framerate")),
        "bitrate": _source_first_from_mapping(item, ("bitrate", "videoBitrate")),
        "duration": _source_first_from_mapping(item, ("duration", "durationSec", "durationSeconds", "length")),
        "file_type": _source_first_from_mapping(item, ("type", "extension", "ext", "fileType", "mime")),
        "video_streams": _hellspy_streams_from_item(item, "video"),
        "audio_streams": _hellspy_streams_from_item(item, "audio"),
    }

    return {k: v for k, v in meta.items() if v not in (None, "", [], {}, 0)}

def _provider_hellspy(query: str, title: str = "", original_title: str = "", year: str = ""):
    q = " ".join((query or "").split()).strip()
    if not q:
        return []

    seen = set()
    out = []
    offset = 0
    next_offset = None
    max_pages = 1 if _query_has_episode_code(q) else 3

    for _page in range(1, max_pages + 1):
        use_offset = next_offset if next_offset is not None else offset
        url = _hellspy_build_search_url(q, use_offset, _HELLSPY_LIMIT)

        payload = _http_get_json(
            url,
            referer="https://www.hellspy.to/",
            origin="https://www.hellspy.to"
        )
        items, nxt = _hellspy_extract_items_and_next(payload)

        if not items:
            break

        added = 0
        for it in items:
            row_title = (it.get("title") or "").strip()
            vid = str(it.get("id") or "").strip()
            hsh = str(it.get("fileHash") or it.get("hash") or "").strip()

            if not row_title or not vid or not hsh:
                continue

            item_url = f"{_HELLSPY_SITE_BASE}video/{hsh}/{vid}"
            if item_url in seen:
                continue
            seen.add(item_url)

            direct = _hellspy_pick_direct_url(it)
            _hellspy_dump_first_item(q, it)

            plot_lines = [
                f"Hellspy | dotaz: {q}",
            ]
            meta = {
                "query": q,
                "size_bytes": _source_size_from_mapping(it),
                "quality": str(it.get("resolution") or it.get("quality") or "").strip(),
                "duration": (it.get("duration") or it.get("length") or ""),
            }
            meta.update(_hellspy_metadata_from_item(it))

            out.append(_provider_result(
                title=row_title,
                url=item_url,
                provider="hellspy",
                year="",
                plot="\n".join(plot_lines),
                direct=direct,
                id=vid,
                fileHash=hsh,
                **meta,
            ))
            added += 1

        if added == 0:
            break

        if nxt is not None and nxt != use_offset:
            next_offset = nxt
        else:
            offset = use_offset + _HELLSPY_LIMIT
            next_offset = None

    return out

_SOURCE_PROVIDERS = {
    "prehrajto": _provider_prehrajto,
    "sktonline": _provider_sktonline,
    "hellspy": _provider_hellspy,
    "webshare": _provider_webshare,
}

def _build_candidate_queries(title: str, original_title: str, year: str,
                             mode: str = "", season: str = "", episode: str = "", episode_title: str = "",
                             provider_name: str = ""):
    provider_name = (provider_name or "").strip().lower()
    mode = (mode or "").strip()

    out = []
    seen = set()

    def add_query(q):
        q = _search_safe_text(q)
        if not q:
            return
        k = q.lower()
        if k in seen:
            return
        seen.add(k)
        out.append(q)

    if provider_name == "prehrajto":
        if mode == "serial_episode":
            for base in _title_search_variants(original_title):
                for number in _episode_query_number_variants(season, episode):
                    add_query(f"{base} {number}")
            for base in _title_search_variants(title):
                for number in _episode_query_number_variants(season, episode):
                    add_query(f"{base} {number}")

        for base in _title_search_variants(original_title):
            add_query(base)
        for base in _short_title_search_variants(title):
            add_query(base)
        for base in _short_title_search_variants(original_title):
            add_query(base)
        for base in _title_search_variants(title):
            add_query(base)

        if mode == "serial_episode" and not _is_generic_episode_title(episode_title):
            add_query(episode_title)
            for number in _episode_query_number_variants(season, episode):
                add_query(f"{episode_title} {number}")

        return out

    if mode == "serial_episode":
        out = []
        seen = set()

        def add_query(q):
            q = _search_safe_text(q)
            if not q:
                return
            k = q.lower()
            if k in seen:
                return
            seen.add(k)
            out.append(q)

        if provider_name == "webshare":
            for base in _title_search_variants(original_title):
                add_query(base)
            for base in _title_search_variants(title):
                add_query(base)
            if not _is_generic_episode_title(episode_title):
                add_query(episode_title)
            for base in (original_title, title):
                base = (base or "").strip()
                if not base:
                    continue
                for number in _episode_query_number_variants(season, episode):
                    add_query(f"{base} {number}")
                for compact in _title_search_variants(base):
                    for number in _episode_query_number_variants(season, episode):
                        add_query(f"{compact} {number}")
            if episode_title and not _is_generic_episode_title(episode_title):
                for number in _episode_query_number_variants(season, episode):
                    add_query(f"{episode_title} {number}")
        else:
            for base in _title_search_variants(title):
                for number in _episode_query_number_variants(season, episode):
                    add_query(f"{base} {number}")
            for base in _title_search_variants(original_title):
                for number in _episode_query_number_variants(season, episode):
                    add_query(f"{base} {number}")
            for base in _title_search_variants(title):
                add_query(base)
            for base in _title_search_variants(original_title):
                add_query(base)
            if not _is_generic_episode_title(episode_title):
                add_query(episode_title)

        return out

    return _query_variants_for_search(title, original_title, year)

def _run_provider_multi(provider_name: str, title: str, original_title: str, year: str,
                        mode: str = "", season: str = "", episode: str = "", episode_title: str = "",
                        season_name: str = ""):
    started_at = __import__("time").time()
    fn = _SOURCE_PROVIDERS.get((provider_name or "").strip())
    if not fn:
        return []

    queries = _build_candidate_queries(
        title=title,
        original_title=original_title,
        year=year,
        mode=mode,
        season=season,
        episode=episode,
        episode_title=episode_title,
        provider_name=provider_name
    )

    def has_fast_exact_match(rows):
        if (mode or "").strip() != "serial_episode":
            return False
        for row in _sort_provider_rows(rows):
            cand_title = (row.get("title") or "").strip()
            exact, _strong, _code, _pos = _episode_number_match_info(cand_title, season, episode)
            if exact and int(row.get("_score", 0) or 0) >= 90:
                return True
        return False

    groups = []
    filtered = []
    for q in queries:
        if (provider_name or "").strip().lower() == "webshare" and mode == "serial_episode":
            rows = fn(
                query=q,
                title=title,
                original_title=original_title,
                year=year,
                limit=100,
                max_pages=5
            ) or []
        else:
            rows = fn(
                query=q,
                title=title,
                original_title=original_title,
                year=year
            ) or []
        groups.append(rows)

        provider_key = (provider_name or "").strip().lower()
        if provider_key in ("prehrajto", "hellspy", "sktonline") and mode == "serial_episode":
            merged_now = _merge_provider_rows(*groups)
            filtered_now = _filter_candidates(
                rows=merged_now,
                title=title,
                original_title=original_title,
                year=year,
                min_score=35,
                mode=mode,
                season=season,
                episode=episode,
                episode_title=episode_title,
                season_name=season_name
            )
            filtered = filtered_now
            if _query_has_episode_code(q) and has_fast_exact_match(filtered_now):
                break

    merged = _merge_provider_rows(*groups)
    if not filtered or len(groups) == len(queries):
        filtered = _filter_candidates(
            rows=merged,
            title=title,
            original_title=original_title,
            year=year,
            min_score=35,
            mode=mode,
            season=season,
            episode=episode,
            episode_title=episode_title,
            season_name=season_name
        )

    provider_key = (provider_name or "").strip().lower()
    if provider_key == "webshare":
        filtered = _enrich_webshare_rows(filtered)
        if (mode or "").strip() == "serial_episode":
            filtered = [
                row for row in filtered
                if _webshare_serial_episode_match_kind(
                    row,
                    media_title=title,
                    original_title=original_title,
                    season=season,
                    episode=episode,
                    episode_title=episode_title,
                    season_name=season_name,
                ) != "reject"
            ]
    elif provider_key == "prehrajto":
        # Search pages already contain enough text for filtering and basic labels.
        # Per-item detail requests are noticeably expensive and are not needed to list results.
        filtered = list(filtered or [])

    elapsed = __import__("time").time() - started_at
    xbmc.log(
        f"[IKARUS][SRC PERF] provider={provider_key or provider_name} queries={len(queries)} results={len(filtered or [])} sec={elapsed:.2f}",
        xbmc.LOGWARNING
    )

    return filtered

def _run_provider_sktonline_raw_debug(title: str, original_title: str):
    fn = _SOURCE_PROVIDERS.get("sktonline")
    if not fn:
        return []

    queries = []
    seen_q = set()

    def add_query(q):
        q = _search_safe_text(q)
        if not q:
            return
        k = q.lower()
        if k in seen_q:
            return
        seen_q.add(k)
        queries.append(q)

    for variant in _title_search_variants(title):
        add_query(variant)
    for variant in _title_search_variants(original_title):
        add_query(variant)

    all_rows = []

    for q in queries:
        rows = fn(
            query=q,
            title=title,
            original_title=original_title,
            year=""
        ) or []

        if rows:
            all_rows = _merge_provider_rows(all_rows, rows)

    return all_rows

def _provider_is_enabled_for_auto_next(provider_name: str) -> bool:
    provider_name = (provider_name or "").strip().lower()
    if provider_name == "prehrajto":
        return _setting_enabled("show_prehrajto", default=True)
    if provider_name == "hellspy":
        return _setting_enabled("show_hellspy", default=True)
    if provider_name == "sktonline":
        return _setting_enabled("show_sktonline", default=True)
    if provider_name == "webshare":
        return _setting_enabled("show_webshare", default=True) and _webshare_connected()
    return False


_AUTO_NEXT_MAX_SOURCE_ATTEMPTS = auto_next_policy.MAX_SOURCE_ATTEMPTS


def _auto_next_attempt_key(provider: str, source_index) -> str:
    return auto_next_policy.source_attempt_key(provider, source_index)


def _auto_next_attempts_parse(value) -> list:
    return auto_next_policy.parse_source_attempts(value)


def _auto_next_attempts_encode(items) -> str:
    return auto_next_policy.encode_source_attempts(items)


def _tmdb_auto_next_provider_order(preferred_provider: str = ""):
    order = ["prehrajto", "hellspy", "sktonline", "webshare"]
    preferred_provider = (preferred_provider or "").strip().lower()
    if preferred_provider in order:
        order.remove(preferred_provider)
        order.insert(0, preferred_provider)
    return [p for p in order if _provider_is_enabled_for_auto_next(p)]

def _tmdb_auto_next_language_score(row: dict) -> int:
    title = (row or {}).get("title") or ""
    plot = (row or {}).get("plot") or ""
    tokens = set(_title_tokens(f"{title} {plot}"))
    text = _norm_text(f"{title} {plot}")

    cz_tokens = {
        "czdab", "czdabing", "ceskydabing", "cesky", "czech",
        "cz", "ceske", "ceska",
    }
    sk_tokens = {
        "skdab", "skdabing", "slovenskydabing", "slovensky", "slovak",
        "sk", "slovenske", "slovenska",
    }
    sub_tokens = {"titulky", "sub", "subs", "subtitle", "subtitles", "czsub", "czsubs"}
    original_tokens = {"en", "eng", "english", "original", "orig", "vo"}

    score = 0
    if tokens & cz_tokens:
        score += 120
    if tokens & sk_tokens:
        score += 70
    if "dabing" in tokens or "dab" in tokens or "dabing" in text:
        score += 45
    if tokens & sub_tokens:
        score -= 45
    if tokens & original_tokens:
        score -= 15
    return score


def _tmdb_auto_next_has_required_audio(row: dict) -> bool:
    row = row or {}
    provider = str(row.get("provider") or "").strip().lower()
    if provider == "webshare":
        langs = _webshare_audio_langs(row)
        if langs:
            return "CZ" in langs or "SK" in langs

    meta = _source_metadata(row)
    audio_text = " ".join(
        str(x or "")
        for x in (
            meta.get("audio"),
            row.get("audio"),
            row.get("audio_lang"),
        )
    )
    tags = set(_source_lang_tag_from_text(audio_text).split(","))
    return bool({"CZ", "SK"} & tags)


def _tmdb_auto_next_source_rows(provider: str, title: str, original_title: str, year: str,
                                season: str, episode: str, episode_title: str,
                                season_name: str = ""):
    provider = (provider or "").strip().lower()
    cached_rows = _source_cache_get(
        provider=provider,
        title=title,
        original_title=original_title,
        year=year,
        mode="serial_episode",
        season=season,
        episode=episode
    )
    if cached_rows is not None:
        filtered = cached_rows
    elif provider == "sktonline":
        raw_rows = _run_provider_sktonline_raw_debug(
            title=title,
            original_title=original_title
        )
        filtered = _filter_sktonline_serial_episode_rows(
            rows=raw_rows,
            title=title,
            original_title=original_title,
            season=season,
            episode=episode,
            episode_title=episode_title,
            season_name=season_name
        )
        _source_cache_set(
            provider=provider,
            title=title,
            original_title=original_title,
            year=year,
            rows=filtered,
            mode="serial_episode",
            season=season,
            episode=episode
        )
    else:
        filtered = _run_provider_multi(
            provider_name=provider,
            title=title,
            original_title=original_title,
            year=year,
            mode="serial_episode",
            season=season,
            episode=episode,
            episode_title=episode_title,
            season_name=season_name
        )
        _source_cache_set(
            provider=provider,
            title=title,
            original_title=original_title,
            year=year,
            rows=filtered,
            mode="serial_episode",
            season=season,
            episode=episode
        )

    if provider == "webshare":
        main_rows, episode_title_only_rows = _split_webshare_episode_title_only_rows(
            filtered,
            media_title=title,
            original_title=original_title,
            season=season,
            episode=episode,
            episode_title=episode_title,
            season_name=season_name,
        )
        return _sort_webshare_rows(main_rows) + _sort_webshare_rows(episode_title_only_rows)
    return list(filtered or [])


def _tmdb_auto_next_pick_first(rows, start_index: int = 0, require_cz_dabing: bool = False,
                               provider: str = "", attempted_keys=None):
    attempted_keys = set(str(x or "").strip().lower() for x in (attempted_keys or []))
    picked = None
    for idx, row in enumerate(rows or []):
        if idx < max(0, int(start_index or 0)):
            continue
        if not isinstance(row, dict):
            continue
        row_provider = str(row.get("provider") or provider or "").strip().lower()
        if _auto_next_attempt_key(row_provider, idx) in attempted_keys:
            continue
        if not ((row.get("url") or "").strip() or (row.get("direct") or "").strip()):
            continue
        if require_cz_dabing and not _tmdb_auto_next_has_required_audio(row):
            continue
        lang_score = _tmdb_auto_next_language_score(row)
        item = dict(row)
        item["_auto_next_lang_score"] = lang_score
        item["_auto_next_index"] = idx
        if picked is None or lang_score > int(picked.get("_auto_next_lang_score") or 0):
            picked = item
    return picked


def _tmdb_auto_next_provider_order_after(current_provider: str = ""):
    order = _tmdb_auto_next_provider_order(current_provider)
    current_provider = (current_provider or "").strip().lower()
    if current_provider in order:
        idx = order.index(current_provider)
        return order[idx:] + order[:idx]
    return order

def find_best_tmdb_episode_source(title: str, original_title: str, year: str,
                                  season: str, episode: str, episode_title: str,
                                  preferred_provider: str = "",
                                  require_cz_dabing: bool = True,
                                  season_name: str = ""):
    """
    Tiche hledani pro autoplay dalsi TMDB epizody. Vraci prvni nejlepsi radek
    podle stejneho filtrovani a cache, ktere pouziva UI seznam zdroju.
    """
    for provider in _tmdb_auto_next_provider_order(preferred_provider):
        try:
            filtered = _tmdb_auto_next_source_rows(
                provider=provider,
                title=title,
                original_title=original_title,
                year=year,
                season=season,
                episode=episode,
                episode_title=episode_title,
                season_name=season_name
            )
            picked = _tmdb_auto_next_pick_first(
                filtered,
                start_index=0,
                require_cz_dabing=require_cz_dabing,
                provider=provider,
            )
            if picked:
                return picked
        except Exception as e:
            try:
                xbmc.log(f"[IKARUS][AUTO NEXT] provider={provider} failed: {e}", xbmc.LOGWARNING)
            except Exception:
                pass

    return None

def find_next_tmdb_episode_source(title: str, original_title: str, year: str,
                                  season: str, episode: str, episode_title: str,
                                  current_provider: str = "", current_index: int = -1,
                                  season_name: str = "",
                                  require_cz_dabing: bool = True,
                                  attempted_keys=None):
    current_provider = (current_provider or "").strip().lower()
    try:
        current_index = int(current_index)
    except Exception:
        current_index = -1

    for provider in _tmdb_auto_next_provider_order_after(current_provider):
        try:
            start_index = current_index + 1 if provider == current_provider else 0
            rows = _tmdb_auto_next_source_rows(
                provider=provider,
                title=title,
                original_title=original_title,
                year=year,
                season=season,
                episode=episode,
                episode_title=episode_title,
                season_name=season_name
            )
            picked = _tmdb_auto_next_pick_first(
                rows,
                start_index=start_index,
                require_cz_dabing=require_cz_dabing,
                provider=provider,
                attempted_keys=attempted_keys,
            )
            if picked:
                return picked
        except Exception as e:
            try:
                xbmc.log(f"[IKARUS][AUTO NEXT] fallback provider={provider} failed: {e}", xbmc.LOGWARNING)
            except Exception:
                pass
    return None

def build_tmdb_episode_source_play_url(row: dict, tmdb_id: str, title: str,
                                       original_title: str, year: str,
                                       season: str, episode: str,
                                       episode_title: str, season_name: str = "",
                                       auto_next: bool = False,
                                       auto_tried: str = "",
                                       auto_trace: str = "") -> str:
    row = row or {}
    provider_name = (row.get("provider") or "").strip()
    row_url = (row.get("url") or "").strip()
    row_direct = (row.get("direct") or "").strip()
    query = {
        "action": "tmdb.movie.source.play",
        "provider": provider_name,
        "tmdb_id": tmdb_id,
        "title": row.get("title") or title,
        "media_title": title,
        "url": row_url,
        "direct": row_direct,
        "id": row.get("id") or "",
        "fileHash": row.get("fileHash") or "",
        "original_title": original_title,
        "year": year,
        "mode": "serial_episode",
        "season": season,
        "episode": episode,
        "episode_title": episode_title,
        "season_name": season_name,
    }
    if auto_next:
        query["auto_next"] = "1"
        query["source_index"] = str(row.get("_auto_next_index", "0"))
        if auto_tried:
            query["auto_tried"] = _auto_next_attempts_encode(_auto_next_attempts_parse(auto_tried))
        if auto_trace:
            query["auto_trace"] = str(auto_trace).strip()[:80]
    return build_url(query)

def source_search_stub_menu():
    action_started_at = __import__("time").time()
    provider = _get_param("provider", "").strip()
    tmdb_id = _get_param("tmdb_id", "").strip() or _get_param("id", "").strip()
    query = _get_param("query", "").strip()
    title = _get_param("title", "Film").strip()
    original_title = _get_param("original_title", "").strip()
    year = _get_param("year", "").strip()
    mode = _get_param("mode", "").strip()
    season = _get_param("season", "").strip()
    episode = _get_param("episode", "").strip()
    episode_title = _get_param("episode_title", "").strip()
    season_name = _get_param("season_name", "").strip()
    force_refresh = _get_param("force_refresh", "").strip() == "1"
    refresh_token = _get_param("refresh_token", "").strip()

    def _log_total_stub_time(result_count: int = 0, cached: bool = False, stage: str = "done"):
        try:
            elapsed = __import__("time").time() - action_started_at
            xbmc.log(
                (
                    f"[IKARUS][SRC TOTAL] provider={provider or 'menu'} stage={stage} "
                    f"cached={'1' if cached else '0'} results={int(result_count or 0)} "
                    f"sec={elapsed:.2f} title={title} year={year} mode={mode} season={season} episode={episode}"
                ),
                xbmc.LOGWARNING
            )
        except Exception:
            pass

    def _log_phase_stub_time(phase: str, started_at: float, result_count: int = 0):
        try:
            elapsed = __import__("time").time() - float(started_at or action_started_at)
            xbmc.log(
                (
                    f"[IKARUS][SRC PHASE] provider={provider or 'menu'} phase={phase} "
                    f"results={int(result_count or 0)} sec={elapsed:.2f} "
                    f"title={title} year={year} mode={mode} season={season} episode={episode}"
                ),
                xbmc.LOGWARNING
            )
        except Exception:
            pass

    def _provider_display_name(provider_name: str) -> str:
        names = {
            "webshare": "WebShare",
            "sktonline": "SkTonLine",
            "prehrajto": "Přehraj.to",
            "hellspy": "Hellspy",
        }
        return names.get((provider_name or "").strip().lower(), provider_name or "Provider")

    def _enabled_source_providers():
        enabled = []
        if _setting_enabled("show_webshare", default=True) and _webshare_connected():
            enabled.append("webshare")
        if _setting_enabled("show_sktonline", default=True):
            enabled.append("sktonline")
        if _setting_enabled("show_prehrajto", default=True):
            enabled.append("prehrajto")
        if _setting_enabled("show_hellspy", default=True):
            enabled.append("hellspy")
        return enabled

    def _source_count_word(count: int) -> str:
        try:
            count = int(count or 0)
        except Exception:
            count = 0
        if count == 1:
            return i18n.T(30749, "source")
        if count in (2, 3, 4):
            return i18n.T(30750, "sources")
        return i18n.T(30751, "sources")

    def _source_query(provider_name: str, extra: dict = None) -> dict:
        out = {
            "action": "tmdb.movie.sources.search.stub",
            "provider": provider_name,
            "tmdb_id": tmdb_id,
            "query": query,
            "title": title,
            "original_title": original_title,
            "year": year,
            "mode": mode,
            "season": season,
            "episode": episode,
            "episode_title": episode_title,
            "season_name": season_name,
        }
        if extra:
            out.update(extra)
        return out

    def _load_source_rows(provider_name: str, clear_cache: bool = False):
        provider_name = (provider_name or "").strip().lower()
        if clear_cache:
            _source_cache_clear(
                provider=provider_name,
                title=title,
                original_title=original_title,
                year=year,
                mode=mode,
                season=season,
                episode=episode
            )
            cached_rows = None
        else:
            cached_rows = _source_cache_get(
                provider=provider_name,
                title=title,
                original_title=original_title,
                year=year,
                mode=mode,
                season=season,
                episode=episode
            )

        if cached_rows is not None:
            xbmc.log(
                f"[IKARUS][SRC CACHE] HIT provider={provider_name} title={title} year={year} mode={mode} season={season} episode={episode}",
                xbmc.LOGWARNING
            )
            return cached_rows, True

        xbmc.log(
            f"[IKARUS][SRC CACHE] MISS provider={provider_name} title={title} year={year} mode={mode} season={season} episode={episode}",
            xbmc.LOGWARNING
        )

        if provider_name == "sktonline":
            raw_rows = _run_provider_sktonline_raw_debug(
                title=title,
                original_title=original_title
            )

            if mode == "serial_episode":
                filtered_rows = _filter_sktonline_serial_episode_rows(
                    rows=raw_rows,
                    title=title,
                    original_title=original_title,
                    season=season,
                    episode=episode,
                    episode_title=episode_title,
                    season_name=season_name
                )
            else:
                filtered_rows = raw_rows
        else:
            filtered_rows = _run_provider_multi(
                provider_name=provider_name,
                title=title,
                original_title=original_title,
                year=year,
                mode=mode,
                season=season,
                episode=episode,
                episode_title=episode_title,
                season_name=season_name
            )

        _source_cache_set(
            provider=provider_name,
            title=title,
            original_title=original_title,
            year=year,
            rows=filtered_rows,
            mode=mode,
            season=season,
            episode=episode
        )
        return filtered_rows, False

    def _render_source_rows(provider_name: str, rows):
        provider_name = (provider_name or "").strip().lower()
        rows = list(rows or [])

        if provider_name == "webshare":
            _add_webshare_result_rows(
                rows=rows,
                tmdb_id=tmdb_id,
                media_title=title,
                original_title=original_title,
                year=year,
                mode=mode,
                season=season,
                episode=episode,
                episode_title=episode_title,
                season_name=season_name,
                group_items=False,
                status_resolver=source_status_resolver
            )
            return

        _add_provider_result_rows(
            rows=rows,
            provider_name=provider_name,
            tmdb_id=tmdb_id,
            media_title=title,
            original_title=original_title,
            year=year,
            mode=mode,
            season=season,
            episode=episode,
            episode_title=episode_title,
            season_name=season_name,
            group_items=False,
            status_resolver=source_status_resolver,
        )
        return

    def _queue_all_sources_refresh(next_stage: int, token: str = ""):
        try:
            next_url = build_url(_source_query("all", {
                "all_stage": str(int(next_stage or 0)),
                "refresh_token": token or "",
            }))
            xbmc.sleep(250)
            xbmc.executebuiltin(f'Container.Update("{next_url}",replace)')
        except Exception:
            pass

    source_status_resolver, provider_status_resolver = _make_source_status_resolvers(
        tmdb_id,
        season=season if mode == "serial_episode" else None,
        episode=episode if mode == "serial_episode" else None,
    )

    xbmcplugin.setPluginCategory(
        addon_handle,
        i18n.T(30738, "Sources / Results / {title}").format(title=title)
    )
    xbmcplugin.setContent(addon_handle, "files")

    if not provider:
        def _provider_label(base_label: str, provider_name: str) -> str:
            provider_status = provider_status_resolver(
                provider_name,
                season=season if mode == "serial_episode" else None,
                episode=episode if mode == "serial_episode" else None,
            )

            if provider_status == "finished":
                return f"[COLOR lime]{base_label}[/COLOR]"
            if provider_status == "started":
                return f"[COLOR orange]{base_label}[/COLOR]"
            return base_label

        enabled_providers = _enabled_source_providers()
        show_prehrajto = "prehrajto" in enabled_providers
        show_hellspy = "hellspy" in enabled_providers
        show_sktonline = "sktonline" in enabled_providers
        show_webshare = "webshare" in enabled_providers

        def _add_filtered_result_dir(query=None, **kwargs):
            provider_name = (((query or {}).get("provider")) or "").strip().lower()
            if provider_name == "all" and not enabled_providers:
                return
            if provider_name == "prehrajto" and not show_prehrajto:
                return
            if provider_name == "hellspy" and not show_hellspy:
                return
            if provider_name == "sktonline" and not show_sktonline:
                return
            if provider_name == "webshare" and not show_webshare:
                return
            label = str(kwargs.get("label") or "")
            if label and "[COLOR " not in label:
                kwargs["label"] = _provider_label(label, provider_name)
            return _add_result_dir(query=query, **kwargs)

        _add_filtered_result_dir(
            label=i18n.T(30739, "All"),
            query=_source_query("all", {"all_stage": "0"}),
            plot=i18n.T(30740, "Search all enabled providers in sequence\nQuery: {query}").format(query=query),
            icon="DefaultAddonProgram.png"
        )

        _add_filtered_result_dir(
            label=_provider_label("WebShare", "webshare"),
            query=_source_query("webshare"),
            plot=i18n.T(30741, "Search on {provider}\nQuery: {query}").format(provider="WebShare", query=query),
            icon="DefaultAddonProgram.png"
        )

        _add_filtered_result_dir(
            label=_provider_label("SkTonLine", "sktonline"),
            query=_source_query("sktonline"),
            plot=i18n.T(30741, "Search on {provider}\nQuery: {query}").format(provider="SkTonLine", query=query),
            icon="DefaultAddonProgram.png"
        )

        _add_filtered_result_dir(
            label="Přehraj.to",
            query=_source_query("prehrajto"),
            plot=i18n.T(30741, "Search on {provider}\nQuery: {query}").format(provider="Přehraj.to", query=query),
            icon="DefaultAddonProgram.png"
        )

        _add_filtered_result_dir(
            label=_provider_label("Hellspy", "hellspy"),
            query=_source_query("hellspy"),
            plot=i18n.T(30741, "Search on {provider}\nQuery: {query}").format(provider="Hellspy", query=query),
            icon="DefaultAddonProgram.png"
        )

        _log_total_stub_time(result_count=len(enabled_providers) + (1 if enabled_providers else 0), cached=False, stage="provider-menu")
        _end("files")
        return

    if provider.lower() == "all":
        all_started_at = __import__("time").time()
        enabled_providers = _enabled_source_providers()
        if not enabled_providers:
            _add_file(
                label=i18n.T(30742, "No provider is enabled"),
                plot=i18n.T(30743, "Enable at least one provider in add-on settings."),
                icon="DefaultAddonProgram.png"
            )
            _log_total_stub_time(result_count=0, cached=False, stage="all-disabled")
            _end("files")
            return

        clear_all_cache = force_refresh and not _refresh_token_already_handled(refresh_token)
        if clear_all_cache:
            _mark_refresh_token_handled(refresh_token)
            for provider_name in enabled_providers:
                _source_cache_clear(
                    provider=provider_name,
                    title=title,
                    original_title=original_title,
                    year=year,
                    mode=mode,
                    season=season,
                    episode=episode
                )

        all_rows_by_provider = {}
        had_cache_hit = False

        for provider_name in enabled_providers:
            try:
                rows, cache_hit = _load_source_rows(provider_name, clear_cache=False)
            except Exception as e:
                rows = []
                cache_hit = False
                xbmc.log(
                    f"[IKARUS][SRC ALL] provider={provider_name} failed: {repr(e)}",
                    xbmc.LOGWARNING
                )
            all_rows_by_provider[provider_name] = list(rows or [])
            had_cache_hit = had_cache_hit or cache_hit

        render_started_at = __import__("time").time()
        for provider_name in enabled_providers:
            rows = all_rows_by_provider.get(provider_name) or []
            display_name = _provider_display_name(provider_name)
            _add_info_file(
                label=i18n.T(30752, "■ {provider} - found {count} {count_word}").format(
                    provider=display_name,
                    count=len(rows),
                    count_word=_source_count_word(len(rows))
                ),
                plot=i18n.T(30753, "Provider: {provider}\nFound: {count}").format(
                    provider=display_name,
                    count=len(rows)
                ),
                color="skyblue",
                icon="DefaultAddonProgram.png"
            )
            if rows:
                _render_source_rows(provider_name, rows)

        _log_phase_stub_time(
            "all-render",
            render_started_at,
            sum(len(v or []) for v in all_rows_by_provider.values())
        )

        _log_total_stub_time(
            result_count=sum(len(v or []) for v in all_rows_by_provider.values()),
            cached=had_cache_hit,
            stage="all-listed"
        )
        _end("files")
        return

    try:
        cache_hit = False
        if force_refresh and not _refresh_token_already_handled(refresh_token):
            _source_cache_clear(
                provider=provider,
                title=title,
                original_title=original_title,
                year=year,
                mode=mode,
                season=season,
                episode=episode
            )
            _mark_refresh_token_handled(refresh_token)
            cached_rows = None
        else:
            cached_rows = _source_cache_get(
                provider=provider,
                title=title,
                original_title=original_title,
                year=year,
                mode=mode,
                season=season,
                episode=episode
            )

        if cached_rows is not None:
            filtered = cached_rows
            cache_hit = True
            xbmc.log(
                f"[IKARUS][SRC CACHE] HIT provider={provider} title={title} year={year} mode={mode} season={season} episode={episode}",
                xbmc.LOGWARNING
            )
        else:
            xbmc.log(
                f"[IKARUS][SRC CACHE] MISS provider={provider} title={title} year={year} mode={mode} season={season} episode={episode}",
                xbmc.LOGWARNING
            )

            if provider == "sktonline":
                raw_rows = _run_provider_sktonline_raw_debug(
                    title=title,
                    original_title=original_title
                )

                if mode == "serial_episode":
                    filtered = _filter_sktonline_serial_episode_rows(
                        rows=raw_rows,
                        title=title,
                        original_title=original_title,
                        season=season,
                        episode=episode,
                        episode_title=episode_title,
                        season_name=season_name
                    )
                else:
                    filtered = raw_rows
            else:
                filtered = _run_provider_multi(
                    provider_name=provider,
                    title=title,
                    original_title=original_title,
                    year=year,
                    mode=mode,
                    season=season,
                    episode=episode,
                    episode_title=episode_title,
                    season_name=season_name
                )

            _source_cache_set(
                provider=provider,
                title=title,
                original_title=original_title,
                year=year,
                rows=filtered,
                mode=mode,
                season=season,
                episode=episode
            )

    except Exception as e:
        _add_file(
            label=i18n.T(30744, "Search error"),
            plot=i18n.T(30754, "Provider: {provider}\nQuery: {query}\nError: {error}").format(
                provider=provider,
                query=query,
                error=repr(e)
            ),
            icon="DefaultAddonProgram.png"
        )
        _log_total_stub_time(result_count=0, cached=False, stage="error")
        _end("files")
        return

    _add_result_dir(
        label=i18n.T(30745, "Refresh sources"),
        query={
            "action": "tmdb.movie.sources.search.stub",
            "provider": provider,
            "tmdb_id": tmdb_id,
            "query": query,
            "title": title,
            "original_title": original_title,
            "year": year,
            "mode": mode,
            "season": season,
            "episode": episode,
            "episode_title": episode_title,
            "season_name": season_name,
            "force_refresh": "1",
            "refresh_token": str(__import__("time").time()),
        },
        plot=i18n.T(30755, "Provider: {provider}\nQuery: {query}\n{message}").format(
            provider=provider,
            query=query,
            message=i18n.T(30746, "Force a new search and overwrite cached results.")
        ),
        icon="DefaultAddonsSearch.png"
    )
    _log_phase_stub_time("search", action_started_at, len(filtered or []))

    if not filtered:
        debug_base = f"skt_search_{_cache_safe_key_part(title or query)}"
        _add_file(
            label=i18n.T(30747, "Nothing found"),
            plot=i18n.T(
                30756,
                "Provider: {provider}\nQuery: {query}\nTitle: {title}\nOriginal title: {original_title}\nYear: {year}\nMode: {mode}\nSeason: {season}\nEpisode: {episode}\nEpisode title: {episode_title}\n\nDebug dump: {debug_dump}\nDebug info: {debug_info}"
            ).format(
                provider=provider,
                query=query,
                title=title,
                original_title=original_title,
                year=year,
                mode=mode,
                season=season,
                episode=episode,
                episode_title=episode_title,
                debug_dump=f"special://temp/{debug_base}_page1.html",
                debug_info=f"special://temp/{debug_base}_debug.txt"
            ),
            icon="DefaultAddonProgram.png"
        )
        _log_total_stub_time(result_count=0, cached=cache_hit, stage="empty")
        _end("files")
        return

    if provider == "webshare":
        render_started_at = __import__("time").time()
        _add_webshare_result_rows(
            rows=filtered,
            tmdb_id=tmdb_id,
            media_title=title,
            original_title=original_title,
            year=year,
            mode=mode,
            season=season,
            episode=episode,
            episode_title=episode_title,
            season_name=season_name,
            group_items=False,
            status_resolver=source_status_resolver
        )
        _log_phase_stub_time("render", render_started_at, len(filtered or []))
        _log_total_stub_time(result_count=len(filtered or []), cached=cache_hit, stage="listed")
        _end("files")
        return

    render_started_at = __import__("time").time()
    _add_provider_result_rows(
        rows=filtered,
        provider_name=provider,
        tmdb_id=tmdb_id,
        media_title=title,
        original_title=original_title,
        year=year,
        mode=mode,
        season=season,
        episode=episode,
        episode_title=episode_title,
        season_name=season_name,
        group_items=False,
        status_resolver=source_status_resolver,
    )
    _log_phase_stub_time("render", render_started_at, len(filtered or []))
    _log_total_stub_time(result_count=len(filtered or []), cached=cache_hit, stage="listed")
    _end("files")
    return

def _source_row_status(row: dict, tmdb_id: str, lookup_title: str = "", status_resolver=None) -> str:
    if status_resolver is not None:
        try:
            return status_resolver(row, lookup_title=lookup_title)
        except Exception:
            return ""
    try:
        from resources.lib import tmdb_playback_state
        return (
            tmdb_playback_state.find_source_status(
                tmdb_id=tmdb_id,
                provider=row.get("provider") or "",
                url=row.get("url") or "",
                direct=row.get("direct") or "",
                source_id=row.get("id") or "",
                file_hash=row.get("fileHash") or "",
                title=lookup_title or row.get("title") or "",
            )
            if tmdb_id else ""
        )
    except Exception:
        return ""

def _source_status_color(label: str, status: str) -> str:
    if status == "finished":
        return f"[COLOR lime]{label}[/COLOR]"
    if status == "started":
        return f"[COLOR orange]{label}[/COLOR]"
    if status == "delete":
        return f"[COLOR red]{label}[/COLOR]"
    return label

def _make_source_status_resolvers(tmdb_id: str, season=None, episode=None):
    def _blank_source_status(row: dict, lookup_title: str = "") -> str:
        return ""

    def _blank_provider_status(provider_name: str, season=None, episode=None) -> str:
        return ""

    tmdb_id = str(tmdb_id or "").strip()
    if not tmdb_id:
        return _blank_source_status, _blank_provider_status

    try:
        from resources.lib import tmdb_playback_state
        state = tmdb_playback_state.playback_state_load() or {}
        state_row = state.get(tmdb_id) or {}
        if not isinstance(state_row, dict):
            state_row = {}

        source_cache = {}
        provider_cache = {}

        def _source_status(row: dict, lookup_title: str = "") -> str:
            row = row if isinstance(row, dict) else {}
            cache_key = (
                str(row.get("provider") or ""),
                str(row.get("url") or ""),
                str(row.get("direct") or ""),
                str(row.get("id") or ""),
                str(row.get("fileHash") or ""),
                str(lookup_title or row.get("title") or ""),
            )
            cached = source_cache.get(cache_key)
            if cached is not None:
                return cached
            status = tmdb_playback_state.find_source_status_in_row(
                state_row,
                provider=row.get("provider") or "",
                url=row.get("url") or "",
                direct=row.get("direct") or "",
                source_id=row.get("id") or "",
                file_hash=row.get("fileHash") or "",
                title=lookup_title or row.get("title") or "",
            ) or ""
            source_cache[cache_key] = status
            return status

        def _provider_status(provider_name: str, season=None, episode=None) -> str:
            cache_key = (
                str(provider_name or "").strip().lower(),
                str(season or ""),
                str(episode or ""),
            )
            cached = provider_cache.get(cache_key)
            if cached is not None:
                return cached
            status = tmdb_playback_state.get_provider_status_in_row(
                state_row,
                provider=provider_name or "",
                season=season,
                episode=episode,
            ) or ""
            provider_cache[cache_key] = status
            return status

        return _source_status, _provider_status
    except Exception:
        return _blank_source_status, _blank_provider_status

def _source_rows_status(rows, tmdb_id: str, status_resolver=None) -> str:
    statuses = []
    for row in _sort_provider_rows(rows):
        if not isinstance(row, dict):
            continue
        if (row.get("provider") or "").strip().lower() == "webshare":
            lookup_title = _webshare_source_name(row)
        else:
            lookup_title = row.get("title") or ""
        status = _source_row_status(row, tmdb_id, lookup_title=lookup_title, status_resolver=status_resolver)
        if status:
            statuses.append(status)

    if "delete" in statuses:
        return "delete"
    if "started" in statuses:
        return "started"
    if "finished" in statuses:
        return "finished"
    return ""

def _source_action_query(row: dict, tmdb_id: str, media_title: str, original_title: str,
                         year: str, mode: str, season: str, episode: str,
                         episode_title: str, season_name: str, play_title: str = "") -> dict:
    row = row or {}
    return {
        "action": "tmdb.movie.source.play",
        "provider": row.get("provider") or "",
        "tmdb_id": tmdb_id,
        "title": play_title or row.get("title") or i18n.T(30728, "Source"),
        "media_title": media_title,
        "url": row.get("url") or "",
        "direct": row.get("direct") or "",
        "id": row.get("id") or "",
        "fileHash": row.get("fileHash") or "",
        "original_title": original_title,
        "year": year,
        "mode": mode,
        "season": season,
        "episode": episode,
        "episode_title": episode_title,
        "season_name": season_name,
    }

def _provider_display_name(provider_name: str) -> str:
    names = {
        "webshare": "WebShare",
        "sktonline": "SkTonLine",
        "prehrajto": "Přehraj.to",
        "hellspy": "Hellspy",
    }
    return names.get((provider_name or "").strip().lower(), provider_name or "Provider")

def _source_count_word(count: int) -> str:
    try:
        count = int(count or 0)
    except Exception:
        count = 0
    if count == 1:
        return i18n.T(30749, "source")
    if count in (2, 3, 4):
        return i18n.T(30750, "sources")
    return i18n.T(30751, "sources")

def _provider_group_enabled(provider_name: str) -> bool:
    return (provider_name or "").strip().lower() in ("prehrajto", "hellspy")

def _provider_group_label(row: dict) -> str:
    row = row or {}
    label = (row.get("title") or "").strip() or i18n.T(30748, "Untitled")
    return _source_label_prefix(row) + label

def _provider_group_key(row: dict) -> str:
    row = row or {}
    provider_name = (row.get("provider") or "").strip().lower()
    title_key = _cache_safe_key_part(row.get("title") or "")
    meta = _source_metadata(row)
    parts = [provider_name, title_key]

    size_bytes = int(meta.get("size_bytes") or 0)
    if size_bytes > 0:
        parts.append(str(size_bytes))
    else:
        for key in ("size", "quality", "audio"):
            value = _cache_safe_key_part(meta.get(key) or "")
            if value:
                parts.append(value)

    return "_".join(part for part in parts if part) or "provider_group"

def _group_provider_rows(rows, provider_name: str) -> list:
    provider_name = (provider_name or "").strip().lower()
    groups = []
    index = {}

    for row in _sort_provider_rows(rows):
        if not isinstance(row, dict):
            continue
        if (row.get("provider") or "").strip().lower() != provider_name:
            continue
        key = _provider_group_key(row)
        if key not in index:
            group = {
                "key": key,
                "label": _provider_group_label(row),
                "rows": [],
            }
            index[key] = group
            groups.append(group)
        index[key]["rows"].append(row)

    return groups

def _provider_group_query(provider_name: str, group_key: str, tmdb_id: str, media_title: str,
                          original_title: str, year: str, mode: str, season: str,
                          episode: str, episode_title: str, season_name: str) -> dict:
    return {
        "action": "tmdb.movie.sources.provider.group",
        "provider": provider_name,
        "tmdb_id": tmdb_id,
        "group": group_key,
        "title": media_title,
        "original_title": original_title,
        "year": year,
        "mode": mode,
        "season": season,
        "episode": episode,
        "episode_title": episode_title,
        "season_name": season_name,
    }

def _add_provider_playable_row(row: dict, tmdb_id: str, media_title: str, original_title: str,
                               year: str, mode: str, season: str, episode: str,
                               episode_title: str, season_name: str, label_prefix: str = "",
                               status_resolver=None):
    row = row or {}
    label = row.get("title") or i18n.T(30748, "Untitled")
    score = row.get("_score", None)
    show_label = label_prefix + _source_label_prefix(row) + label + _source_score_suffix(score)
    lines = _source_detail_lines(row, score)
    src_status = _source_row_status(row, tmdb_id, lookup_title=label, status_resolver=status_resolver)
    show_label = _source_status_color(show_label, src_status)

    _add_playable_result(
        label=show_label,
        action_query=_source_action_query(
            row=row,
            tmdb_id=tmdb_id,
            media_title=media_title,
            original_title=original_title,
            year=year,
            mode=mode,
            season=season,
            episode=episode,
            episode_title=episode_title,
            season_name=season_name,
            play_title=label,
        ),
        plot="\n".join(lines),
        icon="DefaultVideo.png"
    )

def _add_provider_result_rows(rows, provider_name: str, tmdb_id: str, media_title: str,
                              original_title: str, year: str, mode: str, season: str,
                              episode: str, episode_title: str, season_name: str,
                              group_items: bool = False, status_resolver=None):
    provider_name = (provider_name or "").strip().lower()
    if not _provider_group_enabled(provider_name):
        for row in _sort_provider_rows(rows):
            _add_provider_playable_row(
                row=row,
                tmdb_id=tmdb_id,
                media_title=media_title,
                original_title=original_title,
                year=year,
                mode=mode,
                season=season,
                episode=episode,
                episode_title=episode_title,
                season_name=season_name,
                status_resolver=status_resolver,
            )
        return

    for group in _group_provider_rows(rows, provider_name):
        group_rows = group.get("rows") or []
        if not group_rows:
            continue

        if len(group_rows) > 1 and not group_items:
            provider_label = _provider_display_name(provider_name)
            label = f"{group.get('label') or _provider_group_label(group_rows[0])}  [{len(group_rows)} {_source_count_word(len(group_rows))}]"
            label = _source_status_color(label, _source_rows_status(group_rows, tmdb_id, status_resolver=status_resolver))
            plot = "\n".join([
                i18n.T(31975).format(provider=provider_label, count=len(group_rows)),
                "",
                i18n.T(31977),
            ] + _source_detail_lines(group_rows[0], group_rows[0].get("_score", None)))
            _add_result_dir(
                label=label,
                query=_provider_group_query(
                    provider_name=provider_name,
                    group_key=group.get("key") or "",
                    tmdb_id=tmdb_id,
                    media_title=media_title,
                    original_title=original_title,
                    year=year,
                    mode=mode,
                    season=season,
                    episode=episode,
                    episode_title=episode_title,
                    season_name=season_name,
                ),
                plot=plot,
                icon="DefaultFolder.png"
            )
            continue

        for idx, row in enumerate(_sort_provider_rows(group_rows), 1):
            _add_provider_playable_row(
                row=row,
                tmdb_id=tmdb_id,
                media_title=media_title,
                original_title=original_title,
                year=year,
                mode=mode,
                season=season,
                episode=episode,
                episode_title=episode_title,
                season_name=season_name,
                label_prefix=f"{idx}. " if group_items and len(group_rows) > 1 else "",
                status_resolver=status_resolver,
            )

def _add_webshare_playable_row(row: dict, tmdb_id: str, media_title: str, original_title: str,
                               year: str, mode: str, season: str, episode: str,
                               episode_title: str, season_name: str, label_prefix: str = "",
                               status_resolver=None):
    label = _webshare_display_label(
        row,
        media_title=media_title,
        year=year,
        mode=mode,
        season=season,
        episode=episode,
        episode_title=episode_title,
    )
    if label_prefix:
        label = f"{label_prefix}{label}"

    score = row.get("_score", None)
    lines = _source_detail_lines(row, score)
    source_name = _webshare_source_name(row)
    status = _source_row_status(row, tmdb_id, lookup_title=source_name, status_resolver=status_resolver)
    show_label = _source_status_color(label, status)

    _add_playable_result(
        label=show_label,
        action_query=_source_action_query(
            row=row,
            tmdb_id=tmdb_id,
            media_title=media_title,
            original_title=original_title,
            year=year,
            mode=mode,
            season=season,
            episode=episode,
            episode_title=episode_title,
            season_name=season_name,
            play_title=source_name,
        ),
        plot="\n".join(lines),
        icon="DefaultVideo.png"
    )

def _add_webshare_original_playable_row(row: dict, tmdb_id: str, media_title: str, original_title: str,
                                        year: str, mode: str, season: str, episode: str,
                                        episode_title: str, season_name: str, label_prefix: str = "",
                                        status_resolver=None):
    source_name = _webshare_source_name(row)
    label = f"{label_prefix}{source_name}" if label_prefix else source_name
    score = row.get("_score", None)
    lines = [
        i18n.T(31956),
        i18n.T(31957),
        "",
    ] + _source_detail_lines(row, score)
    status = _source_row_status(row, tmdb_id, lookup_title=source_name, status_resolver=status_resolver)
    show_label = _source_status_color(label, status)

    _add_playable_result(
        label=show_label,
        action_query=_source_action_query(
            row=row,
            tmdb_id=tmdb_id,
            media_title=media_title,
            original_title=original_title,
            year=year,
            mode=mode,
            season=season,
            episode=episode,
            episode_title=episode_title,
            season_name=season_name,
            play_title=source_name,
        ),
        plot="\n".join(lines),
        icon="DefaultVideo.png"
    )

def _webshare_group_query(group_key: str, tmdb_id: str, media_title: str, original_title: str,
                          year: str, mode: str, season: str, episode: str,
                          episode_title: str, season_name: str) -> dict:
    return {
        "action": "tmdb.movie.sources.webshare.group",
        "provider": "webshare",
        "tmdb_id": tmdb_id,
        "group": group_key,
        "title": media_title,
        "original_title": original_title,
        "year": year,
        "mode": mode,
        "season": season,
        "episode": episode,
        "episode_title": episode_title,
        "season_name": season_name,
    }

def _add_webshare_result_rows(rows, tmdb_id: str, media_title: str, original_title: str,
                              year: str, mode: str, season: str, episode: str,
                              episode_title: str, season_name: str, group_items: bool = False,
                              status_resolver=None):
    episode_title_only_rows = []
    if (mode or "").strip() == "serial_episode" and not group_items:
        rows, episode_title_only_rows = _split_webshare_episode_title_only_rows(
            rows,
            media_title=media_title,
            original_title=original_title,
            season=season,
            episode=episode,
            episode_title=episode_title,
            season_name=season_name,
        )

    groups = _group_webshare_rows(
        rows,
        media_title=media_title,
        year=year,
        mode=mode,
        season=season,
        episode=episode,
        episode_title=episode_title,
    )

    for group in groups:
        group_rows = group.get("rows") or []
        if not group_rows:
            continue

        if len(group_rows) > 1 and not group_items:
            count_word = _source_count_word(len(group_rows))
            label = f"{group.get('label') or _webshare_display_label(group_rows[0], media_title=media_title, year=year, mode=mode, season=season, episode=episode, episode_title=episode_title)}  [{len(group_rows)} {count_word}]"
            label = _source_status_color(label, _source_rows_status(group_rows, tmdb_id, status_resolver=status_resolver))
            plot = "\n".join([
                i18n.T(31976).format(count=len(group_rows)),
                "",
                i18n.T(31978),
            ] + _source_detail_lines(group_rows[0], group_rows[0].get("_score", None)))
            _add_result_dir(
                label=label,
                query=_webshare_group_query(
                    group_key=group.get("key") or "",
                    tmdb_id=tmdb_id,
                    media_title=media_title,
                    original_title=original_title,
                    year=year,
                    mode=mode,
                    season=season,
                    episode=episode,
                    episode_title=episode_title,
                    season_name=season_name,
                ),
                plot=plot,
                icon="DefaultFolder.png"
            )
            continue

        if group_items and len(group_rows) > 1:
            for idx, row in enumerate(group_rows, 1):
                _add_webshare_playable_row(
                    row=row,
                    tmdb_id=tmdb_id,
                    media_title=media_title,
                    original_title=original_title,
                    year=year,
                    mode=mode,
                    season=season,
                    episode=episode,
                    episode_title=episode_title,
                    season_name=season_name,
                    label_prefix=f"{idx}. ",
                    status_resolver=status_resolver,
                )
        else:
            _add_webshare_playable_row(
                row=group_rows[0],
                tmdb_id=tmdb_id,
                media_title=media_title,
                original_title=original_title,
                year=year,
                mode=mode,
                season=season,
                episode=episode,
                episode_title=episode_title,
                season_name=season_name,
                status_resolver=status_resolver,
            )

    if episode_title_only_rows:
        count = len(episode_title_only_rows)
        count_word = _source_count_word(count)
        label = i18n.T(30757, "Possible matches only by episode title [{count} {count_word}]").format(
            count=count,
            count_word=count_word
        )
        _add_result_dir(
            label=label,
            query=_webshare_group_query(
                group_key=_WEBSHARE_EPISODE_TITLE_ONLY_GROUP,
                tmdb_id=tmdb_id,
                media_title=media_title,
                original_title=original_title,
                year=year,
                mode=mode,
                season=season,
                episode=episode,
                episode_title=episode_title,
                season_name=season_name,
            ),
            plot=i18n.T(
                30758,
                "Sources contain the episode title, or possibly the correct numbering, but not a strong series title match. They are not deleted, only hidden below."
            ),
            icon="DefaultFolder.png"
        )

def source_webshare_group_menu():
    action_started_at = __import__("time").time()
    tmdb_id = _get_param("tmdb_id", "").strip()
    group_key = _get_param("group", "").strip()
    title = _get_param("title", "").strip()
    original_title = _get_param("original_title", "").strip()
    year = _get_param("year", "").strip()
    mode = _get_param("mode", "").strip()
    season = _get_param("season", "").strip()
    episode = _get_param("episode", "").strip()
    episode_title = _get_param("episode_title", "").strip()
    season_name = _get_param("season_name", "").strip()

    def _log_total_group_time(result_count: int = 0, stage: str = "listed"):
        try:
            elapsed = __import__("time").time() - action_started_at
            xbmc.log(
                (
                    f"[IKARUS][SRC TOTAL] provider=webshare-group stage={stage} "
                    f"results={int(result_count or 0)} sec={elapsed:.2f} "
                    f"title={title} year={year} mode={mode} season={season} episode={episode}"
                ),
                xbmc.LOGWARNING
            )
        except Exception:
            pass

    xbmcplugin.setPluginCategory(addon_handle, i18n.T(30729, "{provider} sources").format(provider="WebShare"))
    xbmcplugin.setContent(addon_handle, "files")
    source_status_resolver, _provider_status_resolver = _make_source_status_resolvers(
        tmdb_id,
        season=season if mode == "serial_episode" else None,
        episode=episode if mode == "serial_episode" else None,
    )

    rows = _source_cache_get(
        provider="webshare",
        title=title,
        original_title=original_title,
        year=year,
        mode=mode,
        season=season,
        episode=episode
    )
    if rows is None:
        rows = _run_provider_multi(
            provider_name="webshare",
            title=title,
            original_title=original_title,
            year=year,
            mode=mode,
            season=season,
            episode=episode,
            episode_title=episode_title,
            season_name=season_name
        )

    selected = []
    original_labels = False
    if group_key == _WEBSHARE_EPISODE_TITLE_ONLY_GROUP:
        _main_rows, selected = _split_webshare_episode_title_only_rows(
            rows,
            media_title=title,
            original_title=original_title,
            season=season,
            episode=episode,
            episode_title=episode_title,
            season_name=season_name,
        )
        original_labels = True
    else:
        for group in _group_webshare_rows(
                rows,
                media_title=title,
                year=year,
                mode=mode,
                season=season,
                episode=episode,
                episode_title=episode_title):
            if (group.get("key") or "") == group_key:
                selected = group.get("rows") or []
                break

    if not selected:
        _add_file(i18n.T(30730, "Source group is no longer available. Try refreshing sources."), icon="DefaultAddonProgram.png")
        _log_total_group_time(result_count=0, stage="empty")
        _end("files")
        return

    if original_labels:
        for idx, row in enumerate(_sort_webshare_rows(selected), 1):
            _add_webshare_original_playable_row(
                row=row,
                tmdb_id=tmdb_id,
                media_title=title,
                original_title=original_title,
                year=year,
                mode=mode,
                season=season,
                episode=episode,
                episode_title=episode_title,
                season_name=season_name,
                label_prefix=f"{idx}. ",
                status_resolver=source_status_resolver,
            )
        _log_total_group_time(result_count=len(selected or []), stage="listed")
        _end("files")
        return

    _add_webshare_result_rows(
        rows=selected,
        tmdb_id=tmdb_id,
        media_title=title,
        original_title=original_title,
        year=year,
        mode=mode,
        season=season,
        episode=episode,
        episode_title=episode_title,
        season_name=season_name,
        group_items=True,
        status_resolver=source_status_resolver
    )
    _log_total_group_time(result_count=len(selected or []), stage="listed")
    _end("files")

def source_provider_group_menu():
    action_started_at = __import__("time").time()
    provider = _get_param("provider", "").strip().lower()
    group_key = _get_param("group", "").strip()
    tmdb_id = _get_param("tmdb_id", "").strip()
    title = _get_param("title", "").strip()
    original_title = _get_param("original_title", "").strip()
    year = _get_param("year", "").strip()
    mode = _get_param("mode", "").strip()
    season = _get_param("season", "").strip()
    episode = _get_param("episode", "").strip()
    episode_title = _get_param("episode_title", "").strip()
    season_name = _get_param("season_name", "").strip()

    def _log_total_group_time(result_count: int = 0, stage: str = "listed"):
        try:
            elapsed = __import__("time").time() - action_started_at
            xbmc.log(
                (
                    f"[IKARUS][SRC TOTAL] provider={provider}-group stage={stage} "
                    f"results={int(result_count or 0)} sec={elapsed:.2f} "
                    f"title={title} year={year} mode={mode} season={season} episode={episode}"
                ),
                xbmc.LOGWARNING
            )
        except Exception:
            pass

    xbmcplugin.setPluginCategory(addon_handle, i18n.T(30729, "{provider} sources").format(provider=_provider_display_name(provider)))
    xbmcplugin.setContent(addon_handle, "files")
    source_status_resolver, _provider_status_resolver = _make_source_status_resolvers(
        tmdb_id,
        season=season if mode == "serial_episode" else None,
        episode=episode if mode == "serial_episode" else None,
    )

    rows = _source_cache_get(
        provider=provider,
        title=title,
        original_title=original_title,
        year=year,
        mode=mode,
        season=season,
        episode=episode
    )
    if rows is None:
        rows = _run_provider_multi(
            provider_name=provider,
            title=title,
            original_title=original_title,
            year=year,
            mode=mode,
            season=season,
            episode=episode,
            episode_title=episode_title,
            season_name=season_name
        )

    selected = []
    for group in _group_provider_rows(rows, provider):
        if (group.get("key") or "") == group_key:
            selected = group.get("rows") or []
            break

    if not selected:
        _add_file(i18n.T(30730, "Source group is no longer available. Try refreshing sources."), icon="DefaultAddonProgram.png")
        _log_total_group_time(result_count=0, stage="empty")
        _end("files")
        return

    _add_provider_result_rows(
        rows=selected,
        provider_name=provider,
        tmdb_id=tmdb_id,
        media_title=title,
        original_title=original_title,
        year=year,
        mode=mode,
        season=season,
        episode=episode,
        episode_title=episode_title,
        season_name=season_name,
        group_items=True,
        status_resolver=source_status_resolver,
    )
    _log_total_group_time(result_count=len(selected or []), stage="listed")
    _end("files")

def source_play():
    provider = _get_param("provider", "").strip().lower()
    title = _get_param("title", i18n.T(30728, "Source")).strip() or i18n.T(30728, "Source")
    url = _get_param("url", "").strip()
    tmdb_id = _get_param("tmdb_id", "").strip() or _get_param("id", "").strip()
    media_title = _get_param("media_title", "").strip()
    original_title = _get_param("original_title", "").strip()
    year = _get_param("year", "").strip()
    auto_next = _get_param("auto_next", "").strip() == "1"
    source_index = _get_param("source_index", "").strip()
    auto_tried = _auto_next_attempts_parse(_get_param("auto_tried", ""))
    auto_trace = _get_param("auto_trace", "").strip()

    if not provider:
        xbmcgui.Dialog().ok(i18n.T(30731, "Sources"), i18n.T(30732, "Missing provider."))
        playback_utils.fail_source_playback(
            addon_handle,
            provider="",
            reason="missing provider",
            title=title,
            url=url,
            tmdb_id=tmdb_id,
            auto_next=auto_next,
        )
        return

    def _try_auto_next_fallback(reason: str = "") -> bool:
        if not auto_next:
            return False

        mode = (_get_param("mode", "") or "").strip()
        season = (_get_param("season", "") or "").strip()
        episode = (_get_param("episode", "") or "").strip()
        episode_title = (_get_param("episode_title", "") or "").strip()
        season_name = (_get_param("season_name", "") or "").strip()
        if mode != "serial_episode" or not season or not episode:
            return False

        tried = list(auto_tried)
        current_key = _auto_next_attempt_key(provider, source_index)
        if current_key and current_key not in tried:
            tried.append(current_key)
        if len(tried) >= _AUTO_NEXT_MAX_SOURCE_ATTEMPTS:
            xbmc.log(
                f"[IKARUS][AUTO NEXT] trace={auto_trace or '-'} fallback limit reached "
                f"attempts={len(tried)} reason={reason}",
                xbmc.LOGWARNING,
            )
            return False

        try:
            try:
                require_cz_dabing = (addon.getSetting("tmdb_series_next_require_cz") or "").strip().lower() == "true"
            except Exception:
                require_cz_dabing = True

            row = find_next_tmdb_episode_source(
                title=media_title or title,
                original_title=original_title,
                year=year,
                season=season,
                episode=episode,
                episode_title=episode_title,
                current_provider=provider,
                current_index=source_index,
                season_name=season_name,
                require_cz_dabing=require_cz_dabing,
                attempted_keys=tried,
            )
            if not row:
                return False
            next_url = build_tmdb_episode_source_play_url(
                row=row,
                tmdb_id=tmdb_id,
                title=media_title or title,
                original_title=original_title,
                year=year,
                season=season,
                episode=episode,
                episode_title=episode_title,
                season_name=season_name,
                auto_next=True,
                auto_tried=_auto_next_attempts_encode(tried),
                auto_trace=auto_trace,
            )
            if not next_url:
                return False
            try:
                xbmc.log(
                    f"[IKARUS][AUTO NEXT] trace={auto_trace or '-'} fallback "
                    f"attempt={len(tried) + 1}/{_AUTO_NEXT_MAX_SOURCE_ATTEMPTS} "
                    f"after provider={provider} index={source_index} reason={reason}",
                    xbmc.LOGWARNING,
                )
            except Exception:
                pass
            xbmc.executebuiltin(f'RunPlugin("{next_url}")')
            return True
        except Exception as e:
            try:
                xbmc.log(f"[IKARUS][AUTO NEXT] fallback failed: {e}", xbmc.LOGWARNING)
            except Exception:
                pass
            return False

    def _source_play_failed(reason: str, dialog_title: str = "", dialog_message: str = "",
                            source_id: str = "", file_hash: str = "") -> bool:
        playback_utils.log_source_failure(
            provider=provider,
            reason=reason,
            title=title,
            url=url,
            tmdb_id=tmdb_id,
            source_id=source_id or file_hash,
            auto_next=auto_next,
        )
        if _try_auto_next_fallback(reason):
            return True
        if dialog_title and dialog_message:
            xbmcgui.Dialog().ok(dialog_title, dialog_message)
        _fail_playback()
        return True

    def _tmdb_meta():
        meta = {
            "title": media_title or title,
            "original_title": original_title,
            "year": year,
        }
        if not tmdb_id:
            return meta

        try:
            from resources.lib import TMDB_knihovna as tmdb_lib
            mode = (_get_param("mode", "") or "").strip()
            if mode == "serial_episode":
                fetched = tmdb_lib._tmdb_playback_tv_meta(tmdb_id)
            else:
                fetched = tmdb_lib._tmdb_playback_movie_meta(tmdb_id)
            if fetched:
                meta.update(fetched)
        except Exception:
            pass
        return meta

    def _play_tracked(play_target: str, headers: dict = None, source_id: str = "", file_hash: str = "", direct_url: str = ""):
        if not tmdb_id:
            play_url(title, play_target, headers=headers)
            return True

        from resources.lib import tmdb_playback_state

        meta = _tmdb_meta()
        mode = (_get_param("mode", "") or "").strip()
        season = (_get_param("season", "") or "").strip()
        episode = (_get_param("episode", "") or "").strip()
        episode_title = (_get_param("episode_title", "") or "").strip()
        season_name = (_get_param("season_name", "") or "").strip()
        display_title = _build_playback_display_title(
            meta,
            fallback_title=media_title or title,
            mode=mode,
            season=season,
            episode=episode,
            episode_title=episode_title,
        )
        effective_play_target = _with_playback_display_title(
            play_target,
            display_title,
            subtitle_title=title,
            subtitle_query=_subtitle_search_query(title, media_title, original_title),
            year=year,
        )
        source_meta = {
            "provider": provider,
            "source_id": source_id,
            "file_hash": file_hash,
            "direct": direct_url,
            "url": url,
            "play_url": effective_play_target,
            "title": title,
            "media_title": media_title,
            "original_title": original_title,
            "year": year,
            "display_title": display_title,
            "mode": mode,
            "season": season,
            "episode": episode,
            "episode_title": episode_title,
            "season_name": season_name,
            "auto_next": "1" if auto_next else "",
        }
        source_key = tmdb_playback_state.make_source_key(
            provider=provider,
            url=url,
            direct=direct_url,
            source_id=source_id,
            file_hash=file_hash,
        )
        resume_sec = tmdb_playback_state.ask_resume(tmdb_id, source_key)
        if resume_sec == "__cancel__":
            return False
        return tmdb_playback_state.play_and_record_progress(
            tmdb_id=tmdb_id,
            source_key=source_key,
            play_url=effective_play_target,
            headers=headers,
            meta=meta,
            resume_sec=resume_sec,
            source_meta=source_meta,
        )

    if provider == "prehrajto":
        if _is_direct_media_url(url):
            if _play_tracked(url):
                return
            _source_play_failed("prehrajto direct did not start")
            return

        resolved, headers = _prehrajto_resolve_direct_url(url)
        if resolved:
            if _play_tracked(resolved, headers=headers, direct_url=resolved):
                return
            _source_play_failed("prehrajto resolved did not start")
            return

        _source_play_failed(
            "prehrajto resolve failed",
            "Přehraj.to",
            i18n.T(31933).format(url=url),
        )
        return

    if provider == "sktonline":
        if _is_direct_media_url(url):
            if _play_tracked(url):
                return
            _source_play_failed("sktonline direct did not start")
            return

        resolved, headers = _sktonline_resolve_direct_url(url, interactive=not auto_next)
        if resolved:
            if _play_tracked(resolved, headers=headers, direct_url=resolved):
                return
            _source_play_failed("sktonline resolved did not start")
            return

        _source_play_failed(
            "sktonline resolve failed",
            "SkTonLine",
            i18n.T(31934).format(url=url),
        )
        return

    if provider == "hellspy":
        direct = _get_param("direct", "").strip()
        video_id = _get_param("id", "").strip()
        video_hash = _get_param("fileHash", "").strip()
        resolved, headers = _hellspy_resolve_direct_url(
            url,
            direct,
            video_id,
            video_hash,
            prefer_fresh=auto_next,
        )
        if resolved:
            ok, reason = _hellspy_stream_is_available(resolved, headers)
            if not ok:
                _source_play_failed(
                    "hellspy stream probe failed: " + reason,
                    source_id=video_id,
                    file_hash=video_hash,
                )
                return

            if _play_tracked(
                resolved,
                headers=headers,
                source_id=video_id,
                file_hash=video_hash,
                direct_url=resolved,
            ):
                return
            _source_play_failed("hellspy resolved did not start", source_id=video_id, file_hash=video_hash)
            return

        _source_play_failed(
            "hellspy resolve failed",
            "Hellspy",
            i18n.T(31931) + "\n\n" + i18n.T(31932).format(url=url),
            source_id=video_id,
            file_hash=video_hash,
        )
        return

    if provider == "webshare":
        video_id = _get_param("id", "").strip()

        if not video_id:
            _source_play_failed(
                "webshare missing id",
                "WebShare",
                i18n.T(30733, "Missing file identifier."),
            )
            return

        try:
            old_play_url = (
                "plugin://plugin.video.ikarus/"
                "?action=film.play&id=" + urllib.parse.quote_plus(video_id) +
                "&ident=" + urllib.parse.quote_plus(video_id) +
                ("&silent=1&auto_next=1" if auto_next else "")
            )

            xbmc.log(f"[IKARUS][WS] playback via film.py ident={video_id}", xbmc.LOGWARNING)

            if _play_tracked(old_play_url, source_id=video_id):
                return
            _source_play_failed("webshare did not start", source_id=video_id)
            return

        except Exception as e:
            _source_play_failed(
                "webshare exception",
                "WebShare",
                i18n.T(31936).format(video_id=video_id, error=e),
                source_id=video_id,
            )
            return
        
    xbmcgui.Dialog().ok(i18n.T(30731, "Sources"), i18n.T(30734, "Unknown provider: {provider}").format(provider=provider))
    _source_play_failed("unknown provider")

def _prehrajto_resolve_direct_url(page_url: str):
    try:
        detail_html = _http_get(page_url, referer=_PREHRAJTO_BASE)
    except Exception:
        return "", {}

    sources = _prehrajto_extract_sources_from_html(detail_html, page_url)
    if not sources:
        direct = _try_extract_sources_from_html(detail_html)
        if direct:
            if direct.startswith("/"):
                direct = urllib.parse.urljoin(_PREHRAJTO_BASE, direct)
                sources = [{"label": _prehrajto_guess_label_from_url(direct), "src": direct.strip()}]

    for item in sources:
        src = (item.get("src") or "").strip()
        if not src:
            continue

        final = _http_follow_to_final_url(src, referer=page_url)
        candidate = (final or src).strip()
        if not candidate:
            continue

        if _is_direct_media_url(candidate):
            headers = _prehrajto_build_headers(page_url, candidate)
            try:
                xbmc.log(
                    f"[IKARUS][PREHRAJTO] selected {item.get('label') or i18n.T(30728, 'Source')} final={candidate}",
                    xbmc.LOGWARNING,
                )
            except Exception:
                pass
            return candidate, headers

    return "", {}

def _sktonline_resolve_direct_url(page_url: str, interactive: bool = True):
    detail_html = ""
    embed_html = ""
    embed_url = ""

    try:
        detail_html = _http_get(page_url, referer=_SKTONLINE_BASE)
    except Exception:
        return "", {}

    embed_url = _sktonline_find_embed_url_anywhere(detail_html, page_url)
    if embed_url:
        try:
            embed_html = _http_get(embed_url, referer=page_url)
        except Exception:
            embed_html = ""

    sources_detail = _sktonline_extract_video_sources(detail_html, page_url)
    sources_embed = _sktonline_extract_video_sources(embed_html, embed_url) if embed_html and embed_url else []
    sources_script_detail = _sktonline_extract_sources_from_scripts(detail_html, page_url)
    sources_script_embed = _sktonline_extract_sources_from_scripts(embed_html, embed_url) if embed_html and embed_url else []

    sources = _sktonline_merge_sources(sources_detail, sources_embed, sources_script_detail, sources_script_embed)

    if not sources:
        dl = []
        dl += _sktonline_extract_download_links(detail_html, page_url)
        if embed_html and embed_url:
            dl += _sktonline_extract_download_links(embed_html, embed_url)

        for it in dl:
            durl = it.get("url")
            if not durl:
                continue
            final = _http_follow_to_final_url(durl, referer=embed_url or page_url)
            if final and _is_direct_media_url(final):
                sources.append({"label": it.get("label") or _sktonline_guess_label_from_url(final), "src": final})

    if not sources:
        return "", {}

    picked = sources[0]
    if len(sources) > 1:
        if interactive:
            labels = [(s.get("label") or i18n.T(30728, "Source")).strip() for s in sources]
            idx = xbmcgui.Dialog().select(i18n.T(30727, "Select quality"), labels)
            if idx is None or idx < 0:
                return "", {}
            picked = sources[idx]
        else:
            def _quality_rank(item):
                text = f"{item.get('label') or ''} {item.get('src') or ''}".lower()
                for rank, marker in enumerate(("4320", "2160", "1440", "1080", "720", "576", "480", "360")):
                    if marker in text:
                        return 100 - rank
                return 0

            picked = max(sources, key=_quality_rank)
            xbmc.log(
                f"[IKARUS][SKT] auto selected quality={picked.get('label') or 'unknown'}",
                xbmc.LOGINFO,
            )

    stream_url = (picked.get("src") or "").strip()
    if not stream_url:
        return "", {}

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Referer": _sanitize_url(page_url),
        "Origin": "https://online.sktorrent.eu",
        "Accept": "*/*",
    }

    ck = _cookie_header_for_url(stream_url)
    if ck:
        headers["Cookie"] = ck

    return stream_url, headers

def _hellspy_resolve_direct_url(url: str, direct: str, video_id: str, video_hash: str,
                                prefer_fresh: bool = False):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.hellspy.to/",
        "Origin": "https://www.hellspy.to",
        "Accept": "*/*",
    }

    if prefer_fresh and video_id and video_hash:
        resolved = _hellspy_get_stream(video_id, video_hash)
        if resolved:
            ck = _cookie_header_for_url_basic(resolved)
            if ck:
                headers["Cookie"] = ck
            return resolved, headers

    if direct:
        ck = _cookie_header_for_url_basic(direct)
        if ck:
            headers["Cookie"] = ck
        return direct, headers

    if url and _is_direct_media_url(url):
        ck = _cookie_header_for_url_basic(url)
        if ck:
            headers["Cookie"] = ck
        return url, headers

    if video_id and video_hash:
        resolved = _hellspy_get_stream(video_id, video_hash)
        if resolved:
            ck = _cookie_header_for_url_basic(resolved)
            if ck:
                headers["Cookie"] = ck
            return resolved, headers

    return "", {}

def _resolve_download_target(provider: str, title: str, url: str, direct: str = "", video_id: str = "", video_hash: str = ""):
    provider = (provider or "").strip().lower()

    if provider == "prehrajto":
        if _is_direct_media_url(url):
            return url, {}
        return _prehrajto_resolve_direct_url(url)

    if provider == "sktonline":
        if _is_direct_media_url(url):
            return url, {}
        return _sktonline_resolve_direct_url(url)

    if provider == "hellspy":
        return _hellspy_resolve_direct_url(url, direct, video_id, video_hash)

    if provider == "webshare":
        return _ws_resolve_direct_url(video_id)

    return "", {}

def source_download():
    provider = _get_param("provider", "").strip().lower()
    title = _get_param("title", i18n.T(30728, "Source")).strip() or i18n.T(30728, "Source")
    url = _get_param("url", "").strip()
    direct = _get_param("direct", "").strip()
    video_id = _get_param("id", "").strip()
    video_hash = _get_param("fileHash", "").strip()

    if not _get_setting_bool("download_enable", True):
        xbmcgui.Dialog().ok(i18n.T(30700, "Downloads"), i18n.T(30701, "Downloads are disabled in settings."))
        return

    download_dir = _get_setting_text("download_path", "")
    if not download_dir:
        xbmcgui.Dialog().ok(i18n.T(30700, "Downloads"), i18n.T(30702, "Download target folder is not set."))
        return

    if not _ensure_dir(download_dir):
        xbmcgui.Dialog().ok(
            i18n.T(30700, "Downloads"),
            i18n.T(30703, "Could not create / open target folder:\n{path}").format(path=download_dir),
        )
        return

    resolved_url = ""
    headers = {}

    if provider == "webshare":
        if not video_id:
            xbmcgui.Dialog().ok(
                i18n.T(30700, "Downloads"),
                i18n.T(30704, "Missing WebShare file identifier.")
            )
            return

        ext = ".mp4"
    else:
        resolved_url, headers = _resolve_download_target(
            provider=provider,
            title=title,
            url=url,
            direct=direct,
            video_id=video_id,
            video_hash=video_hash
        )

        if not resolved_url:
            xbmcgui.Dialog().ok(
                i18n.T(30700, "Downloads"),
                i18n.T(
                    30705,
                    "Could not get a direct download link.\n\nProvider: {provider}\nID: {id}\nURL: {url}",
                ).format(provider=provider, id=video_id, url=url)
            )
            return

        ext = _guess_ext_from_url(resolved_url)

    file_name = _safe_filename(title) + ext

    if _get_setting_bool("download_ask_name", True):
        custom = xbmcgui.Dialog().input(i18n.T(30706, "File name"), defaultt=file_name, type=xbmcgui.INPUT_ALPHANUM)
        if not custom:
            return
        custom = _safe_filename(custom)
        if "." not in custom:
            custom += ext
        file_name = custom

    dst_path = download_dir.rstrip("/\\") + "/" + file_name

    if xbmcvfs.exists(dst_path) and not _get_setting_bool("download_overwrite", False):
        yes = xbmcgui.Dialog().yesno(
            i18n.T(30700, "Downloads"),
            i18n.T(30707, "File already exists:\n{file}\n\nOverwrite it?").format(file=file_name)
        )
        if not yes:
            return

    from resources.lib import download_manager

    job = download_manager.add_job(
        title=file_name,
        provider=provider,
        resolved_url=resolved_url,
        headers=headers,
        dst_path=dst_path,
        source_id=video_id if provider == "webshare" else "",
    )

    if not job:
        xbmcgui.Dialog().notification(
            i18n.T(30700, "Downloads"),
            i18n.T(30767, "The download queue could not be saved."),
            xbmcgui.NOTIFICATION_ERROR,
            4000,
            sound=False,
        )
        return

    xbmcgui.Dialog().notification(
        i18n.T(30700, "Downloads"),
        i18n.T(30708, "Added to queue: {file}").format(file=file_name),
        xbmcgui.NOTIFICATION_INFO,
        3000,
        sound=False
    )


def downloads_queue_clear_done():
    from resources.lib import download_manager

    removed = download_manager.remove_done_jobs()

    xbmcgui.Dialog().notification(
        i18n.T(30700, "Downloads"),
        i18n.T(30709, "Removed finished items: {count}").format(count=removed),
        xbmcgui.NOTIFICATION_INFO,
        3000,
        sound=False
    )

    _refresh_current_container()
    _finish_command_action()
    return


def downloads_queue_clear_all():
    from resources.lib import download_manager

    removed = download_manager.remove_all_jobs()

    msg = i18n.T(30710, "Queue was cleared: {count} items.").format(count=removed)
    if removed:
        msg += " " + i18n.T(30711, "Running download was stopped.")

    xbmcgui.Dialog().notification(
        i18n.T(30700, "Downloads"),
        msg,
        xbmcgui.NOTIFICATION_INFO,
        3000,
        sound=False
    )

    _refresh_current_container()
    _finish_command_action()
    return


def downloads_queue_menu():
    from resources.lib import download_manager

    xbmcplugin.setPluginCategory(addon_handle, i18n.T(30712, "Downloads / Queue"))
    xbmcplugin.setContent(addon_handle, "files")

    _add_command_item(
        i18n.T(30713, "Clear finished"),
        {"action": "tmdb.downloads.queue.clear_done"},
        "DefaultAddonProgram.png"
    )

    _add_command_item(
        i18n.T(30714, "Clear all"),
        {"action": "tmdb.downloads.queue.clear_all"},
        "DefaultAddonProgram.png"
    )

    counts = download_manager.get_job_counts()
    current = download_manager.get_current_job()
    jobs = download_manager.get_jobs()
    current_id = (current or {}).get("id") or ""

    if current:
        title = current.get("title") or i18n.T(30728, "Source")
        progress = int(current.get("progress") or 0)
        _add_file(i18n.T(30715, "[B]Currently downloading:[/B] {title}  [{progress}%]").format(title=title, progress=progress), icon="DefaultAddonProgram.png")
    else:
        _add_file(i18n.T(30716, "[B]Currently downloading:[/B] nothing"), icon="DefaultAddonProgram.png")

    _add_file(
        i18n.T(30717, "Queued: {queued} | Downloading: {downloading} | Done: {done} | Error: {error}").format(
            queued=counts.get("queued", 0),
            downloading=counts.get("downloading", 0),
            done=counts.get("done", 0),
            error=counts.get("error", 0),
        ),
        icon="DefaultAddonProgram.png"
    )

    if not jobs:
        _add_file(i18n.T(30718, "Queue is empty."), icon="DefaultAddonProgram.png")
        _end("files")
        return

    for job in jobs:
        st = (job.get("status") or "").strip().lower()
        if current_id and (job.get("id") or "") == current_id and st == "downloading":
            continue
        title = job.get("title") or i18n.T(30728, "Source")
        progress = int(job.get("progress") or 0)

        if st == "downloading":
            label = i18n.T(30719, "[DOWNLOADING] {title} [{progress}%]").format(title=title, progress=progress)
        elif st == "queued":
            label = i18n.T(30720, "[QUEUED] {title}").format(title=title)
        elif st == "done":
            label = i18n.T(30721, "[DONE] {title}").format(title=title)
        elif st == "error":
            err = (job.get("error") or "").strip()
            label = i18n.T(30722, "[ERROR] {title}").format(title=title)
            if err:
                label += f" - {err}"
        else:
            label = f"[{st.upper() or 'UNKNOWN'}] {title}"

        _add_file(label, icon="DefaultVideo.png")

    _end("files")


def _router_impl():
    _refresh_runtime()
    action = _get_param("action", "")

    if action == "tmdb.noop":
        _finish_command_action()
        return

    if action == "tmdb.movie.sources.search.stub":
        source_search_stub_menu()
        return

    if action == "tmdb.movie.sources.webshare.group":
        source_webshare_group_menu()
        return

    if action == "tmdb.movie.sources.provider.group":
        source_provider_group_menu()
        return

    if action == "tmdb.movie.source.play":
        source_play()
        return

    if action == "tmdb.movie.source.download":
        source_download()
        return

    if action == "tmdb.downloads.queue":
        downloads_queue_menu()
        return

    if action == "tmdb.downloads.queue.clear_done":
        downloads_queue_clear_done()
        return

    if action == "tmdb.downloads.queue.clear_all":
        downloads_queue_clear_all()
        return

    _placeholder_dir(i18n.T(30731, "Sources"), i18n.T(30735, "(unknown action: {action})").format(action=action))


def _source_missing_param(action: str, name: str, playback: bool = False):
    try:
        xbmc.log(f"[IKARUS][SRC][PARAM] missing action={action} param={name}", xbmc.LOGWARNING)
    except Exception:
        pass
    if playback:
        _fail_playback()
        return
    _placeholder_dir(i18n.T(30731, "Sources"), i18n.T(30736, "(missing required parameter: {name})").format(name=name))


def _source_validate_action_params(action: str) -> bool:
    action = (action or "").strip()
    if action in {
        "tmdb.movie.sources.provider.group",
        "tmdb.movie.source.play",
        "tmdb.movie.source.download",
    } and not _get_param("provider", "").strip():
        _source_missing_param(action, "provider", playback=action == "tmdb.movie.source.play")
        return False

    if action == "tmdb.movie.source.play":
        provider = _get_param("provider", "").strip().lower()
        if provider == "webshare" and not _get_param("id", "").strip():
            _source_missing_param(action, "id", playback=True)
            return False

    if action == "tmdb.movie.source.download":
        provider = _get_param("provider", "").strip().lower()
        if provider == "webshare" and not _get_param("id", "").strip():
            _source_missing_param(action, "id")
            return False

    return True


def router():
    _refresh_runtime()
    action = _get_param("action", "") or "sources"
    with diagnostics.action_scope("sources", action):
        if not _source_validate_action_params(action):
            return
        return _router_impl()

if __name__ == "__main__":
    router()
    
