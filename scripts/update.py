#!/usr/bin/env python3
"""Builds index.json from sources.json and the plugins' GitHub releases.

    python3 scripts/update.py [--sources sources.json] [--index index.json]

For every plugin in sources.json it reads the repository's releases (drafts
skipped, pre-releases only with "prereleases": true), takes the newest
"keep" of them that have a plugin archive, and writes their versions to
index.json. A version already in index.json is kept as it is when the
release still has the same archive; a new one is downloaded and checked
(size, SHA-256, the manifest inside). The name and the API version come from
the manifest; the rest of an entry from sources.json.

A release that can't be used (no archive, a broken one) is skipped with a
warning. A version already published whose archive changed is an error:
published versions never change. index.json is written only if all is well.

GITHUB_TOKEN, if set, is used for the GitHub API (more requests per hour).
Needs only Python 3.11+.
"""

import argparse
import hashlib
import json
import os
import sys
import tomllib
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import check  # noqa: E402 - the same rules as the check

SOURCE_FIELDS = {"repo", "description", "author", "license", "homepage", "asset", "keep", "prereleases"}
SOURCE_REQUIRED = {"repo", "description", "author", "license"}
DEFAULT_KEEP = 3


def github(path):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "RustCloud-catalog-update"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(f"https://api.github.com{path}", headers=headers)
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def check_sources(sources):
    problems = check.Problems()
    if not isinstance(sources, dict) or not isinstance(sources.get("plugins"), list):
        problems.add("sources.json", "must be an object with a \"plugins\" list")
        return problems
    keep = sources.get("keep", DEFAULT_KEEP)
    if not isinstance(keep, int) or keep < 1:
        problems.add("sources.json", "keep must be a whole number from 1")
    repos = set()
    for index, source in enumerate(sources["plugins"]):
        repo = source.get("repo") if isinstance(source, dict) else None
        where = f"sources.json {repo or f'entry {index + 1}'}"
        if not isinstance(source, dict):
            problems.add(where, "must be an object")
            continue
        check.check_fields(problems, where, source, SOURCE_FIELDS, SOURCE_REQUIRED)
        repo = source.get("repo")
        if not isinstance(repo, str) or repo.count("/") != 1:
            problems.add(where, "repo must be \"owner/name\" on GitHub")
        if repo in repos:
            problems.add(where, "listed more than once")
        repos.add(repo)
        if "keep" in source and (not isinstance(source["keep"], int) or source["keep"] < 1):
            problems.add(where, "keep must be a whole number from 1")
    return problems


def archive_asset(release, source):
    """The plugin archive of a release: the asset named in sources.json, or its only .zip."""
    assets = [a for a in release.get("assets", []) if a.get("state", "uploaded") == "uploaded"]
    if "asset" in source:
        found = [a for a in assets if a["name"] == source["asset"]]
    else:
        found = [a for a in assets if a["name"].endswith(".zip")]
    if len(found) != 1:
        names = ", ".join(a["name"] for a in assets) or "none"
        wanted = f"\"{source['asset']}\"" if "asset" in source else "exactly one .zip"
        raise ValueError(f"needs {wanted} among its assets ({names})")
    return found[0]


def read_release(asset, known):
    """The catalog version of a release's archive; `known` is its entry in index.json, if any."""
    url = asset["browser_download_url"]
    digest = (asset.get("digest") or "").removeprefix("sha256:")
    if known and known["url"] == url and known["size"] == asset["size"] and digest in ("", known["sha256"]):
        return known, None
    archive = check.download(url, asset["size"])
    sha256 = hashlib.sha256(archive).hexdigest()
    if digest and digest != sha256:
        raise ValueError("the downloaded archive is not the one GitHub lists")
    text, wasm = check.unpack(archive)
    if not wasm.startswith(b"\0asm"):
        raise ValueError("app.wasm is not WebAssembly")
    manifest = tomllib.loads(text)
    version = {"version": manifest.get("version"), "api": manifest.get("api"), "url": url, "sha256": sha256,
               "size": len(archive)}
    return version, manifest


