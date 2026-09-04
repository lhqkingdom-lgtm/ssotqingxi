const state = {
  documents: [], batches: [],
  documentQuery: "", family: "all", documentStatus: "all", duplicate: "all",
  batchQuery: "", batchStatus: "all", review: "all",
  parseJob: { status: "idle" }, parseTimer: null,
};
const $ = (id) => document.getElementById(id);

const DOCUMENT_STATUS_LABELS = {
  pending: "待解析", complete: "完成", partial: "部分完成", parse_failed: "解析失败",
  unclassified: "无法分类", classification_conflict: "分类冲突",
};
const FAMILY_LABELS = { ca_miai_v1: "CA / MiAi", stm32_v1: "STM32" };
const BATCH_STATUS_LABELS = { ready: "可审核", state_error: "审核状态异常", dataset_error: "批次数据异常" };

function formatNumber(value) { return Number(value || 0).toLocaleString("zh-CN"); }
function formatTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString("zh-CN", { hour12: false });
}
function formatSize(value) {
  const bytes = Number(value || 0);
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}
function metric(value, label) {
  const item = document.createElement("div"); item.className = "metric";
  const strong = document.createElement("strong"); strong.textContent = formatNumber(value);
  const span = document.createElement("span"); span.textContent = label;
  item.append(strong, span); return item;
}
function textCell(text, className = "") {
  const cell = document.createElement("td"); if (className) cell.className = className;
  cell.textContent = text; return cell;
}

function renderParseJob() {
  const job = state.parseJob || { status: "idle" };
  const panel = $("parsePanel");
  panel.hidden = job.status === "idle";
  if (panel.hidden) return;
  const labels = { running: "正在解析", complete: "解析完成", failed: "解析失败" };
  $("parseTitle").textContent = labels[job.status] || "解析任务";
  $("parseDetail").textContent = (job.filenames || []).join("、") || "—";
  const visibleLogCount = job.status === "running" ? 18 : 6;
  $("parseLogs").textContent = (job.logs || []).slice(-visibleLogCount).join("\n");
  $("parseLogs").scrollTop = $("parseLogs").scrollHeight;
  const link = $("parseReviewLink");
  link.hidden = !(job.status === "complete" && job.review_url);
  if (!link.hidden) link.href = job.review_url;
}

async function refreshParseJob() {
  const response = await fetch("/api/parse/status", { cache: "no-store" });
  if (!response.ok) return;
  const previous = state.parseJob?.status;
  state.parseJob = await response.json();
  renderParseJob();
  if (state.parseJob.status === "running") {
    clearTimeout(state.parseTimer);
    state.parseTimer = setTimeout(refreshParseJob, 1000);
  } else if (previous === "running") {
    await loadAll();
  }
}

