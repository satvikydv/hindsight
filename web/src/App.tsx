import { lazy, Suspense, useState } from 'react'
import { ApiError, Health, keyStore, request } from './api'
import Console from './components/Console'
import Icon from './components/Icons'
import Login from './components/Login'
import Playbooks from './components/Playbooks'
import { usePoll } from './hooks'

const Benchmarks = lazy(() => import('./components/Benchmarks'))   // keeps the charting library out of the first load

type Tab = 'console' | 'playbooks' | 'benchmarks'
const TABS: [Tab, string, string][] = [['console', 'Console', 'siren'], ['playbooks', 'Playbooks', 'book'], ['benchmarks', 'Benchmarks', 'chart']]

export function Logo() {
  return (
    <svg width="26" height="26" viewBox="0 0 32 32" aria-hidden>
      <defs>
        <linearGradient id="lg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stopColor="#4fd1ff" /><stop offset="1" stopColor="#a78bfa" /></linearGradient>
      </defs>
      <path d="M3 16s5-9 13-9 13 9 13 9-5 9-13 9S3 16 3 16z" fill="none" stroke="url(#lg)" strokeWidth="2.2" />
      <circle cx="16" cy="16" r="4.5" fill="url(#lg)" />
      <path d="M16 11.5a4.5 4.5 0 0 0-4.5 4.5" stroke="#07090d" strokeWidth="1.6" fill="none" />
    </svg>
  )
}

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
  const accept = (k: string) => { keyStore.set(k); setKey(k); setTab('console') }
  const needsKey = tab !== 'benchmarks'

  return (
    <>
      <nav className="topbar">
        <span className="brand"><Logo /><span>Hindsight</span></span>
        <div className="tabs" role="tablist">
          {TABS.map(([id, name, icon]) => (
            <button key={id} role="tab" aria-selected={tab === id} className={tab === id ? 'on' : ''} onClick={() => setTab(id)}>
              <Icon name={icon} size={15} /><span>{name}</span>
            </button>
          ))}
        </div>
        <span className="spacer" />
        <span className="status-pills hide-sm">
          <span className={`pill ${down ? 'bad' : health ? 'ok' : ''}`} title="FalkorDB"><i />graph {down ? 'down' : health ? 'live' : '…'}</span>
          {health && <span className={`pill ${health.llm_key_present ? 'ok' : 'warn'}`} title="LLM key on the server"><i />llm</span>}
          {health && <span className="pill dim">v{health.version}</span>}
        </span>
        {key && <button className="icon-btn" onClick={signOut} title="Sign out" aria-label="Sign out"><Icon name="logout" size={17} /></button>}
      </nav>
      {down && <div className="banner bad" role="alert"><Icon name="x" size={16} /> {down}</div>}
      {health && !health.llm_key_present && !down && (
        <div className="banner warn" role="status">The server has no LLM key, so new incidents cannot start. Existing ones and saved benchmarks still work.</div>
      )}
      {needsKey && !key ? <Login onKey={accept} onBenchmarks={() => setTab('benchmarks')} /> :
        tab === 'console' && key ? <Console tkey={key} onSignOut={signOut} /> :
        tab === 'playbooks' && key ? <Playbooks tkey={key} /> :
        <Suspense fallback={<p className="muted pad">Loading…</p>}><Benchmarks /></Suspense>}
    </>
  )
}
