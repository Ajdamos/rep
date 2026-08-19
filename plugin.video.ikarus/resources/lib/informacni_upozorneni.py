# -*- coding: utf-8 -*-
import xbmcaddon
import xbmcgui

from resources.lib import i18n


ADDON_ID = "plugin.video.ikarus"
NOTICE_VERSION = "2026-05-17-1"
SETTING_HIDE_ON_STARTUP = "info_notice_hide_on_startup"
SETTING_ACCEPTED_VERSION = "info_notice_accepted_version"


def _addon():
    try:
        return xbmcaddon.Addon(ADDON_ID)
    except Exception:
        return xbmcaddon.Addon()


def _is_true(value):
    return str(value or "").strip().lower() in ("true", "1", "yes")


def show_notice(require_confirm=False):
    addon = _addon()
    dialog = xbmcgui.Dialog()
    dialog.textviewer(i18n.T(30905, "Ikarus - information notice"), i18n.T(30909, ""))

    if not require_confirm:
        return True

    agreed = dialog.yesno(
        i18n.T(30900, "Ikarus"),
        i18n.T(30906, "To start the plugin, you must confirm the information notice.\n\nDo you agree with this information?"),
        nolabel=i18n.T(30907, "I do not agree"),
        yeslabel=i18n.T(30908, "I agree"),
    )
    if agreed:
        addon.setSetting(SETTING_ACCEPTED_VERSION, NOTICE_VERSION)
        return True

    return False


def ensure_startup_notice():
    addon = _addon()
    accepted_version = addon.getSetting(SETTING_ACCEPTED_VERSION) or ""
    hide_on_startup = _is_true(addon.getSetting(SETTING_HIDE_ON_STARTUP))

    if accepted_version == NOTICE_VERSION and hide_on_startup:
        return True

    return show_notice(require_confirm=True)
