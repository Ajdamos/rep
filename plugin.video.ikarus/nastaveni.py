# -*- coding: utf-8 -*-
import hashlib
import xml.etree.ElementTree as ET

import requests
import xbmc
import xbmcaddon

from resources.lib import i18n


BASE = "https://webshare.cz"
API = BASE + "/api/"
REALM = ":Webshare:"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/81.0.4044.138 Safari/537.36",
    "Referer": BASE,
}
TIMEOUT = 20


try:
    from md5crypto_python3 import md5crypt
except Exception:
    try:
        from md5crypt_python3 import md5crypt
    except Exception:
        try:
            from md5crypt import md5crypt
        except Exception:
            ITOA64 = "./0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

            def _to64(v, n):
                out = ""
                while n > 0:
                    out += ITOA64[v & 0x3F]
                    v >>= 6
                    n -= 1
                return out

            def md5crypt(pw: str, salt: str) -> str:
                magic = "$1$"
                import hashlib as _hl
                if salt.startswith(magic):
                    salt = salt[len(magic):]
                salt = salt.split("$", 1)[0][:8]
                pw_b = pw.encode("utf-8")
                salt_b = salt.encode("utf-8")
                magic_b = magic.encode("utf-8")
                ctx = pw_b + magic_b + salt_b
                final = _hl.md5(pw_b + salt_b + pw_b).digest()
                i = len(pw_b)
                while i > 0:
                    ctx += final[:16] if i > 16 else final[:i]
                    i -= 16
                i = len(pw_b)
                while i > 0:
                    ctx += b"\x00" if (i & 1) else pw_b[:1]
                    i >>= 1
                final = _hl.md5(ctx).digest()
                for i in range(1000):
                    ctx1 = (pw_b if (i & 1) else final)
                    if i % 3:
                        ctx1 += salt_b
                    if i % 7:
                        ctx1 += pw_b
                    ctx1 += (final if (i & 1) else pw_b)
                    final = _hl.md5(ctx1).digest()
                passwd = ""
                passwd += _to64(((final[0] << 16) | (final[6] << 8) | final[12]), 4)
                passwd += _to64(((final[1] << 16) | (final[7] << 8) | final[13]), 4)
                passwd += _to64(((final[2] << 16) | (final[8] << 8) | final[14]), 4)
                passwd += _to64(((final[3] << 16) | (final[9] << 8) | final[15]), 4)
                passwd += _to64(((final[4] << 16) | (final[10] << 8) | final[5]), 4)
                passwd += _to64(final[11], 2)
                return magic + salt + "$" + passwd


def _addon():
    try:
        return xbmcaddon.Addon()
    except Exception:
        return None


def _log(msg):
    xbmc.log(f"[Ikarus] {msg}", xbmc.LOGINFO)


def _warn(msg):
    xbmc.log(f"[Ikarus][WARN] {msg}", xbmc.LOGWARNING)


def _err(msg):
    xbmc.log(f"[Ikarus][ERROR] {msg}", xbmc.LOGERROR)


def _get_setting(key, default=""):
    ad = _addon()
    return ad.getSetting(key) if ad else default


def _set_setting(key, value):
    ad = _addon()
    if ad:
        ad.setSetting(key, value if value is not None else "")


def _set_status(token, text):
    _set_setting("token", token or "")
    _set_setting("status", text or "")


def _get_last_credentials():
    return _get_setting("last_username", ""), _get_setting("last_password", "")


def _set_last_credentials(username: str, password: str):
    _set_setting("last_username", username or "")
    _set_setting("last_password", password or "")


def _set_bad_credentials():
    _set_status("", "špatné přihlašovací údaje")




