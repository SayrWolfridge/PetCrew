import { invoke, isTauri } from "@tauri-apps/api/core";

export type TextSize = "normal" | "large" | "extra_large";
export type CardLayout = "list" | "tiles";
export type AppTheme = "dark" | "light";

export interface WindowPlacement {
  x: number;
  y: number;
  width: number;
  height: number;
  monitor: string | null;
}

export interface Preferences {
  text_size: TextSize;
  card_layout: CardLayout;
  theme: AppTheme;
  recent_completed_limit: number;
}

export interface AppSettings extends Preferences {
  schema_version: number;
  window: WindowPlacement | null;
}

export const DEFAULT_PREFERENCES: Preferences = {
  text_size: "large",
  card_layout: "list",
  theme: "dark",
  recent_completed_limit: 10,
};

export const DEFAULT_SETTINGS: AppSettings = {
  schema_version: 1,
  ...DEFAULT_PREFERENCES,
  window: null,
};

export async function getAppSettings(): Promise<AppSettings> {
  if (!isTauri()) return DEFAULT_SETTINGS;
  return invoke<AppSettings>("get_app_settings");
}

export async function updateAppPreferences(preferences: Preferences): Promise<AppSettings> {
  if (!isTauri()) return { ...DEFAULT_SETTINGS, ...preferences };
  return invoke<AppSettings>("update_app_preferences", { preferences });
}

export async function updateWindowPlacement(window: WindowPlacement): Promise<AppSettings> {
  if (!isTauri()) return { ...DEFAULT_SETTINGS, window };
  return invoke<AppSettings>("update_window_placement", { window });
}
