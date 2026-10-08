# Base Coup rules audit

Audited 2026-10-08 against the [published rulebook](https://www.buffalolib.org/sites/default/files/gaming-unplugged/inst/Coup%20Instructions.pdf).
Scope: the standard five-character game, 2–6 players. Reformation, Inquisitor,
and the separately labelled optional two-player variant are not enabled.

Earlier versions of the engine deviated from the published rules; those
deviations are listed and corrected below. This is a source/code/test audit, not a formal proof of
equivalence. The specification has gaps, documented below; those prevent an
honest claim of perfect equivalence for every conceivable position.

## Rule-to-implementation mapping

Code names refer to `coupbench/engine.py`; tests are in `tests/test_engine.py`
and `tests/test_rules_audit.py`. The table covers gameplay, not physical
packaging such as printed reference cards.

Setup references: [regular setup](https://rules.dized.com/game/xzsTtI3VTV-2wvos3otxIg/AM39r8IyTcmrHEMIQLGypQ/regular-setup).

| Requirement | Implementation | Verification |
|---|---|---|
| Three copies of each of five roles; shuffle and deal two privately | `Game.__init__`, `check_invariants` | Setup across all player counts; 2,800 complete bot games |
| Remaining cards form the Court | `Game.__init__`, `do_exchange` | Deck sizes and card conservation |
| Two starting coins; two-player starter gets one | `Game.__init__` | Every valid player count and starting seat |
| Previous winner starts a continuing series | `Rules.starting_seat`, `play` | Configurable starter; independent tournament games have no previous winner |
| Money public; own hand visible only to its owner | `observation` | Hidden-hand test; private Exchange test |

Action references: [published rulebook, pp. 2–4](https://www.buffalolib.org/sites/default/files/gaming-unplugged/inst/Coup%20Instructions.pdf).

| Requirement | Implementation | Verification |
|---|---|---|
| Clockwise turns; one action; no pass | `play`, `play_turn`, `action_options` | Historical seat-order check; exact action-menu test |
| Affordable actions against other living players | `action_options` | Menus at every balance 0–14, dead-target exclusion |
| Ten or more coins forces Coup | `action_options` | Exact-menu test; existing forced-Coup test |
| Income +1; unchallengeable, unblockable | `do_action` | No-reaction-window test |
| Foreign Aid +2; Duke can block | `do_action`, `block_window` | Block-eligibility test; Treasury handling |
| Coup costs seven and removes influence; no reactions | `do_action` | No-reaction-window test; random-game invariants |
| Tax claims Duke and grants three | `ACTION_ROLE`, `do_action` | Successful and caught Tax challenges |
| Assassin costs three; successful attack removes influence | `do_action` | Both assassination double-loss tests |
| Captain takes at most two coins | `do_action` | Targets with 0, 1, 2 and 5 coins |
| Ambassador draws two, keeps original hand size, returns two | `do_exchange` | Duplicate-card and one-influence cases |
| Exchange choices remain private | `observation`, `do_exchange` | Opponent observation test |

Counteraction references: [counteractions](https://rules.dized.com/game/xzsTtI3VTV-2wvos3otxIg/b4b0aSGGR_2wFKUAwit1Kw/counter-actions).

| Requirement | Implementation | Verification |
|---|---|---|
| Claims may be bluffs | Action/block options independent of hand | Exact-menu test; uncontested Contessa bluff |
| Resolve action challenge before blocking | `do_action` | Failed Assassin challenge followed by Contessa block |
| Any opponent can block Foreign Aid as Duke | `block_window` | Block-eligibility test |
| Only attack target can claim Contessa, Captain or Ambassador block | `BLOCKERS`, `block_window` | Block-eligibility test |
| Successful block cancels action but retains its cost | `block_window`, `do_action` | Blocked assassination cost test |
| Blocks can be challenged by third parties | `challenge_window` | Third-party block challenge test |
| Counteractions cannot themselves be counteracted | Block uses challenge window only | Code review; reaction-kind checks |

Challenge references: [winning/losing challenges](https://rules.dized.com/game/xzsTtI3VTV-2wvos3otxIg/l8n1mI_iSRy1RI4NMYJUIg/winning-and-losing-challenges),
[FAQ](https://rules.dized.com/game/xzsTtI3VTV-2wvos3otxIg/faq).

| Requirement | Implementation | Verification |
|---|---|---|
| Any other living player can challenge a role claim | `challenge_window` | Third-party eligibility tests |
| Challenges occur before resolution, never retroactively | `do_action`, `block_window` | Window ordering tests and code review |
| Claimant may prove or voluntarily concede | New `defend` decision | Truthful Assassin concession test |
| Proven card goes into shuffled Court, then replacement is drawn | `challenge_window` | Failed challenge test; card invariants |
| Losing player chooses a card; it remains face up and inactive | `lose_influence` | Card-choice fixtures; invariants; revealed observations |
| Successfully challenged action fails and refunds payment | `do_action` | False and voluntarily conceded Assassin claims |
| Caught Contessa costs influence plus assassination loss | `block_window`, `do_action` | Two-card regression; all nine historical cases |
| Failed Assassin challenge also permits a second loss | `do_action` | Two-card regression; surviving target may still block |

Other requirements: [table talk](https://rules.dized.com/game/xzsTtI3VTV-2wvos3otxIg/ETmWg_xJQKyPO5kCKPhOFg/negotiations-and-table-talk),
[published rulebook, pp. 2, 6–7](https://www.buffalolib.org/sites/default/files/gaming-unplugged/inst/Coup%20Instructions.pdf).

| Requirement | Implementation | Verification |
|---|---|---|
| Zero influence eliminates immediately and returns coins | `lose_influence` | Eliminated attacker, target and Treasury assertions |
| Pending steal in printed elimination example still succeeds | `block_window` | Printed-example regression |
| One survivor wins; no turn-limit draw | `play`, `result` | No post-victory Exchange; legacy limit option rejected |
| Negotiation allowed, agreements nonbinding | `speak`, optional reply `message` | Public-message replay; promise does not restrict actions |
| No lending, gifting or voluntary card display | No such operation in legal decision menus | Exact action menu; observation privacy |

## Removed additions and corrected omissions

- Removed configurable non-refunding assassinations; refunds are mandatory.
- Removed the 200-turn cap and survivor ties, including sample configurations.
- Added voluntary concession instead of forcing proof of truthful claims.
- Added coin return on elimination, with the printed pending-steal example preserved.
- Corrected two-player starting money and added an explicit starting seat.
- Stopped action decisions after the last opponent is eliminated.
- Replaced seat-based challenge/block priority with receipt-order serialization.
- Added public, nonbinding messages and updated model instructions.
- Removed random substitute moves after exhausted model retries. A failed call
  leaves a resumable partial game instead of creating a player decision.

`placements` remains a benchmark measure of elimination longevity, not a
second-place prize or a rule affecting play. Unfinished games have no placements.
Anonymity, transport concurrency, seeds, replay, and logging are harness settings.
They must not change the costs, targets, cards, or effects of a chosen action.

## Specification gaps and interface limits

1. **Concurrent declarations.** The book allows challenges but does not assign
   seat priority to simultaneous challengers or blockers. The transport now
   serializes actual reply receipt; recorded order is preserved during replay.
   API speed can consequently affect which competing declaration is accepted.
   This is an interface convention, not a rule claimed to appear in the book.
2. **Treasury exhaustion.** The box has 50 coins, but the book does not define
   insufficient-funds handling. Six-player positions can reach this boundary.
   `RulesAmbiguity` stops the run without a completed result; no partial-payment
   or unlimited-bank house rule is applied. Four-player play cannot exhaust
   the bank: each player has at most 12 coins under mandatory Coup.
3. **Timing of elimination money.** The immediate coin-return paragraph and
   printed example must be read together: a successful pending steal is paid,
   and the eliminated player's remaining coins return to the Treasury. The
   regression checks the example's transfer and the final balances.
4. **Conversation.** `Game.speak` accepts table talk during an ongoing game;
   LLM replies can include messages on any decision. The current automated
   runner does not simulate open-ended conversation between decision calls.
   This is still a difference from a physical table, not full social equivalence.
5. **Independent tournament games.** There is no prior-game winner in each
   independent sample. The schedule supplies the starting player. A continuing
   series must supply its previous winner through `starting_seat`.
6. **Repeated Foreign Aid blocks.** The book does not explicitly say whether
   a new block may be declared after a previous block was successfully
   challenged. This engine resolves one declared block. That boundary remains
   a specification question; the audit does not certify a uniquely official
   policy for repeated blocks.

## Historical impact

`python3 -m coupbench.audit runs/main` scans the original unversioned logs
without changing them. Snapshot: 91 completed games; zero structural-error
games in the card, affordability, target, turn-order, Exchange, Contessa-loss,
and winner checks. See [per-game evidence](historical-rules-impact.json).

The broader scan found zero structural-error games across 1,328 completed
games (1,200 bot, 92 main, 30 mock, 6 pilot); see
[all-run counts](all-runs-rules-check.json). Counts are snapshots taken while
the main run was still in progress.

| Historical discrepancy | Occurrences | Games |
|---|---:|---:|
| Assassin payment not refunded | 14 | 13 |
| Eliminated player retains nonzero coins | 230 | 90 |
| Truthful claimant not offered concession | 425 | 90 |
| Competing challengers resolved by seat priority | 137 | 71 |
| Exchange resolved after victory | 5 | 5 |

Every original game used a player interface without negotiation. These counts
identify observed behavior or missing choices, not the number of winners that
would change. Logs cannot establish counterfactual strategies. No original
games were rewritten or rerun. Version checks prevent unversioned partial
decisions/results from silently joining the corrected rules run.

## Verification

Run `python3 -m unittest discover -s tests`. The suite covers the regression
cases above, interrupted-game replay, 2,500 random games with 2–6 players and
300 mixed-bot games. Passing these checks supports the documented mechanics;
it does not resolve the specification gaps or prove every possible execution.

At the time of the audit, all 42 tests passed, and a local mock-CLI tournament
completed and resumed correctly. No paid model calls were used for verification.
