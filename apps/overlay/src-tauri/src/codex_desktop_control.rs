#[cfg(windows)]
use crate::codex_wait_chain::{ProcessWait, WaitChainProbeEnvelope};
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, HashSet};
use std::fs::{self, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::PathBuf;
use std::process::Command;
use std::process::Stdio;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

const CODEX_AUMID: &str = "OpenAI.Codex_2p2nqsd0c76g0!App";
const MAIN_PROCESS_QUERY: &str = "Get-CimInstance Win32_Process -Filter \"Name = 'ChatGPT.exe'\" | Select-Object ProcessId,ExecutablePath,CommandLine | ConvertTo-Json -Compress";
const TREE_QUERY: &str = "Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,Name,CreationDate,ExecutablePath,CommandLine | ConvertTo-Json -Compress";
const CREATE_NO_WINDOW: u32 = 0x0800_0000;
const MAX_SNAPSHOT_DESCENDANTS: usize = 2_048;

#[derive(Debug, Deserialize)]
#[serde(rename_all = "PascalCase")]
struct WindowsProcess {
    process_id: u32,
    executable_path: Option<String>,
    command_line: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct CodexRestartResult {
    old_pid: u32,
    new_pid: u32,
    snapshot_path: String,
    route: &'static str,
    target_pid: Option<u32>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ProcessNameCount {
    name: String,
    count: usize,
}

#[derive(Debug, Clone, Serialize)]
pub struct CodexPressureResult {
    desktop_pid: u32,
    app_server_pid: u32,
    descendant_count: usize,
    direct_child_count: usize,
    runtime_cohort_count: usize,
    direct_child_names: Vec<ProcessNameCount>,
    recent_signals: Vec<String>,
    suspected_thread_ids: Vec<String>,
    tool_app_server_count: usize,
    orphan_tool_app_server_count: usize,
    orphan_tool_app_server_pids: Vec<u32>,
}

#[derive(Debug, Clone, Serialize)]
pub struct OrphanCleanupResult {
    snapshot_path: Option<String>,
    terminated_pids: Vec<u32>,
}

#[derive(Debug, Clone, Serialize)]
struct ProcessSummary {
    app_server_pid: u32,
    descendant_count: usize,
    direct_child_count: usize,
    runtime_cohort_count: usize,
    direct_child_names: Vec<ProcessNameCount>,
}

#[derive(Debug, Clone, Serialize)]
struct CommandDiagnostic {
    exit_code: Option<i32>,
    stdout: String,
    stderr: String,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(rename_all = "PascalCase")]
struct ProcessIdentity {
    process_id: u32,
    parent_process_id: u32,
    name: String,
    creation_date: Option<String>,
    #[serde(default, skip_serializing)]
    executable_path: Option<String>,
    #[serde(default, skip_serializing)]
    command_line: Option<String>,
}

fn parse_inventory(json: &str) -> Result<Vec<ProcessIdentity>, String> {
    let mut rows: Vec<ProcessIdentity> = serde_json::from_str(json)
        .map_err(|_| "Не удалось прочитать снимок процессов Windows".to_string())?;
    if rows.len() > 20_000 || rows.iter().any(|row| row.name.len() > 100) {
        return Err("Снимок процессов Windows выходит за безопасные пределы".into());
    }
    // PID 0 is Windows' System Idle Process, not a killable process or a Codex descendant.
    rows.retain(|row| row.process_id != 0);
    Ok(rows)
}

fn descendants(inventory: &[ProcessIdentity], root: u32) -> Result<Vec<ProcessIdentity>, String> {
    let mut selected = HashSet::from([root]);
    let mut result = Vec::new();
    for _ in 0..inventory.len() {
        let before = selected.len();
        for row in inventory {
            if selected.contains(&row.parent_process_id) && selected.insert(row.process_id) {
                result.push(row.clone());
                if result.len() > MAX_SNAPSHOT_DESCENDANTS {
                    return Err(
                        "Дерево процессов Codex слишком велико для безопасного восстановления"
                            .into(),
                    );
                }
            }
        }
        if selected.len() == before {
            break;
        }
    }
    let main = inventory
        .iter()
        .find(|row| row.process_id == root)
        .ok_or_else(|| "Codex исчез до снимка процессов".to_string())?;
    result.insert(0, main.clone());
    Ok(result)
}

fn process_summary(
    inventory: &[ProcessIdentity],
    app_server_pid: u32,
) -> Result<ProcessSummary, String> {
    let app_tree = descendants(inventory, app_server_pid)?;
    let direct: Vec<&ProcessIdentity> = inventory
        .iter()
        .filter(|row| row.parent_process_id == app_server_pid)
        .collect();
    let mut names = BTreeMap::<String, usize>::new();
    for row in &direct {
        *names.entry(row.name.clone()).or_default() += 1;
    }
    let runtime_cohort_count = direct
        .iter()
        .filter(|row| row.name.eq_ignore_ascii_case("node_repl.exe"))
        .count();
    Ok(ProcessSummary {
        app_server_pid,
        descendant_count: app_tree.len().saturating_sub(1),
        direct_child_count: direct.len(),
        runtime_cohort_count,
        direct_child_names: names
            .into_iter()
            .map(|(name, count)| ProcessNameCount { name, count })
            .collect(),
    })
}

fn codex_bin_prefix() -> Result<String, String> {
    let local_app_data = std::env::var_os("LOCALAPPDATA")
        .ok_or_else(|| "Не найдена папка локальных данных Windows".to_string())?;
    Ok(PathBuf::from(local_app_data)
        .join("OpenAI")
        .join("Codex")
        .join("bin")
        .to_string_lossy()
        .replace('/', "\\")
        .to_ascii_lowercase())
}

fn is_tool_app_server(row: &ProcessIdentity, codex_bin: &str) -> bool {
    if !row.name.eq_ignore_ascii_case("codex.exe") || row.creation_date.is_none() {
        return false;
    }
    let path = row
        .executable_path
        .as_deref()
        .unwrap_or_default()
        .replace('/', "\\")
        .to_ascii_lowercase();
    let expected_prefix = format!("{}\\", codex_bin.trim_end_matches('\\'));
    if !path.starts_with(&expected_prefix) || !path.ends_with("\\codex.exe") {
        return false;
    }
    let command = row
        .command_line
        .as_deref()
        .unwrap_or_default()
        .trim()
        .to_ascii_lowercase();
    let Some(executable_end) = command.rfind("codex.exe") else {
        return false;
    };
    if command[..executable_end + "codex.exe".len()].trim_matches(['\"', ' ']) != path {
        return false;
    }
    command[executable_end + "codex.exe".len()..]
        .trim_start_matches(['\"', ' '])
        .trim()
        == "app-server --listen stdio://"
}

fn is_orphan_tool_app_server(row: &ProcessIdentity, inventory: &[ProcessIdentity]) -> bool {
    let Some(child_created) = row.creation_date.as_deref() else {
        return false;
    };
    match inventory
        .iter()
        .find(|candidate| candidate.process_id == row.parent_process_id)
    {
        None => true,
        Some(parent) => parent
            .creation_date
            .as_deref()
            .is_some_and(|parent_created| parent_created > child_created),
    }
}

fn tool_app_servers(
    inventory: &[ProcessIdentity],
) -> Result<(Vec<ProcessIdentity>, Vec<ProcessIdentity>), String> {
    let codex_bin = codex_bin_prefix()?;
    let tools: Vec<ProcessIdentity> = inventory
        .iter()
        .filter(|row| is_tool_app_server(row, &codex_bin))
        .cloned()
        .collect();
    let orphans = tools
        .iter()
        .filter(|row| is_orphan_tool_app_server(row, inventory))
        .cloned()
        .collect();
    Ok((tools, orphans))
}

fn inventory() -> Result<Vec<ProcessIdentity>, String> {
    parse_inventory(&powershell_output(TREE_QUERY)?)
}

fn diagnostics_dir() -> Result<PathBuf, String> {
    let directory = diagnostics_path()?;
    fs::create_dir_all(&directory)
        .map_err(|_| "Не удалось создать папку диагностического снимка".to_string())?;
    Ok(directory)
}

fn diagnostics_path() -> Result<PathBuf, String> {
    let base = std::env::var_os("LOCALAPPDATA")
        .ok_or_else(|| "Не найдена папка локальных данных Windows".to_string())?;
    Ok(PathBuf::from(base)
        .join("app.petcrew.overlay")
        .join("diagnostics"))
}

fn recent_event_codes(directory: &std::path::Path) -> Vec<serde_json::Value> {
    let path = directory.join("codex-appserver.jsonl");
    let Ok(metadata) = fs::metadata(&path) else {
        return Vec::new();
    };
    if metadata.len() > 1_100_000 {
        return Vec::new();
    }
    let Ok(data) = fs::read_to_string(path) else {
        return Vec::new();
    };
    data.lines()
        .rev()
        .take(16)
        .filter_map(|line| {
            let row: serde_json::Value = serde_json::from_str(line).ok()?;
            Some(serde_json::json!({
                "code": row.get("code")?.as_str()?.chars().take(80).collect::<String>(),
                "severity": row.get("severity")?.as_str()?.chars().take(20).collect::<String>(),
                "timestamp": row.get("occurred_at").cloned().unwrap_or(serde_json::Value::Null),
            }))
        })
        .collect()
}

fn recent_signal_codes(app_server_pid: u32, now: DateTime<Utc>) -> Vec<String> {
    let Ok(directory) = diagnostics_path() else {
        return Vec::new();
    };
    let path = directory.join("codex-appserver.jsonl");
    let Ok(metadata) = fs::metadata(&path) else {
        return Vec::new();
    };
    if metadata.len() > 1_100_000 {
        return Vec::new();
    }
    let Ok(data) = fs::read_to_string(path) else {
        return Vec::new();
    };
    recent_signal_codes_in(&data, app_server_pid, now)
}

fn recent_signal_codes_in(data: &str, app_server_pid: u32, now: DateTime<Utc>) -> Vec<String> {
    let prefix = format!("pid:{app_server_pid}:");
    let allowed = [
        "request_pressure",
        "appserver_timeout",
        "model_list_child_exit_timeout",
        "queue_admission_rejected",
        "turn_input_integrity_error",
    ];
    let mut found = HashSet::new();
    for line in data.lines().rev().take(128) {
        let Ok(row) = serde_json::from_str::<serde_json::Value>(line) else {
            continue;
        };
        if !row
            .get("process_uuid")
            .and_then(|value| value.as_str())
            .is_some_and(|value| value.starts_with(&prefix))
        {
            continue;
        }
        let Some(code) = row.get("code").and_then(|value| value.as_str()) else {
            continue;
        };
        if !allowed.contains(&code) {
            continue;
        }
        let Some(timestamp) = row.get("occurred_at").and_then(|value| value.as_str()) else {
            continue;
        };
        let Ok(when) = DateTime::parse_from_rfc3339(timestamp) else {
            continue;
        };
        let seconds = now
            .signed_duration_since(when.with_timezone(&Utc))
            .num_seconds();
        if (0..=600).contains(&seconds) {
            found.insert(code.to_string());
        }
    }
    let mut result: Vec<String> = found.into_iter().collect();
    result.sort();
    result
}

fn is_canonical_thread_id(value: &str) -> bool {
    value.len() == 36
        && value
            .chars()
            .enumerate()
            .all(|(index, character)| match index {
                8 | 13 | 18 | 23 => character == '-',
                _ => character.is_ascii_hexdigit(),
            })
}

fn recent_suspected_thread_ids(app_server_pid: u32, now: DateTime<Utc>) -> Vec<String> {
    let Ok(directory) = diagnostics_path() else {
        return Vec::new();
    };
    let path = directory.join("codex-appserver.jsonl");
    let Ok(metadata) = fs::metadata(&path) else {
        return Vec::new();
    };
    if metadata.len() > 1_100_000 {
        return Vec::new();
    }
    let Ok(data) = fs::read_to_string(path) else {
        return Vec::new();
    };
    recent_suspected_thread_ids_in(&data, app_server_pid, now)
}

fn recent_suspected_thread_ids_in(
    data: &str,
    app_server_pid: u32,
    now: DateTime<Utc>,
) -> Vec<String> {
    let prefix = format!("pid:{app_server_pid}:");
    let mut found = HashSet::new();
    for line in data.lines().rev().take(128) {
        let Ok(row) = serde_json::from_str::<serde_json::Value>(line) else {
            continue;
        };
        if row.get("code").and_then(|value| value.as_str()) != Some("turn_input_integrity_error")
            || !row
                .get("process_uuid")
                .and_then(|value| value.as_str())
                .is_some_and(|value| value.starts_with(&prefix))
        {
            continue;
        }
        let Some(timestamp) = row.get("occurred_at").and_then(|value| value.as_str()) else {
            continue;
        };
        let Ok(when) = DateTime::parse_from_rfc3339(timestamp) else {
            continue;
        };
        let seconds = now
            .signed_duration_since(when.with_timezone(&Utc))
            .num_seconds();
        if !(0..=600).contains(&seconds) {
            continue;
        }
        let Some(thread_id) = row.get("thread_id").and_then(|value| value.as_str()) else {
            continue;
        };
        if is_canonical_thread_id(thread_id) {
            found.insert(thread_id.to_ascii_lowercase());
        }
    }
    let mut result: Vec<String> = found.into_iter().collect();
    result.sort();
    result
}

fn recent_model_child_timeout(app_server_pid: u32, now: DateTime<Utc>) -> bool {
    let Ok(directory) = diagnostics_dir() else {
        return false;
    };
    let path = directory.join("codex-appserver.jsonl");
    let Ok(mut file) = fs::File::open(path) else {
        return false;
    };
    let Ok(len) = file.metadata().map(|metadata| metadata.len()) else {
        return false;
    };
    let start = len.saturating_sub(262_144);
    if file.seek(SeekFrom::Start(start)).is_err() {
        return false;
    }
    let mut bytes = Vec::new();
    if file.read_to_end(&mut bytes).is_err() {
        return false;
    }
    if start > 0 {
        let Some(line_end) = bytes.iter().position(|byte| *byte == b'\n') else {
            return false;
        };
        bytes.drain(..=line_end);
    }
    let Ok(data) = String::from_utf8(bytes) else {
        return false;
    };
    recent_model_child_timeout_in(&data, app_server_pid, now)
}

fn recent_model_child_timeout_in(data: &str, app_server_pid: u32, now: DateTime<Utc>) -> bool {
    let prefix = format!("pid:{app_server_pid}:");
    data.lines().rev().take(64).any(|line| {
        let Ok(row) = serde_json::from_str::<serde_json::Value>(line) else {
            return false;
        };
        if row.get("code").and_then(|value| value.as_str()) != Some("model_list_child_exit_timeout")
            || !row
                .get("process_uuid")
                .and_then(|value| value.as_str())
                .is_some_and(|value| value.starts_with(&prefix))
        {
            return false;
        }
        let Some(timestamp) = row.get("occurred_at").and_then(|value| value.as_str()) else {
            return false;
        };
        let Ok(when) = DateTime::parse_from_rfc3339(timestamp) else {
            return false;
        };
        let seconds = now
            .signed_duration_since(when.with_timezone(&Utc))
            .num_seconds();
        (0..=600).contains(&seconds)
    })
}

fn unique_deadlocked_direct_child(
    tree: &[ProcessIdentity],
    app_server_pid: u32,
    waits: &[ProcessWait],
) -> Option<ProcessIdentity> {
    let mut targets = waits
        .iter()
        .filter(|wait| wait.cycle)
        .map(|wait| wait.target_process_id);
    let first = targets.next()?;
    if targets.any(|pid| pid != first) {
        return None;
    }
    tree.iter()
        .find(|row| {
            row.process_id == first
                && row.parent_process_id == app_server_pid
                && row.creation_date.is_some()
        })
        .cloned()
}

fn save_snapshot(
    old_pid: u32,
    tree: &[ProcessIdentity],
    process_summary: Option<&ProcessSummary>,
    waits: Option<&[ProcessWait]>,
    wait_status: &str,
) -> Result<PathBuf, String> {
    let directory = diagnostics_dir()?;
    let timestamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| "Не удалось получить время снимка".to_string())?
        .as_millis();
    let path = directory.join(format!("codex-recovery-{timestamp}-{old_pid}.json"));
    let events = recent_event_codes(&directory);
    let record = serde_json::json!({"schema_version": 3, "ts_unix_ms": timestamp,
        "desktop_pid": old_pid, "processes": tree, "recent_events": events,
        "app_server_summary": process_summary,
        "wait_chain_available": waits.is_some(), "wait_chain_status": wait_status,
        "process_waits": waits.unwrap_or(&[])});
    let bytes = serde_json::to_vec_pretty(&record)
        .map_err(|_| "Не удалось подготовить диагностический снимок".to_string())?;
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&path)
        .map_err(|_| "Не удалось сохранить диагностический снимок".to_string())?;
    file.write_all(&bytes)
        .and_then(|_| file.sync_all())
        .map_err(|_| "Не удалось завершить запись диагностического снимка".to_string())?;
    Ok(path)
}

fn hidden_command(program: impl AsRef<std::ffi::OsStr>) -> Command {
    let mut command = Command::new(program);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(CREATE_NO_WINDOW);
    }
    command
}

