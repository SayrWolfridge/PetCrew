import type { DemoAgent } from "./types";

export function pickupPrompt(agent: DemoAgent): string | null {
  const receipt = agent.return_receipt;
  if (
    agent.project !== "Relay Return"
    || agent.phase !== "completed"
    || agent.navigation?.kind !== "task"
    || !receipt
    || !/^completion:[0-9a-f]{64}$/.test(receipt.completion_id)
    || !receipt.session_id.startsWith("ses_")
    || receipt.phase !== "completed"
    || !receipt.workspace
  ) return null;

  return `Забери точный результат OpenCode через opencode_read_session: workspace ${JSON.stringify(receipt.workspace)}, session_id ${receipt.session_id}, completion_id ${receipt.completion_id}, phase ${receipt.phase}. Не переотправляй задачу.`;
}

export async function performPickup(
  agent: DemoAgent,
  copy: (text: string) => Promise<void>,
  open: (threadId: string) => Promise<void>,
): Promise<"opened" | "copy_failed" | "open_failed" | "ineligible"> {
  const prompt = pickupPrompt(agent);
  const target = agent.navigation?.target;
  if (!prompt || !target) return "ineligible";
  try {
    await copy(prompt);
  } catch {
    return "copy_failed";
  }
  try {
    await open(target);
    return "opened";
  } catch {
    return "open_failed";
  }
}
