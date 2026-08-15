import json
import re
import heapq
import itertools
import logging
from collections import deque

logger = logging.getLogger()
logger.setLevel(logging.INFO)

DANGER_COST = 1000
DOOR_COST_LOCKED = 5000
LOCKED_DOOR_HP_DAMAGE = 5  # guide: crossing a locked door does -5 real damage

# Base scores for tile types that don't follow the generic "cN = 400" rule.
TILE_SCORES = {"c1": 400, "c3": 550, "c4": 800, "c5": 250, "c18": 500, "c7": 250}
DEFAULT_CHALLENGE_SCORE = 400
DEFAULT_DOOR_SCORE = 1000   # any "c3X" door not explicitly listed
DEFAULT_KEY_SCORE = 50      # any "c4X" key not explicitly listed
CHALLENGE_HP_COST = 1
DEFAULT_STEP_COST = 3

# Tile types that must ALWAYS be visited when safely reachable, regardless
# of whether the profit-maximizing selection thinks it's "worth" the
# detour. Use this for tiles you want guaranteed coverage of.
FORCE_COLLECT_TYPES = {"c4", "c18"}

_DOOR_RE = re.compile(r"^c3(\d+)$")   # c30, c31, c32, ...
_KEY_RE = re.compile(r"^c4(\d+)$")    # c40, c41, c42, ...
_CHALLENGE_RE = re.compile(r"^c\d+$")

# Labels a game map may use for an IMPASSABLE tile. Only the exact string
# "wall" used to be recognized - any other spelling silently became a
# walkable "normal" tile, letting the planner route straight through it,
# which the game then rejects mid-route (every following move is wrong).
WALL_LABELS = {"wall", "walls", "barrier", "barriers", "brick", "bricks",
               "block", "blocked", "rock", "stone", "obstacle", "impassable",
               "solid", "#", "x"}

# Labels marking the agent's own tile on the map - used ONLY as a
# calibration fallback if the caller-supplied start turns out unusable.
START_LABELS = {"start", "agent", "player", "hero", "spawn"}


def _key_code_for_door(door_code):
    """c30 -> c40, c31 -> c41, ... (door and key share the same suffix,
    per the game's Red/Green/Grey/Yellow/etc. key-door color convention)."""
    m = _DOOR_RE.match(door_code)
    return f"c4{m.group(1)}" if m else None


def _tile_score(cell_lower, pos=None, door_map=None, held_keys=None):
    """
    Score for landing on a tile.

    A LOCKED door must score 0, not DEFAULT_DOOR_SCORE - it previously
    scored the full +1000 reward regardless of whether the matching key
    was held, which made the route optimizer treat every locked door as a
    free +1000 detour. In reality a locked door gives NO reward, only the
    -5 HP hazard cost (modeled separately in _hp_cost_of_path) - the
    optimizer chasing a reward that doesn't exist is exactly what produces
    a route that walks toward a door, gets nothing, and backtracks.
    """
    if pos is not None and door_map is not None and pos in door_map:
        held_keys = held_keys or frozenset()
        if _key_code_for_door(door_map[pos]) not in held_keys:
            return 0
    if cell_lower in TILE_SCORES:
        return TILE_SCORES[cell_lower]
    if _DOOR_RE.match(cell_lower):
        return DEFAULT_DOOR_SCORE
    if _KEY_RE.match(cell_lower):
        return DEFAULT_KEY_SCORE
    if _CHALLENGE_RE.match(cell_lower):
        return DEFAULT_CHALLENGE_SCORE
    return 0


# Dict-key spellings the game has used for a structured position.
_START_ROW_KEYS = ("row", "rows", "r", "y", "line")
_START_COL_KEYS = ("col", "cols", "column", "c", "x")
_START_NESTED_KEYS = ("position", "pos", "start", "start_pos", "cell", "coord",
                      "coords", "coordinates", "location", "current_position",
                      "agent_position")


