import { readFileSync } from "node:fs";
import { readFile, writeFile } from "node:fs/promises";
import { spawnSync } from "node:child_process";
import net from "node:net";
import { dirname } from "node:path";
import { initWasm, Resvg } from "../node_modules/@resvg/resvg-wasm/index.mjs";

const [inputPath, outputPath, wasmPath, widthText, heightText, nonce, deniedPath] =
  process.argv.slice(2);
if (!inputPath || !outputPath || !wasmPath || !widthText || !heightText || !nonce || !deniedPath) {
  throw new Error("usage: render_svg.mjs INPUT OUTPUT WASM WIDTH HEIGHT NONCE DENIED_PATH");
}

const width = Number(widthText);
const height = Number(heightText);
if (!Number.isSafeInteger(width) || !Number.isSafeInteger(height) || width <= 0 || height <= 0) {
  throw new Error("canonical dimensions must be positive safe integers");
}

const writeDirectory = dirname(outputPath);
const evidence = {
  nonce,
  exec_path: process.execPath,
  node_version: process.versions.node,
  release_name: process.release?.name,
  platform: process.platform,
  architecture: process.arch,
  permission_type: typeof process.permission,
  allowed_read_capability: process.permission?.has("fs.read", inputPath),
  denied_read_capability: process.permission?.has("fs.read", deniedPath),
  allowed_write_capability: process.permission?.has("fs.write", writeDirectory),
  child_capability: process.permission?.has("child"),
  worker_capability: process.permission?.has("worker"),
  network_capability: process.permission?.has("net"),
  denied_path: deniedPath,
  render_status: "error",
  render_error: null,
};

try {
  readFileSync(inputPath);
  evidence.filesystem_allowed = true;
} catch (error) {
  evidence.filesystem_allowed = error?.code ?? "UNKNOWN";
}
try {
  readFileSync(deniedPath);
  evidence.filesystem_denial = "ALLOWED";
} catch (error) {
  evidence.filesystem_denial = error?.code ?? "UNKNOWN";
}
try {
  spawnSync(process.execPath, ["--version"]);
  evidence.subprocess_denial = "ALLOWED";
} catch (error) {
  evidence.subprocess_denial = error?.code ?? "UNKNOWN";
}
evidence.network_denial = await new Promise((resolve) => {
  let settled = false;
  let socket;
  let timer;
  const finish = (value) => {
    if (!settled) {
      settled = true;
      clearTimeout(timer);
      socket?.destroy();
      resolve(value);
    }
  };
  try {
    socket = net.connect({ host: "127.0.0.1", port: 1 });
    socket.once("connect", () => finish("ALLOWED"));
    socket.once("error", (error) => finish(error?.code ?? "UNKNOWN"));
    timer = setTimeout(() => finish("TIMEOUT"), 1000);
  } catch (error) {
    resolve(error?.code ?? "UNKNOWN");
  }
});

try {
  const [sourceBytes, wasmBytes] = await Promise.all([readFile(inputPath), readFile(wasmPath)]);
  await initWasm(wasmBytes);

  // Safe-subset validation has already rejected scripts, CSS, links, and
  // non-UTF-8 input. Resolve the only context-dependent paint deterministically.
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
      evidence.render_status = "ok";
    } finally {
      rendered.free();
    }
  } finally {
    renderer.free();
  }
} catch (error) {
  evidence.render_error = String(error?.message ?? error);
  process.exitCode = 1;
}

console.log(JSON.stringify(evidence));
