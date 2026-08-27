import { readFileSync } from "node:fs";
import { readFile, writeFile } from "node:fs/promises";
import { spawnSync } from "node:child_process";
import { dirname } from "node:path";
import { pathToFileURL } from "node:url";
import { initWasm, Resvg } from "../node_modules/@resvg/resvg-wasm/index.mjs";

function runtimeIdentity(nonce) {
  const oldSpaceArgument = process.execArgv.find((value) => value.startsWith("--max-old-space-size="));
  const oldSpaceValue = oldSpaceArgument?.slice("--max-old-space-size=".length);
  return {
    nonce,
    exec_path: process.execPath,
    node_version: process.versions.node,
    release_name: process.release?.name,
    platform: process.platform,
    architecture: process.arch,
    v8_old_space_mib: oldSpaceValue && /^\d+$/.test(oldSpaceValue)
      ? Number(oldSpaceValue)
      : null,
    wasm_trap_handler_disabled: process.execArgv.includes("--disable-wasm-trap-handler"),
  };
}

async function collectCapabilities({ inputPath, deniedPath, writeDirectory }) {
  const evidence = {
    permission_type: typeof process.permission,
    allowed_read_capability: process.permission?.has("fs.read", inputPath),
    denied_read_capability: process.permission?.has("fs.read", deniedPath),
    allowed_write_capability: process.permission?.has("fs.write", writeDirectory),
    child_capability: process.permission?.has("child"),
    worker_capability: process.permission?.has("worker"),
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
  return evidence;
}

function runtimeControlFailure(evidence) {
  const exact = {
    v8_old_space_mib: 512,
    wasm_trap_handler_disabled: true,
    permission_type: "object",
    allowed_read_capability: true,
    denied_read_capability: false,
    allowed_write_capability: true,
    child_capability: false,
    worker_capability: false,
    filesystem_allowed: true,
    filesystem_denial: "ERR_ACCESS_DENIED",
    subprocess_denial: "ERR_ACCESS_DENIED",
  };
  for (const [key, value] of Object.entries(exact)) {
    if (evidence[key] !== value) {
      return key;
    }
  }
  return null;
}

async function renderSvg({ inputPath, outputPath, wasmPath, width, height }) {
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
    } finally {
      rendered.free();
    }
  } finally {
    renderer.free();
  }
}

const defaultDependencies = {
  collectCapabilities,
  renderSvg,
  emit: (record) => console.log(JSON.stringify(record)),
};

export async function runCanonicalRenderer(argv, dependencies = defaultDependencies) {
  const [inputPath, outputPath, wasmPath, widthText, heightText, nonce, deniedPath] = argv;
  if (!inputPath || !outputPath || !wasmPath || !widthText || !heightText || !nonce || !deniedPath) {
    throw new Error("usage: render_svg.mjs INPUT OUTPUT WASM WIDTH HEIGHT NONCE DENIED_PATH");
  }

  const width = Number(widthText);
  const height = Number(heightText);
  if (!Number.isSafeInteger(width) || !Number.isSafeInteger(height) || width <= 0 || height <= 0) {
    throw new Error("canonical dimensions must be positive safe integers");
  }

  const identity = runtimeIdentity(nonce);
  let capabilities;
  try {
    capabilities = await dependencies.collectCapabilities({
      inputPath,
      deniedPath,
      writeDirectory: dirname(outputPath),
    });
  } catch {
    dependencies.emit({
      ...identity,
      render_status: "runtime_control_failure",
      runtime_control_failure: "probe_exception",
    });
    return 1;
  }
  const failure = runtimeControlFailure({ ...identity, ...capabilities });
  if (failure !== null) {
    dependencies.emit({
      ...identity,
      render_status: "runtime_control_failure",
      runtime_control_failure: failure,
    });
    return 1;
  }

  const evidence = {
    ...identity,
    ...capabilities,
    denied_path: deniedPath,
    render_status: "error",
    render_error: null,
  };
  try {
    await dependencies.renderSvg({ inputPath, outputPath, wasmPath, width, height });
    evidence.render_status = "ok";
  } catch (error) {
    evidence.render_error = String(error?.message ?? error);
    dependencies.emit(evidence);
    return 1;
  }
  dependencies.emit(evidence);
  return 0;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  process.exitCode = await runCanonicalRenderer(process.argv.slice(2));
}
