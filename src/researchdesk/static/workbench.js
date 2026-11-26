import { renderMarkdown } from "./report.mjs";

const byId = (id) => document.getElementById(id);
const states = {
  running: "运行中", completed: "已完成", interrupted: "已中断",
  failed: "失败", budget_exhausted: "额度用尽",
};
const historyDate = new Intl.DateTimeFormat("zh-CN", {
  year: "numeric", month: "2-digit", day: "2-digit",
  hour: "2-digit", minute: "2-digit", hourCycle: "h23",
});
const percent = new Intl.NumberFormat("zh-CN", { style: "percent", maximumFractionDigits: 1 });
const modes = { recovery_demo: "离线恢复演示", demo: "离线示例", local: "本地资料", web: "网页研究" };
const messages = {
  403: "真实研究未启用，请使用离线示例或重新启动服务。",
  404: "找不到这次运行。",
  409: "当前无法执行该操作，请查看运行状态。",
  422: "请检查问题、资料和模型调用上限。",
};
let selection = location.hash.slice(1) || null;
let navigationVersion = 0;
let latest = null;
let renderedReport = null;
let renderedActivity = null;
let activeCitation = null;
let renderedHistory = null;
let historyRecords = null;
let refreshing = false;
let refreshQueued = false;
let connected = false;
let submitting = false;
let active = false;

class ConnectionError extends Error {}

async function request(path, options = { signal: AbortSignal.timeout(8000) }) {
  let response;
  try {
    response = await fetch(path, options);
  } catch {
    throw new ConnectionError("无法连接本地服务");
  }
  if (!response.ok) throw new Error(messages[response.status] || "操作失败，请重试。");
  return response.json();
}

function showError(error = null) {
  byId("error").hidden = !error;
  byId("error").textContent = error?.message || "";
}

function showRefreshError(message = "") {
  const warning = byId("refresh-error");
  warning.hidden = !message;
  if (warning.textContent !== message) warning.textContent = message;
}

function setConnectionLabel(message) {
  const label = byId("connection");
  if (label.textContent !== message) label.textContent = message;
}

function syncRunActions() {
  byId("start").disabled = !connected || submitting || active;
  byId("stop").disabled = !connected || submitting || !latest?.can_stop;
  byId("resume").disabled = !connected || submitting || !latest?.can_resume;
  syncDownload("download-summary", "run-summary.json", Boolean(latest));
  syncDownload("download-report", "report.zip", latest?.status === "completed" && Boolean(latest.report));
}

function syncDownload(elementId, filename, available) {
  const download = byId(elementId);
  download.hidden = !available;
  if (download.hidden || !connected) {
    download.removeAttribute("href");
    download.setAttribute("aria-disabled", "true");
  } else {
    download.href = "/api/runs/" + encodeURIComponent(latest.id) + "/" + filename;
    download.removeAttribute("aria-disabled");
  }
}

function setMode() {
  const mode = byId("mode").value;
  const live = mode === "local" || mode === "web";
  byId("question-fields").hidden = !live;
  byId("question").required = live;
  byId("source-fields").hidden = mode !== "local";
  byId("notes").required = mode === "local";
  byId("mode-help").textContent = {
    recovery_demo: "使用固定资料和离线模型，自动中断进程后可手动恢复。",
    demo: "使用固定资料演示报告与证据，无外部 API 请求。",
    local: "由模型分析你粘贴的资料，不发起网页搜索。",
    web: "提交后会调用模型服务和搜索服务。",
  }[mode];
  byId("start").textContent = {
    demo: "运行离线示例",
    recovery_demo: "开始恢复演示",
  }[mode] || "开始研究";
  syncRunActions();
}

