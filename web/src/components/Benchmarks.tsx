import { useMemo, useState } from 'react'
import { Area, CartesianGrid, ComposedChart, Legend, Line, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { BenchReport, BenchRow, ModeStats, request } from '../api'
import { useAsync } from '../hooks'
import Icon from './Icons'

// SVG attributes do not resolve CSS variables reliably, so the chart palette lives here.
const PAL = { off: '#94a3b8', vector: '#ffc857', graph: '#7ee787', grid: '#1e2836', axis: '#64748b', tip: '#0f141c' }
const MODE_LABEL: Record<string, string> = { off: 'No memory', vector: 'Vector only', graph: 'Graph memory' }
const colorOf = (m: string) => PAL[m as 'off' | 'vector' | 'graph'] ?? '#4fd1ff'

const pct = (v: number | null | undefined) => (v == null ? 'n/a' : `${Math.round(v * 100)}%`)
const num = (v: number | null | undefined, d = 1) => (v == null ? 'n/a' : v.toFixed(d))
const usd = (v: number | null | undefined) => (v == null ? 'n/a' : `$${v.toFixed(4)}`)

/** Running mean over `w` incidents, so the trend is visible through the incident-to-incident noise. */
export function smooth(values: (number | null)[], w: number): (number | null)[] {
  return values.map((_, i) => {
    const win = values.slice(Math.max(0, i - w + 1), i + 1).filter((x): x is number => x != null)
    return win.length ? Math.round((win.reduce((a, b) => a + b, 0) / win.length) * 100) / 100 : null
  })
}

function pickDefault(rows: BenchRow[]): string | null {
  const real = rows.filter((r) => r.llm !== 'scripted' && !r.partial)
  return (real[0] ?? rows[0])?.id ?? null
}

export default function Benchmarks() {
  const list = useAsync(() => request<{ benchmarks: BenchRow[] }>('/bench', null), [])
  const [chosen, setChosen] = useState<string | null>(null)
  const rows = list.data?.benchmarks ?? []
  const id = chosen ?? pickDefault(rows)
  const report = useAsync(() => (id ? request<BenchReport>(`/bench/${id}`, null) : Promise.resolve(null)), [id])
  const [win, setWin] = useState(5)

  if (list.error) return <p className="error pad">{list.error}</p>
  if (list.loading) return <p className="muted pad"><span className="spinner" /> Loading benchmarks…</p>
  if (!rows.length) return (
    <div className="placeholder"><h2>No benchmark has been run yet</h2>
      <p className="muted">Run <code>python -m api.bench.runner</code> and commit the report under <code>docs/results/</code>.</p></div>
  )

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <span className="eyebrow"><Icon name="chart" size={13} /> Ablation study</span>
          <h1>Does graph memory make the crew better?</h1>
          <p className="muted">The same incidents, run three ways: no memory, vector recall only, and full graph memory. Precomputed, so this page costs nothing to view.</p>
        </div>
        <label className="field inline">
          <span>Run</span>
          <select value={id ?? ''} onChange={(e) => setChosen(e.target.value)}>
            {rows.map((r) => <option key={r.id} value={r.id}>{r.id} · {r.llm} · {r.incidents} incidents{r.partial ? ' · partial' : ''}</option>)}
          </select>
        </label>
      </header>
      {report.error && <p className="error pad">{report.error}</p>}
      {report.data && <Report r={report.data} win={win} setWin={setWin} />}
    </div>
  )
}

