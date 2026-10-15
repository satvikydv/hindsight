import { useEffect, useMemo, useState } from 'react'
import { CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { BenchReport, BenchRow, ModeStats, request } from '../api'
import { useAsync } from '../hooks'

// SVG attributes do not resolve CSS variables reliably, so the chart takes its palette from the colour scheme in JS.
const PALETTE = {
  dark: { off: '#8b949e', vector: '#e3b341', graph: '#3fb950', grid: '#30363d', axis: '#8b949e', tip: '#161b22' },
  light: { off: '#6e7781', vector: '#bf8700', graph: '#1a7f37', grid: '#d0d7de', axis: '#59636e', tip: '#ffffff' },
}

function useDark(): boolean {
  const query = '(prefers-color-scheme: dark)'
  const [dark, setDark] = useState(() => window.matchMedia(query).matches)
  useEffect(() => {
    const m = window.matchMedia(query)
    const on = () => setDark(m.matches)
    m.addEventListener('change', on)
    return () => m.removeEventListener('change', on)
  }, [])
  return dark
}
const MODE_LABEL: Record<string, string> = { off: 'No memory', vector: 'Vector only', graph: 'Graph memory' }

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
  const [window_, setWindow] = useState(5)

  if (list.error) return <p className="error pad">{list.error}</p>
  if (list.loading) return <p className="muted pad">Loading benchmarks…</p>
  if (!rows.length) return (
    <div className="placeholder"><h2>No benchmark has been run yet</h2>
      <p className="muted">Run <code>python -m api.bench.runner</code> and commit the report under <code>docs/results/</code>.</p></div>
  )

  return (
    <div className="bench">
      <header className="bench-head">
        <h1>Benchmarks</h1>
        <label>Run
          <select value={id ?? ''} onChange={(e) => setChosen(e.target.value)}>
            {rows.map((r) => <option key={r.id} value={r.id}>{r.id} · {r.llm} · {r.incidents} incidents{r.partial ? ' · partial' : ''}</option>)}
          </select>
        </label>
      </header>
      {report.error && <p className="error pad">{report.error}</p>}
      {report.data && <Report r={report.data} win={window_} setWin={setWindow} />}
    </div>
  )
}

function Report({ r, win, setWin }: { r: BenchReport; win: number; setWin: (n: number) => void }) {
  const modes = Object.keys(r.aggregate.modes)
  const pal = PALETTE[useDark() ? 'dark' : 'light']
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

  return (
    <>
      {scripted && (
        <div className="banner warn" role="note">
          <b>Scripted policy, not the language model.</b> This run validates the pipeline and the graph mechanics; it does not
          show how the real agents perform. Costs are nominal and nothing was spent.
        </div>
      )}
      {r.partial && <div className="banner warn" role="note"><b>Partial run</b>, stopped early ({r.stopped_reason}). The numbers cover only what ran.</div>}

      {g && (
        <div className="cards">
          <Card title="Fewer steps, repeat incidents" value={g.steps_reduction_repeat_pct == null ? 'n/a' : `${num(g.steps_reduction_repeat_pct, 0)}%`} sub="graph vs no memory" />
          <Card title="Fewer tokens, repeat incidents" value={g.tokens_reduction_repeat_pct == null ? 'n/a' : `${num(g.tokens_reduction_repeat_pct, 0)}%`} sub="graph vs no memory" />
          <Card title="Resolution rate" value={pct(m.graph?.resolution_rate)} sub={`no memory ${pct(m.off?.resolution_rate)}`} />
          <Card title="Harmful actions" value={String(m.graph?.harmful_total ?? 'n/a')} sub={`${m.graph?.injection_cases ?? 0} planted injections: ${m.graph?.injection_harmful ?? 0} harmful`} good={m.graph?.harmful_total === 0} />
        </div>
      )}

      <section>
        <div className="sec-head">
          <h2>Learning curve</h2>
          <label className="muted">smoothing
            <select value={win} onChange={(e) => setWin(Number(e.target.value))}>
              <option value={1}>none</option><option value={3}>3 incidents</option><option value={5}>5 incidents</option>
            </select>
          </label>
        </div>
        <p className="muted">Steps to resolve each incident, in the order they arrive (mean over {r.config.seeds} seed{r.config.seeds > 1 ? 's' : ''}). A falling line is the crew learning.</p>
        <div className="chart">
          <ResponsiveContainer width="100%" height={300}>
            <LineChart data={series} margin={{ top: 8, right: 16, left: 0, bottom: 22 }}>
              <CartesianGrid stroke={pal.grid} strokeDasharray="3 3" />
              <XAxis dataKey="index" stroke={pal.axis} label={{ value: 'incident #', position: 'insideBottom', offset: -2, fill: pal.axis }} />
              <YAxis stroke={pal.axis} domain={[0, 'auto']} label={{ value: 'steps', angle: -90, position: 'insideLeft', fill: pal.axis }} />
              <Tooltip contentStyle={{ background: pal.tip, border: `1px solid ${pal.grid}` }} />
              <Legend />
              {modes.map((mode) => (
                <Line key={mode} type="monotone" dataKey={mode} name={MODE_LABEL[mode] ?? mode}
                      stroke={pal[mode as 'off' | 'vector' | 'graph'] ?? '#58a6ff'} isAnimationActive={false}
                      strokeWidth={mode === 'off' ? 5 : mode === 'graph' ? 3 : 2} strokeDasharray={mode === 'vector' ? '6 4' : undefined}
                      dot={false} connectNulls />
              ))}
            </LineChart>
          </ResponsiveContainer>
        </div>
      </section>

      <section>
        <h2>Ablation</h2>
        <div className="table-wrap">
          <table>
            <thead><tr><th>metric</th>{modes.map((x) => <th key={x}>{MODE_LABEL[x] ?? x}</th>)}</tr></thead>
            <tbody>
              {ROWS.map(([label, key, fmt]) => (
                <tr key={key}><th scope="row">{label}</th>{modes.map((x) => <td key={x}>{fmt(m[x][key] as any)}</td>)}</tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="muted">{r.records.length} incidents · total LLM cost {usd(r.total_cost_usd)}{scripted ? ' (nominal)' : ''} · {r.config.llm} · started {r.started_at.slice(0, 16).replace('T', ' ')} UTC</p>
      </section>

      {r.aggregate.targets && (
        <section>
          <h2>Against the goals</h2>
          <p className="muted">These are the targets set in advance. They are goals, and a miss is reported as a miss.</p>
          <div className="table-wrap">
            <table>
              <thead><tr><th>goal</th><th>target</th><th>measured</th><th /></tr></thead>
              <tbody>
                {Object.entries(r.aggregate.targets).map(([k, t]) => (
                  <tr key={k}><th scope="row">{t.label}</th><td>{t.target}</td><td>{t.measured == null ? 'n/a' : num(t.measured, t.measured % 1 ? 2 : 0)}</td>
                    <td><span className={`badge ${t.met == null ? '' : t.met ? 'ok' : 'warn'}`}>{t.met == null ? 'n/a' : t.met ? 'met' : 'not met'}</span></td></tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      <section>
        <h2>Not measured yet</h2>
        <p className="muted">Concurrency under load and the tenant-isolation check appear here once they have been run against this build. Isolation is covered by the automated test suite in the meantime.</p>
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

function Card({ title, value, sub, good }: { title: string; value: string; sub: string; good?: boolean }) {
  return (
    <div className={`card${good ? ' good' : ''}`}>
      <span className="muted">{title}</span>
      <strong>{value}</strong>
      <span className="muted">{sub}</span>
    </div>
  )
}
