# -*- coding: utf-8 -*-
from datetime import datetime


MAX_SOURCE_ATTEMPTS = 8


def prefetch_start_sec(total: float) -> float:
    try:
        total = float(total or 0.0)
    except Exception:
        total = 0.0
    if total <= 0:
        return 60.0
    if total <= 40.0:
        return max(5.0, total * 0.5)
    return max(12.0, min(total - 30.0, total * 0.75))


def source_attempt_key(provider: str, source_index) -> str:
    provider = str(provider or "").strip().lower()
    if not provider:
        return ""
    try:
        source_index = int(source_index)
    except Exception:
        source_index = -1
    return f"{provider}:{source_index}"


def parse_source_attempts(value, limit: int = MAX_SOURCE_ATTEMPTS) -> list:
    try:
        limit = max(1, int(limit or MAX_SOURCE_ATTEMPTS))
    except Exception:
        limit = MAX_SOURCE_ATTEMPTS
    out = []
    for item in str(value or "").split("|"):
        item = item.strip().lower()
        if item and item not in out:
            out.append(item)
    return out[:limit]


def encode_source_attempts(items, limit: int = MAX_SOURCE_ATTEMPTS) -> str:
    return "|".join(parse_source_attempts("|".join(str(x) for x in (items or [])), limit=limit))


def episode_is_available(item: dict, today_iso: str = "") -> bool:
    air_date = str((item or {}).get("air_date") or "").strip()
    if not air_date:
        return True
    today_iso = str(today_iso or "").strip() or datetime.utcnow().strftime("%Y-%m-%d")
    return air_date <= today_iso


def reached_end(cur: float, total: float) -> bool:
    try:
        cur = float(cur or 0.0)
        total = float(total or 0.0)
    except Exception:
        return False
    if total <= 0 or cur <= 0:
        return False
    remaining = max(0.0, total - cur)
    ratio = cur / total
    return remaining <= 8.0 or ratio >= 0.995


def is_natural_end(ended: bool, stopped: bool, playback_error: bool,
                   aborted: bool, cur: float, total: float) -> bool:
    if aborted or stopped or playback_error:
        return False
    return bool(ended or reached_end(cur, total))
