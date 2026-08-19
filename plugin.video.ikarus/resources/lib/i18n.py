# -*- coding: utf-8 -*-
import xbmcaddon


def T(msgid, fallback=""):
    try:
        text = xbmcaddon.Addon().getLocalizedString(int(msgid))
        return text.strip() or fallback
    except Exception:
        return fallback


L = T
