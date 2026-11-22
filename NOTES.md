# NOTES — Mech Royale bot log

One change per commit. Score column is the **official** practice score (mean of `16 - place`
over 48 matches) reported back after submitting that exact commit.

| Commit (full hash) | Version / change | Official score |
| --- | --- | --- |
| `0d0aed0c7c981c330fc7767ee695e59a6d373970` | v0: unchanged kit example bot + repo layout (Dockerfile, wrapper, map module, NOTES, MIT). | — |
| `ccd25fce67fae0cfb6ff07df6d18b5488ea7d454` | v1 survival basics: `world.py` (4 m walkable grid, LOS, time-sliced A*), zone-aware goal (early move into next circle), stuck recovery (jump + jet). | (pending) |
| `5861f30178cc56a1974cfe078ba8ed8b98486e7b` | v2 defence: face attackers (rear armour), strafe/dodge incoming fire, break LOS behind a building to regen, re-engage when healthy. Added `los_fast` (wall-height grid). | (pending) |
| `568e51edf88a616874da3c6efbdd7a10c7e80403` | v3 economy: loot only when safe (supply drops > weapons > cubes, inside next circle, hp ≥ 60 %, nobody in LOS), upgrade priority armour/HP → fire rate/damage. | (pending) |
| `640ab848f8fafc7aa0e55141ae9efa2e7677758f` | v4 combat quality: gun lead prediction, laser charge→release, lightning in range, mortar arc on slow targets, fire only with a clear line of fire. | **0.25** (receipt `BB-6EC6E6B5`) |
| `8450f1f967a75496a2acd293553ffc3892f9f735` | Build only, no bot change: Dockerfile base image pinned by `sha256:` digest (`python:3.13.15-slim-bookworm` OCI index, includes linux/amd64) in addition to the tag. | (pending) |
| `675e609dd3a4c10b824f1634224a537e00716d3f` | v5 **fix — the bot could not shoot.** The line-of-fire ray was aimed at `target["pos"]`, which is the target's **feet** (measured: `pos[2] - ground`, p50 = 0.00), so the ray dipped into the ground at its own endpoint and `los_fast` reported **blocked for 98.8 %** of real engagements. `clear` gates the gun, laser, mortar and lightning, so v4 fired **0 shots in 300 ticks**. Now aims at torso height and trusts `obs["mechs"]`, which the server only fills when the target is genuinely visible. | **1.98** (receipt `BB-D6E3ED18`) |
| `76922f2cbdbd0c312a6aa1ec4d5d031da8f41b3e` | v6 **fix — retreat stopped us shooting.** `combat()` returned `fire:[false,false]` whenever `mode=="retreat"` and the target was beyond 60 m. Measured: **165/300 ticks (55 %) in retreat**, firing on **1 of 96** retreat ticks that had a target, HP p50 0.39 — death spiral. Retreat now only changes *where* we move; the gun costs no energy so firing is free. Recorded-stream fires: **26 → 99**. | **2.04** |
| `3b5f42ee508cb22a28667b2efed61bb67271acfa` | v7 **fix — dodging the charging laser.** Laser was **1260 of 1665 damage taken (76 %)**; gun only 405. All 169 visible enemies held a laser. On the 33 ticks one was charging we sat **0.0–0.3° dead centre** in the beam and stepped aside on **none** of them, even though `protocol.md` telegraphs `charging` + `aim` precisely so the line can be dodged. Now sidestep perpendicular to their aim (**33/33 ticks, mean 89.6°**) and boost to clear the 2.5 m beam width inside the 0.75 s charge. | (pending) |

## Rules we follow

- One idea per submission; never mix changes.
- Local `check/check.py` = protocol/timing only, never a skill score.
- Official practice score is the truth. Do not tune to practice seeds (finals use new seeds).

## Target

**Target score 7.0** (baseline 1.15, cutoff ~5.6, top ~6.3). Current best **2.04**.

| Version | Score | Fires / 300 ticks | What it proved |
| --- | --- | --- | --- |
| v4 `8450f1f` | 0.25 | 0 | Bot fired **0** shots in 300 ticks — LOS ray aimed at the target's feet |
| v5 `675e609` | 1.98 | 26 | Fixed line of fire. 7.9×, above the 1.15 baseline |
| v6 `76922f2` | **2.04** | 99 | Retreat no longer suppresses fire — 3.8× more shooting, but only +0.06 |
| v7 `3b5f42e` | (pending) | 99 | Dodge the charging laser — **76 % of all damage taken** |

