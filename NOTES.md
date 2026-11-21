# NOTES — Mech Royale bot log

One change per commit. Score column is the **official** practice score (mean of `16 - place`
over 48 matches) reported back after submitting that exact commit.

| Commit (full hash) | Version / change | Official score |
| --- | --- | --- |
| (v0) | Unchanged kit example bot, baseline. | — |

## Rules we follow

- One idea per submission; never mix changes.
- Local `check/check.py` = protocol/timing only, never a skill score.
- Official practice score is the truth. Do not tune to practice seeds (finals use new seeds).

## Notes on the kit (things that differ from the brief)

- Map files live in a `v2/` subfolder: `/arena/v2/world.json`, `/arena/v2/terrain.i16`.
- `check/check.py` is not a simulator: observations do not react to actions.
- Answer only the newest queued observation; echo `tick` exactly; logs go to stderr only.
