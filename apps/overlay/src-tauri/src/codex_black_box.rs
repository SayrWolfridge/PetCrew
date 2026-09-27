//! Event-driven, evidence-only recorder for Codex app-server degradation.
//! Log bodies and transcript contents are inspected only to derive fixed codes;
//! they are never persisted by this module.

use chrono::{DateTime, Utc};
use notify::{Config, Event, RecommendedWatcher, RecursiveMode, Watcher};
use rusqlite::{Connection, OpenFlags};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::VecDeque;
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{mpsc, Arc};
use std::thread;
use std::time::{Duration, Instant};

const MAX_ROWS: i64 = 10_000;
const MAX_BYTES: u64 = 1024 * 1024;
const PRESSURE_COUNT: usize = 6;
const PRESSURE_WINDOW: i64 = 10;
const PRESSURE_COOLDOWN: i64 = 60;

#[derive(Debug, Deserialize, Serialize)]
struct Cursor {
    schema_version: u32,
    database: String,
    last_log_id: i64,
}

#[derive(Debug)]
struct LogRow {
    id: i64,
    ts: i64,
    ts_nanos: i64,
    level: String,
    target: String,
    body: String,
    process_uuid: Option<String>,
}

#[derive(Clone, Debug, Serialize)]
struct TranscriptProbe {
    thread_id: String,
    size: u64,
    modified_at: Option<String>,
    ends_with_newline: bool,
    final_json_object: bool,
    final_envelope_type: Option<String>,
    final_payload_type: Option<String>,
}

#[derive(Debug, Serialize)]
struct Record {
    schema_version: u32,
    event_id: String,
    occurred_at: String,
    code: &'static str,
    severity: &'static str,
    source_log_id: i64,
    process_uuid: Option<String>,
    technical_target: String,
    method: Option<String>,
    recent_request_count: Option<usize>,
    thread_id: Option<String>,
    transcript_probes: Vec<TranscriptProbe>,
}

fn canonical_thread_id_from_body(body: &str) -> Option<String> {
    let candidate = body
        .split("thread_id=")
        .nth(1)?
        .split(|character: char| character.is_ascii_whitespace() || character == '}')
        .next()?
        .trim_matches(['\"', '\'']);
    let valid = candidate.len() == 36
        && candidate
            .chars()
            .enumerate()
            .all(|(index, character)| match index {
                8 | 13 | 18 | 23 => character == '-',
                _ => character.is_ascii_hexdigit(),
            });
    valid.then(|| candidate.to_ascii_lowercase())
}

struct Recorder {
    log_db: PathBuf,
    state_db: Option<PathBuf>,
    sessions_root: Option<PathBuf>,
    journal: PathBuf,
    cursor_path: PathBuf,
    last_id: i64,
    request_times: VecDeque<i64>,
    last_pressure: Option<i64>,
}

impl Recorder {
    fn open(
        log_db: PathBuf,
        state_db: Option<PathBuf>,
        sessions_root: Option<PathBuf>,
        output: PathBuf,
    ) -> Option<Self> {
        fs::create_dir_all(&output).ok()?;
        let cursor_path = output.join("codex-appserver-cursor.json");
        let database = log_db.file_name()?.to_string_lossy().into_owned();
        let backup_path = cursor_path.with_extension("json.bak");
        let saved = [&cursor_path, &backup_path]
            .into_iter()
            .filter_map(|path| fs::read(path).ok())
            .filter_map(|bytes| serde_json::from_slice::<Cursor>(&bytes).ok())
            .filter(|cursor| cursor.database == database)
            .max_by_key(|cursor| cursor.last_log_id);
        let last_id = saved
            .map(|value| value.last_log_id)
            .unwrap_or(max_id(&log_db).ok()?);
        let recorder = Self {
            log_db,
            state_db,
            sessions_root,
            journal: output.join("codex-appserver.jsonl"),
            cursor_path,
            last_id,
            request_times: VecDeque::new(),
            last_pressure: None,
        };
        recorder.save_cursor().ok()?;
        Some(recorder)
    }

