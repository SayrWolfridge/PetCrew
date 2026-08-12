import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const componentSource = readFileSync(new URL("./components.tsx", import.meta.url), "utf8");
const css = readFileSync(new URL("./styles.css", import.meta.url), "utf8");

describe("compact result acknowledgement", () => {
  it("places acknowledgement inside the progress row", () => {
    expect(componentSource).toContain('className="progress__ack"');
    expect(componentSource).toContain(
      'onAcknowledge={canAcknowledge ? () => onAcknowledge(agent) : undefined}',
    );
    expect(componentSource).not.toContain('className="agent__ack"');
  });

  it("uses the former progress-track column without a separate card row", () => {
    expect(css).toContain(".progress__ack {");
    expect(css).toContain("border-bottom: 3px solid var(--green)");
    expect(css).not.toContain(".agent-list--tiles .agent__ack");
  });
});
