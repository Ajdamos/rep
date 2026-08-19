# -*- coding: utf-8 -*-
import sys
import urllib.parse
import xbmc
import xbmcgui
import xbmcplugin
import xbmcaddon

import film

import nastaveni

from resources.lib import jine_zdroje         
from resources.lib import TMDB_knihovna
from resources.lib import trakt_client
from resources.lib import informacni_upozorneni
from resources.lib import version_info
from resources.lib import diagnostics
from resources.lib import i18n

addon = xbmcaddon.Addon()
addon_handle = int(sys.argv[1]) if len(sys.argv) > 1 else 0
BASE_PATH = sys.argv[0] if len(sys.argv) > 0 else ""


def build_url(query: dict) -> str:
    return BASE_PATH + '?' + urllib.parse.urlencode(query)


def _sync_runtime(module):
    """Kodi reuseLanguageInvoker keeps imported modules alive between clicks."""
    try:
        module.addon_handle = addon_handle
        module.BASE_PATH = BASE_PATH
    except Exception:
        pass


def _refresh_runtime():
    global addon_handle, BASE_PATH
    try:
        addon_handle = int(sys.argv[1]) if len(sys.argv) > 1 else addon_handle
    except Exception:
        pass
    try:
        BASE_PATH = sys.argv[0] if len(sys.argv) > 0 else BASE_PATH
    except Exception:
        pass


def _end_directory(succeeded=True):
    try:
        if addon_handle > 0:
            xbmcplugin.endOfDirectory(
                addon_handle,
                succeeded=bool(succeeded),
                updateListing=False,
                cacheToDisc=False,
            )
    except Exception:
        pass


def _route_guard(label, action, callback):
    with diagnostics.action_scope(label, "delegate"):
        try:
            callback()
        except Exception as e:
            xbmc.log(f"[Ikarus][ERROR] {label} router failed: {repr(e)}", xbmc.LOGERROR)
            xbmcgui.Dialog().notification(
                i18n.T(30900, "Ikarus"),
                i18n.T(30313, "Section could not be loaded. Details are in kodi.log."),
                xbmcgui.NOTIFICATION_ERROR,
                4000,
            )
            diagnostics.finish_failed_action(addon_handle, action)


#--------------------------------------------------------------------------
#                            Hlavní menu Pluginu Ikarus
#--------------------------------------------------------------------------
def main_menu():
    items = [
        (i18n.T(30300, "Movies"), {"action": "tmdb.movies"}, "DefaultMovies.png", True),
        (i18n.T(30301, "Series"), {"action": "tmdb.series"}, "DefaultTVShows.png", True),
        (i18n.T(30302, "People"), {"action": "tmdb.people"}, "DefaultActor.png", True),
        (i18n.T(30303, "Trending"), {"action": "tmdb.trending"}, "DefaultAddonVideo.png", True),
        (i18n.T(30304, "Watched"), {"action": "tmdb.finished"}, "DefaultInProgressShows.png", True),
        (i18n.T(30305, "In progress"), {"action": "tmdb.started"}, "DefaultInProgressShows.png", True),
        (i18n.T(30306, "My lists"), {"action": "tmdb.custom.lists"}, "DefaultVideoPlaylists.png", True),
        (i18n.T(30307, "Search"), {"action": "tmdb.search"}, "DefaultAddonsSearch.png", True),
        (i18n.T(30308, "Download queue"), {"action": "tmdb.downloads.queue"}, "DefaultAddonProgram.png", True),
        (i18n.T(30309, "Manual source search"), {"action": "other_sources"}, "DefaultFavourites.png", True),
        (i18n.T(30310, "Settings"), {"action": "settings"}, "DefaultProgram.png", False),
    ]

    try:
        if trakt_client.is_connected():
            insert_at = len(items)
            for idx, (_, query, _, _) in enumerate(items):
                if (query or {}).get("action") == "tmdb.search":
                    insert_at = idx
                    break
            items.insert(insert_at, (i18n.T(30311, "Smart lists"), {"action": "tmdb.trakt.smart.lists"}, "DefaultVideoPlaylists.png", True))
            insert_at += 1
            items.insert(insert_at, (i18n.T(30312, "Trakt.TV lists"), {"action": "tmdb.trakt.lists"}, "DefaultVideoPlaylists.png", True))
    except Exception:
        pass

    for label, query, icon_p, is_folder in items:
        url = build_url(query)
        li = xbmcgui.ListItem(label=label)
        li.setArt({'icon': icon_p, 'thumb': icon_p})
        xbmcplugin.addDirectoryItem(handle=addon_handle, url=url, listitem=li, isFolder=is_folder)

    xbmcplugin.endOfDirectory(addon_handle)


