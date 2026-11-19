# experiments

Reproducible experiment scripts for article 1 and the theses. Each experiment directory holds the run script,
configuration, analysis scripts and results (figures, tables). Raw run data (event logs, metric snapshots, process logs)
is too large for git and is published as an archive attached to a GitHub release (see below). No numbers are written
into papers unless they come from a run published here.

## Reproducing (layout and build)

The four repositories are expected side by side:

```
<dir>/gateway-probe   <dir>/fleet-emulator   <dir>/lwm2m-objects   <dir>/experiments
```

Requirements: Java 25, Docker (Compose v2), Python 3.9+. Then:

```
(cd lwm2m-objects && ./gradlew publishToMavenLocal)          # shared LwM2M object, used by both services
(cd gateway-probe && scripts/generate-dev-certs.sh)          # dev CA + server/device certificates (not committed)
python3 -m venv experiments/.venv && experiments/.venv/bin/pip install matplotlib
cd experiments/theses-stand && DEVICES=1000 ./run-docker.sh # builds the images on the first run
```

## Raw data

| Archive (GitHub release asset) | Contents |
|---|---|
| `theses-stand-raw-20261119.tar.gz` | all runs of `theses-stand/raw/` used for the theses (unpack into `theses-stand/raw/`) |

| Dir | Experiment | Uses |
|---|---|---|
| `e1-component-cost` | E1 — recovery cost of one session per component: full TLS handshake vs resumption, Register, m observations (CPU time, round trips, bytes) | fleet-emulator, gateway-probe |
| `e2-instance-recovery` (planned) | E2 — recovery time of one instance's sessions, N = 1k / 5k / 10k, in-memory vs Redis | fleet-emulator, gateway-probe, Redis |
| `e3-model-check` (planned) | E3 — crash the instance; compare loss predicted by `session-model` with actual loss | all three |

Output: the parameter table that feeds the stochastic model of article 2.

Layout per experiment: `run.sh` (runs and records), `analyze.py` / `plot*.py` (raw → results), `raw/<run-id>/`
(release archive), `results/<run-id>/` (committed).

| Experiment | Status |
|---|---|
| E1 | `run.sh` + `analyze.py` ready; no published runs yet, see [`e1-component-cost/README.md`](e1-component-cost/README.md) |
| Theses stand | done: series N = 1000…5000 × 3 in the Docker stand, see [`theses-stand/README.md`](theses-stand/README.md) |
| E2 | not started |
| E3 | not started |

## Docker stand (`stand/docker-compose.yml`)

gateway-probe and fleet-emulator as containers on one Docker network, so device traffic uses the Linux network stack
of the Docker VM instead of macOS loopback. The listening side gets `net.core.somaxconn` and
`net.ipv4.tcp_max_syn_backlog` = 4096 (`STAND_SOMAXCONN`, `STAND_SYN_BACKLOG`) and a probe backlog of 4096 — on macOS
the listen queue is capped at `kern.ipc.somaxconn` = 128, which refused connections in mass reconnects. Only the HTTP
ports reach the host (probe 18080, emulator 18090). Scripts record the limits read inside the probe container, the
VM's CPUs and memory, and how many other containers share the VM.

Requires `lwm2m-objects` in `~/.m2` and the dev CA in `gateway-probe/certs` (its server certificate names `probe` and
`gateway-probe`, the Docker service names). Python for figures: `python3 -m venv .venv && .venv/bin/pip install
matplotlib` (the venv is git-ignored).


## License

Code: MIT, see [LICENSE](LICENSE). Results and raw data: CC BY 4.0 (free use, including commercial, with attribution).