function renderHistory() {
  if (!historyRecords) return;
  const query = byId("history-search").value.trim().toLowerCase();
  const status = byId("history-status").value;
  const records = historyRecords.filter((run) =>
    (!status || run.status === status) &&
    (run.question.toLowerCase().includes(query) || run.id.includes(query)));
  const filtering = Boolean(query || status);
  const key = JSON.stringify([selection, filtering, records]);
  if (key === renderedHistory) return;
  renderedHistory = key;
  const nav = byId("history");
  nav.replaceChildren();
  if (!records.length) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.textContent = filtering ? "没有匹配的运行记录" : "暂无运行记录";
    nav.append(empty);
  }
  for (const run of records) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "history-item";
    button.setAttribute("aria-current", String(selection === run.id));
    const title = document.createElement("span");
    title.className = "history-title";
    title.textContent = run.question;
    const meta = document.createElement("span");
    meta.className = "history-meta";
    meta.textContent = `${modes[run.mode]} · ${states[run.status]}`;
    button.append(title, meta);
    const created = new Date(run.created_at);
    if (Number.isFinite(created.getTime())) {
      const timestamp = document.createElement("time");
      timestamp.className = "history-meta";
      timestamp.dateTime = run.created_at;
      timestamp.textContent = historyDate.format(created);
      button.append(timestamp);
    }
    button.addEventListener("click", () => showRun(run.id));
    nav.append(button);
  }
}

const citationIssues = {
  invalid: "核对未通过",
  missing: "缺少证据",
};
const issueSelector = "#report .citation[data-issue]";

function showEvidence(id, evidence, button) {
  activeCitation?.setAttribute("aria-pressed", "false");
  activeCitation = button;
  button.setAttribute("aria-pressed", "true");
  byId("evidence-prompt").hidden = true;
  byId("evidence-content").hidden = false;
  byId("evidence-id").textContent = id;
  const title = byId("source-title");
  title.hidden = !evidence;
  title.textContent = evidence?.title || "来源摘录";
  byId("evidence-integrity").textContent = button.dataset.issue
    ? citationIssues[button.dataset.issue]
    : "摘录与来源原文匹配";
  const snippet = byId("snippet");
  snippet.hidden = !evidence;
  snippet.textContent = evidence?.snippet || "";
  const location = evidence?.location || "";
  byId("source-name").textContent = location;
  const link = byId("source-link");
  link.hidden = !/^https?:\/\//i.test(location);
  if (link.hidden) link.removeAttribute("href");
  else link.href = location;
  const heading = byId("evidence-title");
  heading.focus({ preventScroll: true });
  heading.scrollIntoView({ block: "start", inline: "nearest" });
}

function renderReport(run) {
  const key = JSON.stringify([run.id, run.report, run.evidence, run.status, run.exit_code, run.error_type]);
  if (key === renderedReport) return;
  renderedReport = key;
  activeCitation = null;
  byId("evidence-prompt").hidden = false;
  byId("evidence-content").hidden = true;
  const report = byId("report");
  report.replaceChildren();
  if (!run.report) {
    const placeholder = document.createElement("p");
    placeholder.className = "muted";
    placeholder.textContent = run.status === "running" ? "研究进行中，报告完成后会自动显示。"
      : "当前没有完成的报告。";
    report.append(placeholder);
    return;
  }
  renderMarkdown(report, run.report, (id) => {
    const evidence = run.evidence[id];
    const issue = evidence ? (evidence.integrity === "invalid" ? "invalid" : null) : "missing";
    const citation = document.createElement("button");
    citation.type = "button";
    citation.className = "citation";
    citation.textContent = issue ? id + " · " + citationIssues[issue] : id;
    if (issue) citation.dataset.issue = issue;
    citation.setAttribute("aria-label", "查看引用 " + citation.textContent);
    citation.setAttribute("aria-pressed", "false");
    citation.setAttribute("aria-controls", "evidence-content");
    citation.addEventListener("click", () => showEvidence(id, evidence, citation));
    return citation;
  });
}

const activityLabels = {
  model_started: "已发起模型请求", model_completed: "模型请求完成",
  model_failed: "模型请求失败", model_budget_denied: "模型请求受预算限制",
  started: "工具开始", completed: "工具完成", failed: "工具失败",
  rejected: "工具请求被拒绝", attempt_failed: "工具尝试失败",
  cache_hit: "命中缓存", replayed: "复用已记录的工具结果",
};

