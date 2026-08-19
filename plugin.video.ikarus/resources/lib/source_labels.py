# -*- coding: utf-8 -*-
import html
import re
import unicodedata


def norm_text(text: str) -> str:
    text = "".join(
        ch for ch in unicodedata.normalize("NFKD", text or "")
        if not unicodedata.combining(ch)
    )
    text = re.sub(r"[^a-zA-Z0-9]+", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def source_size_from_text(text: str) -> int:
    try:
        if not text:
            return 0

        value = html.unescape(str(text))
        value = re.sub(r"<[^>]+>", " ", value)
        value = value.replace("\xa0", " ")
        value = re.sub(r"\s+", " ", value).strip()
        if not value:
            return 0

        unit_re = (
            r"tb|tib|gb|gib|mb|mib|kb|kib|"
            r"bajt(?:u|y|u)?|byte(?:s)?|b"
        )
        match = re.search(
            r"(?<!\d)(\d+(?:[ \u00a0]\d{3})*(?:[.,]\d+)?|\d+(?:[.,]\d+)?)\s*("
            + unit_re
            + r")\b",
            value,
            re.IGNORECASE,
        )
        if not match:
            return 0

        number_text = re.sub(r"[ \u00a0]", "", match.group(1)).replace(",", ".")
        number = float(number_text)
        unit = match.group(2).lower()

        if unit in ("tb", "tib"):
            multiplier = 1024 ** 4
        elif unit in ("gb", "gib"):
            multiplier = 1024 ** 3
        elif unit in ("mb", "mib"):
            multiplier = 1024 ** 2
        elif unit in ("kb", "kib"):
            multiplier = 1024
        else:
            multiplier = 1

        return int(number * multiplier)
    except Exception:
        return 0


def source_int_from_any(value, default: int = 0) -> int:
    try:
        if value is None:
            return default
        if isinstance(value, (int, float)):
            return int(value)
        text = str(value).strip()
        if not text:
            return default
        if re.fullmatch(r"\d+", text):
            return int(text)

        size_from_text = source_size_from_text(text)
        if size_from_text > 0:
            return size_from_text
    except Exception:
        pass
    return default


def source_size_from_mapping(value) -> int:
    if not isinstance(value, (dict, list, tuple)):
        return source_size_from_text(value)

    if isinstance(value, dict):
        preferred = []
        fallback_text = []

        for key, val in value.items():
            key_norm = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if any(x in key_norm for x in ("filesize", "sizebytes", "bytes", "contentlength")) or key_norm == "size":
                preferred.append(val)
            elif key_norm in ("description", "desc", "text", "label", "meta"):
                fallback_text.append(val)

        for val in preferred:
            n = source_int_from_any(val, 0)
            if n > 0:
                return n

        for val in value.values():
            if isinstance(val, (dict, list, tuple)):
                n = source_size_from_mapping(val)
                if n > 0:
                    return n

        for val in fallback_text:
            n = source_size_from_text(val)
            if n > 0:
                return n

        return 0

    for item in value:
        n = source_size_from_mapping(item)
        if n > 0:
            return n
    return 0


def source_first_from_mapping(value, key_names):
    wanted = set()
    for key in key_names or []:
        wanted.add(re.sub(r"[^a-z0-9]", "", str(key).lower()))

    def walk(node):
        if isinstance(node, dict):
            for key, val in node.items():
                key_norm = re.sub(r"[^a-z0-9]", "", str(key).lower())
                if key_norm in wanted and val not in (None, "", [], {}):
                    if isinstance(val, (dict, list, tuple)):
                        nested = walk(val)
                        if nested not in (None, "", [], {}):
                            return nested
                    else:
                        return val
            for val in node.values():
                nested = walk(val)
                if nested not in (None, "", [], {}):
                    return nested
        elif isinstance(node, (list, tuple)):
            for val in node:
                nested = walk(val)
                if nested not in (None, "", [], {}):
                    return nested
        return None

    return walk(value)


def source_collect_dicts_from_mapping(value, key_names):
    wanted = set()
    for key in key_names or []:
        wanted.add(re.sub(r"[^a-z0-9]", "", str(key).lower()))

    out = []

    def add_dicts(node):
        if isinstance(node, dict):
            out.append(node)
        elif isinstance(node, (list, tuple)):
            for item in node:
                add_dicts(item)

    def walk(node):
        if isinstance(node, dict):
            for key, val in node.items():
                key_norm = re.sub(r"[^a-z0-9]", "", str(key).lower())
                if key_norm in wanted:
                    add_dicts(val)
                if isinstance(val, (dict, list, tuple)):
                    walk(val)
        elif isinstance(node, (list, tuple)):
            for val in node:
                walk(val)

    walk(value)
    return out


def source_size_bytes(row: dict) -> int:
    if not isinstance(row, dict):
        return 0

    for key in (
        "size_bytes", "size", "filesize", "file_size", "fileSize",
        "bytes", "contentLength", "content_length",
    ):
        n = source_int_from_any(row.get(key), 0)
        if n > 0:
            return n

    for key in ("size_label", "size_text", "plot"):
        n = source_int_from_any(row.get(key), 0)
        if n > 0:
            return n
    return 0


def source_fmt_size(value) -> str:
    n = source_int_from_any(value, 0)
    if n <= 0:
        return ""

    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(n)
    idx = 0
    while size >= 1024 and idx < len(units) - 1:
        size /= 1024.0
        idx += 1

    if idx == 0:
        return f"{int(size)} {units[idx]}"
    return f"{size:.1f} {units[idx]}"


def source_fmt_bitrate(value) -> str:
    try:
        if value in (None, "", [], {}):
            return ""
        text = str(value).strip()
        if not text:
            return ""
        if re.search(r"\b(?:kbps|mbps|kbit|mbit|bit/s)\b", text, re.I):
            return text
        number = float(text.replace(",", "."))
        if number <= 0:
            return ""
        if number >= 1000000:
            return f"{number / 1000000.0:.1f} Mbps"
        if number >= 1000:
            return f"{number / 1000.0:.0f} kbps"
        return f"{number:.0f} bps"
    except Exception:
        return str(value or "").strip()


def source_duration_text(value) -> str:
    try:
        if value in (None, "", [], {}):
            return ""
        if isinstance(value, str) and ":" in value:
            return value.strip()
        sec = float(value)
        if sec <= 0:
            return ""
        sec = int(round(sec))
        h = sec // 3600
        m = (sec % 3600) // 60
        s = sec % 60
        if h:
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m}:{s:02d}"
    except Exception:
        return str(value or "").strip()


