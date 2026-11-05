#!/usr/bin/env python3
"""Checks index.json, the RustCloud plugin catalog.

    python3 scripts/check.py [index.json] [--base old.json] [--download]

Without options it checks the format: the same rules as the server
(crates/plugin-api/src/catalog.rs in RustCloud/server), but strict: an entry
the server would skip is an error here, and so is an unknown field (a typo).

--base old.json   versions published in old.json must stay as they are
                  (a version may be removed, never changed);
--download        downloads every archive (or only the ones new or changed
                  since --base) and checks its size, SHA-256 and manifest.

Needs only Python 3.11+.
"""

import argparse
import hashlib
import io
import json
import re
import sys
import tomllib
import urllib.parse
import urllib.request
import zipfile

SCHEMA = 1
MAX_ARCHIVE_SIZE = 64 * 1024 * 1024
MAX_MANIFEST_SIZE = 64 * 1024
MAX_DESCRIPTION = 500

PLUGIN_FIELDS = {"id", "name", "description", "author", "license", "source", "homepage", "versions"}
PLUGIN_REQUIRED = PLUGIN_FIELDS - {"homepage"}
RELEASE_FIELDS = {"version", "api", "url", "sha256", "size", "notes"}
RELEASE_REQUIRED = RELEASE_FIELDS - {"notes"}

ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
SHA256 = re.compile(r"^[0-9a-f]{64}$")


class Problems:
    def __init__(self):
        self.list = []

    def add(self, where, message):
        self.list.append(f"{where}: {message}")


def texts(value):
    """A map of language code to text."""
    return isinstance(value, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in value.items())


def check_texts(problems, where, value, required=True, longest=None):
    if not texts(value):
        problems.add(where, "must be an object of language code to text")
        return
    if required and not value.get("en", "").strip():
        problems.add(where, "\"en\" is required")
    if longest and any(len(text) > longest for text in value.values()):
        problems.add(where, f"longer than {longest} characters")


def check_fields(problems, where, entry, known, required):
    for key in sorted(entry.keys() - known):
        problems.add(where, f"unknown field \"{key}\"")
    for key in sorted(required - entry.keys()):
        problems.add(where, f"\"{key}\" is required")


def https(value):
    return isinstance(value, str) and value.startswith("https://") and len(value) > len("https://")


def check_release(problems, where, release):
    if not isinstance(release, dict):
        problems.add(where, "must be an object")
        return
    check_fields(problems, where, release, RELEASE_FIELDS, RELEASE_REQUIRED)
    if not SEMVER.match(str(release.get("version", ""))):
        problems.add(where, "version must be a semantic version such as 0.1.0")
    if not isinstance(release.get("api"), str) or not release["api"].strip():
        problems.add(where, "api must be the requirement in the manifest, such as ^0.2.16")
    url = release.get("url")
    relative = isinstance(url, str) and url and "://" not in url and not url.startswith("/")
    if not (https(url) or relative):
        problems.add(where, "url must be an https:// address or a path relative to index.json")
    if not SHA256.match(str(release.get("sha256", ""))):
        problems.add(where, "sha256 must be 64 lower-case hex digits")
    size = release.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or not 1 <= size <= MAX_ARCHIVE_SIZE:
        problems.add(where, f"size must be from 1 to {MAX_ARCHIVE_SIZE} bytes")
    if "notes" in release:
        check_texts(problems, f"{where} notes", release["notes"], required=False)


def check_plugin(problems, where, plugin):
    if not isinstance(plugin, dict):
        problems.add(where, "must be an object")
        return
    check_fields(problems, where, plugin, PLUGIN_FIELDS, PLUGIN_REQUIRED)
    if not ID.match(str(plugin.get("id", ""))):
        problems.add(where, "id must be 1–40 lower-case letters, digits or \"-\", not starting with \"-\"")
    check_texts(problems, f"{where} name", plugin.get("name"))
    check_texts(problems, f"{where} description", plugin.get("description"), longest=MAX_DESCRIPTION)
    for key in ("author", "license"):
        if not isinstance(plugin.get(key), str) or not plugin[key].strip():
            problems.add(where, f"{key} must be a non-empty text")
    if not https(plugin.get("source")):
        problems.add(where, "source must be an https:// address")
    if "homepage" in plugin and not https(plugin["homepage"]):
        problems.add(where, "homepage must be an https:// address")
    versions = plugin.get("versions")
    if not isinstance(versions, list) or not versions:
        problems.add(where, "versions must be a non-empty list")
        return
    seen = set()
    for release in versions:
        version = release.get("version") if isinstance(release, dict) else None
        check_release(problems, f"{where} {version or '?'}", release)
        if version in seen:
            problems.add(where, f"version {version} is listed more than once")
        seen.add(version)


