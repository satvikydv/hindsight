/** Server-Sent Events over fetch, so the tenant key travels in a header instead of a URL (which logs can capture). */

export interface SseEvent {
  id: number
  event: string
  data: any
}

/** Split a buffer into complete events plus the unfinished tail. Keepalive comments and malformed blocks are skipped. */
export function parseSSE(buffer: string): { events: SseEvent[]; rest: string } {
  const blocks = buffer.split('\n\n')
  const rest = blocks.pop() ?? ''
  const events: SseEvent[] = []
  for (const block of blocks) {
    let id = NaN
    let event = ''
    const data: string[] = []
    for (const line of block.split('\n')) {
      if (line.startsWith(':')) continue
      const at = line.indexOf(':')
      if (at < 0) continue
      const field = line.slice(0, at)
      const value = line.slice(at + 1).replace(/^ /, '')
      if (field === 'id') id = Number(value)
      else if (field === 'event') event = value
      else if (field === 'data') data.push(value)
    }
    if (!event || Number.isNaN(id)) continue
    try {
      events.push({ id, event, data: JSON.parse(data.join('\n')) })
    } catch {
      /* a malformed payload must not kill the stream */
    }
  }
  return { events, rest }
}

const SILENCE_MS = 35_000

export type StreamState ='connecting' | 'open' | 'reconnecting' | 'ended' | 'error'

const sleep = (ms: number, signal: AbortSignal) =>
  new Promise<void>((resolve) => {
    const t = setTimeout(resolve, ms)
    signal.addEventListener('abort', () => (clearTimeout(t), resolve()), { once: true })
  })

/**
 * Follow an incident until its `stream_end`. A dropped connection reconnects with Last-Event-ID, so nothing is shown
 * twice and nothing is missed (the server rebuilds the log from the graph when its memory of the run is gone).
 */
export function streamIncident(
  url: string,
  key: string,
  onEvent: (e: SseEvent) => void,
  onState: (s: StreamState) => void,
  signal: AbortSignal,
): void {
  let last = 0
  void (async () => {
    let attempt = 0
    while (!signal.aborted) {
      // The server sends a keepalive every ~15 s, so a silent connection is a dead one even if it never closed:
      // abort it and reconnect from the last event seen.
      const attemptCtl = new AbortController()
      const stop = () => attemptCtl.abort()
      signal.addEventListener('abort', stop, { once: true })
      let watchdog = setTimeout(stop, SILENCE_MS)
      const alive = () => { clearTimeout(watchdog); watchdog = setTimeout(stop, SILENCE_MS) }
      try {
        onState(attempt ? 'reconnecting' : 'connecting')
        const r = await fetch(url, { headers: { 'X-Tenant-Key': key, 'Last-Event-ID': String(last) }, signal: attemptCtl.signal })
        if (!r.ok || !r.body) {
          if (r.status < 500 && r.status !== 429) return onState('error')   // 401/404: retrying cannot help
          throw new Error(`stream ${r.status}`)
        }
        onState('open')
        attempt = 0
        const reader = r.body.getReader()
        const decoder = new TextDecoder()
        let buf = ''
        for (;;) {
          const { done, value } = await reader.read()
          if (done) break
          alive()
          buf += decoder.decode(value, { stream: true }).replace(/\r\n/g, '\n')
          const parsed = parseSSE(buf)
          buf = parsed.rest
          for (const e of parsed.events) {
            if (e.id <= last) continue
            last = e.id
            onEvent(e)
            if (e.event === 'stream_end') return onState('ended')
          }
        }
      } catch {
        if (signal.aborted) return
      } finally {
        clearTimeout(watchdog)
        signal.removeEventListener('abort', stop)
      }
      onState('reconnecting')
      await sleep(Math.min(1000 * 2 ** attempt++, 8000), signal)
    }
  })()
}
