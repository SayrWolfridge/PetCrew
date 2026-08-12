use axum::{
    extract::{rejection::JsonRejection, DefaultBodyLimit, Query, State},
    http::{
        header::{AUTHORIZATION, CONTENT_TYPE},
        HeaderMap, HeaderValue, Method, StatusCode,
    },
    response::{
        sse::{Event as SseEvent, KeepAlive, Sse},
        IntoResponse, Response,
    },
    routing::{get, post},
    Json, Router,
};
use chrono::{DateTime, Utc};
use futures_util::stream::{self, Stream};
use rusqlite::{Connection, OpenFlags};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    collections::{HashMap, HashSet, VecDeque},
    convert::Infallible,
    fs,
    io::{BufRead, BufReader, Read, Seek, SeekFrom, Write},
    path::{Path, PathBuf},
    sync::{Arc, Mutex, OnceLock},
    time::Duration,
};
use tower_http::{cors::CorsLayer, timeout::TimeoutLayer};

#[cfg(test)]
use crate::core_ownership::process_is_alive;
use crate::core_ownership::{acquire_core_ownership, CoreOwnership};

#[cfg(feature = "desktop")]
use tauri::{AppHandle, Emitter, Manager, State as TauriState};

use rand::RngCore;
use std::net::{Ipv4Addr, SocketAddrV4, TcpListener as StdTcpListener};

const PROTOCOL_VERSION: &str = "1.0";
const MAX_BODY_BYTES: usize = 64 * 1024;
const MAX_SEEN_EVENT_IDS: usize = 2048;
const MAX_RETAINED_AGENTS: usize = 100;
const MAX_COMPLETION_RECORDS: usize = 512;
const REGISTRY_FOLDER: &str = "agent-registry";
const MAX_REGISTRY_FILES: usize = 500;
const MAX_REGISTRY_EVENT_BYTES: u64 = 64 * 1024;
const ACTIVE_REGISTRY_TTL_HOURS: i64 = 24;
const TERMINAL_REGISTRY_TTL_HOURS: i64 = 24 * 7;
const CODEX_DISCOVERY_TTL_MINUTES: i64 = 15;
const CODEX_DISCOVERY_RECOVERY_HOURS: i64 = 24;
const CODEX_DISCOVERY_MISSING_GRACE_SECONDS: i64 = 60;
const HUB_POLL_INTERVAL_SECONDS: u64 = 5;
const MAX_ROLLOUT_TAIL_BYTES: u64 = 1024 * 1024;
#[cfg(feature = "desktop")]
const SNAPSHOT_EVENT: &str = "petcrew://snapshot";

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Provider {
    Codex,
    Opencode,
    Simulator,
}

impl Provider {
    fn as_str(self) -> &'static str {
        match self {
            Self::Codex => "codex",
            Self::Opencode => "opencode",
            Self::Simulator => "simulator",
        }
    }
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum AgentPhase {
    Queued,
    Planning,
    Working,
    WaitingInput,
    WaitingApproval,
    Blocked,
    Completed,
    Failed,
    Cancelled,
}

impl AgentPhase {
    fn is_terminal(self) -> bool {
        matches!(self, Self::Completed | Self::Failed | Self::Cancelled)
    }

    fn as_str(self) -> &'static str {
        match self {
            Self::Queued => "queued",
            Self::Planning => "planning",
            Self::Working => "working",
            Self::WaitingInput => "waiting_input",
            Self::WaitingApproval => "waiting_approval",
            Self::Blocked => "blocked",
            Self::Completed => "completed",
            Self::Failed => "failed",
            Self::Cancelled => "cancelled",
        }
    }
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub enum EventType {
    #[serde(rename = "agent.discovered")]
    Discovered,
    #[serde(rename = "agent.started")]
    Started,
    #[serde(rename = "agent.progress")]
    Progress,
    #[serde(rename = "agent.activity")]
    Activity,
    #[serde(rename = "agent.attention_requested")]
    AttentionRequested,
    #[serde(rename = "agent.attention_resolved")]
    AttentionResolved,
    #[serde(rename = "agent.completed")]
    Completed,
    #[serde(rename = "agent.failed")]
    Failed,
    #[serde(rename = "agent.cancelled")]
    Cancelled,
    #[serde(rename = "agent.acknowledged")]
    Acknowledged,
}