def source_quality_from_dimensions(width, height) -> str:
    try:
        w = int(str(width or "0").strip() or "0")
    except Exception:
        w = 0
    try:
        h = int(str(height or "0").strip() or "0")
    except Exception:
        h = 0

    if w >= 7600 or h >= 4000:
        return "8K"
    if w >= 3800 or h >= 2000:
        return "4K"
    if h >= 1440:
        return "1440p"
    if h >= 1000:
        return "1080p"
    if h >= 700:
        return "720p"
    if h >= 470:
        return "480p"
    return ""


def source_quality_from_text(text: str) -> str:
    raw = text or ""
    low = raw.lower()
    norm = norm_text(raw)

    if re.search(r"\b(4320p|8k)\b", low):
        return "8K"
    if re.search(r"\b(2160p|4k|uhd)\b", low):
        return "4K"

    for q in ("1080p", "720p", "576p", "480p", "360p"):
        if q in low:
            return q

    if re.search(r"\bfull\s*hd\b", low) or "fullhd" in norm:
        return "1080p"
    if re.search(r"\bhd\b", norm):
        return "HD"
    if re.search(r"\bsd\b", norm):
        return "SD"

    return ""


def source_codec_from_text(text: str) -> str:
    low = (text or "").lower()
    norm = norm_text(text or "")

    if "av1" in norm:
        return "AV1"
    if "hevc" in norm or "x265" in norm or "h265" in norm or "h 265" in norm:
        return "HEVC"
    if "x264" in norm or "h264" in norm or "h 264" in norm:
        return "x264"
    if "xvid" in low:
        return "Xvid"
    return ""


def source_audio_from_text(text: str) -> str:
    norm = norm_text(text or "")
    flat = norm.replace(" ", "")

    hits = []
    if any(x in flat for x in ("czdab", "czdubbing", "czdabbing", "ceskydabing")) or "cz dab" in norm or "cesky dabing" in norm:
        hits.append("CZ dab")
    elif re.search(r"\bcz\b", norm) and re.search(r"\b(dab|dabing|dubbing)\b", norm):
        hits.append("CZ dab")
    elif re.search(r"\bcz\b", norm):
        hits.append("CZ")

    if any(x in flat for x in ("skdab", "skdubbing", "skdabbing", "slovenskydabing")) or "sk dab" in norm or "slovensky dabing" in norm:
        hits.append("SK dab")
    elif re.search(r"\bsk\b", norm) and re.search(r"\b(dab|dabing|dubbing)\b", norm):
        hits.append("SK dab")
    elif re.search(r"\bsk\b", norm):
        hits.append("SK")

    if re.search(r"\b(en|eng|english)\b", norm):
        hits.append("EN")

    out = []
    for hit in hits:
        if hit not in out:
            out.append(hit)
    return " + ".join(out[:3])