def _parse_start(pos):
    """
    Parse a position from every shape the game has actually sent.

    A dict position used to be parsed by running a regex over str(dict)
    (e.g. str({"row": 4, "col": 0}) == "{'row': 4, 'col': 0}"), which only
    happened to work because Python 3.7+ dicts preserve insertion order
    and this game always sends "row" before "col". If the game ever sends
    {"col": 0, "row": 4} instead, str(dict) becomes "{'col': 0, 'row': 4}"
    and the SAME regex silently returns (0, 4) instead of (4, 0) - a
    bogus origin that desyncs every move that follows. Dicts are now
    destructured by key name instead of relying on that ordering luck.
    """
    try:
        if isinstance(pos, dict):
            for k, v in pos.items():
                if str(k).strip().lower() in _START_NESTED_KEYS:
                    return _parse_start(v)
            row_val = col_val = None
            for k, v in pos.items():
                kl = str(k).strip().lower()
                if kl in _START_ROW_KEYS and row_val is None:
                    row_val = v
                elif kl in _START_COL_KEYS and col_val is None:
                    col_val = v
            if row_val is not None and col_val is not None:
                rs = re.sub(r"[^A-Za-z0-9]", "", str(row_val))
                cs = re.sub(r"[^A-Za-z0-9]", "", str(col_val))
                if cs.isalpha():  # e.g. {"row": 5, "col": "A"} -> A5
                    return (int(rs) - 1, ord(cs[0].upper()) - ord('A'))
                return (int(rs), int(cs))
            return (0, 0)
        if isinstance(pos, (list, tuple)):
            if len(pos) == 1:
                return _parse_start(pos[0])
            if len(pos) >= 2:
                a = re.sub(r"[^A-Za-z0-9]", "", str(pos[0]))
                b = re.sub(r"[^A-Za-z0-9]", "", str(pos[1]))
                if a.isalpha():
                    return (int(b) - 1, ord(a.upper()) - ord('A'))
                if b.isalpha():
                    return (int(a) - 1, ord(b.upper()) - ord('A'))
                return (int(a), int(b))
        s = re.sub(r"[^A-Za-z0-9]", "", str(pos))
        m = re.match(r"([A-Za-z])(\d+)$", s)
        if m:
            return (int(m.group(2)) - 1, ord(m.group(1).upper()) - ord('A'))
        nums = re.findall(r"\d+", s)
        if len(nums) >= 2:
            return (int(nums[0]), int(nums[1]))
    except (ValueError, TypeError, IndexError):
        pass
    return (0, 0)


def _resolve_start(start, rows, cols, barriers, grid):
    """
    Calibrate the parsed start position against the REAL grid.

    A parsed position that lands outside the map or on top of a wall is
    definitely wrong - planning from it makes the agent walk into a wall
    on its very first step, desyncing the whole turn's route. Prefer, in
    order: the map's own start/agent tile, the common off-by-one
    (1-indexed) readings, then a clamp back into bounds.
    """
    def _ok(p):
        return 0 <= p[0] < rows and 0 <= p[1] < cols and p not in barriers

    if _ok(start):
        return start

    for pos, label in grid.items():
        if label in START_LABELS and _ok(pos):
            logger.warning(f"start {start} is out of bounds or inside a wall; "
                           f"using the map's own start tile {pos} instead")
            return pos

    r, c = start
    for cand in ((r - 1, c - 1), (r - 1, c), (r, c - 1)):
        if _ok(cand):
            logger.warning(f"start {start} is unusable; using off-by-one "
                           f"(1-indexed) reading {cand} instead")
            return cand

    clamped = (min(max(r, 0), max(rows - 1, 0)), min(max(c, 0), max(cols - 1, 0)))
    if _ok(clamped):
        logger.warning(f"start {start} is unusable; clamped to {clamped}")
        return clamped

    for pos in grid:
        if _ok(pos):
            logger.warning(f"start {start} is unusable; falling back to first "
                           f"walkable tile {pos}")
            return pos
    return start


def _build_grid(game_map):
    """
    Returns:
      grid: (r,c) -> lowercase tile string
      barriers: set of wall positions
      dangers: set of spike (c8) positions
      coins: set of coin (c7) positions
      challenges: set of generic "cN" challenge positions (excludes doors/keys)
      door_map: {(r,c): door_code}  e.g. {(3,9): "c30", (7,3): "c31"}
      key_map:  {(r,c): key_code}   e.g. {(9,0): "c40", (5,2): "c41"}
      treasure: (r,c) or None
    Supports an arbitrary number of color-coded key/door pairs — not just
    one — since the game can add more colors (Red, Green, Grey, Yellow,
    ...) between rounds.
    """
    grid = {}
    barriers, dangers, coins, challenges = set(), set(), set(), set()
    door_map, key_map = {}, {}
    treasure = None
    rows = len(game_map)
    cols = max(len(r) for r in game_map) if rows > 0 else 0
    for r, row_data in enumerate(game_map):
        for c, cell in enumerate(row_data):
            cell_lower = str(cell).lower().strip() if cell else ""
            grid[(r, c)] = cell_lower
            if cell_lower in WALL_LABELS:
                barriers.add((r, c))
            elif cell_lower == "c8":
                dangers.add((r, c))
            elif cell_lower == "c7":
                coins.add((r, c))
            elif _DOOR_RE.match(cell_lower):
                door_map[(r, c)] = cell_lower
            elif _KEY_RE.match(cell_lower):
                key_map[(r, c)] = cell_lower
            elif cell_lower == "treasure":
                treasure = (r, c)
            elif _CHALLENGE_RE.match(cell_lower):
                challenges.add((r, c))
    return grid, rows, cols, barriers, dangers, coins, challenges, door_map, key_map, treasure


