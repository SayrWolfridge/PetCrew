import { isTauri } from "@tauri-apps/api/core";
import { LogicalSize, PhysicalPosition, PhysicalSize } from "@tauri-apps/api/dpi";
import { availableMonitors, currentMonitor, getCurrentWindow, primaryMonitor } from "@tauri-apps/api/window";
import { useEffect, useMemo, useRef, useState } from "react";
import fixture from "../../../tests/fixtures/demo-team.json";
import {
  AgentCard,
  beginWindowDrag,
  beginWindowResize,
  closePetCrew,
  Pet,
} from "./components";
import {
  acknowledgeHubAgent,
  getHubConnection,
  getHubSnapshot,
  openCodexThread,
  openOpenCodeProject,
  subscribeToHub,
} from "./hub";
import {
  advanceSimulation,
  DEFAULT_RECENT_COMPLETED,
  densityForCount,
  selectLiveAgents,
  selectUnreadSubagents,
  selectUnreadTerminalAgents,
  selectVisibleAgents,
  teamCounts,
  teamMood,
} from "./model";
import type {
  DemoAgent,
  HubConnection,
  HubSnapshot,
} from "./types";
import {
  DEFAULT_PREFERENCES,
  getAppSettings,
  updateAppPreferences,
  updateWindowPlacement,
} from "./settings";
import type { AppTheme, CardLayout, Preferences, TextSize } from "./settings";
import {
  preferredSecondaryPosition,
  preferredSmallMonitorPlacement,
  restorableWindowRect,
} from "./window-placement";

const ORIGINAL_TEAM = fixture as DemoAgent[];
const TEAM_SIZES = [1, 3, 10] as const;
const RECENT_COMPLETED_OPTIONS = [0, 5, 10, 20, 50] as const;
const RECENT_COMPLETED_STORAGE_KEY = "petcrew.recentCompletedLimit";
const EMPTY_HUB: HubSnapshot = { revision: 0, agents: [], overflow: 0 };
const TEXT_SIZES: TextSize[] = ["normal", "large", "extra_large"];

type SourceMode = "fixture" | "hub";
type HubStatus = "unavailable" | "connecting" | "online" | "error";

const HUB_STATUS_TEXT: Record<HubStatus, string> = {
  unavailable: "hub недоступен",
  connecting: "hub запускается",
  online: "local hub",
  error: "ошибка hub",
};

function cloneTeam() {
  return structuredClone(ORIGINAL_TEAM);
}

function loadRecentCompletedLimit(): number {
  try {
    const stored = Number(window.localStorage.getItem(RECENT_COMPLETED_STORAGE_KEY));
    return RECENT_COMPLETED_OPTIONS.includes(stored as (typeof RECENT_COMPLETED_OPTIONS)[number])
      ? stored
      : DEFAULT_RECENT_COMPLETED;
  } catch {
    return DEFAULT_RECENT_COMPLETED;
  }
}

function hubErrorMessage(error: unknown): string {
  const code = error instanceof Error ? error.message : "unknown_error";
  const label: Record<string, string> = {
    replayed_event: "повтор события",
    stale_sequence: "устаревший номер шага",
    terminal_state: "агент уже завершён",
    invalid_progress: "некорректный прогресс",
    unauthorized: "ошибка авторизации",
  };
  return `Пакет отклонён: ${label[code] ?? code}`;
}

