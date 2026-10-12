// Builds the vendored browser files into the directory given as argv[2]:
//   logfire-browser-0.25.0.js         @pydantic/logfire-browser, exporting configure
//   logfire-session-replay-0.5.0.js   @pydantic/logfire-session-replay, exporting
//                                     startSessionReplay (loaded lazily, only for replay)
// Each is one ESM file with every dependency (and every dynamic import) inlined, and a license
// header that lists each bundled package, its version and its license text.
import { readFileSync, writeFileSync, existsSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { build } from "esbuild";

// Packages published without a license file: their repository's LICENSE, kept in licenses/.
// @rrweb/record: https://github.com/rrweb-io/rrweb/blob/rrweb%402.1.1/LICENSE
const LICENSE_FILES = { "@rrweb/record": "licenses/rrweb-2.1.1-LICENSE" };

const dir = process.argv[2];
if (!dir) throw new Error("usage: node build.mjs <output directory>");

await bundle("entry.js", "logfire-browser-0.25.0.js", "@pydantic/logfire-browser 0.25.0 (MIT)");
await bundle(
  "entry-session-replay.js",
  "logfire-session-replay-0.5.0.js",
  "@pydantic/logfire-session-replay 0.5.0 (MIT)",
);

async function bundle(entry, name, title) {
const out = join(dir, name);
const result = await build({
  entryPoints: [entry],
  bundle: true,
  format: "esm",
  platform: "browser",
  target: "es2020",
  minify: true,
  legalComments: "none", // collected into the header below instead
  metafile: true,
  write: false,
  logLevel: "warning",
});
if (result.outputFiles.length !== 1) throw new Error("expected exactly one output file");

// Every package with code in the bundle, found from the metafile's inputs.
const packages = new Map();
for (const input of Object.keys(result.metafile.inputs)) {
  const match = input.match(/node_modules\/((?:@[^/]+\/)?[^/]+)\//g);
  if (!match) continue;
  const root = input.slice(0, input.lastIndexOf(match[match.length - 1]) + match[match.length - 1].length);
  if (packages.has(root)) continue;
  const pkg = JSON.parse(readFileSync(join(root, "package.json"), "utf8"));
  const file = readdirSync(root).find((f) => /^(licen[cs]e|copying|notice)/i.test(f));
  packages.set(root, {
    name: pkg.name,
    version: pkg.version,
    license: pkg.license ?? "SEE LICENSE",
    text: file
      ? readFileSync(join(root, file), "utf8").trim()
      : LICENSE_FILES[pkg.name]
        ? readFileSync(LICENSE_FILES[pkg.name], "utf8").trim()
        : null,
  });
}
const sorted = [...packages.values()].sort((a, b) => a.name.localeCompare(b.name));
const lines = [
  `${title}, bundled by esbuild for Bazaar.`,
  "Build recipe: README-logfire-browser.md next to this file.",
  "",
  "Bundled packages:",
  ...sorted.map((p) => `  ${p.name}@${p.version} (${p.license})`),
  "",
  "License texts (each distinct text once, with the packages it covers):",
];
const byText = new Map();
for (const p of sorted) {
  const text = p.text ?? `${p.license} (no license file in the package)`;
  byText.set(text, [...(byText.get(text) ?? []), `${p.name}@${p.version}`]);
}
for (const [text, names] of byText) lines.push("", `--- ${names.join(", ")} ---`, ...text.split("\n"));
const header = "/*!\n" + lines.map((l) => (" * " + l.replaceAll("*/", "* /")).trimEnd()).join("\n") + "\n */\n";
writeFileSync(out, header + result.outputFiles[0].text);
console.log(`${out}: ${sorted.length} packages, ${(header.length + result.outputFiles[0].text.length)} bytes`);
}
