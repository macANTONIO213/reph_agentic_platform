let currentSessionId = null;
let lastRunId = null;

function getCookie(name) {
  const value = `; ${document.cookie}`;
  const parts = value.split(`; ${name}=`);
  if (parts.length === 2) {
    return parts.pop().split(";").shift();
  }
  return "";
}

function appendMessage(kind, title, text) {
  const log = document.querySelector("#chatLog");
  const message = document.createElement("div");
  message.className = `message ${kind}`;
  const heading = document.createElement("strong");
  heading.textContent = title;
  const paragraph = document.createElement("p");
  paragraph.textContent = text || "";
  message.append(heading, paragraph);
  log.appendChild(message);
  log.scrollTop = log.scrollHeight;
  return paragraph;
}

function appendTool(eventData) {
  const list = document.querySelector("#toolList");
  if (list.querySelector(".empty")) {
    list.innerHTML = "";
  }
  const item = document.createElement("article");
  const heading = document.createElement("strong");
  heading.textContent = eventData.tool_name;
  const summary = document.createElement("span");
  summary.textContent = eventData.summary;
  const output = document.createElement("pre");
  output.textContent = JSON.stringify(eventData.output, null, 2);
  item.append(heading, summary, output);
  list.prepend(item);
}

function appendFeedbackWidget(runId) {
  const log = document.querySelector("#chatLog");
  const widget = document.createElement("div");
  widget.className = "message feedback-widget";
  widget.dataset.runId = runId;
  widget.innerHTML = `
    <strong>Was this helpful?</strong>
    <div class="star-row" role="group" aria-label="Rate this response">
      ${[1, 2, 3, 4, 5].map(n => `<button class="star" type="button" data-rating="${n}" aria-label="${n} star${n > 1 ? "s" : ""}">★</button>`).join("")}
    </div>
  `;
  widget.querySelectorAll(".star").forEach((btn) => {
    btn.addEventListener("click", () => submitFeedback(runId, Number(btn.dataset.rating), widget));
  });
  log.appendChild(widget);
  log.scrollTop = log.scrollHeight;
}

async function submitFeedback(runId, rating, widget) {
  widget.innerHTML = `<strong>Thanks for rating ${rating}/5!</strong>`;
  try {
    await fetch(`/api/runs/${runId}/feedback/`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": getCookie("csrftoken"),
      },
      body: JSON.stringify({ rating }),
    });
  } catch (_) {
    // feedback is best-effort; ignore network errors
  }
}

async function refreshTelemetry() {
  const response = await fetch("/api/telemetry/");
  const payload = await response.json();
  const list = document.querySelector("#eventList");
  list.innerHTML = payload.events
    .map(
      (event) => `
        <article>
          <strong>${event.event_type}</strong>
          <span>${event.agent} | ${new Date(event.created_at).toLocaleTimeString()}</span>
        </article>
      `,
    )
    .join("");
}

async function streamAgentRun(agentId, payload, agentParagraph) {
  const response = await fetch(`/api/agents/${agentId}/run/`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-CSRFToken": getCookie("csrftoken"),
    },
    body: JSON.stringify(payload),
  });

  if (response.status === 403) {
    const errorPayload = await response.json();
    const err = new Error(errorPayload.error || "Access denied.");
    err.requiresApproval = !!errorPayload.requires_approval;
    throw err;
  }

  if (!response.ok || !response.body) {
    const errorPayload = await response.json();
    throw new Error(errorPayload.error || "The agent run failed.");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split("\n\n");
    buffer = frames.pop();
    for (const frame of frames) {
      const lines = frame.split("\n");
      const eventLine = lines.find((line) => line.startsWith("event: "));
      const dataLine = lines.find((line) => line.startsWith("data: "));
      if (!eventLine || !dataLine) continue;
      const eventName = eventLine.slice(7);
      const eventData = JSON.parse(dataLine.slice(6));
      if (eventName === "token") {
        agentParagraph.textContent += eventData.text;
        document.querySelector("#chatLog").scrollTop = document.querySelector("#chatLog").scrollHeight;
      }
      if (eventName === "tool") {
        appendTool(eventData);
      }
      if (eventName === "done") {
        appendTool({
          tool_name: "run_completed",
          summary: `${eventData.tool_calls} tool call${eventData.tool_calls !== 1 ? "s" : ""} · ${eventData.latency_ms}ms${eventData.model_id && eventData.model_id !== "fake" ? ` · ${eventData.model_id}` : ""}${eventData.input_tokens ? ` · ${eventData.input_tokens + eventData.output_tokens} tokens` : ""}`,
          output: eventData,
        });
        if (eventData.session_id) {
          currentSessionId = eventData.session_id;
        }
        if (eventData.run_id) {
          lastRunId = eventData.run_id;
          appendFeedbackWidget(lastRunId);
        }
      }
      if (eventName === "error") {
        agentParagraph.textContent = `Run failed: ${eventData.message}`;
      }
    }
  }
}

