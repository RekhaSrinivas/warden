// Warden dashboard. Renders /api/state as one story: hire, guard, prove, recover, locks.
// Re-renders whenever the audit log grows (server-sent events).
"use strict";

const $ = (id) => document.getElementById(id);
const METAL_EXPLORER = "https://testnet.metalntwx.com/tx/";

const RAILS = {
  transfer: { label: "Bank transfer · Airwallex", icon: '<path d="M3 10h18"/><path d="m12 3 9 5H3z"/><path d="M5 10v8M9.5 10v8M14.5 10v8M19 10v8"/><path d="M3 21h18"/>' },
  card: { label: "Virtual card · Airwallex", icon: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 10h18"/><path d="M7 15h4"/>' },
  vic_checkout: { label: "Checkout · Visa VIC via Airi", icon: '<circle cx="9" cy="20" r="1.4"/><circle cx="18" cy="20" r="1.4"/><path d="M3 4h2l2.4 11h11l2-8H6.3"/>' },
  x402: { label: "Pay-per-call · Alta x402", icon: '<path d="M13 2 4 14h7l-1 8 9-12h-7z"/>' },
};
const BADGES = { ALLOW: "Allowed", STEP_UP: "Needs approval", BLOCK: "Blocked", CONTAIN: "Agent contained" };
const CASE_WORDS = {
  UNATTRIBUTED: "Unattributed", INVESTIGATING: "Investigating", DISPUTE_SUBMITTED: "Dispute submitted",
  RECOVERED: "Recovered", LOST: "Lost", NOT_ELIGIBLE: "Not eligible",
};
const LOCK_ICON = '<svg viewBox="0 0 24 24"><rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/></svg>';

let state = JSON.parse($("initial-state").textContent);
const fireArmed = {}; // agent id -> time until which the second click fires
const tamperNotes = {}; // proposal id -> what happened when the agent tried to change it

// --- helpers ------------------------------------------------------------------

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function money(amount, currency = "USD") {
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
  return hash ? `${String(hash).slice(0, n)}…` : "";
}

function toast(message, ms = 4500) {
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

async function busy(btn, label, work) {
  const old = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = label;
  try {
    await work();
  } finally {
    btn.disabled = false;
    btn.innerHTML = old;
  }
}

function gauge(score) {
  const pct = Math.max(0, Math.min(1, score));
  const arc = Math.PI * 40;
  const color = pct >= 0.75 ? "var(--green)" : pct >= 0.4 ? "var(--amber)" : "var(--crimson)";
  return `<svg class="gauge" viewBox="0 0 100 56" aria-hidden="true">
      <path class="track" d="M10 50a40 40 0 0 1 80 0"/>
      <path class="value" d="M10 50a40 40 0 0 1 80 0" style="stroke:${color};stroke-dasharray:${arc * pct} ${arc}"/>
    </svg>`;
}

// --- header -------------------------------------------------------------------

function renderHeader() {
  const word = { live: "live", simulated: "simulated", offline: "offline" };
  $("rails").innerHTML = state.rails
    .map((r) => `<span class="chip ${esc(r.status.split(" ")[0])}" title="${esc(r.label)}: ${esc(word[r.status] || r.status)}"><span class="dot"></span><span class="chip-label">${esc(r.label)}</span><span class="state">${esc(word[r.status] || r.status)}</span></span>`)
    .join("");
  const pill = $("mode-pill");
  pill.textContent = state.mode === "live" ? "Live sandbox" : "Offline demo";
  pill.title = state.mode === "live" ? "Live: Airwallex sandbox, Visa VIC via Airi, Alta testnet, Claude" : "Offline demo: every rail simulated";
  pill.className = `mode-pill ${state.mode === "live" ? "live" : ""}`;
  // the sidebar's workspace card and counts
  const agents = state.agents.length;
  $("ws-agents").textContent = `${agents} agent${agents === 1 ? "" : "s"}`;
  $("ws-mode-text").textContent = pill.textContent;
  $("ws-mode").className = `ws-mode ${state.mode === "live" ? "live" : ""}`;
  $("ws-mode").title = pill.title;
  $("sb-agents").textContent = agents || "";
  $("sb-decisions").textContent = state.feed.filter((i) => i.pending || i.decision).length || "";
  $("sb-inbox").textContent = state.inbox.length || "";
  $("btn-reset").hidden = state.mode === "live";
  $("btn-locks").hidden = state.mode === "live";
  const m = state.protection;
  if (m) {
    const tile = (cls, label, v, note) => `<div class="tile ${cls}"><span class="label">${label}</span>
      <span class="value">${esc(money(v.amount ?? v))}</span><span class="note">${note}</span></div>`;
    const n = (count, one, many) => `${count} ${count === 1 ? one : many}`;
    $("protect").innerHTML = [
      tile("bad", "Stopped", m.stopped, `${n(m.stopped.count, "payment", "payments")} blocked or agent contained`),
      tile("bad", "Double payments prevented", m.double_payments, `${n(m.double_payments.count, "retry", "retries")} absorbed`),
      tile("bad", "Declined in real time", m.declined_live, `${n(m.declined_live.count, "card charge", "card charges")} without a receipt`),
      tile("warn", "Held for a human", m.held, `${m.held.count} waiting in the inbox`),
      tile("warn", "Under dispute", m.under_dispute, `${n(m.under_dispute.count, "charge", "charges")} no human authorised`),
      tile("ok", "Recovered", m.recovered, "counted only when Airwallex reports WON"),
      tile("ok", "Paid within mandate", m.paid, n(m.paid.count, "payment", "payments")),
    ].join("");
  }
  const pk = state.passkey;
  $("passkey-banner").hidden = !pk;
  $("passkey-banner").innerHTML = pk
    ? `${LOCK_ICON}<div><b>Airi needs your passkey.</b> Warden approved ${esc(pk.agent_id)}'s order:
       ${esc(money(pk.amount, pk.currency))} at ${esc(pk.merchant)} (${esc(pk.description)}). Nothing is charged until you confirm.</div>
       <a class="btn btn-approve" href="${esc(pk.approval_url)}" target="_blank" rel="noopener">Confirm with passkey ↗</a>`
    : "";
}

// --- 1. hire ------------------------------------------------------------------

function agentCard(a) {
  const card = a.card
    ? `<div class="rail-line"><span class="rl-icon"><svg viewBox="0 0 24 24">${RAILS.card.icon}</svg></span>
        <span>Airwallex card <span class="mono">${esc(a.card.id)}</span>${a.card.simulated ? ' <span class="muted">(simulated)</span>' : ""}</span>
        <span class="muted rl-limits">${a.contained ? "frozen" : `${money(a.per_txn_cap)}/purchase · ${money(a.weekly_cap)}/week · MCC ${esc(a.mccs.join(", ") || "none")}`}</span></div>`
    : "";
  const metal = a.metal
    ? `<div class="rail-line"><span class="rl-icon"><svg viewBox="0 0 24 24">${RAILS.x402.icon}</svg></span>
        <span>Alta mandate ${a.metal.explorer ? `<a href="${esc(a.metal.explorer)}" target="_blank" rel="noopener" class="mono">${esc(a.metal.mandate_id)} ↗</a>` : `<span class="mono">${esc(a.metal.mandate_id)}</span>`}</span>
        <span class="muted">${a.contained ? "revoked on-chain" : a.metal.cap ? `${esc(a.metal.cap)} USDZ/call · ${esc(a.metal.budget)} USDZ budget` : "bound"}</span></div>`
    : "";
  const armed = (fireArmed[a.id] || 0) > Date.now();
  const fire = a.contained
    ? `<button class="btn btn-fired" disabled>Fired · contained on every rail</button>`
    : `<button class="btn btn-fire ${armed ? "armed" : ""}" data-fire="${esc(a.id)}">${armed ? "Click again: freeze card and revoke mandate" : "Fire agent"}</button>`;
  return `
    <article class="agent ${a.contained ? "contained" : ""}">
      <div class="agent-top">
        <div>
          <h3>${esc(a.id)} <span class="status ${a.contained ? "bad" : "ok"}">${a.contained ? "contained" : "active"}</span></h3>
          <p class="muted small">${esc(a.purpose || a.role)}</p>
        </div>
        <div class="trust">${gauge(a.trust)}<div class="trust-text"><strong>${Math.round(a.trust * 100)}</strong><span>trust · ${a.strikes} strike${a.strikes === 1 ? "" : "s"}</span></div></div>
      </div>
      <div class="budget">
        <div class="budget-label"><span>Spent this week</span><span>${money(a.spent_week)} of ${money(a.weekly_cap)}</span></div>
        <div class="bar"><span style="width:${a.budget_pct}%"></span></div>
      </div>
      <div class="rails-list">${card}${metal}</div>
      <p class="mandate-line"><span class="muted">mandate</span> <span class="mono">${esc(a.mandate_id)}</span>
        <span class="muted">hash</span> <span class="mono">${esc(a.mandate_hash || "")}</span>
        ${a.human_required.length ? `<span class="muted">human needed for</span> ${esc(a.human_required.join(", "))}` : ""}</p>
      ${fire}
    </article>`;
}

function renderAgents() {
  $("agents").innerHTML = state.agents.length ? state.agents.map(agentCard).join("") : `<div class="empty">No agents hired yet.</div>`;
  const options = state.agents.map((a) => `<option value="${esc(a.id)}">${esc(a.id)}${a.contained ? " (contained)" : ""}</option>`).join("");
  const keep = $("attack-agent").value;
  $("attack-agent").innerHTML = options;
  const active = state.agents.find((a) => !a.contained);
  if (keep && state.agents.some((a) => a.id === keep)) $("attack-agent").value = keep;
  else if (active) $("attack-agent").value = active.id;  // never offer a fired agent by default
  const filterKeep = $("feed-filter").value || "all";
  $("feed-filter").innerHTML = `<option value="all">all agents</option>${state.agents.map((a) => `<option value="${esc(a.id)}">${esc(a.id)}</option>`).join("")}`;
  $("feed-filter").value = [...$("feed-filter").options].some((o) => o.value === filterKeep) ? filterKeep : "all";
}

$("agents").addEventListener("click", async (event) => {
  const btn = event.target.closest("button[data-fire]");
  if (!btn) return;
  const id = btn.dataset.fire;
  if ((fireArmed[id] || 0) < Date.now()) {
    fireArmed[id] = Date.now() + 4000;
    renderAgents();
    setTimeout(renderAgents, 4100);
    return;
  }
  fireArmed[id] = 0;
  await busy(btn, "Freezing card, revoking mandate…", async () => {
    const { ok, data } = await api("/api/fire", { agent_id: id, reason: "Fired from the dashboard" });
    toast(ok ? `<b>${esc(id)} fired.</b><br>${Object.entries(data.results || {}).map(([r, v]) => `${esc(r)}: ${esc(v?.status || v?.card_status || "done")}`).join("<br>")}`
      : `Could not fire: ${esc(data.detail)}`);
  });
  refresh();
});

// What confirming issues: a card when the mandate allows card categories, an Alta mandate when it may pay
// the on-chain data API (an Alta address on its payee list).
function provisions(m) {
  const parts = [];
  if (m.allowed_mccs.length) parts.push("a real card");
  if (m.payee_allowlist.some((p) => /^0x[0-9a-f]{40}$/i.test(p))) parts.push("an Alta mandate");
  return parts.join(" and ") || "the mandate";
}

function draftView(d) {
  const m = d.mandate;
  const changes = d.changes.length
    ? d.changes.map((c) => `<li class="change">${esc(c)}</li>`).join("")
    : `<li class="ok">Within company policy: nothing changed.</li>`;
  const errors = d.errors.map((e) => `<li class="bad">${esc(e)}</li>`).join("");
  return `
    <div class="draft">
      <div class="draft-cols">
        <div><h4>Claude drafted <span class="muted small">(${esc(d.draft.source)})</span></h4>
          <p>${money(d.draft.per_txn_cap)}/purchase · ${money(d.draft.weekly_cap)}/week</p>
          <p class="muted small">categories: ${esc((d.draft.categories || []).join(", ") || "none")}<br>payees: ${esc((d.draft.payees || []).join(", ") || "none")}</p></div>
        <div><h4>Code validated</h4>
          <p>${money(m.per_txn_cap)}/purchase · ${money(m.weekly_cap)}/week</p>
          <p class="muted small">MCC ${esc(m.allowed_mccs.join(", ") || "none")} · ${m.payee_allowlist.length} payees · ${esc(m.currencies.join(", "))}</p></div>
      </div>
      <ul class="changes">${changes}${errors}</ul>
      ${d.errors.length
        ? `<p class="refused"><b>Not provisioned.</b> Code refused this mandate: ${esc(d.errors.join(" "))}
             Warden only issues a card it can actually lock down. Rephrase the job and draft again.</p>
           <button class="btn btn-wide" disabled>Can't provision this mandate</button>`
        : `<button class="btn btn-approve btn-wide" id="btn-confirm" data-agent="${esc(d.agent_id)}" data-mandate="${esc(m.mandate_id)}"
             data-what="${esc(provisions(m))}">
             Confirm and provision ${state.mode === "live" ? esc(provisions(m)) : "(simulated)"}
           </button>`}
    </div>`;
}

$("btn-draft").addEventListener("click", async (event) => {
  const body = { agent_id: $("hire-id").value, role: $("hire-role").value, instruction: $("hire-text").value };
  await busy(event.currentTarget, "Claude is drafting…", async () => {
    const { ok, data } = await api("/api/hire/draft", body);
    $("hire-draft").innerHTML = ok ? draftView(data) : `<p class="bad">${esc(data.detail)}</p>`;
  });
});

// A draft is only good for the exact words it was drafted from: edit anything and it is withdrawn.
for (const id of ["hire-id", "hire-role", "hire-text"]) {
  $(id).addEventListener("input", () => {
    if (!$("btn-confirm") && !$("hire-draft").querySelector(".refused")) return;
    $("hire-draft").innerHTML = `<p class="muted small stale">You changed the job, so the old draft was withdrawn. Press <b>Draft mandate</b> again.</p>`;
  });
}

$("hire-draft").addEventListener("click", async (event) => {
  const btn = event.target.closest("#btn-confirm");
  if (!btn) return;
  if (state.mode === "live" && !confirm(`Provision ${btn.dataset.what} for ${btn.dataset.agent} on the Airwallex sandbox / Alta testnet?`)) return;
  await busy(btn, "Provisioning…", async () => {
    const { ok, data } = await api("/api/hire/confirm", { agent_id: btn.dataset.agent, mandate_id: btn.dataset.mandate });
    $("hire-draft").innerHTML = ok ? `<p class="ok">Hired ${esc(data.agent_id)}.</p>` : `<p class="bad">${esc(data.detail)}</p>`;
  });
  refresh();
});

// --- 2. guard -----------------------------------------------------------------

function codes(decision) {
  if (!decision) return "";
  const flags = (decision.risk?.flags || []).filter((f) => !decision.violations.includes(f));
  const items = decision.violations.map((v) => `<span class="code violation">${esc(v)}</span>`)
    .concat(flags.map((f) => `<span class="code">${esc(f)}</span>`));
  return items.length ? `<div class="codes">${items.join("")}</div>` : "";
}

// Plain-English reasons, citing the rule a human set (the mandate) or what the evidence shows.
function why(decision, p) {
  if (!decision || decision.action === "ALLOW") return "";
  const a = (state.agents || []).find((x) => x.id === p.agent_id) || {};
  const who = esc(p.agent_id);
  const RULES = {
    OVER_TXN_CAP: () => `Asks for ${esc(money(p.amount, p.currency))}; the mandate a human set for ${who} allows at most ${a.per_txn_cap != null ? esc(money(a.per_txn_cap)) : "a smaller amount"} per payment.`,
    OVER_WEEKLY_CAP: () => `Would take ${who} past ${a.weekly_cap != null ? `the ${esc(money(a.weekly_cap))}` : "the"} weekly budget in its mandate.`,
    PAYEE_NOT_ALLOWLISTED: () => `${esc(p.payee_name)} isn't on the approved payee list a human set.`,
    MERCHANT_CATEGORY_NOT_ALLOWED: () => `This merchant's category${p.merchant_category ? ` (${esc(p.merchant_category)})` : ""} isn't one ${who}'s mandate allows${a.mccs?.length ? ` (${esc(a.mccs.join(", "))})` : ""}.`,
    CURRENCY_NOT_ALLOWED: () => `Paid in ${esc(p.currency)}; the mandate allows only ${esc((a.currencies || ["USD"]).join(", "))}.`,
    DUPLICATE_REQUEST: () => "The same request as a payment already made: Warden never pays twice.",
    RECORD_MISMATCH: () => "It doesn't match the approved record in the company's books (amount, payee or bank account).",
    AGENT_CONTAINED: () => `${who} has been fired: it can't pay anything until a human reinstates it.`,
    RESERVE_FLOOR: () => "It would take the account below the reserve the company keeps.",
    VELOCITY: () => `Too many payments from ${who} in one hour.`,
    NON_POSITIVE_AMOUNT: () => "The amount isn't a positive number.",
    APPROVAL_MISMATCH: () => "The payment was changed after a human approved it.",
    LOOKALIKE_DOMAIN: () => "The sender's domain imitates an approved vendor's.",
    BANK_DETAILS_CHANGED: () => "It asks to pay new bank details: the classic invoice scam.",
    URGENCY_PRESSURE: () => "It pushes for speed (\"today\", \"urgent\").",
    SECRECY_REQUEST: () => "It asks to keep the payment quiet.",
    GIFT_CARD: () => "Gift cards are a common way to cash out stolen money.",
    PROMPT_INJECTION: () => "The document contains instructions aimed at the AI, not at a person.",
    UNTRUSTED_INSTRUCTION: () => "The instruction to pay came from a message, not from an approved record.",
    UNKNOWN_SENDER: () => "The sender isn't a known vendor.",
    AMOUNT_MISMATCH: () => "The amount differs from the evidence behind it.",
    RISK_READER_UNAVAILABLE: () => "The evidence couldn't be read, so a human must look.",
  };
  const codes = [...(decision.violations || []), ...(decision.risk?.flags || []).filter((f) => !(decision.violations || []).includes(f))];
  const lines = [...new Set(codes)].filter((c) => RULES[c]).slice(0, 4).map((c) => `<li>${RULES[c]()}</li>`);
  if (!lines.length) return "";
  const label = decision.action === "STEP_UP" ? "Why it's held" : decision.action === "CONTAIN" ? "Why the agent was fired" : "Why it's blocked";
  return `<div class="why"><span class="why-h">${label}</span><ul>${lines.join("")}</ul></div>`;
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

const SHOWN_IDS = ["transfer_id", "card_transaction_id", "transaction", "explorer", "mandate_id"];

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
  for (const k of SHOWN_IDS) if (ex[k]) parts.push(link(k, ex[k]));
  return `<div class="result">${parts.join("")}</div>`;
}

