"""Coup rules engine (base game, 2-6 players).

Design goals, learned from auditing earlier LLM Coup harnesses:
  * Agents only ever choose from an explicit list of legal options, so an
    illegal move (unaffordable coup, dead target, phantom card) cannot happen.
  * Card conservation and non-negative coins are asserted after every step.
  * Every player's challenge/block decision is recorded, even when someone
    earlier in turn order gets priority, so no decision data is discarded.
  * Ground truth (was a claim truthful?) is logged privately for analysis and
    never shown to agents.
"""
from __future__ import annotations

import itertools
import random
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

ROLES = ("Duke", "Assassin", "Captain", "Ambassador", "Contessa")
COPIES = 3
RULES_VERSION = "base-2012-audit-1"

ACTION_ROLE = {"tax": "Duke", "assassinate": "Assassin", "steal": "Captain", "exchange": "Ambassador"}
BLOCKERS = {"foreign_aid": ("Duke",), "steal": ("Captain", "Ambassador"), "assassinate": ("Contessa",)}
ACTION_TEXT = {
    "income": "take Income (+1 coin)",
    "foreign_aid": "take Foreign Aid (+2 coins)",
    "tax": "take Tax (+3 coins)",
    "exchange": "Exchange cards with the deck",
    "steal": "Steal up to 2 coins from {t}",
    "assassinate": "Assassinate {t} (pays 3 coins)",
    "coup": "Coup {t} (pays 7 coins)",
}


@dataclass
class Rules:
    parallel_polls: bool = True  # ask all eligible challengers/blockers concurrently
    # Models see "Player <seat>" instead of model names; logs and results keep the
    # real names (result["seats"] maps seat order to names).
    anonymous: bool = True
    starting_seat: int = 0  # supply the previous winner's seat when playing a series


@dataclass
class Player:
    seat: int
    name: str
    agent: object
    label: str = ""  # what other players (and the player itself) see
    coins: int = 2
    hand: list = field(default_factory=list)
    revealed: list = field(default_factory=list)

    @property
    def alive(self) -> bool:
        return bool(self.hand)


class InvariantError(AssertionError):
    pass


class ReplayMismatch(Exception):
    pass


class RulesAmbiguity(RuntimeError):
    """The published rules do not specify how to resolve this position."""