def source_language_tag(text: str) -> str:
    norm = norm_text(text or "")
    flat = norm.replace(" ", "")

    if any(x in flat for x in ("czdab", "czdabbing", "czdubbing", "ceskydabing")) or "cz dab" in norm:
        return "CZ dab"
    if any(x in flat for x in ("skdab", "skdabbing", "skdubbing", "slovenskydabing")) or "sk dab" in norm:
        return "SK dab"
    if any(x in flat for x in ("cztit", "cztitulky", "czsub", "czsubs")) or "cz tit" in norm:
        return "CZ tit"
    if any(x in flat for x in ("sktit", "sktitulky", "sksub", "sksubs")) or "sk tit" in norm:
        return "SK tit"
    if re.search(r"\bcz\b", norm):
        return "CZ"
    if re.search(r"\bsk\b", norm):
        return "SK"
    if re.search(r"\b(en|eng|english)\b", norm):
        return "EN"
    return ""


def source_subtitles_from_text(text: str) -> str:
    norm = norm_text(text or "")
    flat = norm.replace(" ", "")

    hits = []
    if any(x in flat for x in ("cztit", "cztitulky", "czsub", "czsubs")) or "cz tit" in norm:
        hits.append("CZ tit")
    elif re.search(r"\bcz\b", norm) and re.search(r"\b(tit|titulky|sub|subs)\b", norm):
        hits.append("CZ tit")

    if any(x in flat for x in ("sktit", "sktitulky", "sksub", "sksubs")) or "sk tit" in norm:
        hits.append("SK tit")
    elif re.search(r"\bsk\b", norm) and re.search(r"\b(tit|titulky|sub|subs)\b", norm):
        hits.append("SK tit")

    if re.search(r"\b(en|eng|english)\b", norm) and re.search(r"\b(tit|sub|subs|subtitle|subtitles)\b", norm):
        hits.append("EN tit")

    out = []
    for hit in hits:
        if hit not in out:
            out.append(hit)
    return " + ".join(out[:3])


def source_metadata(row: dict) -> dict:
    row = row or {}
    title = row.get("title") or row.get("name") or row.get("ws_info_name") or ""
    plot = row.get("plot") or ""
    text = f"{title} {plot}"

    size_bytes = source_size_bytes(row)
    quality = (
        str(row.get("quality") or row.get("resolution") or "").strip()
        or source_quality_from_dimensions(row.get("width"), row.get("height"))
        or source_quality_from_text(text)
    )
    codec = str(row.get("codec") or row.get("format") or "").strip() or source_codec_from_text(text)
    audio = str(row.get("audio") or row.get("audio_lang") or "").strip() or source_audio_from_text(text)
    subtitles = str(row.get("subtitles") or row.get("subs") or row.get("subs_langs") or "").strip() or source_subtitles_from_text(text)

    return {
        "size_bytes": size_bytes,
        "size": source_fmt_size(size_bytes),
        "quality": quality,
        "codec": codec,
        "audio": audio,
        "subtitles": subtitles,
    }


def source_stream_line(stream: dict, kind: str = "video") -> str:
    stream = stream or {}
    if kind == "video":
        parts = []
        width = str(stream.get("width") or "").strip()
        height = str(stream.get("height") or "").strip()
        if width and height:
            parts.append(f"{width}x{height}")
        fmt = str(stream.get("format") or stream.get("codec") or "").strip()
        if fmt:
            parts.append(fmt)
        fps = str(stream.get("fps") or "").strip()
        if fps:
            parts.append(f"{fps} fps")
        bitrate = source_fmt_bitrate(stream.get("bitrate"))
        if bitrate:
            parts.append(bitrate)
        return ", ".join(parts)

    parts = []
    fmt = str(stream.get("format") or stream.get("codec") or "").strip()
    if fmt:
        parts.append(fmt)
    channels = str(stream.get("channels") or "").strip()
    if channels:
        parts.append(f"{channels} ch")
    lang = str(stream.get("lang") or stream.get("language") or "").strip()
    if lang:
        parts.append(lang)
    bitrate = source_fmt_bitrate(stream.get("bitrate"))
    if bitrate:
        parts.append(bitrate)
    return ", ".join(parts)