function containment(decision) {
  const c = decision?.containment;
  if (!c) return "";
  const parts = Object.entries(c).map(([rail, r]) => `<span class="bad">${esc(rail === "metal" ? "alta" : rail)}: ${esc(r?.status ?? r?.card_status ?? "done")}</span>`);
  return `<div class="result"><span class="held">Kill switch:</span>${parts.join("")}</div>`;
}

function feedCard(item) {
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
  return `
    <article class="card ${action}">
      <div class="rail-icon"><svg viewBox="0 0 24 24">${rail.icon}</svg></div>
      <div class="card-main">
        <div class="card-title">
          <span class="amount">${esc(money(p.amount, p.currency))}</span>
          <span class="payee">${esc(p.payee_name)}</span>
          <span class="rail-name">${esc(rail.label)}</span>
        </div>
        <p class="desc"><span class="agent-tag">${esc(p.agent_id)}</span>${p.via === "mcp" ? '<span class="via-tag" title="Requested by a real agent connected over MCP">via MCP</span>' : ""} ${esc(p.description)}</p>
        ${why(d, p)}${codes(d)}${explanation(d)}${held}${execution(d)}${containment(d)}${refused}
      </div>
      <div class="card-side">
        <span class="badge ${action}">${BADGES[action] || "Pending"}</span>
        <span class="time">${clock(d?.ts)}</span>
        ${delta}
      </div>
    </article>`;
}

