// Warden dashboard. Renders /api/state; re-renders whenever the audit log grows (SSE).
"use strict";

const $ = (id) => document.getElementById(id);
const METAL_EXPLORER = "https://testnet.metalntwx.com/tx/";

const RAILS = {
  transfer: { label: "Bank transfer · Airwallex", icon: '<path d="M3 10h18"/><path d="m12 3 9 5H3z"/><path d="M5 10v8M9.5 10v8M14.5 10v8M19 10v8"/><path d="M3 21h18"/>' },
  card: { label: "Virtual card · Airwallex", icon: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 10h18"/><path d="M7 15h4"/>' },
  vic_checkout: { label: "Checkout · Visa VIC via Airi", icon: '<circle cx="9" cy="20" r="1.4"/><circle cx="18" cy="20" r="1.4"/><path d="M3 4h2l2.4 11h11l2-8H6.3"/>' },
  x402: { label: "Pay-per-call · Metal x402", icon: '<path d="M13 2 4 14h7l-1 8 9-12h-7z"/>' },
};
const BADGES = { ALLOW: "Allowed", STEP_UP: "Needs approval", BLOCK: "Blocked", CONTAIN: "Agent contained" };
const LOCK_ICON = '<svg viewBox="0 0 24 24"><rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/></svg>';

let state = JSON.parse($("initial-state").textContent);
let killArmedUntil = 0;

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function money(amount, currency) {
  try {
    return new Intl.NumberFormat("en-US", { style: "currency", currency }).format(Number(amount));
  } catch {
    return `${amount} ${currency}`;
  }
}

function clock(ts) {
  return ts ? new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "";
}

function short(hash, n = 10) {
  return hash ? `${hash.slice(0, n)}…` : "";
}

function toast(message, ms = 4200) {
  const el = $("toast");
  el.innerHTML = message;
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (el.hidden = true), ms);
}

async function api(path, body) {
  const res = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  return { ok: res.ok, status: res.status, data };
}

async function refresh() {
  const { ok, data } = await api("/api/state");
  if (ok) {
    state = data;
    render();
  }
}

// --- header -------------------------------------------------------------------

function renderRails() {
  const word = { live: "live", simulated: "simulated", contained: "contained", offline: "offline" };
  $("rails").innerHTML = state.rails
    .map((r) => `<span class="chip ${r.status}"><span class="dot"></span>${esc(r.label)}<span class="state">${word[r.status]}</span></span>`)
    .join("");
}

function renderTrust() {
  const score = state.agent.trust;
  const pct = Math.max(0, Math.min(1, score));
  const arc = Math.PI * 40;
  const color = pct >= 0.75 ? "var(--green)" : pct >= 0.4 ? "var(--amber)" : "var(--crimson)";
  $("trust").innerHTML = `
    <svg class="gauge" viewBox="0 0 100 56" aria-hidden="true">
      <path class="track" d="M10 50a40 40 0 0 1 80 0"/>
      <path class="value" d="M10 50a40 40 0 0 1 80 0" style="stroke:${color};stroke-dasharray:${arc * pct} ${arc}"/>
    </svg>
    <div class="trust-text"><strong>${Math.round(pct * 100)}</strong><span>${esc(state.agent.id)} trust</span></div>`;
}

function renderBanner() {
  const banner = $("banner");
  banner.hidden = !state.agent.contained;
  banner.textContent = state.agent.contained
    ? "Agent shut down on every rail: card frozen, Metal mandate revoked, Airi checkouts cancelled. Everything it proposes is refused."
    : "";
  const pill = $("mode-pill");
  pill.textContent = state.mode === "live" ? "Live · Airwallex sandbox, Metal testnet, Claude" : "Offline demo · simulated rails";
  pill.className = `mode-pill ${state.mode === "live" ? "live" : ""}`;
  $("btn-reset").hidden = state.mode === "live";
}

// --- feed -----------------------------------------------------------------------

