import { useEffect, useState } from 'react'
import { Catalog, Mode } from '../api'

export interface InjectRequest {
  fault_class?: string
  service?: string
  scenario_id?: string
  mode: Mode
  planted_injection?: boolean
}

const MODE_HELP: Record<Mode, string> = {
  off: 'No memory: the crew solves it from scratch',
  vector: 'Text-similarity recall only',
  graph: 'Full graph memory: recall, playbooks, facts',
}

export default function InjectForm({ catalog, busy, onSubmit }: {
  catalog: Catalog | null
  busy: boolean
  onSubmit: (r: InjectRequest) => void
}) {
  const [fc, setFc] = useState('')
  const [svc, setSvc] = useState('')
  const [mode, setMode] = useState<Mode>('graph')
  const [planted, setPlanted] = useState(false)

  useEffect(() => {
    if (catalog && !fc) {
      setFc(catalog.default.fault_class)
      setSvc(catalog.default.service)
    }
  }, [catalog, fc])

  const klass = catalog?.fault_classes.find((c) => c.name === fc)
  const pick = (name: string) => {
    setFc(name)
    const c = catalog?.fault_classes.find((x) => x.name === name)
    setSvc(c?.services.includes(svc) ? svc : (c?.services[0] ?? ''))
  }

  return (
    <form className="inject" onSubmit={(e) => { e.preventDefault(); onSubmit({ fault_class: fc, service: svc, mode, planted_injection: planted }) }}>
      <h2>Inject a fault</h2>
      <label>Fault
        <select value={fc} onChange={(e) => pick(e.target.value)} disabled={!catalog}>
          {catalog?.fault_classes.map((c) => <option key={c.name} value={c.name}>{c.name.replace(/_/g, ' ')}</option>)}
        </select>
      </label>
      {klass && <p className="hint">{klass.injected_as}. Symptoms: {klass.symptoms}.</p>}
      <label>Service
        <select value={svc} onChange={(e) => setSvc(e.target.value)} disabled={!klass}>
          {klass?.services.map((s) => <option key={s}>{s}</option>)}
        </select>
      </label>
      <fieldset>
        <legend>Memory</legend>
        <div className="seg">
          {(['graph', 'vector', 'off'] as Mode[]).map((m) => (
            <label key={m} className={mode === m ? 'on' : ''} title={MODE_HELP[m]}>
              <input type="radio" name="mode" checked={mode === m} onChange={() => setMode(m)} />{m}
            </label>
          ))}
        </div>
        <p className="hint">{MODE_HELP[mode]}</p>
      </fieldset>
      <label className="check">
        <input type="checkbox" checked={planted} onChange={(e) => setPlanted(e.target.checked)} />
        Plant a prompt injection in the logs (safety test)
      </label>
      <button className="primary" type="submit" disabled={busy || !fc || !svc}>{busy ? 'Starting…' : 'Inject fault'}</button>
      {catalog && catalog.scenarios.length > 0 && (
        <div className="stories">
          <span className="muted">Demo story:</span>
          {catalog.scenarios.map((s, i) => (
            <button type="button" key={s.id} className="link" disabled={busy} title={`${s.fault_class} on ${s.service}`}
                    onClick={() => onSubmit({ scenario_id: s.id, mode })}>
              {i + 1}. {s.service}
            </button>
          ))}
        </div>
      )}
    </form>
  )
}
