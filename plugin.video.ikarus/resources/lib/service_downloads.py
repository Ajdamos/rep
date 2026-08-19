# -*- coding: utf-8 -*-
import time
import urllib.request
import urllib.parse
import re

import xbmc
import xbmcgui
import xbmcaddon
import xbmcvfs

import os
import sys

import requests
import io

LIB_DIR = os.path.dirname(__file__)
if LIB_DIR not in sys.path:
    sys.path.insert(0, LIB_DIR)
ADDON_ROOT = os.path.dirname(os.path.dirname(LIB_DIR))
if ADDON_ROOT not in sys.path:
    sys.path.insert(0, ADDON_ROOT)

import download_manager
import source_common
from resources.lib import i18n


_WS_BASE = "https://webshare.cz"
_TRAKT_LIST_OVERVIEW_SYNC_INTERVAL = 600.0
_TRAKT_PLAYBACK_SYNC_INTERVAL = 300.0
_PROGRESS_DIALOG = None
_PROGRESS_JOB_ID = ""


class DownloadCancelled(Exception):
    pass


def _err_non_video(content_type):
    return i18n.T(30759, "Source did not return a video file, but '{content_type}'").format(content_type=content_type)


def _err_too_small(downloaded):
    return i18n.T(30760, "Downloaded file is suspiciously small ({downloaded} B)").format(downloaded=downloaded)


def _err_incomplete(downloaded, total):
    return i18n.T(30761, "Download is incomplete: {downloaded} B of expected {total} B").format(
        downloaded=downloaded,
        total=total
    )


def _startup_cleanup_source_cache():
    try:
        from resources.lib import diagnostics
    except Exception:
        try:
            import diagnostics
        except Exception as e:
            xbmc.log(f"[IKARUS][SERVICE] diagnostics import failed: {repr(e)}", xbmc.LOGERROR)
            return
    try:
        deleted, failed = diagnostics.cleanup_source_cache_silent()
        if deleted or failed:
            xbmc.log(f"[IKARUS][SERVICE] startup source cache cleanup deleted={deleted} failed={failed}", xbmc.LOGWARNING)
        else:
            xbmc.log("[IKARUS][SERVICE] startup source cache cleanup nothing to delete", xbmc.LOGINFO)
    except Exception as e:
        xbmc.log(f"[IKARUS][SERVICE] startup source cache cleanup failed: {repr(e)}", xbmc.LOGERROR)


def _ws_session(token: str = ""):
    sess = requests.Session()
    sess.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Referer": _WS_BASE,
        "Accept": "*/*",
        "Accept-Language": "cs-CZ,cs;q=0.9,en;q=0.8",
        "Accept-Encoding": "identity",
        "Connection": "close",
    })
    token = (token or "").strip()
    if token:
        for dom in ("webshare.cz", ".webshare.cz", "www.webshare.cz"):
            try:
                sess.cookies.set("wst", token, domain=dom, path="/")
            except Exception:
                pass
    return sess

def _ws_login_token(job_token: str = "") -> str:
    try:
        from resources.lib import nastaveni
    except Exception:
        try:
            import nastaveni
        except Exception as e:
            xbmc.log(f"[IKARUS][WS][DL] import nastaveni failed: {repr(e)}", xbmc.LOGERROR)
            nastaveni = None

    addon = xbmcaddon.Addon()

    def _is_valid(token_value: str) -> bool:
        token_value = (token_value or "").strip()
        if not token_value:
            return False
        if not nastaveni:
            return True
        try:
            return bool(nastaveni.over_token(token_value))
        except Exception as e:
            xbmc.log(f"[IKARUS][WS][DL] over_token failed: {repr(e)}", xbmc.LOGERROR)
            return False

    job_token = (job_token or "").strip()
    if _is_valid(job_token):
        return job_token

    try:
        token = (addon.getSetting("token") or "").strip()
    except Exception:
        token = ""

    if _is_valid(token):
        return token

    username = ""
    password = ""
    try:
        username = (addon.getSetting("username") or "").strip()
    except Exception:
        pass
    try:
        password = (addon.getSetting("password") or "").strip()
    except Exception:
        pass

    if not nastaveni:
        xbmc.log("[IKARUS][WS][DL] no valid token and nastaveni import unavailable", xbmc.LOGERROR)
        return ""

    try:
        fresh = (nastaveni.ziskej_token(username, password) or "").strip()
    except Exception as e:
        xbmc.log(f"[IKARUS][WS][DL] ziskej_token failed: {repr(e)}", xbmc.LOGERROR)
        fresh = ""

    if fresh:
        try:
            addon.setSetting("token", fresh)
        except Exception:
            pass
        try:
            addon.setSetting("ws_token", fresh)
        except Exception:
            pass
        return fresh

    xbmc.log("[IKARUS][WS][DL] no valid token available for download", xbmc.LOGERROR)
    return ""


