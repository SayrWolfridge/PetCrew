import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const mainSource = readFileSync(
  new URL("../src-tauri/src/main.rs", import.meta.url),
  "utf8",
);

describe("Windows release executable", () => {
  it("uses the GUI subsystem without hiding debug consoles", () => {
    expect(mainSource).toContain(
      '#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]',
    );
  });
});
