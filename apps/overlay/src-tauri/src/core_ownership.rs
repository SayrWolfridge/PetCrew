use std::{
    fs,
    io::Write,
    path::{Path, PathBuf},
};

pub(crate) struct CoreOwnership {
    path: PathBuf,
    file: Option<fs::File>,
}

impl Drop for CoreOwnership {
    fn drop(&mut self) {
        self.file.take();
        let _ = fs::remove_file(&self.path);
    }
}

#[cfg(not(windows))]
pub(crate) fn process_is_alive(process_id: u32) -> bool {
    process_id == std::process::id()
}

#[cfg(windows)]
fn open_core_lock(path: &Path) -> std::io::Result<fs::File> {
    use std::os::windows::fs::OpenOptionsExt;

    const FILE_SHARE_READ: u32 = 0x0000_0001;
    const ERROR_SHARING_VIOLATION: i32 = 32;
    const ERROR_LOCK_VIOLATION: i32 = 33;

    fs::OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(true)
        // The retained handle is the ownership authority. Diagnostics may
        // still read the PID, but a second writer or delete cannot race the
        // live owner. Windows releases the exclusion when the process exits.
        .share_mode(FILE_SHARE_READ)
        .open(path)
        .map_err(|error| match error.raw_os_error() {
            Some(ERROR_SHARING_VIOLATION | ERROR_LOCK_VIOLATION) => std::io::Error::new(
                std::io::ErrorKind::AlreadyExists,
                "petcrew_core_already_running",
            ),
            _ => error,
        })
}

#[cfg(not(windows))]
fn open_core_lock(path: &Path) -> std::io::Result<fs::File> {
    if let Ok(owner) = fs::read_to_string(path) {
        if owner
            .trim()
            .parse::<u32>()
            .map(process_is_alive)
            .unwrap_or(false)
        {
            return Err(std::io::Error::new(
                std::io::ErrorKind::AlreadyExists,
                "petcrew_core_already_running",
            ));
        }
        let _ = fs::remove_file(path);
    }
    fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(path)
}

pub(crate) fn acquire_core_ownership(app_data: &Path) -> std::io::Result<CoreOwnership> {
    let path = app_data.join("hub-core.lock");
    let mut file = open_core_lock(&path)?;
    write!(file, "{}", std::process::id())?;
    file.flush()?;
    Ok(CoreOwnership {
        path,
        file: Some(file),
    })
}

#[cfg(all(test, windows))]
mod tests {
    use super::*;
    use std::time::{SystemTime, UNIX_EPOCH};

    const CHILD_LOCK_DIR: &str = "PETCREW_TEST_CHILD_LOCK_DIR";

    fn temp_dir(name: &str) -> PathBuf {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let path = std::env::temp_dir().join(format!(
            "petcrew-core-lock-{name}-{}-{nonce}",
            std::process::id()
        ));
        fs::create_dir_all(&path).unwrap();
        path
    }

    #[test]
    fn stale_pid_text_for_a_live_unrelated_process_does_not_own_the_lock() {
        let app_data = temp_dir("pid-reuse");
        fs::write(
            app_data.join("hub-core.lock"),
            std::process::id().to_string(),
        )
        .unwrap();

        let ownership = acquire_core_ownership(&app_data).unwrap();
        assert_eq!(
            fs::read_to_string(app_data.join("hub-core.lock")).unwrap(),
            std::process::id().to_string()
        );

        drop(ownership);
        fs::remove_dir_all(app_data).unwrap();
    }

    #[test]
    fn child_holds_lock_then_exits_without_drop() {
        let Some(app_data) = std::env::var_os(CHILD_LOCK_DIR).map(PathBuf::from) else {
            return;
        };
        let ownership = acquire_core_ownership(&app_data).unwrap();
        std::mem::forget(ownership);
        std::process::exit(0);
    }

    #[test]
    fn process_exit_releases_the_exclusive_handle_and_stale_file_recovers() {
        let app_data = temp_dir("process-exit");
        let status = std::process::Command::new(std::env::current_exe().unwrap())
            .arg("child_holds_lock_then_exits_without_drop")
            .arg("--nocapture")
            .env(CHILD_LOCK_DIR, &app_data)
            .status()
            .unwrap();
        assert!(status.success());
        assert!(app_data.join("hub-core.lock").is_file());

        let ownership = acquire_core_ownership(&app_data).unwrap();
        drop(ownership);
        fs::remove_dir_all(app_data).unwrap();
    }
}
