# -*- coding: utf-8 -*-
import sys
import re
import json
import time
import os
import hashlib
import urllib.parse
import urllib.request
import html
import http.cookiejar
import unicodedata
import xbmcgui
import xbmcplugin
import xbmcaddon
import xbmc
import xbmcvfs

from resources.lib import diagnostics, i18n, playback_utils, source_common
from resources.lib import source_labels
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


def _setting_enabled(setting_id: str, default: bool = True) -> bool:
    value = (addon.getSetting(setting_id) or "").strip().lower()
    if not value:
        return default
    return value == "true"


def _webshare_connected() -> bool:
    status = (addon.getSetting("status") or "").strip().lower()
    return status in ("připojeno", "pripojeno", "přihlášen", "prihlasen")

# ---------------------------
# HTTP (urllib + cookies)
# ---------------------------

_COOKIEJAR = http.cookiejar.CookieJar()
_OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_COOKIEJAR))


# ---------------------------
# ROUTING HELPERS
# ---------------------------

def build_url(query: dict) -> str:
    _refresh_runtime()
    return BASE_PATH + "?" + urllib.parse.urlencode(query)


_MANUAL_SEARCH_CACHE_TTL = 20 * 60
_MANUAL_ACTIVE_SEARCH = {}


def _manual_cache_dir() -> str:
    try:
        profile = xbmcvfs.translatePath(addon.getAddonInfo("profile"))
        path = os.path.join(profile, "cache", "manual_search")
        os.makedirs(path, exist_ok=True)
        return path
    except Exception:
        return ""


def _manual_cache_key(provider: str, payload: dict) -> str:
    raw = json.dumps({"provider": provider, "payload": payload or {}}, sort_keys=True, ensure_ascii=False)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _manual_cache_get(provider: str, payload: dict):
    cache_dir = _manual_cache_dir()
    if not cache_dir:
        return None
    path = os.path.join(cache_dir, _manual_cache_key(provider, payload) + ".json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if (time.time() - float(data.get("_ts") or 0)) > _MANUAL_SEARCH_CACHE_TTL:
            return None
        return data.get("data")
    except Exception:
        return None


def _manual_cache_set(provider: str, payload: dict, data):
    cache_dir = _manual_cache_dir()
    if not cache_dir:
        return
    path = os.path.join(cache_dir, _manual_cache_key(provider, payload) + ".json")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"_ts": int(time.time()), "data": data}, f, ensure_ascii=False)
    except Exception as e:
        xbmc.log(f"[IKARUS][MANUAL][CACHE] write failed provider={provider}: {repr(e)}", xbmc.LOGWARNING)


def _container_update(query: dict):
    target = build_url(query)
    try:
        xbmc.executebuiltin('Container.Update("{0}",replace)'.format(target.replace('"', "%22")))
        xbmcplugin.endOfDirectory(addon_handle, succeeded=True, updateListing=False, cacheToDisc=False)
    except Exception as e:
        xbmc.log(f"[IKARUS][MANUAL] Container.Update failed: {repr(e)}", xbmc.LOGWARNING)
        xbmcplugin.endOfDirectory(addon_handle, succeeded=False, updateListing=False, cacheToDisc=False)


def _prompt_and_open_results(provider_label: str, prompt_title: str, result_action: str, extra: dict = None):
    q = xbmcgui.Dialog().input(prompt_title, type=xbmcgui.INPUT_ALPHANUM)
    if not q or not q.strip():
        main_menu()
        return
    query = {"action": result_action, "q": q.strip()}
    if extra:
        query.update(extra)
    _container_update(query)


def _manual_remember(provider: str, q: str, **kwargs):
    provider = (provider or "").strip().lower()
    q = (q or "").strip()
    if not provider or not q:
        return
    payload = {"q": q}
    payload.update(kwargs or {})
    _MANUAL_ACTIVE_SEARCH[provider] = payload


def _manual_active(provider: str):
    return _MANUAL_ACTIVE_SEARCH.get((provider or "").strip().lower()) or {}


def _add_manual_nav(provider_label: str, search_action: str, refresh_query: dict):
    li_new = xbmcgui.ListItem(label=i18n.T(30600, "[New search]"))
    li_new.setArt({"icon": "DefaultAddonsSearch.png", "thumb": "DefaultAddonsSearch.png"})
    xbmcplugin.addDirectoryItem(addon_handle, build_url({"action": search_action, "new": "1"}), li_new, isFolder=True)

    rq = dict(refresh_query or {})
    rq["force"] = "1"
    li_refresh = xbmcgui.ListItem(label=i18n.T(30601, "[Refresh from web]"))
    li_refresh.setArt({"icon": "DefaultAddonProgram.png", "thumb": "DefaultAddonProgram.png"})
    xbmcplugin.addDirectoryItem(addon_handle, build_url(rq), li_refresh, isFolder=True)


def _add_empty_row(label: str):
    li = xbmcgui.ListItem(label=label)
    li.setArt({"icon": "DefaultVideo.png", "thumb": "DefaultVideo.png"})
    xbmcplugin.addDirectoryItem(handle=addon_handle, url=build_url({"action": "other_sources"}), listitem=li, isFolder=False)


def main_menu():
    xbmcplugin.setContent(addon_handle, "files")

    items = [
        (i18n.T(30502, "Prehraj.to"), {"action": "other.prehrajto"}, "DefaultAddonProgram.png"),
        (i18n.T(30503, "Hellspy"), {"action": "other.hellspy"}, "DefaultAddonProgram.png"),
        (i18n.T(30504, "SkTonLine"), {"action": "other.sktonline"}, "DefaultAddonProgram.png"),
        (i18n.T(30505, "WebShare"), {"action": "film.search"}, "DefaultAddonProgram.png"),
    ]

    allowed_actions = set()
    if _setting_enabled("show_prehrajto", default=True):
        allowed_actions.add("other.prehrajto")
    if _setting_enabled("show_hellspy", default=True):
        allowed_actions.add("other.hellspy")
    if _setting_enabled("show_sktonline", default=True):
        allowed_actions.add("other.sktonline")
    if _setting_enabled("show_webshare", default=True) and _webshare_connected():
        allowed_actions.add("film.search")

    items = [
        (label, query, icon_p)
        for (label, query, icon_p) in items
        if (query.get("action") or "") in allowed_actions
    ]

    for label, query, icon_p in items:
        url = build_url(query)
        li = xbmcgui.ListItem(label=label)
        li.setArt({"icon": icon_p, "thumb": icon_p, "poster": icon_p})
        xbmcplugin.addDirectoryItem(handle=addon_handle, url=url, listitem=li, isFolder=True)

    xbmcplugin.endOfDirectory(addon_handle)


# ---------------------------
# COMMON HTTP (urllib, no gzip)
# ---------------------------
MANUAL_SEARCH_TIMEOUT = 8
SKTONLINE_SEARCH_TIMEOUT = 20

_sanitize_url = source_common.sanitize_url

def _http_get(url: str, timeout: int = 25, referer: str = None) -> str:
    return source_common.http_get(url, _OPENER, timeout=timeout, referer=referer)

def _http_post(url: str, data: dict, timeout: int = 25, referer: str = None) -> str:
    return source_common.http_post(url, data, _OPENER, timeout=timeout, referer=referer)

def _http_get_json(url: str, timeout: int = 25, referer: str = None, origin: str = None) -> dict:
    return source_common.http_get_json(
        url,
        _OPENER,
        timeout=timeout,
        referer=referer,
        origin=origin,
        default_referer="https://www.hellspy.to/",
        default_origin="https://www.hellspy.to",
    )

_is_direct_media_url = source_common.is_direct_media_url

def _kodi_url_with_headers(url: str, headers: dict) -> str:
    """
    Kodi headers v URL: https://...|Header=Value&Header2=Value2
    """
    return playback_utils.kodi_url_with_headers(url, headers)

def play_url(title: str, stream_url: str, headers: dict = None):
    _refresh_runtime()
    try:
        stream_url = _sanitize_url(stream_url)
    except Exception:
        pass

    playback_utils.set_resolved_playback(addon_handle, title, stream_url, headers=headers)


def _fail_playback():
    _refresh_runtime()
    playback_utils.fail_playback(addon_handle)

