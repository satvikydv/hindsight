import { describe, expect, it, vi } from 'vitest'
import { parseSSE, streamIncident } from './sse'
import { argsLine, buildTimeline, pipeline, summarize, toneOf } from './timeline'

const frame = (id: number, event: string, data: object) => `id: ${id}\nevent: ${event}\ndata: ${JSON.stringify(data)}\n\n`

describe('parseSSE', () => {
  it('returns complete events and keeps the unfinished tail', () => {
    const half = frame(2, 'tool_called', { tool: 'x' }).slice(0, 20)
    const { events, rest } = parseSSE(frame(1, 'agent_started', { agent: 'triage' }) + half)
    expect(events).toEqual([{ id: 1, event: 'agent_started', data: { agent: 'triage' } }])
    expect(rest).toBe(half)
  })

  it('completes a split event once the rest arrives', () => {
    const whole = frame(5, 'handoff', { from: 'a', to: 'b' })
    const first = parseSSE(whole.slice(0, 15))
    expect(first.events).toEqual([])
    const second = parseSSE(first.rest + whole.slice(15))
    expect(second.events.map((e) => e.id)).toEqual([5])
  })

  it('skips keepalive comments and malformed payloads without losing later events', () => {
    const text = ': keepalive\n\n' + 'id: 1\nevent: x\ndata: {nope\n\n' + frame(2, 'ok', { fine: true })
    expect(parseSSE(text).events).toEqual([{ id: 2, event: 'ok', data: { fine: true } }])
  })
})

describe('buildTimeline', () => {
  const ev = (id: number, event: string, data: object) => ({ id, event, data })

  it('merges a tool_result into its pending call and keeps handoffs in place', () => {
    const t = buildTimeline([
      ev(1, 'agent_started', { agent: 'triage' }),
      ev(2, 'tool_called', { agent: 'triage', tool: 'blast_radius', args: { service: 'a' } }),
      ev(3, 'tool_result', { agent: 'triage', tool: 'blast_radius', outcome: 'success', result: { summary: 'ok' }, action_id: 'A1' }),
      ev(4, 'handoff', { from: 'triage', to: 'investigator', summary: 'go' }),
    ])
    expect(t.map((e) => e.kind)).toEqual(['agent', 'call', 'handoff'])
    expect(t[1]).toMatchObject({ pending: false, outcome: 'success', actionId: 'A1', args: { service: 'a' } })
  })

  it('leaves a call pending until its result arrives', () => {
    const t = buildTimeline([ev(1, 'tool_called', { agent: 'investigator', tool: 'get_logs', args: {} })])
    expect(t[0]).toMatchObject({ kind: 'call', pending: true })
  })

  it('pairs results with the right call when the same tool repeats', () => {
    const t = buildTimeline([
      ev(1, 'tool_called', { agent: 'r', tool: 'restart', args: { s: 'a' } }),
      ev(2, 'tool_result', { agent: 'r', tool: 'restart', outcome: 'no_effect', result: {}, action_id: 'A1' }),
      ev(3, 'tool_called', { agent: 'r', tool: 'restart', args: { s: 'b' } }),
    ])
    expect(t[0]).toMatchObject({ pending: false, outcome: 'no_effect' })
    expect(t[1]).toMatchObject({ pending: true, args: { s: 'b' } })
  })

  it('records the ending and shows a result whose call was never seen (resumed stream)', () => {
    const t = buildTimeline([
      ev(7, 'tool_result', { agent: 'scribe', tool: 'write_postmortem', outcome: 'success', result: {}, action_id: 'A9' }),
      ev(8, 'failed', { reason: 'step_budget' }),
    ])
    expect(t[0]).toMatchObject({ kind: 'call', pending: false, actionId: 'A9' })
    expect(t[1]).toMatchObject({ kind: 'end', status: 'failed', reason: 'step_budget' })
  })
})

