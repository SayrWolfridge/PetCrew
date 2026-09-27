import { isTauri } from "@tauri-apps/api/core";
import { getCurrentWindow } from "@tauri-apps/api/window";
import type {
  KeyboardEvent as ReactKeyboardEvent,
  MouseEvent as ReactMouseEvent,
} from "react";
import { useId } from "react";
import { agentDisclosureKey } from "./list-disclosure";
import {
  displayProgress,
  elapsedWorkingLabel,
  PHASE_LABELS,
  progressRatio,
  teamCounts,
  terminalTimeLabel,
} from "./model";
import type { AgentProjectGroup } from "./model";
import type { DemoAgent, Density, Provider, TeamMood } from "./types";
import { pickupPrompt } from "./return-pickup";

const TERMINAL_PHASES = new Set<DemoAgent["phase"]>(["completed", "failed", "cancelled"]);

const MOOD_TEXT: Record<TeamMood, string> = {
  idle: "Стая отдыхает",
  working: "Стая работает",
  attention: "Кому-то нужна ты",
  success: "Принесли результат",
  blocked: "Есть препятствие",
};

const PROVIDER_LABELS: Record<Provider, string> = {
  codex: "codex",
  opencode: "opencode",
  simulator: "demo hub",
};

function Progress({
  agent,
  onAcknowledge,
}: {
  agent: DemoAgent;
  onAcknowledge?: () => void;
}) {
  const text = displayProgress(agent);
  const ratio = progressRatio(agent);

  return (
    <div
      className={`progress${onAcknowledge ? " progress--acknowledge" : ""}`}
      aria-label={text ? `Прогресс ${text}` : "Прогресс без точной оценки"}
    >
      {onAcknowledge ? (
        <button
          className="progress__ack"
          type="button"
          onClick={(event) => {
            event.stopPropagation();
            onAcknowledge();
          }}
        >
          ✓ Прочитано
        </button>
      ) : (
        <div className="progress__track">
          <span
            className={ratio === null ? "progress__fill progress__fill--indeterminate" : "progress__fill"}
            style={ratio === null ? undefined : { width: `${ratio * 100}%` }}
          />
        </div>
      )}
      <span className="progress__text">{text ?? "без оценки"}</span>
    </div>
  );
}

function ChangeSummary({ agent }: { agent: DemoAgent }) {
  const summary = agent.change_summary;
  if (!summary) return null;
  const mod100 = summary.files % 100;
  const mod10 = summary.files % 10;
  const fileNoun = mod100 >= 11 && mod100 <= 14
    ? "файлов"
    : mod10 === 1
      ? "файл"
      : mod10 >= 2 && mod10 <= 4
        ? "файла"
        : "файлов";
  return (
    <div className="agent__changes" aria-label={`Изменено файлов ${summary.files}, добавлено строк ${summary.additions}, удалено строк ${summary.deletions}`}>
      Изменено {summary.files} {fileNoun}
      {` · +${summary.additions} −${summary.deletions}`}
    </div>
  );
}

export function AgentCard({
  agent,
  density,
  onAcknowledge,
  onOpen,
  onPickup,
  collapsible = false,
  expanded = true,
  onToggleExpanded,
  nowMillis,
}: {
  agent: DemoAgent;
  density: Density;
  onAcknowledge: (agent: DemoAgent) => void;
  onOpen: (agent: DemoAgent) => void;
  onPickup: (agent: DemoAgent) => void;
  collapsible?: boolean;
  expanded?: boolean;
  onToggleExpanded?: () => void;
  nowMillis: number;
}) {
  const hasResult = TERMINAL_PHASES.has(agent.phase) && Boolean(agent.result);
  const canAcknowledge = TERMINAL_PHASES.has(agent.phase) && Boolean(agent.unread);
  const canOpen = Boolean(
    agent.navigation?.target
      && (agent.navigation.kind === "task"
        || (agent.provider === "opencode" && agent.navigation.kind === "provider")),
  );
  const canPickup = Boolean(pickupPrompt(agent));
  const elapsed = elapsedWorkingLabel(agent, nowMillis);
  const finishedAt = terminalTimeLabel(agent, nowMillis);
  const showDetails = !collapsible || expanded;
  const activate = () => {
    if (canOpen && !canPickup) onOpen(agent);
  };
  const activateFromKeyboard = (event: ReactKeyboardEvent<HTMLElement>) => {
    if (event.target !== event.currentTarget || !canOpen || canPickup) return;
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      activate();
    }
  };

  return (
    <article
      className={`agent agent--${agent.phase} agent--${density}${agent.unread ? " agent--unread" : ""}${canOpen && !canPickup ? " agent--clickable" : ""}${collapsible ? expanded ? " agent--expanded" : " agent--collapsed" : ""}`}
      role={canOpen && !canPickup ? "button" : undefined}
      tabIndex={canOpen && !canPickup ? 0 : undefined}
      title={canOpen && !canPickup ? agent.navigation?.label : undefined}
      onClick={canPickup ? undefined : activate}
      onKeyDown={canPickup ? undefined : activateFromKeyboard}
    >
      <div className="agent__signal" aria-hidden="true" />
      <div className="agent__main">
        <div className="agent__topline">
          {collapsible ? (
            <button
              className="agent__disclosure"
              type="button"
              aria-label={expanded ? `Свернуть карточку ${agent.task}` : `Раскрыть карточку ${agent.task}`}
              aria-expanded={expanded}
              title={expanded ? "Свернуть карточку" : "Раскрыть карточку"}
              onClick={(event) => {
                event.stopPropagation();
                onToggleExpanded?.();
              }}
            >
              <span aria-hidden="true">›</span>
            </button>
          ) : null}
          <span className={`provider provider--${agent.provider}`}>{PROVIDER_LABELS[agent.provider]}</span>
          <span className="agent__project" title={agent.project}>{agent.project}</span>
          <span className="agent__phase">
            {PHASE_LABELS[agent.phase]}{elapsed ? ` · ${elapsed}` : finishedAt ? ` · ${finishedAt}` : ""}
          </span>
        </div>
        <strong className="agent__task" title={agent.task}>{agent.task}</strong>
        {showDetails ? (
          <div className="agent__details">
            <div className="agent__action">{hasResult ? agent.result : agent.current_action}</div>
            {canPickup ? (
              <div className="agent__pickup-row">
                <button className="agent__pickup" type="button" onClick={() => onPickup(agent)}>
                  Забрать результат
                </button>
                <span>Скопирует запрос — вставьте его в Codex</span>
              </div>
            ) : null}
            <ChangeSummary agent={agent} />
            <Progress
              agent={agent}
              onAcknowledge={canAcknowledge ? () => onAcknowledge(agent) : undefined}
            />
          </div>
        ) : null}
      </div>
    </article>
  );
}

