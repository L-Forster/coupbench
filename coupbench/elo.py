"""Elo ratings from finishing places.

    python -m coupbench.elo runs/main [--keep-fallback-games]

Each 4-player game counts as 6 head-to-head results: whoever finished higher
beats whoever finished lower (equal places count as draws).
Ratings are fitted to all games at once (Bradley-Terry, the model behind Elo),
so unlike game-by-game Elo updates they do not depend on the order games were
played. A 400-point gap means the higher-rated model is expected to finish
above the other 10 times out of 11; ratings are centred on 1500.

Games containing a fallback decision (historical runs only) are dropped unless
--keep-fallback-games is given. CIs resample whole deals.
"""
from __future__ import annotations

import argparse
import glob
import itertools
import json
import math
import os
import random
from collections import defaultdict


def load_games(run_dir, keep_fallback_games=False):
    games = []
    for path in sorted(glob.glob(os.path.join(run_dir, "games", "*.json"))):
        with open(path) as f:
            g = json.load(f)
        if keep_fallback_games or not any(d["meta"].get("fallback") for d in g["decisions"]):
            games.append(g)
    return games


def pair_results(game):
    """(a, b, score of a) for every pair of players in one game."""
    pl = game["placements"]
    for a, b in itertools.combinations(sorted(pl), 2):
        yield a, b, 1.0 if pl[a] < pl[b] else 0.0 if pl[a] > pl[b] else 0.5


def fit(games, names, prior=0.5, iters=500):
    """Bradley-Terry by minorise-maximise (Hunter 2004). `prior` adds that many
    virtual draws between every pair so ratings stay finite in tiny samples."""
    wins = defaultdict(float)  # wins[i]
    n = defaultdict(float)     # n[(i, j)] games between i and j, i < j
    for a, b in itertools.combinations(sorted(names), 2):
        wins[a] += prior / 2
        wins[b] += prior / 2
        n[(a, b)] += prior
    for g in games:
        for a, b, s in pair_results(g):
            wins[a] += s
            wins[b] += 1 - s
            n[(a, b)] += 1
    p = {x: 1.0 for x in names}
    for _ in range(iters):
        new = {}
        for i in names:
            den = sum(n[tuple(sorted((i, j)))] / (p[i] + p[j]) for j in names if j != i)
            new[i] = wins[i] / den
        mean_log = sum(math.log(v) for v in new.values()) / len(new)
        p = {k: v / math.exp(mean_log) for k, v in new.items()}
    return {k: 1500 + 400 * math.log10(v) for k, v in p.items()}


def bootstrap(games, names, iters=1000, seed=0):
    by_deal = defaultdict(list)
    for g in games:
        by_deal[g["game_id"].split("_")[0]].append(g)
    deals = list(by_deal.values())
    rng = random.Random(seed)
    samples = defaultdict(list)
    for _ in range(iters):
        sample = [g for _ in deals for g in rng.choice(deals)]
        for k, v in fit(sample, names, iters=200).items():
            samples[k].append(v)
    return {k: (sorted(v)[int(0.025 * iters)], sorted(v)[int(0.975 * iters)]) for k, v in samples.items()}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir")
    ap.add_argument("--keep-fallback-games", action="store_true")
    args = ap.parse_args(argv)
    games = load_games(args.run_dir, args.keep_fallback_games)
    if not games:
        raise SystemExit(f"no usable games in {args.run_dir}/games")
    names = sorted({n for g in games for n in g["placements"]})
    elo = fit(games, names)
    ci = bootstrap(games, names)

    above, met = defaultdict(float), defaultdict(int)
    for g in games:
        for a, b, s in pair_results(g):
            above[(a, b)] += s
            above[(b, a)] += 1 - s
            met[(a, b)] += 1
            met[(b, a)] += 1

    order = sorted(names, key=lambda k: -elo[k])
    print(f"{len(games)} games\n")
    print("| Model | Elo | 95% CI |")
    print("|---|---|---|")
    for k in order:
        print(f"| {k} | {elo[k]:.0f} | {ci[k][0]:.0f}–{ci[k][1]:.0f} |")
    print("\nHow often the row model finished above the column model:\n")
    print("| | " + " | ".join(order) + " |")
    print("|---" * (len(order) + 1) + "|")
    for a in order:
        cells = ["—" if a == b else f"{100 * above[(a, b)] / met[(a, b)]:.0f}%" for b in order]
        print(f"| {a} | " + " | ".join(cells) + " |")


if __name__ == "__main__":
    main()