def _ws_file_link(ident: str, token: str, session) -> str:
    if not ident or not token:
        xbmc.log("[IKARUS][WS][DL] file_link missing ident or token", xbmc.LOGERROR)
        return ""

    try:
        data = {
            "ident": ident,
            "wst": token,
        }
        resp = session.post(_WS_BASE + "/api/file_link/", data=data, timeout=30)
        resp.raise_for_status()
        raw = resp.content or b""
    except Exception as e:
        xbmc.log(f"[IKARUS][WS][DL] file_link request failed: {repr(e)}", xbmc.LOGERROR)
        return ""

    try:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(raw)
    except Exception as e:
        xbmc.log(f"[IKARUS][WS][DL] file_link parse failed: {repr(e)}", xbmc.LOGERROR)
        return ""

    status = (root.findtext("status") or "").strip()

    if status.upper() != "OK":
        code = (root.findtext("code") or "").strip()
        message = (root.findtext("message") or "").strip()
        xbmc.log(f"[IKARUS][WS][DL] file_link fail code={code} message={message}", xbmc.LOGERROR)
        return ""

    for tag in ("link", "url", "file_link", "download_link"):
        val = (root.findtext(tag) or "").strip()
        if val:
            return val

    for elem in root.iter():
        txt = (elem.text or "").strip()
        if txt.startswith("http://") or txt.startswith("https://"):
            return txt

    xbmc.log("[IKARUS][WS][DL] file_link OK but no URL found in XML", xbmc.LOGERROR)
    return ""

def _ws_resolve_for_download(source_id: str, ws_token: str = ""):
    source_id = (source_id or "").strip()
    if not source_id:
        xbmc.log("[IKARUS][WS][DL] missing source_id", xbmc.LOGERROR)
        return "", {}, None

    xbmc.log(f"[IKARUS][WS][DL] resolve source_id={source_id}", xbmc.LOGWARNING)

    token = _ws_login_token(ws_token)
    if not token:
        xbmc.log("[IKARUS][WS][DL] no valid token for download", xbmc.LOGERROR)
        return "", {}, None

    session = _ws_session(token)

    try:
        user_data_resp = session.post(_WS_BASE + "/api/user_data/", data={"wst": token}, timeout=30)
        user_data_resp.raise_for_status()
        import xml.etree.ElementTree as ET
        root = ET.fromstring(user_data_resp.content or b"")
        vip = (root.findtext("vip") or "").strip()
        if vip and vip != "1":
            xbmc.log(f"[IKARUS][WS][DL] account is not VIP (vip={vip})", xbmc.LOGWARNING)
    except Exception as e:
        xbmc.log(f"[IKARUS][WS][DL] user_data check failed: {repr(e)}", xbmc.LOGERROR)

    direct = ""
    try:
        import film
        play_direct = (film._ws_resolve_play_url(source_id, token) or "").strip()
        if play_direct.lower().startswith(("http://", "https://")):
            direct = play_direct
    except Exception as e:
        xbmc.log(f"[IKARUS][WS][DL] playback resolver import/call failed: {repr(e)}", xbmc.LOGERROR)

    if not direct:
        direct = _ws_file_link(source_id, token, session)
    if direct:
        if "/download" in direct and "inline=1" not in direct:
            sep = "&" if "?" in direct else "?"
            direct = f"{direct}{sep}inline=1"

    if not direct:
        xbmc.log(f"[IKARUS][WS][DL] no direct link resolved for source_id={source_id}", xbmc.LOGERROR)
        try:
            session.close()
        except Exception:
            pass
        return "", {}, None

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "*/*",
        "Referer": _WS_BASE,
        "Cookie": "wst=" + token,
    }

    return direct, headers, session

