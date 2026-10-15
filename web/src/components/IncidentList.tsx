import { Catalog, IncidentRow } from '../api'
import Icon from './Icons'
import { InjectRequest } from './InjectForm'

const money = (v: number | null | undefined) => (v == null ? '' : `$${v.toFixed(3)}`)
const ACTIVE = new Set(['queued', 'running', 'open', 'triaging', 'investigating', 'remediating', 'consolidating'])

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
        <div className="empty-art" aria-hidden><Icon name="siren" size={28} /></div>
        <h3>All quiet at Kestrel Pay</h3>
        <p className="muted">Trigger your first incident and watch the crew work it live. A good first one:</p>
        {d && (
          <button className="btn" disabled={busy} onClick={() => onSubmit({ fault_class: d.fault_class, service: d.service, mode: 'graph' })}>
            <Icon name={d.fault_class} size={16} /> {d.fault_class.replace(/_/g, ' ')} on {d.service}
          </button>
        )}
      </div>
    )
  }
  const maxSteps = Math.max(12, ...(rows ?? []).map((r) => r.steps ?? 0))
  return (
    <div className="list-wrap">
      <div className="section-title list-title"><Icon name="layers" size={15} /> Incidents <span className="count">{rows?.length ?? ''}</span></div>
      <ul className="incident-list" aria-label="Incidents">
        {(rows ?? []).map((r) => {
          const live = ACTIVE.has(r.status)
          return (
            <li key={r.id}>
              <button className={`inc-card st-${r.status}${r.id === selected ? ' active' : ''}`} onClick={() => onSelect(r.id)}>
                <span className="inc-row">
                  <span className={`status-dot st-${r.status}${live ? ' pulse' : ''}`} aria-hidden />
                  <span className="inc-title">{r.title ?? 'incident'}</span>
                  {r.fast_path && <span className="tag fast" title="Resolved from memory"><Icon name="bolt" size={12} /></span>}
                </span>
                <span className="inc-meta">
                  <span className={`tag st-${r.status}`}>{r.status}</span>
                  {r.mode && <span className={`tag mode-${r.mode}`}>{r.mode}</span>}
                  <span className="mono muted">{r.steps ?? 0} steps</span>
                  <span className="mono muted">{money(r.cost_to_resolve)}</span>
                </span>
                <span className="stepbar" aria-hidden><i style={{ width: `${Math.round(((r.steps ?? 0) / maxSteps) * 100)}%` }} /></span>
              </button>
            </li>
          )
        })}
        {rows === null && [0, 1, 2].map((i) => <li key={i}><span className="inc-card skeleton" /></li>)}
      </ul>
    </div>
  )
}
