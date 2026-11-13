import type { MetricArtifact } from './MetricAnalysis'
import type { VerifiedAnalysis } from './VerifiedAnalysis'

// Browser-local records only; no server-side persistence or secret storage.
export type EventRecord = { event: string; data: Record<string, unknown>; id: number; at: number }
export type RunSnapshot = {
  id: string
  createdAt: string
  question: string
  answer: string
  verified: VerifiedAnalysis | null
  metricArtifacts: MetricArtifact[]
  events: EventRecord[]
}

const KEY = 'enterprise-insight-agent.history.v03'
const MAX_RUNS = 8
const MAX_ITEM_CHARS = 160_000
const MAX_STORE_CHARS = 800_000

function validSnapshot(value: unknown): value is RunSnapshot {
  if (!value || typeof value !== 'object') return false
  const v = value as Record<string, unknown>
  return typeof v.id === 'string' && typeof v.createdAt === 'string' &&
    typeof v.question === 'string' && typeof v.answer === 'string' &&
    Array.isArray(v.events) && Array.isArray(v.metricArtifacts)
}

export function loadRunHistory(): RunSnapshot[] {
  try {
    const raw = window.localStorage.getItem(KEY)
    if (!raw || raw.length > MAX_STORE_CHARS) return []
    const parsed: unknown = JSON.parse(raw)
    return Array.isArray(parsed) ? parsed.filter(validSnapshot).slice(0, MAX_RUNS) : []
  } catch {
    return []
  }
}

/** Retains up to eight successful runs, clipping older entries if storage quota is tight. */
export function saveRunHistory(entries: RunSnapshot[]): boolean {
  try {
    const safe = entries.filter(validSnapshot).filter(x => JSON.stringify(x).length <= MAX_ITEM_CHARS).slice(0, MAX_RUNS)
    while (safe.length && JSON.stringify(safe).length > MAX_STORE_CHARS) safe.pop()
    window.localStorage.setItem(KEY, JSON.stringify(safe))
    return true
  } catch {
    return false
  }
}

export function clearRunHistory(): void {
  try { window.localStorage.removeItem(KEY) } catch { /* private browsing / disabled storage */ }
}

export function summarizeRun(events: EventRecord[]) {
  return {
    toolCalls: events.filter(e => e.event === 'tool_call').length,
    durationMs: events.length > 1 ? Math.max(0, events.at(-1)!.at - events[0].at) : 0,
    verified: events.some(e => e.event === 'verified_analysis' || e.event === 'metric_artifact'),
  }
}
