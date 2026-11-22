"""Deterministic survival policy for Mech Royale (MIT)."""
from __future__ import annotations

import math
import random

import mechfight as mf
from world import World

STUCK_S = 0.9
WAYPOINT_REACHED = 2.4
TRACK_LIFETIME = 6.0
LOOT_MAX_DIST = 85.0
HEADINGS = [(math.cos(i * math.tau / 16), math.sin(i * math.tau / 16))
            for i in range(16)]

AIM_HEIGHT = 4.5        # metres up the target's model that we aim at (torso centre)

# Upgrade order: armour and HP first, then fire rate and damage, then the rest.
UPGRADE_PRIORITY = [
    # tier 1: armour and HP
    "armor", "plating", "nanites", "servos",
    "reactor", "capacitor", "sensors",
    # tier 2: fire rate and damage
    "gun_rate", "gun_damage", "laser_damage", "mortar_reload", "lightning_efficiency",
    # tier 3: everything else that helps
    "gyros", "gun_velocity", "gun_mag",
    "lightning_range", "lightning_chain", "mortar_splash", "laser_charge",
    "jets", "mortar_velocity", "laser_pierce",
]


def _hypot2(dx, dy):
    return math.sqrt(dx * dx + dy * dy)


def _norm(x, y):
    d = _hypot2(x, y)
    if d < 1e-9:
        return 0.0, 0.0
    return x / d, y / d


