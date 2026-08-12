import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const appSource = readFileSync(new URL("./App.tsx", import.meta.url), "utf8");

describe("window geometry persistence", () => {
  it("saves the same inner-size metric that startup restores", () => {
    const saveBlock = appSource.match(
      /const scheduleSave = \(\) => \{([\s\S]*?)\n      \};\n      \[unlistenMoved/,
    )?.[1];

    expect(saveBlock).toBeDefined();
    expect(saveBlock).toContain("appWindow.innerSize()");
    expect(saveBlock).not.toContain("appWindow.outerSize()");
  });

  it("does not persist a transient drag position outside every monitor", () => {
    const saveBlock = appSource.match(
      /const scheduleSave = \(\) => \{([\s\S]*?)\n      \};\n      \[unlistenMoved/,
    )?.[1];

    expect(saveBlock).toBeDefined();
    expect(saveBlock).toContain("restorableWindowRect(nextPlacement, screenMonitors)");
    expect(saveBlock).toContain("if (!restorableWindowRect(nextPlacement, screenMonitors)) return;");
  });

  it("offers an explicit small-monitor return action that persists immediately", () => {
    const returnBlock = appSource.match(
      /const returnToSmallMonitor = \(\) => \{([\s\S]*?)\n  \};\n\n  const resetFixture/,
    )?.[1];

    expect(returnBlock).toBeDefined();
    expect(returnBlock).toContain("preferredSmallMonitorPlacement(screenMonitors, outerSize)");
    expect(returnBlock).toContain("appWindow.setPosition");
    expect(returnBlock).toContain("updateWindowPlacement");
    expect(appSource).toContain('aria-label="Вернуть PetCrew на маленький экран"');
  });
});
