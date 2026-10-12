# logfire-browser-0.25.0.js

`@pydantic/logfire-browser` 0.25.0 (MIT), bundled with every dependency into one ES module
that exports only `configure`. It is loaded by `/browser-telemetry.js` (see
`bazaar_web/browser.py`), which does nothing unless `BAZAAR_LOGFIRE_BROWSER_TOKEN` is set. The
license header at the top of the file lists each bundled package with its version and license
text.

## Build

The recipe is in `services/web/vendor-build/logfire-browser/`: `package.json` and
`package-lock.json` pin every version (esbuild 0.28.2 included), `entry.js` re-exports
`configure`, and `build.mjs` runs esbuild and writes the license header. From the repository
root, with Node 20 or later:

```sh
cd services/web/vendor-build/logfire-browser
npm ci --no-audit --no-fund
node build.mjs ../../src/bazaar_web/static/vendor/logfire-browser-0.25.0.js
rm -rf node_modules
```

esbuild runs with `bundle`, `format: "esm"`, `platform: "browser"`, `target: "es2020"` and
`minify`, without code splitting. The package's lazy imports (the auto-instrumentations,
web-vitals and the metrics exporter) are therefore inlined: the output is a single file, with no
second chunk and no bare `@...` import left.

Built with Node 26.8.1 and npm 11.19.0. To upgrade, change the versions in `package.json`, run
`npm install`, rebuild, and rename the file and `VENDORED` in `bazaar_web/browser.py` together.
