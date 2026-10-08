import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

const [workbookPath, extractedPath, schemaPath] = process.argv.slice(2);
if (!workbookPath || !extractedPath || !schemaPath) {
  throw new Error("Usage: node validate_workbook_data.mjs <workbook.xlsx> <extracted.json> <schema.json>");
}

function pythonCandidates() {
  const candidates = [];
  if (process.env.SSOT_PYTHON) candidates.push(process.env.SSOT_PYTHON);
  if (process.platform === "win32" && process.env.USERPROFILE) {
    candidates.push(path.join(process.env.USERPROFILE, ".cache", "codex-runtimes", "codex-primary-runtime", "dependencies", "python", "python.exe"));
  }
  candidates.push(process.platform === "win32" ? "python" : "python3", "python");
  return [...new Set(candidates)].filter((candidate) => !path.isAbsolute(candidate) || fs.existsSync(candidate));
}

const validator = path.join(path.dirname(fileURLToPath(import.meta.url)), "validate_workbook_stream.py");
for (const python of pythonCandidates()) {
  const code = await new Promise((resolve) => {
    const child = spawn(python, [validator, "data", workbookPath, extractedPath, schemaPath], { stdio: "inherit", windowsHide: true });
    child.on("error", () => resolve(null));
    child.on("exit", (exitCode) => resolve(exitCode ?? 1));
  });
  if (code !== null) process.exit(code);
}
throw new Error("No Python 3 interpreter found; set SSOT_PYTHON");
