# -*- coding: utf-8 -*-
import os
import sys

import xbmc
import xbmcgui


THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ADDON_ROOT = os.path.dirname(os.path.dirname(THIS_DIR))
if ADDON_ROOT not in sys.path:
    sys.path.insert(0, ADDON_ROOT)

from resources.lib import github_gist_sync, i18n


def main():
    xbmc.log("[IKARUS][GIST SYNC] import script start", xbmc.LOGINFO)
    ok, msg = github_gist_sync.import_settings_from_file()
    icon = xbmcgui.NOTIFICATION_INFO if ok else xbmcgui.NOTIFICATION_ERROR
    xbmcgui.Dialog().notification(i18n.T(30912, "GitHub Gist"), msg, icon, 3500)
    xbmc.log("[IKARUS][GIST SYNC] import script done", xbmc.LOGINFO)


if __name__ == "__main__":
    main()
