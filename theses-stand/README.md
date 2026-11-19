# Theses stand: recovery after a reconnection storm

Scripts: `run-docker.sh` (gateway-probe + fleet-emulator as containers, `../stand/docker-compose.yml`), `run.sh` (host
JVMs, macOS; first runs, listen queue limited by `kern.ipc.somaxconn=128`), `sample.py` (100 ms sampler of CPU time of
both processes and the probe's dispatch queue during the storm), `plot.py` (one run), `plot_series.py` (series figure).

```
for N in 1000 2000 3000 4000 5000; do DEVICES=$N SKIP_BUILD=1 RUN_ID=<date>-docker-n$N ./run-docker.sh; done
../.venv/bin/python plot_series.py raw/<date>-docker-n{1000,...} --out results/<name>
```

t = 0 is the first PEER_CLOSED of the mass close in the probe's own event log (the Docker VM clock can drift from the
host's); samples are aligned to it via the probe's accepted counter.

## Runs (raw data: release archive `theses-stand-raw-20261119.tar.gz`, unpack into `raw/`)

| run | used for |
|---|---|
| `20261119-docker-n1000`, `n2000`, `n3000b`, `n4000`, `n5000` | the series in the theses: `results/series-20261119` (figure, `series.md`, `series.json`) |
| `20261119-diag-n1000-msg` | phase decomposition: every CoAP message logged (`PROBE_COAP_MESSAGES=true`); its `samples.csv` predates a sampler fix (CPU columns are 0) |

All runs: Docker Desktop 25.0.1 on Apple M1 Pro, Linux VM with 4 CPUs / 4.8 GB, `SINGLE` dispatch, `IN_MEMORY` state,
2 sensors per device sampled every 1 s, decorrelated jitter 200 ms–5 s, 3 repetitions each. All sessions recovered in
every repetition; details per repetition in `results/series-20261119/series.md`.

The fix (gateway-probe): a close of a connection is not passed to java-coap when a newer connection from the same
`IP:port` is already active (java-coap identifies TCP peers by address only); the event `address_reused` and the counter
`probe_connections_address_reused_total` record such cases.