async function runAgent(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const agentId = form.dataset.agentId;
  const textarea = document.querySelector("#agentMessage");
  const message = textarea.value.trim();
  if (!message) return;

  appendMessage("user", "You", message);
  const agentParagraph = appendMessage("agent", "Agent Deployment Advisor", "");
  document.querySelector("#toolList").innerHTML = `<div class="empty">Run started...</div>`;

  const submitButton = form.querySelector("button");
  submitButton.disabled = true;
  submitButton.textContent = "Running...";

  const payload = { message };
  if (currentSessionId) {
    payload.session_id = currentSessionId;
  }

  try {
    await streamAgentRun(agentId, payload, agentParagraph);
    await refreshTelemetry();
  } catch (error) {
    if (error.requiresApproval) {
      const confirmed = confirm(
        "⚠ Tier 4 (high-risk) agent — human approval required.\n\n" +
        error.message +
        "\n\nConfirm that you have authorisation to run this agent and accept responsibility for its actions.",
      );
      if (confirmed) {
        try {
          agentParagraph.textContent = "";
          await streamAgentRun(agentId, { ...payload, human_approved: true }, agentParagraph);
          await refreshTelemetry();
        } catch (retryError) {
          agentParagraph.textContent = `Run failed: ${retryError.message}`;
        }
      } else {
        agentParagraph.textContent = "Run cancelled — human approval was not confirmed.";
      }
    } else {
      agentParagraph.textContent = `Run failed: ${error.message}`;
    }
  } finally {
    submitButton.disabled = false;
    submitButton.textContent = "Run agent";
  }
}

function setActiveView(viewName) {
  document.querySelectorAll("[data-view-panel]").forEach((panel) => {
    panel.classList.toggle("active", panel.dataset.viewPanel === viewName);
  });
  document.querySelectorAll("[data-view-tab]").forEach((tab) => {
    const isActive = tab.dataset.viewTab === viewName;
    tab.classList.toggle("active", isActive);
    tab.setAttribute("aria-current", isActive ? "page" : "false");
  });
  if (viewName === "catalog") {
    applyCatalogFilters();
  }
}

function dedupeFilterOptions() {
  document.querySelectorAll("select[data-catalog-filter]").forEach((select) => {
    const seen = new Set();
    Array.from(select.options).forEach((option) => {
      const key = option.value || "__all__";
      if (seen.has(key)) {
        option.remove();
        return;
      }
      seen.add(key);
    });
  });
}

function selectedFilterSummary() {
  const labels = Array.from(document.querySelectorAll("select[data-catalog-filter]"))
    .filter((select) => select.value)
    .map((select) => select.options[select.selectedIndex].textContent.trim());
  if (document.querySelector("#publishedFilter")?.getAttribute("aria-pressed") === "true") {
    labels.push("Published");
  }
  const searchValue = document.querySelector("#catalogSearch")?.value.trim();
  if (searchValue) {
    labels.push(`"${searchValue}"`);
  }
  return labels.length ? labels.join(" | ") : "all catalog entries";
}

