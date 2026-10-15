import { Catalog, IncidentRow } from '../api'
import { InjectRequest } from './InjectForm'

const money = (v: number | null | undefined) => (v == null ? '' : `$${v.toFixed(3)}`)

export default function IncidentList({ rows, selected, onSelect, catalog, busy, onSubmit }: {
  rows: IncidentRow[] | null
  selected: string | null
  onSelect: (id: string) => void
  catalog: Catalog | null
  busy: boolean
  onSubmit: (r: InjectRequest) => void
}) {
  if (rows && rows.length === 0) {
    const d = catalog?.default
    return (
      <div className="empty">
        <h3>No incidents yet</h3>
        <p className="muted">Inject your first incident to watch the crew work. A good first one:</p>
        {d && (
          <button className="primary" disabled={busy} onClick={() => onSubmit({ fault_class: d.fault_class, service: d.service, mode: 'graph' })}>
            {d.fault_class.replace(/_/g, ' ')} on {d.service}
          </button>
        )}
      </div>
    )
  }
  return (
    <ul className="incident-list" aria-label="Incidents">
      {(rows ?? []).map((r) => (
        <li key={r.id}>
          <button className={r.id === selected ? 'active' : ''} onClick={() => onSelect(r.id)}>
            <span className={`dot st-${r.status}`} aria-hidden />
            <span className="inc-title">{(r.title ?? 'incident').replace(/_/g, ' ')}</span>
            <span className="inc-meta">
              <span className={`badge st-${r.status}`}>{r.status}</span>
              {r.mode && <span className="muted">{r.mode}</span>}
              {r.fast_path && <span className="badge fast">fast path</span>}
              {r.steps != null && <span className="muted">{r.steps} steps</span>}
              {r.tokens != null && <span className="muted">{r.tokens.toLocaleString()} tok</span>}
              <span className="muted">{money(r.cost_to_resolve)}</span>
            </span>
          </button>
        </li>
      ))}
      {rows === null && <li className="muted pad">Loading…</li>}
    </ul>
  )
}
