import fs from "node:fs/promises";
import path from "node:path";
import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

function parseArgs(argv) {
  const args = {};
  for (let i = 2; i < argv.length; i += 1) {
    if (!argv[i].startsWith("--")) continue;
    const key = argv[i].slice(2);
    args[key] = argv[i + 1] && !argv[i + 1].startsWith("--") ? argv[++i] : "true";
  }
  return args;
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

function expandRanges(ranges, limit = 40) {
  const rows = [];
  for (const range of ranges ?? []) {
    const [startText, endText] = String(range).split(":");
    const start = Number(startText);
    const end = Number(endText ?? startText);
    for (let row = start; row <= end && rows.length < limit; row += 1) rows.push(row);
    if (rows.length >= limit) break;
  }
  return rows;
}

const args = parseArgs(process.argv);
for (const required of ["workbook", "row-map", "schema", "output-dir"]) {
  if (!args[required]) throw new Error(`Missing --${required}`);
}
await fs.mkdir(args["output-dir"], { recursive: true });
const workbook = await SpreadsheetFile.importXlsx(await FileBlob.load(args.workbook));
const rowMapPayload = JSON.parse(await fs.readFile(args["row-map"], "utf8"));
const rowMap = rowMapPayload.row_map ?? {};
const schema = JSON.parse(await fs.readFile(args.schema, "utf8"));
const temp = workbook.worksheets.add("_evidence_render_temp");
temp.showGridLines = false;
let rendered = 0;

for (const [evidenceId, sheetLocations] of Object.entries(rowMap)) {
  temp.getRange("A1:Z250").clear({ applyTo: "all" });
  let targetRow = 1;
  let maxColumns = 1;
  for (const [sheetName, location] of Object.entries(sheetLocations)) {
    const source = workbook.worksheets.getItem(sheetName);
    const fields = schema.sheets[sheetName]?.fields ?? [];
    if (!fields.length) continue;
    maxColumns = Math.max(maxColumns, fields.length);
    const titleRange = temp.getRangeByIndexes(targetRow - 1, 0, 1, fields.length);
    titleRange.merge(true);
    temp.getRangeByIndexes(targetRow - 1, 0, 1, 1).values = [[`${sheetName} | ${evidenceId} | ${location.count} row(s)`]];
    titleRange.format = { fill: "#D9EAF7", font: { bold: true, color: "#1F1F1F" } };
    targetRow += 1;
    temp.getRangeByIndexes(targetRow - 1, 0, 1, fields.length).values = [fields];
    temp.getRangeByIndexes(targetRow - 1, 0, 1, fields.length).format = { fill: "#1F4E78", font: { bold: true, color: "#FFFFFF" } };
    targetRow += 1;
    const rows = expandRanges(location.ranges, 40);
    const values = rows.map((row) => source.getRangeByIndexes(row - 1, 0, 1, fields.length).values[0]);
    if (values.length) {
      temp.getRangeByIndexes(targetRow - 1, 0, values.length, fields.length).values = values;
      targetRow += values.length;
    }
    targetRow += 1;
  }
  if (targetRow === 1) continue;
  const lastCol = columnName(maxColumns - 1);
  temp.getRange(`A1:${lastCol}${targetRow}`).format.font = { name: "Calibri", size: 10 };
  temp.getRange(`A1:${lastCol}${targetRow}`).format.autofitColumns();
  temp.getRange(`A1:${lastCol}${targetRow}`).format.autofitRows();
  const preview = await workbook.render({ sheetName: temp.name, range: `A1:${lastCol}${targetRow - 1}`, scale: 1.25, format: "png" });
  await fs.writeFile(path.join(args["output-dir"], `${evidenceId}.png`), new Uint8Array(await preview.arrayBuffer()));
  rendered += 1;
}

console.log(JSON.stringify({ rendered, outputDir: args["output-dir"] }));