    fn scan(&mut self) -> rusqlite::Result<usize> {
        if let Some(parent) = self.log_db.parent() {
            if let Some(latest) = latest_database(parent, "logs_", ".sqlite") {
                if latest != self.log_db {
                    self.log_db = latest;
                    self.last_id = max_id(&self.log_db)?;
                    self.request_times.clear();
                    self.last_pressure = None;
                    let emitted = self.write_fixed("log_database_rotated", "info", self.last_id);
                    let _ = self.save_cursor();
                    return Ok(emitted);
                }
            }
        }
        let connection = read_only(&self.log_db)?;
        let maximum: i64 =
            connection.query_row("SELECT COALESCE(MAX(id), 0) FROM logs", [], |row| {
                row.get(0)
            })?;
        if maximum < self.last_id {
            self.last_id = maximum;
            let emitted = self.write_fixed("log_cursor_reset", "warning", maximum);
            let _ = self.save_cursor();
            return Ok(emitted);
        }
        let mut emitted = 0;
        loop {
            let rows = rows_after(&connection, self.last_id)?;
            if rows.is_empty() {
                break;
            }
            for row in rows {
                self.last_id = row.id;
                emitted += self.observe(&row);
            }
            if self.last_id >= maximum {
                break;
            }
        }
        let _ = self.save_cursor();
        Ok(emitted)
    }

    fn observe(&mut self, row: &LogRow) -> usize {
        if let Some(method) = method(&row.target, &row.body) {
            self.request_times.push_back(row.ts);
            while self
                .request_times
                .front()
                .is_some_and(|first| row.ts.saturating_sub(*first) > PRESSURE_WINDOW)
            {
                self.request_times.pop_front();
            }
            if self.request_times.len() >= PRESSURE_COUNT
                && self
                    .last_pressure
                    .is_none_or(|last| row.ts.saturating_sub(last) >= PRESSURE_COOLDOWN)
            {
                self.last_pressure = Some(row.ts);
                return self.write_row(
                    row,
                    "request_pressure",
                    "warning",
                    Some(method),
                    Some(self.request_times.len()),
                    None,
                );
            }
        }

        let lower = row.body.to_ascii_lowercase();
        let target = row.target.to_ascii_lowercase();
        let integrity_thread_id = if target == "codex_core::util"
            && lower.contains("custom tool call output is missing for call id:")
        {
            canonical_thread_id_from_body(&row.body)
        } else {
            None
        };
        let signal = if (target.contains("app_server") || target.contains("queue"))
            && lower.contains("queue")
            && ["reject", "full", "capacity", "overload"]
                .iter()
                .any(|part| lower.contains(part))
        {
            Some(("queue_admission_rejected", "error"))
        } else if target.contains("codex_app_server")
            && (lower.contains("timed out") || lower.contains("timeout"))
        {
            Some(("appserver_timeout", "error"))
        } else if target.contains("models_manager")
            && lower.contains("timeout waiting for child process to exit")
        {
            Some(("model_list_child_exit_timeout", "error"))
        } else if integrity_thread_id.is_some() {
            Some(("turn_input_integrity_error", "error"))
        } else if target == "rmcp::service"
            && lower.contains("mcp_manager_init")
            && matches!(row.level.as_str(), "WARN" | "ERROR")
        {
            Some(("mcp_initialization_warning", "warning"))
        } else {
            None
        };
        signal
            .map(|(code, severity)| {
                self.write_row(row, code, severity, None, None, integrity_thread_id)
            })
            .unwrap_or(0)
    }

    fn write_row(
        &self,
        row: &LogRow,
        code: &'static str,
        severity: &'static str,
        method: Option<String>,
        recent_request_count: Option<usize>,
        thread_id: Option<String>,
    ) -> usize {
        let occurred_at = DateTime::<Utc>::from_timestamp(row.ts, row.ts_nanos as u32)
            .unwrap_or_else(Utc::now)
            .to_rfc3339();
        self.append(&Record {
            schema_version: 1,
            event_id: digest(&format!("{}\0{}\0{}", row.id, code, row.target)),
            occurred_at,
            code,
            severity,
            source_log_id: row.id,
            process_uuid: row.process_uuid.clone(),
            technical_target: row.target.chars().take(160).collect(),
            method,
            recent_request_count,
            thread_id,
            transcript_probes: self.probes(),
        })
        .is_ok() as usize
    }

