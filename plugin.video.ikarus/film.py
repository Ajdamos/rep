# -*- coding: utf-8 -*-
import re
import os
import json
import time
import hashlib
import urllib.parse
import xml.etree.ElementTree as ET
import requests
import xbmc
import xbmcgui
import xbmcplugin
import xbmcaddon
from xbmcvfs import translatePath
from icons import icon_path
from resources.lib import i18n, playback_utils, source_common






BASE = "https://webshare.cz"
API = BASE + "/api/"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/81.0.4044.138 Safari/537.36",
    "Referer": BASE,
}
TIMEOUT = 20
SEARCH_TIMEOUT = 10
FILEINFO_TIMEOUT = 10

def log(msg): xbmc.log(f"[Ikarus/film] {msg}", xbmc.LOGINFO)
def warn(msg): xbmc.log(f"[Ikarus/film][WARN] {msg}", xbmc.LOGWARNING)
def err(msg): xbmc.log(f"[Ikarus/film][ERROR] {msg}", xbmc.LOGERROR)

def _manual_elapsed_ms(start: float) -> int:
    try:
        return int((time.time() - start) * 1000)
    except Exception:
        return -1

def _manual_log_ws(event: str, level=xbmc.LOGINFO, **fields):
    event = re.sub(r"[^A-Z0-9_]+", "_", str(event or "EVENT").upper()).strip("_") or "EVENT"
    parts = []
    for key, value in fields.items():
        if value is None:
            continue
        lowered = str(key or "").strip().lower()
        if lowered in ("url", "direct", "final_url"):
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
    msg = f"[IKARUS][MANUAL][WEBSHARE][{event}]"
    if parts:
        msg += " " + " ".join(parts)
    try:
        xbmc.log(msg, level)
    except Exception:
        pass

def _addon():
    try: return xbmcaddon.Addon()
    except Exception: return None

# ---------- Cesty a cache ----------
def _profile_dir():
    ad = _addon()
    base = translatePath(ad.getAddonInfo('profile'))
    p = os.path.join(base, "cache")
    try: os.makedirs(p, exist_ok=True)
    except Exception: pass
    return p

def _cache_file(name): return os.path.join(_profile_dir(), name)

SEARCH_TTL = 600
FILEINFO_TTL = 7 * 24 * 3600

def _read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def _write_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        warn(f"write_json fail {path}: {e}")

# ---------- HTTP helpers ----------





def api(fnct, data, timeout=None):
    r = requests.post(API + fnct + "/", data=data, headers=HEADERS, timeout=timeout or TIMEOUT)
    r.raise_for_status()
    return r

def _ws_search_simple(query: str, token: str = None, limit: int = 50):
    """
    Minimalistické vyhledání přes /api/search – vrátí list dictů se základními poli:
      {'id': ident, 'name': name, 'size': int(size)}
    """
    if not query:
        return []
    data = {"what": query, "offset": 0, "limit": int(limit)}
    # chceme jen video (ať to nestahuje obrázky/archivy)
    data["category"] = "video"
    if token:
        data["wst"] = token
    try:
        root = ET.fromstring(api("search", data).content)
        if (root.findtext("status") or "OK") != "OK":
            return []
        out = []
        for f in root.findall("file"):
            ident = f.findtext("ident") or ""
            name  = f.findtext("name") or ""
            size_raw = f.findtext("size") or "0"
            img = (f.findtext("img") or "").strip()
            try:
                size_int = int(size_raw) if str(size_raw).isdigit() else 0
            except Exception:
                size_int = 0
            row = {"id": ident, "name": name, "size": size_int}
            if img:
                row["img"] = img
            out.append(row)
        return out
    except Exception:
        return []






# ---------- Pomocné formátování ----------
def fmt_size(bytes_str):
    try: n = int(bytes_str)
    except Exception: return "?"
    units = ["B","KB","MB","GB","TB"]; i,f=0,float(n)
    while f>=1024 and i<len(units)-1: f/=1024.0; i+=1
    return f"{f:.1f} {units[i]}"

def build_url(base_path, query_dict): return base_path + "?" + urllib.parse.urlencode(query_dict)

# ---------- Nastavení vyhledávání ----------
CATEGORY_LABELS = ["Vše", "Video", "Images", "Audio", "Archives", "Docs", "Adult"]
CATEGORY_MAP = {"Vše": None, "Video": "video", "Images": "images", "Audio": "audio",
                "Archives": "archives", "Docs": "docs", "Adult": "adult"}
SORT_LABELS = ["Relevance", "Nejnovější", "Největší", "Nejmenší"]
SORT_MAP   = {"Relevance": None, "Nejnovější": "recent", "Největší": "largest", "Nejmenší": "smallest"}
ORDER_BY_LABELS = ["Název", "Velikost", "Datum", "Hodnocení"]
ORDER_BY_MAP    = {"Název": "name", "Velikost": "size", "Datum": "created", "Hodnocení": "rating"}

def _get_enum_label(ad, setting_id, labels, default_index=0):
    try:
        idx_str = ad.getSetting(setting_id)
        idx = int(idx_str) if idx_str != "" else default_index
    except Exception:
        idx = default_index
    if idx<0 or idx>=len(labels): idx = default_index
    return labels[idx]

def get_search_prefs():
    ad = _addon()
    if not ad:
        return {"category": None, "sort": None, "limit": 25,
                "order_by": None, "desc": False, "use_token": True}
    cat_label  = _get_enum_label(ad, "search_category", CATEGORY_LABELS, default_index=1)
    sort_label = _get_enum_label(ad, "search_sort",     SORT_LABELS,     default_index=0)
    category = CATEGORY_MAP.get(cat_label, None)
    sort     = SORT_MAP.get(sort_label, None)
    try: limit = int(ad.getSetting("search_limit") or "25")
    except Exception: limit = 25
    limit = max(1, min(100, limit))
    try:
        order_label = _get_enum_label(ad, "search_order_by", ORDER_BY_LABELS, default_index=2)
        order_by = ORDER_BY_MAP.get(order_label, None)
    except Exception: order_by = None
    try: desc = (ad.getSetting("search_desc").lower() in ("true","1","yes"))
    except Exception: desc = False
    try: use_token = (ad.getSetting("search_use_token").lower() in ("true","1","yes"))
    except Exception: use_token = True
    return {"category": category, "sort": sort, "limit": limit,
            "order_by": order_by, "desc": desc, "use_token": use_token}



# ---------- Historie hledání ----------
HISTORY_KEY = "search_history"; HISTORY_MAX = 20
_ACTIVE_WS_SEARCH = {}


def _remember_active_ws_search(dotaz, prefs):
    global _ACTIVE_WS_SEARCH
    dotaz = (dotaz or "").strip()
    if not dotaz:
        return
    _ACTIVE_WS_SEARCH = {
        "q": dotaz,
        "prefs": dict(prefs or {}),
        "page": 0,
        "mode": "",
    }


