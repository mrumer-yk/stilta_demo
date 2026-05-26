const state = { matters: [], active: null };

const els = {
  matterList: document.querySelector("#matter-list"),
  newMatter: document.querySelector("#new-matter"),
  modelLine: document.querySelector("#model-line"),
  workspaceTitle: document.querySelector("#workspace-title"),
  analysisStatus: document.querySelector("#analysis-status"),
  saveMatter: document.querySelector("#save-matter"),
  analyzeMatter: document.querySelector("#analyze-matter"),
  matterTitle: document.querySelector("#matter-title"),
  matterPatent: document.querySelector("#matter-patent"),
  matterPriority: document.querySelector("#matter-priority"),
  matterQuestion: document.querySelector("#matter-question"),
  matterClaim: document.querySelector("#matter-claim"),
  chartSummary: document.querySelector("#chart-summary"),
  sourceList: document.querySelector("#source-list"),
  sourceTitle: document.querySelector("#source-title"),
  sourceType: document.querySelector("#source-type"),
  sourceText: document.querySelector("#source-text"),
  sourceFile: document.querySelector("#source-file"),
  addSource: document.querySelector("#add-source"),
  claimChartBody: document.querySelector("#claim-chart tbody"),
  graphSummary: document.querySelector("#graph-summary"),
  evidenceGraph: document.querySelector("#evidence-graph-view"),
  warningsList: document.querySelector("#warnings-list"),
  chatLog: document.querySelector("#chat-log"),
  chatForm: document.querySelector("#chat-form"),
  chatInput: document.querySelector("#chat-input"),
  auditList: document.querySelector("#audit-list"),
  runEvals: document.querySelector("#run-evals"),
  reportPreview: document.querySelector("#report-preview-view"),
  openReport: document.querySelector("#open-report"),
  evalResults: document.querySelector("#eval-results"),
};

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const contentType = response.headers.get("content-type") || "";
  if (!contentType.includes("application/json")) {
    const text = await response.text();
    throw new Error(`Server returned non-JSON (${response.status}) for ${url}: ${text.slice(0, 120)}`);
  }
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || `Request failed (${response.status})`);
  return payload;
}

function activeId() {
  return state.active?.matter?.id;
}

function setBusy(button, busyText, isBusy) {
  if (!button) return;
  if (isBusy) {
    button.dataset.label = button.textContent;
    button.textContent = busyText;
    button.disabled = true;
  } else {
    button.textContent = button.dataset.label || button.textContent;
    button.disabled = false;
  }
}

// ── Render helpers ──────────────────────────────────────────────────

function renderModel(model) {
  const live = Boolean(model?.api_key_present);
  els.modelLine.textContent = live
    ? `OK ${model.model}`
    : `${model?.model || "Gemini"} - no key, deterministic mode`;
  els.modelLine.classList.toggle("ok", live);
}

function renderMatterList() {
  els.matterList.innerHTML = state.matters
    .map(
      (m) => `
      <article class="matter-card ${m.id === activeId() ? "active" : ""}">
        <button class="matter-item" data-id="${m.id}" type="button">
          <strong>${escapeHtml(m.title)}</strong>
          <span>${escapeHtml(m.question || "No question")}</span>
        </button>
        <button class="matter-delete" data-delete-matter-id="${m.id}" type="button" title="Delete matter" aria-label="Delete ${escapeHtml(m.title)}">x</button>
      </article>
    `,
    )
    .join("");
  els.matterList.querySelectorAll(".matter-item").forEach((b) =>
    b.addEventListener("click", () => loadMatter(b.dataset.id)),
  );
  els.matterList.querySelectorAll(".matter-delete").forEach((button) =>
    button.addEventListener("click", () => deleteMatter(button.dataset.deleteMatterId)),
  );
}

function clearWorkspace() {
  els.workspaceTitle.textContent = "Patent Evidence Agent";
  els.analysisStatus.textContent = "Create a matter to begin";
  els.analysisStatus.className = "status-pill";
  els.matterTitle.value = "";
  els.matterPatent.value = "";
  els.matterPriority.value = "";
  els.matterQuestion.value = "";
  els.matterClaim.value = "";
  if (els.chartSummary) els.chartSummary.textContent = "";
  if (els.graphSummary) els.graphSummary.textContent = "";
  els.sourceList.innerHTML = `<div class="empty-state">No matter selected.</div>`;
  els.claimChartBody.innerHTML = `<tr><td colspan="5" class="empty-cell">Create a matter, add claim text and sources, then analyze.</td></tr>`;
  if (els.evidenceGraph) {
    els.evidenceGraph.className = "evidence-graph empty-state";
    els.evidenceGraph.textContent = "Create a matter to build the graph.";
  }
  els.warningsList.className = "empty-state";
  els.warningsList.textContent = "No warnings.";
  els.chatLog.innerHTML = `<div class="empty-state">Create or select a matter to ask evidence questions.</div>`;
  els.auditList.className = "empty-state";
  els.auditList.textContent = "No activity yet.";
  renderReport(null);
}

