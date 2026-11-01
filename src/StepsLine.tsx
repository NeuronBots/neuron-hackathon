import { NeuralThread } from "./NeuralThread"

// What a bot did on the way to its answer, as one quiet line: the latest step, how many there were, and the list on demand.
export function StepsLine({ steps, live }: { steps: string[]; live: boolean }) {
  const latest = steps[steps.length - 1]
  return (
    <details className={`steps-line${live ? " is-live" : ""}`}>
      <summary>
        {live ? <NeuralThread width={34} height={12} amp={3} freq={0.13} label="Working" /> : <span className="steps-dot" aria-hidden="true" />}
        <span className="steps-now">{latest}</span>
        {steps.length > 1 && <span className="steps-count">{steps.length} steps</span>}
      </summary>
      <ol className="steps-list">
        {steps.map((step, i) => <li key={i}>{step}</li>)}
      </ol>
    </details>
  )
}
