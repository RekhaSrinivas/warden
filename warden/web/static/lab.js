// Warden Lab: renders docs/lab/results.json (embedded in the page) as a ladder, a matrix and the measured cards.
(() => {
  const data = JSON.parse(document.getElementById("lab-data").textContent || "null");
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  if (!data || !data.results) {
    $("ladder").innerHTML = `<p class="muted">No lab results yet. Run <code>python -m warden.lab --offline</code>.</p>`;
    return;
  }
  const designs = data.designs.filter((d) => data.results[d.id]);
  const LABEL = { paid: "Paid", held: "Held", blocked: "Blocked" };
  const SHORT = { paid: "P", held: "H", blocked: "B" };
  let set = "redteam";
  let filter = "all";
  let openRow = null;

  const NOTES = {
    redteam: "46 attacks sent the way a fooled AI agent would, through Warden's MCP gateway: 40 written by Claude playing the attacker across 20 techniques, 6 written by hand to sit inside the rules. Plus 30 honest payments, to count false alarms.",
    scripted: "10 attacks submitted as finished payment proposals carrying the evidence the agent saw. Some break no rule at all, such as a lookalike sender on an approved vendor's invoice, or an instruction hidden in an invoice PDF: only reading the evidence catches those.",
  };

  function rows() {
    if (set === "redteam") {
      return data.attacks.map((a) => ({ key: a.id, id: a.id, title: a.technique, sub: a.goal, from: a.from,
        author: a.author, kinds: a.kinds, cells: designs.map((d) => data.results[d.id].cells[a.id]) }));
    }
    return data.scripted.filter((s) => s.attack).map((s, i) => ({ key: s.name, id: `S${String(i + 1).padStart(2, "0")}`,
      title: s.name, sub: "", from: "", author: "scripted", kinds: [],
      cells: designs.map((d) => data.results[d.id].scripted.cells[s.name]) }));
  }

  function summary(d) {
    const r = data.results[d.id];
    if (set === "redteam") {
      const s = r.summary;
      return { n: s.attacks, paid: s.paid, held: s.held, blocked: s.blocked, ln: s.legitimate, lpaid: s.legit_paid,
        lheld: s.legit_held, lblocked: s.legit_blocked, ci: s.paid_rate_ci95 };
    }
    const s = r.scripted.summary;
    return { n: s.attacks, paid: s.paid, held: s.held, blocked: s.blocked, ln: s.clean, lpaid: s.clean_paid,
      lheld: s.clean_held, lblocked: s.clean_blocked };
  }

  function renderLadder() {
    const best = designs[designs.length - 1].id;
    $("ladder").innerHTML = designs.map((d) => {
      const s = summary(d);
      const pct = (k) => (s.n ? (100 * k) / s.n : 0);
      const ci = s.ci ? `<span class="cap">95% CI for the share paid: ${(s.ci[0] * 100).toFixed(1)}–${(s.ci[1] * 100).toFixed(1)}%</span>` : "";
      return `<article class="rung ${d.id === best ? "best" : ""}">
        <span class="tag">${esc(d.id)}</span>
        <span class="name">${esc(d.name)}</span>
        <span class="short">${esc(d.short)}</span>
        <div class="big ${s.paid ? "bad" : "good"}">${s.paid}<small> / ${s.n} paid</small></div>
        <div class="bar" aria-hidden="true"><i class="paid" style="width:${pct(s.paid)}%"></i><i class="held" style="width:${pct(s.held)}%"></i><i class="blocked" style="width:${pct(s.blocked)}%"></i></div>
        <span class="cap">${s.held} held for a human · ${s.blocked} blocked</span>
        ${ci}
        <span class="legit">Honest payments: <b>${s.lpaid}/${s.ln} paid</b>${s.lheld ? `, ${s.lheld} held` : ""}${s.lblocked ? `, <b>${s.lblocked} blocked</b>` : ""}</span>
      </article>`;
    }).join("");
  }

  function renderFilters() {
    if (set !== "redteam") { $("filters").innerHTML = ""; return; }
    const opts = [["all", "All"], ["claude", "Written by Claude"], ["expert", "Written by hand"],
      ["invoice", "Bank transfer"], ["card", "Card"], ["online_checkout", "Visa checkout"], ["data", "x402 data"]];
    $("filters").innerHTML = opts.map(([k, label]) => `<button class="chip" data-f="${k}" aria-pressed="${filter === k}">${label}</button>`).join("");
  }

  function keep(r) {
    if (filter === "all") return true;
    if (filter === "claude" || filter === "expert") return r.author === filter;
    return r.kinds.includes(filter);
  }

  function renderMatrix() {
    const head = `<div class="mrow head" role="row"><span class="id">#</span><span>Attack</span>${designs.map((d) =>
      `<span class="head-cell" title="${esc(d.name)}">${esc(d.id)}</span>`).join("")}</div>`;
    const body = rows().filter(keep).map((r) => `<div class="mrow ${openRow === r.key ? "open" : ""}" role="row" data-key="${esc(r.key)}" tabindex="0">
      <span class="id">${esc(r.id)}</span>
      <span class="what"><span class="tech">${esc(r.title)}</span>${r.sub ? `<div class="goal">${esc(r.sub)}</div>` : ""}</span>
      ${r.cells.map((c, i) => `<span class="cell"><span class="dot ${c.result}" title="${esc(designs[i].name)}: ${LABEL[c.result]}">${SHORT[c.result]}</span></span>`).join("")}
    </div>`).join("");
    $("matrix-grid").innerHTML = head + body;
  }

  function renderDetail() {
    const r = rows().find((x) => x.key === openRow);
    const box = $("detail");
    if (!r) { box.hidden = true; return; }
    box.hidden = false;
    box.innerHTML = `<h3>${esc(r.id)} · ${esc(r.title)}</h3>
      ${r.from ? `<p class="from">From ${esc(r.from)}${r.sub ? ` · goal: ${esc(r.sub)}` : ""}</p>` : ""}
      <div class="grid">${r.cells.map((c, i) => `<div class="d"><b>${esc(designs[i].id)} ${esc(designs[i].name)}</b>
        <span class="dot ${c.result}" style="display:inline-grid;width:auto;padding:0 8px;height:22px;margin-bottom:6px">${LABEL[c.result]}</span>
        <div class="codes">${esc((c.why || []).join(" · ") || (c.result === "paid" ? "nothing stopped it" : ""))}</div></div>`).join("")}</div>`;
  }

  function renderDesigns() {
    $("designs").innerHTML = designs.map((d) => `<div class="design"><b><span>${esc(d.id)}</span>${esc(d.name)}</b>${esc(d.detail)}</div>`).join("");
  }

  function renderMeasured() {
    const m = data.measured || {};
    const sim = m.simulation;
    const cards = [];
    if (sim) {
      cards.push([sim.hostile_auto_paid, `of ${sim.hostile.toLocaleString()} hostile actions paid without a human`,
        `${sim.actions.toLocaleString()} random agent actions across ${sim.worlds.toLocaleString()} simulated companies, rules checked after every action. 95% upper bound on the miss rate: ${(sim.upper95 * 100).toFixed(3)}%.`]);
      cards.push([sim.sabotage_violations.toLocaleString(), "rule breaks caught when the mandate check is sabotaged",
        `The same simulation with Warden's mandate check switched off: ${(sim.sabotage_hostile_paid ?? 0).toLocaleString()} hostile requests get paid, and the checker reports every broken rule. So the test can fail.`, "warn"]);
    }
    cards.push(["0.1 ms", "for Warden's live card authorization",
      "Airwallex asks Warden before each card charge (remote authorization). No Intent Receipt, no authorization: a copied-card charge was declined live with REMOTE_AUTH_DECLINED."]);
    cards.push(["4 / 4", "independent locks held with Warden off",
      "Live: frozen card (CARD_INACTIVE), blocked merchant category, revoked Alta mandate on chain, Airi checkout nobody confirmed (NO_PASSKEY)."]);
    cards.push(["$0", "lost in the live run",
      "$2,419 protected, $1,210 paid within mandate in 8 payments on Airwallex, Visa via Airi and Alta. 39 signed receipts; the chain verifies with only the public key."]);
    $("measured-cards").innerHTML = cards.map(([num, what, text, cls]) =>
      `<div class="card"><div class="num ${cls || ""}">${esc(num)}</div><div class="what">${esc(what)}</div><p>${esc(text)}</p></div>`).join("");
    $("scored-at").textContent = `Scored ${data.scored_at}${data.results.B4 ? ", B4 with Claude as the risk reader" : " (B4 not scored in this run)"}.`;
  }

  function render() {
    $("set-note").textContent = NOTES[set];
    renderLadder();
    renderFilters();
    renderMatrix();
    renderDetail();
  }

  document.querySelectorAll(".set-switch button").forEach((b) => b.addEventListener("click", () => {
    set = b.dataset.set;
    filter = "all";
    openRow = null;
    document.querySelectorAll(".set-switch button").forEach((x) => x.setAttribute("aria-selected", String(x === b)));
    render();
  }));
  $("filters").addEventListener("click", (e) => {
    const b = e.target.closest(".chip");
    if (!b) return;
    filter = b.dataset.f;
    render();
  });
  const toggleRow = (e) => {
    const row = e.target.closest(".mrow:not(.head)");
    if (!row) return;
    if (e.type === "keydown" && e.key !== "Enter" && e.key !== " ") return;
    e.preventDefault();
    openRow = openRow === row.dataset.key ? null : row.dataset.key;
    renderMatrix();
    renderDetail();
    if (openRow) $("detail").scrollIntoView({ block: "nearest", behavior: "smooth" });
  };
  $("matrix-grid").addEventListener("click", toggleRow);
  $("matrix-grid").addEventListener("keydown", toggleRow);

  renderDesigns();
  renderMeasured();
  render();
})();