class Game:
    def __init__(self, seats, seed=0, rules: Rules | None = None, setup: dict | None = None,
                 replay: list[dict] | None = None, on_decision=None):
        """seats: list of (name, agent). setup (tests only): {'hands', 'coins', 'deck'}.
        replay: decision records from an interrupted game with the same seed and
        seating; they are reused in order instead of asking the agents again.
        on_decision: called with each decision record as it is made."""
        self.rules = rules or Rules()
        self.replay = list(replay or [])
        self.on_decision = on_decision
        self.seed = seed
        self.rng = random.Random(seed)
        self.players = [Player(i, name, agent, f"Player {i + 1}" if self.rules.anonymous else name)
                        for i, (name, agent) in enumerate(seats)]
        self.n = len(self.players)
        if not 2 <= self.n <= 6:
            raise ValueError("Coup supports 2-6 players")
        if not 0 <= self.rules.starting_seat < self.n:
            raise ValueError("starting_seat must identify a player")
        self.turn = 0
        self.events: list[dict] = []
        self.decisions: list[dict] = []
        self.elimination_order: list[int] = []

        if setup:
            for p, h in zip(self.players, setup["hands"]):
                p.hand = list(h)
            for p, c in zip(self.players, setup.get("coins", [2] * self.n)):
                p.coins = c
            self.deck = list(setup["deck"])
        else:
            self.deck = [r for r in ROLES for _ in range(COPIES)]
            self.rng.shuffle(self.deck)
            for p in self.players:
                p.hand = [self.deck.pop(), self.deck.pop()]
            if self.n == 2:
                self.players[self.rules.starting_seat].coins = 1
        self.check_invariants()

    # ------------------------------------------------------------------ utils
    def alive_players(self):
        return [p for p in self.players if p.alive]

    @property
    def treasury(self):
        return 50 - sum(p.coins for p in self.players)

    def take_from_treasury(self, p: Player, amount: int):
        if self.treasury < amount:
            raise RulesAmbiguity("The 50-coin Treasury cannot pay this action; the base rulebook gives no exhaustion rule")
        p.coins += amount

    def clockwise_from(self, seat):
        """Alive players after `seat` in turn order (excluding seat itself)."""
        order = [(seat + i) % self.n for i in range(1, self.n)]
        return [self.players[s] for s in order if self.players[s].alive]

    def log(self, etype, public=None, private_to=None, private_text=None, **data):
        ev = {"turn": self.turn, "type": etype, **data}
        if public:
            ev["public"] = public
        if private_to is not None:
            ev["private_to"] = private_to
            ev["private_text"] = private_text
        self.events.append(ev)
        return ev

    def speak(self, p: Player, message: str):
        """Nonbinding table talk; never transfers coins, reveals cards, or binds choices."""
        if not p.alive or len(self.alive_players()) == 1:
            raise ValueError("Only active players in an ongoing game may speak")
        if not isinstance(message, str):
            raise ValueError("Table talk must be text")
        if message:
            self.log("speech", f"{p.label} says: {message}", player=p.seat, message=message)

    def check_invariants(self):
        cards = Counter(self.deck)
        for p in self.players:
            cards.update(p.hand)
            cards.update(p.revealed)
            if p.coins < 0:
                raise InvariantError(f"{p.name} has negative coins")
            if len(p.hand) + len(p.revealed) != 2:
                raise InvariantError(f"{p.name} has wrong card count")
        if cards != Counter({r: COPIES for r in ROLES}):
            raise InvariantError(f"card conservation broken: {dict(cards)}")
        if self.treasury < 0:
            raise InvariantError("More than 50 coins in play")

    # ---------------------------------------------------------- observations
    def observation(self, p: Player) -> dict:
        return {
            "you": p.label,
            "seat": p.seat,
            "hand": list(p.hand),
            "coins": p.coins,
            "turn": self.turn,
            "deck_size": len(self.deck),
            "players": [
                {
                    "name": q.label,
                    "seat": q.seat,
                    "coins": q.coins,
                    "influence": len(q.hand),
                    "revealed": list(q.revealed),
                    "alive": q.alive,
                }
                for q in self.players
            ],
            "history": [f"[turn {e['turn']}] {e['public']}" for e in self.events if "public" in e],
            "private_notes": [
                f"[turn {e['turn']}] {e['private_text']}" for e in self.events if e.get("private_to") == p.seat
            ],
        }

    # ------------------------------------------------------------- decisions
    def _call(self, p: Player, kind, options, context, obs):
        res = p.agent.decide(obs, kind, options, context)
        idx = res["choice"]
        if not isinstance(idx, int) or not 0 <= idx < len(options):
            raise ValueError(f"agent {p.name} returned invalid choice {idx!r}")
        return res

    def _replayed(self, p, kind, options, context):
        """Next saved decision, if any. The game is deterministic given its seed
        and the choices made, so a mismatch means the saved log is unusable."""
        if not self.replay:
            return None
        rec = self.replay.pop(0)
        if (rec["player"], rec["kind"], rec["context"], rec["options"]) != (p.name, kind, context, [o["label"] for o in options]):
            raise ReplayMismatch(f"saved decision does not match the game at turn {self.turn} ({p.name}, {kind})")
        return {"choice": rec["choice"], "reasoning": rec.get("reasoning", ""),
                "message": rec.get("message", ""), "meta": rec.get("meta", {})}

    def _record(self, p, kind, options, context, res, extra):
        self.decisions.append(
            {
                "turn": self.turn,
                "rules_version": RULES_VERSION,
                "player": p.name,
                "seat": p.seat,
                "kind": kind,
                "context": context,
                "options": [o["label"] for o in options],
                "choice": res["choice"],
                "reasoning": res.get("reasoning", ""),
                "message": res.get("message", ""),
                "meta": res.get("meta", {}),
                **(extra or {}),
            }
        )
        if self.on_decision:
            self.on_decision(self.decisions[-1])
        if res.get("message"):
            self.speak(p, res["message"])

    def ask(self, p: Player, kind, options, context, extra=None):
        if len(options) == 1:
            return options[0]
        res = self._replayed(p, kind, options, context) or self._call(p, kind, options, context, self.observation(p))
        self._record(p, kind, options, context, res, extra)
        return options[res["choice"]]

    def poll(self, players, kind, options, context, extra=None):
        """Ask several players the same question; returns {seat: option}."""
        if not players:
            return {}
        results = {}
        by_seat = {p.seat: p for p in players}
        while self.replay and self.replay[0]["seat"] in by_seat:
            p = by_seat[self.replay[0]["seat"]]
            if p.seat in results:
                break
            r = self._replayed(p, kind, options, context)
            results[p.seat] = r
        live = [p for p in players if p.seat not in results]
        obs = {p.seat: self.observation(p) for p in live}
        if self.rules.parallel_polls and len(live) > 1:
            def receive(p):
                reply = self._call(p, kind, options, context, obs[p.seat])
                return time.monotonic_ns(), reply
            with ThreadPoolExecutor(max_workers=len(live)) as ex:
                futs = {ex.submit(receive, p): p.seat for p in live}
                received = [(f.result(), futs[f]) for f in as_completed(futs)]
                for (_, reply), seat in sorted(received, key=lambda item: item[0][0]):
                    results[seat] = reply
        else:
            results.update({p.seat: self._call(p, kind, options, context, obs[p.seat]) for p in live})
        for seat, res in results.items():
            self._record(by_seat[seat], kind, options, context, res, extra)
        return {s: options[r["choice"]] for s, r in results.items()}

    # ---------------------------------------------------------------- turns
    def action_options(self, p: Player):
        targets = [q for q in self.clockwise_from(p.seat)]
        opt = lambda a, t=None: {  # noqa: E731
            "label": ACTION_TEXT[a].format(t=t.label if t else "")
            + (f" [claims {ACTION_ROLE[a]}]" if a in ACTION_ROLE else ""),
            "action": a,
            **({"target": t.seat} if t else {}),
        }
        if p.coins >= 10:
            return [opt("coup", t) for t in targets]
        opts = [opt("income"), opt("foreign_aid"), opt("tax"), opt("exchange")]
        opts += [opt("steal", t) for t in targets]
        if p.coins >= 3:
            opts += [opt("assassinate", t) for t in targets]
        if p.coins >= 7:
            opts += [opt("coup", t) for t in targets]
        return opts

    def play_turn(self, p: Player):
        if not p.alive or len(self.alive_players()) < 2:
            raise ValueError("No turn is legal for this player in this state")
        self.turn += 1
        a = self.ask(p, "action", self.action_options(p), "It is your turn. Choose an action.")
        self.do_action(p, a)
        self.check_invariants()

    def do_action(self, p: Player, a: dict):
        act = a["action"]
        tgt = self.players[a["target"]] if "target" in a else None
        role = ACTION_ROLE.get(act)
        desc = ACTION_TEXT[act].format(t=tgt.label if tgt else "")
        self.log(
            "action",
            f"{p.label} declares: {desc}" + (f", claiming {role}" if role else "") + ".",
            actor=p.seat,
            action=act,
            target=a.get("target"),
            claim=role,
            truthful=(role in p.hand) if role else None,
        )

        if act == "coup":
            p.coins -= 7
            self.lose_influence(tgt, "coup")
            return
        if act == "income":
            self.take_from_treasury(p, 1)
            return
        if act == "assassinate":
            p.coins -= 3

        if role:
            outcome = self.challenge_window(p, role, desc)
            if outcome == "caught":
                if act == "assassinate" and p.alive:
                    p.coins += 3  # a successfully challenged action is refunded
                return

            if len(self.alive_players()) == 1:
                return

        if act in BLOCKERS:
            if tgt is not None and not tgt.alive:
                return  # target eliminated by losing a challenge; action fizzles
            if self.block_window(p, act, tgt, desc):
                return

        if act == "foreign_aid":
            self.take_from_treasury(p, 2)
        elif act == "tax":
            self.take_from_treasury(p, 3)
        elif act == "steal":
            amt = min(2, tgt.coins)
            tgt.coins -= amt
            p.coins += amt
            self.log("steal", f"{p.label} steals {amt} coin(s) from {tgt.label}.", actor=p.seat, target=tgt.seat, amount=amt)
        elif act == "assassinate":
            if tgt.alive:
                self.lose_influence(tgt, "assassination")
        elif act == "exchange":
            self.do_exchange(p)

    def challenge_window(self, claimant: Player, role: str, what: str) -> str:
        """Returns 'none' (unchallenged), 'caught' (claimant lacked role) or 'upheld'."""
        eligible = self.clockwise_from(claimant.seat)
        truthful = role in claimant.hand
        options = [
            {"label": "Allow it (do not challenge)", "challenge": False},
            {"label": f"Challenge: {claimant.label} does not have {role}", "challenge": True},
        ]
        context = f"{claimant.label} claims {role} to {what}. Do you challenge the claim?"
        resp = self.poll(eligible, "challenge", options, context,
                         extra={"claimant": claimant.seat, "claim": role, "claim_truthful": truthful})
        challengers = [self.players[s] for s, r in resp.items() if r["challenge"]]
        if not challengers:
            self.log("unchallenged", claimant=claimant.seat, claim=role, truthful=truthful)
            return "none"
        ch = challengers[0]  # first declaration received, without a seat-priority rule
        prove = False
        if truthful:
            prove = self.ask(claimant, "defend", [
                {"label": f"Reveal {role}, shuffle it into the deck and draw a replacement", "prove": True},
                {"label": "Concede the challenge and lose an influence", "prove": False},
            ], f"{ch.label} challenges your {role}. Prove your claim or concede?")["prove"]
        if prove:
            self.log(
                "challenge",
                f"{ch.label} challenges {claimant.label}'s {role}. {claimant.label} reveals {role}, "
                f"shuffles it into the deck and draws a replacement. The challenge fails.",
                challenger=ch.seat, claimant=claimant.seat, claim=role, truthful=True,
                also_wanted=[q.seat for q in challengers[1:]],
            )
            claimant.hand.remove(role)
            self.deck.append(role)
            self.rng.shuffle(self.deck)
            new = self.deck.pop()
            claimant.hand.append(new)
            self.log("draw", private_to=claimant.seat, private_text=f"You drew {new} to replace your revealed {role}.")
            self.lose_influence(ch, "failed_challenge")
            return "upheld"
        self.log(
            "challenge",
            f"{ch.label} challenges {claimant.label}'s {role}. {claimant.label} "
            + ("declines to show it" if truthful else f"cannot show {role}") + ". The challenge succeeds.",
            challenger=ch.seat, claimant=claimant.seat, claim=role, truthful=truthful,
            conceded=truthful,
            also_wanted=[q.seat for q in challengers[1:]],
        )
        self.lose_influence(claimant, "conceded_challenge" if truthful else "caught_bluff")
        return "caught"

    def block_window(self, actor: Player, act: str, tgt: Player | None, desc: str) -> bool:
        roles = BLOCKERS[act]
        eligible = self.clockwise_from(actor.seat) if act == "foreign_aid" else [tgt]
        options = [{"label": "Do not block", "block": None}] + [
            {"label": f"Block, claiming {r}", "block": r} for r in roles
        ]
        context = f"{actor.label} is about to {desc}. Do you block?"
        resp = self.poll(eligible, "block", options, context, extra={"actor": actor.seat, "action": act})
        blockers = [self.players[s] for s, r in resp.items() if r["block"]]
        if not blockers:
            return False
        b = blockers[0]
        role = resp[b.seat]["block"]
        self.log("block", f"{b.label} blocks, claiming {role}.", blocker=b.seat, actor=actor.seat,
                 action=act, claim=role, truthful=role in b.hand)
        # The printed example awards the pending steal even if catching the
        # target's false block eliminates it. Reserve that transfer before its
        # remaining coins are returned to the Treasury.
        pending_steal = min(2, b.coins) if act == "steal" else 0
        outcome = self.challenge_window(b, role, f"block {actor.label}'s action")
        if outcome == "caught":
            self.log("block_failed", f"The block fails; {actor.label}'s action goes ahead.")
            if act == "steal" and not b.alive:
                actor.coins += pending_steal
                self.log("steal", f"{actor.label} steals {pending_steal} coin(s) from {b.label}.",
                         actor=actor.seat, target=b.seat, amount=pending_steal)
                return True  # already resolved the pending action
            return False
        self.log("block_succeeded", f"The block stands; {actor.label}'s action is cancelled.")
        return True

    def lose_influence(self, p: Player, cause: str):
        if not p.alive:
            return
        unique = sorted(set(p.hand))
        options = [{"label": f"Reveal and lose {c}", "card": c} for c in unique]
        card = self.ask(p, "lose_card", options, f"You must lose an influence ({cause.replace('_', ' ')}). Choose a card to reveal.")["card"]
        p.hand.remove(card)
        p.revealed.append(card)
        self.log("lose_influence", f"{p.label} loses an influence and reveals {card}.", player=p.seat, card=card, cause=cause)
        if not p.alive:
            self.elimination_order.append(p.seat)
            self.log("eliminated", f"{p.label} is eliminated.", player=p.seat, cause=cause)
            p.coins = 0

    def do_exchange(self, p: Player):
        drawn = [self.deck.pop(), self.deck.pop()]
        pool = p.hand + drawn
        k = len(p.hand)
        combos = sorted({tuple(sorted(c)) for c in itertools.combinations(pool, k)})
        options = [{"label": "Keep " + " + ".join(c), "keep": list(c)} for c in combos]
        context = f"Exchange: you drew {' and '.join(drawn)}. From {', '.join(pool)} choose {k} card(s) to keep; the rest go back into the deck."
        keep = self.ask(p, "exchange", options, context)["keep"]
        back = list((Counter(pool) - Counter(keep)).elements())
        p.hand = list(keep)
        self.deck.extend(back)
        self.rng.shuffle(self.deck)
        self.log("exchange", f"{p.label} exchanges cards with the deck.", player=p.seat, kept=keep, returned=back,
                 private_to=p.seat, private_text=f"Exchange: you drew {', '.join(drawn)}, kept {', '.join(keep)}, returned {', '.join(back)}.")

    # ----------------------------------------------------------------- game
    def play(self) -> dict:
        cur = self.rules.starting_seat
        while len(self.alive_players()) > 1:
            p = self.players[cur]
            if p.alive:
                self.play_turn(p)
            cur = (cur + 1) % self.n
        return self.result()

    def result(self) -> dict:
        survivors = [p.seat for p in self.alive_players()]
        placements = {}
        # Longevity is benchmark metadata, not an official second-place award.
        # An unfinished game has no placements and is never a scored draw.
        if len(survivors) == 1:
            placements[self.players[survivors[0]].name] = 1
            for i, s in enumerate(reversed(self.elimination_order), 2):
                placements[self.players[s].name] = i
        return {
            "rules_version": RULES_VERSION,
            "seed": self.seed,
            "seats": [p.name for p in self.players],
            "labels": {p.label: p.name for p in self.players},  # what models saw -> real name
            "winner": self.players[survivors[0]].name if len(survivors) == 1 else None,
            "complete": len(survivors) == 1,
            "timeout": False,  # legacy output field; there is no turn-limit ending
            "placements": placements,
            "elimination_order": [self.players[s].name for s in self.elimination_order],
            "turns": self.turn,
            "events": self.events,
            "decisions": self.decisions,
        }
