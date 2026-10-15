import { useEffect, useMemo, useState } from 'react'
import { API, ApiError, Catalog, Explain, IncidentRow, IncidentView, request, Trace } from '../api'
import { useIncidentStream, useNow, usePoll } from '../hooks'
import { buildTimeline } from '../timeline'
import GraphPanel from './GraphPanel'
import IncidentList from './IncidentList'
import InjectForm, { InjectRequest } from './InjectForm'
import Timeline, { AgentChip } from './Timeline'

const CANCEL_AFTER_S = 60
const money = (v: number | null | undefined) => (v == null ? 'n/a' : `$${v.toFixed(4)}`)

export default function Console({ tkey, onSignOut }: { tkey: string; onSignOut: () => void }) {
  const [catalog, setCatalog] = useState<Catalog | null>(null)
  const [rows, setRows] = useState<IncidentRow[] | null>(null)
  const [selected, setSelected] = useState<string | null>(null)
  const [viewRaw, setView] = useState<IncidentView | null>(null)
  const [traceRaw, setTrace] = useState<Trace | null>(null)
  const [actionId, setActionId] = useState<string | null>(null)
  const [explain, setExplain] = useState<Explain | null>(null)
  const [explainLoading, setExplainLoading] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [graphOpen, setGraphOpen] = useState(false)

  // Never show another incident's header for the render between a click and the reset effect.
  const view = viewRaw?.id === selected ? viewRaw : null
  const trace = traceRaw?.incident.id === selected ? traceRaw : null
  const { events, state: streamState, startedAt } = useIncidentStream(tkey, selected)
  const entries = useMemo(() => buildTimeline(events), [events])
  const ended = events.some((e) => e.event === 'stream_end')     // not `resolved`: the Scribe still runs after it
  const running = !!selected && !ended && streamState !== 'error'
  const now = useNow(running)
  const elapsed = Math.round((now - startedAt) / 1000)

  const fail = (e: unknown) => {
    if (e instanceof ApiError && e.status === 401) return onSignOut()      // the stored key is no longer valid
    setError(e instanceof Error ? e.message : String(e))
  }

  useEffect(() => {
    request<Catalog>('/catalog', tkey).then(setCatalog, fail)
  }, [tkey])

  const refreshList = () => request<{ incidents: IncidentRow[] }>('/incidents?limit=50', tkey).then((r) => setRows(r.incidents), fail)
  const anyActive = !rows || rows.some((r) => r.status === 'queued' || r.status === 'running')
  usePoll(refreshList, anyActive ? 2500 : 10000, true, [tkey])

  // The selected incident's header: polled while running, fetched once more when the stream ends.
  const loadView = async () => { if (selected) await request<IncidentView>(`/incidents/${selected}`, tkey).then(setView, fail) }
  useEffect(() => { setView(null); setTrace(null); setActionId(null); setExplain(null) }, [selected])
  usePoll(loadView, 2500, !!selected && running, [selected, tkey])
  useEffect(() => {
    if (!selected || running) return
    void loadView()
    request<Trace>(`/incidents/${selected}/trace`, tkey).then(setTrace, () => setTrace(null))
    void refreshList()
  }, [selected, running])   // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    setExplain(null)
    if (!actionId) return
    let live = true
    setExplainLoading(true)
    request<Explain>(`/actions/${actionId}/explain`, tkey).then(
      (x) => live && (setExplain(x), setExplainLoading(false)),
      (e) => live && (fail(e), setExplainLoading(false)),
    )
    return () => { live = false }
  }, [actionId, tkey])

  const submit = async (req: InjectRequest) => {
    setBusy(true)
    setError(null)
    try {
      const r = await request<{ incident_id: string }>('/incidents', tkey, { method: 'POST', body: JSON.stringify(req) })
      setSelected(r.incident_id)
      await refreshList()
    } catch (e) {
      const status = e instanceof ApiError ? e.status : 0
      setError(status === 429 ? `${(e as Error).message}. Try again later.` : status === 503 ? `Cannot start: ${(e as Error).message}` : (e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const cancel = () => selected && request(`/incidents/${selected}/cancel`, tkey, { method: 'POST' }).catch(fail)

  const downloadTrace = async () => {
    if (!selected) return
    const r = await fetch(`${API}/incidents/${selected}/trace?format=md`, { headers: { 'X-Tenant-Key': tkey } })
    if (!r.ok) return fail(new Error('could not load the trace'))
    window.open(URL.createObjectURL(await r.blob()), '_blank', 'noopener')
  }

  const origin = view?.services.find((s) => s.role === 'origin')?.name ?? null
  const status = view?.status ?? rows?.find((r) => r.id === selected)?.status ?? (running ? 'running' : '')
  const failed = status === 'failed'
  const out = trace?.outcome

  return (
    <div className={`console${graphOpen ? ' graph-open' : ''}`}>
      <aside className="col-left">
        <InjectForm catalog={catalog} busy={busy} onSubmit={submit} />
        {error && <p className="error" role="alert">{error} <button className="link" onClick={() => setError(null)}>dismiss</button></p>}
        <IncidentList rows={rows} selected={selected} onSelect={setSelected} catalog={catalog} busy={busy} onSubmit={submit} />
      </aside>

      <main className="col-center">
        {!selected ? (
          <div className="placeholder"><h2>Select or inject an incident</h2>
            <p className="muted">The crew's turns appear here live: which agent acted, with what tool, and how it went. Click any action to see the graph path behind it.</p></div>
        ) : (
          <>
            <header className="inc-head">
              <div>
                <h2>{(view?.title ?? rows?.find((r) => r.id === selected)?.title ?? 'incident').replace(/_/g, ' ')}</h2>
                <div className="meta">
                  <span className={`badge st-${status}`}>{status || '…'}</span>
                  {view?.severity && <span className="badge">sev {view.severity}</span>}
                  {view?.mode && <span className="badge">memory: {view.mode}</span>}
                  {out?.fast_path && <span className="badge fast" title="Followed a playbook from memory">fast path</span>}
                  {out?.fast_path_declined && <span className="badge warn" title="The Remediator checked the evidence and the playbook did not apply">fast path declined</span>}
                  {out?.unconfirmed_root_cause && <span className="badge warn">no confirmed root cause</span>}
                  <span className="muted">{view?.steps ?? 0} steps · {(view?.tokens ?? 0).toLocaleString()} tokens · {money(trace?.cost.total_usd ?? view?.cost_usd)}</span>
                </div>
              </div>
              <div className="head-actions">
                {streamState === 'reconnecting' && <span className="badge warn">reconnecting…</span>}
                {running && <span className="muted">{elapsed}s</span>}
                {running && elapsed >= CANCEL_AFTER_S && <button className="danger" onClick={cancel}>Cancel</button>}
                {!running && <button className="link" onClick={downloadTrace}>trace (markdown)</button>}
                <button className="link graph-toggle" onClick={() => setGraphOpen((v) => !v)}>{graphOpen ? 'Timeline' : 'Graph'}</button>
              </div>
            </header>

            {failed && (
              <div className="banner bad" role="alert">
                <b>The crew could not resolve this.</b> {view?.reason ? `Reason: ${view.reason.replace(/_/g, ' ')}. ` : ''}
                {out?.escalated_to ? <>Escalated to <b>{out.escalated_to}</b>. </> : 'Escalating to the on-call owner. '}
                <button className="link" onClick={downloadTrace}>Read the trace</button>
              </div>
            )}

            {trace && trace.memory_used.length > 0 && (
              <div className="memory-used">
                <b>Remembered</b>
                {trace.memory_used.slice(0, 3).map((m) => (
                  <span key={m.incident} className="chip" title={`matched by ${m.basis}`}>
                    {m.title || m.incident} · {m.score.toFixed(2)}{m.playbook_name ? ` · ${m.playbook_name}` : ''}
                  </span>
                ))}
              </div>
            )}

            {view?.hypotheses && view.hypotheses.length > 0 && (
              <div className="hyps">
                {view.hypotheses.slice(0, 3).map((h) => (
                  <span key={h.id} className={`hyp ${h.status}`} title={h.status}>{Math.round(h.score * 100)}% {h.text}</span>
                ))}
              </div>
            )}

            {streamState === 'error' && <p className="error">Could not open the live stream for this incident.</p>}
            {entries.length === 0 && streamState !== 'error' && <p className="muted pad">Waiting for the crew to start{running ? '…' : '.'}</p>}
            <Timeline entries={entries} running={running} selectedAction={actionId} onSelectAction={(id) => { setActionId(id); setGraphOpen(true) }} />
            {view && ended && view.root_causes.length > 0 && (
              <p className="muted pad">Root cause confirmed by <AgentChip agent={view.root_causes[0].confirmed_by} /> on {view.root_causes[0].label}.</p>
            )}
          </>
        )}
      </main>

      <section className="col-right">
        <GraphPanel tkey={tkey} center={origin} explain={explain} explainLoading={explainLoading} onClear={() => setActionId(null)} />
      </section>
    </div>
  )
}