DIRECTIONS = [(1, 0, "down"), (-1, 0, "up"), (0, 1, "right"), (0, -1, "left")]


def _bfs_simple(start, goal, rows, cols, barriers):
    if start == goal:
        return [start]
    queue = deque([(start, [start])])
    visited = {start}
    while queue:
        (r, c), path = queue.popleft()
        for dr, dc, _ in DIRECTIONS:
            nr, nc = r + dr, c + dc
            if not (0 <= nr < rows and 0 <= nc < cols):
                continue
            if (nr, nc) in barriers or (nr, nc) in visited:
                continue
            new_path = path + [(nr, nc)]
            if (nr, nc) == goal:
                return new_path
            visited.add((nr, nc))
            queue.append(((nr, nc), new_path))
    return None


def _locked_doors(door_map, held_keys):
    """Positions of doors whose matching key is NOT currently held."""
    return {pos for pos, code in door_map.items() if _key_code_for_door(code) not in held_keys}


def _weighted_bfs(start, goal, rows, cols, barriers, dangers, door_map, held_keys):
    if start == goal:
        return [start], 0
    locked = _locked_doors(door_map, held_keys)
    pq = [(0, start[0], start[1], [start])]
    best_cost = {start: 0}
    while pq:
        cost, r, c, path = heapq.heappop(pq)
        if (r, c) == goal:
            return path, cost
        if cost > best_cost.get((r, c), float('inf')):
            continue
        for dr, dc, _ in DIRECTIONS:
            nr, nc = r + dr, c + dc
            if not (0 <= nr < rows and 0 <= nc < cols) or (nr, nc) in barriers:
                continue
            move_cost = DOOR_COST_LOCKED if (nr, nc) in locked else (DANGER_COST if (nr, nc) in dangers else 1)
            new_cost = cost + move_cost
            if new_cost < best_cost.get((nr, nc), float('inf')):
                best_cost[(nr, nc)] = new_cost
                heapq.heappush(pq, (new_cost, nr, nc, path + [(nr, nc)]))
    return None, float('inf')


def _hp_cost_of_path(path, dangers, door_map, held_keys):
    locked = _locked_doors(door_map, held_keys)
    cost = 0
    for p in path[1:]:
        if p in dangers:
            cost += 1
        elif p in locked:
            cost += LOCKED_DOOR_HP_DAMAGE
    return cost


def _find_path(start, goal, rows, cols, barriers, dangers, door_map, held_keys, treasure=None):
    """
    Two-phase pathfinding, generalized to any number of key/door color
    pairs. Stepping onto the treasure tile ends the run immediately, so
    it's blocked as an incidental waypoint unless it IS the goal.
    """
    effective_barriers = barriers
    if treasure is not None and treasure != goal:
        effective_barriers = barriers | {treasure}

    locked = _locked_doors(door_map, held_keys)
    hazards = set(dangers) | locked
    safe_path = _bfs_simple(start, goal, rows, cols, effective_barriers | hazards)
    if safe_path is not None:
        return safe_path, 0
    path, _ = _weighted_bfs(start, goal, rows, cols, effective_barriers, dangers, door_map, held_keys)
    if path is None:
        return None, None
    return path, _hp_cost_of_path(path, dangers, door_map, held_keys)


def _is_reachable(start, goal, rows, cols, barriers, treasure=None):
    effective_barriers = barriers
    if treasure is not None and treasure != goal:
        effective_barriers = barriers | {treasure}
    return _bfs_simple(start, goal, rows, cols, effective_barriers) is not None


