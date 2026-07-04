import "./App.css";

const steps = ["Connect", "Analyse", "Build Twin", "Pressure", "Council", "Apply", "Verify", "PR"];

export function App() {
  return (
    <main className="shell">
      <section className="panel" aria-labelledby="title">
        <p className="eyebrow">Kubernetes rehearsal platform</p>
        <h1 id="title">KubeCouncil</h1>
        <ol className="stepper" aria-label="Workflow steps">
          {steps.map((step) => (
            <li key={step}>{step}</li>
          ))}
        </ol>
      </section>
    </main>
  );
}
