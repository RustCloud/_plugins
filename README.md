# RustCloud plugin catalog

`index.json` lists the plugins a RustCloud administrator can install from
Administration → Plugins → Catalog. A server reads it from

```text
https://raw.githubusercontent.com/RustCloud/_plugins/main/index.json
```

(or from another address set in `[plugins] catalog_url`, such as a mirror).
The format is described in the server's
[`docs/dev/plugin-catalog.md`](https://github.com/RustCloud/server/blob/main/docs/dev/plugin-catalog.md).

## Adding a plugin

`index.json` is generated: don't edit it by hand. Add the plugin's
repository to `sources.json`:

```json
{
  "repo": "owner/name",
  "description": { "en": "One or two sentences.", "ru": "Одна-две фразы." },
  "author": "Who makes it",
  "license": "MIT OR Apache-2.0"
}
```

Optional: `homepage` (an https:// page about it), `asset` (the archive's
name when a release has more than one `.zip`), `keep` (how many newest
versions to list; the default is the top-level `keep`, 3), `prereleases`
(`true` to list pre-releases too).

Then run the **Update** workflow (Actions → Update → Run workflow). It reads
each repository's releases, takes the newest ones with a plugin archive
(the zip you'd install by hand: `manifest.toml` and `app.wasm`, at the top or
in one folder; the plugin template's workflow builds it), checks every new
archive (size, SHA-256, the manifest inside) and commits `index.json`. The
plugin's name, id and API version come from its manifest.

Locally: `python3 scripts/update.py` (set `GITHUB_TOKEN` for more GitHub
API requests per hour).

## Rules

- A published version never changes. Re-uploading a release asset with
  other content makes Update fail; publish a new version instead. Older
  versions drop out of `index.json` as newer ones come.
- A release that can't be used (no archive, a broken one) is skipped with a
  warning in the workflow's log; the others are still listed.
- The **Check** workflow checks `index.json` on every push and pull request:
  its format, that published versions stay as they are, and the new
  archives.

What a plugin may do is in its manifest; the administrator approves it when
enabling the plugin. The catalog only says where to get it.
