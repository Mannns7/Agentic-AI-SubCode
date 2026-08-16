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

# Keep one movement reply below the game's/model's single-message output
# ceiling. The supplied 79-step route was visibly split into two chat
# bubbles; the game consumed the first incomplete fragment and ended while
# the agent was still at A5. ponytail: this prioritizes a guaranteed safe
# route to treasure over optional score once a route exceeds 48 moves;
# upgrade to paged/stateful movement when the game supports route chunks.
MAX_ROUTE_DIRECTIONS = 48

# ============================================================================
# OPERATOR-DRAWN ROUTES  (these WIN over anything the planner would compute)
# ============================================================================
# Hand-drawn on the round-3 board and keyed by the tile the turn starts on.
# BLUE  = first start  (A5) -> ends at A8
# YELLOW = second start (A8) -> ends at the J1 treasure
#
# These are followed EXACTLY. The planner is not consulted, the route is not
# re-ordered, and spike/door damage on the way is NOT a reason to reject it -
# the damage is a deliberate trade the operator chose. The only thing checked
# is that a step does not leave the grid or enter a wall, because the game
# refuses such a move and every direction after it would be applied from the
# wrong tile.
MANUAL_ROUTE_BLUE = (
    ["right"] * 3      # A5 -> D5
    + ["down"] * 3     # D5 -> D8  (D6 spikes: -1 HP, accepted)
    + ["left"] * 3     # D8 -> A8
)

MANUAL_ROUTE_YELLOW = (
    ["down"]           # A8 -> A9
    + ["right"] * 3    # A9 -> D9
    + ["down"]         # D9 -> D10
    + ["right"] * 6    # D10 -> J10
    + ["up"] * 2       # J10 -> J8
    + ["left"] * 4     # J8 -> F8
    + ["up"]           # F8 -> F7
    + ["right"] * 4    # F7 -> J7
    + ["up"] * 2       # J7 -> J5  (crosses the J6 grey door)
    + ["left"] * 6     # J5 -> D5  (E5 spikes: -1 HP, accepted)
    + ["up"] * 3       # D5 -> D2
    + ["left"] * 3     # D2 -> A2
    + ["up"]           # A2 -> A1  (grey key)
    + ["right"] * 5    # A1 -> F1
    + ["down"] * 2     # F1 -> F3  (yellow key)
    + ["up"] * 2       # F3 -> F1
    + ["right"] * 4    # F1 -> J1  (G1 coin, then the treasure)
)

MANUAL_ROUTES = {
    (4, 0): MANUAL_ROUTE_BLUE,    # A5, first start
    (7, 0): MANUAL_ROUTE_YELLOW,  # A8, second start
}

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


def _route_moves(route, dist_matrix):
    """Total moves the route costs, or inf if any leg is unreachable."""
    total = 0
    for a, b in zip(route, route[1:]):
        d = dist_matrix[a][b]
        if d == float('inf'):
            return float('inf')
        total += d
    return total