def update_plugin(source, keep, old_entry, warnings):
    """The catalog entry of one source, or None if no release can be used."""
    repo = source["repo"]
    known = {v["url"]: v for v in (old_entry or {}).get("versions", [])}
    releases = [r for r in github(f"/repos/{repo}/releases?per_page=100") if not r.get("draft")]
    if not source.get("prereleases", False):
        releases = [r for r in releases if not r.get("prerelease")]

    plugin_id, names, versions = (old_entry or {}).get("id"), (old_entry or {}).get("name"), []
    for release in releases:  # newest first
        if len(versions) == keep:
            break
        where = f"{repo} {release.get('tag_name')}"
        try:
            asset = archive_asset(release, source)
            version, manifest = read_release(asset, known.get(asset["browser_download_url"]))
            if manifest is not None:
                if plugin_id not in (None, manifest.get("id")):
                    raise ValueError(f"holds \"{manifest.get('id')}\", earlier releases \"{plugin_id}\"")
                plugin_id = manifest.get("id")
                if not versions:  # the newest release names the plugin
                    names = manifest.get("name")
        except Exception as err:  # noqa: BLE001 - a bad release is skipped, the others still count
            warnings.append(f"{where}: skipped: {err}")
            continue
        if any(v["version"] == version["version"] for v in versions):
            warnings.append(f"{where}: skipped: version {version['version']} again")
            continue
        versions.append(version)
    if not versions:
        warnings.append(f"{repo}: no release with a plugin archive")
        return None

    versions.sort(key=lambda v: semver_key(v["version"]), reverse=True)
    entry = {
        "id": plugin_id,
        "name": names,
        "description": source["description"],
        "author": source["author"],
        "license": source["license"],
        "source": f"https://github.com/{repo}",
    }
    if "homepage" in source:
        entry["homepage"] = source["homepage"]
    entry["versions"] = versions
    return entry


def semver_key(version):
    """Sort key: newer is bigger; a pre-release is older than its release."""
    core, _, pre = version.split("+")[0].partition("-")
    numbers = tuple(int(part) for part in core.split("."))
    parts = tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre.split(".")) if pre else ()
    return numbers, not pre, parts


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sources", default="sources.json")
    parser.add_argument("--index", default="index.json")
    args = parser.parse_args()
    sys.stdout.reconfigure(line_buffering=True)  # in order with the warnings in the log

    with open(args.sources, encoding="utf-8") as file:
        sources = json.load(file)
    problems = check_sources(sources)
    if problems.list:
        for problem in problems.list:
            print(f"error: {problem}", file=sys.stderr)
        return 1
    try:
        with open(args.index, encoding="utf-8") as file:
            old = json.load(file)
    except FileNotFoundError:
        old = {"schema": check.SCHEMA, "plugins": []}
    old_by_source = {p.get("source"): p for p in old.get("plugins", [])}

    warnings, plugins = [], []
    for source in sources["plugins"]:
        keep = source.get("keep", sources.get("keep", DEFAULT_KEEP))
        old_entry = old_by_source.get(f"https://github.com/{source['repo']}")
        try:
            entry = update_plugin(source, keep, old_entry, warnings)
        except Exception as err:  # noqa: BLE001 - one unreachable repository doesn't stop the others
            warnings.append(f"{source['repo']}: {err}; its versions are kept as they were")
            entry = old_entry
        if entry:
            plugins.append(entry)
            print(f"{entry['id']}: {', '.join(v['version'] for v in entry['versions'])}")

    index = {"schema": check.SCHEMA, "plugins": plugins}
    problems = check.Problems()
    check.check_catalog(problems, index)
    check.check_unchanged(problems, plugins, old.get("plugins", []))
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    if problems.list:
        for problem in problems.list:
            print(f"error: {problem}", file=sys.stderr)
        print("index.json is not changed", file=sys.stderr)
        return 1
    text = json.dumps(index, ensure_ascii=False, indent=2) + "\n"
    with open(args.index, "w", encoding="utf-8") as file:
        file.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