fn bounded_command_text(bytes: &[u8]) -> String {
    String::from_utf8_lossy(bytes)
        .replace(['\r', '\n'], " ")
        .chars()
        .take(512)
        .collect()
}

fn command_diagnostic(output: &std::process::Output) -> CommandDiagnostic {
    CommandDiagnostic {
        exit_code: output.status.code(),
        stdout: bounded_command_text(&output.stdout),
        stderr: bounded_command_text(&output.stderr),
    }
}

#[cfg(windows)]
fn bounded_wait_chain_probe(app_server_pid: u32) -> (Option<Vec<ProcessWait>>, String) {
    let Ok(executable) = std::env::current_exe() else {
        return (None, "executable_unavailable".into());
    };
    let child = hidden_command(executable)
        .args(["--petcrew-wait-chain-probe", &app_server_pid.to_string()])
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn();
    let Ok(mut child) = child else {
        return (None, "helper_spawn_failed".into());
    };
    let deadline = Instant::now() + Duration::from_secs(20);
    loop {
        match child.try_wait() {
            Ok(Some(_)) => break,
            Ok(None) if Instant::now() < deadline => std::thread::sleep(Duration::from_millis(100)),
            _ => {
                let _ = child.kill();
                let _ = child.wait();
                return (None, "helper_timeout_or_wait_failed".into());
            }
        }
    }
    let Ok(output) = child.wait_with_output() else {
        return (None, "helper_output_failed".into());
    };
    if !output.status.success() {
        return (
            None,
            format!("helper_exit_{}", output.status.code().unwrap_or(-1)),
        );
    }
    if output.stdout.len() > 65_536 {
        return (None, "helper_output_too_large".into());
    }
    match serde_json::from_slice::<WaitChainProbeEnvelope>(&output.stdout) {
        Ok(WaitChainProbeEnvelope {
            waits: Some(waits),
            error: None,
        }) => (Some(waits), "ok".into()),
        Ok(WaitChainProbeEnvelope {
            waits: None,
            error: Some(error),
        }) => (None, error.into()),
        _ => (None, "helper_output_invalid".into()),
    }
}

