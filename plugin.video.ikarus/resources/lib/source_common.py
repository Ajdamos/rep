# -*- coding: utf-8 -*-
import json
import re
import urllib.parse
import urllib.request

import requests
import xbmc
import xbmcvfs


MEDIA_EXTENSIONS = (".mp4", ".m3u8", ".ts", ".mkv", ".avi", ".webm")
SENSITIVE_HEADER_NAMES = {
    "authorization",
    "cookie",
    "proxy-authorization",
    "set-cookie",
    "x-api-key",
}


def sanitize_url(url: str) -> str:
    try:
        p = urllib.parse.urlsplit(url)
        path = urllib.parse.quote(p.path, safe="/%")
        query = urllib.parse.quote(p.query, safe="=&%")
        frag = urllib.parse.quote(p.fragment, safe="%")
        return urllib.parse.urlunsplit((p.scheme, p.netloc, path, query, frag))
    except Exception:
        return url


def redact_url_for_log(url: str) -> str:
    text = str(url or "").strip()
    if not text:
        return ""
    text = text.split("|", 1)[0]
    try:
        parsed = urllib.parse.urlsplit(text)
        if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
            return "<non-http-url>"
        host = parsed.hostname
        try:
            if parsed.port:
                host += f":{parsed.port}"
        except ValueError:
            pass
        segments = [part for part in parsed.path.split("/") if part]
        suffix = ""
        if segments:
            match = re.search(r"(\.[A-Za-z0-9]{1,8})$", segments[-1])
            if match:
                suffix = match.group(1).lower()
        path_hint = f"/<path:{len(segments)}>{suffix}" if segments else ""
        query_hint = "?<redacted>" if parsed.query else ""
        return f"{parsed.scheme.lower()}://{host}{path_hint}{query_hint}"
    except Exception:
        return "<invalid-url>"


def redact_headers_for_log(headers: dict) -> dict:
    out = {}
    for key, value in (headers or {}).items():
        name = str(key or "")
        lowered = name.strip().lower()
        if lowered in SENSITIVE_HEADER_NAMES or "token" in lowered or "secret" in lowered:
            out[name] = "***"
        elif lowered in ("referer", "origin"):
            out[name] = redact_url_for_log(value)
        else:
            out[name] = str(value or "")[:160]
    return out


def common_headers(accept: str = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8") -> dict:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": accept,
        "Accept-Language": "cs-CZ,cs;q=0.9,en;q=0.8",
        "Accept-Encoding": "identity",
        "Connection": "close",
    }


def decode_body(raw) -> str:
    try:
        return raw.decode("utf-8", errors="ignore")
    except Exception:
        return raw.decode(errors="ignore")


def dump_text_to_temp(filename: str, text: str, log_prefix: str = "[IKARUS][SKT]"):
    try:
        path = xbmcvfs.translatePath("special://temp/" + filename)
        file_obj = xbmcvfs.File(path, "w")
        file_obj.write(text or "")
        file_obj.close()
        xbmc.log(f"{log_prefix} dumped: {path}", xbmc.LOGWARNING)
    except Exception as exc:
        xbmc.log(f"{log_prefix} dump failed: {exc}", xbmc.LOGERROR)


def xml_streams(root, parent_tag: str):
    out = []
    try:
        parent = root.find(parent_tag)
        if parent is None:
            return out
        for stream in parent.findall("stream"):
            row = {}
            for child in list(stream):
                row[child.tag] = (child.text or "").strip()
            if row:
                out.append(row)
    except Exception:
        pass
    return out


