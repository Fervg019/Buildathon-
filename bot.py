"""Mech Royale bot (MIT). Reads observations on stdin, writes actions on stdout.

v1 SURVIVAL BASICS (kept):
  - /arena loaded at startup: 4 m walkable grid + line-of-sight (world.py)
  - current/next circle known, early move into the next circle, never die to zone
  - A* around buildings with stuck recovery (jump, then jet)

v2 DEFENCE (kept, minus the part that cost us the game):
  - face the attacker: aim tracks the nearest threat (rear armour takes x1.5)
  - strafe/sidestep: perpendicular dodge away from incoming bullets and shells
  - hurt or outnumbered -> break line of sight behind a building and regenerate;
    re-engage when healthy (never forever: if hunted closely, fight)

v6 RETREAT NO LONGER STOPS US SHOOTING:
  - v4/v5 returned fire:[false,false] whenever mode=="retreat" and the target
    was beyond 60 m. Measured on the recorded stream: 165/300 ticks (55%)
    were spent in retreat and it fired on 1 of the 96 retreat ticks that had
    a target, while HP sat at p50 0.39 - a death spiral (hurt -> retreat ->
    don't shoot -> never recover -> still the weakest -> squad focuses us).
v7 DODGE THE CHARGING LASER (76 % of all damage we take):
  - An enemy charging a laser broadcasts `charging: true` + their aim, and the
    beam is ~1 deg wide, so protocol.md makes the line dodgable. Measured on a
    real match: charging on 33 ticks, we were dead centre in the beam on 29
    (0.0-0.3 deg off) and stepped aside on none of them. Laser was 1260 of the
    1665 damage we took; gun was only 405.
  - Sidestep perpendicular to the beam, on the side we are already on, and
    boost while doing it. Retreat still changes where we run, not whether we
    shoot.
"""
from __future__ import annotations

import math
import random

import mechfight as mf
from world import World

SAFE_MARGIN = 30.0      # metres inside the next circle we want to be
STUCK_S = 1.0           # seconds of no progress before we react
WAYPOINT_REACHED = 5.0  # metres

RETREAT_HP = 0.40       # hp fraction that triggers a retreat
RETREAT_HP_MANY = 0.70  # ...when 2+ enemies have line of sight to us
RENGAGE_HP = 0.70       # hp fraction we want back before re-engaging
RETREAT_MIN_S = 9.0     # stay in cover at least this long (regen needs 5 s + delay)
RETREAT_MAX_S = 16.0    # ...but never hide longer than this
THREAT_CLOSE = 50.0     # metres: an enemy this close ends the retreat, fight
ENGAGE_RANGE = 220.0    # gun range is 230 m

HEAR_S = 2.5            # seconds a sound still counts as a threat hint
PHANTOM_DIST = {"near": 45.0, "mid": 110.0, "far": 250.0}

LOOT_MIN_HP = 0.60      # only shop for loot at this hp fraction or better
LOOT_MAX_DIST = 120.0   # metres: never wander this far off the safe path for loot

AIM_HEIGHT = 4.5        # metres up the target's model that we aim at (torso centre)

# Upgrade order: armour and HP first, then fire rate and damage, then the rest.
UPGRADE_PRIORITY = [
    # tier 1: armour and HP
    "armor", "plating", "nanites",
    # tier 2: fire rate and damage
    "gun_rate", "gun_damage", "laser_damage", "mortar_reload", "lightning_efficiency",
    # tier 3: everything else that helps
    "servos", "gyros", "sensors", "gun_velocity", "gun_mag",
    "lightning_range", "lightning_chain", "mortar_splash", "laser_charge",
    "capacitor", "reactor", "jets", "mortar_velocity", "laser_pierce",
]


def _hypot2(dx, dy):
    return math.sqrt(dx * dx + dy * dy)


def _norm(x, y):
    d = _hypot2(x, y)
    if d < 1e-9:
        return 0.0, 0.0
    return x / d, y / d