function renderActiveMatter() {
  const data = state.active;
  if (!data) {
    clearWorkspace();
    return;
  }
  const { matter, sources, chart, warnings, messages, audit, report, graph } = data;

  els.workspaceTitle.textContent = matter.title || "Patent Evidence Agent";
  els.matterTitle.value = matter.title || "";
  els.matterPatent.value = matter.target_patent || "";
  els.matterPriority.value = matter.priority_date || "";
  els.matterQuestion.value = matter.question || "";
  els.matterClaim.value = matter.claim_text || "";

  // Status pill
  const hasInputs = Boolean(matter.claim_text && sources.length);
  const hasAnalysis = Boolean(chart.length);
  if (!hasInputs) {
    els.analysisStatus.textContent = "Add claim text and at least one source";
    els.analysisStatus.className = "status-pill";
    if (els.chartSummary) els.chartSummary.textContent = "";
  } else if (!hasAnalysis) {
    els.analysisStatus.textContent = "Ready to analyze";
    els.analysisStatus.className = "status-pill ready";
    if (els.chartSummary) els.chartSummary.textContent = "";
  } else {
    const strong = chart.filter((r) => r.support_level === "Strong").length;
    const missing = chart.filter((r) => r.support_level === "Missing").length;
    els.analysisStatus.textContent = `${chart.length} limitations mapped`;
    els.analysisStatus.className = "status-pill done";
    if (els.chartSummary) {
      els.chartSummary.textContent = `${strong} strong / ${missing} missing / ${warnings.length} warning${warnings.length !== 1 ? "s" : ""}`;
    }
  }

  renderSources(sources);
  renderChart(chart);
  renderGraph(graph, chart);
  renderWarnings(warnings);
  renderMessages(messages);
  renderAudit(audit);
  renderReport(report);
}

function renderSources(sources) {
  if (!sources.length) {
    els.sourceList.innerHTML = `<div class="empty-state">No sources added. Paste a patent excerpt, product spec, or technical paper below.</div>`;
    return;
  }
  els.sourceList.innerHTML = sources
    .map(
      (s) => `
      <article class="source-row">
        <div class="source-row-header">
          <div class="source-meta">
            <span class="source-id">${escapeHtml(s.id)}</span>
            <strong>${escapeHtml(s.title)}</strong>
            <span class="source-type">${escapeHtml(s.source_type)}</span>
          </div>
          <button class="source-delete" data-delete-id="${escapeHtml(s.id)}" type="button" title="Delete source" aria-label="Delete ${escapeHtml(s.title)}">x</button>
        </div>
        <p>${escapeHtml((s.redacted_text || s.text).slice(0, 200))}${s.text.length > 200 ? "..." : ""}</p>
      </article>
    `,
    )
    .join("");
  els.sourceList.querySelectorAll(".source-delete").forEach((btn) =>
    btn.addEventListener("click", () => deleteSource(btn.dataset.deleteId)),
  );
}

function supportClass(level) {
  return String(level || "missing").toLowerCase();
}

function renderOverlapTerms(terms) {
  if (!terms?.length) return "";
  return `<div class="overlap-terms">${terms
    .slice(0, 10)
    .map((t) => `<span class="term-pill">${escapeHtml(t)}</span>`)
    .join("")}</div>`;
}

function renderChart(chart) {
  if (!chart.length) {
    els.claimChartBody.innerHTML = `<tr><td colspan="5" class="empty-cell">Add claim text and sources, then click <strong>Analyze</strong> to generate the chart.</td></tr>`;
    return;
  }
  els.claimChartBody.innerHTML = chart
    .map(
      (row) => `
      <tr>
        <td>
          <span class="limitation-id">${escapeHtml(row.limitation_id)}</span>
          <p>${escapeHtml(row.limitation_text)}</p>
        </td>
        <td>${escapeHtml(row.interpretation)}</td>
        <td>
          <span class="support ${supportClass(row.support_level)}">${escapeHtml(row.support_level)}</span>
          <small>${Math.round((row.score || 0) * 100)}%</small>
        </td>
        <td>
          <strong>${escapeHtml(row.source_id || "-")}</strong>
          <p>${escapeHtml(row.snippet || "No cited snippet.")}</p>
          <small>${escapeHtml(row.rationale || "")}</small>
          ${renderOverlapTerms(row.overlap_terms)}
        </td>
        <td>${escapeHtml(row.review_question)}</td>
      </tr>
    `,
    )
    .join("");
}

