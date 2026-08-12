import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const css = readFileSync(new URL("./styles.css", import.meta.url), "utf8");

function ruleBody(selector: string) {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return css.match(new RegExp(`${escaped}\\s*\\{([^}]*)\\}`))?.[1] ?? "";
}

describe("continuous motion budget", () => {
  it("keeps the glow and indeterminate progress static", () => {
    const glow = ruleBody(".pet__glow");
    const indeterminate = ruleBody(".progress__fill--indeterminate");

    expect(glow).toContain("radial-gradient");
    expect(glow).not.toContain("filter:");
    expect(glow).not.toContain("animation:");
    expect(indeterminate).not.toContain("animation:");
    expect(css).not.toContain("@keyframes breathe");
    expect(css).not.toContain("@keyframes indeterminate");
  });

  it("retains only small semantic motion and reduced-motion support", () => {
    expect(css).toContain("@keyframes pet-float");
    expect(css).toContain("@keyframes blink");
    expect(css).toContain("@media (prefers-reduced-motion: reduce)");
    expect(ruleBody(".pet")).toContain("contain: paint");
  });

  it("reserves a separate status row below the pet figure", () => {
    expect(ruleBody(".pet")).toContain("grid-template-rows");
    expect(ruleBody(".pet")).toContain("border-radius: 12px");
    expect(ruleBody(".pet__stage")).toContain("min-height: 58px");
    expect(ruleBody(".pet__status")).not.toContain("position: absolute");
    expect(ruleBody(".pet__status")).toContain("min-height: 2.3em");
    expect(ruleBody(".overview__content")).toContain("grid-template-rows");
  });

  it("keeps every mood glow soft instead of replacing it with a solid disc", () => {
    expect(ruleBody(".pet__glow")).toContain("radial-gradient");
    expect(ruleBody(".pet--attention")).toContain("--pet-glow-core");
    expect(ruleBody(".pet--blocked")).toContain("--pet-glow-core");
    expect(ruleBody(".pet--success")).toContain("--pet-glow-core");
    expect(ruleBody(".pet--success .pet__glow")).toBe("");
  });
});
