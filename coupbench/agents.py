"""Baseline bots. They read the same options list an LLM sees."""
from __future__ import annotations

import random
from collections import Counter

from .engine import COPIES

KEEP_PRIORITY = ["Duke", "Assassin", "Captain", "Contessa", "Ambassador"]


def visible_count(obs, role):
    """Copies of `role` this player can see: own hand + everyone's revealed cards."""
    seen = Counter(obs["hand"])
    for p in obs["players"]:
        seen.update(p["revealed"])
    return seen[role]


def best_target(obs, options):
    """Index of the option whose target is the most dangerous opponent."""
    by_seat = {p["seat"]: p for p in obs["players"]}
    return max(options, key=lambda i_o: (by_seat[i_o[1]["target"]]["influence"], by_seat[i_o[1]["target"]]["coins"]))[0]


class RandomAgent:
    def __init__(self, seed=None):
        self.rng = random.Random(seed)

    def decide(self, obs, kind, options, context):
        return {"choice": self.rng.randrange(len(options)), "reasoning": "random"}


class HonestAgent:
    """Never bluffs, never challenges."""

    def decide(self, obs, kind, options, context):
        hand = obs["hand"]
        idx = list(enumerate(options))
        if kind == "action":
            coups = [(i, o) for i, o in idx if o["action"] == "coup"]
            if coups:
                return {"choice": best_target(obs, coups)}
            if "Duke" in hand:
                return {"choice": next(i for i, o in idx if o["action"] == "tax")}
            recent = obs["history"][-30:]
            names = {p["seat"]: p["name"] for p in obs["players"]}
            kills = [(i, o) for i, o in idx if o["action"] == "assassinate"
                     and not any(f"{names[o['target']]} blocks, claiming Contessa" in h for h in recent)]
            if "Assassin" in hand and kills:
                return {"choice": best_target(obs, kills)}
            steals = [(i, o) for i, o in idx if o["action"] == "steal"
                      and obs["players"][o["target"]]["coins"] >= 2
                      # don't loop forever against someone who keeps blocking steals
                      and not any(f"{names[o['target']]} blocks, claiming C" in h or
                                  f"{names[o['target']]} blocks, claiming A" in h for h in recent)]
            if "Captain" in hand and steals:
                return {"choice": best_target(obs, steals)}
            if "Ambassador" in hand and not {"Duke", "Assassin", "Captain"} & set(hand):
                return {"choice": next(i for i, o in idx if o["action"] == "exchange")}
            if any("blocks, claiming Duke" in h for h in recent):
                return {"choice": next(i for i, o in idx if o["action"] == "income")}
            return {"choice": next(i for i, o in idx if o["action"] == "foreign_aid")}
        if kind == "challenge":
            return {"choice": next(i for i, o in idx if not o["challenge"])}
        if kind == "defend":
            return {"choice": next(i for i, o in idx if o["prove"])}
        if kind == "block":
            for i, o in idx:
                if o["block"] and o["block"] in hand:
                    return {"choice": i}
            return {"choice": 0}
        if kind == "lose_card":
            return {"choice": max(idx, key=lambda i_o: KEEP_PRIORITY.index(i_o[1]["card"]))[0]}
        if kind == "exchange":
            score = lambda keep: sorted(KEEP_PRIORITY.index(c) for c in keep)  # noqa: E731
            return {"choice": min(idx, key=lambda i_o: (len(set(i_o[1]["keep"])) != len(i_o[1]["keep"]), score(i_o[1]["keep"])))[0]}
        raise ValueError(kind)


class HeuristicAgent(HonestAgent):
    """Card-counting bot with a controllable bluff rate.

    bluff: probability of taking Tax without a Duke when a claim is plausible.
    Challenges only when card counting says the claim is impossible or very
    unlikely; bluffs Contessa when an assassination would otherwise kill it.
    """

    def __init__(self, bluff=0.3, seed=None):
        self.bluff = bluff
        self.rng = random.Random(seed)

    def decide(self, obs, kind, options, context):
        idx = list(enumerate(options))
        if kind == "action":
            base = super().decide(obs, kind, options, context)
            chosen = options[base["choice"]]
            if chosen["action"] == "foreign_aid" and visible_count(obs, "Duke") < 2 and self.rng.random() < self.bluff:
                return {"choice": next(i for i, o in idx if o["action"] == "tax"), "reasoning": "bluff tax"}
            return base
        if kind == "challenge":
            role = next(o["label"].split(" does not have ")[1] for o in options if o["challenge"])
            seen = visible_count(obs, role)
            last_card = len(obs["hand"]) == 1
            p = 1.0 if seen >= COPIES else (0.35 if seen == COPIES - 1 and not last_card else 0.0)
            want = self.rng.random() < p
            return {"choice": next(i for i, o in idx if o["challenge"] == want)}
        if kind == "block":
            honest = super().decide(obs, kind, options, context)
            if options[honest["choice"]]["block"]:
                return honest
            if "Assassinate" in context and len(obs["hand"]) == 1 and visible_count(obs, "Contessa") < COPIES:
                return {"choice": next(i for i, o in idx if o["block"] == "Contessa"), "reasoning": "desperation bluff"}
            return honest
        return super().decide(obs, kind, options, context)


def make_bot(spec: str, seed=None):
    kind, _, arg = spec.partition(":")
    if kind == "random":
        return RandomAgent(seed)
    if kind == "honest":
        return HonestAgent()
    if kind == "heuristic":
        return HeuristicAgent(bluff=float(arg) if arg else 0.3, seed=seed)
    raise ValueError(f"unknown bot {spec!r}")