function renderGraph(graph, chart = []) {
  if (!els.evidenceGraph) return;
  const nodes = graph?.nodes || [];
  const edges = graph?.edges || [];
  if (!nodes.length) {
    els.evidenceGraph.className = "evidence-graph empty-state";
    els.evidenceGraph.textContent = "Run analysis to build the graph.";
    if (els.graphSummary) els.graphSummary.textContent = "";
    return;
  }

  const nodeMap = new Map(nodes.map((node) => [node.id, node]));
  const evidenceRows = chart.filter((row) => row.source_id);
  const missingRows = chart.filter((row) => !row.source_id);
  const reviewEdges = edges.filter((edge) => edge.type !== "contains");
  if (els.graphSummary) {
    els.graphSummary.textContent = `${nodes.length} nodes / ${edges.length} edges / ${evidenceRows.length} evidence link${evidenceRows.length !== 1 ? "s" : ""}`;
  }

  const labelFor = (id) => {
    const node = nodeMap.get(id);
    if (!node) return id;
    return node.label || node.id;
  };

  els.evidenceGraph.className = "evidence-graph";
  els.evidenceGraph.innerHTML = `
    <div class="graph-kpis">
      <span><strong>${nodes.length}</strong> nodes</span>
      <span><strong>${edges.length}</strong> edges</span>
      <span><strong>${missingRows.length}</strong> missing</span>
    </div>
    <div class="graph-content">
      <div class="graph-column">
        <h3>Claim-to-source links</h3>
        ${
          chart.length
            ? chart
                .map(
                  (row) => `
            <article class="graph-card ${supportClass(row.support_level)}">
              <div>
                <span class="graph-node limitation">${escapeHtml(row.limitation_id)}</span>
                <span class="support ${supportClass(row.support_level)}">${escapeHtml(row.support_level)}</span>
              </div>
              <p>${escapeHtml(row.source_id ? row.snippet || row.rationale : row.review_question)}</p>
              <small>${escapeHtml(row.source_id || "No source linked yet")}</small>
            </article>
          `,
                )
                .join("")
            : `<div class="empty-state">No chart rows yet.</div>`
        }
      </div>
      <div class="graph-column">
        <h3>Graph edges</h3>
        ${
          reviewEdges.length
            ? reviewEdges
                .slice(0, 12)
                .map(
                  (edge) => `
            <article class="graph-edge-row">
              <span class="graph-node ${escapeHtml(nodeMap.get(edge.from)?.type || "node")}">${escapeHtml(labelFor(edge.from))}</span>
              <span class="graph-edge-label">${escapeHtml(edge.type)}</span>
              <span class="graph-node ${escapeHtml(nodeMap.get(edge.to)?.type || "node")}">${escapeHtml(labelFor(edge.to))}</span>
            </article>
          `,
                )
                .join("")
            : `<div class="empty-state">Only matter containment edges exist so far.</div>`
        }
      </div>
    </div>
  `;
}

function renderWarnings(warnings) {
  if (!warnings.length) {
    els.warningsList.className = "empty-state";
    els.warningsList.innerHTML = "No warnings.";
    return;
  }
  els.warningsList.className = "warnings-list";
  els.warningsList.innerHTML = warnings
    .map(
      (w) => `
      <article class="warning ${escapeHtml(w.severity)}">
        <strong>${escapeHtml(w.warning_type)}</strong>
        <p>${escapeHtml(w.message)}</p>
      </article>
    `,
    )
    .join("");
}

function renderMessages(messages) {
  if (!messages.length) {
    els.chatLog.innerHTML = `<div class="empty-state">Run analysis first, then ask questions about the evidence.</div>`;
    return;
  }
  els.chatLog.innerHTML = messages
    .map(
      (m) => `
      <article class="chat-message ${escapeHtml(m.role)}">
        <strong>${m.role === "user" ? "You" : "Assistant"}</strong>
        <p>${escapeHtml(m.content)}</p>
      </article>
    `,
    )
    .join("");
  els.chatLog.scrollTop = els.chatLog.scrollHeight;
}

