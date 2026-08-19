# -*- coding: utf-8 -*-
import html
import re
import urllib.parse


PREHRAJTO_BASE = "https://prehrajto.cz"
PREHRAJTO_SEARCH_PREFIX = "/hledej/"
PREHRAJTO_PAGINATOR_PARAM = "videoListing-visualPaginator-page"


def prehrajto_build_search_url(query: str, page: int, base_url: str = PREHRAJTO_BASE,
                               search_prefix: str = PREHRAJTO_SEARCH_PREFIX,
                               paginator_param: str = PREHRAJTO_PAGINATOR_PARAM) -> str:
    query_part = urllib.parse.quote((query or "").strip())
    base = urllib.parse.urljoin(base_url, f"{search_prefix}{query_part}")
    return base + "?" + urllib.parse.urlencode({paginator_param: page})


def prehrajto_canonicalize_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    query = [
        (key, value)
        for (key, value) in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() != "player"
    ]
    new_query = urllib.parse.urlencode(query, doseq=True)
    return urllib.parse.urlunparse((
        parsed.scheme,
        parsed.netloc,
        parsed.path,
        parsed.params,
        new_query,
        parsed.fragment,
    ))


def prehrajto_extract_first_source_from_html(html_text: str):
    match = re.search(r"var\s+sources\s*=\s*(\[[\s\S]*?\])\s*;", html_text or "", re.IGNORECASE)
    if not match:
        return None

    sources_text = match.group(1)
    file_match = re.search(r'["\'](?:file|src)["\']\s*:\s*["\']([^"\']+)["\']', sources_text, re.IGNORECASE)
    if not file_match:
        file_match = re.search(r'(?:file|src)\s*:\s*["\']([^"\']+)["\']', sources_text, re.IGNORECASE)

    if file_match:
        return html.unescape(file_match.group(1)).strip()

    return None


def prehrajto_guess_label_from_url(url: str) -> str:
    text = (url or "").lower()
    if ".m3u8" in text or "/hls" in text:
        return "HLS"
    if ".mp4" in text:
        return "MP4"
    if ".ts" in text:
        return "TS"
    return "Zdroj"


def prehrajto_source_rank(url: str):
    text = (url or "").lower()
    if ".m3u8" in text or "/hls" in text:
        return (0, 0)
    if ".mp4" in text:
        return (1, 0)
    if ".ts" in text:
        return (2, 0)
    return (9, len(text))


def prehrajto_extract_sources_from_html(html_text: str, base_url: str = ""):
    if not html_text:
        return []

    text = html.unescape(html_text)
    sources_text = text
    match = re.search(r"var\s+sources\s*=\s*(\[[\s\S]*?\])\s*;", text, re.IGNORECASE)
    if match:
        sources_text = match.group(1) or text

    out = []
    seen = set()
    base = base_url or PREHRAJTO_BASE

    for item_match in re.finditer(r'(?:(?:"|\')?(?:file|src)(?:"|\')?\s*:\s*(?:"|\')([^"\']+?)(?:"|\'))', sources_text, re.IGNORECASE):
        raw = (item_match.group(1) or "").strip()
        if not raw:
            continue
        full = html.unescape(urllib.parse.urljoin(base, raw)).strip()
        if not full or full in seen:
            continue
        seen.add(full)
        out.append({"label": prehrajto_guess_label_from_url(full), "src": full})

    for item_match in re.finditer(r'(https?://[^"\'\s<>]+?(?:\.m3u8|\.mp4|/hls[^"\'\s<>]*)(?:\?[^"\'\s<>]+)?)', text, re.IGNORECASE):
        raw = (item_match.group(1) or "").strip()
        if not raw:
            continue
        full = html.unescape(raw).strip()
        if not full or full in seen:
            continue
        seen.add(full)
        out.append({"label": prehrajto_guess_label_from_url(full), "src": full})

    for item_match in re.finditer(r'(["\'])(/[^"\']+?(?:\.m3u8|\.mp4|/hls[^"\']*)(?:\?[^"\']+)?)\1', text, re.IGNORECASE):
        raw = (item_match.group(2) or "").strip()
        if not raw:
            continue
        full = html.unescape(urllib.parse.urljoin(base, raw)).strip()
        if not full or full in seen:
            continue
        seen.add(full)
        out.append({"label": prehrajto_guess_label_from_url(full), "src": full})

    out.sort(key=lambda item: prehrajto_source_rank(item.get("src", "")))
    return out
