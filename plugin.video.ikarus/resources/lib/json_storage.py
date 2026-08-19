# -*- coding: utf-8 -*-
import copy
import json
import os
import shutil
import threading
import time
from contextlib import contextmanager


def _clone(value):
    try:
        return copy.deepcopy(value)
    except Exception:
        return value


def _notify(logger, message):
    if not logger:
        return
    try:
        logger(message)
    except Exception:
        pass


def _read_candidate(path, expected_type=None):
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if expected_type is not None and not isinstance(data, expected_type):
        raise ValueError(f"unexpected JSON type: {type(data).__name__}")
    return data


def _temp_path(path, suffix="tmp"):
    return f"{path}.{suffix}.{os.getpid()}.{threading.get_ident()}"


def _replace_with_retry(source, destination, timeout=2.0):
    deadline = time.time() + max(0.1, float(timeout or 0.1))
    while True:
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if time.time() >= deadline:
                raise
            time.sleep(0.05)


def _write_payload(path, payload, keep_backup=True):
    path = os.path.abspath(path)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    temp_path = _temp_path(path)
    backup_path = path + ".bak"
    backup_temp = _temp_path(backup_path)
    try:
        with open(temp_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(payload)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass

        if keep_backup and os.path.isfile(path):
            try:
                _read_candidate(path)
                shutil.copyfile(path, backup_temp)
                _replace_with_retry(backup_temp, backup_path)
            except Exception:
                try:
                    if os.path.exists(backup_temp):
                        os.remove(backup_temp)
                except OSError:
                    pass

        _replace_with_retry(temp_path, path)
    finally:
        for candidate in (temp_path, backup_temp):
            try:
                if os.path.exists(candidate):
                    os.remove(candidate)
            except OSError:
                pass


def write_json(path, data, indent=None, keep_backup=True):
    payload = json.dumps(data, ensure_ascii=False, indent=indent)
    json.loads(payload)
    _write_payload(path, payload, keep_backup=keep_backup)
    return True


def read_json(path, default, expected_type=None, logger=None):
    path = os.path.abspath(path)
    backup_path = path + ".bak"
    main_error = None

    if os.path.isfile(path):
        try:
            return _read_candidate(path, expected_type=expected_type)
        except Exception as exc:
            main_error = exc
            _notify(logger, f"invalid JSON in {path}: {exc}")

    if os.path.isfile(backup_path):
        try:
            data = _read_candidate(backup_path, expected_type=expected_type)
            try:
                write_json(path, data, indent=2, keep_backup=False)
            except Exception as exc:
                _notify(logger, f"JSON recovery write failed for {path}: {exc}")
            _notify(logger, f"JSON recovered from backup: {backup_path}")
            return data
        except Exception as exc:
            _notify(logger, f"invalid JSON backup in {backup_path}: {exc}")

    if main_error is None and not os.path.isfile(path):
        return _clone(default)
    return _clone(default)


@contextmanager
def file_lock(path, timeout=5.0, stale_after=30.0):
    lock_path = os.path.abspath(path) + ".lock"
    parent = os.path.dirname(lock_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    deadline = time.time() + max(0.1, float(timeout or 0.1))
    acquired = False

    while not acquired:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, str(os.getpid()).encode("ascii", errors="ignore"))
            finally:
                os.close(fd)
            acquired = True
        except PermissionError:
            if not os.path.exists(lock_path):
                if time.time() >= deadline:
                    raise
                time.sleep(0.05)
                continue
            try:
                age = time.time() - os.path.getmtime(lock_path)
                if age > max(1.0, float(stale_after or 1.0)):
                    os.remove(lock_path)
                    continue
            except FileNotFoundError:
                continue
            if time.time() >= deadline:
                raise TimeoutError(f"timed out waiting for JSON lock: {lock_path}")
            time.sleep(0.05)
        except FileExistsError:
            try:
                age = time.time() - os.path.getmtime(lock_path)
                if age > max(1.0, float(stale_after or 1.0)):
                    os.remove(lock_path)
                    continue
            except FileNotFoundError:
                continue
            if time.time() >= deadline:
                raise TimeoutError(f"timed out waiting for JSON lock: {lock_path}")
            time.sleep(0.05)

    try:
        yield
    finally:
        if acquired:
            try:
                os.remove(lock_path)
            except FileNotFoundError:
                pass


def update_json(path, default, updater, expected_type=None, indent=None, logger=None):
    with file_lock(path):
        data = read_json(path, default, expected_type=expected_type, logger=logger)
        updated = updater(data)
        if updated is None:
            updated = data
        if expected_type is not None and not isinstance(updated, expected_type):
            raise ValueError(f"updater returned unexpected JSON type: {type(updated).__name__}")
        write_json(path, updated, indent=indent, keep_backup=True)
        return updated
