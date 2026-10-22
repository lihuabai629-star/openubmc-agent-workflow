import { build } from "esbuild";
import { mkdir, readFile, readdir, writeFile } from "node:fs/promises";
import { createHash } from "node:crypto";
import { fileURLToPath } from "node:url";

const root = new URL("../", import.meta.url);
const bundle = await build({ entryPoints: [fileURLToPath(new URL("web/app.js", root))], bundle: true,
  write: false, minify: true, format: "iife", target: "es2022", legalComments: "none" });
const template = await readFile(new URL("web/panel.html", root), "utf8");
const css = await readFile(new URL("web/panel.css", root), "utf8");
// One self-contained resource: the sandbox does not need a CDN or a direct network connection.
const html = template.replace("<!--STYLE-->", () => `<style>${css}</style>`)
  .replace("<!--SCRIPT-->", () => `<script>${bundle.outputFiles[0].text.replace(/<\/script/gi, "<\\/script")}</script>`);
await mkdir(new URL("dist/", root), { recursive: true });
await writeFile(new URL("dist/panel.html", root), html);
const packaged = new URL("../plugin/openubmc/panel/", root);
await mkdir(packaged, { recursive: true });
await writeFile(new URL("panel.html", packaged), html);
await build({ entryPoints: [fileURLToPath(new URL("src/native.js", root))], bundle: true,
  outfile: fileURLToPath(new URL("native.mjs", packaged)), platform: "node", format: "esm",
  target: "node20", minify: true, legalComments: "eof",
  define: { OPENUBMC_PANEL_BUNDLE: "true" },
  banner: { js: 'import { createRequire as _createRequire } from "node:module"; const require = _createRequire(import.meta.url);' }
});
const inputs = ["package.json", "package-lock.json", "scripts/build.mjs"];
for (const directory of ["src", "web"]) {
  for (const name of await readdir(new URL(`${directory}/`, root))) inputs.push(`${directory}/${name}`);
}
const sources = {};
for (const name of inputs.sort()) sources[`codex-panel/${name}`] = createHash("sha256")
  .update(await readFile(new URL(name, root))).digest("hex");
await writeFile(new URL("source.json", packaged), JSON.stringify(sources, null, 2) + "\n");
process.stdout.write("Built local resource and packaged Codex panel\n");
