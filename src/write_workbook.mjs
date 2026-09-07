import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

function pythonCandidates() {
  const candidates = [];
  if (process.env.SSOT_PYTHON) candidates.push(process.env.SSOT_PYTHON);
  if (process.platform === "win32" && process.env.USERPROFILE) {
    candidates.push(path.join(
      process.env.USERPROFILE,
      ".cache", "codex-runtimes", "codex-primary-runtime", "dependencies", "python", "python.exe",
    ));
  }
  candidates.push(process.platform === "win32" ? "python" : "python3", "python");
  return [...new Set(candidates)].filter((candidate) => !path.isAbsolute(candidate) || fs.existsSync(candidate));
}

const writer = path.join(path.dirname(fileURLToPath(import.meta.url)), "write_workbook_stream.py");
const forwarded = process.argv.slice(2);

async function run() {
  let lastError;
  for (const python of pythonCandidates()) {
    const code = await new Promise((resolve, reject) => {
      const child = spawn(python, [writer, ...forwarded], { stdio: "inherit", windowsHide: true });
      child.on("error", reject);
      child.on("exit", (exitCode) => resolve(exitCode ?? 1));
    }).catch((error) => {
      lastError = error;
      return null;
    });
    if (code === 0) return;
    if (code !== null) process.exit(code);
  }
  throw lastError ?? new Error("No Python 3 interpreter found; set SSOT_PYTHON");
}

await run();
