import { useEffect, useMemo, useState } from 'react'
import { ApiError, Explain, GEdge, GNode, request, Subgraph } from '../api'
import GraphView, { colorOf } from './GraphView'
import Icon from './Icons'
import { AgentChip } from './Timeline'

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
      <header className="panel-head">
        <span className="section-title"><Icon name="graph" size={15} /> Knowledge graph</span>
        <span className="muted small">{center ? <>around <b className="mono">{center}</b></> : 'no incident selected'}</span>
        <span className="spacer" />
        <div className="seg small" role="radiogroup" aria-label="Neighbourhood size">
          {[1, 2].map((h) => (
            <label key={h} className={hops === h ? 'on' : ''}>
              <input type="radio" name="hops" checked={hops === h} onChange={() => setHops(h)} />{h} hop{h > 1 ? 's' : ''}
            </label>
          ))}
        </div>
      </header>

      <div className="graph-stage">
        {err && <p className="error pad">{err}</p>}
        {!center && (
          <div className="graph-empty"><Icon name="graph" size={34} /><p>The services around the incident's origin appear here.</p></div>
        )}
        <GraphView nodes={nodes} edges={edges} center={sub?.center} highlight={highlight} selected={picked} onSelect={setPicked} />
        {sub && sub.truncated > 0 && <span className="more">+{sub.truncated} more not shown</span>}
        {explain && <button className="clear-hl" onClick={onClear}><Icon name="x" size={12} /> clear highlight</button>}
        {node && <p className="picked"><i style={{ background: colorOf(node.label) }} /> <b>{node.label}</b> {node.name}</p>}
      </div>

      <div className="legend" aria-hidden>
        {['Service', 'Datastore', 'Deploy', 'ConfigFlag', 'Incident', 'Playbook'].map((l) => (
          <span key={l}><i style={{ background: colorOf(l) }} />{l}</span>
        ))}
      </div>

      {!explain && !explainLoading && center && (
        <p className="hint pad"><Icon name="eye" size={13} /> Click <b>why?</b> on any action to light up the evidence and memory behind it.</p>
      )}
      {explainLoading && <p className="muted pad"><span className="spinner" /> Tracing the reasoning…</p>}
      {explain && (
        <section className="why">
          <div className="why-title"><Icon name="eye" size={16} /> Why <code>{explain.action.tool}</code></div>
          {explain.system_initiated && <p className="muted">{explain.note ?? 'Run by the coordinator, not an LLM turn.'}</p>}
          {explain.reasoning && (
            <blockquote>
              <div className="quote-head"><AgentChip agent={explain.reasoning.agent} /><span className="mono muted small">{explain.reasoning.model}</span></div>
              <p>{explain.reasoning.content || <span className="muted">(the model gave no prose, only the tool call)</span>}</p>
              <footer className="mono muted small">
                {(explain.reasoning.prompt_tokens + explain.reasoning.completion_tokens).toLocaleString()} tok · {explain.reasoning.latency_ms} ms
                {explain.reasoning.cost_usd != null && ` · $${explain.reasoning.cost_usd.toFixed(4)}`}
              </footer>
            </blockquote>
          )}
          {mp && (
            <div className="recall-path">
              <div className="rp-title"><Icon name="brain" size={14} /> Memory path</div>
              <div className="rp-chain">
                <span className="rp-node now">this incident</span>
                {mp.similar_incidents.map((s) => (
                  <span key={s.incident} className="rp-seg">
                    <span className="rp-edge">≈ {s.score.toFixed(2)}</span>
                    <span className="rp-node inc">{s.title || s.incident}</span>
                  </span>
                ))}
                <span className="rp-edge">resolved by</span>
                <span className="rp-node pb">{mp.playbook.name}<small> w {mp.playbook.weight.toFixed(2)}</small></span>
                {mp.step && <><span className="rp-edge">step</span><span className="rp-node step">#{mp.step.step}</span></>}
              </div>
            </div>
          )}
          {explain.evidence.length > 0 && (
            <>
              <div className="rp-title"><Icon name="search" size={14} /> Cited evidence</div>
              <ul className="evidence">
                {explain.evidence.map((ev) => (
                  <li key={ev.id}>
                    <button className="ev-chip" onClick={() => setPicked(ev.id)} style={{ ['--c' as string]: colorOf(ev.label) }}>
                      <i />{ev.label}: {ev.name}
                    </button>
                    {ev.why && <span className="muted small"> {ev.why}</span>}
                  </li>
                ))}
              </ul>
            </>
          )}
          {explain.supporting_hypotheses.length > 0 && (
            <ul className="evidence">
              {explain.supporting_hypotheses.map((h) => (
                <li key={h.id}><span className="tag">{pct(h.score)}</span> {h.text} <span className="muted small">({h.status})</span></li>
              ))}
            </ul>
          )}
        </section>
      )}
    </div>
  )
}
