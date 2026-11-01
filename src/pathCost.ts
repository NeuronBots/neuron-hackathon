import type { PhaseCost } from "./types"

// "6 min · 41k tokens": the time and the bot tokens a stage took, from when it began to when the next one did
export function costText(c: PhaseCost): string {
  const time = c.minutes < 1 ? "<1 min" : c.minutes < 60 ? `${Math.round(c.minutes)} min` : `${(c.minutes / 60).toFixed(1)} h`
  const tokens = c.tokens >= 1000 ? `${Math.round(c.tokens / 1000)}k` : String(c.tokens)
  return `${c.running ? "running · " : ""}${time} · ${tokens} tokens`
}

export const COST_NOTE = "Time and bot tokens from when this stage began until the next one began. Muse's work in its sandbox is included, and counts toward your plan."
