import { build } from "esbuild";
import { mkdir, readFile, writeFile } from "node:fs/promises";
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
process.stdout.write("Built dist/panel.html\n");