_sanitize_url = source_common.sanitize_url


def _is_local_filesystem_path(path: str) -> bool:
    p = (path or "").strip()
    if not p:
        return False

    pl = p.lower()

    if pl.startswith("special://"):
        return False
    if pl.startswith("smb://"):
        return False
    if pl.startswith("nfs://"):
        return False
    if pl.startswith("ftp://"):
        return False
    if pl.startswith("http://") or pl.startswith("https://"):
        return False

    # windows disk nebo UNC
    if re.match(r"^[a-zA-Z]:[\\/]", p):
        return True
    if p.startswith("\\\\"):
        return True
    if p.startswith("//"):
        return True

    return False


def _open_binary_writer(dst_path: str):
    if _is_local_filesystem_path(dst_path):
        parent = os.path.dirname(dst_path)
        if parent and not os.path.exists(parent):
            os.makedirs(parent, exist_ok=True)
        return io.open(dst_path, "wb")

    return xbmcvfs.File(dst_path, "w")


def _partial_path(dst_path: str) -> str:
    return str(dst_path or "") + ".part"


def _delete_path(path: str):
    if not path:
        return
    try:
        if _is_local_filesystem_path(path):
            if os.path.exists(path):
                os.remove(path)
            return
        if xbmcvfs.exists(path):
            xbmcvfs.delete(path)
    except Exception as e:
        xbmc.log(f"[IKARUS][DL] cleanup failed path={path}: {repr(e)}", xbmc.LOGWARNING)


def _commit_download(part_path: str, dst_path: str):
    if _is_local_filesystem_path(part_path) and _is_local_filesystem_path(dst_path):
        os.replace(part_path, dst_path)
        return

    backup_path = dst_path + ".ikarus-old"
    had_destination = bool(xbmcvfs.exists(dst_path))
    if xbmcvfs.exists(backup_path):
        xbmcvfs.delete(backup_path)
    if had_destination and not xbmcvfs.rename(dst_path, backup_path):
        raise OSError("could not preserve existing destination before download commit")
    try:
        if not xbmcvfs.rename(part_path, dst_path):
            raise OSError("could not move completed download to destination")
    except Exception:
        if had_destination and xbmcvfs.exists(backup_path):
            xbmcvfs.rename(backup_path, dst_path)
        raise
    if xbmcvfs.exists(backup_path):
        xbmcvfs.delete(backup_path)


def _write_chunk(fh, chunk: bytes):
    try:
        fh.write(chunk)
    except TypeError:
        # fallback pro některé implementace
        fh.write(bytearray(chunk))


def _notify_progress_notification_legacy(title: str, progress: int):
    try:
        xbmcgui.Dialog().notification(
            i18n.T(30700, "Downloads"),
            i18n.T(30910, "{progress}% - {title}").format(progress=progress, title=title),
            xbmcgui.NOTIFICATION_INFO,
            1500,
            sound=False
        )
    except Exception:
        pass


def _close_progress_dialog():
    global _PROGRESS_DIALOG, _PROGRESS_JOB_ID

    if _PROGRESS_DIALOG is not None:
        try:
            _PROGRESS_DIALOG.close()
        except Exception:
            pass

    _PROGRESS_DIALOG = None
    _PROGRESS_JOB_ID = ""


def _notify_progress(job_id: str, title: str, progress: int):
    global _PROGRESS_DIALOG, _PROGRESS_JOB_ID

    try:
        progress = int(progress)
        if progress < 0:
            progress = 0
        if progress > 100:
            progress = 100

        if _PROGRESS_DIALOG is None or _PROGRESS_JOB_ID != job_id:
            _close_progress_dialog()
            _PROGRESS_DIALOG = xbmcgui.DialogProgressBG()
            _PROGRESS_DIALOG.create(i18n.T(30700, "Downloads"), title)
            _PROGRESS_JOB_ID = job_id

        _PROGRESS_DIALOG.update(
            progress,
            i18n.T(30700, "Downloads"),
            i18n.T(30910, "{progress}% - {title}").format(progress=progress, title=title),
        )
    except Exception:
        pass


