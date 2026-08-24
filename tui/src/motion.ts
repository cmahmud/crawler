export const ACTIVE_FRAMES = ["◐", "◓", "◑", "◒"] as const;
export const FLOW_FRAMES = [
  "◆──◇──◇",
  "◇──◆──◇",
  "◇──◇──◆",
  "◇──◆──◇",
] as const;

export function envFlag(value: string | undefined): boolean {
  if (value === undefined) return false;
  return ["1", "true", "yes", "on"].includes(value.trim().toLowerCase());
}

export function reducedMotionFromEnv(env: NodeJS.ProcessEnv = process.env): boolean {
  return envFlag(env.SYNCRAWLER_TUI_REDUCED_MOTION) || env.TERM === "dumb";
}

export function activeFrame(tick: number, reducedMotion: boolean): string {
  if (reducedMotion) return "●";
  return ACTIVE_FRAMES[Math.abs(tick) % ACTIVE_FRAMES.length];
}

export function flowFrame(tick: number, reducedMotion: boolean): string {
  if (reducedMotion) return "◆──◇──◇";
  return FLOW_FRAMES[Math.abs(tick) % FLOW_FRAMES.length];
}

export function revealText(text: string, tick: number, reducedMotion: boolean): string {
  if (reducedMotion) return text;
  const visible = Math.min(text.length, Math.max(1, tick * 3));
  return text.slice(0, visible);
}
