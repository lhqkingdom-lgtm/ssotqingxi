import fs from "node:fs/promises";
import path from "node:path";
import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";


const [workbookPath, outputDir] = process.argv.slice(2);
if (!workbookPath || !outputDir) {
  throw new Error("Usage: node render_workbook_sheets.mjs <workbook.xlsx> <output-dir>");
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

await fs.mkdir(outputDir, { recursive: true });
const workbook = await SpreadsheetFile.importXlsx(await FileBlob.load(workbookPath));
const rendered = [];
for (const sheet of workbook.worksheets.items) {
  const matrix = sheet.getUsedRange()?.values ?? [];
  const rowCount = Math.min(matrix.length, 25);
  const columnCount = Math.max(1, ...matrix.slice(0, rowCount).map((row) => row.length));
  const range = `A1:${columnName(columnCount - 1)}${Math.max(rowCount, 1)}`;
  const preview = await workbook.render({ sheetName: sheet.name, range, scale: 1.25, format: "png" });
  const outputPath = path.join(outputDir, `${sheet.name}.png`);
  await fs.writeFile(outputPath, new Uint8Array(await preview.arrayBuffer()));
  rendered.push({ sheet: sheet.name, range, output: outputPath });
}
console.log(JSON.stringify({ rendered }, null, 2));