function renderFeed() {
  const filter = $("feed-filter").value || "all";
  const items = state.feed.filter((i) => filter === "all" || i.proposal.agent_id === filter);
  $("feed").innerHTML = items.length
    ? items.slice(0, 60).map(feedCard).join("")
    : `<div class="empty">No payments yet. Press <b>Run a normal day</b>, then try to fool an agent.</div>`;
}

$("feed-filter").addEventListener("change", renderFeed);

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
      <p class="desc"><span class="agent-tag">${esc(p.agent_id)}</span> ${esc(p.description)} · ${esc((RAILS[p.rail] || {}).label || p.rail)}</p>
      <div class="evidence">${evidence}</div>
      ${reason}
      <div class="lock">${LOCK_ICON}<div>What you approve is locked: this exact amount, payee and evidence.
        Any change voids it.<br>Approval hash <span class="mono" title="${esc(item.approval_hash)}">${esc(short(item.approval_hash, 24))}</span></div></div>
      <div class="inbox-actions">
        <button class="btn btn-approve" data-act="approve" data-hash="${esc(item.approval_hash)}">Approve</button>
        <button class="btn btn-deny" data-act="deny">Deny</button>
        <button class="btn btn-ghost btn-sm" data-act="tamper">Agent changes amount +$1</button>
      </div>
      <div class="tamper-result" ${tamperNotes[p.id] ? "" : "hidden"}>${esc(tamperNotes[p.id] || "")}</div>
    </div>`;
}

function renderAuthorizations() {
  const items = state.authorizations || [];
  $("auth-count").textContent = items.length;
  $("authorizations").innerHTML = items.length
    ? items.map((a) => `
      <div class="auth ${a.response_status === "AUTHORIZED" ? "ok" : "bad"}">
        <div class="flag-top"><b>${a.response_status === "AUTHORIZED" ? "Authorized" : "Declined"}</b>
          <span class="time">${esc(a.decided_in_ms)} ms · ${clock(a.ts)}</span></div>
        <p>${esc(money(a.amount, a.currency))} at ${esc(a.merchant_name || "?")} on ${esc(a.agent_id || "an unknown card")}</p>
        <div class="muted small">${esc(a.reason)}</div>
      </div>`).join("")
    : `<p class="muted small">No card charges yet.</p>`;
}

function renderFlags() {
  const flags = state.flags || [];
  $("flag-count").textContent = flags.length;
  $("flag-count").className = `count ${flags.length ? "has" : ""}`;
  $("connect-name").textContent = $("attack-agent").value || "an agent";
  $("flags").innerHTML = flags.length
    ? flags.map((f) => `
      <div class="flag">
        <div class="flag-top"><span class="agent-tag">${esc(f.agent_id)}</span><span class="time">${clock(f.ts)}</span></div>
        <p>${esc(f.summary)}</p>
        ${f.source ? `<div class="mono muted">${esc(f.source)}</div>` : ""}
      </div>`).join("")
    : `<p class="muted small">Nothing yet. An honest agent reports what it refuses to pay.</p>`;
}

$("btn-connect").addEventListener("click", async (event) => {
  const agent = $("attack-agent").value;
  await busy(event.currentTarget, "Issuing a token…", async () => {
    const { ok, data } = await api("/api/mcp/token", { agent_id: agent });
    const box = $("connect-result");
    box.hidden = false;
    box.innerHTML = ok
      ? `<p class="small">Token for <b>${esc(data.agent_id)}</b>, shown once (Warden keeps only its hash). Connect Claude Code:</p>
         <pre class="mono cmd">${esc(data.claude_code)}</pre>
         <p class="small muted">Or run the demo agent: <span class="mono">python -m warden.agent_runner --token ${esc(data.token)}</span></p>`
      : `<p class="bad">${esc(data.detail || "Could not issue a token")}</p>`;
  });
});

$("attack-agent").addEventListener("change", () => { $("connect-name").textContent = $("attack-agent").value; });

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
    // Kept by proposal id: the refusal is logged, the page redraws, and the note must survive that.
    tamperNotes[id] = data.refused
      ? `Refused: the agent tried to run it as ${money(data.tried_amount)}, but the approval hash no longer matches. Nothing was paid.`
      : data.detail || "Unexpected result";
    toast(`<b>Changed approval refused.</b><br>${esc(tamperNotes[id])}`, 6000);
    const out = item.querySelector(".tamper-result");
    out.hidden = false;
    out.textContent = tamperNotes[id];
    btn.disabled = false;
    return;
  }
  refresh();
});

$("btn-attack").addEventListener("click", async (event) => {
  const agent = $("attack-agent").value;
  await busy(event.currentTarget, "Agent is reading the email…", async () => {
    const { ok, data } = await api("/api/attack", { agent_id: agent });
    if (ok) toast(`<b>${esc(data.scenario)}</b><br>Warden: ${esc(BADGES[data.action] || data.action)}`);
  });
  refresh();
});

$("btn-card-outside").addEventListener("click", async (event) => {
  const agent = $("attack-agent").value;
  if (state.mode === "live" && !confirm(`Charge ${agent}'s real sandbox card directly, skipping Warden (as if its card details were copied)?`)) return;
  await busy(event.currentTarget, "Charging the card…", async () => {
    const { ok, data } = await api("/api/card-outside-warden", { agent_id: agent });
    if (!ok) { toast(`Not run: ${esc(data.detail)}`); return; }
    const head = `<b>Copied card used outside Warden: ${money(data.amount)}</b><br>`;
    if (data.warden) {
      toast(`${head}Airwallex asked Warden first. Warden: <b>${esc(data.warden.response_status)}</b> in ${esc(data.warden.decided_in_ms)} ms (${esc(data.warden.reason)})<br>Airwallex: ${esc(data.airwallex)}${data.failure_reason ? ` · ${esc(data.failure_reason)}` : ""}`, 9000);
    } else {
      toast(`${head}Airwallex: ${esc(data.airwallex)}${data.failure_reason ? ` · ${esc(data.failure_reason)}` : ""}. Remote authorization is off, so only the card's own limits applied; reconciliation will find this charge and open a dispute.`, 9000);
    }
  });
  refresh();
});

