import type { components } from '@/contracts/generated/api'
import { requestJson } from '@/shared/api/client'

/**
 * Typed access to ``GET /api/traces``.
 *
 * The endpoint derives its numbers from events the runtime already persisted.
 * It is read-only and never re-runs a turn, so this page is safe to leave open
 * on a live conversation.
 */
export type TurnTrace = components['schemas']['TurnTrace']
export type TraceBreakdown = components['schemas']['TraceBreakdown']
export type TraceRound = components['schemas']['TraceRound']
export type TraceTool = components['schemas']['TraceTool']
export type TraceStage = components['schemas']['TraceStage']
export type TraceFinding = components['schemas']['TraceFinding']

export async function fetchRecentTurnTraces(
  limit: number,
  sessionId: string,
  signal?: AbortSignal
): Promise<TurnTrace[]> {
  const query = new URLSearchParams({ limit: String(limit) })
  if (sessionId) query.set('session_id', sessionId)
  return requestJson<TurnTrace[]>(`/api/traces/turns?${query}`, {
    signal,
    scope: 'settings',
  })
}

/** Phases in the order the page renders them, with their breakdown field. */
export const TRACE_PHASES = [
  { key: 'queue_ms', label: 'Queue' },
  { key: 'context_ms', label: 'Context' },
  { key: 'model_ms', label: 'Model' },
  { key: 'tool_ms', label: 'Tool' },
  { key: 'wait_ms', label: 'Wait' },
  { key: 'other_exec_ms', label: 'Other' },
  { key: 'persist_ms', label: 'Persist' },
  { key: 'unattributed_ms', label: 'Unattributed' },
] as const satisfies readonly { key: keyof TraceBreakdown; label: string }[]

/** A measured phase value, or null when this turn predates that measurement. */
export function phaseMs(trace: TurnTrace, key: keyof TraceBreakdown): number | null {
  const value = trace.breakdown?.[key]
  return typeof value === 'number' ? value : null
}

export function formatMs(value: number | null | undefined): string {
  if (value == null) return '—'
  if (value < 1000) return `${Math.round(value)} ms`
  if (value < 60_000) return `${(value / 1000).toFixed(2)} s`
  const minutes = Math.floor(value / 60_000)
  const seconds = ((value % 60_000) / 1000).toFixed(1)
  return `${minutes}m ${seconds}s`
}

/**
 * Percentile over the loaded sample.
 *
 * The sample is explicitly the newest N turns, not the whole history, so this
 * is labelled as a sample statistic on the page rather than a global figure.
 */
export function percentile(values: readonly number[], ratio: number): number | null {
  if (!values.length) return null
  const sorted = [...values].sort((a, b) => a - b)
  const index = Math.min(sorted.length - 1, Math.max(0, Math.ceil(ratio * sorted.length) - 1))
  return sorted[index]
}
