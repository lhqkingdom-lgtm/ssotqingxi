import fs from "node:fs/promises";
import path from "node:path";
import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

function parseArgs(argv) {
  const args = {};
  for (let i = 2; i < argv.length; i += 1) {
    const key = argv[i];
    if (!key.startsWith("--")) continue;
    const value = argv[i + 1] && !argv[i + 1].startsWith("--") ? argv[++i] : "true";
    args[key.slice(2)] = value;
  }
  return args;
}

function keyOf(row, fields) {
  return fields.map((field) => JSON.stringify(row[field] ?? null)).join("|");
}

function publicRow(row, fields) {
  return Object.fromEntries(fields.map((field) => [field, row[field] ?? null]));
}

function excelWallClockDate(value) {
  const match = String(value).match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})/);
  if (!match) return null;
  const [, year, month, day, hour, minute, second] = match.map(Number);
  return new Date(Date.UTC(year, month - 1, day, hour, minute, second));
}

function excelValue(field, value) {
  if (value === undefined || value === null) return null;
  if (field === "ingested_at") return excelWallClockDate(value);
  if (field === "package_types") {
    if (Array.isArray(value)) return JSON.stringify(value);
    return String(value);
  }
  if (field === "constraints" || field === "features" || field === "organization" || field === "conditions" || field === "af_code") {
    return null;
  }
  return value;
}

function columnName(index) {
  let value = index + 1;
  let result = "";
  while (value > 0) {
    const remainder = (value - 1) % 26;
    result = String.fromCharCode(65 + remainder) + result;
    value = Math.floor((value - 1) / 26);
  }
  return result;
}

function rangeGroups(rows) {
  const sorted = [...new Set(rows)].sort((a, b) => a - b);
  const groups = [];
  for (const row of sorted) {
    const last = groups.at(-1);
    if (!last || row > last.end + 1) groups.push({ start: row, end: row });
    else last.end = row;
  }
  return groups;
}

const args = parseArgs(process.argv);
for (const required of ["input", "template", "schema", "output", "row-map"]) {
  if (!args[required]) throw new Error(`Missing --${required}`);
}
const extracted = JSON.parse(await fs.readFile(args.input, "utf8"));
const schema = JSON.parse(await fs.readFile(args.schema, "utf8"));
const workbook = await SpreadsheetFile.importXlsx(await FileBlob.load(args.template));
const writeConflicts = [];
const rowMap = {};
const finalRows = {};

// Imported templates can retain thousands of formatted-but-empty rows even
// after clearing cells. Rebuild only the five schema worksheets so UsedRange,
// filters and downstream imports reflect the generated data exactly.
const rebuildAnchor = workbook.worksheets.add("__ssot_rebuild__");
for (const sheet of [...workbook.worksheets.items]) {
  if (sheet.name !== rebuildAnchor.name) sheet.delete();
}
for (const sheetName of Object.keys(schema.sheets)) workbook.worksheets.add(sheetName);
rebuildAnchor.delete();