export default function App() {
  const [fixtureTeam, setFixtureTeam] = useState<DemoAgent[]>(cloneTeam);
  const [teamSize, setTeamSize] = useState<(typeof TEAM_SIZES)[number]>(10);
  const [recentCompletedLimit, setRecentCompletedLimit] = useState(DEFAULT_PREFERENCES.recent_completed_limit);
  const [textSize, setTextSize] = useState<TextSize>(DEFAULT_PREFERENCES.text_size);
  const [cardLayout, setCardLayout] = useState<CardLayout>(DEFAULT_PREFERENCES.card_layout);
  const [theme, setTheme] = useState<AppTheme>(DEFAULT_PREFERENCES.theme);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [source, setSource] = useState<SourceMode>(isTauri() ? "hub" : "fixture");
  const [running, setRunning] = useState(false);
  const [tick, setTick] = useState(0);
  const [hubSnapshot, setHubSnapshot] = useState<HubSnapshot>(EMPTY_HUB);
  const [hubConnection, setHubConnection] = useState<HubConnection | null>(null);
  const [hubStatus, setHubStatus] = useState<HubStatus>(isTauri() ? "connecting" : "unavailable");
  const [hubMessage, setHubMessage] = useState("");
  const [clockNow, setClockNow] = useState(Date.now);
  const preferencesRef = useRef<Preferences>(DEFAULT_PREFERENCES);
  const preferenceSaveRef = useRef<Promise<void>>(Promise.resolve());

  const team = source === "hub" ? selectLiveAgents(hubSnapshot.agents) : fixtureTeam;
  const selectedTeam = useMemo(
    () => (source === "hub" ? team : team.slice(0, teamSize)),
    [source, team, teamSize],
  );
  const selection = useMemo(
    () => selectVisibleAgents(selectedTeam, recentCompletedLimit),
    [selectedTeam, recentCompletedLimit],
  );
  const visibleAgents = selection.agents;
  const unreadResults = useMemo(
    () => selectUnreadTerminalAgents(selectedTeam),
    [selectedTeam],
  );
  const unreadSubagents = useMemo(
    () => selectUnreadSubagents(selectedTeam),
    [selectedTeam],
  );
  const overflowCount = Math.max(
    selection.overflowCount,
    source === "hub" ? hubSnapshot.overflow : 0,
  );
  const density = densityForCount(visibleAgents.length);
  const counts = teamCounts(visibleAgents);
  const mood = teamMood(visibleAgents);

  useEffect(() => {
    const timer = window.setInterval(() => setClockNow(Date.now()), 30_000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    if (!isTauri()) return;

    let disposed = false;
    let saveTimer: number | undefined;
    let unlistenMoved: (() => void) | undefined;
    let unlistenResized: (() => void) | undefined;
    const appWindow = getCurrentWindow();
    void (async () => {
      let settings = await getAppSettings();
      const legacyLimit = loadRecentCompletedLimit();
      const looksFresh = settings.window === null
        && settings.text_size === DEFAULT_PREFERENCES.text_size
        && settings.card_layout === DEFAULT_PREFERENCES.card_layout
        && settings.theme === DEFAULT_PREFERENCES.theme
        && settings.recent_completed_limit === DEFAULT_PREFERENCES.recent_completed_limit;
      if (looksFresh && legacyLimit !== DEFAULT_PREFERENCES.recent_completed_limit) {
        settings = await updateAppPreferences({
          text_size: settings.text_size,
          card_layout: settings.card_layout,
          theme: settings.theme,
          recent_completed_limit: legacyLimit,
        });
      }
      try { window.localStorage.removeItem(RECENT_COMPLETED_STORAGE_KEY); } catch { /* legacy cleanup only */ }
      if (disposed) return;
      const loadedPreferences: Preferences = {
        text_size: settings.text_size,
        card_layout: settings.card_layout,
        theme: settings.theme,
        recent_completed_limit: settings.recent_completed_limit,
      };
      preferencesRef.current = loadedPreferences;
      setTextSize(loadedPreferences.text_size);
      setCardLayout(loadedPreferences.card_layout);
      setTheme(loadedPreferences.theme);
      setRecentCompletedLimit(loadedPreferences.recent_completed_limit);

      const [physicalSize, scaleFactor, monitors, primary] = await Promise.all([
        appWindow.innerSize(),
        appWindow.scaleFactor(),
        availableMonitors(),
        primaryMonitor(),
      ]);
      if (disposed) return;
      const screenMonitors = monitors.map((monitor) => ({
        ...monitor.workArea,
        scaleFactor: monitor.scaleFactor,
        name: monitor.name,
      }));
      const restored = restorableWindowRect(settings.window, screenMonitors);
      if (restored) {
        await appWindow.setSize(new PhysicalSize(restored.width, restored.height));
        await appWindow.setPosition(new PhysicalPosition(restored.x, restored.y));
      } else {
        const logicalSize = physicalSize.toLogical(scaleFactor);
        await appWindow.setSize(
          new LogicalSize(Math.round(logicalSize.width), Math.round(logicalSize.height)),
        );
        const outerSize = await appWindow.outerSize();
        const placement = preferredSecondaryPosition(
        screenMonitors,
        primary?.workArea ?? null,
        outerSize,
      );
        if (placement) {
          await appWindow.setPosition(new PhysicalPosition(placement.x, placement.y));
        }
      }

      const scheduleSave = () => {
        if (disposed) return;
        if (saveTimer !== undefined) window.clearTimeout(saveTimer);
        saveTimer = window.setTimeout(() => {
          void (async () => {
            const [position, size, monitor] = await Promise.all([
              appWindow.outerPosition(),
              appWindow.innerSize(),
              currentMonitor(),
            ]);
            const nextPlacement = {
              x: position.x,
              y: position.y,
              width: size.width,
              height: size.height,
              monitor: monitor?.name ?? null,
            };
            if (!restorableWindowRect(nextPlacement, screenMonitors)) return;
            await updateWindowPlacement(nextPlacement);
          })().catch((error: unknown) => {
            console.error("Не удалось сохранить положение PetCrew", error);
          });
        }, 500);
      };
      [unlistenMoved, unlistenResized] = await Promise.all([
        appWindow.onMoved(scheduleSave),
        appWindow.onResized(scheduleSave),
      ]);
    })().catch((error: unknown) => {
      console.error("Не удалось загрузить настройки PetCrew", error);
    });

    return () => {
      disposed = true;
      if (saveTimer !== undefined) window.clearTimeout(saveTimer);
      unlistenMoved?.();
      unlistenResized?.();
    };
  }, []);

  useEffect(() => {
    if (!isTauri()) return;

    let disposed = false;
    let unlisten: (() => void) | undefined;
    setHubStatus("connecting");

    void (async () => {
      const connection = await getHubConnection();
      const snapshot = await getHubSnapshot(connection);
      if (disposed) return;
      setHubConnection(connection);
      setHubSnapshot(snapshot);
      setHubStatus("online");
      unlisten = await subscribeToHub(connection, snapshot.revision, (nextSnapshot) => {
        if (!disposed) setHubSnapshot(nextSnapshot);
      });
    })().catch((error: unknown) => {
      console.error("Не удалось подключить интерфейс к local hub", error);
      if (!disposed) {
        setHubStatus("error");
        setHubMessage("Local hub не запустился");
      }
    });

    return () => {
      disposed = true;
      unlisten?.();
    };
  }, []);

  useEffect(() => {
    if (!running || source !== "fixture") return;
    const timer = window.setInterval(() => {
      setTick((currentTick) => {
        const nextTick = currentTick + 1;
        setFixtureTeam((currentTeam) => advanceSimulation(currentTeam, nextTick));
        return nextTick;
      });
    }, 2200);
    return () => window.clearInterval(timer);
  }, [running, source]);

  const chooseSource = (nextSource: SourceMode) => {
    if (nextSource === "hub" && !hubConnection) return;
    setRunning(false);
    setHubMessage("");
    setSource(nextSource);
  };

  const savePreferences = (next: Preferences) => {
    preferencesRef.current = next;
    setTextSize(next.text_size);
    setCardLayout(next.card_layout);
    setTheme(next.theme);
    setRecentCompletedLimit(next.recent_completed_limit);
    preferenceSaveRef.current = preferenceSaveRef.current
      .then(() => updateAppPreferences(next))
      .then(() => undefined)
      .catch((error: unknown) => {
        console.error("Не удалось сохранить настройки PetCrew", error);
        setHubMessage("Настройка применена, но не сохранилась");
      });
  };

  const chooseRecentCompletedLimit = (limit: number) => {
    savePreferences({ ...preferencesRef.current, recent_completed_limit: limit });
  };

  const changeTextSize = (direction: -1 | 1) => {
    const current = TEXT_SIZES.indexOf(textSize);
    const next = TEXT_SIZES[Math.max(0, Math.min(TEXT_SIZES.length - 1, current + direction))];
    savePreferences({ ...preferencesRef.current, text_size: next });
  };

  const returnToSmallMonitor = () => {
    if (!isTauri()) return;

    void (async () => {
      const appWindow = getCurrentWindow();
      const [monitors, innerSize, outerSize] = await Promise.all([
        availableMonitors(),
        appWindow.innerSize(),
        appWindow.outerSize(),
      ]);
      const screenMonitors = monitors.map((monitor) => ({
        ...monitor.workArea,
        scaleFactor: monitor.scaleFactor,
        name: monitor.name,
      }));
      const placement = preferredSmallMonitorPlacement(screenMonitors, outerSize);
      if (!placement) {
        setHubMessage("Не удалось найти экран");
        return;
      }

      await appWindow.setPosition(new PhysicalPosition(placement.x, placement.y));
      await updateWindowPlacement({
        x: placement.x,
        y: placement.y,
        width: innerSize.width,
        height: innerSize.height,
        monitor: placement.monitor ?? null,
      });
      setHubMessage("PetCrew возвращён на маленький экран");
    })().catch((error: unknown) => {
      console.error("Не удалось вернуть PetCrew на маленький экран", error);
      setHubMessage("Не удалось переместить PetCrew");
    });
  };

  const resetFixture = () => {
    setRunning(false);
    setTick(0);
    setFixtureTeam(cloneTeam());
  };

  const acknowledgeMany = async (agents: DemoAgent[], label: string) => {
    const targets = agents.filter((agent) => Boolean(agent.unread));
    if (targets.length === 0) return;

    if (source === "fixture") {
      const targetIds = new Set(targets.map((agent) => agent.agent_id));
      setFixtureTeam((currentTeam) =>
        currentTeam.map((item) => (targetIds.has(item.agent_id) ? { ...item, unread: false } : item)),
      );
      return;
    }
    if (!hubConnection) return;

    const targetIds = new Set(targets.map((agent) => agent.key ?? agent.agent_id));
    setHubSnapshot((currentSnapshot) => ({
      ...currentSnapshot,
      agents: currentSnapshot.agents.map((item) => (
        targetIds.has(item.key ?? item.agent_id) ? { ...item, unread: false } : item
      )),
    }));

    const results = await Promise.allSettled(
      targets.map((agent) => (
        agent.key
          ? acknowledgeHubAgent(hubConnection, agent.key)
          : Promise.reject(new Error("hub_agent_key_missing"))
      )),
    );
    const snapshots = results
      .filter((result): result is PromiseFulfilledResult<HubSnapshot> => result.status === "fulfilled")
      .map((result) => result.value);
    const latestSnapshot = snapshots.reduce<HubSnapshot | null>(
      (latest, snapshot) => (!latest || snapshot.revision > latest.revision ? snapshot : latest),
      null,
    );
    if (latestSnapshot) {
      setHubSnapshot((currentSnapshot) => (
        latestSnapshot.revision >= currentSnapshot.revision ? latestSnapshot : currentSnapshot
      ));
    }

    const failures = results.filter((result) => result.status === "rejected");
    if (failures.length > 0) {
      console.error("Не удалось отметить часть результатов прочитанными", failures.map((failure) => failure.reason));
      try {
        const refreshedSnapshot = await getHubSnapshot(hubConnection);
        setHubSnapshot((currentSnapshot) => (
          refreshedSnapshot.revision >= currentSnapshot.revision ? refreshedSnapshot : currentSnapshot
        ));
      } catch (error) {
        console.error("Не удалось сверить снимок после массового чтения", error);
      }
      setHubMessage(
        targets.length === 1
          ? "Не удалось отметить результат"
          : `${label}: отмечено ${targets.length - failures.length} из ${targets.length}`,
      );
      return;
    }
    setHubMessage(
      targets.length === 1
        ? "Результат отмечен прочитанным"
        : `${label}: отмечено ${targets.length}`,
    );
  };

  const acknowledge = async (agent: DemoAgent) => {
    await acknowledgeMany([agent], "Результаты");
  };

  const openAgent = async (agent: DemoAgent) => {
    try {
      if (agent.navigation?.kind === "task") {
        await openCodexThread(agent.navigation.target);
      } else if (agent.provider === "opencode" && agent.navigation?.kind === "provider") {
        await openOpenCodeProject(agent.navigation.target);
      } else {
        return;
      }
    } catch (error) {
      console.error("Не удалось открыть карточку в провайдере", error);
      setHubMessage(agent.provider === "opencode"
        ? "Не удалось открыть проект в OpenCode"
        : "Не удалось открыть задачу в Codex");
    }
  };

  const hubPort = hubConnection ? new URL(hubConnection.endpoint).port : "—";

  return (
    <main className="shell" data-theme={theme} data-text-size={textSize}>
      <header className="titlebar">
        <div
          className="titlebar__drag"
          data-tauri-drag-region
          onMouseDown={beginWindowDrag}
          title="Перетащить PetCrew"
        >
          <div className="brand" data-tauri-drag-region>
            <span className="brand__mark" data-tauri-drag-region>PC</span>
            <h1 data-tauri-drag-region>PetCrew</h1>
          </div>
          <span
            className={`status-dot status-dot--${source === "hub" ? hubStatus : "online"}`}
            aria-label={source === "hub" ? HUB_STATUS_TEXT[hubStatus] : "Демо, только чтение"}
            title={source === "hub" ? HUB_STATUS_TEXT[hubStatus] : "Демо, только чтение"}
            data-tauri-drag-region
          />
        </div>
        <div className="window-controls" aria-label="Положение, размер текста и закрытие окна">
          <button
            className="window-size window-return"
            type="button"
            aria-label="Вернуть PetCrew на маленький экран"
            title="На маленький экран"
            onClick={returnToSmallMonitor}
          >
            <span aria-hidden="true">↗</span>
          </button>
          <button
            className="window-size"
            type="button"
            aria-label="Уменьшить текст"
            title="Уменьшить текст"
            disabled={textSize === TEXT_SIZES[0]}
            onClick={() => changeTextSize(-1)}
          >
            <span aria-hidden="true">A−</span>
          </button>
          <button
            className="window-size"
            type="button"
            aria-label="Увеличить текст"
            title="Увеличить текст"
            disabled={textSize === TEXT_SIZES[TEXT_SIZES.length - 1]}
            onClick={() => changeTextSize(1)}
          >
            <span aria-hidden="true">A+</span>
          </button>
          <button
            className="window-close"
            type="button"
            aria-label="Закрыть PetCrew"
            title="Закрыть"
            onClick={closePetCrew}
          >
            <span aria-hidden="true">×</span>
          </button>
        </div>
      </header>

      <section className="overview">
        <Pet mood={mood} />
        <div className="overview__content">
          <div className="summary">
            <div><strong>{counts.working}</strong><span>работают</span></div>
            <div><strong>{counts.waiting}</strong><span>ждут</span></div>
            <div><strong>{counts.done}</strong><span>готовы</span></div>
            <div title="Заблокированные агенты и непрочитанные ошибки"><strong>{counts.blocked}</strong><span>проблемы</span></div>
          </div>
          <div className="quick-controls" aria-label="Вид PetCrew">
            <div className="segmented" aria-label="Расположение карточек">
              <button
                className={cardLayout === "list" ? "is-active" : ""}
                type="button"
                onClick={() => savePreferences({ ...preferencesRef.current, card_layout: "list" })}
              >
                Список
              </button>
              <button
                className={cardLayout === "tiles" ? "is-active" : ""}
                type="button"
                onClick={() => savePreferences({ ...preferencesRef.current, card_layout: "tiles" })}
              >
                Плитки
              </button>
            </div>
            <button
              className={`icon-toggle${theme === "light" ? " is-active" : ""}`}
              type="button"
              aria-label="Светлая тема"
              aria-pressed={theme === "light"}
              title={theme === "light" ? "Светлая тема включена" : "Включить светлую тему"}
              onClick={() => savePreferences({ ...preferencesRef.current, theme: theme === "light" ? "dark" : "light" })}
            >
              <span aria-hidden="true">☀</span>
            </button>
            <button
              className={`icon-toggle${settingsOpen ? " is-active" : ""}`}
              type="button"
              aria-label="Настройки"
              aria-expanded={settingsOpen}
              title="Настройки"
              onClick={() => setSettingsOpen((open) => !open)}
            >
              <span aria-hidden="true">⚙</span>
            </button>
          </div>

          {settingsOpen ? (
            <div className="settings-panel" aria-label="Настройки PetCrew">
              <div className="source-control">
                <span className="control-label">источник</span>
                <div className="segmented">
                  <button className={source === "hub" ? "is-active" : ""} type="button" disabled={!hubConnection} onClick={() => chooseSource("hub")}>Живые</button>
                  <button className={source === "fixture" ? "is-active" : ""} type="button" onClick={() => chooseSource("fixture")}>Демо</button>
                </div>
              </div>
              <label className="retention-control">
                <span className="control-label">прочитанных результатов</span>
                <select aria-label="Сколько последних прочитанных результатов показывать" value={recentCompletedLimit} onChange={(event) => chooseRecentCompletedLimit(Number(event.target.value))}>
                  {RECENT_COMPLETED_OPTIONS.map((limit) => (
                    <option key={limit} value={limit}>{limit === 0 ? "не показывать" : `последние ${limit}`}</option>
                  ))}
                </select>
              </label>
              {source === "fixture" ? (
                <div className="demo-settings">
                  <div className="segmented">
                    {TEAM_SIZES.map((size) => (
                      <button key={size} className={teamSize === size ? "is-active" : ""} type="button" onClick={() => setTeamSize(size)}>{size}</button>
                    ))}
                  </div>
                  <div className="simulation-actions">
                    <button className="primary" type="button" onClick={() => setRunning((value) => !value)}>{running ? "Пауза" : "Запустить"}</button>
                    <button type="button" onClick={resetFixture}>Сбросить</button>
                  </div>
                </div>
              ) : null}
            </div>
          ) : null}
          {source === "hub" && hubMessage ? <div className="hub-message" role="status">{hubMessage}</div> : null}
        </div>
      </section>

      <section className="team" aria-label="Агенты команды">
        <div className="team__header">
          <div>
            <span className="eyebrow">{source === "hub" ? "живой снимок" : "вся стая"}</span>
            <h2>
              {visibleAgents.length === selectedTeam.length
                ? `${visibleAgents.length} агентов`
                : `${visibleAgents.length} из ${selectedTeam.length}`}
            </h2>
          </div>
          <div className="team__actions" aria-label="Массовое чтение результатов">
            <button
              className="team__action team__action--primary"
              type="button"
              disabled={unreadResults.length === 0}
              title={unreadResults.length > 0 ? `Отметить прочитанными: ${unreadResults.length}` : "Нет непрочитанных результатов"}
              onClick={() => void acknowledgeMany(unreadResults, "Все результаты")}
            >
              Прочитать все
            </button>
            <button
              className="team__action"
              type="button"
              disabled={unreadSubagents.length === 0}
              title={unreadSubagents.length > 0 ? `Отметить прочитанными субагентов: ${unreadSubagents.length}` : "Нет непрочитанных результатов субагентов"}
              onClick={() => void acknowledgeMany(unreadSubagents, "Результаты субагентов")}
            >
              Прочитать субагентов
            </button>
            <span className="density">плотность: {density === "detailed" ? "подробная" : density === "compact" ? "компактная" : "плотная"}</span>
          </div>
        </div>

        {selection.hiddenRecentCount > 0 || overflowCount > 0 ? (
          <div className="retention-notes" role="status">
            {selection.hiddenRecentCount > 0 ? (
              <span>Скрыто старых прочитанных результатов: {selection.hiddenRecentCount}</span>
            ) : null}
            {overflowCount > 0 ? (
              <span className="retention-note--overflow">
                Защищённых карточек сверх обычной ёмкости 100: {overflowCount}
              </span>
            ) : null}
          </div>
        ) : null}

        {source === "hub" && visibleAgents.length === 0 ? (
          <div className="empty-state">
            <strong>Ждёт живые события Codex</strong>
            <span>Новая задача или явный статус появятся здесь автоматически.</span>
          </div>
        ) : (
          <div
            className={`agent-list agent-list--${density} agent-list--${cardLayout}`}
            role="region"
            aria-label="Прокручиваемый список агентов"
            tabIndex={0}
          >
            {visibleAgents.map((agent) => (
              <AgentCard
                key={agent.key ?? agent.agent_id}
                agent={agent}
                density={density}
                onAcknowledge={acknowledge}
                onOpen={openAgent}
                nowMillis={clockNow}
              />
            ))}
          </div>
        )}
      </section>

      <footer className="footer">
        <span>{source === "hub" ? `Ревизия: ${hubSnapshot.revision}` : `Тик симуляции: ${tick}`}</span>
        <span>{source === "hub" ? `127.0.0.1:${hubPort} · hooks + MCP` : "fixture · локально"}</span>
      </footer>
      <button
        className="resize-grip"
        type="button"
        aria-label="Изменить размер окна"
        title="Потяни за угол, чтобы изменить размер"
        onMouseDown={beginWindowResize}
      />
    </main>
  );
}
