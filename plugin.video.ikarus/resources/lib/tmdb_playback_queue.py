# -*- coding: utf-8 -*-
import copy
import time
import uuid

from resources.lib import json_storage


SCHEMA_VERSION = 1
MAX_OPERATIONS = 20000


def _empty_data():
    return {
        "version": SCHEMA_VERSION,
        "operations": [],
        "tombstones": {},
    }


def _text(value):
    return str(value or "").strip()


def _integer(value):
    try:
        text = _text(value)
        return int(text) if text else None
    except Exception:
        return None


def _number(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default


def _media_type(value):
    value = _text(value).lower()
    if value == "tv":
        value = "show"
    if value in ("series", "serial"):
        value = "show"
    return value if value in ("movie", "episode", "show") else ""


def _normalize(data):
    if not isinstance(data, dict):
        data = {}
    operations = data.get("operations")
    tombstones = data.get("tombstones")
    return {
        "version": SCHEMA_VERSION,
        "operations": [row for row in (operations or []) if isinstance(row, dict)],
        "tombstones": dict(tombstones) if isinstance(tombstones, dict) else {},
    }


def load(path):
    return _normalize(json_storage.read_json(path, _empty_data(), expected_type=dict))


def _identity_key(media_type, tmdb_id, season=None, episode=None):
    media_type = _media_type(media_type)
    tmdb_id = _text(tmdb_id)
    season_i = _integer(season)
    episode_i = _integer(episode)
    if media_type == "movie" and tmdb_id:
        return f"movie:{tmdb_id}"
    if media_type == "episode" and tmdb_id and season_i is not None and episode_i is not None:
        return f"episode:{tmdb_id}:s{season_i}:e{episode_i}"
    if media_type == "show" and tmdb_id:
        season_key = "*" if season_i is None else str(season_i)
        return f"show:{tmdb_id}:s{season_key}"
    return ""


def _new_operation(kind, media_type, tmdb_id, season=None, episode=None,
                   progress=None, event_at="", now_ts=None):
    now_ts = _number(now_ts, time.time()) or time.time()
    media_type = _media_type(media_type)
    identity = _identity_key(media_type, tmdb_id, season=season, episode=episode)
    if not identity:
        return None
    row = {
        "id": uuid.uuid4().hex,
        "dedupe_key": f"{kind}:{identity}",
        "kind": kind,
        "media_type": media_type,
        "tmdb_id": _text(tmdb_id),
        "created_ts": now_ts,
        "updated_ts": now_ts,
        "event_at": _text(event_at),
        "attempts": 0,
        "next_attempt_ts": 0.0,
        "last_error": "",
    }
    season_i = _integer(season)
    episode_i = _integer(episode)
    if season_i is not None:
        row["season"] = season_i
    if episode_i is not None:
        row["episode"] = episode_i
    if progress is not None:
        row["progress"] = max(0.0, min(100.0, _number(progress, 0.0)))
    return row


def _same_identity(left, right):
    return _identity_key(
        left.get("media_type"),
        left.get("tmdb_id"),
        left.get("season"),
        left.get("episode"),
    ) == _identity_key(
        right.get("media_type"),
        right.get("tmdb_id"),
        right.get("season"),
        right.get("episode"),
    )


def _clear_matches(clear_row, target_row):
    clear_type = _media_type(clear_row.get("media_type"))
    target_type = _media_type(target_row.get("media_type"))
    if _text(clear_row.get("tmdb_id")) != _text(target_row.get("tmdb_id")):
        return False
    if clear_type == "movie":
        return target_type == "movie"
    if clear_type == "episode":
        return (
            target_type == "episode"
            and _integer(clear_row.get("season")) == _integer(target_row.get("season"))
            and _integer(clear_row.get("episode")) == _integer(target_row.get("episode"))
        )
    if clear_type == "show":
        if target_type not in ("episode", "show"):
            return False
        clear_season = _integer(clear_row.get("season"))
        return clear_season is None or clear_season == _integer(target_row.get("season"))
    return False


def _clear_is_exact_for_target(clear_row, target_row):
    clear_type = _media_type(clear_row.get("media_type"))
    target_type = _media_type(target_row.get("media_type"))
    if clear_type != target_type:
        return False
    if clear_type == "movie":
        return _text(clear_row.get("tmdb_id")) == _text(target_row.get("tmdb_id"))
    if clear_type == "episode":
        return _same_identity(clear_row, target_row)
    return False


def _remove_conflicting_clears(data, target_row):
    tombstones = data.get("tombstones") or {}
    remove_keys = [
        key for key, tombstone in tombstones.items()
        if isinstance(tombstone, dict) and _clear_is_exact_for_target(tombstone, target_row)
    ]
    for key in remove_keys:
        tombstones.pop(key, None)
    data["operations"] = [
        row for row in data.get("operations") or []
        if not (
            row.get("kind") == "clear"
            and any(row.get("tombstone_key") == key for key in remove_keys)
        )
    ]


def _append_replacing(data, operation, replace_kinds):
    operations = []
    for row in data.get("operations") or []:
        if row.get("kind") in replace_kinds and _same_identity(row, operation):
            continue
        operations.append(row)
    operations.append(operation)
    data["operations"] = _limit_operations(operations)


def _limit_operations(operations):
    operations = [row for row in operations or [] if isinstance(row, dict)]
    if len(operations) <= MAX_OPERATIONS:
        return operations
    clears = [row for row in operations if row.get("kind") == "clear"]
    others = [row for row in operations if row.get("kind") != "clear"]
    if len(clears) >= MAX_OPERATIONS:
        return clears[-MAX_OPERATIONS:]
    return clears + others[-(MAX_OPERATIONS - len(clears)):]


def enqueue_progress(path, media_type, tmdb_id, progress, season=None, episode=None,
                     event_at="", now_ts=None):
    operation = _new_operation(
        "progress",
        media_type,
        tmdb_id,
        season=season,
        episode=episode,
        progress=progress,
        event_at=event_at,
        now_ts=now_ts,
    )
    if operation is None:
        return ""

    def _update(raw):
        data = _normalize(raw)
        _remove_conflicting_clears(data, operation)
        _append_replacing(data, operation, {"progress"})
        return data

    json_storage.update_json(path, _empty_data(), _update, expected_type=dict, indent=2)
    return operation["id"]


def enqueue_history(path, media_type, tmdb_id, season=None, episode=None,
                    event_at="", now_ts=None):
    operation = _new_operation(
        "history",
        media_type,
        tmdb_id,
        season=season,
        episode=episode,
        event_at=event_at,
        now_ts=now_ts,
    )
    if operation is None:
        return ""

    def _update(raw):
        data = _normalize(raw)
        _remove_conflicting_clears(data, operation)
        _append_replacing(data, operation, {"progress", "history"})
        return data

    json_storage.update_json(path, _empty_data(), _update, expected_type=dict, indent=2)
    return operation["id"]


def enqueue_history_items(path, movies=None, episodes=None, now_ts=None):
    operations = []
    for item in movies or []:
        item = item if isinstance(item, dict) else {"tmdb_id": item}
        operation = _new_operation(
            "history",
            "movie",
            item.get("tmdb_id") or item.get("id"),
            event_at=item.get("watched_at"),
            now_ts=now_ts,
        )
        if operation:
            operations.append(operation)
    for item in episodes or []:
        if not isinstance(item, dict):
            continue
        operation = _new_operation(
            "history",
            "episode",
            item.get("tmdb_id"),
            season=item.get("season"),
            episode=item.get("episode"),
            event_at=item.get("watched_at"),
            now_ts=now_ts,
        )
        if operation:
            operations.append(operation)
    if not operations:
        return []

    def _update(raw):
        data = _normalize(raw)
        for operation in operations:
            _remove_conflicting_clears(data, operation)
            _append_replacing(data, operation, {"progress", "history"})
        return data

    json_storage.update_json(path, _empty_data(), _update, expected_type=dict, indent=2)
    return [row["id"] for row in operations]


def enqueue_clear(path, media_type, tmdb_id, season=None, episode=None, now_ts=None):
    operation = _new_operation(
        "clear",
        media_type,
        tmdb_id,
        season=season,
        episode=episode,
        now_ts=now_ts,
    )
    if operation is None:
        return ""
    tombstone_key = operation["dedupe_key"]
    operation["tombstone_key"] = tombstone_key

    def _update(raw):
        data = _normalize(raw)
        operations = []
        for row in data.get("operations") or []:
            if row.get("kind") == "clear" and (
                row.get("tombstone_key") == tombstone_key
                or _clear_matches(operation, row)
            ):
                continue
            if row.get("kind") in ("progress", "history") and _clear_matches(operation, row):
                continue
            operations.append(row)
        operations.append(operation)
        data["operations"] = _limit_operations(operations)

        tombstones = data.get("tombstones") or {}
        for key, tombstone in list(tombstones.items()):
            if key != tombstone_key and isinstance(tombstone, dict) and _clear_matches(operation, tombstone):
                tombstones.pop(key, None)
        tombstones[tombstone_key] = {
            "media_type": operation.get("media_type"),
            "tmdb_id": operation.get("tmdb_id"),
            "season": operation.get("season"),
            "episode": operation.get("episode"),
            "created_ts": operation.get("created_ts"),
            "remote_sent_at": 0.0,
        }
        data["tombstones"] = tombstones
        return data

    json_storage.update_json(path, _empty_data(), _update, expected_type=dict, indent=2)
    return operation["id"]


def due_operations(path, now_ts=None, limit=200):
    now_ts = _number(now_ts, time.time()) or time.time()
    data = load(path)
    tombstones = data.get("tombstones") or {}
    rows = [
        copy.deepcopy(row)
        for row in data.get("operations") or []
        if _number(row.get("next_attempt_ts"), 0.0) <= now_ts
        and not (
            row.get("kind") in ("history", "progress")
            and any(
                isinstance(tombstone, dict) and _clear_matches(tombstone, row)
                for tombstone in tombstones.values()
            )
        )
    ]
    priority = {"clear": 0, "history": 1, "progress": 2}
    rows.sort(key=lambda row: (priority.get(row.get("kind"), 9), _number(row.get("created_ts"), 0.0)))
    return rows[:max(1, int(limit or 1))]


def mark_completed(path, operation_ids):
    operation_ids = {_text(value) for value in operation_ids or [] if _text(value)}
    if not operation_ids:
        return 0
    removed = [0]

    def _update(raw):
        data = _normalize(raw)
        kept = []
        for row in data.get("operations") or []:
            if _text(row.get("id")) in operation_ids:
                removed[0] += 1
            else:
                kept.append(row)
        data["operations"] = kept
        return data

    json_storage.update_json(path, _empty_data(), _update, expected_type=dict, indent=2)
    return removed[0]


def mark_failed(path, operation_ids, error, now_ts=None):
    operation_ids = {_text(value) for value in operation_ids or [] if _text(value)}
    if not operation_ids:
        return 0
    now_ts = _number(now_ts, time.time()) or time.time()
    changed = [0]

    def _update(raw):
        data = _normalize(raw)
        for row in data.get("operations") or []:
            if _text(row.get("id")) not in operation_ids:
                continue
            attempts = max(0, _integer(row.get("attempts")) or 0) + 1
            delay = min(1800.0, 15.0 * (2 ** min(attempts - 1, 7)))
            row["attempts"] = attempts
            row["last_error"] = _text(error)[:500]
            row["next_attempt_ts"] = now_ts + delay
            row["updated_ts"] = now_ts
            changed[0] += 1
        return data

    json_storage.update_json(path, _empty_data(), _update, expected_type=dict, indent=2)
    return changed[0]


def mark_clears_sent(path, operation_ids, now_ts=None):
    operation_ids = {_text(value) for value in operation_ids or [] if _text(value)}
    if not operation_ids:
        return 0
    now_ts = _number(now_ts, time.time()) or time.time()
    changed = [0]

    def _update(raw):
        data = _normalize(raw)
        tombstones = data.get("tombstones") or {}
        for row in data.get("operations") or []:
            if row.get("kind") != "clear" or _text(row.get("id")) not in operation_ids:
                continue
            row["attempts"] = max(0, _integer(row.get("attempts")) or 0) + 1
            row["last_error"] = ""
            row["next_attempt_ts"] = now_ts + 300.0
            row["updated_ts"] = now_ts
            tombstone = tombstones.get(row.get("tombstone_key"))
            if isinstance(tombstone, dict):
                tombstone["remote_sent_at"] = now_ts
            changed[0] += 1
        data["tombstones"] = tombstones
        return data

    json_storage.update_json(path, _empty_data(), _update, expected_type=dict, indent=2)
    return changed[0]


def operation_pending(path, operation_id):
    operation_id = _text(operation_id)
    if not operation_id:
        return False
    return any(_text(row.get("id")) == operation_id for row in load(path).get("operations") or [])


def pending_count(path):
    return len(load(path).get("operations") or [])


def _source_target(tmdb_id, src):
    season_i = _integer((src or {}).get("season"))
    episode_i = _integer((src or {}).get("episode"))
    mode = _text((src or {}).get("mode")).lower()
    if season_i is not None and episode_i is not None:
        media_type = "episode"
    elif mode in ("episode", "serial_episode", "series", "serial", "tv"):
        media_type = "episode"
    else:
        media_type = "movie"
    return {
        "media_type": media_type,
        "tmdb_id": _text(tmdb_id),
        "season": season_i,
        "episode": episode_i,
    }


def _remote_contains_tombstone(remote_state, tombstone):
    tmdb_id = _text((tombstone or {}).get("tmdb_id"))
    row = (remote_state or {}).get(tmdb_id)
    if not isinstance(row, dict):
        return False
    for src in (row.get("sources") or {}).values():
        if isinstance(src, dict) and _clear_matches(tombstone, _source_target(tmdb_id, src)):
            return True
    return False


def filter_remote_state(remote_state, queue_data):
    state = copy.deepcopy(remote_state if isinstance(remote_state, dict) else {})
    tombstones = (queue_data or {}).get("tombstones")
    tombstones = tombstones if isinstance(tombstones, dict) else {}
    if not tombstones:
        return state

    for tmdb_id, row in list(state.items()):
        if not isinstance(row, dict):
            state.pop(tmdb_id, None)
            continue
        sources = row.get("sources") if isinstance(row.get("sources"), dict) else {}
        kept = {}
        for source_key, src in sources.items():
            if not isinstance(src, dict):
                continue
            target = _source_target(tmdb_id, src)
            hidden = any(
                isinstance(tombstone, dict) and _clear_matches(tombstone, target)
                for tombstone in tombstones.values()
            )
            if not hidden:
                kept[source_key] = src
        if kept:
            row["sources"] = kept
        else:
            state.pop(tmdb_id, None)
    return state


def acknowledge_clears(path, remote_state, fetch_started_ts):
    fetch_started_ts = _number(fetch_started_ts, 0.0)
    if fetch_started_ts <= 0:
        return 0
    acknowledged = [0]

    def _update(raw):
        data = _normalize(raw)
        tombstones = data.get("tombstones") or {}
        remove_keys = []
        for key, tombstone in tombstones.items():
            if not isinstance(tombstone, dict):
                continue
            created_ts = _number(tombstone.get("created_ts"), 0.0)
            sent_at = _number(tombstone.get("remote_sent_at"), 0.0)
            if created_ts > fetch_started_ts or sent_at <= 0 or sent_at > fetch_started_ts:
                continue
            if not _remote_contains_tombstone(remote_state, tombstone):
                remove_keys.append(key)
        if not remove_keys:
            return data
        for key in remove_keys:
            tombstones.pop(key, None)
        data["tombstones"] = tombstones
        data["operations"] = [
            row for row in data.get("operations") or []
            if not (row.get("kind") == "clear" and row.get("tombstone_key") in remove_keys)
        ]
        acknowledged[0] = len(remove_keys)
        return data

    json_storage.update_json(path, _empty_data(), _update, expected_type=dict, indent=2)
    return acknowledged[0]
