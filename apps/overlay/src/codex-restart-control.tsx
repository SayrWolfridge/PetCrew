export function CodexRestartControl({
  busy,
  inspecting,
  cleaning,
  suspectedThreadId,
  onInspect,
  onCleanup,
  onOpenSuspected,
  onRestart,
}: {
  busy: boolean;
  inspecting: boolean;
  cleaning: boolean;
  suspectedThreadId: string | null;
  onInspect: () => void;
  onCleanup: () => void;
  onOpenSuspected: () => void;
  onRestart: () => void;
}) {
  return (
    <div className="codex-restart-control">
      <div>
        <span className="control-label">Codex Desktop</span>
        <span className="monitor-reset-hint">Сохранит снимок и восстановит Codex</span>
      </div>
      <div className="codex-restart-actions">
        <button type="button" disabled={busy || inspecting || cleaning} onClick={onInspect}>
          {inspecting ? "Проверяю…" : "Проверить нагрузку"}
        </button>
        <button type="button" disabled={busy || inspecting || cleaning} onClick={onCleanup}>
          {cleaning ? "Очищаю…" : "Убрать сироты"}
        </button>
        {suspectedThreadId ? (
          <button
            type="button"
            disabled={busy || inspecting || cleaning}
            title={`Открыть задачу ${suspectedThreadId}`}
            onClick={onOpenSuspected}
          >
            Открыть зацикленную задачу
          </button>
        ) : null}
        <button type="button" disabled={busy || inspecting || cleaning} onClick={onRestart}>
          {busy ? "Восстанавливаю…" : "Аварийно восстановить Codex"}
        </button>
      </div>
    </div>
  );
}