def _trim_to_move_budget(route, dist_matrix, scores, first_optional_idx, budget):
    """
    Drop the least valuable stops until the route fits in `budget` moves.

    Without this, a route over the atomic output limit was discarded
    WHOLESALE for a straight run to the treasure, so on a dense map the
    agent collected NOTHING. Trimming keeps the best-paying stops that fit.
    Optional stops go first, worst value-per-move saved first; forced quest
    tiles are only sacrificed once nothing optional is left.
    """
    while len(route) > 2 and _route_moves(route, dist_matrix) > budget:
        optional = [i for i in range(1, len(route) - 1) if route[i] >= first_optional_idx]
        removable = optional or list(range(1, len(route) - 1))
        worst_pos, worst_ratio = None, None
        for i in removable:
            trial = route[:i] + route[i + 1:]
            saved = _route_moves(route, dist_matrix) - _route_moves(trial, dist_matrix)
            if saved <= 0:
                worst_pos, worst_ratio = i, -1.0  # pure dead weight
                break
            ratio = scores.get(route[i], 0) / saved
            if worst_ratio is None or ratio < worst_ratio:
                worst_pos, worst_ratio = i, ratio
        if worst_pos is None:
            break
        route = route[:worst_pos] + route[worst_pos + 1:]
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

    # True greedy insertion: re-price EVERY remaining candidate each round
    # and take the single best ACTUAL profit (score minus the real detour).
    # Sorting by raw score once committed to a far-away high scorer before a
    # near-identical one right next to the path, and every later insertion
    # had to detour around that bad commitment - the compounding zigzag.
    remaining = set(optional_idx)
    while remaining:
        best = None
        for cand in remaining:
            pos, extra = _cheapest_insertion(route, dist_matrix, cand)
            if pos is None or extra == float('inf'):
                continue
            net_gain = scores[cand] - step_cost * extra
            if net_gain <= 0:
                continue
            if best is None or (net_gain, -extra) > (best[0], -best[1]):
                best = (net_gain, extra, pos, cand)
        if best is None:
            break
        _, _, pos, cand = best
        remaining.discard(cand)
        trial_route = route[:pos] + [cand] + route[pos:]
        if _route_hp_feasible(trial_route, waypoints, simulated_hp, rows, cols, barriers, dangers, door_map, held_keys, challenges, treasure=treasure):
            route = trial_route

    cleaned_route = _two_opt(list(route), dist_matrix)
    if cleaned_route != route and _route_hp_feasible(cleaned_route, waypoints, simulated_hp, rows, cols, barriers, dangers, door_map, held_keys, challenges, treasure=treasure):
        route = cleaned_route

    cleaned_route2 = _or_opt(list(route), dist_matrix)
    if cleaned_route2 != route and _route_hp_feasible(cleaned_route2, waypoints, simulated_hp, rows, cols, barriers, dangers, door_map, held_keys, challenges, treasure=treasure):
        route = cleaned_route2

    # Keep the whole turn inside ONE atomic reply. Trimming the cheapest
    # stops beats handing back a route the game splits and rejects - and
    # beats throwing the entire route away for an empty run to the treasure.
    route = _trim_to_move_budget(route, dist_matrix, scores,
                                 1 + len(forced_targets), MAX_ROUTE_DIRECTIONS)

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


_MOVE_DELTAS = {name: (dr, dc) for dr, dc, name in DIRECTIONS}
_DELTA_MOVES = {(dr, dc): name for dr, dc, name in DIRECTIONS}


def _simulate_route(start, directions, rows, cols, barriers, dangers, door_map,
                    key_map, treasure):
    """
    Replay a finished direction list exactly the way the GAME will and
    report (fatal_reason, hp_damage).

    Every "walked into a wall"/"died on turn 1" report came from trusting
    the planner's own segment bookkeeping. This replays the FINAL answer
    instead, so a bug anywhere upstream (bad start calibration, a segment
    stitched at the wrong tile, an off-by-one) is caught here instead of by
    the agent losing the run.

    Only GUARANTEED terrain damage is counted: spikes and locked doors.
    Challenge tiles (c1/c2/c4/c5/c17/c18) are tiles the agent is SUPPOSED
    to land on - they only cost HP on a wrong answer, so treating them as
    lethal here would forbid the whole scoring strategy and, worse, leave
    the agent standing still. Keys picked up en route count immediately, so
    a door opened with a key collected earlier in the same route is free.
    """
    r, c = start
    if not (0 <= r < rows and 0 <= c < cols) or (r, c) in barriers:
        return f"start {(r, c)} is not a walkable tile", 0

    held = set()
    damage = 0
    for i, d in enumerate(directions):
        move = _MOVE_DELTAS.get(d)
        if move is None:
            return f"move {i} is not a direction: {d!r}", damage
        r, c = r + move[0], c + move[1]
        if not (0 <= r < rows and 0 <= c < cols):
            return f"move {i} ({d}) leaves the map at {(r, c)}", damage
        if (r, c) in barriers:
            return f"move {i} ({d}) walks into a wall at {(r, c)}", damage
        # Stepping onto the treasure ends the run, so it may only ever be
        # the LAST tile - otherwise the rest of the route never happens.
        if (r, c) == treasure and i != len(directions) - 1:
            return f"move {i} ({d}) ends the run early on the treasure", damage
        if (r, c) in key_map:
            held.add(key_map[(r, c)])
        if (r, c) in dangers:
            damage += 1
        elif (r, c) in door_map and _key_code_for_door(door_map[(r, c)]) not in held:
            damage += LOCKED_DOOR_HP_DAMAGE
    if (r, c) != treasure:
        return f"route ends at {(r, c)} instead of the treasure {treasure}", damage
    return None, damage