fn powershell_output(script: &str) -> Result<String, String> {
    let output = hidden_command("powershell.exe")
        .args(["-NoProfile", "-NonInteractive", "-Command", script])
        .output()
        .map_err(|error| format!("Не удалось проверить Windows: {error}"))?;
    if !output.status.success() {
        return Err("Не удалось проверить процессы Codex в Windows".into());
    }
    Ok(String::from_utf8_lossy(&output.stdout).trim().to_owned())
}

fn parse_main_processes(json: &str) -> Result<Vec<u32>, String> {
    if json.is_empty() {
        return Ok(Vec::new());
    }
    let value: serde_json::Value = serde_json::from_str(json)
        .map_err(|_| "Windows вернула некорректный список процессов Codex".to_string())?;
    let rows = match value {
        serde_json::Value::Array(rows) => rows,
        row @ serde_json::Value::Object(_) => vec![row],
        _ => return Err("Windows вернула неожиданный список процессов Codex".into()),
    };
    let mut result = Vec::new();
    for row in rows {
        let process: WindowsProcess = serde_json::from_value(row)
            .map_err(|_| "Windows вернула неполные сведения о процессе Codex".to_string())?;
        let path = process
            .executable_path
            .as_deref()
            .unwrap_or_default()
            .replace('/', "\\")
            .to_ascii_lowercase();
        let command = process.command_line.as_deref().unwrap_or_default();
        if path.contains("\\windowsapps\\openai.codex_")
            && path.ends_with("\\app\\chatgpt.exe")
            && !command.contains("--type=")
        {
            result.push(process.process_id);
        }
    }
    Ok(result)
}