export function ProjectGroup({
  group,
  density,
  collapsed,
  expandedCardKeys,
  onToggleProject,
  onToggleCard,
  onAcknowledge,
  onOpen,
  onPickup,
  nowMillis,
}: {
  group: AgentProjectGroup;
  density: Density;
  collapsed: boolean;
  expandedCardKeys: ReadonlySet<string>;
  onToggleProject: () => void;
  onToggleCard: (agent: DemoAgent) => void;
  onAcknowledge: (agent: DemoAgent) => void;
  onOpen: (agent: DemoAgent) => void;
  onPickup: (agent: DemoAgent) => void;
  nowMillis: number;
}) {
  const contentId = useId();
  const counts = teamCounts(group.agents);

  return (
    <section className={`project-group${collapsed ? " project-group--collapsed" : ""}`}>
      <button
        className="project-group__header"
        type="button"
        aria-expanded={!collapsed}
        aria-controls={contentId}
        onClick={onToggleProject}
      >
        <span className="project-group__chevron" aria-hidden="true">›</span>
        <strong className="project-group__name">{group.project}</strong>
        <span className="project-group__total">{group.agents.length}</span>
        <span className="project-group__summary">
          {counts.blocked > 0 ? <span className="project-group__count project-group__count--blocked">проблемы {counts.blocked}</span> : null}
          {counts.waiting > 0 ? <span className="project-group__count project-group__count--waiting">ждут {counts.waiting}</span> : null}
          {counts.working > 0 ? <span className="project-group__count project-group__count--working">работают {counts.working}</span> : null}
          {counts.done > 0 ? <span className="project-group__count project-group__count--done">готовы {counts.done}</span> : null}
        </span>
      </button>
      {!collapsed ? (
        <div className="project-group__cards" id={contentId}>
          {group.agents.map((agent) => (
            <AgentCard
              key={agentDisclosureKey(agent)}
              agent={agent}
              density={density}
              collapsible
              expanded={expandedCardKeys.has(agentDisclosureKey(agent))}
              onToggleExpanded={() => onToggleCard(agent)}
              onAcknowledge={onAcknowledge}
              onOpen={onOpen}
              onPickup={onPickup}
              nowMillis={nowMillis}
            />
          ))}
        </div>
      ) : null}
    </section>
  );
}

export function beginWindowDrag(event: ReactMouseEvent<HTMLElement>) {
  if (event.button !== 0 || !isTauri()) return;

  event.preventDefault();
  void getCurrentWindow().startDragging().catch((error: unknown) => {
    console.error("Не удалось начать перетаскивание окна PetCrew", error);
  });
}

export function closePetCrew() {
  if (!isTauri()) return;

  void getCurrentWindow().close().catch((error: unknown) => {
    console.error("Не удалось закрыть окно PetCrew", error);
  });
}

export function beginWindowResize(event: ReactMouseEvent<HTMLElement>) {
  if (event.button !== 0 || !isTauri()) return;

  event.preventDefault();
  void getCurrentWindow().startResizeDragging("SouthEast").catch((error: unknown) => {
    console.error("Не удалось начать изменение размера PetCrew", error);
  });
}

export function Pet({ mood }: { mood: TeamMood }) {
  return (
    <div
      className={`pet pet--${mood}`}
      aria-label={`${MOOD_TEXT[mood]}. Зажми и перетащи окно`}
      onMouseDown={beginWindowDrag}
      title="Зажми и перетащи PetCrew"
    >
      <div className="pet__stage" aria-hidden="true">
        <div className="pet__glow" />
        <div className="pet__figure">
          <div className="pet__ear pet__ear--left" />
          <div className="pet__ear pet__ear--right" />
          <div className="pet__head">
            <span className="pet__eye pet__eye--left" />
            <span className="pet__eye pet__eye--right" />
            <span className="pet__nose" />
          </div>
        </div>
      </div>
      <div className="pet__status">{MOOD_TEXT[mood]}</div>
    </div>
  );
}
