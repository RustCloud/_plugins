# RustCloud plugin catalog

`index.json` lists the plugins a RustCloud administrator can install from
Administration → Plugins → Catalog. A server reads it from

```text
https://raw.githubusercontent.com/RustCloud/_plugins/main/index.json
```

(or from another address set in `[plugins] catalog_url`, such as a mirror).
The format is described in the server's
[`docs/dev/plugin-catalog.md`](https://github.com/RustCloud/server/blob/main/docs/dev/plugin-catalog.md).

## Adding a plugin or a version

1. Publish the plugin archive as a release asset of a tag (the same zip
   you'd install by hand: `manifest.toml` and `app.wasm`, at the top or in
   one folder). The plugin template's workflow builds it.
2. Add an entry or a version to `index.json` with the asset's address, size
   and SHA-256:

   ```sh
   curl -sSLO https://github.com/<owner>/<repo>/releases/download/v0.1.0/<id>.zip
   sha256sum <id>.zip && stat -c %s <id>.zip
   ```

3. Check it, then open a pull request:

   ```sh
   python3 scripts/check.py --download
   ```

CI runs the same check: the format, that versions already published stay
as they are (a version may be removed, never changed), and that the new
archives match their entries (size, SHA-256, `id`, `version` and `api` in the
manifest).

What a plugin may do is in its manifest; the administrator approves it when
enabling the plugin. The catalog only says where to get it.