    fn write_fixed(&self, code: &'static str, severity: &'static str, id: i64) -> usize {
        self.append(&Record {
            schema_version: 1,
            event_id: digest(&format!("fixed\0{code}\0{id}")),
            occurred_at: Utc::now().to_rfc3339(),
            code,
            severity,
            source_log_id: id,
            process_uuid: None,
            technical_target: "codex_technical_log".to_string(),
            method: None,
            recent_request_count: None,
            thread_id: None,
            transcript_probes: self.probes(),
        })
        .is_ok() as usize
    }

    fn probes(&self) -> Vec<TranscriptProbe> {
        match (&self.state_db, &self.sessions_root) {
            (Some(database), Some(root)) => recent_probes(database, root).unwrap_or_default(),
            _ => Vec::new(),
        }
    }

    fn append(&self, record: &Record) -> std::io::Result<()> {
        rotate(&self.journal)?;
        let mut file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.journal)?;
        file.write_all(&serde_json::to_vec(record).map_err(std::io::Error::other)?)?;
        file.write_all(b"\n")
    }

    fn save_cursor(&self) -> std::io::Result<()> {
        let cursor = Cursor {
            schema_version: 1,
            database: self
                .log_db
                .file_name()
                .unwrap_or_default()
                .to_string_lossy()
                .into_owned(),
            last_log_id: self.last_id,
        };
        let temporary = self.cursor_path.with_extension("json.tmp");
        let backup = self.cursor_path.with_extension("json.bak");
        fs::write(
            &temporary,
            serde_json::to_vec_pretty(&cursor).map_err(std::io::Error::other)?,
        )?;
        if backup.exists() {
            fs::remove_file(&backup)?;
        }
        if self.cursor_path.exists() {
            fs::rename(&self.cursor_path, &backup)?;
        }
        if let Err(error) = fs::rename(&temporary, &self.cursor_path) {
            if backup.exists() && !self.cursor_path.exists() {
                let _ = fs::rename(&backup, &self.cursor_path);
            }
            return Err(error);
        }
        Ok(())
    }
}

pub(crate) struct BlackBoxHandle {
    stop: Arc<AtomicBool>,
    signal: mpsc::Sender<()>,
    worker: Option<thread::JoinHandle<()>>,
}

impl BlackBoxHandle {
    pub(crate) fn spawn(
        codex_home: &Path,
        state_db: Option<PathBuf>,
        sessions_root: Option<PathBuf>,
        app_data: &Path,
    ) -> Option<Self> {
        let log_db = latest_database(codex_home, "logs_", ".sqlite")?;
        let name = log_db.file_name()?.to_os_string();
        let parent = log_db.parent()?.to_path_buf();
        let mut recorder = Recorder::open(
            log_db,
            state_db,
            sessions_root,
            app_data.join("diagnostics"),
        )?;
        let stop = Arc::new(AtomicBool::new(false));
        let thread_stop = stop.clone();
        let (tx, rx) = mpsc::channel();
        let callback_tx = tx.clone();
        let worker = thread::Builder::new()
            .name("petcrew-codex-black-box".to_string())
            .spawn(move || {
                let watched = name.to_string_lossy().into_owned();
                let mut watcher = match RecommendedWatcher::new(
                    move |result: notify::Result<Event>| {
                        if result.ok().is_some_and(|event| {
                            event.paths.iter().any(|path| {
                                let candidate =
                                    path.file_name().unwrap_or_default().to_string_lossy();
                                candidate == watched
                                    || candidate.starts_with(&format!("{watched}-"))
                                    || (candidate.starts_with("logs_")
                                        && candidate.contains(".sqlite"))
                            })
                        }) {
                            let _ = callback_tx.send(());
                        }
                    },
                    Config::default(),
                ) {
                    Ok(value) => value,
                    Err(_) => return,
                };
                if watcher.watch(&parent, RecursiveMode::NonRecursive).is_err() {
                    return;
                }
                while rx.recv().is_ok() {
                    if thread_stop.load(Ordering::Acquire) {
                        break;
                    }
                    let deadline = Instant::now() + Duration::from_secs(2);
                    loop {
                        let now = Instant::now();
                        if now >= deadline {
                            break;
                        }
                        let remaining = deadline.saturating_duration_since(now);
                        if rx
                            .recv_timeout(remaining.min(Duration::from_millis(750)))
                            .is_err()
                        {
                            break;
                        }
                    }
                    let _ = recorder.scan();
                }
            })
            .ok()?;
        Some(Self {
            stop,
            signal: tx,
            worker: Some(worker),
        })
    }
}

