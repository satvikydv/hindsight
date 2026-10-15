import { Playbook, request } from '../api'
import { useAsync } from '../hooks'
import Icon from './Icons'

export default function Playbooks({ tkey }: { tkey: string }) {
  const { data, error, loading, reload } = useAsync(() => request<{ playbooks: Playbook[] }>('/playbooks', tkey), [tkey])
  if (loading && !data) return <p className="muted pad"><span className="spinner" /> Loading playbooks…</p>
  if (error) return <p className="error pad">{error}</p>
  const rows = data?.playbooks ?? []
  return (
    <div className="page">
      <header className="page-head">
        <div>
          <span className="eyebrow"><Icon name="book" size={13} /> Procedural memory</span>
          <h1>Playbooks the crew has learned</h1>
          <p className="muted">Written by the Scribe after each incident. Weight rises with every success and fades when a playbook goes unused for weeks.</p>
        </div>
        <button className="btn small" onClick={reload}><Icon name="refresh" size={14} /> Refresh</button>
      </header>
      {rows.length === 0 ? (
        <div className="placeholder"><div className="empty-art" aria-hidden><Icon name="book" size={28} /></div>
          <h2>Nothing learned yet</h2><p className="muted">Resolve an incident and the Scribe will write the first playbook.</p></div>
      ) : (
        <div className="pb-grid">
          {rows.map((p) => (
            <article key={p.id} className={`pb-card ${p.status}`}>
              <header>
                <span className="pb-icon"><Icon name={p.fault_class} size={18} /></span>
                <div><h3>{p.name}</h3><span className="muted small">{p.fault_class.replace(/_/g, ' ')}</span></div>
                <span className={`tag ${p.status === 'active' ? 'ok' : 'warn'}`}>{p.status}</span>
              </header>
              <div className="pb-weight">
                <span className="muted small">weight</span>
                <span className="mb-track"><span className="mb-fill" style={{ width: `${Math.min(100, Math.round(p.weight * 100))}%` }} /></span>
                <span className="mono">{p.weight.toFixed(2)}</span>
              </div>
              <footer>
                <span><b className="mono">{p.successes}/{p.attempts}</b> <span className="muted small">worked</span></span>
                <span><b className="mono">{p.derived_from.length}</b> <span className="muted small">source incident{p.derived_from.length === 1 ? '' : 's'}</span></span>
              </footer>
            </article>
          ))}
        </div>
      )}
    </div>
  )
}