class Bot:
    name = "mech-royale-v9"

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
        self.cover = None
        self.cover_t = -1e9
        self.tracks = {}
        self.evade = (0.0, 0.0)
        self.target_id = None

    @staticmethod
    def zone_limits(obs):
        """Return an interior buffer and whether rotation has become urgent."""
        zone = obs["zone"]
        x, y = obs["self"]["pos"][:2]
        nx, ny = zone["next_center"]
        radius = zone["next_radius"]
        buffer = min(22.0, radius * 0.23)
        distance = math.hypot(x - nx, y - ny)
        remaining = zone["shrink_end"] - obs["t"]
        walk = obs["self"]["stats"]["walk_speed"]
        travel = max(0.0, distance - radius + buffer) / walk
        urgent = (not obs["self"]["in_zone"] or
                  travel > 0 and remaining <= 1.7 * travel + 9.0)
        return buffer, urgent

    def rotation_goal(self, obs, buffer):
        """Enter the next circle along its near edge, preferring walkable ground."""
        x, y = obs["self"]["pos"][:2]
        zone = obs["zone"]
        nx, ny = zone["next_center"]
        cx, cy = zone["center"]
        radius = max(0.0, zone["next_radius"] - buffer)
        angle = math.atan2(y - ny, x - nx)
        best = (nx, ny)
        best_cost = math.inf
        for scale in (1.0, 0.75, 0.35, 0.0):
            for offset in (0, 0.24, -0.24, 0.52, -0.52, 1.0, -1.0):
                px = nx + radius * scale * math.cos(angle + offset)
                py = ny + radius * scale * math.sin(angle + offset)
                if self.world and not self.world.is_walkable(px, py):
                    continue
                cost = math.hypot(px - x, py - y)
                cost += 2.0 * max(0, math.hypot(px - cx, py - cy) - zone["radius"])
                if cost < best_cost:
                    best, best_cost = (px, py), cost
        return best

    def threats(self, obs):
        me = obs["self"]
        x, y, z = me["pos"]
        eye = z + me["stats"]["eye_height"]
        now = obs["t"]
        vis = []
        for actor in obs["mechs"]:
            if actor["kind"] != "mech":
                continue
            px, py, pz = actor["pos"]
            target_z = pz + AIM_HEIGHT * (1.0 + 0.05 * actor["cubes"])
            self.tracks[actor["id"]] = (now, px, py, target_z)
            distance = math.hypot(px - x, py - y)
            if self.world is None or self.world.los_fast((x, y, eye), (px, py, target_z)):
                vis.append((distance, actor))
        vis.sort(key=lambda entry: (entry[0], entry[1]["id"]))
        for event in obs.get("events", ()):
            if event["type"] == "kill":
                self.tracks.pop(event["target"], None)
        self.tracks = {key: track for key, track in self.tracks.items()
                       if now - track[0] <= TRACK_LIFETIME}
        contacts = [(px, py, pz, 1.0 - 0.11 * (now - seen))
                    for seen, px, py, pz in self.tracks.values()]
        contacts.sort(key=lambda p: math.hypot(p[0] - x, p[1] - y) / p[3])
        return vis, contacts[:4]

    def pick_cover(self, obs, contacts, buffer, urgent):
        """Find a reachable retreat point with fewer lines of fire."""
        x, y, _ = obs["self"]["pos"]
        zone = obs["zone"]
        cx, cy = zone["center"]
        nx, ny = zone["next_center"]
        radius = zone["radius"]
        next_radius = zone["next_radius"]
        me = obs["self"]
        height = me["stats"]["height"] * 0.55
        current_next = math.hypot(x - nx, y - ny)
        best = None
        best_cost = math.inf
        points = [(x, y, 0.0)]
        if self.cover is not None:
            px, py = self.cover
            points.append((px, py, math.hypot(px - x, py - y)))
        for r in (18.0, 36.0, 62.0):
            points.extend((x + dx * r, y + dy * r, r) for dx, dy in HEADINGS)
        for px, py, r in points:
            edge = math.hypot(px - cx, py - cy) - radius
            if edge > -min(4.0, radius * 0.16):
                continue
            next_dist = math.hypot(px - nx, py - ny)
            if urgent and next_dist > max(next_radius - buffer, current_next - 5.0):
                continue
            if r and self.world and not self.world.is_walkable(px, py):
                continue
            clear_path = self.world is None or self.world.walkable_line((x, y), (px, py))
            cost = 0.47 * r + (0.0 if clear_path else 31.0)
            cost += (1.1 if urgent else 0.17) * max(0.0, next_dist - next_radius + buffer)
            if self.cover == (px, py):
                cost -= 8.0
            pz = (self.world.ground(px, py) if self.world and r else me["pos"][2]) + height
            exposed = False
            for tx, ty, tz, confidence in contacts:
                distance = math.hypot(px - tx, py - ty)
                exposure = min(1.0, max(0.0, (295.0 - distance) / 155.0))
                if exposure and (self.world is None or self.world.los_fast((px, py, pz), (tx, ty, tz))):
                    cost += confidence * exposure * 66.0
                    exposed = exposed or exposure > 0.32
                cost += confidence * max(0.0, 65.0 - distance) * 1.35
                if cost >= best_cost:
                    break
            if exposed and r < 5.0:
                cost += 20.0
            if cost < best_cost:
                best, best_cost = (px, py), cost
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
            return _hypot2(px - ncx, py - ncy) <= nr - min(10.0, nr * 0.2) and \
                   _hypot2(px - x, py - y) <= LOOT_MAX_DIST and self.world.is_walkable(px, py)

        best, best_d = None, 1e9

        # Approach a drop shortly before landing if it is uncontested.
        for d in obs.get("drops", ()):
            if d["lands_in"] > 5.0:
                continue
            if d.get("holder") is not None and d["holder"] != me["id"]:
                continue
            if not usable(d["pos"][0], d["pos"][1]):
                continue
            dd = _hypot2(d["pos"][0] - x, d["pos"][1] - y)
            if dd * 0.45 < best_d:
                best, best_d = [d["pos"][0], d["pos"][1], "drop", d["id"]], dd * 0.45

        secondary = me["weapons"][1]
        for pickup in obs.get("pickups", ()):
            kind = pickup["kind"]
            if kind not in ("lightning", "laser"):
                continue
            if secondary and (secondary["kind"] != "laser" or kind != "lightning"):
                continue
            px, py, pz = pickup["pos"]
            if not usable(px, py) or abs(pz - self.world.ground(px, py)) > 4.0:
                continue
            cost = math.hypot(px - x, py - y) * (0.65 if kind == "lightning" else 1.15)
            if cost < best_d:
                best, best_d = [px, py, "pickup", pickup["id"]], cost

        # Cubes are collected automatically on contact.
        for c in obs.get("cubes", ()):
            if (not usable(c["pos"][0], c["pos"][1]) or
                    abs(c["pos"][2] - self.world.ground(*c["pos"][:2])) > 4.0):
                continue
            dd = _hypot2(c["pos"][0] - x, c["pos"][1] - y)
            if dd < best_d:
                best, best_d = [c["pos"][0], c["pos"][1], "cube", c["id"]], dd
        if me["cubes"] < 3 and best is None:
            for crate in obs.get("crates", ()):
                px, py, pz = crate["pos"]
                distance = math.hypot(px - x, py - y)
                if distance < min(best_d, 65.0) and usable(px, py):
                    if self.world.los_fast((x, y, me["pos"][2] + me["stats"]["muzzle_height"]),
                                           (px, py, pz + 1.0)):
                        best, best_d = [px, py, "crate", crate["id"]], distance
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
        elif t - self.stuck_since > STUCK_S:
            self.stuck_since = t
            self.last_pos = (x, y)
            self.jump_hold = 12
            self.path = None

        if at_goal:
            self.path = None
            return [0.0, 0.0], 0.0
        if self.world and self.world.walkable_line((x, y), goal):
            self.path = None
            dx, dy = goal[0] - x, goal[1] - y
            distance = math.hypot(dx, dy)
            scale = max(distance, 5.0)
            return [dx / scale, dy / scale], distance

        need_plan = (
            self.path is None
            or self.path_goal is None
            or _hypot2(goal[0] - self.path_goal[0], goal[1] - self.path_goal[1]) > 7.0
            or self.wp_i >= len(self.path)
            or self.wp_i == len(self.path) - 1 and
            math.hypot(x - self.path[-1][0], y - self.path[-1][1]) < WAYPOINT_REACHED
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
        if self.path == []:
            # No progress within the grid search: do not charge a known wall.
            return [0.0, 0.0], 0.0
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
            self.wp_i = best
            tx, ty = self.path[best]

        dx, dy = tx - x, ty - y
        d = _hypot2(dx, dy)
        if d < 0.5:
            return [0.0, 0.0], 0.0
        return [dx / d, dy / d], d

    def evade_fire(self, obs, desired, boost, urgent):
        """Score short movement trajectories against beam lines and projectile intercepts."""
        me = obs["self"]
        x, y, z = me["pos"]
        vx, vy, vz = me["vel"]
        stats = me["stats"]
        radius = stats["radius"]
        lasers = []
        for m in obs["mechs"]:
            if m["kind"] != "mech" or not m.get("charging"):
                continue
            px, py, pz = m["pos"]
            yaw, pitch = m["aim"]
            bx, by = math.cos(yaw), math.sin(yaw)
            dx, dy = x - px, y - py
            along = dx * bx + dy * by
            lateral = bx * dy - by * dx
            height = pz + self.mech["muzzle_height"] * (1 + 0.05 * m["cubes"])
            beam_z = height + along * math.tan(pitch)
            if (0.0 < along < 300.0 and abs(lateral) < radius + 8.0
                    and z - 4.0 < beam_z < z + stats["height"] + 4.0):
                lasers.append((bx, by, lateral))
        shots = []
        for shot in obs.get("projectiles", ()):
            px, py, pz = shot["pos"]
            dx, dy, dz = shot["vel"]
            rx, ry = x - px, y - py
            sx, sy = dx - vx, dy - vy
            speed2 = sx * sx + sy * sy
            if speed2 < 1:
                continue
            eta = (rx * sx + ry * sy) / speed2
            if not 0.0 < eta < 0.65:
                continue
            mortar = shot["kind"] == "mortar"
            width = radius + (5.0 if mortar else 0.5)
            if math.hypot(rx - eta * sx, ry - eta * sy) < width + 7.0:
                shots.append((eta, px, py, pz, dx, dy, dz, width, mortar))
        if not lasers and not shots:
            self.evade = (0.0, 0.0)
            return desired, boost

        shots.sort()
        boost = boost or (me["grounded"] and me["energy"] > (8 if lasers else 20))
        speed = stats["boost_speed"] if boost and me["grounded"] else stats["walk_speed"]
        accel = stats["ground_accel"] if me["grounded"] else stats["air_accel"]
        zone = obs["zone"]
        cx, cy = zone["center"]
        old_edge = math.hypot(x - cx, y - cy)
        best = desired
        best_score = -math.inf
        for mx, my in (tuple(desired), self.evade, *HEADINGS):
            dvx, dvy = mx * speed - vx, my * speed - vy
            change = math.hypot(dvx, dvy) / accel
            horizon = 0.39
            ramp = min(change, horizon)
            scale = (horizon - 0.5 * ramp) if change <= horizon else horizon * horizon / (2 * change)
            dx = vx * horizon + dvx * scale
            dy = vy * horizon + dvy * scale
            px, py = x + dx, y + dy
            blocked = (self.world and self.world.is_walkable(x, y)
                       and not self.world.walkable_line((x, y), (px, py)))
            if blocked:
                continue
            score = (mx * desired[0] + my * desired[1]) * (3.0 if urgent else 1.5)
            score += (mx * self.evade[0] + my * self.evade[1]) * 1.3
            edge = math.hypot(px - cx, py - cy)
            score -= 1.2 * max(0.0, edge - max(old_edge, zone["radius"] - 1.0))
            for bx, by, lateral in lasers:
                miss = abs(lateral + bx * dy - by * dx)
                score -= 19.0 * max(0.0, radius + 1.8 - miss) / (radius + 1.8)
                score += min(5.0, abs(bx * dy - by * dx)) * 0.65
            for eta, sx, sy, sz, svx, svy, svz, width, mortar in shots[:8]:
                shot_z = sz + svz * eta - (11.0 * eta * eta if mortar else 0.0)
                feet = z + vz * eta
                if not feet - width <= shot_z <= feet + stats["height"] + width:
                    continue
                ramp = min(change, eta)
                scale = eta - 0.5 * ramp if change <= eta else eta * eta / (2 * change)
                miss = math.hypot(x + vx * eta + dvx * scale - sx - svx * eta,
                                  y + vy * eta + dvy * scale - sy - svy * eta)
                score -= 12.0 * max(0.0, width - miss) / width
            if score > best_score:
                best, best_score = (mx, my), score
        self.evade = best
        return list(best), boost

    # --------------------------------------------------------------- combat
    @staticmethod
    def _aim_err(actual, desired):
        """Angular difference between the torso's real aim and the one we want (rad)."""
        dy = (desired[0] - actual[0] + math.pi) % (2.0 * math.pi) - math.pi
        return math.hypot(dy, desired[1] - actual[1])

    def _gun_lead(self, me_pos, target, speed, muzzle_height):
        """Point a straight bullet must be aimed at, given the target's velocity.

        Aims at torso height, not at `pos`: `pos[2]` is the target's FEET
        (it sits on the ground), and a ray that ends at ground level is
        reported blocked by the terrain test at its endpoint.
        """
        x, y, z = me_pos
        tx, ty, tz = target["pos"]
        tz += target.get("aim_height", AIM_HEIGHT * (1 + 0.05 * target.get("cubes", 0)))                 # torso centre, not the feet
        tv = target.get("vel") or [0.0, 0.0, 0.0]
        mz = z + muzzle_height
        px, py, pz = tx, ty, tz
        tof = 0.0
        for _ in range(3):
            d = math.sqrt((px - x) ** 2 + (py - y) ** 2 + (pz - mz) ** 2)
            tof = max(0.0, d - 3.0) / speed   # shot starts 3 m out along the aim
            px = tx + tv[0] * tof
            py = ty + tv[1] * tof
            pz = tz + tv[2] * tof
        return [px, py, pz], tof

    def combat(self, obs, target, distance, retreat):
        me = obs["self"]
        x, y, z = me["pos"]
        gun, secondary = me["weapons"]
        muzzle_height = me["stats"]["muzzle_height"]
        mz = z + muzzle_height
        gstats = gun["stats"] if gun else self.cfg["weapons"]["gun"]
        tp = target["pos"]
        height = target.get("aim_height", AIM_HEIGHT * (1 + 0.05 * target.get("cubes", 0)))
        lead, _ = self._gun_lead(me["pos"], target, gstats["speed"], muzzle_height)
        direct = [math.atan2(tp[1] - y, tp[0] - x), math.atan2(tp[2] + height - mz, distance)]
        aim = [math.atan2(lead[1] - y, lead[0] - x),
               math.atan2(lead[2] - mz, math.hypot(lead[0] - x, lead[1] - y))]
        fire = [False, False]
        kind = secondary["kind"] if secondary else None
        if kind == "lightning":
            stats = secondary["stats"]
            yaw_error = abs((direct[0] - me["aim"][0] + math.pi) % math.tau - math.pi)
            fire[1] = (math.hypot(distance, tp[2] + height - mz) <= stats["range"] - 1.0
                       and yaw_error < math.radians(stats["cone_deg"] * 0.5)
                       and me["energy"] > stats["energy"] + 7.0)
        elif kind == "laser":
            stats = secondary["stats"]
            charge = secondary.get("charge", 0.0)
            if charge > 0.0:
                hold = charge < stats["charge"] - 0.05 and not retreat
                return {"aim": direct, "fire": [False, hold]}
            if (not retreat and target.get("kind") == "mech" and
                    secondary["cooldown"] <= 0 and 55.0 < distance < stats["range"] - 4.0 and
                    self._aim_err(me["aim"], direct) < 0.087):
                return {"aim": direct, "fire": [False, True]}
        radius = 1.0 if "aim_height" in target else 2.5 * (1 + 0.05 * target["cubes"])
        tolerance = max(0.009, min(0.11, math.atan2(radius * 0.92, max(7.0, distance))))
        muzzle = (x, y, mz)
        aligned = (gun and gun["ammo"] > 0 and gun["reload"] <= 0
                   and math.dist(muzzle, lead) <= gstats["range"]
                   and self._aim_err(me["aim"], aim) <= tolerance)
        fire[0] = bool(aligned and (self.world is None or self.world.los_fast(muzzle, lead)))
        return {"aim": aim, "fire": fire}

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
        now = obs["t"]
        hp = me["hp"] / me["max_hp"]
        act = {"tick": obs["tick"]}
        if obs.get("offer"):
            act["pick"] = self.pick_upgrade(obs["offer"])

        visible, contacts = self.threats(obs)
        damage = next((event for event in obs.get("events", ())
                       if event["type"] == "damaged" and event.get("bearing") is not None), None)
        sound = next((event for event in obs.get("sounds", ()) if event["age"] < 0.45
                      and event["kind"] in ("gun", "laser", "laser_charge", "mortar", "lightning")
                      and event["dist"] in ("near", "mid")), None)
        bearing = damage["bearing"] if damage else sound["bearing"] if sound else None
        if bearing is not None and not visible:
            distance = {"near": 45.0, "mid": 110.0}.get(sound["dist"], 90.0) if sound else 90.0
            contacts = ([(x + math.cos(bearing) * distance, y + math.sin(bearing) * distance,
                          z + AIM_HEIGHT, 0.7)] + contacts)[:4]

        buffer, urgent = self.zone_limits(obs)
        nearby = sum(d < 210.0 for d, _ in visible)
        incoming = any(m.get("charging") and d < 285.0 for d, m in visible)
        retreat = bool(contacts and (hp < 0.86 or nearby > 1 or incoming
                                    or (visible and visible[0][0] < 68.0)))
        goal = None
        loot = None
        zone = obs["zone"]
        if retreat and me["in_zone"]:
            if now - self.cover_t >= 0.7:
                self.cover = self.pick_cover(obs, contacts, buffer, urgent)
                self.cover_t = now
            goal = self.cover
            if urgent and goal and math.dist(goal, zone["next_center"]) >= math.dist((x, y), zone["next_center"]) - 3.0:
                goal = None
        else:
            self.cover = None
            self.cover_t = -1e9
        if urgent and goal is None:
            goal = self.rotation_goal(obs, buffer)
        elif goal is None and not visible and not retreat:
            loot = self.economy_target(obs, not damage and not sound)
            if loot and loot[2] != "crate":
                goal = loot[:2]
                if loot[2] == "pickup":
                    pickup = next(p for p in obs["pickups"] if p["id"] == loot[3])
                    if math.hypot(goal[0] - x, goal[1] - y) <= self.cfg["loot"]["pickup_radius"] and abs(pickup["pos"][2] - z) <= 4.0:
                        act["take"] = loot[3]
                        if me["weapons"][1]:
                            act["drop"] = 1

        if goal is None and visible:
            _, threat = visible[0]
            tx, ty = threat["pos"][:2]
            ax, ay = _norm(x - tx, y - ty)
            sign = 1 if int(now / 2.2) % 2 == 0 else -1
            candidates = [(x + 18.0 * (0.55 * ax - sign * ay), y + 18.0 * (0.55 * ay + sign * ax)),
                          (x + 18.0 * (0.55 * ax + sign * ay), y + 18.0 * (0.55 * ay - sign * ax))]
            goal = next((g for g in candidates if math.dist(g, zone["center"]) <= zone["radius"]
                         and (self.world is None or self.world.walkable_line((x, y), g))), None)

        if goal is None:
            self.last_pos = None
            self.path = None
            move, distance = [0.0, 0.0], 0.0
        else:
            move, distance = self.navigate(obs, goal)
        boost = bool(not me["in_zone"] or (distance > 10.0 and (retreat or urgent)
                                           and me["energy"] > (18 if urgent else 34)))
        move, boost = self.evade_fire(obs, move, boost, urgent)
        jump = False
        if self.jump_hold:
            self.jump_hold -= 1
            jump = bool(me["grounded"] and self.jump_hold == 11 and goal is not None)
            boost = boost or (goal is not None and not me["grounded"] and me["energy"] > 14)
        act.update(move=move, boost=boost, jump=jump)

        target = None
        if visible:
            distance, target = min(visible, key=lambda entry: self.target_cost(entry, me))
            self.target_id = target["id"]
        elif loot and loot[2] == "crate":
            crate = next(c for c in obs["crates"] if c["id"] == loot[3])
            target = dict(crate, aim_height=1.0)
            distance = math.dist((x, y), crate["pos"][:2])
        else:
            self.target_id = None
        if target:
            act.update(self.combat(obs, target, distance, retreat))
        elif bearing is not None:
            act["aim"] = [bearing, 0.0]
        elif contacts:
            act["aim"] = [math.atan2(contacts[0][1] - y, contacts[0][0] - x), 0.0]
        else:
            act["aim"] = [math.atan2(move[1], move[0]), 0.0] if any(move) else list(me["aim"])

        gun = me["weapons"][0]
        if gun and not act.get("fire", (False, False))[0] and gun["reload"] <= 0:
            if gun["ammo"] == 0 or target is None and gun["ammo"] < gun["stats"]["mag"]:
                act["reload"] = True
        return act

    def target_cost(self, entry, me):
        distance, target = entry
        facing = math.atan2(target["pos"][1] - me["pos"][1], target["pos"][0] - me["pos"][0])
        turn = abs((facing - me["aim"][0] + math.pi) % math.tau - math.pi)
        cost = distance + turn * 32.0 + 38.0 * (target["hp"] / target["max_hp"])
        if target.get("charging") and distance < 280.0:
            cost -= 22.0
        if target["id"] == self.target_id:
            cost -= 17.0
        return cost


if __name__ == "__main__":
    mf.run(Bot)