function codes(decision) {
  if (!decision) return "";
  const flags = (decision.risk?.flags || []).filter((f) => !decision.violations.includes(f));
  const items = decision.violations.map((v) => `<span class="code violation">${esc(v)}</span>`)
    .concat(flags.map((f) => `<span class="code">${esc(f)}</span>`));
  return items.length ? `<div class="codes">${items.join("")}</div>` : "";
}

function explanation(decision) {
  const risk = decision?.risk;
  if (!risk || !risk.explanation || risk.source === "skipped") return "";
  const who = risk.source === "claude" ? "Claude" : "Risk reader";
  return `<p class="explain"><span class="who">${who}</span>${esc(risk.explanation)}</p>`;
}

function link(key, value) {
  const v = String(value);
  if (/^https?:\/\//.test(v)) return `<a href="${esc(v)}" target="_blank" rel="noopener">${esc(key)} ↗</a>`;
  if (/^0x[0-9a-fA-F]{64}$/.test(v) && /tx|transaction|hash/i.test(key)) {
    return `<a href="${METAL_EXPLORER}${esc(v)}" target="_blank" rel="noopener">${esc(key)} ${esc(short(v, 10))} ↗</a>`;
  }
  return `<span>${esc(key)} <span class="mono">${esc(v.length > 40 ? short(v, 18) : v)}</span></span>`;
}

function execution(decision) {
  const ex = decision?.execution;
  if (!ex) return "";
  const parts = [];
  if (ex.status === "DECLINED_BY_RAIL") {
    parts.push(`<span class="bad">Second lock held: ${esc(ex.rail)} declined it (${esc(ex.reason)})</span>`);
  } else if (ex.status === "RAIL_ERROR" || ex.status === "NO_RAIL") {
    parts.push(`<span class="bad">${esc(ex.status)}: ${esc(ex.error)}</span>`);
  } else {
    parts.push(`<span class="ok">Executed${ex.status ? ` · ${esc(ex.status)}` : ""}</span>`);
  }
  for (const [k, v] of Object.entries(ex)) {
    if (["rail", "status", "reason", "error"].includes(k) || v === null || typeof v === "object") continue;
    parts.push(link(k, v));
  }
  return `<div class="result">${parts.join("")}</div>`;
}

function containment(decision) {
  const c = decision?.containment;
  if (!c) return "";
  const parts = Object.entries(c).map(([rail, r]) => `<span class="bad">${esc(rail)}: ${esc(r?.status ?? "done")}</span>`);
  return `<div class="result"><span class="held">Kill switch:</span>${parts.join("")}</div>`;
}

function card(item) {
  const p = item.proposal;
  const d = item.decision;
  const rail = RAILS[p.rail] || { label: p.rail, icon: '<circle cx="12" cy="12" r="8"/>' };
  const action = item.pending ? "STEP_UP" : d ? d.action : "PENDING";
  const delta = d && d.trust_after !== d.trust_before
    ? `<span class="trust-delta ${d.trust_after < d.trust_before ? "down" : ""}">trust ${Math.round(d.trust_before * 100)} → ${Math.round(d.trust_after * 100)}</span>`
    : "";
  const held = item.pending ? `<div class="result"><span class="held">Waiting for a human in the approval inbox</span></div>` : "";
  const refused = item.rejected_approvals.length
    ? `<div class="result"><span class="bad">Changed-proposal approval refused (${esc(item.rejected_approvals.join(", "))})</span></div>`
    : "";
  const receipt = d && !item.pending ? `<a class="small" href="/api/receipt/${encodeURIComponent(p.id)}">Trust receipt ↓</a>` : "";
  return `
    <article class="card ${action}">
      <div class="rail-icon"><svg viewBox="0 0 24 24">${rail.icon}</svg></div>
      <div class="card-main">
        <div class="card-title">
          <span class="amount">${esc(money(p.amount, p.currency))}</span>
          <span class="payee">${esc(p.payee_name)}</span>
          <span class="rail-name">${esc(rail.label)}</span>
        </div>
        <p class="desc">${esc(p.description)}</p>
        ${codes(d)}${explanation(d)}${held}${execution(d)}${containment(d)}${refused}
      </div>
      <div class="card-side">
        <span class="badge ${action}">${BADGES[action] || "Pending"}</span>
        <span class="time">${clock(d?.ts)}</span>
        ${delta}${receipt}
      </div>
    </article>`;
}

function renderFeed() {
  $("feed").innerHTML = state.feed.length
    ? state.feed.map(card).join("")
    : `<div class="empty">No payments yet. Press <b>Run a normal day</b>, then try to fool the agent.</div>`;
}

// --- inbox ----------------------------------------------------------------------

function inboxItem(item) {
  const p = item.proposal;
  const evidence = (p.evidence || []).map((e) => `
    <div class="ev">
      <div class="src"><span class="tag ${e.trusted ? "trusted" : "untrusted"}">${e.trusted ? "TRUSTED" : "UNTRUSTED"}</span>${esc(e.source)}</div>
      ${esc(e.text)}
    </div>`).join("");
  const reason = item.risk?.explanation ? `<p class="explain"><span class="who">${item.risk.source === "claude" ? "Claude" : "Risk reader"}</span>${esc(item.risk.explanation)}</p>` : "";
  return `
    <div class="inbox-item" data-id="${esc(p.id)}">
      <div class="card-title">
        <span class="amount">${esc(money(p.amount, p.currency))}</span>
        <span class="payee">${esc(p.payee_name)}</span>
      </div>
      <p class="desc">${esc(p.description)} · ${esc((RAILS[p.rail] || {}).label || p.rail)}</p>
      <div class="evidence">${evidence}</div>
      ${reason}
      <div class="lock">${LOCK_ICON}<div>What you approve is locked: this exact amount, payee and evidence.
        Any change voids it.<br>Approval hash <span class="mono" title="${esc(item.approval_hash)}">${esc(short(item.approval_hash, 24))}</span></div></div>
      <div class="inbox-actions">
        <button class="btn btn-approve" data-act="approve" data-hash="${esc(item.approval_hash)}">Approve</button>
        <button class="btn btn-deny" data-act="deny">Deny</button>
        <button class="btn btn-ghost btn-sm" data-act="tamper">Agent changes amount +$1</button>
      </div>
      <div class="tamper-result" hidden></div>
    </div>`;
}

function renderInbox() {
  const n = state.inbox.length;
  $("inbox-count").textContent = n;
  $("inbox-count").className = `count ${n ? "has" : ""}`;
  $("inbox").innerHTML = n ? state.inbox.map(inboxItem).join("") : `<p class="muted">Nothing waiting. Warden asks you only when a payment needs judgment.</p>`;
}

$("inbox").addEventListener("click", async (event) => {
  const btn = event.target.closest("button[data-act]");
  if (!btn) return;
  const item = btn.closest(".inbox-item");
  const id = item.dataset.id;
  btn.disabled = true;
  if (btn.dataset.act === "approve") {
    const { ok, data } = await api("/api/approve", { proposal_id: id, approval_hash: btn.dataset.hash });
    toast(ok ? `Approved · ${esc(data.action)}` : `Not executed: ${esc(data.detail)}`);
  } else if (btn.dataset.act === "deny") {
    const { data } = await api("/api/deny", { proposal_id: id });
    toast(data.action === "CONTAIN" ? "Denied. Second strike: agent shut down on every rail." : "Denied. Strike recorded against the agent.");
  } else {
    const { data } = await api("/api/tamper", { proposal_id: id });
    const out = item.querySelector(".tamper-result");
    out.hidden = false;
    out.textContent = data.refused
      ? `Refused: the agent tried to run it as ${money(data.tried_amount, "USD")}, but the approval hash no longer matches.`
      : data.detail || "Unexpected result";
    btn.disabled = false;
    return;
  }
  refresh();
});

// --- controls ------------------------------------------------------------------

$("btn-attack").addEventListener("click", async (event) => {
  const btn = event.currentTarget;
  btn.disabled = true;
  const { ok, data } = await api("/api/attack", {});
  btn.disabled = false;
  if (ok) toast(`<b>${esc(data.scenario)}</b><br>Warden: ${esc(BADGES[data.action] || data.action)}`);
  refresh();
});

$("btn-normal").addEventListener("click", async (event) => {
  const btn = event.currentTarget;
  btn.disabled = true;
  await api("/api/normal-day", {});
  btn.disabled = false;
  refresh();
});

$("btn-kill").addEventListener("click", async (event) => {
  const btn = event.currentTarget;
  if (state.agent.contained) return;
  if (Date.now() > killArmedUntil) {
    killArmedUntil = Date.now() + 4000;
    btn.classList.add("armed");
    btn.querySelector("span").textContent = "Click again to shut down on every rail";
    setTimeout(() => { if (Date.now() > killArmedUntil) renderKill(); }, 4100);
    return;
  }
  killArmedUntil = 0;
  btn.disabled = true;
  await api("/api/contain", { reason: "Kill switch pressed on the dashboard" });
  toast("Agent shut down: card frozen, mandate revoked, checkouts cancelled.");
  refresh();
});

function renderKill() {
  const btn = $("btn-kill");
  btn.classList.remove("armed");
  btn.classList.toggle("done", state.agent.contained);
  btn.disabled = state.agent.contained;
  btn.querySelector("span").textContent = state.agent.contained ? "Agent is shut down" : "Shut down agent";
}

$("btn-reset").addEventListener("click", async () => {
  await api("/api/reset", {});
  $("verify-result").textContent = "";
  refresh();
});

// --- audit and scorecard -----------------------------------------------------------

function renderAudit() {
  const rows = state.audit.recent.map((e) => `
    <tr>
      <td class="mono">#${e.seq}</td>
      <td>${clock(e.ts)}</td>
      <td class="ev-name">${esc(e.event.replaceAll("_", " "))}</td>
      <td class="mono chain">${esc(short(e.prev_hash, 12))} → ${esc(short(e.hash, 12))}</td>
    </tr>`).join("");
  $("audit").innerHTML = `<thead><tr><th>Entry</th><th>Time</th><th>Event</th><th>Chain (prev → this)</th></tr></thead><tbody>${rows ||
    '<tr><td colspan="4" class="muted">Empty</td></tr>'}</tbody>`;
}

$("btn-verify").addEventListener("click", async () => {
  const { data } = await api("/api/verify");
  const out = $("verify-result");
  out.className = `verify ${data.intact ? "ok" : "bad"}`;
  out.textContent = data.intact ? `✓ Chain intact · ${data.entries} entries` : `✗ Tampered at entry #${data.broken_at}`;
});

async function loadScorecard() {
  const { ok, data } = await api("/api/scorecard");
  if (!ok) return;
  const rows = data.rows.map((r) => {
    const good = r.attack ? r.action !== "ALLOW" : r.action === "ALLOW";
    return `<li><span class="mark ${good ? "ok" : "bad"}">${good ? "✓" : "✗"}</span><span>${esc(r.name)}</span><span class="act">${esc(r.action)}</span></li>`;
  }).join("");
  $("scorecard").innerHTML = `<div class="score-big"><span class="${data.ok ? "ok" : ""}">${esc(data.summary)}</span></div><ul class="score-rows">${rows}</ul>`;
}

function render() {
  renderRails();
  renderTrust();
  renderBanner();
  renderFeed();
  renderInbox();
  renderKill();
  renderAudit();
}

render();
loadScorecard();

const events = new EventSource("/api/stream");
events.addEventListener("audit", refresh);
