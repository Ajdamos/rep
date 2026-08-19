# -*- coding: utf-8 -*-
import os
import time

import xbmc
import xbmcvfs

from resources.lib import json_storage, tmdb_custom_lists


QUEUE_TTL_SECONDS = 60 * 60
QUEUE_MAX_JOBS = 12
JOB_COOLDOWN_SECONDS = 2


def _log(message, level=xbmc.LOGINFO):
    try:
        xbmc.log(f"[IKARUS][TMDB PREFETCH] {message}", level)
    except Exception:
        pass


def _queue_path():
    return os.path.join(tmdb_custom_lists._profile_dir(), "tmdb_custom_list_prefetch_queue.json")


def _read_queue():
    path = _queue_path()
    if not xbmcvfs.exists(path):
        return []
    try:
        return json_storage.read_json(
            path,
            [],
            expected_type=list,
            logger=lambda message: _log(message, xbmc.LOGWARNING),
        )
    except Exception as e:
        _log(f"queue read error: {e}", xbmc.LOGWARNING)
        return []


def _write_queue(rows):
    try:
        json_storage.write_json(_queue_path(), rows or [], indent=2, keep_backup=True)
    except Exception as e:
        _log(f"queue write error: {e}", xbmc.LOGWARNING)


def _clean_queue(rows):
    now = time.time()
    cleaned = []
    seen = set()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        list_id = str(row.get("list_id") or "").strip()
        try:
            page = int(row.get("page") or 0)
            page_limit = int(row.get("page_limit") or 0)
            created_at = float(row.get("created_at") or 0.0)
        except Exception:
            continue
        if not list_id or page < 1 or page_limit < 1:
            continue
        if created_at and (now - created_at) > QUEUE_TTL_SECONDS:
            continue
        key = (list_id, page, page_limit)
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(row)
    return cleaned[-QUEUE_MAX_JOBS:]


def enqueue_pages(list_id: str, pages, page_limit: int):
    list_id = str(list_id or "").strip()
    if not list_id:
        return False
    try:
        page_limit = int(page_limit or 0)
    except Exception:
        page_limit = 0
    if page_limit < 1:
        return False

    now = time.time()
    queue = _clean_queue(_read_queue())
    existing = {
        (str(row.get("list_id") or ""), int(row.get("page") or 0), int(row.get("page_limit") or 0))
        for row in queue
        if isinstance(row, dict)
    }
    new_jobs = []
    for page in pages or []:
        try:
            page = int(page or 0)
        except Exception:
            continue
        if page < 1:
            continue
        key = (list_id, page, page_limit)
        if key in existing:
            continue
        new_jobs.append({
            "list_id": list_id,
            "page": page,
            "page_limit": page_limit,
            "created_at": now,
        })
        existing.add(key)

    if new_jobs:
        _write_queue(_clean_queue(new_jobs + queue))
        return True
    return False


def process_one():
    queue = _clean_queue(_read_queue())
    if not queue:
        return False

    job = queue.pop(0)
    _write_queue(queue)

    list_id = str(job.get("list_id") or "").strip()
    try:
        page = int(job.get("page") or 0)
        page_limit = int(job.get("page_limit") or 0)
        created_at = float(job.get("created_at") or 0.0)
    except Exception:
        return False

    if not list_id or page < 1 or page_limit < 1:
        return False
    if created_at and (time.time() - created_at) < JOB_COOLDOWN_SECONDS:
        queue.append(job)
        _write_queue(_clean_queue(queue))
        return False

    started = time.time()
    try:
        from resources.lib import TMDB_knihovna as tmdb_ui

        row = tmdb_custom_lists.get_list(list_id)
        if not row:
            return False
        raw_items = [item for item in (row.get("items") or []) if isinstance(item, dict)]
        items = sorted(raw_items, key=tmdb_ui._tmdb_custom_list_year_sort_key, reverse=True)
        total_pages = max(1, (len(items) + page_limit - 1) // page_limit)
        if page > total_pages:
            return False

        start_idx = (page - 1) * page_limit
        page_items = items[start_idx:start_idx + page_limit]
        if not page_items:
            return False

        missing = [
            item for item in page_items
            if not tmdb_ui._tmdb_custom_item_has_listing_meta(item)
        ]
        if missing:
            tmdb_ui._tmdb_prefetch_custom_list_details(missing)
            tmdb_ui._tmdb_enrich_custom_list_page_for_storage(list_id, raw_items, page_items)

        elapsed_ms = int((time.time() - started) * 1000)
        _log(f"done list_id={list_id} page={page}/{total_pages} count={len(page_items)} missing={len(missing)} ms={elapsed_ms}")
        return True
    except Exception as e:
        _log(f"job failed list_id={list_id} page={page}: {e}", xbmc.LOGWARNING)
        return False
