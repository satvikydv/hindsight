import { FormEvent, useState } from 'react'
import { request } from '../api'

export default function Login({ onKey }: { onKey: (key: string) => void }) {
  const [name, setName] = useState('')
  const [key, setKey] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const create = async (e: FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      const t = await request<{ api_key: string }>('/tenants', null, { method: 'POST', body: JSON.stringify({ name: name.trim() || 'sandbox', seed: 42 }) })
      onKey(t.api_key)
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const use = async (e: FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await request('/tenant', key.trim())              // the key is only kept once the server has accepted it
      onKey(key.trim())
    } catch (err) {
      setError((err as Error).message === 'unknown tenant key' ? 'That key was not recognised.' : (err as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="login">
      <h1>Hindsight</h1>
      <p className="lede">An incident-response crew that remembers. Every fix it learns is stored in a graph, and every decision links back to the evidence.</p>
      <form onSubmit={create} className="card-form">
        <h2>Try it</h2>
        <p className="muted">Creates a private sandbox company with its own graph. Nothing is shared with other visitors.</p>
        <label>Sandbox name<input value={name} onChange={(e) => setName(e.target.value)} maxLength={40} placeholder="my-sandbox" /></label>
        <button className="primary" disabled={busy}>Create sandbox</button>
      </form>
      <form onSubmit={use} className="card-form">
        <h2>I have a key</h2>
        <label>Tenant key<input value={key} onChange={(e) => setKey(e.target.value)} placeholder="hk_…" autoComplete="off" spellCheck={false} /></label>
        <button disabled={busy || !key.trim()}>Open</button>
      </form>
      {error && <p className="error" role="alert">{error}</p>}
    </div>
  )
}
