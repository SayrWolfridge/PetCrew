import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const css = readFileSync(new URL("./styles.css", import.meta.url), "utf8");
const appSource = readFileSync(new URL("./App.tsx", import.meta.url), "utf8");

function ruleBody(selector: string) {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return css.match(new RegExp(`${escaped}\\s*\\{([^}]*)\\}`))?.[1] ?? "";
}

describe("settings panel layout", () => {
  it("keeps the expanded panel inside the viewport and scrollable", () => {
    const panel = ruleBody(".settings-panel");

    expect(panel).toContain("max-height: min(48vh, 420px)");
    expect(panel).toContain("overflow-y: auto");
    expect(panel).toContain("flex-direction: column");
    expect(panel).toContain("background: var(--subtle-bg)");
    expect(panel).toContain("scrollbar-color: var(--scroll-thumb-strong) var(--scroll-track)");
  });

  it("keeps a visible, usable settings scrollbar", () => {
    expect(ruleBody(".settings-panel::-webkit-scrollbar")).toContain("width: 12px");
    expect(ruleBody(".settings-panel::-webkit-scrollbar-track")).toContain("background: var(--scroll-track)");
    expect(ruleBody(".settings-panel::-webkit-scrollbar-thumb")).toContain("min-height: 40px");
    expect(ruleBody(".settings-panel::-webkit-scrollbar-thumb")).toContain("background: var(--scroll-thumb-strong)");
  });

  it("gives the settings panel its own explicit wheel handler", () => {
    expect(appSource).toContain("ref={settingsPanelRef}");
    expect(appSource).toContain('panel.addEventListener("wheel", handleWheel, { passive: false })');
    expect(appSource).toContain("scrollTargetFromWheel(panel, event.deltaY, event.deltaMode)");
  });

  it("keeps each settings block on one row with compact actions", () => {
    const row = ruleBody(".relay-control, .monitor-reset-control, .codex-restart-control");
    const actions = ruleBody(".codex-restart-control .codex-restart-actions");

    expect(row).toContain("align-items: center");
    expect(row).toContain("justify-content: space-between");
    expect(actions).toContain("display: flex");
    expect(actions).toContain("flex-direction: row");
    expect(actions).toContain("justify-content: flex-end");
  });

  it("lets the blue inspection result grow instead of clipping it", () => {
    const message = ruleBody(".hub-message");

    expect(message).toContain("overflow-wrap: anywhere");
    expect(message).toContain("white-space: normal");
    expect(message).not.toContain("text-overflow: ellipsis");
  });

  it("shows a strong scrollbar on the live snapshot", () => {
    const list = ruleBody(".agent-list");
    const scrollbar = ruleBody(".agent-list::-webkit-scrollbar");
    const thumb = ruleBody(".agent-list::-webkit-scrollbar-thumb");

    expect(list).toContain("overflow-y: scroll");
    expect(list).toContain("scrollbar-color: var(--scroll-thumb-strong) var(--scroll-track)");
    expect(scrollbar).toContain("width: 12px");
    expect(thumb).toContain("min-height: 40px");
    expect(thumb).toContain("background: var(--scroll-thumb-strong)");
  });
});