function applyCatalogFilters() {
  const rows = Array.from(document.querySelectorAll("#agents tbody tr:not(#catalogEmptyRow)"));
  const searchValue = (document.querySelector("#catalogSearch")?.value || "").trim().toLowerCase();
  const publishedOnly = document.querySelector("#publishedFilter")?.getAttribute("aria-pressed") === "true";
  const filters = Object.fromEntries(
    Array.from(document.querySelectorAll("select[data-catalog-filter]")).map((select) => [
      select.dataset.catalogFilter,
      select.value,
    ]),
  );

  // Map camelCase filter keys to kebab-case data attributes on rows
  const dataKey = (key) => key.replace(/([A-Z])/g, "-$1").toLowerCase();

  let visibleCount = 0;
  rows.forEach((row) => {
    const matchesSearch = !searchValue || row.textContent.toLowerCase().includes(searchValue);
    const matchesPublished = !publishedOnly || row.dataset.status === "production";
    const matchesFilters = Object.entries(filters).every(([key, value]) => {
      if (!value) return true;
      return row.dataset[dataKey(key)] === value || row.dataset[key] === value;
    });
    const isVisible = matchesSearch && matchesPublished && matchesFilters;
    row.hidden = !isVisible;
    if (isVisible) visibleCount += 1;
  });

  const emptyRow = document.querySelector("#catalogEmptyRow");
  if (emptyRow) {
    emptyRow.hidden = visibleCount > 0;
  }
  const count = document.querySelector("#catalogResultCount");
  if (count) {
    count.textContent = `${visibleCount} agent${visibleCount === 1 ? "" : "s"}`;
  }
  const summary = document.querySelector("#catalogFilterSummary");
  if (summary) {
    summary.textContent = selectedFilterSummary();
  }
}

function resetCatalogFilters() {
  document.querySelectorAll("select[data-catalog-filter]").forEach((select) => {
    select.value = "";
  });
  const catalogSearch = document.querySelector("#catalogSearch");
  if (catalogSearch) {
    catalogSearch.value = "";
  }
  const platformSearch = document.querySelector("#platformSearch");
  if (platformSearch) {
    platformSearch.value = "";
  }
  document.querySelector("#publishedFilter")?.setAttribute("aria-pressed", "false");
  applyCatalogFilters();
}

// ── Cascading org filters ────────────────────────────────────────────────────

async function loadOrgChildren(level, parentId, targetSelect, placeholder) {
  const url = `/api/org/children/?level=${level}${parentId ? `&parent_id=${parentId}` : ""}`;
  const resp = await fetch(url);
  const { items } = await resp.json();
  targetSelect.innerHTML = `<option value="">${placeholder}</option>`;
  items.forEach(({ id, name }) => {
    const opt = document.createElement("option");
    opt.value = id;
    opt.textContent = name;
    targetSelect.appendChild(opt);
  });
  targetSelect.dispatchEvent(new Event("change"));
}

const filterBU = document.querySelector("#filterBU");
const filterDiv = document.querySelector("#filterDivision");
const filterWS = document.querySelector("#filterWorkStream");

if (filterBU) {
  filterBU.addEventListener("change", () => {
    loadOrgChildren("divisions", filterBU.value, filterDiv, "all divisions");
    if (filterWS) {
      filterWS.innerHTML = `<option value="">all work streams</option>`;
      filterWS.dispatchEvent(new Event("change"));
    }
  });
}

if (filterDiv) {
  filterDiv.addEventListener("change", () => {
    if (filterWS) {
      loadOrgChildren("workstreams", filterDiv.value, filterWS, "all work streams");
    }
  });
}

// Initialise divisions for the currently selected BU on page load
if (filterBU && filterBU.value && filterDiv) {
  loadOrgChildren("divisions", filterBU.value, filterDiv, "all divisions");
}

