// Theme: follows the system until the reader picks one with the switch; the pick is remembered.
// Loaded in <head>, before the stylesheet applies, so a stored choice never flashes the wrong theme.
// A file rather than an inline script, so the CSP needs no 'unsafe-inline'.

(() => {
  const root = document.documentElement;
  const systemDark = window.matchMedia("(prefers-color-scheme: dark)");
  const KEY = "theme";

  const read = () => {
    try {
      return localStorage.getItem(KEY);
    } catch {
      return null; // storage blocked (private window, site data off): follow the system
    }
  };
  const write = (value) => {
    try {
      if (value) localStorage.setItem(KEY, value);
      else localStorage.removeItem(KEY);
    } catch {
      /* the choice still applies for this page view */
    }
  };

  const stored = read();
  if (stored === "light" || stored === "dark") root.dataset.theme = stored;

  const current = () => root.dataset.theme || (systemDark.matches ? "dark" : "light");

  const syncButtons = () => {
    const next = current() === "dark" ? "light" : "dark";
    for (const button of document.querySelectorAll("[data-theme-toggle]")) {
      button.dataset.next = next;
      button.setAttribute("aria-label", `Switch to ${next} theme`);
      button.title = `Switch to ${next} theme`;
    }
    root.dispatchEvent(new CustomEvent("themechange", { detail: current() }));
  };

  const choose = (theme) => {
    // Picking what the system already shows means "follow the system" again.
    const systemTheme = systemDark.matches ? "dark" : "light";
    if (theme === systemTheme) {
      delete root.dataset.theme;
      write(null);
    } else {
      root.dataset.theme = theme;
      write(theme);
    }
    syncButtons();
  };

  const toggle = () => {
    const next = current() === "dark" ? "light" : "dark";
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (document.startViewTransition && !reduced) document.startViewTransition(() => choose(next));
    else choose(next);
  };

  systemDark.addEventListener("change", syncButtons);
  document.addEventListener("DOMContentLoaded", () => {
    for (const button of document.querySelectorAll("[data-theme-toggle]")) {
      button.addEventListener("click", toggle);
    }
    syncButtons();
  });

  window.driveToTextTheme = { current };
})();
