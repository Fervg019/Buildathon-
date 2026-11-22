"""Map module for the mechfight bot (MIT, standard library only).

Loads the static map from `hello.map.dir` (in a match: /arena):
  - v2/world.json    bounds, terrain grid description, prisms (buildings/bridges), trees
  - v2/terrain.i16   ground heights, little-endian int16 centimetres

Provides:
  - ground(x, y)             bilinear ground height
  - a ~4 m walkable grid     (slope, step-up and headroom aware)
  - los(a, b)                line of sight / line of fire (terrain + buildings; trees do not
                             stop shots, so they are ignored here too)
  - walkable_line(a, b)      movement clearance check for path smoothing
  - astar(sx, sy, gx, gy)    A* on the grid with a fixed expansion budget
"""
from __future__ import annotations

import array
import heapq
import json
import math
import os
import sys

CELL = 4.0            # grid cell size, metres
STEP_UP = 1.2         # config.mech.step_up
HEADROOM = 9.0        # mech height: anything lower than this over your head is a wall
MAX_SLOPE_DEG = 50.0  # config.mech.max_slope_deg
LOS_STEP = 1.5        # metres between terrain samples along a sight line
MOVE_SAMPLE = 2.0     # metres between samples along a movement clearance line


def _pip(px: float, py: float, pts) -> bool:
    """Point in polygon (ray casting)."""
    inside = False
    n = len(pts)
    j = n - 1
    for i in range(n):
        xi, yi = pts[i]
        xj, yj = pts[j]
        if (yi > py) != (yj > py):
            if px < (xj - xi) * (py - yi) / (yj - yi) + xi:
                inside = not inside
        j = i
    return inside


def _seg_param(ax, ay, bx, by, cx, cy, dx, dy):
    """Parameter t along segment A->B where it crosses edge C->D, or None."""
    rx, ry = bx - ax, by - ay
    sx, sy = dx - cx, dy - cy
    denom = rx * sy - ry * sx
    if denom == 0.0:
        return None
    qpx, qpy = cx - ax, cy - ay
    t = (qpx * sy - qpy * sx) / denom
    if t < 0.0 or t > 1.0:
        return None
    u = (qpx * ry - qpy * rx) / denom
    if u < 0.0 or u > 1.0:
        return None
    return t


