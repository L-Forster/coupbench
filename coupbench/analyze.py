"""Summarise a run directory.

    python -m coupbench.analyze runs/pilot [--drop-fallback-games]

Writes summary.md and summary.json next to the games folder.
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import random
from collections import defaultdict


def wilson(k, n, z=1.96):
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (c - h, c + h)


def binom_two_sided(k, n, p):
    """Exact two-sided binomial test p-value (log space, safe for large n)."""
    def logpmf(i):
        return (math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1)
                + (i * math.log(p) if i else 0.0) + ((n - i) * math.log(1 - p) if n - i else 0.0))
    lp = [logpmf(i) for i in range(n + 1)]
    obs = lp[k]
    return min(1.0, sum(math.exp(x) for x in lp if x <= obs + 1e-9))


def bootstrap_mean_ci(values_by_deal, iters=2000, seed=0):
    """CI for a mean, resampling whole deals (games within a deal are correlated)."""
    deals = list(values_by_deal.values())
    if not deals:
        return (float("nan"), float("nan"))
    rng = random.Random(seed)
    means = []
    for _ in range(iters):
        sample = [v for _ in deals for v in rng.choice(deals)]
        means.append(sum(sample) / len(sample))
    means.sort()
    return (means[int(0.025 * iters)], means[int(0.975 * iters)])


def pct(x):
    return "—" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{100 * x:.0f}%"


def ratio(a, b):
    return a / b if b else None


def analyse(run_dir, drop_fallback_games=False):
    games = []
    for path in sorted(glob.glob(os.path.join(run_dir, "games", "*.json"))):
        with open(path) as f:
            games.append(json.load(f))
    if not games:
        raise SystemExit(f"no games in {run_dir}/games")

    n_fb_games = sum(any(d["meta"].get("fallback") for d in g["decisions"]) for g in games)
    if drop_fallback_games:
        games = [g for g in games if not any(d["meta"].get("fallback") for d in g["decisions"])]

    s = defaultdict(lambda: defaultdict(float))
    placements_by_deal = defaultdict(lambda: defaultdict(list))
    n_players = len(games[0]["seats"])
    for g in games:
        seats = g["seats"]
        deal = g["game_id"].split("_")[0]
        for name in seats:
            s[name]["games"] += 1
            s[name]["wins"] += g["winner"] == name
            s[name]["placement_sum"] += g["placements"][name]
            placements_by_deal[name][deal].append(g["placements"][name])
        for e in g["events"]:
            t = e["type"]
            if t in ("action", "block") and e.get("claim"):
                who = seats[e["actor"] if t == "action" else e["blocker"]]
                kind = "action" if t == "action" else "block"
                s[who][f"{kind}_claims"] += 1
                s[who][f"{kind}_bluffs"] += e["truthful"] is False
            elif t == "challenge":
                who = seats[e["claimant"]]
                if e["truthful"]:
                    s[who]["honest_claims_challenged"] += 1
                else:
                    s[who]["bluffs_caught"] += 1
            elif t == "unchallenged" and e["truthful"] is False:
                s[seats[e["claimant"]]]["bluffs_unchallenged"] += 1
            elif t == "eliminated":
                s[seats[e["player"]]][f"elim_{e['cause']}"] += 1
        for d in g["decisions"]:
            who = d["player"]
            s[who]["decisions"] += 1
            s[who]["fallbacks"] += bool(d["meta"].get("fallback"))
            s[who]["latency_sum"] += d["meta"].get("latency_s", 0)
            if d["kind"] == "challenge":
                said_yes = d["choice"] == 1
                s[who]["challenge_opps"] += 1
                s[who]["challenges"] += said_yes
                s[who]["challenges_correct"] += said_yes and not d["claim_truthful"]
                s[who]["bluffs_faced"] += not d["claim_truthful"]
                s[who]["bluffs_let_pass"] += (not said_yes) and not d["claim_truthful"]

    chance = 1 / n_players
    rows = []
    for name in sorted(s, key=lambda k: s[k]["placement_sum"] / s[k]["games"]):
        x = s[name]
        n, k = int(x["games"]), int(x["wins"])
        claims = x["action_claims"] + x["block_claims"]
        bluffs = x["action_bluffs"] + x["block_bluffs"]
        rows.append({
            "model": name,
            "games": n,
            "wins": k,
            "win_rate": k / n,
            "win_ci95": wilson(k, n),
            "p_vs_chance": binom_two_sided(k, n, chance),
            "mean_placement": x["placement_sum"] / n,
            "placement_ci95": bootstrap_mean_ci(placements_by_deal[name]),
            "claims_per_game": claims / n,
            "bluff_rate": ratio(bluffs, claims),
            "action_bluff_rate": ratio(x["action_bluffs"], x["action_claims"]),
            "block_bluff_rate": ratio(x["block_bluffs"], x["block_claims"]),
            "bluffs_caught_rate": ratio(x["bluffs_caught"], bluffs),
            "challenge_rate": ratio(x["challenges"], x["challenge_opps"]),
            "challenge_accuracy": ratio(x["challenges_correct"], x["challenges"]),
            "bluffs_let_pass_rate": ratio(x["bluffs_let_pass"], x["bluffs_faced"]),
            "fallback_rate": ratio(x["fallbacks"], x["decisions"]),
            "avg_latency_s": ratio(x["latency_sum"], x["decisions"]),
            "elim_caught_bluff": int(x["elim_caught_bluff"]),
            "elim_failed_challenge": int(x["elim_failed_challenge"]),
        })

    timeouts = sum(g["timeout"] for g in games)
    md = [
        f"# CoupBench results: {os.path.basename(os.path.normpath(run_dir))}",
        "",
        f"{len(games)} games, {n_players} players each (chance win rate {pct(chance)}); "
        f"{timeouts} hit the turn limit; {n_fb_games} contained at least one fallback decision"
        + (" (excluded)" if drop_fallback_games else "") + ".",
        "",
        "## Skill",
        "",
        "| Model | Games | Win rate (95% CI) | p vs chance | Mean place (95% CI) |",
        "|---|---|---|---|---|",
    ]
    for r in rows:
        lo, hi = r["win_ci95"]
        plo, phi = r["placement_ci95"]
        md.append(f"| {r['model']} | {r['games']} | {pct(r['win_rate'])} ({pct(lo)}–{pct(hi)}) | "
                  f"{r['p_vs_chance']:.3f} | {r['mean_placement']:.2f} ({plo:.2f}–{phi:.2f}) |")
    md += [
        "",
        "## Lying",
        "",
        "| Model | Claims/game | Bluff rate | Action bluffs | Block bluffs | Bluffs caught | Out via caught bluff |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        md.append(f"| {r['model']} | {r['claims_per_game']:.1f} | {pct(r['bluff_rate'])} | {pct(r['action_bluff_rate'])} | "
                  f"{pct(r['block_bluff_rate'])} | {pct(r['bluffs_caught_rate'])} | {r['elim_caught_bluff']} |")
    md += [
        "",
        "## Lie detection",
        "",
        "| Model | Challenge rate | Challenge accuracy | Bluffs let pass | Out via failed challenge |",
        "|---|---|---|---|---|",
    ]
    for r in rows:
        md.append(f"| {r['model']} | {pct(r['challenge_rate'])} | {pct(r['challenge_accuracy'])} | "
                  f"{pct(r['bluffs_let_pass_rate'])} | {r['elim_failed_challenge']} |")
    md += [
        "",
        "## Reliability",
        "",
        "| Model | Decisions | Fallback rate | Avg latency |",
        "|---|---|---|---|",
    ]
    for r in rows:
        lat = f"{r['avg_latency_s']:.1f}s" if r["avg_latency_s"] else "—"
        md.append(f"| {r['model']} | {int(s[r['model']]['decisions'])} | {pct(r['fallback_rate'])} | {lat} |")
    md += [
        "",
        "Notes: challenge accuracy counts every player who wanted to challenge, not only the one who got priority. "
        "Bluff rate = claims made without holding the card. Placement CIs resample whole deals.",
    ]

    text = "\n".join(md)
    with open(os.path.join(run_dir, "summary.md"), "w") as f:
        f.write(text + "\n")
    with open(os.path.join(run_dir, "summary.json"), "w") as f:
        json.dump({"games": len(games), "players": n_players, "timeouts": timeouts,
                   "fallback_games": n_fb_games, "models": rows}, f, indent=2)
    return text


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir")
    ap.add_argument("--drop-fallback-games", action="store_true")
    args = ap.parse_args(argv)
    print(analyse(args.run_dir, args.drop_fallback_games))


if __name__ == "__main__":
    main()
