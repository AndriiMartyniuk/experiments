#!/usr/bin/env python3
"""Samples probe and emulator metrics every PERIOD seconds until killed (SIGTERM) -> CSV on stdout.

Columns: t (host wall clock, s), probe_cpu_s, emulator_cpu_s (process CPU time, s: the rate is cores in use),
dispatch_queue (packets decoded but not yet handed to java-coap), established, accepted, address_reused,
registered, observe_ok. Prometheus' 5 s scrape is too coarse for a 1-4 s recovery, hence this sampler.
Usage: sample.py <probe-prometheus-url> <emulator-prometheus-url> [period]
"""
import signal
import sys
import time
import urllib.request

# sample names; the endpoint filters by family name (without _total / _count)
PROBE = ["process_cpu_time_ns_total", "probe_transport_dispatch_queue", "probe_connections_established",
         "probe_connections_accepted_total", "probe_connections_address_reused_total",
         "probe_lwm2m_registrations_total", "probe_lwm2m_observe_latency_seconds_count"]
EMU = ["process_cpu_time_ns_total"]


def family(name):
    for suffix in ("_total", "_count"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def scrape(url, names):
    """Sums per metric name (label filter: transport=tls, event=registered, outcome=success where they apply)."""
    values = dict.fromkeys(names, 0.0)
    with urllib.request.urlopen(url + "?" + "&".join("includedNames=" + family(n) for n in names), timeout=2) as r:
        for line in r.read().decode().splitlines():
            if not line or line[0] == "#":
                continue
            name = line.split("{", 1)[0].split(" ", 1)[0]
            if name not in values:
                continue
            if 'transport="tcp"' in line or ('event="' in line and 'event="registered"' not in line) \
                    or ('outcome="' in line and 'outcome="success"' not in line):
                continue
            values[name] += float(line.rsplit(" ", 1)[1])
    return values


def main():
    probe, emu = sys.argv[1], sys.argv[2]
    period = float(sys.argv[3]) if len(sys.argv) > 3 else 0.1
    running = [True]
    signal.signal(signal.SIGTERM, lambda *_: running.__setitem__(0, False))
    print("t,probe_cpu_s,emulator_cpu_s,dispatch_queue,established,accepted,address_reused,registered,observe_ok",
          flush=True)
    next_t = time.time()
    while running[0]:
        t = time.time()
        try:
            p, e = scrape(probe, PROBE), scrape(emu, EMU)
            print(f"{t:.3f},{p[PROBE[0]] / 1e9:.4f},{e[EMU[0]] / 1e9:.4f},{p[PROBE[1]]:.0f},{p[PROBE[2]]:.0f},"
                  f"{p[PROBE[3]]:.0f},{p[PROBE[4]]:.0f},{p[PROBE[5]]:.0f},{p[PROBE[6]]:.0f}", flush=True)
        except Exception as ex:  # a missed sample is a gap, not a failure of the run
            print(f"# {t:.3f} {ex}", file=sys.stderr, flush=True)
        next_t += period
        time.sleep(max(0.0, next_t - time.time()))


if __name__ == "__main__":
    main()
