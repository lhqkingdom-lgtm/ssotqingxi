const SHEET_LABELS = {
  chips: "芯片参数",
  memory: "存储器",
  peripherals: "外设",
  electrical: "电气参数",
  pin_mux: "管脚复用",
};

const FIELD_LABELS = {
  chip_id: "芯片型号", series: "系列", core: "内核", flash_kb: "Flash (KB)",
  ram_kb: "RAM (KB)", max_freq_mhz: "最高频率 (MHz)", package_types: "封装",
  voltage_min: "最低电压", voltage_max: "最高电压", pin_name: "管脚名",
  pin_number: "脚号", function_name: "功能", is_supported: "支持", af_code: "AF编号",
  default_function: "默认功能", constraints: "限制", peripheral_type: "外设类型",
  instance_count: "数量 / 通道数", max_resolution: "最高分辨率", max_frequency_mhz: "最高频率",
  features: "特性", param_name: "参数名", min_value: "最小值", max_value: "最大值",
  unit: "单位", conditions: "条件", memory_type: "存储类型", size_kb: "容量 (KB)",
  organization: "组织方式",
};

const HIDDEN_FIELDS = new Set(["chip_id", "pdf_source_file", "evidence_pages", "ingested_by", "ingested_at", "is_reviewed"]);
const EXTRACTION_METHODS = { A: "直接提取", B: "结构转换", C: "回退推断" };
const PAGE_SIZE = 50;
const NUMERIC_FIELDS = new Set(["flash_kb", "ram_kb", "max_freq_mhz", "voltage_min", "voltage_max", "instance_count", "max_resolution", "max_frequency_mhz", "min_value", "max_value", "size_kb"]);
const BOOLEAN_FIELDS = new Set(["is_supported"]);
const LONG_TEXT_FIELDS = new Set(["function_name", "default_function", "constraints", "features", "conditions", "organization"]);
const state = { data: null, reviewState: null, reviewBatchPath: null, serverRevision: 0, saveQueue: Promise.resolve(), saveState: "loading", query: "", reviewFilter: "all", activeChipId: null, activeSheet: "chips", page: 0, activeRowId: null, activeEvidenceId: null, sidebarCollapsed: false, evidenceZoom: "fit", editMode: false, editingRecord: null, inlineEditor: null, lastCellClick: null, pendingDeleteRowId: null };
const $ = (id) => document.getElementById(id);
const REVIEW_STATUS_LABELS = {
  unreviewed: "未审核",
  issue: "有问题",
  pending_export: "已审核·待导出",
  exported: "已导出",
};

