// Shared light/dark theme for every Warden page. Loaded in <head> so the saved theme is set
// before the first paint (no flash). Dark is the default; the choice is remembered per browser.
(function () {
  "use strict";
  const KEY = "warden-theme";
  const root = document.documentElement;
  let theme = "dark";
  try { theme = localStorage.getItem(KEY) || "dark"; } catch { /* storage blocked: dark */ }
  root.dataset.theme = theme === "light" ? "light" : "dark";

  function label(btn) {
    const next = root.dataset.theme === "dark" ? "light" : "dark";
    btn.setAttribute("aria-label", `Switch to ${next} theme`);
    btn.setAttribute("aria-pressed", String(root.dataset.theme === "light"));
    btn.title = `Switch to ${next} theme (T)`;
  }

  function set(next) {
    root.classList.add("theme-switching");  // a short cross-fade while colours change
    root.dataset.theme = next;
    try { localStorage.setItem(KEY, next); } catch { /* not remembered */ }
    document.querySelectorAll(".theme-toggle").forEach(label);
    window.dispatchEvent(new CustomEvent("themechange", { detail: next }));
    setTimeout(() => root.classList.remove("theme-switching"), 450);
  }

  const toggle = () => set(root.dataset.theme === "dark" ? "light" : "dark");
  window.wardenTheme = { toggle, get: () => root.dataset.theme };

  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll(".theme-toggle").forEach((btn) => {
      label(btn);
      btn.addEventListener("click", toggle);
    });
  });
  document.addEventListener("keydown", (e) => {  // T switches, unless the person is typing
    if (e.key.toLowerCase() !== "t" || e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.target.closest("input, textarea, select, [contenteditable]")) return;
    toggle();
  });
})();
