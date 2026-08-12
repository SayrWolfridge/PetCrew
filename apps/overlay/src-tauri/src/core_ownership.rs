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

#[cfg(windows)]
pub(crate) fn process_is_alive(process_id: u32) -> bool {
    const PROCESS_QUERY_LIMITED_INFORMATION: u32 = 0x1000;
    const STILL_ACTIVE: u32 = 259;
    #[link(name = "kernel32")]
    unsafe extern "system" {
        fn OpenProcess(
            desired_access: u32,
            inherit_handle: i32,
            process_id: u32,
        ) -> *mut std::ffi::c_void;
        fn GetExitCodeProcess(process: *mut std::ffi::c_void, exit_code: *mut u32) -> i32;
        fn CloseHandle(handle: *mut std::ffi::c_void) -> i32;
    }
    let handle = unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, process_id) };
    if handle.is_null() {
        return false;
    }
    let mut exit_code = 0u32;
    let queried = unsafe { GetExitCodeProcess(handle, &mut exit_code) } != 0;
    unsafe { CloseHandle(handle) };
    // Fail closed when Windows refuses the status query: accepting a second
    // Core is more dangerous than requiring stale-lock recovery. A process
    // object may remain open after termination, so OpenProcess alone is not
    // proof that the PID still represents a running owner.
    !queried || exit_code == STILL_ACTIVE
}

#[cfg(not(windows))]
pub(crate) fn process_is_alive(process_id: u32) -> bool {
    process_id == std::process::id()
}

pub(crate) fn acquire_core_ownership(app_data: &Path) -> std::io::Result<CoreOwnership> {
    let path = app_data.join("hub-core.lock");
    if let Ok(owner) = fs::read_to_string(&path) {
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
        let _ = fs::remove_file(&path);
    }
    let mut file = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&path)?;
    write!(file, "{}", std::process::id())?;
    file.flush()?;
    Ok(CoreOwnership {
        path,
        file: Some(file),
    })
}
