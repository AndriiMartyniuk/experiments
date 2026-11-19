#!/usr/bin/env python3
"""Theses stand: recovery after a mass loss of connections for several fleet sizes (reconnection storm).

Usage: ../.venv/bin/python plot_series.py raw/<run-n1000> raw/<run-n2000> ... [--out results/<name>]

Uses plot.analyse() for every repetition of every run. Figure (300 dpi PNG + SVG):
  (a) recovery curve per fleet size (the repetition with the median T(90 %));
  (b) T(90 %) of accepted connections and T(90 %) of recovered sessions against fleet size (median of the
      repetitions, whiskers = min..max), the difference labelled: the delay added by the gateway;
  (c) CPU of the gateway and of the emulator (cores) during the storm for the largest fleet size, VM capacity line.
Also writes series.json and series.md (incl. failed Observes and unprocessed CoAP messages = degradation).
"""
import argparse
import glob
import json
import os
import re
import statistics

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402

from plot import analyse  # noqa: E402

comma = lambda v, d=2: f"{v:.{d}f}".replace(".", ",")  # noqa: E731


def load_run(raw):
    env = json.load(open(os.path.join(raw, "environment.json")))
    reps = sorted(glob.glob(os.path.join(raw, "rep*")), key=lambda p: int(re.sub(r"\D", "", os.path.basename(p))))
    return env, [analyse(r, env["parameters"]["sensors"]) for r in reps]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("raws", nargs="+")
    parser.add_argument("--out")
    args = parser.parse_args()
    runs = [(os.path.basename(os.path.abspath(r).rstrip("/")),) + load_run(os.path.abspath(r)) for r in args.raws]
    runs.sort(key=lambda r: r[1]["parameters"]["devices"])
    here = os.path.dirname(os.path.abspath(__file__))
    out = args.out or os.path.join(here, "results", "series-" + "-".join(str(r[1]["parameters"]["devices"]) for r in runs))
    os.makedirs(out, exist_ok=True)

    plt.rcParams.update({"font.family": "Times New Roman", "font.size": 11, "axes.unicode_minus": False})
    fig = plt.figure(figsize=(6.6, 5.4))
    grid = fig.add_gridspec(2, 2, width_ratios=[1.25, 1], height_ratios=[1, 0.8], wspace=0.32, hspace=0.42)
    left, right, bottom = fig.add_subplot(grid[0, 0]), fig.add_subplot(grid[0, 1]), fig.add_subplot(grid[1, :])
    styles = ["-", "--", "-.", ":", (0, (5, 1, 1, 1))]
    greys = ["0.0", "0.25", "0.45", "0.6", "0.75"]
    sizes, t90, a90 = [], [], []
    rows = []
    shown_by_n = {}

    def t(r, q, key="t_rec_s"):
        v = r[key][q]
        return v if v is not None else float("nan")

    for i, (name, env, results) in enumerate(runs):
        n = env["parameters"]["devices"]
        # repetition with the median T(90 %) drawn as the curve
        ordered = sorted(results, key=lambda r: t(r, "90"))
        shown = ordered[len(ordered) // 2]
        shown_by_n[n] = shown
        xs = [0.0] + shown["recovery_times_s"]
        ys = [0.0] + [100.0 * (k + 1) / shown["devices"] for k in range(len(shown["recovery_times_s"]))]
        left.step(xs, ys, where="post", color=greys[i % 5], linestyle=styles[i % 5], linewidth=1.3,
                  label=f"N = {n}")
        sizes.append(n)
        t90.append([t(r, "90") for r in results])
        a90.append([t(r, "90", "t_accept_s") for r in results])
        rows.append({
            "run": name, "devices": n, "repetitions": len(results),
            "recovered": [r["recovered"] for r in results],
            "t_rec_s": {q: [r["t_rec_s"][q] for r in results] for q in ("50", "90", "99", "100")},
            "t_accept_s": {q: [r["t_accept_s"][q] for r in results] for q in ("50", "90")},
            "cpu_peak_cores": [r["cpu_peak_cores"] for r in results],
            "dispatch_queue_peak": [r["dispatch_queue_peak"] for r in results],
            "address_reused": [r["address_reused"] for r in results],
            "clock_offset_s": [r["clock_offset_s"] for r in results],
            "observe_failures_after_drop": [r["observe_failures_after_drop"] for r in results],
            "unprocessed_coap_messages": [r["unprocessed_coap_messages"] for r in results],
            "warmup_complete": [r["warmup_complete"] for r in results],
            "client_connect_failures": [r["client_connect_failures"] for r in results],
            "tls_handshakes_resumed": [r["tls_handshakes_resumed"] for r in results],
        })
    left.set_xlabel("Час після обриву з'єднань, с")
    left.set_ylabel("Відновлені сесії, %")
    left.set_ylim(0, 102)
    left.set_xlim(0, max(max(r[1]["recovery_times_s"] or [0]) for r in [(None, sh) for sh in shown_by_n.values()]) + 0.45)
    left.grid(True, linewidth=0.3, color="0.85")
    left.legend(loc="upper left", fontsize=8, frameon=False, handlelength=1.8, borderaxespad=0.2)
    left.set_title("а", fontsize=11, loc="left")

    def med(values):
        clean = [v for v in values if v == v]
        return statistics.median(clean) if clean else float("nan")

    series = {}
    for store, marker, fill, label in ((a90, "o", "white", "прийняття з'єднань"),
                                       (t90, "s", "black", "відновлення сесій")):
        medians = [med(v) for v in store]
        series[label] = medians
        lows = [m - min(v) for m, v in zip(medians, store)]
        highs = [max(v) - m for m, v in zip(medians, store)]
        right.errorbar(sizes, medians, yerr=[lows, highs], marker=marker, color="black", markersize=4, linewidth=1,
                       capsize=3, label=label, markerfacecolor=fill)
    for n, lo, hi in zip(sizes, series["прийняття з'єднань"], series["відновлення сесій"]):
        right.annotate("", xy=(n, hi), xytext=(n, lo), arrowprops={"arrowstyle": "<->", "color": "0.5", "lw": 0.6})
        right.text(n + (sizes[-1] - sizes[0]) * 0.03, (lo + hi) / 2, comma(hi - lo), fontsize=8.5, va="center",
                   color="0.3")
    right.set_xlabel("Кількість пристроїв N")
    right.set_ylabel("T(90 %), с")
    right.set_ylim(0, max(max(v) for v in t90 if v) * 1.45)
    right.set_xlim(sizes[0] - (sizes[-1] - sizes[0]) * 0.12, sizes[-1] + (sizes[-1] - sizes[0]) * 0.2)
    right.grid(True, linewidth=0.3, color="0.85")
    right.legend(loc="upper left", fontsize=8.5, frameon=False)
    right.set_xticks(sizes)
    right.set_title("б", fontsize=11, loc="left")

    # (c) CPU during the storm for the largest fleet size in which every repetition recovered completely
    complete = [n for n, (_, _, res) in zip(sizes, runs) if all(r["recovered"] == r["devices"] for r in res)]
    n_cpu = complete[-1] if complete else sizes[-1]
    shown = shown_by_n[n_cpu]
    vm_cpus = int(runs[0][1]["host"]["runtime"].split(" CPUs")[0].rsplit(" ", 1)[1])
    horizon = (shown["t_rec_s"]["100"] or max(shown["recovery_times_s"])) + 1.5
    for key, style, label in (("probe", "-", "шлюз"), ("emulator", "--", "емулятор")):
        pts = [(x, y) for x, y in shown["samples"][key] if -0.5 <= x <= horizon]
        bottom.plot([x for x, _ in pts], [y for _, y in pts], color="black", linestyle=style, linewidth=1.2,
                    label=label)
    both = [(x, y1 + y2) for (x, y1), (_, y2) in zip(shown["samples"]["probe"], shown["samples"]["emulator"])
            if -0.5 <= x <= horizon]
    bottom.plot([x for x, _ in both], [y for _, y in both], color="0.6", linewidth=1.0, label="разом")
    bottom.axhline(vm_cpus, color="black", linestyle=":", linewidth=0.8)
    bottom.set_ylim(0, vm_cpus + 1.8)
    bottom.axvline(shown["t_rec_s"]["90"], color="0.4", linestyle="-.", linewidth=0.7)
    bottom.text(shown["t_rec_s"]["90"] + 0.04, vm_cpus + 1.05, "T(90 %)", fontsize=8.5, color="0.3", va="bottom")
    bottom.set_xlim(-0.5, horizon)
    bottom.text(horizon - 0.05, vm_cpus + 0.1, f"ресурс ВМ: {vm_cpus} ядра", fontsize=8.5, va="bottom", ha="right")
    bottom.set_xlabel("Час після обриву з'єднань, с")
    bottom.set_ylabel("Процесор, ядер")
    bottom.grid(True, linewidth=0.3, color="0.85")
    bottom.legend(loc="upper left", fontsize=8.5, frameon=False, ncol=3)
    bottom.set_title("в", fontsize=11, loc="left")
    fmt_axis = FuncFormatter(lambda v, _: f"{v:g}".replace(".", ","))
    for ax in (left, right, bottom):
        ax.yaxis.set_major_formatter(fmt_axis)
    for ax in (left, bottom):
        ax.xaxis.set_major_formatter(fmt_axis)
    fig.savefig(os.path.join(out, "figure.png"), dpi=300, bbox_inches="tight")
    fig.savefig(os.path.join(out, "figure.svg"), bbox_inches="tight")

    env0 = runs[0][1]
    with open(os.path.join(out, "series.json"), "w") as f:
        json.dump({"environment": env0, "runs": rows}, f, indent=2, ensure_ascii=False)
    fmt = lambda vs: "–".join(comma(x) for x in (min(vs), max(vs))) if all(v is not None for v in vs) else "n/a"  # noqa: E731
    lines = ["| N | recovered | T(50 %) s | T(90 %) s | T(100 %) s | accept T(90 %) s | gap (medians) s | CPU peak probe / emulator, cores | "
             "dispatch queue peak | address reused | failed Observes | unprocessed CoAP | warm-up complete |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        gap = statistics.median(r['t_rec_s']['90']) - statistics.median(r['t_accept_s']['90'])
        cpu = "; ".join(f"{c['probe'] or 0:.1f} / {c['emulator'] or 0:.1f}" for c in r['cpu_peak_cores'])
        lines.append(f"| {r['devices']} | {', '.join(str(x) for x in r['recovered'])} | {fmt(r['t_rec_s']['50'])} | "
                     f"{fmt(r['t_rec_s']['90'])} | {fmt(r['t_rec_s']['100'])} | {fmt(r['t_accept_s']['90'])} | "
                     f"{comma(gap)} | {cpu} | {', '.join(f'{x or 0:.0f}' for x in r['dispatch_queue_peak'])} | "
                     f"{', '.join(str(x) for x in r['address_reused'])} | "
                     f"{', '.join(f'{x:.0f}' for x in r['observe_failures_after_drop'])} | "
                     f"{', '.join(str(x) for x in r['unprocessed_coap_messages'])} | "
                     f"{', '.join('yes' if x else 'no' for x in r['warmup_complete'])} |")
    with open(os.path.join(out, "series.md"), "w") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines))
    print(f"\nwritten: {out}")


if __name__ == "__main__":
    main()
