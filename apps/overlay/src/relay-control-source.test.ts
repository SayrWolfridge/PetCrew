import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";

const appSource = readFileSync(new URL("./App.tsx", import.meta.url), "utf8");
const rustSource = readFileSync(
  new URL("../src-tauri/src/relay_control.rs", import.meta.url),
  "utf8",
);

describe("Relay recovery control", () => {
  it("requires an explicit user confirmation and exposes no timer loop", () => {
    expect(appSource).toContain("Восстановить Relay");
    expect(appSource).toContain("window.confirm");
    expect(appSource).not.toMatch(/setInterval\([^)]*Relay/i);
  });

  it("uses only the canonical scheduled task and fails closed on unexpected owners", () => {
    expect(rustSource).toContain('const TASK_NAME: &str = "OpenCode Bridge Server"');
    expect(rustSource).toContain("занят неожиданным процессом; восстановление отменено");
    expect(rustSource).not.toContain("opencode serve");
  });
});
