"""Deterministic survival policy for Mech Royale (MIT)."""
from __future__ import annotations

import math
import random

import mechfight as mf
from world import World

STUCK_S = 0.9
WAYPOINT_REACHED = 2.4
TRACK_LIFETIME = 6.0
LOOT_MAX_DIST = 120.0
HEADINGS = [(math.cos(i * math.tau / 16), math.sin(i * math.tau / 16))
            for i in range(16)]

AIM_HEIGHT = 4.5        # metres up the target's model that we aim at (torso centre)

# Upgrade order: armour and HP first, then fire rate and damage, then the rest.
UPGRADE_PRIORITY = [
    # tier 1: armour and HP
    "nanites", "armor", "plating", "servos", "jets",
    "gun_damage", "gun_rate", "laser_damage", "laser_charge",
    "reactor", "capacitor", "sensors",
    # tier 2: fire rate and damage
    "lightning_efficiency", "mortar_reload",
    # tier 3: everything else that helps
    "gyros", "gun_velocity", "gun_mag",
    "lightning_range", "lightning_chain", "mortar_splash",
    "mortar_velocity", "laser_pierce",
]


def _hypot2(dx, dy):
    return math.sqrt(dx * dx + dy * dy)


def _norm(x, y):
    d = _hypot2(x, y)
    if d < 1e-9:
        return 0.0, 0.0
    return x / d, y / d