def _manual_play_failed(provider: str, reason: str, title: str = "", url: str = "",
                        dialog_title: str = "", dialog_message: str = ""):
    _refresh_runtime()
    _manual_log(provider, "PLAY_FAIL", xbmc.LOGWARNING, reason=reason, title=title, url=url)
    playback_utils.log_source_failure(
        provider=provider,
        reason=reason,
        title=title,
        url=url,
    )
    if dialog_title and dialog_message:
        xbmcgui.Dialog().ok(dialog_title, dialog_message)
    _fail_playback()


def _manual_missing_param(provider: str, param_name: str, title: str = "", url: str = ""):
    _manual_play_failed(
        provider,
        "missing param " + str(param_name or ""),
        title=title,
        url=url,
        dialog_title="Ikarus",
        dialog_message="Chybi povinny parametr: " + str(param_name or ""),
    )


def _manual_elapsed_ms(start: float) -> int:
    try:
        return int((time.time() - start) * 1000)
    except Exception:
        return -1


def _manual_log(provider: str, event: str, level=xbmc.LOGINFO, **fields):
    provider = re.sub(r"[^A-Z0-9_]+", "_", str(provider or "GENERIC").upper()).strip("_") or "GENERIC"
    event = re.sub(r"[^A-Z0-9_]+", "_", str(event or "EVENT").upper()).strip("_") or "EVENT"
    parts = []
    for key, value in fields.items():
        if value is None:
            continue
        lowered = str(key or "").strip().lower()
        if lowered in ("url", "direct", "final_url", "page_url", "embed_url"):
            value = source_common.redact_url_for_log(value)
        elif lowered in ("headers", "header") and isinstance(value, dict):
            value = source_common.redact_headers_for_log(value)
        elif (
            "token" in lowered
            or "secret" in lowered
            or lowered in ("cookie", "password", "pwd", "hash", "filehash", "video_hash")
        ):
            value = "***"
        text = str(value).replace("\r", " ").replace("\n", " ").strip()
        if len(text) > 220:
            text = text[:217] + "..."
        parts.append(f"{key}={text}")
    msg = f"[IKARUS][MANUAL][{provider}][{event}]"
    if parts:
        msg += " " + " ".join(parts)
    try:
        xbmc.log(msg, level)
    except Exception:
        pass


def _manual_norm_text(text: str) -> str:
    return source_labels.norm_text(text)


def _manual_audio_tag(title: str) -> str:
    return source_labels.source_language_tag(title)


def _manual_quality_tag(title: str, fallback: str = "") -> str:
    return source_labels.source_quality_from_text(f"{title or ''} {fallback or ''}")


def _manual_color_tag(tag: str, kind: str = "") -> str:
    tag = (tag or "").strip()
    if not tag:
        return ""
    kind = (kind or "").strip().lower()
    color = "gray"
    tag_low = tag.lower()
    if kind == "audio":
        if tag_low.startswith("cz"):
            color = "lime"
        elif tag_low.startswith("sk"):
            color = "lightgreen"
        elif tag_low.startswith("en"):
            color = "deepskyblue"
        else:
            color = "silver"
    elif kind == "quality":
        color = "skyblue"
    elif kind == "size":
        color = "gray"
    elif kind == "extra":
        color = "darkgray"
    return f"[COLOR {color}][{tag}][/COLOR]"


def _manual_source_label(title: str, audio: str = "", quality: str = "", size: str = "", extra: str = "") -> str:
    title = (title or i18n.T(30748, "Untitled")).strip()
    tags = []
    for tag, kind in ((audio, "audio"), (quality, "quality"), (size, "size")):
        tag = (tag or "").strip()
        if tag and tag not in tags:
            tags.append(_manual_color_tag(tag, kind))
    prefix = "".join(tags)
    suffix = f" {_manual_color_tag(extra.strip(), 'extra')}" if (extra or "").strip() else ""
    return f"{prefix} {title}{suffix}".strip()


def _manual_quality_rank(quality: str) -> int:
    q = (quality or "").strip().lower()
    if q in ("8k", "4320p"):
        return 80
    if q in ("4k", "uhd", "2160p"):
        return 70
    if q == "1080p":
        return 60
    if q == "720p":
        return 50
    if q == "576p":
        return 40
    if q == "480p":
        return 30
    if q == "360p":
        return 20
    if q == "hd":
        return 45
    if q == "sd":
        return 10
    return 0


def _manual_audio_rank(title: str) -> int:
    audio = (_manual_audio_tag(title) or "").lower()
    if audio == "cz dab":
        return 80
    if audio == "cz":
        return 70
    if audio == "sk dab":
        return 60
    if audio == "sk":
        return 50
    if audio == "cz tit":
        return 45
    if audio == "sk tit":
        return 40
    if audio == "en":
        return 20
    return 0


def _manual_size_bytes(value) -> int:
    return source_labels.source_int_from_any(value, 0)


def _manual_row_size_bytes(row: dict) -> int:
    return source_labels.source_size_bytes(row or {})


def _manual_result_sort_key(item):
    idx, row = item
    row = row or {}
    title = row.get("title") or row.get("name") or ""
    quality = _manual_quality_tag(title, row.get("quality") or "")
    return (
        -_manual_audio_rank(title),
        -_manual_quality_rank(quality),
        -_manual_row_size_bytes(row),
        idx,
        _manual_norm_text(title),
    )


def _manual_sort_results(rows):
    return [
        row for _idx, row in sorted(
            enumerate(list(rows or [])),
            key=_manual_result_sort_key,
        )
    ]


def _cookie_header_for_url_basic(url: str) -> str:
    return source_common.cookie_header_for_url(_COOKIEJAR, url, relaxed_domain=False)


_PREHRAJTO_BASE = "https://prehrajto.cz"
_PREHRAJTO_LEGACY_BASE = "https://prehraj.to:443"
_PREHRAJTO_LOGIN_URL = urllib.parse.urljoin(_PREHRAJTO_BASE, "/prihlaseni")
_PREHRAJTO_SEARCH_PREFIX = "/hledej/"
_PREHRAJTO_PAGINATOR_PARAM = "videoListing-visualPaginator-page"
_PREHRAJTO_DETAIL_RE = re.compile(r"^/[^/]+/[0-9a-f]{10,}$", re.IGNORECASE)
_PREHRAJTO_MAX_PAGES = 30
_PREHRAJTO_UI_PAGE_SIZE = 32
_PREHRAJTO_LOGIN_ATTEMPTED = False


def _build_prehrajto_search_url_legacy(query: str, page: int) -> str:
    q = urllib.parse.quote(query.strip())
    base = urllib.parse.urljoin(_PREHRAJTO_LEGACY_BASE, f"{_PREHRAJTO_SEARCH_PREFIX}{q}")
    return base + "?" + urllib.parse.urlencode({"vp-page": page})


def _prehrajto_setting(setting_id: str) -> str:
    try:
        return (addon.getSetting(setting_id) or "").strip()
    except Exception:
        return ""


def _prehrajto_auth_enabled() -> bool:
    return bool(_prehrajto_setting("prehrajto_email") and _prehrajto_setting("prehrajto_password"))


def _prehrajto_mask_login(login: str) -> str:
    login = (login or "").strip()
    if not login:
        return ""
    if "@" in login:
        name, domain = login.split("@", 1)
        return (name[:2] + "***@" + domain) if name else "***@" + domain
    return login[:2] + "***"


def _prehrajto_cookie_value(name: str) -> str:
    name = (name or "").strip()
    if not name:
        return ""
    try:
        for cookie in _COOKIEJAR:
            if cookie.name != name:
                continue
            value = str(cookie.value or "").strip()
            if value and value.lower() != "deleted":
                return value
    except Exception:
        pass
    return ""


def _prehrajto_has_auth_cookie() -> bool:
    return bool(_prehrajto_cookie_value("access_token") or _prehrajto_cookie_value("refresh_token"))


