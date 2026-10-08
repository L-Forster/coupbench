"""LLM player: renders the game as text, calls a model through a shell command
or an OpenAI-compatible API, parses a JSON choice and retries on bad output."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request

RULES = """You are playing Coup, a bluffing card game. The goal is to be the last player with influence (face-down cards).

DECK: 15 cards, 3 each of Duke, Assassin, Captain, Ambassador, Contessa. Each player starts with 2 face-down cards and 2 coins. Lost cards are revealed face up and stay out of the game.

ACTIONS (one per turn):
- Income: +1 coin. Cannot be blocked or challenged.
- Foreign Aid: +2 coins. Any player may block by claiming Duke.
- Coup: pay 7 coins, target loses an influence. Cannot be blocked or challenged. Mandatory if you start your turn with 10+ coins.
- Tax (claim Duke): +3 coins.
- Assassinate (claim Assassin): pay 3 coins, target loses an influence. The target may block by claiming Contessa. A successful block keeps the 3 coins spent. A successful challenge to the Assassin claim refunds the 3 coins.
- Steal (claim Captain): take up to 2 coins from the target. The target may block by claiming Captain or Ambassador.
- Exchange (claim Ambassador): draw 2 cards, then keep as many cards as you had and return the rest.

CLAIMS AND CHALLENGES: You may claim any character, whether or not you hold it. Any other player may challenge a claim (including a block). If challenged and you hold the card, you may reveal it, shuffle it into the deck and draw a replacement, and the challenger loses an influence; your action or block goes ahead. You may instead concede, even when you hold the card. If you do not prove your claim, you lose an influence and the action or block fails.
Note: if you are assassinated, falsely claim Contessa and are caught, you lose one influence for the failed bluff and then another to the assassination. If you unsuccessfully challenge an Assassin, you lose one influence for the challenge and then another if the assassination succeeds. After a failed challenge, a surviving target may still block.
Eliminated players return their coins to the Treasury and cannot act. With two players, the starting player receives only 1 coin.
TABLE TALK: You may include an optional public \"message\" in your reply to negotiate, threaten, promise or bluff. No agreement is binding. You cannot show hidden cards or give or lend coins. Statements about cards are only statements, not proof. The last surviving player wins; there is no second place."""



def render_prompt(obs, kind, options, context, error_note=None):
    lines = [RULES, "", "== GAME STATE =="]
    for p in obs["players"]:
        me = " (YOU)" if p["name"] == obs["you"] else ""
        status = "eliminated" if not p["alive"] else f"{p['influence']} hidden card(s), {p['coins']} coins"
        rev = f"; revealed: {', '.join(p['revealed'])}" if p["revealed"] else ""
        lines.append(f"- {p['name']}{me}: {status}{rev}")
    lines.append(f"Cards in the deck: {obs['deck_size']}")
    lines += ["", f"YOUR HIDDEN CARDS: {', '.join(obs['hand'])}", f"YOUR COINS: {obs['coins']}"]
    lines += ["", "== PUBLIC HISTORY (turn order is the player list order) =="]
    lines += obs["history"] or ["(nothing yet)"]
    if obs["private_notes"]:
        lines += ["", "== YOUR PRIVATE NOTES =="] + obs["private_notes"]
    lines += ["", "== DECISION ==", context, ""]
    lines += [f"{i}. {o['label']}" for i, o in enumerate(options)]
    lines += [
        "",
        'Reply with a single JSON object and nothing else: {"choice": <option number>, "message": "optional public table talk"}',
    ]
    if error_note:
        lines += ["", f"NOTE: {error_note}"]
    return "\n".join(lines)


def parse_reply(text, n_options):
    """Validate the last JSON object containing a choice, retaining table talk."""
    dec = json.JSONDecoder()
    for i in range(len(text) - 1, -1, -1):
        if text[i] != "{":
            continue
        try:
            obj, _ = dec.raw_decode(text[i:])
        except ValueError:
            continue
        if isinstance(obj, dict) and "choice" in obj:
            c = obj["choice"]
            if isinstance(c, str) and c.strip().isdigit():
                c = int(c.strip())
            if isinstance(c, int) and not isinstance(c, bool) and 0 <= c < n_options:
                message = obj.get("message", "")
                if not isinstance(message, str):
                    raise ValueError("message must be text")
                return {"choice": c, "reasoning": str(obj.get("reasoning", "")), "message": message}
            raise ValueError(f"choice {c!r} is not a valid option number (0-{n_options - 1})")
    raise ValueError("no JSON object with a 'choice' field found")


def parse_choice(text, n_options):
    reply = parse_reply(text, n_options)
    return reply["choice"], reply["reasoning"]


