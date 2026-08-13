#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    let arguments = std::env::args().skip(1).collect::<Vec<_>>();
    if let Some(command) = arguments.first() {
        let result = match (command.as_str(), arguments.len()) {
            ("--install-autostart", 1) => petcrew_lib::autostart::install_and_start(),
            ("--uninstall-autostart", 1) => petcrew_lib::autostart::uninstall(),
            ("--autostart-status", 1) => {
                petcrew_lib::autostart::is_registered().and_then(|found| {
                    if found {
                        Ok(())
                    } else {
                        Err("task_not_registered")
                    }
                })
            }
            _ => Err("unknown_core_command"),
        };
        if let Err(error) = result {
            eprintln!("PetCrew Core command failed: {error}");
            std::process::exit(1);
        }
        return;
    }

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
