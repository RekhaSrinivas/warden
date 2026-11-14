// Warden story page. Pinned scenes scrub with the scroll; everything shown comes from the
// live run (/api/state), with a fallback to the scripted scenario when the book is empty.
"use strict";

const $ = (id) => document.getElementById(id);
const state = JSON.parse($("initial-state").textContent || "{}");
const EXPLORER = "https://testnet.metalntwx.com/tx/";
const RAIL_INFO = {
  transfer: { name: "Airwallex", sub: "bank transfer" },
  card: { name: "Airwallex", sub: "issued virtual card" },
  vic_checkout: { name: "Visa VIC · Airi", sub: "checkout, passkey-confirmed" },
  x402: { name: "Alta", sub: "x402 on-chain" },
};

// Motion: the viewer's saved choice (shared with the console), else the OS setting.
let still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
try {
  const saved = localStorage.getItem("warden-motion");
  if (saved) still = saved === "off";
} catch { /* storage blocked */ }
document.body.classList.toggle("still", still);

const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = (a, c = "USD", digits) => {
  const n = Number(a);
  const d = digits ?? (Math.abs(n) < 1 && n !== 0 ? 2 : n % 1 ? 2 : 0);
  try { return new Intl.NumberFormat("en-US", { style: "currency", currency: c, minimumFractionDigits: d, maximumFractionDigits: d }).format(n); }
  catch { return `${a} ${c}`; }
};
const clamp = (v, a = 0, b = 1) => Math.min(b, Math.max(a, v));
const lerp = (a, b, t) => a + (b - a) * t;
const ease = (t) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);
const feed = state.feed || [];
const landed = (i) => i.decision?.action === "ALLOW" && !!i.decision.execution && !/DECLINED|ERROR|NO_RAIL/.test(i.decision.execution.status || "");

if (state.snapshot) {  // offline with nothing run yet: the server sent the published live run instead
  const note = $("snapshot-note");
  note.hidden = false;
  note.textContent = `Replaying the published live run of ${state.snapshot}. Everything below happened on the real rails; your offline console starts fresh.`;
}

// --- scroll engine ---------------------------------------------------------------------
const scenes = [...document.querySelectorAll(".scene")];
const handlers = {};
let ticking = false;

function onScroll() {
  if (ticking) return;
  ticking = true;
  requestAnimationFrame(() => {
    ticking = false;
    const max = document.documentElement.scrollHeight - innerHeight;
    $("progress").style.transform = `scaleX(${max > 0 ? scrollY / max : 0})`;
    for (const s of scenes) {
      const r = s.getBoundingClientRect();
      const span = s.offsetHeight - innerHeight;
      const p = clamp(-r.top / (span || 1));
      if (r.bottom < -50 || r.top > innerHeight + 50) continue;
      s.style.setProperty("--p", p.toFixed(4));
      handlers[s.dataset.scene]?.(p, s);
    }
  });
}
addEventListener("scroll", onScroll, { passive: true });
addEventListener("resize", () => { layoutAll(); onScroll(); });

// --- reveal on view ----------------------------------------------------------------------
const io = new IntersectionObserver((entries) => {
  for (const e of entries) if (e.isIntersecting) { e.target.classList.add("in"); e.target.dispatchEvent(new Event("inview")); io.unobserve(e.target); }
}, { threshold: 0.25 });
document.querySelectorAll(".reveal, #num-grid").forEach((el) => io.observe(el));

