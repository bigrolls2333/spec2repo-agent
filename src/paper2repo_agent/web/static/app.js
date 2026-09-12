const state = {
  runs: [],
  selectedRun: null,
  detail: null,
  activeTab: "overview",
  reviewSelections: {},
  pendingRunName: null,
};
const ACTIVE_JOB_POLL_MS = 2000;
let jobPollHandle = null;

const $ = (id) => {
  const node = document.getElementById(id);
  if (!node) throw new Error(`Missing element: ${id}`);
  return node;
};

async function api(url, init) {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json", ...((init && init.headers) || {}) },
    ...init,
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || response.statusText);
  }
  const contentType = response.headers.get("content-type") || "";
  if (contentType.includes("application/json")) return response.json();
  return response.text();
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function formatValue(value) {
  if (value === null || value === undefined) return "";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function stableValue(value) {
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function valuesEqual(left, right) {
  return stableValue(left) === stableValue(right);
}

function parseCustomValue(raw) {
  const trimmed = raw.trim();
  if (!trimmed) return null;
  try {
    return JSON.parse(trimmed);
  } catch {
    return trimmed;
  }
}

function showToast(message) {
  const toast = $("toast");
  toast.textContent = message;
  toast.classList.add("show");
  window.setTimeout(() => toast.classList.remove("show"), 2600);
}

function statusClass(status) {
  const value = String(status || "").toLowerCase();
  if (["partial", "skipped", "awaiting", "planned", "running"].some((item) => value.includes(item))) return "warn";
  if (["approved", "executed", "passed", "succeeded"].some((item) => value.includes(item))) return "ok";
  if (["failed", "cancelled", "error"].some((item) => value.includes(item))) return "fail";
  return "";
}

const STATUS_LABELS = {
  queued: "排队中",
  running: "运行中",
  succeeded: "已完成",
  failed: "失败",
  completed: "已完成",
  approved: "已确认",
  approval_recorded: "已记录确认",
  awaiting_review: "等待人工确认",
  not_started: "未开始",
  "not started": "未开始",
  planned: "已生成计划",
  revised: "已修订",
  executed: "已执行",
  passed: "通过",
  partial: "部分完成",
  partial_success: "部分成功",
  skipped: "已跳过",
  cancelled: "已取消",
  success: "成功",
  error: "错误",
  unknown: "未知",
  waiting: "等待中",
};

const JOB_KIND_LABELS = {
  plan: "规划",
  execute: "执行",
};

const SUMMARY_STAGE_LABELS = {
  PaperSpec: "论文规格抽取",
  ReproductionPlan: "复现计划",
  "HITL proposals": "人工确认建议",
  CodeImplementationPlan: "代码实现计划",
  Coding: "代码生成",
  "Coding fallback": "代码回退",
  LLM: "LLM 摘要",
};

const ARTIFACT_LABELS = {
  manifest: "运行清单",
  parsed_paper: "解析后的论文",
  evidence_store: "证据库 JSONL",
  evidence_refs: "证据引用",
  paper_spec: "论文规格",
  validation_result: "规格校验结果",
  draft_plan: "草案计划",
  human_review: "人工确认文档",
  latest_plan: "最新计划",
  latest_human_review: "最新人工确认",
  approved_plan: "已确认计划",
  execution_policy: "执行策略",
  file_generation_tasks: "文件生成任务",
  generated_files: "已生成文件记录",
  provenance: "生成溯源",
  command_specs: "命令规格",
  tool_calls: "工具调用",
  command_results: "命令结果",
  verification_result: "验证结果",
  execution_result: "执行结果",
  final_report: "最终报告",
};

function displayStatus(value) {
  const text = String(value ?? "").trim();
  if (!text) return "-";
  return STATUS_LABELS[text] || STATUS_LABELS[text.toLowerCase()] || text;
}

function displayJobKind(value) {
  const text = String(value ?? "").trim();
  return JOB_KIND_LABELS[text] || text || "-";
}

function displaySummaryStage(value) {
  const text = String(value ?? "").trim();
  return SUMMARY_STAGE_LABELS[text] || text || "LLM 摘要";
}

function displayArtifactKey(value) {
  const text = String(value ?? "").trim();
  return ARTIFACT_LABELS[text] || text;
}

function displayContext() {
  return state.detail?.display_context || {};
}

function claimInfo(claimId) {
  const claims = displayContext().claims || {};
  return claims[String(claimId)] || null;
}

function evidenceInfo(evidenceId) {
  const evidence = displayContext().evidence || {};
  return evidence[String(evidenceId)] || null;
}

function fieldInfo(fieldPath) {
  const fields = displayContext().fields || {};
  return fields[String(fieldPath)] || null;
}

function displayFieldLabel(fieldPath) {
  return fieldInfo(fieldPath)?.label || fieldPath;
}

function formatDecisionValue(value, item) {
  if (item?.field_path === "selected_claims") {
    return formatClaimValue(value);
  }
  if (typeof value === "boolean") return value ? "是" : "否";
  if (Array.isArray(value)) return value.map((itemValue) => formatDecisionValue(itemValue, null)).join("；");
  if (value && typeof value === "object") return JSON.stringify(value);
  return formatValue(value);
}

function formatClaimValue(value) {
  if (Array.isArray(value)) {
    return value.map((claimId) => formatClaimValue(claimId)).join("；");
  }
  const claim = claimInfo(value);
  if (!claim) return formatValue(value);
  const prefix = claim.claim_type ? `${claim.claim_type}：` : "";
  return `${prefix}${claim.description || claim.id}`;
}

function renderEvidencePreview(evidenceIds) {
  const ids = Array.isArray(evidenceIds) ? evidenceIds : [];
  const items = ids
    .map((evidenceId) => evidenceInfo(evidenceId))
    .filter(Boolean)
    .slice(0, 3);
  if (!items.length) return "";
  return `
    <div class="evidence-preview">
      ${items.map((item) => `
        <div class="evidence-preview-item">
          <code>${escapeHtml(item.id)}</code>
          <span>${escapeHtml(item.section || item.source || "论文证据")}</span>
          <p>${escapeHtml(item.text || "")}</p>
        </div>
      `).join("")}
    </div>
  `;
}

async function loadStatus() {
  try {
    const status = await api("/api/status");
    $("apiDot").className = "status-dot ok";
    $("apiStatus").textContent = `${status.run_count} 个运行`;
    $("runSubtitle").textContent = status.runs_dir;
  } catch {
    $("apiDot").className = "status-dot fail";
    $("apiStatus").textContent = "离线";
  }
}

async function loadRuns() {
  state.runs = await api("/api/runs");
  $("runCount").textContent = String(state.runs.length);
  renderRuns();
  if (!state.selectedRun && state.runs[0]) {
    await selectRun(state.runs[0].run_name);
  }
}

async function loadJobs() {
  const jobs = await api("/api/jobs");
  if (_hasActiveJobs(jobs)) {
    scheduleJobPolling();
  } else {
    stopJobPolling();
  }
  const completedPendingJob = jobs.find((job) => (
    state.pendingRunName
    && job.run_name === state.pendingRunName
    && job.kind === "plan"
    && job.status === "succeeded"
  ));
  const failedPendingJob = jobs.find((job) => (
    state.pendingRunName
    && job.run_name === state.pendingRunName
    && job.kind === "plan"
    && job.status === "failed"
  ));
  $("jobCount").textContent = String(jobs.length);
  const list = $("jobList");
  list.innerHTML = jobs.length ? "" : `<div class="job-item">暂无任务。</div>`;
  for (const job of jobs.slice(0, 6)) {
    const summaries = Array.isArray(job.summaries) ? job.summaries.slice(-4) : [];
    const logs = Array.isArray(job.logs)
      ? job.logs.filter((line) => !isSummaryLog(line)).slice(-6)
      : [];
    const visualStatus = job.outcome_status || job.status;
    const statusText = job.outcome_status
      ? `${displayStatus(job.status)} · ${displayStatus(job.outcome_status)}`
      : displayStatus(job.status);
    const outcomeMeta = job.outcome_label ? `<span>${escapeHtml(displayStatus(job.outcome_label))}</span>` : "";
    const item = document.createElement("div");
    item.className = "job-item";
    item.innerHTML = `
      <div class="item-title">
        <span>${escapeHtml(displayJobKind(job.kind))} · ${escapeHtml(job.id)}</span>
        <span class="status-dot ${statusClass(visualStatus)}"></span>
      </div>
      <div class="item-meta">
        <span>${escapeHtml(statusText)}</span>
        <span>${escapeHtml(job.run_name || "")}</span>
        ${outcomeMeta}
      </div>
      ${summaries.length ? `
        <div class="job-summaries">
          ${summaries.map((summary) => `
            <div class="job-summary">
              <span>${escapeHtml(displaySummaryStage(summary.stage || "LLM"))}</span>
              <p>${escapeHtml(summary.text || "")}</p>
            </div>
          `).join("")}
        </div>
      ` : ""}
      ${logs.length ? `
        <ol class="job-log">
          ${logs.map((line) => `<li>${escapeHtml(cleanProgressMessage(line))}</li>`).join("")}
        </ol>
      ` : ""}
      ${job.error ? `<div class="item-meta">${escapeHtml(job.error)}</div>` : ""}
    `;
    list.appendChild(item);
  }
  if (completedPendingJob) {
    const runName = state.pendingRunName;
    state.pendingRunName = null;
    await loadRuns();
    await selectRun(runName);
    activateTab("review");
    showToast("规划已完成，请进入人工确认。");
  } else if (failedPendingJob) {
    renderWorkingState(state.pendingRunName, "规划任务失败。请查看左侧任务栏的错误和日志。");
    state.pendingRunName = null;
    showToast("规划任务失败。");
  }
}

function _hasActiveJobs(jobs) {
  return jobs.some((job) => job.status === "queued" || job.status === "running");
}

function isSummaryLog(message) {
  return String(message || "").startsWith("LLM summary");
}

function stopJobPolling() {
  if (jobPollHandle !== null) {
    window.clearTimeout(jobPollHandle);
    jobPollHandle = null;
  }
}

function scheduleJobPolling() {
  stopJobPolling();
  jobPollHandle = window.setTimeout(() => {
    loadJobs().catch(() => {
      stopJobPolling();
    });
  }, ACTIVE_JOB_POLL_MS);
}

function cleanProgressMessage(message) {
  return String(message || "").replace(/^\[paper2repo\]\s*/, "");
}

function activateTab(tab) {
  state.activeTab = tab;
  document.querySelectorAll(".tab").forEach((item) => {
    item.classList.toggle("active", item.dataset.tab === tab);
  });
  document.querySelectorAll(".tab-panel").forEach((panel) => panel.classList.remove("active"));
  const panel = $(`tab-${tab}`);
  panel.classList.add("active");
}

function renderWorkingState(runName, message) {
  $("runTitle").textContent = runName || "运行中";
  $("runSubtitle").textContent = message;
  setText("statusValue", "运行中");
  setText("approvalValue", "等待中");
  setText("executionValue", "未开始");
  setText("updatedValue", new Date().toLocaleTimeString());
  $("planScope").innerHTML = `
    <div class="empty-state">
      <strong>Paper2Repo 正在解析论文并生成复现计划。</strong>
      <p>左侧任务栏会显示 MinerU 解析、证据构建、LLM 抽取、规划和人工确认建议的进度。</p>
    </div>
  `;
  $("claimsList").innerHTML = `<li>论文规格抽取完成后会显示选中的论文主张。</li>`;
  $("reviewSummary").textContent = "规划完成后会在这里显示需要确认的问题。";
  $("confirmationList").innerHTML = `<div class="confirmation-item">当前无需操作，请等待规划任务完成。</div>`;
}

function renderRuns() {
  const list = $("runList");
  list.innerHTML = state.runs.length ? "" : `<div class="run-item">暂无运行记录。</div>`;
  for (const run of state.runs) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `run-item ${state.selectedRun === run.run_name ? "active" : ""}`;
    button.innerHTML = `
      <div class="item-title">
        <span>${escapeHtml(run.run_name)}</span>
        <span class="status-dot ${statusClass(run.status)}"></span>
      </div>
      <div class="item-meta">
        <span>${escapeHtml(displayStatus(run.status))}</span>
        <span>${escapeHtml(displayStatus(run.approval_status))}</span>
      </div>
    `;
    button.addEventListener("click", () => selectRun(run.run_name));
    list.appendChild(button);
  }
}

async function selectRun(runName) {
  state.selectedRun = runName;
  state.detail = await api(`/api/runs/${encodeURIComponent(runName)}`);
  renderRuns();
  renderDetail();
}

function setText(id, value) {
  $(id).textContent = String(value ?? "-");
}

function renderDetail() {
  const detail = state.detail;
  if (!detail) return;
  const manifest = detail.manifest || {};
  const plan = detail.plan || {};
  $("runTitle").textContent = detail.run_name;
  $("runSubtitle").textContent = detail.run_dir;
  setText("statusValue", displayStatus(manifest.status));
  setText("approvalValue", displayStatus(manifest.approval_status));
  setText("executionValue", displayStatus(detail.execution_result?.final_status || "not started"));
  setText("updatedValue", manifest.updated_at);
  renderPlanScope(plan);
  renderClaims(plan);
  renderReview(detail.review);
  renderExecution(detail.execution_result, detail.final_report);
  renderArtifacts(detail.artifacts);
}

function renderPlanScope(plan) {
  const scope = plan?.scope || {};
  const objective = plan?.objective || {};
  const constraints = plan?.user_constraints || {};
  const rows = [
    ["论文", plan?.paper_title],
    ["目标", objective.goal],
    ["框架", scope.framework],
    ["设备", scope.device_target],
    ["数据集", scope.dataset_mode],
    ["算力", constraints.compute_target],
    ["包名", plan?.implementation_strategy?.package_name],
    ["用户要求", constraints.initial_request],
  ];
  $("planScope").innerHTML = rows.map(([key, value]) => `
    <div class="kv"><span>${escapeHtml(key)}</span><strong>${escapeHtml(value || "-")}</strong></div>
  `).join("");
}

function renderClaims(plan) {
  const claims = plan?.selected_claims || [];
  $("claimsList").innerHTML = claims.length
    ? claims.map((claimId) => {
      const claim = claimInfo(claimId);
      if (!claim) return `<li><strong>${escapeHtml(claimId)}</strong></li>`;
      return `
        <li>
          <strong>${escapeHtml(claim.description || claim.id)}</strong>
          <div class="item-meta">
            <span>ID：${escapeHtml(claim.id)}</span>
            <span>类型：${escapeHtml(claim.claim_type || "-")}</span>
            <span>可行性：${escapeHtml(claim.feasibility || "-")}</span>
          </div>
          ${renderEvidencePreview(claim.evidence_ids)}
        </li>
      `;
    }).join("")
    : `<li>暂无选中的论文主张。</li>`;
}

function renderReview(review) {
  $("reviewSummary").textContent = review?.summary || "暂无人工确认产物。";
  const items = review?.confirmation_items || [];
  $("reviewCount").textContent = String(items.length);
  $("confirmationList").innerHTML = items.length
    ? items.map((item, index) => `
      <div class="confirmation-item">
        <div class="confirmation-head">
          <strong>${escapeHtml(displayFieldLabel(item.field_path))}</strong>
          <span class="status-badge">${escapeHtml(item.severity || "-")}</span>
        </div>
        <div class="item-meta"><span>字段：${escapeHtml(item.field_path)}</span></div>
        <div class="question-text">${escapeHtml(item.question || item.reason || "")}</div>
        <div class="question-reason">${escapeHtml(item.reason || "")}</div>
        <div class="item-meta">
          <span>建议值：${escapeHtml(formatDecisionValue(item.suggested_value, item))}</span>
          <span>当前值：${escapeHtml(formatDecisionValue(item.current_value, item))}</span>
          <span>证据：${escapeHtml((item.evidence_ids || []).join(", ") || "无")}</span>
        </div>
        ${renderEvidencePreview(item.evidence_ids)}
        <div class="choice-row" data-confirmation-index="${index}">
          ${renderChoiceButtons(item, index)}
          <button class="choice-btn secondary" type="button" data-confirmation-index="${index}" data-action="defer">稍后处理</button>
          <button class="choice-btn secondary" type="button" data-confirmation-index="${index}" data-action="reject">拒绝</button>
        </div>
        <div class="review-reply">
          <textarea class="decision-custom-input" data-confirmation-index="${index}" placeholder="也可以直接输入自定义答案或修改要求"></textarea>
          <div class="reply-hint">这里的输入会覆盖当前问题已选择的选项。</div>
        </div>
      </div>
    `).join("")
    : `<div class="confirmation-item">暂无需要确认的条目。</div>`;
  bindDecisionControls();
}

function renderChoiceButtons(item, index) {
  const options = Array.isArray(item.options) && item.options.length
    ? item.options
    : [item.suggested_value].filter((value) => value !== null && value !== undefined && value !== "");
  const unique = [];
  for (const option of options) {
    if (!unique.some((existing) => valuesEqual(existing, option))) unique.push(option);
  }
  return unique.map((option, optionIndex) => `
    <button
      class="choice-btn ${optionIndex === 0 ? "active" : ""}"
      type="button"
      data-confirmation-index="${index}"
      data-option-index="${optionIndex}"
      title="${escapeHtml(formatValue(option))}"
    >
      ${escapeHtml(formatDecisionValue(option, item) || "使用建议值")}
    </button>
  `).join("");
}

function renderDecisionValueControl(item, index) {
  const options = Array.isArray(item.options) ? item.options : [];
  if (options.length) {
    const renderedOptions = options.map((option, optionIndex) => `
      <option value="${optionIndex}" ${valuesEqual(option, item.suggested_value) ? "selected" : ""}>
        ${escapeHtml(formatValue(option))}
      </option>
    `).join("");
    return `
      <div class="decision-value-grid">
        <select class="decision-value-select" data-confirmation-index="${index}">
          ${renderedOptions}
          <option value="__custom__">自定义值...</option>
        </select>
        <input class="decision-custom-input" data-confirmation-index="${index}" placeholder="自定义值" />
      </div>
    `;
  }
  return `
    <input
      class="decision-free-input"
      data-confirmation-index="${index}"
      value="${escapeHtml(formatValue(item.suggested_value))}"
      placeholder="值"
    />
  `;
}

function bindDecisionControls() {
  document.querySelectorAll(".choice-btn").forEach((button) => {
    button.addEventListener("click", () => {
      const index = button.dataset.confirmationIndex;
      if (index === undefined) return;
      document
        .querySelectorAll(`.choice-btn[data-confirmation-index="${index}"]`)
        .forEach((item) => item.classList.remove("active"));
      button.classList.add("active");
      const input = document.querySelector(`.decision-custom-input[data-confirmation-index="${index}"]`);
      if (input) input.value = "";
    });
  });
  document.querySelectorAll(".decision-value-select").forEach((select) => {
    select.addEventListener("change", () => {
      const index = select.dataset.confirmationIndex;
      if (index === undefined) return;
      setDecisionAction(index, "override");
      if (select.value === "__custom__") {
        const input = document.querySelector(`.decision-custom-input[data-confirmation-index="${index}"]`);
        input?.focus();
      }
    });
  });
  document.querySelectorAll(".decision-custom-input, .decision-free-input").forEach((input) => {
    input.addEventListener("input", () => {
      const index = input.dataset.confirmationIndex;
      if (index === undefined) return;
      setDecisionAction(index, "override");
    });
  });
}

function setDecisionAction(index, action) {
  const actionSelect = document.querySelector(`.decision-action[data-confirmation-index="${index}"]`);
  if (actionSelect) actionSelect.value = action;
}

function renderExecution(execution, finalReport) {
  const rows = [
    ["最终状态", displayStatus(execution?.final_status || "not started")],
    ["生成仓库", execution?.output_dir || "-"],
    ["报告状态", displayStatus(finalReport?.status || "-")],
    ["摘要", finalReport?.verification_summary || "-"],
  ];
  $("executionSummary").innerHTML = rows.map(([key, value]) => `
    <div class="kv"><span>${escapeHtml(key)}</span><strong>${escapeHtml(value)}</strong></div>
  `).join("");
  const checks = execution?.verification_result?.checks || [];
  $("checkList").innerHTML = checks.length
    ? checks.map((check) => `
      <div class="check-item">
        <strong>${escapeHtml(check.check_id)}</strong>
        <span class="status-badge">${escapeHtml(displayStatus(check.status))}</span>
        <div><code>${escapeHtml(check.command || "")}</code></div>
        ${check.stderr_tail ? `<div class="item-meta">${escapeHtml(check.stderr_tail)}</div>` : ""}
      </div>
    `).join("")
    : `<div class="check-item">暂无检查结果。</div>`;
}

function renderArtifacts(artifacts) {
  const select = $("artifactSelect");
  select.innerHTML = "";
  for (const [key, path] of Object.entries(artifacts || {})) {
    if (!path) continue;
    const option = document.createElement("option");
    option.value = path;
    option.textContent = displayArtifactKey(key);
    select.appendChild(option);
  }
  if (select.options.length) {
    loadArtifact(select.value);
  } else {
    $("artifactPreview").textContent = "暂无产物。";
  }
}

async function loadArtifact(path) {
  if (!state.selectedRun || !path) return;
  const content = await api(`/api/runs/${encodeURIComponent(state.selectedRun)}/artifact?path=${encodeURIComponent(path)}`);
  $("artifactPreview").textContent = typeof content === "string" ? content : JSON.stringify(content, null, 2);
}

async function searchEvidence() {
  if (!state.selectedRun) return;
  const params = new URLSearchParams();
  const fieldPath = $("fieldPathInput").value.trim();
  const evidenceId = $("evidenceIdInput").value.trim();
  const q = $("evidenceQueryInput").value.trim();
  if (fieldPath) params.set("field_path", fieldPath);
  if (evidenceId) params.set("evidence_id", evidenceId);
  if (q) params.set("q", q);
  const rows = await api(`/api/runs/${encodeURIComponent(state.selectedRun)}/evidence?${params.toString()}`);
  $("evidenceList").innerHTML = rows.length
    ? rows.map((row) => `
      <div class="evidence-item">
        <div><code>${escapeHtml(row.id || row.evidence_id)}</code></div>
        <div>${escapeHtml(row.text || "")}</div>
        <div class="item-meta">
          <span>${escapeHtml(row.field_path || "")}</span>
          <span>${escapeHtml(row.section || "")}</span>
          <span>${escapeHtml(row.relation || "")}</span>
        </div>
      </div>
    `).join("")
    : `<div class="evidence-item">没有匹配的证据。</div>`;
}

async function startPlan() {
  const paperPath = $("paperPathInput").value.trim();
  const runName = $("runNameInput").value.trim();
  const sourceType = $("sourceTypeInput").value;
  const fakeLlm = $("fakeLlmInput").checked;
  const initialRequest = $("initialRequestInput").value.trim();
  if (!paperPath || !runName) {
    showToast("请填写论文路径和运行名称。");
    return;
  }
  await api("/api/plan", {
    method: "POST",
    body: JSON.stringify({
      paper_path: paperPath,
      run_name: runName,
      source_type: sourceType,
      fake_llm: fakeLlm,
      initial_request: initialRequest,
      constraints: { initial_request: initialRequest },
    }),
  });
  state.pendingRunName = runName;
  renderWorkingState(runName, "规划任务已启动，可在左侧任务栏查看进度。");
  activateTab("overview");
  showToast("规划任务已启动。");
  await loadJobs();
}

async function approveSelected() {
  if (!state.selectedRun) return;
  const decisions = collectReviewDecisions({ fillMissing: true });
  await api(`/api/runs/${encodeURIComponent(state.selectedRun)}/approve`, {
    method: "POST",
    body: JSON.stringify({ approved_by: "web", decisions }),
  });
  showToast("计划已确认。");
  await selectRun(state.selectedRun);
}

async function reviseSelected() {
  if (!state.selectedRun) return;
  const decisions = collectReviewDecisions({ fillMissing: true });
  const userNotes = decisions
    .filter((decision) => decision.note)
    .map((decision) => `${decision.item_id}: ${decision.note}`)
    .join("\n");
  await api(`/api/runs/${encodeURIComponent(state.selectedRun)}/revise`, {
    method: "POST",
    body: JSON.stringify({
      approved_by: "web",
      decisions,
      user_notes: userNotes || "请根据用户在确认项中的选择修改计划。",
      fake_llm: $("fakeLlmInput").checked,
    }),
  });
  showToast("已提交计划修改请求。");
  await selectRun(state.selectedRun);
}

function collectReviewDecisions(options) {
  const items = state.detail?.review?.confirmation_items || [];
  return items.map((item, index) => {
    const customInput = document.querySelector(`.decision-custom-input[data-confirmation-index="${index}"]`);
    const active = document.querySelector(`.choice-btn.active[data-confirmation-index="${index}"]`);
    const customText = customInput?.value.trim() || "";
    let action = active?.dataset.action || "accept";
    let value = null;
    if (customText) {
      action = "override";
      value = parseCustomValue(customText);
    } else if (action === "accept") {
      value = readChoiceValue(item, active);
      if (!valuesEqual(value, item.suggested_value)) action = "override";
    } else if (options?.fillMissing) {
      value = item.suggested_value ?? item.current_value ?? null;
    }
    return {
      item_id: item.field_path,
      action,
      value,
      note: customText || null,
    };
  });
}

function readChoiceValue(item, activeButton) {
  const options = Array.isArray(item.options) && item.options.length
    ? item.options
    : [item.suggested_value];
  const index = Number(activeButton?.dataset.optionIndex || 0);
  if (Number.isInteger(index) && index >= 0 && index < options.length) {
    return options[index];
  }
  return item.suggested_value;
}

function readDecisionValue(item, index) {
  const options = Array.isArray(item.options) ? item.options : [];
  const valueSelect = document.querySelector(`.decision-value-select[data-confirmation-index="${index}"]`);
  const customInput = document.querySelector(`.decision-custom-input[data-confirmation-index="${index}"]`);
  const freeInput = document.querySelector(`.decision-free-input[data-confirmation-index="${index}"]`);

  if (customInput?.value.trim()) {
    return parseCustomValue(customInput.value);
  }
  if (valueSelect && valueSelect.value !== "__custom__") {
    const optionIndex = Number(valueSelect.value);
    if (Number.isInteger(optionIndex) && optionIndex >= 0 && optionIndex < options.length) {
      return options[optionIndex];
    }
  }
  if (freeInput?.value.trim()) {
    if (freeInput.value.trim() === formatValue(item.suggested_value)) {
      return item.suggested_value;
    }
    return parseCustomValue(freeInput.value);
  }
  return item.suggested_value;
}

async function executeSelected() {
  if (!state.selectedRun) return;
  await api(`/api/runs/${encodeURIComponent(state.selectedRun)}/execute`, { method: "POST" });
  showToast("执行任务已启动。");
  await loadJobs();
}

function bindTabs() {
  document.querySelectorAll(".tab").forEach((button) => {
    button.addEventListener("click", () => {
      const tab = button.dataset.tab || "overview";
      activateTab(tab);
    });
  });
}

async function refreshAll() {
  await loadStatus();
  await loadRuns();
  await loadJobs();
  if (state.selectedRun) {
    await selectRun(state.selectedRun);
  }
}

function bindEvents() {
  $("refreshBtn").addEventListener("click", () => refreshAll().catch((error) => showToast(error.message)));
  $("startPlanBtn").addEventListener("click", () => startPlan().catch((error) => showToast(error.message)));
  $("reviseBtn").addEventListener("click", () => reviseSelected().catch((error) => showToast(error.message)));
  $("approveBtn").addEventListener("click", () => approveSelected().catch((error) => showToast(error.message)));
  $("executeBtn").addEventListener("click", () => executeSelected().catch((error) => showToast(error.message)));
  $("searchEvidenceBtn").addEventListener("click", () => searchEvidence().catch((error) => showToast(error.message)));
  $("artifactSelect").addEventListener("change", (event) => {
    const value = event.target.value;
    loadArtifact(value).catch((error) => showToast(error.message));
  });
}

bindTabs();
bindEvents();
refreshAll().catch((error) => showToast(error.message));
