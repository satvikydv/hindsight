import cytoscape from 'cytoscape'
import { useEffect, useRef } from 'react'
import type { GEdge, GNode } from '../api'

const COLORS: Record<string, string> = {
  Service: '#58a6ff', Datastore: '#bc8cff', Deploy: '#ffa657', ConfigFlag: '#e3b341', Incident: '#ff7b72',
  Playbook: '#3fb950', Step: '#56d4dd', Fact: '#f778ba', Team: '#8b949e', Person: '#8b949e', Hypothesis: '#79c0ff',
  Action: '#f0f6fc', Message: '#6e7681', Agent: '#6e7681', Oncall: '#8b949e',
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
      'background-color': 'data(color)', label: 'data(label)', color: '#c9d1d9', 'font-size': 9,
      'text-valign': 'bottom', 'text-margin-y': 3, 'text-wrap': 'ellipsis', 'text-max-width': '96px',
      width: 'data(size)', height: 'data(size)', 'border-width': 0 } },
  { selector: 'edge', style: {
      width: 1, 'line-color': '#30363d', 'target-arrow-color': '#30363d', 'target-arrow-shape': 'triangle',
      'curve-style': 'bezier', 'arrow-scale': 0.7 } },
  { selector: '.center', style: { 'border-width': 3, 'border-color': '#f0f6fc' } },
  { selector: '.dim', style: { opacity: 0.16 } },
  { selector: 'node.hl', style: { 'border-width': 3, 'border-color': '#f0b429', 'z-index': 20, 'font-weight': 'bold' } },
  { selector: 'edge.hl', style: {
      'line-color': '#f0b429', 'target-arrow-color': '#f0b429', width: 2.5, label: 'data(type)', 'font-size': 8,
      color: '#f0b429', 'text-rotation': 'autorotate', 'z-index': 19 } },
  { selector: 'node:selected', style: { 'border-width': 3, 'border-color': '#58a6ff' } },
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
  }, [highlight, selected, nodes, edges])

  return <div ref={box} className="graph-canvas" role="img" aria-label="Graph around the incident's origin service" />
}
