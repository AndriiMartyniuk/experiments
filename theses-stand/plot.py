#!/usr/bin/env python3
"""Theses stand figure and numbers from the raw output of run.sh.

Usage: .venv/bin/python plot.py raw/<run-id> [--out results/<run-id>]

Per repetition (raw/<run-id>/rep<i>/): event-log/*.jsonl, run.json, emulator-metrics-{before,after}.txt.
A device counts as recovered once, after the drop, it is registered again AND every one of its sensors is observed
again (registered event + `sensors` successful observe_done events of that registration).

Outputs: figure.png (300 dpi), summary.json, summary.md.
"""
import argparse
import glob
import json
import os
import re

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

LABELS = {
    "x": "Час після обриву всіх з'єднань, с",
    "share": "Відновлені сесії, %",
    "rate": "Нові з'єднання, 1/с",
}


def load_events(rep_dir):
    events = []
    for path in sorted(glob.glob(os.path.join(rep_dir, "event-log", "*.jsonl"))):
        with open(path) as f:
            events.extend(json.loads(line) for line in f if line.strip())
    events.sort(key=lambda e: e["t"])
    return events


def metric_sum(path, name, labels=None):
    total = 0.0
    with open(path) as f:
        for line in f:
            if line.startswith("#") or not line.startswith(name):
                continue
            if labels and not all(f'{k}="{v}"' in line for k, v in labels.items()):
                continue
            if line[len(name)] not in "{ ":
                continue
            total += float(line.rsplit(" ", 1)[1])
    return total


def load_samples(rep_dir, t0_us, first_accept_us, window=0.3):
    """samples.csv of sample.py -> {probe|emulator: [(t, cores)], queue: [(t, packets)]}, t relative to t0.

    CPU in cores = CPU-time increment over a sliding `window` (the JVM reports CPU time in 10 ms steps).
    The sampler runs on the host and the probe in the Docker VM, whose clocks can differ: the host time of t0 is taken
    as the first sample in which the probe's accepted counter grew, minus (first accepted - t0) from the event log
    (alignment error <= one sample period).
    """
    path = os.path.join(rep_dir, "samples.csv")
    out = {"probe": [], "emulator": [], "queue": []}
    if not os.path.exists(path):
        return out
    with open(path) as f:
        rows = [line.strip().split(",") for line in f if line[0].isdigit()]
    rows = [[float(x) for x in r] for r in rows]
    grown = next((r[0] for r in rows if r[5] > rows[0][5]), None)
    t0 = grown - (first_accept_us - t0_us) / 1e6 if grown is not None else t0_us / 1e6
    j = 0
    for i, r in enumerate(rows):
        while rows[j][0] < r[0] - window:
            j += 1
        dt = r[0] - rows[j][0]
        if dt > 0:
            out["probe"].append((r[0] - t0, (r[1] - rows[j][1]) / dt))
            out["emulator"].append((r[0] - t0, (r[2] - rows[j][2]) / dt))
        out["queue"].append((r[0] - t0, r[3]))
    return out


def quantile_time(times, n, share):
    """Time by which `share` of n devices recovered (None if never)."""
    k = int(round(share * n))
    return times[k - 1] if 0 < k <= len(times) else None