function renderAudit(audit) {
  if (!audit.length) {
    els.auditList.className = "empty-state";
    els.auditList.textContent = "No activity yet.";
    return;
  }
  els.auditList.className = "audit-list";
  // Show only last 5 to keep it compact
  els.auditList.innerHTML = audit
    .slice(0, 5)
    .map(
      (item) => `
      <article class="audit-row ${escapeHtml(item.status)}">
        <span>${escapeHtml(item.agent_name)} / ${escapeHtml(item.status)}</span>
        <strong>${escapeHtml(item.input?.title || "Run")}</strong>
        <p>${escapeHtml(item.output?.body || "")}</p>
        ${
          item.tool_calls?.length
            ? `<small>Tools: ${item.tool_calls.map((tool) => escapeHtml(tool.tool_name)).join(", ")}</small>`
            : ""
        }
      </article>
    `,
    )
    .join("");
}

function renderReport(report) {
  if (!report) {
    els.reportPreview.className = "empty-state";
    els.reportPreview.textContent = "Run analysis to render a report.";
    if (els.openReport) els.openReport.disabled = true;
    return;
  }
  els.reportPreview.className = "report-preview";
  els.reportPreview.innerHTML = report.html;
  if (els.openReport) els.openReport.disabled = false;
}

// ── API calls ───────────────────────────────────────────────────────

async function loadState() {
  const payload = await fetchJson("/api/state");
  state.matters = payload.matters;
  state.active = payload.active;
  renderModel(payload.model);
  renderMatterList();
  renderActiveMatter();
}

async function loadMatter(id) {
  state.active = await fetchJson(`/api/matters/${id}`);
  await refreshMatterList();
  renderMatterList();
  renderActiveMatter();
}

async function refreshMatterList() {
  const payload = await fetchJson("/api/state");
  state.matters = payload.matters;
  renderModel(payload.model);
}

function matterPayload() {
  return {
    title: els.matterTitle.value.trim() || "Untitled Patent Matter",
    target_patent: els.matterPatent.value.trim(),
    priority_date: els.matterPriority.value.trim(),
    question: els.matterQuestion.value.trim(),
    claim_text: els.matterClaim.value.trim(),
  };
}

function nextClientSourceId() {
  const count = state.active?.sources?.length || 0;
  return `M${activeId()}-SRC-${String(count + 1).padStart(3, "0")}`;
}

async function saveMatter() {
  if (!activeId()) return;
  setBusy(els.saveMatter, "Saving...", true);
  try {
    state.active = await fetchJson(`/api/matters/${activeId()}/update`, {
      method: "POST",
      body: JSON.stringify(matterPayload()),
    });
    await refreshMatterList();
    renderMatterList();
    renderActiveMatter();
  } finally {
    setBusy(els.saveMatter, "Saving...", false);
  }
}