pub(crate) fn main_processes() -> Result<Vec<u32>, String> {
    parse_main_processes(&powershell_output(MAIN_PROCESS_QUERY)?)
}

fn process_exists(pid: u32) -> Result<bool, String> {
    let query = format!(
        "Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}' | Select-Object -ExpandProperty ProcessId"
    );
    Ok(powershell_output(&query)?.trim() == pid.to_string())
}

pub(crate) fn app_server_children(parent_pid: u32) -> Result<Vec<u32>, String> {
    let query = format!(
        "Get-CimInstance Win32_Process -Filter \"ParentProcessId = {parent_pid} AND Name = 'codex.exe'\" | Select-Object -ExpandProperty ProcessId"
    );
    powershell_output(&query)?
        .lines()
        .map(|line| {
            line.trim()
                .parse::<u32>()
                .map_err(|_| "Не удалось проверить дочерний App Server Codex".to_string())
        })
        .collect()
}

fn registered_app_id() -> Result<(), String> {
    let script = "Get-StartApps | Where-Object { $_.AppID -eq 'OpenAI.Codex_2p2nqsd0c76g0!App' } | Select-Object -ExpandProperty AppID";
    if powershell_output(script)? == CODEX_AUMID {
        Ok(())
    } else {
        Err("Установленный Codex не найден в меню Windows; перезапуск отменён".into())
    }
}

fn record_restart_attempt(
    stage: &str,
    outcome: &str,
    old_pid: Option<u32>,
    new_pid: Option<u32>,
    target_pid: Option<u32>,
    snapshot_path: Option<&std::path::Path>,
    command: Option<&CommandDiagnostic>,
) {
    let Some(base) = std::env::var_os("LOCALAPPDATA") else {
        return;
    };
    let directory = PathBuf::from(base)
        .join("app.petcrew.overlay")
        .join("diagnostics");
    if fs::create_dir_all(&directory).is_err() {
        return;
    }
    let path = directory.join("codex-restart.jsonl");
    let timestamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs())
        .unwrap_or_default();
    let record = serde_json::json!({
        "ts_unix": timestamp,
        "stage": stage,
        "outcome": outcome,
        "old_pid": old_pid,
        "new_pid": new_pid,
        "target_pid": target_pid,
        "snapshot_path": snapshot_path.map(|path| path.to_string_lossy().into_owned()),
        "command": command,
    });
    if let Ok(mut file) = OpenOptions::new().create(true).append(true).open(path) {
        let _ = writeln!(file, "{record}");
    }
}

fn tracked_processes_alive(main_pid: u32, app_server_pids: &[u32]) -> Result<bool, String> {
    if process_exists(main_pid)? {
        return Ok(true);
    }
    for pid in app_server_pids {
        if process_exists(*pid)? {
            return Ok(true);
        }
    }
    Ok(false)
}

fn forced_close_args(pid: u32) -> [String; 4] {
    ["/F".into(), "/T".into(), "/PID".into(), pid.to_string()]
}

pub(crate) fn snapshot_preflight_only() -> Result<PathBuf, String> {
    registered_app_id()?;
    let mains = main_processes()?;
    let [main_pid] = mains.as_slice() else {
        return Err("Codex Desktop неоднозначен".into());
    };
    let children = app_server_children(*main_pid)?;
    let current = inventory()?;
    let tree = descendants(&current, *main_pid)?;
    let summary = children
        .first()
        .and_then(|app_server_pid| process_summary(&current, *app_server_pid).ok());
    let (waits, wait_status) = if children.len() == 1 {
        bounded_wait_chain_probe(children[0])
    } else {
        (None, "app_server_ambiguous".into())
    };
    save_snapshot(
        *main_pid,
        &tree,
        summary.as_ref(),
        waits.as_deref(),
        &wait_status,
    )
}

