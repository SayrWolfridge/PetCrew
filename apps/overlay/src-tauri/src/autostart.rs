use std::ffi::OsStr;
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use std::time::{Duration, Instant};

const TASK_NAME: &str = "PetCrew Core";

fn xml_escape(value: &str) -> String {
    value
        .replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&apos;")
}

fn task_xml(account: &str, executable: &Path) -> Result<String, &'static str> {
    let working_directory = executable
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
        .ok_or("core_parent_missing")?;
    let account = xml_escape(account);
    let executable = xml_escape(&executable.to_string_lossy());
    let working_directory = xml_escape(&working_directory.to_string_lossy());

    Ok(format!(
        r#"<?xml version="1.0" encoding="UTF-8"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Starts the local PetCrew Core for the current user.</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{account}</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{account}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{executable}</Command>
      <WorkingDirectory>{working_directory}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"#
    ))
}

fn current_account() -> Result<String, &'static str> {
    let username = std::env::var("USERNAME").map_err(|_| "username_missing")?;
    if username.trim().is_empty() {
        return Err("username_missing");
    }
    match std::env::var("USERDOMAIN") {
        Ok(domain) if !domain.trim().is_empty() => Ok(format!("{domain}\\{username}")),
        _ => Ok(username),
    }
}

#[cfg(windows)]
fn schtasks<I, S>(args: I) -> Result<Output, &'static str>
where
    I: IntoIterator<Item = S>,
    S: AsRef<OsStr>,
{
    use std::os::windows::process::CommandExt;

    const CREATE_NO_WINDOW: u32 = 0x0800_0000;
    Command::new("schtasks.exe")
        .args(args)
        .creation_flags(CREATE_NO_WINDOW)
        .output()
        .map_err(|_| "task_scheduler_unavailable")
}

#[cfg(not(windows))]
fn schtasks<I, S>(_args: I) -> Result<Output, &'static str>
where
    I: IntoIterator<Item = S>,
    S: AsRef<OsStr>,
{
    Err("windows_only")
}

fn temporary_task_xml() -> PathBuf {
    let nonce = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_nanos())
        .unwrap_or_default();
    std::env::temp_dir().join(format!(
        "petcrew-core-task-{}-{nonce}.xml",
        std::process::id()
    ))
}

fn wait_for_core_exit() -> Result<(), &'static str> {
    let local_app_data = std::env::var_os("LOCALAPPDATA").ok_or("local_app_data_missing")?;
    let lock_path = PathBuf::from(local_app_data)
        .join("app.petcrew.overlay")
        .join("hub-core.lock");
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        let owner_is_alive = fs::read_to_string(&lock_path)
            .ok()
            .and_then(|value| value.trim().parse::<u32>().ok())
            .map(crate::core_ownership::process_is_alive)
            .unwrap_or(false);
        if !owner_is_alive {
            return Ok(());
        }
        if Instant::now() >= deadline {
            return Err("task_stop_failed");
        }
        std::thread::sleep(Duration::from_millis(100));
    }
}

pub fn install_and_start() -> Result<(), &'static str> {
    let executable = std::env::current_exe().map_err(|_| "core_executable_missing")?;
    let account = current_account()?;
    let xml = task_xml(&account, &executable)?;
    let xml_path = temporary_task_xml();
    let mut xml_file = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&xml_path)
        .map_err(|_| "task_definition_write_failed")?;
    xml_file
        .write_all(xml.as_bytes())
        .map_err(|_| "task_definition_write_failed")?;
    drop(xml_file);

    let create = schtasks([
        OsStr::new("/Create"),
        OsStr::new("/TN"),
        OsStr::new(TASK_NAME),
        OsStr::new("/XML"),
        xml_path.as_os_str(),
        OsStr::new("/F"),
    ]);
    let _ = fs::remove_file(&xml_path);
    if !create?.status.success() {
        return Err("task_registration_failed");
    }

    let start = schtasks(["/Run", "/TN", TASK_NAME])?;
    if !start.status.success() {
        return Err("task_start_failed");
    }
    Ok(())
}

pub fn uninstall() -> Result<(), &'static str> {
    let query = schtasks(["/Query", "/TN", TASK_NAME])?;
    if !query.status.success() {
        return Ok(());
    }

    let _ = schtasks(["/End", "/TN", TASK_NAME]);
    wait_for_core_exit()?;
    let delete = schtasks(["/Delete", "/TN", TASK_NAME, "/F"])?;
    if !delete.status.success() {
        return Err("task_removal_failed");
    }
    Ok(())
}

pub fn is_registered() -> Result<bool, &'static str> {
    Ok(schtasks(["/Query", "/TN", TASK_NAME])?.status.success())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn task_contract_is_current_user_least_privilege_and_non_expiring() {
        let xml = task_xml(
            "Sample & User",
            Path::new(r"C:\Program Files & Tools\PetCrew\petcrew-core.exe"),
        )
        .unwrap();

        assert!(xml.contains("<UserId>Sample &amp; User</UserId>"));
        assert!(xml.contains("<LogonType>InteractiveToken</LogonType>"));
        assert!(xml.contains("<RunLevel>LeastPrivilege</RunLevel>"));
        assert!(xml.contains("<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>"));
        assert!(xml.contains("<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>"));
        assert!(xml.contains("<RestartOnFailure>"));
        assert!(xml.contains("C:\\Program Files &amp; Tools\\PetCrew\\petcrew-core.exe"));
        assert!(xml.contains(
            "<WorkingDirectory>C:\\Program Files &amp; Tools\\PetCrew</WorkingDirectory>"
        ));
    }

    #[test]
    fn task_xml_requires_an_executable_parent() {
        assert_eq!(
            task_xml("Sample", Path::new("petcrew-core.exe")),
            Err("core_parent_missing")
        );
    }
}