class CommandTransport:
    """Runs a CLI once per decision. The prompt goes to stdin unless an argument
    contains {prompt} or {prompt_file}, which are substituted instead."""

    def __init__(self, command, timeout=300, max_concurrency=2, env=None, cwd=None):
        self.command = list(command)
        # Agentic CLIs can read files in their working directory; run them in an
        # empty folder so they cannot peek at game logs.
        self.cwd = cwd or tempfile.mkdtemp(prefix="coupbench_")
        self.timeout = timeout
        self.sem = threading.Semaphore(max_concurrency)
        self.env = {**os.environ, **(env or {})}

    def __call__(self, prompt: str) -> str:
        with self.sem:
            path = None
            try:
                cmd, stdin = [], prompt
                for arg in self.command:
                    if "{prompt_file}" in arg:
                        if path is None:
                            fd, path = tempfile.mkstemp(suffix=".txt")
                            with os.fdopen(fd, "w") as f:
                                f.write(prompt)
                        arg = arg.replace("{prompt_file}", path)
                        stdin = None
                    if "{prompt}" in arg:
                        arg = arg.replace("{prompt}", prompt)
                        stdin = None
                    cmd.append(arg)
                r = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=self.timeout, env=self.env, cwd=self.cwd)
                if r.returncode != 0:
                    # Some CLIs (claude -p) print API errors on stdout.
                    raise RuntimeError(f"exit {r.returncode}: {(r.stderr.strip() or r.stdout.strip())[-500:]}")
                return r.stdout
            finally:
                if path:
                    os.unlink(path)


class APITransport:
    """Calls an OpenAI-compatible /chat/completions endpoint (Mistral, MiMo,
    Gemini's OpenAI shim, ...). `params` is merged into the request body, e.g.
    {"reasoning_effort": "high"} or {"thinking": {"type": "enabled"}}."""

    def __init__(self, base_url, model, key_env, params=None, timeout=300, max_concurrency=2):
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.model = model
        self.key_env = key_env
        self.params = params or {}
        self.timeout = timeout
        self.sem = threading.Semaphore(max_concurrency)
        self._local = threading.local()  # one transport serves all parallel games

    @property
    def last_usage(self):
        """Token usage of the last call made from this thread."""
        return getattr(self._local, "usage", None)

    def __call__(self, prompt: str) -> str:
        key = os.environ.get(self.key_env)
        if not key:
            raise RuntimeError(f"environment variable {self.key_env} is not set")
        body = {"model": self.model, "messages": [{"role": "user", "content": prompt}], **self.params}
        req = urllib.request.Request(self.url, data=json.dumps(body).encode(), method="POST",
                                     headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        with self.sem:
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    data = json.load(r)
            except urllib.error.HTTPError as e:
                raise RuntimeError(f"HTTP {e.code}: {e.read().decode(errors='replace')[-500:]}") from None
        self._local.usage = data.get("usage")
        content = data["choices"][0]["message"].get("content") or ""
        if isinstance(content, list):  # Mistral returns [{"type": "thinking", ...}, {"type": "text", ...}]
            content = "".join(c.get("text", "") for c in content if c.get("type") == "text")
        return content


class LLMAgent:
    def __init__(self, transport, parse_retries=2, transport_retries=5, backoff=(10, 60, 300, 900, 1800), seed=None):
        self.transport = transport
        self.parse_retries = parse_retries
        self.transport_retries = transport_retries
        self.backoff = backoff

    def decide(self, obs, kind, options, context):
        note, attempts, errors = None, 0, []
        t0 = time.time()
        for _ in range(self.parse_retries + 1):
            prompt = render_prompt(obs, kind, options, context, note)
            raw = None
            for t in range(self.transport_retries + 1):
                attempts += 1
                try:
                    raw = self.transport(prompt)
                    break
                except Exception as e:  # timeouts, rate limits, CLI errors
                    errors.append(f"transport: {e}"[:300])
                    if t < self.transport_retries:
                        time.sleep(self.backoff[min(t, len(self.backoff) - 1)])
            if raw is None:
                break
            try:
                reply = parse_reply(raw, len(options))
                return {
                    **reply,
                    "meta": {"attempts": attempts, "errors": errors, "latency_s": round(time.time() - t0, 2),
                             "fallback": False, "raw": raw[-3000:],
                             **({"usage": u} if (u := getattr(self.transport, "last_usage", None)) else {})},
                }
            except ValueError as e:
                errors.append(f"parse: {e}"[:300])
                note = f"Your previous reply was invalid ({e}). Reply with only the JSON object."
        # A failed player call cannot manufacture a substitute move.
        raise RuntimeError(f"Player decision unavailable after {attempts} attempts: {errors[-1] if errors else 'no reply'}")
