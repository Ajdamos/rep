#!/usr/bin/env python3
"""Build the Kodi repository tree (zips/, addons.xml, addons.xml.md5) from the
addon source folders in this directory.

Every top-level folder that contains an addon.xml is treated as an addon.
Run this after changing any addon, then commit and push.
"""

import hashlib
import os
import shutil
import xml.etree.ElementTree as ET
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
ZIPS = os.path.join(ROOT, "zips")

# never packaged into an addon zip
EXCLUDE_DIRS = {".git", ".github", "__pycache__", ".idea", ".vscode", "zips"}
EXCLUDE_FILES = {".DS_Store", "Thumbs.db", "index.html"}
EXCLUDE_EXT = {".pyc", ".pyo", ".zip", ".md5"}


def md5_of(path):
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def addon_folders():
    for name in sorted(os.listdir(ROOT)):
        path = os.path.join(ROOT, name)
        if name in EXCLUDE_DIRS or not os.path.isdir(path):
            continue
        if os.path.isfile(os.path.join(path, "addon.xml")):
            yield name, path


def build_zip(addon_id, version, src):
    out_dir = os.path.join(ZIPS, addon_id)
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, "%s-%s.zip" % (addon_id, version))

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for base, dirs, files in os.walk(src):
            dirs[:] = sorted(d for d in dirs if d not in EXCLUDE_DIRS)
            for fname in sorted(files):
                if fname in EXCLUDE_FILES:
                    continue
                if os.path.splitext(fname)[1].lower() in EXCLUDE_EXT:
                    continue
                full = os.path.join(base, fname)
                rel = os.path.relpath(full, src)
                # top-level folder inside the zip MUST equal the addon id
                zf.write(full, os.path.join(addon_id, rel))

    with open(out + ".md5", "w") as fh:
        fh.write(md5_of(out))
    return out, out_dir


def copy_assets(tree, src, out_dir):
    """Kodi reads addon.xml / icon / fanart from zips/<id>/ when browsing."""
    shutil.copy2(os.path.join(src, "addon.xml"), os.path.join(out_dir, "addon.xml"))
    for tag in ("icon", "fanart"):
        node = tree.find("./extension[@point='xbmc.addon.metadata']/assets/%s" % tag)
        if node is None or not (node.text or "").strip():
            continue
        asset = os.path.join(src, node.text.strip())
        if os.path.isfile(asset):
            shutil.copy2(asset, os.path.join(out_dir, tag + os.path.splitext(asset)[1]))


def write_index(directory, names):
    """Apache-style listing so Kodi's HTTP browser can see the files.

    Kodi's HTTP filesystem builds a directory listing by scraping <a href>
    links out of whatever HTML the server returns for a directory URL.
    GitHub Pages serves index.html for a directory, so this makes the repo
    browsable from Kodi's 'Install from zip file' dialog.
    """
    rel = os.path.relpath(directory, ROOT).replace(os.sep, "/")
    html = ["<!DOCTYPE html><html><head><title>Index of /%s</title></head><body>" % rel]
    html.append("<h1>Index of /%s</h1><hr><pre>" % rel)
    html.append('<a href="../">../</a>')
    for name in sorted(names):
        html.append('<a href="%s">%s</a>' % (name, name))
    html.append("</pre><hr></body></html>")
    with open(os.path.join(directory, "index.html"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(html) + "\n")


def main():
    os.makedirs(ZIPS, exist_ok=True)
    entries = []
    addon_dirs = []

    for name, src in addon_folders():
        tree = ET.parse(os.path.join(src, "addon.xml"))
        root = tree.getroot()
        addon_id = root.get("id")
        version = root.get("version")

        if addon_id != name:
            raise SystemExit(
                "folder %r does not match addon id %r - rename the folder" % (name, addon_id)
            )

        out, out_dir = build_zip(addon_id, version, src)
        copy_assets(tree, src, out_dir)
        entries.append(ET.tostring(root, encoding="unicode").strip())
        addon_dirs.append(addon_id)
        print("packaged %s %s -> %s" % (addon_id, version, os.path.relpath(out, ROOT)))

    xml = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<addons>\n'
    xml += "\n".join(entries)
    xml += "\n</addons>\n"

    # listings for Kodi's HTTP directory browser
    for addon_id in addon_dirs:
        d = os.path.join(ZIPS, addon_id)
        write_index(d, [n for n in os.listdir(d) if n != "index.html"])
    write_index(ZIPS, sorted([a + "/" for a in addon_dirs] + ["addons.xml", "addons.xml.md5"]))
    print("wrote directory listings (index.html)")

    for target in (ZIPS, ROOT):
        idx = os.path.join(target, "addons.xml")
        with open(idx, "w", encoding="utf-8") as fh:
            fh.write(xml)
        with open(idx + ".md5", "w") as fh:
            fh.write(md5_of(idx))
        print("wrote %s" % os.path.relpath(idx, ROOT))


if __name__ == "__main__":
    main()