class Bot:
    name = "mech-royale-v7"

    def __init__(self, hello):
        self.cfg = hello["config"]
        self.mech = self.cfg["mech"]
        self.rng = random.Random(hello.get("seed", 0))
        self.world = None
        try:
            self.world = World(hello["map"]["dir"])
        except Exception as e:
            mf.log("map load failed, steering direct:", repr(e))
        # navigation state
        self.path = None
        self.path_goal = None
        self.wp_i = 0
        self.stuck_since = None
        self.last_pos = None
        self.jump_hold = 0
        # defence state
        self.mode = "roam"          # roam | retreat
        self.cover = None
        self.cover_t = -1e9
        self.cover_threat = None
        self.retreat_t = -1e9
        self.strafe_sign = 1.0
        self.strafe_flip_t = -1e9

    # ------------------------------------------------------------------ zone
    def zone_goal(self, obs):
        """Where to walk to be safe from the zone, or None to hold position."""
        z = obs["zone"]
        me = obs["self"]
        x, y = me["pos"][0], me["pos"][1]
        ncx, ncy = z["next_center"]
        nr = z["next_radius"]
        d = _hypot2(x - ncx, y - ncy)
        if not me["in_zone"]:
            return [ncx, ncy]
        if d > max(0.0, nr - SAFE_MARGIN):
            return [ncx, ncy]
        return None

    # ------------------------------------------------------------- threats
    def threats(self, obs):
        """(visible enemies, phantom points from sounds/damage) in world coordinates."""
        me = obs["self"]
        x, y, z = me["pos"]
        vis = []
        for m in obs["mechs"]:
            if m["kind"] != "mech":
                continue
            d = _hypot2(m["pos"][0] - x, m["pos"][1] - y)
            vis.append((d, m))
        vis.sort(key=lambda t: t[0])

        phantoms = []
        for s in obs.get("sounds", ()):          # heard a shot: something is out there
            if s["kind"] in ("gun", "mortar", "explosion", "laser", "lightning", "death") and s["age"] <= HEAR_S:
                d = PHANTOM_DIST.get(s["dist"], 150.0)
                phantoms.append((s["bearing"], d, s["age"]))
        for e in obs.get("events", ()):          # got hit: aim back at the shooter
            if e["type"] == "damaged" and e.get("bearing") is not None:
                phantoms.append((e["bearing"], 90.0, 0.0))
        return vis, phantoms

    @staticmethod
    def phantom_point(me_pos, bearing, dist):
        return [me_pos[0] + math.cos(bearing) * dist, me_pos[1] + math.sin(bearing) * dist]

    # --------------------------------------------------------- cover seeking
    def pick_cover(self, obs, threat_xy):
        """A walkable point the threat cannot see, biased toward the next circle."""
        if self.world is None:
            return None
        me = obs["self"]
        x, y, z = me["pos"]
        tx, ty = threat_xy
        eye = z + self.mech["eye_height"]
        t_eye = (tx, ty, self.world.ground(tx, ty) + self.mech["eye_height"])
        away = math.atan2(y - ty, x - tx)
        zc = obs["zone"]
        ncx, ncy = zc["next_center"]
        nr = max(1.0, zc["next_radius"])
        best, best_score = None, -1e9
        for r in (35.0, 50.0, 70.0, 95.0):
            for k in range(12):
                ang = away + math.radians(-100 + k * (200 / 11.0))
                px = x + math.cos(ang) * r
                py = y + math.sin(ang) * r
                if not self.world.is_walkable(px, py):
                    continue
                pz = self.world.ground(px, py)
                if self.world.los_fast((px, py, pz + eye), t_eye):
                    continue  # still visible from there: no cover
                dz = _hypot2(px - ncx, py - ncy)
                score = -r * 0.4 + max(0.0, (nr - dz)) * 1.5
                if score > best_score:
                    best_score, best = score, [px, py]
        return best

    # ------------------------------------------------------------- economy
    def pick_upgrade(self, offer):
        """Index of the best choice: armour/HP first, then fire rate/damage."""
        ids = [c["id"] for c in offer["choices"]]
        best_i, best_rank = 0, len(UPGRADE_PRIORITY) + 1
        for i, uid in enumerate(ids):
            rank = UPGRADE_PRIORITY.index(uid) if uid in UPGRADE_PRIORITY else len(UPGRADE_PRIORITY)
            if rank < best_rank:
                best_i, best_rank = i, rank
        return best_i

    def economy_target(self, obs, safe):
        """A loot goal (supply drop / weapon / cube) when it is safe to go for it.

        Returns (x, y, kind, id) or None. `safe` means no enemy in line of sight
        near us and we are healthy: only then do we spend time on loot.
        """
        if not safe or self.world is None:
            return None
        me = obs["self"]
        x, y = me["pos"][0], me["pos"][1]
        zc = obs["zone"]
        ncx, ncy = zc["next_center"]
        nr = max(1.0, zc["next_radius"])

        def usable(px, py):
            # loot must not drag us out of the next circle or too far off path
            return _hypot2(px - ncx, py - ncy) <= nr - 10.0 and \
                   _hypot2(px - x, py - y) <= LOOT_MAX_DIST

        best, best_d = None, 1e9

        # supply drop: 3 cubes + full HP + full energy (only once it has landed
        # and nobody else is capturing it)
        for d in obs.get("drops", ()):
            if d["lands_in"] > 0.0:
                continue
            if d.get("holder") is not None and d["holder"] != me["id"]:
                continue
            if not usable(d["pos"][0], d["pos"][1]):
                continue
            dd = _hypot2(d["pos"][0] - x, d["pos"][1] - y)
            if dd < best_d:
                best, best_d = [d["pos"][0], d["pos"][1], "drop", d["id"]], dd

        # weapon: fill an empty slot, or upgrade a gun for something better
        held = {w["kind"] for w in me["weapons"] if w}
        want_swap = me["weapons"][1] is None
        if want_swap:
            for p in obs.get("pickups", ()):
                if p["kind"] == "gun" or p["kind"] in held:
                    continue
                if not usable(p["pos"][0], p["pos"][1]):
                    continue
                dd = _hypot2(p["pos"][0] - x, p["pos"][1] - y)
                if dd < best_d:
                    best, best_d = [p["pos"][0], p["pos"][1], "pickup", p["id"]], dd

        # power cubes: free HP and damage, pick them up by walking over them
        for c in obs.get("cubes", ()):
            if not usable(c["pos"][0], c["pos"][1]):
                continue
            dd = _hypot2(c["pos"][0] - x, c["pos"][1] - y)
            if dd < best_d:
                best, best_d = [c["pos"][0], c["pos"][1], "cube", c["id"]], dd
        return best

    # ------------------------------------------------------------- movement
    def navigate(self, obs, goal):
        """Move direction toward `goal` using a cached A* path (replanned lazily)."""
        me = obs["self"]
        x, y = me["pos"][0], me["pos"][1]
        t = obs["t"]
        at_goal = _hypot2(goal[0] - x, goal[1] - y) < 2.5

        if self.last_pos is None:
            self.last_pos = (x, y)
            self.stuck_since = t
        elif _hypot2(x - self.last_pos[0], y - self.last_pos[1]) > 1.0:
            self.last_pos = (x, y)
            self.stuck_since = t
        elif at_goal:
            self.last_pos = (x, y)      # arrived on purpose: not stuck, do not hop
            self.stuck_since = t
        elif t - (self.stuck_since or t) > STUCK_S:
            self.stuck_since = t
            self.last_pos = (x, y)
            self.jump_hold = 12
            self.path = None

        need_plan = (
            self.path is None
            or self.path_goal is None
            or _hypot2(goal[0] - self.path_goal[0], goal[1] - self.path_goal[1]) > 10.0
            or self.wp_i >= len(self.path)
        )
        if need_plan and self.world is not None:
            gx, gy = goal
            if self.jump_hold > 0:
                gx += self.rng.uniform(-8, 8)
                gy += self.rng.uniform(-8, 8)
            try:
                self.path = self.world.astar(x, y, gx, gy)
                self.path_goal = [goal[0], goal[1]]
                self.wp_i = 0
            except Exception as e:
                mf.log("astar failed:", repr(e))
                self.path = None

        tx, ty = goal
        if self.path:
            while self.wp_i < len(self.path) - 1:
                wx, wy = self.path[self.wp_i]
                if _hypot2(wx - x, wy - y) < WAYPOINT_REACHED:
                    self.wp_i += 1
                else:
                    break
            best = self.wp_i
            for j in range(self.wp_i, min(len(self.path), self.wp_i + 4)):
                if self.world is None or self.world.walkable_line((x, y), self.path[j]):
                    best = j
                else:
                    break
            tx, ty = self.path[best]

        dx, dy = tx - x, ty - y
        d = _hypot2(dx, dy)
        if d < 0.5:
            return [0.0, 0.0], 0.0
        return [dx / d, dy / d], d

    # ------------------------------------------------------------------ aim
    def aim_at(self, me_pos, target_xy, target_z):
        dx = target_xy[0] - me_pos[0]
        dy = target_xy[1] - me_pos[1]
        return [
            math.atan2(dy, dx),
            math.atan2(target_z - me_pos[2] - 2.5, _hypot2(dx, dy)),
        ]

    # ---------------------------------------------------------- dodge vector
    def dodge(self, obs):
        """Sidestep bullets and shells on a collision course with us."""
        me = obs["self"]
        x, y = me["pos"][0], me["pos"][1]
        vx, vy = me["vel"][0], me["vel"][1]
        out = [0.0, 0.0]
        for p in obs.get("projectiles", ()):
            rx, ry = x - p["pos"][0], y - p["pos"][1]
            pvx, pvy = p["vel"][0], p["vel"][1]
            vv = pvx * pvx + pvy * pvy
            if vv < 1.0:
                continue
            tca = (rx * pvx + ry * pvy) / vv
            if tca <= 0.0 or tca > 1.6:
                continue
            miss = _hypot2(rx - pvx * tca, ry - pvy * tca)
            limit = 13.0 if p["kind"] == "mortar" else 8.0
            if miss > limit:
                continue
            # perpendicular to the shell, on the side we are already drifting to
            side = 1.0 if (rx * -pvy + ry * pvx) >= 0 else -1.0
            px, py = -pvy * side, pvx * side
            if vx * px + vy * py < 0:      # don't fight our own momentum
                px, py = -px, -py
            out[0] += px
            out[1] += py
        return out

    # --------------------------------------------------------------- combat
    @staticmethod
    def _aim_err(actual, desired):
        """Angular difference between the torso's real aim and the one we want (rad)."""
        dy = (desired[0] - actual[0] + math.pi) % (2.0 * math.pi) - math.pi
        return math.hypot(dy, desired[1] - actual[1])

    def _gun_lead(self, me_pos, target, speed):
        """Point a straight bullet must be aimed at, given the target's velocity.

        Aims at torso height, not at `pos`: `pos[2]` is the target's FEET
        (it sits on the ground), and a ray that ends at ground level is
        reported blocked by the terrain test at its endpoint.
        """
        x, y, z = me_pos
        tx, ty, tz = target["pos"]
        tz += AIM_HEIGHT                 # torso centre, not the feet
        tv = target.get("vel") or [0.0, 0.0, 0.0]
        mz = z + self.mech["muzzle_height"]
        px, py, pz = tx, ty, tz
        tof = 0.0
        for _ in range(3):
            d = math.sqrt((px - x) ** 2 + (py - y) ** 2 + (pz - mz) ** 2)
            tof = max(0.0, d - 3.0) / speed   # shot starts 3 m out along the aim
            px = tx + tv[0] * tof
            py = ty + tv[1] * tof
            pz = tz + tv[2] * tof
        return [px, py, pz], tof

    def _mortar_lead(self, me_pos, target, stats):
        """Solve yaw/pitch for a lobbed shell onto a (possibly moving) target."""
        g = stats.get("gravity", 22.0)
        v = stats.get("speed", 72.0)
        min_pitch = self.mech.get("min_pitch", -0.7)
        max_pitch = self.mech.get("max_pitch", 1.3)
        x, y, z = me_pos
        tp = target["pos"]
        tv = target.get("vel") or [0.0, 0.0, 0.0]
        px, py, pz = tp
        yaw = math.atan2(py - y, px - x)
        pitch = 0.5
        tof = 0.0
        for _ in range(3):
            lx = x + 3.0 * math.cos(yaw)          # tube: 3 m out, z + 9.5
            ly = y + 3.0 * math.sin(yaw)
            lz = z + 9.5
            dx, dy = px - lx, py - ly
            d = math.hypot(dx, dy)
            if d < 3.0:
                return None
            h = pz - lz
            a = g * d * d / (2.0 * v * v)
            disc = d * d - 4.0 * a * (a + h)
            if disc < 0.0:
                return None                    # out of range
            T = (d - math.sqrt(disc)) / (2.0 * a)   # low arc: tan(pitch)
            pitch = math.atan(T)
            if pitch > max_pitch or pitch < min_pitch:
                return None
            cp = math.cos(pitch)
            if cp < 0.05:
                return None
            tof = d / (v * cp)
            yaw = math.atan2(dy, dx)
            px = tp[0] + tv[0] * tof
            py = tp[1] + tv[1] * tof
            pz = tp[2] + tv[2] * tof
        return [yaw, pitch], tof

    def combat(self, obs, target, tdist, mode):
        """Aim / fire / reload for a visible target. Returns a dict for act.update()."""
        me = obs["self"]
        x, y, z = me["pos"]
        mz = z + self.mech["muzzle_height"]
        w = me["weapons"]
        gun = w[0]
        gstats = gun["stats"] if gun else self.cfg["weapons"]["gun"]
        tv = target.get("vel") or [0.0, 0.0, 0.0]
        target_speed = math.hypot(tv[0], tv[1])
        tp = target["pos"]
        out: dict = {"fire": [False, False]}

        lead, _ = self._gun_lead((x, y, z), target, gstats.get("speed", 252.0))
        gun_aim = [math.atan2(lead[1] - y, lead[0] - x),
                   math.atan2(lead[2] - mz, math.hypot(lead[0] - x, lead[1] - y))]
        direct_aim = [math.atan2(tp[1] - y, tp[0] - x),
                      math.atan2(tp[2] + AIM_HEIGHT - mz,
                                 math.hypot(tp[0] - x, tp[1] - y))]
        # Line of fire is ALREADY decided by the server: a mech only appears in
        # `obs["mechs"]` when it is in range, inside the 120 deg FOV wedge and
        # nothing solid is between us (protocol, "Vision"). We used to re-check
        # it here, but aimed the ray at `pos` - the target's FEET - so the ray
        # ran into the ground at its own endpoint and came back "blocked" for
        # 98.8 % of real engagements, which stopped us firing almost completely.
        clear = True
        in_range = math.hypot(lead[0] - x, lead[1] - y) <= gstats.get("range", 230.0)

        w1 = w[1] if len(w) > 1 else None
        kind1 = w1["kind"] if w1 else None
        st1 = w1["stats"] if w1 else {}
        ready1 = bool(w1) and w1.get("cooldown", 0.0) <= 0.0

        aim = None
        suppress_gun = False

        # lightning: instant, 30 deg cone, fires alongside the gun
        if kind1 == "lightning" and ready1 and clear and tdist <= st1.get("range", 45.0) \
                and me["energy"] >= max(9.0, st1.get("energy", 3.0) * 3.0):
            out["fire"][1] = True

        # mortar: lob it at a slow target (this aim replaces the gun's)
        if kind1 == "mortar" and ready1 and clear and mode != "retreat" \
                and 20.0 <= tdist <= 200.0 and tdist > 15.0 and target_speed < 6.0:
            sol = self._mortar_lead((x, y, z), target, st1)
            if sol is not None:
                aim = sol[0]
                out["fire"][1] = True
                suppress_gun = True

        # laser: hold to charge, release near full (hitscan: aim where it is now)
        if kind1 == "laser":
            charge = w1.get("charge", 0.0)
            want = clear and mode != "retreat" and 40.0 <= tdist <= st1.get("range", 280.0)
            if charge > 0.0:
                if charge >= 0.65 or not want:
                    out["fire"][1] = False      # release the beam
                else:
                    out["fire"][1] = True       # keep charging
                aim = direct_aim
                suppress_gun = True
            elif want and ready1:
                out["fire"][1] = True           # start charging
                aim = direct_aim
                suppress_gun = True

        if aim is None:
            aim = gun_aim

        # gun: fire only with a clear line of fire and the barrel roughly on target
        if not suppress_gun:
            tol = min(0.08, max(0.015, 1.2 * math.atan2(2.5, max(15.0, tdist))))
            if clear and in_range and self._aim_err(me["aim"], aim) <= tol:
                if gun and gun.get("reload", 0.0) <= 0.0 and gun.get("ammo", 0) > 0:
                    out["fire"][0] = True

        out["aim"] = aim
        return out

    # ---------------------------------------------------------- laser dodge
    def laser_dodge(self, obs):
        """Sidestep out of a charging laser's line, which the protocol makes dodgable.

        A charged laser does up to 315 damage and is 76 % of everything we take
        (1260 of 1665 over a real 37 s window). While charging the shooter
        broadcasts `charging: true` plus their aim, and the beam is only about
        1 deg wide (mech radius 2.5 m) - protocol.md says the line is meant to
        be dodged. Measured: an enemy was charging on 33 ticks and we sat dead
        centre in the beam on 29 of them, 0.0-0.3 deg off, and stepped aside on
        none of them.

        Returns an unnormalised [dx, dy] pushing us off the line, on whichever
        side of it we are already standing.
        """
        me = obs["self"]
        x, y = me["pos"][0], me["pos"][1]
        out = [0.0, 0.0]
        for m in obs["mechs"]:
            if m["kind"] != "mech" or not m.get("charging"):
                continue
            dx, dy = x - m["pos"][0], y - m["pos"][1]
            d = _hypot2(dx, dy)
            if d < 5.0 or d > 260.0:
                continue                      # too close to aim at, or out of range
            yaw = m["aim"][0]                 # the beam travels along their aim
            bx, by = math.cos(yaw), math.sin(yaw)
            side = 1.0 if (bx * dy - by * dx) >= 0.0 else -1.0
            out[0] += -by * side              # perpendicular to the beam
            out[1] += bx * side
        return out

    # ------------------------------------------------------------------ act
    def act(self, obs):
        try:
            return self._act(obs)
        except Exception as e:
            mf.log("act failed:", repr(e))
            z = obs.get("zone", {})
            nc = z.get("next_center", [0.0, 0.0])
            p = obs.get("self", {}).get("pos", [0.0, 0.0, 0.0])
            dx, dy = nc[0] - p[0], nc[1] - p[1]
            d = _hypot2(dx, dy) or 1.0
            return {"tick": obs["tick"], "move": [dx / d, dy / d], "boost": True}

    def _act(self, obs):
        me = obs["self"]
        x, y, z = me["pos"]
        t = obs["t"]
        hp_frac = me["hp"] / max(1.0, me["max_hp"])
        act = {"tick": obs["tick"]}
        if obs.get("offer"):
            act["pick"] = self.pick_upgrade(obs["offer"])

        vis, phantoms = self.threats(obs)
        primary = vis[0][1] if vis else None
        primary_d = vis[0][0] if vis else None
        n_close = 0
        for d, m in vis:
            if d >= 140.0:
                continue
            if self.world is None or self.world.los_fast(
                    (x, y, z + 8), (m["pos"][0], m["pos"][1], m["pos"][2] + 4.5)):
                n_close += 1

        # threat point (for facing / cover), from vision first, else sounds/damage
        threat_xy = None
        if primary is not None:
            threat_xy = [primary["pos"][0], primary["pos"][1]]
        elif phantoms:
            phantoms.sort(key=lambda p: p[2])
            threat_xy = self.phantom_point((x, y), phantoms[0][0], phantoms[0][1])

        # ---------------------------------------------------- mode switching
        hunted = n_close >= 1 and primary_d is not None and primary_d < THREAT_CLOSE
        if self.mode != "retreat":
            if hp_frac < RETREAT_HP or (n_close >= 2 and hp_frac < RETREAT_HP_MANY):
                if threat_xy is not None:
                    self.mode = "retreat"
                    self.retreat_t = t
                    self.cover = None
                    self.path = None
        else:
            too_long = t - self.retreat_t > RETREAT_MAX_S
            min_done = t - self.retreat_t >= RETREAT_MIN_S
            if hunted or too_long or (min_done and hp_frac >= RENGAGE_HP) or hp_frac >= 0.95:
                self.mode = "roam"
                self.cover = None
                self.path = None

        # --------------------------------------------------------- movement
        zone_goal = self.zone_goal(obs)
        move = [0.0, 0.0]
        dist_goal = 0.0
        if not me["in_zone"]:
            # never trade zone damage for anything: zone wins
            move, dist_goal = self.navigate(obs, zone_goal or [obs["zone"]["next_center"][0],
                                                               obs["zone"]["next_center"][1]])
            if self.mode == "retreat":
                self.mode = "roam"
                self.cover = None
        elif self.mode == "retreat":
            if threat_xy is None:
                threat_xy = [obs["zone"]["next_center"][0], obs["zone"]["next_center"][1]]
            if self.cover is None or t - self.cover_t > 1.5 or (
                    self.cover_threat and _hypot2(self.cover_threat[0] - threat_xy[0],
                                                  self.cover_threat[1] - threat_xy[1]) > 25.0):
                self.cover = self.pick_cover(obs, threat_xy)
                self.cover_t = t
                self.cover_threat = list(threat_xy) if threat_xy else None
                self.path = None
            goal = self.cover or [x - (threat_xy[0] - x), y - (threat_xy[1] - y)]
            move, dist_goal = self.navigate(obs, goal)
            if zone_goal is not None and self.cover is None:
                move, dist_goal = self.navigate(obs, zone_goal)
        elif zone_goal is not None and not vis:
            move, dist_goal = self.navigate(obs, zone_goal)
        elif not vis:
            # safe and inside the next circle: spend the time on loot
            eco = self.economy_target(obs, hp_frac >= LOOT_MIN_HP and n_close == 0)
            if eco is not None:
                move, dist_goal = self.navigate(obs, eco[:2])
                if eco[2] == "pickup" and _hypot2(eco[0] - x, eco[1] - y) <= 4.0 \
                        and me["weapons"][1] is None:
                    act["take"] = eco[3]      # weapon pickup: fill the empty slot
            else:
                jx, jy = self.dodge(obs)
                if jx or jy:
                    move = list(_norm(jx, jy))
                else:
                    move, dist_goal = [0.0, 0.0], 0.0
                    self.path = None
        elif vis:
            # engage: hold ~100 m, strafe perpendicular, dodge what is flying at us
            tx, ty = threat_xy if threat_xy else (x + 1.0, y)
            dx, dy = _norm(tx - x, ty - y)
            if t - self.strafe_flip_t > 1.3:
                self.strafe_flip_t = t
                self.strafe_sign = -self.strafe_sign
                if self.world is not None and self.world.is_walkable(
                        x - dy * 12 * self.strafe_sign, y + dx * 12 * self.strafe_sign) is False:
                    self.strafe_sign = -self.strafe_sign
            range_push = 0.0
            if primary_d is not None:
                if primary_d > 130.0:
                    range_push = 0.7
                elif primary_d < 60.0:
                    range_push = -0.7
            mx = dx * range_push - dy * self.strafe_sign
            my = dy * range_push + dx * self.strafe_sign
            jx, jy = self.dodge(obs)
            mx += jx * 1.4
            my += jy * 1.4
            if zone_goal is not None:           # don't strafe out of safety
                zx, zy = _norm(zone_goal[0] - x, zone_goal[1] - y)
                mx = mx * 0.45 + zx * 1.0
                my = my * 0.45 + zy * 1.0
            move = [mx, my]
            d = _hypot2(mx, my)
            if d > 1e-6:
                move = [mx / d, my / d]
            dist_goal = 0.0
        else:
            # nothing visible: still sidestep anything aimed at us
            jx, jy = self.dodge(obs)
            if jx or jy:
                move = [_norm(jx, jy)[0], _norm(jx, jy)[1]]
            elif zone_goal is not None:
                move, dist_goal = self.navigate(obs, zone_goal)

        # ---- v7: get out of a charging laser's line (76 % of our damage) ----
        dodging_laser = False
        if me["in_zone"]:
            ld = self.laser_dodge(obs)
            if ld[0] or ld[1]:
                mx = move[0] * 0.25 + ld[0] * 1.6
                my = move[1] * 0.25 + ld[1] * 1.6
                d = _hypot2(mx, my)
                if d > 1e-6:
                    move = [mx / d, my / d]
                    dodging_laser = True       # worth the energy to clear it fast

        boost = (not me["in_zone"]) or (dist_goal > 60.0 and me["energy"] > 30) \
            or (dodging_laser and me["energy"] > 25.0)
        jump = False
        if self.jump_hold > 0:
            self.jump_hold -= 1
            jump = me["grounded"]
            boost = True
        act["move"] = move
        act["boost"] = bool(boost)
        act["jump"] = bool(jump)

        # -------------------------------------------------------- aim + fire
        if primary is not None:
            act.update(self.combat(obs, primary, primary_d, self.mode))
            if not (act.get("fire", [False, False])[0] or act.get("fire", [False, False])[1]):
                # nothing worth shooting at right now: top the gun up between fights
                gun = me["weapons"][0]
                if gun and gun.get("ammo", 0) < gun["stats"].get("mag", 12) \
                        and gun.get("reload", 0.0) <= 0.0 and primary_d > 120.0:
                    act["reload"] = True
        elif threat_xy is not None:
            # heard something: face it (front armour), do not waste ammo blind
            act["aim"] = [math.atan2(threat_xy[1] - y, threat_xy[0] - x), 0.0]
            act["fire"] = [False, False]
        else:
            act["aim"] = [math.atan2(move[1], move[0]), 0.0] if move != [0.0, 0.0] else list(me["aim"])
            act["fire"] = [False, False]
            gun = me["weapons"][0]
            if gun and gun.get("ammo", 0) < gun["stats"].get("mag", 12) \
                    and gun.get("reload", 0.0) <= 0.0:
                act["reload"] = True

        if primary is not None:
            mf.draw.line([x, y, z + 7], [primary["pos"][0], primary["pos"][1], primary["pos"][2] + 4.5], "#ff5040")
        if self.mode == "retreat" and self.cover:
            mf.draw.point([self.cover[0], self.cover[1], z], "#40ff70", 6)
        mf.draw.label(f"hp {me['hp']:.0f} {self.mode} n={len(vis)}")
        return act


mf.run(Bot)