def _notify_download_done(title: str):
    try:
        xbmcgui.Dialog().notification(
            i18n.T(30700, "Downloads"),
            i18n.T(30911, "Downloaded: {title}").format(title=title),
            xbmcgui.NOTIFICATION_INFO,
            3000,
            sound=False
        )
    except Exception:
        pass


def _refresh_queue_container_if_visible():
    try:
        folder_path = xbmc.getInfoLabel("Container.FolderPath") or ""
        folder_path = folder_path.lower()

        if "action=tmdb.downloads.queue" in folder_path:
            xbmc.executebuiltin("Container.Refresh")
    except Exception:
        pass


def _trakt_lists_overview_sync_tick():
    try:
        from resources.lib import trakt_client
        if not trakt_client.is_connected():
            return False
        from resources.lib import tmdb_trakt_list_sync
        from resources.lib import tmdb_custom_lists
        result = tmdb_trakt_list_sync.sync_now(fetch_items=False, push_items=False)
        pulled_result = None
        try:
            data = tmdb_custom_lists.load(sync_remote=False)
            needs_items = False
            for row in data.get("lists") or []:
                if not isinstance(row, dict):
                    continue
                remote_count = int(row.get("trakt_item_count") or 0)
                local_count = len(row.get("items") or [])
                if str(row.get("trakt_slug") or "").strip() and remote_count > local_count:
                    needs_items = True
                    break
            if needs_items:
                pulled_result = tmdb_trakt_list_sync.sync_now(fetch_items=True, push_items=False)
        except Exception as e:
            xbmc.log(f"[IKARUS][TRAKT LIST SYNC] background item pull check failed: {repr(e)}", xbmc.LOGWARNING)
        try:
            xbmc.log(
                "[IKARUS][TRAKT LIST SYNC] overview background sync "
                f"mode={result.get('mode')} linked={result.get('linked')} "
                f"created={result.get('created')} "
                f"pulled_items={(pulled_result or {}).get('pulled_items', 0)}",
                xbmc.LOGINFO
            )
        except Exception:
            pass
        return True
    except Exception as e:
        xbmc.log(f"[IKARUS][TRAKT LIST SYNC] background overview failed: {repr(e)}", xbmc.LOGWARNING)
        return False


def _trakt_playback_sync_tick():
    try:
        from resources.lib import trakt_client
        if not trakt_client.is_connected():
            return False
        from resources.lib import tmdb_playback_sync

        started = time.time()
        state = tmdb_playback_sync.sync_local_with_trakt(force=False, push_local=True)
        elapsed_ms = int((time.time() - started) * 1000)
        try:
            xbmc.log(
                "[IKARUS][TRAKT PLAYBACK SYNC] background sync "
                f"items={len(state or {})} mode=bidirectional "
                f"elapsed={elapsed_ms}ms",
                xbmc.LOGINFO
            )
        except Exception:
            pass
        return True
    except Exception as e:
        xbmc.log(f"[IKARUS][TRAKT PLAYBACK SYNC] background failed: {repr(e)}", xbmc.LOGWARNING)
        return False


def _custom_lists_prefetch_tick():
    try:
        from resources.lib import tmdb_custom_prefetch
        return tmdb_custom_prefetch.process_one()
    except Exception as e:
        xbmc.log(f"[IKARUS][TMDB PREFETCH] tick failed: {repr(e)}", xbmc.LOGWARNING)
        return False


def _ensure_job_active(job_id: str):
    if not job_id:
        return

    active_job = download_manager.get_job(job_id)
    if not active_job:
        raise DownloadCancelled("download byl zrusen a odebran z fronty")


