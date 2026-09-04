const assert = require("node:assert/strict");
const workflow = require("../src/review_app/static/review_state.js");

function memoryStorage() {
  const values = new Map();
  return {
    getItem: (key) => values.has(key) ? values.get(key) : null,
    setItem: (key, value) => values.set(key, value),
  };
}

const meta = { run_id: "run-a", base_extracted_sha256: "abc" };
const storage = memoryStorage();
const state = workflow.loadState(storage, meta, [
  { chip_id: "MCU1", is_reviewed: false },
  { chip_id: "MCU2", is_reviewed: true },
]);

assert.equal(state.chips.MCU1.decision, "unreviewed");
assert.equal(workflow.reviewStatus(state.chips.MCU1), "unreviewed");
assert.equal(state.chips.MCU2.decision, "approved");
assert.equal(workflow.reviewStatus(state.chips.MCU2), "exported");

workflow.setDecision(state, "MCU1", "approved", { at: "2026-09-03T08:00:00Z", content_hash: "h1" });
assert.equal(state.chips.MCU1.review_revision, 1);
assert.equal(workflow.reviewStatus(state.chips.MCU1), "pending_export");
assert.deepEqual(workflow.exportChipIds(state, "pending_export"), ["MCU1"]);

const firstBatch = workflow.createExportBatch(
  state,
  "pending_export",
  { MCU1: { tables: { chips: [{ chip_id: "MCU1", is_reviewed: true }] } } },
  { at: "2026-09-03T08:30:00Z", batch_id: "batch-1" },
);
assert.equal(firstBatch.export_batch_id, "batch-1");
assert.deepEqual(firstBatch.chips.map((chip) => chip.chip_id), ["MCU1"]);
assert.equal(workflow.reviewStatus(state.chips.MCU1), "exported");
assert.deepEqual(workflow.exportChipIds(state, "pending_export"), []);
assert.deepEqual(workflow.exportChipIds(state, "all_reviewed").sort(), ["MCU1", "MCU2"]);

workflow.setDecision(state, "MCU1", "issue", { at: "2026-09-03T08:35:00Z" });
workflow.setDecision(state, "MCU1", "approved", { at: "2026-09-03T08:36:00Z", content_hash: "h2" });
const preparedBatch = workflow.buildExportBatch(
  state,
  "pending_export",
  { MCU1: { tables: { chips: [{ chip_id: "MCU1", is_reviewed: true }] } } },
  { at: "2026-09-03T08:40:00Z", batch_id: "batch-prepared" },
);
assert.equal(workflow.reviewStatus(state.chips.MCU1), "pending_export", "preparing a workbook must not mark it exported");
workflow.commitExportBatch(state, preparedBatch);
assert.equal(workflow.reviewStatus(state.chips.MCU1), "exported");

// Restore the first exported revision for the repeated-approval check.
state.chips.MCU1.review_revision = 1;
state.chips.MCU1.exported_revision = 1;
state.chips.MCU1.decision = "approved";
state.chips.MCU1.content_hash = "h1";

workflow.setDecision(state, "MCU1", "approved", { at: "2026-09-03T09:00:00Z", content_hash: "h1" });
assert.equal(state.chips.MCU1.review_revision, 1, "repeated approval must be idempotent");

workflow.setDecision(state, "MCU1", "issue", { at: "2026-09-03T10:00:00Z" });
assert.equal(state.chips.MCU1.review_revision, 2);
assert.equal(workflow.reviewStatus(state.chips.MCU1), "issue");

workflow.saveState(storage, state);
const restored = workflow.loadState(storage, meta, [{ chip_id: "MCU1" }, { chip_id: "MCU3" }]);
assert.equal(restored.chips.MCU1.review_revision, 2);
assert.equal(restored.chips.MCU3.decision, "unreviewed");

const otherRun = workflow.loadState(storage, { run_id: "run-b", base_extracted_sha256: "abc" }, [{ chip_id: "MCU1" }]);
assert.equal(otherRun.chips.MCU1.review_revision, 0, "different runs must not share review state");

const baseRecords = [
  { row_id: "r1", values: { chip_id: "MCU1", peripheral_type: "ADC", instance_count: 1 } },
  { row_id: "r2", values: { chip_id: "MCU1", peripheral_type: "UART", instance_count: 2 } },
];
workflow.saveRecord(state, "MCU1", "peripherals", {
  row_id: "r1", values: { chip_id: "MCU1", peripheral_type: "ADC", instance_count: 3 },
});
workflow.deleteRecord(state, "MCU1", "peripherals", "r2");
workflow.saveRecord(state, "MCU1", "peripherals", {
  row_id: "manual-r3", values: { chip_id: "MCU1", peripheral_type: "SPI", instance_count: 1 },
}, { is_new: true });
const changedRecords = workflow.applyRecordChanges(state, "MCU1", "peripherals", baseRecords);
assert.deepEqual(changedRecords.map((record) => record.values.peripheral_type), ["ADC", "SPI"]);
assert.equal(changedRecords[0].values.instance_count, 3);
assert.equal(workflow.hasSheetChanges(state, "MCU1", "peripherals"), true);
workflow.resetSheetChanges(state, "MCU1", "peripherals");
assert.deepEqual(workflow.applyRecordChanges(state, "MCU1", "peripherals", baseRecords), baseRecords);

console.log("review_state tests: PASS");