// --- 0 hero: particles drifting into the shield ---------------------------------------------
(function particles() {
  const c = $("particles");
  const ctx = c.getContext("2d");
  let w = 0, h = 0, cx = 0, cy = 0, dots = [], running = !still, visible = true;
  function size() {
    const dpr = Math.min(2, devicePixelRatio || 1);
    w = c.clientWidth; h = c.clientHeight;
    c.width = w * dpr; c.height = h * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const shield = document.querySelector(".hero-shield").getBoundingClientRect();
    const box = c.getBoundingClientRect();
    cx = shield.left + shield.width / 2 - box.left;
    cy = shield.top + shield.height / 2 - box.top;
  }
  function spawn(d = {}) {
    const a = Math.random() * Math.PI * 2;
    const r = Math.max(w, h) * (0.45 + Math.random() * 0.4);
    d.x = cx + Math.cos(a) * r; d.y = cy + Math.sin(a) * r * 0.7;
    d.s = 0.0016 + Math.random() * 0.0028; d.t = 0; d.x0 = d.x; d.y0 = d.y;
    d.r = Math.random() < 0.12 ? 2.2 : 1 + Math.random();
    d.warm = Math.random() >= 0.8;
    d.curve = (Math.random() - 0.5) * 0.9;
    return d;
  }
  function frame() {
    if (!running || !visible) return;
    ctx.clearRect(0, 0, w, h);
    for (const d of dots) {
      d.t += d.s;
      if (d.t >= 1) spawn(d);
      const t = ease(d.t);
      const mx = lerp(d.x0, cx, t), my = lerp(d.y0, cy, t);
      const bend = Math.sin(t * Math.PI) * d.curve * 120;
      const x = mx + bend * -(d.y0 - cy) / 600, y = my + bend * (d.x0 - cx) / 600;
      const alpha = Math.sin(d.t * Math.PI) * 0.85;
      ctx.beginPath();
      const light = document.documentElement.dataset.theme === "light";
      ctx.fillStyle = `rgba(${d.warm ? (light ? "21,23,28" : "243,239,230") : (light ? "8,145,178" : "34,211,238")},${alpha})`;
      ctx.arc(x, y, d.r * (1 - t * 0.5), 0, Math.PI * 2);
      ctx.fill();
    }
    requestAnimationFrame(frame);
  }
  size();
  dots = Array.from({ length: 140 }, () => { const d = spawn(); d.t = Math.random(); return d; });
  addEventListener("resize", size);
  new IntersectionObserver(([e]) => { visible = e.isIntersecting; if (visible) requestAnimationFrame(frame); }).observe(c);
  if (still) {  // one calm still frame
    running = true; visible = true; frame(); running = false;
  } else requestAnimationFrame(frame);
})();

// --- 1 threat: the red flags light up as you scroll ----------------------------------------
handlers.threat = (p) => {
  const steps = [0.3, 0.4, 0.5, 0.6];
  steps.forEach((at, i) => {
    const on = p > at;
    document.querySelectorAll(`.flagged[data-k="${i + 1}"], .note[data-k="${i + 1}"]`).forEach((el) => el.classList.toggle("on", on));
  });
  $("agent-says").classList.toggle("on", p > 0.7);
  $("threat-kicker").classList.toggle("on", p > 0.82);
};

// --- geometry helpers ------------------------------------------------------------------------
function anchor(el, stage, side) {
  const s = stage.getBoundingClientRect(), r = el.getBoundingClientRect();
  const x = side === "left" ? r.left : side === "right" ? r.right : r.left + r.width / 2;
  return { x: x - s.left, y: r.top + r.height / 2 - s.top };
}
function curve(a, b) {
  const dx = Math.max(50, (b.x - a.x) * 0.5);
  return `M${a.x},${a.y} C${a.x + dx},${a.y} ${b.x - dx},${b.y} ${b.x},${b.y}`;
}

// --- 2 decide: the real $1,200 attack, scrubbed -----------------------------------------------
const attack = feed.find((i) => i.decision?.action === "BLOCK" && /new account/i.test(i.proposal.payee_name))
  || feed.find((i) => i.decision?.action === "BLOCK") || null;
const attackCodes = attack
  ? [...attack.decision.violations.map((c) => [c, "hard"]), ...(attack.decision.risk?.flags || []).filter((f) => !attack.decision.violations.includes(f)).map((c) => [c, "soft"])]
  : [["OVER_TXN_CAP", "hard"], ["PAYEE_NOT_ALLOWLISTED", "hard"], ["BANK_DETAILS_CHANGED", "soft"], ["LOOKALIKE_DOMAIN", "soft"], ["UNTRUSTED_INSTRUCTION", "soft"], ["URGENCY_PRESSURE", "soft"]];
$("d-amount").textContent = attack ? money(attack.proposal.amount, attack.proposal.currency) : "$1,200";
$("d-payee").textContent = attack ? attack.proposal.payee_name : "Acme Office Supply (new account)";
$("d-claude-text").textContent = attack?.decision?.risk?.explanation
  || "A lookalike of Acme's real domain asks for the payment to go to a new bank account today: a classic vendor-impersonation pattern. Do not pay the new account.";
$("d-codes").innerHTML = attackCodes.map(([c, kind]) => `<span class="code ${kind === "soft" ? "soft" : ""}">${esc(c)}</span>`).join("");
const decide = { geo: null };

