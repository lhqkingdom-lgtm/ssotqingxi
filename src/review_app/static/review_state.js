(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.ReviewWorkflow = api;
})(typeof window !== "undefined" ? window : null, function () {
  "use strict";

  const STATE_VERSION = 1;
  const DECISIONS = new Set(["unreviewed", "issue", "approved"]);

  function nowIso() {
    return new Date().toISOString();
  }

  function storageKey(meta) {
    return `mcu-review-state:v${STATE_VERSION}:${meta.run_id}:${meta.base_extracted_sha256}`;
  }

  function defaultChipState(initialReviewed = false) {
    return {
      decision: initialReviewed ? "approved" : "unreviewed",
      review_revision: initialReviewed ? 1 : 0,
      reviewed_at: initialReviewed ? nowIso() : null,
      issue_at: null,
      content_hash: null,
      exported_revision: initialReviewed ? 1 : 0,
    };
  }

  function createState(meta, chips) {
    const chipStates = {};
    for (const chip of chips) {
      chipStates[chip.chip_id] = defaultChipState(Boolean(chip.is_reviewed));
    }
    return {
      state_version: STATE_VERSION,
      run_id: meta.run_id,
      base_extracted_sha256: meta.base_extracted_sha256,
      updated_at: nowIso(),
      chips: chipStates,
      record_changes: {},
      export_batches: [],
    };
  }

  function isCompatible(state, meta) {
    return Boolean(
      state &&
      state.state_version === STATE_VERSION &&
      state.run_id === meta.run_id &&
      state.base_extracted_sha256 === meta.base_extracted_sha256 &&
      state.chips && typeof state.chips === "object"
    );
  }

  function mergeChips(state, chips) {
    if (!Array.isArray(state.export_batches)) state.export_batches = [];
    if (!state.record_changes || typeof state.record_changes !== "object") state.record_changes = {};
    for (const chip of chips) {
      if (!state.chips[chip.chip_id]) {
        state.chips[chip.chip_id] = defaultChipState(Boolean(chip.is_reviewed));
      }
    }
    return state;
  }

  function sheetChanges(state, chipId, sheet, create = false) {
    if (!state.record_changes) state.record_changes = {};
    if (create) {
      state.record_changes[chipId] ??= {};
      state.record_changes[chipId][sheet] ??= { updated: {}, deleted: [], added: [] };
    }
    return state.record_changes[chipId]?.[sheet] || { updated: {}, deleted: [], added: [] };
  }

  function applyRecordChanges(state, chipId, sheet, baseRecords) {
    const changes = sheetChanges(state, chipId, sheet);
    const deleted = new Set(changes.deleted || []);
    const records = (baseRecords || [])
      .filter((record) => !deleted.has(record.row_id))
      .map((record) => changes.updated?.[record.row_id]
        ? { ...record, values: { ...record.values, ...changes.updated[record.row_id] }, manually_changed: true }
        : record);
    for (const record of changes.added || []) records.push({ ...record, manually_added: true });
    return records;
  }

  function saveRecord(state, chipId, sheet, record, options = {}) {
    if (!record || !record.row_id || !record.values) throw new Error("invalid review record");
    const changes = sheetChanges(state, chipId, sheet, true);
    if (options.is_new) {
      if (changes.added.some((item) => item.row_id === record.row_id)) throw new Error(`duplicate row_id: ${record.row_id}`);
      changes.added.push(record);
    } else {
      const added = changes.added.find((item) => item.row_id === record.row_id);
      if (added) added.values = { ...record.values };
      else changes.updated[record.row_id] = { ...record.values };
      changes.deleted = changes.deleted.filter((rowId) => rowId !== record.row_id);
    }
    state.updated_at = options.at || nowIso();
    return record;
  }

  function deleteRecord(state, chipId, sheet, rowId, options = {}) {
    const changes = sheetChanges(state, chipId, sheet, true);
    const addedIndex = changes.added.findIndex((item) => item.row_id === rowId);
    if (addedIndex >= 0) changes.added.splice(addedIndex, 1);
    else if (!changes.deleted.includes(rowId)) changes.deleted.push(rowId);
    delete changes.updated[rowId];
    state.updated_at = options.at || nowIso();
  }

  function resetSheetChanges(state, chipId, sheet, options = {}) {
    if (state.record_changes?.[chipId]) delete state.record_changes[chipId][sheet];
    state.updated_at = options.at || nowIso();
  }

  function hasSheetChanges(state, chipId, sheet) {
    const changes = sheetChanges(state, chipId, sheet);
    return Boolean(Object.keys(changes.updated || {}).length || (changes.deleted || []).length || (changes.added || []).length);
  }

  function loadState(storage, meta, chips) {
    const key = storageKey(meta);
    let saved = null;
    try {
      saved = JSON.parse(storage.getItem(key) || "null");
    } catch (_) {
      saved = null;
    }
    const state = isCompatible(saved, meta) ? mergeChips(saved, chips) : createState(meta, chips);
    storage.setItem(key, JSON.stringify(state));
    return state;
  }

  function saveState(storage, state) {
    state.updated_at = nowIso();
    storage.setItem(storageKey(state), JSON.stringify(state));
    return state;
  }

  function setDecision(state, chipId, decision, options = {}) {
    if (!DECISIONS.has(decision)) throw new Error(`unsupported review decision: ${decision}`);
    const chip = state.chips[chipId];
    if (!chip) throw new Error(`unknown chip_id: ${chipId}`);
    const contentHash = options.content_hash ?? chip.content_hash;
    const changed = chip.decision !== decision || chip.content_hash !== contentHash;
    if (!changed) return chip;
    chip.review_revision += 1;
    chip.decision = decision;
    chip.content_hash = contentHash;
    const timestamp = options.at || nowIso();
    chip.reviewed_at = decision === "approved" ? timestamp : null;
    chip.issue_at = decision === "issue" ? timestamp : null;
    state.updated_at = timestamp;
    return chip;
  }

  function reviewStatus(chip) {
    if (chip.decision === "issue") return "issue";
    if (chip.decision !== "approved") return "unreviewed";
    if ((chip.synced_revision || 0) >= chip.review_revision) return "exported";
    if (chip.exported_revision >= chip.review_revision) return "exported";
    return "pending_export";
  }

  function exportChipIds(state, scope, currentChipId = null) {
    const approved = Object.entries(state.chips)
      .filter(([, chip]) => chip.decision === "approved")
      .map(([chipId]) => chipId);
    if (scope === "all_reviewed") return approved;
    if (scope === "current") return currentChipId && approved.includes(currentChipId) ? [currentChipId] : [];
    if (scope === "pending_export") {
      return approved.filter((chipId) => state.chips[chipId].review_revision > state.chips[chipId].exported_revision);
    }
    throw new Error(`unsupported export scope: ${scope}`);
  }

  function buildExportBatch(state, scope, payloadsByChip, options = {}) {
    const createdAt = options.at || nowIso();
    const chipIds = exportChipIds(state, scope, options.current_chip_id || null);
    const chips = chipIds.map((chipId) => {
      const review = state.chips[chipId];
      const payload = payloadsByChip[chipId];
      if (!payload) throw new Error(`missing export payload for chip_id: ${chipId}`);
      return {
        chip_id: chipId,
        review_revision: review.review_revision,
        reviewed_at: review.reviewed_at,
        content_hash: review.content_hash,
        is_reviewed: true,
        tables: payload.tables,
      };
    });
    if (!chips.length) return null;
    const batchId = options.batch_id || `${state.run_id}-${createdAt.replace(/[^0-9]/g, "").slice(0, 14)}-${state.export_batches.length + 1}`;
    const batch = {
      package_version: 1,
      export_batch_id: batchId,
      source_run_id: state.run_id,
      base_extracted_sha256: state.base_extracted_sha256,
      created_at: createdAt,
      export_scope: scope,
      chips,
    };
    return batch;
  }

  function commitExportBatch(state, batch) {
    if (!batch || batch.source_run_id !== state.run_id) throw new Error("导出批次不属于当前解析任务");
    if (state.export_batches.some((item) => item.export_batch_id === batch.export_batch_id)) return batch;
    for (const item of batch.chips) {
      const chip = state.chips[item.chip_id];
      if (!chip || chip.review_revision !== item.review_revision || chip.decision !== "approved") {
        throw new Error(`导出批次中的芯片审核版本已变化: ${item.chip_id}`);
      }
      chip.exported_revision = Math.max(chip.exported_revision, item.review_revision);
    }
    state.export_batches.push({
      export_batch_id: batch.export_batch_id,
      created_at: batch.created_at,
      export_scope: batch.export_scope,
      chips: batch.chips.map((item) => ({ chip_id: item.chip_id, review_revision: item.review_revision })),
    });
    state.updated_at = batch.created_at;
    return batch;
  }

  function createExportBatch(state, scope, payloadsByChip, options = {}) {
    const batch = buildExportBatch(state, scope, payloadsByChip, options);
    return batch ? commitExportBatch(state, batch) : null;
  }

  return {
    STATE_VERSION,
    applyRecordChanges,
    buildExportBatch,
    commitExportBatch,
    createState,
    deleteRecord,
    createExportBatch,
    exportChipIds,
    loadState,
    hasSheetChanges,
    resetSheetChanges,
    saveState,
    saveRecord,
    setDecision,
    storageKey,
    reviewStatus,
  };
});
