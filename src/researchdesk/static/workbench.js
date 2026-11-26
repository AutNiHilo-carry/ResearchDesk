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
const modes = { demo: "离线示例", local: "本地资料", web: "网页研究" };
const messages = {
  403: "真实研究未启用，请使用离线示例或重新启动服务。",
  404: "找不到这次运行。",
  409: "当前无法执行该操作，请查看运行状态。",
  422: "请检查问题、资料和模型调用上限。",
};
let selection = location.hash.slice(1) || null;
let latest = null;
let renderedReport = null;
let activeCitation = null;
let renderedHistory = null;
let historyRecords = null;
let refreshing = false;
let submitting = false;
let active = false;

async function request(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) throw new Error(messages[response.status] || "操作失败，请重试。");
  return response.json();
}

function showError(error = null) {
  byId("error").hidden = !error;
  byId("error").textContent = error?.message || "";
}

function setMode() {
  const mode = byId("mode").value;
  byId("question-fields").hidden = mode === "demo";
  byId("question").required = mode !== "demo";
  byId("source-fields").hidden = mode !== "local";
  byId("notes").required = mode === "local";
  byId("mode-help").textContent = {
    demo: "使用固定资料演示报告与证据，无外部 API 请求。",
    local: "由模型分析你粘贴的资料，不发起网页搜索。",
    web: "提交后会调用模型服务和搜索服务。",
  }[mode];
  byId("start").textContent = mode === "demo" ? "运行离线示例" : "开始研究";
  byId("start").disabled = submitting || active;
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
      : run.exit_code === 2 ? "缺少模型或搜索服务配置。"
      : run.status === "interrupted" && run.can_resume ? "运行已中断，可以从已有状态恢复。"
      : run.status === "budget_exhausted" ? "模型调用额度已用完，当前没有完成的报告。"
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

function renderRun(run) {
  latest = run;
  byId("run-id").textContent = run.id;
  byId("run-title").textContent = run.question;
  byId("run-status").textContent = states[run.status];
  byId("run-status").dataset.state = run.status;
  byId("stop").hidden = !run.can_stop;
  byId("resume").hidden = !run.can_resume;
  byId("run-actions").hidden = !run.can_stop && !run.can_resume && !run.resume_count;
  byId("resume-count").textContent = run.resume_count ? `已恢复 ${run.resume_count} 次` : "";
  byId("model-calls").textContent = run.model_limit
    ? `${run.metrics.model_calls ?? "—"} / ${run.model_limit}` : run.metrics.model_calls ?? "—";
  byId("tool-calls").textContent = run.metrics.tool_calls ?? "—";
  byId("provider-requests").textContent = run.metrics.provider_requests ?? "—";
  const download = byId("download-report");
  download.hidden = run.status !== "completed" || !run.report;
  if (download.hidden) download.removeAttribute("href");
  else download.href = `/api/runs/${encodeURIComponent(run.id)}/report.zip`;
  const check = run.citation_check;
  const summary = byId("citation-check");
  summary.hidden = !run.report || !check;
  if (!summary.hidden) {
    summary.textContent = "引用原文核对 " +
      "· 匹配 " + check.matched + "/" + check.total +
      " · 未通过 " + check.mismatched + " · 缺失 " + check.missing;
  }
  renderReport(run);
  byId("locate-citation-issue").hidden = !document.querySelector(issueSelector);
  byId("editor").hidden = true;
  byId("detail").hidden = false;
}

async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    const snapshot = await request("/api/runs");
    byId("connection").textContent = snapshot.allow_live ? "本地 · 支持真实研究" : "本地 · 离线模式";
    for (const option of byId("mode").options) {
      if (option.value !== "demo") option.disabled = !snapshot.allow_live;
    }
    active = snapshot.runs.some((run) => run.status === "running");
    setMode();
    historyRecords = snapshot.runs;
    renderHistory();
    const selected = selection;
    if (selected) {
      const run = await request(`/api/runs/${encodeURIComponent(selected)}`);
      if (selection === selected) renderRun(run);
    }
  } catch (error) {
    showError(error);
  } finally {
    refreshing = false;
  }
}

function showRun(id) {
  selection = id;
  latest = null;
  showError();
  history.replaceState(null, "", id ? `#${id}` : location.pathname);
  if (!id) {
    byId("editor").hidden = false;
    byId("detail").hidden = true;
  }
  void refresh();
}

async function changeRun(path, data) {
  if (submitting) return;
  submitting = true;
  showError();
  setMode();
  byId("resume").disabled = true;
  byId("stop").disabled = true;
  try {
    const run = await request(path, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data ?? {}) });
    showRun(run.id);
  } catch (error) {
    showError(error);
  } finally {
    submitting = false;
    byId("resume").disabled = false;
    byId("stop").disabled = false;
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
  const data = mode === "demo" ? { mode } : {
    mode, question: byId("question").value, notes: mode === "local" ? byId("notes").value : "",
    max_model_calls: Number(byId("budget").value),
  };
  void changeRun("/api/runs", data);
});
byId("stop").addEventListener("click", () => latest && changeRun(`/api/runs/${latest.id}/stop`));
byId("resume").addEventListener("click", () => latest && changeRun(`/api/runs/${latest.id}/resume`));
setMode();
void refresh();
setInterval(refresh, 1500);