pub(crate) fn inspect_codex_pressure_blocking() -> Result<CodexPressureResult, String> {
    let mains = main_processes()?;
    let [desktop_pid] = mains.as_slice() else {
        return Err("Не удалось однозначно определить открытый Codex".into());
    };
    let app_servers = app_server_children(*desktop_pid)?;
    let [app_server_pid] = app_servers.as_slice() else {
        return Err("Не удалось однозначно определить App Server Codex".into());
    };
    let current = inventory()?;
    let summary = process_summary(&current, *app_server_pid)?;
    let (tool_app_servers, orphan_tool_app_servers) = tool_app_servers(&current)?;
    Ok(CodexPressureResult {
        desktop_pid: *desktop_pid,
        app_server_pid: *app_server_pid,
        descendant_count: summary.descendant_count,
        direct_child_count: summary.direct_child_count,
        runtime_cohort_count: summary.runtime_cohort_count,
        direct_child_names: summary.direct_child_names,
        recent_signals: recent_signal_codes(*app_server_pid, Utc::now()),
        suspected_thread_ids: recent_suspected_thread_ids(*app_server_pid, Utc::now()),
        tool_app_server_count: tool_app_servers.len(),
        orphan_tool_app_server_count: orphan_tool_app_servers.len(),
        orphan_tool_app_server_pids: orphan_tool_app_servers
            .into_iter()
            .map(|row| row.process_id)
            .collect(),
    })
}

fn same_process_identity(left: &ProcessIdentity, right: &ProcessIdentity) -> bool {
    left.process_id == right.process_id
        && left.parent_process_id == right.parent_process_id
        && left.name == right.name
        && left.creation_date == right.creation_date
        && left.executable_path == right.executable_path
        && left.command_line == right.command_line
}

fn save_orphan_cleanup_snapshot(
    inventory: &[ProcessIdentity],
    candidates: &[ProcessIdentity],
) -> Result<PathBuf, String> {
    if candidates.len() > 32 {
        return Err("Слишком много сиротских tool-серверов для безопасной очистки".into());
    }
    let mut included = BTreeMap::<u32, ProcessIdentity>::new();
    for candidate in candidates {
        for row in descendants(inventory, candidate.process_id)? {
            included.insert(row.process_id, row);
            if included.len() > MAX_SNAPSHOT_DESCENDANTS {
                return Err("Дерево сиротских tool-серверов слишком велико".into());
            }
        }
    }
    let directory = diagnostics_dir()?;
    let timestamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| "Не удалось получить время снимка".to_string())?
        .as_millis();
    let path = directory.join(format!("codex-tool-orphans-{timestamp}.json"));
    let record = serde_json::json!({
        "schema_version": 1,
        "ts_unix_ms": timestamp,
        "selection_rule": "exact_tool_app_server_with_missing_or_reused_parent",
        "candidate_pids": candidates.iter().map(|row| row.process_id).collect::<Vec<_>>(),
        "processes": included.into_values().collect::<Vec<_>>(),
    });
    let bytes = serde_json::to_vec_pretty(&record)
        .map_err(|_| "Не удалось подготовить снимок сиротских tool-серверов".to_string())?;
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&path)
        .map_err(|_| "Не удалось сохранить снимок сиротских tool-серверов".to_string())?;
    file.write_all(&bytes)
        .and_then(|_| file.sync_all())
        .map_err(|_| "Не удалось завершить снимок сиротских tool-серверов".to_string())?;
    Ok(path)
}

fn cleanup_orphan_tool_app_servers_blocking() -> Result<OrphanCleanupResult, String> {
    let before = inventory()?;
    let (_, candidates) = tool_app_servers(&before)?;
    if candidates.is_empty() {
        return Ok(OrphanCleanupResult {
            snapshot_path: None,
            terminated_pids: Vec::new(),
        });
    }
    let snapshot = save_orphan_cleanup_snapshot(&before, &candidates)?;
    let codex_bin = codex_bin_prefix()?;
    let mut terminated = Vec::new();
    for candidate in candidates {
        let fresh = inventory()?;
        let Some(current) = fresh
            .iter()
            .find(|row| row.process_id == candidate.process_id)
        else {
            continue;
        };
        if !same_process_identity(&candidate, current)
            || !is_tool_app_server(current, &codex_bin)
            || !is_orphan_tool_app_server(current, &fresh)
        {
            return Err(format!(
                "Tool-сервер {} изменился; очистка остановлена",
                candidate.process_id
            ));
        }
        let output = hidden_command("taskkill.exe")
            .args(forced_close_args(candidate.process_id))
            .output()
            .map_err(|error| format!("Не удалось завершить tool-сервер: {error}"))?;
        let diagnostic = command_diagnostic(&output);
        record_restart_attempt(
            "cleanup_orphan_tool_app_server",
            if output.status.success() {
                "attempted"
            } else {
                "failed"
            },
            None,
            None,
            Some(candidate.process_id),
            Some(&snapshot),
            Some(&diagnostic),
        );
        if !output.status.success() {
            return Err(format!(
                "Windows не смогла завершить сиротский tool-сервер {}",
                candidate.process_id
            ));
        }
        let deadline = Instant::now() + Duration::from_secs(10);
        while process_exists(candidate.process_id)? && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(200));
        }
        if process_exists(candidate.process_id)? {
            return Err(format!(
                "Tool-сервер {} не завершился после команды Windows",
                candidate.process_id
            ));
        }
        terminated.push(candidate.process_id);
        record_restart_attempt(
            "cleanup_orphan_tool_app_server",
            "success",
            None,
            None,
            Some(candidate.process_id),
            Some(&snapshot),
            Some(&diagnostic),
        );
    }
    Ok(OrphanCleanupResult {
        snapshot_path: Some(snapshot.to_string_lossy().into_owned()),
        terminated_pids: terminated,
    })
}