dedupeFilterOptions();
setActiveView(window.location.hash === "#catalog" ? "catalog" : window.location.hash === "#monitoring" ? "monitoring" : "deployments");
document.querySelector("#agentRunForm")?.addEventListener("submit", runAgent);
document.querySelector("#refreshTelemetry")?.addEventListener("click", refreshTelemetry);
document.querySelectorAll("[data-view-tab]").forEach((tab) => {
  tab.addEventListener("click", () => {
    setActiveView(tab.dataset.viewTab);
  });
});
document.querySelector("#catalogSearch")?.addEventListener("input", applyCatalogFilters);
document.querySelector("#platformSearch")?.addEventListener("input", (event) => {
  const catalogSearch = document.querySelector("#catalogSearch");
  if (catalogSearch) {
    catalogSearch.value = event.currentTarget.value;
  }
  if (event.currentTarget.value.trim()) {
    setActiveView("catalog");
  } else {
    applyCatalogFilters();
  }
});
document.querySelectorAll("select[data-catalog-filter]").forEach((select) => {
  select.addEventListener("change", applyCatalogFilters);
});
document.querySelector("#publishedFilter")?.addEventListener("click", (event) => {
  const isPressed = event.currentTarget.getAttribute("aria-pressed") === "true";
  event.currentTarget.setAttribute("aria-pressed", isPressed ? "false" : "true");
  applyCatalogFilters();
});
document.querySelector("#catalogResetFilters")?.addEventListener("click", resetCatalogFilters);

// ── Monitoring filters — 4-level hierarchy + agent ────────────────────────

async function monLoadSelect(selectId, url, placeholder) {
  const sel = document.getElementById(selectId);
  if (!sel) return;
  const resp = await fetch(url);
  const data = await resp.json();
  const items = data.items || data.agents || [];
  sel.innerHTML = `<option value="">${placeholder}</option>`;
  items.forEach(({ id, name }) => {
    const opt = document.createElement("option");
    opt.value = id;
    opt.textContent = name;
    sel.appendChild(opt);
  });
}

function monOrgParams() {
  const v = (id) => document.getElementById(id)?.value || "";
  return {
    business_unit: v("monBU"),
    division:      v("monDivision"),
    work_stream:   v("monWorkStream"),
    process:       v("monProcess"),
    agent:         v("monAgent"),
  };
}

function monQueryString() {
  const w = monWindow();
  const params = new URLSearchParams({ window: w });
  const { business_unit, division, work_stream, process, agent } = monOrgParams();
  if (business_unit) params.set("business_unit", business_unit);
  if (division)      params.set("division",      division);
  if (work_stream)   params.set("work_stream",   work_stream);
  if (process)       params.set("process",       process);
  if (agent)         params.set("agent",         agent);
  return params.toString();
}

async function monRefreshAgentOptions() {
  const { business_unit, division, work_stream, process } = monOrgParams();
  const params = new URLSearchParams();
  if (business_unit) params.set("business_unit", business_unit);
  if (division)      params.set("division",      division);
  if (work_stream)   params.set("work_stream",   work_stream);
  if (process)       params.set("process",       process);
  await monLoadSelect("monAgent", `/api/v1/agents/options/?${params}`, "All agents");
}

function monClearChildren(...ids) {
  ids.forEach((id) => {
    const sel = document.getElementById(id);
    if (sel) {
      const placeholder = sel.options[0]?.textContent || "All";
      sel.innerHTML = `<option value="">${placeholder}</option>`;
    }
  });
}

// Wire cascading selects
const monBU = document.getElementById("monBU");
const monDiv = document.getElementById("monDivision");
const monWS  = document.getElementById("monWorkStream");
const monProc = document.getElementById("monProcess");
const monAgentSel = document.getElementById("monAgent");

if (monBU) {
  monBU.addEventListener("change", async () => {
    monClearChildren("monDivision", "monWorkStream", "monProcess", "monAgent");
    if (monBU.value) {
      await monLoadSelect("monDivision", `/api/org/children/?level=divisions&parent_id=${monBU.value}`, "All divisions");
    }
    loadMonitoring();
  });
}

if (monDiv) {
  monDiv.addEventListener("change", async () => {
    monClearChildren("monWorkStream", "monProcess", "monAgent");
    if (monDiv.value) {
      await monLoadSelect("monWorkStream", `/api/org/children/?level=workstreams&parent_id=${monDiv.value}`, "All work streams");
    }
    await monRefreshAgentOptions();
    loadMonitoring();
  });
}

