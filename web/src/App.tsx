import { lazy, Suspense, useState } from 'react'
import { ApiError, Health, keyStore, request } from './api'
import Console from './components/Console'
import Login from './components/Login'
import Playbooks from './components/Playbooks'
import { usePoll } from './hooks'

const Benchmarks = lazy(() => import('./components/Benchmarks'))   // keeps the charting library out of the first load

type Tab = 'console' | 'playbooks' | 'benchmarks'
const TABS: [Tab, string][] = [['console', 'Console'], ['playbooks', 'Playbooks'], ['benchmarks', 'Benchmarks']]

export default function App() {
  const [key, setKey] = useState<string | null>(keyStore.get())
  const [tab, setTab] = useState<Tab>('console')
  const [health, setHealth] = useState<Health | null>(null)
  const [down, setDown] = useState<string | null>(null)

  usePoll(async () => {
    try {
      setHealth(await request<Health>('/health', null))
      setDown(null)
    } catch (e) {
      setDown(e instanceof ApiError && e.status === 503 ? 'The graph database is unavailable. Nothing runs without it, and there is no fallback.' : (e as Error).message)
    }
  }, 10000)

  const signOut = () => { keyStore.clear(); setKey(null) }
  const accept = (k: string) => { keyStore.set(k); setKey(k) }
  const needsKey = tab !== 'benchmarks'

  return (
    <>
      <nav className="topbar">
        <span className="brand"><i aria-hidden />Hindsight</span>
        <div className="tabs" role="tablist">
          {TABS.map(([id, name]) => (
            <button key={id} role="tab" aria-selected={tab === id} className={tab === id ? 'on' : ''} onClick={() => setTab(id)}>{name}</button>
          ))}
        </div>
        <span className="spacer" />
        {health && <span className="muted hide-sm">v{health.version}</span>}
        {key && <button className="link" onClick={signOut}>Sign out</button>}
      </nav>
      {down && <div className="banner bad" role="alert">{down}</div>}
      {health && !health.llm_key_present && !down && (
        <div className="banner warn" role="status">The server has no LLM key, so new incidents cannot start. Existing ones and saved benchmarks still work.</div>
      )}
      {needsKey && !key ? <Login onKey={accept} /> :
        tab === 'console' && key ? <Console tkey={key} onSignOut={signOut} /> :
        tab === 'playbooks' && key ? <Playbooks tkey={key} /> :
        <Suspense fallback={<p className="muted pad">Loading…</p>}><Benchmarks /></Suspense>}
    </>
  )
}