class World:
    def __init__(self, map_dir: str, cell: float = CELL) -> None:
        with open(os.path.join(map_dir, "v2", "world.json"), encoding="utf-8") as f:
            data = json.load(f)
        b = data["bounds"]
        self.min_x, self.max_x = float(b["minX"]), float(b["maxX"])
        self.min_y, self.max_y = float(b["minY"]), float(b["maxY"])

        t = data["terrain"]
        self.nx, self.ny = int(t["nx"]), int(t["ny"])
        self.t_min_x = float(t["minX"])
        self.t_min_y = float(t["minY"])
        self.t_step = float(t["step"])
        self.inv_step = 1.0 / self.t_step
        with open(os.path.join(map_dir, "v2", t["file"]), "rb") as f:
            raw = f.read()
        self.heights = array.array("h")
        self.heights.frombytes(raw)
        if sys.byteorder == "big":
            self.heights.byteswap()

        self.prisms = []  # (base, top, points, bbox)
        for p in data.get("prisms", []):
            ring = p["ring"]
            pts = [(float(ring[i]), float(ring[i + 1])) for i in range(0, len(ring), 2)]
            xs = [q[0] for q in pts]
            ys = [q[1] for q in pts]
            self.prisms.append(
                (float(p["base"]), float(p["top"]), pts, (min(xs), min(ys), max(xs), max(ys)))
            )

        self.cell = cell
        self.ox, self.oy = self.min_x, self.min_y
        self.cols = max(1, int(math.ceil((self.max_x - self.min_x) / cell)))
        self.rows = max(1, int(math.ceil((self.max_y - self.min_y) / cell)))
        self._hash_prisms()
        self._build_grid()
        self.roof_access = self._find_roof_access()

    # ------------------------------------------------------------- terrain
    def ground(self, x: float, y: float) -> float:
        i = (x - self.t_min_x) * self.inv_step
        j = (y - self.t_min_y) * self.inv_step
        nx, ny = self.nx, self.ny
        if i < 0.0:
            i = 0.0
        elif i > nx - 1:
            i = nx - 1.0
        if j < 0.0:
            j = 0.0
        elif j > ny - 1:
            j = ny - 1.0
        i0 = int(i)
        j0 = int(j)
        if i0 > nx - 2:
            i0 = nx - 2
        if j0 > ny - 2:
            j0 = ny - 2
        fx = i - i0
        fy = j - j0
        h = self.heights
        row = j0 * nx
        row2 = row + nx
        a = h[row + i0]
        b = h[row + i0 + 1]
        c = h[row2 + i0]
        d = h[row2 + i0 + 1]
        return ((a + (b - a) * fx) * (1.0 - fy) + (c + (d - c) * fx) * fy) * 0.01

    # ------------------------------------------------------------- prisms
    def _hash_prisms(self) -> None:
        bs = 32.0
        self.bucket_size = bs
        buckets = {}
        for idx, (_base, _top, _pts, bb) in enumerate(self.prisms):
            i0 = int(math.floor(bb[0] / bs))
            i1 = int(math.floor(bb[2] / bs))
            j0 = int(math.floor(bb[1] / bs))
            j1 = int(math.floor(bb[3] / bs))
            for i in range(i0, i1 + 1):
                for j in range(j0, j1 + 1):
                    buckets.setdefault((i, j), []).append(idx)
        self.buckets = buckets

    def _blocked(self, x: float, y: float, h: float) -> bool:
        """True when a prism makes this footprint impassable at ground height h."""
        bs = self.bucket_size
        for idx in self.buckets.get((int(math.floor(x / bs)), int(math.floor(y / bs))), ()):
            base, top, pts, bb = self.prisms[idx]
            if top <= h + STEP_UP:      # low ledge: step up over it
                continue
            if base >= h + HEADROOM:    # high overhang: walk under it
                continue
            if bb[0] <= x <= bb[2] and bb[1] <= y <= bb[3] and _pip(x, y, pts):
                return True
        return False

    # ------------------------------------------------------------- grid
    def _build_grid(self) -> None:
        cell = self.cell
        walk = bytearray(self.cols * self.rows)
        tan_max = math.tan(math.radians(MAX_SLOPE_DEG))
        for r in range(self.rows):
            cy = self.oy + (r + 0.5) * cell
            if cy < self.min_y or cy > self.max_y:
                continue
            row = r * self.cols
            for c in range(self.cols):
                cx = self.ox + (c + 0.5) * cell
                if cx < self.min_x or cx > self.max_x:
                    continue
                h = self.ground(cx, cy)
                hx = self.ground(cx + 1.5, cy) - self.ground(cx - 1.5, cy)
                hy = self.ground(cx, cy + 1.5) - self.ground(cx, cy - 1.5)
                if math.hypot(hx, hy) / 3.0 > tan_max:
                    continue
                if self._blocked(cx, cy, h):
                    continue
                walk[row + c] = 1
        self.walk = walk
        # Prefer room for the mech cylinder when choosing a route around a corner.
        # Keep narrow cells available for cases where they are the only exit.
        cols, rows = self.cols, self.rows
        self.narrow = bytearray(cols * rows)
        for r in range(1, rows - 1):
            for c in range(1, cols - 1):
                i = r * cols + c
                if walk[i]:
                    self.narrow[i] = sum(not walk[j] for j in
                                         (i - 1, i + 1, i - cols, i + cols))
        self._build_wall_heights()

    def _build_wall_heights(self) -> None:
        """Per cell: lowest base and highest top of any prism covering it.

        Feeds los_fast(): a sight line at height z is blocked where
        wall_base <= z <= wall_top. Cheap array lookups instead of polygon tests.
        """
        n = self.cols * self.rows
        self.wall_base = array.array("f", [1e9]) * n
        self.wall_top = array.array("f", [-1e9]) * n
        cell = self.cell
        for base, top, pts, bb in self.prisms:
            c0 = max(0, int((bb[0] - self.ox) / cell))
            c1 = min(self.cols - 1, int((bb[2] - self.ox) / cell))
            r0 = max(0, int((bb[1] - self.oy) / cell))
            r1 = min(self.rows - 1, int((bb[3] - self.oy) / cell))
            for r in range(r0, r1 + 1):
                cy = self.oy + (r + 0.5) * cell
                row = r * self.cols
                for c in range(c0, c1 + 1):
                    cx = self.ox + (c + 0.5) * cell
                    if _pip(cx, cy, pts):
                        i = row + c
                        if base < self.wall_base[i]:
                            self.wall_base[i] = base
                        if top > self.wall_top[i]:
                            self.wall_top[i] = top

    def roof_top(self, x, y):
        c, r = self.cell_of(x, y)
        return self.wall_top[r * self.cols + c]

    def _find_roof_access(self):
        """Pair clear vertical approaches with broad, level rooftop cells."""
        entries = []
        cols, rows, cell = self.cols, self.rows, self.cell
        tops = self.wall_top
        for r in range(4, rows - 4, 2):
            y = self.oy + (r + 0.5) * cell
            for c in range(4, cols - 4, 2):
                x = self.ox + (c + 0.5) * cell
                i = r * cols + c
                if not self.walk[i]:
                    continue
                if any(tops[i + offset] > tops[i] + 1.0
                       for offset in (-1, 1, -cols, cols, -cols-1, -cols+1, cols-1, cols+1)):
                    continue
                ground = self.ground(x, y)
                if tops[i] > ground + 0.7:
                    continue
                for dc, dr in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    delta = dc + dr * cols
                    first = next((k for k in range(2, 5)
                                  if 13.0 <= tops[i + k * delta] - ground <= 39.0), None)
                    if first is None:
                        continue
                    top = tops[i + first * delta]
                    across = dr + dc * cols
                    landing = min(first + 3, 7)
                    if not (2 <= c + landing * dc < cols - 2 and 2 <= r + landing * dr < rows - 2):
                        continue
                    first_cell = i + first * delta
                    pad_cell = i + landing * delta
                    if tops[first_cell - delta] > top + 0.6:
                        continue
                    if any(abs(tops[cell_id] - top) > 0.8 for cell_id in (
                            pad_cell, pad_cell - across, pad_cell + across,
                            pad_cell - delta, pad_cell + delta, pad_cell - 2 * delta)):
                        continue
                    if any(tops[i + k * delta + side * across] > top + 0.8
                           for k in range(1, landing + 1) for side in (-1, 0, 1)):
                        continue
                    entries.append(((x, y), (x + landing * cell * dc, y + landing * cell * dr), top))
        return entries

    def los_fast(self, a, b) -> bool:
        """Cheap line of sight using the wall-height grid (terrain + buildings).

        Same answer as los() for building-scale blockers, ~100x faster; use it for
        per-decision combat/cover checks. Trees are ignored (they never stop shots).
        """
        ax, ay, az = a
        bx, by, bz = b
        dx, dy, dz = bx - ax, by - ay, bz - az
        dist = math.hypot(dx, dy)
        n = int(dist / 3.0) + 1
        sx, sy, sz = dx / n, dy / n, dz / n
        px, py, pz = ax, ay, az
        cols, rows, wb, wt = self.cols, self.rows, self.wall_base, self.wall_top
        inv = 1.0 / self.cell
        for _ in range(n):
            px += sx
            py += sy
            pz += sz
            if pz <= self.ground(px, py) + 0.2:
                return False
            c = int((px - self.ox) * inv)
            r = int((py - self.oy) * inv)
            if c < 0 or r < 0 or c >= cols or r >= rows:
                continue
            i = r * cols + c
            if wb[i] <= pz <= wt[i]:
                return False
        return True

    def cell_of(self, x: float, y: float):
        c = int((x - self.ox) / self.cell)
        r = int((y - self.oy) / self.cell)
        if c < 0:
            c = 0
        elif c > self.cols - 1:
            c = self.cols - 1
        if r < 0:
            r = 0
        elif r > self.rows - 1:
            r = self.rows - 1
        return c, r

    def is_walkable(self, x: float, y: float) -> bool:
        if not (self.min_x <= x < self.max_x and self.min_y <= y < self.max_y):
            return False
        c, r = self.cell_of(x, y)
        return bool(self.walk[r * self.cols + c])

    def nearest_walkable(self, c: int, r: int, limit: int = 60):
        cols, rows, walk = self.cols, self.rows, self.walk
        if 0 <= c < cols and 0 <= r < rows and walk[r * cols + c]:
            return c, r
        for ring in range(1, limit + 1):
            for dc in range(-ring, ring + 1):
                for dr in (-ring, ring):
                    cc, rr = c + dc, r + dr
                    if 0 <= cc < cols and 0 <= rr < rows and walk[rr * cols + cc]:
                        return cc, rr
            for dr in range(-ring + 1, ring):
                for dc in (-ring, ring):
                    cc, rr = c + dc, r + dr
                    if 0 <= cc < cols and 0 <= rr < rows and walk[rr * cols + cc]:
                        return cc, rr
        return c, r

    # ------------------------------------------------------------- LOS
    def los(self, a, b) -> bool:
        """True when the straight segment a->b clears terrain and buildings."""
        ax, ay, az = a
        bx, by, bz = b
        dx, dy, dz = bx - ax, by - ay, bz - az
        dist = math.hypot(dx, dy)
        n = int(dist / LOS_STEP) + 1
        if n > 1:
            sx, sy, sz = dx / n, dy / n, dz / n
            px, py, pz = ax, ay, az
            for _ in range(n - 1):
                px += sx
                py += sy
                pz += sz
                if pz <= self.ground(px, py) + 0.2:
                    return False
        for prism in self.prisms:
            if self._seg_hits(ax, ay, az, bx, by, bz, prism):
                return False
        return True

    @staticmethod
    def _seg_hits(ax, ay, az, bx, by, bz, prism) -> bool:
        base, top, pts, bb = prism
        if max(ax, bx) < bb[0] or min(ax, bx) > bb[2]:
            return False
        if max(ay, by) < bb[1] or min(ay, by) > bb[3]:
            return False
        if _pip(ax, ay, pts) and base <= az <= top:
            return True
        if _pip(bx, by, pts) and base <= bz <= top:
            return True
        n = len(pts)
        for i in range(n):
            x1, y1 = pts[i]
            x2, y2 = pts[(i + 1) % n]
            t = _seg_param(ax, ay, bx, by, x1, y1, x2, y2)
            if t is not None and base <= az + (bz - az) * t <= top:
                return True
        return False

    def walkable_line(self, a, b) -> bool:
        """True when every ~2 m sample of a->b sits in a walkable cell (movement clearance)."""
        ax, ay = a[0], a[1]
        bx, by = b[0], b[1]
        dx, dy = bx - ax, by - ay
        dist = math.hypot(dx, dy)
        n = int(dist / MOVE_SAMPLE) + 1
        cols, walk = self.cols, self.walk
        sx, sy = dx / n, dy / n
        px, py = ax, ay
        for _ in range(n + 1):
            if not (self.min_x <= px < self.max_x and self.min_y <= py < self.max_y):
                return False
            c = int((px - self.ox) / self.cell)
            r = int((py - self.oy) / self.cell)
            if not walk[r * cols + c]:
                return False
            px += sx
            py += sy
        return True

    # ------------------------------------------------------------- A*
    @staticmethod
    def _h(c: int, r: int, gc: int, gr: int) -> float:
        dc = abs(c - gc)
        dr = abs(r - gr)
        return max(dc, dr) + 0.41421356 * min(dc, dr)

    def astar(self, sx: float, sy: float, gx: float, gy: float,
              budget: int = 1800):
        """A* from (sx,sy) to (gx,gy) in metres. Returns waypoints [[x,y], ...] (goal last).

        Search stops after a fixed expansion budget and returns the best partial path.
        """
        cols, rows, walk = self.cols, self.rows, self.walk
        c0, r0 = self.cell_of(sx, sy)
        c1, r1 = self.cell_of(gx, gy)
        starts_on_ground = bool(walk[r0 * cols + c0])
        c0, r0 = self.nearest_walkable(c0, r0)
        c1, r1 = self.nearest_walkable(c1, r1)
        start = r0 * cols + c0
        goal_i = r1 * cols + c1
        if start == goal_i:
            if self.is_walkable(gx, gy):
                return [[gx, gy]]
            return [[self.ox + (c1 + 0.5) * self.cell, self.oy + (r1 + 0.5) * self.cell]]

        openh = [(self._h(c0, r0, c1, r1), 0, start)]
        g = {start: 0.0}
        came = {}
        closed = set()
        best, best_h = start, self._h(c0, r0, c1, r1)
        pops = 0
        nbrs = ((1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
                (1, 1, 1.41421356), (1, -1, 1.41421356),
                (-1, 1, 1.41421356), (-1, -1, 1.41421356))
        found = False
        while openh:
            _f, _ord, cur = heapq.heappop(openh)
            if cur in closed:
                continue
            closed.add(cur)
            pops += 1
            cc = cur % cols
            cr = cur // cols
            if cur == goal_i:
                best, found = cur, True
                break
            hh = self._h(cc, cr, c1, r1)
            if hh < best_h:
                best_h, best = hh, cur
            if pops >= budget:
                break
            gc = g[cur]
            for dc, dr, cost in nbrs:
                nc2 = cc + dc
                nr2 = cr + dr
                if nc2 < 0 or nr2 < 0 or nc2 >= cols or nr2 >= rows:
                    continue
                ni = nr2 * cols + nc2
                if not walk[ni] or ni in closed:
                    continue
                if dc and dr:  # no corner cutting
                    if not walk[cr * cols + nc2] or not walk[nr2 * cols + cc]:
                        continue
                ng = gc + cost + 0.42 * self.narrow[ni]
                if ng < g.get(ni, 1e18):
                    g[ni] = ng
                    came[ni] = cur
                    heapq.heappush(openh, (ng + self._h(nc2, nr2, c1, r1), pops, ni))

        path = []
        cur = best
        while cur != start:
            path.append(cur)
            nxt = came.get(cur)
            if nxt is None:
                break
            cur = nxt
        path.reverse()
        pts = [[self.ox + (p % cols + 0.5) * self.cell,
                self.oy + (p // cols + 0.5) * self.cell] for p in path]
        if found and self.is_walkable(gx, gy):
            pts[-1] = [gx, gy]
        if not starts_on_ground:
            pts.insert(0, [self.ox + (c0 + 0.5) * self.cell,
                           self.oy + (r0 + 0.5) * self.cell])
        return pts