describe('pipeline', () => {
  const ev = (id: number, event: string, data: object) => ({ id, event, data })
  const triage = [ev(1, 'agent_started', { agent: 'triage' }), ev(2, 'tool_called', { agent: 'triage', tool: 't', args: {} })]

  it('marks the running agent active and earlier ones done', () => {
    const p = pipeline(buildTimeline([...triage, ev(3, 'agent_started', { agent: 'investigator' })]), true)
    expect(p.triage.state).toBe('done')
    expect(p.investigator.state).toBe('active')
    expect(p.remediator.state).toBe('idle')
  })

  it('shows the fast path as a skipped investigator', () => {
    const p = pipeline(buildTimeline([...triage, ev(3, 'agent_started', { agent: 'remediator' })]), true)
    expect(p.investigator.state).toBe('skipped')
    expect(p.remediator.state).toBe('active')
  })

  it('counts bounce-backs and settles to done when the incident ends', () => {
    const p = pipeline(buildTimeline([
      ...triage, ev(3, 'agent_started', { agent: 'investigator' }), ev(4, 'agent_started', { agent: 'remediator' }),
      ev(5, 'agent_started', { agent: 'investigator' }), ev(6, 'agent_started', { agent: 'scribe' }),
    ]), false)
    expect(p.investigator).toMatchObject({ state: 'done', turns: 2 })
    expect(Object.values(p).every((s) => s.state === 'done')).toBe(true)
  })
})

describe('display helpers', () => {
  it('maps outcomes to a tone, harmful and errors being the loud ones', () => {
    expect(toneOf('success')).toBe('ok')
    expect(toneOf('no_effect')).toBe('warn')
    expect(toneOf('harmful')).toBe('bad')
    expect(toneOf('error')).toBe('bad')
    expect(toneOf(undefined, true)).toBe('pending')
  })

  it('summarises results in one short line', () => {
    expect(summarize({ summary: 'rolled back v2' })).toBe('rolled back v2')
    expect(summarize({ error: 'boom' })).toBe('error: boom')
    expect(summarize({ truncated: true, preview: '{"a":1' })).toBe('{"a":1')
    expect(summarize({ x: 'y'.repeat(500) }).length).toBeLessThanOrEqual(160)
    expect(summarize(null)).toBe('')
    expect(summarize({ detail: 'rollback applied', target: 'x' })).toBe('rollback applied')
    expect(summarize({ service: 'a', affected: [1, 2], root_cause: { type: 'Deploy' }, none: null }))
      .toBe('service: a · affected: 2 · root cause: type: Deploy')
  })

  it('formats args compactly', () => {
    expect(argsLine({ service: 'checkout-api', n: 2 })).toBe('service=checkout-api, n=2')
    expect(argsLine(undefined)).toBe('')
  })
})

describe('streamIncident', () => {
  it('reconnects a connection that went silent, resuming after the last event', async () => {
    vi.useFakeTimers()
    const enc = new TextEncoder()
    const seenIds: string[] = []
    // like a browser, aborting the request errors its body stream
    const body = (sig: AbortSignal, chunks: string[], close: boolean) => new ReadableStream({ start(c) {
      chunks.forEach((x) => c.enqueue(enc.encode(x)))
      if (close) c.close()
      else sig.addEventListener('abort', () => c.error(new DOMException('aborted', 'AbortError')))
    } })
    const responses = [
      (sig: AbortSignal) => body(sig, [frame(1, 'agent_started', { agent: 'triage' })], false),   // then silence, never closes
      (sig: AbortSignal) => body(sig, [frame(2, 'stream_end', {})], true),
    ]
    vi.stubGlobal('fetch', vi.fn(async (_url: string, init: RequestInit) => {
      seenIds.push((init.headers as Record<string, string>)['Last-Event-ID'])
      return new Response(responses.shift()!(init.signal!), { status: 200 })
    }))
    const events: number[] = []
    const states: string[] = []
    const ctl = new AbortController()
    streamIncident('/s', 'k', (e) => events.push(e.id), (s) => states.push(s), ctl.signal)
    await vi.advanceTimersByTimeAsync(1000)
    expect(events).toEqual([1])
    await vi.advanceTimersByTimeAsync(40_000)     // silence past the watchdog, then the 1 s backoff
    expect(events).toEqual([1, 2])
    expect(seenIds).toEqual(['0', '1'])
    expect(states).toContain('reconnecting')
    expect(states.at(-1)).toBe('ended')
    vi.unstubAllGlobals()
    vi.useRealTimers()
  })
})