def _clear_active_ws_search():
    global _ACTIVE_WS_SEARCH
    _ACTIVE_WS_SEARCH = {}


def _active_ws_search():
    return dict(_ACTIVE_WS_SEARCH or {})


def _default_prefs_for_history():
    p = get_search_prefs()
    return {
        "category": p.get("category"),
        "sort":     p.get("sort"),
        "limit":    p.get("limit"),
        "order_by": p.get("order_by"),
        "desc":     p.get("desc"),
        "use_token": p.get("use_token", True),
    }
def _normalize_hist_entry(x):
    if isinstance(x, str):
        return {"q": x, "prefs": _default_prefs_for_history()}
    if isinstance(x, dict) and "q" in x:
        prefs = x.get("prefs") or _default_prefs_for_history()
        return {"q": str(x["q"]), "prefs": {
            "category": prefs.get("category"),
            "sort":     prefs.get("sort"),
            "limit":    int(prefs.get("limit", 25)),
            "order_by": prefs.get("order_by"),
            "desc":     bool(prefs.get("desc")),
            "use_token": bool(prefs.get("use_token", True)),
        }}
    return None
def _load_history():
    ad = _addon()
    if not ad: return []
    raw = ad.getSetting(HISTORY_KEY) or "[]"
    out = []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            for x in data:
                norm = _normalize_hist_entry(x)
                if norm: out.append(norm)
    except Exception:
        pass
    return out
def _save_history(entries):
    ad = _addon()
    if not ad: return
    ad.setSetting(HISTORY_KEY, json.dumps(entries[:HISTORY_MAX], ensure_ascii=False))
def _save_query_to_history(q, prefs):
    q = (q or "").strip()
    if not q: return
    entries = _load_history()
    cat = (prefs or {}).get("category"); srt = (prefs or {}).get("sort")
    filtered = [e for e in entries if not (e["q"] == q and (e["prefs"] or {}).get("category")==cat and (e["prefs"] or {}).get("sort")==srt)]
    filtered.insert(0, {"q": q, "prefs": _normalize_hist_entry({"q": q, "prefs": prefs})["prefs"]})
    _save_history(filtered)

# ---------- Normalizace názvů (pro ČSFD) ----------
EPISODE_PATTERNS = [
    r"\b[Ss](\d{1,2})[Ee](\d{1,2})\b",      # S03E02
    r"\b(\d{1,2})[xX](\d{1,2})\b",          # 3x07
    r"\b[Ee]pisode[ ._-]?(\d{1,3})\b",      # Episode 5 / episode-12
    r"\b[Ee][Pp]?\.?\s?(\d{1,3})\b",        # Ep 12 / E12
]





# ---------- Cache (search + csfd + fileinfo) ----------
def _search_cache_key(dotaz, page, prefs):
    payload = {
        "q": dotaz or "",
        "page": int(page or 0),
        "category": prefs.get("category"),
        "sort": prefs.get("sort"),
        "limit": int(prefs.get("limit", 25)),
        "order_by": prefs.get("order_by"),
        "desc": bool(prefs.get("desc")),
        "use_token": bool(prefs.get("use_token", True)),
    }
    s = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.md5(s.encode("utf-8")).hexdigest()

def _search_cache_get(key):
    path = _cache_file(f"search_{key}.json")
    data = _read_json(path)
    if not data: return None
    ts = data.get("_ts", 0)
    if (time.time() - ts) > SEARCH_TTL: return None
    return data

def _search_cache_set(key, rows, raw_count=None):
    path = _cache_file(f"search_{key}.json")
    data = {"_ts": int(time.time()), "rows": rows}
    if raw_count is not None:
        data["raw_count"] = int(raw_count or 0)
    _write_json(path, data)












from resources.lib.source_labels import (
    norm_text as _norm_text,
    source_audio_from_text as _source_audio_from_text,
    source_duration_text as _duration_text,
    source_fmt_bitrate as _fmt_bitrate,
    source_int_from_any as _source_int_from_any,
    source_metadata as _source_metadata,
    source_stream_line as _source_stream_line,
    source_subtitles_from_text as _source_subtitles_from_text,
)

