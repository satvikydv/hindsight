import { useEffect, useMemo, useState } from 'react'
import { ApiError, Explain, GEdge, GNode, request, Subgraph } from '../api'
import GraphView, { colorOf } from './GraphView'

const pct = (n: number) => `${Math.round(n * 100)}%`

export default function GraphPanel({ tkey, center, explain, explainLoading, onClear }: {
  tkey: string
  center: string | null
  explain: Explain | null
  explainLoading: boolean
  onClear: () => void
}) {
  const [hops, setHops] = useState(1)
  const [sub, setSub] = useState<Subgraph | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [picked, setPicked] = useState<string | null>(null)

  useEffect(() => {
    setSub(null)
    setErr(null)
    if (!center) return
    let live = true
    request<Subgraph>(`/graph/subgraph?center=${encodeURIComponent(center)}&hops=${hops}`, tkey).then(
      (s) => live && setSub(s),
      (e) => live && setErr(e instanceof ApiError ? e.message : 'could not load the graph'),
    )
    return () => { live = false }
  }, [tkey, center, hops])

  // The neighbourhood plus whatever the selected action cited (its evidence, hypotheses and the recall path).
  const { nodes, edges } = useMemo(() => {
    const byId = new Map<string, GNode>((sub?.nodes ?? []).map((n) => [n.id, n]))
    const all: GEdge[] = [...(sub?.edges ?? [])]
    if (explain) {
      for (const n of explain.subgraph.nodes) if (!byId.has(n.id)) byId.set(n.id, n)
      all.push(...explain.subgraph.edges)
    }
    return { nodes: [...byId.values()], edges: all }
  }, [sub, explain])

  const highlight = useMemo(() => new Set(explain?.highlight ?? []), [explain])
  const node = nodes.find((n) => n.id === picked)
  const mp = explain?.memory_path

  return (
    <div className="graph-panel">
      <header>
        <h2>Graph</h2>
        <span className="muted">{center ? `around ${center}` : 'select an incident'}</span>
        <span className="spacer" />
        <div className="seg small" role="group" aria-label="Neighbourhood size">
          {[1, 2].map((h) => (
            <label key={h} className={hops === h ? 'on' : ''}>
              <input type="radio" name="hops" checked={hops === h} onChange={() => setHops(h)} />{h} hop{h > 1 ? 's' : ''}
            </label>
          ))}
        </div>
        {explain && <button className="link" onClick={onClear}>clear highlight</button>}
      </header>

      <div className="graph-stage">
        {err && <p className="error pad">{err}</p>}
        {!center && <p className="muted pad">Pick an incident to see the services around its origin.</p>}
        <GraphView nodes={nodes} edges={edges} center={sub?.center} highlight={highlight} selected={picked} onSelect={setPicked} />
        {sub && sub.truncated > 0 && <span className="more">+{sub.truncated} more not shown</span>}
      </div>

      <div className="legend" aria-hidden>
        {['Service', 'Datastore', 'Deploy', 'ConfigFlag', 'Incident', 'Playbook'].map((l) => (
          <span key={l}><i style={{ background: colorOf(l) }} />{l}</span>
        ))}
      </div>
      {node && <p className="picked"><i style={{ background: colorOf(node.label) }} /> <b>{node.label}</b> {node.name}</p>}

      {explainLoading && <p className="muted pad">Loading the reasoning…</p>}
      {explain && (
        <section className="why">
          <h3>Why <code>{explain.action.tool}</code>?</h3>
          {explain.system_initiated && <p className="muted">{explain.note ?? 'Run by the coordinator, not an LLM turn.'}</p>}
          {explain.reasoning && (
            <blockquote>
              {explain.reasoning.content || <span className="muted">(the model gave no prose, only the tool call)</span>}
              <footer className="muted">
                {explain.reasoning.agent} · {explain.reasoning.model} · {explain.reasoning.prompt_tokens + explain.reasoning.completion_tokens} tokens
                · {explain.reasoning.latency_ms} ms{explain.reasoning.cost_usd != null && ` · $${explain.reasoning.cost_usd.toFixed(4)}`}
              </footer>
            </blockquote>
          )}
          {mp && (
            <div className="recall">
              <b>Recall path</b>
              <p>
                {mp.similar_incidents.map((s) => (
                  <span key={s.incident}>this incident ≈ <em>{s.title || s.incident}</em> ({s.score.toFixed(2)}, {s.basis}) → </span>
                ))}
                playbook <em>{mp.playbook.name}</em> (weight {mp.playbook.weight.toFixed(2)}, {mp.playbook.status})
                {mp.step && <> → step {mp.step.step}</>}
              </p>
            </div>
          )}
          {explain.evidence.length > 0 && (
            <>
              <b>Cited evidence</b>
              <ul className="evidence">
                {explain.evidence.map((ev) => (
                  <li key={ev.id}>
                    <button className="link" onClick={() => setPicked(ev.id)}><i style={{ background: colorOf(ev.label) }} />{ev.label}: {ev.name}</button>
                    {ev.why && <span className="muted"> — {ev.why}</span>}
                  </li>
                ))}
              </ul>
            </>
          )}
          {explain.supporting_hypotheses.length > 0 && (
            <ul className="evidence">
              {explain.supporting_hypotheses.map((h) => (
                <li key={h.id}>hypothesis <b>{pct(h.score)}</b> ({h.status}): {h.text}</li>
              ))}
            </ul>
          )}
        </section>
      )}
    </div>
  )
}