class Bot:
    name = "mech-royale-v13"

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
        self.last_combat = -math.inf
        self.search_goal = None
        self.search_until = -math.inf
        self.searched = []
        self.loot = None
        self.loot_until = -math.inf
        self.sprinting = False
        self.laser_fired_at = {}
        self.beam_danger = False

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
                  travel > 0 and remaining <= 1.65 * travel + 13.0)
        return buffer, urgent

    def rotation_goal(self, obs, buffer):
        """Enter the next circle along its near edge, preferring walkable ground."""
        x, y = obs["self"]["pos"][:2]
        zone = obs["zone"]
        nx, ny = zone["next_center"]
        cx, cy = zone["center"]
        radius = max(0.0, zone["next_radius"] - buffer - 5.0)
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
            target_z = pz + AIM_HEIGHT * (1.0 + 0.05 * actor.get("cubes", 0))
            self.tracks[actor["id"]] = (now, px, py, target_z, *actor["vel"][:2])
            distance = math.hypot(px - x, py - y)
            if (self.world is None or
                    self.world.los_fast((x, y, eye), (px, py, target_z + 1.5))):
                vis.append((distance, actor))
        vis.sort(key=lambda entry: (entry[0], entry[1]["id"]))
        for event in obs.get("events", ()):
            if event["type"] == "kill":
                self.tracks.pop(event["target"], None)
        self.tracks = {key: track for key, track in self.tracks.items()
                       if now - track[0] <= TRACK_LIFETIME}
        contacts = [(px, py, pz, 1.0 - 0.13 * (now - seen), vx, vy)
                    for seen, px, py, pz, vx, vy in self.tracks.values()
                    if math.hypot(px - x, py - y) < 315.0]
        contacts.sort(key=lambda p: math.hypot(p[0] - x, p[1] - y) / p[3])
        return vis, contacts[:4]

    def pick_cover(self, obs, contacts, buffer, urgent):
        """Rate both the approach and the destination against moving pursuers."""
        me = obs["self"]
        x, y, z = me["pos"]
        height = me["stats"]["height"] * 0.6
        zone = obs["zone"]
        center, next_center = zone["center"], zone["next_center"]
        radius, next_radius = zone["radius"], zone["next_radius"]
        current_next = math.dist((x, y), next_center)
        best = None
        best_cost = math.inf
        points = [(x, y, 0.0)]
        if self.cover is not None:
            points.append((*self.cover, math.dist((x, y), self.cover)))
        for r in (15.0, 31.0, 58.0):
            points.extend((x + dx * r, y + dy * r, r) for dx, dy in HEADINGS)
        threats = []
        for tx, ty, tz, confidence, vx, vy in contacts:
            future_x, future_y = tx + 1.6 * vx, ty + 1.6 * vy
            future_z = tz
            if self.world:
                future_z += self.world.ground(future_x, future_y) - self.world.ground(tx, ty)
            threats.append(((tx, ty, tz), (future_x, future_y, future_z), confidence))

        for px, py, distance in points:
            point = (px, py)
            if math.dist(point, center) > radius - min(4.0, radius * 0.16):
                continue
            next_dist = math.dist(point, next_center)
            if urgent and next_dist > max(next_radius - buffer, current_next - 5.0):
                continue
            if distance and self.world and not self.world.is_walkable(px, py):
                continue
            direct = not self.world or self.world.walkable_line((x, y), point)
            cost = 0.47 * distance + (0 if direct else 28.0)
            cost += (1.3 if urgent else 0.24) * max(0.0, next_dist - next_radius + buffer)
            if point == self.cover:
                cost -= 5.0
            pz = self.world.ground(px, py) if self.world and distance else z
            surface = (px, py, pz + height)
            mx, my = (x + px) * 0.5, (y + py) * 0.5
            mz = self.world.ground(mx, my) if self.world and distance else z
            middle = (mx, my, mz + height)
            for current, future, confidence in threats:
                separation = min(math.dist(point, current[:2]), math.dist(point, future[:2]))
                cost += confidence * 1.15 * max(0.0, 81.0 - separation)
                if separation < 285.0:
                    exposure = (285.0 - separation) / 285.0
                    if not self.world or self.world.los_fast(surface, current):
                        cost += confidence * (32.0 + 48.0 * exposure)
                        if distance < 5.0:
                            cost += 20.0
                    elif self.world.los_fast(surface, future):
                        cost += confidence * 22.0 * exposure
                    if distance > 14.0 and self.world and self.world.los_fast(middle, current):
                        cost += confidence * 17.0 * exposure
                if cost >= best_cost:
                    break
            if cost < best_cost:
                best, best_cost = point, cost
        return best

    def search_for_loot(self, obs):
        """Sweep nearby ground when vision has yielded no safe loot."""
        here = tuple(obs["self"]["pos"][:2])
        zone = obs["zone"]
        if self.search_goal and (math.dist(here, self.search_goal) < 8
                                 or obs["t"] >= self.search_until):
            self.searched = (self.searched + [self.search_goal])[-9:]
            self.search_goal = None
        if self.search_goal and math.dist(self.search_goal, zone["center"]) < zone["radius"] - 7:
            return self.search_goal
        if not self.world:
            return None
        options = []
        for dx, dy in HEADINGS:
            candidate = here[0] + 48 * dx, here[1] + 48 * dy
            if not self.world.is_walkable(*candidate):
                continue
            if math.dist(candidate, zone["center"]) > zone["radius"] - 10:
                continue
            route_penalty = 0 if self.world.walkable_line(here, candidate) else 31.0
            overlap = sum(max(0, 65.0 - math.dist(candidate, old)) for old in self.searched)
            remaining = math.dist(candidate, zone["next_center"]) - zone["next_radius"]
            map_edge = min(candidate[0] - self.world.min_x, self.world.max_x - candidate[0],
                           candidate[1] - self.world.min_y, self.world.max_y - candidate[1])
            cost = route_penalty + overlap + 0.6 * max(0, remaining) + 0.2 * max(0, 28 - map_edge)
            options.append((cost, candidate))
        self.search_goal = min(options)[1] if options else None
        self.search_until = obs["t"] + 10.0
        return self.search_goal

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

    def economy_target(self, obs, contacts):
        """Rank visible rewards and carry a walking goal through brief vision loss."""
        if self.world is None:
            return None
        me = obs["self"]
        x, y, z = me["pos"]
        zone = obs["zone"]
        secondary = me["weapons"][1]
        now = obs["t"]
        best, best_cost = None, math.inf
        taken = {e.get("id") for e in obs.get("events", ()) if e["type"] == "took"}

        def consider(item, kind, value):
            nonlocal best, best_cost
            px, py, pz = item["pos"]
            point = (px, py)
            distance = math.dist(point, (x, y))
            if distance > LOOT_MAX_DIST or not self.world.is_walkable(px, py):
                return
            if math.dist(point, zone["center"]) > zone["radius"] - 5:
                return
            excess = max(0.0, math.dist(point, zone["next_center"]) - zone["next_radius"])
            if now + 1.8 * (distance + excess) / me["stats"]["walk_speed"] + 8 > zone["shrink_end"]:
                return
            if abs(pz - self.world.ground(px, py)) > (6 if kind == "npc" else 4):
                return
            if any(math.dist(point, (tx, ty)) < 65 * confidence
                   for tx, ty, _, confidence, _, _ in contacts):
                return
            direct = self.world.walkable_line((x, y), point)
            cost = (distance + (0 if direct else 28) + 0.20 * excess + 5) / value
            if self.loot is not None and item["id"] == self.loot[3]:
                cost *= 0.78
            if cost < best_cost:
                best, best_cost = (px, py, kind, item["id"], pz), cost

        for item in obs.get("cubes", ()):
            if me["cubes"] < self.cfg["cubes"]["stat_cap"]:
                consider(item, "cube", 2.8)
        for item in obs.get("pickups", ()):
            kind = item["kind"]
            if not secondary and kind in ("laser", "lightning"):
                consider(item, "pickup", 3.7 if kind == "laser" else 2.8)
        for item in obs.get("drops", ()):
            if item["lands_in"] <= 5 and item.get("holder") in (None, me["id"]):
                consider(item, "drop", 4.0)
        if not contacts and me["cubes"] < 10 and me["hp"] > 0.64 * me["max_hp"]:
            muzzle = (x, y, z + me["stats"]["muzzle_height"])
            for item in obs.get("crates", ()):
                px, py, pz = item["pos"]
                if math.dist((x, y), (px, py)) < 105 and self.world.los_fast(muzzle, (px, py, pz + 1)):
                    consider(item, "crate", 0.84)
            farming_drone = self.loot and self.loot[2] == "npc"
            if me["weapons"][0] and (farming_drone or me["weapons"][0].get("ammo", 0) >= 8):
                for item in obs["mechs"]:
                    px, py, pz = item["pos"]
                    if (item["kind"] == "npc" and math.dist((x, y), (px, py)) < 80
                            and self.world.los_fast(muzzle, (px, py, pz + 1.5))):
                        consider(item, "npc", 1.5)
        if best:
            self.loot, self.loot_until = best, now + 2.5
        elif (self.loot and self.loot[2] in ("pickup", "cube", "drop")
              and not (self.loot[2] == "pickup" and secondary)
              and self.loot[3] not in taken and now < self.loot_until
              and math.dist((x, y), self.loot[:2]) > 4.0
              and not contacts):
            best = self.loot
        else:
            self.loot = None
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
        self.beam_danger = False
        for m in obs["mechs"]:
            if m["kind"] != "mech" or not m.get("charging"):
                continue
            px, py, pz = m["pos"]
            yaw, pitch = m["aim"]
            bx, by = math.cos(yaw), math.sin(yaw)
            dx, dy = x - px, y - py
            along = dx * bx + dy * by
            lateral = bx * dy - by * dx
            height = pz + self.mech["muzzle_height"] * (1 + 0.05 * m.get("cubes", 0))
            beam_z = height + along * math.tan(pitch)
            if (0.0 < along < 300.0 and abs(lateral) < radius + 8.0
                    and z - 4.0 < beam_z < z + stats["height"] + 4.0):
                if not self.world or self.world.los_fast((px, py, height), (x, y, beam_z)):
                    lasers.append((bx, by, lateral))
        self.beam_danger = bool(lasers)
        shots = []
        gun_margin = radius + 0.5
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
            width = radius + 5.0 if mortar else gun_margin
            gravity = self.cfg["weapons"]["mortar"]["gravity"] if mortar else 0.0
            shot_z = pz + dz * eta - 0.5 * gravity * eta * eta
            feet = z + vz * eta
            if not feet - width <= shot_z <= feet + stats["height"] + width:
                continue
            # Reserve boost for trajectories that could actually intersect.
            if math.hypot(rx - eta * sx, ry - eta * sy) >= width + 2.1:
                continue
            shots.append((eta, px, py, dx, dy, width))
        if not lasers and not shots:
            self.evade = (0.0, 0.0)
            return desired, boost

        shots.sort()
        boost = boost or (me["grounded"] and me["energy"] > (8 if lasers else 20))
        speed = stats["boost_speed"] if boost and me["grounded"] else stats["walk_speed"]
        secondary = me["weapons"][1]
        if secondary and secondary["kind"] == "laser" and secondary.get("charge", 0) > 0:
            speed *= stats["charge_move_mult"]
        accel = stats["ground_accel"] if me["grounded"] else stats["air_accel"]
        zone = obs["zone"]
        cx, cy = zone["center"]
        old_edge = math.hypot(x - cx, y - cy)
        best = desired
        best_score = -math.inf
        near_shots = shots[:8]
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
                score += min(7.0, abs(bx * dy - by * dx)) * 1.05
            for eta, sx, sy, svx, svy, width in near_shots:
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
        return [px, py, pz]

    def combat(self, obs, target, distance, movement_urgent):
        me = obs["self"]
        x, y, z = me["pos"]
        gun, secondary = me["weapons"]
        muzzle_height = me["stats"]["muzzle_height"]
        muzzle = (x, y, z + muzzle_height)
        gstats = gun["stats"] if gun else self.cfg["weapons"]["gun"]
        tp = target["pos"]
        scale = 1.0 + 0.05 * target.get("cubes", 0)
        height = target.get("aim_height", AIM_HEIGHT * scale)
        lead = self._gun_lead(me["pos"], target, gstats["speed"], muzzle_height)
        direct = [math.atan2(tp[1] - y, tp[0] - x),
                  math.atan2(tp[2] + height - muzzle[2], distance)]
        aim = [math.atan2(lead[1] - y, lead[0] - x),
               math.atan2(lead[2] - muzzle[2], math.hypot(lead[0] - x, lead[1] - y))]
        fire = [False, False]
        kind = secondary["kind"] if secondary else None
        gun_aim = aim
        if kind == "lightning":
            stats = secondary["stats"]
            yaw_error = abs((direct[0] - me["aim"][0] + math.pi) % math.tau - math.pi)
            fire[1] = (math.hypot(distance, tp[2] + height - muzzle[2]) <= stats["range"] - 0.5
                       and yaw_error < math.radians(stats["cone_deg"] * 0.42)
                       and me["energy"] > stats["energy"] + 7.0)
        elif kind == "laser" and target.get("kind") == "mech":
            stats = secondary["stats"]
            charge = secondary.get("charge", 0.0)
            error = self._aim_err(me["aim"], direct)
            target_center = (tp[0], tp[1], tp[2] + height)
            within_range = 25.0 < math.dist(muzzle, target_center) < stats["range"] - 1.5
            open_ray = within_range and (not self.world or self.world.los_fast(muzzle, target_center))
            aligned = open_ray and error < max(0.012, math.atan2(2.8 * scale, max(10.0, distance)))
            elapsed = obs["t"] - self.laser_fired_at.get(target["id"], -math.inf)
            enemy_reloading = 0 <= elapsed < 0.88
            incoming_laser = self.beam_danger or target.get("charging", False)
            if charge > 0.0:
                cube_stats = self.cfg["cubes"]
                multiplier = 1 + cube_stats["damage_per_cube"] * min(me["cubes"], cube_stats["stat_cap"])
                fraction = stats["min_frac"] + (1 - stats["min_frac"]) * min(1, charge / stats["charge"])
                lethal = fraction * stats["damage"] * multiplier * 0.82 >= target.get("hp", math.inf)
                full = charge >= stats["charge"] - 0.045
                break_mobility = movement_urgent or incoming_laser and not enemy_reloading
                charged_enough = charge >= stats["min_charge"] - 0.015
                release = aligned and charged_enough and (lethal or full or break_mobility)
                force_release = not open_ray or (break_mobility and charge >= 0.35)
                aim = direct
                fire[1] = not (release or force_release)
            elif (not movement_urgent and not incoming_laser and open_ray
                    and secondary["cooldown"] <= 0.05 and error < 0.11
                    and (self.evade == (0.0, 0.0) or enemy_reloading)):
                aim = direct
                fire[1] = True

        radius = target.get("aim_height", 2.5 * scale)
        tolerance = max(0.012, min(0.18, math.atan2(radius * 1.15, max(7.0, distance))))
        aligned = (gun and gun.get("ammo", 0) > 0 and gun.get("reload", 0.0) <= 0
                   and gun["cooldown"] < 0.11 and math.dist(muzzle, lead) <= gstats["range"] + 2.0
                   and self._aim_err(me["aim"], gun_aim) <= tolerance
                   and self._aim_err(aim, gun_aim) <= tolerance)
        if aligned:
            current = (tp[0], tp[1], tp[2] + height)
            fire[0] = (self.world is None or self.world.los_fast(muzzle, lead)
                       or self.world.los_fast(muzzle, current))
        return {"aim": aim, "fire": fire}

    def reload_when_safe(self, obs, act, target):
        gun = obs["self"]["weapons"][0]
        if gun and gun.get("reload", 0.0) <= 0 and not act.get("fire", (False, False))[0]:
            ammo = gun.get("ammo", 0)
            if not ammo or target is None and ammo < gun["stats"]["mag"]:
                act["reload"] = True

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
        for beam in obs.get("beams", ()):
            if beam["kind"] == "laser" and beam.get("by") is not None:
                self.laser_fired_at[beam["by"]] = now - beam.get("age", 0.0)
        for event in obs.get("events", ()):
            if event["type"] == "damaged" and event.get("weapon") == "laser" and event.get("by") is not None:
                self.laser_fired_at[event["by"]] = now

        visible, contacts = self.threats(obs)
        damage = next((event for event in obs.get("events", ())
                       if event["type"] == "damaged" and event.get("bearing") is not None
                       and event.get("weapon") != "npc"), None)
        sound = next((event for event in obs.get("sounds", ()) if event["age"] < 0.35
                      and event["kind"] in ("gun", "laser", "laser_charge", "mortar", "lightning")
                      and event["dist"] == "near"), None)
        if any(e["type"] in ("damaged", "hit") for e in obs.get("events", ())):
            self.last_combat = now
        bearing = damage["bearing"] if damage else sound["bearing"] if sound else None
        if bearing is not None and not visible:
            distance = 42.0 if sound else 95.0
            contacts = ([(x + math.cos(bearing) * distance, y + math.sin(bearing) * distance,
                          z + AIM_HEIGHT, 0.62, 0.0, 0.0)] + contacts)[:4]

        buffer, urgent = self.zone_limits(obs)
        nearby = sum(d < 215.0 for d, _ in visible)
        close = visible and visible[0][0] < 47.0
        secondary = me["weapons"][1]
        lightning = secondary and secondary["kind"] == "lightning" and me["energy"] > 18
        target = None
        distance = math.inf
        if visible:
            distance, target = min(visible, key=lambda entry: self.target_cost(entry, me))
            self.target_id = target["id"]
        else:
            self.target_id = None
        weak_enemy = target and target.get("hp", 1400) < 390 and distance < 175
        outmatched = nearby > 1 or hp < (0.43 if weak_enemy else 0.64)
        retreat = bool(contacts and (outmatched or (close and not lightning and not weak_enemy)
                                     or (damage and not visible) or (not visible and hp < 0.82)))
        recovering = (hp < 0.94 and not visible and
                      (now - self.last_combat < me["stats"]["regen_delay"] + 10.0))
        goal = None
        loot = None
        zone = obs["zone"]
        if urgent:
            goal = self.rotation_goal(obs, buffer)
        if retreat and me["in_zone"]:
            if now - self.cover_t >= 0.65:
                self.cover = self.pick_cover(obs, contacts, buffer, urgent)
                self.cover_t = now
            if self.cover and (not urgent or math.dist(self.cover, zone["next_center"]) <
                               math.dist((x, y), zone["next_center"]) - 5):
                goal = self.cover
        else:
            self.cover = None
            self.cover_t = -math.inf
        if goal is None and not retreat and not recovering and not visible:
            loot = self.economy_target(obs, contacts)
            if loot and loot[2] in ("crate", "npc"):
                objects = obs["crates"] if loot[2] == "crate" else obs["mechs"]
                item = next((obj for obj in objects if obj["id"] == loot[3]), None)
                if item:
                    target = dict(item, aim_height=1.0 if loot[2] == "crate" else 1.5)
                    distance = math.dist((x, y), loot[:2])
                    if distance > 55.0:
                        goal = loot[:2]
            elif loot:
                goal = loot[:2]
            elif me["cubes"] < 8:
                goal = self.search_for_loot(obs)

        # Take a nearby useful weapon even when cover or rotation is the movement goal.
        if not secondary:
            pickups = []
            for pickup in obs.get("pickups", ()):
                px, py, pz = pickup["pos"]
                if pickup["kind"] in ("laser", "lightning") and abs(z - pz) <= 4:
                    reach = math.dist((x, y), (px, py))
                    if reach <= 22:
                        pickups.append((reach, pickup["id"], px, py))
            if pickups:
                reach, pickup_id, px, py = min(pickups)
                if reach <= self.cfg["loot"]["pickup_radius"]:
                    act["take"] = pickup_id
                elif (not urgent and not retreat and visible and visible[0][0] > 100 and
                      self.world and self.world.walkable_line((x, y), (px, py))):
                    goal = (px, py)

        if goal is None and visible:
            _, threat = visible[0]
            tx, ty = threat["pos"][:2]
            ax, ay = _norm(x - tx, y - ty)
            vx, vy = me["vel"][:2]
            # Retain the current strafe while firing. Close only to secure an isolated kill.
            preferred_range = 38.0 if lightning else 111.0
            push = min(0.95, max(-0.7, (preferred_range - visible[0][0]) / 55.0))
            if weak_enemy and not retreat:
                push = min(push, -0.37)
            if retreat:
                push = max(push, 0.55)
            sign = 1 if vx * -ay + vy * ax >= 0 else -1
            directions = [(push * ax - side * ay, push * ay + side * ax) for side in (sign, -sign)]
            directions.extend(((ax, ay), (-ay, ax), (ay, -ax)))
            for dx, dy in directions:
                candidate = x + 12 * dx, y + 12 * dy
                if (math.dist(candidate, zone["center"]) <= zone["radius"] and
                        (not self.world or self.world.walkable_line((x, y), candidate))):
                    goal = candidate
                    break

        if goal is None:
            self.last_pos = None
            self.path = None
            move = [0.0, 0.0]
        else:
            move, _ = self.navigate(obs, goal)
        long_move = goal is not None and math.dist((x, y), goal) > 10.0
        if long_move and (retreat or urgent) and me["grounded"]:
            reserve = 13.0 if retreat else 6.0
            self.sprinting = me["energy"] > (reserve if self.sprinting else reserve + 34.0)
        else:
            self.sprinting = False
        boost = self.sprinting or not me["in_zone"]
        move, boost = self.evade_fire(obs, move, boost, urgent)
        jump = False
        if self.jump_hold:
            self.jump_hold -= 1
            jump = bool(me["grounded"] and self.jump_hold == 11 and goal is not None)
            boost = boost or (goal is not None and not me["grounded"] and me["energy"] > 14)
        act.update(move=move, boost=boost, jump=jump)

        if target:
            must_move = urgent or (retreat and (nearby > 1 or hp < 0.47 or distance < 50))
            act.update(self.combat(obs, target, distance, must_move))
        elif bearing is not None:
            act["aim"] = [bearing, 0.0]
        elif contacts:
            act["aim"] = [math.atan2(contacts[0][1] - y, contacts[0][0] - x), 0.0]
        elif any(move):
            act["aim"] = [math.atan2(move[1], move[0]), 0.0]
        else:
            act["aim"] = list(me["aim"])
        self.reload_when_safe(obs, act, target)
        return act

    def target_cost(self, entry, me):
        distance, target = entry
        facing = math.atan2(target["pos"][1] - me["pos"][1], target["pos"][0] - me["pos"][0])
        turn = abs((facing - me["aim"][0] + math.pi) % math.tau - math.pi)
        hp = target.get("hp", 1400)
        cost = min(distance, 235.0) * 0.60 + max(0, distance - 225.0) * 2.5
        cost += turn * 33.0 + min(1400.0, hp) * 0.066
        if target.get("charging") and distance < 280.0:
            cost -= 15.0
        if target["id"] == self.target_id:
            cost -= 13.0
        return cost


if __name__ == "__main__":
    mf.run(Bot)
