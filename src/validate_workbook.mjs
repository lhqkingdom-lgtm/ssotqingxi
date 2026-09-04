import fs from "node:fs/promises";
import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const [workbookPath, schemaPath, mode = "clean"] = process.argv.slice(2);
if (!workbookPath || !schemaPath) {
  throw new Error("Usage: node validate_workbook.mjs <workbook.xlsx> <schema.json> [clean|evidence]");
}
if (!["clean", "evidence"].includes(mode)) throw new Error(`Unknown validation mode: ${mode}`);

const schema = JSON.parse(await fs.readFile(schemaPath, "utf8"));
const workbook = await SpreadsheetFile.importXlsx(await FileBlob.load(workbookPath));
const report = { mode, sheets: {}, errors: [], formulas: 0 };
const expectedSheets = Object.keys(schema.sheets);
const actualSheets = workbook.worksheets.items.map((sheet) => sheet.name);
if (JSON.stringify(actualSheets) !== JSON.stringify(expectedSheets)) {
  report.errors.push({ kind: "sheet_structure", expected: expectedSheets, actual: actualSheets });
}

for (const [sheetName, config] of Object.entries(schema.sheets)) {
  const sheet = workbook.worksheets.getItem(sheetName);
  const values = sheet.getUsedRange()?.values ?? [];
  const formulas = sheet.getUsedRange()?.formulas ?? [];
  const headers = (values[0] ?? []).slice(0, config.fields.length).map((value) => String(value ?? ""));
  const usedColumnCount = Math.max(0, ...values.map((row) => row.length));
  if (usedColumnCount !== config.fields.length) {
    report.errors.push({ sheet: sheetName, kind: "column_count", expected: config.fields.length, actual: usedColumnCount });
  }
  if (JSON.stringify(headers) !== JSON.stringify(config.fields)) {
    report.errors.push({ sheet: sheetName, kind: "header_mismatch", expected: config.fields, actual: headers });
  }
  for (let row = 0; row < values.length; row += 1) {
    for (let column = 0; column < values[row].length; column += 1) {
      const value = String(values[row][column] ?? "");
      if (/^(?:#REF!|#DIV\/0!|#VALUE!|#NAME\?|#N\/A)$/.test(value)) {
        report.errors.push({ sheet: sheetName, kind: "formula_error", row: row + 1, column: column + 1, value });
      }
    }
  }
  const nonBlankRows = values.slice(1).filter((row) => row.slice(0, config.fields.length).some((value) => value !== null && value !== "" && value !== undefined));
  if (nonBlankRows.length !== Math.max(0, values.length - 1)) {
    report.errors.push({ sheet: sheetName, kind: "blank_formatted_rows", count: values.length - 1 - nonBlankRows.length });
  }
  let formulaCount = 0;
  const allowedColumn = config.fields.indexOf(config.evidence_link_field);
  for (let row = 1; row < formulas.length; row += 1) {
    for (let column = 0; column < (formulas[row]?.length ?? 0); column += 1) {
      const formula = String(formulas[row][column] ?? "");
      if (!formula.startsWith("=")) continue;
      formulaCount += 1;
      if (mode === "clean") report.errors.push({ sheet: sheetName, kind: "formula_in_clean_workbook", row: row + 1, column: column + 1 });
      if (mode === "evidence" && column !== allowedColumn) report.errors.push({ sheet: sheetName, kind: "formula_in_wrong_column", row: row + 1, column: column + 1 });
      if (mode === "evidence" && !/^=HYPERLINK\("evidence\/index\.html#row-[0-9a-f]{16}",/.test(formula)) {
        report.errors.push({ sheet: sheetName, kind: "invalid_evidence_formula", row: row + 1, column: column + 1, formula });
      }
    }
  }
  if (mode === "evidence" && formulaCount !== 0) report.errors.push({ sheet: sheetName, kind: "formula_in_evidence_workbook", actual: formulaCount });
  report.formulas += formulaCount;
  report.sheets[sheetName] = {
    rows: nonBlankRows.length,
    used_range_rows: Math.max(0, values.length - 1),
    blank_formatted_rows: Math.max(0, values.length - 1 - nonBlankRows.length),
    columns: headers.length,
    formulas: formulaCount,
  };
}

console.log(JSON.stringify(report));
process.exit(report.errors.length ? 2 : 0);
