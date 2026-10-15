import { Stage, StageInfo, STAGES } from '../timeline'
import Icon, { AGENT_ICON } from './Icons'

const ROLE: Record<Stage, string> = {
  triage: 'scope it',
  investigator: 'find cause',
  remediator: 'fix it',
  scribe: 'learn',
}

/** The crew as a relay: who has the baton, who is done, who the fast path skipped. */
export default function Pipeline({ stages }: { stages: Record<Stage, StageInfo> }) {
  return (
    <ol className="pipeline" aria-label="Agent progress">
      {STAGES.map((s, i) => {
        const st = stages[s]
        return (
          <li key={s} className={`stage stage-${st.state} agent-${s}`} aria-current={st.state === 'active' ? 'step' : undefined}
              title={st.state === 'skipped' ? 'Memory already knew the fix, so the fast path skipped this agent' : undefined}>
            {i > 0 && <span className={`wire${st.state !== 'idle' ? ' lit' : ''}`} aria-hidden />}
            <span className="orb">
              <Icon name={st.state === 'done' ? 'check' : st.state === 'skipped' ? 'skip' : AGENT_ICON[s]} size={16} />
            </span>
            <span className="stage-text">
              <b>{s}</b>
              <small>
                {st.state === 'skipped' ? 'skipped · memory' : st.state === 'active' ? 'working…' : ROLE[s]}
                {st.turns > 1 && <em> · ×{st.turns}</em>}
              </small>
            </span>
          </li>
        )
      })}
    </ol>
  )
}

export function Ring({ value, size = 44, label }: { value: number; size?: number; label?: string }) {
  const r = (size - 6) / 2
  const c = 2 * Math.PI * r
  const v = Math.max(0, Math.min(1, value))
  return (
    <svg className="ring" width={size} height={size} viewBox={`0 0 ${size} ${size}`} role="img" aria-label={label ?? `${Math.round(v * 100)}%`}>
      <circle cx={size / 2} cy={size / 2} r={r} className="ring-bg" />
      <circle cx={size / 2} cy={size / 2} r={r} className="ring-fg" strokeDasharray={`${c * v} ${c}`}
              transform={`rotate(-90 ${size / 2} ${size / 2})`} />
      <text x="50%" y="50%" dominantBaseline="central" textAnchor="middle">{Math.round(v * 100)}</text>
    </svg>
  )
}
