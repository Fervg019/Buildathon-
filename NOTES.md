# NOTES — Mech Royale bot log

One change per commit. Score column is the **official** practice score (mean of `16 - place`
over 48 matches) reported back after submitting that exact commit.

| Commit (full hash) | Version / change | Official score |
| --- | --- | --- |
| `0d0aed0c7c981c330fc7767ee695e59a6d373970` | v0: unchanged kit example bot + repo layout (Dockerfile, wrapper, map module, NOTES, MIT). | — |
| `ccd25fce67fae0cfb6ff07df6d18b5488ea7d454` | v1 survival basics: `world.py` (4 m walkable grid, LOS, time-sliced A*), zone-aware goal (early move into next circle), stuck recovery (jump + jet). | (pending) |
| `5861f30178cc56a1974cfe078ba8ed8b98486e7b` | v2 defence: face attackers (rear armour), strafe/dodge incoming fire, break LOS behind a building to regen, re-engage when healthy. Added `los_fast` (wall-height grid). | (pending) |
| `568e51edf88a616874da3c6efbdd7a10c7e80403` | v3 economy: loot only when safe (supply drops > weapons > cubes, inside next circle, hp ≥ 60 %, nobody in LOS), upgrade priority armour/HP → fire rate/damage. | (pending) |

## Rules we follow

- One idea per submission; never mix changes.
- Local `check/check.py` = protocol/timing only, never a skill score.
- Official practice score is the truth. Do not tune to practice seeds (finals use new seeds).

## Notes on the kit (things that differ from the brief)

- Map files live in a `v2/` subfolder: `/arena/v2/world.json`, `/arena/v2/terrain.i16`.
- `check/check.py` is not a simulator: observations do not react to actions.
- Answer only the newest queued observation; echo `tick` exactly; logs go to stderr only.
