import { describe, expect, it } from "vitest";
import {
  preferredSecondaryPosition,
  preferredSmallMonitorPlacement,
  restorableWindowRect,
} from "./window-placement";

const primary = {
  position: { x: 0, y: 0 },
  size: { width: 1920, height: 1040 },
  scaleFactor: 1,
};

describe("secondary monitor placement", () => {
  it("keeps normal placement when only one monitor exists", () => {
    expect(
      preferredSecondaryPosition([primary], primary, { width: 520, height: 760 }),
    ).toBeNull();
  });

  it("uses the top-right work area of a non-primary monitor", () => {
    const secondary = {
      position: { x: 1920, y: 0 },
      size: { width: 2560, height: 1400 },
      scaleFactor: 1.5,
    };

    expect(
      preferredSecondaryPosition(
        [primary, secondary],
        primary,
        { width: 520, height: 760 },
      ),
    ).toEqual({ x: 3936, y: 24 });
  });

  it("supports a secondary monitor positioned left of the primary", () => {
    const secondary = {
      position: { x: -1600, y: -120 },
      size: { width: 1600, height: 900 },
      scaleFactor: 1,
    };

    expect(
      preferredSecondaryPosition(
        [secondary, primary],
        primary,
        { width: 500, height: 700 },
      ),
    ).toEqual({ x: -516, y: -104 });
  });
});

describe("saved window placement", () => {
  it("restores a rectangle that remains visibly on a monitor", () => {
    const saved = { x: 1400, y: 100, width: 520, height: 760, monitor: "DISPLAY1" };
    expect(restorableWindowRect(saved, [primary])).toEqual(saved);
  });

  it("rejects a rectangle whose monitor is no longer reachable", () => {
    expect(
      restorableWindowRect(
        { x: 3000, y: 100, width: 520, height: 760, monitor: "DISPLAY2" },
        [primary],
      ),
    ).toBeNull();
  });

  it("rejects implausibly small saved geometry", () => {
    expect(
      restorableWindowRect({ x: 20, y: 20, width: 100, height: 100 }, [primary]),
    ).toBeNull();
  });
});

describe("small-monitor return placement", () => {
  it("uses the top-right of the monitor with the smallest logical work area", () => {
    const large = {
      position: { x: 1280, y: 0 },
      size: { width: 2560, height: 1440 },
      scaleFactor: 1,
      name: "LARGE",
    };
    const small = {
      position: { x: 0, y: 0 },
      size: { width: 1280, height: 984 },
      scaleFactor: 1.25,
      name: "SMALL",
    };

    expect(
      preferredSmallMonitorPlacement([large, small], { width: 620, height: 760 }),
    ).toEqual({ x: 640, y: 20, monitor: "SMALL" });
  });

  it("compares logical area so a high-DPI monitor is not mistaken for the larger screen", () => {
    const highDpiSmall = {
      position: { x: -3000, y: 0 },
      size: { width: 3000, height: 1800 },
      scaleFactor: 2,
      name: "HIGH_DPI_SMALL",
    };
    const regularLarge = {
      position: { x: 0, y: 0 },
      size: { width: 1920, height: 1080 },
      scaleFactor: 1,
      name: "REGULAR_LARGE",
    };

    expect(
      preferredSmallMonitorPlacement(
        [regularLarge, highDpiSmall],
        { width: 620, height: 760 },
      ),
    ).toEqual({ x: -652, y: 32, monitor: "HIGH_DPI_SMALL" });
  });

  it("returns null when monitor discovery has no result", () => {
    expect(preferredSmallMonitorPlacement([], { width: 620, height: 760 })).toBeNull();
  });
});
