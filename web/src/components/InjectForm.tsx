import { useEffect, useState } from 'react'
import { Catalog, Mode } from '../api'
import Icon from './Icons'

export interface InjectRequest {
  fault_class?: string
  service?: string
  scenario_id?: string
  mode: Mode
  planted_injection?: boolean
}

const MODES: [Mode, string, string][] = [
  ['graph', 'Graph', 'Full graph memory: recall, playbooks, facts'],
  ['vector', 'Vector', 'Text-similarity recall only'],
  ['off', 'Off', 'No memory: solved from scratch'],
]
const SHORT: Record<string, string> = {
  bad_deploy: 'Bad deploy', bad_flag: 'Bad flag', db_pool_exhaustion: 'DB pool', memory_leak: 'Mem leak',
  cert_expiry: 'Cert expiry', dependency_outage: 'Outage', traffic_spike: 'Traffic', cascading: 'Cascading',
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
      <div className="section-title"><Icon name="bolt" size={15} /> Fault injector</div>
      <div className="tiles" role="radiogroup" aria-label="Fault class">
        {(catalog?.fault_classes ?? []).map((c) => (
          <button type="button" key={c.name} role="radio" aria-checked={fc === c.name} className={`tile${fc === c.name ? ' on' : ''}`}
                  onClick={() => pick(c.name)} title={`${c.injected_as}. Fix: ${c.correct_fix}`}>
            <Icon name={c.name} size={18} />
            <span>{SHORT[c.name] ?? c.name}</span>
          </button>
        ))}
        {!catalog && Array.from({ length: 8 }, (_, i) => <span key={i} className="tile skeleton" />)}
      </div>
      {klass && <p className="hint"><b>{klass.injected_as}.</b> {klass.symptoms}.</p>}

      <label className="field">
        <span>Target service</span>
        <select value={svc} onChange={(e) => setSvc(e.target.value)} disabled={!klass}>
          {klass?.services.map((s) => <option key={s}>{s}</option>)}
        </select>
      </label>

      <div className="field">
        <span>Memory</span>
        <div className="seg" role="radiogroup" aria-label="Memory mode">
          {MODES.map(([m, label, help]) => (
            <label key={m} className={mode === m ? 'on' : ''} title={help}>
              <input type="radio" name="mode" checked={mode === m} onChange={() => setMode(m)} />{label}
            </label>
          ))}
        </div>
      </div>

      <label className="toggle">
        <input type="checkbox" checked={planted} onChange={(e) => setPlanted(e.target.checked)} />
        <span className="switch" aria-hidden />
        <span>Plant a prompt injection <small className="muted">(safety test)</small></span>
      </label>

      <button className="cta" type="submit" disabled={busy || !fc || !svc}>
        <Icon name="siren" size={17} /> {busy ? 'Paging the crew…' : 'Trigger incident'}
      </button>

      {catalog && catalog.scenarios.length > 0 && (
        <div className="stories">
          <span className="muted">Demo story</span>
          {catalog.scenarios.map((s, i) => (
            <button type="button" key={s.id} className="story" disabled={busy} title={`${s.fault_class} on ${s.service}`}
                    onClick={() => onSubmit({ scenario_id: s.id, mode })}>
              <b>{i + 1}</b> {s.service}
            </button>
          ))}
        </div>
      )}
    </form>
  )
}