$("btn-normal").addEventListener("click", async (event) => {
  const agent = $("attack-agent").value;
  if (state.mode === "live" && !confirm(`Run ${agent}'s normal day with real sandbox payments?`)) return;
  await busy(event.currentTarget, "Working…", () => api("/api/normal-day", { agent_id: agent }));
  refresh();
});

// --- 3. prove -----------------------------------------------------------------

function renderReceipts() {
  const r = state.receipts;
  $("receipt-head").innerHTML = `
    <span class="verify ${r.chain_intact ? "ok" : "bad"}">${r.chain_intact ? "✓" : "✗"} ${r.count} receipts · ${r.chain_intact ? "every signature and link verifies" : `broken at receipt ${r.broken_at}`}</span>
    <span class="muted small">Ed25519 signer <span class="mono">${esc(r.fingerprint || "")}</span> · payees masked, evidence hashed: no account numbers, no card data</span>`;
  const rows = r.recent.map((x) => `
    <tr>
      <td><a class="mono" href="/verify/${encodeURIComponent(x.id)}" target="_blank" rel="noopener">${esc(x.id)} ↗</a></td>
      <td>${esc(x.agent_id || "")}</td>
      <td><span class="badge ${esc(x.action)}">${esc(BADGES[x.action] || x.action)}</span></td>
      <td class="num">${x.amount ? esc(money(x.amount, x.currency || "USD")) : ""}</td>
      <td>${esc(x.payee || "")}</td>
      <td class="muted">${esc((RAILS[x.rail] || {}).label || x.rail || "")}</td>
      <td>${esc(x.settlement || "")}</td>
      <td>${x.signed ? '<span class="ok">✓ signed</span>' : '<span class="bad">✗</span>'}${x.metal_tx ? ` · <a href="/verify/${encodeURIComponent(x.id)}" target="_blank" rel="noopener">verify on-chain ↗</a>` : ""}</td>
    </tr>`).join("");
  $("receipts").innerHTML = `<thead><tr><th>Receipt</th><th>Agent</th><th>Decision</th><th>Amount</th><th>Payee</th><th>Rail</th><th>Settlement</th><th>Proof</th></tr></thead>
    <tbody>${rows || '<tr><td colspan="8" class="muted">No decisions yet.</td></tr>'}</tbody>`;
}

// --- 4. recover ---------------------------------------------------------------

function pipeline(stateName) {
  const steps = state.cases.pipeline;
  const lost = stateName === "LOST" || stateName === "NOT_ELIGIBLE";
  const at = lost ? steps.length - 1 : steps.indexOf(stateName);
  return `<ol class="pipeline">${steps.map((s, i) => {
    const label = i === steps.length - 1 && lost ? CASE_WORDS[stateName] : CASE_WORDS[s];
    const cls = i < at ? "done" : i === at ? (lost ? "lost" : "now") : "";
    return `<li class="${cls}">${esc(label)}</li>`;
  }).join("")}</ol>`;
}

