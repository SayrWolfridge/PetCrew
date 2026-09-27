mod codex_black_box;
mod codex_desktop_control;
#[cfg(windows)]
mod codex_wait_chain;

#[cfg(windows)]
pub fn run_wait_chain_probe(process_id: u32) -> i32 {
    let result = codex_wait_chain::probe_envelope(process_id);
    if serde_json::to_writer(std::io::stdout(), &result).is_err() {
        return 3;
    }
    0
}

#[cfg(windows)]
pub fn run_snapshot_preflight_only() -> i32 {
    let Ok(path) = codex_desktop_control::snapshot_preflight_only() else {
        return 2;
    };
    println!("{}", path.to_string_lossy());
    0
}

#[cfg(windows)]
pub fn run_pressure_inspection_only() -> i32 {
    let Ok(result) = codex_desktop_control::inspect_codex_pressure_blocking() else {
        return 2;
    };
    if serde_json::to_writer(std::io::stdout(), &result).is_err() {
        return 3;
    }
    0
}
mod core_ownership;
mod hub;
mod relay_control;
mod settings;

pub async fn run_core(app_data: std::path::PathBuf) -> Result<(), Box<dyn std::error::Error>> {
    hub::run_headless(app_data).await
}

#[cfg(feature = "desktop")]
#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        .setup(|app| {
            settings::setup(app)?;
            hub::setup(app)
        })
        .invoke_handler(tauri::generate_handler![
            hub::get_hub_connection,
            hub::get_hub_snapshot,
            hub::open_codex_thread,
            hub::open_opencode_project,
            hub::acknowledge_hub_agent,
            hub::clear_hub,
            relay_control::get_relay_health,
            relay_control::recover_relay,
            codex_desktop_control::inspect_codex_pressure,
            codex_desktop_control::cleanup_orphan_tool_app_servers,
            codex_desktop_control::restart_codex_desktop,
            settings::get_app_settings,
            settings::update_app_preferences,
            settings::update_window_placement
        ])
        .build(tauri::generate_context!())
        .expect("error while building PetCrew");

    app.run(|app_handle, event| {
        if matches!(event, tauri::RunEvent::Resumed) {
            hub::resume(app_handle);
        }
        if matches!(event, tauri::RunEvent::Exit) {
            hub::cleanup(app_handle);
        }
    });
}
