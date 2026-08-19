# -*- coding: utf-8 -*-
import urllib.parse

import xbmc
import xbmcgui
import xbmcplugin

from resources.lib import source_common


def kodi_url_with_headers(url: str, headers: dict = None) -> str:
    if not headers:
        return url

    parts = []
    for key, value in headers.items():
        if value is None:
            continue
        parts.append(f"{key}={urllib.parse.quote_plus(str(value), safe='')}")

    if not parts:
        return url
    return url + "|" + "&".join(parts)


def guess_stream_mimetype(url: str) -> str:
    ul = str(url or "").lower()
    if ".m3u8" in ul or "/hls" in ul:
        return "application/vnd.apple.mpegurl"
    if ".mp4" in ul:
        return "video/mp4"
    if ".ts" in ul:
        return "video/mp2t"
    if ".webm" in ul:
        return "video/webm"
    if ".mkv" in ul:
        return "video/x-matroska"
    if ".avi" in ul:
        return "video/x-msvideo"
    return "application/octet-stream"


def make_video_list_item(title: str, path: str, subtitle_title: str = "", year: str = "", mimetype: str = ""):
    title = str(title or "Zdroj").strip() or "Zdroj"
    li = xbmcgui.ListItem(label=title, path=path)
    li.setProperty("IsPlayable", "true")
    li.setContentLookup(False)
    if mimetype:
        try:
            li.setMimeType(mimetype)
        except Exception:
            pass
        try:
            li.setProperty("mimetype", mimetype)
        except Exception:
            pass

    info = {"title": title}
    subtitle_title = str(subtitle_title or "").strip()
    if subtitle_title:
        info["originaltitle"] = subtitle_title
    year = str(year or "").strip()
    if year.isdigit():
        info["year"] = int(year)
    li.setInfo("video", info)
    return li


def set_resolved_playback(handle: int, title: str, stream_url: str, headers: dict = None,
                          subtitle_title: str = "", year: str = "", mimetype: str = ""):
    final_url = kodi_url_with_headers(stream_url, headers)
    li = make_video_list_item(
        title,
        final_url,
        subtitle_title=subtitle_title,
        year=year,
        mimetype=mimetype or guess_stream_mimetype(stream_url),
    )
    li.setPath(final_url)
    xbmcplugin.setResolvedUrl(handle, True, li)
    return li


def fail_playback(handle: int):
    try:
        xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())
    except Exception:
        try:
            xbmcplugin.endOfDirectory(
                handle,
                succeeded=False,
                updateListing=False,
                cacheToDisc=False,
            )
        except Exception:
            pass


def log_source_failure(provider: str = "", reason: str = "", title: str = "", url: str = "",
                       tmdb_id: str = "", source_id: str = "", auto_next: bool = False,
                       action: str = "play"):
    provider = str(provider or "").strip().lower() or "unknown"
    reason = str(reason or "").strip() or "unknown"
    parts = [
        f"provider={provider}",
        f"action={action}",
        f"reason={reason}",
    ]
    if tmdb_id:
        parts.append(f"tmdb_id={tmdb_id}")
    if source_id:
        parts.append(f"source_id={source_id}")
    if title:
        parts.append(f"title={str(title)[:120]}")
    if url:
        parts.append(f"url={source_common.redact_url_for_log(url)}")
    if auto_next:
        parts.append("auto_next=1")
    try:
        xbmc.log("[IKARUS][PLAYBACK][FAIL] " + " ".join(parts), xbmc.LOGWARNING)
    except Exception:
        pass


def fail_source_playback(handle: int, provider: str = "", reason: str = "", title: str = "",
                         url: str = "", tmdb_id: str = "", source_id: str = "",
                         auto_next: bool = False, action: str = "play"):
    log_source_failure(
        provider=provider,
        reason=reason,
        title=title,
        url=url,
        tmdb_id=tmdb_id,
        source_id=source_id,
        auto_next=auto_next,
        action=action,
    )
    fail_playback(handle)