impl EventType {
    fn is_terminal(self) -> bool {
        matches!(self, Self::Completed | Self::Failed | Self::Cancelled)
    }
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ProgressKind {
    Steps,
    Activity,
    Indeterminate,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ProgressSource {
    Explicit,
    Inferred,
    Unavailable,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct EventProgress {
    kind: ProgressKind,
    current: Option<u64>,
    total: Option<u64>,
    label: String,
    source: ProgressSource,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ChangeSummaryPayload {
    files: u64,
    additions: u64,
    deletions: u64,
    source: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ProjectPayload {
    id: String,
    name: String,
    path: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct TaskPayload {
    title: String,
    detail: Option<String>,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum AttentionKind {
    Input,
    Approval,
    Blocked,
    Failure,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct AttentionPayload {
    kind: AttentionKind,
    summary: String,
    requested_at: String,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ResultOutcome {
    Success,
    Failure,
    Cancelled,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ResultPayload {
    summary: String,
    outcome: ResultOutcome,
    completed_at: String,
    unread: bool,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct NavigationPayload {
    kind: String,
    label: String,
    target: String,
}

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct EventPayload {
    project: Option<ProjectPayload>,
    task: Option<TaskPayload>,
    phase: Option<AgentPhase>,
    progress: Option<EventProgress>,
    change_summary: Option<ChangeSummaryPayload>,
    current_action: Option<String>,
    started_at: Option<String>,
    raw_tool_name: Option<String>,
    attention: Option<AttentionPayload>,
    result: Option<ResultPayload>,
    navigation: Option<NavigationPayload>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct AgentEvent {
    protocol_version: String,
    event_id: String,
    sequence: u64,
    occurred_at: String,
    provider: Provider,
    session_id: String,
    agent_id: String,
    parent_agent_id: Option<String>,
    event_type: EventType,
    payload: EventPayload,
}

impl AgentEvent {
    fn key(&self) -> String {
        format!(
            "{}:{}:{}",
            self.provider.as_str(),
            self.session_id,
            self.agent_id
        )
    }

    fn validate(&self) -> Result<(), ApplyError> {
        if self.protocol_version != PROTOCOL_VERSION {
            return Err(ApplyError::Invalid("unsupported_protocol"));
        }
        validate_required_text(&self.event_id, 200, "invalid_event_id")?;
        validate_required_text(&self.session_id, 300, "invalid_session_id")?;
        validate_required_text(&self.agent_id, 300, "invalid_agent_id")?;
        if let Some(parent) = &self.parent_agent_id {
            validate_optional_text(parent, 300, "invalid_parent_agent_id")?;
        }
        validate_timestamp(&self.occurred_at, "invalid_occurred_at")?;

        if let Some(project) = &self.payload.project {
            validate_required_text(&project.id, 200, "invalid_project")?;
            validate_required_text(&project.name, 120, "invalid_project")?;
            if let Some(path) = &project.path {
                validate_optional_text(path, 500, "invalid_project")?;
            }
        }
        if let Some(task) = &self.payload.task {
            validate_required_text(&task.title, 120, "invalid_task")?;
            if let Some(detail) = &task.detail {
                validate_optional_text(detail, 1000, "invalid_task")?;
            }
        }
        if let Some(action) = &self.payload.current_action {
            validate_optional_text(action, 160, "invalid_current_action")?;
        }
        if let Some(started_at) = &self.payload.started_at {
            validate_timestamp(started_at, "invalid_started_at")?;
        }
        if let Some(tool) = &self.payload.raw_tool_name {
            validate_optional_text(tool, 160, "invalid_tool_name")?;
        }
        if let Some(progress) = &self.payload.progress {
            validate_optional_text(&progress.label, 160, "invalid_progress")?;
            match progress.kind {
                ProgressKind::Steps => {
                    let (Some(current), Some(total)) = (progress.current, progress.total) else {
                        return Err(ApplyError::Invalid("invalid_progress"));
                    };
                    if progress.source != ProgressSource::Explicit || total == 0 || current > total
                    {
                        return Err(ApplyError::Invalid("invalid_progress"));
                    }
                }
                ProgressKind::Activity | ProgressKind::Indeterminate => {
                    if progress.current.is_some() || progress.total.is_some() {
                        return Err(ApplyError::Invalid("invalid_progress"));
                    }
                }
            }
        }
        if let Some(summary) = &self.payload.change_summary {
            if summary.source != "provider" {
                return Err(ApplyError::Invalid("invalid_change_summary"));
            }
        }
        if let Some(attention) = &self.payload.attention {
            validate_required_text(&attention.summary, 500, "invalid_attention")?;
            validate_timestamp(&attention.requested_at, "invalid_attention")?;
        }
        if let Some(result) = &self.payload.result {
            validate_required_text(&result.summary, 500, "invalid_result")?;
            validate_timestamp(&result.completed_at, "invalid_result")?;
        }
        if let Some(navigation) = &self.payload.navigation {
            if !matches!(
                navigation.kind.as_str(),
                "task" | "terminal" | "folder" | "provider"
            ) {
                return Err(ApplyError::Invalid("invalid_navigation"));
            }
            validate_required_text(&navigation.label, 100, "invalid_navigation")?;
            validate_required_text(&navigation.target, 1000, "invalid_navigation")?;
        }

        match self.event_type {
            EventType::Progress if self.payload.progress.is_none() => {
                Err(ApplyError::Invalid("missing_progress"))
            }
            EventType::AttentionRequested if self.payload.attention.is_none() => {
                Err(ApplyError::Invalid("missing_attention"))
            }
            EventType::Completed if self.payload.result.is_none() => {
                Err(ApplyError::Invalid("missing_result"))
            }
            EventType::Failed => match &self.payload.result {
                Some(result) if result.outcome == ResultOutcome::Failure => Ok(()),
                _ => Err(ApplyError::Invalid("missing_failure_result")),
            },
            _ => Ok(()),
        }
    }
}

fn canonical_opencode_root_agent_id(session_id: &str) -> Option<String> {
    let digest = session_id.strip_prefix("session:")?;
    (digest.len() == 64
        && digest
            .chars()
            .all(|character| character.is_ascii_hexdigit()))
    .then(|| format!("root:{}", digest.to_ascii_lowercase()))
}

fn valid_prefixed_digest(value: &str, prefix: &str) -> bool {
    value
        .strip_prefix(prefix)
        .map(|digest| {
            digest.len() == 64
                && digest
                    .chars()
                    .all(|character| character.is_ascii_hexdigit())
        })
        .unwrap_or(false)
}

fn valid_opencode_agent_identity(value: &str) -> bool {
    valid_prefixed_digest(value, "root:") || valid_prefixed_digest(value, "turn:")
}

fn valid_codex_completion_identity(value: &str) -> bool {
    valid_prefixed_digest(value, "turn:")
}

fn normalize_opencode_root_event(event: &mut AgentEvent) {
    if event.provider == Provider::Opencode && event.parent_agent_id.is_none() {
        if let Some(agent_id) = canonical_opencode_root_agent_id(&event.session_id) {
            event.agent_id = agent_id;
        }
    }
}

fn normalize_opencode_root_snapshot(agent: &mut AgentSnapshot) {
    if agent.provider == Provider::Opencode && agent.parent_agent_id.is_none() {
        if let Some(agent_id) = canonical_opencode_root_agent_id(&agent.session_id) {
            agent.agent_id = agent_id;
            agent.key = format!("opencode:{}:{}", agent.session_id, agent.agent_id);
        }
    }
}

fn snapshot_is_newer(candidate: &AgentSnapshot, current: &AgentSnapshot) -> bool {
    let candidate_time = DateTime::parse_from_rfc3339(&candidate.updated_at)
        .map(|timestamp| timestamp.timestamp_millis())
        .unwrap_or(i64::MIN);
    let current_time = DateTime::parse_from_rfc3339(&current.updated_at)
        .map(|timestamp| timestamp.timestamp_millis())
        .unwrap_or(i64::MIN);
    (candidate_time, candidate.last_sequence) > (current_time, current.last_sequence)
}

fn proves_new_opencode_root_turn(event: &AgentEvent, current: &AgentSnapshot) -> bool {
    if event.provider != Provider::Opencode || event.parent_agent_id.is_some() {
        return false;
    }
    let Some(started_at) = event.payload.started_at.as_deref() else {
        return false;
    };
    let Ok(started_at) = DateTime::parse_from_rfc3339(started_at) else {
        return false;
    };
    let Ok(completed_at) = DateTime::parse_from_rfc3339(&current.updated_at) else {
        return false;
    };
    started_at > completed_at
}

fn recovered_codex_start_is_newer(event: &AgentEvent, current: &AgentSnapshot) -> bool {
    if event.provider != Provider::Codex
        || !event.agent_id.starts_with("bootstrap-")
        || !current.phase.is_terminal()
    {
        return false;
    }
    let Some(started_at) = event.payload.started_at.as_deref() else {
        return false;
    };
    let Ok(started_at) = DateTime::parse_from_rfc3339(started_at) else {
        return false;
    };
    let Ok(completed_at) = DateTime::parse_from_rfc3339(&current.updated_at) else {
        return false;
    };
    started_at > completed_at
}

fn proves_new_recovered_codex_turn(event: &AgentEvent, current: &AgentSnapshot) -> bool {
    recovered_codex_start_is_newer(event, current)
        || (event.provider == Provider::Codex
            && event.agent_id.starts_with("bootstrap-")
            && current.phase.is_terminal()
            && event.sequence > current.last_sequence)
}

fn validate_required_text(
    value: &str,
    max_chars: usize,
    code: &'static str,
) -> Result<(), ApplyError> {
    let length = value.chars().count();
    if length == 0 || length > max_chars {
        return Err(ApplyError::Invalid(code));
    }
    Ok(())
}

fn validate_optional_text(
    value: &str,
    max_chars: usize,
    code: &'static str,
) -> Result<(), ApplyError> {
    if value.chars().count() > max_chars {
        return Err(ApplyError::Invalid(code));
    }
    Ok(())
}

fn validate_timestamp(value: &str, code: &'static str) -> Result<(), ApplyError> {
    DateTime::parse_from_rfc3339(value)
        .map(|_| ())
        .map_err(|_| ApplyError::Invalid(code))
}

fn contains_sensitive_marker(value: &str) -> bool {
    let lower = value.to_ascii_lowercase();
    [
        "bearer ",
        "password=",
        "password:",
        "api_key=",
        "apikey=",
        "access_token=",
        "secret=",
        "sk-",
    ]
    .iter()
    .any(|marker| lower.contains(marker))
}

fn redact(value: &str, fallback: &str) -> String {
    if contains_sensitive_marker(value) {
        fallback.to_string()
    } else {
        value.to_string()
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct AgentProgress {
    kind: ProgressKind,
    current: Option<u64>,
    total: Option<u64>,
    source: ProgressSource,
}

impl AgentProgress {
    fn unavailable() -> Self {
        Self {
            kind: ProgressKind::Indeterminate,
            current: None,
            total: None,
            source: ProgressSource::Unavailable,
        }
    }
}

impl From<&EventProgress> for AgentProgress {
    fn from(progress: &EventProgress) -> Self {
        Self {
            kind: progress.kind,
            current: progress.current,
            total: progress.total,
            source: progress.source,
        }
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct AgentSnapshot {
    key: String,
    provider: Provider,
    session_id: String,
    agent_id: String,
    parent_agent_id: Option<String>,
    project: String,
    task: String,
    phase: AgentPhase,
    progress: AgentProgress,
    #[serde(default)]
    change_summary: Option<ChangeSummaryPayload>,
    current_action: String,
    #[serde(default)]
    started_at: Option<String>,
    result: Option<String>,
    unread: bool,
    #[serde(default)]
    navigation: Option<NavigationPayload>,
    last_sequence: u64,
    updated_at: String,
}

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
pub struct HubSnapshot {
    revision: u64,
    agents: Vec<AgentSnapshot>,
    overflow: usize,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
struct CompletionRecord {
    cursor: u64,
    completion_id: String,
    provider: Provider,
    session_id: String,
    agent_id: String,
    parent_agent_id: Option<String>,
    phase: AgentPhase,
    completed_at: String,
}

impl CompletionRecord {
    fn is_valid(&self) -> bool {
        let valid_identity = match self.provider {
            Provider::Opencode => {
                valid_opencode_agent_identity(&self.agent_id)
                    && self
                        .parent_agent_id
                        .as_deref()
                        .map(valid_opencode_agent_identity)
                        .unwrap_or(true)
            }
            Provider::Codex => {
                valid_codex_completion_identity(&self.agent_id) && self.parent_agent_id.is_none()
            }
            Provider::Simulator => false,
        };
        self.cursor > 0
            && valid_prefixed_digest(&self.completion_id, "completion:")
            && valid_prefixed_digest(&self.session_id, "session:")
            && valid_identity
            && self.phase.is_terminal()
            && DateTime::parse_from_rfc3339(&self.completed_at).is_ok()
    }
}

#[derive(Clone, Debug)]
struct CodexCompletionCandidate {
    session_id: String,
    agent_id: String,
    phase: AgentPhase,
    completed_at: String,
}

#[derive(Clone, Debug, Serialize)]
struct CompletionInbox {
    protocol_version: &'static str,
    oldest_cursor: u64,
    latest_cursor: u64,
    truncated: bool,
    completions: Vec<CompletionRecord>,
}

#[derive(Debug)]
enum ApplyError {
    Invalid(&'static str),
    Replay,
    StaleSequence,
    TerminalState,
    MissingAgent,
}

#[derive(Debug, Default, Deserialize, Serialize)]
struct CacheFile {
    revision: u64,
    agents: Vec<AgentSnapshot>,
    seen_event_ids: Vec<String>,
    #[serde(default)]
    completion_cursor: u64,
    #[serde(default)]
    completions: Vec<CompletionRecord>,
}

#[derive(Debug)]
struct EventStore {
    revision: u64,
    agents: HashMap<String, AgentSnapshot>,
    seen_event_ids: HashSet<String>,
    seen_order: VecDeque<String>,
    completion_cursor: u64,
    completions: VecDeque<CompletionRecord>,
    recovered_missing_since: HashMap<String, DateTime<Utc>>,
    cache_path: Option<PathBuf>,
}

impl EventStore {
    fn load(cache_path: Option<PathBuf>) -> Self {
        let cached = cache_path
            .as_ref()
            .and_then(|path| fs::read(path).ok())
            .and_then(|bytes| serde_json::from_slice::<CacheFile>(&bytes).ok());

        if let Some(cache) = cached {
            let mut migrated = false;
            let mut agents: HashMap<String, AgentSnapshot> = HashMap::new();
            for mut agent in cache.agents {
                let original_key = agent.key.clone();
                normalize_opencode_root_snapshot(&mut agent);
                migrated |= agent.key != original_key;
                let key = agent.key.clone();
                if let Some(current) = agents.get(&key) {
                    migrated = true;
                    if !snapshot_is_newer(&agent, current) {
                        continue;
                    }
                }
                agents.insert(key, agent);
            }
            let seen_order: VecDeque<String> = cache
                .seen_event_ids
                .into_iter()
                .take(MAX_SEEN_EVENT_IDS)
                .collect();
            let seen_event_ids = seen_order.iter().cloned().collect();
            let mut completion_records = cache.completions;
            completion_records.retain(CompletionRecord::is_valid);
            completion_records.sort_by_key(|record| record.cursor);
            completion_records.dedup_by(|left, right| {
                left.cursor == right.cursor || left.completion_id == right.completion_id
            });
            let completion_cursor = cache.completion_cursor.max(
                completion_records
                    .last()
                    .map(|record| record.cursor)
                    .unwrap_or_default(),
            );
            let mut store = Self {
                revision: cache.revision,
                agents,
                seen_event_ids,
                seen_order,
                completion_cursor,
                completions: completion_records.into(),
                recovered_missing_since: HashMap::new(),
                cache_path,
            };
            store.prune_acknowledged_history();
            store.prune_completions(Utc::now());
            if migrated {
                store.revision = store.revision.saturating_add(1);
                store.persist();
            }
            store
        } else {
            Self {
                revision: 0,
                agents: HashMap::new(),
                seen_event_ids: HashSet::new(),
                seen_order: VecDeque::new(),
                completion_cursor: 0,
                completions: VecDeque::new(),
                recovered_missing_since: HashMap::new(),
                cache_path,
            }
        }
    }

    fn snapshot(&self) -> HubSnapshot {
        let mut agents: Vec<_> = self.agents.values().cloned().collect();
        agents.sort_by(|left, right| left.key.cmp(&right.key));
        HubSnapshot {
            revision: self.revision,
            overflow: agents.len().saturating_sub(MAX_RETAINED_AGENTS),
            agents,
        }
    }

    fn prune_acknowledged_history(&mut self) {
        while self.agents.len() > MAX_RETAINED_AGENTS {
            let oldest_key = self
                .agents
                .iter()
                .filter(|(_, agent)| agent.phase.is_terminal() && !agent.unread)
                .min_by(|(left_key, left), (right_key, right)| {
                    let left_time = DateTime::parse_from_rfc3339(&left.updated_at)
                        .map(|timestamp| timestamp.timestamp_millis())
                        .unwrap_or(i64::MIN);
                    let right_time = DateTime::parse_from_rfc3339(&right.updated_at)
                        .map(|timestamp| timestamp.timestamp_millis())
                        .unwrap_or(i64::MIN);
                    left_time
                        .cmp(&right_time)
                        .then_with(|| left_key.cmp(right_key))
                })
                .map(|(key, _)| key.clone());

            let Some(oldest_key) = oldest_key else {
                break;
            };
            self.agents.remove(&oldest_key);
        }
    }

    fn persist(&self) {
        let Some(path) = &self.cache_path else {
            return;
        };
        let mut agents = self.snapshot().agents;
        for agent in &mut agents {
            agent.navigation = None;
        }
        let cache = CacheFile {
            revision: self.revision,
            agents,
            seen_event_ids: self.seen_order.iter().cloned().collect(),
            completion_cursor: self.completion_cursor,
            completions: self.completions.iter().cloned().collect(),
        };
        if let Ok(bytes) = serde_json::to_vec_pretty(&cache) {
            let _ = fs::write(path, bytes);
        }
    }

    fn remember_event(&mut self, event_id: String) {
        if self.seen_order.len() >= MAX_SEEN_EVENT_IDS {
            if let Some(expired) = self.seen_order.pop_front() {
                self.seen_event_ids.remove(&expired);
            }
        }
        self.seen_event_ids.insert(event_id.clone());
        self.seen_order.push_back(event_id);
    }

    fn append_completion(
        &mut self,
        provider: Provider,
        session_id: String,
        agent_id: String,
        parent_agent_id: Option<String>,
        phase: AgentPhase,
        completed_at: String,
        completion_id: String,
    ) -> bool {
        let record = CompletionRecord {
            cursor: self.completion_cursor.saturating_add(1),
            completion_id,
            provider,
            session_id,
            agent_id,
            parent_agent_id,
            phase,
            completed_at,
        };
        if !record.is_valid()
            || self
                .completions
                .iter()
                .any(|current| current.completion_id == record.completion_id)
        {
            return false;
        }
        self.completion_cursor = record.cursor;
        self.completions.push_back(record);
        self.prune_completions(Utc::now());
        true
    }

    fn codex_completion_id(session_id: &str, agent_id: &str, phase: AgentPhase) -> String {
        let mut hasher = Sha256::new();
        hasher.update(b"codex\0");
        hasher.update(session_id.as_bytes());
        hasher.update(b"\0");
        hasher.update(agent_id.as_bytes());
        hasher.update(b"\0");
        hasher.update(phase.as_str().as_bytes());
        format!("completion:{:x}", hasher.finalize())
    }

    fn record_codex_candidate(&mut self, candidate: CodexCompletionCandidate) -> bool {
        let completion_id =
            Self::codex_completion_id(&candidate.session_id, &candidate.agent_id, candidate.phase);
        self.append_completion(
            Provider::Codex,
            candidate.session_id,
            candidate.agent_id,
            None,
            candidate.phase,
            candidate.completed_at,
            completion_id,
        )
    }

    fn record_completion(&mut self, event: &AgentEvent) {
        if !event.event_type.is_terminal() {
            return;
        }
        let phase = match event.event_type {
            EventType::Completed => AgentPhase::Completed,
            EventType::Failed => AgentPhase::Failed,
            EventType::Cancelled => AgentPhase::Cancelled,
            _ => return,
        };
        let completed_at = event
            .payload
            .result
            .as_ref()
            .map(|result| result.completed_at.clone())
            .unwrap_or_else(|| event.occurred_at.clone());
        let completion_id = match event.provider {
            Provider::Opencode => {
                let mut hasher = Sha256::new();
                hasher.update(event.event_id.as_bytes());
                format!("completion:{:x}", hasher.finalize())
            }
            Provider::Codex => Self::codex_completion_id(&event.session_id, &event.agent_id, phase),
            Provider::Simulator => return,
        };
        let _ = self.append_completion(
            event.provider,
            event.session_id.clone(),
            event.agent_id.clone(),
            event.parent_agent_id.clone(),
            phase,
            completed_at,
            completion_id,
        );
    }

    fn prune_completions(&mut self, now: DateTime<Utc>) {
        while self.completions.len() > MAX_COMPLETION_RECORDS {
            self.completions.pop_front();
        }
        while let Some(record) = self.completions.front() {
            let expired = DateTime::parse_from_rfc3339(&record.completed_at)
                .map(|completed_at| {
                    now.signed_duration_since(completed_at.with_timezone(&Utc))
                        > chrono::Duration::hours(TERMINAL_REGISTRY_TTL_HOURS)
                })
                .unwrap_or(true);
            if !expired {
                break;
            }
            self.completions.pop_front();
        }
    }

    fn completion_inbox(&self, after: u64) -> CompletionInbox {
        let oldest_cursor = self
            .completions
            .front()
            .map(|record| record.cursor)
            .unwrap_or(self.completion_cursor);
        CompletionInbox {
            protocol_version: PROTOCOL_VERSION,
            oldest_cursor,
            latest_cursor: self.completion_cursor,
            truncated: !self.completions.is_empty() && after.saturating_add(1) < oldest_cursor,
            completions: self
                .completions
                .iter()
                .filter(|record| record.cursor > after)
                .cloned()
                .collect(),
        }
    }

    fn apply(&mut self, mut event: AgentEvent) -> Result<HubSnapshot, ApplyError> {
        event.validate()?;
        normalize_opencode_root_event(&mut event);
        if self.seen_event_ids.contains(&event.event_id) {
            return Err(ApplyError::Replay);
        }

        let key = event.key();
        self.recovered_missing_since.remove(&key);
        let recovered_event = event.agent_id.starts_with("bootstrap-");
        let opencode_root_new_turn = self
            .agents
            .get(&key)
            .map(|current| proves_new_opencode_root_turn(&event, current))
            .unwrap_or(false);
        let recovered_codex_new_turn = self
            .agents
            .get(&key)
            .map(|current| proves_new_recovered_codex_turn(&event, current))
            .unwrap_or(false);
        let recovered_codex_newer_start = self
            .agents
            .get(&key)
            .map(|current| recovered_codex_start_is_newer(&event, current))
            .unwrap_or(false);
        let existing_terminal = self
            .agents
            .get(&key)
            .map(|agent| agent.phase.is_terminal())
            .unwrap_or(false);
        if let Some(existing) = self.agents.get(&key) {
            let recovered_refresh = recovered_event && event.sequence == existing.last_sequence;
            if event.sequence < existing.last_sequence
                || (event.sequence == existing.last_sequence && !recovered_refresh)
            {
                return Err(ApplyError::StaleSequence);
            }
            if existing.phase.is_terminal()
                && event.event_type != EventType::Acknowledged
                && !recovered_event
                && !opencode_root_new_turn
            {
                return Err(ApplyError::TerminalState);
            }
        } else if event.event_type == EventType::Acknowledged {
            return Err(ApplyError::MissingAgent);
        }

        if event.provider == Provider::Codex {
            let recovered_agent_id = if event.agent_id.starts_with("turn:") {
                event
                    .session_id
                    .strip_prefix("session:")
                    .map(|digest| format!("bootstrap-root:{digest}"))
            } else {
                event
                    .agent_id
                    .strip_prefix("agent:")
                    .map(|digest| format!("bootstrap-child:{digest}"))
            };
            if let Some(recovered_agent_id) = recovered_agent_id {
                let recovered_key = format!(
                    "{}:{}:{}",
                    event.provider.as_str(),
                    event.session_id,
                    recovered_agent_id
                );
                self.agents.remove(&recovered_key);
            }
        }

        let default_phase = match event.event_type {
            EventType::Started | EventType::Progress | EventType::Activity => AgentPhase::Working,
            EventType::AttentionRequested => AgentPhase::WaitingInput,
            EventType::Completed => AgentPhase::Completed,
            EventType::Failed => AgentPhase::Failed,
            EventType::Cancelled => AgentPhase::Cancelled,
            _ => AgentPhase::Queued,
        };
        let default_progress = event
            .payload
            .progress
            .as_ref()
            .map(AgentProgress::from)
            .unwrap_or_else(AgentProgress::unavailable);
        let default_action = event
            .payload
            .current_action
            .as_deref()
            .map(|value| redact(value, "Выполняет действие (детали скрыты)"))
            .unwrap_or_else(|| "Ожидает обновления".to_string());
        let initial_phase = event.payload.phase.unwrap_or(default_phase);
        let initial_started_at = event.payload.started_at.clone().or_else(|| {
            matches!(initial_phase, AgentPhase::Working | AgentPhase::Planning)
                .then(|| event.occurred_at.clone())
        });

        let agent = self
            .agents
            .entry(key.clone())
            .or_insert_with(|| AgentSnapshot {
                key,
                provider: event.provider,
                session_id: event.session_id.clone(),
                agent_id: event.agent_id.clone(),
                parent_agent_id: event.parent_agent_id.clone(),
                project: event
                    .payload
                    .project
                    .as_ref()
                    .map(|project| redact(&project.name, "Проект (название скрыто)"))
                    .unwrap_or_else(|| "Без проекта".to_string()),
                task: event
                    .payload
                    .task
                    .as_ref()
                    .map(|task| redact(&task.title, "Задача (детали скрыты)"))
                    .unwrap_or_else(|| "Новая задача".to_string()),
                phase: initial_phase,
                progress: default_progress,
                change_summary: event.payload.change_summary.clone(),
                current_action: default_action,
                started_at: initial_started_at,
                result: None,
                unread: false,
                navigation: event.payload.navigation.clone(),
                last_sequence: 0,
                updated_at: event.occurred_at.clone(),
            });

        agent.parent_agent_id = event.parent_agent_id.clone();
        if let Some(project) = &event.payload.project {
            agent.project = redact(&project.name, "Проект (название скрыто)");
        }
        if let Some(task) = &event.payload.task {
            agent.task = redact(&task.title, "Задача (детали скрыты)");
        }
        if let Some(progress) = &event.payload.progress {
            agent.progress = AgentProgress::from(progress);
            if event.payload.current_action.is_none() && !progress.label.is_empty() {
                agent.current_action = redact(&progress.label, "Выполняет шаг (детали скрыты)");
            }
        }
        if let Some(summary) = &event.payload.change_summary {
            agent.change_summary = Some(summary.clone());
        }
        if let Some(action) = &event.payload.current_action {
            agent.current_action = redact(action, "Выполняет действие (детали скрыты)");
        }
        if let Some(started_at) = &event.payload.started_at {
            if !recovered_event || agent.started_at.is_none() || recovered_codex_new_turn {
                agent.started_at = Some(started_at.clone());
            }
        }
        if let Some(navigation) = &event.payload.navigation {
            agent.navigation = Some(navigation.clone());
        }
        if let Some(phase) = event.payload.phase {
            agent.phase = phase;
        }
        if existing_terminal && opencode_root_new_turn {
            agent.result = None;
            agent.unread = false;
            agent.progress = event
                .payload
                .progress
                .as_ref()
                .map(AgentProgress::from)
                .unwrap_or_else(AgentProgress::unavailable);
            agent.change_summary = event.payload.change_summary.clone();
        }

        match event.event_type {
            EventType::Discovered => {
                if event.payload.phase.is_none() {
                    agent.phase = AgentPhase::Queued;
                }
                if matches!(agent.phase, AgentPhase::Working | AgentPhase::Planning) {
                    if existing_terminal {
                        agent.result = None;
                        agent.unread = false;
                        agent.started_at = if recovered_codex_newer_start {
                            event.payload.started_at.clone()
                        } else {
                            Some(event.occurred_at.clone())
                        };
                    } else if agent.started_at.is_none() {
                        agent.started_at = Some(event.occurred_at.clone());
                    }
                }
                if agent.phase.is_terminal() && (!existing_terminal || recovered_codex_new_turn) {
                    if recovered_codex_new_turn && !recovered_codex_newer_start {
                        agent.started_at = Some(event.occurred_at.clone());
                    }
                    if let Some(result) = &event.payload.result {
                        agent.result =
                            Some(redact(&result.summary, "Результат получен (детали скрыты)"));
                        agent.unread = result.unread;
                    }
                }
            }
            EventType::Started => {
                if event.payload.phase.is_none() {
                    agent.phase = AgentPhase::Working;
                }
                agent.started_at = event
                    .payload
                    .started_at
                    .clone()
                    .or_else(|| Some(event.occurred_at.clone()));
                agent.result = None;
                agent.unread = false;
                agent.progress = event
                    .payload
                    .progress
                    .as_ref()
                    .map(AgentProgress::from)
                    .unwrap_or_else(AgentProgress::unavailable);
                agent.change_summary = event.payload.change_summary.clone();
            }
            EventType::Progress | EventType::Activity => {
                if event.payload.phase.is_none() {
                    agent.phase = AgentPhase::Working;
                }
                if matches!(agent.phase, AgentPhase::Working | AgentPhase::Planning)
                    && agent.started_at.is_none()
                {
                    agent.started_at = Some(event.occurred_at.clone());
                }
            }
            EventType::AttentionRequested => {
                if let Some(attention) = &event.payload.attention {
                    if event.payload.current_action.is_none() {
                        agent.current_action =
                            redact(&attention.summary, "Требуется внимание (детали скрыты)");
                    }
                    if event.payload.phase.is_none() {
                        agent.phase = match attention.kind {
                            AttentionKind::Input => AgentPhase::WaitingInput,
                            AttentionKind::Approval => AgentPhase::WaitingApproval,
                            AttentionKind::Blocked => AgentPhase::Blocked,
                            AttentionKind::Failure => AgentPhase::Failed,
                        };
                    }
                }
            }
            EventType::AttentionResolved => {
                if event.payload.phase.is_none() {
                    agent.phase = AgentPhase::Working;
                }
                if event.payload.current_action.is_none() {
                    agent.current_action = "Продолжает работу".to_string();
                }
                if agent.started_at.is_none() {
                    agent.started_at = Some(event.occurred_at.clone());
                }
            }
            EventType::Completed | EventType::Failed | EventType::Cancelled => {
                agent.phase = match event.event_type {
                    EventType::Completed => AgentPhase::Completed,
                    EventType::Failed => AgentPhase::Failed,
                    _ => AgentPhase::Cancelled,
                };
                if let Some(result) = &event.payload.result {
                    agent.result =
                        Some(redact(&result.summary, "Результат получен (детали скрыты)"));
                    agent.unread = result.unread;
                }
            }
            EventType::Acknowledged => {
                agent.unread = false;
            }
        }

        agent.last_sequence = event.sequence;
        agent.updated_at = event.occurred_at.clone();
        self.record_completion(&event);
        self.revision = self.revision.saturating_add(1);
        self.remember_event(event.event_id);
        self.prune_acknowledged_history();
        self.persist();
        Ok(self.snapshot())
    }

    fn acknowledge(&mut self, key: &str) -> Result<HubSnapshot, ApplyError> {
        let Some(agent) = self.agents.get_mut(key) else {
            return Err(ApplyError::MissingAgent);
        };
        agent.unread = false;
        self.revision = self.revision.saturating_add(1);
        self.prune_acknowledged_history();
        self.persist();
        Ok(self.snapshot())
    }

    fn remove_keys(&mut self, keys: &HashSet<String>) -> bool {
        let original_len = self.agents.len();
        self.agents.retain(|key, _| !keys.contains(key));
        self.recovered_missing_since
            .retain(|key, _| !keys.contains(key));
        if self.agents.len() == original_len {
            return false;
        }
        self.revision = self.revision.saturating_add(1);
        self.persist();
        true
    }

    #[cfg_attr(not(feature = "desktop"), allow(dead_code))]
    fn retain_recovered(&mut self, active_keys: &HashSet<String>, now: DateTime<Utc>) -> bool {
        let mut remove_keys = Vec::new();
        for (key, agent) in &self.agents {
            if !agent.agent_id.starts_with("bootstrap-") {
                self.recovered_missing_since.remove(key);
                continue;
            }
            if active_keys.contains(key) {
                self.recovered_missing_since.remove(key);
                continue;
            }
            if agent.phase.is_terminal() {
                self.recovered_missing_since.remove(key);
                let keep = DateTime::parse_from_rfc3339(&agent.updated_at)
                    .map(|updated_at| {
                        now.signed_duration_since(updated_at.with_timezone(&Utc))
                            <= chrono::Duration::hours(TERMINAL_REGISTRY_TTL_HOURS)
                    })
                    .unwrap_or(false);
                if !keep {
                    remove_keys.push(key.clone());
                }
                continue;
            }
            let missing_since = self
                .recovered_missing_since
                .entry(key.clone())
                .or_insert(now);
            if now.signed_duration_since(*missing_since)
                > chrono::Duration::seconds(CODEX_DISCOVERY_MISSING_GRACE_SECONDS)
            {
                remove_keys.push(key.clone());
            }
        }
        if remove_keys.is_empty() {
            return false;
        }
        for key in remove_keys {
            self.agents.remove(&key);
            self.recovered_missing_since.remove(&key);
        }
        self.revision = self.revision.saturating_add(1);
        self.persist();
        true
    }

    fn merge_recovered_navigation(&mut self, event: &AgentEvent) -> Option<bool> {
        if event.provider != Provider::Codex || !event.agent_id.starts_with("bootstrap-") {
            return None;
        }
        let navigation = event.payload.navigation.as_ref()?;
        let child_digest = event.agent_id.strip_prefix("bootstrap-child:");
        let mut matched = false;
        let mut changed = false;
        for agent in self.agents.values_mut() {
            let same_agent = if let Some(child_digest) = child_digest {
                agent.agent_id == format!("agent:{child_digest}")
            } else {
                agent.agent_id.starts_with("turn:")
            };
            if agent.provider == Provider::Codex
                && agent.session_id == event.session_id
                && same_agent
            {
                matched = true;
                if agent.navigation.as_ref() != Some(navigation) {
                    agent.navigation = Some(navigation.clone());
                    changed = true;
                }
            }
        }
        if !matched {
            return None;
        }
        changed |= self.agents.remove(&event.key()).is_some();
        if changed {
            self.revision = self.revision.saturating_add(1);
            self.persist();
        }
        Some(changed)
    }

    #[cfg(feature = "desktop")]
    fn clear(&mut self) -> HubSnapshot {
        self.agents.clear();
        self.seen_event_ids.clear();
        self.seen_order.clear();
        self.completion_cursor = 0;
        self.completions.clear();
        self.revision = self.revision.saturating_add(1);
        self.persist();
        self.snapshot()
    }
}

#[derive(Clone)]
struct SharedStore(Arc<Mutex<EventStore>>);

#[derive(Default)]
struct RegistryScan {
    events: Vec<AgentEvent>,
    expired_active_keys: HashSet<String>,
}

fn registry_json_files(path: &Path) -> Vec<(PathBuf, std::time::SystemTime)> {
    let Ok(entries) = fs::read_dir(path) else {
        return Vec::new();
    };
    let mut files: Vec<_> = entries
        .flatten()
        .filter_map(|entry| {
            let path = entry.path();
            let metadata = entry.metadata().ok()?;
            if !metadata.is_file()
                || path.extension().and_then(|value| value.to_str()) != Some("json")
            {
                return None;
            }
            Some((
                path,
                metadata
                    .modified()
                    .unwrap_or(std::time::SystemTime::UNIX_EPOCH),
            ))
        })
        .collect();
    files.sort_by(|left, right| right.1.cmp(&left.1).then_with(|| left.0.cmp(&right.0)));
    files
}

fn scan_registry(path: &Path, now: DateTime<Utc>) -> RegistryScan {
    let mut scan = RegistryScan::default();
    for (index, (file, _)) in registry_json_files(path).into_iter().enumerate() {
        if index >= MAX_REGISTRY_FILES {
            let _ = fs::remove_file(file);
            continue;
        }
        let Ok(metadata) = fs::metadata(&file) else {
            continue;
        };
        if metadata.len() > MAX_REGISTRY_EVENT_BYTES {
            let _ = fs::remove_file(file);
            continue;
        }
        let Ok(bytes) = fs::read(&file) else {
            continue;
        };
        let Ok(event) = serde_json::from_slice::<AgentEvent>(&bytes) else {
            let _ = fs::remove_file(file);
            continue;
        };
        if event.validate().is_err() {
            let _ = fs::remove_file(file);
            continue;
        }
        let Ok(occurred_at) = DateTime::parse_from_rfc3339(&event.occurred_at) else {
            let _ = fs::remove_file(file);
            continue;
        };
        let ttl_hours = if event.event_type.is_terminal() {
            TERMINAL_REGISTRY_TTL_HOURS
        } else {
            ACTIVE_REGISTRY_TTL_HOURS
        };
        if now.signed_duration_since(occurred_at.with_timezone(&Utc))
            > chrono::Duration::hours(ttl_hours)
        {
            if !event.event_type.is_terminal() {
                scan.expired_active_keys.insert(event.key());
            }
            let _ = fs::remove_file(file);
            continue;
        }
        scan.events.push(event);
    }
    scan.events.sort_by(|left, right| {
        left.sequence
            .cmp(&right.sequence)
            .then_with(|| left.event_id.cmp(&right.event_id))
    });
    scan
}

fn import_registry(path: &Path, store: &SharedStore, now: DateTime<Utc>) -> Option<HubSnapshot> {
    let scan = scan_registry(path, now);
    let mut store = store.0.lock().ok()?;
    let mut changed = store.remove_keys(&scan.expired_active_keys);
    for event in scan.events {
        if let Some(merged) = store.merge_recovered_navigation(&event) {
            changed |= merged;
            continue;
        }
        match store.apply(event) {
            Ok(_) => changed = true,
            Err(ApplyError::Replay | ApplyError::StaleSequence | ApplyError::TerminalState) => {}
            Err(_) => {}
        }
    }
    changed.then(|| store.snapshot())
}

fn clear_registry(path: &Path) -> std::io::Result<()> {
    if !path.exists() {
        return Ok(());
    }
    for entry in fs::read_dir(path)? {
        let entry = entry?;
        let file = entry.path();
        if entry.file_type()?.is_file()
            && matches!(
                file.extension().and_then(|value| value.to_str()),
                Some("json" | "tmp")
            )
        {
            fs::remove_file(file)?;
        }
    }
    Ok(())
}

#[derive(Debug, Deserialize)]
struct SessionIndexRow {
    id: String,
    thread_name: String,
}

#[derive(Default)]
struct CodexDiscoveryScan {
    events: Vec<AgentEvent>,
    active_keys: HashSet<String>,
    completions: Vec<CodexCompletionCandidate>,
}

fn opaque_digest(value: &str) -> String {
    format!("{:x}", Sha256::digest(value.as_bytes()))
}

fn safe_discovery_label(value: Option<&str>, fallback: &str, max_chars: usize) -> String {
    let Some(value) = value else {
        return fallback.to_string();
    };
    let cleaned = value.split_whitespace().collect::<Vec<_>>().join(" ");
    if cleaned.is_empty() || contains_sensitive_marker(&cleaned) {
        return fallback.to_string();
    }
    cleaned.chars().take(max_chars).collect()
}

fn safe_assistant_status(value: &str) -> Option<String> {
    let cleaned = value.split_whitespace().collect::<Vec<_>>().join(" ");
    if cleaned.is_empty()
        || contains_sensitive_marker(&cleaned)
        || serde_json::from_str::<serde_json::Value>(&cleaned).is_ok()
    {
        return None;
    }
    Some(cleaned.chars().take(160).collect())
}

fn session_names(path: &Path) -> HashMap<String, String> {
    let Ok(contents) = fs::read_to_string(path) else {
        return HashMap::new();
    };
    contents
        .lines()
        .filter_map(|line| serde_json::from_str::<SessionIndexRow>(line).ok())
        .map(|row| {
            let label = safe_discovery_label(Some(&row.thread_name), "Задача Codex", 120);
            (row.id, label)
        })
        .collect()
}

#[derive(Debug, Deserialize)]
struct RolloutEnvelope {
    timestamp: Option<String>,
    #[serde(rename = "type")]
    record_type: String,
    payload: RolloutPayload,
}

#[derive(Debug, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
enum RolloutPayload {
    TaskStarted {
        turn_id: String,
    },
    AgentMessage {
        message: String,
    },
    TaskComplete {
        turn_id: String,
    },
    FunctionCall {
        name: String,
        call_id: String,
    },
    FunctionCallOutput {
        call_id: String,
    },
    #[serde(other)]
    Other,
}

#[derive(Clone, Default)]
struct RolloutState {
    active_turn: Option<String>,
    started_at: Option<String>,
    completed_turn: Option<String>,
    completed_at: Option<String>,
    last_message: Option<String>,
    pending_input_call: Option<String>,
}

impl RolloutState {
    fn is_working(&self) -> Option<bool> {
        self.active_turn.as_ref().map(|turn_id| !turn_id.is_empty())
    }

    fn is_waiting_input(&self) -> bool {
        self.is_working() == Some(true) && self.pending_input_call.is_some()
    }
}

#[derive(Clone)]
struct CachedRolloutState {
    length: u64,
    state: RolloutState,
}

static CODEX_ROLLOUT_STATES: OnceLock<Mutex<HashMap<PathBuf, CachedRolloutState>>> =
    OnceLock::new();

fn apply_rollout_record(line: &str, state: &mut RolloutState) -> bool {
    let Ok(envelope) = serde_json::from_str::<RolloutEnvelope>(line) else {
        return false;
    };
    let timestamp = envelope
        .timestamp
        .filter(|value| DateTime::parse_from_rfc3339(value).is_ok());
    match (envelope.record_type.as_str(), envelope.payload) {
        ("event_msg", RolloutPayload::TaskStarted { turn_id }) => {
            state.active_turn = Some(turn_id);
            state.started_at = timestamp;
            state.completed_turn = None;
            state.completed_at = None;
            state.last_message = None;
            state.pending_input_call = None;
            true
        }
        ("event_msg", RolloutPayload::AgentMessage { message }) => {
            if let Some(message) = safe_assistant_status(&message) {
                state.last_message = Some(message);
            }
            false
        }
        ("event_msg", RolloutPayload::TaskComplete { turn_id }) => {
            if !turn_id.trim().is_empty() {
                state.completed_turn = Some(turn_id);
            }
            state.completed_at = timestamp;
            state.active_turn = Some(String::new());
            state.pending_input_call = None;
            true
        }
        ("response_item", RolloutPayload::FunctionCall { name, call_id })
            if matches!(
                name.as_str(),
                "request_user_input" | "functions.request_user_input"
            ) && !call_id.trim().is_empty() =>
        {
            state.pending_input_call = Some(call_id);
            true
        }
        ("response_item", RolloutPayload::FunctionCallOutput { call_id })
            if state.pending_input_call.as_deref() == Some(call_id.as_str()) =>
        {
            state.pending_input_call = None;
            true
        }
        _ => false,
    }
}

fn scan_rollout_range(
    file: &mut fs::File,
    start: u64,
    end: u64,
    skip_partial_first_line: bool,
    state: &mut RolloutState,
) -> Option<bool> {
    file.seek(SeekFrom::Start(start)).ok()?;
    let mut reader = BufReader::new(file.take(end.saturating_sub(start)));
    if skip_partial_first_line {
        let mut partial = String::new();
        reader.read_line(&mut partial).ok()?;
    }
    let mut line = String::new();
    let mut saw_lifecycle = false;
    loop {
        line.clear();
        if reader.read_line(&mut line).ok()? == 0 {
            break;
        }
        saw_lifecycle |= apply_rollout_record(&line, state);
    }
    Some(saw_lifecycle)
}

fn rollout_state(path: &Path, sessions_root: &Path) -> Option<RolloutState> {
    let canonical_root = fs::canonicalize(sessions_root).ok()?;
    let canonical_path = fs::canonicalize(path).ok()?;
    if !canonical_path.starts_with(&canonical_root) {
        return None;
    }
    let mut file = fs::File::open(&canonical_path).ok()?;
    let length = file.metadata().ok()?.len();

    let states = CODEX_ROLLOUT_STATES.get_or_init(|| Mutex::new(HashMap::new()));
    let mut states = states.lock().ok()?;
    if let Some(cached) = states.get_mut(&canonical_path) {
        if cached.length == length {
            return Some(cached.state.clone());
        }
        if cached.length < length {
            scan_rollout_range(&mut file, cached.length, length, false, &mut cached.state)?;
            cached.length = length;
            return Some(cached.state.clone());
        }
    }

    let start = length.saturating_sub(MAX_ROLLOUT_TAIL_BYTES);
    let mut state = RolloutState::default();
    let saw_lifecycle = scan_rollout_range(&mut file, start, length, start > 0, &mut state)?;
    if !saw_lifecycle && start > 0 {
        state = RolloutState::default();
        scan_rollout_range(&mut file, 0, length, false, &mut state)?;
    }
    states.insert(
        canonical_path,
        CachedRolloutState {
            length,
            state: state.clone(),
        },
    );
    Some(state)
}

fn discovered_event(
    thread_id: &str,
    agent_source_id: &str,
    parent_source_id: Option<&str>,
    cwd: &str,
    task_title: &str,
    action: &str,
    phase: AgentPhase,
    updated_at_ms: i64,
    child: bool,
) -> Option<AgentEvent> {
    let occurred_at = DateTime::<Utc>::from_timestamp_millis(updated_at_ms)?.to_rfc3339();
    let normalized_cwd = cwd.trim().to_lowercase();
    let project_name = Path::new(cwd)
        .file_name()
        .and_then(|value| value.to_str())
        .map(|value| safe_discovery_label(Some(value), "Проект", 120))
        .unwrap_or_else(|| "Без проекта".to_string());
    let session_digest = opaque_digest(thread_id);
    let agent_digest = opaque_digest(agent_source_id);
    let agent_id = if child {
        format!("bootstrap-child:{agent_digest}")
    } else {
        format!("bootstrap-root:{agent_digest}")
    };
    let parent_agent_id = parent_source_id
        .map(opaque_digest)
        .map(|digest| format!("bootstrap-root:{digest}"));
    let event_seed =
        format!("{thread_id}\0{agent_source_id}\0{updated_at_ms}\0{phase:?}\0{action}");
    Some(AgentEvent {
        protocol_version: PROTOCOL_VERSION.to_string(),
        event_id: format!("bootstrap:{}", opaque_digest(&event_seed)),
        sequence: u64::try_from(updated_at_ms).ok()?,
        occurred_at: occurred_at.clone(),
        provider: Provider::Codex,
        session_id: format!("session:{session_digest}"),
        agent_id,
        parent_agent_id,
        event_type: EventType::Discovered,
        payload: EventPayload {
            project: Some(ProjectPayload {
                id: format!("project:{}", opaque_digest(&normalized_cwd)),
                name: project_name,
                path: None,
            }),
            task: Some(TaskPayload {
                title: safe_discovery_label(Some(task_title), "Задача Codex", 120),
                detail: None,
            }),
            phase: Some(phase),
            progress: (!phase.is_terminal() && phase != AgentPhase::WaitingInput).then_some(
                EventProgress {
                    kind: ProgressKind::Activity,
                    current: None,
                    total: None,
                    label: action.to_string(),
                    source: ProgressSource::Inferred,
                },
            ),
            current_action: Some(action.to_string()),
            started_at: matches!(phase, AgentPhase::Working | AgentPhase::Planning)
                .then_some(occurred_at.clone()),
            navigation: Some(NavigationPayload {
                kind: "task".to_string(),
                label: "Открыть в Codex".to_string(),
                target: agent_source_id.to_string(),
            }),
            result: phase.is_terminal().then_some(ResultPayload {
                summary: action.to_string(),
                outcome: ResultOutcome::Success,
                completed_at: occurred_at.clone(),
                unread: true,
            }),
            ..EventPayload::default()
        },
    })
}

fn discover_codex_tasks(
    database_path: &Path,
    session_index_path: &Path,
    now: DateTime<Utc>,
) -> Result<CodexDiscoveryScan, rusqlite::Error> {
    let connection = Connection::open_with_flags(
        database_path,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )?;
    connection.busy_timeout(Duration::from_millis(100))?;
    let visible_cutoff_ms =
        (now - chrono::Duration::minutes(CODEX_DISCOVERY_TTL_MINUTES)).timestamp_millis();
    let recovery_cutoff_ms =
        (now - chrono::Duration::hours(CODEX_DISCOVERY_RECOVERY_HOURS)).timestamp_millis();
    let names = session_names(session_index_path);
    let sessions_root = session_index_path
        .parent()
        .map(|path| path.join("sessions"))
        .unwrap_or_default();
    let mut scan = CodexDiscoveryScan::default();

    let mut roots = connection.prepare(
        "SELECT t.id, t.cwd, t.rollout_path, \
                COALESCE(t.updated_at_ms, t.updated_at * 1000) \
         FROM threads t \
         WHERE t.archived = 0 \
           AND NOT (COALESCE(t.thread_source, '') = 'subagent' \
                    AND COALESCE(t.source, '') LIKE '{\"subagent\":%') \
           AND COALESCE(t.updated_at_ms, t.updated_at * 1000) >= ?1 \
           AND NOT EXISTS (SELECT 1 FROM thread_spawn_edges e WHERE e.child_thread_id = t.id)",
    )?;
    let root_rows = roots.query_map(rusqlite::params![recovery_cutoff_ms], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, String>(2)?,
            row.get::<_, i64>(3)?,
        ))
    })?;
    for row in root_rows {
        let (thread_id, cwd, rollout_path, updated_at_ms) = row?;
        let title = names
            .get(&thread_id)
            .map(String::as_str)
            .unwrap_or("Задача Codex");
        let state = rollout_state(Path::new(&rollout_path), &sessions_root);
        let working = state
            .as_ref()
            .and_then(RolloutState::is_working)
            .unwrap_or_else(|| now.timestamp_millis() - updated_at_ms <= 30_000);
        let waiting_input = state.as_ref().is_some_and(RolloutState::is_waiting_input);
        if updated_at_ms < visible_cutoff_ms && !working {
            continue;
        }
        let started_at = state.as_ref().and_then(|state| state.started_at.clone());
        let phase = if waiting_input {
            AgentPhase::WaitingInput
        } else if working {
            AgentPhase::Working
        } else {
            AgentPhase::Completed
        };
        if phase.is_terminal() {
            if let Some(turn_id) = state
                .as_ref()
                .and_then(|state| state.completed_turn.as_deref())
            {
                let completed_at = state
                    .as_ref()
                    .and_then(|state| state.completed_at.clone())
                    .or_else(|| {
                        DateTime::<Utc>::from_timestamp_millis(updated_at_ms)
                            .map(|value| value.to_rfc3339())
                    });
                if let Some(completed_at) = completed_at {
                    scan.completions.push(CodexCompletionCandidate {
                        session_id: format!("session:{}", opaque_digest(&thread_id)),
                        agent_id: format!("turn:{}", opaque_digest(turn_id)),
                        phase,
                        completed_at,
                    });
                }
            }
        }
        let action = if waiting_input {
            "Ждёт ответа".to_string()
        } else {
            state
                .and_then(|state| state.last_message)
                .unwrap_or_else(|| {
                    if working {
                        "Работает в Codex".to_string()
                    } else {
                        "Закончил работу".to_string()
                    }
                })
        };
        if let Some(mut event) = discovered_event(
            &thread_id,
            &thread_id,
            None,
            &cwd,
            title,
            &action,
            phase,
            updated_at_ms,
            false,
        ) {
            event.payload.started_at = started_at.or(event.payload.started_at);
            scan.active_keys.insert(event.key());
            scan.events.push(event);
        }
    }

    let mut children = connection.prepare(
        "SELECT e.parent_thread_id, t.id, t.cwd, t.agent_nickname, t.rollout_path, \
                COALESCE(t.updated_at_ms, t.updated_at * 1000) \
         FROM thread_spawn_edges e \
         JOIN threads t ON t.id = e.child_thread_id \
         WHERE e.status = 'open' AND t.archived = 0 \
           AND COALESCE(t.updated_at_ms, t.updated_at * 1000) >= ?1",
    )?;
    let child_rows = children.query_map(rusqlite::params![recovery_cutoff_ms], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, String>(2)?,
            row.get::<_, Option<String>>(3)?,
            row.get::<_, String>(4)?,
            row.get::<_, i64>(5)?,
        ))
    })?;
    for row in child_rows {
        let (parent_id, child_id, cwd, nickname, rollout_path, updated_at_ms) = row?;
        let title = safe_discovery_label(nickname.as_deref(), "Помощник Codex", 120);
        let state = rollout_state(Path::new(&rollout_path), &sessions_root);
        let working = state
            .as_ref()
            .and_then(RolloutState::is_working)
            .unwrap_or_else(|| now.timestamp_millis() - updated_at_ms <= 30_000);
        let waiting_input = state.as_ref().is_some_and(RolloutState::is_waiting_input);
        if updated_at_ms < visible_cutoff_ms && !working {
            continue;
        }
        let started_at = state.as_ref().and_then(|state| state.started_at.clone());
        let phase = if waiting_input {
            AgentPhase::WaitingInput
        } else if working {
            AgentPhase::Working
        } else {
            AgentPhase::Completed
        };
        let action = if waiting_input {
            "Ждёт ответа".to_string()
        } else {
            state
                .and_then(|state| state.last_message)
                .unwrap_or_else(|| {
                    if working {
                        "Работает в Codex".to_string()
                    } else {
                        "Закончил свою часть".to_string()
                    }
                })
        };
        if let Some(mut event) = discovered_event(
            &parent_id,
            &child_id,
            Some(&parent_id),
            &cwd,
            &title,
            &action,
            phase,
            updated_at_ms,
            true,
        ) {
            event.payload.started_at = started_at.or(event.payload.started_at);
            scan.active_keys.insert(event.key());
            scan.events.push(event);
        }
    }

    scan.events.sort_by(|left, right| {
        left.sequence
            .cmp(&right.sequence)
            .then_with(|| left.event_id.cmp(&right.event_id))
    });
    Ok(scan)
}

#[cfg_attr(not(feature = "desktop"), allow(dead_code))]
fn import_codex_tasks(
    database_path: &Path,
    session_index_path: &Path,
    store: &SharedStore,
    now: DateTime<Utc>,
) -> Option<HubSnapshot> {
    let scan = discover_codex_tasks(database_path, session_index_path, now).ok()?;
    let mut store = store.0.lock().ok()?;
    let mut changed = store.retain_recovered(&scan.active_keys, now);
    for event in scan.events {
        if let Some(merged) = store.merge_recovered_navigation(&event) {
            changed |= merged;
            continue;
        }
        match store.apply(event) {
            Ok(_) => changed = true,
            Err(ApplyError::Replay | ApplyError::StaleSequence | ApplyError::TerminalState) => {}
            Err(_) => {}
        }
    }
    let mut completion_changed = false;
    for completion in scan.completions {
        completion_changed |= store.record_codex_candidate(completion);
    }
    if completion_changed {
        store.revision = store.revision.saturating_add(1);
        store.persist();
        changed = true;
    }
    changed.then(|| store.snapshot())
}

#[cfg(feature = "desktop")]
fn codex_state_sources() -> Option<(PathBuf, PathBuf)> {
    let codex_home = std::env::var_os("CODEX_HOME")
        .map(PathBuf::from)
        .or_else(|| {
            std::env::var_os("USERPROFILE")
                .map(PathBuf::from)
                .map(|path| path.join(".codex"))
        })?;
    let sqlite_home = std::env::var_os("CODEX_SQLITE_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| codex_home.clone());
    let database_path = fs::read_dir(sqlite_home)
        .ok()?
        .flatten()
        .filter_map(|entry| {
            let path = entry.path();
            let name = path.file_name()?.to_str()?;
            let version = name
                .strip_prefix("state_")?
                .strip_suffix(".sqlite")?
                .parse::<u64>()
                .ok()?;
            Some((version, path))
        })
        .max_by_key(|(version, _)| *version)?
        .1;
    Some((database_path, codex_home.join("session_index.jsonl")))
}

#[derive(Clone)]
struct HttpState {
    token: Arc<String>,
    store: SharedStore,
    emitter: Option<SnapshotEmitter>,
    completion_notifier: tokio::sync::watch::Sender<u64>,
}

type SnapshotEmitter = Arc<dyn Fn(HubSnapshot) + Send + Sync>;

#[derive(Clone, Debug, Serialize)]
pub struct HubConnection {
    endpoint: String,
    token: String,
    protocol_version: String,
}

#[cfg(feature = "desktop")]
pub struct HubRuntime {
    connection: HubConnection,
    store: Option<SharedStore>,
    runtime_path: Option<PathBuf>,
    registry_path: Option<PathBuf>,
    codex_sources: Option<(PathBuf, PathBuf)>,
    emitter: Option<SnapshotEmitter>,
    _ownership: Option<CoreOwnership>,
}

#[derive(Debug, Deserialize, Serialize)]
struct RuntimeDescriptor {
    endpoint: String,
    protocol_version: String,
    process_id: u32,
    secret_file: String,
}

struct PreparedCore {
    connection: HubConnection,
    store: SharedStore,
    runtime_path: PathBuf,
    registry_path: PathBuf,
    codex_sources: Option<(PathBuf, PathBuf)>,
    emitter: SnapshotEmitter,
    listener: StdTcpListener,
    router: Router,
    ownership: CoreOwnership,
}

#[derive(Debug, Serialize)]
struct HealthResponse {
    status: &'static str,
    protocol_version: &'static str,
}

#[derive(Debug, Serialize)]
struct AcceptedResponse {
    revision: u64,
}

#[derive(Debug, Default, Deserialize)]
struct CompletionQuery {
    #[serde(default)]
    after: u64,
}

#[derive(Debug, Serialize)]
struct ErrorResponse {
    error: &'static str,
}

#[derive(Debug)]
struct ApiError {
    status: StatusCode,
    code: &'static str,
}

impl ApiError {
    fn new(status: StatusCode, code: &'static str) -> Self {
        Self { status, code }
    }
}

impl From<ApplyError> for ApiError {
    fn from(error: ApplyError) -> Self {
        match error {
            ApplyError::Invalid(code) => Self::new(StatusCode::BAD_REQUEST, code),
            ApplyError::Replay => Self::new(StatusCode::CONFLICT, "replayed_event"),
            ApplyError::StaleSequence => Self::new(StatusCode::CONFLICT, "stale_sequence"),
            ApplyError::TerminalState => Self::new(StatusCode::CONFLICT, "terminal_state"),
            ApplyError::MissingAgent => Self::new(StatusCode::CONFLICT, "missing_agent"),
        }
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        (self.status, Json(ErrorResponse { error: self.code })).into_response()
    }
}

async fn health() -> Json<HealthResponse> {
    Json(HealthResponse {
        status: "ok",
        protocol_version: PROTOCOL_VERSION,
    })
}

fn constant_time_equal(left: &[u8], right: &[u8]) -> bool {
    if left.len() != right.len() {
        return false;
    }
    left.iter()
        .zip(right)
        .fold(0_u8, |difference, (left, right)| {
            difference | (left ^ right)
        })
        == 0
}

fn authorized(headers: &HeaderMap, token: &str) -> bool {
    let Some(value) = headers
        .get(AUTHORIZATION)
        .and_then(|value| value.to_str().ok())
    else {
        return false;
    };
    let expected = format!("Bearer {token}");
    constant_time_equal(value.as_bytes(), expected.as_bytes())
}

async fn submit_event(
    State(state): State<HttpState>,
    headers: HeaderMap,
    payload: Result<Json<AgentEvent>, JsonRejection>,
) -> Result<(StatusCode, Json<AcceptedResponse>), ApiError> {
    if !authorized(&headers, &state.token) {
        return Err(ApiError::new(StatusCode::UNAUTHORIZED, "unauthorized"));
    }
    let Json(event) = payload.map_err(|rejection| {
        if rejection.status() == StatusCode::PAYLOAD_TOO_LARGE {
            ApiError::new(StatusCode::PAYLOAD_TOO_LARGE, "payload_too_large")
        } else {
            ApiError::new(StatusCode::BAD_REQUEST, "invalid_json")
        }
    })?;
    let snapshot = {
        let mut store =
            state.store.0.lock().map_err(|_| {
                ApiError::new(StatusCode::INTERNAL_SERVER_ERROR, "store_unavailable")
            })?;
        store.apply(event).map_err(ApiError::from)?
    };
    if let Some(emitter) = &state.emitter {
        emitter(snapshot.clone());
    }
    Ok((
        StatusCode::ACCEPTED,
        Json(AcceptedResponse {
            revision: snapshot.revision,
        }),
    ))
}

async fn get_completions(
    State(state): State<HttpState>,
    headers: HeaderMap,
    Query(query): Query<CompletionQuery>,
) -> Result<Json<CompletionInbox>, ApiError> {
    if !authorized(&headers, &state.token) {
        return Err(ApiError::new(StatusCode::UNAUTHORIZED, "unauthorized"));
    }
    let inbox = state
        .store
        .0
        .lock()
        .map_err(|_| ApiError::new(StatusCode::INTERNAL_SERVER_ERROR, "store_unavailable"))?
        .completion_inbox(query.after);
    Ok(Json(inbox))
}

struct CompletionStreamState {
    store: SharedStore,
    receiver: tokio::sync::watch::Receiver<u64>,
    after: u64,
    pending: VecDeque<CompletionRecord>,
}

#[derive(Debug, Deserialize)]
struct AcknowledgementRequest {
    key: String,
}

async fn get_snapshot(
    State(state): State<HttpState>,
    headers: HeaderMap,
) -> Result<Json<HubSnapshot>, ApiError> {
    if !authorized(&headers, &state.token) {
        return Err(ApiError::new(StatusCode::UNAUTHORIZED, "unauthorized"));
    }
    let snapshot = state
        .store
        .0
        .lock()
        .map_err(|_| ApiError::new(StatusCode::INTERNAL_SERVER_ERROR, "store_unavailable"))?
        .snapshot();
    Ok(Json(snapshot))
}

async fn acknowledge_agent(
    State(state): State<HttpState>,
    headers: HeaderMap,
    payload: Result<Json<AcknowledgementRequest>, JsonRejection>,
) -> Result<Json<HubSnapshot>, ApiError> {
    if !authorized(&headers, &state.token) {
        return Err(ApiError::new(StatusCode::UNAUTHORIZED, "unauthorized"));
    }
    let Json(request) =
        payload.map_err(|_| ApiError::new(StatusCode::BAD_REQUEST, "invalid_json"))?;
    if request.key.is_empty() || request.key.len() > 512 {
        return Err(ApiError::new(StatusCode::BAD_REQUEST, "invalid_agent_key"));
    }
    let snapshot = state
        .store
        .0
        .lock()
        .map_err(|_| ApiError::new(StatusCode::INTERNAL_SERVER_ERROR, "store_unavailable"))?
        .acknowledge(&request.key)
        .map_err(ApiError::from)?;
    let _ = state.completion_notifier.send(snapshot.revision);
    if let Some(emitter) = &state.emitter {
        emitter(snapshot.clone());
    }
    Ok(Json(snapshot))
}

struct SnapshotStreamState {
    store: SharedStore,
    receiver: tokio::sync::watch::Receiver<u64>,
    after: u64,
}

fn snapshot_stream(state: SnapshotStreamState) -> impl Stream<Item = Result<SseEvent, Infallible>> {
    stream::unfold(state, |mut state| async move {
        loop {
            let snapshot = match state.store.0.lock() {
                Ok(store) => store.snapshot(),
                Err(_) => return None,
            };
            if snapshot.revision > state.after {
                state.after = snapshot.revision;
                let data = serde_json::to_string(&snapshot)
                    .unwrap_or_else(|_| "{\"error\":\"serialization_failed\"}".to_string());
                let event = SseEvent::default()
                    .event("snapshot")
                    .id(snapshot.revision.to_string())
                    .data(data);
                return Some((Ok(event), state));
            }
            if state.receiver.changed().await.is_err() {
                return None;
            }
        }
    })
}

async fn stream_snapshots(
    State(state): State<HttpState>,
    headers: HeaderMap,
    Query(query): Query<CompletionQuery>,
) -> Result<Sse<impl Stream<Item = Result<SseEvent, Infallible>>>, ApiError> {
    if !authorized(&headers, &state.token) {
        return Err(ApiError::new(StatusCode::UNAUTHORIZED, "unauthorized"));
    }
    let stream_state = SnapshotStreamState {
        store: state.store,
        receiver: state.completion_notifier.subscribe(),
        after: query.after,
    };
    Ok(Sse::new(snapshot_stream(stream_state)).keep_alive(
        KeepAlive::new()
            .interval(Duration::from_secs(15))
            .text("keepalive"),
    ))
}

fn completion_stream(
    state: CompletionStreamState,
) -> impl Stream<Item = Result<SseEvent, Infallible>> {
    stream::unfold(state, |mut state| async move {
        loop {
            if let Some(record) = state.pending.pop_front() {
                state.after = state.after.max(record.cursor);
                let data = serde_json::to_string(&record)
                    .unwrap_or_else(|_| "{\"error\":\"serialization_failed\"}".to_string());
                let event = SseEvent::default()
                    .event("completion")
                    .id(record.cursor.to_string())
                    .data(data);
                return Some((Ok(event), state));
            }

            let inbox = match state.store.0.lock() {
                Ok(store) => store.completion_inbox(state.after),
                Err(_) => return None,
            };
            state.pending = inbox.completions.into();
            if !state.pending.is_empty() {
                continue;
            }
            if state.receiver.changed().await.is_err() {
                return None;
            }
        }
    })
}

async fn stream_completions(
    State(state): State<HttpState>,
    headers: HeaderMap,
    Query(query): Query<CompletionQuery>,
) -> Result<Sse<impl Stream<Item = Result<SseEvent, Infallible>>>, ApiError> {
    if !authorized(&headers, &state.token) {
        return Err(ApiError::new(StatusCode::UNAUTHORIZED, "unauthorized"));
    }
    let stream_state = CompletionStreamState {
        store: state.store,
        receiver: state.completion_notifier.subscribe(),
        after: query.after,
        pending: VecDeque::new(),
    };
    Ok(Sse::new(completion_stream(stream_state)).keep_alive(
        KeepAlive::new()
            .interval(Duration::from_secs(15))
            .text("keepalive"),
    ))
}

fn build_router(state: HttpState) -> Router {
    let cors = CorsLayer::new()
        .allow_origin([
            HeaderValue::from_static("http://tauri.localhost"),
            HeaderValue::from_static("https://tauri.localhost"),
            HeaderValue::from_static("http://localhost:1420"),
        ])
        .allow_methods([Method::GET, Method::POST])
        .allow_headers([AUTHORIZATION, CONTENT_TYPE]);

    Router::new()
        .route("/health", get(health))
        .route("/v1/events", post(submit_event))
        .route("/v1/snapshot", get(get_snapshot))
        .route("/v1/snapshots/stream", get(stream_snapshots))
        .route("/v1/acknowledgements", post(acknowledge_agent))
        .route("/v1/completions", get(get_completions))
        .route("/v1/completions/stream", get(stream_completions))
        .layer(DefaultBodyLimit::max(MAX_BODY_BYTES))
        .layer(TimeoutLayer::with_status_code(
            StatusCode::REQUEST_TIMEOUT,
            Duration::from_secs(2),
        ))
        .layer(cors)
        .with_state(state)
}

fn load_or_create_secret(path: &Path) -> std::io::Result<String> {
    if let Ok(existing) = fs::read_to_string(path) {
        let token = existing.trim();
        if token.len() == 64 && token.chars().all(|character| character.is_ascii_hexdigit()) {
            return Ok(token.to_string());
        }
    }

    let mut bytes = [0_u8; 32];
    rand::rng().fill_bytes(&mut bytes);
    let token = bytes
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect::<String>();
    fs::write(path, token.as_bytes())?;
    Ok(token)
}

fn valid_secret(token: &str) -> bool {
    token.len() == 64 && token.chars().all(|character| character.is_ascii_hexdigit())
}

fn runtime_is_healthy(descriptor: &RuntimeDescriptor) -> bool {
    if descriptor.protocol_version != PROTOCOL_VERSION {
        return false;
    }
    let Ok(url) = url::Url::parse(&descriptor.endpoint) else {
        return false;
    };
    if url.scheme() != "http" || url.host_str() != Some("127.0.0.1") {
        return false;
    }
    let Some(port) = url.port() else {
        return false;
    };
    let address = std::net::SocketAddr::from(([127, 0, 0, 1], port));
    let Ok(mut stream) = std::net::TcpStream::connect_timeout(&address, Duration::from_secs(1))
    else {
        return false;
    };
    let _ = stream.set_read_timeout(Some(Duration::from_secs(1)));
    let _ = stream.set_write_timeout(Some(Duration::from_secs(1)));
    if stream
        .write_all(b"GET /health HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
        .is_err()
    {
        return false;
    }
    let mut response = [0_u8; 64];
    stream
        .read(&mut response)
        .map(|size| {
            std::str::from_utf8(&response[..size])
                .map(|text| text.starts_with("HTTP/1.1 200"))
                .unwrap_or(false)
        })
        .unwrap_or(false)
}

fn existing_core_connection(app_data: &Path) -> Option<HubConnection> {
    let descriptor: RuntimeDescriptor = fs::read(app_data.join("hub-runtime.json"))
        .ok()
        .and_then(|bytes| serde_json::from_slice(&bytes).ok())?;
    if !runtime_is_healthy(&descriptor) {
        return None;
    }
    let expected_secret = app_data.join("hub-secret.txt");
    if Path::new(&descriptor.secret_file) != expected_secret {
        return None;
    }
    let token = fs::read_to_string(expected_secret).ok()?;
    let token = token.trim();
    if !valid_secret(token) {
        return None;
    }
    Some(HubConnection {
        endpoint: descriptor.endpoint,
        token: token.to_string(),
        protocol_version: descriptor.protocol_version,
    })
}

fn import_core_sources(
    registry_path: &Path,
    codex_sources: &Option<(PathBuf, PathBuf)>,
    store: &SharedStore,
    emitter: &SnapshotEmitter,
) {
    if let Some(snapshot) = import_registry(registry_path, store, Utc::now()) {
        emitter(snapshot);
    }
    if let Some((database_path, session_index_path)) = codex_sources {
        if let Some(snapshot) =
            import_codex_tasks(database_path, session_index_path, store, Utc::now())
        {
            emitter(snapshot);
        }
    }
}

async fn poll_core_sources(
    registry_path: PathBuf,
    codex_sources: Option<(PathBuf, PathBuf)>,
    store: SharedStore,
    emitter: SnapshotEmitter,
) {
    let mut interval = tokio::time::interval(Duration::from_secs(HUB_POLL_INTERVAL_SECONDS));
    interval.tick().await;
    loop {
        interval.tick().await;
        import_core_sources(&registry_path, &codex_sources, &store, &emitter);
    }
}

fn prepare_core(
    app_data: &Path,
    external_emitter: Option<SnapshotEmitter>,
) -> Result<PreparedCore, Box<dyn std::error::Error>> {
    fs::create_dir_all(app_data)?;
    if existing_core_connection(app_data).is_some() {
        return Err("petcrew_core_already_running".into());
    }

    let runtime_path = app_data.join("hub-runtime.json");
    let ownership = acquire_core_ownership(app_data)?;
    let _ = fs::remove_file(&runtime_path);

    let secret_path = app_data.join("hub-secret.txt");
    let cache_path = app_data.join("hub-cache.json");
    let registry_path = app_data.join(REGISTRY_FOLDER);
    fs::create_dir_all(&registry_path)?;
    let token = load_or_create_secret(&secret_path)?;

    let listener = StdTcpListener::bind(SocketAddrV4::new(Ipv4Addr::LOCALHOST, 0))?;
    listener.set_nonblocking(true)?;
    let endpoint = format!("http://127.0.0.1:{}", listener.local_addr()?.port());
    let descriptor = RuntimeDescriptor {
        endpoint: endpoint.clone(),
        protocol_version: PROTOCOL_VERSION.to_string(),
        process_id: std::process::id(),
        secret_file: secret_path.to_string_lossy().into_owned(),
    };
    fs::write(&runtime_path, serde_json::to_vec_pretty(&descriptor)?)?;

    let store = SharedStore(Arc::new(Mutex::new(EventStore::load(Some(cache_path)))));
    let connection = HubConnection {
        endpoint,
        token: token.clone(),
        protocol_version: PROTOCOL_VERSION.to_string(),
    };
    let (completion_notifier, _) = tokio::sync::watch::channel(0_u64);
    let emitter_notifier = completion_notifier.clone();
    let emitter: SnapshotEmitter = Arc::new(move |snapshot: HubSnapshot| {
        let _ = emitter_notifier.send(snapshot.revision);
        if let Some(external) = &external_emitter {
            external(snapshot);
        }
    });
    let router = build_router(HttpState {
        token: Arc::new(token),
        store: store.clone(),
        emitter: Some(emitter.clone()),
        completion_notifier,
    });
    let codex_sources = codex_state_sources();
    import_core_sources(&registry_path, &codex_sources, &store, &emitter);

    Ok(PreparedCore {
        connection,
        store,
        runtime_path,
        registry_path,
        codex_sources,
        emitter,
        listener,
        router,
        ownership,
    })
}

pub async fn run_headless(app_data: PathBuf) -> Result<(), Box<dyn std::error::Error>> {
    if existing_core_connection(&app_data).is_some() {
        return Err("petcrew_core_already_running".into());
    }
    let prepared = prepare_core(&app_data, None)?;
    let poll = tokio::spawn(poll_core_sources(
        prepared.registry_path.clone(),
        prepared.codex_sources.clone(),
        prepared.store.clone(),
        prepared.emitter.clone(),
    ));
    let listener = tokio::net::TcpListener::from_std(prepared.listener)?;
    let result = axum::serve(listener, prepared.router).await;
    poll.abort();
    let _ = fs::remove_file(&prepared.runtime_path);
    drop(prepared.ownership);
    result.map_err(Into::into)
}

#[cfg(feature = "desktop")]
pub fn setup(app: &mut tauri::App) -> Result<(), Box<dyn std::error::Error>> {
    let app_data = app.path().app_local_data_dir()?;
    if let Some(connection) = existing_core_connection(&app_data) {
        app.manage(HubRuntime {
            connection,
            store: None,
            runtime_path: None,
            registry_path: None,
            codex_sources: None,
            emitter: None,
            _ownership: None,
        });
        return Ok(());
    }

    let emit_handle = app.handle().clone();
    let desktop_emitter = Arc::new(move |snapshot: HubSnapshot| {
        let _ = emit_handle.emit(SNAPSHOT_EVENT, snapshot);
    });
    let prepared = prepare_core(&app_data, Some(desktop_emitter))?;
    let listener = prepared.listener;
    let router = prepared.router;

    tauri::async_runtime::spawn(async move {
        match tokio::net::TcpListener::from_std(listener) {
            Ok(listener) => {
                if let Err(error) = axum::serve(listener, router).await {
                    eprintln!("PetCrew local hub stopped: {error}");
                }
            }
            Err(error) => eprintln!("PetCrew local hub could not start: {error}"),
        }
    });

    tauri::async_runtime::spawn(poll_core_sources(
        prepared.registry_path.clone(),
        prepared.codex_sources.clone(),
        prepared.store.clone(),
        prepared.emitter.clone(),
    ));

    app.manage(HubRuntime {
        connection: prepared.connection,
        store: Some(prepared.store),
        runtime_path: Some(prepared.runtime_path),
        registry_path: Some(prepared.registry_path),
        codex_sources: prepared.codex_sources,
        emitter: Some(prepared.emitter),
        _ownership: Some(prepared.ownership),
    });
    Ok(())
}

#[cfg(feature = "desktop")]
pub fn cleanup(app: &AppHandle) {
    if let Some(runtime) = app.try_state::<HubRuntime>() {
        if let Some(runtime_path) = &runtime.runtime_path {
            let _ = fs::remove_file(runtime_path);
        }
    }
}

#[cfg(feature = "desktop")]
pub fn resume(app: &AppHandle) {
    let Some(runtime) = app.try_state::<HubRuntime>() else {
        return;
    };
    let (Some(registry_path), Some(store), Some(emitter)) = (
        runtime.registry_path.clone(),
        runtime.store.clone(),
        runtime.emitter.clone(),
    ) else {
        return;
    };
    let codex_sources = runtime.codex_sources.clone();
    tauri::async_runtime::spawn(async move {
        import_core_sources(&registry_path, &codex_sources, &store, &emitter);
    });
}

#[cfg(feature = "desktop")]
#[tauri::command]
pub fn get_hub_connection(state: TauriState<'_, HubRuntime>) -> HubConnection {
    state.connection.clone()
}

#[cfg(feature = "desktop")]
#[tauri::command]
pub fn get_hub_snapshot(state: TauriState<'_, HubRuntime>) -> Result<HubSnapshot, String> {
    state
        .store
        .as_ref()
        .ok_or_else(|| "hub_snapshot_available_over_http".to_string())?
        .0
        .lock()
        .map(|store| store.snapshot())
        .map_err(|_| "hub_store_unavailable".to_string())
}

fn valid_codex_thread_id(value: &str) -> bool {
    if value.len() != 36 {
        return false;
    }
    value.chars().enumerate().all(|(index, character)| {
        if matches!(index, 8 | 13 | 18 | 23) {
            character == '-'
        } else {
            character.is_ascii_hexdigit()
        }
    })
}

#[cfg(feature = "desktop")]
#[tauri::command]
pub fn open_codex_thread(thread_id: String) -> Result<(), String> {
    if !valid_codex_thread_id(&thread_id) {
        return Err("invalid_codex_thread_id".to_string());
    }
    std::process::Command::new("explorer.exe")
        .arg(format!("codex://threads/{thread_id}"))
        .spawn()
        .map(|_| ())
        .map_err(|_| "codex_navigation_failed".to_string())
}

fn opencode_project_uri(directory: &str) -> Result<String, &'static str> {
    let path = Path::new(directory);
    if directory.len() > 500 || directory.contains('\0') || !path.is_absolute() || !path.is_dir() {
        return Err("invalid_opencode_directory");
    }
    let mut uri = url::Url::parse("opencode://open-project").map_err(|_| "invalid_opencode_uri")?;
    uri.query_pairs_mut().append_pair("directory", directory);
    Ok(uri.into())
}

fn opencode_desktop_executable(local_app_data: &Path) -> Result<PathBuf, &'static str> {
    let executable = local_app_data
        .join("Programs")
        .join("@opencode-aidesktop")
        .join("OpenCode.exe");
    executable
        .is_file()
        .then_some(executable)
        .ok_or("opencode_desktop_not_found")
}

#[cfg(feature = "desktop")]
#[tauri::command]
pub fn open_opencode_project(directory: String) -> Result<(), String> {
    let uri = opencode_project_uri(&directory).map_err(str::to_string)?;
    let local_app_data =
        std::env::var_os("LOCALAPPDATA").ok_or_else(|| "local_app_data_missing".to_string())?;
    let executable =
        opencode_desktop_executable(Path::new(&local_app_data)).map_err(str::to_string)?;
    std::process::Command::new(executable)
        .arg(uri)
        .spawn()
        .map(|_| ())
        .map_err(|_| "opencode_navigation_failed".to_string())
}

#[cfg(feature = "desktop")]
#[tauri::command]
pub fn acknowledge_hub_agent(
    app: AppHandle,
    state: TauriState<'_, HubRuntime>,
    key: String,
) -> Result<HubSnapshot, String> {
    let snapshot = {
        let mut store = state
            .store
            .as_ref()
            .ok_or_else(|| "hub_acknowledgement_available_over_http".to_string())?
            .0
            .lock()
            .map_err(|_| "hub_store_unavailable".to_string())?;
        store
            .acknowledge(&key)
            .map_err(|_| "hub_agent_not_found".to_string())?
    };
    let _ = app.emit(SNAPSHOT_EVENT, snapshot.clone());
    Ok(snapshot)
}

#[cfg(feature = "desktop")]
#[tauri::command]
pub fn clear_hub(app: AppHandle, state: TauriState<'_, HubRuntime>) -> Result<HubSnapshot, String> {
    let registry_path = state
        .registry_path
        .as_ref()
        .ok_or_else(|| "hub_clear_available_only_on_core".to_string())?;
    clear_registry(registry_path).map_err(|_| "hub_registry_clear_failed".to_string())?;
    let snapshot = {
        let mut store = state
            .store
            .as_ref()
            .ok_or_else(|| "hub_clear_available_only_on_core".to_string())?
            .0
            .lock()
            .map_err(|_| "hub_store_unavailable".to_string())?;
        store.clear()
    };
    let _ = app.emit(SNAPSHOT_EVENT, snapshot.clone());
    Ok(snapshot)
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::{body::Body, http::Request};
    use futures_util::StreamExt;
    use std::time::{SystemTime, UNIX_EPOCH};
    use tower::ServiceExt;

    fn temp_registry(name: &str) -> PathBuf {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let path =
            std::env::temp_dir().join(format!("petcrew-{name}-{}-{nonce}", std::process::id()));
        fs::create_dir_all(&path).unwrap();
        path
    }

    #[test]
    fn core_ownership_rejects_a_second_live_owner_and_recovers_after_release() {
        let app_data = temp_registry("core-ownership");
        let first = acquire_core_ownership(&app_data).unwrap();
        let error = match acquire_core_ownership(&app_data) {
            Ok(_) => panic!("second Core owner was accepted"),
            Err(error) => error,
        };
        assert_eq!(error.kind(), std::io::ErrorKind::AlreadyExists);
        drop(first);
        let second = acquire_core_ownership(&app_data).unwrap();
        drop(second);
        fs::remove_dir_all(app_data).unwrap();
    }

    #[cfg(windows)]
    #[test]
    fn core_ownership_recovers_lock_for_exited_process_with_retained_handle() {
        let app_data = temp_registry("core-stale-exited-owner");
        let mut child = std::process::Command::new("cmd")
            .args(["/C", "exit", "0"])
            .spawn()
            .unwrap();
        let dead_pid = child.id();
        assert!(child.wait().unwrap().success());

        // Keep `child` in scope: its Windows process handle retains the exited
        // process object, reproducing the case where OpenProcess succeeds even
        // though the owner is no longer running.
        fs::write(app_data.join("hub-core.lock"), dead_pid.to_string()).unwrap();
        assert!(!process_is_alive(dead_pid));

        let ownership = acquire_core_ownership(&app_data).unwrap();
        assert_eq!(
            fs::read_to_string(app_data.join("hub-core.lock")).unwrap(),
            std::process::id().to_string()
        );
        drop(ownership);
        drop(child);
        fs::remove_dir_all(app_data).unwrap();
    }

    fn sample_event(event_id: &str, sequence: u64) -> AgentEvent {
        AgentEvent {
            protocol_version: PROTOCOL_VERSION.to_string(),
            event_id: event_id.to_string(),
            sequence,
            occurred_at: "2026-07-17T16:00:00+03:00".to_string(),
            provider: Provider::Simulator,
            session_id: "hub-test".to_string(),
            agent_id: "agent-1".to_string(),
            parent_agent_id: None,
            event_type: EventType::Progress,
            payload: EventPayload {
                project: Some(ProjectPayload {
                    id: "petcrew".to_string(),
                    name: "PetCrew".to_string(),
                    path: None,
                }),
                task: Some(TaskPayload {
                    title: "Проверить local hub".to_string(),
                    detail: None,
                }),
                phase: Some(AgentPhase::Working),
                progress: Some(EventProgress {
                    kind: ProgressKind::Steps,
                    current: Some(sequence),
                    total: Some(10),
                    label: format!("Шаг {sequence}"),
                    source: ProgressSource::Explicit,
                }),
                current_action: Some(format!("Выполняет шаг {sequence}")),
                ..EventPayload::default()
            },
        }
    }

    fn completed_event(index: usize, unread: bool) -> AgentEvent {
        let mut event = sample_event(&format!("event-completed-{index}"), 1);
        event.agent_id = format!("completed-{index:03}");
        event.occurred_at = (DateTime::parse_from_rfc3339("2026-07-18T12:00:00+03:00").unwrap()
            + chrono::Duration::minutes(index as i64))
        .to_rfc3339();
        event.event_type = EventType::Completed;
        event.payload.progress = None;
        event.payload.result = Some(ResultPayload {
            summary: format!("Готово {index}"),
            outcome: ResultOutcome::Success,
            completed_at: event.occurred_at.clone(),
            unread,
        });
        event
    }

    fn recent_opencode_completion(event_id: &str, session_digest: &str) -> AgentEvent {
        let completed_at = Utc::now().to_rfc3339();
        let mut event = completed_event(0, true);
        event.event_id = event_id.to_string();
        event.occurred_at = completed_at.clone();
        event.provider = Provider::Opencode;
        event.session_id = format!("session:{session_digest}");
        event.agent_id = format!("root:{session_digest}");
        event.payload.result.as_mut().unwrap().completed_at = completed_at;
        event
    }

    fn recent_codex_completion(
        event_id: &str,
        session_digest: &str,
        turn_digest: &str,
    ) -> AgentEvent {
        let completed_at = Utc::now().to_rfc3339();
        let mut event = completed_event(0, true);
        event.event_id = event_id.to_string();
        event.occurred_at = completed_at.clone();
        event.provider = Provider::Codex;
        event.session_id = format!("session:{session_digest}");
        event.agent_id = format!("turn:{turn_digest}");
        event.parent_agent_id = None;
        event.payload.result.as_mut().unwrap().completed_at = completed_at;
        event
    }

    fn test_notifier() -> tokio::sync::watch::Sender<u64> {
        tokio::sync::watch::channel(0_u64).0
    }

    #[test]
    fn accepts_truthful_progress_and_rejects_stale_sequence() {
        let mut store = EventStore::load(None);
        let snapshot = store.apply(sample_event("event-1", 1)).unwrap();
        assert_eq!(snapshot.revision, 1);
        assert_eq!(snapshot.agents[0].progress.current, Some(1));

        let error = store.apply(sample_event("event-2", 1)).unwrap_err();
        assert!(matches!(error, ApplyError::StaleSequence));
    }

    #[test]
    fn preserves_provider_change_summary_and_rejects_other_sources() {
        let mut event = sample_event("event-change-summary", 1);
        event.payload.change_summary = Some(ChangeSummaryPayload {
            files: 2,
            additions: 164,
            deletions: 0,
            source: "provider".to_string(),
        });
        let mut store = EventStore::load(None);
        let snapshot = store.apply(event).unwrap();
        let summary = snapshot.agents[0].change_summary.as_ref().unwrap();
        assert_eq!(summary.files, 2);
        assert_eq!(summary.additions, 164);
        assert_eq!(summary.deletions, 0);

        let mut invalid = sample_event("event-invalid-change-summary", 2);
        invalid.payload.change_summary = Some(ChangeSummaryPayload {
            files: 1,
            additions: 1,
            deletions: 0,
            source: "inferred".to_string(),
        });
        let error = invalid.validate().unwrap_err();
        assert!(matches!(
            error,
            ApplyError::Invalid("invalid_change_summary")
        ));
    }

    #[test]
    fn rejects_invented_step_progress() {
        let mut event = sample_event("event-invalid", 1);
        event.payload.progress.as_mut().unwrap().source = ProgressSource::Inferred;
        let error = event.validate().unwrap_err();
        assert!(matches!(error, ApplyError::Invalid("invalid_progress")));
    }

    #[test]
    fn terminal_state_is_sticky() {
        let mut store = EventStore::load(None);
        let mut completed = sample_event("event-complete", 1);
        completed.event_type = EventType::Completed;
        completed.payload.progress = None;
        completed.payload.result = Some(ResultPayload {
            summary: "Готово".to_string(),
            outcome: ResultOutcome::Success,
            completed_at: completed.occurred_at.clone(),
            unread: true,
        });
        store.apply(completed).unwrap();

        let error = store.apply(sample_event("event-late", 2)).unwrap_err();
        assert!(matches!(error, ApplyError::TerminalState));
    }

    #[test]
    fn legacy_opencode_root_turns_coalesce_into_one_conversation_card() {
        let mut store = EventStore::load(None);
        let session_id = format!("session:{}", "a".repeat(64));

        let mut first = sample_event("opencode-root-first", 1);
        first.provider = Provider::Opencode;
        first.session_id = session_id.clone();
        first.agent_id = "turn:legacy-first".to_string();
        first.event_type = EventType::Started;
        store.apply(first).unwrap();

        let mut next = sample_event("opencode-root-next", 2);
        next.provider = Provider::Opencode;
        next.session_id = session_id;
        next.agent_id = "turn:legacy-second".to_string();
        next.event_type = EventType::Activity;
        let snapshot = store.apply(next).unwrap();

        assert_eq!(snapshot.agents.len(), 1);
        assert_eq!(
            snapshot.agents[0].agent_id,
            format!("root:{}", "a".repeat(64))
        );
    }

    #[test]
    fn explicit_opencode_root_start_reactivates_a_completed_conversation() {
        let mut store = EventStore::load(None);
        let session_id = format!("session:{}", "b".repeat(64));
        let mut completed = completed_event(1, true);
        completed.provider = Provider::Opencode;
        completed.session_id = session_id.clone();
        completed.agent_id = "turn:legacy-completed".to_string();
        store.apply(completed).unwrap();

        let mut started = sample_event("opencode-root-restarted", 2);
        started.provider = Provider::Opencode;
        started.session_id = session_id;
        started.agent_id = "root:adapter-stable".to_string();
        started.event_type = EventType::Started;
        started.occurred_at = "2026-07-19T16:05:00+03:00".to_string();
        started.payload.started_at = Some(started.occurred_at.clone());
        let snapshot = store.apply(started).unwrap();

        assert_eq!(snapshot.agents.len(), 1);
        assert_eq!(snapshot.agents[0].phase, AgentPhase::Working);
        assert!(!snapshot.agents[0].unread);
        assert!(snapshot.agents[0].result.is_none());
        assert_eq!(
            snapshot.agents[0].started_at.as_deref(),
            Some("2026-07-19T16:05:00+03:00")
        );
    }

    #[test]
    fn newer_opencode_activity_recovers_a_missed_start_event() {
        let mut store = EventStore::load(None);
        let session_id = format!("session:{}", "d".repeat(64));
        let mut completed = completed_event(1, true);
        completed.provider = Provider::Opencode;
        completed.session_id = session_id.clone();
        completed.agent_id = "root:old-turn".to_string();
        store.apply(completed).unwrap();

        let mut activity = sample_event("opencode-newer-activity", 2);
        activity.provider = Provider::Opencode;
        activity.session_id = session_id;
        activity.agent_id = "root:stable".to_string();
        activity.event_type = EventType::Activity;
        activity.occurred_at = "2026-07-19T16:10:00+03:00".to_string();
        activity.payload.started_at = Some("2026-07-19T16:00:00+03:00".to_string());
        let snapshot = store.apply(activity).unwrap();

        assert_eq!(snapshot.agents[0].phase, AgentPhase::Working);
        assert_eq!(
            snapshot.agents[0].started_at.as_deref(),
            Some("2026-07-19T16:00:00+03:00")
        );
        assert!(!snapshot.agents[0].unread);
        assert!(snapshot.agents[0].result.is_none());
    }

    #[test]
    fn same_turn_opencode_activity_cannot_reopen_a_terminal_card() {
        let mut store = EventStore::load(None);
        let session_id = format!("session:{}", "e".repeat(64));
        let mut completed = completed_event(1, true);
        completed.provider = Provider::Opencode;
        completed.session_id = session_id.clone();
        completed.agent_id = "root:stable".to_string();
        store.apply(completed).unwrap();

        let mut late = sample_event("opencode-late-activity", 2);
        late.provider = Provider::Opencode;
        late.session_id = session_id;
        late.agent_id = "root:stable".to_string();
        late.event_type = EventType::Activity;
        late.occurred_at = "2026-07-18T12:10:00+03:00".to_string();
        late.payload.started_at = Some("2026-07-18T11:00:00+03:00".to_string());

        let error = store.apply(late).unwrap_err();
        assert!(matches!(error, ApplyError::TerminalState));
    }

    #[test]
    fn newer_opencode_completion_replaces_an_older_turn_result() {
        let mut store = EventStore::load(None);
        let session_id = format!("session:{}", "f".repeat(64));
        let mut older = completed_event(1, true);
        older.provider = Provider::Opencode;
        older.session_id = session_id.clone();
        older.agent_id = "root:stable".to_string();
        store.apply(older).unwrap();

        let mut newer = completed_event(2, true);
        newer.provider = Provider::Opencode;
        newer.session_id = session_id;
        newer.agent_id = "root:stable".to_string();
        newer.sequence = 2;
        newer.occurred_at = "2026-07-19T17:00:00+03:00".to_string();
        newer.payload.started_at = Some("2026-07-19T16:00:00+03:00".to_string());
        newer.payload.result.as_mut().unwrap().summary = "Новый результат".to_string();
        newer.payload.result.as_mut().unwrap().completed_at = newer.occurred_at.clone();
        let snapshot = store.apply(newer).unwrap();

        assert_eq!(snapshot.agents.len(), 1);
        assert_eq!(snapshot.agents[0].phase, AgentPhase::Completed);
        assert_eq!(
            snapshot.agents[0].result.as_deref(),
            Some("Новый результат")
        );
        assert_eq!(
            snapshot.agents[0].started_at.as_deref(),
            Some("2026-07-19T16:00:00+03:00")
        );
    }

    #[test]
    fn completion_inbox_survives_a_new_turn_and_cache_reload_without_card_text() {
        let directory = temp_registry("completion-inbox");
        let cache_path = directory.join("hub-cache.json");
        let digest = "7".repeat(64);
        let mut store = EventStore::load(Some(cache_path.clone()));
        let completed = recent_opencode_completion("completion-sticky", &digest);
        let completed_at = completed.occurred_at.clone();
        store.apply(completed).unwrap();

        let next_at = (DateTime::parse_from_rfc3339(&completed_at).unwrap()
            + chrono::Duration::seconds(1))
        .to_rfc3339();
        let mut started = sample_event("completion-next-turn", 2);
        started.provider = Provider::Opencode;
        started.session_id = format!("session:{digest}");
        started.agent_id = format!("root:{digest}");
        started.event_type = EventType::Started;
        started.occurred_at = next_at.clone();
        started.payload.started_at = Some(next_at);
        store.apply(started).unwrap();

        let inbox = store.completion_inbox(0);
        assert_eq!(inbox.completions.len(), 1);
        assert_eq!(inbox.completions[0].session_id, format!("session:{digest}"));
        let serialized = serde_json::to_value(&inbox.completions[0]).unwrap();
        for forbidden in [
            "task",
            "project",
            "path",
            "result",
            "summary",
            "progress",
            "navigation",
            "change_summary",
        ] {
            assert!(serialized.get(forbidden).is_none(), "{forbidden}");
        }

        drop(store);
        let reloaded = EventStore::load(Some(cache_path));
        assert_eq!(reloaded.completion_inbox(0).completions.len(), 1);
        fs::remove_dir_all(directory).unwrap();
    }

    #[test]
    fn codex_completion_receipt_is_deterministic_and_survives_cache_reload() {
        let directory = temp_registry("codex-completion-inbox");
        let cache_path = directory.join("hub-cache.json");
        let session_digest = "1".repeat(64);
        let turn_digest = "2".repeat(64);
        let mut store = EventStore::load(Some(cache_path.clone()));
        let first = recent_codex_completion(
            "codex-completion-first-delivery",
            &session_digest,
            &turn_digest,
        );
        let mut duplicate = first.clone();
        duplicate.event_id = "codex-completion-repeat-delivery".to_string();

        store.record_completion(&first);
        store.record_completion(&duplicate);
        assert_eq!(store.completion_inbox(0).completions.len(), 1);
        store.persist();

        drop(store);
        let reloaded = EventStore::load(Some(cache_path));
        let inbox = reloaded.completion_inbox(0);
        assert_eq!(inbox.completions.len(), 1);
        assert_eq!(inbox.completions[0].provider, Provider::Codex);
        assert_eq!(
            inbox.completions[0].session_id,
            format!("session:{session_digest}")
        );
        assert_eq!(inbox.completions[0].agent_id, format!("turn:{turn_digest}"));
        fs::remove_dir_all(directory).unwrap();
    }

    #[test]
    fn codex_hook_and_rollout_recovery_converge_on_one_receipt() {
        let session_digest = "3".repeat(64);
        let turn_digest = "4".repeat(64);
        let hook = recent_codex_completion("codex-hook-terminal", &session_digest, &turn_digest);
        let candidate = CodexCompletionCandidate {
            session_id: hook.session_id.clone(),
            agent_id: hook.agent_id.clone(),
            phase: AgentPhase::Completed,
            completed_at: hook.occurred_at.clone(),
        };
        let mut store = EventStore::load(None);

        store.record_completion(&hook);
        assert!(!store.record_codex_candidate(candidate));
        assert_eq!(store.completion_inbox(0).completions.len(), 1);
    }

    #[test]
    fn rollout_recovery_retains_exact_completed_turn_receipt() {
        let mut state = RolloutState::default();
        assert!(apply_rollout_record(
            "{\"timestamp\":\"2026-08-10T10:00:00Z\",\"type\":\"event_msg\",\"payload\":{\"type\":\"task_started\",\"turn_id\":\"turn-42\"}}",
            &mut state,
        ));
        assert!(apply_rollout_record(
            "{\"timestamp\":\"2026-08-10T10:05:00Z\",\"type\":\"event_msg\",\"payload\":{\"type\":\"task_complete\",\"turn_id\":\"turn-42\"}}",
            &mut state,
        ));

        assert_eq!(state.completed_turn.as_deref(), Some("turn-42"));
        assert_eq!(state.completed_at.as_deref(), Some("2026-08-10T10:05:00Z"));
        assert_eq!(state.is_working(), Some(false));
    }

    #[test]
    fn completion_inbox_rejects_non_opaque_opencode_identity() {
        let mut store = EventStore::load(None);
        let mut event = recent_opencode_completion("completion-non-opaque", &"a".repeat(64));
        event.session_id = r"C:\private\session".to_string();
        event.agent_id = "root:visible-session".to_string();
        store.apply(event).unwrap();

        assert!(store.completion_inbox(0).completions.is_empty());
    }

    #[test]
    fn completion_inbox_reports_when_the_requested_cursor_was_truncated() {
        let mut store = EventStore::load(None);
        for index in 0..=MAX_COMPLETION_RECORDS {
            let digest = format!("{index:064x}");
            let mut event =
                recent_opencode_completion(&format!("completion-capacity-{index}"), &digest);
            event.payload.result.as_mut().unwrap().unread = false;
            store.apply(event).unwrap();
        }

        let inbox = store.completion_inbox(0);
        assert_eq!(inbox.completions.len(), MAX_COMPLETION_RECORDS);
        assert_eq!(inbox.oldest_cursor, 2);
        assert_eq!(inbox.latest_cursor, (MAX_COMPLETION_RECORDS + 1) as u64);
        assert!(inbox.truncated);
        assert!(!store.completion_inbox(1).truncated);
    }

    #[test]
    fn cached_legacy_opencode_roots_are_migrated_without_manual_deletion() {
        let directory = temp_registry("opencode-cache-migration");
        let cache_path = directory.join("hub-cache.json");
        let digest = "c".repeat(64);
        let session_id = format!("session:{digest}");

        let mut older = EventStore::load(None)
            .apply(sample_event("cache-source", 1))
            .unwrap()
            .agents
            .remove(0);
        older.provider = Provider::Opencode;
        older.session_id = session_id.clone();
        older.agent_id = "turn:legacy-older".to_string();
        older.key = format!("opencode:{session_id}:{}", older.agent_id);
        older.updated_at = "2026-07-17T16:00:00+03:00".to_string();

        let mut newer = older.clone();
        newer.agent_id = "turn:legacy-newer".to_string();
        newer.key = format!("opencode:{session_id}:{}", newer.agent_id);
        newer.current_action = "Новейшее состояние".to_string();
        newer.updated_at = "2026-07-17T16:05:00+03:00".to_string();
        newer.last_sequence = 2;

        let cache = CacheFile {
            revision: 7,
            agents: vec![older, newer],
            seen_event_ids: Vec::new(),
            completion_cursor: 0,
            completions: Vec::new(),
        };
        fs::write(&cache_path, serde_json::to_vec_pretty(&cache).unwrap()).unwrap();

        let store = EventStore::load(Some(cache_path));
        let snapshot = store.snapshot();
        assert_eq!(snapshot.agents.len(), 1);
        assert_eq!(snapshot.agents[0].agent_id, format!("root:{digest}"));
        assert_eq!(snapshot.agents[0].current_action, "Новейшее состояние");
        assert_eq!(snapshot.revision, 8);

        fs::remove_dir_all(directory).unwrap();
    }

    #[test]
    fn redacts_sensitive_markers_before_snapshot() {
        let mut store = EventStore::load(None);
        let mut event = sample_event("event-secret", 1);
        event.payload.current_action = Some("Bearer very-secret-token".to_string());
        let snapshot = store.apply(event).unwrap();
        assert_eq!(
            snapshot.agents[0].current_action,
            "Выполняет действие (детали скрыты)"
        );
    }

    #[test]
    fn evicts_the_oldest_acknowledged_terminal_records_at_capacity() {
        let mut store = EventStore::load(None);
        for index in 0..105 {
            store.apply(completed_event(index, false)).unwrap();
        }

        let snapshot = store.snapshot();
        let retained_ids: HashSet<_> = snapshot
            .agents
            .iter()
            .map(|agent| agent.agent_id.as_str())
            .collect();
        assert_eq!(snapshot.agents.len(), MAX_RETAINED_AGENTS);
        assert_eq!(snapshot.overflow, 0);
        assert!(!retained_ids.contains("completed-000"));
        assert!(!retained_ids.contains("completed-004"));
        assert!(retained_ids.contains("completed-005"));
        assert!(retained_ids.contains("completed-104"));
    }

    #[test]
    fn protected_records_can_exceed_the_soft_capacity() {
        let mut store = EventStore::load(None);
        for index in 0..=MAX_RETAINED_AGENTS {
            let mut event = sample_event(&format!("event-active-{index}"), 1);
            event.agent_id = format!("active-{index:03}");
            store.apply(event).unwrap();
        }

        let snapshot = store.snapshot();
        assert_eq!(snapshot.agents.len(), MAX_RETAINED_AGENTS + 1);
        assert_eq!(snapshot.overflow, 1);
    }

    #[test]
    fn acknowledgement_is_returned_in_the_next_snapshot() {
        let mut store = EventStore::load(None);
        let created = store.apply(completed_event(0, true)).unwrap();
        let key = created.agents[0].key.clone();

        let acknowledged = store.acknowledge(&key).unwrap();

        assert!(!acknowledged.agents[0].unread);
        assert_eq!(acknowledged.revision, 2);
    }

    #[test]
    fn imports_fresh_cross_project_registry_event() {
        let registry = temp_registry("import");
        let mut event = sample_event("registry-event", 7);
        event.provider = Provider::Codex;
        event.session_id = "session-hash".to_string();
        event.agent_id = "agent-hash".to_string();
        event.occurred_at = Utc::now().to_rfc3339();
        event.payload.project = Some(ProjectPayload {
            id: "project-hash".to_string(),
            name: "Другой проект".to_string(),
            path: None,
        });
        fs::write(
            registry.join("agent-hash.json"),
            serde_json::to_vec(&event).unwrap(),
        )
        .unwrap();
        let store = SharedStore(Arc::new(Mutex::new(EventStore::load(None))));

        let snapshot = import_registry(&registry, &store, Utc::now()).unwrap();

        assert_eq!(snapshot.agents.len(), 1);
        assert_eq!(snapshot.agents[0].project, "Другой проект");
        assert_eq!(snapshot.agents[0].agent_id, "agent-hash");
        fs::remove_dir_all(registry).unwrap();
    }

    #[test]
    fn expires_stale_active_registry_event_and_cached_agent() {
        let registry = temp_registry("expire");
        let mut event = sample_event("stale-registry-event", 1);
        event.occurred_at =
            (Utc::now() - chrono::Duration::hours(ACTIVE_REGISTRY_TTL_HOURS + 1)).to_rfc3339();
        let registry_file = registry.join("stale-agent.json");
        fs::write(&registry_file, serde_json::to_vec(&event).unwrap()).unwrap();
        let mut cached_store = EventStore::load(None);
        cached_store.apply(event).unwrap();
        let store = SharedStore(Arc::new(Mutex::new(cached_store)));

        let snapshot = import_registry(&registry, &store, Utc::now()).unwrap();

        assert!(snapshot.agents.is_empty());
        assert!(!registry_file.exists());
        fs::remove_dir_all(registry).unwrap();
    }

    #[test]
    fn clearing_registry_removes_owned_events_only() {
        let registry = temp_registry("clear");
        fs::write(registry.join("agent.json"), b"{}").unwrap();
        fs::write(registry.join("agent.tmp"), b"{}").unwrap();
        fs::write(registry.join("keep.txt"), b"keep").unwrap();

        clear_registry(&registry).unwrap();

        assert!(!registry.join("agent.json").exists());
        assert!(!registry.join("agent.tmp").exists());
        assert!(registry.join("keep.txt").exists());
        fs::remove_dir_all(registry).unwrap();
    }

    fn discovery_database(path: &Path) -> Connection {
        let connection = Connection::open(path).unwrap();
        connection
            .execute_batch(
                "CREATE TABLE threads (
                    id TEXT PRIMARY KEY,
                    cwd TEXT NOT NULL,
                    rollout_path TEXT NOT NULL DEFAULT '',
                    thread_source TEXT,
                    source TEXT,
                    updated_at INTEGER NOT NULL,
                    updated_at_ms INTEGER,
                    archived INTEGER NOT NULL DEFAULT 0,
                    agent_nickname TEXT
                );
                CREATE TABLE thread_spawn_edges (
                    parent_thread_id TEXT NOT NULL,
                    child_thread_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL
                );",
            )
            .unwrap();
        connection
    }

    #[test]
    fn discovers_existing_root_without_reading_database_title_or_full_path() {
        let directory = temp_registry("codex-root");
        let database = directory.join("state_5.sqlite");
        let index = directory.join("session_index.jsonl");
        let connection = discovery_database(&database);
        let now = Utc::now();
        connection
            .execute(
                "INSERT INTO threads (id, cwd, updated_at, updated_at_ms, archived) \
                 VALUES (?1, ?2, ?3, ?4, 0)",
                rusqlite::params![
                    "thread-existing",
                    r"C:\Projects\SampleProject\SampleWorkspace",
                    now.timestamp(),
                    now.timestamp_millis()
                ],
            )
            .unwrap();
        fs::write(
            &index,
            r#"{"id":"thread-existing","thread_name":"Живая задача","updated_at":1}"#,
        )
        .unwrap();

        let scan = discover_codex_tasks(&database, &index, now).unwrap();

        assert_eq!(scan.events.len(), 1);
        let event = &scan.events[0];
        assert_eq!(
            event.payload.project.as_ref().unwrap().name,
            "SampleWorkspace"
        );
        assert_eq!(event.payload.project.as_ref().unwrap().path, None);
        assert_eq!(event.payload.task.as_ref().unwrap().title, "Живая задача");
        assert_eq!(
            event.payload.navigation.as_ref().unwrap().target,
            "thread-existing"
        );
        assert!(!serde_json::to_string(event)
            .unwrap()
            .contains(r"C:\Projects"));
        assert!(event.agent_id.starts_with("bootstrap-root:"));
        drop(connection);
        fs::remove_dir_all(directory).unwrap();
    }

    #[test]
    fn discovers_recent_open_child_and_ignores_stale_child() {
        let directory = temp_registry("codex-child");
        let database = directory.join("state_5.sqlite");
        let index = directory.join("session_index.jsonl");
        let connection = discovery_database(&database);
        let now = Utc::now();
        for (id, nickname, updated_at_ms) in [
            ("child-live", Some("Ada"), now.timestamp_millis()),
            (
                "child-stale",
                Some("Old"),
                (now - chrono::Duration::minutes(CODEX_DISCOVERY_TTL_MINUTES + 1))
                    .timestamp_millis(),
            ),
        ] {
            connection
                .execute(
                    "INSERT INTO threads \
                     (id, cwd, updated_at, updated_at_ms, archived, agent_nickname) \
                     VALUES (?1, ?2, ?3, ?4, 0, ?5)",
                    rusqlite::params![
                        id,
                        r"C:\Projects\PetCrew",
                        updated_at_ms / 1000,
                        updated_at_ms,
                        nickname
                    ],
                )
                .unwrap();
            connection
                .execute(
                    "INSERT INTO thread_spawn_edges \
                     (parent_thread_id, child_thread_id, status) VALUES (?1, ?2, 'open')",
                    rusqlite::params!["parent-thread", id],
                )
                .unwrap();
        }

        let scan = discover_codex_tasks(&database, &index, now).unwrap();

        assert_eq!(scan.events.len(), 1);
        assert_eq!(scan.events[0].payload.task.as_ref().unwrap().title, "Ada");
        assert!(scan.events[0].agent_id.starts_with("bootstrap-child:"));
        drop(connection);
        fs::remove_dir_all(directory).unwrap();
    }

    #[test]
    fn keeps_typed_active_root_after_recent_visibility_cutoff() {
        let directory = temp_registry("codex-active-beyond-visible-cutoff");
        let sessions = directory.join("sessions");
        fs::create_dir_all(&sessions).unwrap();
        let active_rollout = sessions.join("active.jsonl");
        let completed_rollout = sessions.join("completed.jsonl");
        fs::write(
            &active_rollout,
            "{\"timestamp\":\"2026-07-19T12:00:00Z\",\"type\":\"event_msg\",\"payload\":{\"type\":\"task_started\",\"turn_id\":\"active-turn\"}}\n",
        )
        .unwrap();
        fs::write(
            &completed_rollout,
            concat!(
                "{\"timestamp\":\"2026-07-19T12:00:00Z\",\"type\":\"event_msg\",\"payload\":{\"type\":\"task_started\",\"turn_id\":\"completed-turn\"}}\n",
                "{\"timestamp\":\"2026-07-19T12:10:00Z\",\"type\":\"event_msg\",\"payload\":{\"type\":\"task_complete\",\"turn_id\":\"completed-turn\"}}\n"
            ),
        )
        .unwrap();
        let database = directory.join("state_5.sqlite");
        let index = directory.join("session_index.jsonl");
        let connection = discovery_database(&database);
        let now = Utc::now();
        let stale_update =
            (now - chrono::Duration::minutes(CODEX_DISCOVERY_TTL_MINUTES + 1)).timestamp_millis();
        for (id, rollout) in [
            ("active-root", &active_rollout),
            ("completed-root", &completed_rollout),
        ] {
            connection
                .execute(
                    "INSERT INTO threads \
                     (id, cwd, rollout_path, updated_at, updated_at_ms, archived) \
                     VALUES (?1, ?2, ?3, ?4, ?5, 0)",
                    rusqlite::params![
                        id,
                        r"C:\Projects\PetCrew",
                        rollout.to_string_lossy(),
                        stale_update / 1000,
                        stale_update
                    ],
                )
                .unwrap();
        }

        let scan = discover_codex_tasks(&database, &index, now).unwrap();

        assert_eq!(scan.events.len(), 1);
        assert_eq!(scan.events[0].payload.phase, Some(AgentPhase::Working));
        assert!(scan.events[0]
            .payload
            .navigation
            .as_ref()
            .is_some_and(|navigation| navigation.target == "active-root"));
        drop(connection);
        fs::remove_dir_all(directory).unwrap();
    }

    #[test]
    fn completed_root_uses_last_assistant_status_as_unread_result() {
        let directory = temp_registry("codex-rollout");
        let sessions = directory
            .join("sessions")
            .join("2026")
            .join("07")
            .join("19");
        fs::create_dir_all(&sessions).unwrap();
        let rollout = sessions.join("rollout.jsonl");
        let database = directory.join("state_5.sqlite");
        let index = directory.join("session_index.jsonl");
        let connection = discovery_database(&database);
        let now = Utc::now();
        fs::write(
            &rollout,
            concat!(
                "{\"timestamp\":\"2026-07-19T12:00:00Z\",\"type\":\"event_msg\",\"payload\":{\"type\":\"task_started\",\"turn_id\":\"turn-1\"}}\n",
                "{\"type\":\"event_msg\",\"payload\":{\"type\":\"user_message\",\"message\":\"PRIVATE USER TEXT\"}}\n",
                "{\"type\":\"event_msg\",\"payload\":{\"type\":\"agent_message\",\"message\":\"Проверила данные\",\"phase\":\"commentary\"}}\n",
                "{\"type\":\"event_msg\",\"payload\":{\"type\":\"task_complete\",\"turn_id\":\"turn-1\"}}\n"
            ),
        )
        .unwrap();
        connection
            .execute(
                "INSERT INTO threads \
                 (id, cwd, rollout_path, updated_at, updated_at_ms, archived) \
                 VALUES (?1, ?2, ?3, ?4, ?5, 0)",
                rusqlite::params![
                    "thread-waiting",
                    r"C:\Projects\PetCrew",
                    rollout.to_string_lossy(),
                    now.timestamp(),
                    now.timestamp_millis()
                ],
            )
            .unwrap();

        let scan = discover_codex_tasks(&database, &index, now).unwrap();

        assert_eq!(scan.events.len(), 1);
        assert_eq!(scan.events[0].payload.phase, Some(AgentPhase::Completed));
        assert_eq!(
            scan.events[0].payload.current_action.as_deref(),
            Some("Проверила данные")
        );
        let result = scan.events[0].payload.result.as_ref().unwrap();
        assert_eq!(result.summary, "Проверила данные");
        assert!(result.unread);
        assert_eq!(
            scan.events[0].payload.started_at.as_deref(),
            Some("2026-07-19T12:00:00Z")
        );
        assert!(!serde_json::to_string(&scan.events[0])
            .unwrap()
            .contains("PRIVATE USER TEXT"));
        drop(connection);
        fs::remove_dir_all(directory).unwrap();
    }

    #[test]
    fn unmatched_request_user_input_is_exact_waiting_state() {
        use std::io::Write as _;

        let directory = temp_registry("codex-waiting-input");
        let sessions = directory
            .join("sessions")
            .join("2026")
            .join("07")
            .join("19");
        fs::create_dir_all(&sessions).unwrap();
        let rollout = sessions.join("rollout.jsonl");
        let database = directory.join("state_5.sqlite");
        let index = directory.join("session_index.jsonl");
        let connection = discovery_database(&database);
        let now = Utc::now();
        fs::write(
            &rollout,
            concat!(
                "{\"timestamp\":\"2026-07-19T12:00:00Z\",\"type\":\"event_msg\",\"payload\":{\"type\":\"task_started\",\"turn_id\":\"turn-1\"}}\n",
                "{\"timestamp\":\"2026-07-19T12:00:01Z\",\"type\":\"response_item\",\"payload\":{\"type\":\"function_call\",\"name\":\"request_user_input\",\"arguments\":\"PRIVATE QUESTION AND CHOICES\",\"call_id\":\"call-1\"}}\n"
            ),
        )
        .unwrap();
        connection
            .execute(
                "INSERT INTO threads \
                 (id, cwd, rollout_path, updated_at, updated_at_ms, archived) \
                 VALUES (?1, ?2, ?3, ?4, ?5, 0)",
                rusqlite::params![
                    "thread-waiting-input",
                    r"C:\Projects\PetCrew",
                    rollout.to_string_lossy(),
                    now.timestamp(),
                    now.timestamp_millis()
                ],
            )
            .unwrap();

        let waiting = discover_codex_tasks(&database, &index, now).unwrap();
        assert_eq!(waiting.events.len(), 1);
        assert_eq!(
            waiting.events[0].payload.phase,
            Some(AgentPhase::WaitingInput)
        );
        assert_eq!(
            waiting.events[0].payload.current_action.as_deref(),
            Some("Ждёт ответа")
        );
        assert!(!serde_json::to_string(&waiting.events[0])
            .unwrap()
            .contains("PRIVATE QUESTION"));

        fs::OpenOptions::new()
            .append(true)
            .open(&rollout)
            .unwrap()
            .write_all(
                b"{\"timestamp\":\"2026-07-19T12:00:02Z\",\"type\":\"response_item\",\"payload\":{\"type\":\"function_call_output\",\"call_id\":\"call-1\",\"output\":\"PRIVATE ANSWER\"}}\n",
            )
            .unwrap();

        let resumed = discover_codex_tasks(&database, &index, now).unwrap();
        assert_eq!(resumed.events.len(), 1);
        assert_eq!(resumed.events[0].payload.phase, Some(AgentPhase::Working));
        assert_eq!(
            resumed.events[0].payload.current_action.as_deref(),
            Some("Работает в Codex")
        );
        assert!(!serde_json::to_string(&resumed.events[0])
            .unwrap()
            .contains("PRIVATE ANSWER"));
        drop(connection);
        fs::remove_dir_all(directory).unwrap();
    }

    #[test]
    fn working_root_uses_typed_task_started_timestamp() {
        let directory = temp_registry("codex-working-start");
        let sessions = directory
            .join("sessions")
            .join("2026")
            .join("07")
            .join("19");
        fs::create_dir_all(&sessions).unwrap();
        let rollout = sessions.join("rollout.jsonl");
        let database = directory.join("state_5.sqlite");
        let index = directory.join("session_index.jsonl");
        let connection = discovery_database(&database);
        let now = Utc::now();
        let started_at = (now - chrono::Duration::minutes(17)).to_rfc3339();
        fs::write(
            &rollout,
            format!(
                "{{\"timestamp\":\"{started_at}\",\"type\":\"event_msg\",\"payload\":{{\"type\":\"task_started\",\"turn_id\":\"turn-1\"}}}}\n"
            ),
        )
        .unwrap();
        connection
            .execute(
                "INSERT INTO threads \
                 (id, cwd, rollout_path, updated_at, updated_at_ms, archived) \
                 VALUES (?1, ?2, ?3, ?4, ?5, 0)",
                rusqlite::params![
                    "thread-working-start",
                    r"C:\Projects\PetCrew",
                    rollout.to_string_lossy(),
                    now.timestamp(),
                    now.timestamp_millis()
                ],
            )
            .unwrap();

        let scan = discover_codex_tasks(&database, &index, now).unwrap();

        assert_eq!(scan.events.len(), 1);
        assert_eq!(scan.events[0].payload.phase, Some(AgentPhase::Working));
        assert_eq!(
            scan.events[0].payload.started_at.as_deref(),
            Some(started_at.as_str())
        );
        drop(connection);
        fs::remove_dir_all(directory).unwrap();
    }

    #[test]
    fn activity_updates_preserve_turn_start() {
        let mut store = EventStore::load(None);
        let mut started = sample_event("started-with-clock", 1);
        started.event_type = EventType::Started;
        started.payload.progress = None;
        started.payload.started_at = None;
        let first = store.apply(started).unwrap();
        let started_at = first.agents[0].started_at.clone();

        let mut activity = sample_event("later-activity", 2);
        activity.occurred_at = "2026-07-17T16:05:00+03:00".to_string();
        activity.payload.started_at = None;
        let updated = store.apply(activity).unwrap();

        assert_eq!(updated.agents[0].started_at, started_at);
    }

    #[test]
    fn recovered_placeholder_can_refresh_at_same_database_sequence() {
        let mut store = EventStore::load(None);
        let updated_at_ms = Utc::now().timestamp_millis();
        let waiting = discovered_event(
            "thread-refresh",
            "thread-refresh",
            None,
            r"C:\Projects\PetCrew",
            "PetCrew",
            "Ждёт ответа",
            AgentPhase::WaitingInput,
            updated_at_ms,
            false,
        )
        .unwrap();
        store.apply(waiting).unwrap();
        let working = discovered_event(
            "thread-refresh",
            "thread-refresh",
            None,
            r"C:\Projects\PetCrew",
            "PetCrew",
            "Продолжает работу",
            AgentPhase::Working,
            updated_at_ms,
            false,
        )
        .unwrap();

        let snapshot = store.apply(working).unwrap();

        assert_eq!(snapshot.agents[0].phase, AgentPhase::Working);
        assert_eq!(snapshot.agents[0].current_action, "Продолжает работу");
    }

    #[test]
    fn recovered_completed_refresh_preserves_acknowledgement() {
        let mut store = EventStore::load(None);
        let updated_at_ms = Utc::now().timestamp_millis();
        let completed = discovered_event(
            "thread-completed",
            "thread-completed",
            None,
            r"C:\Projects\PetCrew",
            "PetCrew",
            "Работа закончена",
            AgentPhase::Completed,
            updated_at_ms,
            false,
        )
        .unwrap();
        let same_turn_started_at = (DateTime::<Utc>::from_timestamp_millis(updated_at_ms).unwrap()
            - chrono::Duration::minutes(1))
        .to_rfc3339();
        let mut completed = completed;
        completed.payload.started_at = Some(same_turn_started_at.clone());
        let key = completed.key();
        let initial = store.apply(completed).unwrap();
        assert!(initial.agents[0].unread);
        store.acknowledge(&key).unwrap();

        let refreshed = discovered_event(
            "thread-completed",
            "thread-completed",
            None,
            r"C:\Projects\PetCrew",
            "PetCrew",
            "Уточнённый результат",
            AgentPhase::Completed,
            updated_at_ms,
            false,
        )
        .unwrap();
        let mut refreshed = refreshed;
        refreshed.payload.started_at = Some(same_turn_started_at);
        let snapshot = store.apply(refreshed).unwrap();

        assert!(!snapshot.agents[0].unread);
        assert_eq!(snapshot.agents[0].current_action, "Уточнённый результат");
    }

    #[test]
    fn recovered_new_completion_becomes_unread_when_working_sample_was_missed() {
        let mut store = EventStore::load(None);
        let older_completed_at = DateTime::parse_from_rfc3339("2026-07-19T12:00:00Z")
            .unwrap()
            .timestamp_millis();
        let mut older = discovered_event(
            "thread-missed-working",
            "thread-missed-working",
            None,
            r"C:\Projects\PetCrew",
            "PetCrew",
            "Старый результат",
            AgentPhase::Completed,
            older_completed_at,
            false,
        )
        .unwrap();
        older.payload.started_at = Some("2026-07-19T11:55:00Z".to_string());
        let key = older.key();
        store.apply(older).unwrap();
        store.acknowledge(&key).unwrap();

        let newer_completed_at = DateTime::parse_from_rfc3339("2026-07-19T12:10:00Z")
            .unwrap()
            .timestamp_millis();
        let mut newer = discovered_event(
            "thread-missed-working",
            "thread-missed-working",
            None,
            r"C:\Projects\PetCrew",
            "PetCrew",
            "Новый результат",
            AgentPhase::Completed,
            newer_completed_at,
            false,
        )
        .unwrap();
        newer.payload.started_at = None;

        let snapshot = store.apply(newer).unwrap();

        assert_eq!(snapshot.agents.len(), 1);
        assert_eq!(snapshot.agents[0].phase, AgentPhase::Completed);
        assert!(snapshot.agents[0].unread);
        assert_eq!(
            snapshot.agents[0].result.as_deref(),
            Some("Новый результат")
        );
        assert_eq!(
            snapshot.agents[0].started_at.as_deref(),
            Some("2026-07-19T12:10:00+00:00")
        );
    }

    #[test]
    fn recovered_working_refresh_cannot_move_the_turn_start_backwards() {
        let mut store = EventStore::load(None);
        let first_updated_at = DateTime::parse_from_rfc3339("2026-07-19T12:06:00Z")
            .unwrap()
            .timestamp_millis();
        let mut first = discovered_event(
            "thread-stable-clock",
            "thread-stable-clock",
            None,
            r"C:\Projects\PetCrew",
            "PetCrew",
            "Работает",
            AgentPhase::Working,
            first_updated_at,
            false,
        )
        .unwrap();
        first.payload.started_at = Some("2026-07-19T12:05:00Z".to_string());
        store.apply(first).unwrap();

        let next_updated_at = DateTime::parse_from_rfc3339("2026-07-19T12:07:00Z")
            .unwrap()
            .timestamp_millis();
        let mut stale_refresh = discovered_event(
            "thread-stable-clock",
            "thread-stable-clock",
            None,
            r"C:\Projects\PetCrew",
            "PetCrew",
            "Продолжает работу",
            AgentPhase::Working,
            next_updated_at,
            false,
        )
        .unwrap();
        stale_refresh.payload.started_at = Some("2026-07-19T11:00:00Z".to_string());

        let snapshot = store.apply(stale_refresh).unwrap();

        assert_eq!(snapshot.agents[0].phase, AgentPhase::Working);
        assert_eq!(
            snapshot.agents[0].started_at.as_deref(),
            Some("2026-07-19T12:05:00Z")
        );
    }

    #[test]
    fn latest_task_complete_wins_when_its_start_fell_outside_the_tail() {
        let directory = temp_registry("codex-rollout-missing-start");
        let sessions = directory.join("sessions");
        fs::create_dir_all(&sessions).unwrap();
        let rollout = sessions.join("rollout.jsonl");
        fs::write(
            &rollout,
            concat!(
                "{\"timestamp\":\"2026-07-19T12:00:00Z\",\"type\":\"event_msg\",\"payload\":{\"type\":\"task_started\",\"turn_id\":\"older-turn\"}}\n",
                "{\"timestamp\":\"2026-07-19T12:10:00Z\",\"type\":\"event_msg\",\"payload\":{\"type\":\"task_complete\",\"turn_id\":\"newer-turn\"}}\n"
            ),
        )
        .unwrap();

        let state = rollout_state(&rollout, &sessions).unwrap();

        assert_eq!(state.is_working(), Some(false));
        fs::remove_dir_all(directory).unwrap();
    }

    #[test]
    fn active_rollout_survives_when_task_started_falls_outside_the_tail() {
        use std::io::Write as _;

        let directory = temp_registry("codex-rollout-active-beyond-tail");
        let sessions = directory.join("sessions");
        fs::create_dir_all(&sessions).unwrap();
        let rollout = sessions.join("rollout.jsonl");
        let started_at = "2026-07-19T12:00:00Z";
        let start = format!(
            "{{\"timestamp\":\"{started_at}\",\"type\":\"event_msg\",\"payload\":{{\"type\":\"task_started\",\"turn_id\":\"long-turn\"}}}}\n"
        );
        let filler = format!(
            "{{\"type\":\"response_item\",\"payload\":{{\"type\":\"reasoning\",\"data\":\"{}\"}}}}\n",
            "x".repeat(MAX_ROLLOUT_TAIL_BYTES as usize + 1024)
        );
        fs::write(&rollout, format!("{start}{filler}")).unwrap();

        let initial = rollout_state(&rollout, &sessions).unwrap();
        assert_eq!(initial.is_working(), Some(true));
        assert_eq!(initial.started_at.as_deref(), Some(started_at));

        let activity = concat!(
            "{\"timestamp\":\"2026-07-19T12:10:00Z\",\"type\":\"event_msg\",",
            "\"payload\":{\"type\":\"agent_message\",\"message\":\"Продолжает проверку\"}}\n"
        );
        fs::OpenOptions::new()
            .append(true)
            .open(&rollout)
            .unwrap()
            .write_all(activity.as_bytes())
            .unwrap();

        let refreshed = rollout_state(&rollout, &sessions).unwrap();
        assert_eq!(refreshed.is_working(), Some(true));
        assert_eq!(refreshed.started_at.as_deref(), Some(started_at));
        assert_eq!(
            refreshed.last_message.as_deref(),
            Some("Продолжает проверку")
        );
        fs::remove_dir_all(directory).unwrap();
    }

    #[test]
    fn recovered_retention_keeps_recent_results_and_graces_missing_active_cards() {
        let mut store = EventStore::load(None);
        let now = Utc::now();
        let recent_completed = discovered_event(
            "thread-recent-result",
            "thread-recent-result",
            None,
            r"C:\Projects\PetCrew",
            "Недавний результат",
            "Готово",
            AgentPhase::Completed,
            (now - chrono::Duration::minutes(CODEX_DISCOVERY_TTL_MINUTES + 1)).timestamp_millis(),
            false,
        )
        .unwrap();
        let recent_key = recent_completed.key();
        store.apply(recent_completed).unwrap();

        let stale_completed = discovered_event(
            "thread-stale-result",
            "thread-stale-result",
            None,
            r"C:\Projects\PetCrew",
            "Старый результат",
            "Готово",
            AgentPhase::Completed,
            (now - chrono::Duration::hours(TERMINAL_REGISTRY_TTL_HOURS + 1)).timestamp_millis(),
            false,
        )
        .unwrap();
        let stale_key = stale_completed.key();
        store.apply(stale_completed).unwrap();

        let inactive_working = discovered_event(
            "thread-inactive-work",
            "thread-inactive-work",
            None,
            r"C:\Projects\PetCrew",
            "Старая работа",
            "Работает",
            AgentPhase::Working,
            (now - chrono::Duration::minutes(CODEX_DISCOVERY_TTL_MINUTES + 1)).timestamp_millis(),
            false,
        )
        .unwrap();
        let working_key = inactive_working.key();
        store.apply(inactive_working).unwrap();

        let changed = store.retain_recovered(&HashSet::new(), now);
        let snapshot = store.snapshot();
        let keys = snapshot
            .agents
            .iter()
            .map(|agent| agent.key.as_str())
            .collect::<HashSet<_>>();

        assert!(changed);
        assert!(keys.contains(recent_key.as_str()));
        assert!(!keys.contains(stale_key.as_str()));
        assert!(keys.contains(working_key.as_str()));

        let expired_missing =
            now + chrono::Duration::seconds(CODEX_DISCOVERY_MISSING_GRACE_SECONDS + 1);
        assert!(store.retain_recovered(&HashSet::new(), expired_missing));
        assert!(!store.agents.contains_key(&working_key));
    }

    #[test]
    fn rediscovery_resets_the_missing_active_card_grace() {
        let mut store = EventStore::load(None);
        let now = Utc::now();
        let working = discovered_event(
            "thread-transient-gap",
            "thread-transient-gap",
            None,
            r"C:\Projects\PetCrew",
            "PetCrew",
            "Работает",
            AgentPhase::Working,
            now.timestamp_millis(),
            false,
        )
        .unwrap();
        let key = working.key();
        store.apply(working).unwrap();

        assert!(!store.retain_recovered(&HashSet::new(), now));
        assert!(store.agents.contains_key(&key));

        let present_at = now + chrono::Duration::seconds(CODEX_DISCOVERY_MISSING_GRACE_SECONDS - 1);
        assert!(!store.retain_recovered(&HashSet::from([key.clone()]), present_at));

        let missing_again =
            now + chrono::Duration::seconds(CODEX_DISCOVERY_MISSING_GRACE_SECONDS + 1);
        assert!(!store.retain_recovered(&HashSet::new(), missing_again));
        assert!(store.agents.contains_key(&key));

        let finally_expired =
            missing_again + chrono::Duration::seconds(CODEX_DISCOVERY_MISSING_GRACE_SECONDS + 1);
        assert!(store.retain_recovered(&HashSet::new(), finally_expired));
        assert!(!store.agents.contains_key(&key));
    }

    #[test]
    fn excludes_internal_guardian_but_keeps_user_visible_delegated_root() {
        assert_eq!(safe_assistant_status(r#"{"outcome":"allow"}"#), None);
        assert_eq!(
            safe_assistant_status("Проверяет данные"),
            Some("Проверяет данные".to_string())
        );

        let directory = temp_registry("codex-guardian");
        let database = directory.join("state_5.sqlite");
        let index = directory.join("session_index.jsonl");
        let connection = discovery_database(&database);
        let now = Utc::now();
        connection
            .execute(
                "INSERT INTO threads \
                 (id, cwd, updated_at, updated_at_ms, archived, thread_source, source) \
                 VALUES (?1, ?2, ?3, ?4, 0, 'subagent', \
                         '{\"subagent\":{\"other\":\"guardian\"}}')",
                rusqlite::params![
                    "guardian-thread",
                    r"C:\Projects\PetCrew",
                    now.timestamp(),
                    now.timestamp_millis()
                ],
            )
            .unwrap();
        connection
            .execute(
                "INSERT INTO threads \
                 (id, cwd, updated_at, updated_at_ms, archived, thread_source, source) \
                 VALUES (?1, ?2, ?3, ?4, 0, 'subagent', 'vscode')",
                rusqlite::params![
                    "delegated-user-thread",
                    r"C:\Projects\PetCrew",
                    now.timestamp(),
                    now.timestamp_millis()
                ],
            )
            .unwrap();

        let scan = discover_codex_tasks(&database, &index, now).unwrap();

        assert_eq!(scan.events.len(), 1);
        assert!(scan.events[0]
            .session_id
            .contains(&opaque_digest("delegated-user-thread")));
        drop(connection);
        fs::remove_dir_all(directory).unwrap();
    }

    #[test]
    fn validates_only_canonical_codex_thread_ids() {
        assert!(valid_codex_thread_id(
            "018f0000-0000-7000-8000-000000000001"
        ));
        assert!(!valid_codex_thread_id("../../settings"));
        assert!(!valid_codex_thread_id(
            "018f0000-0000-7000-8000-00000000000Z"
        ));
    }

    #[test]
    fn constructs_only_a_fixed_opencode_project_uri_for_an_existing_directory() {
        let directory = temp_registry("opencode-navigation target");
        let uri = opencode_project_uri(directory.to_str().unwrap()).unwrap();
        assert!(uri.starts_with("opencode://open-project?directory="));
        assert!(uri.contains("opencode-navigation+target"));
        assert!(!uri.contains(' '));
        assert_eq!(
            opencode_project_uri(r"C:\definitely-missing-petcrew-project"),
            Err("invalid_opencode_directory")
        );
        assert_eq!(
            opencode_project_uri(r"..\relative"),
            Err("invalid_opencode_directory")
        );
        fs::remove_dir_all(directory).unwrap();
    }

    #[test]
    fn resolves_only_the_standard_existing_opencode_desktop_executable() {
        let local_app_data = temp_registry("opencode-desktop-path");
        assert_eq!(
            opencode_desktop_executable(&local_app_data),
            Err("opencode_desktop_not_found")
        );

        let executable = local_app_data
            .join("Programs")
            .join("@opencode-aidesktop")
            .join("OpenCode.exe");
        fs::create_dir_all(executable.parent().unwrap()).unwrap();
        fs::write(&executable, b"test").unwrap();
        assert_eq!(
            opencode_desktop_executable(&local_app_data).unwrap(),
            executable
        );
        fs::remove_dir_all(local_app_data).unwrap();
    }

    #[test]
    fn cache_does_not_persist_navigation_target() {
        let directory = temp_registry("navigation-cache");
        let cache_path = directory.join("hub-cache.json");
        let mut store = EventStore::load(Some(cache_path.clone()));
        let event = discovered_event(
            "018f0000-0000-7000-8000-000000000001",
            "018f0000-0000-7000-8000-000000000001",
            None,
            r"C:\Projects\PetCrew",
            "PetCrew",
            "Ждёт ответа",
            AgentPhase::WaitingInput,
            Utc::now().timestamp_millis(),
            false,
        )
        .unwrap();
        let snapshot = store.apply(event).unwrap();

        assert!(snapshot.agents[0].navigation.is_some());
        let cached: CacheFile = serde_json::from_slice(&fs::read(&cache_path).unwrap()).unwrap();
        assert!(cached.agents[0].navigation.is_none());
        fs::remove_dir_all(directory).unwrap();
    }

    #[test]
    fn authoritative_hook_replaces_recovered_root_placeholder() {
        let mut store = EventStore::load(None);
        let now = Utc::now();
        let bootstrap = discovered_event(
            "thread-real",
            "thread-real",
            None,
            r"C:\Projects\PetCrew",
            "PetCrew",
            "Недавно активна в Codex",
            AgentPhase::Working,
            now.timestamp_millis(),
            false,
        )
        .unwrap();
        store.apply(bootstrap).unwrap();

        let mut hook = sample_event("real-hook", 1);
        hook.provider = Provider::Codex;
        hook.session_id = format!("session:{}", opaque_digest("thread-real"));
        hook.agent_id = format!("turn:{}", opaque_digest("turn-real"));
        hook.occurred_at = now.to_rfc3339();
        let snapshot = store.apply(hook).unwrap();

        assert_eq!(snapshot.agents.len(), 1);
        assert!(snapshot.agents[0].agent_id.starts_with("turn:"));
    }

    #[test]
    fn recovered_codex_navigation_restores_a_terminal_hook_card() {
        let mut store = EventStore::load(None);
        let now = Utc::now();
        let session_id = format!("session:{}", opaque_digest("thread-finished"));

        let mut completed = completed_event(1, true);
        completed.provider = Provider::Codex;
        completed.session_id = session_id;
        completed.agent_id = format!("turn:{}", opaque_digest("turn-finished"));
        completed.payload.navigation = None;
        store.apply(completed).unwrap();

        let recovered = discovered_event(
            "thread-finished",
            "thread-finished",
            None,
            r"C:\Projects\PetCrew",
            "PetCrew",
            "Закончил работу",
            AgentPhase::Completed,
            now.timestamp_millis(),
            false,
        )
        .unwrap();

        assert_eq!(store.merge_recovered_navigation(&recovered), Some(true));
        let snapshot = store.snapshot();
        assert_eq!(snapshot.agents.len(), 1);
        assert_eq!(snapshot.agents[0].phase, AgentPhase::Completed);
        assert_eq!(
            snapshot.agents[0].navigation.as_ref().unwrap().target,
            "thread-finished"
        );
    }

    #[test]
    fn recovered_navigation_probe_cannot_delete_an_unmatched_recovered_card() {
        let mut store = EventStore::load(None);
        let recovered = discovered_event(
            "thread-unmatched",
            "thread-unmatched",
            None,
            r"C:\Projects\PetCrew",
            "PetCrew",
            "Работает",
            AgentPhase::Working,
            Utc::now().timestamp_millis(),
            false,
        )
        .unwrap();
        let key = recovered.key();
        store.apply(recovered.clone()).unwrap();

        assert_eq!(store.merge_recovered_navigation(&recovered), None);
        assert!(store.agents.contains_key(&key));
        assert!(matches!(store.apply(recovered), Err(ApplyError::Replay)));
        assert!(store.agents.contains_key(&key));
    }

    #[tokio::test]
    async fn http_endpoint_requires_bearer_and_accepts_valid_event() {
        let state = HttpState {
            token: Arc::new("test-token".to_string()),
            store: SharedStore(Arc::new(Mutex::new(EventStore::load(None)))),
            emitter: None,
            completion_notifier: test_notifier(),
        };
        let router = build_router(state);
        let body = serde_json::to_vec(&sample_event("event-http", 1)).unwrap();

        let unauthorized = router
            .clone()
            .oneshot(
                Request::builder()
                    .method(Method::POST)
                    .uri("/v1/events")
                    .header(CONTENT_TYPE, "application/json")
                    .body(Body::from(body.clone()))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(unauthorized.status(), StatusCode::UNAUTHORIZED);

        let accepted = router
            .oneshot(
                Request::builder()
                    .method(Method::POST)
                    .uri("/v1/events")
                    .header(CONTENT_TYPE, "application/json")
                    .header(AUTHORIZATION, "Bearer test-token")
                    .body(Body::from(body))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(accepted.status(), StatusCode::ACCEPTED);
    }

    #[tokio::test]
    async fn snapshot_endpoint_requires_bearer_and_returns_complete_state() {
        let shared = SharedStore(Arc::new(Mutex::new(EventStore::load(None))));
        shared
            .0
            .lock()
            .unwrap()
            .apply(sample_event("snapshot-event", 1))
            .unwrap();
        let state = HttpState {
            token: Arc::new("test-token".to_string()),
            store: shared,
            emitter: None,
            completion_notifier: test_notifier(),
        };
        let router = build_router(state);

        let unauthorized = router
            .clone()
            .oneshot(
                Request::builder()
                    .method(Method::GET)
                    .uri("/v1/snapshot")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(unauthorized.status(), StatusCode::UNAUTHORIZED);

        let response = router
            .oneshot(
                Request::builder()
                    .method(Method::GET)
                    .uri("/v1/snapshot")
                    .header(AUTHORIZATION, "Bearer test-token")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        let body = axum::body::to_bytes(response.into_body(), 64 * 1024)
            .await
            .unwrap();
        let snapshot: HubSnapshot = serde_json::from_slice(&body).unwrap();
        assert_eq!(snapshot.revision, 1);
        assert_eq!(snapshot.agents.len(), 1);
    }

    #[tokio::test]
    async fn acknowledgement_endpoint_updates_only_local_snapshot_state() {
        let shared = SharedStore(Arc::new(Mutex::new(EventStore::load(None))));
        let key = {
            let snapshot = shared
                .0
                .lock()
                .unwrap()
                .apply(completed_event(7, true))
                .unwrap();
            snapshot.agents[0].key.clone()
        };
        let state = HttpState {
            token: Arc::new("test-token".to_string()),
            store: shared,
            emitter: None,
            completion_notifier: test_notifier(),
        };
        let response = build_router(state)
            .oneshot(
                Request::builder()
                    .method(Method::POST)
                    .uri("/v1/acknowledgements")
                    .header(CONTENT_TYPE, "application/json")
                    .header(AUTHORIZATION, "Bearer test-token")
                    .body(Body::from(
                        serde_json::to_vec(&serde_json::json!({ "key": key })).unwrap(),
                    ))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        let body = axum::body::to_bytes(response.into_body(), 64 * 1024)
            .await
            .unwrap();
        let snapshot: HubSnapshot = serde_json::from_slice(&body).unwrap();
        assert!(!snapshot.agents[0].unread);
        assert!(snapshot.agents[0].phase.is_terminal());
    }

    #[tokio::test]
    async fn snapshot_sse_replays_current_revision_and_then_streams_changes() {
        let shared = SharedStore(Arc::new(Mutex::new(EventStore::load(None))));
        let notifier = test_notifier();
        let emitter_notifier = notifier.clone();
        let state = HttpState {
            token: Arc::new("test-token".to_string()),
            store: shared,
            emitter: Some(Arc::new(move |snapshot| {
                let _ = emitter_notifier.send(snapshot.revision);
            })),
            completion_notifier: notifier,
        };
        let router = build_router(state);

        let response = router
            .clone()
            .oneshot(
                Request::builder()
                    .method(Method::GET)
                    .uri("/v1/snapshots/stream?after=0")
                    .header(AUTHORIZATION, "Bearer test-token")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        let mut body = response.into_body().into_data_stream();
        let chunk_task = tokio::spawn(async move {
            tokio::time::timeout(Duration::from_secs(2), body.next())
                .await
                .unwrap()
                .unwrap()
                .unwrap()
        });
        tokio::task::yield_now().await;

        let event = sample_event("snapshot-sse", 1);
        let accepted = router
            .oneshot(
                Request::builder()
                    .method(Method::POST)
                    .uri("/v1/events")
                    .header(CONTENT_TYPE, "application/json")
                    .header(AUTHORIZATION, "Bearer test-token")
                    .body(Body::from(serde_json::to_vec(&event).unwrap()))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(accepted.status(), StatusCode::ACCEPTED);

        let text = String::from_utf8(chunk_task.await.unwrap().to_vec()).unwrap();
        assert!(text.contains("event: snapshot"));
        assert!(text.contains("id: 1"));
        assert!(text.contains("Проверить local hub"));
    }

    #[tokio::test]
    async fn completion_inbox_requires_bearer_and_returns_only_sanitary_records() {
        let shared = SharedStore(Arc::new(Mutex::new(EventStore::load(None))));
        let digest = "8".repeat(64);
        shared
            .0
            .lock()
            .unwrap()
            .apply(recent_opencode_completion("completion-http", &digest))
            .unwrap();
        let state = HttpState {
            token: Arc::new("test-token".to_string()),
            store: shared,
            emitter: None,
            completion_notifier: test_notifier(),
        };
        let router = build_router(state);

        let unauthorized = router
            .clone()
            .oneshot(
                Request::builder()
                    .method(Method::GET)
                    .uri("/v1/completions?after=0")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(unauthorized.status(), StatusCode::UNAUTHORIZED);

        let response = router
            .oneshot(
                Request::builder()
                    .method(Method::GET)
                    .uri("/v1/completions?after=0")
                    .header(AUTHORIZATION, "Bearer test-token")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        let body = axum::body::to_bytes(response.into_body(), 64 * 1024)
            .await
            .unwrap();
        let json: serde_json::Value = serde_json::from_slice(&body).unwrap();
        let record = &json["completions"][0];
        assert_eq!(record["session_id"], format!("session:{digest}"));
        for forbidden in [
            "task",
            "project",
            "path",
            "result",
            "summary",
            "progress",
            "navigation",
            "change_summary",
        ] {
            assert!(record.get(forbidden).is_none(), "{forbidden}");
        }
    }

    #[tokio::test]
    async fn completion_sse_stream_receives_the_existing_accepted_event_path() {
        let shared = SharedStore(Arc::new(Mutex::new(EventStore::load(None))));
        let notifier = test_notifier();
        let emitter_notifier = notifier.clone();
        let state = HttpState {
            token: Arc::new("test-token".to_string()),
            store: shared,
            emitter: Some(Arc::new(move |snapshot| {
                let _ = emitter_notifier.send(snapshot.revision);
            })),
            completion_notifier: notifier,
        };
        let router = build_router(state);
        let response = router
            .clone()
            .oneshot(
                Request::builder()
                    .method(Method::GET)
                    .uri("/v1/completions/stream?after=0")
                    .header(AUTHORIZATION, "Bearer test-token")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        assert_eq!(
            response.headers().get(CONTENT_TYPE).unwrap(),
            "text/event-stream"
        );
        let mut body = response.into_body().into_data_stream();
        let chunk_task = tokio::spawn(async move {
            tokio::time::timeout(Duration::from_secs(2), body.next())
                .await
                .unwrap()
                .unwrap()
                .unwrap()
        });
        tokio::task::yield_now().await;

        let digest = "9".repeat(64);
        let event = recent_opencode_completion("completion-sse", &digest);
        let accepted = router
            .oneshot(
                Request::builder()
                    .method(Method::POST)
                    .uri("/v1/events")
                    .header(CONTENT_TYPE, "application/json")
                    .header(AUTHORIZATION, "Bearer test-token")
                    .body(Body::from(serde_json::to_vec(&event).unwrap()))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(accepted.status(), StatusCode::ACCEPTED);

        let text = String::from_utf8(chunk_task.await.unwrap().to_vec()).unwrap();
        assert!(text.contains("event: completion"));
        assert!(text.contains(&format!("session:{digest}")));
        assert!(!text.contains("Готово 0"));
    }

    #[tokio::test]
    async fn http_endpoint_preserves_payload_limit_status() {
        let state = HttpState {
            token: Arc::new("test-token".to_string()),
            store: SharedStore(Arc::new(Mutex::new(EventStore::load(None)))),
            emitter: None,
            completion_notifier: test_notifier(),
        };
        let response = build_router(state)
            .oneshot(
                Request::builder()
                    .method(Method::POST)
                    .uri("/v1/events")
                    .header(CONTENT_TYPE, "application/json")
                    .header(AUTHORIZATION, "Bearer test-token")
                    .body(Body::from(vec![b' '; MAX_BODY_BYTES + 1]))
                    .unwrap(),
            )
            .await
            .unwrap();

        assert_eq!(response.status(), StatusCode::PAYLOAD_TOO_LARGE);
    }

    #[tokio::test]
    async fn cors_allows_only_the_packaged_tauri_origin() {
        let state = HttpState {
            token: Arc::new("test-token".to_string()),
            store: SharedStore(Arc::new(Mutex::new(EventStore::load(None)))),
            emitter: None,
            completion_notifier: test_notifier(),
        };
        let response = build_router(state)
            .oneshot(
                Request::builder()
                    .method(Method::OPTIONS)
                    .uri("/v1/events")
                    .header("origin", "http://tauri.localhost")
                    .header("access-control-request-method", "POST")
                    .header(
                        "access-control-request-headers",
                        "authorization,content-type",
                    )
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();

        assert_eq!(
            response
                .headers()
                .get("access-control-allow-origin")
                .unwrap(),
            "http://tauri.localhost"
        );
    }
}