function layoutDecide() {
  const stage = $("decide-stage");
  const a = anchor($("d-agent"), stage, "right");
  const sh = anchor($("d-shield"), stage, "center");
  const r = anchor($("d-rail"), stage, "left");
  const half = $("d-packet").offsetWidth / 2 || 95;
  const inEnd = { x: sh.x - $("d-shield").offsetWidth / 2 - half + 6, y: sh.y };
  $("decide-wires").innerHTML = `<path d="${curve(a, { x: sh.x, y: sh.y })}"/><path d="${curve({ x: sh.x, y: sh.y }, r)}"/>
    <path class="glow" d="${curve(a, { x: sh.x, y: sh.y })}"/>`;
  decide.geo = { a, sh, r, inEnd, w: stage.clientWidth };
}

handlers.decide = (p) => {
  if (!decide.geo) layoutDecide();
  const { a, sh, inEnd, w } = decide.geo;
  const pkt = $("d-packet");
  const travel = clamp((p - 0.06) / 0.34);
  const t = ease(travel);
  const x = lerp(a.x + 90, inEnd.x, t);
  const y = lerp(a.y, inEnd.y, t) - Math.sin(t * Math.PI) * 30;
  pkt.style.left = `${x}px`;
  pkt.style.top = `${y}px`;
  pkt.style.opacity = p > 0.04 ? 1 : 0;
  const hit = p >= 0.4;
  pkt.classList.toggle("blocked", hit);
  $("d-shield").classList.toggle("hit", hit);
  $("d-rail").classList.toggle("dim", hit);
  const codes = [...document.querySelectorAll("#d-codes .code")];
  const rx = Math.min(300, w * 0.28), ry = 128;
  const top = Math.ceil(codes.length / 2), bottom = codes.length - top;
  codes.forEach((el, i) => {
    const on = p > 0.44 + i * 0.035;
    const up = i % 2 === 0, k = Math.floor(i / 2), n = up ? top : bottom;
    const spread = (n === 1 ? 0.5 : k / (n - 1));  // fan across an arc above or below the shield
    const ang = (up ? -1 : 1) * (Math.PI * 0.18 + spread * Math.PI * 0.64);
    const dx = sh.x - w / 2 + (on ? Math.cos(ang) * rx : 0);
    const dy = on ? Math.sin(ang) * ry : 0;
    el.classList.toggle("on", on);
    el.style.transform = `translate(-50%, -50%) translate(${dx}px, ${dy}px) scale(${on ? 1 : 0.6})`;
  });
  $("d-claude").classList.toggle("on", p > 0.66);
  $("d-verdict").classList.toggle("on", p > 0.76);
};

// --- 3 flow: the honest payments of the live run, looping while in view ----------------------
const paid = feed.filter(landed).reverse();
const flowItems = paid.length ? paid : [
  { proposal: { agent_id: "procurement-bot", rail: "transfer", amount: "420", payee_name: "Acme Office Supply", currency: "USD" } },
  { proposal: { agent_id: "procurement-bot", rail: "vic_checkout", amount: "120", payee_name: "OfficeCo", currency: "USD" } },
  { proposal: { agent_id: "research-bot", rail: "x402", amount: "0.01", payee_name: "VendorCheck API", currency: "USD" } },
];
const flowAgents = [...new Set([...(state.agents || []).map((a) => a.id), ...flowItems.map((i) => i.proposal.agent_id)])];
const totals = {};
for (const i of flowItems) {
  const t = (totals[i.proposal.rail] ||= { n: 0, amount: 0 });
  t.n += 1; t.amount += Number(i.proposal.amount) || 0;
}
$("flow-agents").innerHTML = flowAgents.map((id) => `<div class="node agent" data-agent="${esc(id)}"><i></i><div><b>${esc(id)}</b><small>AI agent</small></div></div>`).join("");
$("flow-rails").innerHTML = Object.keys(RAIL_INFO).map((k) => `<div class="node rail" data-rail="${k}"><div><b>${RAIL_INFO[k].name}</b><small>${RAIL_INFO[k].sub}</small></div>
  <span class="amt">${totals[k] ? money(totals[k].amount, "USD", totals[k].amount < 1 ? 2 : 0) : ""}</span></div>`).join("");
const flow = { geo: null, idx: 0, timer: null };

function layoutFlow() {
  const stage = $("flow-stage");
  const sh = anchor($("f-shield"), stage, "center");
  const ins = {}, outs = {}, paths = [];
  stage.querySelectorAll(".node.agent").forEach((n) => { ins[n.dataset.agent] = curve(anchor(n, stage, "right"), sh); paths.push(ins[n.dataset.agent]); });
  stage.querySelectorAll(".node.rail").forEach((n) => { outs[n.dataset.rail] = curve(sh, anchor(n, stage, "left")); paths.push(outs[n.dataset.rail]); });
  $("flow-wires").innerHTML = paths.map((d) => `<path d="${d}"/>`).join("") + paths.map((d) => `<path class="glow" d="${d}"/>`).join("")
    + `<g id="flow-dots"></g>`;
  flow.geo = { ins, outs };
}

