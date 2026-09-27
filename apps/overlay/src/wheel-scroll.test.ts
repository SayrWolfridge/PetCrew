import { describe, expect, it } from "vitest";
import { scrollTargetFromWheel, wheelDeltaInPixels } from "./wheel-scroll";

describe("wheel scrolling", () => {
  it("moves a bounded list by pixel wheel deltas", () => {
    const target = { clientHeight: 423, scrollHeight: 782, scrollTop: 0 };

    expect(scrollTargetFromWheel(target, 120)).toBe(true);
    expect(target.scrollTop).toBe(120);
  });

  it("clamps the list at both boundaries", () => {
    const target = { clientHeight: 423, scrollHeight: 782, scrollTop: 340 };

    expect(scrollTargetFromWheel(target, 120)).toBe(true);
    expect(target.scrollTop).toBe(359);
    expect(scrollTargetFromWheel(target, 120)).toBe(false);
    expect(scrollTargetFromWheel(target, -500)).toBe(true);
    expect(target.scrollTop).toBe(0);
  });

  it("normalizes line and page wheel modes", () => {
    expect(wheelDeltaInPixels(3, 1, 420)).toBe(48);
    expect(wheelDeltaInPixels(1, 2, 420)).toBe(420);
    expect(wheelDeltaInPixels(Number.NaN, 0, 420)).toBe(0);
  });
});
