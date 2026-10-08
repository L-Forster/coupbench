"""Read-only structural and historical impact audit: python -m coupbench.audit runs/main."""
from __future__ import annotations

import argparse
import json
import random
from collections import Counter, defaultdict
from pathlib import Path

from .engine import ACTION_ROLE, BLOCKERS, COPIES, ROLES


def inspect_game(g):
    """Validate recorded cards/actions, using historical balances, not counterfactual play."""
    if g.get("rules_version"):
        raise ValueError("This impact scanner is for unversioned historical games only")
    n = len(g["seats"])
    rng = random.Random(g["seed"])
    cards = [r for r in ROLES for _ in range(COPIES)]
    rng.shuffle(cards)
    hands = [[cards.pop(), cards.pop()] for _ in range(n)]
    deck = Counter(cards)
    revealed = [[] for _ in range(n)]
    coins = [2] * n  # unversioned historical runs used 2 even in two-player games
    findings, errors = [], []
    if n == 2:
        findings.append({"rule": "two_player_start", "turn": 0})
    turns = defaultdict(list)
    for e in g["events"]:
        turns[e["turn"]].append(e)
    expected_seat = 0
    for turn, events in turns.items():
        act = next((e for e in events if e["type"] == "action"), None)
        if act is None:
            errors.append(f"turn {turn}: no action")
            continue
        actor, action, target = act["actor"], act["action"], act.get("target")
        while not hands[expected_seat]: expected_seat = (expected_seat + 1) % n
        if actor != expected_seat: errors.append(f"turn {turn}: wrong acting seat")
        expected_seat = (actor + 1) % n
        if coins[actor] >= 10 and action != "coup": errors.append(f"turn {turn}: mandatory coup omitted")
        cost = {"coup": 7, "assassinate": 3}.get(action, 0)
        if coins[actor] < cost: errors.append(f"turn {turn}: unaffordable {action}")
        if action in ("coup", "steal", "assassinate") and (target == actor or not hands[target]):
            errors.append(f"turn {turn}: invalid target")
        coins[actor] -= cost
        if act.get("claim") != ACTION_ROLE.get(action): errors.append(f"turn {turn}: wrong action claim")
        cancelled = False
        for i, e in enumerate(events):
            t = e["type"]
            if t in ("action", "block", "unchallenged", "challenge") and e.get("claim"):
                s = e.get("claimant", e.get("blocker", e.get("actor")))
                if (e["claim"] in hands[s]) != e["truthful"]:
                    errors.append(f"turn {turn}: incorrect truth label for {t}")
            if t == "block":
                if e["claim"] not in BLOCKERS.get(action, ()):
                    errors.append(f"turn {turn}: illegal block role")
                if action != "foreign_aid" and e["blocker"] != target:
                    errors.append(f"turn {turn}: third-party attack block")
            if t == "challenge":
                s = e["claimant"]
                if e["challenger"] == s or not hands[e["challenger"]]:
                    errors.append(f"turn {turn}: ineligible challenger")
                if e.get("also_wanted"):
                    findings.append({"rule": "seat_priority", "turn": turn})
                if e["truthful"]:
                    findings.append({"rule": "forced_proof", "turn": turn})
                    hands[s].remove(e["claim"]); deck[e["claim"]] += 1
                else:
                    if s == actor and e["claim"] == ACTION_ROLE.get(action):
                        cancelled = True
                        if action == "assassinate":
                            findings.append({"rule": "assassin_refund", "turn": turn,
                                             "player": g["seats"][s], "survives": len(hands[s]) == 2})
                    if e["claim"] == "Contessa":
                        losses = [x for x in events[i+1:] if x["type"] == "lose_influence" and x["player"] == s]
                        if len(losses) != min(2, len(hands[s])):
                            errors.append(f"turn {turn}: incorrect Contessa double loss")
            elif t == "draw":
                s = e["private_to"]
                role = e["private_text"].split("You drew ")[1].split(" to replace")[0]
                hands[s].append(role); deck[role] -= 1
            elif t == "lose_influence":
                s = e["player"]
                if e["card"] not in hands[s]:
                    errors.append(f"turn {turn}: lost a card not held")
                else:
                    hands[s].remove(e["card"]); revealed[s].append(e["card"])
            elif t == "eliminated":
                s = e["player"]
                if hands[s]: errors.append(f"turn {turn}: premature elimination")
                if coins[s]: findings.append({"rule": "elimination_coins", "turn": turn, "coins": coins[s]})
            elif t == "steal":
                s = e["target"]
                if e["amount"] != min(2, coins[s]): errors.append(f"turn {turn}: wrong stolen amount")
                coins[s] -= e["amount"]; coins[e["actor"]] += e["amount"]
            elif t == "exchange":
                s = e["player"]
                if sum(bool(h) for h in hands) == 1:
                    findings.append({"rule": "post_victory_exchange", "turn": turn})
                drawn = e["private_text"].split("you drew ")[1].split(", kept ")[0].split(", ")
                if Counter(hands[s] + drawn) != Counter(e["kept"] + e["returned"]):
                    errors.append(f"turn {turn}: invalid exchange pool")
                if len(e["kept"]) != len(hands[s]) or len(e["returned"]) != 2:
                    errors.append(f"turn {turn}: wrong exchange size")
                deck.subtract(drawn); deck.update(e["returned"]); hands[s] = e["kept"].copy()
            elif t == "block_succeeded": cancelled = True
        if not cancelled:
            coins[actor] += {"income": 1, "foreign_aid": 2, "tax": 3}.get(action, 0)
        all_cards = deck.copy()
        for h, r in zip(hands, revealed): all_cards.update(h); all_cards.update(r)
        if all_cards != Counter({r: COPIES for r in ROLES}) or any(v < 0 for v in deck.values()):
            errors.append(f"turn {turn}: card conservation failure")
        if min(coins) < 0: errors.append(f"turn {turn}: negative coins")
    survivors = [g["seats"][s] for s in range(n) if hands[s]]
    if g.get("winner") != (survivors[0] if len(survivors) == 1 else None): errors.append("winner mismatch")
    if g.get("timeout"): findings.append({"rule": "turn_limit", "turn": g["turns"]})
    return {"game_id": g["game_id"], "findings": findings, "structural_errors": errors}


def audit(run_dir):
    files = sorted(Path(run_dir, "games").glob("*.json"))
    games = [inspect_game(json.loads(p.read_text())) for p in files]
    counts, affected = Counter(), defaultdict(set)
    for game in games:
        for f in game["findings"]:
            counts[f["rule"]] += 1; affected[f["rule"]].add(game["game_id"])
    return {"run": str(run_dir), "games": len(games),
            "structural_error_games": sum(bool(g["structural_errors"]) for g in games),
            "issues": {k: {"occurrences": counts[k], "games": sorted(v)} for k, v in affected.items()},
            "details": games,
            "limits": "Historical unversioned runs only. Checks mechanics, not unseen player intentions or counterfactual winners. All lacked voluntary concession and negotiation. Seat-priority counts are observed conflicts, not proven outcome changes."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir")
    args = parser.parse_args()
    print(json.dumps(audit(args.run_dir), indent=2))


if __name__ == "__main__": main()