function renderActivity(run) {
  const events = run.activity ?? [];
  const key = JSON.stringify([run.id, events]);
  if (key === renderedActivity) return;
  renderedActivity = key;
  const panel = byId("run-activity");
  panel.hidden = !events.length;
  const list = byId("activity-events");
  list.replaceChildren();
  if (!events.length) return;
  const label = (item) => [activityLabels[item.event] || "记录运行事件", item.tool]
    .filter(Boolean).join(" · ");
  byId("activity-summary").textContent = "最近活动 · " + label(events[0]);
  for (const event of events) {
    const row = document.createElement("li");
    const time = new Date(event.timestamp);
    const timestamp = Number.isFinite(time.getTime()) ? time.toLocaleTimeString("zh-CN", { hour12: false }) : "";
    row.textContent = [timestamp, label(event), event.error_code, event.will_retry ? "将重试" : ""]
      .filter(Boolean).join(" · ");
    list.append(row);
  }
}

function renderOutcome(run) {
  const panel = byId("run-outcome");
  panel.hidden = !["failed", "interrupted", "budget_exhausted"].includes(run.status);
  if (panel.hidden) return;
  let reason;
  if (run.status === "interrupted") {
    reason = run.stopped_by_workbench ? "工作台已中断运行。"
      : run.recovery_demo_injected && !run.resume_count ? "演示已在报告写入后终止进程。"
      : "运行已中断。";
    reason += run.can_resume ? "可从已保存的状态继续。" : "当前无可用的网页恢复入口。";
  } else if (run.status === "budget_exhausted") {
    reason = "已达到模型调用上限。";
  } else if (run.exit_code === 2) {
    reason = "启动时未通过配置或参数检查。";
  } else if (run.error_type === "CitationValidationError") {
    reason = "报告未通过引用校验。";
  } else if (run.error_type === "RecoveryError") {
    reason = "已保存的运行状态未通过检查。";
  } else {
    reason = "研究运行失败。";
  }
  byId("outcome-reason").textContent = reason;
  const details = byId("outcome-details");
  const fields = [];
  if (Number.isInteger(run.exit_code)) fields.push("退出码 " + run.exit_code);
  if (/^[A-Za-z][A-Za-z0-9_]{0,79}$/.test(run.error_type ?? "")) fields.push(run.error_type);
  details.hidden = !fields.length;
  details.textContent = fields.join(" · ");
}

function renderRun(run) {
  latest = run;
  byId("run-id").textContent = run.id;
  byId("run-title").textContent = run.question;
  byId("run-status").textContent = states[run.status];
  byId("run-status").dataset.state = run.status;
  byId("stop").hidden = !run.can_stop;
  byId("resume").hidden = !run.can_resume;
  byId("resume-count").hidden = !run.resume_count;
  byId("resume-count").textContent = run.resume_count ? `已恢复 ${run.resume_count} 次` : "";
  byId("model-calls").textContent = run.model_limit
    ? `${run.metrics.model_calls ?? "—"} / ${run.model_limit}` : run.metrics.model_calls ?? "—";
  byId("tool-calls").textContent = run.metrics.tool_calls ?? "—";
  byId("provider-requests").textContent = run.metrics.provider_requests ?? "—";
  const { tool_success_rate, tool_successes, tool_outcomes, retry_count, recovery_replays } = run.metrics;
  byId("tool-success-rate").textContent = Number.isFinite(tool_success_rate) ? percent.format(tool_success_rate) : "—";
  byId("tool-outcomes").textContent = tool_outcomes ? tool_successes + " / " + tool_outcomes + " 已计入" : "";
  byId("tool-retries").textContent = retry_count ?? "—";
  byId("tool-replays").textContent = recovery_replays ?? "—";
  syncRunActions();
  const check = run.citation_check;
  const summary = byId("citation-check");
  summary.hidden = !run.report || !check;
  if (!summary.hidden) {
    summary.textContent = "引用原文核对 " +
      "· 匹配 " + check.matched + "/" + check.total +
      " · 未通过 " + check.mismatched + " · 缺失 " + check.missing;
  }
  const demo = byId("recovery-demo-state");
  demo.hidden = run.mode !== "recovery_demo";
  if (!demo.hidden) {
    demo.textContent = run.resume_count && run.status === "completed"
      ? "离线演示完成，已用原运行 ID 恢复并发布报告。"
      : "离线恢复演示使用固定资料和脚本模型。";
  }
  renderOutcome(run);
  renderActivity(run);
  renderReport(run);
  byId("locate-citation-issue").hidden = !document.querySelector(issueSelector);
  byId("run-loading").hidden = true;
  byId("editor").hidden = true;
  byId("detail").hidden = false;
}