function caseCard(c) {
  return `
    <article class="case">
      <div class="case-top">
        <div>
          <div class="card-title"><span class="amount">${esc(money(c.amount, c.currency || "USD"))}</span>
            <span class="payee">${esc(c.merchant_name || "unknown merchant")}</span>
            <span class="rail-name">MCC ${esc(c.merchant_category)} · ${esc(c.agent_id)}'s card</span></div>
          <p class="desc">Card debit with <b>no Intent Receipt</b> and no human approval · case <span class="mono">${esc(c.case_id)}</span></p>
        </div>
        ${pipeline(c.state)}
      </div>
      ${c.summary ? `<p class="explain"><span class="who">Investigation</span>${esc(c.summary)}</p>` : ""}
      <div class="result">
        <span>status: ${esc(c.reason || CASE_WORDS[c.state])}</span>
        ${c.evidence_file_id ? `<span>evidence <span class="mono">${esc(short(c.evidence_file_id, 14))}</span></span>` : ""}
        ${c.dispute_id ? `<span>Airwallex dispute <span class="mono">${esc(short(c.dispute_id, 13))}</span> · ${esc(c.dispute_status || "")}</span>` : ""}
        <span class="muted">card txn <span class="mono">${esc(short(c.card_transaction_id, 13))}</span></span>
      </div>
      <p class="fine">Recovered only when Airwallex reports the dispute won. A submitted dispute is not money back.</p>
    </article>`;
}

function renderCases() {
  const items = state.cases.items;
  $("cases").innerHTML = items.length
    ? items.map(caseCard).join("")
    : `<div class="empty">No unexplained charges. Press <b>Run reconciliation</b> to match every card debit to a receipt.</div>`;
}

$("btn-reconcile").addEventListener("click", async (event) => {
  await busy(event.currentTarget, "Matching debits to receipts…", async () => {
    const { ok, data } = await api("/api/reconcile", {});
    toast(ok ? `Reconciliation: ${data.cases.length} unexplained charge${data.cases.length === 1 ? "" : "s"}` : `Failed: ${esc(data.detail)}`);
  });
  refresh();
});

// --- 5. locks -----------------------------------------------------------------

function renderLocks() {
  const l = state.locks;
  const items = (l.results || []).map((r) => `
    <article class="lock-card ${r.held ? "held" : "open"}">
      <div class="lock-icon">${LOCK_ICON}</div>
      <div>
        <h4>${esc(r.title.replace("Metal mandate", "Alta mandate"))}</h4>
        <p class="muted small">${esc(r.lock || "")}</p>
        <p>rail answered <span class="code ${r.held ? "violation" : ""}">${esc(r.got)}</span></p>
        <p class="${r.held ? "ok" : "bad"}">${r.held ? "Independent lock held" : `Expected ${esc(r.expected)}`}</p>
        ${r.ids ? `<div class="lock-ids">${Object.entries(r.ids).map(([k, v]) => `<div>${link(k, v)}</div>`).join("")}</div>` : ""}
      </div>
    </article>`).join("");
  $("lock-results").innerHTML = items
    ? `${items}<p class="muted small lock-source">Source: ${esc(l.source || "")}</p>`
    : `<div class="empty">${state.mode === "live" ? "Run <b>python -m warden.demo --live</b> to test the locks against the real rails." : "Fire procurement-bot, then press <b>Test the locks</b>."}</div>`;
}

$("btn-locks").addEventListener("click", async (event) => {
  await busy(event.currentTarget, "Testing…", async () => {
    const { ok, data } = await api("/api/locks", {});
    if (!ok) toast(esc(data.detail || "Could not test the locks"), 6000);
  });
  refresh();
});

// --- audit, reset and scorecard ---------------------------------------------------

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

$("btn-reset").addEventListener("click", async () => {
  await api("/api/reset", {});
  $("verify-result").textContent = "";
  $("hire-draft").innerHTML = "";
  refresh();
});

async function loadScorecard() {
  const { ok, data } = await api("/api/scorecard");
  if (!ok) return;
  const r = data.redteam;
  const red = r ? `<span class="score ${r.attacks_paid_without_a_human === 0 ? "ok" : "bad"}">Red team: ${r.attacks} attacks, ${r.attacks_paid_without_a_human} paid without a human · ${r.legitimate_paid}/${r.legitimate} legitimate paid</span>
    <span class="muted small">Claude as the attacker, plus expert attacks · <a href="https://github.com/RekhaSrinivas/warden/blob/main/docs/redteam/README.md" target="_blank" rel="noopener">every attack and verdict</a></span>` : "";
  $("scorecard").innerHTML = `${red}<span class="score ${data.ok ? "ok" : "bad"}">${esc(data.summary)}</span>
    <span class="muted small">attack gauntlet on simulated rails · ${data.rows.length} scenarios</span>`;
}

// --- live money flow ------------------------------------------------------------
// Every pulse is a real decision from the feed: agent -> Warden -> rail when allowed,
// stopped at the shield when blocked, parked at the shield when a human must decide.

const SVG_NS = "http://www.w3.org/2000/svg";
const FLOW_RAILS = [
  { kind: "transfer", name: "Airwallex", sub: "bank transfer" },
  { kind: "card", name: "Airwallex", sub: "virtual card" },
  { kind: "vic_checkout", name: "Visa VIC · Airi", sub: "agent checkout" },
  { kind: "x402", name: "Alta", sub: "x402 on-chain" },
];
const FLOW_TONE = { ALLOW: "ok", BLOCK: "bad", CONTAIN: "crit", STEP_UP: "hold" };
// Honour the OS "reduce motion" setting unless the viewer switches motion on (remembered).
let reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
try {
  const saved = localStorage.getItem("warden-motion");
  if (saved) reduceMotion = saved === "off";
} catch { /* storage blocked: keep the OS setting */ }
function applyMotion() {
  document.body.dataset.motion = reduceMotion ? "off" : "on";
  $("btn-motion").textContent = reduceMotion ? "Motion: off" : "Motion: on";
}
const flow = { seen: new Set(), queue: [], draining: false, shown: 0, primed: false };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function flowKey(item) {
  return `${item.proposal.id}:${item.pending ? "STEP_UP" : item.decision?.action || ""}`;
}

function flowAction(item) {
  return item.pending ? "STEP_UP" : item.decision?.action;
}

function toneOf(item) {
  const action = flowAction(item);
  return action === "ALLOW" && !landed(item) ? "hold" : FLOW_TONE[action] || "hold";
}

function landed(item) {  // allowed AND the rail actually executed it
  const ex = item.decision?.execution;
  return item.decision?.action === "ALLOW" && !!ex && !/DECLINED|ERROR|NO_RAIL/.test(ex.status || "");
}

function renderFlowNodes() {
  const paid = {};
  for (const i of state.feed) {
    if (landed(i)) {
      const r = (paid[i.proposal.rail] ||= { n: 0, amount: 0 });
      r.n += 1;
      r.amount += Number(i.proposal.amount) || 0;
    }
  }
  $("flow-agents").innerHTML = state.agents.map((a) => `
    <div class="fnode agent-node ${a.contained ? "contained" : ""}" data-agent="${esc(a.id)}">
      <span class="fdot"></span>
      <div><b>${esc(a.id)}</b><small>${a.contained ? "fired · contained on every rail" : `trust ${Math.round(a.trust * 100)} · ${esc(money(a.spent_week))} of ${esc(money(a.weekly_cap))}`}</small></div>
    </div>`).join("") || `<div class="fnode"><small>No agents hired yet</small></div>`;
  $("flow-rails").innerHTML = FLOW_RAILS.map((r) => {
    const p = paid[r.kind];
    return `<div class="fnode rail-node" data-rail="${r.kind}">
      <span class="ic"><svg viewBox="0 0 24 24">${RAILS[r.kind].icon}</svg></span>
      <div><b>${esc(r.name)}</b><small>${esc(r.sub)}</small></div>
      <span class="rail-count" title="paid through this rail">${p ? `${p.n} · ${esc(money(p.amount))}` : "–"}</span>
    </div>`;
  }).join("");
  const shape = state.agents.map((a) => `${a.id}:${a.contained}`).join("|");
  if (shape !== flow.shape) {  // paths move only when the cast changes; pulses in flight keep theirs
    flow.shape = shape;
    drawFlowPaths();
  }
}

