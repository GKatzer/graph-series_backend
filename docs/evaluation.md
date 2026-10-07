# Evaluation: does graph re-ranking help?

The question: `hybrid` mode re-ranks vector candidates with graph signals (shared actors, shared countries). Does that find better similar series than plain vector order, or does it just reshuffle the same list with noise on top? Answered by `fastapi/scripts/eval_hybrid.py` against the live databases; the per-series results are committed in `report_hybrid.csv` (200 rows, committed 2026-10-01) and the figure below is drawn from that file by `docs/figures/make_figures.py`.

**Short answer:** a small improvement that is statistically distinguishable from zero (+0.0095 in genre overlap@10, 95 % interval +0.001 to +0.0185), obtained by changing about a fifth of the top 10, with some series clearly worse off.

![Paired difference, per-series scatter and churn for 200 source series](media/hybrid-eval.png)

*Left: paired difference of genre overlap@10 (hybrid minus semantic) per source series, log scale. Middle: semantic against hybrid score per series (points jittered, because values are multiples of 0.1). Right: share of the top 10 that differs from the semantic list. n = 200 source series; drawn from `report_hybrid.csv`.*

## Method

- **Sample.** 200 random series from Neo4j (`ORDER BY rand()`, only series with at least one genre, because genre overlap is undefined otherwise). The sample is not seeded; the committed CSV is the one result set behind every number here. Sources come from the graph, not from the ETL dataset, because serving is being evaluated, not the embeddings.
- **Query.** The source series' own vector (fetched from Qdrant by point id), the `k × 8 = 80` nearest neighbours without the source itself: the same setup as `/similar`.
- **Three rankings of the same candidates**, all through the production code:
  1. `semantic`: Qdrant order, filtered to series present in the graph;
  2. `hybrid`: `_hybrid_rerank` with the production weights (`actor_boost` 0.05, `country_boost` 0.1);
  3. `hybrid, zero weights`: the same code path with the bonuses set to 0, which separates the effect of the bonuses from the effect of passing through the re-ranking function (graph filter, sorting).
- **Metric: genre overlap@10.** The share of the top 10 that has at least one genre in common with the source. Genre is **not** part of the re-ranking formula, so it is an independent signal; scoring the re-ranking by shared actors or countries would only check that it does what it was told to do.
- **Churn@10.** The share of the hybrid top 10 that is not in the semantic top 10.
- **Uncertainty.** Paired bootstrap of the mean difference over source series: 5,000 resamples, seed 42, percentile interval (`bootstrap_delta_ci`). Same numbers recomputed from the CSV by `make_figures.py`.

## Results

| Ranking | genre overlap@10 (mean of 200) |
|---|---|
| semantic | 0.764 |
| hybrid, production weights | 0.774 |
| hybrid, zero weights | 0.764 |

| Quantity | Value |
|---|---|
| Δ hybrid − semantic | **+0.0095**, 95 % bootstrap interval [+0.0010, +0.0185] (n = 200) |
| Δ hybrid − zero-weight hybrid | +0.0095, same interval (the zero-weight ranking equals the semantic one in all 200 rows) |
| Relative gain | about +1.2 % |
| Churn@10 | 0.206 on average; the top 10 was unchanged for 53 series, one entry was replaced for 57, and 29 series had five or more entries replaced |
| Per series | better in 35, equal in 146, worse in 19 |
| Worst cases | two series lost 0.3 and 0.2 (tmdb ids 64914: 0.7 to 0.4; 71449: 0.7 to 0.5) |

Reading it:

- The effect is real but small. The interval excludes zero, yet the lower end is +0.001, and the gain is about one percent relative.
- The zero-weight control matches plain semantic ranking exactly, so the gain comes from the weights and not from the filtering that goes with re-ranking.
- 146 of 200 series are unchanged on this metric, 35 improve and 19 get worse; the average hides a trade-off.

## Not covered

- **The weights were chosen by hand and not tuned**; only the production values were evaluated.
- **The graph fill-in weights** of `/similar` (actors 1.0, creators 3.0, genres 0.1, countries 0.05) are not evaluated: the script always fetches 80 candidates, so the thin-coverage situation that triggers the fill-in almost never occurs in a random sample. A dedicated sample of series with few vector neighbours has not been made.
- **`/search?mode=hybrid`** (anchor = first hit of the query, not a known series) is not evaluated; the script mirrors `/similar`.
- **Genre overlap is a proxy** for relevance, not a judgement by users. Overlap of at least one genre is a lenient criterion.
- **One run, one sample** of 200 series on one date; the sample was not seeded, so rerunning gives different numbers within the interval's order of magnitude.

## Reproduce

Needs live Neo4j and Qdrant (the same `.env` as the API) and the inference service; not run in this environment, so the evaluation numbers above are read from the committed CSV, not recomputed from the databases:

```bash
cd fastapi
python scripts/eval_hybrid.py --verbose --csv ../report_hybrid.csv   # default: 200 series, top 10, production weights
python scripts/eval_hybrid.py --n 500 --k 20 --actor-boost 0.1 --country-boost 0.2
```

The figure and the statistics can be recomputed from the committed CSV without any database:

```bash
pip install matplotlib
python docs/figures/make_figures.py
# n=200 semantic=0.7642 hybrid=0.7737 noboost=0.7642 delta=+0.0095 CI=[+0.0010,+0.0185] churn=0.206 noboost==semantic:True
```
