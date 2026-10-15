import { useEffect, useRef } from 'react'

/**
 * A slowly drifting knowledge graph behind the landing page. Pulses travel along edges now and then: memory being
 * recalled. Static when the visitor prefers reduced motion, and paused while the tab is hidden.
 */
export default function Backdrop() {
  const ref = useRef<HTMLCanvasElement>(null)

  useEffect(() => {
    const canvas = ref.current
    const ctx = canvas?.getContext('2d')
    if (!canvas || !ctx) return
    const still = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    const dpr = Math.min(window.devicePixelRatio || 1, 2)
    let w = 0, h = 0, raf = 0
    const colors = ['#4fd1ff', '#a78bfa', '#7ee787', '#ff7a45', '#ffc857']
    type N = { x: number; y: number; vx: number; vy: number; r: number; c: string }
    let nodes: N[] = []
    let edges: [number, number][] = []
    let pulses: { e: number; t: number }[] = []

    const build = () => {
      w = canvas.clientWidth
      h = canvas.clientHeight
      canvas.width = w * dpr
      canvas.height = h * dpr
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
      const n = Math.round(Math.min(70, Math.max(24, (w * h) / 22000)))
      nodes = Array.from({ length: n }, (_, i) => ({
        x: Math.random() * w, y: Math.random() * h, vx: (Math.random() - 0.5) * 0.18, vy: (Math.random() - 0.5) * 0.18,
        r: i % 9 === 0 ? 3.2 : 1.8, c: colors[i % colors.length],
      }))
      edges = []
      nodes.forEach((a, i) => {
        const near = nodes.map((b, j) => [j, (a.x - b.x) ** 2 + (a.y - b.y) ** 2] as const)
          .filter(([j]) => j !== i).sort((p, q) => p[1] - q[1]).slice(0, 2)
        near.forEach(([j]) => i < j && edges.push([i, j]))
      })
    }

    const draw = () => {
      ctx.clearRect(0, 0, w, h)
      for (const [i, j] of edges) {
        const a = nodes[i], b = nodes[j]
        ctx.strokeStyle = 'rgba(148,163,184,0.10)'
        ctx.lineWidth = 1
        ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke()
      }
      for (const p of pulses) {
        const [i, j] = edges[p.e]
        const a = nodes[i], b = nodes[j]
        const x = a.x + (b.x - a.x) * p.t, y = a.y + (b.y - a.y) * p.t
        const g = ctx.createRadialGradient(x, y, 0, x, y, 10)
        g.addColorStop(0, 'rgba(79,209,255,0.9)'); g.addColorStop(1, 'rgba(79,209,255,0)')
        ctx.fillStyle = g
        ctx.beginPath(); ctx.arc(x, y, 10, 0, Math.PI * 2); ctx.fill()
      }
      for (const n of nodes) {
        ctx.fillStyle = n.c
        ctx.globalAlpha = n.r > 2 ? 0.75 : 0.45
        ctx.beginPath(); ctx.arc(n.x, n.y, n.r, 0, Math.PI * 2); ctx.fill()
      }
      ctx.globalAlpha = 1
    }

    const step = () => {
      for (const n of nodes) {
        n.x += n.vx; n.y += n.vy
        if (n.x < 0 || n.x > w) n.vx *= -1
        if (n.y < 0 || n.y > h) n.vy *= -1
      }
      if (edges.length && Math.random() < 0.03) pulses.push({ e: Math.floor(Math.random() * edges.length), t: 0 })
      pulses = pulses.map((p) => ({ ...p, t: p.t + 0.012 })).filter((p) => p.t <= 1)
      draw()
      raf = requestAnimationFrame(step)
    }

    build()
    if (still) draw()
    else raf = requestAnimationFrame(step)
    const onResize = () => { build(); if (still) draw() }
    const onVis = () => {
      cancelAnimationFrame(raf)
      if (!document.hidden && !still) raf = requestAnimationFrame(step)
    }
    window.addEventListener('resize', onResize)
    document.addEventListener('visibilitychange', onVis)
    return () => {
      cancelAnimationFrame(raf)
      window.removeEventListener('resize', onResize)
      document.removeEventListener('visibilitychange', onVis)
    }
  }, [])

  return <canvas ref={ref} className="backdrop" aria-hidden />
}
