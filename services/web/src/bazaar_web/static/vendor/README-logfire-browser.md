# logfire-browser-0.25.0.js and logfire-session-replay-0.5.0.js

- `logfire-browser-0.25.0.js`: `@pydantic/logfire-browser` 0.25.0 (MIT), bundled with every
  dependency into one ES module that exports only `configure`.
- `logfire-session-replay-0.5.0.js`: `@pydantic/logfire-session-replay` 0.5.0 (MIT), with
  `@rrweb/record` 2.1.1 and `fflate` 0.8.3, in one ES module that exports only
  `startSessionReplay`. It is a separate file so the replay code loads only when
  `sessionReplay.load()` asks for it.

Both are loaded by `/browser-telemetry.js` (see `bazaar_web/browser.py`), which does nothing
unless `BAZAAR_LOGFIRE_BROWSER_TOKEN` is set. The license header at the top of each file lists
each bundled package with its version and license text. `@rrweb/record` ships no license file,
so its notice is rrweb's own LICENSE at the `rrweb@2.1.1` tag, kept in the recipe's
`licenses/`.

## Build

The recipe is in `services/web/vendor-build/logfire-browser/`:

- `package.json` and `package-lock.json` pin every version (esbuild 0.28.2 included);
- `entry.js` re-exports `configure`, and `entry-session-replay.js` re-exports
  `startSessionReplay`;
- `build.mjs` runs esbuild and writes the license headers.

From the repository root, with Node 20 or later:

```sh
cd services/web/vendor-build/logfire-browser
npm ci --no-audit --no-fund
node build.mjs ../../src/bazaar_web/static/vendor
rm -rf node_modules
```

esbuild runs with `bundle`, `format: "esm"`, `platform: "browser"`, `target: "es2020"` and
`minify`, without code splitting. The packages' lazy imports (the auto-instrumentations,
web-vitals and the metrics exporter) are therefore inlined: each output is a single file, with
no second chunk and no bare `@...` import left. A clean `npm ci` and build reproduce both files
byte for byte.

Built with Node 26.8.1 and npm 11.19.0. To upgrade, change the versions in `package.json`, run
`npm install`, rebuild, and rename the files and `VENDORED` / `VENDORED_REPLAY` in
`bazaar_web/browser.py` together.
