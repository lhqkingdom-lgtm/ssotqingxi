import fs from "node:fs/promises";
import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const [cleanPath, evidencePath, schemaPath] = process.argv.slice(2);
if (!cleanPath || !evidencePath || !schemaPath) {
  throw new Error("Usage: node compare_workbooks.mjs <clean.xlsx> <evidence.xlsx> <schema.json>");
}
const schema = JSON.parse(await fs.readFile(schemaPath, "utf8"));
const clean = await SpreadsheetFile.importXlsx(await FileBlob.load(cleanPath));
const evidence = await SpreadsheetFile.importXlsx(await FileBlob.load(evidencePath));
const errors = [];
for (const [sheetName, config] of Object.entries(schema.sheets)) {
  const left = clean.worksheets.getItem(sheetName).getUsedRange()?.values ?? [];
  const right = evidence.worksheets.getItem(sheetName).getUsedRange()?.values ?? [];
  const width = config.fields.length;
  const height = Math.max(left.length, right.length);
  for (let row = 0; row < height; row += 1) {
    const leftRow = (left[row] ?? []).slice(0, width);
    const rightRow = (right[row] ?? []).slice(0, width);
    if (JSON.stringify(leftRow) !== JSON.stringify(rightRow)) {
      errors.push({ sheet: sheetName, row: row + 1, clean: leftRow, evidence: rightRow });
      if (errors.length >= 20) break;
    }
  }
}
console.log(JSON.stringify({ identicalDisplayedValues: errors.length === 0, errors }));
process.exit(errors.length ? 2 : 0);