for (const [sheetName, config] of Object.entries(schema.sheets)) {
  const sheet = workbook.worksheets.getItem(sheetName);
  const fields = config.fields;
  const incomingRows = extracted.data[sheetName] ?? [];
  const rows = incomingRows.map((row) => ({ ...publicRow(row, fields), _evidence_ids: row._evidence_ids ?? [] }));
  rows.sort((a, b) => config.key.map((field) => String(a[field] ?? "")).join("|").localeCompare(config.key.map((field) => String(b[field] ?? "")).join("|"), "en"));
  finalRows[sheetName] = rows;

  for (const table of [...sheet.tables.items]) table.delete();
  const used = sheet.getUsedRange();
  const existingRowCount = used?.values?.length ?? 1;
  const existingColumnCount = used?.values?.[0]?.length ?? fields.length;
  if (used && existingRowCount > 0 && existingColumnCount > 0) {
    // Explicit null matrices are reliably serialized by artifact-tool. A
    // range.clear() call can leave the template's cached cell values in the
    // exported XLSX on Windows.
    sheet.getRangeByIndexes(0, 0, existingRowCount, existingColumnCount).values = Array.from(
      { length: existingRowCount },
      () => Array(existingColumnCount).fill(null),
    );
    used.clear({ applyTo: "all" });
  }
  sheet.getRangeByIndexes(0, 0, 1, fields.length).values = [fields];
  if (rows.length) {
    const matrix = rows.map((row) => fields.map((field) => excelValue(field, row[field])));
    sheet.getRangeByIndexes(1, 0, matrix.length, fields.length).values = matrix;
  }

  const lastRow = Math.max(1, rows.length + 1);
  const lastCol = columnName(fields.length - 1);
  const table = sheet.tables.add(`A1:${lastCol}${lastRow}`, true, `${sheetName.replace(/[^A-Za-z0-9]/g, "_")}Table`);
  table.style = "TableStyleMedium2";
  sheet.freezePanes.freezeRows(1);
  sheet.getRange(`A1:${lastCol}1`).format = {
    fill: "#1F4E78",
    font: { bold: true, color: "#FFFFFF" },
    verticalAlignment: "center",
  };
  sheet.getRange(`A1:${lastCol}${lastRow}`).format.font = { name: "Calibri", size: 10 };
  sheet.getRange(`A1:${lastCol}${lastRow}`).format.rowHeight = 18;
  fields.forEach((field, index) => {
    const widthMap = {
      chip_id: 18, series: 12, core: 12, package_types: 22, pin_name: 14,
      function_name: 20, default_function: 28, peripheral_type: 18,
      param_name: 18, unit: 10, memory_type: 18, max_frequency_mhz: 22,
      pdf_source_file: 42, evidence_pages: 24, ingested_by: 16, ingested_at: 22, is_reviewed: 14,
    };
    sheet.getRange(`${columnName(index)}1:${columnName(index)}${lastRow}`).format.columnWidth = widthMap[field] ?? 14;
    if (field === "ingested_at") {
      sheet.getRange(`${columnName(index)}2:${columnName(index)}${lastRow}`).format.numberFormat = "yyyy-mm-dd hh:mm:ss";
    }
  });

  rows.forEach((row, index) => {
    const excelRow = index + 2;
    for (const evidenceId of row._evidence_ids ?? []) {
      rowMap[evidenceId] ??= {};
      rowMap[evidenceId][sheetName] ??= [];
      rowMap[evidenceId][sheetName].push(excelRow);
    }
  });
}

for (const [evidenceId, sheets] of Object.entries(rowMap)) {
  for (const [sheetName, rows] of Object.entries(sheets)) {
    sheets[sheetName] = {
      count: rows.length,
      ranges: rangeGroups(rows).map((group) => group.start === group.end ? String(group.start) : `${group.start}:${group.end}`),
    };
  }
}

for (const sheetName of ["run_summary", "review_index", "extraction_log"]) {
  const existing = workbook.worksheets.items.find((sheet) => sheet.name === sheetName);
  if (existing) existing.delete();
}

await fs.mkdir(path.dirname(args.output), { recursive: true });
const cleanXlsx = await SpreadsheetFile.exportXlsx(workbook);
await cleanXlsx.save(args.output);
await fs.writeFile(args["row-map"], JSON.stringify({ row_map: rowMap, write_conflicts: writeConflicts }, null, 2), "utf8");

const previewDir = path.join(path.dirname(args.output), "previews");
await fs.mkdir(previewDir, { recursive: true });
for (const [sheetName, rows] of Object.entries(finalRows)) {
  const fields = schema.sheets[sheetName].fields;
  const lastCol = columnName(fields.length - 1);
  const lastRow = Math.min(rows.length + 1, 31);
  const preview = await workbook.render({ sheetName, range: `A1:${lastCol}${Math.max(2, lastRow)}`, scale: 1.3, format: "png" });
  await fs.writeFile(path.join(previewDir, `${sheetName}.png`), new Uint8Array(await preview.arrayBuffer()));
}
console.log(JSON.stringify({ output: args.output, sheets: workbook.worksheets.items.length, formulasWritten: 0, writeConflicts: writeConflicts.length }));
// The bundled spreadsheet runtime can fault during native teardown on Windows
// after every artifact has already been flushed. Exit explicitly once all
// awaited writes and checks have completed so the one-click wrapper receives 0.
process.exit(0);
