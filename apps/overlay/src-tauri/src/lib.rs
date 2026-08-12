mod core_ownership;
mod hub;
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
