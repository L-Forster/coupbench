"""Regression cases mapped to docs/rules-audit.md and the printed base rules."""
import json
import tempfile
import unittest
from pathlib import Path
from threading import Event

from coupbench.engine import Game, Rules, RulesAmbiguity
from coupbench.agents import RandomAgent
from coupbench.audit import inspect_game
from coupbench.llm import LLMAgent
from coupbench.run import run_game
from tests.test_engine import Scripted, pick, setup_game


def one_card(game, seat, keep):
    p = game.players[seat]
    p.hand.remove(keep)
    p.revealed.extend(p.hand)
    p.hand = [keep]
    game.check_invariants()


class RulesAudit(unittest.TestCase):
    def test_setup_all_player_counts_and_starting_seats(self):
        for n in range(2, 7):
            for start in range(n):
                g = Game([(str(i), Scripted()) for i in range(n)], rules=Rules(starting_seat=start))
                self.assertEqual(len(g.deck), 15 - 2*n)
                self.assertTrue(all(len(p.hand) == 2 for p in g.players))
                self.assertEqual([p.coins for p in g.players],
                                 [1 if n == 2 and i == start else 2 for i in range(n)])
                self.assertEqual(g.treasury + sum(p.coins for p in g.players), 50)

    def test_action_menu_exactly_matches_costs_and_forced_coup(self):
        g = setup_game([["Contessa", "Contessa"], ["Captain", "Captain"], ["Duke", "Duke"]], [Scripted()]*3)
        one_card(g, 2, "Duke")
        g.lose_influence(g.players[2], "coup")
        for coins in range(15):
            g.players[0].coins = coins
            opts = g.action_options(g.players[0])
            expected = {"income", "foreign_aid", "tax", "exchange", "steal"}
            if coins >= 3: expected.add("assassinate")
            if coins >= 7: expected.add("coup")
            if coins >= 10: expected = {"coup"}
            self.assertEqual({o["action"] for o in opts}, expected)
            self.assertTrue(all(o.get("target", 1) == 1 for o in opts))

    def test_income_and_coup_have_no_reaction_window(self):
        for act, coins in [("income", 2), ("coup", 7)]:
            a = Scripted(action=lambda obs, opts, c: pick(opts, action=act, **({"target": 1} if act == "coup" else {})))
            g = setup_game([["Duke", "Captain"], ["Contessa", "Assassin"]], [a, Scripted()], coins=[coins, 2])
            g.play_turn(g.players[0])
            self.assertFalse(any(d["kind"] in ("challenge", "block") for d in g.decisions))
            self.assertEqual(g.players[0].coins, 3 if act == "income" else 0)

    def test_successfully_challenged_assassin_refunds_three(self):
        for holds_role in (False, True):
            a = Scripted(action=lambda o, opts, c: pick(opts, action="assassinate", target=1),
                         defend=lambda o, opts, c: pick(opts, prove=False))
            b = Scripted(challenge=lambda *args: 1)
            g = setup_game([["Assassin" if holds_role else "Duke", "Captain"], ["Contessa", "Ambassador"]],
                           [a, b], coins=[5, 2])
            g.play_turn(g.players[0])
            self.assertEqual(g.players[0].coins, 5)
            self.assertEqual(len(g.players[0].hand), 1)
            self.assertEqual(len(g.players[1].hand), 2)

    def test_refund_does_not_leave_coins_on_eliminated_attacker(self):
        a = Scripted(action=lambda o, opts, c: pick(opts, action="assassinate", target=1))
        g = setup_game([["Duke", "Captain"], ["Contessa", "Ambassador"]],
                       [a, Scripted(challenge=lambda *args: 1)], coins=[5, 2])
        one_card(g, 0, "Duke")
        g.play_turn(g.players[0])
        self.assertFalse(g.players[0].alive)
        self.assertEqual(g.players[0].coins, 0)

    def test_failed_assassin_challenge_and_caught_contessa_each_lose_two(self):
        for method in ("challenge", "block"):
            a = Scripted(action=lambda o, opts, c: pick(opts, action="assassinate", target=1),
                         challenge=lambda o, opts, c: int("Contessa" in c))
            b = Scripted(**({"challenge": lambda *args: 1} if method == "challenge"
                            else {"block": lambda o, opts, c: pick(opts, block="Contessa")}))
            g = setup_game([["Assassin", "Duke"], ["Captain", "Ambassador"], ["Duke", "Contessa"]],
                           [a, b, Scripted()], coins=[3, 2, 2])
            g.play_turn(g.players[0])
            losses = [e["cause"] for e in g.events if e["type"] == "lose_influence"]
            self.assertEqual(losses, ["failed_challenge" if method == "challenge" else "caught_bluff", "assassination"])
            self.assertEqual(g.players[0].coins, 0)
            self.assertEqual(g.players[1].coins, 0)
            self.assertFalse(g.players[1].alive)

    def test_surviving_failed_challenger_can_block_assassination(self):
        a = Scripted(action=lambda o, opts, c: pick(opts, action="assassinate", target=1))
        b = Scripted(challenge=lambda *args: 1,
                     lose_card=lambda o, opts, c: pick(opts, card="Captain"),
                     block=lambda o, opts, c: pick(opts, block="Contessa"))
        g = setup_game([["Assassin", "Duke"], ["Captain", "Contessa"], ["Duke", "Ambassador"]],
                       [a, b, Scripted()], coins=[3, 2, 2])
        g.play_turn(g.players[0])
        self.assertEqual(g.players[1].hand, ["Contessa"])
        self.assertEqual(g.players[0].coins, 0)

    def test_uncontested_contessa_bluff_blocks_without_loss(self):
        a = Scripted(action=lambda o, opts, c: pick(opts, action="assassinate", target=1))
        b = Scripted(block=lambda o, opts, c: pick(opts, block="Contessa"))
        g = setup_game([["Assassin", "Duke"], ["Captain", "Ambassador"]], [a, b], coins=[3, 2])
        g.play_turn(g.players[0])
        self.assertEqual(len(g.players[1].hand), 2)
        self.assertEqual(g.players[0].coins, 0)

    def test_only_target_can_block_attacks_but_any_opponent_can_block_aid(self):
        for act in ("steal", "assassinate", "foreign_aid"):
            a = Scripted(action=lambda o, opts, c: pick(opts, action=act, **({"target": 1} if act != "foreign_aid" else {})))
            g = setup_game([["Assassin", "Captain"], ["Contessa", "Duke"], ["Ambassador", "Duke"]],
                           [a, Scripted(), Scripted()], coins=[3, 2, 2])
            g.play_turn(g.players[0])
            self.assertEqual({d["seat"] for d in g.decisions if d["kind"] == "block"},
                             {1, 2} if act == "foreign_aid" else {1})
            if act != "foreign_aid":
                self.assertEqual({d["seat"] for d in g.decisions if d["kind"] == "challenge"}, {1, 2})

    def test_third_party_can_challenge_block(self):
        a = Scripted(action=lambda o, opts, c: pick(opts, action="steal", target=1))
        b = Scripted(block=lambda o, opts, c: pick(opts, block="Ambassador"))
        c = Scripted(challenge=lambda o, opts, c: int("block" in c))
        g = setup_game([["Captain", "Duke"], ["Assassin", "Contessa"], ["Ambassador", "Duke"]], [a, b, c])
        g.play_turn(g.players[0])
        self.assertEqual(g.players[0].coins, 4)
        self.assertEqual(len(g.players[1].hand), 1)
        self.assertEqual(next(e for e in g.events if e["type"] == "challenge")["challenger"], 2)

    def test_each_truthful_block_role_can_be_proven(self):
        cases = [("foreign_aid", "Duke"), ("assassinate", "Contessa"),
                 ("steal", "Captain"), ("steal", "Ambassador")]
        for action, role in cases:
            a = Scripted(action=lambda o, opts, c: pick(opts, action=action, **({"target": 1} if action != "foreign_aid" else {})),
                         challenge=lambda o, opts, c: int("block" in c))
            b = Scripted(block=lambda o, opts, c: pick(opts, block=role))
            g = setup_game([["Captain", "Assassin"], [role, "Duke"]], [a, b], coins=[3, 2])
            g.play_turn(g.players[0])
            self.assertEqual(g.players[0].coins, 0 if action == "assassinate" else 3)
            self.assertEqual(len(g.players[0].hand), 1)
            self.assertEqual(len(g.players[1].hand), 2)
            self.assertEqual(g.players[1].coins, 2)
            self.assertTrue(any(e["type"] == "block_succeeded" for e in g.events))

    def test_printed_example_steal_survives_target_elimination(self):
        a = Scripted(action=lambda o, opts, c: pick(opts, action="steal", target=1),
                     challenge=lambda o, opts, c: int("block" in c))
        b = Scripted(block=lambda o, opts, c: pick(opts, block="Ambassador"))
        g = setup_game([["Captain", "Duke"], ["Assassin", "Contessa"], ["Duke", "Ambassador"]],
                       [a, b, Scripted()], coins=[3, 5, 2])
        one_card(g, 1, "Assassin")
        g.play_turn(g.players[0])
        self.assertEqual([p.coins for p in g.players], [5, 0, 2])
        self.assertEqual(g.treasury, 43)

    def test_steal_zero_one_or_two(self):
        for coins in (0, 1, 2, 5):
            a = Scripted(action=lambda o, opts, c: pick(opts, action="steal", target=1))
            g = setup_game([["Captain", "Duke"], ["Assassin", "Contessa"]], [a, Scripted()], coins=[2, coins])
            g.play_turn(g.players[0])
            self.assertEqual(g.players[0].coins, 2 + min(2, coins))
            self.assertEqual(g.players[1].coins, max(0, coins-2))

    def test_exchange_with_one_influence_keeps_one_and_returns_two_privately(self):
        a = Scripted(action=lambda o, opts, c: pick(opts, action="exchange"))
        g = setup_game([["Ambassador", "Duke"], ["Captain", "Contessa"]], [a, Scripted()])
        one_card(g, 0, "Ambassador")
        deck_size = len(g.deck)
        g.play_turn(g.players[0])
        self.assertEqual(len(g.players[0].hand), 1)
        self.assertEqual(len(g.deck), deck_size)
        e = next(e for e in g.events if e["type"] == "exchange")
        self.assertEqual(len(e["returned"]), 2)
        self.assertNotIn("kept", g.observation(g.players[1])["history"][-1])
        self.assertFalse(g.observation(g.players[1])["private_notes"])

    def test_no_exchange_decision_after_last_opponent_loses_challenge(self):
        a = Scripted(action=lambda o, opts, c: pick(opts, action="exchange"))
        b = Scripted(challenge=lambda *args: 1)
        g = setup_game([["Ambassador", "Duke"], ["Captain", "Contessa"]], [a, b])
        one_card(g, 1, "Captain")
        g.play_turn(g.players[0])
        self.assertFalse(any(d["kind"] == "exchange" for d in g.decisions))
        self.assertEqual(g.result()["winner"], "P0")
        with self.assertRaises(ValueError): g.play_turn(g.players[0])

    def test_table_talk_is_public_nonbinding_and_replays(self):
        a = LLMAgent(lambda _: '{"choice":0,"message":"I promise not to coup you."}')
        g = Game([("A", a), ("B", Scripted())], rules=Rules(parallel_polls=False))
        g.play_turn(g.players[0])
        self.assertIn("I promise", g.observation(g.players[1])["history"][0])
        self.assertEqual(g.players[0].coins, 2)
        replay = Game([("A", Scripted()), ("B", Scripted())], rules=Rules(parallel_polls=False), replay=g.decisions)
        replay.play_turn(replay.players[0])
        self.assertEqual(g.events, replay.events)
        replay.players[0].coins = 10
        self.assertEqual({o["action"] for o in replay.action_options(replay.players[0])}, {"coup"})

    def test_no_turn_limit_or_refund_house_rule(self):
        with self.assertRaises(TypeError): Rules(max_turns=200)
        with self.assertRaises(TypeError): Rules(refund_assassin_on_failed_challenge=False)

    def test_unfinished_game_has_no_awarded_placements(self):
        g = Game([("A", Scripted()), ("B", Scripted())])
        self.assertIsNone(g.result()["winner"])
        self.assertEqual(g.result()["placements"], {})
        self.assertFalse(g.result()["complete"])

    def test_selected_starter_actually_plays_first(self):
        class EndAfterChoice(Exception): pass
        class ObserveStarter:
            def decide(self, obs, *args):
                self.seat = obs["seat"]
                raise EndAfterChoice()
        starter = ObserveStarter()
        g = Game([("A", Scripted()), ("B", starter)], rules=Rules(starting_seat=1))
        with self.assertRaises(EndAfterChoice): g.play()
        self.assertEqual(starter.seat, 1)

    def test_parallel_full_game_replay_never_calls_an_agent(self):
        class NoLiveCalls:
            def decide(self, *args): raise AssertionError("replay asked a live player")
        for seed in range(8):
            names = ["A", "B", "C", "D"]
            g = Game([(n, RandomAgent(seed+i)) for i, n in enumerate(names)], seed=seed)
            expected = g.play()
            r = Game([(n, NoLiveCalls()) for n in names], seed=seed, replay=expected["decisions"])
            self.assertEqual(r.play(), expected)
            self.assertFalse(r.replay)

    def test_historical_scanner_detects_corruption_and_rejects_new_rules(self):
        class SaveThenCoup:
            def decide(self, obs, kind, options, context):
                if kind == "action":
                    return {"choice": pick(options, action="coup", target=options[-1]["target"])
                            if obs["coins"] >= 7 else pick(options, action="income")}
                return {"choice": 0}
        g = Game([(n, SaveThenCoup()) for n in ("A", "B", "C")], rules=Rules(parallel_polls=False)).play()
        with self.assertRaises(ValueError): inspect_game(g)
        g.pop("rules_version")
        g["game_id"] = "fixture"
        self.assertEqual(inspect_game(g)["structural_errors"], [])
        g["winner"] = "not a player"
        self.assertIn("winner mismatch", inspect_game(g)["structural_errors"])

    def test_empty_treasury_stops_instead_of_inventing_payout_rule(self):
        hands = [["Duke", "Duke"], ["Captain", "Captain"], ["Contessa", "Contessa"],
                 ["Assassin", "Assassin"], ["Ambassador", "Ambassador"], ["Duke", "Captain"]]
        a = Scripted(action=lambda o, opts, c: pick(opts, action="tax"))
        g = setup_game(hands, [a]+[Scripted()]*5, coins=[9, 9, 9, 9, 9, 4])
        with self.assertRaises(RulesAmbiguity): g.play_turn(g.players[0])
        self.assertEqual(g.players[0].coins, 9)

    def test_parallel_claims_use_received_order_and_full_replay_preserves_it(self):
        released = Event()
        class Slow(Scripted):
            def decide(self, *args):
                if not released.wait(2): raise AssertionError("other challenger never replied")
                return super().decide(*args)
        class Fast(Scripted):
            def decide(self, *args):
                released.set()
                return super().decide(*args)
        # A controlled poll isolates the receipt-order arbitration from card play.
        g = Game([("A", Scripted()), ("B", Slow()), ("C", Fast())])
        opts = [{"label": "allow", "challenge": False}, {"label": "challenge", "challenge": True}]
        g.poll(g.players[1:], "challenge", opts, "test")
        order = [d["seat"] for d in g.decisions]
        r = Game([("A", Scripted()), ("B", Scripted()), ("C", Scripted())], replay=g.decisions)
        r.poll(r.players[1:], "challenge", opts, "test")
        self.assertEqual([d["seat"] for d in r.decisions], order)

    def test_unversioned_partial_is_not_overwritten_or_restarted(self):
        with tempfile.TemporaryDirectory() as folder:
            games = Path(folder, "games"); games.mkdir()
            partial = games / "g.partial.jsonl"
            original = json.dumps({"choice": 0}) + "\n"
            partial.write_text(original)
            with self.assertRaises(ValueError):
                run_game("g", 1, ["A", "B"], {n: lambda s: Scripted() for n in ("A", "B")}, Rules(), folder)
            self.assertEqual(partial.read_text(), original)


if __name__ == "__main__":
    unittest.main()