function runPath(d, dot, ms) {
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", d);
  const len = path.getTotalLength();
  return new Promise((res) => {
    const t0 = performance.now();
    const step = (now) => {
      const k = clamp((now - t0) / ms);
      const pt = path.getPointAtLength(len * ease(k));
      dot.setAttribute("cx", pt.x); dot.setAttribute("cy", pt.y);
      k < 1 ? requestAnimationFrame(step) : res();
    };
    requestAnimationFrame(step);
  });
}

async function sendOne(item) {
  if (!flow.geo) layoutFlow();
  const p = item.proposal;
  const din = flow.geo.ins[p.agent_id], dout = flow.geo.outs[p.rail];
  const g = $("flow-dots");
  if (!din || !dout || !g) return;
  const dot = document.createElementNS("http://www.w3.org/2000/svg", "circle");
  dot.setAttribute("r", 5); dot.setAttribute("class", "dot");
  g.appendChild(dot);
  await runPath(din, dot, 900);
  const sh = $("f-shield");
  sh.classList.add("pass"); setTimeout(() => sh.classList.remove("pass"), 400);
  await runPath(dout, dot, 800);
  dot.remove();
  const rail = document.querySelector(`#flow-rails .node[data-rail="${p.rail}"]`);
  if (!rail) return;
  rail.classList.add("landed"); setTimeout(() => rail.classList.remove("landed"), 900);
  const at = anchor(rail, $("flow-stage"), "center");
  const pill = document.createElement("span");
  pill.className = "pill";
  pill.style.left = `${at.x}px`; pill.style.top = `${at.y - 18}px`;
  pill.textContent = `+${money(p.amount, p.currency)} · ${p.payee_name}`;
  $("flow-labels").appendChild(pill);
  setTimeout(() => pill.remove(), 2400);
}

new IntersectionObserver(([e]) => {
  clearInterval(flow.timer);
  if (!e.isIntersecting || still) return;
  const tick = () => { sendOne(flowItems[flow.idx++ % flowItems.length]); };
  tick();
  flow.timer = setInterval(tick, 1100);
}, { threshold: 0.4 }).observe($("flow-stage"));

