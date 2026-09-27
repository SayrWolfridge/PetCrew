use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::Path;
use std::process::Command;
use std::time::{Duration, Instant};

const TASK_NAME: &str = "OpenCode Bridge Server";
const RELAY_PORT: u16 = 4099;
const OPENCODE_PORT: u16 = 4098;
const MAX_HEALTH_RESPONSE_BYTES: usize = 16 * 1024;

#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize)]
pub struct RelayHealth {
    healthy: bool,
    relay_ready: bool,
    opencode_ready: bool,
}

#[derive(Debug, Deserialize)]
struct RelayHealthPayload {
    healthy: bool,
    relay_ready: bool,
    opencode_ready: bool,
    service: String,
    protocol_version: u64,
}

fn http_get(port: u16, path: &str) -> Option<Vec<u8>> {
    let address = SocketAddr::from(([127, 0, 0, 1], port));
    let mut stream = TcpStream::connect_timeout(&address, Duration::from_millis(800)).ok()?;
    let timeout = Some(Duration::from_millis(1200));
    stream.set_read_timeout(timeout).ok()?;
    stream.set_write_timeout(timeout).ok()?;
    let request = format!("GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n");
    stream.write_all(request.as_bytes()).ok()?;

    let mut response = Vec::new();
    stream
        .take((MAX_HEALTH_RESPONSE_BYTES + 1) as u64)
        .read_to_end(&mut response)
        .ok()?;
    if response.is_empty() || response.len() > MAX_HEALTH_RESPONSE_BYTES {
        return None;
    }
    Some(response)
}

fn successful_http_body(response: &[u8]) -> Option<&[u8]> {
    let header_end = response
        .windows(4)
        .position(|window| window == b"\r\n\r\n")?;
    let status_end = response.windows(2).position(|window| window == b"\r\n")?;
    if status_end > header_end {
        return None;
    }
    let status_line = std::str::from_utf8(&response[..status_end]).ok()?;
    let mut columns = status_line.split_whitespace();
    if columns.next()? != "HTTP/1.1" || columns.next()? != "200" {
        return None;
    }
    Some(&response[header_end + 4..])
}

fn relay_health_from_response(response: &[u8]) -> Option<RelayHealth> {
    let payload: RelayHealthPayload =
        serde_json::from_slice(successful_http_body(response)?).ok()?;
    if payload.service != "codex-return-reader" || payload.protocol_version != 1 {
        return None;
    }
    let expected_healthy = payload.relay_ready && payload.opencode_ready;
    if payload.healthy != expected_healthy {
        return None;
    }
    Some(RelayHealth {
        healthy: payload.healthy,
        relay_ready: payload.relay_ready,
        opencode_ready: payload.opencode_ready,
    })
}

fn relay_health() -> RelayHealth {
    http_get(RELAY_PORT, "/health")
        .as_deref()
        .and_then(relay_health_from_response)
        .unwrap_or_default()
}

fn command_output(program: &str, args: &[&str]) -> Result<std::process::Output, String> {
    Command::new(program)
        .args(args)
        .output()
        .map_err(|error| format!("Не удалось запустить {program}: {error}"))
}

fn listener_owners_from(text: &str) -> BTreeMap<u16, u32> {
    let mut owners = BTreeMap::new();
    for line in text.lines() {
        let columns: Vec<_> = line.split_whitespace().collect();
        if columns.len() < 4 || !columns[0].eq_ignore_ascii_case("TCP") {
            continue;
        }
        let Some(local) = columns.get(1) else {
            continue;
        };
        if !local.starts_with("127.0.0.1:") {
            continue;
        }
        let Some(port) = local
            .rsplit(':')
            .next()
            .and_then(|value| value.parse::<u16>().ok())
        else {
            continue;
        };
        if !matches!(port, RELAY_PORT | OPENCODE_PORT) {
            continue;
        }
        if let Some(pid) = columns.last().and_then(|value| value.parse::<u32>().ok()) {
            owners.insert(port, pid);
        }
    }
    owners
}

fn listener_owners() -> Result<BTreeMap<u16, u32>, String> {
    let output = command_output("netstat.exe", &["-ano", "-p", "TCP"])?;
    if !output.status.success() {
        return Err("Не удалось проверить владельцев портов Relay".to_string());
    }
    Ok(listener_owners_from(&String::from_utf8_lossy(
        &output.stdout,
    )))
}