impl Drop for BlackBoxHandle {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Release);
        let _ = self.signal.send(());
        if let Some(worker) = self.worker.take() {
            let _ = worker.join();
        }
    }
}

fn read_only(path: &Path) -> rusqlite::Result<Connection> {
    let connection = Connection::open_with_flags(
        path,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )?;
    connection.busy_timeout(Duration::from_millis(50))?;
    Ok(connection)
}

fn max_id(path: &Path) -> rusqlite::Result<i64> {
    read_only(path)?.query_row("SELECT COALESCE(MAX(id), 0) FROM logs", [], |row| {
        row.get(0)
    })
}

fn rows_after(connection: &Connection, after: i64) -> rusqlite::Result<Vec<LogRow>> {
    let mut statement = connection.prepare(
        "SELECT id, ts, ts_nanos, level, target, COALESCE(feedback_log_body, ''), process_uuid \
         FROM logs WHERE id > ?1 ORDER BY id LIMIT ?2",
    )?;
    let rows = statement
        .query_map(rusqlite::params![after, MAX_ROWS], |row| {
            Ok(LogRow {
                id: row.get(0)?,
                ts: row.get(1)?,
                ts_nanos: row.get(2)?,
                level: row.get(3)?,
                target: row.get(4)?,
                body: row.get(5)?,
                process_uuid: row.get(6)?,
            })
        })?
        .collect();
    rows
}

fn method(target: &str, body: &str) -> Option<String> {
    if target != "codex_app_server::message_processor" {
        return None;
    }
    let value = body
        .strip_prefix("app-server request: ")?
        .split_whitespace()
        .next()?;
    [
        "thread/resume",
        "thread/start",
        "thread/read",
        "thread/turns/list",
        "thread/queue/list",
        "thread/queue/add",
        "model/list",
        "account/read",
        "mcpServerStatus/list",
    ]
    .contains(&value)
    .then(|| value.to_string())
}

fn digest(value: &str) -> String {
    format!("{:x}", Sha256::digest(value.as_bytes()))
}

fn latest_database(root: &Path, prefix: &str, suffix: &str) -> Option<PathBuf> {
    fs::read_dir(root)
        .ok()?
        .filter_map(Result::ok)
        .filter_map(|entry| {
            let name = entry.file_name().to_string_lossy().into_owned();
            let version = name
                .strip_prefix(prefix)?
                .strip_suffix(suffix)?
                .parse::<u64>()
                .ok()?;
            Some((version, entry.path()))
        })
        .max_by_key(|(version, _)| *version)
        .map(|(_, path)| path)
}

fn recent_probes(state_db: &Path, sessions_root: &Path) -> rusqlite::Result<Vec<TranscriptProbe>> {
    let Ok(root) = fs::canonicalize(sessions_root) else {
        return Ok(Vec::new());
    };
    let connection = read_only(state_db)?;
    let mut statement = connection.prepare(
        "SELECT id, rollout_path FROM threads WHERE rollout_path IS NOT NULL AND rollout_path <> '' \
         ORDER BY COALESCE(updated_at_ms, updated_at * 1000) DESC LIMIT 8",
    )?;
    let rows = statement.query_map([], |row| {
        Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
    })?;
    let mut probes = Vec::new();
    for row in rows {
        let (thread_id, path) = row?;
        let Ok(path) = fs::canonicalize(path) else {
            continue;
        };
        if path.starts_with(&root) {
            if let Some(probe) = probe_transcript(&thread_id, &path) {
                probes.push(probe);
            }
        }
    }
    Ok(probes)
}

