#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    #[cfg(windows)]
    {
        let args: Vec<String> = std::env::args().collect();
        if args
            .get(1)
            .is_some_and(|arg| arg == "--petcrew-wait-chain-probe")
        {
            let Some(process_id) = args.get(2).and_then(|arg| arg.parse::<u32>().ok()) else {
                std::process::exit(2);
            };
            if args.len() != 3 {
                std::process::exit(2)
            }
            std::process::exit(petcrew_lib::run_wait_chain_probe(process_id));
        }
        if args
            .get(1)
            .is_some_and(|arg| arg == "--petcrew-snapshot-preflight")
        {
            if args.len() != 2 {
                std::process::exit(2)
            }
            std::process::exit(petcrew_lib::run_snapshot_preflight_only());
        }
        if args
            .get(1)
            .is_some_and(|arg| arg == "--petcrew-pressure-inspect")
        {
            if args.len() != 2 {
                std::process::exit(2)
            }
            std::process::exit(petcrew_lib::run_pressure_inspection_only());
        }
    }
    petcrew_lib::run();
}
