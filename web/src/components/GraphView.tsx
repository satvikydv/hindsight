import cytoscape from 'cytoscape'
import { useEffect, useRef } from 'react'
import type { GEdge, GNode } from '../api'

const COLORS: Record<string, string> = {
  Service: '#4fd1ff', Datastore: '#a78bfa', Deploy: '#ff9f5a', ConfigFlag: '#ffc857', Incident: '#ff5d6c',
  Playbook: '#7ee787', Step: '#5eead4', Fact: '#f472b6', Team: '#94a3b8', Person: '#94a3b8', Hypothesis: '#93c5fd',
  Action: '#f8fafc', Message: '#64748b', Agent: '#64748b', Oncall: '#94a3b8',
}
const SIZES: Record<string, number> = { Service: 22, Datastore: 22, Incident: 26, Playbook: 24, Action: 18, Step: 16 }

export const colorOf = (label: string) => COLORS[label] ?? '#8b949e'

interface Props {
  nodes: GNode[]
  edges: GEdge[]
  center?: string
  highlight: Set<string>
  selected: string | null
  onSelect: (id: string | null) => void
}

const STYLE: any[] = [
  { selector: 'node', style: {
      'background-color': 'data(color)', label: 'data(label)', color: '#cbd5e1', 'font-size': 9, 'font-family': 'Inter, system-ui, sans-serif',
      'text-valign': 'bottom', 'text-margin-y': 4, 'text-wrap': 'ellipsis', 'text-max-width': '96px',
      'text-outline-color': '#0a0e15', 'text-outline-width': 2, width: 'data(size)', height: 'data(size)',
      'border-width': 2, 'border-color': '#0a0e15',
      'underlay-color': 'data(color)', 'underlay-opacity': 0.12, 'underlay-padding': 5, 'underlay-shape': 'ellipse' } },
  { selector: 'edge', style: {
      width: 1, 'line-color': '#273244', 'target-arrow-color': '#273244', 'target-arrow-shape': 'triangle',
      'curve-style': 'bezier', 'arrow-scale': 0.6 } },
  { selector: '.center', style: { 'border-width': 3, 'border-color': '#f8fafc', 'underlay-opacity': 0.3, 'underlay-padding': 9 } },
  { selector: '.dim', style: { opacity: 0.12 } },
  { selector: 'node.hl', style: { 'border-width': 3, 'border-color': '#ffc857', 'z-index': 20, 'font-weight': 'bold', color: '#f8fafc',
      'underlay-color': '#ffc857', 'underlay-opacity': 0.35, 'underlay-padding': 9 } },
  { selector: 'edge.hl', style: {
      'line-color': '#ffc857', 'target-arrow-color': '#ffc857', width: 2.5, label: 'data(type)', 'font-size': 8,
      color: '#ffc857', 'text-rotation': 'autorotate', 'text-outline-color': '#0a0e15', 'text-outline-width': 2, 'z-index': 19,
      'line-style': 'dashed', 'line-dash-pattern': [6, 3] } },
  { selector: 'node:selected', style: { 'border-width': 3, 'border-color': '#4fd1ff' } },
]

/** Cytoscape wrapper. Dimming everything outside `highlight` is what makes a cited path readable. */
export default function GraphView({ nodes, edges, center, highlight, selected, onSelect }: Props) {
  const box = useRef<HTMLDivElement>(null)
  const cy = useRef<cytoscape.Core | null>(null)
  const onSelectRef = useRef(onSelect)
  onSelectRef.current = onSelect

  useEffect(() => {
    if (!box.current) return
    const inst = cytoscape({ container: box.current, style: STYLE, wheelSensitivity: 0.25, minZoom: 0.2, maxZoom: 3 })
    inst.on('tap', 'node', (e) => onSelectRef.current(e.target.id()))
    inst.on('tap', (e) => e.target === inst && onSelectRef.current(null))
    cy.current = inst
    return () => {
      inst.destroy()
      cy.current = null
    }
  }, [])

  useEffect(() => {
    const inst = cy.current
    if (!inst) return
    const ids = new Set(nodes.map((n) => n.id))
    const seen = new Set<string>()
    const elements: cytoscape.ElementDefinition[] = nodes.map((n) => ({
      data: { id: n.id, label: n.name || n.label, kind: n.label, color: colorOf(n.label), size: SIZES[n.label] ?? 18 },
      classes: n.id === center ? 'center' : '',
    }))
    for (const e of edges) {
      const id = `${e.from}>${e.type}>${e.to}`
      if (!ids.has(e.from) || !ids.has(e.to) || seen.has(id)) continue
      seen.add(id)
      elements.push({ data: { id, source: e.from, target: e.to, type: e.type } })
    }
    inst.batch(() => {
      inst.elements().remove()
      inst.add(elements)
    })
    if (elements.length) {
      inst.layout({ name: 'cose', animate: false, padding: 24, nodeRepulsion: () => 9000, idealEdgeLength: () => 70,
                    randomize: true } as cytoscape.LayoutOptions).run()
      inst.fit(undefined, 24)
    }
  }, [nodes, edges, center])

  useEffect(() => {
    const inst = cy.current
    if (!inst) return
    inst.batch(() => {
      inst.elements().removeClass('hl dim')
      if (highlight.size) {
        inst.elements().addClass('dim')
        inst.nodes().filter((n) => highlight.has(n.id())).removeClass('dim').addClass('hl')
        inst.edges().filter((e) => highlight.has(e.source().id()) && highlight.has(e.target().id()))
          .removeClass('dim').addClass('hl')
      }
      inst.nodes().unselect()
      if (selected) inst.getElementById(selected).select()
    })
    // zoom to the cited path, or back out to the whole neighbourhood when the highlight clears
    const target = highlight.size ? inst.nodes('.hl') : inst.elements()
    if (target.length) inst.animate({ fit: { eles: target, padding: highlight.size ? 60 : 24 } }, { duration: 350 })
  }, [highlight, selected, nodes, edges])

  return <div ref={box} className="graph-canvas" role="img" aria-label="Graph around the incident's origin service" />
}