#[derive(Clone, Debug, Deserialize, PartialEq, Eq)]
struct ProcessIdentity {
    pid: u32,
    name: String,
    executable_path: String,
    command_line: String,
    creation_time: String,
}

#[derive(Clone, Copy, Debug)]
enum ProcessRole {
    Relay,
    OpenCode,
}

fn process_identity(pid: u32) -> Result<Option<ProcessIdentity>, String> {
    let script = format!(
        "$p = Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}' -ErrorAction Stop; \
         if ($null -ne $p) {{ \
         if ($null -eq $p.ExecutablePath -or $null -eq $p.CommandLine -or $null -eq $p.CreationDate) {{ exit 3 }}; \
         [pscustomobject]@{{ pid=[uint32]$p.ProcessId; name=[string]$p.Name; \
         executable_path=[string]$p.ExecutablePath; command_line=[string]$p.CommandLine; \
         creation_time=([datetime]$p.CreationDate).ToUniversalTime().ToString('o') }} \
         | ConvertTo-Json -Compress }}"
    );
    let output = command_output(
        "powershell.exe",
        &["-NoProfile", "-NonInteractive", "-Command", &script],
    )?;
    if output.status.code() == Some(3) {
        return Err(format!(
            "Не удалось получить полную идентичность процесса {pid}; восстановление отменено"
        ));
    }
    if !output.status.success() {
        return Err(format!(
            "Не удалось проверить идентичность процесса {pid}; восстановление отменено"
        ));
    }
    let text = String::from_utf8_lossy(&output.stdout);
    let text = text.trim().trim_start_matches('\u{feff}');
    if text.is_empty() {
        return Ok(None);
    }
    serde_json::from_str(text)
        .map(Some)
        .map_err(|_| format!("Windows вернула некорректную идентичность процесса {pid}"))
}

fn windows_command_line_args(command_line: &str) -> Vec<String> {
    let characters: Vec<char> = command_line.chars().collect();
    let mut arguments = Vec::new();
    let mut index = 0;
    while index < characters.len() {
        while index < characters.len() && characters[index].is_whitespace() {
            index += 1;
        }
        if index >= characters.len() {
            break;
        }
        let mut argument = String::new();
        let mut quoted = false;
        while index < characters.len() {
            if characters[index].is_whitespace() && !quoted {
                break;
            }
            if characters[index] == '\\' {
                let start = index;
                while index < characters.len() && characters[index] == '\\' {
                    index += 1;
                }
                let count = index - start;
                if index < characters.len() && characters[index] == '"' {
                    argument.extend(std::iter::repeat_n('\\', count / 2));
                    if count % 2 == 0 {
                        quoted = !quoted;
                    } else {
                        argument.push('"');
                    }
                    index += 1;
                } else {
                    argument.extend(std::iter::repeat_n('\\', count));
                }
                continue;
            }
            if characters[index] == '"' {
                quoted = !quoted;
                index += 1;
                continue;
            }
            argument.push(characters[index]);
            index += 1;
        }
        arguments.push(argument);
        while index < characters.len() && characters[index].is_whitespace() {
            index += 1;
        }
    }
    arguments
}

fn path_file_name_eq(path: &str, expected: &str) -> bool {
    Path::new(path)
        .file_name()
        .and_then(|name| name.to_str())
        .is_some_and(|name| name.eq_ignore_ascii_case(expected))
}

fn has_argument_pair(arguments: &[String], key: &str, value: &str) -> bool {
    arguments
        .windows(2)
        .any(|pair| pair[0].eq_ignore_ascii_case(key) && pair[1].eq_ignore_ascii_case(value))
}