function anchorOf(el, side) {
  const s = $("flow-stage").getBoundingClientRect();
  const r = el.getBoundingClientRect();
  return { x: (side === "right" ? r.right : r.left) - s.left, y: r.top + r.height / 2 - s.top };
}

function curve(a, b) {
  const dx = Math.max(40, (b.x - a.x) * 0.55);
  return `M${a.x},${a.y} C${a.x + dx},${a.y} ${b.x - dx},${b.y} ${b.x},${b.y}`;
}

function drawFlowPaths() {
  const svg = $("flow-svg");
  const stage = $("flow-stage").getBoundingClientRect();
  if (!stage.width) return;
  svg.setAttribute("viewBox", `0 0 ${stage.width} ${stage.height}`);
  const shield = $("shield");
  const sIn = anchorOf(shield, "left");
  const sOut = anchorOf(shield, "right");
  sIn.x += 18; sOut.x -= 18;
  const paths = [];
  document.querySelectorAll("#flow-agents .agent-node").forEach((n) => {
    paths.push(`<path class="fpath ${n.classList.contains("contained") ? "dead" : ""}" id="in-${esc(n.dataset.agent)}" d="${curve(anchorOf(n, "right"), sIn)}"/>`);
  });
  document.querySelectorAll("#flow-rails .rail-node").forEach((n) => {
    paths.push(`<path class="fpath" id="out-${n.dataset.rail}" d="${curve(sOut, anchorOf(n, "left"))}"/>`);
  });
  $("flow-dots-svg").setAttribute("viewBox", `0 0 ${stage.width} ${stage.height}`);
  svg.innerHTML = `<g class="fpaths">${paths.join("")}</g>
    <g class="flive">${paths.map((p) => p.replace('class="fpath', 'class="fpath live').replace(/id="[^"]*"/, "")).join("")}</g>`;
}

function travel(path, dot, ms) {
  return new Promise((resolve) => {
    if (!path) return resolve();
    const length = path.getTotalLength();
    const t0 = performance.now();
    const step = (t) => {
      const k = Math.min(1, (t - t0) / ms);
      const e = k < 0.5 ? 2 * k * k : 1 - Math.pow(-2 * k + 2, 2) / 2;
      const pt = path.getPointAtLength(length * e);
      dot.setAttribute("transform", `translate(${pt.x},${pt.y})`);
      k < 1 ? requestAnimationFrame(step) : resolve();
    };
    requestAnimationFrame(step);
  });
}

function pop(x, y, html, tone) {
  const el = document.createElement("div");
  el.className = `pop ${tone}`;
  el.style.left = `${x}px`;
  el.style.top = `${y}px`;
  el.innerHTML = html;
  $("flow-pops").appendChild(el);
  setTimeout(() => el.remove(), 3400);
}

function verdict(html, tone) {  // one verdict card under the shield; the newest replaces the last
  const el = $("shield-caption");
  el.className = `shield-caption verdict ${tone}`;
  el.innerHTML = html;
  clearTimeout(verdict.t);
  verdict.t = setTimeout(() => { el.className = "shield-caption"; el.textContent = "checks every payment"; }, 3600);
}

function flash(el, cls, ms = 900) {
  if (!el) return;
  el.classList.remove(cls);
  void el.offsetWidth;
  el.classList.add(cls);
  setTimeout(() => el.classList.remove(cls), ms);
}

function reasonOf(item) {
  const d = item.decision;
  if (item.pending) return "waiting for you";
  if (!d) return "";
  if (d.action === "ALLOW") return (RAILS[item.proposal.rail] || {}).label || item.proposal.rail;
  const v = d.violations[0] || d.risk?.flags?.[0];
  return v ? v.replaceAll("_", " ").toLowerCase() : "stopped";
}

function addTicker(item) {
  const action = flowAction(item);
  const p = item.proposal;
  const li = document.createElement("li");
  li.className = toneOf(item);
  const word = { ALLOW: landed(item) ? "Paid" : "Allowed", BLOCK: "Blocked", CONTAIN: "Fired", STEP_UP: "Held" }[action] || action;
  li.innerHTML = `<span class="tk-badge">${esc(word)}</span>
    <span class="tk-main"><b>${esc(money(p.amount, p.currency))}</b> ${esc(p.payee_name)}<small>${esc(p.agent_id)} · ${esc(reasonOf(item))}</small></span>`;
  const list = $("ticker");
  list.prepend(li);
  while (list.children.length > 6) list.lastElementChild.remove();
}

async function animateDecision(item) {
  const action = flowAction(item);
  const tone = toneOf(item);
  const p = item.proposal;
  addTicker(item);
  if (reduceMotion || !$("flow-svg").getBBox) return;
  const dots = $("flow-dots");
  if (!dots) return;
  const dot = document.createElementNS(SVG_NS, "g");
  dot.setAttribute("class", `fdot-pulse ${tone === "ok" ? "" : "pending"}`);
  dot.innerHTML = `<circle r="16" fill="url(#glow)" opacity=".35"/><circle r="6"/>`;
  dots.appendChild(dot);
  flash(document.querySelector(`.agent-node[data-agent="${CSS.escape(p.agent_id)}"]`), "emit", 700);
  await travel(document.getElementById(`in-${p.agent_id}`), dot, 900);
  const shield = $("shield");
  dot.setAttribute("class", `fdot-pulse ${tone}`);
  shield.classList.remove("hit-ok", "hit-bad", "hit-crit", "hit-hold");
  flash(shield, `hit-${tone}`, 1000);
  if (action === "ALLOW" && landed(item)) {
    verdict(`✓ ${esc(money(p.amount, p.currency))} ${esc(p.payee_name)} · <b>within mandate</b><small>${esc(reasonOf(item))}</small>`, "ok");
    await travel(document.getElementById(`out-${p.rail}`), dot, 800);
    const rail = document.querySelector(`.rail-node[data-rail="${p.rail}"]`);
    flash(rail, "landed", 1200);
    if (rail) {
      const r = anchorOf(rail, "left");
      pop(r.x - 16, r.y, `✓ ${esc(money(p.amount, p.currency))} paid`, "ok");
    }
    dot.remove();
  } else {
    const label = action === "STEP_UP" ? "held for a human" : action === "CONTAIN" ? "agent fired"
      : action === "ALLOW" ? "rail refused" : "blocked";
    verdict(`${action === "STEP_UP" ? "⏸" : "✕"} ${esc(money(p.amount, p.currency))} ${esc(p.payee_name)} · <b>${esc(label)}</b><small>${esc(reasonOf(item))}</small>`, tone);
    if (action === "CONTAIN") flash(document.querySelector(`.agent-node[data-agent="${CSS.escape(p.agent_id)}"]`), "struck", 1600);
    dot.classList.add("burst");
    await sleep(600);
    dot.remove();
  }
}

async function drainFlow(gap) {
  if (flow.draining) return;
  flow.draining = true;
  while (flow.queue.length) {
    const item = flow.queue.shift();
    rpFollow(item);
    animateDecision(item);
    await sleep(reduceMotion ? 0 : gap);
  }
  flow.draining = false;
}

// --- decision replay --------------------------------------------------------------
// The bar under the flow steps through state.feed oldest first, one decision per step, each drawn
// with the same pulse as a live one (none when motion is off). New live decisions still play as they
// arrive: they pause the replay and the bar follows them.

