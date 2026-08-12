#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    let Some(local_app_data) = std::env::var_os("LOCALAPPDATA") else {
        eprintln!("PetCrew Core: LOCALAPPDATA is unavailable");
        std::process::exit(1);
    };
    let app_data = std::path::PathBuf::from(local_app_data).join("app.petcrew.overlay");
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
        .expect("PetCrew Core runtime could not start");
    if let Err(error) = runtime.block_on(petcrew_lib::run_core(app_data)) {
        eprintln!("PetCrew Core stopped: {error}");
        std::process::exit(1);
    }
}