def _continuous_directions(path):
    """
    Convert a planned path to directions, refusing a path that teleports.

    The old converter silently SKIPPED a non-adjacent step, which turned a
    mis-stitched segment into a shorter route that looked perfectly valid.
    Repeated identical positions are harmless (segments are concatenated
    end-to-start) and are collapsed; a real jump returns None.
    """
    directions = []
    for (r1, c1), (r2, c2) in zip(path, path[1:]):
        dr, dc = r2 - r1, c2 - c1
        if (dr, dc) == (0, 0):
            continue
        if abs(dr) + abs(dc) != 1:
            logger.error("path jumps from %s to %s; discarding it", (r1, c1), (r2, c2))
            return None
        directions.append(_DELTA_MOVES[(dr, dc)])
    return directions


def _route_is_survivable(start, directions, hp_remaining, rows, cols, barriers,
                         dangers, door_map, key_map, treasure, label):
    """The one gate every returned route must pass: no wall, no death."""
    if not directions:
        return False
    if len(directions) > MAX_ROUTE_DIRECTIONS:
        logger.error("rejected %s route: %d moves exceeds the %d-move atomic limit",
                     label, len(directions), MAX_ROUTE_DIRECTIONS)
        return False
    fatal, damage = _simulate_route(start, directions, rows, cols, barriers,
                                     dangers, door_map, key_map, treasure)
    if fatal is not None:
        logger.error("rejected %s route: %s", label, fatal)
        return False
    if damage >= hp_remaining:
        logger.error("rejected %s route: it costs %d HP but only %s remains",
                     label, damage, hp_remaining)
        return False
    return True


def _manual_route(start, rows, cols, barriers, dangers, door_map, key_map, hp_remaining):
    """
    Return the operator-drawn route for this start tile, or None.

    Walls and grid bounds are the ONLY veto: the game refuses such a move and
    every later direction would then be applied from the wrong tile. HP cost
    is reported, never used to reject - taking damage on this line is the
    operator's decision, not a bug to be routed around.
    """
    directions = MANUAL_ROUTES.get(tuple(start))
    if not directions:
        return None

    r, c = start
    held, damage = set(), 0
    for i, d in enumerate(directions):
        dr, dc = _MOVE_DELTAS[d]
        r, c = r + dr, c + dc
        if not (0 <= r < rows and 0 <= c < cols):
            logger.error("drawn route step %d (%s) leaves the map at %s; ignoring the "
                         "drawn route for this start", i, d, (r, c))
            return None
        if (r, c) in barriers:
            logger.error("drawn route step %d (%s) hits a wall at %s; ignoring the "
                         "drawn route for this start", i, d, (r, c))
            return None
        if (r, c) in key_map:
            held.add(key_map[(r, c)])
        if (r, c) in dangers:
            damage += 1
        elif (r, c) in door_map and _key_code_for_door(door_map[(r, c)]) not in held:
            damage += LOCKED_DOOR_HP_DAMAGE
    logger.warning("following the drawn route from %s: %d moves, %d HP of damage "
                   "(HP now %s)", start, len(directions), damage, hp_remaining)
    if damage >= hp_remaining:
        logger.error("WARNING: the drawn route from %s costs %d HP but only %s "
                     "remains - following it anyway as instructed",
                     start, damage, hp_remaining)
    return list(directions)