def _download_one_requests(job, req_headers, session=None):
    job_id = job.get("id") or ""
    title = job.get("title") or "Zdroj"
    provider = (job.get("provider") or "").strip().lower()
    raw_url = job.get("resolved_url") or ""
    url = raw_url if provider == "webshare" else _sanitize_url(raw_url)
    dst_path = job.get("dst_path") or ""

    downloaded = 0
    total = 0

    if session is not None:
        with session.get(url, headers=req_headers, stream=True, timeout=120, allow_redirects=True) as resp:
            final_url = resp.url or url
            content_type = (resp.headers.get("Content-Type") or "").lower().strip()
            total_raw = resp.headers.get("Content-Length") or "0"

            try:
                total = int(total_raw)
            except Exception:
                total = 0

            xbmc.log(
                f"[IKARUS][DL][REQ] start title={title} "
                f"url={source_common.redact_url_for_log(url)} "
                f"final={source_common.redact_url_for_log(final_url)} "
                f"content_type={content_type} content_length={total}",
                xbmc.LOGWARNING
            )

            resp.raise_for_status()

            if (
                "text/html" in content_type
                or "text/plain" in content_type
                or "application/xml" in content_type
                or "text/xml" in content_type
                or "application/json" in content_type
            ):
                raise Exception(_err_non_video(content_type))

            f = _open_binary_writer(dst_path)
            try:
                last_notified = -1
                monitor = xbmc.Monitor()

                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    if monitor.abortRequested():
                        raise DownloadCancelled("Kodi ukoncilo stahovaci sluzbu")
                    _ensure_job_active(job_id)
                    if not chunk:
                        continue

                    _write_chunk(f, chunk)
                    downloaded += len(chunk)

                    if total > 0:
                        pct = int((downloaded * 100) / total)
                        if pct > 100:
                            pct = 100
                        if pct != last_notified and (pct == 100 or pct % 10 == 0):
                            download_manager.update_job(job_id, progress=pct)
                            _notify_progress(job_id, title, pct)
                            _refresh_queue_container_if_visible()
                            last_notified = pct
            finally:
                f.close()

        xbmc.log(
            f"[IKARUS][DL][REQ] finished title={title} bytes={downloaded} total={total}",
            xbmc.LOGWARNING
        )

        if downloaded < 1024 * 100:
            try:
                if xbmcvfs.exists(dst_path):
                    xbmcvfs.delete(dst_path)
            except Exception:
                pass
            raise Exception(_err_too_small(downloaded))

        if total > 0 and downloaded < max(1, total - 1024):
            try:
                if xbmcvfs.exists(dst_path):
                    xbmcvfs.delete(dst_path)
            except Exception:
                pass
            raise Exception(_err_incomplete(downloaded, total))

        return

    with requests.get(url, headers=req_headers, stream=True, timeout=120, allow_redirects=True) as resp:
        final_url = resp.url or url
        content_type = (resp.headers.get("Content-Type") or "").lower().strip()
        total_raw = resp.headers.get("Content-Length") or "0"

        try:
            total = int(total_raw)
        except Exception:
            total = 0

        xbmc.log(
            f"[IKARUS][DL][REQ] start title={title} "
            f"url={source_common.redact_url_for_log(url)} "
            f"final={source_common.redact_url_for_log(final_url)} "
            f"content_type={content_type} content_length={total}",
            xbmc.LOGWARNING
        )

        resp.raise_for_status()

        if (
            "text/html" in content_type
            or "text/plain" in content_type
            or "application/xml" in content_type
            or "text/xml" in content_type
            or "application/json" in content_type
        ):
            raise Exception(_err_non_video(content_type))

        f = _open_binary_writer(dst_path)
        try:
            last_notified = -1
            monitor = xbmc.Monitor()

            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                if monitor.abortRequested():
                    raise DownloadCancelled("Kodi ukoncilo stahovaci sluzbu")
                _ensure_job_active(job_id)
                if not chunk:
                    continue

                _write_chunk(f, chunk)
                downloaded += len(chunk)

                if total > 0:
                    pct = int((downloaded * 100) / total)
                    if pct > 100:
                        pct = 100
                    if pct != last_notified and (pct == 100 or pct % 10 == 0):
                        download_manager.update_job(job_id, progress=pct)
                        _notify_progress(job_id, title, pct)
                        _refresh_queue_container_if_visible()
                        last_notified = pct
        finally:
            f.close()

    xbmc.log(
        f"[IKARUS][DL][REQ] finished title={title} bytes={downloaded} total={total}",
        xbmc.LOGWARNING
    )

    if downloaded < 1024 * 100:
        try:
            if xbmcvfs.exists(dst_path):
                xbmcvfs.delete(dst_path)
        except Exception:
            pass
        raise Exception(_err_too_small(downloaded))

    if total > 0 and downloaded < max(1, total - 1024):
        try:
            if xbmcvfs.exists(dst_path):
                xbmcvfs.delete(dst_path)
        except Exception:
            pass
        raise Exception(_err_incomplete(downloaded, total))


