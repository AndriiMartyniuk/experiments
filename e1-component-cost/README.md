# E1 — recovery cost of one session, per component

Article 1, section 8. Question: when one device reconnects, what does each part of its session cost the server to
restore?

| Component | Measured as |
|---|---|
| TLS handshake, full vs resumed | server-side duration (accept → handshake done), wire bytes in/out, flights |
| CSM + Register | duration (connection established → registration accepted), CoAP bytes, 1 round trip |
| *m* observations | latency per Observe, observe phase (registered → last Observe answered), CoAP bytes, *m* round trips |
| whole session | recovery time (accept → last Observe answered), probe CPU per session |

Output: the component parameter table (`components.json`) that feeds the stochastic model of article 2.

## Method

For every configuration — *m* ∈ `SENSORS` × TLS session resumption ∈ `RESUMPTION` — `run.sh` starts a fresh
`gateway-probe` and `fleet-emulator`:

1. **First wave** (not measured): `DEVICES` devices connect at `CONNECT_RATE`/s (full handshakes), register and
   are observed.
2. **Warm-up reconnects** (not measured): `WARMUP_CYCLES` reconnects per device, so the JIT has compiled the reconnect
   and resumption path.
3. **Idle baseline**: probe process CPU over `IDLE_WINDOW` seconds with nothing happening.
4. **Measured reconnects**: `CYCLES` reconnects per device. Connections of devices whose session is complete are
   dropped in batches of `DROP_BATCH` every `DROP_INTERVAL` s (no Deregister); devices reconnect immediately
   (strategy `IMMEDIATE`) — each reconnect is one session, with little overlap between sessions.

Fixed: CoAP over TLS (mutual X.509, TLS 1.2), Probe Sensor sampling off (no notifications during the run), state
`IN_MEMORY`, dispatch `SINGLE` (both configurable via `PLACEMENT`, `DISPATCH`).

CPU: (probe CPU in the measured window − idle rate × window) / sessions, per configuration; then a least-squares fit
CPU = a + b·m per resumption setting: *b* = CPU per observation, *a*(full) − *a*(resumed) = CPU difference of the
handshakes.

## Run

Requires Java 25, Python ≥ 3.9 (standard library only), `openssl`; Docker only for Redis placements.

```bash
./run.sh                                   # defaults: 20 devices, 2 warm-up + 5 measured cycles, m = 0 1 2 5
python3 analyze.py raw/<run-id>            # -> results/<run-id>/{summary.md, sessions.csv, components.json}
```

Parameters (environment variables, see the header of `run.sh`): `DEVICES`, `WARMUP_CYCLES`, `CYCLES`, `SENSORS`,
`RESUMPTION`, `DROP_BATCH`, `DROP_INTERVAL`, `CONNECT_RATE`, `IDLE_WINDOW`, `COAP_MESSAGE_LOG`, `PLACEMENT`,
`DISPATCH`, `RUN_ID`, `SKIP_BUILD`, `E1_JAVA_HOME`.

The script builds `lwm2m-objects` (to the local Maven repository), `gateway-probe` and `fleet-emulator`, uses the
dev CA of `gateway-probe/certs` (generated if missing) and fixed ports 18080 / 18090 / 15683 / 15684.

## Layout

```
raw/<run-id>/
  environment.json                 parameters, git revisions (+ dirty flag), Java, host CPU / OS / somaxconn
  m<m>-resumption-<bool>/
    event-log/events-*.jsonl       probe per-session event log (incl. coap_message lines)
    cpu.json                       CPU and time windows
    probe-metrics-warmup.txt       Prometheus snapshot after warm-up
    probe-metrics.txt              ... after the measured reconnects
    emulator-metrics.txt           client-side metrics (client TLS handshake, register latency)
    probe.log, emulator.log
results/<run-id>/
  sessions.csv                     one row per session (measured = after the idle baseline)
  summary.md                       per-configuration and per-component tables
  components.json                  component parameter table
```

Commit `results/<run-id>/` of every run that numbers are taken from; `raw/<run-id>/` is published as a release archive (git-ignored). The 20261114 runs are smoke runs (CPU numbers not usable), their raw data is not published.

## Caveats

- Emulator and probe on one host share CPU: durations include scheduling noise; CPU is the probe process only.
  For numbers to publish, run the emulator on a separate host (and record that in `environment.json`).
- `COAP_MESSAGE_LOG=true` (per-session CoAP bytes) adds JSON serialisation per message to the probe CPU. For CPU
  figures, run again with `COAP_MESSAGE_LOG=false` (aggregated bytes remain in `probe-metrics.txt`).
- Observe requests to one device are sent one at a time (java-coap congestion control per peer), so the observe
  phase grows linearly with *m* — a property of the stack, reported, not corrected.
- Server-side durations start at TCP accept; client-side connect latency is in `emulator-metrics.txt`.
- A smoke run on a laptop (5 devices) showed per-session CPU of 19–35 ms — dominated by noise and instrumentation at
  that size; do not use smoke runs for numbers.
