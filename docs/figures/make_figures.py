#!/usr/bin/env python3
"""Draw the evaluation figures from the committed report_hybrid.csv (200 random source series).

    pip install matplotlib
    python docs/figures/make_figures.py        # writes docs/media/hybrid-*.png

Everything is read from report_hybrid.csv (columns: tmdb_id, semantic_overlap, hybrid_overlap,
noboost_overlap, churn), the output of fastapi/scripts/eval_hybrid.py. The bootstrap interval is computed with
the same function and seed as the script (5000 resamples, seed 42).
"""
import csv
import random
import statistics
import sys
from collections import Counter
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "fastapi"))
OUT = ROOT / "docs" / "media"
OUT.mkdir(exist_ok=True)

SEM, HYB, GREY, RED = "#5b6ee1", "#e07b39", "#8a8f98", "#c0392b"
plt.rcParams.update({"font.size": 11, "axes.spines.top": False, "axes.spines.right": False, "figure.dpi": 150})

rows = list(csv.DictReader(open(ROOT / "report_hybrid.csv")))
n = len(rows)
sem = [float(r["semantic_overlap"]) for r in rows]
hyb = [float(r["hybrid_overlap"]) for r in rows]
nob = [float(r["noboost_overlap"]) for r in rows]
churn = [float(r["churn"]) for r in rows]
delta = [round(h - s, 10) for h, s in zip(hyb, sem)]


def bootstrap(deltas, n_boot=5000, seed=42):  # same procedure as bootstrap_delta_ci in eval_hybrid.py
    rng = random.Random(seed)
    idx = range(len(deltas))
    boots = sorted(statistics.mean(deltas[i] for i in rng.choices(idx, k=len(deltas))) for _ in range(n_boot))
    return boots[int(0.025 * n_boot)], boots[int(0.975 * n_boot) - 1]


lo, hi = bootstrap(delta)
mean_d = statistics.mean(delta)

fig, ax = plt.subplots(1, 3, figsize=(13, 4.2))

# 1. paired difference per source series
counts = Counter(delta)
xs = sorted(counts)
cols = [RED if x < 0 else (SEM if x == 0 else HYB) for x in xs]
ax[0].bar([f"{x:+.1f}" for x in xs], [counts[x] for x in xs], color=cols)
ax[0].set_yscale("log")
ax[0].set_ylim(0.7, 1500)
ax[0].set_xlabel("hybrid − semantic, genre overlap@10 (one source series)")
ax[0].set_ylabel("source series (log scale)")
ax[0].set_title(f"Paired difference, n = {n}")
ax[0].text(0.98, 0.97,
           f"mean {mean_d:+.4f}\n95 % CI [{lo:+.4f}, {hi:+.4f}]\n"
           f"better {sum(d > 0 for d in delta)} · equal {sum(d == 0 for d in delta)} · worse {sum(d < 0 for d in delta)}",
           transform=ax[0].transAxes, va="top", ha="right", fontsize=9.5)

# 2. semantic vs hybrid per source (jittered to show the discrete grid)
rng = random.Random(7)
jit = lambda v: [x + rng.uniform(-0.012, 0.012) for x in v]
ax[1].scatter(jit(sem), jit(hyb), s=14, alpha=0.45, color=SEM, edgecolor="none")
ax[1].plot([0, 1], [0, 1], color=GREY, lw=1)
ax[1].set_xlabel("semantic genre overlap@10")
ax[1].set_ylabel("hybrid genre overlap@10")
ax[1].set_title("Per source series (points jittered)")
ax[1].set_xlim(-0.05, 1.05)
ax[1].set_ylim(-0.05, 1.05)

# 3. churn
ax[2].hist(churn, bins=[i / 10 for i in range(0, 11)], color=HYB, edgecolor="white")
ax[2].axvline(statistics.mean(churn), color="black", lw=1, ls="--")
ax[2].set_xlabel("churn@10: share of the top 10 that differs from semantic")
ax[2].set_ylabel("source series")
ax[2].set_title(f"Churn, mean {statistics.mean(churn):.3f}")

fig.suptitle("Does graph re-ranking help? 200 random source series, genre overlap@10 as the independent signal", y=1.02)
fig.tight_layout()
fig.savefig(OUT / "hybrid-eval.png", bbox_inches="tight")

# zero-weight control: identical to semantic?
same = all(abs(a - b) < 1e-12 for a, b in zip(sem, nob))
print(f"n={n} semantic={statistics.mean(sem):.4f} hybrid={statistics.mean(hyb):.4f} noboost={statistics.mean(nob):.4f} "
      f"delta={mean_d:+.4f} CI=[{lo:+.4f},{hi:+.4f}] churn={statistics.mean(churn):.3f} noboost==semantic:{same}")