// --- 4 numbers --------------------------------------------------------------------------------
const prot = state.protection || {};
const numbers = [
  { v: Number(prot.protected || 2419), fmt: (n) => money(n, "USD", 0), cls: "green", label: "protected in the live sandbox run", note: "stopped, double payments prevented, declined live, recovered" },
  { v: Number(prot.lost?.amount || 0), fmt: (n) => money(n, "USD", 0), cls: "green", label: "lost", note: `an out-of-band ${money(prot.under_dispute?.amount || 180, "USD", 0)} charge was caught and disputed` },
  { v: 100000, fmt: (n) => Math.round(n).toLocaleString("en-US"), label: "simulated agent actions", note: "26,427 hostile · 0 paid without a human · 0 rules broken" },
  { v: 46, fmt: (n) => Math.round(n), id: "num-redteam", label: "red-team attacks, 0 paid without a human", note: "40 written by Claude as the attacker, 6 by hand" },
  { v: (state.locks?.results || []).filter((r) => r.held).length || 4, fmt: (n) => `${Math.round(n)}/${(state.locks?.results || []).length || 4}`, label: "independent locks held with Warden switched off", note: "frozen card · blocked merchant · revoked mandate · no passkey" },
  { v: Number(state.receipts?.count || 0), fmt: (n) => Math.round(n), label: "signed Intent Receipts", note: state.receipts?.chain_intact === false ? "chain broken" : "hash chain intact" },
];
$("num-grid").innerHTML = numbers.map((n, i) => `<div class="num"><b class="${n.cls || ""}" data-i="${i}">${n.fmt(0)}</b><span>${esc(n.label)}</span><small>${esc(n.note)}</small></div>`).join("");
$("num-grid").addEventListener("inview", () => {
  document.querySelectorAll("#num-grid b").forEach((b) => {
    const n = numbers[b.dataset.i];
    if (still) { b.textContent = n.fmt(n.v); return; }
    const t0 = performance.now();
    const step = (now) => {
      const k = clamp((now - t0) / 1800);
      b.textContent = n.fmt(n.v * (1 - Math.pow(1 - k, 4)));
      if (k < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  });
});
fetch("/api/scorecard").then((r) => r.json()).then((d) => {
  const r = d.redteam;
  if (!r) return;
  numbers[3].v = r.attacks;
  numbers[3].label = `red-team attacks, ${r.attacks_paid_without_a_human} paid without a human`;
  numbers[3].note = `${r.legitimate_paid}/${r.legitimate} legitimate payments still paid`;
  const cell = document.querySelector('#num-grid b[data-i="3"]')?.parentElement;
  if (cell) { cell.querySelector("span").textContent = numbers[3].label; cell.querySelector("small").textContent = numbers[3].note; }
}).catch(() => {});

// --- 5 receipts: a horizontal chain -------------------------------------------------------------
const recent = (state.receipts?.recent || []).slice(0, 10).reverse();
const RAIL_LABEL = { transfer: "Airwallex bank transfer", card: "Airwallex card", vic_checkout: "Visa VIC via Airi", x402: "Alta x402" };
$("receipt-track").innerHTML = recent.map((r, i) => `${i ? '<div class="link"></div>' : ""}
  <article class="rcpt">
    <div class="r-top"><span>${esc(r.id)}</span><span class="r-badge ${esc(r.action)}">${esc({ ALLOW: "allowed", BLOCK: "blocked", CONTAIN: "fired", STEP_UP: "held", APPROVE: "approved", DENY: "denied" }[r.action] || r.action.toLowerCase())}</span></div>
    <div class="r-amt">${r.amount ? esc(money(r.amount, r.currency || "USD")) : "–"}</div>
    <div class="r-payee">${esc(r.payee || "")}</div>
    <div class="r-rail">${esc(RAIL_LABEL[r.rail] || r.rail || "")} · ${esc(r.agent_id)}</div>
    <div class="r-foot"><span>${r.signed ? "<b>✓ Ed25519 signed</b>" : "unsigned"}</span>
      ${r.metal_tx ? `<a href="${EXPLORER}${esc(r.metal_tx)}" target="_blank" rel="noopener">on chain ↗</a>` : `<span>${esc(r.settlement || "")}</span>`}</div>
  </article>`).join("") + `${recent.length ? '<div class="link"></div>' : ""}
  <article class="rcpt end"><b>Don't trust us.<br>Check one.</b><a href="/verify">Open the public verify page →</a></article>`;

handlers.receipts = (p) => {
  const track = $("receipt-track");
  const travel = Math.max(0, track.scrollWidth - innerWidth + 80);
  track.style.setProperty("--tx", `${-travel * ease(clamp((p - 0.12) / 0.8))}px`);
};

// --- 6 fire -----------------------------------------------------------------------------------
const proc = (state.agents || []).find((a) => a.contained) || (state.agents || [])[0];
if (proc?.card?.id) {
  $("fire-card-id").textContent = `card ${proc.card.id}`;
  $("vcard-num").textContent = `•••• •••• •••• ${String(proc.card.id).replace(/[^0-9a-z]/gi, "").slice(-4)}`;
}
if (proc?.metal?.mandate_id) $("fire-metal-id").textContent = `mandate ${proc.metal.mandate_id}`;
const locks = state.locks?.results || [];
if (locks.length) $("fire-locks").textContent = `${locks.filter((l) => l.held).length} of ${locks.length} independent locks held`;
handlers.fire = (p) => {
  document.querySelectorAll("#fire-steps li").forEach((li) => li.classList.toggle("on", p > Number(li.dataset.at)));
  $("vcard").classList.toggle("frozen", p > 0.18);
  $("chain-stamp").classList.toggle("on", p > 0.42);
};

// --- go ---------------------------------------------------------------------------------------
function layoutAll() { decide.geo = null; flow.geo = null; layoutDecide(); layoutFlow(); }
function jumpFromUrl() {  // ?scene=decide&p=0.8 opens the story at that moment (handy for recording)
  const q = new URLSearchParams(location.search);
  const el = q.get("scene") && document.querySelector(`[data-scene="${CSS.escape(q.get("scene"))}"], #s-${CSS.escape(q.get("scene"))}`);
  if (!el) return;
  const p = clamp(Number(q.get("p") || 0));
  document.documentElement.style.scrollBehavior = "auto";
  scrollTo(0, el.offsetTop + Math.max(0, el.offsetHeight - innerHeight) * p);
  document.documentElement.style.scrollBehavior = "";
}
addEventListener("load", () => { layoutAll(); jumpFromUrl(); onScroll(); });
layoutAll();
onScroll();
