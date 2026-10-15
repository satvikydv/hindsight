/** Hand-drawn 24px stroke icons, so the console needs no icon library. */

const PATHS: Record<string, string> = {
  // fault classes
  bad_deploy: 'M12 3v12M7 8l5-5 5 5M5 21h14',
  bad_flag: 'M5 21V4M5 4h11l-2 4 2 4H5',
  db_pool_exhaustion: 'M12 3c4.4 0 8 1.3 8 3s-3.6 3-8 3-8-1.3-8-3 3.6-3 8-3zM4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3',
  memory_leak: 'M7 7h10v10H7zM9 3v4M15 3v4M9 17v4M15 17v4M3 9h4M3 15h4M17 9h4M17 15h4',
  cert_expiry: 'M6 11h12v10H6zM8 11V7a4 4 0 0 1 8 0v4M12 15v2',
  dependency_outage: 'M9 7V3M15 7V3M7 7h10v4a5 5 0 0 1-10 0zM12 16v5M4 4l16 16',
  traffic_spike: 'M3 12h4l3-8 4 16 3-8h4',
  cascading: 'M12 3l9 5-9 5-9-5zM3 13l9 5 9-5M3 17.5l9 5 9-5',
  // ui
  bolt: 'M13 2L4 14h7l-1 8 9-12h-7z',
  eye: 'M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12zM12 9a3 3 0 1 1 0 6 3 3 0 0 1 0-6z',
  check: 'M5 12l5 5L20 7',
  skip: 'M5 5l7 7-7 7M13 5l7 7-7 7',
  x: 'M6 6l12 12M18 6L6 18',
  play: 'M7 4l13 8-13 8z',
  doc: 'M7 3h7l5 5v13H7zM14 3v5h5M10 13h6M10 17h6',
  graph: 'M6 6a2 2 0 1 1 0 .1zM18 6a2 2 0 1 1 0 .1zM12 18a2 2 0 1 1 0 .1zM7.5 7.5l3.5 9M16.5 7.5l-3.5 9M8 6h8',
  brain: 'M9 4a3 3 0 0 0-3 3 3 3 0 0 0-2 5 3 3 0 0 0 2 5 3 3 0 0 0 6 1V5a2 2 0 0 0-3-1zM15 4a3 3 0 0 1 3 3 3 3 0 0 1 2 5 3 3 0 0 1-2 5 3 3 0 0 1-6 1',
  shield: 'M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z',
  clock: 'M12 3a9 9 0 1 1 0 18 9 9 0 0 1 0-18zM12 7v5l3 2',
  coin: 'M12 3a9 9 0 1 1 0 18 9 9 0 0 1 0-18zM14.5 9.5c-.5-1-1.4-1.5-2.5-1.5-1.5 0-2.5.8-2.5 2s1 1.6 2.5 2 2.5.8 2.5 2-1 2-2.5 2c-1.1 0-2-.5-2.5-1.5M12 6.5V8M12 16v1.5',
  steps: 'M4 20h4v-4h4v-4h4V8h4',
  token: 'M4 7h16M4 12h10M4 17h13',
  search: 'M11 4a7 7 0 1 1 0 14 7 7 0 0 1 0-14zM20 20l-4-4',
  wrench: 'M14.7 6.3a4 4 0 0 0 5 5L21 13l-8 8-3-3 8-8-1.3-1.3a4 4 0 0 0-5-5L14 6z',
  pen: 'M4 20l4-1 11-11-3-3L5 16zM14 6l3 3',
  siren: 'M7 18v-6a5 5 0 0 1 10 0v6M5 18h14v3H5zM12 3v2M4.2 6.2l1.4 1.4M19.8 6.2l-1.4 1.4',
  logout: 'M15 4h4v16h-4M10 8l-4 4 4 4M6 12h10',
  refresh: 'M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7',
  layers: 'M12 3l9 5-9 5-9-5zM3 13l9 5 9-5',
  chart: 'M4 20V4M4 20h16M8 16l4-5 3 3 5-7',
  book: 'M5 4h10a4 4 0 0 1 4 4v12H9a4 4 0 0 1-4-4zM5 16a4 4 0 0 1 4-4h10',
  arrow: 'M5 12h14M13 6l6 6-6 6',
}

export const AGENT_ICON: Record<string, string> = { triage: 'siren', investigator: 'search', remediator: 'wrench', scribe: 'pen' }

export default function Icon({ name, size = 18, className = '' }: { name: string; size?: number; className?: string }) {
  return (
    <svg className={`icon ${className}`} width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor"
         strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round" aria-hidden focusable="false">
      <path d={PATHS[name] ?? PATHS.bolt} />
    </svg>
  )
}
