
# -*- coding: utf-8 -*-
"""Jednoduché mapování na vestavěné (skinové) ikony Kodi."""

_FALLBACK = "DefaultFolder.png"

_MAP = {
    "search":   "DefaultAddonsSearch.png",   # lupa (vyhledávání)
    "tv":       "DefaultTVShows.png",        # seriály / TV
    "settings": "DefaultAddonSettings.png",  # nastavení
    # zbytek necháme na výchozí složku (funguje všude)
    "box":      _FALLBACK,
    "add":      _FALLBACK,
    "history":  _FALLBACK,
    "weight":   _FALLBACK,
}

def icon_path(name: str) -> str:
    return _MAP.get(name, _FALLBACK)
