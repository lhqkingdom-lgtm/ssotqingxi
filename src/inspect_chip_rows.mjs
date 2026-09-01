import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const [workbookPath, pattern = "MI6090A"] = process.argv.slice(2);
if (!workbookPath) throw new Error("Usage: node inspect_chip_rows.mjs <workbook.xlsx> [pattern]");
const workbook = await SpreadsheetFile.importXlsx(await FileBlob.load(workbookPath));
const sheet = workbook.worksheets.getItem("chips");
const values = sheet.getUsedRange()?.values ?? [];
const headers = values[0]?.map((value) => String(value ?? "")) ?? [];
const rows = values.slice(1)
  .map((row, index) => ({ excel_row: index + 2, ...Object.fromEntries(headers.map((field, column) => [field, row[column] ?? null])) }))
  .filter((row) => String(row.chip_id ?? "").toUpperCase().includes(pattern.toUpperCase()));
console.log(JSON.stringify({ workbook: workbookPath, rows }, null, 2));
