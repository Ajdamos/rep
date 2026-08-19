# -*- coding: utf-8 -*-
import time
import uuid

import xbmc
import xbmcaddon
import xbmcvfs

from resources.lib import i18n, json_storage, source_common


addon = xbmcaddon.Addon()


def _profile_dir():
    return xbmcvfs.translatePath(addon.getAddonInfo("profile"))


def _queue_path():
    return _profile_dir().rstrip("/\\") + "/download_queue.json"


def _ensure_profile():
    path = _profile_dir()
    if not xbmcvfs.exists(path):
        xbmcvfs.mkdirs(path)
    return path


_safe_filename = source_common.safe_filename


def _clean_jobs(jobs):
    cleaned = []
    for job in jobs or []:
        if not isinstance(job, dict):
            continue
        row = dict(job)
        row.pop("ws_token", None)
        cleaned.append(row)
    return cleaned


def _read_queue():
    _ensure_profile()
    path = _queue_path()

    if not xbmcvfs.exists(path):
        return {"jobs": []}

    try:
        data = json_storage.read_json(
            path,
            {"jobs": []},
            expected_type=dict,
            logger=lambda message: xbmc.log(f"[IKARUS][DL] {message}", xbmc.LOGWARNING),
        )
        jobs = data.get("jobs")
        if not isinstance(jobs, list):
            jobs = []
        return {"jobs": _clean_jobs(jobs)}
    except Exception as e:
        xbmc.log(f"[IKARUS][DL] queue read failed: {repr(e)}", xbmc.LOGERROR)
        return {"jobs": []}


def _write_queue(data):
    _ensure_profile()
    path = _queue_path()
    try:
        json_storage.write_json(path, data, keep_backup=True)
        return True
    except Exception as e:
        xbmc.log(f"[IKARUS][DL] queue write failed: {repr(e)}", xbmc.LOGERROR)
        return False


def add_job(title: str, provider: str, resolved_url: str, headers: dict, dst_path: str, source_id: str = "", ws_token: str = ""):
    job = {
        "id": str(uuid.uuid4()),
        "title": title or "Zdroj",
        "provider": provider or "",
        "resolved_url": resolved_url or "",
        "headers": headers or {},
        "dst_path": dst_path or "",
        "source_id": source_id or "",
        "status": "queued",
        "progress": 0,
        "error": "",
        "created_ts": int(time.time()),
        "started_ts": 0,
        "finished_ts": 0,
    }

    def append_job(data):
        jobs = data.get("jobs")
        if not isinstance(jobs, list):
            jobs = []
        jobs = _clean_jobs(jobs)
        jobs.append(job)
        data["jobs"] = jobs
        return data

    try:
        json_storage.update_json(
            _queue_path(),
            {"jobs": []},
            append_job,
            expected_type=dict,
            logger=lambda message: xbmc.log(f"[IKARUS][DL] {message}", xbmc.LOGWARNING),
        )
        return job
    except Exception as e:
        xbmc.log(f"[IKARUS][DL] add job failed: {repr(e)}", xbmc.LOGERROR)
        return None


def get_jobs():
    return (_read_queue().get("jobs") or [])


def get_job(job_id: str):
    if not job_id:
        return None

    jobs = get_jobs()
    for job in jobs:
        if (job.get("id") or "") == job_id:
            return job
    return None


def find_next_job():
    jobs = get_jobs()
    for job in jobs:
        if (job.get("status") or "") == "queued":
            return job
    return None


def update_job(job_id: str, **changes):
    result = {"changed": False}

    def update(data):
        jobs = data.get("jobs")
        if not isinstance(jobs, list):
            jobs = []
        jobs = _clean_jobs(jobs)
        for job in jobs:
            if isinstance(job, dict) and (job.get("id") or "") == job_id:
                for key, value in changes.items():
                    job[key] = value
                result["changed"] = True
                break
        data["jobs"] = jobs
        return data

    try:
        json_storage.update_json(_queue_path(), {"jobs": []}, update, expected_type=dict)
        return result["changed"]
    except Exception as e:
        xbmc.log(f"[IKARUS][DL] update job failed id={job_id}: {repr(e)}", xbmc.LOGERROR)
        return False




def get_job_counts():
    jobs = get_jobs()

    out = {
        "queued": 0,
        "downloading": 0,
        "done": 0,
        "error": 0,
        "all": len(jobs),
    }

    for job in jobs:
        st = (job.get("status") or "").strip().lower()
        if st in out:
            out[st] += 1

    return out


def get_current_job():
    jobs = get_jobs()
    for job in jobs:
        if (job.get("status") or "") == "downloading":
            return job
    return None

def reset_stuck_downloading_jobs():
    result = {"changed": 0}

    def reset(data):
        jobs = data.get("jobs")
        if not isinstance(jobs, list):
            jobs = []
        jobs = _clean_jobs(jobs)
        for job in jobs:
            if not isinstance(job, dict):
                continue
            if (job.get("status") or "").strip().lower() == "downloading":
                job["status"] = "queued"
                job["progress"] = 0
                job["error"] = i18n.T(30766, "Restored after interrupted download")
                result["changed"] += 1
        data["jobs"] = jobs
        return data

    try:
        json_storage.update_json(_queue_path(), {"jobs": []}, reset, expected_type=dict)
        return result["changed"]
    except Exception as e:
        xbmc.log(f"[IKARUS][DL] reset jobs failed: {repr(e)}", xbmc.LOGERROR)
        return 0


def remove_done_jobs():
    result = {"removed": 0}

    def remove(data):
        jobs = data.get("jobs")
        if not isinstance(jobs, list):
            jobs = []
        jobs = _clean_jobs(jobs)
        new_jobs = []
        for job in jobs:
            status = (job.get("status") or "").strip().lower() if isinstance(job, dict) else ""
            if status == "done":
                result["removed"] += 1
            else:
                new_jobs.append(job)
        data["jobs"] = new_jobs
        return data

    try:
        json_storage.update_json(_queue_path(), {"jobs": []}, remove, expected_type=dict)
        return result["removed"]
    except Exception as e:
        xbmc.log(f"[IKARUS][DL] remove done jobs failed: {repr(e)}", xbmc.LOGERROR)
        return 0





def remove_all_jobs(keep_downloading: bool = False):
    result = {"removed": 0}

    def remove(data):
        jobs = data.get("jobs")
        if not isinstance(jobs, list):
            jobs = []
        jobs = _clean_jobs(jobs)
        if not keep_downloading:
            result["removed"] = len(jobs)
            data["jobs"] = []
            return data

        new_jobs = []
        for job in jobs:
            status = (job.get("status") or "").strip().lower() if isinstance(job, dict) else ""
            if status == "downloading":
                new_jobs.append(job)
            else:
                result["removed"] += 1
        data["jobs"] = new_jobs
        return data

    try:
        json_storage.update_json(_queue_path(), {"jobs": []}, remove, expected_type=dict)
        return result["removed"]
    except Exception as e:
        xbmc.log(f"[IKARUS][DL] remove jobs failed: {repr(e)}", xbmc.LOGERROR)
        return 0