fn probe_transcript(thread_id: &str, path: &Path) -> Option<TranscriptProbe> {
    let metadata = fs::metadata(path).ok()?;
    let size = metadata.len();
    let modified_at = metadata
        .modified()
        .ok()
        .map(DateTime::<Utc>::from)
        .map(|value| value.to_rfc3339());
    let mut file = File::open(path).ok()?;
    let mut tail = vec![0; size.min(64 * 1024) as usize];
    if !tail.is_empty() {
        file.seek(SeekFrom::End(-(tail.len() as i64))).ok()?;
        file.read_exact(&mut tail).ok()?;
    }
    let ends_with_newline = tail.last().is_none_or(|byte| *byte == b'\n');
    let parsed = tail
        .split(|byte| *byte == b'\n')
        .rev()
        .find(|line| !line.is_empty())
        .and_then(|line| serde_json::from_slice::<serde_json::Value>(line).ok());
    Some(TranscriptProbe {
        thread_id: thread_id.to_string(),
        size,
        modified_at,
        ends_with_newline,
        final_json_object: parsed.as_ref().is_some_and(serde_json::Value::is_object),
        final_envelope_type: parsed
            .as_ref()
            .and_then(|v| v.get("type"))
            .and_then(|v| v.as_str())
            .map(|v| v.chars().take(64).collect()),
        final_payload_type: parsed
            .as_ref()
            .and_then(|v| v.get("payload"))
            .and_then(|v| v.get("type"))
            .and_then(|v| v.as_str())
            .map(|v| v.chars().take(64).collect()),
    })
}

