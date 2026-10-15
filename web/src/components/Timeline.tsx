import { useEffect, useRef, useState } from 'react'
import { argsLine, Entry, summarize, toneOf } from '../timeline'
import { useNow } from '../hooks'
import Icon, { AGENT_ICON } from './Icons'

const label = (s: string) => s.charAt(0).toUpperCase() + s.slice(1)

export function AgentChip({ agent }: { agent: string | null | undefined }) {
  const a = agent || 'system'
  return <span className={`chip agent-${a}`}><Icon name={AGENT_ICON[a] ?? 'bolt'} size={12} />{label(a)}</span>
}

interface Props {
  entries: Entry[]
  running: boolean
  selectedAction: string | null
  onSelectAction: (id: string) => void
}

export default function Timeline({ entries, running, selectedAction, onSelectAction }: Props) {
  const pending = entries.some((e) => e.kind === 'call' && e.pending)
  const now = useNow(pending)
  const seen = useRef(new Map<string, number>())          // when this page first saw a call start, for "running 12s"
  const bottom = useRef<HTMLDivElement>(null)
  const [open, setOpen] = useState<Set<string>>(new Set())

  for (const e of entries) if (e.kind === 'call' && e.pending && !seen.current.has(e.key)) seen.current.set(e.key, Date.now())

  useEffect(() => {
    if (running) bottom.current?.scrollIntoView({ block: 'end', behavior: 'smooth' })
  }, [entries.length, running])

  const toggle = (key: string) =>
    setOpen((prev) => {
      const next = new Set(prev)
      next.has(key) ? next.delete(key) : next.add(key)
      return next
    })

  return (
    <ol className="timeline">
      {entries.map((e) => {
        switch (e.kind) {
          case 'agent':
            return (
              <li key={e.key} className={`tl-agent agent-${e.agent}`}>
                <span className="rail-node big"><Icon name={AGENT_ICON[e.agent] ?? 'bolt'} size={14} /></span>
                <span className="agent-name">{label(e.agent)}</span><span className="muted">has the incident</span>
              </li>
            )
          case 'handoff':
            return (
              <li key={e.key} className="tl-handoff">
                <span className="rail-node baton" aria-hidden><Icon name="arrow" size={12} /></span>
                <div className="baton-card">
                  <div className="baton-head"><AgentChip agent={e.from} /><span className="baton-line" aria-hidden /><AgentChip agent={e.to} /></div>
                  {e.summary && <p>{e.summary}</p>}
                </div>
              </li>
            )
          case 'state':
            return null
          case 'end':
            return (
              <li key={e.key} className={`tl-end ${e.status}`}>
                <span className="rail-node end"><Icon name={e.status === 'resolved' ? 'check' : 'x'} size={14} /></span>
                <div>
                  <b>{e.status === 'resolved' ? 'Incident resolved' : 'Crew could not resolve it'}</b>
                  {e.reason && <span className="muted"> · {e.reason.replace(/_/g, ' ')}</span>}
                </div>
              </li>
            )
          case 'call': {
            const tone = toneOf(e.outcome, e.pending)
            const selectable = !!e.actionId
            const expanded = open.has(e.key)
            const secs = e.pending ? Math.max(0, Math.round((now - (seen.current.get(e.key) ?? now)) / 1000)) : 0
            return (
              <li key={e.key} className={`tl-call tone-${tone} agent-${e.agent}${selectable && selectedAction === e.actionId ? ' selected' : ''}`}>
                <span className="rail-node" aria-hidden />
                <div className="call-card">
                  <button className="call-main" disabled={!selectable} onClick={() => e.actionId && onSelectAction(e.actionId)}
                          aria-label={`${e.tool}: show why`} title={selectable ? 'Show the graph path behind this action' : 'Waiting for the result'}>
                    <span className="tool">{e.tool}</span>
                    <span className="args">{argsLine(e.args)}</span>
                    <span className={`outcome tone-${tone}`}>
                      {e.pending ? <><span className="spinner" aria-hidden /> {secs}s</> : (e.outcome ?? 'done').replace('_', ' ')}
                    </span>
                  </button>
                  {!e.pending && <p className="summary">{summarize(e.result)}</p>}
                  {!e.pending && (
                    <div className="call-foot">
                      {selectable && <button className="why-btn" onClick={() => onSelectAction(e.actionId!)}><Icon name="eye" size={13} /> why?</button>}
                      {e.result != null && <button className="link small" onClick={() => toggle(e.key)}>{expanded ? 'hide raw' : 'raw'}</button>}
                    </div>
                  )}
                  {expanded && <pre className="raw">{JSON.stringify(e.result, null, 2)}</pre>}
                </div>
              </li>
            )
          }
        }
      })}
      <div ref={bottom} />
    </ol>
  )
}
