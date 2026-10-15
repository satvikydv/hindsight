import { useEffect, useMemo, useState } from 'react'
import { API, ApiError, Catalog, Explain, IncidentRow, IncidentView, request, Trace } from '../api'
import { useIncidentStream, useNow, usePoll } from '../hooks'
import { buildTimeline, pipeline, STAGES } from '../timeline'
import GraphPanel from './GraphPanel'
import Icon, { AGENT_ICON } from './Icons'
import IncidentList from './IncidentList'
import InjectForm, { InjectRequest } from './InjectForm'
import Pipeline, { Ring } from './Pipeline'
import Timeline, { AgentChip } from './Timeline'

const CANCEL_AFTER_S = 60
const money = (v: number | null | undefined) => (v == null ? 'n/a' : `$${v.toFixed(4)}`)
const fmtTime = (s: number) => (s >= 60 ? `${Math.floor(s / 60)}m ${s % 60}s` : `${s}s`)

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
  const stages = useMemo(() => pipeline(entries, running), [entries, running])
  const now = useNow(running)
  const elapsed = Math.round((now - startedAt) / 1000)

  const fail = (e: unknown) => {
    if (e instanceof ApiError && e.status === 401) return onSignOut()      // the stored key is no longer valid
    setError(e instanceof Error ? e.message : String(e))
  }

  useEffect(() => {
    request<Catalog>('/catalog', tkey).then(setCatalog, fail)
  }, [tkey])   // eslint-disable-line react-hooks/exhaustive-deps

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
  }, [actionId, tkey])   // eslint-disable-line react-hooks/exhaustive-deps

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
  const title = view?.title ?? rows?.find((r) => r.id === selected)?.title ?? 'incident'

  return (
    <div className={`console${graphOpen ? ' graph-open' : ''}`}>
      <aside className="col-left">
        <InjectForm catalog={catalog} busy={busy} onSubmit={submit} />
        {error && <p className="error toast" role="alert">{error} <button className="link" onClick={() => setError(null)}>dismiss</button></p>}
        <IncidentList rows={rows} selected={selected} onSelect={setSelected} catalog={catalog} busy={busy} onSubmit={submit} />
      </aside>

      <main className="col-center">
        {!selected ? (
          <div className="placeholder">
            <div className="placeholder-art" aria-hidden>
              {STAGES.map((a) => <span key={a} className={`orb agent-${a}`}><Icon name={AGENT_ICON[a]} size={18} /></span>)}
            </div>
            <h2>The crew is standing by</h2>
            <p className="muted">Trigger an incident on the left. Triage scopes it, the Investigator finds the cause, the Remediator
              fixes it and the Scribe writes down what was learned. Click any action to see why it was taken.</p>
          </div>
        ) : (
          <>
            <header className="inc-head">
              <div className="inc-head-top">
                <div className="inc-head-title">
                  <span className={`status-dot big st-${status}${running ? ' pulse' : ''}`} aria-hidden />
                  <div>
                    <h2>{title}</h2>
                    <div className="meta">
                      <span className={`tag st-${status}`}>{status || '…'}</span>
                      {view?.severity && <span className={`tag sev-${view.severity}`}>{view.severity}</span>}
                      {view?.mode && <span className={`tag mode-${view.mode}`}>memory · {view.mode}</span>}
                      {out?.fast_path && <span className="tag fast" title="Followed a playbook from memory"><Icon name="bolt" size={12} /> fast path</span>}
                      {out?.fast_path_declined && <span className="tag warn" title="The Remediator checked the evidence and the playbook did not apply">fast path declined</span>}
                      {out?.unconfirmed_root_cause && <span className="tag warn" title="Fixed, but no agent confirmed the root cause">cause unconfirmed</span>}
                      {streamState === 'reconnecting' && <span className="tag warn">reconnecting…</span>}
                    </div>
                  </div>
                </div>
                <div className="head-actions">
                  {running && elapsed >= CANCEL_AFTER_S && <button className="danger" onClick={cancel}><Icon name="x" size={14} /> Cancel</button>}
                  {!running && <button className="btn small" onClick={downloadTrace}><Icon name="doc" size={14} /> Trace</button>}
                  <button className="btn small graph-toggle" onClick={() => setGraphOpen((v) => !v)}><Icon name="graph" size={14} /> {graphOpen ? 'Timeline' : 'Graph'}</button>
                </div>
              </div>
              <div className="metrics">
                <Metric icon="steps" value={String(view?.steps ?? 0)} label="steps" />
                <Metric icon="token" value={(view?.tokens ?? 0).toLocaleString()} label="tokens" />
                <Metric icon="coin" value={money(trace?.cost.total_usd ?? view?.cost_usd)} label="LLM cost" />
                <Metric icon="clock" value={running ? fmtTime(elapsed) : trace ? (trace.cost.llm_ms < 1000 ? `${trace.cost.llm_ms} ms` : fmtTime(Math.round(trace.cost.llm_ms / 1000))) : '–'}
                        label={running ? 'elapsed' : 'model time'} />
              </div>
              <Pipeline stages={stages} />
            </header>

            {failed && (
              <div className="banner bad inline" role="alert">
                <Icon name="siren" size={18} />
                <div><b>The crew could not resolve this.</b> {view?.reason ? `Reason: ${view.reason.replace(/_/g, ' ')}. ` : ''}
                  {out?.escalated_to ? <>Escalated to <b>{out.escalated_to}</b>. </> : 'Escalating to the on-call owner. '}
                  <button className="link" onClick={downloadTrace}>Read the trace</button></div>
              </div>
            )}

            {trace && trace.memory_used.length > 0 && (
              <section className="recall-card">
                <div className="card-head"><Icon name="brain" size={16} /> <b>Hindsight recalled</b>
                  <span className="muted">similar past incidents, scored on graph structure and text</span></div>
                <div className="recall-list">
                  {trace.memory_used.slice(0, 3).map((m) => (
                    <div key={m.incident} className="recall-item">
                      <Ring value={m.score} label={`similarity ${m.score.toFixed(2)}`} />
                      <div><b>{m.title || m.incident}</b>
                        <small className="muted">matched by {m.basis}{m.playbook_name ? <> · playbook <em>{m.playbook_name}</em></> : null}</small></div>
                    </div>
                  ))}
                </div>
              </section>
            )}

            {view?.hypotheses && view.hypotheses.length > 0 && (
              <section className="hyp-card">
                <div className="card-head"><Icon name="search" size={16} /> <b>Hypotheses</b></div>
                {view.hypotheses.slice(0, 3).map((h) => (
                  <div key={h.id} className={`hyp ${h.status}`}>
                    <span className="hyp-bar"><i style={{ width: `${Math.round(h.score * 100)}%` }} /></span>
                    <span className="mono">{Math.round(h.score * 100)}%</span>
                    <span className="hyp-text">{h.text}</span>
                    <span className={`tag ${h.status === 'confirmed' ? 'ok' : h.status === 'rejected' ? 'dim' : ''}`}>{h.status}</span>
                  </div>
                ))}
              </section>
            )}

            {streamState === 'error' && <p className="error">Could not open the live stream for this incident.</p>}
            {entries.length === 0 && streamState !== 'error' && <p className="muted pad"><span className="spinner" /> Paging the crew…</p>}
            <Timeline entries={entries} running={running} selectedAction={actionId} onSelectAction={(id) => { setActionId(id); setGraphOpen(true) }} />
            {view && ended && view.root_causes.length > 0 && (
              <p className="muted pad">Root cause confirmed by <AgentChip agent={view.root_causes[0].confirmed_by} /> on a {view.root_causes[0].label}.</p>
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

function Metric({ icon, value, label }: { icon: string; value: string; label: string }) {
  return <div className="metric"><Icon name={icon} size={16} /><span><b>{value}</b><small>{label}</small></span></div>
}