const VERDICT_WORD = { ALLOW: "ALLOW", STEP_UP: "HELD", BLOCK: "BLOCK", CONTAIN: "CONTAIN" };
const RP_GAP = 1600;  // ms between steps at 1x, the old replay's pace
const rp = { list: [], pos: 0, id: null, playing: false, timer: 0, phases: [] };

// One feed entry: a retried request reuses its proposal id, so the decision's audit seq tells them apart.
function rpKey(item) {
  return `${item.proposal.id}#${item.decision?.seq ?? "-"}`;
}

function rpSync() {  // after every refresh: same decision, new list
  rp.list = state.feed.filter((i) => i.pending || i.decision).slice().reverse();
  const at = rp.id ? rp.list.findIndex((i) => rpKey(i) === rp.id) : -1;
  if (at >= 0) rp.pos = at + 1;
  else if (!rp.playing) rp.pos = rp.list.length;
  rpBar();
  if (rp.playing) return;
  rpCaption(rp.list[rp.pos - 1]);
  if (rp.id === null) rpPhases(rp.list[rp.pos - 1]);
}

function rpBar() {
  const n = rp.list.length;
  const s = $("rp-scrub");
  s.min = n ? 1 : 0;
  s.max = n;
  s.value = rp.pos;
  s.disabled = !n;
  s.style.setProperty("--fill", `${n > 1 ? ((rp.pos - 1) / (n - 1)) * 100 : n ? 100 : 0}%`);
  $("rp-count").textContent = `${rp.pos} / ${n}`;
  $("rp-restart").disabled = !n;
  $("rp-play").disabled = !n;
  $("rp-back").disabled = rp.pos <= 1;
  $("rp-next").disabled = rp.pos >= n;
  $("replay").classList.toggle("playing", rp.playing);
  $("rp-play").setAttribute("aria-label", rp.playing ? "Pause" : "Play");
  $("rp-play").title = rp.playing ? "Pause" : n && rp.pos >= n ? "Play from the first decision" : "Play";
}

function rpCaption(item) {
  if (!item) {
    $("rp-caption").innerHTML = `<span class="muted">No decisions yet. Press <b>Run a normal day</b>, then try to fool an agent.</span>`;
    rpPhases(null);
    return;
  }
  const p = item.proposal;
  const d = item.decision;
  const action = flowAction(item);
  const rail = (RAILS[p.rail] || {}).label || p.rail;
  const violations = d?.violations || [];
  const codesHtml = violations.map((v) => `<span class="code violation">${esc(v)}</span>`)
    .concat((d?.risk?.flags || []).filter((f) => !violations.includes(f)).map((f) => `<span class="code">${esc(f)}</span>`)).join("");
  const risk = d?.risk;
  const why = risk && risk.explanation && risk.source !== "skipped"
    ? `<span class="rp-why"><b>${risk.source === "claude" ? "Claude" : "Risk reader"}</b> ${esc(risk.explanation)}</span>` : "";
  const railNote = action === "ALLOW" ? (landed(item) ? "" : "refused it") : action === "STEP_UP" ? "waits for you" : "never reached";
  const railHtml = landed(item) ? esc(rail) : `<s>${esc(rail)}</s>`;
  $("rp-caption").innerHTML = `
    <div class="rp-line">
      <span class="rp-time">${esc(clock(d?.ts))}</span>
      <span class="agent-tag">${esc(p.agent_id)}</span><span class="rp-arrow" aria-hidden="true">→</span>
      <span class="rp-warden">Warden</span><span class="rp-verdict ${toneOf(item)}">${esc(VERDICT_WORD[action] || action || "PENDING")}</span><span class="rp-arrow" aria-hidden="true">→</span>
      <span class="rp-rail">${railHtml}</span>${railNote ? `<span class="rp-note">${esc(railNote)}</span>` : ""}
      <span class="rp-amt">${esc(money(p.amount, p.currency))}</span><span class="rp-payee">${esc(p.payee_name)}</span>
    </div>
    <div class="rp-line rp-sub">${codesHtml}${why || (codesHtml ? "" : `<span class="muted">Within mandate · ${esc(p.description)}</span>`)}</div>`;
}

// The five chips light up with the pulse: asked, limits checked, evidence read, verdict, rail.
function rpPhases(item, animate = false) {
  rp.phases.forEach(clearTimeout);
  rp.phases = [];
  const ol = $("rp-steps");
  const lis = [...ol.children];
  const set = (k) => lis.forEach((li, i) => { li.classList.toggle("on", i < k); li.classList.toggle("now", i === k - 1); });
  ol.className = `rp-steps ${item ? toneOf(item) : ""}`;
  const action = item && flowAction(item);
  ol.querySelector(".rp-last").textContent = !item ? "Rail" : landed(item) ? "Paid" : action === "STEP_UP" ? "Held"
    : action === "CONTAIN" ? "Fired" : action === "ALLOW" ? "Refused" : "Stopped";
  if (!item) return set(0);
  if (!animate) return set(5);
  set(1);
  for (const [ms, k] of [[350, 2], [700, 3], [950, 4], [landed(item) ? 1750 : 1150, 5]]) rp.phases.push(setTimeout(() => set(k), ms));
}

function rpShow(k, animate = true) {  // show step k (1-based); animate adds it to the ticker and pulses
  const n = rp.list.length;
  if (!n) return;
  rp.pos = Math.max(1, Math.min(n, k));
  const item = rp.list[rp.pos - 1];
  rp.id = rpKey(item);
  rpBar();
  rpCaption(item);
  rpPhases(item, animate && !reduceMotion);
  if (animate) animateDecision(item);
}

function rpJump(k) {  // a jump: the ticker shows the decisions leading up to step k, then step k plays
  const upto = Math.max(1, Math.min(rp.list.length, k)) - 1;
  $("ticker").innerHTML = "";
  rp.list.slice(Math.max(0, upto - 5), upto).forEach(addTicker);
  rpShow(k);
}

function rpFollow(item) {  // a live decision: the bar moves to it
  const at = rp.list.findIndex((i) => rpKey(i) === rpKey(item));
  if (at < 0) return;
  rp.pos = at + 1;
  rp.id = rpKey(item);
  rpBar();
  rpCaption(item);
  rpPhases(item, !reduceMotion);
}

function rpSchedule(delay) {
  clearTimeout(rp.timer);
  if (!rp.playing) return;
  if (rp.pos >= rp.list.length && delay === undefined) { rpPause(); return; }
  rp.timer = setTimeout(() => {
    if (!rp.playing) return;
    if (rp.pos >= rp.list.length) { rpPause(); return; }
    rpShow(rp.pos + 1);
    rpSchedule();
  }, delay ?? RP_GAP / (Number($("rp-speed").value) || 1));
}

function rpPlay() {
  if (!rp.list.length) return;
  rp.playing = true;
  if (rp.pos >= rp.list.length) rpJump(1);
  else rpShow(rp.pos + 1);
  rpSchedule();
}

function rpPause() {
  rp.playing = false;
  clearTimeout(rp.timer);
  rpBar();
}

$("rp-play").addEventListener("click", () => (rp.playing ? rpPause() : rpPlay()));
$("rp-restart").addEventListener("click", () => { rpPause(); rp.playing = true; rpJump(1); rpSchedule(); });
$("rp-back").addEventListener("click", () => { rpPause(); rpJump(rp.pos - 1); });
$("rp-next").addEventListener("click", () => { rpPause(); rpJump(rp.pos + 1); });
// read the thumb before pausing: pausing redraws the bar, thumb included
$("rp-scrub").addEventListener("input", (e) => { const k = Number(e.target.value); rpPause(); rpShow(k, false); });
$("rp-scrub").addEventListener("change", (e) => { const k = Number(e.target.value); rpPause(); rpJump(k); });
$("rp-speed").addEventListener("change", () => { if (rp.playing) rpSchedule(); });

