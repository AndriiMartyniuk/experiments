# experiments

Reproducible experiment scripts for article 1. Each experiment directory holds the run script, configuration,
**raw results (committed)** and plots. No numbers are written into papers unless they come from a run stored here.

| Dir | Experiment | Uses |
|---|---|---|
| `e1-component-cost` | E1 — recovery cost of one session per component: full TLS handshake vs resumption, Register, m observations (CPU time, round trips, bytes) | fleet-emulator, gateway-probe |
| `e2-instance-recovery` | E2 — recovery time of one instance's sessions, N = 1k / 5k / 10k, in-memory vs Redis | fleet-emulator, gateway-probe, Redis |
| `e3-model-check` | E3 — crash the instance; compare loss predicted by `session-model` with actual loss | all three |

Output: the parameter table that feeds the stochastic model of article 2.

Layout per experiment: `run.sh` (runs and records), `analyze.py` (raw → results), `raw/<run-id>/` (committed),
`results/<run-id>/` (committed).

| Experiment | Status |
|---|---|
| E1 | `run.sh` + `analyze.py` ready, smoke-tested; see [`e1-component-cost/README.md`](e1-component-cost/README.md) |
| E2 | not started |
| E3 | not started |