def safe_filename(name: str, fallback: str = "zdroj", max_length: int = 180) -> str:
    name = (name or "").strip()
    if not name:
        name = fallback
    name = re.sub(r'[\\/:*?"<>|]+', "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:max_length] or fallback


def http_get(url: str, opener, timeout: int = 25, referer: str = None) -> str:
    url = sanitize_url(url)
    headers = common_headers()
    if referer:
        headers["Referer"] = referer

    req = urllib.request.Request(url, headers=headers, method="GET")
    with opener.open(req, timeout=timeout) as resp:
        return decode_body(resp.read())


def http_post(url: str, data: dict, opener, timeout: int = 25, referer: str = None) -> str:
    url = sanitize_url(url)
    body = urllib.parse.urlencode(data or {}).encode("utf-8")
    headers = common_headers()
    headers["Content-Type"] = "application/x-www-form-urlencoded"
    if referer:
        headers["Referer"] = referer

    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with opener.open(req, timeout=timeout) as resp:
        return decode_body(resp.read())


def http_get_json(url: str, opener, timeout: int = 25, referer: str = None, origin: str = None,
                  default_referer: str = "", default_origin: str = "") -> dict:
    url = sanitize_url(url)
    headers = common_headers("application/json,*/*")
    if referer or default_referer:
        headers["Referer"] = referer or default_referer
    if origin or default_origin:
        headers["Origin"] = origin or default_origin

    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with opener.open(req, timeout=timeout) as resp:
            txt = decode_body(resp.read())
        return json.loads(txt) if txt else {}
    except Exception:
        return {}


def is_direct_media_url(url: str) -> bool:
    if not url:
        return False
    low = str(url).lower()
    return any(ext in low for ext in MEDIA_EXTENSIONS)


def _registered_domain(host: str) -> str:
    parts = [p for p in str(host or "").split(".") if p]
    return ".".join(parts[-2:]) if len(parts) >= 2 else str(host or "")


def cookie_header_for_url(cookiejar, url: str, relaxed_domain: bool = False) -> str:
    try:
        u = urllib.parse.urlsplit(url)
        host = (u.hostname or "").lower()
        path = u.path or "/"
        host_rd = _registered_domain(host)

        cookies = []
        for cookie in cookiejar:
            dom = (cookie.domain or "").lstrip(".").lower()
            if not dom:
                continue

            ok_domain = (host == dom) or host.endswith("." + dom)
            if not ok_domain and relaxed_domain:
                dom_rd = _registered_domain(dom)
                ok_domain = bool(dom_rd and host_rd and dom_rd == host_rd)
            if not ok_domain:
                continue

            cpath = cookie.path or "/"
            if path.startswith(cpath):
                cookies.append(f"{cookie.name}={cookie.value}")

        return "; ".join(cookies)
    except Exception:
        return ""


def follow_to_final_url(url: str, opener, referer: str = None, timeout: int = 25) -> str:
    url = sanitize_url(url)
    headers = common_headers("*/*")
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.geturl() or url
    except Exception:
        return url


def probe_stream(url: str, headers: dict, opener, timeout: int = 15, log_prefix: str = "[IKARUS][SRC][PROBE]"):
    target = sanitize_url(url)
    try:
        req_headers = dict(headers or {})
        req_headers.setdefault("Accept", "*/*")
        req_headers.setdefault("Range", "bytes=0-")
        req = urllib.request.Request(target, headers=req_headers, method="GET")
        with opener.open(req, timeout=timeout) as resp:
            ct = resp.headers.get("Content-Type", "")
            code = getattr(resp, "status", None)
            final = resp.geturl()
            xbmc.log(
                f"{log_prefix} code={code} ct={ct} final={redact_url_for_log(final)}",
                xbmc.LOGWARNING,
            )
            resp.read(256)
            return {
                "ok": True,
                "code": code,
                "content_type": ct,
                "final_url": final,
                "url": target,
            }
    except Exception as e:
        error = f"{type(e).__name__}: {str(e)[:160]}"
        xbmc.log(f"{log_prefix} FAILED: {error}", xbmc.LOGERROR)
        return {
            "ok": False,
            "error": error,
            "url": target,
        }


def hellspy_download_stream(video_id: str, video_hash: str, timeout: int = 25) -> str:
    video_id = str(video_id or "").strip()
    video_hash = str(video_hash or "").strip()
    if not video_id or not video_hash:
        return ""

    try:
        response = requests.get(
            f"https://api.hellspy.to/gw/video/{video_id}/{video_hash}/download",
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Linux; Android 15; Mi A3 Build/AP4A.250105.002) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.6778.200 Mobile Safari/537.36"
                ),
                "Accept": "*/*",
                "Accept-Language": "cs-CZ,cs;q=0.9,en;q=0.8",
                "Accept-Encoding": "gzip, deflate, br, zstd",
                "Referer": "https://www.hellspy.to/",
                "Origin": "https://www.hellspy.to",
                "X-Requested-With": "org.lineageos.jelly",
                "Sec-Fetch-Site": "same-site",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Dest": "empty",
            },
            allow_redirects=False,
            timeout=timeout,
        )
        if response.status_code in (301, 302, 303, 307, 308):
            return (response.headers.get("Location") or "").strip()
    except Exception:
        pass
    return ""
