'use client'

import { useEffect, useMemo, useState } from 'react'
import { ChevronRight, RefreshCw } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { SettingsPageHeader } from '@/components/settings/shared'
import {
  TRACE_PHASES,
  fetchRecentTurnTraces,
  formatMs,
  percentile,
  phaseMs,
  type TurnTrace,
} from '@/lib/turn-traces'
import { isApiError } from '@/shared/api/errors'

const LIMITS = [20, 50, 100] as const

/** Width of a stacked-bar segment, as a percentage of the turn total. */
function segmentPercent(value: number | null, total: number): number {
  if (value == null || total <= 0) return 0
  return Math.min(100, Math.max(0, (value / total) * 100))
}

function PhaseBar({ trace }: { trace: TurnTrace }) {
  const { t } = useTranslation()
  const total = trace.total_ms || 0
  return (
    <div className="flex h-2 w-full overflow-hidden rounded-full bg-[var(--muted)]" aria-hidden="true">
      {TRACE_PHASES.map((phase, index) => {
        const value = phaseMs(trace, phase.key)
        const percent = segmentPercent(value, total)
        if (percent <= 0) return null
        return (
          <span
            key={phase.key}
            title={`${t(phase.label)}: ${formatMs(value)}`}
            className="h-full"
            style={{
              width: `${percent}%`,
              // One hue ramp keeps the bar readable in both themes without
              // adding eight new palette entries.
              opacity: 1 - index * 0.09,
              backgroundColor: 'var(--foreground)',
            }}
          />
        )
      })}
    </div>
  )
}

function PhaseGrid({ trace }: { trace: TurnTrace }) {
  const { t } = useTranslation()
  const total = trace.total_ms || 0
  return (
    <dl className="grid grid-cols-2 gap-x-6 gap-y-3 sm:grid-cols-4">
      {TRACE_PHASES.map(phase => {
        const value = phaseMs(trace, phase.key)
        const percent = segmentPercent(value, total)
        return (
          <div key={phase.key} className="min-w-0">
            <dt className="mb-1 flex items-baseline justify-between gap-2 text-[11px] text-[var(--muted-foreground)]">
              <span className="truncate">{t(phase.label)}</span>
              {value != null && <span className="tabular-nums">{percent.toFixed(0)}%</span>}
            </dt>
            <dd className="text-sm font-medium tabular-nums">{formatMs(value)}</dd>
            <div className="mt-1 h-1 w-full overflow-hidden rounded-full bg-[var(--muted)]">
              <div
                className="h-full rounded-full bg-[var(--foreground)]/70"
                style={{ width: `${percent}%` }}
              />
            </div>
          </div>
        )
      })}
    </dl>
  )
}