fn process_matches_role(identity: &ProcessIdentity, role: ProcessRole) -> bool {
    let (expected_image, expected_script) = match role {
        ProcessRole::Relay => ("pythonw.exe", Some("opencode_external_server.py")),
        ProcessRole::OpenCode => ("opencode.exe", None),
    };
    if identity.pid == 0
        || !identity.name.eq_ignore_ascii_case(expected_image)
        || !path_file_name_eq(&identity.executable_path, expected_image)
        || identity.creation_time.is_empty()
    {
        return false;
    }
    let arguments = windows_command_line_args(&identity.command_line);
    if arguments.is_empty() || !path_file_name_eq(&arguments[0], expected_image) {
        return false;
    }
    if let Some(script) = expected_script {
        return arguments
            .iter()
            .skip(1)
            .any(|argument| path_file_name_eq(argument, script));
    }
    arguments
        .get(1)
        .is_some_and(|argument| argument.eq_ignore_ascii_case("serve"))
        && has_argument_pair(&arguments, "--hostname", "127.0.0.1")
        && has_argument_pair(&arguments, "--port", "4098")
}

fn role_for_port(port: u16) -> Option<ProcessRole> {
    match port {
        RELAY_PORT => Some(ProcessRole::Relay),
        OPENCODE_PORT => Some(ProcessRole::OpenCode),
        _ => None,
    }
}

fn verified_listener_identities(
    owners: &BTreeMap<u16, u32>,
) -> Result<BTreeMap<u16, ProcessIdentity>, String> {
    let mut identities = BTreeMap::new();
    for (&port, &pid) in owners {
        let role = role_for_port(port)
            .ok_or_else(|| format!("Порт {port} не относится к Relay; восстановление отменено"))?;
        let identity = process_identity(pid)?.ok_or_else(|| {
            format!("Владелец порта {port} исчез во время проверки; восстановление отменено")
        })?;
        if !process_matches_role(&identity, role) {
            return Err(format!(
                "Порт {port} занят неожиданным процессом; восстановление отменено"
            ));
        }
        identities.insert(port, identity);
    }
    Ok(identities)
}

fn task_exists() -> Result<bool, String> {
    let output = command_output("schtasks.exe", &["/Query", "/TN", TASK_NAME])?;
    Ok(output.status.success())
}

fn taskkill_tree(expected: &ProcessIdentity, role: ProcessRole) -> Result<(), String> {
    let Some(current) = process_identity(expected.pid)? else {
        return Ok(());
    };
    if current != *expected || !process_matches_role(&current, role) {
        return Err(format!(
            "Идентичность процесса {} изменилась; принудительная остановка отменена",
            expected.pid
        ));
    }
    let pid_text = expected.pid.to_string();
    let output = command_output("taskkill.exe", &["/PID", &pid_text, "/T", "/F"])?;
    if output.status.success() || process_identity(expected.pid)?.is_none() {
        Ok(())
    } else {
        Err(format!(
            "Не удалось остановить старый процесс Relay {}",
            expected.pid
        ))
    }
}

fn recover_relay_blocking() -> Result<RelayHealth, String> {
    if !task_exists()? {
        return Err(format!("Scheduled task «{TASK_NAME}» не найдена"));
    }

    let old_owners = listener_owners()?;
    let old = verified_listener_identities(&old_owners)?;

    let stopped = command_output("schtasks.exe", &["/End", "/TN", TASK_NAME])?;
    if !stopped.status.success() && !old.is_empty() {
        return Err("Windows не остановила штатную задачу Relay".to_string());
    }
    std::thread::sleep(Duration::from_millis(700));

    for (&port, identity) in &old {
        if listener_owners()?
            .values()
            .any(|owner| *owner == identity.pid)
        {
            taskkill_tree(
                identity,
                role_for_port(port).expect("verified Relay port must have a process role"),
            )?;
            std::thread::sleep(Duration::from_millis(if port == RELAY_PORT {
                500
            } else {
                300
            }));
        }
    }
    if !listener_owners()?.is_empty() {
        return Err("Старые порты Relay остались заняты; новый процесс не запускался".to_string());
    }

    let started = command_output("schtasks.exe", &["/Run", "/TN", TASK_NAME])?;
    if !started.status.success() {
        return Err("Windows не запустила штатную задачу Relay".to_string());
    }

    let deadline = Instant::now() + Duration::from_secs(25);
    loop {
        let health = relay_health();
        if health.healthy {
            return Ok(health);
        }
        if Instant::now() >= deadline {
            return Err(
                "Relay запущен, но не подтвердил готовность Relay и OpenCode за 25 секунд"
                    .to_string(),
            );
        }
        std::thread::sleep(Duration::from_millis(500));
    }
}