def api(fnct, data):
    url = API + fnct + "/"
    try:
        r = requests.post(url, data=data, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        return r
    except Exception as e:
        _err(f"HTTP error at {fnct}: {e}")
        raise


def over_token(token):
    if not token:
        return False
    try:
        r = api("user_data", {"wst": token})
        ok = ET.fromstring(r.content).findtext("status") == "OK"
        _log(f"user_data -> {'OK' if ok else 'FAIL'}")
        return ok
    except Exception as e:
        _warn(f"over_token exception: {e}")
        return False


def prihlaseni_webshare(username, password):
    try:
        r = api("salt", {"username_or_email": username})
        root = ET.fromstring(r.content)
        if root.findtext("status") != "OK":
            _warn("salt: status != OK")
            return None
        salt = root.findtext("salt") or ""
        if not salt:
            _warn("salt: empty")
            return None

        md5crypt_full = md5crypt(password, salt)
        encrypted_pass = hashlib.sha1(md5crypt_full.encode("utf-8")).hexdigest()
        digest = hashlib.md5((username + REALM + encrypted_pass).encode("utf-8")).hexdigest()

        r = api("login", {
            "username_or_email": username,
            "password": encrypted_pass,
            "digest": digest,
            "keep_logged_in": 1,
        })
        root = ET.fromstring(r.content)
        if root.findtext("status") != "OK":
            code = root.findtext("code") or ""
            msg = root.findtext("message") or ""
            _warn(f"login fail: {code} {msg}")
            if "LOGIN" in code or "Invalid" in msg:
                _set_bad_credentials()
            return None

        token = root.findtext("token")
        if over_token(token):
            return token

        _warn("token nevalidní po loginu")
        return None
    except Exception as e:
        _err(f"login exception: {e}")
        return None


def ziskej_token(username_hint=None, password_hint=None):
    username = _get_setting("username") or username_hint or ""
    password = _get_setting("password") or password_hint or ""
    token = _get_setting("token") or ""

    last_user, last_pass = _get_last_credentials()
    creds_changed = (username != last_user) or (password != last_pass)

    if not creds_changed and token and over_token(token):
        _set_status(token, "připojeno")
        return token

    if not username or not password:
        _set_status("", "nepřipojeno")
        return None

    new_token = prihlaseni_webshare(username, password)
    if new_token:
        _set_status(new_token, "připojeno")
        _set_last_credentials(username, password)
        return new_token

    _set_bad_credentials()
    return None






def refresh_token_now():
    import xbmcgui

    try:
        username = (_get_setting("username") or _get_setting("last_username") or "").strip()
        password = (_get_setting("password") or _get_setting("last_password") or "").strip()

        _set_status("", "")

        if not username or not password:
            _set_status("", "nepřipojeno")
            xbmcgui.Dialog().notification(
                i18n.T(30800, "Ikarus - WebShare"),
                i18n.T(30801, "Fill in username and password in settings."),
                xbmcgui.NOTIFICATION_ERROR,
                3500,
            )
            return None

        new_token = prihlaseni_webshare(username, password)
        if new_token:
            _set_last_credentials(username, password)
            _set_status(new_token, "připojeno")
            xbmcgui.Dialog().notification(
                i18n.T(30800, "Ikarus - WebShare"),
                i18n.T(30802, "Token was refreshed successfully."),
                xbmcgui.NOTIFICATION_INFO,
                2500,
            )
            return new_token

        _set_bad_credentials()
        xbmcgui.Dialog().notification(
            i18n.T(30800, "Ikarus - WebShare"),
            i18n.T(30803, "Login failed (bad credentials or outage)."),
            xbmcgui.NOTIFICATION_ERROR,
            3500,
        )
        return None
    except Exception as e:
        try:
            xbmc.log(f"[Ikarus][ERROR] refresh_token_now exception: {e}", xbmc.LOGERROR)
        except Exception:
            pass
        try:
            xbmcgui.Dialog().notification(
                i18n.T(30800, "Ikarus - WebShare"),
                i18n.T(30804, "Unexpected error while refreshing token."),
                xbmcgui.NOTIFICATION_ERROR,
                3500,
            )
        except Exception:
            pass
        return None
