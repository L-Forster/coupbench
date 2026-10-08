import random
import unittest
from collections import Counter

from coupbench.agents import HeuristicAgent, HonestAgent, RandomAgent
from coupbench.engine import COPIES, ROLES, Game, Rules, RulesAmbiguity
from coupbench.llm import LLMAgent, parse_choice, render_prompt


class Scripted:
    """Answers from per-kind functions; defaults to option 0 (allow / don't block)."""

    def __init__(self, **fns):
        self.fns = fns

    def decide(self, obs, kind, options, context):
        fn = self.fns.get(kind)
        return {"choice": fn(obs, options, context) if fn else 0}


def pick(options, **match):
    return next(i for i, o in enumerate(options) if all(o.get(k) == v for k, v in match.items()))


def setup_game(hands, agents, coins=None, top=()):
    """Build a game with fixed hands; `top` lists the next cards to be drawn, in order."""
    rest = Counter({r: COPIES for r in ROLES})
    for h in hands:
        rest.subtract(h)
    rest.subtract(top)
    deck = list(rest.elements()) + list(reversed(top))
    seats = [(f"P{i}", a) for i, a in enumerate(agents)]
    return Game(seats, seed=1, rules=Rules(parallel_polls=False),
                setup={"hands": hands, "coins": coins or [2] * len(hands), "deck": deck})


class TestRules(unittest.TestCase):
    def test_blocked_assassination_still_costs_three(self):
        a = Scripted(action=lambda o, opts, c: pick(opts, action="assassinate", target=1))
        b = Scripted(block=lambda o, opts, c: pick(opts, block="Contessa"))
        g = setup_game([["Assassin", "Duke"], ["Contessa", "Captain"], ["Duke", "Captain"]],
                       [a, b, Scripted()], coins=[3, 2, 2])
        g.play_turn(g.players[0])
        self.assertEqual(g.players[0].coins, 0)
        self.assertEqual(len(g.players[1].hand), 2)

    def test_caught_contessa_bluff_loses_both_cards(self):
        a = Scripted(action=lambda o, opts, c: pick(opts, action="assassinate", target=1),
                     challenge=lambda o, opts, c: 1 if "Contessa" in c else 0)
        b = Scripted(block=lambda o, opts, c: pick(opts, block="Contessa"))
        g = setup_game([["Assassin", "Duke"], ["Duke", "Captain"], ["Ambassador", "Captain"]],
                       [a, b, Scripted()], coins=[3, 2, 2])
        g.play_turn(g.players[0])
        self.assertFalse(g.players[1].alive)
        causes = [e["cause"] for e in g.events if e["type"] == "lose_influence"]
        self.assertEqual(causes, ["caught_bluff", "assassination"])

    def test_ten_coins_forces_coup_and_player_picks_target(self):
        g = setup_game([["Duke", "Duke"], ["Captain", "Captain"], ["Contessa", "Contessa"]],
                       [Scripted()] * 3, coins=[10, 2, 2])
        opts = g.action_options(g.players[0])
        self.assertTrue(all(o["action"] == "coup" for o in opts))
        self.assertEqual({o["target"] for o in opts}, {1, 2})

    def test_unaffordable_actions_not_offered(self):
        g = setup_game([["Duke", "Duke"], ["Captain", "Captain"]], [Scripted()] * 2, coins=[2, 2])
        acts = {o["action"] for o in g.action_options(g.players[0])}
        self.assertNotIn("coup", acts)
        self.assertNotIn("assassinate", acts)

    def test_exchange_with_duplicates_conserves_cards(self):
        a = Scripted(action=lambda o, opts, c: pick(opts, action="exchange"),
                     exchange=lambda o, opts, c: next(i for i, x in enumerate(opts) if sorted(x["keep"]) == ["Captain", "Duke"]))
        g = setup_game([["Duke", "Duke"], ["Captain", "Contessa"], ["Assassin", "Ambassador"]],
                       [a, Scripted(), Scripted()], top=["Duke", "Captain"])
        g.play_turn(g.players[0])  # invariants are asserted inside play_turn
        self.assertEqual(sorted(g.players[0].hand), ["Captain", "Duke"])
        self.assertEqual(Counter(g.deck)["Duke"], 2)
        ex = [d for d in g.decisions if d["kind"] == "exchange"][0]
        self.assertEqual(len(ex["options"]), 2)  # {Duke,Duke} and {Captain,Duke}; no phantom duplicates

    def test_serial_poll_first_received_challenger_resolves_and_all_are_recorded(self):
        yes = Scripted(challenge=lambda o, opts, c: 1)
        actor = Scripted(action=lambda o, opts, c: pick(opts, action="tax"))
        g = setup_game([["Captain", "Contessa"], ["Captain", "Contessa"], ["Assassin", "Ambassador"], ["Duke", "Duke"]],
                       [yes, Scripted(), actor, yes])
        g.play_turn(g.players[2])
        ch = [e for e in g.events if e["type"] == "challenge"][0]
        self.assertEqual(ch["challenger"], 3)
        self.assertEqual(ch["also_wanted"], [0])
        polled = [d["seat"] for d in g.decisions if d["kind"] == "challenge"]
        self.assertEqual(sorted(polled), [0, 1, 3])
        self.assertEqual(g.players[2].coins, 2)  # bluff caught: no tax

    def test_failed_challenge_replaces_card_and_action_goes_ahead(self):
        a = Scripted(action=lambda o, opts, c: pick(opts, action="tax"))
        b = Scripted(challenge=lambda o, opts, c: 1)
        g = setup_game([["Duke", "Captain"], ["Contessa", "Assassin"]], [a, b])
        g.play_turn(g.players[0])
        self.assertEqual(g.players[0].coins, 5)
        self.assertEqual(len(g.players[0].hand), 2)
        self.assertEqual(len(g.players[1].hand), 1)

    def test_steal_takes_what_target_has(self):
        a = Scripted(action=lambda o, opts, c: pick(opts, action="steal", target=1))
        g = setup_game([["Captain", "Duke"], ["Contessa", "Assassin"]], [a, Scripted()], coins=[2, 1])
        g.play_turn(g.players[0])
        self.assertEqual((g.players[0].coins, g.players[1].coins), (3, 0))

    def test_observation_hides_other_hands(self):
        g = setup_game([["Captain", "Duke"], ["Contessa", "Assassin"]], [Scripted(), Scripted()])
        obs = g.observation(g.players[0])
        self.assertNotIn("Assassin", render_prompt(obs, "action", g.action_options(g.players[0]), "x").split("YOUR HIDDEN CARDS")[0].split("== GAME STATE ==")[1])
        self.assertTrue(all("hand" not in p for p in obs["players"]))