fn restart_codex_desktop_blocking() -> Result<CodexRestartResult, String> {
    let mut stage = "registration";
    let mut recorded_pid = None;
    let mut snapshot_path = None;
    let mut command_attempt = None;
    let result = (|| {
        registered_app_id()?;
        stage = "identify_main";
        let candidates = main_processes()?;
        let [old_pid] = candidates.as_slice() else {
            return Err(
                "Не удалось однозначно определить открытый Codex; перезапуск отменён".into(),
            );
        };
        let old_pid = *old_pid;
        recorded_pid = Some(old_pid);
        stage = "identify_children";
        let children = app_server_children(old_pid)?;

        stage = "snapshot";
        let before = inventory()?;
        let tree = descendants(&before, old_pid)?;
        let main_identity = tree[0].clone();
        let (waits, wait_status) = if children.len() == 1 {
            bounded_wait_chain_probe(children[0])
        } else {
            (None, "app_server_ambiguous".into())
        };
        let summary = children
            .first()
            .and_then(|app_server_pid| process_summary(&before, *app_server_pid).ok());
        let path = save_snapshot(
            old_pid,
            &tree,
            summary.as_ref(),
            waits.as_deref(),
            &wait_status,
        )?;
        snapshot_path = Some(path.clone());

        if let ([app_server_pid], Some(waits)) = (children.as_slice(), waits.as_deref()) {
            if recent_model_child_timeout(*app_server_pid, Utc::now()) {
                if let Some(target) = unique_deadlocked_direct_child(&tree, *app_server_pid, waits)
                {
                    stage = "recheck_target_child";
                    let fresh = inventory()?;
                    let still_same = fresh.iter().any(|row| {
                        row.process_id == target.process_id
                            && row.parent_process_id == target.parent_process_id
                            && row.creation_date == target.creation_date
                    });
                    let main_still_same = fresh.iter().any(|row| {
                        row.process_id == old_pid
                            && row.creation_date == main_identity.creation_date
                            && row.name == main_identity.name
                    });
                    if still_same
                        && main_still_same
                        && main_processes()? == vec![old_pid]
                        && app_server_children(old_pid)? == vec![*app_server_pid]
                    {
                        stage = "terminate_target_child";
                        let output = hidden_command("taskkill.exe")
                            .args(["/F", "/PID", &target.process_id.to_string()])
                            .output();
                        let stopped = output.as_ref().is_ok_and(|output| output.status.success());
                        command_attempt = Some(match output {
                            Ok(output) => command_diagnostic(&output),
                            Err(error) => CommandDiagnostic {
                                exit_code: None,
                                stdout: String::new(),
                                stderr: bounded_command_text(error.to_string().as_bytes()),
                            },
                        });
                        if stopped {
                            let deadline = Instant::now() + Duration::from_secs(10);
                            while process_exists(target.process_id)? && Instant::now() < deadline {
                                std::thread::sleep(Duration::from_millis(200));
                            }
                            if !process_exists(target.process_id)?
                                && process_exists(*app_server_pid)?
                            {
                                return Ok(CodexRestartResult {
                                    old_pid,
                                    new_pid: old_pid,
                                    snapshot_path: path.to_string_lossy().into_owned(),
                                    route: "targeted_attempt",
                                    target_pid: Some(target.process_id),
                                });
                            }
                        }
                        record_restart_attempt(
                            "terminate_target_child",
                            "failed",
                            Some(old_pid),
                            None,
                            Some(target.process_id),
                            Some(&path),
                            command_attempt.as_ref(),
                        );
                    }
                }
            }
        }

        // Recheck immediately before a destructive action; never select by process name alone.
        stage = "recheck_main";
        let rechecked = inventory()?;
        if main_processes()? != vec![old_pid]
            || rechecked
                .iter()
                .find(|row| row.process_id == old_pid)
                .is_none_or(|row| {
                    row.creation_date != main_identity.creation_date
                        || row.name != main_identity.name
                })
        {
            return Err("Процесс Codex изменился до завершения; перезапуск отменён".into());
        }
        stage = "force_close";
        let close = match hidden_command("taskkill.exe")
            .args(forced_close_args(old_pid))
            .output()
        {
            Ok(output) => output,
            Err(error) => {
                command_attempt = Some(CommandDiagnostic {
                    exit_code: None,
                    stdout: String::new(),
                    stderr: bounded_command_text(error.to_string().as_bytes()),
                });
                return Err("Не удалось запустить принудительное завершение Codex".into());
            }
        };
        command_attempt = Some(command_diagnostic(&close));
        if !close.status.success() && tracked_processes_alive(old_pid, &children)? {
            return Err("Windows не смогла принудительно завершить Codex".into());
        }

        stage = "wait_exit";
        let deadline = Instant::now() + Duration::from_secs(15);
        while tracked_processes_alive(old_pid, &children)? {
            if Instant::now() >= deadline {
                return Err("Старый Codex ещё работает; повторный запуск отменён".into());
            }
            std::thread::sleep(Duration::from_millis(400));
        }
        if !main_processes()?.is_empty() {
            return Err("Появился другой процесс Codex; повторный запуск отменён".into());
        }

        stage = "launch";
        hidden_command("explorer.exe")
            .arg(format!("shell:AppsFolder\\{CODEX_AUMID}"))
            .spawn()
            .map_err(|error| format!("Не удалось открыть Codex через Windows: {error}"))?;
        stage = "wait_new_main";
        let deadline = Instant::now() + Duration::from_secs(20);
        let new_pid = loop {
            match main_processes()?.as_slice() {
                [new_pid] if *new_pid != old_pid => {
                    break *new_pid;
                }
                [] => {}
                _ => return Err("Новый процесс Codex неоднозначен; проверьте окно вручную".into()),
            }
            if Instant::now() >= deadline {
                return Err("Windows получила запрос на запуск, но окно Codex не появилось".into());
            }
            std::thread::sleep(Duration::from_millis(500));
        };
        stage = "wait_new_app_server";
        let deadline = Instant::now() + Duration::from_secs(30);
        while app_server_children(new_pid)?.len() != 1 {
            if Instant::now() >= deadline {
                return Err("Окно Codex открылось, но новый App Server не появился".into());
            }
            std::thread::sleep(Duration::from_millis(500));
        }
        Ok(CodexRestartResult {
            old_pid,
            new_pid,
            snapshot_path: path.to_string_lossy().into_owned(),
            route: "desktop_restarted",
            target_pid: None,
        })
    })();
    record_restart_attempt(
        stage,
        if result.is_ok() { "success" } else { "failed" },
        recorded_pid,
        result.as_ref().ok().map(|value| value.new_pid),
        result.as_ref().ok().and_then(|value| value.target_pid),
        snapshot_path.as_deref(),
        command_attempt.as_ref(),
    );
    result
}

#[tauri::command]
pub async fn restart_codex_desktop() -> Result<CodexRestartResult, String> {
    tauri::async_runtime::spawn_blocking(restart_codex_desktop_blocking)
        .await
        .map_err(|error| format!("Не удалось выполнить перезапуск Codex: {error}"))?
}

