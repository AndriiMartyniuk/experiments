#!/usr/bin/env python3
"""E1 analysis: per-session recovery cost by component, from the raw output of run.sh.

Usage: python3 analyze.py raw/<run-id> [--out results/<run-id>]

Inputs per configuration directory (m<m>-resumption-<bool>/):
  event-log/*.jsonl   gateway-probe per-session event log (with coap_message lines)
  cpu.json            probe process CPU: idle baseline and reconnect window

Outputs (default results/<run-id>/):
  sessions.csv        one row per measured session (reconnect after the warm-up wave)
  summary.md          per-configuration and per-component tables
  components.json     component parameter table (input for the stochastic model of article 2)

Standard library only (Python >= 3.9), so the analysis runs anywhere the raw data is.
"""
import argparse
import csv
import glob
import json
import math
import os
import statistics
import sys
from collections import defaultdict

PHASE_KINDS = {
    "csm": ("CSM",),
    "register": ("REGISTER", "REGISTER_RESPONSE"),
    "observe": ("OBSERVE", "OBSERVE_RESPONSE"),
}


def quantile(values, q):
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def describe(values):
    values = [v for v in values if v is not None]
    if not values:
        return {"n": 0, "median": None, "p90": None, "mean": None, "stdev": None}
    return {
        "n": len(values),
        "median": quantile(values, 0.5),
        "p90": quantile(values, 0.9),
        "mean": statistics.fmean(values),
        "stdev": statistics.stdev(values) if len(values) > 1 else 0.0,
    }


def load_events(config_dir):
    files = sorted(glob.glob(os.path.join(config_dir, "event-log", "*.jsonl")))
    events = []
    for path in files:
        with open(path) as f:
            events.extend(json.loads(line) for line in f if line.strip())
    events.sort(key=lambda e: e["t"])
    return events


def sessions_of(events, t_measure_start_us, m):
    """Group events by connection; one session = one connection that registered."""
    has_coap_log = any(e["event"] == "coap_message" for e in events)
    by_conn = defaultdict(list)
    for e in events:
        if e.get("conn"):
            by_conn[e["conn"]].append(e)
    sessions = []
    for conn, evs in by_conn.items():
        first = {}
        for e in evs:
            first.setdefault(e["event"], e)
        if "registered" not in first or "accepted" not in first:
            continue
        accepted, registered = first["accepted"], first["registered"]
        tls = first.get("tls_handshake")
        observes = [e for e in evs if e["event"] == "observe_done"]
        observe_ok = [e for e in observes if not e.get("reason")]
        coap = defaultdict(int)
        coap_count = defaultdict(int)
        deregistered = False
        for e in evs:
            if e["event"] == "coap_message":
                coap[(e["dir"], e["kind"])] += e.get("bytes") or 0
                coap_count[(e["dir"], e["kind"])] += 1
                if e["kind"] == "DEREGISTER":
                    deregistered = True
        closed = first.get("closed")
        last_observe_t = max((e["t"] for e in observe_ok), default=None)
        complete = len(observe_ok) >= m
        recovered_t = (last_observe_t if m > 0 else registered["t"]) if complete else None
        row = {
            "conn": conn,
            "endpoint": registered.get("ep"),
            "measured": accepted["t"] >= t_measure_start_us,
            "complete": complete,
            "resumed": tls.get("resumed") if tls else None,
            "tls_ms": tls.get("ms") if tls else None,
            "tls_bytes_in": tls.get("bytesIn") if tls else None,
            "tls_bytes_out": tls.get("bytesOut") if tls else None,
            "tls_flights": tls.get("flights") if tls else None,
            "register_ms": registered.get("ms"),
            "observes": len(observe_ok),
            "observe_ms_median": quantile([e["ms"] for e in observe_ok], 0.5) if observe_ok else None,
            "observe_phase_ms": (last_observe_t - registered["t"]) / 1000 if last_observe_t and m > 0 else None,
            "recovery_ms": (recovered_t - accepted["t"]) / 1000 if recovered_t else None,
        }
        for phase, kinds in PHASE_KINDS.items():
            # without the per-message CoAP log (COAP_MESSAGE_LOG=false) these are unknown, not zero
            row[f"coap_{phase}_bytes_in"] = sum(coap[("in", k)] for k in kinds) if has_coap_log else None
            row[f"coap_{phase}_bytes_out"] = sum(coap[("out", k)] for k in kinds) if has_coap_log else None
            row[f"coap_{phase}_messages"] = (sum(coap_count[("in", k)] + coap_count[("out", k)] for k in kinds)
                                             if has_coap_log else None)
        # wire bytes after the handshake (CoAP phases incl. TLS record overhead); only for connections that carried
        # nothing but the recovery (dropped, not deregistered at the end of the run)
        if closed and tls and not deregistered and closed.get("bytesIn") is not None:
            row["post_handshake_wire_in"] = closed["bytesIn"] - tls["bytesIn"]
            row["post_handshake_wire_out"] = closed["bytesOut"] - tls["bytesOut"]
        else:
            row["post_handshake_wire_in"] = row["post_handshake_wire_out"] = None
        sessions.append(row)
    return sessions