def _prehrajto_login_if_configured(force: bool = False) -> bool:
    global _PREHRAJTO_LOGIN_ATTEMPTED
    if _prehrajto_has_auth_cookie():
        return True
    email = _prehrajto_setting("prehrajto_email")
    password = _prehrajto_setting("prehrajto_password")
    if not email or not password:
        return False
    if _PREHRAJTO_LOGIN_ATTEMPTED and not force:
        return _prehrajto_has_auth_cookie()

    _PREHRAJTO_LOGIN_ATTEMPTED = True
    start = time.time()
    try:
        _http_get(_PREHRAJTO_LOGIN_URL, timeout=MANUAL_SEARCH_TIMEOUT)
        payload = {
            "email": email,
            "password": password,
            "remember_login": "on",
            "login": "Přihlásit se",
            "_do": "login-loginForm-submit",
        }
        html_text = _http_post(
            _PREHRAJTO_LOGIN_URL + "?frm=login-loginForm",
            payload,
            timeout=MANUAL_SEARCH_TIMEOUT,
            referer=_PREHRAJTO_LOGIN_URL,
        )
        ok = _prehrajto_has_auth_cookie()
        _manual_log(
            "prehrajto",
            "LOGIN",
            level=xbmc.LOGINFO if ok else xbmc.LOGWARNING,
            ok=ok,
            user=_prehrajto_mask_login(email),
            token=_prehrajto_has_auth_cookie(),
            ms=_manual_elapsed_ms(start),
        )
        return bool(ok)
    except Exception as e:
        _manual_log("prehrajto", "LOGIN_FAIL", xbmc.LOGWARNING, user=_prehrajto_mask_login(email), error=repr(e), ms=_manual_elapsed_ms(start))
        return False


def _prehrajto_has_next_page(html_text: str, page: int) -> bool:
    html_text = html_text or ""
    next_page = int(page or 1) + 1
    if re.search(rf'videoListing-visualPaginator-page={next_page}\b', html_text, re.IGNORECASE):
        return True
    if re.search(r'title=["\']Zobrazit\s+dal', html_text, re.IGNORECASE):
        return True
    return False


def _prehrajto_next_page_url(html_text: str, current_page: int) -> str:
    html_text = html_text or ""
    next_page = int(current_page or 1) + 1
    patterns = [
        rf'<a[^>]+href=["\'](?P<href>[^"\']*{_PREHRAJTO_PAGINATOR_PARAM}={next_page}\b[^"\']*)["\'][^>]*>',
        r'<a[^>]+rel=["\']next["\'][^>]+href=["\'](?P<href>[^"\']+)["\']',
        r'<a[^>]+href=["\'](?P<href>[^"\']+)["\'][^>]+title=["\']Zobrazit\s+dal',
    ]
    for pattern in patterns:
        m = re.search(pattern, html_text, re.IGNORECASE)
        if not m:
            continue
        href = html.unescape(m.group("href") or "").strip()
        if href:
            return urllib.parse.urljoin(_PREHRAJTO_BASE, href)
    return ""


def _prehrajto_requires_login(html_text: str) -> bool:
    html_text = html_text or ""
    if re.search(r"Chcete\s+vid[eě]t\s+dal[sš][ií]\s+v[yý]sledky", html_text, re.IGNORECASE):
        return True
    if re.search(r"pokra[cč]ov[aá]n[ií]\s+ve\s+vyhled[aá]v[aá]n[ií].{0,80}p[řr]ihl[aá]sit", html_text, re.IGNORECASE | re.DOTALL):
        return True
    if "registrationForm-registrationForm" in html_text and "loginDialog-login-loginForm" in html_text:
        return True
    return False


def _prehrajto_probe_next_page(query: str, page: int, current_url: str = ""):
    if not query:
        return False, False
    next_page = max(1, int(page or 1)) + 1
    next_url = _build_prehrajto_search_url(query, next_page)
    try:
        html_text = _http_get(next_url, timeout=MANUAL_SEARCH_TIMEOUT, referer=current_url or None)
    except Exception as e:
        _manual_log("prehrajto", "NEXT_PROBE_FAIL", xbmc.LOGWARNING, q=query, page=next_page, error=repr(e))
        return True, False
    if _prehrajto_requires_login(html_text):
        return False, True
    return bool(_extract_prehrajto_results(html_text)), False


def _prehrajto_log_url(url: str) -> str:
    try:
        u = urllib.parse.urlsplit(url or "")
        query_parts = []
        for key, value in urllib.parse.parse_qsl(u.query, keep_blank_values=True):
            if any(secret in key.lower() for secret in ("token", "password", "email")):
                value = "***"
            query_parts.append((key, value))
        query = urllib.parse.urlencode(query_parts, doseq=True)
        return urllib.parse.urlunsplit(("", "", u.path or "/", query, ""))
    except Exception:
        return ""


def _prehrajto_item_id(url: str) -> str:
    try:
        path = urllib.parse.urlsplit(url or "").path.strip("/")
        return path.rsplit("/", 1)[-1] if path else ""
    except Exception:
        return ""


def _extract_prehrajto_results(html_text: str):
    results = []
    seen = set()

    card_re = re.compile(
        r'<a\b(?P<attrs>[^>]*\bhref=["\'](?P<href>/[^"\']+)["\'][^>]*)>(?P<body>.*?)</a>',
        re.IGNORECASE | re.DOTALL,
    )
    for m in card_re.finditer(html_text or ""):
        href = (m.group("href") or "").strip()
        attrs = m.group("attrs") or ""
        body = m.group("body") or ""

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
        mt = re.search(r'\btitle=["\']([^"\']+)["\']', attrs, re.IGNORECASE)
        if mt:
            title = html.unescape(mt.group(1)).strip()

        if not title or len(title) < 2:
            mt = re.search(r'<h3[^>]*class=["\'][^"\']*video__title[^"\']*["\'][^>]*>(.*?)</h3>', body, re.IGNORECASE | re.DOTALL)
            if mt:
                title = html.unescape(re.sub(r"<[^>]+>", " ", mt.group(1))).strip()

        if not title or len(title) < 2:
            seg = href.strip("/").split("/", 1)[0]
            title = seg

        thumb = ""
        mi = re.search(r'<img[^>]+class=["\'][^"\']*\bthumb1\b[^"\']*["\'][^>]+\bsrc=["\']([^"\']+)["\']', body, re.IGNORECASE)
        if not mi:
            mi = re.search(r'<img[^>]+\bsrc=["\']([^"\']+)["\']', body, re.IGNORECASE)
        if mi:
            thumb = html.unescape(mi.group(1)).strip()
            if thumb.startswith("//"):
                thumb = "https:" + thumb
            elif thumb.startswith("/"):
                thumb = urllib.parse.urljoin(_PREHRAJTO_BASE, thumb)

        size_label = ""
        ms = re.search(r'<div[^>]+class=["\'][^"\']*video__tag--size[^"\']*["\'][^>]*>(.*?)</div>', body, re.IGNORECASE | re.DOTALL)
        if ms:
            size_label = html.unescape(re.sub(r"<[^>]+>", " ", ms.group(1))).strip()
            size_label = re.sub(r"\s+", " ", size_label)

        title = re.sub(r"\s+", " ", title).strip()
        results.append({
            "title": title,
            "url": full,
            "thumb": thumb,
            "size": size_label,
            "size_label": size_label,
        })

    return results

def prehrajto_search_all_pages(query: str, max_pages: int = 30):
    all_results = []
    seen_urls = set()

    for page in range(1, max_pages + 1):
        url = _build_prehrajto_search_url(query, page)
        html_text = _http_get(url)
        page_results = _extract_prehrajto_results(html_text)

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

def prehrajto_try_resolve_and_play(title: str, page_url: str) -> bool:
    start = time.time()
    try:
        detail_html = _http_get(page_url)
    except Exception as e:
        _manual_log("prehrajto", "RESOLVE_FAIL", xbmc.LOGWARNING, stage="detail", ms=_manual_elapsed_ms(start), title=title, error=repr(e), url=page_url)
        return False

    direct = _try_extract_sources_from_html(detail_html)
    if not direct:
        _manual_log("prehrajto", "RESOLVE_FAIL", xbmc.LOGWARNING, stage="sources", ms=_manual_elapsed_ms(start), title=title, url=page_url)
        return False

    if direct.startswith("/"):
        direct = urllib.parse.urljoin(_PREHRAJTO_BASE, direct)

    _manual_log("prehrajto", "PLAY", title=title, ms=_manual_elapsed_ms(start), url=direct)
    play_url(title, direct)
    return True


# ---------------------------
# HELLLSPY (vyhledání + zobrazení + přehrávání pokud API dá direct URL)
# ---------------------------

_HELLSPY_API_BASE = "https://api.hellspy.to"
_HELLSPY_SEARCH_ENDPOINT = _HELLSPY_API_BASE + "/gw/search"
_HELLSPY_SITE_BASE = "https://www.hellspy.to/"
_HELLSPY_LIMIT = 40
_SKTONLINE_BASE = "https://online.sktorrent.eu/"