#[tauri::command]
pub async fn inspect_codex_pressure() -> Result<CodexPressureResult, String> {
    tauri::async_runtime::spawn_blocking(inspect_codex_pressure_blocking)
        .await
        .map_err(|error| format!("Не удалось проверить нагрузку Codex: {error}"))?
}

#[tauri::command]
pub async fn cleanup_orphan_tool_app_servers() -> Result<OrphanCleanupResult, String> {
    tauri::async_runtime::spawn_blocking(cleanup_orphan_tool_app_servers_blocking)
        .await
        .map_err(|error| format!("Не удалось очистить сиротские tool-серверы: {error}"))?
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn model_timeout_must_be_recent_and_from_the_same_app_server() {
        let now = DateTime::parse_from_rfc3339("2026-09-22T19:00:00+03:00")
            .unwrap()
            .with_timezone(&Utc);
        let current = r#"{"code":"model_list_child_exit_timeout","process_uuid":"pid:42:abc","occurred_at":"2026-09-22T18:59:00+03:00"}"#;
        let old = r#"{"code":"model_list_child_exit_timeout","process_uuid":"pid:42:abc","occurred_at":"2026-09-22T18:49:00+03:00"}"#;
        assert!(recent_model_child_timeout_in(current, 42, now));
        assert!(!recent_model_child_timeout_in(current, 43, now));
        assert!(!recent_model_child_timeout_in(old, 42, now));
    }

    #[cfg(windows)]
    #[test]
    fn child_first_requires_a_unique_deadlock_cycle_and_direct_child_identity() {
        let tree = vec![
            ProcessIdentity {
                process_id: 42,
                parent_process_id: 10,
                name: "codex.exe".into(),
                creation_date: Some("a".into()),
                executable_path: None,
                command_line: None,
            },
            ProcessIdentity {
                process_id: 43,
                parent_process_id: 42,
                name: "pwsh.exe".into(),
                creation_date: Some("b".into()),
                executable_path: None,
                command_line: None,
            },
        ];
        let mut wait = ProcessWait {
            owner_thread_id: 1,
            target_process_id: 43,
            wait_ms: 1_000_000,
            cycle: false,
        };
        assert!(unique_deadlocked_direct_child(&tree, 42, &[wait.clone()]).is_none());
        wait.cycle = true;
        assert_eq!(
            unique_deadlocked_direct_child(&tree, 42, &[wait.clone()])
                .unwrap()
                .process_id,
            43
        );
        let other = ProcessWait {
            target_process_id: 44,
            ..wait.clone()
        };
        assert!(unique_deadlocked_direct_child(&tree, 42, &[wait, other]).is_none());
        let mut unproven = tree;
        unproven[1].creation_date = None;
        assert!(unique_deadlocked_direct_child(
            &unproven,
            42,
            &[ProcessWait {
                owner_thread_id: 1,
                target_process_id: 43,
                wait_ms: 1,
                cycle: true
            }]
        )
        .is_none());
    }

    #[test]
    fn accepts_only_the_main_installed_codex_process() {
        let rows = r#"[{"ProcessId":11,"ExecutablePath":"C:\\Program Files\\WindowsApps\\OpenAI.Codex_1_x64__2p2nqsd0c76g0\\app\\ChatGPT.exe","CommandLine":"ChatGPT.exe"},{"ProcessId":12,"ExecutablePath":"C:\\Program Files\\WindowsApps\\OpenAI.Codex_1_x64__2p2nqsd0c76g0\\app\\ChatGPT.exe","CommandLine":"ChatGPT.exe --type=renderer"},{"ProcessId":13,"ExecutablePath":"C:\\Other\\ChatGPT.exe","CommandLine":"ChatGPT.exe"}]"#;
        assert_eq!(parse_main_processes(rows).unwrap(), vec![11]);
    }

    #[test]
    fn tool_app_server_requires_exact_installed_path_and_stdio_command() {
        let row = ProcessIdentity {
            process_id: 42,
            parent_process_id: 7,
            name: "codex.exe".into(),
            creation_date: Some("20260923140000".into()),
            executable_path: Some(r"C:\Program Files\OpenAI\Codex\bin\abc\codex.exe".into()),
            command_line: Some(
                r#""C:\Program Files\OpenAI\Codex\bin\abc\codex.exe" app-server --listen stdio://"#
                    .into(),
            ),
        };
        assert!(is_tool_app_server(
            &row,
            r"c:\program files\openai\codex\bin"
        ));
        let mut desktop = row.clone();
        desktop.command_line = Some("codex.exe app-server --analytics-default-enabled".into());
        assert!(!is_tool_app_server(
            &desktop,
            r"c:\program files\openai\codex\bin"
        ));
        let mut foreign = row;
        foreign.executable_path = Some(r"C:\Other\codex.exe".into());
        assert!(!is_tool_app_server(
            &foreign,
            r"c:\program files\openai\codex\bin"
        ));
    }

    #[test]
    fn orphan_requires_missing_or_reused_parent_not_a_live_owner() {
        let rows = parse_inventory(
            r#"[{"ProcessId":10,"ParentProcessId":5,"Name":"node_repl.exe","CreationDate":"20260923135900"},{"ProcessId":42,"ParentProcessId":10,"Name":"codex.exe","CreationDate":"20260923140000"}]"#,
        )
        .unwrap();
        assert!(!is_orphan_tool_app_server(&rows[1], &rows));
        let without_parent = vec![rows[1].clone()];
        assert!(is_orphan_tool_app_server(
            &without_parent[0],
            &without_parent
        ));
        let reused = parse_inventory(
            r#"[{"ProcessId":10,"ParentProcessId":5,"Name":"other.exe","CreationDate":"20260923140100"},{"ProcessId":42,"ParentProcessId":10,"Name":"codex.exe","CreationDate":"20260923140000"}]"#,
        )
        .unwrap();
        assert!(is_orphan_tool_app_server(&reused[1], &reused));
    }

    #[test]
    fn empty_inventory_cannot_target_a_process() {
        assert!(parse_main_processes("").unwrap().is_empty());
        assert!(parse_main_processes("null").is_err());
    }

    #[test]
    fn process_tree_command_is_exact_and_forced() {
        assert_eq!(forced_close_args(1234), ["/F", "/T", "/PID", "1234"]);
    }

    #[test]
    fn snapshot_tree_includes_only_verified_descendants() {
        let rows = parse_inventory(r#"[{"ProcessId":10,"ParentProcessId":1,"Name":"ChatGPT.exe","CreationDate":"20260922100000"},{"ProcessId":11,"ParentProcessId":10,"Name":"codex.exe","CreationDate":"20260922100001"},{"ProcessId":12,"ParentProcessId":11,"Name":"python.exe","CreationDate":"20260922100002"},{"ProcessId":20,"ParentProcessId":1,"Name":"unrelated.exe","CreationDate":"20260922100003"}]"#).unwrap();
        let tree = descendants(&rows, 10).unwrap();
        assert_eq!(
            tree.iter().map(|row| row.process_id).collect::<Vec<_>>(),
            vec![10, 11, 12]
        );
        assert!(!serde_json::to_string(&tree)
            .unwrap()
            .contains("CommandLine"));
    }

    #[test]
    fn pressure_summary_counts_direct_runtime_cohorts_without_guessing_capacity() {
        let rows = parse_inventory(r#"[{"ProcessId":10,"ParentProcessId":1,"Name":"codex.exe","CreationDate":"a"},{"ProcessId":11,"ParentProcessId":10,"Name":"node_repl.exe","CreationDate":"b"},{"ProcessId":12,"ParentProcessId":10,"Name":"python.exe","CreationDate":"c"},{"ProcessId":13,"ParentProcessId":12,"Name":"node.exe","CreationDate":"d"}]"#).unwrap();
        let summary = process_summary(&rows, 10).unwrap();
        assert_eq!(summary.direct_child_count, 2);
        assert_eq!(summary.descendant_count, 3);
        assert_eq!(summary.runtime_cohort_count, 1);
        assert_eq!(summary.direct_child_names.len(), 2);
    }

    #[test]
    fn recent_signals_are_scoped_to_current_app_server_and_ten_minutes() {
        let now = DateTime::parse_from_rfc3339("2026-09-23T10:20:00Z")
            .unwrap()
            .with_timezone(&Utc);
        let data = concat!(
            "{\"code\":\"request_pressure\",\"process_uuid\":\"pid:42:a\",\"occurred_at\":\"2026-09-23T10:19:00Z\"}\n",
            "{\"code\":\"appserver_timeout\",\"process_uuid\":\"pid:43:b\",\"occurred_at\":\"2026-09-23T10:19:00Z\"}\n",
            "{\"code\":\"model_list_child_exit_timeout\",\"process_uuid\":\"pid:42:a\",\"occurred_at\":\"2026-09-23T10:09:59Z\"}\n"
        );
        assert_eq!(
            recent_signal_codes_in(data, 42, now),
            vec!["request_pressure"]
        );
    }

    #[test]
    fn suspected_threads_are_scoped_validated_recent_and_deduplicated() {
        let now = DateTime::parse_from_rfc3339("2026-09-24T00:20:00Z")
            .unwrap()
            .with_timezone(&Utc);
        let data = concat!(
            "{\"code\":\"turn_input_integrity_error\",\"process_uuid\":\"pid:42:a\",\"occurred_at\":\"2026-09-24T00:19:00Z\",\"thread_id\":\"01A0A486-D5FD-7423-BC01-825B6B77072F\"}\n",
            "{\"code\":\"turn_input_integrity_error\",\"process_uuid\":\"pid:42:a\",\"occurred_at\":\"2026-09-24T00:18:00Z\",\"thread_id\":\"01a0a486-d5fd-7423-bc01-825b6b77072f\"}\n",
            "{\"code\":\"turn_input_integrity_error\",\"process_uuid\":\"pid:43:b\",\"occurred_at\":\"2026-09-24T00:19:00Z\",\"thread_id\":\"019f9a58-1f22-7f63-bd1c-7c480dd3ea1d\"}\n",
            "{\"code\":\"turn_input_integrity_error\",\"process_uuid\":\"pid:42:a\",\"occurred_at\":\"2026-09-24T00:09:59Z\",\"thread_id\":\"019f9a58-1f22-7f63-bd1c-7c480dd3ea1d\"}\n",
            "{\"code\":\"turn_input_integrity_error\",\"process_uuid\":\"pid:42:a\",\"occurred_at\":\"2026-09-24T00:19:00Z\",\"thread_id\":\"not-a-thread\"}\n"
        );
        assert_eq!(
            recent_suspected_thread_ids_in(data, 42, now),
            vec!["01a0a486-d5fd-7423-bc01-825b6b77072f"]
        );
    }

    #[test]
    fn taskkill_output_is_bounded_and_single_line() {
        let text = format!("first\r\n{}", "x".repeat(600));
        let bounded = bounded_command_text(text.as_bytes());
        assert!(!bounded.contains(['\r', '\n']));
        assert_eq!(bounded.chars().count(), 512);
    }

    #[test]
    fn missing_desktop_cannot_make_a_snapshot_target() {
        let rows = parse_inventory("[]").unwrap();
        assert!(descendants(&rows, 10).is_err());
    }

    #[test]
    fn system_idle_pid_zero_does_not_invalidate_the_windows_inventory() {
        let rows = parse_inventory(r#"[{"ProcessId":0,"ParentProcessId":0,"Name":"System Idle Process","CreationDate":"2026-09-22T10:55:15+03:00"},{"ProcessId":10,"ParentProcessId":1,"Name":"ChatGPT.exe","CreationDate":"2026-09-22T11:00:00+03:00"}]"#).unwrap();
        assert_eq!(
            rows.iter().map(|row| row.process_id).collect::<Vec<_>>(),
            vec![10]
        );
        assert_eq!(descendants(&rows, 10).unwrap().len(), 1);
    }

    #[cfg(windows)]
    #[test]
    #[ignore = "manual read-only preflight of the installed Codex; saves a snapshot but never restarts"]
    fn live_snapshot_preflight_without_termination() {
        let mains = main_processes().unwrap();
        let [main_pid] = mains.as_slice() else {
            panic!("expected exactly one installed Codex Desktop")
        };
        let current = inventory().unwrap();
        let tree = descendants(&current, *main_pid).unwrap();
        let children = app_server_children(*main_pid).unwrap();
        let summary = children
            .first()
            .and_then(|pid| process_summary(&current, *pid).ok());
        let path = save_snapshot(*main_pid, &tree, summary.as_ref(), None, "not_run").unwrap();
        assert!(fs::metadata(path).unwrap().len() > 0);
    }
}
