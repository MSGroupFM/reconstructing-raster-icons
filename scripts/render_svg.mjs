import { readFile, writeFile } from "node:fs/promises";
import { initWasm, Resvg } from "../node_modules/@resvg/resvg-wasm/index.mjs";

const [inputPath, outputPath, wasmPath, widthText, heightText] = process.argv.slice(2);
if (!inputPath || !outputPath || !wasmPath || !widthText || !heightText) {
  throw new Error("usage: render_svg.mjs INPUT OUTPUT WASM WIDTH HEIGHT");
}

const width = Number(widthText);
const height = Number(heightText);
if (!Number.isSafeInteger(width) || !Number.isSafeInteger(height) || width <= 0 || height <= 0) {
  throw new Error("canonical dimensions must be positive safe integers");
}

const [sourceBytes, wasmBytes] = await Promise.all([readFile(inputPath), readFile(wasmPath)]);
await initWasm(wasmBytes);

// Safe-subset validation has already rejected scripts, CSS, links, and non-UTF-8
// input. Resolve the only context-dependent paint value deterministically.
const source = new TextDecoder("utf-8", { fatal: true })
  .decode(sourceBytes)
  .replaceAll("currentColor", "#000000");
const fitTo = width >= height
  ? { mode: "width", value: width }
  : { mode: "height", value: height };
const renderer = new Resvg(source, {
  font: { loadSystemFonts: false },
  shapeRendering: 2,
  textRendering: 2,
  fitTo,
});

try {
  const rendered = renderer.render();
  try {
    if (rendered.width !== width || rendered.height !== height) {
      throw new Error(
        `rendered dimensions ${rendered.width}x${rendered.height} do not match ${width}x${height}`,
      );
    }
    await writeFile(outputPath, rendered.asPng(), { flag: "wx", mode: 0o600 });
  } finally {
    rendered.free();
  }
} finally {
  renderer.free();
}
