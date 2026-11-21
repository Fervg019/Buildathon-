"""Mech Royale bot (MIT). Reads observations on stdin, writes actions on stdout.

v1 SURVIVAL BASICS:
  - loads /arena at startup: 4 m walkable grid + line-of-sight (world.py)
  - always knows the current and next circle, moves into the next circle early
  - A* around buildings, with stuck recovery (jump, then jet)
The wrapper (mechfight.py) already answers `ready`, echoes `tick`, skips stale
observations, and coasts on exceptions; this file adds a second safety net.
"""
from __future__ import annotations

import math
import random

import mechfight as mf
from world import World

SAFE_MARGIN = 30.0     # metres inside the next circle we want to be
STUCK_S = 1.0          # seconds of no progress before we react
WAYPOINT_REACHED = 5.0  # metres


def _hypot2(dx, dy):
    return math.sqrt(dx * dx + dy * dy)


class Bot:
    name = "mech-royale-v1"

    def __init__(self, hello):
        self.cfg = hello["config"]
        self.mech = self.cfg["mech"]
        self.walk_speed = self.mech["walk_speed"]
        self.boost_speed = self.mech["boost_speed"]
        self.rng = random.Random(hello.get("seed", 0))
        self.world = None
        try:
            self.world = World(hello["map"]["dir"])
        except Exception as e:  # never die because the map failed to load
            mf.log("map load failed, steering direct:", repr(e))
        # navigation state
        self.path = None
        self.path_goal = None
        self.wp_i = 0
        self.stuck_since = None
        self.last_pos = None
        self.jump_hold = 0  # ticks of forced jump/jet left

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
            return [ncx, ncy]            # outside now: straight in, boosting
        if d > max(0.0, nr - SAFE_MARGIN):
            return [ncx, ncy]            # not comfortably inside the next circle yet
        return None                      # safe: hold (combat/later versions use this time)

    # ------------------------------------------------------------- navigation
    def navigate(self, obs, goal):
        """Move direction toward `goal` using a cached A* path (replanned lazily)."""
        me = obs["self"]
        x, y = me["pos"][0], me["pos"][1]
        t = obs["t"]

        # stuck bookkeeping: no progress while we want to move
        if self.last_pos is None:
            self.last_pos = (x, y)
            self.stuck_since = t
        elif _hypot2(x - self.last_pos[0], y - self.last_pos[1]) > 1.0:
            self.last_pos = (x, y)
            self.stuck_since = t
        elif t - (self.stuck_since or t) > STUCK_S:
            self.stuck_since = t
            self.last_pos = (x, y)
            self.jump_hold = 12          # jump + jet for 1.2 s
            self.path = None             # force a fresh plan (with jitter below)

        need_plan = (
            self.path is None
            or self.path_goal is None
            or _hypot2(goal[0] - self.path_goal[0], goal[1] - self.path_goal[1]) > 10.0
            or self.wp_i >= len(self.path)
        )
        if need_plan and self.world is not None:
            gx, gy = goal
            if self.jump_hold > 0:       # while unsticking, aim slightly off to slide around
                gx += self.rng.uniform(-8, 8)
                gy += self.rng.uniform(-8, 8)
            try:
                self.path = self.world.astar(x, y, gx, gy)
                self.path_goal = [goal[0], goal[1]]
                self.wp_i = 0
            except Exception as e:
                mf.log("astar failed:", repr(e))
                self.path = None

        # pick the waypoint to walk to
        tx, ty = goal
        if self.path:
            while self.wp_i < len(self.path) - 1:
                wx, wy = self.path[self.wp_i]
                if _hypot2(wx - x, wy - y) < WAYPOINT_REACHED:
                    self.wp_i += 1
                else:
                    break
            # look-ahead: skip ahead while the line is clear
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
            return [0.0, 0.0], False
        return [dx / d, dy / d], d

    # ------------------------------------------------------------------ act
    def act(self, obs):
        try:
            return self._act(obs)
        except Exception as e:  # second safety net: the wrapper coasts if we raise
            mf.log("act failed:", repr(e))
            z = obs.get("zone", {})
            nc = z.get("next_center", [0.0, 0.0])
            me = obs.get("self", {})
            p = me.get("pos", [0.0, 0.0, 0.0])
            dx, dy = nc[0] - p[0], nc[1] - p[1]
            d = _hypot2(dx, dy) or 1.0
            return {"tick": obs["tick"], "move": [dx / d, dy / d], "boost": True}

    def _act(self, obs):
        me = obs["self"]
        x, y, z = me["pos"]
        act = {"tick": obs["tick"]}
        if obs.get("offer"):
            act["pick"] = 0

        # ---- move: zone safety first
        goal = self.zone_goal(obs)
        if goal is None:
            move, dist_goal = [0.0, 0.0], 0.0
            self.path = None
        else:
            move, dist_goal = self.navigate(obs, goal)
        boost = (not me["in_zone"]) or (goal is not None and dist_goal > 60.0 and me["energy"] > 30)
        jump = False
        if self.jump_hold > 0:
            self.jump_hold -= 1
            jump = me["grounded"]
            boost = True                # jet while airborne to clear the obstacle
        act["move"] = move
        act["boost"] = bool(boost)
        act["jump"] = bool(jump)

        # ---- shoot: the starter behaviour, kept unchanged in v1
        enemies = [m for m in obs["mechs"] if m["kind"] == "mech"]
        enemies.sort(key=lambda m: _hypot2(m["pos"][0] - x, m["pos"][1] - y))
        yaw = math.atan2(move[1], move[0]) if move != [0.0, 0.0] else me["aim"][0]
        act["aim"] = [yaw, 0.0]
        if enemies:
            t = enemies[0]["pos"]
            act["aim"] = [
                math.atan2(t[1] - y, t[0] - x),
                math.atan2(t[2] - z - 2.5, _hypot2(t[0] - x, t[1] - y)),
            ]
            act["fire"] = [True, True]
            mf.draw.line([x, y, z + 7], [t[0], t[1], t[2] + 4.5], "#ff5040")

        mf.draw.label(f"hp {me['hp']:.0f} {'IN ZONE' if me['in_zone'] else 'ZONE!'}")
        if goal:
            mf.draw.point([goal[0], goal[1], z], "#40c0ff", 6)
        return act


mf.run(Bot)
