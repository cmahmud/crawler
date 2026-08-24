import { describe, expect, test } from "bun:test";
import {
  ACTIVE_FRAMES,
  activeFrame,
  envFlag,
  flowFrame,
  reducedMotionFromEnv,
  revealText,
} from "./motion";

describe("TUI motion primitives", () => {
  test("parses explicit boolean environment flags", () => {
    expect(envFlag("true")).toBeTrue();
    expect(envFlag("YES")).toBeTrue();
    expect(envFlag("0")).toBeFalse();
    expect(envFlag(undefined)).toBeFalse();
  });

  test("honors reduced-motion environment settings", () => {
    expect(reducedMotionFromEnv({ SYNCRAWLER_TUI_REDUCED_MOTION: "true" })).toBeTrue();
    expect(reducedMotionFromEnv({ TERM: "dumb" })).toBeTrue();
    expect(reducedMotionFromEnv({ TERM: "xterm-256color" })).toBeFalse();
  });

  test("cycles active crawl frames when motion is enabled", () => {
    expect(activeFrame(0, false)).toBe(ACTIVE_FRAMES[0]);
    expect(activeFrame(ACTIVE_FRAMES.length, false)).toBe(ACTIVE_FRAMES[0]);
    expect(activeFrame(2, true)).toBe("●");
  });

  test("keeps flow and title static when reduced motion is enabled", () => {
    expect(flowFrame(3, true)).toBe("◆──◇──◇");
    expect(revealText("SyndCrawler", 1, true)).toBe("SyndCrawler");
  });

  test("reveals the title progressively", () => {
    expect(revealText("SyndCrawler", 1, false)).toBe("Syn");
    expect(revealText("SyndCrawler", 50, false)).toBe("SyndCrawler");
  });
});
