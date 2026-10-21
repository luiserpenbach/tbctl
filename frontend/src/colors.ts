// Colours for canvas drawing (uPlot, XY plots), read from the CSS custom
// properties in theme.css so charts follow the light / dark theme.
export function token(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

/** Categorical series colour for slot i (fixed order, see --series-1..8). */
export const seriesColor = (i: number): string => token(`--series-${(i % 8) + 1}`);

/** "light" | "dark" as shown, re-rendering when it changes (switch or OS). */
export { useTheme as useColorScheme } from "./theme-pref";