def cpu_per_session(cpu):
    idle_rate = cpu["cpu_idle_ns"] / cpu["idle_window_s"] if cpu["idle_window_s"] > 0 else 0
    net = cpu["cpu_reconnect_ns"] - idle_rate * cpu["reconnect_window_s"]
    return net / cpu["reconnects"] / 1e6 if cpu["reconnects"] else None  # ms of CPU per session


def linear_fit(points):
    """Least squares y = a + b x; returns (a, b) or (None, None)."""
    if len(points) < 2:
        return None, None
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None, None
    b = sum((x - mx) * (y - my) for x, y in points) / sxx
    return my - b * mx, b


def fmt(value, digits=1):
    if value is None:
        return "–"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("raw", help="raw/<run-id> directory written by run.sh")
    parser.add_argument("--out", help="output directory (default results/<run-id>)")
    args = parser.parse_args()

    raw = os.path.abspath(args.raw)
    run_id = os.path.basename(raw.rstrip("/"))
    out = args.out or os.path.join(os.path.dirname(os.path.dirname(raw)), "results", run_id)
    os.makedirs(out, exist_ok=True)
    environment = json.load(open(os.path.join(raw, "environment.json")))

    all_rows, configs = [], []
    for config_dir in sorted(glob.glob(os.path.join(raw, "m*-resumption-*"))):
        cpu = json.load(open(os.path.join(config_dir, "cpu.json")))
        m, resumption = cpu["m"], cpu["resumption"]
        sessions = sessions_of(load_events(config_dir), int(cpu["t_reconnects_start"] * 1e6), m)
        measured = [s for s in sessions if s["measured"] and s["complete"]]
        for s in sessions:
            s.update({"config": os.path.basename(config_dir), "m": m, "resumption": resumption})
        all_rows.extend(sessions)
        configs.append({
            "config": os.path.basename(config_dir), "m": m, "resumption": resumption,
            "run_complete": cpu["complete"], "sessions": len(measured),
            "incomplete_sessions": sum(1 for s in sessions if s["measured"] and not s["complete"]),
            "resumed_share": (sum(1 for s in measured if s["resumed"]) / len(measured)) if measured else None,
            "tls_ms": describe([s["tls_ms"] for s in measured]),
            "register_ms": describe([s["register_ms"] for s in measured]),
            "observe_phase_ms": describe([s["observe_phase_ms"] for s in measured]),
            "recovery_ms": describe([s["recovery_ms"] for s in measured]),
            "cpu_ms_per_session": cpu_per_session(cpu),
            "measured_rows": measured,
        })

    if not configs:
        sys.exit(f"no configuration directories in {raw}")

    # ---- sessions.csv
    columns = list(all_rows[0].keys())
    with open(os.path.join(out, "sessions.csv"), "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(all_rows)

    # ---- components, pooled over configurations
    measured = [s for c in configs for s in c["measured_rows"]]
    full = [s for s in measured if s["resumed"] is False]
    resumed = [s for s in measured if s["resumed"] is True]
    with_obs = [s for s in measured if s["m"] > 0]

    def per_observation(s, key):
        return s[key] / s["m"] if s["m"] and s[key] is not None else None

    components = {
        "tls_full": {
            "duration_ms": describe([s["tls_ms"] for s in full]),
            "bytes_in": describe([s["tls_bytes_in"] for s in full]),
            "bytes_out": describe([s["tls_bytes_out"] for s in full]),
            "flights": describe([s["tls_flights"] for s in full]),
        },
        "tls_resumed": {
            "duration_ms": describe([s["tls_ms"] for s in resumed]),
            "bytes_in": describe([s["tls_bytes_in"] for s in resumed]),
            "bytes_out": describe([s["tls_bytes_out"] for s in resumed]),
            "flights": describe([s["tls_flights"] for s in resumed]),
        },
        "csm": {
            "coap_bytes_in": describe([s["coap_csm_bytes_in"] for s in measured]),
            "coap_bytes_out": describe([s["coap_csm_bytes_out"] for s in measured]),
        },
        "register": {
            "duration_ms": describe([s["register_ms"] for s in measured]),
            "coap_bytes_in": describe([s["coap_register_bytes_in"] for s in measured]),
            "coap_bytes_out": describe([s["coap_register_bytes_out"] for s in measured]),
            "round_trips": 1,
        },
        "observe_per_observation": {
            "latency_ms": describe([s["observe_ms_median"] for s in with_obs]),
            "coap_bytes_in": describe([per_observation(s, "coap_observe_bytes_in") for s in with_obs]),
            "coap_bytes_out": describe([per_observation(s, "coap_observe_bytes_out") for s in with_obs]),
            "round_trips": 1,
        },
    }

    # CPU: per-session CPU against m, per resumption setting
    cpu_fit = {}
    for resumption in (True, False):
        points = [(c["m"], c["cpu_ms_per_session"]) for c in configs
                  if c["resumption"] is resumption and c["cpu_ms_per_session"] is not None]
        a, b = linear_fit(points)
        cpu_fit[str(resumption).lower()] = {"points": points, "intercept_ms": a, "per_observation_ms": b}
    a_full, a_res = cpu_fit["false"]["intercept_ms"], cpu_fit["true"]["intercept_ms"]
    components["cpu"] = {
        "fit_per_session_ms": cpu_fit,
        "full_minus_resumed_handshake_ms": (a_full - a_res) if a_full is not None and a_res is not None else None,
        "method": "probe process CPU in the reconnect window minus the idle baseline rate, per reconnect; "
                  "linear fit over m per resumption setting",
    }
    with open(os.path.join(out, "components.json"), "w") as f:
        json.dump({"run_id": run_id, "environment": environment, "components": components,
                   "configurations": [{k: v for k, v in c.items() if k != "measured_rows"} for c in configs]},
                  f, indent=2)

    # ---- summary.md
    p = environment["parameters"]
    host = environment["host"]
    lines = [
        f"# E1 — component recovery cost, run `{run_id}`",
        "",
        f"{p['devices']} devices × {p['cycles']} reconnects per configuration; transport {p['transport']}, "
        f"placement {p['placement']}, dispatch {p['dispatch']}, reconnect {p['reconnect_strategy']}, "
        f"drops of {p['drop_batch']} every {p['drop_interval_s']} s; per-message CoAP log "
        f"{'on (adds probe CPU)' if p.get('coap_message_log', True) else 'off'}.",
        f"Host: {host['cpu']} ({host['cores']} cores), {host['os']}, somaxconn {host['somaxconn']}; "
        f"emulator and probe on the same host: {host['emulator_and_probe_on_same_host']}.",
        f"Versions: gateway-probe {environment['versions']['gateway_probe']}"
        f"{' (dirty)' if environment['versions']['gateway_probe_dirty'] else ''}, "
        f"fleet-emulator {environment['versions']['fleet_emulator']}"
        f"{' (dirty)' if environment['versions']['fleet_emulator_dirty'] else ''}, "
        f"{environment['versions']['java']}.",
        "",
        "Durations are server-side (gateway-probe event log). Medians, p90 in parentheses.",
        "",
        "## Per configuration",
        "",
        "| config | sessions | resumed | TLS ms | Register ms | observe phase ms | recovery ms | CPU ms / session |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for c in configs:
        def mp(d):
            return f"{fmt(d['median'])} ({fmt(d['p90'])})" if d["n"] else "–"
        lines.append(
            f"| {c['config']} | {c['sessions']}{'' if c['run_complete'] else ' ⚠'} | "
            f"{fmt(c['resumed_share'] * 100 if c['resumed_share'] is not None else None, 0)} % | "
            f"{mp(c['tls_ms'])} | {mp(c['register_ms'])} | {mp(c['observe_phase_ms'])} | "
            f"{mp(c['recovery_ms'])} | {fmt(c['cpu_ms_per_session'], 2)} |")
    lines += [
        "",
        "## Components (pooled over configurations)",
        "",
        "| component | n | duration ms | bytes in | bytes out | flights / round trips |",
        "|---|---|---|---|---|---|",
    ]

    def row(name, comp, duration_key, bytes_in_key, bytes_out_key, trips):
        d = comp.get(duration_key, {"n": 0, "median": None, "p90": None})
        bi, bo = comp.get(bytes_in_key, {}), comp.get(bytes_out_key, {})
        lines.append(f"| {name} | {d.get('n') or bi.get('n', 0)} | "
                     f"{fmt(d.get('median'))} ({fmt(d.get('p90'))}) | {fmt(bi.get('median'), 0)} | "
                     f"{fmt(bo.get('median'), 0)} | {trips} |")

    for key, name in (("tls_full", "TLS handshake, full (wire bytes)"),
                      ("tls_resumed", "TLS handshake, resumed (wire bytes)")):
        flights = components[key]["flights"]["median"]
        row(name, components[key], "duration_ms", "bytes_in", "bytes_out",
            f"{fmt(flights, 0)} flights" if flights is not None else "–")
    row("CSM (CoAP bytes)", components["csm"], "-", "coap_bytes_in", "coap_bytes_out", "– (no response)")
    row("Register (CoAP bytes)", components["register"], "duration_ms", "coap_bytes_in", "coap_bytes_out", "1")
    row("Observe, per observation (CoAP bytes)", components["observe_per_observation"], "latency_ms",
        "coap_bytes_in", "coap_bytes_out", "1")
    cf = components["cpu"]
    lines += [
        "",
        "## CPU (probe process)",
        "",
        "CPU per session = (CPU in the reconnect window − idle baseline rate × window) / reconnects; "
        "fit CPU = a + b·m per resumption setting.",
        "",
        "| resumption | points (m, CPU ms) | a: per session without observations, ms | b: per observation, ms |",
        "|---|---|---|---|",
    ]
    for key, fit in cf["fit_per_session_ms"].items():
        pts = ", ".join(f"({x}, {y:.2f})" for x, y in fit["points"])
        lines.append(f"| {key} | {pts} | {fmt(fit['intercept_ms'], 2)} | {fmt(fit['per_observation_ms'], 3)} |")
    lines += [
        "",
        f"Full − resumed handshake (difference of intercepts): {fmt(cf['full_minus_resumed_handshake_ms'], 2)} ms CPU.",
        "",
        "Notes: CoAP bytes are serialised CoAP-over-TCP sizes without TLS record overhead; TLS bytes are wire bytes "
        "from TCP accept to handshake completion. Observe requests to one device are sent one at a time "
        "(java-coap congestion control per peer), so the observe phase grows with m.",
    ]
    with open(os.path.join(out, "summary.md"), "w") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines))
    print(f"\nwritten: {out}/sessions.csv, summary.md, components.json")


if __name__ == "__main__":
    main()