def _download_one(job):
    job_id = job.get("id") or ""
    title = job.get("title") or "Zdroj"
    provider = (job.get("provider") or "").strip().lower()
    raw_url = job.get("resolved_url") or ""
    headers = dict(job.get("headers") or {})
    dst_path = job.get("dst_path") or ""
    source_id = (job.get("source_id") or "").strip()

    if not job_id or not dst_path:
        return False, i18n.T(30762, "Invalid job")

    if not download_manager.update_job(
        job_id,
        status="downloading",
        started_ts=int(time.time()),
        progress=0,
        error=""
    ):
        return False, i18n.T(30762, "Invalid job")
    _notify_progress(job_id, title, 0)
    part_path = _partial_path(dst_path)
    _delete_path(part_path)

    try:
        if provider == "webshare":
            if not source_id:
                raise Exception(i18n.T(30763, "WebShare job is missing source_id"))

            job_ws_token = (job.get("ws_token") or "").strip()
            url, fresh_headers, ws_session = _ws_resolve_for_download(source_id, job_ws_token)
            
            if not url:
                raise Exception(i18n.T(30764, "Failed to get a fresh WebShare download link"))

            req_headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
                "Accept": "*/*",
                "Accept-Language": "cs-CZ,cs;q=0.9,en;q=0.8",
                "Accept-Encoding": "identity",
            }
            req_headers.update(fresh_headers or {})

            temp_job = dict(job)
            temp_job["resolved_url"] = url
            temp_job["headers"] = req_headers
            temp_job["dst_path"] = part_path

            try:
                _download_one_requests(temp_job, req_headers, session=ws_session)
            finally:
                if ws_session is not None:
                    try:
                        ws_session.close()
                    except Exception:
                        pass

            _ensure_job_active(job_id)
            _commit_download(part_path, dst_path)
            download_manager.update_job(
                job_id,
                status="done",
                progress=100,
                finished_ts=int(time.time())
            )
            _notify_progress(job_id, title, 100)
            _close_progress_dialog()
            _notify_download_done(title)
            _refresh_queue_container_if_visible()
            return True, ""

        url = _sanitize_url(raw_url)
        if not url:
            raise Exception(i18n.T(30765, "Missing resolved_url"))

        req_headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            "Accept": "*/*",
            "Accept-Language": "cs-CZ,cs;q=0.9,en;q=0.8",
            "Accept-Encoding": "identity",
            "Connection": "close",
        }
        req_headers.update(headers or {})

        req = urllib.request.Request(url, headers=req_headers, method="GET")

        with urllib.request.urlopen(req, timeout=120) as resp:
            final_url = resp.geturl() or url
            content_type = (resp.headers.get("Content-Type") or "").lower().strip()
            total_raw = resp.headers.get("Content-Length") or "0"

            try:
                total = int(total_raw)
            except Exception:
                total = 0

            xbmc.log(
                f"[IKARUS][DL] start title={title} "
                f"url={source_common.redact_url_for_log(url)} "
                f"final={source_common.redact_url_for_log(final_url)} "
                f"content_type={content_type} content_length={total}",
                xbmc.LOGWARNING
            )

            if (
                "text/html" in content_type
                or "text/plain" in content_type
                or "application/xml" in content_type
                or "text/xml" in content_type
                or "application/json" in content_type
            ):
                raise Exception(
                    _err_non_video(content_type)
                )

            f = _open_binary_writer(part_path)
            try:
                downloaded = 0
                last_notified = -1
                monitor = xbmc.Monitor()

                while True:
                    if monitor.abortRequested():
                        raise DownloadCancelled("Kodi ukoncilo stahovaci sluzbu")
                    _ensure_job_active(job_id)
                    chunk = resp.read(1024 * 256)
                    if not chunk:
                        break

                    f.write(chunk)
                    downloaded += len(chunk)

                    if total > 0:
                        pct = int((downloaded * 100) / total)
                        if pct > 100:
                            pct = 100
                        if pct != last_notified and (pct == 100 or pct % 10 == 0):
                            download_manager.update_job(job_id, progress=pct)
                            _notify_progress(job_id, title, pct)
                            _refresh_queue_container_if_visible()
                            last_notified = pct
            finally:
                f.close()

        xbmc.log(
            f"[IKARUS][DL] finished title={title} bytes={downloaded} total={total}",
            xbmc.LOGWARNING
        )

        if downloaded < 1024 * 100:
            _delete_path(part_path)
            raise Exception(_err_too_small(downloaded))

        if total > 0 and downloaded < max(1, total - 1024):
            _delete_path(part_path)
            raise Exception(
                _err_incomplete(downloaded, total)
            )

        _ensure_job_active(job_id)
        _commit_download(part_path, dst_path)
        download_manager.update_job(
            job_id,
            status="done",
            progress=100,
            finished_ts=int(time.time())
        )
        _notify_progress(job_id, title, 100)
        _close_progress_dialog()
        _notify_download_done(title)
        _refresh_queue_container_if_visible()
        return True, ""

    except DownloadCancelled as e:
        _delete_path(part_path)

        _refresh_queue_container_if_visible()
        _close_progress_dialog()
        xbmc.log(f"[IKARUS][DL] CANCELLED: {repr(e)}", xbmc.LOGWARNING)
        return False, repr(e)

    except Exception as e:
        _delete_path(part_path)

        download_manager.update_job(
            job_id,
            status="error",
            error=repr(e),
            finished_ts=int(time.time())
        )
        _refresh_queue_container_if_visible()
        _close_progress_dialog()
        xbmc.log(f"[IKARUS][DL] FAILED: {repr(e)}", xbmc.LOGERROR)
        return False, repr(e)