function TraceDetail({ trace }: { trace: TurnTrace }) {
  const { t } = useTranslation()
  const rounds = trace.rounds ?? []
  const tools = trace.tools ?? []
  const stages = trace.stages ?? []
  const findings = trace.findings ?? []
  return (
    <div className="space-y-5 border-t border-[var(--border)]/60 bg-[var(--muted)]/20 px-4 py-4">
      {trace.user_message && (
        <section>
          <h3 className="mb-2 text-xs font-medium">{t('User question')}</h3>
          <p className="whitespace-pre-wrap break-words text-xs leading-relaxed">
            {trace.user_message}
          </p>
          {trace.user_message_truncated && (
            <p className="mt-1 text-[11px] text-[var(--muted-foreground)]">…</p>
          )}
        </section>
      )}

      <section>
        <h3 className="mb-2 text-xs font-medium">{t('Phase breakdown')}</h3>
        <PhaseGrid trace={trace} />
        {trace.estimated && (
          <p className="mt-2 text-[11px] text-[var(--muted-foreground)]">
            {t('Estimated: this turn predates phase timings.')}
          </p>
        )}
      </section>

      {rounds.length > 0 && (
        <section>
          <h3 className="mb-2 text-xs font-medium">{t('Model rounds')}</h3>
          <div className="overflow-x-auto rounded-lg border border-[var(--border)]">
            <table className="w-full min-w-[520px] text-xs tabular-nums">
              <thead>
                <tr className="border-b border-[var(--border)] bg-[var(--muted)]/40 text-[11px] text-[var(--muted-foreground)]">
                  {[t('Round'), t('Total'), t('TTFT'), t('Tokens'), t('Tools'), t('Status')].map(
                    (label, index) => (
                      <th
                        key={label}
                        scope="col"
                        className={`px-3 py-2 font-medium ${index === 0 || index === 5 ? 'text-left' : 'text-right'}`}
                      >
                        {label}
                      </th>
                    ),
                  )}
                </tr>
              </thead>
              <tbody>
                {rounds.map(round => (
                  <tr key={round.n} className="border-b border-[var(--border)]/60 last:border-0">
                    <th scope="row" className="px-3 py-2 text-left font-medium">
                      #{round.n}
                    </th>
                    <td className="px-3 py-2 text-right">{formatMs(round.ms)}</td>
                    <td className="px-3 py-2 text-right">{formatMs(round.ttft_ms)}</td>
                    <td className="px-3 py-2 text-right">
                      {round.completion_tokens == null ? '—' : round.completion_tokens}
                    </td>
                    <td className="px-3 py-2 text-right">{round.tools ?? 0}</td>
                    <td className="px-3 py-2 text-left text-[var(--muted-foreground)]">
                      {round.status || '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      {tools.length > 0 && (
        <section>
          <h3 className="mb-2 text-xs font-medium">{t('Tool calls')}</h3>
          <ul className="space-y-1">
            {tools.map((tool, index) => (
              <li
                key={`${tool.name}-${index}`}
                className="flex items-baseline justify-between gap-3 text-xs"
              >
                <span className="truncate font-medium">{tool.name}</span>
                <span className="flex shrink-0 items-baseline gap-3 tabular-nums">
                  <span
                    className={
                      tool.status === 'error'
                        ? 'font-medium text-[var(--destructive)]'
                        : 'text-[var(--muted-foreground)]'
                    }
                  >
                    {tool.status || '—'}
                  </span>
                  <span>{formatMs(tool.ms)}</span>
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}

      {stages.length > 0 && (
        <section>
          <h3 className="mb-2 text-xs font-medium">{t('Stages')}</h3>
          <ul className="space-y-1">
            {stages.map((stage, index) => (
              <li
                key={`${stage.name}-${index}`}
                className="flex items-baseline justify-between gap-3 text-xs"
              >
                <span className="truncate">{stage.name || '—'}</span>
                <span className="shrink-0 tabular-nums">{formatMs(stage.ms)}</span>
              </li>
            ))}
          </ul>
        </section>
      )}

      {findings.length > 0 && (
        <section>
          <h3 className="mb-2 text-xs font-medium">{t('Findings')}</h3>
          <ul className="space-y-1">
            {findings.map(finding => (
              <li key={finding.code} className="text-xs text-[var(--muted-foreground)]">
                <span
                  className={
                    finding.severity === 'warning'
                      ? 'mr-2 font-medium text-[var(--destructive)]'
                      : 'mr-2 font-medium'
                  }
                >
                  {finding.severity}
                </span>
                {finding.text}
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  )
}

export   default function LatencySettingsSection() {
  const { t, i18n } = useTranslation()
  const [limit, setLimit] = useState<number>(LIMITS[0])
  const [sessionId, setSessionId] = useState('')
  const [expanded, setExpanded] = useState<string | null>(null)
  const [revision, setRevision] = useState(0)
  const [snapshot, setSnapshot] = useState<{
    key: string
    data: TurnTrace[] | null
    error: string | null
  } | null>(null)

  const requestKey = `${limit}:${sessionId}:${revision}`
  const loading = snapshot?.key !== requestKey
  const error = !loading && Boolean(snapshot?.error)

  useEffect(() => {
    const controller = new AbortController()
    fetchRecentTurnTraces(limit, sessionId, controller.signal)
      .then(value => {
        if (!controller.signal.aborted) setSnapshot({ key: requestKey, data: value, error: null })
      })
      .catch((failure: unknown) => {
        if (!controller.signal.aborted)
          setSnapshot(previous => ({
            key: requestKey,
            data: previous?.data ?? null,
            error: failure instanceof Error
              ? `${failure.message}${isApiError(failure) && failure.status ? ` (HTTP ${failure.status})` : ''}`
              : 'Unable to load turn latency.',
          }))
      })
    return () => controller.abort()
  }, [limit, sessionId, requestKey])

  const visible = snapshot?.key === requestKey ? snapshot?.data ?? null : null

  /** Conversations present in the loaded sample, with how many turns each has. */
  const sessions = useMemo(() => {
    const seen = new Map<string, number>()
    for (const trace of snapshot?.data ?? []) {
      if (!trace.session_id) continue
      seen.set(trace.session_id, (seen.get(trace.session_id) ?? 0) + 1)
    }
    return [...seen.entries()]
  }, [snapshot])

  const totals = useMemo(() => {
    const values = (visible ?? []).map(trace => trace.total_ms).filter(value => value > 0)
    if (!values.length) return null
    return {
      count: values.length,
      average: values.reduce((sum, value) => sum + value, 0) / values.length,
      slowest: Math.max(...values),
      p95: percentile(values, 0.95),
    }
  }, [visible])

  const when = (seconds: number) =>
    seconds > 0
      ? new Date(seconds * 1000).toLocaleString(i18n.language, {
          month: '2-digit',
          day: '2-digit',
          hour: '2-digit',
          minute: '2-digit',
        })
      : '—'


  return (
    <div>
      <SettingsPageHeader
        title={t('Turn latency')}
        description={t(
          'Where each turn spent its time, derived from persisted events. Nothing is re-run.',
        )}
      />

      <div className="mb-6 flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap items-center gap-2">
          <label className="sr-only" htmlFor="latency-session">
            {t('Conversation')}
          </label>
          <select
            id="latency-session"
            value={sessionId}
            onChange={event => setSessionId(event.target.value)}
            className="max-w-[220px] rounded-lg border border-[var(--border)] bg-transparent px-2.5 py-1.5 text-xs outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring)]"
          >
            <option value="">{t('All conversations')}</option>
            {sessions.map(([id, count]) => (
              <option key={id} value={id}>
                {id.slice(0, 8)} ({count})
              </option>
            ))}
          </select>
          <label className="sr-only" htmlFor="latency-limit">
            {t('Turns')}
          </label>
          <select
            id="latency-limit"
            value={limit}
            onChange={event => setLimit(Number(event.target.value))}
            className="rounded-lg border border-[var(--border)] bg-transparent px-2.5 py-1.5 text-xs tabular-nums outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring)]"
          >
            {LIMITS.map(value => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
        </div>
        <button
          type="button"
          aria-label={t('Refresh')}
          disabled={loading}
          onClick={() => setRevision(value => value + 1)}
          className="rounded-lg p-2 text-[var(--muted-foreground)] hover:bg-[var(--muted)] disabled:opacity-40"
        >
          <RefreshCw size={15} className={loading ? 'animate-spin' : ''} />
        </button>
      </div>

      {error && (
        <div
          role="alert"
          className="mb-5 flex items-center justify-between gap-3 rounded-lg bg-[var(--destructive)]/5 px-3 py-2 text-xs text-[var(--destructive)]"
        >
          <span>
            {t('Unable to load turn latency.')}
            {snapshot?.error && <span className="mt-1 block break-words">{t(snapshot.error)}</span>}
          </span>
          <button
            type="button"
            onClick={() => setRevision(value => value + 1)}
            className="shrink-0 px-2 py-1 font-medium"
          >
            {t('Retry')}
          </button>
        </div>
      )}

      {loading && !visible && (
        <div role="status" className="py-16 text-center text-sm text-[var(--muted-foreground)]">
          {t('Loading')}
        </div>
      )}

      {visible && totals && (
        <dl className="mb-6 grid grid-cols-2 gap-x-6 gap-y-5 sm:grid-cols-4">
          {[
            [t('Turns'), String(totals.count)],
            [t('Average'), formatMs(totals.average)],
            [t('P95'), formatMs(totals.p95)],
            [t('Slowest'), formatMs(totals.slowest)],
          ].map(([label, value]) => (
            <div key={label}>
              <dt className="mb-1.5 text-xs text-[var(--muted-foreground)]">{label}</dt>
              <dd className="text-xl font-semibold tracking-tight tabular-nums">{value}</dd>
            </div>
          ))}
        </dl>
      )}

      {visible && !visible.length && (
        <p className="py-16 text-center text-sm text-[var(--muted-foreground)]">
          {t('No turns recorded yet.')}
        </p>
      )}

      {visible && visible.length > 0 && (
        <div className="overflow-x-auto rounded-xl border border-[var(--border)]">
          <table className="w-full min-w-[980px] text-xs tabular-nums" aria-busy={loading}>
            <thead>
              <tr className="border-b border-[var(--border)] bg-[var(--muted)]/40 text-[11px] text-[var(--muted-foreground)]">
                <th scope="col" className="w-8 px-2 py-3" />
                {[t('Time'), t('Question'), t('Capability'), t('Status'), t('Total'), t('Bottleneck')].map(
                  (label, index) => (
                    <th
                      key={label}
                      scope="col"
                      className={`px-3 py-3 font-medium ${index === 4 ? 'text-right' : 'text-left'}`}
                    >
                      {label}
                    </th>
                  ),
                )}
              </tr>
            </thead>
            <tbody>
              {visible.map(trace => {
                const open = expanded === trace.turn_id
                const detailId = `latency-detail-${trace.turn_id}`
                return [
                  <tr
                    key={trace.turn_id}
                    className={`border-b border-[var(--border)]/60 ${open ? '' : 'last:border-0'} hover:bg-[var(--muted)]/30`}
                  >
                    <td className="px-2 py-3 align-middle">
                      <button
                        type="button"
                        aria-expanded={open}
                        aria-controls={detailId}
                        aria-label={open ? t('Collapse') : t('Expand')}
                        onClick={() => setExpanded(open ? null : trace.turn_id)}
                        className="rounded-md p-1 text-[var(--muted-foreground)] hover:bg-[var(--muted)]"
                      >
                        <ChevronRight
                          size={14}
                          className={`transition-transform ${open ? 'rotate-90' : ''}`}
                        />
                      </button>
                    </td>
                    <td className="whitespace-nowrap px-3 py-3 text-left text-[var(--muted-foreground)]">
                      {when(trace.created_at)}
                    </td>
                    <td className="max-w-[260px] px-3 py-3 text-left">
                      {trace.user_message ? (
                        <span
                          className="block truncate"
                          title={trace.user_message}
                        >
                          {trace.user_message}
                        </span>
                      ) : (
                        <span className="text-[var(--muted-foreground)]">—</span>
                      )}
                    </td>
                    <td className="px-3 py-3 text-left font-medium">{trace.capability || '—'}</td>
                    <td className="px-3 py-3 text-left text-[var(--muted-foreground)]">
                      {trace.status || '—'}
                    </td>
                    <td className="px-3 py-3 text-right font-medium">
                      {formatMs(trace.total_ms)}
                      <div className="mt-1.5">
                        <PhaseBar trace={trace} />
                      </div>
                    </td>
                    <td className="px-3 py-3 text-left text-[var(--muted-foreground)]">
                      {trace.top_bottleneck || '—'}
                    </td>
                  </tr>,
                  open ? (
                    <tr
                      key={`${trace.turn_id}-detail`}
                      id={detailId}
                      className="border-b border-[var(--border)]/60"
                    >
                      <td colSpan={7} className="p-0">
                        <TraceDetail trace={trace} />
                      </td>
                    </tr>
                  ) : null,
                ]
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

