import { describe, expect, it } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { CodexRestartControl } from "./codex-restart-control";
import { codexPressureGuidance, requestCodexRestart } from "./codex-restart";
import type { CodexPressureResult } from "./hub";

function pressure(overrides: Partial<CodexPressureResult> = {}): CodexPressureResult {
  return {
    desktop_pid: 1,
    app_server_pid: 2,
    descendant_count: 3,
    direct_child_count: 2,
    runtime_cohort_count: 1,
    direct_child_names: [],
    recent_signals: [],
    suspected_thread_ids: [],
    tool_app_server_count: 0,
    orphan_tool_app_server_count: 0,
    orphan_tool_app_server_pids: [],
    ...overrides,
  };
}

describe("manual Codex Desktop restart", () => {
  it("renders the whole-app action with a disabled busy state", () => {
    const render = (busy: boolean) => renderToStaticMarkup(createElement(CodexRestartControl, {
      busy,
      inspecting: false,
      cleaning: false,
      suspectedThreadId: null,
      onInspect: () => undefined,
      onCleanup: () => undefined,
      onOpenSuspected: () => undefined,
      onRestart: () => undefined,
    }));
    expect(render(false)).toContain("Проверить нагрузку");
    expect(render(false)).toContain("Убрать сироты");
    expect(render(false)).toContain("Аварийно восстановить Codex");
    expect(render(false)).toContain("Сохранит снимок и восстановит Codex");
    expect(render(false)).not.toContain("Перезапустить App Server");
    expect(render(true)).toContain("disabled");
    expect(render(true)).toContain("Восстанавливаю…");
  });

  it("shows a distinct read-only inspection busy state", () => {
    const html = renderToStaticMarkup(createElement(CodexRestartControl, {
      busy: false,
      inspecting: true,
      cleaning: false,
      suspectedThreadId: null,
      onInspect: () => undefined,
      onCleanup: () => undefined,
      onOpenSuspected: () => undefined,
      onRestart: () => undefined,
    }));
    expect(html).toContain("Проверяю…");
    expect(html.match(/disabled/g)?.length).toBe(3);
  });

  it("offers navigation when inspection identifies a looping task", () => {
    const threadId = "01a0a486-d5fd-7423-bc01-825b6b77072f";
    const html = renderToStaticMarkup(createElement(CodexRestartControl, {
      busy: false,
      inspecting: false,
      cleaning: false,
      suspectedThreadId: threadId,
      onInspect: () => undefined,
      onCleanup: () => undefined,
      onOpenSuspected: () => undefined,
      onRestart: () => undefined,
    }));
    expect(html).toContain("Открыть зацикленную задачу");
    expect(html).toContain(threadId);
  });

  it("explains why orphan cleanup cannot repair a live App Server failure", () => {
    const guidance = codexPressureGuidance(pressure({ recent_signals: ["appserver_timeout"] }));
    expect(guidance.suspectedThreadId).toBeNull();
    expect(guidance.message).toContain("«Убрать сироты» не поможет");
    expect(guidance.message).toContain("Аварийно восстановить Codex");
  });

  it("prioritizes an evidenced looping task over broader pressure signals", () => {
    const threadId = "01a0a486-d5fd-7423-bc01-825b6b77072f";
    const guidance = codexPressureGuidance(pressure({
      recent_signals: ["turn_input_integrity_error", "request_pressure"],
      suspected_thread_ids: [threadId],
    }));
    expect(guidance.suspectedThreadId).toBe(threadId);
    expect(guidance.message).toContain(`ошибка задачи ${threadId}`);
    expect(guidance.message).toContain("остановите текущий ход");
  });

  it("directs proven orphans to the guarded cleanup", () => {
    const guidance = codexPressureGuidance(pressure({ orphan_tool_app_server_count: 2 }));
    expect(guidance.message).toContain("можно использовать «Убрать сироты»");
  });

  it("never invokes native restart when the warning is declined", async () => {
    const calls: string[] = [];
    const outcome = await requestCodexRestart(
      (message) => { calls.push(message); return false; },
      async () => { calls.push("restart"); },
      () => { calls.push("start"); },
    );
    expect(outcome).toBe("cancelled");
    expect(calls).toHaveLength(1);
    expect(calls[0]).toContain("весь Codex");
    expect(calls[0]).toContain("принудительно завершит");
    expect(calls[0]).toContain("Все текущие ходы могут прерваться");
  });

  it("invokes once only after confirmation", async () => {
    const calls: string[] = [];
    const outcome = await requestCodexRestart(
      () => { calls.push("confirm"); return true; },
      async () => { calls.push("restart"); },
      () => { calls.push("start"); },
    );
    expect(outcome).toBe("restarted");
    expect(calls).toEqual(["confirm", "start", "restart"]);
  });
});
