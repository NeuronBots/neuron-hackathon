// The wireframe N from the design system, drawing itself while something is being generated.
// `inline` is the compact one that sits in a button or a line of text; the default is the hero loader with a caption.
const EDGES: [number, number, number, number, number][] = [
  [24, 76, 24, 28, 0.0],
  [24, 28, 38, 18, 0.1],
  [38, 18, 76, 68, 0.2],
  [76, 68, 62, 78, 0.3],
  [62, 78, 24, 28, 0.4],
  [76, 68, 76, 20, 0.5],
  [76, 20, 64, 22, 0.6],
  [24, 76, 36, 74, 0.7],
  [36, 74, 76, 20, 0.15],
  [24, 76, 64, 22, 0.25],
  [38, 50, 62, 50, 0.35],
  [38, 18, 50, 50, 0.45],
  [50, 50, 62, 78, 0.55],
  [24, 28, 38, 50, 0.65],
  [62, 50, 76, 20, 0.75],
]
const SMALL = [0, 2, 4, 5, 8, 10]  // the edges the compact loader draws

export function NeuronLoader({ label, size = 96, inline = false }: { label?: string; size?: number; inline?: boolean }) {
  const edges = inline ? EDGES.filter((_, i) => SMALL.includes(i)) : EDGES
  const svg = (
    <svg className={inline ? "neuron-loader-sm" : "neuron-loader-svg"} width={inline ? 28 : size} height={inline ? 28 : size} viewBox="0 0 100 100" aria-hidden="true">
      <g className="wire-base" strokeWidth={inline ? 1.8 : 1}>{edges.map(([a, b, c, d]) => <line key={`b${a}${b}${c}${d}`} x1={a} y1={b} x2={c} y2={d} />)}</g>
      <g className="wire-edge" strokeWidth={inline ? 2 : 1.1}>{edges.map(([a, b, c, d, delay]) => <line key={`e${a}${b}${c}${d}`} x1={a} y1={b} x2={c} y2={d} style={{ animationDelay: `${delay}s` }} />)}</g>
    </svg>
  )
  if (inline) return <span className="neuron-loader-inline" role="status" aria-live="polite">{svg}{label && <span>{label}</span>}</span>
  return (
    <div className="neuron-loader" role="status" aria-live="polite">
      {svg}
      {label && <span className="neuron-loader-label">{label}</span>}
    </div>
  )
}
