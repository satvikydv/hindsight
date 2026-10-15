import { FormEvent, useState } from 'react'
import { request } from '../api'
import Backdrop from './Backdrop'
import Icon from './Icons'

const FEATURES: [string, string, string][] = [
  ['brain', 'Remembers', 'Every incident, fact and fix lives in one FalkorDB graph the agents share.'],
  ['bolt', 'Gets faster', 'A repeat incident skips straight to the playbook that worked last time.'],
  ['eye', 'Shows its work', 'Click any action to see the graph path and evidence behind it.'],
]

export default function Login({ onKey, onBenchmarks }: { onKey: (key: string) => void; onBenchmarks: () => void }) {
  const [name, setName] = useState('')
  const [key, setKey] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const run = async (e: FormEvent, fn: () => Promise<string>) => {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      onKey(await fn())
    } catch (err) {
      const m = (err as Error).message
      setError(m === 'unknown tenant key' ? 'That key was not recognised.' : m)
    } finally {
      setBusy(false)
    }
  }

  const create = (e: FormEvent) => run(e, async () => (await request<{ api_key: string }>('/tenants', null,
    { method: 'POST', body: JSON.stringify({ name: name.trim() || 'sandbox', seed: 42 }) })).api_key)
  const use = (e: FormEvent) => run(e, async () => {
    await request('/tenant', key.trim())                  // keep the key only once the server accepts it
    return key.trim()
  })

  return (
    <div className="landing">
      <Backdrop />
      <div className="landing-inner">
        <section className="hero">
          <span className="eyebrow"><span className="live-dot" /> Graph Hacks 2026 · incident response</span>
          <h1>The on-call crew that <span className="grad">remembers</span>.</h1>
          <p className="lede">Four agents triage, investigate, fix and write up production incidents. Everything they learn
            goes into a graph, so the second time is faster than the first, and every decision links back to its evidence.</p>
          <ul className="features">
            {FEATURES.map(([icon, title, text]) => (
              <li key={title}><span className="feat-icon"><Icon name={icon} size={20} /></span><div><b>{title}</b><p>{text}</p></div></li>
            ))}
          </ul>
          <button className="ghost" onClick={onBenchmarks}><Icon name="chart" size={16} /> See the benchmarks first</button>
        </section>

        <section className="auth">
          <form onSubmit={create} className="glass auth-card primary-card">
            <h2>Spin up a sandbox</h2>
            <p className="muted">A private fictional fintech, Kestrel Pay: 40 services, its own graph. Nothing is shared with other visitors.</p>
            <label>Name<input value={name} onChange={(e) => setName(e.target.value)} maxLength={40} placeholder="my-sandbox" /></label>
            <button className="cta" disabled={busy}><Icon name="play" size={16} /> {busy ? 'Seeding the graph…' : 'Create sandbox'}</button>
          </form>
          <form onSubmit={use} className="glass auth-card">
            <h2>I have a tenant key</h2>
            <label>Key<input value={key} onChange={(e) => setKey(e.target.value)} placeholder="hk_…" autoComplete="off" spellCheck={false} /></label>
            <button className="btn" disabled={busy || !key.trim()}>Open console <Icon name="arrow" size={16} /></button>
          </form>
          {error && <p className="error" role="alert">{error}</p>}
        </section>
      </div>
    </div>
  )
}