if (monWS) {
  monWS.addEventListener("change", async () => {
    monClearChildren("monProcess", "monAgent");
    if (monWS.value) {
      await monLoadSelect("monProcess", `/api/org/children/?level=processes&parent_id=${monWS.value}`, "All processes");
    }
    await monRefreshAgentOptions();
    loadMonitoring();
  });
}

if (monProc) {
  monProc.addEventListener("change", async () => {
    monClearChildren("monAgent");
    await monRefreshAgentOptions();
    loadMonitoring();
  });
}

if (monAgentSel) {
  monAgentSel.addEventListener("change", () => loadMonitoring());
}

document.getElementById("monFilterClear")?.addEventListener("click", async () => {
  ["monBU", "monDivision", "monWorkStream", "monProcess"].forEach((id) => {
    const sel = document.getElementById(id);
    if (sel) sel.value = "";
  });
  monClearChildren("monDivision", "monWorkStream", "monProcess");
  await monRefreshAgentOptions();
  loadMonitoring();
});

// ── Monitoring dashboard ───────────────────────────────────────────────────

const CHART_COLORS = {
  orange:  "#f58220",
  green:   "#0b742d",
  blue:    "#146a86",
  cyan:    "#17a3cf",
  purple:  "#b02896",
  red:     "#c0392b",
  muted:   "#aaaaaa",
};

let _charts = {};

function _destroyChart(id) {
  if (_charts[id]) { _charts[id].destroy(); delete _charts[id]; }
}

function monWindow() {
  return document.querySelector("#monWindow")?.value || "30d";
}

function fmtNum(n) {
  if (n === null || n === undefined) return "—";
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + "M";
  if (n >= 1_000) return (n / 1_000).toFixed(1) + "k";
  return String(n);
}

function fmtCost(v) {
  const f = parseFloat(v);
  if (isNaN(f)) return "—";
  if (f === 0) return "$0.00";
  if (f < 0.01) return `$${f.toFixed(5)}`;
  return `$${f.toFixed(3)}`;
}

async function loadMonitoringSummary() {
  const resp = await fetch(`/api/v1/monitoring/summary/?${monQueryString()}`);
  const d = await resp.json();

  const set = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };

  set("mt-total-runs", fmtNum(d.total_runs));
  const delta = d.run_delta_pct;
  set("mt-total-runs-delta", delta !== null ? `${delta > 0 ? "+" : ""}${delta}% vs prev period` : "");
  set("mt-success-rate", d.success_rate + "%");
  set("mt-success-rate-sub", `${d.succeeded} ok · ${d.failed} failed`);
  set("mt-active-users", fmtNum(d.active_users));
  set("mt-p50", d.p50_latency_ms + "ms");
  set("mt-p95", `p95: ${d.p95_latency_ms}ms`);
  set("mt-p99", d.p99_latency_ms + "ms");
  set("mt-cost", fmtCost(d.total_cost_usd));
  const totalTok = (d.total_input_tokens || 0) + (d.total_output_tokens || 0);
  set("mt-tokens", fmtNum(totalTok));
  set("mt-tokens-sub", `${fmtNum(d.total_input_tokens)} in · ${fmtNum(d.total_output_tokens)} out`);
  set("mt-satisfaction", d.avg_satisfaction > 0 ? d.avg_satisfaction.toFixed(1) + "★" : "—");
  set("mt-pending", String(d.pending_reviews));
}