v6 taught us that *dealing* more damage barely moved the score: firing tripled and
the score went up 0.06. The constraint is *surviving*, and the measured cause of
our damage is standing still inside a telegraphed laser beam.

## What the 0.25 taught us

`check.py` cannot catch a bot that does nothing useful — it only validates that we
*reply*, on time, with a schema-valid action. So v4 passed every check while:

1. **It never fired.** `combat()` gated every weapon on `los_fast(muzzle, lead)`, and
   `lead` was `target["pos"]` = the target's **feet**. The ray fell from our 7 m muzzle
   to ground level, and `los_fast` returns False when `z <= ground + 0.2`, so the ray
   tripped its own ground test at the endpoint: **blocked on 167/169 = 98.8 %** of
   real engagements. Measured on the recorded stream: **v4 fired 0 times in 300 ticks.**
2. **Then it stopped fighting.** 55 % of ticks were spent in `retreat`, which returns
   `fire: [false,false]` whenever the target is beyond 60 m — firing on **1 of 96**
   retreat ticks that had a target. HP sat at p50 0.39, so it was a death spiral:
   hurt → retreat → don't shoot → never recover → still the weakest → squad focuses us.

Fix (1) shipped as **v5 → 1.98** (7.9×, now above the 1.15 baseline).
Fix (2) shipped as **v6** (fires 26 → 99 on the recorded stream).

## Confirmed from the submission page (scorer `builders-3`)

- **6 submissions per hour** (the brief said 4). A rejected entry still uses a slot, but
  failed builds/checks **never affect your best score** — rejections are free information.
- A bot that crashes or stops answering is **scored, not rejected**: it stays in the match
  standing still as a target. Crashing loses placement points, it does not disqualify.
- Submit = public repo URL + **optional** 40-char commit hash (empty = newest commit on the
  default branch). We always paste the hash explicitly.
- ≤ 20 MB at the submitted commit. The repo may be made **private** once scored; the
  organizers keep a copy of every commit they score.
- Practice = 48 matches vs 15 house bots (**4 easy, 4 medium, 4 hard, 3 expert**) acting as
  one squad: shared vision, never fight each other, hunt the weakest mech first.
- Builds: linux/amd64, network on, ≤ 10 min, uid 65534, no disk writes outside `/tmp` (64 MB).
- Cutoff: **Sun 9:00 PM GMT+5**.

## Notes on the kit (things that differ from the brief)

- Map files live in a `v2/` subfolder: `/arena/v2/world.json`, `/arena/v2/terrain.i16`.
- `check/check.py` is not a simulator: observations do not react to actions.
- Answer only the newest queued observation; echo `tick` exactly; logs go to stderr only.

## v9 candidate — survival policy and bounded navigation

Official score pending. The supplied ZIP identifies its bot as v7. This v9
candidate can be overlaid on the existing submission checkout.

- **Positioning.** Replaced the timed retreat state with a pressure check that can
  keep the bot in cover as long as necessary. Cover scoring considers up to four
  contacts, their sight lines, approach cost, proximity, and the current and next
  circles. Gunfire and laser-charge sounds give an approximate threat direction
  when contacts are hidden. A map ray separates remembered contacts from firing
  targets because the protocol keeps reporting mech positions after LOS breaks.
- **Movement.** Lead-time-based zone routing aims for the near side of the next
  circle. It takes priority when the remaining shrink window runs short. A*
  uses an octile heuristic and a fixed expansion budget. A partial route ends at
  a reached grid cell and replans from there. Removed debug drawing from the hot
  path.
- **Evasion.** Score candidate moves against forward laser lines and projectile
  intercepts. Account for current velocity, acceleration, beam pitch, zone edge
  and approximate wall clearance. Keep some heading persistence to reduce
  left/right reversals. Use boost in short defensive bursts.
- **Combat and economy.** Account for cube-scaled muzzle and target dimensions.
  Prefer an aligned, weakened, or charging opponent over repeatedly switching
  to the nearest contact. Check the predicted gun line at torso height. Seek
  cubes, safe crates, supply drops, and lightning. Lightning can short enemy
  lasers and avoids the extra cook-off damage of carrying mortar. Keep the arm
  gun and favor defensive and mobility upgrades before weapon upgrades.