#--------------------------------------------------------------------------
#                            Router
#--------------------------------------------------------------------------
def _router_impl(paramstring):
    _refresh_runtime()
    # params z querystringu (nepřepisujeme později!)
    params = dict(urllib.parse.parse_qsl(paramstring)) if paramstring else {}
    action = params.get("action")

    base_path = BASE_PATH

    if not action:
        if not informacni_upozorneni.ensure_startup_notice():
            xbmcgui.Dialog().notification(
                i18n.T(30900, "Ikarus"),
                i18n.T(30314, "The plugin will not start without confirming the information notice."),
                xbmcgui.NOTIFICATION_WARNING,
                3500,
            )
            _end_directory(False)
            return
        main_menu()
        return

    if action == "show_info_notice":
        informacni_upozorneni.show_notice(require_confirm=False)
        _end_directory(True)
        return

    if action == "refresh_token":
        nastaveni.refresh_token_now()
        main_menu()
        return

    if action == "check_updates":
        xbmc.executebuiltin("UpdateAddonRepos")
        xbmcgui.Dialog().notification(
            i18n.T(30900, "Ikarus"),
            i18n.T(30315, "Update check has started."),
            xbmcgui.NOTIFICATION_INFO,
            3500,
        )
        _end_directory(True)
        return

    if action == "version_info":
        version_info.main()
        _end_directory(True)
        return

    if action == "ikarus.diagnostics":
        diagnostics.run_self_test(addon_handle)
        return

    if action == "trakt.login":
        trakt_client.login_device_flow()
        _end_directory(True)
        return

    if action == "trakt.logout":
        trakt_client.logout()
        _end_directory(True)
        return

    if action == "trakt.check":
        trakt_client.check_connection(show_notification=True)
        _end_directory(True)
        return

    # --- film sekce ---
    if action.startswith("film."):
        user = addon.getSetting("username") or ""
        pwd  = addon.getSetting("password") or ""
        token = nastaveni.ziskej_token(user, pwd)
        _route_guard("film", action, lambda: film.handle_router(token, addon_handle, base_path, params))
        return

    # --- Jiné zdroje / Test menu ---
    if action == "other_sources" or action.startswith("other."):
        _sync_runtime(jine_zdroje)
        _route_guard("other_sources", action, jine_zdroje.router)
        return

    # --- TMDB menu ---
    if action.startswith("tmdb."):
        _sync_runtime(TMDB_knihovna)
        _route_guard("tmdb", action, TMDB_knihovna.router)
        return

    if action == "settings":
        xbmc.executebuiltin("Addon.OpenSettings(plugin.video.ikarus)")
        _end_directory(True)
        return

    # fallback – když neznáme akci, aspoň zobrazíme hlavní menu
    main_menu()


def router(paramstring):
    _refresh_runtime()
    params = dict(urllib.parse.parse_qsl(paramstring)) if paramstring else {}
    action = params.get("action") or "main_menu"
    with diagnostics.action_scope("main", action, params):
        return _router_impl(paramstring)


if __name__ == "__main__":
    router(sys.argv[2][1:] if len(sys.argv) > 2 else "")