async function analyzeMatter() {
  if (!activeId()) return;
  setBusy(els.analyzeMatter, "Analyzing...", true);
  try {
    await saveMatter();
    const payload = await fetchJson(`/api/matters/${activeId()}/analyze`, {
      method: "POST",
      body: "{}",
    });
    state.active = payload.matter;
    renderModel(payload.model);
    await refreshMatterList();
    renderMatterList();
    renderActiveMatter();
    document
      .getElementById("claim-chart-section")
      ?.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (err) {
    alert(err.message);
  } finally {
    setBusy(els.analyzeMatter, "Analyzing...", false);
  }
}

async function createMatter() {
  const payload = await fetchJson("/api/matters", {
    method: "POST",
    body: JSON.stringify({
      title: "Untitled Patent Matter",
      question: "",
      claim_text: "",
    }),
  });
  state.active = payload;
  await refreshMatterList();
  renderMatterList();
  renderActiveMatter();
  // Focus on title so user can immediately start typing
  els.matterTitle.value = "";
  els.matterTitle.focus();
  els.matterTitle.select();
}


async function addSource() {
  if (!activeId()) return;
  const text = els.sourceText.value.trim();
  if (text.length < 10) {
    alert("Paste a source excerpt before adding.");
    return;
  }
  setBusy(els.addSource, "Adding...", true);
  try {
    const payload = await fetchJson(`/api/matters/${activeId()}/sources`, {
      method: "POST",
      body: JSON.stringify({
        id: nextClientSourceId(),
        title: els.sourceTitle.value.trim() || "Untitled Source",
        source_type: els.sourceType.value,
        text,
      }),
    });
    state.active = payload.matter;
    els.sourceTitle.value = "";
    els.sourceText.value = "";
    await refreshMatterList();
    renderMatterList();
    renderActiveMatter();
  } finally {
    setBusy(els.addSource, "Adding...", false);
  }
}

async function importSourceFile(event) {
  const file = event.target.files?.[0];
  if (!file) return;
  if (!activeId()) {
    alert("Create or select a matter before importing a source.");
    event.target.value = "";
    return;
  }

  const text = await file.text();
  if (text.trim().length < 10) {
    alert("This file does not contain enough text to add as evidence.");
    event.target.value = "";
    return;
  }

  const cleanName = file.name.replace(/\.[^.]+$/, "").replaceAll("_", " ");
  if (!els.sourceTitle.value.trim()) els.sourceTitle.value = cleanName;
  els.sourceText.value = text.trim();
  els.sourceText.focus();
  event.target.value = "";
}

function openReport() {
  if (!activeId()) return;
  if (!state.active?.report) {
    alert("Run analysis first to generate the report.");
    return;
  }
  window.open(`/api/matters/${activeId()}/report`, "_blank", "noopener");
}

async function deleteSource(sourceId) {
  if (!activeId() || !sourceId) return;
  try {
    state.active = await fetchJson(
      `/api/matters/${activeId()}/sources/${encodeURIComponent(sourceId)}`,
      { method: "DELETE" },
    );
    await refreshMatterList();
    renderMatterList();
    renderActiveMatter();
  } catch (err) {
    alert(err.message);
  }
}

async function deleteMatter(matterId) {
  if (!matterId) return;
  try {
    const payload = await fetchJson(`/api/matters/${matterId}`, { method: "DELETE" });
    state.matters = payload.matters || [];
    state.active = payload.active || null;
    renderModel(payload.model);
    renderMatterList();
    renderActiveMatter();
  } catch (err) {
    alert(err.message);
  }
}

async function askMatter(event) {
  event.preventDefault();
  const message = els.chatInput.value.trim();
  if (!message || !activeId()) return;
  els.chatInput.value = "";

  // Optimistic render with thinking indicator
  renderMessages([
    ...(state.active.messages || []),
    { role: "user", content: message },
    { role: "assistant", content: "Thinking..." },
  ]);

  const submitBtn = els.chatForm.querySelector("button[type=submit]");
  setBusy(submitBtn, "Thinking...", true);
  els.chatInput.disabled = true;

  try {
    const payload = await fetchJson(`/api/matters/${activeId()}/chat`, {
      method: "POST",
      body: JSON.stringify({ message }),
    });
    state.active = payload.matter;
    renderModel(payload.model);
    renderActiveMatter();
  } catch (err) {
    alert(err.message);
  } finally {
    setBusy(submitBtn, "Thinking...", false);
    els.chatInput.disabled = false;
    els.chatInput.focus();
  }
}

async function runEvals() {
  setBusy(els.runEvals, "Running...", true);
  try {
    const payload = await fetchJson("/api/evals");
    renderModel(payload.model);
    els.evalResults.innerHTML = `
      <div class="eval-summary">${payload.passed}/${payload.total} passing</div>
      ${payload.results
        .map(
          (item) => `
        <article class="eval-row ${item.passed ? "passed" : "failed"}">
          <strong>${escapeHtml(item.name)}</strong>
          <span>${item.passed ? "pass" : "fail"}</span>
          <p>${escapeHtml(item.details)}</p>
        </article>
      `,
        )
        .join("")}
    `;
  } finally {
    setBusy(els.runEvals, "Running...", false);
  }
}

// ── Event listeners ─────────────────────────────────────────────────

els.newMatter.addEventListener("click", createMatter);
els.saveMatter.addEventListener("click", saveMatter);
els.analyzeMatter.addEventListener("click", analyzeMatter);
els.addSource.addEventListener("click", addSource);
els.sourceFile?.addEventListener("change", importSourceFile);
els.chatForm.addEventListener("submit", askMatter);
els.runEvals.addEventListener("click", runEvals);
els.openReport?.addEventListener("click", openReport);

// Ctrl+Enter / Cmd+Enter to submit chat
els.chatInput.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
    e.preventDefault();
    els.chatForm.requestSubmit();
  }
});

// ── Boot ────────────────────────────────────────────────────────────

loadState().catch((err) => {
  document.body.innerHTML = `<pre style="padding:20px;color:#991b1b">Startup failed: ${escapeHtml(err.message)}</pre>`;
});
