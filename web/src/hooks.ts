import { useCallback, useEffect, useRef, useState } from 'react'
import { API } from './api'
import { streamIncident, StreamState } from './sse'
import type { Ev } from './timeline'

/** Run `fn` now and then every `ms` while `enabled`. The latest `fn` is always the one called. */
export function usePoll(fn: () => void | Promise<void>, ms: number, enabled = true, deps: unknown[] = []) {
  const ref = useRef(fn)
  ref.current = fn
  useEffect(() => {
    if (!enabled) return
    let live = true
    const tick = () => live && void ref.current()
    tick()
    const t = setInterval(tick, ms)
    return () => {
      live = false
      clearInterval(t)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ms, enabled, ...deps])
}

/** Seconds-resolution clock, only ticking while `active` (a idle page should not re-render every second). */
export function useNow(active: boolean): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!active) return
    setNow(Date.now())
    const t = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(t)
  }, [active])
  return now
}

/** The live event log of one incident, reconnecting on its own. Switching incidents starts a clean log. */
export function useIncidentStream(key: string, iid: string | null) {
  // The log is tagged with its incident so a render between a click and the reset never shows the previous one.
  const [log, setLog] = useState<{ iid: string | null; events: Ev[] }>({ iid: null, events: [] })
  const [state, setState] = useState<StreamState>('connecting')
  const [startedAt, setStartedAt] = useState(() => Date.now())
  useEffect(() => {
    setLog({ iid, events: [] })
    if (!iid) return
    setState('connecting')
    setStartedAt(Date.now())
    const ctl = new AbortController()
    streamIncident(
      `${API}/incidents/${iid}/stream`, key,
      (e) => setLog((prev) => (prev.iid === iid ? { iid, events: [...prev.events, e] } : prev)),
      setState, ctl.signal,
    )
    return () => ctl.abort()
  }, [key, iid])
  return { events: log.iid === iid ? log.events : [], state, startedAt }
}

export function useAsync<T>(fn: () => Promise<T>, deps: unknown[]) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const run = useCallback(() => {
    let live = true
    setLoading(true)
    fn().then(
      (d) => live && (setData(d), setError(null), setLoading(false)),
      (e) => live && (setError(e instanceof Error ? e.message : String(e)), setLoading(false)),
    )
    return () => { live = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)
  useEffect(() => run(), [run])
  return { data, error, loading, reload: run }
}