- **Packaging.** Use the existing pinned Python base image, copy only the
  runtime Python files, set UID/GID 65534, and disable bytecode writes. The map
  remains available for local checks and is supplied by the scorer at /arena.

### Local verification

- The official kit conformance replay passed all 300 observations on one
  logical CPU. Two fresh policy instances emitted identical actions across
  the replay. The replay recorded 34 gun-trigger decisions with v9. Trigger
  decisions on fixed observations are not a damage or placement metric.
- A synthetic one-CPU stress case with 15 charging contacts and 24
  projectiles ran 160 direct calls in under 5 ms per call on this host.
  Focused checks covered lateral dodge, rejection of an away-pointing beam,
  map boundaries, and partial A* endpoints.
- Docker was unavailable in this workspace and the host rejected switching to
  UID 65534, so container startup still needs a Docker smoke test. The archive
  includes the unmodified protocol, schema, map, and checker.
- The first placement test requires the official 48-match practice evaluation.
  Record its commit SHA and score here. Placement cannot be inferred from the
  recorded-stream checker or the synthetic stress case.

## v10 candidate — early power and moving cover

v9's official practice score was **1.41** (user-reported). The run's individual
match records were unavailable. v6 remains the best measured baseline at
**2.04**. The **7.00** objective requires a new official practice result.

- Use quiet periods to search for cubes, weapons, safe crates, and isolated
  drones. Loot outside the next circle is eligible when the current circle
  and rotation deadline permit it. Briefly remember pickup positions when
  they leave view. Equip a nearby laser or lightning even during a fight.
- Choose cover based on both the destination and approach, including where
  moving pursuers could reopen a firing angle. Use sustained sprint bursts
  while escaping and leave a window to regenerate energy. Preserve health
  regeneration when hurt and out of contact.
- Keep pressure on an isolated weakened opponent. At safe range, charge
  the laser and use the gun in parallel when both aim solutions agree.
  Ignore beam telegraphs with a solid obstruction in the way.
- Prefer wider A* routes around walls. Allow more time for urgent zone
  rotation and aim several metres past the next circle's safety buffer.

Local checks on one logical CPU: 300/300 conformance observations passed,
zero late or invalid responses. The final observed p99 was 8.4 ms and the
maximum was 11.8 ms. Two independent runs generated the same actions.
A separate 160-tick synthetic stress case with 15 charging contacts and
24 projectiles peaked at 23.02 ms. Fixed observations and synthetic loads
measure policy behavior and latency. Official placement remains unmeasured
for v10. Docker was unavailable in the editing environment.

Run `python check/check.py -- python -u bot.py`, then the Docker smoke test
in README.md. Submit the exact tested commit for the 48-match practice run.

## v11 candidate — convert isolated fights into eliminations

The reported v10 score was **3.15**. v11 targets a failure observed in a
controlled chase test: the older strafe could keep the bot out of its own
lightning range while an isolated enemy was moving away.

- Give lightning priority over an idle laser when a swap is available.
  Approach isolated laser-armed enemies when healthy, and stabilize
  inside lightning range. Avoid trading with groups or pursuing during
  urgent rotation. Use short sprint bursts to get within range while
  preserving enough energy to fire. Prevent a needless dodge when a
  well-aligned lightning shot can interrupt the opposing charge.
- Keep a consistent strafe side, account for recent target acceleration
  in gun lead, and favor attackers that recently landed high damage.
  Check the laser ray before charging, and validate horizontal and
  vertical gun alignment independently.
- Prioritize the first three cubes, collect nearby safe cubes while
  fighting at range, and stay in a nearly complete supply capture when
  the heal is worth the exposure. Dashes and dodges still respect urgency.

The newly attached official kit has the same protocol and recorded checker
stream as the existing kit. v11 passed all 300 observations on one logical
CPU with zero invalid or late answers (p99 6.2 ms, max 6.6 ms in the final
checker run). Separate stress testing peaked at 8.75 ms across 160 ticks
with 15 charging contacts and 24 projectiles. Two runs of the conformance
stream produced identical actions. In a simplified flat 12-second test
against a retreating 6 m/s target, v10 never got a lightning shot and v11
first fired at 2.5 s. These are local checks. v11's official placement
score and the 7.00 target require a builders-4 practice run.

Run `python check/check.py -- python -u bot.py` and submit the tested commit
to the official 96-match scorer. A Docker runtime was unavailable here.
