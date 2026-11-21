import { spawnSync, spawn } from "node:child_process";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const command = process.argv[2];
if (!["start", "stop"].includes(command)) throw new Error("Use start or stop");
const network = "cashly-be-local";
const options = { cwd: root, encoding: "utf8", windowsHide: true, timeout: 30000 };
const info = spawnSync("docker", ["info", "--format", "{{.OSType}}"], options);
if (info.status !== 0 || info.stdout.trim() !== "linux") {
  console.error("Supabase lokal memerlukan Docker Linux engine yang sudah aktif.");
  process.exit(1);
}
if (command === "start") {
  const found = spawnSync("docker", ["network", "inspect", network], options);
  if (found.status !== 0) {
    const created = spawnSync("docker", ["network", "create", "--driver", "bridge", "--opt", "com.docker.network.bridge.host_binding_ipv4=127.0.0.1", network], options);
    if (created.status !== 0) throw new Error("Gagal menyiapkan network Supabase lokal.");
  } else if (JSON.parse(found.stdout)[0].Options?.["com.docker.network.bridge.host_binding_ipv4"] !== "127.0.0.1") {
    throw new Error("Network lokal yang sudah ada harus bind port ke loopback.");
  }
}
// npm puts this repo's CLI on PATH. Do not install tools in the parent workspace.
const require = createRequire(import.meta.url);
const cli = require.resolve("supabase/package.json");
const metadata = require(cli);
const binary = typeof metadata.bin === "string" ? metadata.bin : metadata.bin.supabase;
const entry = resolve(dirname(cli), binary);
const child = spawn(process.execPath, [entry, command, ...(command === "start" ? ["--network-id", network] : [])], { cwd: root, stdio: "inherit", windowsHide: true });
child.on("error", () => { console.error("Supabase CLI belum siap; jalankan npm ci di repo BE."); process.exitCode = 1; });
child.on("exit", code => { process.exitCode = code ?? 1; });
