use serde::{Deserialize, Serialize};
use std::{
    fs,
    path::{Path, PathBuf},
    sync::Mutex,
};

#[cfg(feature = "desktop")]
use tauri::{Manager, State as TauriState};

const SETTINGS_SCHEMA_VERSION: u32 = 1;

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum TextSize {
    Normal,
    Large,
    ExtraLarge,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum CardLayout {
    List,
    Tiles,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum AppTheme {
    Dark,
    Light,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
pub struct WindowPlacement {
    pub x: i32,
    pub y: i32,
    pub width: u32,
    pub height: u32,
    pub monitor: Option<String>,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
pub struct Preferences {
    pub text_size: TextSize,
    pub card_layout: CardLayout,
    pub theme: AppTheme,
    pub recent_completed_limit: u32,
}

impl Default for Preferences {
    fn default() -> Self {
        Self {
            text_size: TextSize::Large,
            card_layout: CardLayout::List,
            theme: AppTheme::Dark,
            recent_completed_limit: 10,
        }
    }
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
pub struct AppSettings {
    pub schema_version: u32,
    #[serde(flatten)]
    pub preferences: Preferences,
    pub window: Option<WindowPlacement>,
}

impl Default for AppSettings {
    fn default() -> Self {
        Self {
            schema_version: SETTINGS_SCHEMA_VERSION,
            preferences: Preferences::default(),
            window: None,
        }
    }
}

impl AppSettings {
    fn validated(mut self) -> Self {
        if self.schema_version != SETTINGS_SCHEMA_VERSION {
            return Self::default();
        }
        if !matches!(
            self.preferences.recent_completed_limit,
            0 | 5 | 10 | 20 | 50
        ) {
            self.preferences.recent_completed_limit = Preferences::default().recent_completed_limit;
        }
        self.window = self.window.filter(valid_window);
        self
    }
}

fn valid_window(window: &WindowPlacement) -> bool {
    (390..=4000).contains(&window.width)
        && (620..=4000).contains(&window.height)
        && (-100_000..=100_000).contains(&window.x)
        && (-100_000..=100_000).contains(&window.y)
        && window.monitor.as_ref().is_none_or(|name| name.len() <= 256)
}

fn load(path: &Path) -> AppSettings {
    fs::read(path)
        .ok()
        .and_then(|bytes| serde_json::from_slice::<AppSettings>(&bytes).ok())
        .map(AppSettings::validated)
        .unwrap_or_default()
}

fn save_atomic(path: &Path, settings: &AppSettings) -> Result<(), String> {
    let parent = path.parent().ok_or("settings_path_has_no_parent")?;
    fs::create_dir_all(parent).map_err(|error| error.to_string())?;
    let temporary = parent.join("settings.json.tmp");
    let backup = parent.join("settings.json.bak");
    fs::write(
        &temporary,
        serde_json::to_vec_pretty(settings).map_err(|error| error.to_string())?,
    )
    .map_err(|error| error.to_string())?;

    let _ = fs::remove_file(&backup);
    if path.exists() {
        fs::rename(path, &backup).map_err(|error| error.to_string())?;
    }
    if let Err(error) = fs::rename(&temporary, path) {
        if backup.exists() {
            let _ = fs::rename(&backup, path);
        }
        let _ = fs::remove_file(&temporary);
        return Err(error.to_string());
    }
    let _ = fs::remove_file(&backup);
    Ok(())
}

#[cfg(feature = "desktop")]
pub struct SettingsRuntime {
    path: PathBuf,
    settings: Mutex<AppSettings>,
}

#[cfg(feature = "desktop")]
pub fn setup(app: &mut tauri::App) -> Result<(), Box<dyn std::error::Error>> {
    let path = app.path().app_local_data_dir()?.join("settings.json");
    app.manage(SettingsRuntime {
        settings: Mutex::new(load(&path)),
        path,
    });
    Ok(())
}

#[cfg(feature = "desktop")]
#[tauri::command]
pub fn get_app_settings(state: TauriState<'_, SettingsRuntime>) -> Result<AppSettings, String> {
    state
        .settings
        .lock()
        .map(|settings| settings.clone())
        .map_err(|_| "settings_lock_poisoned".to_string())
}

#[cfg(feature = "desktop")]
#[tauri::command]
pub fn update_app_preferences(
    preferences: Preferences,
    state: TauriState<'_, SettingsRuntime>,
) -> Result<AppSettings, String> {
    let mut settings = state
        .settings
        .lock()
        .map_err(|_| "settings_lock_poisoned".to_string())?;
    let next = AppSettings {
        preferences,
        ..settings.clone()
    }
    .validated();
    save_atomic(&state.path, &next)?;
    *settings = next.clone();
    Ok(next)
}

#[cfg(feature = "desktop")]
#[tauri::command]
pub fn update_window_placement(
    window: WindowPlacement,
    state: TauriState<'_, SettingsRuntime>,
) -> Result<AppSettings, String> {
    if !valid_window(&window) {
        return Err("invalid_window_placement".to_string());
    }
    let mut settings = state
        .settings
        .lock()
        .map_err(|_| "settings_lock_poisoned".to_string())?;
    let next = AppSettings {
        window: Some(window),
        ..settings.clone()
    };
    save_atomic(&state.path, &next)?;
    *settings = next.clone();
    Ok(next)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn temp_path(name: &str) -> PathBuf {
        let suffix = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("clock")
            .as_nanos();
        std::env::temp_dir().join(format!("petcrew-settings-{name}-{suffix}.json"))
    }

    #[test]
    fn defaults_are_large_list_and_dark() {
        let settings = AppSettings::default();
        assert_eq!(settings.preferences.text_size, TextSize::Large);
        assert_eq!(settings.preferences.card_layout, CardLayout::List);
        assert_eq!(settings.preferences.theme, AppTheme::Dark);
        assert_eq!(settings.preferences.recent_completed_limit, 10);
    }

    #[test]
    fn invalid_file_falls_back_to_defaults() {
        let path = temp_path("invalid");
        fs::write(&path, br#"{"schema_version":1,"theme":"neon"}"#).expect("write");
        assert_eq!(load(&path), AppSettings::default());
        let _ = fs::remove_file(path);
    }

    #[test]
    fn save_and_reload_preserves_preferences_and_window() {
        let path = temp_path("roundtrip");
        let settings = AppSettings {
            schema_version: SETTINGS_SCHEMA_VERSION,
            preferences: Preferences {
                text_size: TextSize::ExtraLarge,
                card_layout: CardLayout::Tiles,
                theme: AppTheme::Light,
                recent_completed_limit: 20,
            },
            window: Some(WindowPlacement {
                x: 2100,
                y: 20,
                width: 520,
                height: 760,
                monitor: Some("DISPLAY2".to_string()),
            }),
        };
        save_atomic(&path, &settings).expect("save");
        assert_eq!(load(&path), settings);
        let _ = fs::remove_file(path);
    }

    #[test]
    fn invalid_window_is_dropped_without_losing_preferences() {
        let mut settings = AppSettings::default();
        settings.preferences.theme = AppTheme::Light;
        settings.window = Some(WindowPlacement {
            x: 0,
            y: 0,
            width: 20,
            height: 20,
            monitor: None,
        });
        let validated = settings.validated();
        assert_eq!(validated.preferences.theme, AppTheme::Light);
        assert!(validated.window.is_none());
    }
}
