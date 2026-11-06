---
name: benchmark
description: Measure an agent or stack on the Deep Document Web Benchmark (finding what public bodies bought from their meeting records): recall, precision, wrong claims, cost per correct decision and time, against two-judge truth sets with a miss breakdown. Use to compare models, tools or prompts, or to test your own agent.
---

# benchmark

- Truth sets: buying decisions in 8 sampled meetings per body, read by two blind judges and an adjudicator, with who
  pays; development bodies for tuning, held-out bodies for checking, a sealed set for the final test.
- Score a run: `uv run python -m ddwb.pipeline score --run <run>` prints recall, precision, fields right, the chain of
  stages (where true decisions leaked) and cost per stage.
- Regression check at $0: `uv run python -m ddwb.pipeline ci --from-run <run> --from-arm <arm> --arm <arm>` replays a
  cached run and fails if scores drop.
- Your own agent: record its decisions with the nav `record` step in a session named `<run>:<your-arm>:<body>`, then
  score the run the same way.
