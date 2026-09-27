import type { CodexPressureResult } from "./hub";

export const CODEX_RESTART_WARNING =
  "Аварийно восстановить Codex? PetCrew сначала сохранит снимок процессов и попробует завершить только доказанно зависший дочерний процесс. Если такой процесс не найден, он принудительно завершит весь Codex и откроет снова. Все текущие ходы могут прерваться; незавершённую работу и транскрипты может потребоваться восстановить.";

export async function requestCodexRestart(
  confirm: (message: string) => boolean,
  restart: () => Promise<unknown>,
  onStart: () => void,
): Promise<"cancelled" | "restarted"> {
  if (!confirm(CODEX_RESTART_WARNING)) return "cancelled";
  onStart();
  await restart();
  return "restarted";
}

export interface CodexPressureGuidance {
  message: string;
  suspectedThreadId: string | null;
}

export function codexPressureGuidance(result: CodexPressureResult): CodexPressureGuidance {
  const signalLabels: Record<string, string> = {
    request_pressure: "частые запросы",
    appserver_timeout: "таймаут App Server",
    model_list_child_exit_timeout: "таймаут списка моделей",
    queue_admission_rejected: "отказ очереди",
    turn_input_integrity_error: "повреждённый ход задачи",
  };
  const signals = result.recent_signals.length > 0
    ? ` За последние 10 минут: ${result.recent_signals.map((signal) => signalLabels[signal] ?? signal).join(", ")}.`
    : " За последние 10 минут тревожных сигналов нет.";
  const summary = `App Server: ${result.direct_child_count} прямых дочерних процессов, `
    + `${result.runtime_cohort_count} комплектов инструментов, `
    + `${result.descendant_count} потомков всего. `
    + `Tool-серверов: ${result.tool_app_server_count}, `
    + `доказанных сирот: ${result.orphan_tool_app_server_count}.${signals}`;
  const suspectedThreadId = result.suspected_thread_ids[0] ?? null;
  if (suspectedThreadId) {
    return {
      suspectedThreadId,
      message: `${summary} Обнаружена повторяющаяся ошибка задачи ${suspectedThreadId}. `
        + "Откройте её и остановите текущий ход. Если Codex не отвечает, используйте аварийное восстановление.",
    };
  }
  if (result.orphan_tool_app_server_count > 0) {
    return {
      suspectedThreadId: null,
      message: `${summary} Найдены доказанные сироты; можно использовать «Убрать сироты».`,
    };
  }
  const hardFailure = result.recent_signals.some((signal) => [
    "appserver_timeout",
    "model_list_child_exit_timeout",
    "queue_admission_rejected",
  ].includes(signal));
  if (hardFailure) {
    return {
      suspectedThreadId: null,
      message: `${summary} Сирот нет: сбой находится внутри живого App Server, поэтому «Убрать сироты» не поможет. `
        + "Если Codex не отвечает, используйте «Аварийно восстановить Codex».",
    };
  }
  if (result.recent_signals.includes("request_pressure")) {
    return {
      suspectedThreadId: null,
      message: `${summary} Сирот нет. Если Codex отвечает, ничего завершать не нужно; `
        + "если перестал отвечать — используйте аварийное восстановление.",
    };
  }
  return {
    suspectedThreadId: null,
    message: `${summary} Признаков зависания и доказанных сирот сейчас нет.`,
  };
}