async function loadMonitoringTimeseries() {
  const resp = await fetch(`/api/v1/monitoring/timeseries/?${monQueryString()}`);
  const d = await resp.json();

  const labels = d.runs.map((r) => r.date);

  // Runs chart
  _destroyChart("runs");
  const ctxRuns = document.getElementById("chartRuns");
  if (ctxRuns) {
    _charts.runs = new Chart(ctxRuns, {
      type: "bar",
      data: {
        labels,
        datasets: [
          {
            label: "Succeeded",
            data: d.runs.map((r) => r.succeeded),
            backgroundColor: CHART_COLORS.green + "cc",
            stack: "runs",
          },
          {
            label: "Failed",
            data: d.runs.map((r) => r.failed),
            backgroundColor: CHART_COLORS.orange + "cc",
            stack: "runs",
          },
        ],
      },
      options: {
        responsive: true,
        plugins: { legend: { position: "bottom" } },
        scales: { x: { stacked: true }, y: { stacked: true, beginAtZero: true } },
      },
    });
  }

  // Latency chart
  _destroyChart("latency");
  const ctxLat = document.getElementById("chartLatency");
  if (ctxLat) {
    _charts.latency = new Chart(ctxLat, {
      type: "line",
      data: {
        labels: d.latency.map((r) => r.date),
        datasets: [
          {
            label: "Avg latency (ms)",
            data: d.latency.map((r) => r.avg_latency_ms),
            borderColor: CHART_COLORS.blue,
            backgroundColor: CHART_COLORS.blue + "22",
            fill: true,
            tension: 0.3,
            pointRadius: 3,
          },
        ],
      },
      options: {
        responsive: true,
        plugins: { legend: { display: false } },
        scales: { y: { beginAtZero: true } },
      },
    });
  }

  // Cost chart
  _destroyChart("cost");
  const ctxCost = document.getElementById("chartCost");
  if (ctxCost) {
    _charts.cost = new Chart(ctxCost, {
      type: "line",
      data: {
        labels,
        datasets: [
          {
            label: "Cost USD",
            data: d.runs.map((r) => r.cost_usd),
            borderColor: CHART_COLORS.purple,
            backgroundColor: CHART_COLORS.purple + "22",
            fill: true,
            tension: 0.3,
            pointRadius: 3,
          },
        ],
      },
      options: {
        responsive: true,
        plugins: { legend: { display: false } },
        scales: { y: { beginAtZero: true } },
      },
    });
  }
}

async function loadMonitoringBreakdowns() {
  const resp = await fetch(`/api/v1/monitoring/breakdowns/?${monQueryString()}`);
  const d = await resp.json();

  // Rating distribution chart
  _destroyChart("ratings");
  const ctxRat = document.getElementById("chartRatings");
  if (ctxRat) {
    _charts.ratings = new Chart(ctxRat, {
      type: "bar",
      data: {
        labels: ["1★", "2★", "3★", "4★", "5★"],
        datasets: [
          {
            data: d.ratings.map((r) => r.count),
            backgroundColor: [
              CHART_COLORS.red + "cc",
              CHART_COLORS.orange + "cc",
              CHART_COLORS.muted + "cc",
              CHART_COLORS.cyan + "cc",
              CHART_COLORS.green + "cc",
            ],
          },
        ],
      },
      options: {
        responsive: true,
        plugins: { legend: { display: false } },
        scales: { y: { beginAtZero: true } },
      },
    });
  }

  // By platform
  renderBdList("bdPlatform", d.by_platform, "platform", "count");

  // By agent
  renderBdList("bdAgent", d.by_agent, "agent_name", "count");

  // Low-rated runs
  const container = document.getElementById("bdLowRated");
  if (container) {
    if (!d.low_rated || d.low_rated.length === 0) {
      container.innerHTML = `<div class="bd-empty">No low-rated runs in this period.</div>`;
    } else {
      container.innerHTML = d.low_rated.slice(0, 8).map((r) => `
        <div class="low-rated-item">
          <strong>${escHtml(r.agent_name)}</strong>
          <div class="lr-meta">
            <span class="lr-stars">${"★".repeat(r.rating)}${"☆".repeat(5 - r.rating)}</span>
            · ${escHtml(r.submitted_by)}
            ${r.comment ? `· <em>${escHtml(r.comment.slice(0, 60))}</em>` : ""}
          </div>
        </div>
      `).join("");
    }
  }
}