def _lang_label(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    key = _norm_text(text).replace(" ", "")
    mapping = {
        "cze": "CZ", "ces": "CZ", "cz": "CZ", "cs": "CZ", "czech": "CZ",
        "slo": "SK", "slk": "SK", "sk": "SK", "slovak": "SK",
        "eng": "EN", "en": "EN", "english": "EN",
        "ger": "DE", "deu": "DE", "de": "DE", "german": "DE",
        "pol": "PL", "pl": "PL", "polish": "PL",
        "hun": "HU", "hu": "HU", "hungarian": "HU",
    }
    return mapping.get(key, text.upper() if len(text) <= 3 else text)


def _stream_language_summary(streams, fallback_text: str = "") -> str:
    if isinstance(streams, dict):
        streams = [streams]
    out = []
    for stream in streams or []:
        if not isinstance(stream, dict):
            continue
        for key in ("language", "lang", "locale"):
            label = _lang_label(stream.get(key))
            if label and label not in out:
                out.append(label)
    if out:
        return " + ".join(out)
    return _source_audio_from_text(fallback_text)


def _subtitle_language_summary(streams, fallback_text: str = "") -> str:
    summary = _stream_language_summary(streams, "")
    if summary:
        return summary
    return _source_subtitles_from_text(fallback_text)


def _ws_info_preview_url(ws_info: dict) -> str:
    ws_info = ws_info or {}
    for key in ("img", "preview", "thumbnail", "thumb", "poster", "image", "stripe"):
        value = str(ws_info.get(key) or "").strip()
        if value.lower().startswith(("http://", "https://")):
            return value
    return ""


def _ws_info_panel_lines(name, size_h="", size_bytes=0, mime="", meta=None):
    meta = meta or {}
    title = (meta.get("ws_info_name") or meta.get("name") or name or "Zdroj").strip()
    row = dict(meta)
    row["title"] = title
    if size_bytes:
        row["size_bytes"] = size_bytes
    elif meta.get("size"):
        row["size"] = meta.get("size")

    src_meta = _source_metadata(row)
    audio = _stream_language_summary(meta.get("audio_streams"), title) or "-"
    subtitles = (
        str(meta.get("subtitles") or meta.get("subtitle") or meta.get("subs") or "").strip()
        or _subtitle_language_summary(meta.get("subtitle_streams"), title)
        or src_meta.get("subtitles")
        or "-"
    )
    quality = src_meta.get("quality") or "-"
    duration = _duration_text(meta.get("duration") or meta.get("length"))
    file_type = str(meta.get("type") or meta.get("file_type") or mime or "").strip()

    lines = [
        f"Nazev: {title}",
        f"Jazyky: {audio}",
        f"Titulky: {subtitles}",
        f"Kvalita: {quality}",
    ]
    if src_meta.get("size") or size_h:
        lines.append(f"Velikost: {src_meta.get('size') or size_h}")
    if duration:
        lines.append(f"Delka: {duration}")
    if file_type:
        lines.append(f"Typ: {file_type}")

    warnings = []
    if _ws_info_flag_false(meta, "available"):
        warnings.append("nedostupne")
    if _ws_info_flag_true(meta, "password"):
        warnings.append("heslo")
    if _ws_info_flag_true(meta, "removed"):
        warnings.append("odstraneno")
    if _ws_info_flag_true(meta, "copyrighted"):
        warnings.append("copyright")
    if warnings:
        lines.append(f"Stav: {', '.join(warnings)}")

    video_streams = meta.get("video_streams") or []
    if isinstance(video_streams, dict):
        video_streams = [video_streams]
    video_lines = []
    for stream in video_streams:
        stream_line = _source_stream_line(stream, "video")
        if stream_line and stream_line not in video_lines:
            video_lines.append(stream_line)
    if video_lines:
        lines.append(f"Video: {' / '.join(video_lines[:2])}")

    audio_streams = meta.get("audio_streams") or []
    if isinstance(audio_streams, dict):
        audio_streams = [audio_streams]
    audio_lines = []
    for stream in audio_streams:
        stream_line = _source_stream_line(stream, "audio")
        if stream_line and stream_line not in audio_lines:
            audio_lines.append(stream_line)
    if audio_lines:
        lines.append(f"Audio: {' / '.join(audio_lines[:3])}")

    return lines


def _source_detail_lines(row: dict, score=None):
    row = row or {}
    meta = _source_metadata(row)
    lines = []

    provider_name = (row.get("provider") or "").strip()
    if provider_name:
        lines.append(f"Provider: {provider_name}")
    if meta.get("size"):
        lines.append(f"Velikost: {meta['size']}")
    file_type = str(row.get("file_type") or row.get("type") or "").strip()
    if file_type:
        lines.append(f"Typ: {file_type}")
    if meta.get("quality"):
        lines.append(f"Kvalita: {meta['quality']}")

    video_streams = row.get("video_streams") or []
    if isinstance(video_streams, dict):
        video_streams = [video_streams]
    has_video_stream = any(_source_stream_line(stream, "video") for stream in video_streams)

    if not has_video_stream and meta.get("codec"):
        lines.append(f"Kodek: {meta['codec']}")
    width = str(row.get("width") or "").strip()
    height = str(row.get("height") or "").strip()
    if not has_video_stream and width and height:
        lines.append(f"Rozliseni: {width}x{height}")
    fps = str(row.get("fps") or "").strip()
    if not has_video_stream and fps:
        lines.append(f"FPS: {fps}")
    bitrate = _fmt_bitrate(row.get("bitrate"))
    if not has_video_stream and bitrate:
        lines.append(f"Bitrate: {bitrate}")
    duration = _duration_text(row.get("duration"))
    if duration:
        lines.append(f"Delka: {duration}")
    upload_date = str(row.get("upload_date") or "").strip()
    if upload_date:
        lines.append(f"Nahrano: {upload_date}")
    if meta.get("audio"):
        lines.append(f"Zvuk: {meta['audio']}")
    if meta.get("subtitles"):
        lines.append(f"Titulky: {meta['subtitles']}")

    for stream in video_streams:
        stream_line = _source_stream_line(stream, "video")
        if stream_line:
            lines.append(f"Video stream: {stream_line}")

    audio_streams = row.get("audio_streams") or []
    if isinstance(audio_streams, dict):
        audio_streams = [audio_streams]
    for stream in audio_streams:
        stream_line = _source_stream_line(stream, "audio")
        if stream_line:
            lines.append(f"Audio stream: {stream_line}")

    available = str(row.get("available") or "").strip().lower()
    if available in ("0", "false", "no", "ne"):
        lines.append("Dostupne: Ne")
    password = str(row.get("password") or "").strip().lower()
    if password in ("1", "true", "yes", "ano"):
        lines.append("Heslo: Ano")
    copyrighted = str(row.get("copyrighted") or "").strip().lower()
    if copyrighted in ("1", "true", "yes", "ano"):
        lines.append("Copyright: Ano")
    removed = str(row.get("removed") or "").strip().lower()
    if removed in ("1", "true", "yes", "ano"):
        lines.append("Odstraneno: Ano")
        removal_reason = str(row.get("removal_reason") or "").strip()
        if removal_reason:
            lines.append(f"Duvod odstraneni: {removal_reason}")
    if score is not None:
        lines.append(f"Shoda: score {score}")
    query = (row.get("query") or "").strip()
    if query:
        lines.append(f"Dotaz: {query}")
    why = row.get("_why") or []
    if why:
        lines.append("")
        lines.append("Duvody shody:")
        lines.extend(str(x) for x in why if str(x).strip())
    return lines


def _source_badge_label(name: str, row: dict, size_h: str = "") -> str:
    row = dict(row or {})
    if size_h and not row.get("size") and not row.get("size_bytes"):
        row["size"] = size_h
    meta = _source_metadata(row)
    tags = []
    for key in ("audio", "quality", "size"):
        value = (meta.get(key) or "").strip()
        if value and value not in tags:
            color = "gray"
            low = value.lower()
            if key == "audio":
                if low.startswith("cz"):
                    color = "lime"
                elif low.startswith("sk"):
                    color = "lightgreen"
                elif low.startswith("en"):
                    color = "deepskyblue"
                else:
                    color = "silver"
            elif key == "quality":
                color = "skyblue"
            tags.append(f"[COLOR {color}][{value}][/COLOR]")
    prefix = "".join(tags)
    return f"{prefix} {name or i18n.T(30748, 'Untitled')}".strip()


def _build_webshare_video_info(name, size_h="", size_bytes=0, mime="", meta=None):
    meta = meta or {}
    title = (meta.get("ws_info_name") or meta.get("name") or name or i18n.T(30749, "source")).strip()
    lines = _ws_info_panel_lines(title, size_h=size_h, size_bytes=size_bytes, mime=mime, meta=meta)

    info = {
        "title": title,
        "sorttitle": title,
        "originaltitle": title,
        "plot": "\n".join(lines),
        "plotoutline": "\n".join(lines[:5]),
        "mediatype": "movie",
    }
    return info


def _fileinfo_cache_path(): return _cache_file("fileinfo_cache.json")
def _fileinfo_cache_load():
    data = _read_json(_fileinfo_cache_path(), default={})
    return data if isinstance(data, dict) else {}
def _fileinfo_cache_save(cache):
    now = time.time()
    keep = {k:v for k,v in cache.items() if now - v.get("_ts",0) <= FILEINFO_TTL}
    _write_json(_fileinfo_cache_path(), keep)

def _fileinfo_cache_peek(ident):
    ident = (ident or "").strip()
    if not ident:
        return {}
    cached = _fileinfo_cache_load().get(ident)
    if isinstance(cached, dict):
        if cached.get("ws_file_info") and cached.get("ws_raw_info_version") == 3:
            return cached
    return {}

def _fileinfo_cache_peek_from(cache, ident):
    ident = (ident or "").strip()
    if not ident or not isinstance(cache, dict):
        return {}
    cached = cache.get(ident)
    if isinstance(cached, dict):
        if cached.get("ws_file_info") and cached.get("ws_raw_info_version") == 3:
            return cached
    return {}

def _playdiag_cache_path(): return _cache_file("webshare_playdiag_cache.json")
def _playdiag_cache_load():
    data = _read_json(_playdiag_cache_path(), default={})
    return data if isinstance(data, dict) else {}
def _playdiag_cache_save(cache):
    now = time.time()
    keep = {k:v for k,v in cache.items() if now - v.get("_ts",0) <= FILEINFO_TTL}
    _write_json(_playdiag_cache_path(), keep)
def _playdiag_set(ident, diag):
    ident = (ident or "").strip()
    if not ident:
        return
    cache = _playdiag_cache_load()
    row = dict(diag or {})
    row["_ts"] = int(time.time())
    cache[ident] = row
    _playdiag_cache_save(cache)
def _playdiag_get(ident):
    ident = (ident or "").strip()
    if not ident:
        return {}
    row = _playdiag_cache_load().get(ident) or {}
    return row if isinstance(row, dict) else {}

def _xml_simple_dict(root):
    out = {}
    try:
        for child in list(root):
            if list(child):
                continue
            out[child.tag] = (child.text or "").strip()
    except Exception:
        pass
    return out

def _probe_play_url(url: str):
    return {
        "probe_skipped": "1",
        "probe_reason": "disabled_to_avoid_kodi_freeze",
        "probe_url": (url or "")[:500],
    }

_ws_xml_streams = source_common.xml_streams

_WS_INFO_HIDDEN_KEYS = {
    "size", "width", "height", "format", "fps", "bitrate", "bitrade", "length",
    "stripe", "name",
}


def _ws_info_value_lines(key, value):
    lines = []
    if str(key or "").strip().lower() in _WS_INFO_HIDDEN_KEYS:
        return lines
    if value in (None, "", [], {}):
        return lines
    if isinstance(value, list):
        lines.append(f"{key}:")
        for idx, item in enumerate(value, 1):
            if isinstance(item, dict):
                child_lines = []
                for child_key in sorted(item.keys()):
                    if str(child_key or "").strip().lower() in _WS_INFO_HIDDEN_KEYS:
                        continue
                    child_val = item.get(child_key)
                    if child_val not in (None, "", [], {}):
                        child_lines.append(f"    {child_key}: {child_val}")
                if child_lines:
                    lines.append(f"  stream {idx}:")
                    lines.extend(child_lines)
            else:
                lines.append(f"  {idx}: {item}")
        if len(lines) == 1:
            return []
        return lines
    if isinstance(value, dict):
        lines.append(f"{key}:")
        for child_key in sorted(value.keys()):
            if str(child_key or "").strip().lower() in _WS_INFO_HIDDEN_KEYS:
                continue
            child_val = value.get(child_key)
            if child_val not in (None, "", [], {}):
                lines.append(f"  {child_key}: {child_val}")
        if len(lines) == 1:
            return []
        return lines
    lines.append(f"{key}: {value}")
    return lines

def _ws_info_debug_lines(ws_info: dict):
    ws_info = ws_info or {}
    if not ws_info:
        return ["file_info: bez dat"]

    internal = {
        "_ts", "ws_raw_info_version", "ws_file_info",
        "ws_info_name", "size_bytes", "file_type",
    }
    preferred = [
        "status", "ident",
        "mime", "type", "duration", "available", "password",
        "removed", "removed_at", "removal_reason", "copyrighted",
        "video_streams", "audio_streams",
    ]

    lines = []
    used = set()
    for key in preferred:
        if key in ws_info and key not in internal:
            lines.extend(_ws_info_value_lines(key, ws_info.get(key)))
            used.add(key)

    for key in sorted(k for k in ws_info.keys() if k not in used and k not in internal):
        lines.extend(_ws_info_value_lines(key, ws_info.get(key)))

    return lines or ["file_info: bez dat"]

def _ws_info_flag_true(ws_info: dict, key: str) -> bool:
    value = str((ws_info or {}).get(key) or "").strip().lower()
    return value in ("1", "true", "yes", "ano")

def _ws_info_flag_false(ws_info: dict, key: str) -> bool:
    value = str((ws_info or {}).get(key) or "").strip().lower()
    return value in ("0", "false", "no", "ne")

def _ws_info_exclude_reason(ws_info: dict) -> str:
    ws_info = ws_info or {}
    status = str(ws_info.get("status") or "").strip()
    if status and status.upper() != "OK":
        return f"status={status}"
    if _ws_info_flag_false(ws_info, "available"):
        return "available=0"
    if _ws_info_flag_true(ws_info, "removed"):
        return "removed=1"
    if _ws_info_flag_true(ws_info, "copyrighted"):
        return "copyrighted=1"
    return ""

def get_file_info_cached(ident, token):
    cache = _fileinfo_cache_load()
    cached = cache.get(ident)
    if isinstance(cached, dict):
        if cached.get("ws_file_info") and cached.get("ws_raw_info_version") == 3:
            return cached
    try:
        data = {"ident": ident}
        if token: data["wst"] = token
        root = ET.fromstring(api("file_info", data, timeout=FILEINFO_TIMEOUT).content)
        def text(name):
            return (root.findtext(name) or "").strip()

        info = {
            "ws_file_info": True,
            "ws_raw_info_version": 3,
            "ident": ident,
            "_ts": int(time.time()),
        }
        for child in list(root):
            if list(child):
                continue
            info[child.tag] = (child.text or "").strip()

        info["ws_info_name"] = text("name")
        info["size_bytes"] = _source_int_from_any(text("size"), 0)
        info["file_type"] = text("type") or text("mime")
        info["video_streams"] = _ws_xml_streams(root, "video")
        info["audio_streams"] = _ws_xml_streams(root, "audio")
        info["subtitle_streams"] = _ws_xml_streams(root, "subtitles") or _ws_xml_streams(root, "subtitle")
        cache[ident] = info
        _fileinfo_cache_save(cache)
        return info
    except Exception:
        return None




# ---------- Podezřelé názvy → sáhnout na file_info ----------
VIDEO_EXTS = (".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".m4v", ".ts", ".mpeg", ".mpg")
SUSPECT_RE = re.compile(r'^[A-Za-z0-9._-]{8,}$')
def _looks_suspicious(name: str) -> bool:
    base = name.rsplit("/",1)[-1].rsplit("\\",1)[-1]
    low = base.lower()
    has_video_ext = any(low.endswith(ext) for ext in VIDEO_EXTS)
    return (not has_video_ext) or bool(SUSPECT_RE.match(os.path.splitext(low)[0]))

# ---------- Heuristiky kvality/jazyka/roku/ext ----------


# ---------- Menu / router ----------
def open_menu(addon_handle, base_path):
    entries = [
        (i18n.T(31000, "New search"), {"action": "film.search_new"}, icon_path('add')),
        (i18n.T(31001, "Search history"), {"action": "film.history"}, icon_path('history')),
        (i18n.T(31002, "Newest on WebShare"), {"action": "film.latest"}, icon_path('box')),
        (i18n.T(31003, "Largest on WebShare"), {"action": "film.biggest"}, icon_path('weight')),
    ]
    for label, q, icon_p in entries:
        url = build_url(base_path, q)
        li = xbmcgui.ListItem(label)
        li.setArt({'icon': icon_p, 'thumb': icon_p, 'poster': icon_p, 'fanart': icon_p})
        xbmcplugin.addDirectoryItem(addon_handle, url, li, isFolder=True)
    xbmcplugin.endOfDirectory(addon_handle)

def search_ui(token, addon_handle, base_path):
    log("SEARCH_UI HIT - direct results")

    dotaz = xbmcgui.Dialog().input(i18n.T(31004, "Enter search query"))
    if not dotaz:
        xbmcplugin.endOfDirectory(addon_handle, succeeded=True)
        return

    prefs = get_search_prefs()
    _save_query_to_history(dotaz, prefs)
    _remember_active_ws_search(dotaz, prefs)

    show_results(
        token,
        addon_handle,
        base_path,
        dotaz,
        page=0,
        prefs=prefs,
        mode="",
        force_refresh=True
    )
    return


def action_film_back_to_search(addon_handle=None, base_path=None):
    """
    Nahradí aktuální listing podmenu 'Hledej film',
    aby '..' vedlo opravdu o úroveň výš (ne zpět na poslední stránku výsledků).
    """
    try:
        home_url = build_url(base_path or sys.argv[0], {"action": "film.search"})
        xbmc.executebuiltin(f'Container.Update("{home_url}",replace)')
        xbmcplugin.endOfDirectory(addon_handle, succeeded=True, updateListing=False, cacheToDisc=False)
    except Exception as e:
        xbmc.log(f"[Ikarus] action_film_back_to_search failed: {e}", xbmc.LOGWARNING)
        try:
            xbmcplugin.endOfDirectory(addon_handle, succeeded=False, updateListing=False, cacheToDisc=False)
        except Exception:
            pass


def show_history(token, addon_handle, base_path):
    hist = _load_history()
    # 1. položka: Vymazat historii (JAKO SLOŽKA)
    clear_url = build_url(base_path, {"action": "film.history_clear"})
    li_clear = xbmcgui.ListItem(i18n.T(31005, "[Clear history]"))
    ic = icon_path('history')
    li_clear.setArt({'icon': ic, 'thumb': ic, 'poster': ic, 'fanart': ic})
    xbmcplugin.addDirectoryItem(addon_handle, clear_url, li_clear, isFolder=True)

    if not hist:
        xbmcgui.Dialog().notification(
            i18n.T(30900, "Ikarus"),
            i18n.T(31006, "History is empty."),
            xbmcgui.NOTIFICATION_INFO,
            2000,
        )
 
    for entry in hist:
        q = entry["q"]
        p = entry.get("prefs") or _default_prefs_for_history()
        cat = p.get("category") or "vše"
        srt = p.get("sort") or "relevance"
        label = f"{q}  [COLOR gray]({cat}, {srt})[/COLOR]"
        url = build_url(base_path, {
            "action":"film.page","q":q,"page":0,
            "limit": int(p.get("limit",25)),
            "cat":   p.get("category") or "",
            "sort":  p.get("sort") or "",
            "ob":    p.get("order_by") or "",
            "desc":  "1" if p.get("desc") else "0",
            "wt":    "1" if p.get("use_token", True) else "0",
        })
        li = xbmcgui.ListItem(label)
        ic = icon_path('search')
        li.setArt({'icon': ic, 'thumb': ic, 'poster': ic, 'fanart': ic})
        xbmcplugin.addDirectoryItem(addon_handle, url, li, isFolder=True)
    xbmcplugin.endOfDirectory(addon_handle)

def handle_router(token, addon_handle, base_path, params):
    action = params.get("action")

    if action == "film.search":
        _clear_active_ws_search()
        open_menu(addon_handle, base_path); return
    if action == "film.search_new":
        active = _active_ws_search()
        if active.get("q"):
            show_results(
                token,
                addon_handle,
                base_path,
                active.get("q") or "",
                page=int(active.get("page") or 0),
                prefs=active.get("prefs") or get_search_prefs(),
                mode=active.get("mode") or "",
                force_refresh=False,
            )
            return
        search_ui(token, addon_handle, base_path); return
    if action == "film.history":
        show_history(token, addon_handle, base_path); return
    if action == "film.history_clear":
        # bez potvrzování – přímo smaž a vrať listing
        try:
            _save_history([])
            try:
                # druhý pojistný zápis do settings (pro případ rozjetých verzí)
                ad = xbmcaddon.Addon()
                ad.setSetting(HISTORY_KEY, "[]")
            except Exception:
                pass
            xbmcgui.Dialog().notification(
                i18n.T(30900, "Ikarus"),
                i18n.T(31007, "History cleared."),
                xbmcgui.NOTIFICATION_INFO,
                1200,
            )
        except Exception:
            pass
        # Okamžitě vyrenderuj prázdnou historii (vrátí endOfDirectory)
        show_history(token, addon_handle, base_path); return

    if action == "film.latest":
        prefs = get_search_prefs(); prefs = dict(prefs)
        prefs["sort"] = "recent"
        show_results(token, addon_handle, base_path, "*", page=0, prefs=prefs, mode="latest", force_refresh=True)
        return

    if action == "film.biggest":
        prefs = get_search_prefs(); prefs = dict(prefs)
        prefs["sort"] = "largest"
        show_results(token, addon_handle, base_path, "*", page=0, prefs=prefs, mode="biggest", force_refresh=True)
        return

    if action == "film.page":
        dotaz=params.get("q",""); page=int(params.get("page","0"))
        prefs={
            "limit": int(params.get("limit","25")),
            "category": params.get("cat") or None,
            "sort": params.get("sort") or None,
            "order_by": params.get("ob") or None,
            "desc": (params.get("desc","0")=="1"),
            "use_token": (params.get("wt","1")=="1"),
        }
        force = (params.get("force","0")=="1")
        show_results(token, addon_handle, base_path, dotaz, page=page, prefs=prefs, mode=params.get("mode") or "", force_refresh=force); return
    if action == "film.detail":
        ident=params.get("id")
        if ident: show_detail(token, ident); return
    if action == "film.back_to_search":
        return action_film_back_to_search(addon_handle=addon_handle, base_path=base_path)

    elif action in ("film.play", "play"):
        play_start = time.time()
        ident = params.get("id") or params.get("ident") or params.get("file_id") or ""
        display_title = (
            params.get("play_title")
            or params.get("title")
            or params.get("name")
            or params.get("file")
            or ident
        )
        display_title = (display_title or ident or "Zdroj").strip()
        subtitle_title = (
            params.get("subtitle_query")
            or params.get("original_title")
            or params.get("subtitle_title")
            or params.get("file")
            or ""
        ).strip()
        year = (params.get("year") or "").strip()
        silent_fail = (params.get("silent") or params.get("auto_next") or "").strip() == "1"
        if not ident:
            _manual_log_ws("PLAY_FAIL", xbmc.LOGWARNING, reason="missing ident", title=display_title, ms=_manual_elapsed_ms(play_start))
            try:
                import xbmcgui
                if not silent_fail:
                    xbmcgui.Dialog().notification(
                        i18n.T(30900, "Ikarus"),
                        i18n.T(31018, "Missing file identifier."),
                        xbmcgui.NOTIFICATION_ERROR,
                        2500,
                    )
            except Exception:
                pass
            playback_utils.fail_source_playback(
                addon_handle,
                provider="webshare",
                reason="missing ident",
                title=display_title,
                action="film.play",
            )
            return

        # 1) Vytáhni (a případně obnov) token z nastavení
        try:
            import xbmcaddon, nastaveni
            addon = xbmcaddon.Addon()
            user  = addon.getSetting("username") or ""
            pwd   = addon.getSetting("password") or ""
            token = nastaveni.ziskej_token(user, pwd)
        except Exception:
            token = None

        # 2) Přes resolver si nech vrátit finální stream URL
        resolve_start = time.time()
        final_url = _ws_resolve_play_url(ident, token)
        _manual_log_ws("RESOLVE", title=display_title, id=ident, ok=bool(final_url), ms=_manual_elapsed_ms(resolve_start))
        xbmc.log(
            f"[Ikarus/OLD RESUME] ident={ident}, final_url={source_common.redact_url_for_log(final_url)}",
            xbmc.LOGINFO,
        )

        # 3) Pusť jen když máme reálný HTTP/HTTPS stream; jinak jen oznámení
        import xbmcplugin, xbmcgui
        if final_url and str(final_url).lower().startswith(("http://", "https://")):
            diag = _playdiag_get(ident)
            diag["play_handler_result"] = "resolved_to_kodi"
            diag["play_handler_url_is_http"] = "1"
            _playdiag_set(ident, diag)
            li = playback_utils.make_video_list_item(
                display_title,
                final_url,
                subtitle_title=subtitle_title,
                year=year,
                mimetype=playback_utils.guess_stream_mimetype(final_url),
            )
            xbmc.log(
                f"[Ikarus/SubtitleSearch] title={display_title!r}, originaltitle={subtitle_title!r}, year={year!r}",
                xbmc.LOGINFO,
            )
            _manual_log_ws("PLAY", title=display_title, id=ident, ms=_manual_elapsed_ms(play_start), url=final_url)
            xbmcplugin.setResolvedUrl(addon_handle, True, li)
        else:
            diag = _playdiag_get(ident)
            diag["play_handler_result"] = "no_http_url"
            diag["play_handler_url_is_http"] = "0"
            diag["final_url"] = (final_url or "")[:500]
            _playdiag_set(ident, diag)
            if silent_fail:
                _manual_log_ws("PLAY_FAIL", xbmc.LOGWARNING, reason="no http url", title=display_title, id=ident, ms=_manual_elapsed_ms(play_start), final_url=final_url or "")
                playback_utils.fail_source_playback(
                    addon_handle,
                    provider="webshare",
                    reason="no http url",
                    title=display_title,
                    tmdb_id=params.get("tmdb_id") or "",
                    source_id=ident,
                    action="film.play",
                )
                return
            # zdroj je nedostupný -> oznámení uživateli
            try:
                xbmcgui.Dialog().notification(
                    i18n.T(30900, "Ikarus"),
                    i18n.T(31019, "Source is unavailable, try another one."),
                    xbmcgui.NOTIFICATION_ERROR,
                    3000
                )
            except Exception:
                pass

            _manual_log_ws("PLAY_FAIL", xbmc.LOGWARNING, reason="no http url", title=display_title, id=ident, ms=_manual_elapsed_ms(play_start), final_url=final_url or "")
            playback_utils.fail_source_playback(
                addon_handle,
                provider="webshare",
                reason="no http url",
                title=display_title,
                tmdb_id=params.get("tmdb_id") or "",
                source_id=ident,
                action="film.play",
            )
        return

    try:
        xbmcplugin.endOfDirectory(addon_handle, succeeded=False, updateListing=False, cacheToDisc=False)
    except Exception:
        pass


def _api_error_text(xml_root):
    return f"{xml_root.findtext('code') or ''} {xml_root.findtext('message') or ''}".strip()

# ---------- Vykreslení výsledků ----------
def _render_rows(addon_handle, base_path, rows, dotaz, page, prefs, mode, has_prev=False, has_next=False):
    try:
        xbmcplugin.setContent(addon_handle, "movies")
    except Exception:
        pass

    # 0) VLOŽENÝ PRVNÍ ŘÁDEK → zpět do podmenu "Hledej film" (REPLACE)
    back_url = build_url(base_path, {"action": "film.back_to_search"})
    li_home = xbmcgui.ListItem(i18n.T(31008, "Back: Search movie"))
    li_home.setArt({'icon': "DefaultFolderBack.png"})
    # isFolder=False => klik spustí RunPlugin() a my uvnitř uděláme Container.Update(..., replace)
    xbmcplugin.addDirectoryItem(addon_handle, back_url, li_home, isFolder=False)

    # 1) Aktualizovat z webu (zachováno)
    refresh_url = build_url(base_path, {
        "action":"film.page","q":dotaz,"page":page,
        "limit": prefs["limit"],
        "cat":   prefs.get("category") or "",
        "sort":  prefs.get("sort") or "",
        "ob":    prefs.get("order_by") or "",
        "desc":  "1" if prefs.get("desc") else "0",
        "wt":    "1" if prefs.get("use_token", True) else "0",
        "mode":  mode or "",
        "force": "1",
    })
    li_refresh = xbmcgui.ListItem(
        i18n.T(31009, "[Refresh from web]  [COLOR gray](page {page})[/COLOR]").format(page=page + 1)
    )
    ic = icon_path('search')
    li_refresh.setArt({'icon': ic, 'thumb': ic, 'poster': ic, 'fanart': ic})
    xbmcplugin.addDirectoryItem(addon_handle, refresh_url, li_refresh, isFolder=True)

    # 2) Předchozí stránka (zachováno)
    if has_prev:
        prev_url = build_url(base_path, {
            "action":"film.page","q":dotaz,"page":page-1,
            "limit": prefs["limit"],
            "cat":   prefs.get("category") or "",
            "sort":  prefs.get("sort") or "",
            "ob":    prefs.get("order_by") or "",
            "desc":  "1" if prefs.get("desc") else "0",
            "wt":    "1" if prefs.get("use_token", True) else "0",
            "mode":  mode or ""
        })
        li_prev = xbmcgui.ListItem(i18n.T(31010, "<< Previous page"))
        li_prev.setArt({'icon': "DefaultFolder.png"})
        xbmcplugin.addDirectoryItem(addon_handle, prev_url, li_prev, isFolder=True)

    # 3) Samotné položky souborů (zachováno)
    for r in rows:
        ident = r.get("id","")
        name = r.get("name") or i18n.T(30748, "Untitled")
        size  = r.get("size_h","?")
        size_bytes = r.get("size") or 0
        poster_url = r.get("poster","")
        plot  = r.get("plot","")
        mime = r.get("mime", "")
        meta = r.get("ws_info") or {}
        if not meta and isinstance(r.get("meta"), dict) and r.get("meta", {}).get("ws_file_info"):
            meta = r.get("meta") or {}
        search_img = str(r.get("img") or "").strip()
        if search_img:
            meta = dict(meta or {})
            meta["img"] = search_img
        play_diag = _playdiag_get(ident)
        if play_diag:
            meta = dict(meta)
            meta["play_check"] = play_diag

        label_meta = dict(meta or {})
        label_meta["name"] = name
        if size_bytes:
            label_meta["size_bytes"] = size_bytes
        label = _source_badge_label(name, label_meta, size)
        preview_url = poster_url or _ws_info_preview_url(meta)
        play_url = build_url(base_path, {"action":"film.play","id": ident,"name": name,"poster": preview_url or ""})
        li = xbmcgui.ListItem(label=label)
        li.setProperty('IsPlayable','true')
        info = _build_webshare_video_info(name, size, size_bytes, mime, meta)
        li.setInfo("video", info)
        art = {"icon": "DefaultVideo.png"}
        if preview_url:
            stripe_url = str(meta.get("stripe") or "").strip()
            is_search_img = bool(search_img and preview_url == search_img)
            is_stripe_preview = bool(stripe_url and preview_url == stripe_url)
            art["icon"] = preview_url
            art["thumb"] = preview_url
            if is_search_img:
                art["poster"] = preview_url
            elif not is_stripe_preview:
                art["landscape"] = preview_url
                art["banner"] = preview_url
                art["poster"] = preview_url
                art["fanart"] = preview_url
        li.setArt(art)

        cm = [(i18n.T(31011, "Show detail"), f'RunPlugin({build_url(base_path, {"action":"film.detail","id":ident})})')]
        li.addContextMenuItems(cm, replaceItems=False)
        xbmcplugin.addDirectoryItem(addon_handle, play_url, li, isFolder=False)

    # 4) Další stránka (zachováno)
    if has_next:
        next_url = build_url(base_path, {
            "action":"film.page","q":dotaz,"page":page+1,
            "limit": prefs["limit"],
            "cat":   prefs.get("category") or "",
            "sort":  prefs.get("sort") or "",
            "ob":    prefs.get("order_by") or "",
            "desc":  "1" if prefs.get("desc") else "0",
            "wt":    "1" if prefs.get("use_token", True) else "0",
            "mode":  mode or ""
        })
        li_next = xbmcgui.ListItem(i18n.T(31012, "Next page >>"))
        li_next.setArt({'icon': "DefaultFolder.png"})
        xbmcplugin.addDirectoryItem(addon_handle, next_url, li_next, isFolder=True)

    xbmcplugin.endOfDirectory(addon_handle, succeeded=True)

# ---------- Výsledky webshare ----------
def show_results(token, addon_handle, base_path, dotaz, page=0, prefs=None, mode="", force_refresh=False):
    start = time.time()
    prefs = prefs or get_search_prefs()
    limit = prefs["limit"]; offset = page * limit

    # WS search často nebere "*" – když chceme "výpis", použij bezpečný fallback
    dq = (dotaz or "").strip()
    if dq == "*" or dq == "":
        dotaz = "a"   # nejběžnější token → skoro vždy něco vrátí


    cache_key = _search_cache_key(dotaz, page, prefs)
    cached = None if force_refresh else _search_cache_get(cache_key)

    if cached:
        cached_rows = cached.get("rows", [])
        fileinfo_cache = _fileinfo_cache_load()
        rows = []
        changed = False
        for r in cached_rows:
            if not isinstance(r, dict):
                continue
            ws_info = r.get("ws_info") or _fileinfo_cache_peek_from(fileinfo_cache, r.get("id") or "") or {}
            if ws_info:
                if fi_name := ws_info.get("name"):
                    if _looks_suspicious(r.get("name") or ""):
                        r["name"] = fi_name
                if ws_info.get("size") and not r.get("size_h"):
                    r["size_h"] = fmt_size(ws_info["size"])
                if ws_info.get("size_bytes") and not r.get("size"):
                    r["size"] = ws_info["size_bytes"]
                r["mime"] = ws_info.get("mime", r.get("mime", ""))
                r["ws_info"] = ws_info
            if _ws_info_exclude_reason(ws_info):
                changed = True
                continue
            r.pop("meta", None)
            rows.append(r)
        if changed:
            _search_cache_set(cache_key, rows, cached.get("raw_count", len(cached_rows)))
        has_prev = page > 0
        has_next = (int(cached.get("raw_count", len(cached_rows)) or 0) == limit)
        _manual_log_ws("CACHE", q=dotaz, page=page, rows=len(rows), raw=len(cached_rows), next=has_next, ms=_manual_elapsed_ms(start))
        _render_rows(addon_handle, base_path, rows, dotaz, page, prefs, mode, has_prev=has_prev, has_next=has_next)
        return

    data = {"what": dotaz, "offset": offset, "limit": limit}
    if prefs.get("category"): data["category"]=prefs["category"]
    if prefs.get("sort"):     data["sort"]=prefs["sort"]
    if prefs.get("order_by"): data["order_by"]=prefs["order_by"]
    if prefs.get("desc"):     data["desc"]="true"
    if prefs.get("use_token", True) and token: data["wst"]=token

    try:
        log(f"WS search data={data}")
        root = ET.fromstring(api("search", data, timeout=SEARCH_TIMEOUT).content)
        if (root.findtext("status") or "OK") != "OK":
            _manual_log_ws("SEARCH_FAIL", xbmc.LOGERROR, q=dotaz, page=page, ms=_manual_elapsed_ms(start), error=_api_error_text(root))
            xbmcgui.Dialog().ok(
                i18n.T(30900, "Ikarus"),
                i18n.T(31013, "API /search error: {error}").format(error=_api_error_text(root)),
            )
            xbmcplugin.endOfDirectory(addon_handle, succeeded=False); return
        log(f"WS search status={root.findtext('status')} files={len(root.findall('file'))}")
    except Exception as e:
        _manual_log_ws("SEARCH_FAIL", xbmc.LOGERROR, q=dotaz, page=page, ms=_manual_elapsed_ms(start), error=repr(e))
        err(f"search error: {e}"); xbmcplugin.endOfDirectory(addon_handle, succeeded=False); return

    files = root.findall("file")
    fileinfo_cache = _fileinfo_cache_load()
    rows = []
    for f in files:
        ident = f.findtext("ident") or ""
        name = f.findtext("name") or i18n.T(30748, "Untitled")
        size_raw = f.findtext("size") or "0"
        img = (f.findtext("img") or "").strip()
        mime = ""
        ws_info = _fileinfo_cache_peek_from(fileinfo_cache, ident) or {}

        if ws_info:
            if _ws_info_exclude_reason(ws_info):
                continue
            if ws_info.get("name") and _looks_suspicious(name):
                name = ws_info["name"]
            if ws_info.get("size"):
                size_raw = ws_info["size"]
            mime = ws_info.get("mime","") or mime

        size_h = fmt_size(size_raw)
        try:
            size_int = int(size_raw) if str(size_raw).isdigit() else 0
        except Exception:
            size_int = 0

        rows.append({
            "id": ident,
            "name": name,
            "size": size_int,
            "size_h": size_h,
            "poster": "",
            "plot": "",
            "mime": mime,
            "ws_info": ws_info,
            "img": img,
            "query": dotaz
        })

    _search_cache_set(cache_key, rows, len(files))

    has_prev = page > 0
    has_next = (len(files) == limit)
    _manual_log_ws("SEARCH", q=dotaz, page=page, rows=len(rows), raw=len(files), next=has_next, ms=_manual_elapsed_ms(start))
    _render_rows(addon_handle, base_path, rows, dotaz, page, prefs, mode, has_prev=has_prev, has_next=has_next)

def show_detail(token, ident):
    try:
        data={"ident": ident}
        if token: data["wst"]=token
        root = ET.fromstring(api("file_info", data).content)
        if (root.findtext("status") or "OK") != "OK":
            xbmcgui.Dialog().ok(
                i18n.T(30900, "Ikarus"),
                i18n.T(31014, "API /file_info error: {error}").format(error=_api_error_text(root)),
            )
            return
        name = root.findtext("name") or i18n.T(30748, "Untitled")
        size=fmt_size(root.findtext("size") or "0")
        mime=root.findtext("mime") or "-"
        link=f"https://webshare.cz/file/{ident}/download"
        text = i18n.T(
            31015,
            "Name:\n  {name}\n\nSize:\n  {size}\n\nMIME:\n  {mime}\n\nLink:\n  {link}\n",
        ).format(name=name, size=size, mime=mime, link=link)
        xbmcgui.Dialog().textviewer(i18n.T(31016, "File detail"), text)
    except Exception as e:
        err(f"file_info error: {e}")
        xbmcgui.Dialog().ok(
            i18n.T(30900, "Ikarus"),
            i18n.T(31017, "Could not load detail: {error}").format(error=e),
        )

def _ws_resolve_play_url(ident: str, token: str = None) -> str:
    """
    Vrátí finální streamovací URL pro daný ident.
    Postup:
      1) api('file_link') → <link>
      2) api('file_info') → <stream_link> nebo <download_link>
      3) pokud je to stále /download?... přidej &inline=1 (některé CDN tím pošlou přímo data)
      4) poslední záchrana: vrať pluginovou URL zpět na náš handler (nový běh může získat nový token)
    """
    def _file_link_try():
        try:
            data = {"ident": ident}
            if token:
                data["wst"] = token
            resp = api("file_link", data).content
            root = ET.fromstring(resp)
            diag = {
                "stage": "file_link",
                "file_link": _xml_simple_dict(root),
            }
            _playdiag_set(ident, diag)
            if (root.findtext("status") or "").upper() == "OK":
                link = (root.findtext("link") or "").strip()
                if link:
                    diag["file_link_ok"] = "1"
                    diag["resolved_url"] = link[:500]
                    _playdiag_set(ident, diag)
                    return link
        except Exception as e:
            _playdiag_set(ident, {"stage": "file_link", "file_link_exception": repr(e)})
        return ""

    # 1) zkusi file_link
    link = _file_link_try()
    if link:
        # někteří poskytovatelé vrací /download – přidej inline, ať je to stream
        if "/download" in link and "inline=1" not in link:
            sep = "&" if "?" in link else "?"
            link = f"{link}{sep}inline=1"
        diag = _playdiag_get(ident)
        diag["stage"] = "resolved_file_link"
        diag["resolved_url"] = link[:500]
        diag["probe"] = _probe_play_url(link)
        _playdiag_set(ident, diag)
        return link

    # 2) zkus file_info
    try:
        data = {"ident": ident}
        if token:
            data["wst"] = token
        root = ET.fromstring(api("file_info", data).content)
        # některé implementace vrací <stream_link>, jindy <download_link>
        diag = _playdiag_get(ident)
        diag["stage"] = "file_info_link"
        diag["file_info_link"] = _xml_simple_dict(root)
        _playdiag_set(ident, diag)
        for tag in ("stream_link", "download_link", "link"):
            cand = (root.findtext(tag) or "").strip()
            if cand:
                link = cand
                break
        else:
            link = ""
        if link:
            if "/download" in link and "inline=1" not in link:
                sep = "&" if "?" in link else "?"
                link = f"{link}{sep}inline=1"
            diag["resolved_url"] = link[:500]
            diag["probe"] = _probe_play_url(link)
            _playdiag_set(ident, diag)
            return link
    except Exception as e:
        diag = _playdiag_get(ident)
        diag["stage"] = "file_info_link"
        diag["file_info_link_exception"] = repr(e)
        _playdiag_set(ident, diag)

    # 3) poslední pokus – ještě jednou file_link; pokud nic, vrať pluginovou URL zpět
    link = _file_link_try()
    if link:
        if "/download" in link and "inline=1" not in link:
            sep = "&" if "?" in link else "?"
            link = f"{link}{sep}inline=1"
        diag = _playdiag_get(ident)
        diag["stage"] = "resolved_file_link_retry"
        diag["resolved_url"] = link[:500]
        diag["probe"] = _probe_play_url(link)
        _playdiag_set(ident, diag)
        return link

    # 4) fallback – pluginová URL zpět na přehrávací handler (další běh může získat fresh token)
    import urllib.parse, sys
    _playdiag_set(ident, {"stage": "unresolved", "result": "no_http_link"})
    return sys.argv[0] + "?" + urllib.parse.urlencode({
        "action": "film.play",
        "id": ident,
        "ident": ident,
        "retry": "1"
    })




