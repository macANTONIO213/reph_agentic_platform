"use strict";

// ── Tab navigation ──────────────────────────────────────────────────────────

document.querySelectorAll("[data-view-tab]").forEach(btn => {
  btn.addEventListener("click", () => {
    const target = btn.dataset.viewTab;
    document.querySelectorAll("[data-view-tab]").forEach(b => b.classList.toggle("active", b === btn));
    document.querySelectorAll("[data-view-panel]").forEach(p => p.classList.toggle("active", p.dataset.viewPanel === target));
  });
});

// ── Utilities ───────────────────────────────────────────────────────────────

function getCsrf() {
  return document.cookie.split(";").map(c => c.trim())
    .find(c => c.startsWith("csrftoken="))?.split("=")[1] ?? "";
}

function toast(msg, type = "ok") {
  const el = document.getElementById("toast");
  if (!el) return;
  el.textContent = msg;
  el.className = `toast show toast-${type}`;
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.remove("show"), 3500);
}

function postJSON(url, body) {
  return fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrf() },
    body: JSON.stringify(body),
  });
}

// ── Agent search filter ─────────────────────────────────────────────────────

const agentSearch = document.getElementById("agentSearch");
if (agentSearch) {
  agentSearch.addEventListener("input", () => {
    const q = agentSearch.value.toLowerCase();
    document.querySelectorAll("#agentTable tbody tr").forEach(row => {
      const name = row.dataset.agentName || "";
      row.style.display = name.includes(q) ? "" : "none";
    });
  });
}

// ── Audit log search filter ─────────────────────────────────────────────────

const auditSearch = document.getElementById("auditSearch");
if (auditSearch) {
  auditSearch.addEventListener("input", () => {
    const q = auditSearch.value.toLowerCase();
    document.querySelectorAll("#auditTable tbody tr").forEach(row => {
      const text = row.dataset.auditText || "";
      row.style.display = text.includes(q) ? "" : "none";
    });
  });
}

// ── Governance: approve / reject ────────────────────────────────────────────

document.addEventListener("click", async e => {
  const btn = e.target.closest("[data-action='governance-decide']");
  if (!btn) return;

  const reviewId = btn.dataset.reviewId;
  const decision = btn.dataset.decision;
  const notesInput = document.getElementById(`notes-${reviewId}`);
  const notes = notesInput ? notesInput.value.trim() : "";

  btn.disabled = true;
  const card = document.getElementById(`review-${reviewId}`);
  const sibling = card?.querySelector(`[data-action='governance-decide'][data-decision='${decision === "approved" ? "rejected" : "approved"}']`);
  if (sibling) sibling.disabled = true;

  try {
    const res = await postJSON(`/api/v1/governance/${reviewId}/decide/`, { decision, notes });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || res.statusText);
    toast(`Review ${decision}. Refreshing…`, "ok");
    if (card) {
      card.classList.add("resolved");
      card.innerHTML = `<div style="padding:8px 0;font-size:13px;color:#64748b">
        ✓ Marked as <strong>${decision}</strong> — reload to update counts.
      </div>`;
    }
    // Update pending badge after a short delay
    setTimeout(() => location.reload(), 1800);
  } catch (err) {
    toast(err.message, "err");
    btn.disabled = false;
    if (sibling) sibling.disabled = false;
  }
});

// ── Agent status transition ─────────────────────────────────────────────────

// Build allowed transition map from server-rendered rows
const TRANSITIONS = {
  draft:      ["review", "archived"],
  review:     ["pilot", "draft", "archived"],
  pilot:      ["production", "review", "archived"],
  production: ["archived"],
  archived:   ["draft"],
};

function buildTransitionButtons(row) {
  const agentId   = row.dataset.agentId;
  const agentName = row.querySelector("strong")?.textContent ?? "";
  const statusEl  = row.querySelector(".status-dot");
  if (!statusEl) return;
  const currentStatus = [...statusEl.classList]
    .find(c => c.startsWith("status-") && c !== "status-dot")
    ?.replace("status-", "") ?? "";

  const container = row.querySelector(".row-actions");
  if (!container) return;
  container.innerHTML = "";

  const targets = TRANSITIONS[currentStatus] ?? [];
  targets.forEach(target => {
    const btn = document.createElement("button");
    btn.className = "action-btn action-btn--sm";
    btn.dataset.action = "transition";
    btn.dataset.agentId = agentId;
    btn.dataset.agentName = agentName;
    btn.dataset.status = target;
    btn.textContent = `→ ${target}`;
    container.appendChild(btn);
  });

  if (!targets.length) {
    container.innerHTML = `<span style="font-size:11px;color:#94a3b8">No transitions</span>`;
  }
}

document.querySelectorAll("#agentTable tbody tr").forEach(buildTransitionButtons);

document.addEventListener("click", async e => {
  const btn = e.target.closest("[data-action='transition']");
  if (!btn) return;

  const agentId   = btn.dataset.agentId;
  const newStatus = btn.dataset.status;
  const agentName = btn.dataset.agentName;

  if (!confirm(`Move "${agentName}" → ${newStatus}?`)) return;

  btn.disabled = true;
  try {
    const res = await postJSON(`/api/v1/agents/${agentId}/transition/`, { status: newStatus });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || res.statusText);
    toast(`${agentName} moved to ${data.status}`, "ok");

    // Update status display in table row
    const row = btn.closest("tr");
    const statusEl = row?.querySelector(".status-dot");
    if (statusEl) {
      statusEl.className = `status-dot status-${data.status}`;
      statusEl.textContent = data.status.charAt(0).toUpperCase() + data.status.slice(1);
    }
    // Rebuild transition buttons for new status
    if (row) buildTransitionButtons(row);
  } catch (err) {
    toast(err.message, "err");
    btn.disabled = false;
  }
});

// ── Create approval ─────────────────────────────────────────────────────────

const approvalForm = document.getElementById("approvalForm");
if (approvalForm) {
  approvalForm.addEventListener("submit", async e => {
    e.preventDefault();
    const agentId = document.getElementById("approvalAgent").value;
    const ttl     = parseInt(document.getElementById("approvalTTL").value, 10) || 8;
    const notes   = document.getElementById("approvalNotes").value.trim();
    const result  = document.getElementById("approvalResult");

    if (!agentId) { result.textContent = "Please select an agent."; result.className = "form-result err"; return; }

    result.textContent = "Granting…";
    result.className = "form-result";

    try {
      const res = await postJSON(`/api/v1/agents/${agentId}/approvals/`, { ttl_hours: ttl, notes });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || res.statusText);
      result.textContent = data.message;
      result.className = "form-result ok";
      approvalForm.reset();
      toast("Approval granted.", "ok");
      // Refresh active approvals list
      setTimeout(() => location.reload(), 2000);
    } catch (err) {
      result.textContent = err.message;
      result.className = "form-result err";
      toast(err.message, "err");
    }
  });
}

// ── Audit log: toggle payload ───────────────────────────────────────────────

function togglePayload(btn) {
  const pre = btn.nextElementSibling;
  if (!pre) return;
  const hidden = pre.hidden;
  pre.hidden = !hidden;
  if (hidden) {
    try {
      pre.textContent = JSON.stringify(JSON.parse(btn.dataset.payload), null, 2);
    } catch {
      // leave raw
    }
  }
}

// Expose for inline onclick
window.togglePayload = togglePayload;