def _held_karp_profit(dist_matrix, target_indices, treasure_idx, scores, step_cost):
    n = len(target_indices)
    if n == 0:
        return []
    NEG_INF = float('-inf')
    dp_val = [[NEG_INF] * n for _ in range(1 << n)]
    parent = [[-1] * n for _ in range(1 << n)]
    for i in range(n):
        d = dist_matrix[0][target_indices[i]]
        if d == float('inf'):
            continue
        dp_val[1 << i][i] = scores[target_indices[i]] - step_cost * d
    for mask in range(1, 1 << n):
        for last in range(n):
            if dp_val[mask][last] == NEG_INF:
                continue
            for nxt in range(n):
                if mask & (1 << nxt):
                    continue
                d = dist_matrix[target_indices[last]][target_indices[nxt]]
                if d == float('inf'):
                    continue
                new_mask = mask | (1 << nxt)
                new_val = dp_val[mask][last] + scores[target_indices[nxt]] - step_cost * d
                if new_val > dp_val[new_mask][nxt]:
                    dp_val[new_mask][nxt] = new_val
                    parent[new_mask][nxt] = last
    best_val, best_mask, best_last = float('-inf'), 0, -1
    for mask in range(1, 1 << n):
        for last in range(n):
            if dp_val[mask][last] == NEG_INF:
                continue
            d_home = dist_matrix[target_indices[last]][treasure_idx]
            if d_home == float('inf'):
                continue
            total = dp_val[mask][last] - step_cost * d_home
            if total > best_val:
                best_val, best_mask, best_last = total, mask, last
    if best_last == -1:
        return []
    order, mask, cur = [], best_mask, best_last
    while cur != -1:
        order.append(target_indices[cur])
        prev = parent[mask][cur]
        mask ^= (1 << cur)
        cur = prev
    order.reverse()
    return order


def _nearest_neighbor(dist_matrix, target_indices, treasure_idx, scores, step_cost):
    remaining = set(target_indices)
    order = []
    current = 0
    while remaining:
        best, best_net = None, 0.0
        for t in remaining:
            d = dist_matrix[current][t]
            if d == float('inf'):
                continue
            net = scores.get(t, 1) - step_cost * d
            if best is None or net > best_net:
                best, best_net = t, net
        if best is None or best_net <= 0:
            break
        order.append(best)
        remaining.discard(best)
        current = best
    return order


def _solve_tsp_order(dist_matrix, treasure_idx, scores, step_cost):
    target_indices = list(range(1, treasure_idx))
    if not target_indices:
        return []
    if len(target_indices) <= 13:
        return _held_karp_profit(dist_matrix, target_indices, treasure_idx, scores, step_cost)
    return _nearest_neighbor(dist_matrix, target_indices, treasure_idx, scores, step_cost)


def _route_hp_feasible(route, waypoints, hp_start, rows, cols, barriers, dangers, door_map, held_keys, challenges, treasure=None):
    hp_left = hp_start
    cur = route[0]
    for nxt in route[1:]:
        path, hp_cost = _find_path(waypoints[cur], waypoints[nxt], rows, cols, barriers, dangers, door_map, held_keys, treasure=treasure)
        if path is None:
            return False
        extra = CHALLENGE_HP_COST if waypoints[nxt] in challenges else 0
        cost = hp_cost + extra
        if cost >= hp_left:
            return False
        hp_left -= cost
        cur = nxt
    return True


def _two_opt(route, dist_matrix, max_passes=6):
    n = len(route)
    if n < 4:
        return route
    for _ in range(max_passes):
        improved = False
        for i in range(1, n - 2):
            for j in range(i + 1, n - 1):
                a, b = route[i - 1], route[i]
                c, d = route[j], route[j + 1]
                d_ab, d_cd = dist_matrix[a][b], dist_matrix[c][d]
                d_ac, d_bd = dist_matrix[a][c], dist_matrix[b][d]
                if float('inf') in (d_ab, d_cd, d_ac, d_bd):
                    continue
                if d_ac + d_bd < d_ab + d_cd - 1e-9:
                    route[i:j + 1] = reversed(route[i:j + 1])
                    improved = True
        if not improved:
            break
    return route