def check_catalog(problems, catalog):
    if not isinstance(catalog, dict):
        problems.add("index.json", "must be an object")
        return []
    check_fields(problems, "index.json", catalog, {"schema", "plugins"}, {"schema", "plugins"})
    if catalog.get("schema") != SCHEMA:
        problems.add("index.json", f"schema must be {SCHEMA}")
    plugins = catalog.get("plugins")
    if not isinstance(plugins, list):
        problems.add("index.json", "plugins must be a list")
        return []
    ids = set()
    for index, plugin in enumerate(plugins):
        id = plugin.get("id") if isinstance(plugin, dict) else None
        check_plugin(problems, f"\"{id}\"" if id else f"entry {index + 1}", plugin)
        if id in ids:
            problems.add(f"\"{id}\"", "listed more than once")
        ids.add(id)
    return plugins


def releases(plugins):
    """(id, version) → release, for the well-formed ones."""
    found = {}
    for plugin in plugins:
        if isinstance(plugin, dict) and isinstance(plugin.get("versions"), list):
            for release in plugin["versions"]:
                if isinstance(release, dict):
                    found[(plugin.get("id"), release.get("version"))] = release
    return found


def check_unchanged(problems, plugins, base_plugins):
    """Published versions never change: servers and mirrors trust them."""
    now = releases(plugins)
    for key, old in releases(base_plugins).items():
        new = now.get(key)
        if new is None:
            continue
        for field in ("url", "sha256", "size", "api"):
            if new.get(field) != old.get(field):
                problems.add(f"\"{key[0]}\" {key[1]}", f"{field} changed; publish a new version instead")


def unpack(archive):
    """manifest.toml and app.wasm, at the top of the zip or in one folder, as the server reads them."""
    found = {}
    with zipfile.ZipFile(io.BytesIO(archive)) as zip:
        for info in zip.infolist():
            parts = info.filename.split("/")
            if info.is_dir() or len(parts) > 2 or ".." in parts or info.filename.startswith("/"):
                continue
            name = parts[-1]
            if name not in ("manifest.toml", "app.wasm"):
                continue
            if name in found:
                raise ValueError(f"the archive has two files named {name}")
            if name == "manifest.toml" and info.file_size > MAX_MANIFEST_SIZE:
                raise ValueError("manifest.toml is too large")
            found[name] = zip.read(info)
    for name in ("manifest.toml", "app.wasm"):
        if name not in found:
            raise ValueError(f"the archive has no {name}")
    return found["manifest.toml"].decode("utf-8"), found["app.wasm"]


def download(url, size):
    request = urllib.request.Request(url, headers={"User-Agent": "RustCloud-catalog-check"})
    with urllib.request.urlopen(request, timeout=60) as response:
        data = response.read(size + 1)
    if len(data) != size:
        raise ValueError(f"the archive has {len(data) if len(data) <= size else 'more than ' + str(size)} bytes, "
                         f"index.json says {size}")
    return data


def check_archive(problems, index_url, id, release):
    where = f"\"{id}\" {release['version']}"
    url = urllib.parse.urljoin(index_url, release["url"])
    try:
        archive = download(url, release["size"])
        if hashlib.sha256(archive).hexdigest() != release["sha256"]:
            raise ValueError("the archive's SHA-256 is not the one in index.json")
        text, wasm = unpack(archive)
        manifest = tomllib.loads(text)
    except Exception as err:  # noqa: BLE001 - every failure is a problem of this entry
        problems.add(where, f"{url}: {err}")
        return
    if not wasm.startswith(b"\0asm"):
        problems.add(where, "app.wasm is not WebAssembly")
    expected = {"id": id, "version": release["version"], "api": release["api"]}
    for key, value in expected.items():
        if str(manifest.get(key, "")).replace(" ", "") != value.replace(" ", ""):
            problems.add(where, f"the manifest has {key} = \"{manifest.get(key)}\", index.json says \"{value}\"")
    print(f"ok  {where}  {url}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("index", nargs="?", default="index.json")
    parser.add_argument("--base", help="index.json before the change")
    parser.add_argument("--download", action="store_true", help="download and check the archives")
    parser.add_argument("--index-url", default="https://raw.githubusercontent.com/RustCloud/_plugins/main/index.json",
                        help="where index.json is published; relative archive URLs are resolved against it")
    args = parser.parse_args()

    problems = Problems()
    with open(args.index, encoding="utf-8") as file:
        plugins = check_catalog(problems, json.load(file))
    base_plugins = []
    if args.base:
        with open(args.base, encoding="utf-8") as file:
            base = json.load(file)
        base_plugins = base.get("plugins", []) if isinstance(base, dict) else []
        check_unchanged(problems, plugins, base_plugins)

    if args.download and not problems.list:
        old = releases(base_plugins)
        for (id, version), release in releases(plugins).items():
            if old.get((id, version)) != release:
                check_archive(problems, args.index_url, id, release)

    for problem in problems.list:
        print(f"error: {problem}", file=sys.stderr)
    count = len(plugins)
    print(f"{count} plugin{'s' if count != 1 else ''}, {len(problems.list)} problem(s)")
    return 1 if problems.list else 0


if __name__ == "__main__":
    sys.exit(main())
