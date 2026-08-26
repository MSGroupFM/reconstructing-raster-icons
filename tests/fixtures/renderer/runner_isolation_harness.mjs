import { existsSync } from "node:fs";
import { runCanonicalRenderer } from "../../../scripts/render_svg.mjs";

const [mode, temporaryDirectory] = process.argv.slice(2);
if (!mode || !temporaryDirectory) {
  throw new Error("usage: runner_isolation_harness.mjs MODE TEMPORARY_DIRECTORY");
}

const outputPath = `${temporaryDirectory}/render.png`;
const renderMarker = `${temporaryDirectory}/wasm-render-called`;
const baseline = {
  permission_type: "object",
  allowed_read_capability: true,
  denied_read_capability: false,
  allowed_write_capability: true,
  child_capability: false,
  worker_capability: false,
  network_capability: false,
  filesystem_allowed: true,
  filesystem_denial: "ERR_ACCESS_DENIED",
  subprocess_denial: "ERR_ACCESS_DENIED",
  network_denial: "EPERM",
};
const mutations = {
  permission_type: ["permission_type", "undefined"],
  allowed_read_capability: ["allowed_read_capability", false],
  denied_read_capability: ["denied_read_capability", true],
  allowed_write_capability: ["allowed_write_capability", false],
  child_capability: ["child_capability", true],
  worker_capability: ["worker_capability", true],
  network_capability: ["network_capability", true],
  filesystem_allowed: ["filesystem_allowed", "ERR_ACCESS_DENIED"],
  filesystem_denial: ["filesystem_denial", "ALLOWED"],
  subprocess_denial: ["subprocess_denial", "ALLOWED"],
  network_denial: ["network_denial", "ECONNREFUSED"],
};
let emitted;
const dependencies = {
  collectCapabilities: async () => {
    if (mode === "probe_exception") {
      throw new Error("synthetic probe exception");
    }
    const evidence = { ...baseline };
    const mutation = mutations[mode];
    if (!mutation) {
      throw new Error(`unknown isolation mode ${mode}`);
    }
    evidence[mutation[0]] = mutation[1];
    return evidence;
  },
  renderSvg: async () => {
    const { writeFile } = await import("node:fs/promises");
    await writeFile(renderMarker, "render path entered");
    await writeFile(outputPath, "not a PNG");
  },
  emit: (record) => {
    emitted = record;
  },
};

const result = await runCanonicalRenderer(
  [
    `${temporaryDirectory}/candidate.svg`,
    outputPath,
    `${temporaryDirectory}/index_bg.wasm`,
    "128",
    "128",
    "runner-isolation-test-nonce",
    `${temporaryDirectory}/denied`,
  ],
  dependencies,
);

console.log(JSON.stringify({
  mode,
  result,
  emitted,
  render_called: existsSync(renderMarker),
  output_exists: existsSync(outputPath),
}));
