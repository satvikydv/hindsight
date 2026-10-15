/** Turn the raw event stream into the entries the timeline draws. Pure, so it is unit-tested. */

export interface Ev {
  id: number
  event: string
  data: any
}

export type Entry =
  | { kind: 'agent'; key: string; agent: string }
  | {
      kind: 'call'
      key: string
      agent: string
      tool: string
      args: any
      pending: boolean
      outcome?: string
      result?: any
      actionId?: string
    }
  | { kind: 'handoff'; key: string; from: string; to: string; summary: string }
  | { kind: 'state'; key: string; state: string }
  | { kind: 'end'; key: string; status: 'resolved' | 'failed'; reason: string }

export function buildTimeline(events: Ev[]): Entry[] {
  const out: Entry[] = []
  for (const e of events) {
    const d = e.data ?? {}
    const key = `e${e.id}`
    switch (e.event) {
      case 'agent_started':
        out.push({ kind: 'agent', key, agent: d.agent })
        break
      case 'tool_called':
        out.push({ kind: 'call', key, agent: d.agent, tool: d.tool, args: d.args, pending: true })
        break
      case 'tool_result': {
        // fill the newest still-pending call of the same agent and tool
        let i = out.length - 1
        while (i >= 0) {
          const c = out[i]
          if (c.kind === 'call' && c.pending && c.agent === d.agent && c.tool === d.tool) break
          i--
        }
        if (i >= 0) {
          const c = out[i] as Extract<Entry, { kind: 'call' }>
          out[i] = { ...c, pending: false, outcome: d.outcome, result: d.result, actionId: d.action_id }
        } else {
          out.push({ kind: 'call', key, agent: d.agent, tool: d.tool, args: undefined, pending: false,
                     outcome: d.outcome, result: d.result, actionId: d.action_id })
        }
        break
      }
      case 'handoff':
        out.push({ kind: 'handoff', key, from: d.from, to: d.to, summary: d.summary })
        break
      case 'state_changed':
        out.push({ kind: 'state', key, state: d.state })
        break
      case 'resolved':
      case 'failed':
        out.push({ kind: 'end', key, status: e.event, reason: d.reason ?? '' })
        break
    }
  }
  return out
}

export type Tone = 'ok' | 'warn' | 'bad' | 'pending'

export function toneOf(outcome: string | undefined, pending = false): Tone {
  if (pending) return 'pending'
  if (outcome === 'harmful' || outcome === 'error') return 'bad'
  if (outcome === 'no_effect') return 'warn'
  return 'ok'
}

const clip = (s: string, n = 160) => (s.length > n ? s.slice(0, n - 1) + '…' : s)

/** One line for a tool result. The full payload is one click away, so this only has to be recognisable. */
export function summarize(result: any): string {
  if (result == null) return ''
  if (typeof result === 'string') return clip(result)
  if (typeof result.summary === 'string') return clip(result.summary)
  if (result.error) return clip('error: ' + String(result.error))
  if (result.truncated && typeof result.preview === 'string') return clip(result.preview)
  if (typeof result.detail === 'string') return clip(result.detail)
  return clip(compact(result))
}

/** `key: value` pairs for flat results, with nested data reduced to its size, so a line reads like a sentence. */
function compact(result: any): string {
  if (Array.isArray(result)) return `${result.length} item${result.length === 1 ? '' : 's'}`
  if (typeof result !== 'object') return String(result)
  return Object.entries(result)
    .map(([k, v]) => {
      const key = k.replace(/_/g, ' ')
      if (v == null) return null
      if (Array.isArray(v)) return `${key}: ${v.length}`
      if (typeof v === 'object') return `${key}: ${compact(v)}`
      return `${key}: ${v}`
    })
    .filter(Boolean)
    .join(' · ')
}

export function argsLine(args: any): string {
  if (!args || typeof args !== 'object') return ''
  const parts = Object.entries(args).map(([k, v]) => `${k}=${typeof v === 'string' ? v : JSON.stringify(v)}`)
  return clip(parts.join(', '), 90)
}
