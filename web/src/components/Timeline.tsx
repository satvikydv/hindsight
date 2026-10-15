import { useEffect, useRef, useState } from 'react'
import { argsLine, Entry, summarize, toneOf } from '../timeline'
import { useNow } from '../hooks'

const label = (s: string) => s.charAt(0).toUpperCase() + s.slice(1)

export function AgentChip({ agent }: { agent: string | null | undefined }) {
  const a = agent || 'system'
  return <span className={`chip agent-${a}`}>{label(a)}</span>
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
              <li key={e.key} className="tl-agent">
                <AgentChip agent={e.agent} />
                <span className="muted">took the turn</span>
              </li>
            )
          case 'handoff':
            return (
              <li key={e.key} className="tl-handoff">
                <div className="rule" />
                <div className="handoff-body">
                  <span><AgentChip agent={e.from} /> <span aria-hidden>→</span> <AgentChip agent={e.to} /></span>
                  {e.summary && <p className="muted">{e.summary}</p>}
                </div>
              </li>
            )
          case 'state':
            return <li key={e.key} className="tl-state">state: {e.state.toLowerCase()}</li>
          case 'end':
            return (
              <li key={e.key} className={`tl-end ${e.status}`}>
                {e.status === 'resolved' ? 'Resolved' : 'Failed'}{e.reason ? `: ${e.reason.replace(/_/g, ' ')}` : ''}
              </li>
            )
          case 'call': {
            const tone = toneOf(e.outcome, e.pending)
            const selectable = !!e.actionId
            const expanded = open.has(e.key)
            const secs = e.pending ? Math.max(0, Math.round((now - (seen.current.get(e.key) ?? now)) / 1000)) : 0
            return (
              <li key={e.key} className={`tl-call tone-${tone}${selectable && selectedAction === e.actionId ? ' selected' : ''}`}>
                <button
                  className="call-main"
                  disabled={!selectable}
                  onClick={() => e.actionId && onSelectAction(e.actionId)}
                  aria-label={`${e.tool}: show why`}
                  title={selectable ? 'Show the graph path behind this action' : 'Waiting for the result'}
                >
                  <AgentChip agent={e.agent} />
                  <span className="tool">{e.tool}</span>
                  <span className="args muted">{argsLine(e.args)}</span>
                  <span className={`badge tone-${tone}`}>
                    {e.pending ? <><span className="spinner" aria-hidden /> running {secs}s</> : (e.outcome ?? 'done').replace('_', ' ')}
                  </span>
                </button>
                {!e.pending && <p className="summary">{summarize(e.result)}</p>}
                {!e.pending && e.result != null && (
                  <button className="link" onClick={() => toggle(e.key)}>{expanded ? 'hide raw result' : 'raw result'}</button>
                )}
                {expanded && <pre className="raw">{JSON.stringify(e.result, null, 2)}</pre>}
              </li>
            )
          }
        }
      })}
      <div ref={bottom} />
    </ol>
  )
}
