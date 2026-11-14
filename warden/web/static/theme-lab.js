// Temporary colour-theme picker (a bar under the top navigation). Pick a colour; the moon/sun toggle (or the
// Light/Dark button here) switches that colour between its light and dark versions. Remove once a theme is chosen.
(function () {
  "use strict";
  const THEMES = [
    ["emerald", "Emerald (now)", "#2bd4a4"],
    ["teal", "Teal", "#2dd4bf"],
    ["cyan", "Cyan", "#38bdf8"],
    ["blue", "Blue", "#60a5fa"],
    ["indigo", "Indigo", "#818cf8"],
    ["violet", "Violet", "#a78bfa"],
    ["pink", "Pink", "#f472b6"],
    ["rose", "Rose", "#fb7185"],
    ["red", "Red", "#f87171"],
    ["orange", "Orange", "#fb923c"],
    ["gold", "Gold", "#fbbf24"],
    ["lime", "Lime", "#a3e635"],
    ["graphite", "Graphite", "#e5e7eb"]
  ];
  const root = document.documentElement;
  let pick = "emerald";
  try { pick = localStorage.getItem("warden-palette") || "emerald"; } catch {}
  if (!THEMES.some((t) => t[0] === pick)) pick = "emerald";
  const q = new URLSearchParams(location.search);
  if (THEMES.some((t) => t[0] === q.get("palette"))) pick = q.get("palette");
  if (q.get("mode") === "light" || q.get("mode") === "dark") {
    root.dataset.theme = q.get("mode");
    try { localStorage.setItem("warden-theme", q.get("mode")); } catch {}
  }
  const FX = [["grid", "Grid", "A faint circuit grid behind everything"],
              ["gradient", "Gradient", "Two-tone gradient on headline words, key numbers and main buttons"],
              ["glass", "Glass", "See-through frosted cards with a glowing top edge"]];
  let fx = [];
  try { fx = (localStorage.getItem("warden-fx") || "").split(" ").filter(Boolean); } catch {}
  if (q.get("fx") !== null) fx = q.get("fx").split(/[ ,]+/).filter((f) => FX.some((x) => x[0] === f));
  function setFx() {
    if (fx.length) root.dataset.fx = fx.join(" "); else delete root.dataset.fx;
    try { localStorage.setItem("warden-fx", fx.join(" ")); } catch {}
    refresh();
  }
  function toggleFx(f) { fx = fx.includes(f) ? fx.filter((x) => x !== f) : [...fx, f]; setFx(); }
  function refresh() {
    document.querySelectorAll(".theme-lab .tl-fx").forEach((b) => b.setAttribute("aria-pressed", String(fx.includes(b.dataset.fx))));
    document.querySelectorAll(".theme-lab button[data-p]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.p === pick)));
    const m = document.querySelector(".theme-lab .tl-mode");
    if (m) m.textContent = root.dataset.theme === "light" ? "☀ Light · switch to dark" : "☾ Dark · switch to light";
  }
  function apply(name) {
    pick = name;
    if (name === "emerald") delete root.dataset.palette; else root.dataset.palette = name;
    try { localStorage.setItem("warden-palette", name); } catch {}
    refresh();
  }
  apply(pick);
  setFx();
  document.addEventListener("DOMContentLoaded", () => {
    const bar = document.createElement("div");
    bar.className = "theme-lab";
    bar.innerHTML = "<b>Colour</b>" + THEMES.map((t) =>
      `<button type="button" data-p="${t[0]}"><i style="background:${t[2]}"></i>${t[1]}</button>`).join("") +
      '<span class="tl-sep"></span><b>Style</b>' + FX.map((f) => `<button type="button" class="tl-fx" data-fx="${f[0]}" title="${f[2]}">${f[1]}</button>`).join("") +
      '<span class="tl-sep"></span><button type="button" class="tl-mode"></button>' +
      '<button type="button" class="tl-hide" title="Hide this bar">Hide</button><button type="button" class="tl-show">Themes</button>';
    document.body.appendChild(bar);
    bar.addEventListener("click", (e) => {
      const b = e.target.closest("button");
      if (!b) return;
      if (b.classList.contains("tl-hide") || b.classList.contains("tl-show")) { bar.classList.toggle("min"); return; }
      if (b.dataset.fx) { toggleFx(b.dataset.fx); return; }
      if (b.classList.contains("tl-mode")) {
        if (window.wardenTheme) window.wardenTheme.toggle();
        else root.dataset.theme = root.dataset.theme === "light" ? "dark" : "light";
        setTimeout(refresh, 0);
        return;
      }
      apply(b.dataset.p);
    });
    refresh();
  });
  window.addEventListener("themechange", () => setTimeout(refresh, 0));
})();