def plan_path(start, game_map, hp_remaining=5, step_cost=DEFAULT_STEP_COST):
    grid, rows, cols, barriers, dangers, coins, challenges, door_map, key_map, treasure = _build_grid(game_map)

    if treasure is None:
        treasure = (rows - 1, cols - 1)

    # Calibrate the start against the real grid BEFORE planning anything -
    # a start inside a wall/off the map makes every subsequent move wrong.
    start = _resolve_start(start, rows, cols, barriers, grid)

    # An operator-drawn route WINS. No planning, no reordering, no "safer"
    # substitute - follow the line exactly as drawn.
    drawn = _manual_route(start, rows, cols, barriers, dangers, door_map, key_map, hp_remaining)
    if drawn is not None:
        return drawn

    # Group key positions by code (multiple tiles could share a code,
    # though typically one key tile per color).
    key_groups = {}
    for pos, code in key_map.items():
        key_groups.setdefault(code, set()).add(pos)

    best_path, _ = _plan_recursive(
        start, frozenset(), hp_remaining, coins, challenges, treasure, rows, cols,
        barriers, dangers, door_map, key_groups, step_cost, grid,
    )

    # Try every route we can build, best-scoring first, and return the
    # first one that SURVIVES a full replay. Nothing leaves this function
    # unvalidated, so a planning bug can no longer reach the game as a
    # wall collision or a death - it just falls through to a safer route.
    candidates = [("optimized", best_path)]

    # Hazard-aware shortest path: may accept a spike/locked door if that is
    # genuinely the only way through, but never a wall.
    direct_path, _ = _find_path(
        start, treasure, rows, cols, barriers, dangers, door_map,
        frozenset(), treasure=treasure,
    )
    candidates.append(("shortest", direct_path))

    # Last resort: refuse every spike and every locked door outright, so
    # this route takes ZERO guaranteed damage by construction. It is the
    # safest thing that still reaches the treasure.
    candidates.append(("no-damage", _bfs_simple(
        start, treasure, rows, cols,
        barriers | set(dangers) | _locked_doors(door_map, frozenset()),
    )))

    for label, path in candidates:
        if not path:
            continue
        directions = _continuous_directions(path)
        if directions is None:
            continue
        if _route_is_survivable(start, directions, hp_remaining, rows, cols,
                                barriers, dangers, door_map, key_map,
                                treasure, label):
            if label != "optimized":
                logger.warning("using the %s route (%d moves) instead of the optimized one",
                               label, len(directions))
            return directions

    logger.error("no survivable atomic route from %s to %s; issuing NO movement",
                 start, treasure)
    return []


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


def _detect_schema_type(event):
    """Detect the invocation envelope used by Bedrock or local tests."""
    if not isinstance(event, dict):
        return "plain"
    if "function" in event and "parameters" in event:
        return "function"
    if "apiPath" in event and "httpMethod" in event:
        return "openapi"
    return "plain"


def _coerce_param_value(value, ptype=None):
    """Decode Bedrock's JSON-encoded array/object parameter values."""
    if not isinstance(value, str):
        return value
    s = value.strip()
    if s.startswith("[") or s.startswith("{"):
        try:
            return json.loads(s)
        except (TypeError, ValueError):
            return value
    if ptype in ("integer", "number"):
        try:
            return int(s) if ptype == "integer" else float(s)
        except (TypeError, ValueError):
            return value
    return value


def _extract_request(event):
    """Return (body, schema_type) for Bedrock function/OpenAPI/plain calls."""
    schema_type = _detect_schema_type(event)
    if schema_type == "function":
        body = {}
        for param in event.get("parameters") or []:
            name = param.get("name")
            if name:
                body[name] = _coerce_param_value(param.get("value"), param.get("type"))
        return body, schema_type
    if schema_type == "openapi":
        properties = (((event.get("requestBody") or {}).get("content") or {})
                      .get("application/json", {}).get("properties", []))
        if isinstance(properties, dict):
            return dict(properties), schema_type
        body = {}
        for param in properties:
            name = param.get("name")
            if name:
                body[name] = _coerce_param_value(param.get("value"), param.get("type"))
        return body, schema_type
    if isinstance(event, dict) and isinstance(event.get("body"), str):
        try:
            return json.loads(event["body"]), schema_type
        except (TypeError, ValueError):
            return {}, schema_type
    if isinstance(event, dict):
        body = event.get("body", event)
        return (body if isinstance(body, dict) else {}), schema_type
    return {}, schema_type


def _wrap_response(event, schema_type, result, status_code=200):
    """Reply in the same envelope Bedrock used to invoke this Lambda."""
    body_json = json.dumps(result)
    if schema_type == "function":
        return {
            "messageVersion": "1.0",
            "response": {
                "actionGroup": event.get("actionGroup", "PathfindingLambdaTarget"),
                "function": event.get("function", "plan_path"),
                "functionResponse": {"responseBody": {"TEXT": {"body": body_json}}},
            },
            "sessionAttributes": event.get("sessionAttributes", {}),
            "promptSessionAttributes": event.get("promptSessionAttributes", {}),
        }
    if schema_type == "openapi":
        return {
            "messageVersion": "1.0",
            "response": {
                "actionGroup": event.get("actionGroup", "PathfindingLambdaTarget"),
                "apiPath": event.get("apiPath", "/plan_path"),
                "httpMethod": event.get("httpMethod", "POST"),
                "httpStatusCode": status_code,
                "responseBody": {"application/json": {"body": body_json}},
            },
            "sessionAttributes": event.get("sessionAttributes", {}),
            "promptSessionAttributes": event.get("promptSessionAttributes", {}),
        }
    return {"statusCode": status_code, "body": body_json}


