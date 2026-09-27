import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const appSource = readFileSync(new URL("./App.tsx", import.meta.url), "utf8");
const componentsSource = readFileSync(new URL("./components.tsx", import.meta.url), "utf8");

describe("project grouped list wiring", () => {
  it("uses project groups only for the list layout", () => {
    expect(appSource).toContain('cardLayout === "list"');
    expect(appSource).toContain("projectGroups.map((group) => (");
    expect(appSource).toContain("<ProjectGroup");
    expect(appSource).toContain("filteredAgents.map((agent) => (");
    expect(appSource).toContain("<AgentCard");
  });

  it("keeps the disclosure click separate from the card navigation click", () => {
    const disclosureHandler = componentsSource.match(
      /className="agent__disclosure"[\s\S]*?onClick=\{\(event\) => \{([\s\S]*?)\}\}/,
    );

    expect(disclosureHandler?.[1]).toContain("event.stopPropagation()");
    expect(disclosureHandler?.[1]).toContain("onToggleExpanded?.()");
    expect(componentsSource).toContain("onClick={canPickup ? undefined : activate}");
    expect(componentsSource).toContain("if (canOpen && !canPickup) onOpen(agent)");
  });

  it("binds a non-passive wheel handler to the actual agent list", () => {
    expect(appSource).toContain('list.addEventListener("wheel", handleWheel, { passive: false })');
    expect(appSource).toContain("ref={agentListRef}");
    expect(appSource).toContain("scrollTargetFromWheel(list, event.deltaY, event.deltaMode)");
  });
});