function Report({ r, win, setWin }: { r: BenchReport; win: number; setWin: (n: number) => void }) {
  const modes = Object.keys(r.aggregate.modes)
  const scripted = r.config.llm === 'scripted'
  const g = r.aggregate.comparisons.graph
  const m = r.aggregate.modes

  const series = useMemo(() => {
    const idx = new Set<number>()
    Object.values(r.curves).forEach((c) => c.forEach((p) => idx.add(p.index)))
    const xs = [...idx].sort((a, b) => a - b)
    const per: Record<string, (number | null)[]> = {}
    for (const mode of modes) {
      const by = new Map((r.curves[mode] ?? []).map((p) => [p.index, p.mean]))
      per[mode] = smooth(xs.map((x) => by.get(x) ?? null), win)
    }
    return xs.map((x, i) => Object.fromEntries([['index', x], ...modes.map((mode) => [mode, per[mode][i]])]))
  }, [r, modes.join(), win])      // eslint-disable-line react-hooks/exhaustive-deps

  const maxRepeat = Math.max(...modes.map((x) => m[x].steps_repeat ?? 0), 1)

  return (
    <>
      {scripted && (
        <div className="banner warn inline" role="note">
          <Icon name="shield" size={18} />
          <div><b>Scripted policy, not the language model.</b> This run validates the pipeline and the graph mechanics; it does not
            show how the real agents perform. Costs are nominal and nothing was spent.</div>
        </div>
      )}
      {r.partial && <div className="banner warn inline" role="note"><b>Partial run</b>, stopped early ({r.stopped_reason}). The numbers cover only what ran.</div>}

      {g && (
        <div className="cards">
          <Card icon="steps" title="Fewer steps on repeats" value={g.steps_reduction_repeat_pct == null ? 'n/a' : `${num(g.steps_reduction_repeat_pct, 0)}%`} sub="graph vs no memory" accent="green" />
          <Card icon="token" title="Fewer tokens on repeats" value={g.tokens_reduction_repeat_pct == null ? 'n/a' : `${num(g.tokens_reduction_repeat_pct, 0)}%`} sub="graph vs no memory" accent="cyan" />
          <Card icon="check" title="Resolution rate" value={pct(m.graph?.resolution_rate)} sub={`no memory ${pct(m.off?.resolution_rate)}`} accent="violet" />
          <Card icon="shield" title="Harmful actions" value={String(m.graph?.harmful_total ?? 'n/a')} sub={`${m.graph?.injection_cases ?? 0} planted injections, ${m.graph?.injection_harmful ?? 0} harmful`} accent={m.graph?.harmful_total === 0 ? 'green' : 'red'} />
        </div>
      )}

      <div className="bench-grid">
        <section className="panel chart-panel">
          <div className="sec-head">
            <div><h2>Learning curve</h2>
              <p className="muted small">Steps per incident in arrival order, mean of {r.config.seeds} seed{r.config.seeds > 1 ? 's' : ''}. Lower is better.</p></div>
            <div className="seg small" role="radiogroup" aria-label="Smoothing">
              {[[1, 'raw'], [3, '3'], [5, '5']].map(([v, l]) => (
                <label key={v} className={win === v ? 'on' : ''}><input type="radio" checked={win === v} onChange={() => setWin(v as number)} />{l}</label>
              ))}
            </div>
          </div>
          <ResponsiveContainer width="100%" height={300}>
            <ComposedChart data={series} margin={{ top: 8, right: 12, left: -8, bottom: 18 }}>
              <defs>
                <linearGradient id="gGraph" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0" stopColor={PAL.graph} stopOpacity={0.35} /><stop offset="1" stopColor={PAL.graph} stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid stroke={PAL.grid} vertical={false} />
              <XAxis dataKey="index" stroke={PAL.axis} tickLine={false} axisLine={false} fontSize={11}
                     label={{ value: 'incident #', position: 'insideBottom', offset: -10, fill: PAL.axis, fontSize: 11 }} />
              <YAxis stroke={PAL.axis} domain={[0, 'auto']} tickLine={false} axisLine={false} fontSize={11} />
              <Tooltip contentStyle={{ background: PAL.tip, border: `1px solid ${PAL.grid}`, borderRadius: 10 }} labelFormatter={(v) => `incident ${v}`} />
              <Legend verticalAlign="top" height={28} iconType="plainline" />
              {modes.includes('graph') && (
                <Area type="monotone" dataKey="graph" name={MODE_LABEL.graph} stroke={PAL.graph} strokeWidth={3}
                      fill="url(#gGraph)" isAnimationActive={false} connectNulls dot={false} />
              )}
              {modes.filter((x) => x !== 'graph').map((mode) => (
                <Line key={mode} type="monotone" dataKey={mode} name={MODE_LABEL[mode] ?? mode} stroke={colorOf(mode)}
                      strokeWidth={mode === 'off' ? 4 : 2} strokeDasharray={mode === 'vector' ? '6 4' : undefined}
                      strokeOpacity={mode === 'off' ? 0.6 : 1} isAnimationActive={false} dot={false} connectNulls />
              ))}
            </ComposedChart>
          </ResponsiveContainer>
        </section>

        <section className="panel bars-panel">
          <h2>Repeat incidents</h2>
          <p className="muted small">Mean steps once the crew has seen the fault class before.</p>
          <div className="mode-bars">
            {modes.map((x) => (
              <div key={x} className="mode-bar">
                <span className="mb-label"><i style={{ background: colorOf(x) }} />{MODE_LABEL[x] ?? x}</span>
                <span className="mb-track"><span className="mb-fill" style={{ width: `${((m[x].steps_repeat ?? 0) / maxRepeat) * 100}%`, background: colorOf(x) }} /></span>
                <span className="mono">{num(m[x].steps_repeat)}</span>
              </div>
            ))}
          </div>
          <div className="mini-stats">
            <div><b>{pct(m.graph?.recall_at_3)}</b><small>Recall@3, graph</small></div>
            <div><b>{pct(m.vector?.recall_at_3)}</b><small>Recall@3, vector</small></div>
            <div><b>{pct(m.graph?.fast_path_rate_repeat)}</b><small>fast path on repeats</small></div>
          </div>
        </section>
      </div>

      <section className="panel">
        <h2>Ablation</h2>
        <div className="table-wrap">
          <table>
            <thead><tr><th>metric</th>{modes.map((x) => <th key={x} className={x === 'graph' ? 'hl-col' : ''}><i className="dot" style={{ background: colorOf(x) }} />{MODE_LABEL[x] ?? x}</th>)}</tr></thead>
            <tbody>
              {ROWS.map(([label, key, fmt]) => (
                <tr key={key}><th scope="row">{label}</th>{modes.map((x) => <td key={x} className={`mono${x === 'graph' ? ' hl-col' : ''}`}>{fmt(m[x][key] as any)}</td>)}</tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="muted small">{r.records.length} incidents · total LLM cost {usd(r.total_cost_usd)}{scripted ? ' (nominal)' : ''} · {r.config.llm} · started {r.started_at.slice(0, 16).replace('T', ' ')} UTC</p>
      </section>

      {r.aggregate.targets && (
        <section className="panel">
          <h2>Against the goals</h2>
          <p className="muted small">Targets set before the run. A miss is reported as a miss.</p>
          <div className="goals">
            {Object.entries(r.aggregate.targets).map(([k, t]) => (
              <div key={k} className={`goal ${t.met == null ? '' : t.met ? 'met' : 'missed'}`}>
                <span className="goal-icon"><Icon name={t.met ? 'check' : t.met === false ? 'x' : 'clock'} size={14} /></span>
                <span className="goal-label">{t.label}</span>
                <span className="mono">{t.measured == null ? 'n/a' : num(t.measured, t.measured % 1 ? 2 : 0)} <small className="muted">/ {t.target}</small></span>
              </div>
            ))}
          </div>
        </section>
      )}

      <section className="panel subtle">
        <h2>Not measured yet</h2>
        <p className="muted small">Concurrency under load and the tenant-isolation check appear here once they have been run against this build. Isolation is covered by the automated test suite in the meantime.</p>
      </section>
    </>
  )
}

type Key = keyof ModeStats
const ROWS: [string, Key, (v: number | null) => string][] = [
  ['resolution rate', 'resolution_rate', pct],
  ['steps, all incidents', 'steps_mean', (v) => num(v)],
  ['steps, novel', 'steps_novel', (v) => num(v)],
  ['steps, repeat', 'steps_repeat', (v) => num(v)],
  ['tokens, repeat', 'tokens_repeat', (v) => (v == null ? 'n/a' : Math.round(v).toLocaleString())],
  ['cost per incident', 'cost_mean_usd', usd],
  ['harmful actions', 'harmful_total', (v) => String(v ?? 'n/a')],
  ['root cause confirmed', 'confirmed_rate', pct],
  ['accuracy when confirmed', 'cause_accuracy', pct],
  ['Recall@3', 'recall_at_3', pct],
  ['fast path, repeat incidents', 'fast_path_rate_repeat', pct],
  ['steps on never-failed services', 'heldout_steps', (v) => num(v)],
]

function Card({ icon, title, value, sub, accent }: { icon: string; title: string; value: string; sub: string; accent: string }) {
  return (
    <div className={`stat-card accent-${accent}`}>
      <span className="stat-icon"><Icon name={icon} size={18} /></span>
      <span className="muted small">{title}</span>
      <strong>{value}</strong>
      <span className="muted small">{sub}</span>
    </div>
  )
}
