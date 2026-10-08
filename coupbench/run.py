"""Tournament runner.

    python -m coupbench.run config.toml [--deals N] [--workers W]

Each deal (a seeded shuffle) is replayed once for every seating permutation of
the players, so deal luck and seat advantage cancel across the rotation.
Finished games are written to <out_dir>/games/*.json; re-running the same
command resumes where it stopped.
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import sys
import threading
import time
import tomllib
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed

from .agents import make_bot
from .engine import Game, ReplayMismatch, Rules, RULES_VERSION
from .llm import APITransport, CommandTransport, LLMAgent


def load_config(path):
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    models = cfg.get("model", [])
    if not 2 <= len(models) <= 6:
        raise SystemExit("config needs 2-6 [[model]] entries")
    names = [m["name"] for m in models]
    if len(set(names)) != len(names):
        raise SystemExit("model names must be unique")
    return cfg


def build_factories(cfg):
    """name -> callable(seed) returning a fresh agent. Transports are shared so
    each model's max_concurrency applies across all parallel games."""
    factories = {}
    for m in cfg["model"]:
        if "bot" in m:
            factories[m["name"]] = (lambda spec: lambda seed: make_bot(spec, seed))(m["bot"])
        elif "api" in m:
            a = m["api"]
            tr = APITransport(a["base_url"], a["model"], a["key_env"], params=a.get("params"),
                              timeout=m.get("timeout", 300), max_concurrency=m.get("max_concurrency", 2))
            factories[m["name"]] = (lambda tr: lambda seed: LLMAgent(tr, seed=seed))(tr)
        else:
            tr = CommandTransport(m["command"], timeout=m.get("timeout", 300),
                                  max_concurrency=m.get("max_concurrency", 2), env=m.get("env"),
                                  cwd=m.get("cwd"))
            factories[m["name"]] = (lambda tr: lambda seed: LLMAgent(tr, seed=seed))(tr)
    return factories


def cyclic_orders(names):
    """Distinct seating orders up to rotation: the first player fixed, the rest
    permuted. Who sits after whom matters for challenge priority."""
    return [(names[0],) + p for p in itertools.permutations(names[1:])]


def schedule(names, deals, seed):
    """Each deal is played in every rotation of one cyclic order, so every model
    sits in every seat on the same cards. The cyclic order cycles across deals,
    so neighbour pairings balance over the run. Many deals x few seatings beats
    few deals x all permutations: deals are the independent unit."""
    orders = cyclic_orders(names)
    for d in range(deals):
        order = orders[d % len(orders)]
        for r in range(len(names)):
            yield f"d{d:04d}_s{r:02d}", seed * 1_000_003 + d, list(order[r:] + order[:r])


def _play(deal_seed, seating, factories, rules, partial, replay):
    """Rewrites the partial log with the replayed decisions, then appends each
    new decision as it is made."""
    agents = [(name, factories[name](zlib.crc32(f"{deal_seed}:{name}".encode()))) for name in seating]
    seen = 0
    with open(partial, "w") as log:
        def save(rec):
            nonlocal seen
            seen += 1
            if seen > len(replay):  # replayed decisions are already written
                log.write(json.dumps(rec) + "\n")
                log.flush()
        for rec in replay:
            log.write(json.dumps(rec) + "\n")
        log.flush()
        return Game(agents, seed=deal_seed, rules=rules, replay=replay, on_decision=save).play()


def run_game(game_id, deal_seed, seating, factories, rules, out_dir):
    """Plays one game. Every decision is appended to <game_id>.partial.jsonl as it
    is made; if the run is interrupted, the next run replays those decisions
    instead of asking the models again, then continues live."""
    games = os.path.join(out_dir, "games")
    partial = os.path.join(games, f"{game_id}.partial.jsonl")
    replay = []
    if os.path.exists(partial):
        with open(partial) as f:
            for line in f:
                try:
                    replay.append(json.loads(line))
                except ValueError:  # last line cut off mid-write
                    break
        if any(r.get("rules_version") != RULES_VERSION for r in replay):
            raise ValueError("Saved decisions use different or unversioned rules; use a new run directory")
    t0 = time.time()
    try:
        res = _play(deal_seed, seating, factories, rules, partial, list(replay))
    except ReplayMismatch as e:
        print(f"WARNING {game_id}: {e}; restarting the game", file=sys.stderr, flush=True)
        replay = []
        res = _play(deal_seed, seating, factories, rules, partial, [])
    res.update(game_id=game_id, deal_seed=deal_seed, seconds=round(time.time() - t0, 1),
               resumed_decisions=len(replay))
    path = os.path.join(games, f"{game_id}.json")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(res, f)
    os.replace(tmp, path)
    os.remove(partial)
    return res


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("config")
    ap.add_argument("--deals", type=int)
    ap.add_argument("--workers", type=int)
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    run = cfg.get("run", {})
    deals = args.deals or run.get("deals", 10)
    workers = args.workers or run.get("workers", 2)
    out_dir = run.get("out_dir", "runs/default")
    rules = Rules(**cfg.get("rules", {}))
    os.makedirs(os.path.join(out_dir, "games"), exist_ok=True)
    # A rules correction must not silently reuse results from the old game.
    from pathlib import Path
    for path in Path(out_dir, "games").glob("*.json"):
        with path.open() as f:
            previous = json.load(f)
        if previous.get("rules_version") != RULES_VERSION:
            raise SystemExit(f"{path} uses different or unversioned rules. Choose a new out_dir; existing logs were preserved.")

    names = [m["name"] for m in cfg["model"]]
    factories = build_factories(cfg)
    todo = [g for g in schedule(names, deals, run.get("seed", 1))
            if not os.path.exists(os.path.join(out_dir, "games", f"{g[0]}.json"))]
    total = deals * len(names)
    print(f"{total - len(todo)}/{total} games already done; running {len(todo)} with {workers} workers", flush=True)

    done, failed, lock = 0, 0, threading.Lock()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(run_game, gid, seed, seating, factories, rules, out_dir): gid for gid, seed, seating in todo}
        for fut in as_completed(futs):
            with lock:
                try:
                    r = fut.result()
                    done += 1
                    fb = sum(d["meta"].get("fallback", False) for d in r["decisions"])
                    print(f"[{done}/{len(todo)}] {futs[fut]}: winner={r['winner']} turns={r['turns']}"
                          f"{' fallbacks=' + str(fb) if fb else ''} ({r['seconds']}s)", flush=True)
                except Exception as e:  # engine errors are bugs: report loudly, keep going
                    failed += 1
                    print(f"ERROR {futs[fut]}: {e!r}", file=sys.stderr, flush=True)
    print(f"finished: {done} ok, {failed} failed. Analyse with: python -m coupbench.analyze {out_dir}")


if __name__ == "__main__":
    main()