function countUp(el, to) {
  const from = Number(el.dataset.v || 0);
  el.dataset.v = to;
  if (reduceMotion || from === to) { el.textContent = money(to); return; }
  const t0 = performance.now();
  const step = (t) => {
    const k = Math.min(1, (t - t0) / 1400);
    el.textContent = money(from + (to - from) * (1 - Math.pow(1 - k, 3)));
    if (k < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

function renderFlow() {
  renderFlowNodes();
  const m = state.protection;
  if (m) {
    countUp($("big-protected"), Number(m.protected));
    $("big-sub").innerHTML = `<span class="${Number(m.lost.amount) ? "bad" : "ok"}">${esc(money(m.lost.amount))} lost</span>
      · ${esc(money(m.paid.amount))} paid within mandate · ${state.receipts.count} signed receipts`;
  }
  const decided = state.feed.filter((i) => i.pending || i.decision);
  rpSync();
  if (!flow.primed) {  // first paint: replay the recent story so the page opens in motion
    flow.primed = true;
    decided.forEach((i) => flow.seen.add(flowKey(i)));
    replayFlow();
    return;
  }
  const fresh = decided.filter((i) => !flow.seen.has(flowKey(i))).reverse();
  fresh.forEach((i) => flow.seen.add(flowKey(i)));
  if (fresh.length) rpPause();  // live decisions take over from a replay
  flow.queue.push(...fresh);
  drainFlow(700);
}

// Replays the last ten decisions through the replay bar. With motion off it shows the latest at once.
function replayFlow() {
  rpPause();
  const n = rp.list.length;
  if (!n) return;
  if (reduceMotion) { rpJump(n); return; }
  $("ticker").innerHTML = "";
  rp.pos = Math.max(0, n - 10);  // the next step is the first of the last ten
  rp.id = rp.pos ? rpKey(rp.list[rp.pos - 1]) : null;
  rp.playing = true;
  rpBar();
  clearTimeout(rp.timer);
  rp.timer = setTimeout(() => {
    if (!rp.playing) return;
    rpShow(rp.pos + 1);
    rpSchedule();
  }, 400);
}

$("btn-replay").addEventListener("click", () => { if (!flow.draining) replayFlow(); });
$("btn-motion").addEventListener("click", () => {
  reduceMotion = !reduceMotion;
  try { localStorage.setItem("warden-motion", reduceMotion ? "off" : "on"); } catch { /* not remembered */ }
  applyMotion();
  if (!reduceMotion && !flow.draining) replayFlow();
});
applyMotion();
window.addEventListener("load", drawFlowPaths);
window.addEventListener("resize", () => { clearTimeout(drawFlowPaths.t); drawFlowPaths.t = setTimeout(drawFlowPaths, 120); });

function render() {
  renderFlow();
  renderHeader();
  renderAgents();
  renderFeed();
  renderInbox();
  renderFlags();
  renderAuthorizations();
  renderReceipts();
  renderCases();
  renderLocks();
  renderAudit();
  requestAnimationFrame(spySections);
}

// --- app shell: sidebar links, scroll-spy, phone drawer ----------------------------------
// Each sidebar link scrolls to a part of this page. The highlight follows the section in view; a
// link inside a section (Approvals, Card network…) stays lit after a click until you leave that section.

const navLinks = [...document.querySelectorAll("#sidebar a[data-nav]")];
const spyLinks = navLinks
  .filter((a) => a.closest(".sb-nav"))
  .map((a) => {
    const el = document.querySelector(a.getAttribute("href"));
    return el && { a, section: el.closest(".act") || el };
  })
  .filter(Boolean);
const spySectionsList = [...new Set(spyLinks.map((l) => l.section))];
const spy = { clicked: null, lockUntil: 0, raf: 0 };

function markLink(a) {
  for (const l of spyLinks) {
    const on = l.a === a;
    if (on && !l.a.classList.contains("active")) keepInNav(l.a);
    l.a.classList.toggle("active", on);
    if (on) l.a.setAttribute("aria-current", "location");
    else l.a.removeAttribute("aria-current");
  }
}

function keepInNav(a) {  // on a short screen the link list scrolls: keep the lit link in it
  const nav = $("sb-nav");
  if (nav.scrollHeight <= nav.clientHeight) return;
  const r = a.getBoundingClientRect();
  const n = nav.getBoundingClientRect();
  if (r.bottom > n.bottom - 24) nav.scrollTop += r.bottom - n.bottom + 32;
  else if (r.top < n.top) nav.scrollTop -= n.top - r.top + 8;
}

function navFade() {  // a soft fade at the bottom while there is more list below
  const nav = $("sb-nav");
  nav.classList.toggle("more", nav.scrollTop + nav.clientHeight < nav.scrollHeight - 2);
}
$("sb-nav").addEventListener("scroll", navFade, { passive: true });

function spySections() {
  if (performance.now() < spy.lockUntil) return;
  const line = ($("sb-toggle").closest(".topbar")?.offsetHeight || 0) + 96;
  let current = spySectionsList[0];
  for (const s of spySectionsList) if (s.getBoundingClientRect().top <= line) current = s;
  if (!current) return;
  if (spy.clicked && spyLinks.find((l) => l.a === spy.clicked)?.section === current) { markLink(spy.clicked); return; }
  spy.clicked = null;
  markLink(spyLinks.find((l) => l.section === current)?.a);
}

function setDrawer(open) {
  document.body.classList.toggle("sb-open", open);
  $("sb-backdrop").hidden = !open;
  $("sb-toggle").setAttribute("aria-expanded", String(open));
  $("sb-toggle").setAttribute("aria-label", open ? "Close navigation" : "Open navigation");
}

for (const a of navLinks) {
  a.addEventListener("click", (event) => {
    const el = document.querySelector(a.getAttribute("href"));
    if (!el) return;
    event.preventDefault();
    setDrawer(false);
    el.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "start" });
    try { history.replaceState(null, "", a.getAttribute("href")); } catch { /* fine without */ }
    if (spyLinks.some((l) => l.a === a)) {
      spy.clicked = a;
      markLink(a);
      // hold the highlight until the smooth scroll lands (scrollend), with a timer as the fallback
      spy.lockUntil = performance.now() + (reduceMotion ? 120 : 2500);
      const release = () => { spy.lockUntil = 0; spySections(); };
      window.addEventListener("scrollend", release, { once: true });
      setTimeout(release, reduceMotion ? 140 : 2520);
    }
  });
}
$("sb-toggle").addEventListener("click", () => setDrawer(!document.body.classList.contains("sb-open")));
$("sb-backdrop").addEventListener("click", () => setDrawer(false));
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && document.body.classList.contains("sb-open")) setDrawer(false); });
window.matchMedia("(max-width: 760px)").addEventListener?.("change", () => setDrawer(false));
window.addEventListener("scroll", () => {
  cancelAnimationFrame(spy.raf);
  spy.raf = requestAnimationFrame(spySections);
}, { passive: true });
window.addEventListener("resize", () => requestAnimationFrame(() => { spySections(); navFade(); }));
window.addEventListener("load", () => { spySections(); navFade(); });

render();
spySections();
navFade();
loadScorecard();

const events = new EventSource("/api/stream");
events.addEventListener("audit", refresh);
