import fs from "node:fs/promises";
import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const [workbookPath, schemaPath, outputPath] = process.argv.slice(2);
if (!workbookPath || !schemaPath || !outputPath) {
  throw new Error("Usage: node export_reference.mjs <template.xlsx> <schema.json> <output.json>");
}
const schema = JSON.parse(await fs.readFile(schemaPath, "utf8"));
const workbook = await SpreadsheetFile.importXlsx(await FileBlob.load(workbookPath));
const result = { source: workbookPath, sheets: {} };
for (const [sheetName, config] of Object.entries(schema.sheets)) {
  const values = workbook.worksheets.getItem(sheetName).getUsedRange()?.values ?? [];
  const headers = (values[0] ?? []).map((value) => String(value ?? ""));
  const indexes = Object.fromEntries(config.fields.map((field) => [field, headers.indexOf(field)]));
  result.sheets[sheetName] = values.slice(1)
    .map((row) => Object.fromEntries(config.fields.map((field) => [field, indexes[field] >= 0 ? row[indexes[field]] ?? null : null])))
    .filter((row) => config.key.every((field) => row[field] !== null && row[field] !== ""));
}
await fs.writeFile(outputPath, JSON.stringify(result, null, 2), "utf8");
console.log(JSON.stringify({ output: outputPath, rows: Object.fromEntries(Object.entries(result.sheets).map(([sheet, rows]) => [sheet, rows.length])) }));
