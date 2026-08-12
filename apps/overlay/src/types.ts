export type Provider = "codex" | "opencode" | "simulator";

export type AgentPhase =
  | "queued"
  | "planning"
  | "working"
  | "waiting_input"
  | "waiting_approval"
  | "blocked"
  | "completed"
  | "failed"
  | "cancelled";

export type ProgressSource = "explicit" | "inferred" | "unavailable";

export type AgentProgress =
  | {
      kind: "steps";
      current: number;
      total: number;
      source: "explicit";
    }
  | {
      kind: "activity" | "indeterminate";
      source: ProgressSource;
    };

export interface ChangeSummary {
  files: number;
  additions: number;
  deletions: number;
  source: "provider";
}

export interface DemoAgent {
  key?: string;
  agent_id: string;
  session_id?: string;
  parent_agent_id?: string | null;
  provider: Provider;
  project: string;
  task: string;
  phase: AgentPhase;
  progress: AgentProgress;
  change_summary?: ChangeSummary | null;
  current_action: string;
  started_at?: string | null;
  result?: string;
  unread?: boolean;
  navigation?: {
    kind: "task" | "terminal" | "folder" | "provider";
    label: string;
    target: string;
  } | null;
  last_sequence?: number;
  updated_at?: string;
}

export interface HubConnection {
  endpoint: string;
  token: string;
  protocol_version: "1.0";
}

export interface HubSnapshot {
  revision: number;
  agents: DemoAgent[];
  overflow: number;
}

export type Density = "detailed" | "compact" | "grouped";
export type TeamMood = "idle" | "working" | "attention" | "success" | "blocked";
