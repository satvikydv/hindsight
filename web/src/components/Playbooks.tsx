import { Playbook, request } from '../api'
import { useAsync } from '../hooks'

export default function Playbooks({ tkey }: { tkey: string }) {
  const { data, error, loading, reload } = useAsync(() => request<{ playbooks: Playbook[] }>('/playbooks', tkey), [tkey])
  if (loading && !data) return <p className="muted pad">Loading playbooks…</p>
  if (error) return <p className="error pad">{error}</p>
  const rows = data?.playbooks ?? []
  return (
    <div className="bench">
      <header className="bench-head">
        <h1>Playbooks</h1>
        <button className="link" onClick={reload}>refresh</button>
      </header>
      <p className="muted">What the crew has learned works. The weight rises with successes and fades if a playbook goes unused.</p>
      {rows.length === 0 ? (
        <div className="placeholder"><h2>Nothing learned yet</h2><p className="muted">Resolve an incident and the Scribe will write the first playbook.</p></div>
      ) : (
        <div className="table-wrap">
          <table>
            <thead><tr><th>playbook</th><th>weight</th><th>worked</th><th>status</th><th>learned from</th></tr></thead>
            <tbody>
              {rows.map((p) => (
                <tr key={p.id}>
                  <th scope="row">{p.name}<br /><span className="muted">{p.fault_class}</span></th>
                  <td><div className="bar" title={p.weight.toFixed(3)}><i style={{ width: `${Math.min(100, Math.round(p.weight * 100))}%` }} /></div></td>
                  <td>{p.successes}/{p.attempts}</td>
                  <td><span className={`badge ${p.status === 'active' ? 'ok' : 'warn'}`}>{p.status}</span></td>
                  <td>{p.derived_from.length} incident{p.derived_from.length === 1 ? '' : 's'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
