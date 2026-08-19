# -*- coding: utf-8 -*-
import html
import re
import urllib.parse


def sktonline_abs(url: str, base: str) -> str:
    return urllib.parse.urljoin(base, url)


def sktonline_parse_tag_attr(tag_attrs: str, name: str) -> str:
    match = re.search(r"\b" + re.escape(name) + r'\s*=\s*"([^"]*)"', tag_attrs, re.IGNORECASE)
    if match:
        return match.group(1)
    match = re.search(r"\b" + re.escape(name) + r"\s*=\s*'([^']*)'", tag_attrs, re.IGNORECASE)
    if match:
        return match.group(1)
    match = re.search(r"\b" + re.escape(name) + r"\s*=\s*([^\s>]+)", tag_attrs, re.IGNORECASE)
    if match:
        return match.group(1)
    return ""


def sktonline_guess_label_from_url(url: str) -> str:
    low = (url or "").lower()
    match = re.search(r"(\d{3,4})p", low)
    if match:
        return match.group(1) + "p"
    if ".m3u8" in low:
        return "HLS"
    if ".mp4" in low:
        return "MP4"
    return "Zdroj"


def sktonline_find_embed_url_anywhere(detail_html: str, base_url: str):
    if not detail_html:
        return None

    text = html.unescape(detail_html)

    match = re.search(r'<iframe\b[^>]*\bsrc\s*=\s*["\']([^"\']*?/embed/[^"\']+)["\']', text, re.IGNORECASE)
    if match:
        return sktonline_abs(match.group(1).strip(), base_url)

    match = re.search(r"(https?://online\.sktorrent\.eu/embed/[a-zA-Z0-9]+)", text, re.IGNORECASE)
    if match:
        return match.group(1).strip()

    match = re.search(r"(/embed/[a-zA-Z0-9]+)", text, re.IGNORECASE)
    if match:
        return sktonline_abs(match.group(1).strip(), base_url)

    return None


def sktonline_extract_video_sources(detail_html: str, page_url: str):
    if not detail_html:
        return []

    video_match = re.search(r"(<video\b[^>]*>)([\s\S]*?)</video>", detail_html, re.IGNORECASE)
    if not video_match:
        return []

    video_open = video_match.group(1) or ""
    inner = video_match.group(2) or ""
    out = []
    seen = set()

    for source_match in re.finditer(r"<source\b([^>]*?)>", inner, re.IGNORECASE):
        attrs = source_match.group(1) or ""
        src = sktonline_parse_tag_attr(attrs, "src")
        if not src:
            continue
        src = html.unescape(src).strip()
        if not src:
            continue
        full = sktonline_abs(src, page_url)
        if full in seen:
            continue
        seen.add(full)
        label = (
            sktonline_parse_tag_attr(attrs, "label")
            or sktonline_parse_tag_attr(attrs, "res")
            or sktonline_parse_tag_attr(attrs, "data-res")
        )
        label = html.unescape(label).strip() if label else sktonline_guess_label_from_url(full)
        out.append({"label": label, "src": full})

    if not out:
        video_src = sktonline_parse_tag_attr(video_open, "src") or sktonline_parse_tag_attr(video_open, "data-src")
        if video_src:
            full = sktonline_abs(html.unescape(video_src).strip(), page_url)
            out.append({"label": sktonline_guess_label_from_url(full), "src": full})

    return out


def sktonline_extract_sources_from_scripts(detail_html: str, base_url: str):
    if not detail_html:
        return []
    text = html.unescape(detail_html)

    out = []
    seen = set()

    for match in re.finditer(r'(?:(?:"|\')?(?:file|src)(?:"|\')?\s*:\s*(?:"|\')([^"\']+?)(?:"|\'))', text, re.IGNORECASE):
        url = (match.group(1) or "").strip()
        if not url:
            continue
        low = url.lower()
        if (".mp4" not in low) and (".m3u8" not in low) and ("m3u8" not in low) and ("mp4" not in low) and ("/hls" not in low) and ("/stream" not in low):
            continue
        full = sktonline_abs(url, base_url)
        if full in seen:
            continue
        seen.add(full)
        out.append({"label": sktonline_guess_label_from_url(full), "src": full})

    for match in re.finditer(r'(https?://[^"\'\s<>]+?\.(?:m3u8|mp4)(?:\?[^"\'\s<>]+)?)', text, re.IGNORECASE):
        url = (match.group(1) or "").strip()
        if not url:
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append({"label": sktonline_guess_label_from_url(url), "src": url})

    for match in re.finditer(r'(["\'])(/[^"\']+?\.(?:m3u8|mp4)(?:\?[^"\']+)?)\1', text, re.IGNORECASE):
        url = (match.group(2) or "").strip()
        if not url:
            continue
        full = sktonline_abs(url, base_url)
        if full in seen:
            continue
        seen.add(full)
        out.append({"label": sktonline_guess_label_from_url(full), "src": full})

    return out


def sktonline_extract_download_links(html_text: str, base_url: str):
    out = []
    seen = set()
    if not html_text:
        return out

    text = html.unescape(html_text)

    for match in re.finditer(r'<a\b([^>]*?)href=["\']([^"\']+)["\']([^>]*)>', text, re.IGNORECASE):
        pre = match.group(1) or ""
        href = (match.group(2) or "").strip()
        post = match.group(3) or ""
        if not href:
            continue
        low = href.lower()
        if not any(token in low for token in ("download", "/download/", "/get/", "/file/", "/dl/")):
            continue
        full = sktonline_abs(href, base_url)
        if full in seen:
            continue
        seen.add(full)
        attrs = pre + " " + post
        title_match = re.search(r'title=["\']([^"\']+)["\']', attrs, re.IGNORECASE)
        label = html.unescape(title_match.group(1)).strip() if title_match else sktonline_guess_label_from_url(full)
        out.append({"label": label, "url": full})

    for match in re.finditer(r'(https?://[^"\'\s<>]+/(?:download|get|file|dl)/[^"\'\s<>]+)', text, re.IGNORECASE):
        full = html.unescape(match.group(1)).strip()
        if not full:
            continue
        if full in seen:
            continue
        seen.add(full)
        out.append({"label": sktonline_guess_label_from_url(full), "url": full})

    return out


def sktonline_merge_sources(*lists):
    out = []
    seen = set()
    for items in lists:
        for source in items or []:
            url = (source.get("src") or "").strip()
            if not url:
                continue
            if url in seen:
                continue
            seen.add(url)
            label = (source.get("label") or "").strip() or sktonline_guess_label_from_url(url)
            out.append({"label": label, "src": url})

    def rank(label):
        match = re.search(r"(\d{3,4})p", (label or "").lower())
        if match:
            try:
                return -int(match.group(1))
            except Exception:
                pass
        if "hls" in (label or "").lower():
            return -1
        return 0

    out.sort(key=lambda item: rank(item.get("label", "")))
    return out
