import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const appSource = readFileSync(new URL("./App.tsx", import.meta.url), "utf8");
const hubSource = readFileSync(new URL("./hub.ts", import.meta.url), "utf8");

describe("monitor reset control", () => {
  it("uses the authenticated Core endpoint and replaces the complete snapshot", () => {
    expect(hubSource).toContain("/v1/monitor/reset");
    expect(hubSource).toContain("headers: authorizedHeaders(connection)");
    expect(appSource).toContain("const snapshot = await resetHubMonitor(hubConnection)");
    expect(appSource).toContain("setHubSnapshot(snapshot)");
  });

  it("states the local-only boundary in the visible settings control", () => {
    expect(appSource).toContain("Очистить всё");
    expect(appSource).toContain("Задачи и Relay останутся");
    expect(appSource).toContain("Убрать все карточки, включая ошибки");
  });
});
