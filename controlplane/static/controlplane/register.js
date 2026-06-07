"use strict";

// ── Modal open / close ───────────────────────────────────────────────────────

const modal   = document.getElementById("registerModal");
const openBtn = document.getElementById("openRegisterModal");
const closeBtn = document.getElementById("closeRegisterModal");
const cancelBtn = document.getElementById("cancelRegisterModal");

function openModal() {
  if (!modal) return;
  modal.hidden = false;
  document.body.style.overflow = "hidden";
  modal.querySelector("input[name='name']")?.focus();
}

function closeModal() {
  if (!modal) return;
  modal.hidden = true;
  document.body.style.overflow = "";
  document.getElementById("regFormError")?.setAttribute("hidden", "");
}

openBtn?.addEventListener("click", openModal);
closeBtn?.addEventListener("click", closeModal);
cancelBtn?.addEventListener("click", closeModal);

// Close on backdrop click
modal?.addEventListener("click", e => {
  if (e.target === modal) closeModal();
});

// Close on Escape
document.addEventListener("keydown", e => {
  if (e.key === "Escape" && modal && !modal.hidden) closeModal();
});

// ── Cascading org selects ────────────────────────────────────────────────────

async function loadOptions(url, selectEl) {
  selectEl.innerHTML = '<option value="">None</option>';
  try {
    const res = await fetch(url);
    const data = await res.json();
    (data.items || []).forEach(item => {
      const opt = document.createElement("option");
      opt.value = item.id;
      opt.textContent = item.name;
      selectEl.appendChild(opt);
    });
  } catch { /* leave as None */ }
}

const regBU         = document.getElementById("regBU");
const regDivision   = document.getElementById("regDivision");
const regWorkStream = document.getElementById("regWorkStream");
const regProcess    = document.getElementById("regProcess");

regBU?.addEventListener("change", async () => {
  const buId = regBU.value;
  await loadOptions(`/api/v1/org/divisions/?business_unit=${buId}`, regDivision);
  regWorkStream.innerHTML = '<option value="">None</option>';
  regProcess.innerHTML    = '<option value="">None</option>';
});

regDivision?.addEventListener("change", async () => {
  const divId = regDivision.value;
  await loadOptions(`/api/v1/org/work-streams/?division=${divId}`, regWorkStream);
  regProcess.innerHTML = '<option value="">None</option>';
});

regWorkStream?.addEventListener("change", async () => {
  const wsId = regWorkStream.value;
  await loadOptions(`/api/v1/org/processes/?work_stream=${wsId}`, regProcess);
});

// ── Form submission ──────────────────────────────────────────────────────────

function getCsrf() {
  return document.cookie.split(";").map(c => c.trim())
    .find(c => c.startsWith("csrftoken="))?.split("=")[1] ?? "";
}

function showToast(msg, type = "ok") {
  const el = document.getElementById("toast");
  if (!el) return;
  el.textContent = msg;
  el.className = `toast show toast-${type}`;
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.remove("show"), 4000);
}

const form      = document.getElementById("registerAgentForm");
const submitBtn = document.getElementById("regSubmitBtn");
const errBox    = document.getElementById("regFormError");

form?.addEventListener("submit", async e => {
  e.preventDefault();
  errBox.hidden = true;
  submitBtn.disabled = true;
  submitBtn.textContent = "Registering…";

  const fd = new FormData(form);
  const body = {};
  fd.forEach((val, key) => {
    if (key !== "csrfmiddlewaretoken") body[key] = val;
  });

  // Basic client-side required check
  const missing = ["name", "platform", "owner", "purpose"].filter(f => !body[f]?.trim());
  if (missing.length) {
    errBox.textContent = `Please fill in: ${missing.join(", ")}.`;
    errBox.hidden = false;
    submitBtn.disabled = false;
    submitBtn.textContent = "Register agent";
    return;
  }

  try {
    const res = await fetch("/api/v1/agents/register/", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": getCsrf(),
      },
      body: JSON.stringify(body),
    });
    const data = await res.json();

    if (!res.ok) {
      errBox.textContent = data.error || "Registration failed.";
      errBox.hidden = false;
      return;
    }

    closeModal();
    form.reset();
    regDivision.innerHTML   = '<option value="">None</option>';
    regWorkStream.innerHTML = '<option value="">None</option>';
    regProcess.innerHTML    = '<option value="">None</option>';

    showToast(`"${data.name}" registered as draft. Open the Agent Catalog to continue.`, "ok");

    // Add a draft card to the catalog list without a full reload.
    _injectDraftCard(data);

  } catch (err) {
    errBox.textContent = `Network error: ${err.message}`;
    errBox.hidden = false;
  } finally {
    submitBtn.disabled = false;
    submitBtn.textContent = "Register agent";
  }
});

// ── Inject a lightweight draft card into the catalog on success ──────────────

function _injectDraftCard(agent) {
  const grid = document.querySelector(".catalog-content .agent-grid");
  if (!grid) return;

  const card = document.createElement("article");
  card.className = "agent-card";
  card.dataset.status = "draft";
  card.dataset.name = (agent.name || "").toLowerCase();
  card.innerHTML = `
    <header class="card-header">
      <strong class="card-name">${_esc(agent.name)}</strong>
      <span class="status-badge draft">Draft</span>
    </header>
    <p class="card-purpose">Newly registered — governance review pending.</p>
    <footer class="card-footer">
      <span class="card-tier">Tier ${agent.risk_tier}</span>
    </footer>`;
  grid.prepend(card);
}

function _esc(str) {
  return String(str).replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}
