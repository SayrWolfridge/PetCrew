import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";

describe("summary filters", () => {
  it("renders four toggle buttons and filters the card collection", () => {
    const appSource = readFileSync(new URL("./App.tsx", import.meta.url), "utf8");

    expect(appSource.match(/aria-pressed=\{teamFilter ===/g)).toHaveLength(4);
    expect(appSource).toContain("toggleTeamFilter(\"working\")");
    expect(appSource).toContain("toggleTeamFilter(\"waiting\")");
    expect(appSource).toContain("toggleTeamFilter(\"done\")");
    expect(appSource).toContain("toggleTeamFilter(\"blocked\")");
    expect(appSource).toContain("filteredAgents.map");
    expect(appSource).toContain("Нажмите выбранный счётчик ещё раз");
  });
});