def _hellspy_pick_thumb(item: dict) -> str:
    if not isinstance(item, dict):
        return ""

    thumbs = item.get("thumbs")
    if isinstance(thumbs, list):
        for thumb in thumbs:
            if isinstance(thumb, str) and thumb.strip():
                return thumb.strip()
    if isinstance(thumbs, str) and thumbs.strip():
        return thumbs.strip()

    for key in ("thumb", "thumbnail", "image", "poster", "cover"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            for nested_key in ("url", "src", "href"):
                nested = value.get(nested_key)
                if isinstance(nested, str) and nested.strip():
                    return nested.strip()
    return ""


_hellspy_get_stream = source_common.hellspy_download_stream

def _hellspy_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default

def _hellspy_size_label(size_bytes) -> str:
    size = _hellspy_int(size_bytes)
    if size <= 0:
        return ""
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(size)
    idx = 0
    while value >= 1024 and idx < len(units) - 1:
        value /= 1024.0
        idx += 1
    if idx <= 1:
        return f"{int(value)} {units[idx]}"
    return f"{value:.1f} {units[idx]}"

def _hellspy_sort_key(row: dict):
    # Hellspy mixes older derived ids first; newer ids are much more likely to
    # produce live CDN links, so keep the API page but prefer the fresh entries.
    return (
        _hellspy_int((row or {}).get("id")),
        _hellspy_int((row or {}).get("size")),
        str((row or {}).get("title") or "").lower(),
    )

def hellspy_search_all(query: str, limit: int = _HELLSPY_LIMIT, max_pages: int = 50, sleep_s: float = 0.0):
    seen = set()
    out = []

    offset = 0
    next_offset = None

    for _page in range(1, max_pages + 1):
        use_offset = next_offset if next_offset is not None else offset
        url = _hellspy_build_search_url(query, use_offset, limit)

        payload = _http_get_json(url)
        items, nxt = _hellspy_extract_items_and_next(payload)

        if not items:
            break

        added = 0
        for it in items:
            title = (it.get("title") or "").strip()
            vid = it.get("id")
            hsh = it.get("fileHash") or it.get("hash")
            if not (title and vid and hsh):
                continue

            item_url = f"{_HELLSPY_SITE_BASE}video/{hsh}/{vid}"
            if item_url in seen:
                continue
            seen.add(item_url)

            # direct ber jen pokud už ho search API samo vrátí
            direct = _hellspy_pick_direct_url(it)

            out.append({
                "title": re.sub(r"\s+", " ", title),
                "url": item_url,
                "direct": direct,
                "id": str(vid),
                "fileHash": str(hsh),
            })
            added += 1

        if added == 0:
            break

        if nxt is not None and nxt != use_offset:
            next_offset = nxt
        else:
            offset = use_offset + limit
            next_offset = None

        if sleep_s > 0:
            time.sleep(sleep_s)

    return out

def _sktonline_search_query(query: str) -> str:
    text = " ".join((query or "").split()).strip()
    try:
        text = "".join(
            ch for ch in unicodedata.normalize("NFKD", text)
            if not unicodedata.combining(ch)
        )
    except Exception:
        pass
    return text

def _sktonline_build_search_url(query: str) -> str:
    query = _sktonline_search_query(query)
    return _sktonline_abs(
        "/search/videos?type=public&t=a&o=mr&search_query=" + urllib.parse.quote_plus(query) + "&page=1",
        _SKTONLINE_BASE,
    )

def _sktonline_result_matches_query(title: str, wanted_query: str = "") -> bool:
    tokens = [
        t for t in _manual_norm_text(wanted_query).split()
        if len(t) >= 3 and not t.isdigit()
    ]
    if not tokens:
        return True

    title_norm = _manual_norm_text(title)
    return all(t in title_norm for t in tokens)

def _sktonline_extract_results(results_html: str, base_url: str, wanted_query: str = ""):
    def _strip_tags(s: str) -> str:
        s = re.sub(r"<[^>]+>", " ", s or "")
        s = html.unescape(s)
        s = re.sub(r"\s+", " ", s).strip()
        return s

    video_results = []
    seen_video = set()
    for m in re.finditer(
        r'<a\b([^>]*?)href=["\'](?P<href>/video/[^"\']+)["\']([^>]*)>(?P<inner>[\s\S]*?)</a>',
        results_html,
        re.IGNORECASE,
    ):
        href = (m.group("href") or "").strip()
        inner = m.group("inner") or ""
        full = _sktonline_abs(href, base_url)
        if full in seen_video:
            continue
        seen_video.add(full)

        title = ""
        for pat in (
            r'<span[^>]+class=["\'][^"\']*\bvideo-title\b[^"\']*["\'][^>]*>([\s\S]*?)</span>',
            r'<img\b[^>]+title=["\']([^"\']+)["\']',
            r'<img\b[^>]+alt=["\']([^"\']+)["\']',
        ):
            mt = re.search(pat, inner, re.IGNORECASE)
            if mt:
                title = _strip_tags(mt.group(1))
                break

        if not title:
            parts = [p for p in urllib.parse.urlparse(full).path.split("/") if p]
            title = parts[2] if len(parts) >= 3 else parts[-1] if parts else "SkTonLine"

        duration = ""
        mdur = re.search(r'<div[^>]+class=["\'][^"\']*\bduration\b[^"\']*["\'][^>]*>([\s\S]*?)</div>', inner, re.IGNORECASE)
        if mdur:
            duration = _strip_tags(mdur.group(1))
        quality = "HD" if "hd-text-icon" in inner.lower() else ""
        thumb = ""
        mthumb = re.search(r'<img\b[^>]+\bsrc=["\']([^"\']+)["\']', inner, re.IGNORECASE)
        if not mthumb:
            mthumb = re.search(r'<img\b[^>]+\bdata-src=["\']([^"\']+)["\']', inner, re.IGNORECASE)
        if mthumb:
            thumb = _sktonline_abs(html.unescape(mthumb.group(1)).strip(), base_url)

        clean_title = re.sub(r"\s+", " ", title).strip()
        if not _sktonline_result_matches_query(clean_title, wanted_query):
            continue

        video_results.append({
            "title": clean_title,
            "url": full,
            "quality": quality,
            "duration": duration,
            "thumb": thumb,
        })

    return video_results

def _sktonline_find_next_page_url(current_html: str, current_url: str):
    m = re.search(r'<a[^>]+rel=["\']next["\'][^>]+href=["\']([^"\']+)["\']', current_html, re.IGNORECASE)
    if m:
        return _sktonline_abs(m.group(1), current_url)

    pag = re.search(r'(<ul[^>]*class="[^"]*pagination[^"]*"[^>]*>[\s\S]*?</ul>)', current_html, re.IGNORECASE)
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
    q = " ".join((query or "").split()).strip()
    if not q:
        return []

    q_web = _sktonline_search_query(q)
    q_enc = urllib.parse.quote_plus(q_web)
    page_limit = min(int(max_pages or 50), 4)
    search_templates = [
        _SKTONLINE_BASE.rstrip("/") + "/search/videos?type=public&t=a&o=mr&search_query=" + q_enc + "&page={page}",
        _SKTONLINE_BASE.rstrip("/") + "/search/videos?search_query=" + q_enc + "&page={page}",
    ]

    out = []
    seen_items = set()

    for tmpl in search_templates:
        for page_no in range(1, page_limit + 1):
            cur_url = tmpl.format(page=page_no)
            cur_html = _http_get(
                cur_url,
                timeout=SKTONLINE_SEARCH_TIMEOUT,
                referer=_SKTONLINE_BASE.rstrip("/") + "/videos",
            )

            results = _sktonline_extract_results(cur_html, cur_url, wanted_query=q)
            added = 0
            for r in results:
                u = r.get("url") or ""
                if not u or u in seen_items:
                    continue
                seen_items.add(u)
                out.append(r)
                added += 1

            if added == 0:
                break

    return out

_dump_text_to_temp = source_common.dump_text_to_temp








def _http_follow_to_final_url(url: str, referer: str = None, timeout: int = 25) -> str:
    return source_common.follow_to_final_url(url, _OPENER, referer=referer, timeout=timeout)


def _cookie_header_for_url(url: str) -> str:
    return source_common.cookie_header_for_url(_COOKIEJAR, url, relaxed_domain=True)

def _probe_stream(url: str, headers: dict, timeout: int = 15):
    return source_common.probe_stream(url, headers, _OPENER, timeout=timeout, log_prefix="[IKARUS][SKT][PROBE]")

def _hellspy_stream_is_available(url: str, headers: dict):
    info = source_common.probe_stream(
        url,
        headers,
        _OPENER,
        timeout=8,
        log_prefix="[IKARUS][HELLSPY][PROBE]",
    )
    if info.get("ok"):
        return True, ""
    return False, info.get("error") or "stream probe failed"

def sktonline_try_resolve_and_play(title: str, page_url: str) -> bool:
    start = time.time()
    detail_html = ""
    embed_html = ""
    embed_url = ""

    try:
        detail_html = _http_get(page_url, referer=_SKTONLINE_BASE)
    except Exception as e:
        _dump_text_to_temp("skt_detail.html", detail_html)
        _dump_text_to_temp("skt_embed.html", embed_html)
        _dump_text_to_temp("skt_debug.txt", f"page_url={page_url}\nembed_url={embed_url}\nerror={repr(e)}\n")
        _manual_log("sktonline", "RESOLVE_FAIL", xbmc.LOGWARNING, stage="detail", ms=_manual_elapsed_ms(start), title=title, error=repr(e), url=page_url)
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
        _manual_log("sktonline", "RESOLVE_FAIL", xbmc.LOGWARNING, stage="sources", ms=_manual_elapsed_ms(start), title=title, embed=bool(embed_url), url=page_url)
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
        probe = _probe_stream(src1, headers, timeout=6)
        if isinstance(probe, dict) and probe.get("ok") is False:
            _manual_log("sktonline", "RESOLVE_FAIL", xbmc.LOGWARNING, stage="probe", ms=_manual_elapsed_ms(start), title=title, url=src1, error=probe.get("error"))
            return False
        _manual_log("sktonline", "PLAY", title=title, sources=len(sources), ms=_manual_elapsed_ms(start), url=src1)
        play_url(title, src1, headers=headers)
        return True

    idx = xbmcgui.Dialog().select(i18n.T(30727, "Select quality"), labels)
    if idx is None or idx < 0:
        _manual_log("sktonline", "CANCEL", title=title, sources=len(sources), ms=_manual_elapsed_ms(start))
        return True

    picked = sources[idx]
    stream_url = (picked.get("src") or "").strip()
    if not stream_url:
        _manual_log("sktonline", "RESOLVE_FAIL", xbmc.LOGWARNING, stage="empty_source", ms=_manual_elapsed_ms(start), title=title)
        return False

    headers = dict(base_headers)
    ck = _cookie_header_for_url(stream_url)
    if ck:
        headers["Cookie"] = ck

    picked_label = (picked.get("label") or "").strip()
    play_title = f"{title} ({picked_label})" if picked_label else title

    xbmc.log(f"[IKARUS][SKT] PLAY: {source_common.redact_url_for_log(stream_url)}", xbmc.LOGWARNING)
    xbmc.log(f"[IKARUS][SKT] HDR: {source_common.redact_headers_for_log(headers)}", xbmc.LOGWARNING)
    probe = _probe_stream(stream_url, headers, timeout=6)
    if isinstance(probe, dict) and probe.get("ok") is False:
        _manual_log("sktonline", "RESOLVE_FAIL", xbmc.LOGWARNING, stage="probe", ms=_manual_elapsed_ms(start), title=title, url=stream_url, error=probe.get("error"))
        return False
    _manual_log("sktonline", "PLAY", title=play_title, sources=len(sources), picked=picked_label, ms=_manual_elapsed_ms(start), url=stream_url)
    play_url(play_title, stream_url, headers=headers)
    return True


# ---------------------------
# FAST MANUAL SEARCH UI
# ---------------------------

def prehrajto_search_page(query: str, page: int = 1):
    page = max(1, int(page or 1))
    html_text = _http_get(_build_prehrajto_search_url(query, page), timeout=MANUAL_SEARCH_TIMEOUT)
    return _extract_prehrajto_results(html_text)


def _prehrajto_fetch_search_page(query: str, page: int, page_url: str = "", referer: str = ""):
    urls = [page_url] if page_url else [_build_prehrajto_search_url(query, page)]
    legacy = _build_prehrajto_search_url_legacy(query, page)
    if not page_url and page == 1 and legacy not in urls:
        urls.append(legacy)

    last_html = ""
    last_results = []
    last_auth_required = False
    last_has_next = False
    last_next_url = ""
    for url in urls:
        try:
            html_text = _http_get(url, timeout=MANUAL_SEARCH_TIMEOUT, referer=referer or None)
            auth_required = _prehrajto_requires_login(html_text)
            results = _extract_prehrajto_results(html_text)
            has_next = False if auth_required else _prehrajto_has_next_page(html_text, page)
            next_url = "" if auth_required else _prehrajto_next_page_url(html_text, page)
            if results or auth_required:
                return results, has_next, auth_required, html_text, next_url
            last_html = html_text
            last_results = results
            last_auth_required = auth_required
            last_has_next = has_next
            last_next_url = next_url
        except Exception as e:
            _manual_log("prehrajto", "PAGE_FAIL", xbmc.LOGWARNING, q=query, page=page, error=repr(e), url=url)
    return last_results, last_has_next, last_auth_required, last_html, last_next_url


def _prehrajto_load_all_pages(query: str, max_pages: int = _PREHRAJTO_MAX_PAGES):
    all_results = []
    seen_urls = set()
    pages_loaded = 0
    blocked_page = 0
    auth_required = False
    logged_in = _prehrajto_login_if_configured()
    page_url = ""
    previous_url = ""
    duplicate_streak = 0

    for source_page in range(1, max(1, int(max_pages or _PREHRAJTO_MAX_PAGES)) + 1):
        page_start = time.time()
        current_url = page_url or _build_prehrajto_search_url(query, source_page)
        page_results, has_next, page_auth_required, _html_text, next_url = _prehrajto_fetch_search_page(
            query,
            source_page,
            page_url=page_url,
            referer=previous_url,
        )

        if page_auth_required:
            auth_required = True
            blocked_page = source_page
            _manual_log(
                "prehrajto",
                "PAGE",
                q=query,
                source_page=source_page,
                rows=len(page_results or []),
                new=0,
                total=len(all_results),
                next=has_next,
                page_url=_prehrajto_log_url(current_url),
                next_page_url=_prehrajto_log_url(next_url),
                auth_required=page_auth_required,
                logged_in=logged_in,
                ms=_manual_elapsed_ms(page_start),
            )
            break
        if not page_results:
            _manual_log(
                "prehrajto",
                "PAGE",
                q=query,
                source_page=source_page,
                rows=0,
                new=0,
                total=len(all_results),
                next=has_next,
                page_url=_prehrajto_log_url(current_url),
                next_page_url=_prehrajto_log_url(next_url),
                auth_required=page_auth_required,
                logged_in=logged_in,
                ms=_manual_elapsed_ms(page_start),
            )
            break

        new_added = 0
        for item in page_results:
            url = item.get("url") or ""
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            all_results.append(item)
            new_added += 1

        first_id = _prehrajto_item_id((page_results[0] or {}).get("url") if page_results else "")
        last_id = _prehrajto_item_id((page_results[-1] or {}).get("url") if page_results else "")
        _manual_log(
            "prehrajto",
            "PAGE",
            q=query,
            source_page=source_page,
            rows=len(page_results or []),
            new=new_added,
            total=len(all_results),
            next=has_next,
            page_url=_prehrajto_log_url(current_url),
            next_page_url=_prehrajto_log_url(next_url),
            first=first_id,
            last=last_id,
            auth_required=page_auth_required,
            logged_in=logged_in,
            ms=_manual_elapsed_ms(page_start),
        )

        if new_added:
            pages_loaded = source_page
            duplicate_streak = 0
        if new_added == 0:
            duplicate_streak += 1
            _manual_log(
                "prehrajto",
                "DUPLICATE_PAGE_STOP",
                q=query,
                source_page=source_page,
                rows=len(page_results or []),
                total=len(all_results),
                next=has_next,
                page_url=_prehrajto_log_url(current_url),
                next_page_url=_prehrajto_log_url(next_url),
                streak=duplicate_streak,
            )
            break
        if not has_next:
            break
        previous_url = current_url
        page_url = next_url or _build_prehrajto_search_url(query, source_page + 1)

    sorted_results = _manual_sort_results(all_results)
    ui_pages = (len(sorted_results) + _PREHRAJTO_UI_PAGE_SIZE - 1) // _PREHRAJTO_UI_PAGE_SIZE
    return {
        "results": sorted_results,
        "pages_loaded": pages_loaded,
        "ui_pages": max(1, ui_pages) if sorted_results else 0,
        "auth_required": auth_required,
        "blocked_page": blocked_page,
        "logged_in": logged_in,
        "max_pages": int(max_pages or _PREHRAJTO_MAX_PAGES),
        "page_size": _PREHRAJTO_UI_PAGE_SIZE,
    }


def _prehrajto_cache_payload(q: str) -> dict:
    email = _prehrajto_setting("prehrajto_email").lower()
    auth_key = hashlib.md5(email.encode("utf-8")).hexdigest()[:10] if email else ""
    return {
        "q": q,
        "mode": "all-pages",
        "display_order": "lang-quality-size-v10-prehrajto-size-thumb",
        "page_size": _PREHRAJTO_UI_PAGE_SIZE,
        "auth": bool(email and _prehrajto_setting("prehrajto_password")),
        "auth_key": auth_key,
    }


def _prehrajto_page_slice(results, page: int):
    page = max(1, int(page or 1))
    start = (page - 1) * _PREHRAJTO_UI_PAGE_SIZE
    end = start + _PREHRAJTO_UI_PAGE_SIZE
    return list(results or [])[start:end]


def prehrajto_search_ui():
    q = xbmcgui.Dialog().input(i18n.T(30602, "Prehraj.to - enter search query"), type=xbmcgui.INPUT_ALPHANUM)
    if not q or not q.strip():
        main_menu()
        return
    _manual_remember("prehrajto", q.strip(), page=1)
    prehrajto_results_ui(q.strip(), page=1, force=True)


def prehrajto_results_ui(q: str, page: int = 1, force: bool = False):
    q = (q or "").strip()
    page = max(1, int(page or 1))
    if not q:
        prehrajto_search_ui()
        return

    cache_payload = _prehrajto_cache_payload(q)
    cached = None if force else _manual_cache_get("prehrajto", cache_payload)
    if cached is not None:
        if isinstance(cached, dict):
            data = cached
        else:
            data = {"results": cached or [], "auth_required": False, "blocked_page": 0, "pages_loaded": 0}
        _manual_log(
            "prehrajto",
            "CACHE",
            q=q,
            page=page,
            rows=len(data.get("results") or []),
            pages_loaded=data.get("pages_loaded"),
            ui_pages=data.get("ui_pages"),
            auth_required=bool(data.get("auth_required")),
            logged_in=bool(data.get("logged_in")),
        )
    else:
        start = time.time()
        try:
            data = _prehrajto_load_all_pages(q, max_pages=_PREHRAJTO_MAX_PAGES)
        except Exception as e:
            _manual_log("prehrajto", "SEARCH_FAIL", xbmc.LOGERROR, q=q, page=page, ms=_manual_elapsed_ms(start), error=repr(e))
            data = {"results": [], "auth_required": False, "blocked_page": 0, "pages_loaded": 0, "ui_pages": 0}
        _manual_cache_set("prehrajto", cache_payload, data)
        _manual_log(
            "prehrajto",
            "SEARCH",
            q=q,
            page=page,
            rows=len(data.get("results") or []),
            pages_loaded=data.get("pages_loaded"),
            ui_pages=data.get("ui_pages"),
            auth_required=bool(data.get("auth_required")),
            blocked_page=data.get("blocked_page") or "",
            logged_in=bool(data.get("logged_in")),
            ms=_manual_elapsed_ms(start),
        )

    results = data.get("results") or []
    total_rows = len(results)
    ui_pages = int(data.get("ui_pages") or 0)
    if total_rows:
        ui_pages = max(1, (total_rows + _PREHRAJTO_UI_PAGE_SIZE - 1) // _PREHRAJTO_UI_PAGE_SIZE)
    page = min(page, ui_pages or 1)
    page_results = _prehrajto_page_slice(results, page)
    auth_required = bool(data.get("auth_required"))
    blocked_page = int(data.get("blocked_page") or 0)
    pages_loaded = int(data.get("pages_loaded") or 0)

    xbmcplugin.setContent(addon_handle, "files")
    xbmcplugin.setPluginCategory(addon_handle, f"Prehraj.to: {q} ({page}/{ui_pages or 1})")
    _add_manual_nav("Prehraj.to", "other.prehrajto", {"action": "other.prehrajto.results", "q": q, "page": page})

    if not page_results:
        if auth_required:
            _add_empty_row(i18n.T(30605, "More Prehraj.to pages require web login."))
        else:
            _add_empty_row(i18n.T(30606, "Nothing found."))
        xbmcplugin.endOfDirectory(addon_handle, succeeded=True, cacheToDisc=True)
        return

    for it in page_results:
        title = it.get("title") or i18n.T(30748, "Untitled")
        url = it.get("url") or ""
        thumb = it.get("thumb") or "DefaultVideo.png"
        size_label = it.get("size_label") or it.get("size") or ""
        label = _manual_source_label(
            title,
            audio=_manual_audio_tag(title),
            quality=_manual_quality_tag(title),
            size=size_label,
        )
        li = xbmcgui.ListItem(label=label)
        li.setArt({"icon": thumb, "thumb": thumb, "poster": thumb, "fanart": thumb})
        info = {"title": title}
        if size_label:
            info["plot"] = f"{i18n.T(30612, 'Size')}: {size_label}"
        li.setInfo("video", info)
        li.setProperty("IsPlayable", "true")
        xbmcplugin.addDirectoryItem(
            handle=addon_handle,
            url=build_url({"action": "other.prehrajto_item", "title": title, "url": url}),
            listitem=li,
            isFolder=False,
        )

    if page > 1:
        li_prev = xbmcgui.ListItem(label=i18n.T(30607, "<< Previous page"))
        li_prev.setArt({"icon": "DefaultFolderBack.png", "thumb": "DefaultFolderBack.png"})
        xbmcplugin.addDirectoryItem(
            handle=addon_handle,
            url=build_url({"action": "other.prehrajto.results", "q": q, "page": page - 1}),
            listitem=li_prev,
            isFolder=True,
        )

    if page < ui_pages:
        li_next = xbmcgui.ListItem(label=i18n.T(30608, "Next page >>"))
        li_next.setArt({"icon": "DefaultFolder.png", "thumb": "DefaultFolder.png"})
        xbmcplugin.addDirectoryItem(
            handle=addon_handle,
            url=build_url({"action": "other.prehrajto.results", "q": q, "page": page + 1}),
            listitem=li_next,
            isFolder=True,
        )

    if pages_loaded:
        _add_empty_row(i18n.T(30609, "Loaded from web: {pages} pages").format(pages=pages_loaded))
    xbmcplugin.endOfDirectory(addon_handle, succeeded=True, cacheToDisc=True)


def hellspy_search_page(query: str, offset: int = 0, limit: int = _HELLSPY_LIMIT):
    offset = max(0, int(offset or 0))
    limit = max(1, min(100, int(limit or _HELLSPY_LIMIT)))
    payload = _http_get_json(_hellspy_build_search_url(query, offset, limit), timeout=MANUAL_SEARCH_TIMEOUT)
    items, next_offset = _hellspy_extract_items_and_next(payload)
    out = []
    seen = set()

    for it in items:
        title = (it.get("title") or "").strip()
        vid = it.get("id")
        hsh = it.get("fileHash") or it.get("hash")
        if not (title and vid and hsh):
            continue
        item_url = f"{_HELLSPY_SITE_BASE}video/{hsh}/{vid}"
        seen_key = str(hsh or item_url).strip()
        if seen_key in seen:
            continue
        seen.add(seen_key)
        size = it.get("size") or it.get("fileSize") or it.get("filesize") or 0
        duration = it.get("duration") or it.get("length") or ""
        out.append({
            "title": re.sub(r"\s+", " ", title),
            "url": item_url,
            "direct": _hellspy_pick_direct_url(it),
            "id": str(vid),
            "fileHash": str(hsh),
            "size": _hellspy_int(size),
            "duration": str(duration or ""),
            "thumb": _hellspy_pick_thumb(it),
        })

    out.sort(key=_hellspy_sort_key, reverse=True)
    if next_offset is None and len(items) >= limit:
        next_offset = offset + limit
    return out, next_offset


def hellspy_search_ui():
    q = xbmcgui.Dialog().input(i18n.T(30603, "Hellspy - enter search query"), type=xbmcgui.INPUT_ALPHANUM)
    if not q or not q.strip():
        main_menu()
        return
    _manual_remember("hellspy", q.strip(), offset=0)
    hellspy_results_ui(q.strip(), offset=0, force=True)


def hellspy_results_ui(q: str, offset: int = 0, force: bool = False):
    q = (q or "").strip()
    offset = max(0, int(offset or 0))
    if not q:
        hellspy_search_ui()
        return

    cache_payload = {"q": q, "offset": offset, "limit": _HELLSPY_LIMIT, "order": "lang-quality-size-v2-thumb"}
    cached = None if force else _manual_cache_get("hellspy", cache_payload)
    if cached is not None:
        results = cached.get("results") or []
        next_offset = cached.get("next_offset")
        _manual_log("hellspy", "CACHE", q=q, offset=offset, rows=len(results), next=next_offset)
    else:
        start = time.time()
        try:
            results, next_offset = hellspy_search_page(q, offset=offset, limit=_HELLSPY_LIMIT)
        except Exception as e:
            _manual_log("hellspy", "SEARCH_FAIL", xbmc.LOGERROR, q=q, offset=offset, ms=_manual_elapsed_ms(start), error=repr(e))
            results, next_offset = [], None
        _manual_cache_set("hellspy", cache_payload, {"results": results, "next_offset": next_offset})
        _manual_log("hellspy", "SEARCH", q=q, offset=offset, rows=len(results), next=next_offset, ms=_manual_elapsed_ms(start))

    xbmcplugin.setContent(addon_handle, "files")
    xbmcplugin.setPluginCategory(addon_handle, f"Hellspy: {q}")
    _add_manual_nav("Hellspy", "other.hellspy", {"action": "other.hellspy.results", "q": q, "offset": offset})

    if not results:
        _add_empty_row(i18n.T(30606, "Nothing found."))
        xbmcplugin.endOfDirectory(addon_handle, succeeded=True, cacheToDisc=True)
        return

    for it in _manual_sort_results(results):
        title = it.get("title") or i18n.T(30748, "Untitled")
        size_label = _hellspy_size_label(it.get("size"))
        thumb = it.get("thumb") or "DefaultVideo.png"
        label = _manual_source_label(
            title,
            audio=_manual_audio_tag(title),
            quality=_manual_quality_tag(title),
            size=size_label,
        )
        li = xbmcgui.ListItem(label=label)
        li.setArt({"icon": thumb, "thumb": thumb, "poster": thumb, "fanart": thumb})
        info = {"title": title}
        if size_label:
            info["plot"] = f"{i18n.T(30612, 'Size')}: {size_label}"
        li.setInfo("video", info)
        li.setProperty("IsPlayable", "true")
        xbmcplugin.addDirectoryItem(
            handle=addon_handle,
            url=build_url({
                "action": "other.hellspy_item",
                "title": title,
                "url": it.get("url") or "",
                "direct": it.get("direct") or "",
                "id": it.get("id") or "",
                "fileHash": it.get("fileHash") or "",
            }),
            listitem=li,
            isFolder=False,
        )

    if next_offset is not None and int(next_offset) != offset:
        li_next = xbmcgui.ListItem(label=i18n.T(30610, "More results >>"))
        li_next.setArt({"icon": "DefaultFolder.png", "thumb": "DefaultFolder.png"})
        xbmcplugin.addDirectoryItem(
            handle=addon_handle,
            url=build_url({"action": "other.hellspy.results", "q": q, "offset": int(next_offset)}),
            listitem=li_next,
            isFolder=True,
        )

    xbmcplugin.endOfDirectory(addon_handle, succeeded=True, cacheToDisc=True)


def sktonline_search_page(query: str, page_url: str = ""):
    query = (query or "").strip()
    page_url = (page_url or "").strip()

    if page_url:
        cur_url = page_url
        cur_html = _http_get(cur_url, timeout=SKTONLINE_SEARCH_TIMEOUT, referer=_SKTONLINE_BASE.rstrip("/") + "/videos")
    else:
        cur_url = _sktonline_build_search_url(query)
        cur_html = _http_get(cur_url, timeout=SKTONLINE_SEARCH_TIMEOUT, referer=_SKTONLINE_BASE.rstrip("/") + "/videos")

    results = _sktonline_extract_results(cur_html, cur_url, wanted_query=query)
    if not results:
        try:
            safe_q = re.sub(r"[^a-zA-Z0-9_-]+", "_", _sktonline_search_query(query or "empty")).strip("_") or "empty"
            _dump_text_to_temp(f"skt_manual_{safe_q}.html", cur_html or "")
        except Exception:
            pass
    next_url = _sktonline_find_next_page_url(cur_html, cur_url)
    if next_url == cur_url:
        next_url = ""
    return results, next_url or "", cur_url


def sktonline_search_ui():
    q = xbmcgui.Dialog().input(i18n.T(30604, "SkTonLine - enter search query"), type=xbmcgui.INPUT_ALPHANUM)
    if not q or not q.strip():
        main_menu()
        return
    _manual_remember("sktonline", q.strip(), page_url="")
    sktonline_results_ui(q.strip(), page_url="", force=True)


def sktonline_results_ui(q: str, page_url: str = "", force: bool = False):
    q = (q or "").strip()
    page_url = (page_url or "").strip()
    if not q:
        sktonline_search_ui()
        return

    cache_payload = {"q": q, "page_url": page_url, "parser": "video-card-v7-strict-query", "web_q": _sktonline_search_query(q), "display_order": "lang-quality-size-v2-thumb"}
    cached = None if force else _manual_cache_get("sktonline", cache_payload)
    if cached is not None:
        results = cached.get("results") or []
        next_url = cached.get("next_url") or ""
        cur_url = cached.get("cur_url") or page_url
        _manual_log("sktonline", "CACHE", q=q, rows=len(results), next=bool(next_url), page_url=page_url)
    else:
        start = time.time()
        try:
            results, next_url, cur_url = sktonline_search_page(q, page_url=page_url)
        except Exception as e:
            _manual_log("sktonline", "SEARCH_FAIL", xbmc.LOGERROR, q=q, page_url=page_url, ms=_manual_elapsed_ms(start), error=repr(e))
            results, next_url, cur_url = [], "", page_url
        _manual_cache_set("sktonline", cache_payload, {"results": results, "next_url": next_url, "cur_url": cur_url})
        _manual_log("sktonline", "SEARCH", q=q, rows=len(results), next=bool(next_url), ms=_manual_elapsed_ms(start), page_url=page_url)

    xbmcplugin.setContent(addon_handle, "files")
    xbmcplugin.setPluginCategory(addon_handle, f"SkTonLine: {q}")
    _add_manual_nav(
        "SkTonLine",
        "other.sktonline",
        {"action": "other.sktonline.results", "q": q, "page_url": page_url},
    )

    if not results:
        _add_empty_row(i18n.T(30606, "Nothing found."))
        xbmcplugin.endOfDirectory(addon_handle, succeeded=True, cacheToDisc=True)
        return

    for it in _manual_sort_results(results):
        title = it.get("title") or i18n.T(30748, "Untitled")
        url = it.get("url") or ""
        thumb = it.get("thumb") or "DefaultVideo.png"
        label = _manual_source_label(
            title,
            audio=_manual_audio_tag(title),
            quality=_manual_quality_tag(title, it.get("quality") or ""),
            extra=it.get("duration") or "",
        )
        li = xbmcgui.ListItem(label=label)
        li.setArt({"icon": thumb, "thumb": thumb, "poster": thumb, "fanart": thumb})
        info = {"title": title}
        if it.get("duration"):
            info["plot"] = f"{i18n.T(30613, 'Length')}: {it.get('duration')}"
        li.setInfo("video", info)
        li.setProperty("IsPlayable", "true")
        xbmcplugin.addDirectoryItem(
            handle=addon_handle,
            url=build_url({"action": "other.sktonline_item", "title": title, "url": url}),
            listitem=li,
            isFolder=False,
        )

    if next_url:
        li_next = xbmcgui.ListItem(label=i18n.T(30608, "Next page >>"))
        li_next.setArt({"icon": "DefaultFolder.png", "thumb": "DefaultFolder.png"})
        xbmcplugin.addDirectoryItem(
            handle=addon_handle,
            url=build_url({"action": "other.sktonline.results", "q": q, "page_url": next_url}),
            listitem=li_next,
            isFolder=True,
        )

    xbmcplugin.endOfDirectory(addon_handle, succeeded=True, cacheToDisc=True)


# ---------------------------
# ROUTER
# ---------------------------

def _router_impl():
    _refresh_runtime()
    paramstring = sys.argv[2][1:] if len(sys.argv) > 2 and sys.argv[2].startswith("?") else ""
    params = dict(urllib.parse.parse_qsl(paramstring)) if paramstring else {}
    action = params.get("action") or ""

    if not action or action == "other_sources":
        main_menu()
        return

    # Přehraj.to
    if action == "other.prehrajto":
        active = _manual_active("prehrajto")
        if (params.get("new") or "") != "1" and active.get("q"):
            prehrajto_results_ui(active.get("q") or "", page=int(active.get("page") or 1), force=False)
            return
        prehrajto_search_ui()
        return

    if action == "other.prehrajto.results":
        _manual_remember("prehrajto", params.get("q") or "", page=int(params.get("page") or "1"))
        prehrajto_results_ui(
            params.get("q") or "",
            page=int(params.get("page") or "1"),
            force=(params.get("force") or "") == "1",
        )
        return

    if action == "other.prehrajto_item":
        play_start = time.time()
        title = params.get("title") or "Přehraj.to"
        url = params.get("url") or ""
        if not url:
            _manual_missing_param("prehrajto", "url", title=title)
            return

        if _is_direct_media_url(url):
            _manual_log("prehrajto", "PLAY", title=title, mode="direct", ms=_manual_elapsed_ms(play_start), url=url)
            play_url(title, url)
            return

        ok = prehrajto_try_resolve_and_play(title, url)
        if ok:
            return

        _manual_play_failed(
            "prehrajto",
            "manual resolve failed",
            title=title,
            url=url,
            dialog_title="Přehraj.to",
            dialog_message=i18n.T(31933).format(url=url),
        )
        return

    # Hellspy
    if action == "other.hellspy":
        active = _manual_active("hellspy")
        if (params.get("new") or "") != "1" and active.get("q"):
            hellspy_results_ui(active.get("q") or "", offset=int(active.get("offset") or 0), force=False)
            return
        hellspy_search_ui()
        return

    if action == "other.hellspy.results":
        _manual_remember("hellspy", params.get("q") or "", offset=int(params.get("offset") or "0"))
        hellspy_results_ui(
            params.get("q") or "",
            offset=int(params.get("offset") or "0"),
            force=(params.get("force") or "") == "1",
        )
        return

    if action == "other.hellspy_item":
        play_start = time.time()
        title = params.get("title") or "Hellspy"
        url = params.get("url") or ""
        direct = params.get("direct") or ""
        video_id = params.get("id") or ""
        video_hash = params.get("fileHash") or ""
        _manual_log("hellspy", "CLICK", title=title, id=video_id, hash=video_hash, has_direct=bool(direct))

        if not direct and not url and not (video_id and video_hash):
            _manual_missing_param("hellspy", "url/direct/id+fileHash", title=title)
            return
        if video_id and not video_hash:
            _manual_missing_param("hellspy", "fileHash", title=title, url=url)
            return
        if video_hash and not video_id:
            _manual_missing_param("hellspy", "id", title=title, url=url)
            return

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

        # 1) když už direct máme, rovnou přehrát
        if direct:
            ck = _cookie_header_for_url_basic(direct)
            if ck:
                headers["Cookie"] = ck

            ok, reason = _hellspy_stream_is_available(direct, headers)
            if not ok:
                _manual_play_failed(
                    "hellspy",
                    "hellspy stream probe failed: " + reason,
                    title=title,
                    url=direct,
                    dialog_title="Hellspy",
                    dialog_message=i18n.T(31937),
                )
                return

            xbmc.log(f"[IKARUS][HELLSPY] PLAY direct={source_common.redact_url_for_log(direct)}", xbmc.LOGWARNING)
            _manual_log("hellspy", "PLAY", title=title, mode="direct", id=video_id, ms=_manual_elapsed_ms(play_start), url=direct)
            play_url(title, direct, headers=headers)
            return

        # 2) fallback: když by url samo bylo přehratelné
        if url and _is_direct_media_url(url):
            ck = _cookie_header_for_url_basic(url)
            if ck:
                headers["Cookie"] = ck

            ok, reason = _hellspy_stream_is_available(url, headers)
            if not ok:
                _manual_play_failed(
                    "hellspy",
                    "hellspy stream probe failed: " + reason,
                    title=title,
                    url=url,
                    dialog_title="Hellspy",
                    dialog_message=i18n.T(31937),
                )
                return

            xbmc.log(f"[IKARUS][HELLSPY] PLAY url={source_common.redact_url_for_log(url)}", xbmc.LOGWARNING)
            _manual_log("hellspy", "PLAY", title=title, mode="url", id=video_id, ms=_manual_elapsed_ms(play_start), url=url)
            play_url(title, url, headers=headers)
            return

        # 3) stejně jako v main.py: až po kliknutí vezmeme id + hash a získáme stream
        if video_id and video_hash:
            resolve_start = time.time()
            direct = _hellspy_get_stream(video_id, video_hash)
            _manual_log("hellspy", "DOWNLOAD", title=title, id=video_id, ok=bool(direct), ms=_manual_elapsed_ms(resolve_start))
            if direct:
                ck = _cookie_header_for_url_basic(direct)
                if ck:
                    headers["Cookie"] = ck

                ok, reason = _hellspy_stream_is_available(direct, headers)
                if not ok:
                    _manual_play_failed(
                        "hellspy",
                        "hellspy stream probe failed: " + reason,
                        title=title,
                        url=direct,
                        dialog_title="Hellspy",
                        dialog_message=i18n.T(31937),
                    )
                    return

                xbmc.log(f"[IKARUS][HELLSPY] PLAY resolved={direct}", xbmc.LOGWARNING)
                _manual_log("hellspy", "PLAY", title=title, mode="resolved", id=video_id, ms=_manual_elapsed_ms(play_start), url=direct)
                play_url(title, direct, headers=headers)
                return

        _manual_play_failed(
            "hellspy",
            "manual stream url failed",
            title=title,
            url=url,
            dialog_title="Hellspy",
            dialog_message=i18n.T(31935).format(url=url),
        )
        return

    # SkTonLine
    if action == "other.sktonline":
        active = _manual_active("sktonline")
        if (params.get("new") or "") != "1" and active.get("q"):
            sktonline_results_ui(active.get("q") or "", page_url=active.get("page_url") or "", force=False)
            return
        sktonline_search_ui()
        return

    if action == "other.sktonline.results":
        _manual_remember("sktonline", params.get("q") or "", page_url=params.get("page_url") or "")
        sktonline_results_ui(
            params.get("q") or "",
            page_url=params.get("page_url") or "",
            force=(params.get("force") or "") == "1",
        )
        return

    if action == "other.sktonline_item":
        play_start = time.time()
        title = params.get("title") or "SkTonLine"
        url = params.get("url") or ""
        if not url:
            _manual_missing_param("sktonline", "url", title=title)
            return

        if _is_direct_media_url(url):
            _manual_log("sktonline", "PLAY", title=title, mode="direct", ms=_manual_elapsed_ms(play_start), url=url)
            play_url(title, url)
            return

        ok = sktonline_try_resolve_and_play(title, url)
        if ok:
            return

        _manual_play_failed(
            "sktonline",
            "manual resolve failed",
            title=title,
            url=url,
            dialog_title="SkTonLine",
            dialog_message=i18n.T(31934).format(url=url),
        )
        return

    # fallback
    main_menu()


def router():
    _refresh_runtime()
    paramstring = sys.argv[2][1:] if len(sys.argv) > 2 and sys.argv[2].startswith("?") else ""
    params = dict(urllib.parse.parse_qsl(paramstring)) if paramstring else {}
    action = params.get("action") or "other_sources"
    with diagnostics.action_scope("other_sources", action, params):
        return _router_impl()
