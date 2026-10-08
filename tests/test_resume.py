"""Interrupted games resume from their saved decisions without re-asking models."""
import json
import os
import tempfile
import unittest
import zlib

from coupbench.engine import Rules
from coupbench.run import run_game


class CountingAgent:
    """Deterministic in the game state, so a replayed game continues identically."""
    calls = 0

    def decide(self, obs, kind, options, context):
        CountingAgent.calls += 1
        h = zlib.crc32(f"{obs['you']}|{context}|{len(obs['history'])}|{obs['hand']}".encode())
        return {"choice": h % len(options), "reasoning": "", "meta": {}}


FACTORIES = {n: (lambda seed: CountingAgent()) for n in ["A", "B", "C", "D"]}
SEATING = ["A", "B", "C", "D"]


def play(out_dir, game_id="g", seed=5):
    CountingAgent.calls = 0
    res = run_game(game_id, seed, SEATING, FACTORIES, Rules(parallel_polls=False), out_dir)
    return res, CountingAgent.calls


class ResumeTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.dir, "games"))
        self.partial = os.path.join(self.dir, "games", "g.partial.jsonl")
        self.full, self.full_calls = play(self.dir)
        os.remove(os.path.join(self.dir, "games", "g.json"))
        self.assertGreater(len(self.full["decisions"]), 10)

    def write_partial(self, records, tail=""):
        with open(self.partial, "w") as f:
            f.writelines(json.dumps(r) + "\n" for r in records)
            f.write(tail)

    def test_no_partial_left_after_finish(self):
        self.assertFalse(os.path.exists(self.partial))

    def test_resume_reuses_saved_decisions(self):
        decisions = self.full["decisions"]
        for k in [1, 7, len(decisions) // 2, len(decisions) - 1]:
            self.write_partial(decisions[:k])
            res, calls = play(self.dir)
            os.remove(os.path.join(self.dir, "games", "g.json"))
            self.assertEqual(res["decisions"], decisions, k)
            self.assertEqual(res["events"], self.full["events"], k)
            self.assertEqual(res["winner"], self.full["winner"])
            self.assertEqual(res["resumed_decisions"], k)
            self.assertEqual(calls, self.full_calls - k, k)

    def test_cut_off_last_line_is_dropped(self):
        decisions = self.full["decisions"]
        self.write_partial(decisions[:5], tail='{"turn": 3, "pla')
        res, calls = play(self.dir)
        self.assertEqual(res["decisions"], decisions)
        self.assertEqual(calls, self.full_calls - 5)

    def test_mismatched_log_restarts_game(self):
        bad = [dict(r) for r in self.full["decisions"][:5]]
        bad[2]["player"] = "nobody"
        self.write_partial(bad)
        res, calls = play(self.dir)
        self.assertEqual(res["decisions"], self.full["decisions"])
        self.assertEqual(res["resumed_decisions"], 0)
        self.assertEqual(calls, self.full_calls)


if __name__ == "__main__":
    unittest.main()