def _or_opt(route, dist_matrix, max_passes=6):
    n = len(route)
    if n < 4:
        return route
    for _ in range(max_passes):
        improved = False
        for i in range(1, n - 1):
            node = route[i]
            prev_n, next_n = route[i - 1], route[i + 1]
            d_prev_node = dist_matrix[prev_n][node]
            d_node_next = dist_matrix[node][next_n]
            d_prev_next = dist_matrix[prev_n][next_n]
            if float('inf') in (d_prev_node, d_node_next, d_prev_next):
                continue
            removed_cost = d_prev_node + d_node_next - d_prev_next
            if removed_cost <= 1e-9:
                continue
            trial = route[:i] + route[i + 1:]
            pos, extra = _cheapest_insertion(trial, dist_matrix, node)
            if pos is not None and extra < removed_cost - 1e-9:
                trial.insert(pos, node)
                route = trial
                improved = True
                break
        if not improved:
            break
    return route


def _cheapest_insertion(route_indices, dist_matrix, candidate_idx):
    best_pos, best_extra = None, float('inf')
    for i in range(len(route_indices) - 1):
        a, b = route_indices[i], route_indices[i + 1]
        d_ab = dist_matrix[a][b]
        d_ac = dist_matrix[a][candidate_idx]
        d_cb = dist_matrix[candidate_idx][b]
        if d_ac == float('inf') or d_cb == float('inf'):
            continue
        extra = d_ac + d_cb - (d_ab if d_ab != float('inf') else 0)
        if extra < best_extra:
            best_extra, best_pos = extra, i + 1
    return best_pos, best_extra