#[tauri::command]
pub async fn get_relay_health() -> Result<RelayHealth, String> {
    tauri::async_runtime::spawn_blocking(relay_health)
        .await
        .map_err(|error| format!("Не удалось проверить Relay: {error}"))
}

#[tauri::command]
pub async fn recover_relay() -> Result<RelayHealth, String> {
    tauri::async_runtime::spawn_blocking(recover_relay_blocking)
        .await
        .map_err(|error| format!("Не удалось восстановить Relay: {error}"))?
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_only_expected_loopback_listeners() {
        let owners = listener_owners_from(
            "TCP    127.0.0.1:4098    0.0.0.0:0    LISTENING    101\n\
             TCP    127.0.0.1:4099    0.0.0.0:0    LISTENING    202\n\
             TCP    0.0.0.0:4098      0.0.0.0:0    LISTENING    303\n\
             TCP    127.0.0.1:5000    0.0.0.0:0    LISTENING    404",
        );
        assert_eq!(owners.get(&OPENCODE_PORT), Some(&101));
        assert_eq!(owners.get(&RELAY_PORT), Some(&202));
        assert_eq!(owners.len(), 2);
    }

    #[test]
    fn accepts_only_the_exact_health_contract() {
        let valid = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{\"healthy\":true,\"relay_ready\":true,\"opencode_ready\":true,\"service\":\"codex-return-reader\",\"protocol_version\":1}";
        assert_eq!(
            relay_health_from_response(valid),
            Some(RelayHealth {
                healthy: true,
                relay_ready: true,
                opencode_ready: true,
            })
        );

        let waiting = b"HTTP/1.1 200 OK\r\n\r\n{\"healthy\":false,\"relay_ready\":true,\"opencode_ready\":false,\"service\":\"codex-return-reader\",\"protocol_version\":1}";
        assert_eq!(
            relay_health_from_response(waiting),
            Some(RelayHealth {
                healthy: false,
                relay_ready: true,
                opencode_ready: false,
            })
        );

        assert!(relay_health_from_response(b"HTTP/1.1 401 Unauthorized\r\n\r\n").is_none());
        assert!(relay_health_from_response(b"HTTP/1.1 500 Error\r\n\r\n{}").is_none());
        assert!(relay_health_from_response(b"HTTP/1.1 200 OK\r\n\r\n{\"healthy\":true}").is_none());
        assert!(relay_health_from_response(b"HTTP/1.1 200 OK\r\n\r\n{\"healthy\":true,\"relay_ready\":true,\"opencode_ready\":true,\"service\":\"other\",\"protocol_version\":1}").is_none());
    }

    #[test]
    fn verifies_relay_and_opencode_process_roles() {
        let relay = ProcessIdentity {
            pid: 101,
            name: "pythonw.exe".to_string(),
            executable_path: r"C:\Python\pythonw.exe".to_string(),
            command_line:
                r#""C:\Python\pythonw.exe" "C:\Program Files\PetCrew\opencode_external_server.py""#
                    .to_string(),
            creation_time: "2026-09-27T10:00:00.0000000Z".to_string(),
        };
        assert!(process_matches_role(&relay, ProcessRole::Relay));
        let mut wrong_relay = relay.clone();
        wrong_relay.command_line =
            r#""C:\Python\pythonw.exe" "C:\Program Files\Other\server.py""#.to_string();
        assert!(!process_matches_role(&wrong_relay, ProcessRole::Relay));

        let opencode = ProcessIdentity {
            pid: 202,
            name: "opencode.exe".to_string(),
            executable_path: r"C:\Program Files\OpenCode\opencode.exe".to_string(),
            command_line:
                r#""C:\Program Files\OpenCode\opencode.exe" serve --hostname 127.0.0.1 --port 4098"#
                    .to_string(),
            creation_time: "2026-09-27T10:00:01.0000000Z".to_string(),
        };
        assert!(process_matches_role(&opencode, ProcessRole::OpenCode));
        let mut wrong_opencode = opencode.clone();
        wrong_opencode.command_line =
            r#""C:\Program Files\OpenCode\opencode.exe" serve --hostname 127.0.0.1 --port 5000"#
                .to_string();
        assert!(!process_matches_role(
            &wrong_opencode,
            ProcessRole::OpenCode
        ));
    }
}
