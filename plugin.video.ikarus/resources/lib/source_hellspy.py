# -*- coding: utf-8 -*-
import urllib.parse


HELLSPY_SEARCH_ENDPOINT = "https://api.hellspy.to/gw/search"
MEDIA_EXTENSIONS = (".mp4", ".m3u8", ".ts", ".mkv", ".avi", ".webm")


def hellspy_build_search_url(query: str, offset: int, limit: int) -> str:
    return HELLSPY_SEARCH_ENDPOINT + "?" + urllib.parse.urlencode({
        "query": query,
        "offset": str(offset),
        "limit": str(limit),
    })


def hellspy_extract_items_and_next(payload: dict):
    if not isinstance(payload, dict):
        return [], None
    items = payload.get("items")
    if not isinstance(items, list):
        items = []
    next_offset = payload.get("nextOffset")
    try:
        next_offset = int(next_offset) if next_offset is not None else None
    except Exception:
        next_offset = None
    return items, next_offset


def hellspy_is_direct_media_url(url: str) -> bool:
    if not url:
        return False
    low = str(url).lower()
    return any(ext in low for ext in MEDIA_EXTENSIONS)


def hellspy_pick_direct_url(item: dict) -> str:
    if not isinstance(item, dict):
        return ""

    candidates = []

    for key in (
        "stream", "streamUrl", "hls", "hlsUrl", "mp4", "mp4Url",
        "url", "file", "fileUrl", "download", "downloadUrl",
    ):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            candidates.append(value.strip())

    media = item.get("media")
    if isinstance(media, dict):
        for key in ("url", "hls", "hlsUrl", "mp4", "mp4Url"):
            value = media.get(key)
            if isinstance(value, str) and value.strip():
                candidates.append(value.strip())

    streams = item.get("streams")
    if isinstance(streams, dict):
        for key in ("hls", "mp4", "url"):
            value = streams.get(key)
            if isinstance(value, str) and value.strip():
                candidates.append(value.strip())

    if isinstance(streams, list):
        for stream in streams:
            if isinstance(stream, dict):
                value = stream.get("url") or stream.get("src") or stream.get("file")
                if isinstance(value, str) and value.strip():
                    candidates.append(value.strip())

    for url in candidates:
        if hellspy_is_direct_media_url(url):
            return url

    return ""