def _compute_pairwise_distances(waypoints, rows, cols, barriers, dangers, door_map, held_keys, treasure=None):
    n = len(waypoints)
    dist = [[float('inf')] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            if i == j:
                dist[i][j] = 0
                continue
            path, _ = _find_path(waypoints[i], waypoints[j], rows, cols, barriers, dangers, door_map, held_keys, treasure=treasure)
            dist[i][j] = len(path) - 1 if path else float('inf')
    return dist


def _path_to_directions(path):
    directions = []
    for (r1, c1), (r2, c2) in zip(path, path[1:]):
        dr, dc = r2 - r1, c2 - c1
        if dr == 1:
            directions.append("down")
        elif dr == -1:
            directions.append("up")
        elif dc == 1:
            directions.append("right")
        elif dc == -1:
            directions.append("left")
    return directions


def _plan_collect(current_pos, coins, challenges, treasure, rows, cols, barriers, dangers,
                   door_map, held_keys, simulated_hp, step_cost, grid, door_targets=frozenset()):
    collectibles = coins | challenges | door_targets
    safe_targets, forced_targets = [], []
    for pos in collectibles:
        if not _is_reachable(current_pos, pos, rows, cols, barriers, treasure=treasure):
            continue
        if not _is_reachable(pos, treasure, rows, cols, barriers, treasure=treasure):
            continue
        path, hp_cost = _find_path(current_pos, pos, rows, cols, barriers, dangers, door_map, held_keys, treasure=treasure)
        if path is None:
            continue
        extra = CHALLENGE_HP_COST if pos in challenges else 0
        if hp_cost + extra >= simulated_hp:
            continue
        if grid.get(pos, "") in FORCE_COLLECT_TYPES:
            forced_targets.append(pos)
        else:
            safe_targets.append(pos)

    all_targets = forced_targets + safe_targets
    waypoints = [current_pos] + all_targets + [treasure]
    treasure_idx = len(waypoints) - 1
    # A LOCKED door (matching key not in held_keys) must score 0 here, or
    # the optimizer chases a +1000/+50 reward that doesn't actually exist
    # for an unopened door - see _tile_score's docstring for why that
    # produces a route that walks toward a door and then backtracks.
    scores = {i: _tile_score(grid.get(pos, ""), pos=pos, door_map=door_map, held_keys=held_keys)
              for i, pos in enumerate(waypoints)}

    if not all_targets:
        path, _ = _find_path(current_pos, treasure, rows, cols, barriers, dangers, door_map, held_keys, treasure=treasure)
        if path is None:
            return None, None
        return path, -step_cost * (len(path) - 1)

    dist_matrix = _compute_pairwise_distances(waypoints, rows, cols, barriers, dangers, door_map, held_keys, treasure=treasure)

    forced_idx = list(range(1, 1 + len(forced_targets)))
    optional_idx = list(range(1 + len(forced_targets), treasure_idx))

    if forced_idx:
        remaining = set(forced_idx)
        route = [0]
        cur = 0
        while remaining:
            nxt = min(remaining, key=lambda t: dist_matrix[cur][t] if dist_matrix[cur][t] != float('inf') else 1e9)
            route.append(nxt)
            remaining.discard(nxt)
            cur = nxt
        route.append(treasure_idx)

        while len(route) > 2 and not _route_hp_feasible(route, waypoints, simulated_hp, rows, cols, barriers, dangers, door_map, held_keys, challenges, treasure=treasure):
            worst_pos, worst_extra = None, -1
            for i in range(1, len(route) - 1):
                trial = route[:i] + route[i + 1:]
                _, extra = _cheapest_insertion(trial, dist_matrix, route[i])
                if extra is not None and extra > worst_extra:
                    worst_extra, worst_pos = extra, i
            if worst_pos is None:
                route.pop(len(route) - 2)
            else:
                route.pop(worst_pos)
    else:
        route = [0, treasure_idx]

    optional_sorted = sorted(optional_idx, key=lambda i: -scores[i])
    for cand in optional_sorted:
        pos, extra = _cheapest_insertion(route, dist_matrix, cand)
        if pos is None:
            continue
        net_gain = scores[cand] - step_cost * extra
        if net_gain <= 0:
            continue
        trial_route = route[:pos] + [cand] + route[pos:]
        if _route_hp_feasible(trial_route, waypoints, simulated_hp, rows, cols, barriers, dangers, door_map, held_keys, challenges, treasure=treasure):
            route = trial_route

    cleaned_route = _two_opt(list(route), dist_matrix)
    if cleaned_route != route and _route_hp_feasible(cleaned_route, waypoints, simulated_hp, rows, cols, barriers, dangers, door_map, held_keys, challenges, treasure=treasure):
        route = cleaned_route

    cleaned_route2 = _or_opt(list(route), dist_matrix)
    if cleaned_route2 != route and _route_hp_feasible(cleaned_route2, waypoints, simulated_hp, rows, cols, barriers, dangers, door_map, held_keys, challenges, treasure=treasure):
        route = cleaned_route2

    seg_path = []
    hp_left = simulated_hp
    total_score = 0
    current_idx = 0
    for next_idx in route[1:]:
        pos = waypoints[next_idx]
        path, hp_cost = _find_path(waypoints[current_idx], pos, rows, cols, barriers, dangers, door_map, held_keys, treasure=treasure)
        if path is None:
            continue
        extra = CHALLENGE_HP_COST if pos in challenges else 0
        cost = hp_cost + extra
        if next_idx != treasure_idx and cost >= hp_left:
            continue
        seg_path.extend(path[1:] if seg_path else path)
        hp_left -= cost
        if next_idx != treasure_idx:
            total_score += scores[next_idx]
        current_idx = next_idx

    if current_idx != treasure_idx:
        path, _ = _find_path(waypoints[current_idx], treasure, rows, cols, barriers, dangers, door_map, held_keys, treasure=treasure)
        if path is None:
            return None, None
        seg_path.extend(path[1:] if seg_path else path)

    net = total_score - step_cost * (len(seg_path) - 1)
    return seg_path, net


def _plan_recursive(pos, held_keys, hp, coins, challenges, treasure, rows, cols, barriers,
                     dangers, door_map, key_map, step_cost, grid, prefix_path=(), depth=0, max_depth=4):
    """
    Generalized branch search over key pickups, supporting an arbitrary
    number of key/door color pairs (not hardcoded to one). At each state
    (position + set of keys currently held), consider:
      (a) heading straight for the treasure/collectibles with the keys
          held so far, or
      (b) picking up any not-yet-held key next, then recursing.
    Returns the best (full_path, net_value) found across all branches.
    depth is capped (max_depth) as a safety net — in practice it's bounded
    by the number of distinct key colors on the map, which is small.
    """
    candidates = []

    door_targets = set(door_map.keys())
    seg, net = _plan_collect(pos, coins, challenges, treasure, rows, cols, barriers, dangers,
                             door_map, held_keys, hp, step_cost, grid, door_targets=door_targets)
    if seg is not None:
        candidates.append((net, list(prefix_path) + seg))

    if depth < max_depth:
        for key_code, key_positions in key_map.items():
            if key_code in held_keys:
                continue
            for key_pos in key_positions:
                if not _is_reachable(pos, key_pos, rows, cols, barriers, treasure=treasure):
                    continue
                key_path, key_hp_cost = _find_path(pos, key_pos, rows, cols, barriers, dangers, door_map, held_keys, treasure=treasure)
                if key_path is None or key_hp_cost >= hp:
                    continue
                new_prefix = list(prefix_path) + (key_path if not prefix_path else key_path[1:])
                sub_seg, sub_net = _plan_recursive(
                    key_pos, held_keys | {key_code}, hp - key_hp_cost,
                    coins, challenges, treasure, rows, cols, barriers, dangers,
                    door_map, key_map, step_cost, grid,
                    prefix_path=tuple(new_prefix), depth=depth + 1, max_depth=max_depth,
                )
                if sub_seg is not None:
                    full_net = sub_net - step_cost * (len(key_path) - 1)
                    candidates.append((full_net, sub_seg))

    if not candidates:
        return None, None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1], candidates[0][0]