async function startParse(item, button) {
  button.disabled = true;
  button.textContent = "正在启动…";
  try {
    const response = await fetch("/api/parse/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ document_ids: [item.document_id] }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    state.parseJob = payload;
    renderParseJob();
    renderDocumentRows();
    refreshParseJob();
  } catch (error) {
    button.disabled = false;
    button.textContent = "开始解析";
    $("documentLoadState").textContent = `启动失败：${error.message}`;
  }
}

function renderDocumentSummary() {
  const docs = state.documents;
  $("documentSummary").replaceChildren(
    metric(docs.length, "输入 PDF"),
    metric(docs.filter((item) => item.status === "complete").length, "解析完成"),
    metric(docs.filter((item) => item.status === "pending").length, "待解析"),
    metric(docs.filter((item) => ["partial", "parse_failed", "unclassified", "classification_conflict"].includes(item.status)).length, "需要处理"),
    metric(docs.filter((item) => item.duplicate).length, "重复文件"),
  );
}

function documentMatches(item) {
  const text = `${item.filename} ${item.relative_path} ${(item.chip_ids || []).join(" ")}`.toLocaleLowerCase();
  if (state.documentQuery && !text.includes(state.documentQuery)) return false;
  if (state.family === "unknown" && item.family_id) return false;
  if (state.family !== "all" && state.family !== "unknown" && item.family_id !== state.family) return false;
  if (state.documentStatus !== "all" && item.status !== state.documentStatus) return false;
  if (state.duplicate === "duplicate" && !item.duplicate) return false;
  if (state.duplicate === "unique" && item.duplicate) return false;
  return true;
}

function renderDocumentRows() {
  const rows = $("documentRows"); rows.replaceChildren();
  const visible = state.documents.filter(documentMatches);
  $("documentEmptyState").hidden = visible.length > 0;
  $("documentLoadState").textContent = `显示 ${visible.length}/${state.documents.length} 份 PDF`;
  for (const item of visible) {
    const row = document.createElement("tr");
    const nameCell = document.createElement("td");
    const name = document.createElement("span"); name.className = "batch-name"; name.textContent = item.filename;
    const path = document.createElement("span"); path.className = "batch-id";
    path.textContent = item.relative_path === item.filename ? "输入目录根目录" : item.relative_path;
    nameCell.append(name, path);
    if (item.duplicate) {
      const duplicate = document.createElement("span"); duplicate.className = "duplicate-badge";
      duplicate.textContent = `同内容 ${item.duplicate_count} 份`; nameCell.appendChild(duplicate);
    }

    const familyCell = document.createElement("td");
    const family = document.createElement("span"); family.className = "family";
    family.textContent = FAMILY_LABELS[item.family_id] || "未分类"; familyCell.appendChild(family);
    const source = document.createElement("span"); source.className = "content-sub";
    source.textContent = item.classification_source === "extraction" ? "提取结果确认" : "文件名预分类"; familyCell.appendChild(source);

    const statusCell = document.createElement("td");
    const status = document.createElement("span"); status.className = `status document-status ${item.status}`;
    status.textContent = DOCUMENT_STATUS_LABELS[item.status] || item.status; statusCell.appendChild(status);
    if (item.batch_name) {
      const batch = document.createElement("span"); batch.className = "content-sub"; batch.textContent = `批次：${item.batch_name}`; statusCell.appendChild(batch);
    }

    const chipsCell = document.createElement("td");
    const chipCount = document.createElement("span"); chipCount.className = "content-main";
    chipCount.textContent = item.chip_count ? `${item.chip_count} 颗` : "—"; chipsCell.appendChild(chipCount);
    if (item.chip_ids?.length) {
      const chips = document.createElement("span"); chips.className = "chip-preview"; chips.title = item.chip_ids.join("、");
      chips.textContent = item.chip_ids.slice(0, 3).join("、") + (item.chip_ids.length > 3 ? "…" : ""); chipsCell.appendChild(chips);
    }

    const actionCell = document.createElement("td");
    const open = document.createElement("button"); open.type = "button"; open.className = "open-button";
    if (item.review_url) {
      open.textContent = "查看芯片";
      open.addEventListener("click", () => { window.location.href = item.review_url; });
    } else if (item.status === "pending") {
      open.textContent = "开始解析";
      open.disabled = state.parseJob?.status === "running";
      open.addEventListener("click", () => startParse(item, open));
    } else {
      open.textContent = "不可解析";
      open.disabled = true;
    }
    actionCell.appendChild(open);
    row.append(nameCell, familyCell, statusCell, chipsCell, textCell(formatSize(item.size_bytes)), textCell(formatTime(item.modified_at), "updated"), actionCell);
    rows.appendChild(row);
  }
}

function renderBatchSummary() {
  const batches = state.batches;
  $("batchSummary").replaceChildren(
    metric(batches.length, "审核批次"),
    metric(batches.reduce((sum, item) => sum + item.chip_count, 0), "芯片型号"),
    metric(batches.reduce((sum, item) => sum + item.record_count, 0), "业务记录"),
    metric(batches.filter((item) => item.status !== "ready").length, "异常批次"),
  );
}
function batchMatches(batch) {
  const text = `${batch.name || ""} ${batch.run_id || ""}`.toLocaleLowerCase();
  if (state.batchQuery && !text.includes(state.batchQuery)) return false;
  if (state.batchStatus === "ready" && batch.status !== "ready") return false;
  if (state.batchStatus === "error" && batch.status === "ready") return false;
  if (state.review !== "all" && !(batch.review_counts?.[state.review] > 0)) return false;
  return true;
}
function renderBatchRows() {
  const rows = $("batchRows"); rows.replaceChildren();
  const visible = state.batches.filter(batchMatches);
  $("batchEmptyState").hidden = visible.length > 0;
  $("batchLoadState").textContent = `显示 ${visible.length}/${state.batches.length} 个批次`;
  for (const batch of visible) {
    const row = document.createElement("tr");
    const nameCell = document.createElement("td");
    const name = document.createElement("span"); name.className = "batch-name"; name.textContent = batch.name;
    const id = document.createElement("span"); id.className = "batch-id"; id.textContent = batch.run_id ? `运行编号：${batch.run_id}` : batch.batch_id;
    nameCell.append(name, id);
    const contentCell = document.createElement("td");
    const contentMain = document.createElement("span"); contentMain.className = "content-main"; contentMain.textContent = `${formatNumber(batch.chip_count)} 颗芯片`;
    const contentSub = document.createElement("span"); contentSub.className = "content-sub"; contentSub.textContent = `${formatNumber(batch.document_count)} 份文档 · ${formatNumber(batch.record_count)} 条记录`;
    contentCell.append(contentMain, contentSub);
    const counts = batch.review_counts || {};
    const reviewed = (counts.pending_export || 0) + (counts.exported || 0);
    const total = batch.chip_count || 0; const percent = total ? Math.round(reviewed * 100 / total) : 0;
    const progressCell = document.createElement("td");
    const progress = document.createElement("div"); progress.className = "progress-line";
    const track = document.createElement("span"); track.className = "progress-track";
    const value = document.createElement("span"); value.className = "progress-value"; value.style.width = `${percent}%`; track.appendChild(value);
    const progressLabel = document.createElement("span"); progressLabel.className = "progress-label"; progressLabel.textContent = `${reviewed}/${total}`;
    progress.append(track, progressLabel);
    const detail = document.createElement("div"); detail.className = "review-detail"; detail.append(`未审核 ${counts.unreviewed || 0}`);
    if (counts.issue) { const issue = document.createElement("span"); issue.className = "issue"; issue.textContent = ` · 有问题 ${counts.issue}`; detail.appendChild(issue); }
    progressCell.append(progress, detail);
    const statusCell = document.createElement("td");
    const status = document.createElement("span"); status.className = `status${batch.status === "ready" ? "" : " error"}`; status.textContent = BATCH_STATUS_LABELS[batch.status] || batch.status; statusCell.appendChild(status);
    if (batch.error) { const error = document.createElement("span"); error.className = "error-text"; error.textContent = batch.error; statusCell.appendChild(error); }
    const actionCell = document.createElement("td"); const open = document.createElement("button"); open.type = "button"; open.className = "open-button"; open.textContent = "进入审核"; open.disabled = batch.status !== "ready";
    open.addEventListener("click", () => { window.location.href = batch.review_url; }); actionCell.appendChild(open);
    row.append(nameCell, contentCell, progressCell, statusCell, textCell(formatTime(batch.modified_at), "updated"), actionCell); rows.appendChild(row);
  }
}

async function loadAll() {
  $("refreshAll").disabled = true;
  $("documentLoadState").textContent = "正在读取文档…";
  $("batchLoadState").textContent = "正在读取批次…";
  try {
    const [documentResponse, batchResponse] = await Promise.all([
      fetch("/api/documents", { cache: "no-store" }),
      fetch("/api/review/batches", { cache: "no-store" }),
    ]);
    if (!documentResponse.ok) throw new Error(`文档接口 HTTP ${documentResponse.status}`);
    if (!batchResponse.ok) throw new Error(`批次接口 HTTP ${batchResponse.status}`);
    const documentPayload = await documentResponse.json(); const batchPayload = await batchResponse.json();
    state.documents = Array.isArray(documentPayload.documents) ? documentPayload.documents : [];
    state.batches = Array.isArray(batchPayload.batches) ? batchPayload.batches : [];
    state.documentQuery = $("documentSearch").value.trim().toLocaleLowerCase();
    state.family = $("familyFilter").value;
    state.documentStatus = $("documentStatusFilter").value;
    state.duplicate = $("duplicateFilter").value;
    state.batchQuery = $("batchSearch").value.trim().toLocaleLowerCase();
    state.batchStatus = $("statusFilter").value;
    state.review = $("reviewFilter").value;
    $("inputDirectory").textContent = `输入目录：${documentPayload.input_dir || "—"}`;
    renderDocumentSummary(); renderDocumentRows(); renderBatchSummary(); renderBatchRows(); renderParseJob();
  } catch (error) {
    $("documentLoadState").textContent = `读取失败：${error.message}`; $("documentLoadState").classList.add("error");
  } finally { $("refreshAll").disabled = false; }
}

$("documentSearch").addEventListener("input", (event) => { state.documentQuery = event.target.value.trim().toLocaleLowerCase(); renderDocumentRows(); });
$("familyFilter").addEventListener("change", (event) => { state.family = event.target.value; renderDocumentRows(); });
$("documentStatusFilter").addEventListener("change", (event) => { state.documentStatus = event.target.value; renderDocumentRows(); });
$("duplicateFilter").addEventListener("change", (event) => { state.duplicate = event.target.value; renderDocumentRows(); });
$("batchSearch").addEventListener("input", (event) => { state.batchQuery = event.target.value.trim().toLocaleLowerCase(); renderBatchRows(); });
$("statusFilter").addEventListener("change", (event) => { state.batchStatus = event.target.value; renderBatchRows(); });
$("reviewFilter").addEventListener("change", (event) => { state.review = event.target.value; renderBatchRows(); });
$("refreshAll").addEventListener("click", loadAll);
loadAll();
refreshParseJob();