fn rotate(path: &Path) -> std::io::Result<()> {
    if fs::metadata(path).map(|value| value.len()).unwrap_or(0) < MAX_BYTES {
        return Ok(());
    }
    let _ = fs::remove_file(path.with_extension("jsonl.4"));
    for generation in (1..4).rev() {
        let source = path.with_extension(format!("jsonl.{generation}"));
        if source.exists() {
            fs::rename(
                source,
                path.with_extension(format!("jsonl.{}", generation + 1)),
            )?;
        }
    }
    fs::rename(path, path.with_extension("jsonl.1"))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp(name: &str) -> PathBuf {
        let path = std::env::temp_dir().join(format!(
            "petcrew-black-box-{name}-{}",
            Utc::now().timestamp_nanos_opt().unwrap()
        ));
        fs::create_dir_all(&path).unwrap();
        path
    }

    fn log_db(path: &Path) -> Connection {
        let connection = Connection::open(path).unwrap();
        connection.execute_batch("CREATE TABLE logs (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, ts_nanos INTEGER NOT NULL, level TEXT NOT NULL, target TEXT NOT NULL, feedback_log_body TEXT, process_uuid TEXT);").unwrap();
        connection
    }

    #[test]
    fn sanitizes_and_deduplicates_signal() {
        let dir = temp("sanitize");
        let db = dir.join("logs_2.sqlite");
        let connection = log_db(&db);
        let mut recorder = Recorder::open(db, None, None, dir.join("out")).unwrap();
        connection.execute("INSERT INTO logs (ts,ts_nanos,level,target,feedback_log_body,process_uuid) VALUES (?1,0,'ERROR','codex_app_server::queue',?2,'p')", rusqlite::params![Utc::now().timestamp(), "queue full rejected SECRET_PROMPT_9981"]).unwrap();
        assert_eq!(recorder.scan().unwrap(), 1);
        assert_eq!(recorder.scan().unwrap(), 0);
        let stored = fs::read_to_string(dir.join("out/codex-appserver.jsonl")).unwrap();
        assert!(stored.contains("queue_admission_rejected"));
        assert!(!stored.contains("SECRET_PROMPT_9981"));
        drop(connection);
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn request_pressure_emits_once() {
        let dir = temp("pressure");
        let db = dir.join("logs_2.sqlite");
        let connection = log_db(&db);
        let mut recorder = Recorder::open(db, None, None, dir.join("out")).unwrap();
        let now = Utc::now().timestamp();
        for offset in 0..8 {
            connection.execute("INSERT INTO logs (ts,ts_nanos,level,target,feedback_log_body,process_uuid) VALUES (?1,0,'TRACE','codex_app_server::message_processor','app-server request: thread/resume connection_id=0','p')", [now + offset]).unwrap();
        }
        assert_eq!(recorder.scan().unwrap(), 1);
        assert_eq!(
            fs::read_to_string(dir.join("out/codex-appserver.jsonl"))
                .unwrap()
                .lines()
                .count(),
            1
        );
        drop(connection);
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn integrity_error_retains_only_a_canonical_thread_id() {
        let dir = temp("integrity");
        let db = dir.join("logs_2.sqlite");
        let connection = log_db(&db);
        let mut recorder = Recorder::open(db, None, None, dir.join("out")).unwrap();
        let body = "Custom tool call output is missing for call id: call_SECRET_9981 thread_id=01A0A486-D5FD-7423-BC01-825B6B77072F extra=SECRET_PROMPT";
        connection.execute("INSERT INTO logs (ts,ts_nanos,level,target,feedback_log_body,process_uuid) VALUES (?1,0,'ERROR','codex_core::util',?2,'pid:42:test')", rusqlite::params![Utc::now().timestamp(), body]).unwrap();
        assert_eq!(recorder.scan().unwrap(), 1);
        let stored = fs::read_to_string(dir.join("out/codex-appserver.jsonl")).unwrap();
        assert!(stored.contains("turn_input_integrity_error"));
        assert!(stored.contains("01a0a486-d5fd-7423-bc01-825b6b77072f"));
        assert!(!stored.contains("call_SECRET_9981"));
        assert!(!stored.contains("SECRET_PROMPT"));
        drop(connection);
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn integrity_error_without_a_canonical_thread_id_is_not_recorded() {
        let dir = temp("integrity-invalid");
        let db = dir.join("logs_2.sqlite");
        let connection = log_db(&db);
        let mut recorder = Recorder::open(db, None, None, dir.join("out")).unwrap();
        let body =
            "Custom tool call output is missing for call id: call_SECRET thread_id=not-a-thread";
        connection.execute("INSERT INTO logs (ts,ts_nanos,level,target,feedback_log_body,process_uuid) VALUES (?1,0,'ERROR','codex_core::util',?2,'pid:42:test')", rusqlite::params![Utc::now().timestamp(), body]).unwrap();
        assert_eq!(recorder.scan().unwrap(), 0);
        assert!(!dir.join("out/codex-appserver.jsonl").exists());
        drop(connection);
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn transcript_probe_is_read_only_and_content_free() {
        let dir = temp("transcript");
        let path = dir.join("rollout.jsonl");
        let body = b"{\"type\":\"event_msg\",\"payload\":{\"type\":\"task_complete\",\"message\":\"SECRET_TRANSCRIPT\"}}\n";
        fs::write(&path, body).unwrap();
        let before = fs::read(&path).unwrap();
        let probe = probe_transcript("thread-a", &path).unwrap();
        assert_eq!(before, fs::read(&path).unwrap());
        assert!(probe.ends_with_newline && probe.final_json_object);
        let stored = serde_json::to_string(&probe).unwrap();
        assert!(!stored.contains("SECRET_TRANSCRIPT"));
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn live_file_event_emits_once_and_shutdown_is_bounded() {
        let dir = temp("live");
        let db = dir.join("logs_2.sqlite");
        let connection = log_db(&db);
        let app_data = dir.join("app-data");
        let handle = BlackBoxHandle::spawn(&dir, None, None, &app_data).unwrap();
        std::thread::sleep(Duration::from_millis(150));
        connection.execute("INSERT INTO logs (ts,ts_nanos,level,target,feedback_log_body,process_uuid) VALUES (?1,0,'ERROR','codex_app_server::queue','queue capacity full rejected','p')", [Utc::now().timestamp()]).unwrap();
        let journal = app_data.join("diagnostics/codex-appserver.jsonl");
        let deadline = Instant::now() + Duration::from_secs(5);
        while !journal.exists() && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(25));
        }
        assert!(
            journal.exists(),
            "filesystem event did not produce a record"
        );
        assert_eq!(fs::read_to_string(&journal).unwrap().lines().count(), 1);
        let shutdown = Instant::now();
        drop(handle);
        assert!(shutdown.elapsed() < Duration::from_secs(1));
        drop(connection);
        fs::remove_dir_all(dir).unwrap();
    }
}
