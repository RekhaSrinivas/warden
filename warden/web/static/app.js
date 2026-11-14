// Warden dashboard. Renders /api/state as one story: hire, guard, prove, recover, locks.
// Re-renders whenever the audit log grows (server-sent events).
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
const CASE_WORDS = {
  UNATTRIBUTED: "Unattributed", INVESTIGATING: "Investigating", DISPUTE_SUBMITTED: "Dispute submitted",
  RECOVERED: "Recovered", LOST: "Lost", NOT_ELIGIBLE: "Not eligible",
};
const LOCK_ICON = '<svg viewBox="0 0 24 24"><rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/></svg>';

let state = JSON.parse($("initial-state").textContent);
const fireArmed = {}; // agent id -> time until which the second click fires

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
    .map((r) => `<span class="chip ${esc(r.status.split(" ")[0])}"><span class="dot"></span>${esc(r.label)}<span class="state">${esc(word[r.status] || r.status)}</span></span>`)
    .join("");
  const pill = $("mode-pill");
  pill.textContent = state.mode === "live" ? "Live · Airwallex sandbox · Metal testnet · Claude" : "Offline demo · simulated rails";
  pill.className = `mode-pill ${state.mode === "live" ? "live" : ""}`;
  $("btn-reset").hidden = state.mode === "live";
  $("btn-locks").hidden = state.mode === "live";
  const m = state.protection;
  if (m) {
    const tile = (cls, label, v, note) => `<div class="tile ${cls}"><span class="label">${label}</span>
      <span class="value">${esc(money(v.amount ?? v))}</span><span class="note">${note}</span></div>`;
    $("protect").innerHTML = [
      tile("hero", "Money protected", m.protected, "stopped + double payments + declined live + recovered"),
      tile("bad", "Stopped", m.stopped, `${m.stopped.count} payments blocked or agent contained`),
      tile("bad", "Double payments prevented", m.double_payments, `${m.double_payments.count} retries absorbed`),
      tile("bad", "Declined in real time", m.declined_live, `${m.declined_live.count} card charges without a receipt`),
      tile("warn", "Held for a human", m.held, `${m.held.count} waiting in the inbox`),
      tile("warn", "Under dispute", m.under_dispute, `${m.under_dispute.count} charges no human authorised`),
      tile("ok", "Recovered", m.recovered, "counted only when Airwallex reports WON"),
      tile("ok", "Paid within mandate", m.paid, `${m.paid.count} payments`),
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
        <span class="muted">${a.contained ? "frozen" : `${money(a.per_txn_cap)}/purchase · ${money(a.weekly_cap)}/week · MCC ${esc(a.mccs.join(", ") || "none")}`}</span></div>`
    : "";
  const metal = a.metal
    ? `<div class="rail-line"><span class="rl-icon"><svg viewBox="0 0 24 24">${RAILS.x402.icon}</svg></span>
        <span>Metal mandate ${a.metal.explorer ? `<a href="${esc(a.metal.explorer)}" target="_blank" rel="noopener" class="mono">${esc(a.metal.mandate_id)} ↗</a>` : `<span class="mono">${esc(a.metal.mandate_id)}</span>`}</span>
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
  if (keep && state.agents.some((a) => a.id === keep)) $("attack-agent").value = keep;
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
      <button class="btn btn-approve btn-wide" id="btn-confirm" data-agent="${esc(d.agent_id)}" ${d.errors.length ? "disabled" : ""}>
        Confirm and provision ${state.mode === "live" ? "a real card and Metal mandate" : "(simulated)"}
      </button>
    </div>`;
}

$("btn-draft").addEventListener("click", async (event) => {
  const body = { agent_id: $("hire-id").value, role: $("hire-role").value, instruction: $("hire-text").value };
  await busy(event.currentTarget, "Claude is drafting…", async () => {
    const { ok, data } = await api("/api/hire/draft", body);
    $("hire-draft").innerHTML = ok ? draftView(data) : `<p class="bad">${esc(data.detail)}</p>`;
  });
});

$("hire-draft").addEventListener("click", async (event) => {
  const btn = event.target.closest("#btn-confirm");
  if (!btn) return;
  if (state.mode === "live" && !confirm("Provision a real Airwallex sandbox card and bind a Metal mandate on testnet?")) return;
  await busy(btn, "Provisioning…", async () => {
    const { ok, data } = await api("/api/hire/confirm", { agent_id: btn.dataset.agent });
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
  const parts = Object.entries(c).map(([rail, r]) => `<span class="bad">${esc(rail)}: ${esc(r?.status ?? r?.card_status ?? "done")}</span>`);
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
        ${codes(d)}${explanation(d)}${held}${execution(d)}${containment(d)}${refused}
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
      <div class="tamper-result" hidden></div>
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
    const out = item.querySelector(".tamper-result");
    out.hidden = false;
    out.textContent = data.refused
      ? `Refused: the agent tried to run it as ${money(data.tried_amount)}, but the approval hash no longer matches.`
      : data.detail || "Unexpected result";
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
        <h4>${esc(r.title)}</h4>
        <p class="muted small">${esc(r.lock || "")}</p>
        <p>rail answered <span class="code ${r.held ? "violation" : ""}">${esc(r.got)}</span></p>
        <p class="${r.held ? "ok" : "bad"}">${r.held ? "Independent lock held" : `Expected ${esc(r.expected)}`}</p>
        ${r.ids ? Object.entries(r.ids).map(([k, v]) => link(k, v)).join(" ") : ""}
      </div>
    </article>`).join("");
  $("lock-results").innerHTML = items
    ? `${items}<p class="muted small lock-source">Source: ${esc(l.source || "")}</p>`
    : `<div class="empty">${state.mode === "live" ? "Run <b>python -m warden.demo --live</b> to test the locks against the real rails." : "Fire procurement-bot, then press <b>Test the locks</b>."}</div>`;
}

$("btn-locks").addEventListener("click", async (event) => {
  await busy(event.currentTarget, "Testing…", () => api("/api/locks", {}));
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
  $("scorecard").innerHTML = `<span class="score ${data.ok ? "ok" : "bad"}">${esc(data.summary)}</span>
    <span class="muted small">attack gauntlet on simulated rails · ${data.rows.length} scenarios</span>`;
}

function render() {
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
}

render();
loadScorecard();

const events = new EventSource("/api/stream");
events.addEventListener("audit", refresh);