function renderBdList(containerId, items, labelKey, valueKey) {
  const container = document.getElementById(containerId);
  if (!container) return;
  if (!items || items.length === 0) {
    container.innerHTML = `<div class="bd-empty">No data.</div>`;
    return;
  }
  const maxVal = Math.max(...items.map((i) => i[valueKey]), 1);
  container.innerHTML = items.slice(0, 8).map((item) => {
    const pct = Math.round((item[valueKey] / maxVal) * 100);
    return `
      <div class="bd-row">
        <span class="bd-label" title="${escHtml(String(item[labelKey]))}">${escHtml(String(item[labelKey]))}</span>
        <div class="bd-bar-wrap"><div class="bd-bar" style="width:${pct}%"></div></div>
        <span class="bd-value">${item[valueKey]}</span>
      </div>
    `;
  }).join("");
}

function escHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

async function loadMonitoring() {
  await Promise.all([
    loadMonitoringSummary(),
    loadMonitoringTimeseries(),
    loadMonitoringBreakdowns(),
  ]);
}

// Override setActiveView to load monitoring on first visit
const _origSetActiveView = setActiveView;
// Patch to trigger monitoring load when switching to monitoring tab
let _monitoringLoaded = false;
document.querySelectorAll("[data-view-tab]").forEach((tab) => {
  tab.addEventListener("click", () => {
    if (tab.dataset.viewTab === "monitoring") {
      if (!_monitoringLoaded) {
        _monitoringLoaded = true;
        monRefreshAgentOptions();
      }
      loadMonitoring();
    }
  });
});

document.querySelector("#monWindow")?.addEventListener("change", loadMonitoring);
document.querySelector("#refreshMonitoring")?.addEventListener("click", loadMonitoring);

// Auto-refresh monitoring every 30s when the tab is visible
setInterval(() => {
  const panel = document.querySelector("[data-view-panel='monitoring']");
  if (panel && panel.classList.contains("active")) {
    loadMonitoringSummary();
  }
}, 30_000);

// ── Agent picker (Deployments tab) ──────────────────────────────────────────

(function () {
  const picker   = document.getElementById("agentPicker");
  const form     = document.getElementById("agentRunForm");
  if (!picker || !form) return;

  function applySelection(opt) {
    if (!opt) return;

    // Update form target
    form.dataset.agentId = opt.value;

    // Update heading block
    const name    = opt.dataset.name    || opt.textContent.split("—")[0].trim();
    const owner   = opt.dataset.owner   || "";
    const version = opt.dataset.version || "";
    const statusD = opt.dataset.statusDisplay || opt.dataset.status || "";
    const status  = opt.dataset.status || "";
    const platform = opt.dataset.platform || "";
    const purpose  = opt.dataset.purpose  || "";

    const nameEl     = document.getElementById("runAgentName");
    const metaEl     = document.getElementById("runAgentMeta");
    const statusEl   = document.getElementById("runAgentStatus");
    const purposeEl  = document.getElementById("runAgentPurpose");
    const platformEl = document.getElementById("runAgentPlatform");
    const chatName   = document.getElementById("chatAgentName");

    if (nameEl)     nameEl.textContent  = name;
    if (metaEl)     metaEl.textContent  = `Owner: ${owner} | Version: v${version} | Status: ${statusD}`;
    if (purposeEl)  purposeEl.textContent = purpose || "Run the agent live inside the platform.";
    if (platformEl) platformEl.textContent = platform;
    if (chatName)   chatName.textContent   = name;

    if (statusEl) {
      statusEl.textContent = statusD;
      statusEl.className   = `status-pill ${status}`;
    }

    // Reset chat log for the new agent
    const chatLog = document.getElementById("chatLog");
    if (chatLog) {
      chatLog.innerHTML = `<div class="message agent">
        <strong>${name}</strong>
        <p>Ready. Type a prompt and click Run.</p>
      </div>`;
    }
  }

  // Apply on change
  picker.addEventListener("change", () => {
    applySelection(picker.options[picker.selectedIndex]);
  });

  // Apply on first load (in case page restores a non-default selection)
  applySelection(picker.options[picker.selectedIndex]);
}());