def run():
    monitor = xbmc.Monitor()
    xbmc.log("[IKARUS][DL] service started", xbmc.LOGWARNING)
    xbmc.log("[IKARUS][SYNC SERVICE] integrated service started", xbmc.LOGWARNING)
    _startup_cleanup_source_cache()

    try:
        repaired = download_manager.reset_stuck_downloading_jobs()
        if repaired:
            xbmc.log(f"[IKARUS][DL] repaired stuck jobs: {repaired}", xbmc.LOGWARNING)
    except Exception as e:
        xbmc.log(f"[IKARUS][DL] stuck repair failed: {repr(e)}", xbmc.LOGERROR)

    last_trakt_lists_sync_tick = 0.0
    last_trakt_playback_sync_tick = 0.0

    while not monitor.abortRequested():
        now = time.time()
        if (now - last_trakt_lists_sync_tick) >= _TRAKT_LIST_OVERVIEW_SYNC_INTERVAL:
            last_trakt_lists_sync_tick = now
            _trakt_lists_overview_sync_tick()
        if (now - last_trakt_playback_sync_tick) >= _TRAKT_PLAYBACK_SYNC_INTERVAL:
            last_trakt_playback_sync_tick = now
            _trakt_playback_sync_tick()

        job = download_manager.find_next_job()
        if job:
            _download_one(job)
        else:
            _custom_lists_prefetch_tick()
            monitor.waitForAbort(2)

    xbmc.log("[IKARUS][SYNC SERVICE] integrated service stopped", xbmc.LOGWARNING)
    xbmc.log("[IKARUS][DL] service stopped", xbmc.LOGWARNING)
    _close_progress_dialog()

if __name__ == "__main__":
    run()