def plan_path(start, game_map, hp_remaining=5, step_cost=DEFAULT_STEP_COST):
    grid, rows, cols, barriers, dangers, coins, challenges, door_map, key_map, treasure = _build_grid(game_map)

    if treasure is None:
        treasure = (rows - 1, cols - 1)

    # Calibrate the start against the real grid BEFORE planning anything -
    # a start inside a wall/off the map makes every subsequent move wrong.
    start = _resolve_start(start, rows, cols, barriers, grid)

    # Group key positions by code (multiple tiles could share a code,
    # though typically one key tile per color).
    key_groups = {}
    for pos, code in key_map.items():
        key_groups.setdefault(code, set()).add(pos)

    best_path, _ = _plan_recursive(
        start, frozenset(), hp_remaining, coins, challenges, treasure, rows, cols,
        barriers, dangers, door_map, key_groups, step_cost, grid,
    )

    if not best_path:
        return []
    return _path_to_directions(best_path)


def _coerce_number(value, default, field_name=""):
    """Best-effort convert `value` to a number, falling back to `default`
    (instead of raising) if it isn't a clean number."""
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        logger.warning(f"Ignoring non-numeric value for {field_name!r}: {value!r}, using default {default}")
        return default


def lambda_handler(event, context):
    try:
        body = json.loads(event["body"]) if "body" in event and isinstance(event["body"], str) else event.get("body", event)
        game_map = body.get("map", body.get("game_map", body.get("grid", [])))
        if game_map:
            max_cols = max(len(row) for row in game_map)
            game_map = [row + ["normal"] * (max_cols - len(row)) for row in game_map]
        start_raw = body.get("start", body.get("start_pos", body.get("position", body.get("agent_position", "A1"))))
        start = _parse_start(start_raw)
        hp_raw = body.get("hp", body.get("health", body.get("life_points", 5)))
        hp = _coerce_number(hp_raw, 5, "hp")
        step_cost_raw = body.get("step_cost", body.get("time_penalty", DEFAULT_STEP_COST))
        step_cost = _coerce_number(step_cost_raw, DEFAULT_STEP_COST, "step_cost")
        directions = plan_path(start, game_map, hp_remaining=hp, step_cost=step_cost)
        if not directions:
            directions = ["down", "right"]
        # Only ever return ONE field for the move list, named "directions".
        # A duplicate "action"/"path" field reads, to an LLM consuming this
        # tool's result, as "the thing to output" - risking the agent
        # forwarding just directions[0] instead of the whole route.
        return {"statusCode": 200, "body": json.dumps({"directions": directions})}
    except Exception as e:
        return {"statusCode": 200, "body": json.dumps({"directions": ["down", "right", "down", "right"], "message": f"Fallback mode: {str(e)}"})}