class TestFullGames(unittest.TestCase):
    def test_thousands_of_random_games_keep_invariants(self):
        rng = random.Random(0)
        timeouts = 0
        for i in range(2500):
            n = 2 + i % 5
            seats = [(f"P{j}", RandomAgent(rng.random())) for j in range(n)]
            r = Game(seats, seed=i, rules=Rules(parallel_polls=False)).play()
            timeouts += r["timeout"]
            if not r["timeout"]:
                self.assertEqual(sorted(r["placements"].values()), list(range(1, n + 1)))
        self.assertLess(timeouts, 50)

    def test_bot_mix_games(self):
        for i in range(300):
            seats = [("honest", HonestAgent()), ("heur", HeuristicAgent(seed=i)), ("rand", RandomAgent(i))]
            Game(seats, seed=i, rules=Rules(parallel_polls=False)).play()


class TestLLMPlumbing(unittest.TestCase):
    def test_parse_choice(self):
        self.assertEqual(parse_choice('think {not json} ... {"reasoning": "a {b}", "choice": 2}', 3), (2, "a {b}"))
        self.assertEqual(parse_choice('{"choice": "1"}', 2)[0], 1)
        with self.assertRaises(ValueError):
            parse_choice('{"choice": 5}', 3)
        with self.assertRaises(ValueError):
            parse_choice("no json here", 3)

    def test_retry_then_success(self):
        replies = iter(["garbage", '{"reasoning": "ok", "choice": 1}'])
        agent = LLMAgent(lambda prompt: next(replies), backoff=(0,))
        r = agent.decide({"players": [], "you": "P0", "hand": ["Duke"], "coins": 2, "deck_size": 5,
                          "history": [], "private_notes": []}, "challenge",
                         [{"label": "a"}, {"label": "b"}], "ctx")
        self.assertEqual(r["choice"], 1)
        self.assertEqual(r["meta"]["attempts"], 2)
        self.assertFalse(r["meta"]["fallback"])

    def test_unavailable_player_does_not_generate_a_random_move(self):
        agent = LLMAgent(lambda prompt: "never valid", backoff=(0,))
        with self.assertRaisesRegex(RuntimeError, "after 3 attempts"):
            agent.decide({"players": [], "you": "P0", "hand": ["Duke"], "coins": 2, "deck_size": 5,
                          "history": [], "private_notes": []}, "challenge",
                         [{"label": "a"}, {"label": "b"}], "ctx")


if __name__ == "__main__":
    unittest.main()
