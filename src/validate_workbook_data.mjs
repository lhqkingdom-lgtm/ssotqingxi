import fs from "node:fs/promises";
import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const [workbookPath, extractedPath, schemaPath] = process.argv.slice(2);
if (!workbookPath || !extractedPath || !schemaPath) {
  throw new Error("Usage: node validate_workbook_data.mjs <workbook.xlsx> <extracted.json> <schema.json>");
}

const extracted = JSON.parse(await fs.readFile(extractedPath, "utf8"));
const schema = JSON.parse(await fs.readFile(schemaPath, "utf8"));
const workbook = await SpreadsheetFile.importXlsx(await FileBlob.load(workbookPath));
const report = { sheets: {}, differences: [] };

function keyText(row, fields) {
  return fields.map((field) => String(row[field] ?? "")).join("|");
}

function excelDateSerial(value) {
  const match = String(value ?? "").match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})/);
  if (!match) return null;
  const [, year, month, day, hour, minute, second] = match.map(Number);
  return Date.UTC(year, month - 1, day, hour, minute, second) / 86400000 + 25569;
}

function expectedValue(field, value) {
  if (value === undefined || value === null) return null;
  if (field === "ingested_at") return excelDateSerial(value);
  if (field === "package_types") return Array.isArray(value) ? JSON.stringify(value) : String(value);
  return value;
}

function equivalent(left, right) {
  if ((left === null || left === "" || left === undefined) && (right === null || right === "" || right === undefined)) return true;
  if (typeof left === "number" && typeof right === "number") return Math.abs(left - right) < 1e-8;
  return left === right;
}

for (const [sheetName, config] of Object.entries(schema.sheets)) {
  const sheet = workbook.worksheets.getItem(sheetName);
  const matrix = sheet.getUsedRange()?.values ?? [];
  const fields = config.fields;
  const actualRows = matrix.slice(1).filter((row) => row.slice(0, fields.length).some((value) => value !== null && value !== "" && value !== undefined));
  const expectedRows = (extracted.data[sheetName] ?? [])
    .map((row) => Object.fromEntries(fields.map((field) => [field, expectedValue(field, row[field])])))
    .sort((left, right) => keyText(left, config.key).localeCompare(keyText(right, config.key), "en"));
  if (actualRows.length !== expectedRows.length) {
    report.differences.push({ sheet: sheetName, kind: "row_count", expected: expectedRows.length, actual: actualRows.length });
  }
  const count = Math.min(actualRows.length, expectedRows.length);
  for (let rowIndex = 0; rowIndex < count; rowIndex += 1) {
    for (let column = 0; column < fields.length; column += 1) {
      const field = fields[column];
      const actual = actualRows[rowIndex][column] ?? null;
      const expected = expectedRows[rowIndex][field] ?? null;
      if (!equivalent(actual, expected)) {
        report.differences.push({ sheet: sheetName, row: rowIndex + 2, field, expected, actual });
        if (report.differences.length >= 100) break;
      }
    }
    if (report.differences.length >= 100) break;
  }
  report.sheets[sheetName] = { rows: actualRows.length, fields: fields.length };
}

console.log(JSON.stringify(report));
process.exit(report.differences.length ? 2 : 0);
