"""Send one sample decision to every model in a config and report the reply.

    source .env && python -m coupbench.check configs/main.toml

Run this before a tournament: it catches wrong model ids, endpoints, keys and
reply formats in a few seconds per model instead of mid-run.
"""
from __future__ import annotations

import sys
import time

from .llm import parse_choice, render_prompt
from .run import build_factories, load_config

OBS = {
    "you": "P0", "hand": ["Duke", "Captain"], "coins": 2, "deck_size": 9, "history": [], "private_notes": [],
    "players": [{"name": "P0", "seat": 0, "coins": 2, "influence": 2, "revealed": [], "alive": True},
                {"name": "P1", "seat": 1, "coins": 2, "influence": 2, "revealed": [], "alive": True}],
}
OPTIONS = [{"label": "take Income (+1 coin)"}, {"label": "take Tax (+3 coins) [claims Duke]"}]


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        raise SystemExit(__doc__)
    cfg = load_config(args[0])
    factories = build_factories(cfg)
    prompt = render_prompt(OBS, "action", OPTIONS, "It is your turn. Choose an action.")
    ok = True
    for m in cfg["model"]:
        name = m["name"]
        if "bot" in m:
            print(f"{name}: bot, skipped")
            continue
        transport = factories[name](0).transport
        t0 = time.time()
        try:
            raw = transport(prompt)
            choice, reasoning = parse_choice(raw, len(OPTIONS))
            usage = getattr(transport, "last_usage", None)
            print(f"OK   {name} ({time.time() - t0:.1f}s): choice={choice}" + (f" usage={usage}" if usage else ""))
        except Exception as e:
            ok = False
            print(f"FAIL {name} ({time.time() - t0:.1f}s): {e}"[:800])
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