async function refresh({ queueIfBusy = false } = {}) {
  if (refreshing) {
    if (queueIfBusy) refreshQueued = true;
    return;
  }
  refreshing = true;
  let selected = null;
  let version = navigationVersion;
  const reconnecting = !connected;
  try {
    const snapshot = await request("/api/runs");
    connected = true;
    setConnectionLabel(snapshot.allow_live ? "本地 · 支持真实研究" : "本地 · 离线模式");
    for (const option of byId("mode").options) {
      if (option.value === "local" || option.value === "web") option.disabled = !snapshot.allow_live;
    }
    active = snapshot.runs.some((run) => run.status === "running");
    setMode();
    historyRecords = snapshot.runs;
    renderHistory();
    selected = selection;
    version = navigationVersion;
    const selectedRecord = historyRecords.find((run) => run.id === selected);
    // A completed report stays readable from the last response until the user
    // reopens it, the connection returns, or its status changes.
    const reuseReport = !reconnecting && latest?.id === selected &&
      latest.status === "completed" && selectedRecord?.status === "completed";
    if (selected && !reuseReport) {
      const run = await request(`/api/runs/${encodeURIComponent(selected)}`);
      if (version !== navigationVersion) return;
      renderRun(run);
    }
    showRefreshError();
  } catch (error) {
    if (!selected || error instanceof ConnectionError) {
      connected = false;
      setConnectionLabel("连接中断");
      showRefreshError("暂时无法刷新运行记录，正在自动重连。");
    } else if (version === navigationVersion) {
      byId("run-loading").hidden = true;
      showRefreshError(error.message);
    }
  } finally {
    syncRunActions();
    refreshing = false;
    if (refreshQueued) {
      refreshQueued = false;
      void refresh();
    }
  }
}

function showPendingRun() {
  byId("editor").hidden = Boolean(selection);
  byId("run-loading").hidden = !selection;
  byId("detail").hidden = true;
}

function showRun(id, updateHistory = true) {
  if (id === selection) {
    latest = null;
    void refresh({ queueIfBusy: true });
    return;
  }
  navigationVersion += 1;
  selection = id;
  latest = null;
  showError();
  showRefreshError();
  syncRunActions();
  if (updateHistory) {
    history.pushState(null, "", id ? `#${id}` : location.pathname + location.search);
  }
  showPendingRun();
  renderHistory();
  void refresh({ queueIfBusy: true });
}

async function changeRun(path, data) {
  if (!connected || submitting) return;
  submitting = true;
  const version = navigationVersion;
  showError();
  setMode();
  try {
    const run = await request(path, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data ?? {}) });
    if (version === navigationVersion) showRun(run.id);
    else void refresh({ queueIfBusy: true });
  } catch (error) {
    if (version === navigationVersion) showError(error);
  } finally {
    submitting = false;
    setMode();
  }
}

byId("locate-citation-issue").addEventListener("click", () => document.querySelector(issueSelector)?.focus());
byId("back-to-citation").addEventListener("click", () => activeCitation?.focus());
byId("history-search").addEventListener("input", renderHistory);
const historyStatus = byId("history-status");
for (const [status, label] of Object.entries(states)) {
  historyStatus.add(new Option(label, status));
}
historyStatus.addEventListener("change", renderHistory);
byId("compose").addEventListener("click", () => showRun(null));
byId("mode").addEventListener("change", setMode);
byId("research-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const mode = byId("mode").value;
  const data = mode === "demo" || mode === "recovery_demo" ? { mode } : {
    mode, question: byId("question").value, notes: mode === "local" ? byId("notes").value : "",
    max_model_calls: Number(byId("budget").value),
  };
  void changeRun("/api/runs", data);
});
byId("stop").addEventListener("click", () => latest && changeRun(`/api/runs/${latest.id}/stop`));
byId("resume").addEventListener("click", () => latest && changeRun(`/api/runs/${latest.id}/resume`));
window.addEventListener("hashchange", () => showRun(location.hash.slice(1) || null, false));
showPendingRun();
setMode();
void refresh();
setInterval(refresh, 1500);
