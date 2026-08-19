# Ikarus Kodi repository

Kodi repository hosted straight from this GitHub repo — Kodi fetches the files
over `raw.githubusercontent.com`, so nothing besides a `git push` is needed.

## Install in Kodi (straight from the server, no manual download)

Requires the repo to be **public** with **GitHub Pages** enabled
(Settings → Pages → Deploy from branch → `main` / root).

1. Settings → System → Add-ons → *Unknown sources* → on.
2. Settings → File manager → Add source → `https://ajdamos.github.io/rep/zips/`
   → name it `ikarus`.
3. Add-ons → Install from zip file → `ikarus` → `repository.ikarus` →
   `repository.ikarus-1.0.1.zip`.
4. Add-ons → Install from repository → Ikarus Repository → Video add-ons → Ikarus.

Updates from then on are automatic.

Step 2 works because `make_repo.py` writes an `index.html` into every served
directory. Kodi's HTTP filesystem builds its listing by scraping `<a href>`
links from whatever HTML the server returns for a directory URL — without
those files Kodi shows an empty folder. `raw.githubusercontent.com` cannot be
browsed this way at all; it 404s on directory paths.

## Layout

```
addons.xml                 index (root copy, for convenience)
addons.xml.md5
make_repo.py               generator — rebuilds everything below
plugin.video.ikarus/       addon source
repository.ikarus/         repository addon source
zips/
  addons.xml               index Kodi actually reads
  addons.xml.md5
  plugin.video.ikarus/
    addon.xml  icon.png
    plugin.video.ikarus-0.1.22.zip
    plugin.video.ikarus-0.1.22.zip.md5
  repository.ikarus/
    addon.xml  icon.png
    repository.ikarus-1.0.1.zip
    repository.ikarus-1.0.1.zip.md5
```

Two rules Kodi is strict about:

* the top-level folder **inside** each zip must be exactly the addon id
  (`repository.ikarus/…`), not `repository.ikarus-1.0.1/…`;
* the zip must live at `zips/<addon.id>/<addon.id>-<version>.zip` — that is the
  path Kodi builds from `<datadir zip="true">`.

## Releasing a new version

1. Edit the addon under its source folder and bump `version` in its `addon.xml`.
2. Run the generator:

   ```
   python3 make_repo.py
   ```

3. `git add -A && git commit && git push` to `main`.

Kodi picks the change up on its next add-on update check (or force it with
*Add-ons → Check for updates*).

## Changing the hosting URL

The fetch URLs live in [`repository.ikarus/addon.xml`](repository.ikarus/addon.xml):

```xml
<info compressed="false">https://ajdamos.github.io/rep/zips/addons.xml</info>
<checksum>https://ajdamos.github.io/rep/zips/addons.xml.md5</checksum>
<datadir zip="true">https://ajdamos.github.io/rep/zips/</datadir>
```

If you later enable GitHub Pages, swap the three URLs for
`https://ajdamos.github.io/rep/zips/…`, bump the repository version, re-run
`make_repo.py` and re-distribute the repository zip.
