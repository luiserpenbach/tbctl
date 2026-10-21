// Theme preference: "system" follows the OS, "light" / "dark" pin it. The
// resolved theme lives on <html data-theme>, which theme.css keys off; the
// same logic runs inline in index.html before first paint (no flash).
import { useEffect, useState } from "react";

export type ThemePref = "system" | "light" | "dark";
export type Theme = "light" | "dark";

const KEY = "tbctl.theme";
const mq = window.matchMedia("(prefers-color-scheme: light)");

export function getThemePref(): ThemePref {
  try {
    const v = localStorage.getItem(KEY);
    if (v === "light" || v === "dark" || v === "system") return v;
  } catch {
    // storage blocked (private window): fall back to the OS
  }
  return "system";
}

const resolve = (pref: ThemePref): Theme =>
  pref === "system" ? (mq.matches ? "light" : "dark") : pref;

function apply(pref: ThemePref) {
  document.documentElement.dataset.theme = resolve(pref);
}

export function setThemePref(pref: ThemePref) {
  try {
    localStorage.setItem(KEY, pref);
  } catch {
    // not persisted; still applied for this page
  }
  apply(pref);
}

// keep following the OS while the preference is "system"
mq.addEventListener("change", () => {
  if (getThemePref() === "system") apply("system");
});
apply(getThemePref());

/** The theme currently shown, re-rendering whenever it changes. */
export function useTheme(): Theme {
  const read = (): Theme => (document.documentElement.dataset.theme === "light" ? "light" : "dark");
  const [theme, setTheme] = useState<Theme>(read);
  useEffect(() => {
    const obs = new MutationObserver(() => setTheme(read()));
    obs.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    return () => obs.disconnect();
  }, []);
  return theme;
}
