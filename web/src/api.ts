export const API = ((import.meta.env.VITE_API_URL as string | undefined) ?? '/api').replace(/\/$/, '')

export class ApiError extends Error {
  constructor(public status: number, message: string, public body?: any) {
    super(message)
  }
}

const KEY = 'hindsight.tenant_key'
// Storage can be blocked (private windows, embedded previews): the console then just asks for the key each visit.
export const keyStore = {
  get: () => {
    try { return localStorage.getItem(KEY) } catch { return null }
  },
  set: (v: string) => {
    try { localStorage.setItem(KEY, v) } catch { /* ignore */ }
  },
  clear: () => {
    try { localStorage.removeItem(KEY) } catch { /* ignore */ }
  },
}

function detail(body: any, fallback: string): string {
  const d = body?.detail ?? body
  if (typeof d === 'string') return d
  if (d && typeof d === 'object') return JSON.stringify(d)
  return fallback
}

export async function request<T>(path: string, key: string | null, init: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  if (key) headers['X-Tenant-Key'] = key
  let r: Response
  try {
    r = await fetch(API + path, { ...init, headers: { ...headers, ...(init.headers as object) } })
  } catch {
    throw new ApiError(0, 'Cannot reach the Hindsight API. Is the backend running?')
  }
  const text = await r.text()
  let body: any = text
  try { body = text ? JSON.parse(text) : null } catch { /* plain text */ }
  if (!r.ok) throw new ApiError(r.status, detail(body, r.statusText), body)
  return body as T
}

// ---- shapes (only the fields the console reads) -------------------------------------------------------------------
export interface Health { falkordb: string; llm_key_present: boolean; version: string; demo_tenant: boolean }
export interface FaultClass { name: string; injected_as: string; symptoms: string; correct_fix: string; services: string[] }
export interface Catalog {
  fault_classes: FaultClass[]
  scenarios: { id: string; fault_class: string; service: string }[]
  default: { fault_class: string; service: string }
}
export type Mode = 'off' | 'vector' | 'graph'
export interface IncidentRow {
  id: string; title: string | null; severity?: string | null; status: string; reason?: string | null
  steps?: number | null; tokens?: number | null; cost_to_resolve?: number | null; steps_to_resolve?: number | null
  fast_path?: boolean; mode?: Mode | null
}
export interface IncidentView {
  id: string; title: string; severity: string; status: string; reason: string | null
  steps: number; tokens: number; cost_usd: number | null; mode: Mode | null; running?: boolean; queued?: boolean
  services: { name: string; role: string; tier: string }[]
  hypotheses: { id: string; text: string; score: number; status: string }[]
  root_causes: { label: string; id: string; confirmed_by: string }[]
}
export interface Trace {
  incident: { id: string; title: string; severity: string; status: string; reason: string | null }
  outcome: {
    fast_path: string | null; fast_path_declined: string | null; steps_to_resolve: number | null
    tokens_to_resolve: number | null; cost_to_resolve: number | null; escalated_to: string | null
    unconfirmed_root_cause: boolean
  }
  memory_used: { incident: string; title: string; score: number; basis: string; playbook: string | null; playbook_name: string | null }[]
  cost: { total_usd: number; llm_calls: number; llm_ms: number }
}
export interface GNode { id: string; label: string; name: string | null; tier?: string | null }
export interface GEdge { from: string; to: string; type: string; why?: string }
export interface Subgraph { center: string; nodes: GNode[]; edges: GEdge[]; truncated: number }
export interface Explain {
  action: { id: string; seq: number; agent: string | null; tool: string; args: any; outcome: string; summary: string; rationale: string | null }
  reasoning: { agent: string; content: string; model: string; prompt_tokens: number; completion_tokens: number; latency_ms: number; cost_usd: number | null } | null
  evidence: { label: string; id: string; name: string; why: string | null }[]
  supporting_hypotheses: { id: string; text: string; score: number; status: string }[]
  memory_path: {
    playbook: { id: string; name: string; weight: number; status: string }
    similar_incidents: { incident: string; title: string; score: number; basis: string }[]
    step: { step: number } | null
  } | null
  system_initiated: boolean
  note: string | null
  subgraph: { nodes: GNode[]; edges: GEdge[] }
  highlight: string[]
}
export interface Playbook {
  id: string; name: string; fault_class: string; weight: number; successes: number; attempts: number
  status: string; derived_from: string[]
}

export interface ModeStats {
  n: number; seeds: number; resolution_rate: number | null; steps_mean: number | null; steps_novel: number | null
  steps_repeat: number | null; tokens_novel: number | null; tokens_repeat: number | null
  cost_mean_usd: number | null; cost_total_usd: number; harmful_total: number; injection_harmful: number
  injection_cases: number; confirmed_rate: number | null; cause_accuracy: number | null; recall_at_3: number | null
  fast_path_rate_repeat: number | null; heldout_steps: number | null
}
export interface BenchReport {
  bench_id: string; started_at: string; partial: boolean; stopped_reason: string | null
  config: { llm: string; modes: string[]; seeds: number; sets: string[]; limit: number | null }
  total_cost_usd: number
  records: unknown[]
  aggregate: {
    modes: Record<string, ModeStats>
    comparisons: Record<string, Record<string, number | null>>
    targets?: Record<string, { label: string; target: number; measured: number | null; met: boolean | null }>
  }
  curves: Record<string, { index: number; mean: number; min: number; max: number; n: number }[]>
}
export interface BenchRow { id: string; llm: string; started_at: string; incidents: number; partial: boolean; total_cost_usd: number }
