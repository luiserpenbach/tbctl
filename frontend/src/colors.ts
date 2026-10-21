// Colours for canvas drawing (uPlot, XY plots), read from the CSS custom
// properties in theme.css so charts follow the light / dark theme.
import { useEffect, useState } from "react";

export function token(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

/** Categorical series colour for slot i (fixed order, see --series-1..8). */
export const seriesColor = (i: number): string => token(`--series-${(i % 8) + 1}`);

/** "light" | "dark", re-rendering when the OS theme changes. */
export function useColorScheme(): "light" | "dark" {
  const query = "(prefers-color-scheme: light)";
  const [scheme, setScheme] = useState<"light" | "dark">(() =>
    window.matchMedia(query).matches ? "light" : "dark",
  );
  useEffect(() => {
    const mq = window.matchMedia(query);
    const onChange = () => setScheme(mq.matches ? "light" : "dark");
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);
  return scheme;
}