def analyse(rep_dir, sensors):
    run = json.load(open(os.path.join(rep_dir, "run.json")))
    all_events = load_events(rep_dir)
    # t0 on the probe's own clock: the first connection closed by the peer in the mass close (the drop is issued on
    # the host; the event log is written in the Docker VM, whose clock can drift from the host's, so the burst is found
    # in the log itself: the first PEER_CLOSED after which half of the fleet's connections close within 3 s)
    n_half = max(1, run["dropped"] // 2)
    peer = sorted(e["t"] for e in all_events if e["event"] == "closed" and e.get("reason") == "PEER_CLOSED")
    burst = next((peer[i] for i in range(len(peer) - n_half) if peer[i + n_half] - peer[i] < 3e6), None)
    closes = [burst] if burst is not None else []
    t0 = burst if burst is not None else run["t_drop"] * 1e6
    events = [e for e in all_events if e["t"] >= t0]
    registered = {}  # regId -> (endpoint, t)
    for e in events:
        if e["event"] == "registered" and e.get("regId"):
            registered[e["regId"]] = (e.get("ep"), e["t"])
    observed = {}
    for e in events:
        if e["event"] == "observe_done" and not e.get("reason") and e.get("regId") in registered:
            observed.setdefault(e["regId"], []).append(e["t"])
    recovered = {}
    for reg_id, (endpoint, t_reg) in registered.items():
        times = sorted(observed.get(reg_id, []))
        if len(times) >= sensors:
            t = max([t_reg] + times[:sensors]) if sensors else t_reg
            # a device that re-registered more than once: its first full recovery counts
            recovered[endpoint] = min(recovered.get(endpoint, float("inf")), (t - t0) / 1e6)
    times = sorted(recovered.values())
    accepted = [(e["t"] - t0) / 1e6 for e in events if e["event"] == "accepted"]
    handshakes = [e for e in events if e["event"] == "tls_handshake"]
    n = run["dropped"]
    before = os.path.join(rep_dir, "emulator-metrics-before.txt")
    after = os.path.join(rep_dir, "emulator-metrics-after.txt")
    attempts = metric_sum(after, "emulator_connect_attempts_total") - metric_sum(before, "emulator_connect_attempts_total")
    failures = metric_sum(after, "emulator_connect_failures_total") - metric_sum(before, "emulator_connect_failures_total")
    probe_before = os.path.join(rep_dir, "probe-metrics-before.txt")
    probe_after = os.path.join(rep_dir, "probe-metrics-after.txt")
    observe_failures = (metric_sum(probe_after, "probe_lwm2m_observe_latency_seconds_count", {"outcome": "failure"})
                        - metric_sum(probe_before, "probe_lwm2m_observe_latency_seconds_count", {"outcome": "failure"}))
    log_path = os.path.join(rep_dir, "probe.log")
    cannot_process = 0
    if os.path.exists(log_path):
        with open(log_path, errors="replace") as f:
            cannot_process = sum(1 for line in f if "Can not process CoAP message" in line)
    first_accept = min((e["t"] for e in events if e["event"] == "accepted"), default=t0)
    samples = load_samples(rep_dir, t0, first_accept)
    reused = sum(1 for e in events if e["event"] == "address_reused")
    return {
        "address_reused": reused,
        "t_accept_s": {f"{int(q * 100)}": quantile_time(sorted(accepted), n, q) for q in (0.5, 0.9)},
        "clock_offset_s": t0 / 1e6 - run["t_drop"],
        "cpu_peak_cores": {k: max((v for _, v in samples[k]), default=None) for k in ("probe", "emulator")},
        "dispatch_queue_peak": max((v for _, v in samples["queue"]), default=None),
        "samples": samples,
        "warmup_complete": run.get("warmup_complete", True),
        "observe_failures_after_drop": observe_failures,
        "unprocessed_coap_messages": cannot_process,
        "t0_source": "first PEER_CLOSED in probe log" if closes else "host t_drop",
        "devices": n,
        "recovered": len(times),
        "complete": run["complete"],
        "t_rec_s": {f"{int(q * 100)}": quantile_time(times, n, q) for q in (0.5, 0.9, 0.99, 1.0)},
        "connections_accepted": len(accepted),
        "tls_handshakes_ok": sum(1 for e in handshakes if not e.get("reason")),
        "tls_handshakes_resumed": sum(1 for e in handshakes if e.get("resumed") and not e.get("reason")),
        "tls_handshakes_failed": sum(1 for e in handshakes if e.get("reason")),
        "client_connect_attempts": attempts,
        "client_connect_failures": failures,
        "recovery_times_s": times,
        "accepted_s": accepted,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("raw")
    parser.add_argument("--out")
    args = parser.parse_args()
    raw = os.path.abspath(args.raw)
    run_id = os.path.basename(raw.rstrip("/"))
    out = args.out or os.path.join(os.path.dirname(os.path.dirname(raw)), "results", run_id)
    os.makedirs(out, exist_ok=True)
    env = json.load(open(os.path.join(raw, "environment.json")))
    sensors = env["parameters"]["sensors"]
    reps = sorted(glob.glob(os.path.join(raw, "rep*")), key=lambda p: int(re.sub(r"\D", "", os.path.basename(p))))
    results = [analyse(r, sensors) for r in reps]

    # ---- figure: recovery curves of all repetitions + connection rate of the first
    plt.rcParams.update({"font.family": "Times New Roman", "font.size": 11, "text.usetex": False,
                         "axes.unicode_minus": False})
    fig, (top, bottom) = plt.subplots(2, 1, figsize=(6.3, 4.6), sharex=True,
                                      gridspec_kw={"height_ratios": [2.2, 1], "hspace": 0.08})
    horizon = max(max(r["recovery_times_s"] or [0]) for r in results) * 1.08 + 0.2
    for i, r in enumerate(results):
        xs = [0.0] + r["recovery_times_s"]
        ys = [0.0] + [100.0 * (k + 1) / r["devices"] for k in range(len(r["recovery_times_s"]))]
        top.step(xs, ys, where="post", color="black" if i == 0 else "0.6", linewidth=1.4 if i == 0 else 0.9,
                 label=f"прогін {i + 1}")
    first = results[0]
    comma = lambda v, d=2: f"{v:.{d}f}".replace(".", ",")  # noqa: E731  Ukrainian decimal comma
    for q, style, y in (("90", "--", 46), ("99", ":", 26)):
        t = first["t_rec_s"][q]
        if t is not None:
            top.axvline(t, color="black", linestyle=style, linewidth=0.8)
            top.text(t + 0.02, y, f"T({q} %) = {comma(t)} с", fontsize=10, va="bottom",
                     bbox={"facecolor": "white", "edgecolor": "none", "pad": 1})
    top.set_ylabel(LABELS["share"])
    top.set_ylim(0, 102)
    top.grid(True, linewidth=0.3, color="0.85")
    top.legend(loc="upper left", fontsize=9.5, frameon=False)
    width = 0.1
    bins = [k * width for k in range(int(horizon / width) + 2)]
    counts = [0] * len(bins)
    for a in first["accepted_s"]:
        k = int(a / width)
        if 0 <= k < len(counts):
            counts[k] += 1
    bottom.bar([b + width / 2 for b in bins], [c / width for c in counts], width=width * 0.95, color="0.35")
    bottom.set_ylabel(LABELS["rate"])
    bottom.set_xlabel(LABELS["x"])
    bottom.set_xlim(0, horizon)
    bottom.grid(True, axis="y", linewidth=0.3, color="0.85")
    from matplotlib.ticker import FuncFormatter
    bottom.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}".replace(".", ",")))
    fig.align_ylabels()
    fig.savefig(os.path.join(out, "figure.png"), dpi=300, bbox_inches="tight")

    # ---- numbers
    summary = {"run_id": run_id, "environment": env,
               "repetitions": [{k: v for k, v in r.items() if k not in ("recovery_times_s", "accepted_s", "samples")}
                               for r in results]}
    with open(os.path.join(out, "summary.json"), "w") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    lines = [f"# Theses stand run `{run_id}`", "",
             f"{env['parameters']['devices']} devices × {sensors} sensors, {env['parameters']['transport']}, "
             f"reconnect {env['parameters']['reconnect_strategy']} ({env['parameters']['base_delay']}–"
             f"{env['parameters']['max_delay']}); host {env['host']['cpu']}, somaxconn {env['host']['somaxconn']}"
             f"{'; ' + env['host']['runtime'] if 'runtime' in env['host'] else ''}.",
             "", "| rep | recovered | T_rec 50 % s | 90 % | 99 % | 100 % | accepted | TLS ok (resumed) | TLS failed | "
             "client connect attempts | client connect failures |", "|" + "---|" * 11]
    for i, r in enumerate(results):
        t = r["t_rec_s"]
        f = lambda v: "–" if v is None else f"{v:.2f}"  # noqa: E731
        lines.append(f"| {i + 1} | {r['recovered']}/{r['devices']} | {f(t['50'])} | {f(t['90'])} | {f(t['99'])} | "
                     f"{f(t['100'])} | {r['connections_accepted']} | {r['tls_handshakes_ok']} "
                     f"({r['tls_handshakes_resumed']}) | {r['tls_handshakes_failed']} | "
                     f"{r['client_connect_attempts']:.0f} | {r['client_connect_failures']:.0f} |")
    with open(os.path.join(out, "summary.md"), "w") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines))
    print(f"\nwritten: {out}/figure.png, summary.json, summary.md")


if __name__ == "__main__":
    main()
