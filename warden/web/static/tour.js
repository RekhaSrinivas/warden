// Warden console: a guided tour (coach marks), so a first-time viewer knows what each panel is for.
// Start it with the Tour button in the header, or open /console?tour=1. Add &step=N to open at step N
// (counted in STEPS below, 1-based), which is handy for re-takes. Nothing is remembered between visits.
// app.js redraws panels from /api/state at any moment, so the target is looked up by selector on every
// frame and the ring and card follow it. A step whose target is missing or hidden is skipped.
(function () {
  "use strict";

  const STEPS = [
    { section: "Overview", sel: "#flow-stage", title: "Every payment passes Warden first",
      text: "AI agents on the left, money rails on the right. Before an agent can pay anyone, the payment stops at Warden. Each pulse is a real decision: paid, held for a human, or stopped." },
    { section: "Overview", sel: "#protect", title: "Money protected, by outcome",
      text: "What was stopped, double payments prevented, card charges declined in real time, payments held for a human, and what was paid within mandate. The headline total above shows $0 lost." },
    { section: "Overview", sel: "#rails", title: "Three real money rails",
      text: "Airwallex for bank transfers and issued cards, Visa Intelligent Commerce through Airi for agent checkout, and Alta for x402 pay-per-call with on-chain mandates." },
    { section: "Hire it", sel: ".hire-panel", title: "Hire an agent in plain English",
      text: "Describe the job. Claude drafts the mandate, code checks it against company policy, and confirming issues a real card with exactly the same limits." },
    { section: "Hire it", sel: "#agents", title: "Every agent, and its off switch",
      text: "Each agent has a trust score, a weekly budget, its own card and an Alta mandate. One click on Fire agent stops it on every rail at once: card frozen, Alta mandate revoked, Airi purchases off." },
    { section: "Guard it", sel: ".panel.controls", title: "Try to fool an agent",
      text: "Run a normal day and honest spending goes through. Then send the agent a scam email, or use its card outside Warden, and watch what happens." },
    { section: "Guard it", sel: "#feed", title: "Every decision, explained",
      text: "Each payment is allowed, held for a human, blocked, or the agent is contained. Rule codes show which limit tripped; Claude's note explains how it read the evidence." },
    { section: "Guard it", sel: "#inbox", up: ".panel", title: "Approval inbox",
      text: "When a payment needs judgment, a human approves that exact payment. If the agent changes it by even $1, the approval hash no longer matches and nothing is paid." },
    { section: "Guard it", sel: "#authorizations", up: ".panel", title: "The card network asks Warden",
      text: "With Airwallex remote authorization on, Airwallex asks Warden before every card charge. No Intent Receipt, no authorization. The answer comes back in under a millisecond." },
    { section: "Guard it", sel: ".mcp-panel", title: "Real agents, over MCP",
      text: "Claude Code or any MCP agent can work as a hired agent. It connects with its own token and gets Warden's tools, never a card number, key or credential." },
    { section: "Prove it", sel: "#prove", title: "Signed proof for every decision",
      text: "Every decision gets an Ed25519-signed Intent Receipt, chained by hash to the one before. The public verify page checks them without trusting Warden." },
    { section: "Recover", sel: "#recover", title: "Every card charge needs a receipt",
      text: "Reconciliation matches each card charge to an Intent Receipt. A charge without one becomes a case and an Airwallex dispute, counted as recovered only when the dispute is won." },
    { section: "Independent locks", sel: "#locks", title: "Four locks that hold without Warden",
      text: "Switch Warden off and the rails still refuse on their own: the frozen card, Airwallex merchant categories, the Alta on-chain mandate and Airi's passkey." },
  ];

  const PAD = 8;      // ring padding around the target
  const GAP = 14;     // between the ring and the card
  const EDGE = 12;    // the card never comes closer than this to the viewport edge
  const CARD_W = 360;

  // Where the card goes: below, above, right or left of the ring (the first side where it fits whole),
  // slid along that side to stay on screen. If no side fits (a target taller than a phone screen), it
  // sits at the bottom edge of the viewport, over the target. t: ring rect in viewport px; view, size: {w, h}.
  function placeCard(t, view, size) {
    const clamp = (v, lo, hi) => Math.max(lo, Math.min(v, hi));
    const xIn = (x) => clamp(x, EDGE, Math.max(EDGE, view.w - size.w - EDGE));
    const yIn = (y) => clamp(y, EDGE, Math.max(EDGE, view.h - size.h - EDGE));
    const cx = (t.left + t.right) / 2;
    const cy = (t.top + t.bottom) / 2;
    const room = {
      bottom: view.h - EDGE - t.bottom - GAP >= size.h,
      top: t.top - GAP - EDGE >= size.h,
      right: view.w - EDGE - t.right - GAP >= size.w,
      left: t.left - GAP - EDGE >= size.w,
    };
    let p;
    if (room.bottom) p = { side: "bottom", top: t.bottom + GAP, left: xIn(cx - size.w / 2) };
    else if (room.top) p = { side: "top", top: t.top - GAP - size.h, left: xIn(cx - size.w / 2) };
    else if (room.right) p = { side: "right", left: t.right + GAP, top: yIn(cy - size.h / 2) };
    else if (room.left) p = { side: "left", left: t.left - GAP - size.w, top: yIn(cy - size.h / 2) };
    else {  // bottom edge, so a tall panel keeps its heading; top edge only if the panel starts low
      const top = t.top > view.h / 2 ? EDGE : view.h - size.h - EDGE;
      return { side: "over", top: yIn(top), left: xIn(cx - size.w / 2), arrow: 0 };
    }
    // the little arrow points at the target's middle, kept off the card's rounded corners
    p.arrow = p.side === "bottom" || p.side === "top" ? clamp(cx - p.left, 22, size.w - 22) : clamp(cy - p.top, 22, size.h - 22);
    return p;
  }

  if (typeof document === "undefined") {  // loaded by a test runner: only the layout maths
    if (typeof module === "object" && module.exports) module.exports = { placeCard, STEPS, EDGE, GAP };
    return;
  }

  let active = false;
  let at = 0;             // index into STEPS
  let ring = null;
  let card = null;
  let raf = 0;
  let moving = null;      // while scrolling to a new step: {goal, t0, lastY, still}
  let glideUntil = 0;
  let lastLayout = "";
  let returnFocus = null;

  // The page's own motion rule (app.js): the Motion toggle if the viewer used it, else the OS setting.
  function reduced() {
    const m = document.body.dataset.motion;
    if (m) return m === "off";
    try {
      const saved = localStorage.getItem("warden-motion");
      if (saved) return saved === "off";
    } catch { /* storage blocked: OS setting */ }
    return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  }

  function targetOf(step) {
    let el = document.querySelector(step.sel);
    if (el && step.up) el = el.closest(step.up);
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return r.width && r.height ? el : null;  // display:none or empty: skip
  }

  function find(k, dir) {  // nearest step from k in direction dir whose target is on the page
    for (; k >= 0 && k < STEPS.length; k += dir) if (targetOf(STEPS[k])) return k;
    return -1;
  }

  function build() {
    ring = document.createElement("div");
    ring.className = "tour-ring";
    ring.setAttribute("aria-hidden", "true");
    ring.hidden = true;
    card = document.createElement("div");
    card.className = "tour-card";
    card.setAttribute("role", "dialog");
    card.setAttribute("aria-labelledby", "tour-title");
    card.setAttribute("aria-describedby", "tour-text");
    card.tabIndex = -1;
    card.hidden = true;
    card.innerHTML = `
      <span class="tour-arrow" aria-hidden="true"></span>
      <button class="tour-x" type="button" aria-label="Close tour">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18"/></svg>
      </button>
      <div class="tour-body" aria-live="polite">
        <div class="tour-meta"><span class="tour-section"></span><span class="tour-dot" aria-hidden="true">·</span><span class="tour-count"></span></div>
        <h2 class="tour-title" id="tour-title"></h2>
        <p class="tour-text" id="tour-text"></p>
      </div>
      <div class="tour-nav">
        <span class="tour-keys" aria-hidden="true"><kbd>←</kbd><kbd>→</kbd> move · <kbd>Esc</kbd> close</span>
        <button class="tour-back" type="button">Back</button>
        <button class="tour-next" type="button">Next</button>
      </div>`;
    card.querySelector(".tour-x").addEventListener("click", close);
    card.querySelector(".tour-back").addEventListener("click", back);
    card.querySelector(".tour-next").addEventListener("click", next);
    document.body.append(ring, card);
  }

  function stickyTop() {  // the sticky story nav covers the top of the viewport once scrolled
    const nav = document.querySelector(".story");
    return nav && getComputedStyle(nav).position === "sticky" ? nav.offsetHeight : 0;
  }

  function cardSize() {
    const w = Math.min(CARD_W, document.documentElement.clientWidth - 2 * EDGE);
    if (card.style.width !== `${w}px`) card.style.width = `${w}px`;
    return { w, h: card.offsetHeight };
  }

  // The scroll position that shows the target (and room for the card), or the current one if it
  // already shows. Targets taller than the screen are aligned to the top, under the sticky nav.
  function scrollGoal(el, size) {
    const r = el.getBoundingClientRect();
    const vw = document.documentElement.clientWidth;
    const vh = window.innerHeight;
    const top = stickyTop() + EDGE;
    const bottom = vh - EDGE;
    const ringTop = r.top - PAD;
    const ringBottom = r.bottom + PAD;
    const beside = vw - EDGE - (r.right + PAD) - GAP >= size.w || r.left - PAD - GAP - EDGE >= size.w;
    const cardFits = beside || ringTop - GAP - EDGE >= size.h || bottom - ringBottom - GAP >= size.h;
    if (ringTop >= top && ringBottom <= bottom && cardFits) return window.scrollY;
    const need = (ringBottom - ringTop) + (beside ? 0 : GAP + size.h);
    const y = window.scrollY + ringTop - top - Math.max(0, (bottom - top - need) / 2);
    const max = document.documentElement.scrollHeight - vh;
    return Math.round(Math.max(0, Math.min(y, max)));
  }

  function layout(el) {
    const vw = document.documentElement.clientWidth;
    const vh = window.innerHeight;
    const r = el.getBoundingClientRect();
    const t = {  // the ring: the target plus padding, clipped to the viewport so a tall one still shows
      top: Math.max(r.top - PAD, 3), left: Math.max(r.left - PAD, 3),
      bottom: Math.min(r.bottom + PAD, vh - 3), right: Math.min(r.right + PAD, vw - 3),
    };
    const off = t.bottom - t.top < 4 || t.right - t.left < 4;
    const size = cardSize();
    const p = placeCard(off ? { top: vh / 2, bottom: vh / 2, left: vw / 2, right: vw / 2 } : t, { w: vw, h: vh }, size);
    const key = [t.top, t.left, t.bottom, t.right, off, p.top, p.left, p.side, p.arrow].map((v) => (typeof v === "number" ? Math.round(v) : v)).join();
    if (key === lastLayout) return;
    lastLayout = key;
    ring.classList.toggle("is-off", off);
    Object.assign(ring.style, off
      ? { top: `${vh / 2}px`, left: `${vw / 2}px`, width: "0px", height: "0px" }
      : { top: `${t.top}px`, left: `${t.left}px`, width: `${t.right - t.left}px`, height: `${t.bottom - t.top}px` });
    card.dataset.side = p.side;
    card.style.top = `${Math.round(p.top)}px`;
    card.style.left = `${Math.round(p.left)}px`;
    card.style.setProperty("--tour-arrow", `${Math.round(p.arrow)}px`);
  }

  function frame(now) {
    raf = requestAnimationFrame(frame);
    const el = targetOf(STEPS[at]);
    if (!el) {  // the target went away (hidden or emptied): move on rather than point at nothing
      const k = find(at + 1, 1) >= 0 ? find(at + 1, 1) : find(at - 1, -1);
      if (k < 0) close();
      else show(k);
      return;
    }
    layout(el);
    if (moving) {
      const y = window.scrollY;
      moving.still = y === moving.lastY ? moving.still + 1 : 0;
      moving.lastY = y;
      if (Math.abs(y - moving.goal) < 2 || moving.still > 10 || now - moving.t0 > 1400) reveal();
    } else if (glideUntil && now > glideUntil) {
      glideUntil = 0;
      ring.classList.remove("glide");
    }
  }

  function reveal() {
    moving = null;
    glideUntil = performance.now() + 340;  // let the ring finish gliding, then track exactly
    card.classList.remove("is-moving");
  }

  function show(k, first = false) {
    at = k;
    const step = STEPS[k];
    const avail = STEPS.map((s, i) => i).filter((i) => targetOf(STEPS[i]));
    card.querySelector(".tour-section").textContent = step.section;
    card.querySelector(".tour-count").textContent = `${avail.indexOf(k) + 1} of ${avail.length}`;
    card.querySelector(".tour-title").textContent = step.title;
    card.querySelector(".tour-text").textContent = step.text;
    card.querySelector(".tour-back").style.visibility = k === avail[0] ? "hidden" : "";
    card.querySelector(".tour-next").textContent = k === avail[avail.length - 1] ? "Done" : "Next";
    const still = reduced();
    ring.classList.toggle("tour-still", still);
    card.classList.toggle("tour-still", still);
    ring.classList.toggle("glide", !still && !first);  // the first step appears in place, later ones glide
    card.classList.add("is-moving");
    lastLayout = "";
    const goal = scrollGoal(targetOf(step), cardSize());
    moving = { goal, t0: performance.now(), lastY: window.scrollY, still: 0 };
    if (Math.abs(goal - window.scrollY) >= 1) window.scrollTo({ top: goal, behavior: still ? "auto" : "smooth" });
  }

  function start(k) {
    if (!ring) build();
    const first = find(k, 1) >= 0 ? find(k, 1) : find(k, -1);
    if (first < 0) return;
    const opening = !active;
    if (opening) {
      active = true;
      returnFocus = document.activeElement;
      ring.hidden = false;
      card.hidden = false;
      raf = requestAnimationFrame(frame);
    }
    show(first, opening);
    document.getElementById("btn-tour")?.setAttribute("aria-expanded", "true");
    card.focus({ preventScroll: true });
  }

  function next() {
    const k = find(at + 1, 1);
    if (k < 0) close();
    else show(k);
  }

  function back() {
    const k = find(at - 1, -1);
    if (k >= 0) show(k);
  }

  function close() {
    if (!active) return;
    active = false;
    cancelAnimationFrame(raf);
    moving = null;
    glideUntil = 0;
    ring.hidden = true;
    card.hidden = true;
    ring.classList.remove("glide");
    document.getElementById("btn-tour")?.setAttribute("aria-expanded", "false");
    if (returnFocus && returnFocus.isConnected && typeof returnFocus.focus === "function") returnFocus.focus({ preventScroll: true });
    returnFocus = null;
  }

  document.addEventListener("keydown", (e) => {
    if (!active || e.altKey || e.ctrlKey || e.metaKey) return;
    if (e.key === "Escape") { e.preventDefault(); close(); return; }
    const el = e.target instanceof Element ? e.target : null;
    if (el && el.closest("input, textarea, select, [contenteditable]")) return;  // the viewer is typing
    if (e.key === "ArrowRight") { e.preventDefault(); next(); }
    else if (e.key === "ArrowLeft") { e.preventDefault(); back(); }
    else if (e.key === "Enter") {
      if (el && el.closest("button, a, summary")) return;  // Enter on a button presses that button
      e.preventDefault();
      next();
    }
  });

  const button = document.getElementById("btn-tour");
  if (button) button.addEventListener("click", () => start(0));

  // ?tour=1 starts the tour; &step=N opens step N. Starts once the page has laid out (fonts, panels).
  const q = new URLSearchParams(window.location.search);
  if (q.has("tour") && !/^(0|false|no|off)$/i.test(q.get("tour") || "")) {
    const n = parseInt(q.get("step") || "", 10);
    const k = Number.isFinite(n) ? Math.min(Math.max(n, 1), STEPS.length) - 1 : 0;
    if ("scrollRestoration" in history) history.scrollRestoration = "manual";  // the tour picks the scroll
    let started = false;
    const go = () => { if (!started) { started = true; start(k); } };
    if (document.readyState === "complete") setTimeout(go, 50);
    else {
      window.addEventListener("load", () => setTimeout(go, 50), { once: true });
      setTimeout(go, 1500);  // a slow font server should not hold the tour back
    }
  }

  window.wardenTour = { start: (n = 1) => start(Math.max(0, Math.min(STEPS.length, n) - 1)), close };
})();
