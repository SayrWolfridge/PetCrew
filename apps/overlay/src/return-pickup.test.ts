import { describe, expect, it } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { AgentCard } from "./components";
import { performPickup, pickupPrompt } from "./return-pickup";
import type { DemoAgent } from "./types";

const agent: DemoAgent = {
  agent_id: "relay:test",
  provider: "codex",
  project: "Relay Return",
  task: "Результат OpenCode сохранён",
  phase: "completed",
  progress: { kind: "indeterminate", source: "unavailable" },
  current_action: "Результат OpenCode сохранён",
  navigation: {
    kind: "task",
    label: "Открыть в Codex",
    target: "019f9a58-1f22-7f63-bd1c-7c480dd3ea1d",
  },
  return_receipt: {
    session_id: "ses_exact_test",
    completion_id: `completion:${"a".repeat(64)}`,
    phase: "completed",
    workspace: "C:\\Work\\PetCrew",
  },
};

describe("exact OpenCode pickup", () => {
  it("creates a bounded prompt for the exact receipt", () => {
    const prompt = pickupPrompt(agent);
    expect(prompt).toContain("ses_exact_test");
    expect(prompt).toContain(agent.return_receipt?.completion_id);
    expect(prompt).toContain('"C:\\\\Work\\\\PetCrew"');
    expect(prompt).toContain("Не переотправляй задачу");
  });

  it("does not offer pickup on waiting, failed or unrelated cards", () => {
    expect(pickupPrompt({ ...agent, phase: "queued" })).toBeNull();
    expect(pickupPrompt({ ...agent, phase: "failed" })).toBeNull();
    expect(pickupPrompt({ ...agent, project: "Another" })).toBeNull();
    expect(pickupPrompt({ ...agent, return_receipt: null })).toBeNull();
  });

  it("renders a dedicated action only for an eligible Relay card", () => {
    const render = (candidate: DemoAgent) => renderToStaticMarkup(createElement(AgentCard, {
      agent: candidate,
      density: "compact",
      onAcknowledge: () => undefined,
      onOpen: () => undefined,
      onPickup: () => undefined,
      nowMillis: Date.now(),
    }));
    expect(render(agent)).toContain("Забрать результат");
    expect(render({ ...agent, phase: "queued" })).not.toContain("Забрать результат");
  });

  it("copies the exact request before opening only its source task", async () => {
    const calls: string[] = [];
    const outcome = await performPickup(
      agent,
      async (prompt) => { calls.push(`copy:${prompt}`); },
      async (target) => { calls.push(`open:${target}`); },
    );
    expect(outcome).toBe("opened");
    expect(calls[0]).toContain(agent.return_receipt?.completion_id);
    expect(calls[1]).toBe(`open:${agent.navigation?.target}`);
  });

  it("never opens the source task when copying failed", async () => {
    let opened = false;
    const outcome = await performPickup(
      agent,
      async () => { throw new Error("clipboard unavailable"); },
      async () => { opened = true; },
    );
    expect(outcome).toBe("copy_failed");
    expect(opened).toBe(false);
  });
});