def lambda_handler(event, context):
    """
    Bedrock-compatible plan_path entrypoint.

    Never invent fallback moves. The old fallback was ["down", "right"];
    on the supplied map the agent starts at A5 and A6 is a wall, so any
    input/transport error became an immediate guaranteed collision.
    Returning an explicit error lets the supervisor retry instead of
    turning a recoverable tool error into a lost game.
    """
    try:
        if not isinstance(event, dict):
            event = {}
        body, schema_type = _extract_request(event)
        game_map = body.get("map", body.get("game_map", body.get("grid", [])))
        if not isinstance(game_map, list) or not game_map:
            return _wrap_response(event, schema_type,
                                  {"directions": [], "error": "Missing non-empty map/game_map/grid."}, 400)
        if not all(isinstance(row, list) for row in game_map):
            return _wrap_response(event, schema_type,
                                  {"directions": [], "error": "Map rows must be arrays."}, 400)
        max_cols = max(len(row) for row in game_map)
        game_map = [row + ["normal"] * (max_cols - len(row)) for row in game_map]
        start_raw = body.get("start", body.get("start_pos", body.get("position",
                    body.get("current_position", body.get("agent_position", "A1")))))
        start = _parse_start(start_raw)
        hp_raw = body.get("hp", body.get("current_hp", body.get("health", body.get("life_points", 5))))
        hp = _coerce_number(hp_raw, 5, "hp")
        step_cost_raw = body.get("step_cost", body.get("time_penalty", DEFAULT_STEP_COST))
        step_cost = _coerce_number(step_cost_raw, DEFAULT_STEP_COST, "step_cost")
        directions = plan_path(start, game_map, hp_remaining=hp, step_cost=step_cost)
        if not directions:
            return _wrap_response(event, schema_type,
                                  {"directions": [], "error": "No safe route found; no movement was issued."}, 422)
        return _wrap_response(event, schema_type, {"directions": directions})
    except Exception as exc:
        logger.exception("plan_path failed")
        schema_type = _detect_schema_type(event)
        return _wrap_response(event, schema_type,
                              {"directions": [], "error": f"plan_path failed: {exc}"}, 500)


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

    # The next block tests the PLANNER, so the drawn routes are parked for it
    # (A5 now has a drawn route, which correctly wins over any planning).
    _saved_manual = dict(MANUAL_ROUTES)
    MANUAL_ROUTES.clear()

    # Exact regression map reconstructed cell-for-cell from the supplied
    # 10x10 screenshot (A-J, rows 1-10). This is the real layout that
    # starts at A5: A6-C6 are walls, so the old invented fallback
    # ["down", "right"] lost immediately by walking from A5 into A6.
    supplied_map = [
        ["c42", "c5", "normal", "normal", "c1", "normal", "c7", "normal", "normal", "treasure"],
        ["c17", "normal", "normal", "c4", "wall", "normal", "normal", "normal", "normal", "normal"],
        ["normal", "normal", "normal", "normal", "wall", "c43", "normal", "normal", "normal", "normal"],
        ["wall", "wall", "wall", "c2", "wall", "wall", "c8", "wall", "wall", "c33"],
        ["start", "normal", "normal", "normal", "c8", "normal", "normal", "normal", "normal", "normal"],
        ["wall", "wall", "wall", "c8", "wall", "wall", "wall", "wall", "wall", "c32"],
        ["c8", "normal", "normal", "normal", "wall", "c7", "c7", "c7", "c7", "c1"],
        ["c4", "normal", "normal", "c17", "wall", "c5", "c7", "c7", "c7", "c7"],
        ["normal", "normal", "normal", "normal", "wall", "wall", "wall", "wall", "wall", "normal"],
        ["c8", "normal", "normal", "c5", "c2", "c7", "c7", "c7", "c7", "c7"],
    ]
    supplied_event = {
        "messageVersion": "1.0",
        "actionGroup": "PathfindingLambdaTarget",
        "function": "plan_path",
        "parameters": [
            {"name": "map", "type": "array", "value": json.dumps(supplied_map)},
            {"name": "start_pos", "type": "object", "value": json.dumps({"row": 4, "col": 0})},
            {"name": "hp", "type": "integer", "value": "5"},
            {"name": "step_cost", "type": "number", "value": "3"},
        ],
        "sessionAttributes": {},
        "promptSessionAttributes": {},
    }
    supplied_response = lambda_handler(supplied_event, None)
    supplied_body = json.loads(
        supplied_response["response"]["functionResponse"]["responseBody"]["TEXT"]["body"]
    )
    supplied_directions = supplied_body["directions"]
    assert supplied_directions and "error" not in supplied_body, supplied_body
    supplied_visited = _walk((4, 0), supplied_directions)
    supplied_grid = _build_grid(supplied_map)
    supplied_barriers, supplied_coins = supplied_grid[3], supplied_grid[5]
    assert all(0 <= r < 10 and 0 <= c < 10 and (r, c) not in supplied_barriers
               for r, c in supplied_visited), \
        f"supplied-map route crossed a wall or left the grid: {supplied_visited}"
    assert supplied_visited[-1] == (0, 9), \
        f"supplied-map route must finish at J1 treasure, got {supplied_visited[-1]}"
    expected_supplied_directions = ["right"] * 3 + ["up"] * 4 + ["right"] * 6
    assert supplied_directions == expected_supplied_directions, \
        f"supplied-map route must follow A5 -> D5 -> D1 -> J1 exactly: {supplied_directions}"
    assert len(supplied_directions) <= MAX_ROUTE_DIRECTIONS, \
        "supplied-map route must fit in one atomic model/game response"
    assert (0, 6) in supplied_visited, \
        "the direct supplied-map route should collect the G1 coin on its way to J1"

    # The same atomic ceiling must apply to the shortest fallback itself.
    # This valid corridor has one 53-step route; returning it would recreate
    # the split-response failure, so the only safe atomic answer is no move.
    serpentine_map = [
        ["normal"] * 10,
        ["wall"] * 9 + ["normal"],
        ["normal"] * 10,
        ["normal"] + ["wall"] * 9,
        ["normal"] * 10,
        ["wall"] * 9 + ["normal"],
        ["normal"] * 10,
        ["normal"] + ["wall"] * 9,
        ["normal"] * 10,
    ]
    serpentine_map[0][0], serpentine_map[8][9] = "start", "treasure"
    assert plan_path((0, 0), serpentine_map, hp_remaining=10) == [], \
        "a 53-step direct fallback must not exceed the 48-direction atomic limit"

    # The final gate must catch a bad route even if the planner produced it.
    # These are the two ways the agent actually lost: a wall collision and
    # running out of HP mid-route.
    gate_map = [
        ["start", "normal", "c40"],
        ["c8", "c8", "c30"],
        ["wall", "normal", "treasure"],
    ]
    g_gate = _build_grid(gate_map)
    gate_rows, gate_cols, gate_barriers = g_gate[1], g_gate[2], g_gate[3]
    gate_dangers = g_gate[4]
    gate_doors, gate_keys, gate_treasure = g_gate[7], g_gate[8], g_gate[9]

    def _gate(directions, hp):
        return _route_is_survivable((0, 0), directions, hp, gate_rows, gate_cols,
                                    gate_barriers, gate_dangers, gate_doors,
                                    gate_keys, gate_treasure, "test")

    assert _gate(["down", "down"], 5) is False, \
        "a move into a wall must be rejected, not forwarded to the game"
    assert _gate(["right", "right", "down", "down"], 5) is True, \
        "a survivable route that reaches the treasure must be accepted"
    assert _gate(["down", "right", "down", "right"], 2) is False, \
        "a route costing 2 HP with 2 HP left is fatal and must be rejected"
    assert _gate(["down", "right", "down", "right"], 3) is True, \
        "the same 2-spike route IS allowed when the agent can survive it"
    assert _gate(["up"], 5) is False, "a move off the map must be rejected"
    assert _gate(["sideways"], 5) is False, "an unknown direction must be rejected"
    assert _gate([], 5) is False, "an empty route is never a usable answer"
    assert _gate(["down", "right"], 5) is False, \
        "a route that stops short of the treasure must be rejected"

    # The key must be credited DURING the replay: the same door costs 5 HP
    # without its key and nothing once the key has been walked over.
    assert _gate(["down", "right", "right", "down"], 5) is False, \
        "crossing the c30 door without the c40 key costs 5 HP and must be fatal"
    assert _gate(["right", "right", "down", "down"], 1) is True, \
        "collecting c40 first makes the c30 door free, so 1 HP is enough"

    # A mis-stitched path that teleports must be discarded, not silently
    # shortened into a route that ends in the wrong place.
    assert _continuous_directions([(0, 0), (1, 0), (1, 2), (2, 2)]) is None, \
        "a non-adjacent step must invalidate the whole path"
    assert _continuous_directions([(0, 0), (1, 0), (1, 0), (1, 1)]) == ["down", "right"], \
        "a repeated position is just a segment join and must be collapsed"

    # End-to-end: with only 3 HP the planner must route AROUND the spike row
    # instead of eating 3 spikes to save two moves.
    spike_gauntlet = [
        ["start", "c8", "c8", "c8", "treasure"],
        ["normal", "normal", "normal", "normal", "normal"],
    ]
    d6 = plan_path((0, 0), spike_gauntlet, hp_remaining=3)
    v6 = _walk((0, 0), d6)
    g_spike = _build_grid(spike_gauntlet)
    assert d6, "a safe detour exists, so the planner must still move"
    assert not (set(v6) & g_spike[4]), f"route stepped on a spike with only 3 HP: {v6}"
    assert v6[-1] == (0, 4), f"route must still finish at the treasure: {v6}"

    # Challenge tiles are tiles the agent is MEANT to land on - they must
    # never be treated as lethal terrain, or the safety gate would reject
    # every route and leave the agent standing still (a guaranteed loss).
    challenge_corridor = [["start", "c1", "c1", "c1", "c1", "c1", "treasure"]]
    d7 = plan_path((0, 0), challenge_corridor, hp_remaining=5)
    assert d7 == ["right"] * 6, \
        f"a plain challenge corridor must still be walked, got {d7}"

    # DENSE MAP (>15 collectibles) - the case none of the earlier checks
    # covered. A board packed with value used to produce a 100+ move route
    # that blew the atomic limit, so the whole thing was thrown away for a
    # bare run to the treasure and the agent collected almost nothing.
    # It must now come back UNDER the limit and still collect real score.
    dense_map = [["c7"] * 10 for _ in range(10)]
    dense_map[0][0], dense_map[9][9] = "start", "treasure"
    g_dense = _build_grid(dense_map)
    dense_coins, dense_barriers, dense_treasure = g_dense[5], g_dense[3], g_dense[9]
    assert len(dense_coins) > 15, "this regression only bites when >15 tiles are worth taking"
    d8 = plan_path((0, 0), dense_map, hp_remaining=5, step_cost=1)
    v8 = _walk((0, 0), d8)
    assert d8, "a dense map must still produce movement"
    assert len(d8) <= MAX_ROUTE_DIRECTIONS, \
        f"dense route must fit one atomic reply, got {len(d8)} moves"
    assert v8[-1] == dense_treasure, f"dense route must end at the treasure: {v8[-1]}"
    assert not (set(v8) & dense_barriers), "dense route must never cross a wall"
    # A bare beeline is 18 moves and picks up ~17 coins purely by accident;
    # trimming a real scoring route must beat that by a wide margin.
    dense_collected = len(set(v8) & dense_coins)
    assert dense_collected >= 25, \
        f"dense route should still collect real score, only got {dense_collected} coins"

    # Trimming itself: an over-budget route must be shortened, not emptied.
    trim_dist = [[0, 2, 30, 4], [2, 0, 30, 4], [30, 30, 0, 30], [4, 4, 30, 0]]
    trim_scores = {0: 0, 1: 250, 2: 250, 3: 0}
    trimmed = _trim_to_move_budget([0, 1, 2, 3], trim_dist, trim_scores, 1, budget=10)
    assert trimmed == [0, 1, 3], \
        f"trim must drop only the far stop and keep the cheap one: {trimmed}"

    MANUAL_ROUTES.update(_saved_manual)  # drawn routes back in charge

    # OPERATOR-DRAWN ROUTES: the drawn line WINS. It must come back verbatim,
    # never re-planned and never swapped for something "safer", and taking
    # spike/door damage on it must NOT cause a rejection.
    drawn_map = [
        ["c42", "c5", "normal", "normal", "c1", "normal", "c7", "normal", "normal", "treasure"],
        ["c18", "normal", "normal", "c17", "wall", "normal", "normal", "normal", "normal", "normal"],
        ["normal", "normal", "normal", "normal", "wall", "c43", "normal", "normal", "normal", "normal"],
        ["wall", "wall", "wall", "c17", "wall", "wall", "c8", "wall", "wall", "c33"],
        ["start", "normal", "normal", "normal", "c8", "normal", "normal", "normal", "normal", "normal"],
        ["wall", "wall", "wall", "c8", "wall", "wall", "wall", "wall", "wall", "c32"],
        ["c8", "normal", "normal", "normal", "wall", "c7", "c7", "c7", "c7", "c1"],
        ["c17", "normal", "normal", "c18", "wall", "c2", "c7", "c7", "c7", "c7"],
        ["normal", "normal", "normal", "normal", "wall", "wall", "wall", "wall", "wall", "normal"],
        ["c8", "normal", "normal", "c5", "c17", "c7", "c7", "c7", "c7", "c7"],
    ]
    g_drawn = _build_grid(drawn_map)
    drawn_barriers = g_drawn[3]

    blue = plan_path((4, 0), drawn_map, hp_remaining=5)
    assert blue == MANUAL_ROUTE_BLUE, f"the BLUE drawn route must be returned verbatim: {blue}"
    blue_visited = _walk((4, 0), blue)
    assert not (set(blue_visited) & drawn_barriers), "BLUE route must not cross a wall"
    assert blue_visited[-1] == (7, 0), f"BLUE route must end at A8: {blue_visited[-1]}"
    assert (5, 3) in blue_visited, "BLUE route deliberately takes the D6 spike"

    yellow = plan_path((7, 0), drawn_map, hp_remaining=5)
    assert yellow == MANUAL_ROUTE_YELLOW, f"the YELLOW drawn route must be returned verbatim: {yellow}"
    yellow_visited = _walk((7, 0), yellow)
    assert not (set(yellow_visited) & drawn_barriers), "YELLOW route must not cross a wall"
    assert all(0 <= r < 10 and 0 <= c < 10 for r, c in yellow_visited), "YELLOW route left the grid"
    assert yellow_visited[-1] == (0, 9), f"YELLOW route must end at the J1 treasure: {yellow_visited[-1]}"
    assert (4, 4) in yellow_visited, "YELLOW route deliberately takes the E5 spike"
    assert (5, 9) in yellow_visited, "YELLOW route deliberately crosses the J6 grey door"
    # It is longer than the atomic planner budget ON PURPOSE - a drawn route is
    # never trimmed, because trimming it would stop following the line.
    assert len(yellow) > MAX_ROUTE_DIRECTIONS, \
        "the drawn YELLOW route is 50 moves and must NOT be cut down to the planner budget"

    # A drawn route that would hit a wall is the one case we refuse: the game
    # rejects that move and every later direction lands on the wrong tile.
    MANUAL_ROUTES[(4, 0)] = ["down"]  # A5 -> A6 is a wall
    assert plan_path((4, 0), drawn_map, hp_remaining=5) != ["down"], \
        "a drawn route that walks into a wall must fall through to the planner"
    MANUAL_ROUTES[(4, 0)] = MANUAL_ROUTE_BLUE

    # A malformed/empty Bedrock call must return an explicit error and NO
    # movement. It must never resurrect the fatal ["down", "right"]
    # fallback (A5 -> A6 wall) that caused the reported immediate loss.
    bad_event = {
        "messageVersion": "1.0", "actionGroup": "PathfindingLambdaTarget",
        "function": "plan_path", "parameters": [],
    }
    bad_response = lambda_handler(bad_event, None)
    bad_body = json.loads(
        bad_response["response"]["functionResponse"]["responseBody"]["TEXT"]["body"]
    )
    assert bad_body["directions"] == [] and bad_body.get("error"), bad_body

    print("OK: all self-checks passed, including exact supplied map + Bedrock transport")