function stableStringify(value) {
  if (Array.isArray(value)) return `[${value.map(stableStringify).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${stableStringify(value[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

function contentHash(value) {
  const text = stableStringify(value);
  let hash = 2166136261;
  for (let index = 0; index < text.length; index += 1) {
    hash ^= text.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return `fnv1a-${(hash >>> 0).toString(16).padStart(8, "0")}`;
}

function businessRecords(chip, sheet) {
  return ReviewWorkflow.applyRecordChanges(state.reviewState, chip.chip_id, sheet, chip.tables[sheet] || []);
}

function displayRecords(chip, sheet) {
  return state.editMode ? businessRecords(chip, sheet) : reviewRecords(chip, sheet);
}

function chipPayload(chip) {
  const tables = {};
  for (const sheet of state.data.sheet_order) {
    tables[sheet] = businessRecords(chip, sheet).map((record) => ({ ...record.values, is_reviewed: true }));
  }
  return { tables };
}

function chipReviewStatus(chipId) {
  return ReviewWorkflow.reviewStatus(state.reviewState.chips[chipId]);
}

function emptyBusinessSections(chip) {
  return state.data.sheet_order.filter((sheet) => sheet !== "chips" && businessRecords(chip, sheet).length === 0);
}

function setSaveStatus(message, kind = "") {
  const element = $("saveStatus");
  element.textContent = message;
  element.className = `save-status${kind ? ` ${kind}` : ""}`;
  state.saveState = kind || "idle";
}

function reviewBatchPath() {
  const configured = new URL(window.location.href).searchParams.get("batch");
  if (configured) return configured.replace(/^\/+|\/+$/g, "");
  const directory = decodeURIComponent(new URL(".", window.location.href).pathname);
  return directory.replace(/^\/+|\/+$/g, "");
}

function batchResource(relativePath) {
  const configured = new URL(window.location.href).searchParams.get("batch");
  if (!configured) return relativePath;
  const batch = configured.replace(/^\/+|\/+$/g, "");
  const encoded = [...batch.split("/"), ...relativePath.split("/")]
    .filter(Boolean)
    .map((part) => encodeURIComponent(part))
    .join("/");
  return `/${encoded}`;
}

function reviewStateEndpoint() {
  return `/api/review/state?batch=${encodeURIComponent(state.reviewBatchPath)}`;
}

function persistReviewState(options = {}) {
  state.reviewState.updated_at = new Date().toISOString();
  const snapshot = JSON.parse(JSON.stringify(state.reviewState));
  setSaveStatus("正在保存…", "saving");
  const operation = state.saveQueue.then(async () => {
    const response = await fetch(reviewStateEndpoint(), {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ expected_revision: state.serverRevision, state: snapshot }),
      cache: "no-store",
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
    state.serverRevision = result.revision;
    setSaveStatus(`已保存 · v${result.revision}`, "saved");
    return result;
  });
  state.saveQueue = operation.catch((error) => {
    const conflict = /revision conflict/i.test(error.message);
    setSaveStatus(conflict ? "保存冲突，请刷新页面" : `保存失败：${error.message}`, "error");
  });
  return options.throwOnError ? operation : state.saveQueue;
}

async function loadPersistedReviewState(reviewMeta, reviewSeeds) {
  state.reviewBatchPath = reviewBatchPath();
  const legacyKey = ReviewWorkflow.storageKey(reviewMeta);
  const legacyState = ReviewWorkflow.loadState(window.localStorage, reviewMeta, reviewSeeds);
  const response = await fetch(reviewStateEndpoint(), { cache: "no-store" });
  const result = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(result.error || `审核状态读取失败：HTTP ${response.status}`);
  state.serverRevision = result.revision || 0;
  if (result.exists) {
    window.localStorage.removeItem(legacyKey);
    setSaveStatus(`已从服务器加载 · v${state.serverRevision}`, "saved");
    return result.state;
  }
  state.reviewState = legacyState;
  const saved = await persistReviewState({ throwOnError: true });
  window.localStorage.removeItem(legacyKey);
  setSaveStatus(`已迁移到服务器 · v${saved.revision}`, "saved");
  return legacyState;
}

function setChipDecision(chip, decision) {
  const beforeRevision = state.reviewState.chips[chip.chip_id].review_revision;
  ReviewWorkflow.setDecision(state.reviewState, chip.chip_id, decision, {
    content_hash: contentHash(chipPayload(chip)),
  });
  if (state.reviewState.chips[chip.chip_id].review_revision === beforeRevision) {
    render();
    return;
  }
  persistReviewState();
  render();
}

function invalidateReviewAfterDataChange(chip) {
  ReviewWorkflow.setDecision(state.reviewState, chip.chip_id, "unreviewed", {
    content_hash: contentHash(chipPayload(chip)),
  });
  persistReviewState();
}

function downloadFile(filename, url) {
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
}

async function exportReviewData() {
  const scope = $("exportScope").value;
  const payloads = Object.fromEntries(state.data.chips.map((chip) => [chip.chip_id, chipPayload(chip)]));
  const batch = ReviewWorkflow.buildExportBatch(state.reviewState, scope, payloads, {
    current_chip_id: state.activeChipId,
  });
  if (!batch) {
    $("exportMessage").textContent = "当前范围没有可导出的已审核芯片";
    return;
  }
  const button = $("exportReview");
  button.disabled = true;
  $("exportMessage").textContent = "正在生成 Excel…";
  try {
    const response = await fetch("/api/review/export-xlsx", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(batch),
    });
    if (!response.ok) {
      let message = `HTTP ${response.status}`;
      try { message = (await response.json()).error || message; } catch (_) { /* response is not JSON */ }
      throw new Error(message);
    }
    const result = await response.json();
    if (!result.download_url || !result.filename) throw new Error("服务未返回 Excel 下载地址");
    const beforeCommit = JSON.parse(JSON.stringify(state.reviewState));
    ReviewWorkflow.commitExportBatch(state.reviewState, batch);
    try {
      await persistReviewState({ throwOnError: true });
    } catch (error) {
      state.reviewState = beforeCommit;
      throw error;
    }
    downloadFile(result.filename, result.download_url);
    $("exportMessage").textContent = `已导出 ${batch.chips.length} 颗芯片的 Excel`;
    render();
  } catch (error) {
    $("exportMessage").textContent = `Excel 导出失败：${error.message}`;
  } finally {
    button.disabled = false;
  }
}

function formatValue(value) {
  if (value === null || value === undefined || value === "") return "—";
  if (Array.isArray(value)) return value.join(" · ") || "—";
  if (typeof value === "boolean") return value ? "是" : "否";
  return String(value);
}

function formatFieldValue(sheet, field, record) {
  const value = record.values[field];
  if (sheet === "peripherals" && field === "instance_count" && value !== null && value !== undefined && value !== "") {
    return ["ADC", "PWM", "Touch"].includes(record.values.peripheral_type)
      ? `${value} 个通道`
      : `${value} 个接口`;
  }
  return formatValue(value);
}

function editableFields(sheet) {
  return state.data.sheet_contracts[sheet].fields.filter((field) => !HIDDEN_FIELDS.has(field));
}

function defaultNewValues(chip, sheet) {
  const source = businessRecords(chip, "chips")[0]?.values || chip.tables.chips[0]?.values || {};
  const values = Object.fromEntries(state.data.sheet_contracts[sheet].fields.map((field) => [field, null]));
  values.chip_id = chip.chip_id;
  values.pdf_source_file = source.pdf_source_file || chip.documents[0] || "人工新增";
  values.evidence_pages = "人工新增";
  values.ingested_by = source.ingested_by || "reviewer";
  values.ingested_at = new Date().toISOString();
  values.is_reviewed = false;
  return values;
}

function editorInput(field, value) {
  let control;
  if (BOOLEAN_FIELDS.has(field)) {
    control = document.createElement("select");
    for (const [label, optionValue] of [["是", "true"], ["否", "false"]]) {
      const option = document.createElement("option");
      option.value = optionValue;
      option.textContent = label;
      control.appendChild(option);
    }
    control.value = value === false ? "false" : "true";
  } else if (LONG_TEXT_FIELDS.has(field)) {
    control = document.createElement("textarea");
    control.rows = 3;
    control.value = value ?? "";
  } else {
    control = document.createElement("input");
    control.type = NUMERIC_FIELDS.has(field) ? "number" : "text";
    if (control.type === "number") control.step = "any";
    control.value = Array.isArray(value) ? value.join(", ") : (value ?? "");
  }
  control.dataset.field = field;
  control.name = field;
  return control;
}

function openRecordEditor(chip, record = null) {
  const isNew = !record;
  const values = isNew ? defaultNewValues(chip, state.activeSheet) : { ...record.values };
  state.editingRecord = { chip_id: chip.chip_id, sheet: state.activeSheet, row_id: record?.row_id || `manual-${Date.now()}-${Math.random().toString(16).slice(2, 8)}`, is_new: isNew, original: record };
  $("recordEditorTitle").textContent = `${isNew ? "新增" : "编辑"}${SHEET_LABELS[state.activeSheet]}`;
  const fields = $("recordEditorFields");
  fields.replaceChildren();
  for (const field of editableFields(state.activeSheet)) {
    const label = document.createElement("label");
    const caption = document.createElement("span");
    caption.textContent = FIELD_LABELS[field] || field;
    if (state.data.sheet_contracts[state.activeSheet].key.includes(field)) caption.className = "required-field";
    label.append(caption, editorInput(field, values[field]));
    fields.appendChild(label);
  }
  $("recordEditorError").textContent = "";
  $("recordEditor").hidden = false;
  fields.querySelector("input,select,textarea")?.focus();
}

function closeRecordEditor() {
  $("recordEditor").hidden = true;
  state.editingRecord = null;
}

function parsedEditorValue(field, control, originalValue) {
  const raw = control.value.trim();
  if (BOOLEAN_FIELDS.has(field)) return raw === "true";
  if (NUMERIC_FIELDS.has(field)) {
    if (!raw) return null;
    const value = Number(raw);
    if (!Number.isFinite(value)) throw new Error(`${FIELD_LABELS[field] || field}不是有效数值`);
    return value;
  }
  if (field === "package_types") return raw ? raw.split(/[,，]/).map((item) => item.trim()).filter(Boolean) : [];
  if (!raw) return null;
  return Array.isArray(originalValue) ? raw.split(/[,，]/).map((item) => item.trim()).filter(Boolean) : raw;
}

function editorValues(chip, editing) {
  const base = editing.is_new ? defaultNewValues(chip, editing.sheet) : { ...editing.original.values };
  for (const control of $("recordEditorFields").querySelectorAll("[data-field]")) {
    base[control.dataset.field] = parsedEditorValue(control.dataset.field, control, base[control.dataset.field]);
  }
  base.chip_id = chip.chip_id;
  base.is_reviewed = false;
  return base;
}

function validateEditorValues(chip, editing, values) {
  const contract = state.data.sheet_contracts[editing.sheet];
  for (const field of contract.key) {
    if (values[field] === null || values[field] === undefined || String(values[field]).trim() === "") {
      throw new Error(`主键字段“${FIELD_LABELS[field] || field}”不能为空`);
    }
  }
  const key = contract.key.map((field) => JSON.stringify(values[field] ?? null)).join("|");
  const duplicate = businessRecords(chip, editing.sheet).some((record) =>
    record.row_id !== editing.row_id && contract.key.map((field) => JSON.stringify(record.values[field] ?? null)).join("|") === key
  );
  if (duplicate) throw new Error("相同主键的记录已经存在");
  if (editing.sheet === "pin_mux" && /^-?\d+$/.test(String(values.pin_number ?? "")) && Number(values.pin_number) <= 0) {
    throw new Error("脚号必须大于0");
  }
  for (const [minField, maxField] of editing.sheet === "chips"
    ? [["voltage_min", "voltage_max"]]
    : editing.sheet === "electrical" ? [["min_value", "max_value"]] : []) {
    if (values[minField] !== null && values[maxField] !== null && Number(values[minField]) > Number(values[maxField])) {
      throw new Error(`${FIELD_LABELS[minField]}不能大于${FIELD_LABELS[maxField]}`);
    }
  }
}

function saveRecordEditor() {
  const editing = state.editingRecord;
  const chip = currentChip();
  if (!editing || !chip || editing.chip_id !== chip.chip_id) return;
  try {
    const values = editorValues(chip, editing);
    validateEditorValues(chip, editing, values);
    const source = editing.original || {};
    ReviewWorkflow.saveRecord(state.reviewState, chip.chip_id, editing.sheet, {
      ...source,
      row_id: editing.row_id,
      values,
      evidence_ids: source.evidence_ids || [],
      field_evidence: source.field_evidence || {},
      confidence: source.confidence || "manual",
    }, { is_new: editing.is_new });
    invalidateReviewAfterDataChange(chip);
    closeRecordEditor();
    const recordIndex = businessRecords(chip, editing.sheet).findIndex((record) => record.row_id === editing.row_id);
    state.page = Math.max(0, Math.floor(recordIndex / PAGE_SIZE));
    state.activeRowId = editing.row_id;
    render();
  } catch (error) {
    $("recordEditorError").textContent = error.message;
  }
}

function setTableEditMessage(message = "", isError = false) {
  const element = $("tableEditMessage");
  element.textContent = message;
  element.classList.toggle("error", isError);
}

function cancelInlineEdit(chip) {
  state.inlineEditor = null;
  setTableEditMessage();
  renderTable(chip);
}

function saveInlineEdit(chip, record, field, control, td) {
  if (!state.inlineEditor) return;
  try {
    const values = { ...record.values };
    values[field] = parsedEditorValue(field, control, values[field]);
    values.chip_id = chip.chip_id;
    values.is_reviewed = false;
    const editing = { chip_id: chip.chip_id, sheet: state.activeSheet, row_id: record.row_id, is_new: false, original: record };
    validateEditorValues(chip, editing, values);
    ReviewWorkflow.saveRecord(state.reviewState, chip.chip_id, state.activeSheet, {
      ...record,
      row_id: record.row_id,
      values,
      evidence_ids: record.evidence_ids || [],
      field_evidence: record.field_evidence || {},
      confidence: record.confidence || "manual",
    }, { is_new: false });
    state.inlineEditor = null;
    state.pendingDeleteRowId = null;
    invalidateReviewAfterDataChange(chip);
    setTableEditMessage(`${FIELD_LABELS[field] || field}已保存`);
    render();
  } catch (error) {
    td.classList.add("inline-edit-error");
    setTableEditMessage(error.message, true);
    control.focus();
    if (control instanceof HTMLInputElement && control.type !== "number") control.select();
    if (control instanceof HTMLTextAreaElement) control.select();
  }
}

function beginInlineEdit(chip, record, field, td) {
  if (!state.editMode || HIDDEN_FIELDS.has(field)) return;
  if (state.inlineEditor) {
    cancelInlineEdit(chip);
    return;
  }
  try {
    setTableEditMessage("Enter 保存 · Esc 取消");
    td.classList.add("inline-editing");
    const control = editorInput(field, record.values[field]);
    control.classList.add("inline-editor");
    control.removeAttribute("data-field");
    if (control.tagName === "TEXTAREA") control.rows = 1;
    control.addEventListener("click", (event) => event.stopPropagation());
    control.addEventListener("dblclick", (event) => event.stopPropagation());
    control.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        cancelInlineEdit(chip);
      } else if (event.key === "Enter" && !(event.shiftKey && control.tagName === "TEXTAREA")) {
        event.preventDefault();
        saveInlineEdit(chip, record, field, control, td);
      }
    });
    td.replaceChildren(control);
    state.inlineEditor = { row_id: record.row_id, field };
    control.focus();
    if (control instanceof HTMLInputElement && control.type !== "number") control.select();
    if (control instanceof HTMLTextAreaElement) control.select();
  } catch (error) {
    state.inlineEditor = null;
    td.classList.add("inline-edit-error");
    setTableEditMessage(`无法开始编辑：${error.message}`, true);
  }
}

function requestDeleteRecord(chip, record) {
  if (state.pendingDeleteRowId !== record.row_id) {
    state.pendingDeleteRowId = record.row_id;
    renderTable(chip);
    return;
  }
  ReviewWorkflow.deleteRecord(state.reviewState, chip.chip_id, state.activeSheet, record.row_id);
  state.pendingDeleteRowId = null;
  state.activeRowId = null;
  invalidateReviewAfterDataChange(chip);
  render();
}

function selectedBusinessRecord(chip) {
  return businessRecords(chip, state.activeSheet).find((record) => record.row_id === state.activeRowId) || null;
}

function renderSelectedRecordActions(chip) {
  const record = chip ? selectedBusinessRecord(chip) : null;
  $("editSelectedRecord").hidden = !state.editMode || !record;
  $("deleteSelectedRecord").hidden = !state.editMode || state.activeSheet === "chips" || !record;
  $("deleteSelectedRecord").textContent = record && state.pendingDeleteRowId === record.row_id
    ? "确认删除选中行"
    : "删除选中行";
}

function resetCurrentSheetChanges(chip) {
  ReviewWorkflow.resetSheetChanges(state.reviewState, chip.chip_id, state.activeSheet);
  state.pendingDeleteRowId = null;
  invalidateReviewAfterDataChange(chip);
  render();
}

function uniqueValues(values) {
  return [...new Set(values.filter((value) => value !== null && value !== undefined && value !== ""))];
}

function updateEvidenceZoomControls() {
  const isFit = state.evidenceZoom === "fit";
  $("evidenceZoomFit").classList.toggle("active", isFit);
  $("evidenceZoomActual").classList.toggle("active", state.evidenceZoom === 1);
  $("evidenceZoomLabel").textContent = isFit ? "适宽" : `${Math.round(state.evidenceZoom * 100)}%`;
}

function applyEvidenceZoom(image, wrapper) {
  if (!image || !wrapper) return;
  if (state.evidenceZoom === "fit") {
    wrapper.style.width = "100%";
    image.style.width = "100%";
  } else if (image.naturalWidth) {
    const width = Math.max(320, Math.round(image.naturalWidth * state.evidenceZoom));
    wrapper.style.width = `${width}px`;
    image.style.width = `${width}px`;
  }
  updateEvidenceZoomControls();
}

function setEvidenceZoom(value) {
  state.evidenceZoom = value;
  const image = $("evidenceViewport").querySelector("img");
  applyEvidenceZoom(image, image?.parentElement);
}

function reviewRecords(chip, sheet) {
  const records = businessRecords(chip, sheet);
  if (sheet !== "pin_mux") return records;
  const groups = new Map();
  for (const record of records) {
    const values = record.values;
    const key = `${values.pin_number}\u0000${values.pin_name}`;
    if (!groups.has(key)) {
      groups.set(key, {
        row_id: `pin-group-${record.row_id}`,
        values: { ...values, function_name: [], af_code: [], default_function: [], constraints: [] },
        evidence_ids: [],
        field_evidence: {},
        function_entries: [],
        source_row_ids: [],
      });
    }
    const group = groups.get(key);
    group.values.function_name.push(values.function_name);
    group.values.af_code.push(values.af_code);
    group.values.default_function.push(values.default_function);
    group.values.constraints.push(values.constraints);
    group.evidence_ids.push(...record.evidence_ids);
    group.function_entries.push({
      function_name: values.function_name,
      af_code: values.af_code,
      function_evidence: record.field_evidence?.function_name || record.evidence_ids || [],
      af_evidence: record.field_evidence?.af_code || [],
    });
    for (const [field, ids] of Object.entries(record.field_evidence || {})) {
      if (!group.field_evidence[field]) group.field_evidence[field] = [];
      group.field_evidence[field].push(...ids);
    }
    group.source_row_ids.push(record.row_id);
  }
  return [...groups.values()].map((group) => ({
    ...group,
    values: {
      ...group.values,
      function_name: uniqueValues(group.values.function_name),
      af_code: uniqueValues(group.values.af_code),
      default_function: uniqueValues(group.values.default_function),
      constraints: uniqueValues(group.values.constraints),
    },
    evidence_ids: uniqueValues(group.evidence_ids),
    field_evidence: Object.fromEntries(
      Object.entries(group.field_evidence).map(([field, ids]) => [field, uniqueValues(ids)])
    ),
  }));
}

function pinCoverageHint(chip, groupedRecords) {
  const detailCount = businessRecords(chip, "pin_mux").length;
  const packageValues = businessRecords(chip, "chips")[0]?.values.package_types || [];
  const packages = Array.isArray(packageValues) ? packageValues : [packageValues];
  const leadCounts = uniqueValues(packages.map((value) => String(value).match(/(\d+)/)?.[1]).filter(Boolean));
  let hint = `${groupedRecords.length} 个管脚 · ${detailCount} 条功能`;
  if (leadCounts.length !== 1) return hint;
  const leadCount = Number(leadCounts[0]);
  const observed = new Set(groupedRecords.map((record) => Number(record.values.pin_number)).filter(Number.isInteger));
  const absent = Array.from({ length: leadCount }, (_, index) => index + 1).filter((number) => !observed.has(number));
  if (absent.length && absent.length <= 12) {
    hint += ` · pin_mux 未收录：${absent.join("、")}（可能为电源、地等无复用功能管脚）`;
  }
  return hint;
}

function chipPackages(chip) {
  const value = businessRecords(chip, "chips")[0]?.values.package_types || [];
  return uniqueValues(Array.isArray(value) ? value : [value]);
}

function sectionEvidence(chip, sheet) {
  const ids = uniqueValues(businessRecords(chip, sheet).flatMap((record) => record.evidence_ids || []));
  return ids
    .map((id) => state.data.evidence.find((item) => item.evidence_id === id))
    .filter(Boolean)
    .sort((left, right) => left.page - right.page || left.evidence_id.localeCompare(right.evidence_id));
}

function visibleChips() {
  const query = state.query.trim().toLocaleLowerCase();
  let chips = state.data.chips;
  if (query) {
    const modelMatches = chips.filter((chip) => chip.chip_id.toLocaleLowerCase().includes(query));
    chips = modelMatches.length
      ? modelMatches
      : chips.filter((chip) => chip.documents.join(" ").toLocaleLowerCase().includes(query));
  }
  if (state.reviewFilter !== "all") {
    chips = chips.filter((chip) => chipReviewStatus(chip.chip_id) === state.reviewFilter);
  }
  return chips;
}

function currentChip() {
  return state.data.chips.find((chip) => chip.chip_id === state.activeChipId) || visibleChips()[0] || null;
}

function setChip(chipId) {
  state.activeChipId = chipId;
  state.page = 0;
  state.activeRowId = null;
  state.activeEvidenceId = null;
  render();
}

function renderChipList() {
  const chips = visibleChips();
  $("visibleCount").textContent = `${chips.length}/${state.data.chips.length}`;
  const list = $("chipList");
  list.replaceChildren();
  for (const chip of chips) {
    const button = document.createElement("button");
    button.className = `chip-button${chip.chip_id === state.activeChipId ? " active" : ""}`;
    button.type = "button";
    button.addEventListener("click", () => setChip(chip.chip_id));
    const name = document.createElement("span");
    name.className = "chip-name";
    name.textContent = chip.chip_id;
    const marker = document.createElement("span");
    const reviewStatus = chipReviewStatus(chip.chip_id);
    marker.className = `review-marker ${reviewStatus}${emptyBusinessSections(chip).length ? " has-warning" : ""}`;
    marker.textContent = REVIEW_STATUS_LABELS[reviewStatus];
    const doc = document.createElement("span");
    doc.className = "chip-doc";
    doc.textContent = chip.documents.join(" · ");
    button.append(name, marker, doc);
    list.appendChild(button);
  }
  if (!chips.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = "没有匹配的芯片";
    list.appendChild(empty);
  }
}

function moveChip(delta) {
  const chips = visibleChips();
  const index = chips.findIndex((chip) => chip.chip_id === state.activeChipId);
  const next = chips[index + delta];
  if (next) setChip(next.chip_id);
}

function renderChipHeader(chip) {
  const header = $("chipHeader");
  header.replaceChildren();
  if (!chip) return;
  const chipRow = businessRecords(chip, "chips")[0]?.values || {};
  const heading = document.createElement("div");
  heading.className = "chip-heading";
  const title = document.createElement("h1");
  title.className = "chip-title";
  title.textContent = chip.chip_id;
  const summary = document.createElement("div");
  summary.className = "chip-summary";
  const pinCount = reviewRecords(chip, "pin_mux").length;
  summary.textContent = `Flash ${formatValue(chipRow.flash_kb)} KB · RAM ${formatValue(chipRow.ram_kb)} KB · ${formatValue(chipRow.max_freq_mhz)} MHz · ${pinCount} 个管脚 / ${chip.counts.pin_mux} 条功能`;
  const documentLine = document.createElement("div");
  documentLine.className = "chip-document";
  documentLine.textContent = chip.documents.join(" · ");
  heading.append(title, summary, documentLine);
  header.appendChild(heading);
  const emptySections = emptyBusinessSections(chip);
  if (emptySections.length) {
    const warning = document.createElement("div");
    warning.className = "warning";
    warning.textContent = `未提取：${emptySections.map((sheet) => SHEET_LABELS[sheet]).join("、")}`;
    header.appendChild(warning);
  }
  const chips = visibleChips();
  const index = chips.findIndex((item) => item.chip_id === chip.chip_id);
  const reviewStatus = chipReviewStatus(chip.chip_id);
  const status = document.createElement("span");
  status.className = `review-status ${reviewStatus}`;
  status.textContent = REVIEW_STATUS_LABELS[reviewStatus];
  const issue = document.createElement("button");
  issue.type = "button";
  issue.className = "review-action issue-action";
  issue.textContent = reviewStatus === "issue" ? "已有问题" : "有问题";
  issue.disabled = reviewStatus === "issue";
  issue.addEventListener("click", () => setChipDecision(chip, "issue"));
  const approve = document.createElement("button");
  approve.type = "button";
  approve.className = "review-action approve-action";
  approve.textContent = ["pending_export", "exported"].includes(reviewStatus) ? "已通过" : "审核通过";
  approve.disabled = ["pending_export", "exported"].includes(reviewStatus);
  approve.addEventListener("click", () => setChipDecision(chip, "approved"));
  header.append(status, issue, approve);
  for (const [label, delta, disabled] of [["上一颗", -1, index <= 0], ["下一颗", 1, index < 0 || index >= chips.length - 1]]) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "nav-button";
    button.textContent = label;
    button.disabled = disabled;
    button.addEventListener("click", () => moveChip(delta));
    header.appendChild(button);
  }
}

function renderTabs(chip) {
  const tabs = $("sheetTabs");
  tabs.replaceChildren();
  for (const sheet of state.data.sheet_order) {
    const count = reviewRecords(chip, sheet).length;
    const button = document.createElement("button");
    button.type = "button";
    button.className = `sheet-tab${sheet === state.activeSheet ? " active" : ""}${count === 0 ? " empty" : ""}`;
    button.textContent = `${SHEET_LABELS[sheet] || sheet} ${count}`;
    button.addEventListener("click", () => {
      state.activeSheet = sheet;
      state.page = 0;
      state.activeRowId = null;
      state.activeEvidenceId = null;
      state.pendingDeleteRowId = null;
      state.inlineEditor = null;
      setTableEditMessage();
      closeRecordEditor();
      render();
    });
    tabs.appendChild(button);
  }
}

function renderTable(chip) {
  state.lastCellClick = null;
  const records = displayRecords(chip, state.activeSheet);
  $("toggleDataMode").textContent = state.editMode ? "完成管理" : "管理数据";
  $("addRecord").hidden = !state.editMode || state.activeSheet === "chips";
  $("resetSheetChanges").hidden = !state.editMode || !ReviewWorkflow.hasSheetChanges(state.reviewState, chip.chip_id, state.activeSheet);
  $("editSelectedRecord").hidden = true;
  $("deleteSelectedRecord").hidden = true;
  $("sheetTitle").textContent = SHEET_LABELS[state.activeSheet] || state.activeSheet;
  if (!state.inlineEditor) setTableEditMessage();
  $("sheetHint").textContent = state.editMode
    ? `${records.length} 条原始业务记录 · 修改后需重新审核通过`
    : state.activeSheet === "pin_mux"
    ? pinCoverageHint(chip, records)
    : `${records.length} 条记录`;
  const viewport = $("tableViewport");
  const pagination = $("pagination");
  viewport.replaceChildren();
  pagination.replaceChildren();
  if (!records.length) {
    renderSelectedRecordActions(chip);
    const empty = document.createElement("div");
    empty.className = "empty-state";
    const strong = document.createElement("strong");
    strong.textContent = `未提取到${SHEET_LABELS[state.activeSheet] || state.activeSheet}数据`;
    const detail = document.createElement("span");
    detail.textContent = "这不是审核通过，后续需要人工确认原文。";
    empty.append(strong, detail);
    viewport.appendChild(empty);
    return;
  }
  const contract = state.data.sheet_contracts[state.activeSheet];
  // 审核视图必须保持业务字段完整。即使整列为空，也显示“—”，这样审核人
  // 能区分“原文没有值/尚未提取”与“页面根本没有展示这个字段”。
  let fields = contract.fields.filter((field) => !HIDDEN_FIELDS.has(field));
  if (state.activeSheet === "pin_mux" && !state.editMode) fields = fields.filter((field) => field !== "af_code");
  const start = state.page * PAGE_SIZE;
  const pageRows = records.slice(start, start + PAGE_SIZE);
  if (!records.some((record) => record.row_id === state.activeRowId)) {
    state.activeRowId = pageRows[0]?.row_id || null;
    state.activeEvidenceId = pageRows[0]?.evidence_ids?.[0] || null;
  }
  renderSelectedRecordActions(chip);
  const table = document.createElement("table");
  table.className = state.editMode ? "management-table" : (state.activeSheet === "pin_mux" ? "pin-mux-table" : "");
  document.querySelector(".review-split")?.classList.toggle("pin-layout", state.activeSheet === "pin_mux");
  const thead = document.createElement("thead");
  const headerRow = document.createElement("tr");
  for (const field of fields) {
    const th = document.createElement("th");
    th.textContent = state.activeSheet === "pin_mux" && field === "pin_number"
      ? "脚号"
      : state.activeSheet === "pin_mux" && field === "function_name"
        ? "功能 / AF"
        : state.activeSheet === "pin_mux" && field === "default_function"
          ? "复位默认功能"
          : FIELD_LABELS[field] || field;
    headerRow.appendChild(th);
  }
  if (state.editMode) {
    const th = document.createElement("th");
    th.className = "record-actions-heading";
    th.textContent = "操作";
    headerRow.appendChild(th);
  }
  thead.appendChild(headerRow);
  const tbody = document.createElement("tbody");
  for (const record of pageRows) {
    const tr = document.createElement("tr");
    tr.className = `${record.row_id === state.activeRowId ? "selected " : ""}${record.manually_changed ? "changed-record " : ""}${record.manually_added ? "added-record" : ""}`.trim();
    tr.addEventListener("click", (event) => {
      state.activeRowId = record.row_id;
      state.activeEvidenceId = record.evidence_ids?.[0] || null;
      if (state.editMode) {
        const td = event.target.closest("td.editable-cell");
        if (td) {
          const now = Date.now();
          const key = `${record.row_id}:${td.dataset.field}`;
          const previous = state.lastCellClick;
          state.lastCellClick = { key, at: now };
          if (event.detail === 2 || (previous?.key === key && now - previous.at <= 1200)) {
            state.lastCellClick = null;
            event.preventDefault();
            event.stopPropagation();
            beginInlineEdit(chip, record, td.dataset.field, td);
            return;
          }
        }
        for (const row of tbody.querySelectorAll("tr")) row.classList.toggle("selected", row === tr);
        renderSelectedRecordActions(chip);
        renderEvidence(chip);
        return;
      }
      renderTable(chip);
      renderEvidence(chip);
    });
    tr.addEventListener("dblclick", (event) => {
      if (!state.editMode) return;
      const td = event.target.closest("td.editable-cell");
      if (!td || td.classList.contains("inline-editing")) return;
      event.preventDefault();
      event.stopPropagation();
      beginInlineEdit(chip, record, td.dataset.field, td);
    });
    for (const field of fields) {
      const td = document.createElement("td");
      if (state.activeSheet === "pin_mux" && field === "function_name" && !state.editMode) {
        td.className = "function-af-cell";
        for (const entry of record.function_entries || []) {
          const button = document.createElement("button");
          button.type = "button";
          button.className = "function-af-entry";
          button.title = entry.af_evidence.length ? "点击查看AF矩阵证据" : "点击查看管脚定义证据";
          const name = document.createElement("span");
          name.textContent = formatValue(entry.function_name);
          const badge = document.createElement("span");
          badge.className = `af-badge${entry.af_code ? "" : " no-af"}`;
          badge.textContent = entry.af_code || "无AF";
          button.append(name, badge);
          button.addEventListener("click", (event) => {
            event.stopPropagation();
            state.activeRowId = record.row_id;
            state.activeEvidenceId = entry.af_evidence[0] || entry.function_evidence[0] || record.evidence_ids?.[0] || null;
            renderTable(chip);
            renderEvidence(chip);
          });
          td.appendChild(button);
        }
      } else if (state.activeSheet === "pin_mux" && field === "default_function" && !state.editMode) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "evidence-field-button";
        button.textContent = formatFieldValue(state.activeSheet, field, record);
        button.title = "点击查看复位默认功能的管脚定义证据";
        button.addEventListener("click", (event) => {
          event.stopPropagation();
          state.activeRowId = record.row_id;
          state.activeEvidenceId = record.field_evidence?.default_function?.[0] || record.evidence_ids?.[0] || null;
          renderTable(chip);
          renderEvidence(chip);
        });
        td.appendChild(button);
      } else {
        td.textContent = formatFieldValue(state.activeSheet, field, record);
      }
      if (state.editMode) {
        td.classList.add("editable-cell");
        td.title = "双击修改；Enter 保存，Esc 取消";
        td.dataset.rowId = record.row_id;
        td.dataset.field = field;
      }
      tr.appendChild(td);
    }
    if (state.editMode) {
      const actions = document.createElement("td");
      actions.className = "record-actions";
      const edit = document.createElement("button");
      edit.type = "button";
      edit.textContent = "编辑";
      edit.addEventListener("click", (event) => { event.stopPropagation(); openRecordEditor(chip, record); });
      actions.appendChild(edit);
      if (state.activeSheet !== "chips") {
        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "delete-record";
        remove.textContent = state.pendingDeleteRowId === record.row_id ? "确认删除" : "删除";
        remove.addEventListener("click", (event) => { event.stopPropagation(); requestDeleteRecord(chip, record); });
        actions.appendChild(remove);
      }
      tr.appendChild(actions);
    }
    tbody.appendChild(tr);
  }
  table.append(thead, tbody);
  viewport.appendChild(table);

  const pages = Math.ceil(records.length / PAGE_SIZE);
  if (pages > 1) {
    const previous = document.createElement("button");
    previous.type = "button";
    previous.textContent = "上一页";
    previous.disabled = state.page === 0;
    previous.addEventListener("click", () => { state.page -= 1; state.activeRowId = null; renderTable(chip); renderEvidence(chip); });
    const label = document.createElement("span");
    label.textContent = `${state.page + 1}/${pages} · 当前显示 ${start + 1}–${Math.min(start + PAGE_SIZE, records.length)}`;
    const next = document.createElement("button");
    next.type = "button";
    next.textContent = "下一页";
    next.disabled = state.page >= pages - 1;
    next.addEventListener("click", () => { state.page += 1; state.activeRowId = null; renderTable(chip); renderEvidence(chip); });
    pagination.append(previous, label, next);
  }
}

function renderEvidence(chip) {
  const records = chip ? displayRecords(chip, state.activeSheet) : [];
  const record = records.find((item) => item.row_id === state.activeRowId);
  const evidence = chip ? sectionEvidence(chip, state.activeSheet) : [];
  const tabs = $("evidenceTabs");
  const viewport = $("evidenceViewport");
  const packageBadge = $("currentPackage");
  tabs.replaceChildren();
  viewport.replaceChildren();
  packageBadge.hidden = true;
  if (!record || !evidence.length) {
    $("evidenceMeta").textContent = "该记录没有可用证据";
    const empty = document.createElement("div");
    empty.className = "evidence-empty";
    empty.textContent = "未找到对应 PDF 证据";
    viewport.appendChild(empty);
    return;
  }
  if (!evidence.some((item) => item.evidence_id === state.activeEvidenceId)) {
    state.activeEvidenceId = record.evidence_ids?.find((id) => evidence.some((item) => item.evidence_id === id)) || evidence[0].evidence_id;
  }
  evidence.forEach((item) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `evidence-tab${item.evidence_id === state.activeEvidenceId ? " active" : ""}`;
    button.textContent = `第 ${item.page} 页`;
    button.addEventListener("click", () => { state.activeEvidenceId = item.evidence_id; renderEvidence(chip); });
    tabs.appendChild(button);
  });
  const item = evidence.find((value) => value.evidence_id === state.activeEvidenceId) || evidence[0];
  const packages = chipPackages(chip);
  packageBadge.hidden = state.activeSheet !== "pin_mux" || packages.length === 0;
  packageBadge.textContent = packages.length ? `当前核对：${packages.join(" / ")}` : "";
  const confidence = item.confidence || "";
  const method = EXTRACTION_METHODS[confidence] || "未标记";
  $("evidenceMeta").textContent = `${item.source_name} · 第 ${item.page} 页 · 提取方式：${method}${confidence ? `（${confidence}）` : ""}`;
  const wrapper = document.createElement("div");
  wrapper.style.width = "100%";
  if (state.activeSheet === "pin_mux" && packages.length) {
    const note = document.createElement("div");
    note.className = "evidence-package-note";
    note.textContent = `当前型号 ${chip.chip_id}：请核对 PDF 中的 ${packages.join(" / ")} 封装列`;
    wrapper.appendChild(note);
  }
  const image = document.createElement("img");
  image.alt = `${item.source_name} 第 ${item.page} 页证据`;
  image.src = batchResource(`evidence/${item.evidence_id}.png`);
  image.title = "点击放大查看原始证据";
  image.addEventListener("load", () => applyEvidenceZoom(image, wrapper));
  image.addEventListener("click", () => {
    $("evidenceModalImage").src = image.src;
    $("evidenceModalImage").alt = image.alt;
    $("evidenceModal").hidden = false;
  });
  image.addEventListener("error", () => {
    image.remove();
    const error = document.createElement("div");
    error.className = "evidence-empty";
    error.textContent = "证据图未生成，请检查源 PDF 是否仍在原路径";
    wrapper.prepend(error);
  });
  const caption = document.createElement("div");
  caption.className = "evidence-caption";
  caption.textContent = `来源：${item.source_name}　页码：${item.page}　证据类型：${item.kind || "—"}`;
  wrapper.append(image, caption);
  viewport.appendChild(wrapper);
}

function render() {
  const chips = visibleChips();
  if (!chips.some((chip) => chip.chip_id === state.activeChipId)) state.activeChipId = chips[0]?.chip_id || null;
  renderChipList();
  const chip = currentChip();
  renderChipHeader(chip);
  if (chip) {
    renderTabs(chip);
    renderTable(chip);
    renderEvidence(chip);
  } else {
    $("sheetTabs").replaceChildren();
    $("tableViewport").replaceChildren();
    $("evidenceTabs").replaceChildren();
    $("evidenceViewport").replaceChildren();
  }
}

async function start() {
  try {
    const response = await fetch(batchResource("review_dataset.json"), { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    state.data = await response.json();
    if (!window.ReviewWorkflow) throw new Error("审核状态模块未加载");
    const reviewMeta = { run_id: state.data.run_id, base_extracted_sha256: state.data.base_extracted_sha256 };
    const reviewSeeds = state.data.chips.map((chip) => ({
      chip_id: chip.chip_id,
      is_reviewed: Boolean(chip.tables.chips?.[0]?.values?.is_reviewed),
    }));
    state.reviewState = await loadPersistedReviewState(reviewMeta, reviewSeeds);
    const initialDocument = new URL(window.location.href).searchParams.get("document") || "";
    if (initialDocument) {
      state.query = initialDocument;
      $("chipSearch").value = initialDocument;
    }
    state.activeChipId = visibleChips()[0]?.chip_id || null;
    $("runSummary").textContent = `${state.data.summary.chips} 颗芯片 · ${Object.values(state.data.summary.records).reduce((a, b) => a + b, 0)} 条业务记录`;
    $("chipSearch").addEventListener("input", (event) => {
      state.query = event.target.value;
      state.page = 0;
      render();
    });
    $("chipStatusFilter").addEventListener("change", (event) => {
      state.reviewFilter = event.target.value;
      state.page = 0;
      render();
    });
    $("exportReview").addEventListener("click", exportReviewData);
    $("exportScope").addEventListener("change", () => { $("exportMessage").textContent = ""; });
    $("toggleDataMode").addEventListener("click", () => {
      state.editMode = !state.editMode;
      state.page = 0;
      state.activeRowId = null;
      state.activeEvidenceId = null;
      state.pendingDeleteRowId = null;
      state.inlineEditor = null;
      setTableEditMessage();
      closeRecordEditor();
      render();
    });
    $("addRecord").addEventListener("click", () => openRecordEditor(currentChip()));
    $("editSelectedRecord").addEventListener("click", () => {
      const chip = currentChip();
      const record = chip ? selectedBusinessRecord(chip) : null;
      if (chip && record) openRecordEditor(chip, record);
    });
    $("deleteSelectedRecord").addEventListener("click", () => {
      const chip = currentChip();
      const record = chip ? selectedBusinessRecord(chip) : null;
      if (chip && record) requestDeleteRecord(chip, record);
    });
    $("resetSheetChanges").addEventListener("click", () => resetCurrentSheetChanges(currentChip()));
    $("cancelRecordEditor").addEventListener("click", closeRecordEditor);
    $("saveRecordEditor").addEventListener("click", saveRecordEditor);
    $("recordEditor").addEventListener("click", (event) => { if (event.target === $("recordEditor")) closeRecordEditor(); });
    $("toggleSidebar").addEventListener("click", () => {
      state.sidebarCollapsed = !state.sidebarCollapsed;
      $("toggleSidebar").textContent = state.sidebarCollapsed ? "›" : "‹";
      $("toggleSidebar").title = state.sidebarCollapsed ? "展开芯片列表" : "收起芯片列表";
      $("toggleSidebar").setAttribute("aria-label", $("toggleSidebar").title);
      document.querySelector(".app-shell")?.classList.toggle("sidebar-collapsed", state.sidebarCollapsed);
    });
    $("evidenceZoomOut").addEventListener("click", () => setEvidenceZoom(Math.max(0.25, (state.evidenceZoom === "fit" ? 0.65 : state.evidenceZoom) - 0.15)));
    $("evidenceZoomFit").addEventListener("click", () => setEvidenceZoom("fit"));
    $("evidenceZoomActual").addEventListener("click", () => setEvidenceZoom(1));
    $("evidenceZoomIn").addEventListener("click", () => setEvidenceZoom(Math.min(2, (state.evidenceZoom === "fit" ? 0.65 : state.evidenceZoom) + 0.15)));
    $("closeEvidenceModal").addEventListener("click", () => { $("evidenceModal").hidden = true; });
    $("evidenceModal").addEventListener("click", (event) => {
      if (event.target === $("evidenceModal")) $("evidenceModal").hidden = true;
    });
    window.addEventListener("beforeunload", (event) => {
      if (state.saveState === "saving") {
        event.preventDefault();
        event.returnValue = "审核修改仍在保存中";
      }
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        $("evidenceModal").hidden = true;
        closeRecordEditor();
      }
    });
    window.reviewApp = { ready: true, state, visibleChips, currentChip };
    render();
  } catch (error) {
    const box = $("fatalError");
    box.hidden = false;
    box.textContent = `审核数据加载失败：${error.message}。请通过本地审核服务打开本页面。`;
  }
}

start();