if __name__ == "__main__":
    mv = {"up": (-1, 0), "down": (1, 0), "left": (0, -1), "right": (0, 1)}

    def _walk(start, directions):
        r, c = start
        visited = []
        for d in directions:
            dr, dc = mv[d]
            r, c = r + dr, c + dc
            visited.append((r, c))
        return visited

    # Single red door/key still works exactly as before
    door_map_test = [
        ["start", "normal", "c40"],
        ["normal", "wall", "c30"],
        ["normal", "wall", "c7"],
        ["normal", "wall", "normal"],
        ["normal", "normal", "treasure"],
    ]
    d2 = plan_path((0, 0), door_map_test, hp_remaining=5)
    v2 = _walk((0, 0), d2)
    assert (1, 2) in v2, "must cross the (only) door to reach the coin/treasure corridor"
    assert v2.index((0, 2)) < v2.index((1, 2)), "key must be collected before the door"
    assert v2[-1] == (4, 2), "must end at the treasure"

    # Two independent key/door pairs (Red c30/c40 + Green c31/c41) on the
    # same map — both must be solved, in whichever order is cheaper.
    two_door_map = [
        ["start", "normal", "c40", "normal", "c41"],
        ["normal", "wall",  "c30", "wall",   "c31"],
        ["normal", "wall",  "c7",  "wall",   "c7"],
        ["normal", "wall",  "normal", "wall", "normal"],
        ["normal", "normal", "normal", "normal", "treasure"],
    ]
    d3 = plan_path((0, 0), two_door_map, hp_remaining=10)
    v3 = _walk((0, 0), d3)
    assert (1, 2) in v3, "must cross the RED door"
    assert (1, 4) in v3, "must cross the GREEN door"
    assert v3.index((0, 2)) < v3.index((1, 2)), "red key before red door"
    assert v3.index((0, 4)) < v3.index((1, 4)), "green key before green door"
    assert v3[-1] == (4, 4), "must end at the treasure"

    # NEW: a locked door with NO reachable key anywhere on the map must
    # score ZERO, not the full DEFAULT_DOOR_SCORE - previously the
    # optimizer treated an unopenable door as a free +1000 reward, which
    # pulled the route toward it, gained nothing, and had to backtrack
    # (the exact zigzag reported from live play: down,down,up,up,...).
    unreachable_key_map = [
        ["start", "normal", "normal"],
        ["normal", "wall",  "c30"],
        ["normal", "wall",  "normal"],
        ["normal", "normal", "treasure"],
    ]
    d4 = plan_path((0, 0), unreachable_key_map, hp_remaining=10)
    v4 = _walk((0, 0), d4)
    assert (1, 2) not in v4, \
        f"a locked door worth 0 real points must not be chased as a detour: {v4}"
    assert v4[-1] == (3, 2), "must go straight to the treasure, not backtrack toward the useless door"

    # NEW: _tile_score itself must report 0 for a locked door and the
    # real reward for an unlocked one - this is the exact scoring bug
    # that made the optimizer chase a reward that doesn't exist.
    assert _tile_score("c30", pos=(1, 2), door_map={(1, 2): "c30"}, held_keys=frozenset()) == 0, \
        "a locked door (matching key not held) must score 0"
    assert _tile_score("c30", pos=(1, 2), door_map={(1, 2): "c30"}, held_keys=frozenset({"c40"})) == DEFAULT_DOOR_SCORE, \
        "an unlocked door (matching key held) must score its real reward"

    # NEW: a dict start position must be destructured BY KEY, not by a
    # regex over str(dict) - that only happened to work because the game
    # always sends "row" before "col"; a different key order would
    # silently swap row/col and desync the whole route.
    assert _parse_start({"row": 4, "col": 0}) == (4, 0), _parse_start({"row": 4, "col": 0})
    assert _parse_start({"col": 0, "row": 4}) == (4, 0), \
        "key order must not matter - this used to silently return (0, 4)"
    assert _parse_start({"row": 5, "col": "A"}) == (4, 0), _parse_start({"row": 5, "col": "A"})
    assert _parse_start("A5") == (4, 0), "the plain 'A5' form must keep working"

    # NEW: a start that lands inside a wall must be calibrated back onto
    # a real walkable tile, or the agent walks into a wall on its very
    # first step (the other reported symptom).
    walled_map = [
        ["wall", "wall", "wall"],
        ["wall", "start", "normal"],
        ["wall", "normal", "treasure"],
    ]
    g_w = _build_grid(walled_map)
    grid_w, rows_w, cols_w, barriers_w = g_w[0], g_w[1], g_w[2], g_w[3]
    assert _resolve_start((0, 0), rows_w, cols_w, barriers_w, grid_w) == (1, 1), \
        "a start inside a wall must fall back to the map's own start tile"

    # NEW: walls labelled with anything other than the exact string "wall"
    # must still be impassable.
    brick_map = [
        ["start", "brick", "normal"],
        ["normal", "brick", "normal"],
        ["normal", "normal", "treasure"],
    ]
    d5 = plan_path((0, 0), brick_map, hp_remaining=10)
    v5 = _walk((0, 0), d5)
    assert (0, 1) not in v5 and (1, 1) not in v5, f"route walked through a 'brick' wall: {v5}"
    assert v5[-1] == (2, 2), "must still reach the treasure around the wall"

    print("OK: all self-checks passed (including multi-door/key support, locked-door scoring, and start/wall fixes)")
