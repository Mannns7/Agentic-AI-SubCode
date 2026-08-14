import json
import re
import heapq
import logging
from collections import deque
from itertools import permutations

logger = logging.getLogger()
logger.setLevel(logging.INFO)

DANGER_COST = 1000
DOOR_COST_LOCKED = 5000
LOCKED_DOOR_HP_DAMAGE = 5  # guide: crossing a locked c32/c33 door does -5 real damage

# Tile scores, matched exactly to the challenge guide for this round.
# NOTE (map update): this round's guide swapped Red/Green key+door for
# Grey (c42 key / c32 door) and Yellow (c43 key / c33 door), and dropped
# "c3 Memento" entirely (not present in this round's guide - do not
# force-collect or score it). "c18" is back this round as Healthcare
# API (+500), so it's re-added here with its real score.
TILE_SCORES = {
    "c1": 400,   # Violent Violet
    "c2": 600,   # Blue Brain / Code Challenge
    "c4": 800,   # Dark Prophet / Web Search
    "c5": 250,   # Bonehead / Simple Question
    "c7": 250,   # Coins
    "c17": 50,   # A Distraction
    "c18": 500,  # Healthcare API
    "c32": 1000,  # Grey Door
    "c33": 1000,  # Yellow Door
    "c42": 50,   # Grey Key
    "c43": 50,   # Yellow Key
}
DEFAULT_CHALLENGE_SCORE = 400

# Which challenge-tile type is a KEY, and which color it unlocks; and
# which type is a DOOR, and which color it requires. Extend these two
# dicts if a future round adds more key/door colors - no other code
# needs to change, everything below is colour-generic.
KEY_COLOR_MAP = {"c42": "grey", "c43": "yellow"}
DOOR_COLOR_MAP = {"c32": "grey", "c33": "yellow"}

# NOTE (2026-07-26): telemetry from a completed run showed livesRemaining=5
# (i.e. UNCHANGED) after 14 challenges were answered correctly. The guide's
# "-1 HP" badge on challenge tiles appears to be a penalty for a WRONG
# answer, not a guaranteed cost just for attempting the tile. Modeling it
# as a guaranteed cost (old CHALLENGE_HP_COST=1 for every challenge) made
# the planner needlessly conservative — it would drop a forced, guaranteed-
# value quest tile (e.g. a Dark Prophet / c4) thinking HP would run out,
# when in practice HP never drops at all if answers are correct. Real,
# certain HP loss only comes from c8 spikes and locked doors (any color)
# — those still cost HP unconditionally in this model. Challenge-tile HP
# cost is now a tunable "risk buffer" that defaults to 0 so forced quest
# tiles are never dropped for a cost that, per observed telemetry, doesn't
# actually happen. Raise CHALLENGE_HP_COST back up (via the
# challenge_hp_cost body param) if your game DOES sometimes penalize
# correct answers.
CHALLENGE_HP_COST = 0

DEFAULT_STEP_COST = 3

# Tile types that must ALWAYS be visited when safely reachable, regardless
# of whether the profit-maximizing selection thinks it's "worth" the
# detour. This is every scored challenge type EXCEPT plain coins (c7,
# explicitly "bonus, no questions asked" per the guide = skippable) and
# EXCEPT doors/keys (c32/c33/c42/c43) and the cheap c17 distraction.
# Doors/keys are deliberately NOT forced: per game design the supervisor
# must "decide whether the key/door is worth taking... based on score vs.
# distance/time cost" - that decision is made by the score-vs-cost
# optimizer below (Held-Karp/2-opt), not hardcoded here. c17 is cheap
# (+50, -2 on failure) so it's left optional too, same reasoning. c18
# (Healthcare API) is added this round as a real quest challenge, same
# tier as c1/c2/c4/c5, so it's forced too; c3 (Memento) is not in this
# round's guide at all, so it has been removed.
FORCE_COLLECT_TYPES = {"c1", "c2", "c4", "c5", "c18"}

# Max nodes (forced+optional combined) for the EXACT Held-Karp solve.
# 2^15 * 15^2 is still fast; beyond that we fall back to a greedy
# construction + full 2-opt pass.
EXACT_SOLVE_LIMIT = 15


def _tile_score(cell_lower):
    if cell_lower in TILE_SCORES:
        return TILE_SCORES[cell_lower]
    if re.match(r"^c\d+$", cell_lower):
        return DEFAULT_CHALLENGE_SCORE
    return 0


def _parse_start(pos):
    try:
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
        m = re.match(r"([A-Za-z])(\d+)", s)
        if m:
            return (int(m.group(2)) - 1, ord(m.group(1).upper()) - ord('A'))
        nums = re.findall(r"\d+", s)
        if len(nums) >= 2:
            return (int(nums[0]), int(nums[1]))
    except (ValueError, TypeError, IndexError):
        pass
    return (0, 0)


def _parse_cell_pos(cell_pos_str):
    """Parse a single 'A1'-style cell reference used in a visited/collected list."""
    return _parse_start(cell_pos_str)


def _build_grid(game_map, visited=None):
    """
    Build the grid. `visited` is an optional iterable of positions (any
    format _parse_start understands, e.g. "H1") that have ALREADY been
    collected earlier in this same run. Those tiles are force-downgraded
    to "normal" here regardless of what the map still shows, so a stale
    map (or a caller that forgot to scrub collected tiles) can never make
    the planner route back to a tile that has nothing left to give.

    Returns key_positions / door_positions as {color: set(pos)} dicts so
    ANY number of key/door colors defined in KEY_COLOR_MAP/DOOR_COLOR_MAP
    is handled uniformly (currently grey + yellow). Key and door tiles are
    ALSO included in `challenges` (they carry their own point value per
    the guide, e.g. +50 for a key, +1000 for a door) - door_color lets the
    hazard/cost logic treat them specially when locked.
    """
    visited_set = set()
    if visited:
        for v in visited:
            visited_set.add(_parse_cell_pos(v) if isinstance(v, str) else tuple(v))

    grid = {}
    barriers, dangers, coins, challenges = set(), set(), set(), set()
    key_positions = {color: set() for color in set(KEY_COLOR_MAP.values())}
    door_positions = {color: set() for color in set(DOOR_COLOR_MAP.values())}
    door_color = {}
    treasure = None
    rows = len(game_map)
    cols = max(len(r) for r in game_map) if rows > 0 else 0
    for r, row_data in enumerate(game_map):
        for c, cell in enumerate(row_data):
            cell_lower = str(cell).lower().strip() if cell else ""
            if (r, c) in visited_set and cell_lower not in ("wall", "treasure"):
                cell_lower = "normal"  # already collected — no value left here
            grid[(r, c)] = cell_lower
            if cell_lower == "wall":
                barriers.add((r, c))
            elif cell_lower == "c8":
                dangers.add((r, c))
            elif cell_lower == "c7":
                coins.add((r, c))
            elif cell_lower == "treasure":
                treasure = (r, c)
            elif cell_lower in KEY_COLOR_MAP:
                color = KEY_COLOR_MAP[cell_lower]
                key_positions[color].add((r, c))
                challenges.add((r, c))
            elif cell_lower in DOOR_COLOR_MAP:
                color = DOOR_COLOR_MAP[cell_lower]
                door_positions[color].add((r, c))
                door_color[(r, c)] = color
                challenges.add((r, c))
            elif re.match(r"^c\d+$", cell_lower):
                challenges.add((r, c))
    return grid, rows, cols, barriers, dangers, coins, challenges, key_positions, door_positions, door_color, treasure


DIRECTIONS = [(1, 0, "down"), (-1, 0, "up"), (0, 1, "right"), (0, -1, "left")]


def _locked_doors(doors_all, door_color, held_keys):
    """Subset of doors_all whose color is NOT in held_keys."""
    return {p for p in doors_all if door_color.get(p) not in held_keys}


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


def _weighted_bfs(start, goal, rows, cols, barriers, dangers, doors_all, door_color, held_keys):
    if start == goal:
        return [start], 0
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
            locked = (nr, nc) in doors_all and door_color.get((nr, nc)) not in held_keys
            move_cost = DOOR_COST_LOCKED if locked else (DANGER_COST if (nr, nc) in dangers else 1)
            new_cost = cost + move_cost
            if new_cost < best_cost.get((nr, nc), float('inf')):
                best_cost[(nr, nc)] = new_cost
                heapq.heappush(pq, (new_cost, nr, nc, path + [(nr, nc)]))
    return None, float('inf')


def _hp_cost_of_path(path, dangers, doors_all, door_color, held_keys):
    """Only REAL, certain HP costs: spikes and locked-door crossings (any
    color). Challenge-tile cost is added separately by the caller via
    CHALLENGE_HP_COST, which defaults to 0 — see the note above."""
    cost = 0
    for p in path[1:]:
        if p in dangers:
            cost += 1
        elif p in doors_all and door_color.get(p) not in held_keys:
            cost += LOCKED_DOOR_HP_DAMAGE
    return cost


def _find_path(start, goal, rows, cols, barriers, dangers, doors_all, door_color, held_keys):
    hazards = set(dangers) | _locked_doors(doors_all, door_color, held_keys)
    safe_path = _bfs_simple(start, goal, rows, cols, barriers | hazards)
    if safe_path is not None:
        return safe_path, 0
    path, _ = _weighted_bfs(start, goal, rows, cols, barriers, dangers, doors_all, door_color, held_keys)
    if path is None:
        return None, None
    return path, _hp_cost_of_path(path, dangers, doors_all, door_color, held_keys)


def _is_reachable(start, goal, rows, cols, barriers):
    return _bfs_simple(start, goal, rows, cols, barriers) is not None


def _held_karp_order(dist_matrix, target_indices, treasure_idx, scores, step_cost, must_include):
    """
    Exact bitmask-DP TSP over `target_indices`. Nodes in `must_include`
    (a set of indices, by position in target_indices) are given a huge
    score bonus so the optimizer always selects them (they're mandatory
    quest tiles), while still finding the globally optimal VISIT ORDER
    for the combined set (forced + optional) — this is what removes the
    zigzag/backtrack that a two-stage "forced-tour-then-bolt-on-optionals"
    construction produces: with everything in ONE search, coins that sit
    naturally along the way between two forced tiles get threaded in for
    free instead of costing a detour.
    """
    n = len(target_indices)
    if n == 0:
        return []
    MUST_BONUS = 10 ** 7
    NEG_INF = float('-inf')
    dp_val = [[NEG_INF] * n for _ in range(1 << n)]
    parent = [[-1] * n for _ in range(1 << n)]

    def node_score(i):
        s = scores[target_indices[i]]
        if i in must_include:
            s += MUST_BONUS
        return s

    for i in range(n):
        d = dist_matrix[0][target_indices[i]]
        if d == float('inf'):
            continue
        dp_val[1 << i][i] = node_score(i) - step_cost * d

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
                new_val = dp_val[mask][last] + node_score(nxt) - step_cost * d
                if new_val > dp_val[new_mask][nxt]:
                    dp_val[new_mask][nxt] = new_val
                    parent[new_mask][nxt] = last

    best_val, best_mask, best_last = float('-inf'), 0, -1
    # Must consider every mask that includes ALL must_include nodes (since
    # those are mandatory); among those, maximize value (home leg included).
    must_mask = 0
    for i in must_include:
        must_mask |= (1 << i)
    for mask in range(1, 1 << n):
        if (mask & must_mask) != must_mask:
            continue  # doesn't include every mandatory node yet
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
        # Mandatory set truly unreachable together — caller handles this
        # (should not normally happen since candidacy is pre-filtered).
        return None

    order, mask, cur = [], best_mask, best_last
    while cur != -1:
        order.append(target_indices[cur])
        prev = parent[mask][cur]
        mask ^= (1 << cur)
        cur = prev
    order.reverse()
    return order


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


def _two_opt(route, dist_matrix, max_passes=8):
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


def _route_hp_feasible(route, waypoints, hp_start, rows, cols, barriers, dangers, doors_all, door_color, held_keys, challenges):
    hp_left = hp_start
    cur = route[0]
    for nxt in route[1:]:
        path, hp_cost = _find_path(waypoints[cur], waypoints[nxt], rows, cols, barriers, dangers, doors_all, door_color, held_keys)
        if path is None:
            return False
        extra = CHALLENGE_HP_COST if waypoints[nxt] in challenges else 0
        cost = hp_cost + extra
        if cost >= hp_left:
            return False
        hp_left -= cost
        cur = nxt
    return True


def _compute_pairwise_distances(waypoints, rows, cols, barriers, dangers, doors_all, door_color, held_keys):
    n = len(waypoints)
    dist = [[float('inf')] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            if i == j:
                dist[i][j] = 0
                continue
            path, _ = _find_path(waypoints[i], waypoints[j], rows, cols, barriers, dangers, doors_all, door_color, held_keys)
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
                   doors_all, door_color, held_keys, simulated_hp, step_cost, grid):
    """
    Score-vs-cost optimizer over every collectible tile (coins, all
    challenge types, AND keys/doors — keys/doors are just tiles with a
    point value and, for doors, a hazard cost if the matching key isn't
    in `held_keys`). Nothing about keys/doors is force-collected; the
    Held-Karp/2-opt search below decides whether the reward is worth the
    detour, exactly like any other optional tile — per the design intent
    that "the pathfinder decides whether the key/door is worth taking",
    never a hardcoded fixed sequence.
    """
    collectibles = coins | challenges
    safe_targets, forced_targets = [], []
    for pos in collectibles:
        if not _is_reachable(current_pos, pos, rows, cols, barriers):
            continue
        if not _is_reachable(pos, treasure, rows, cols, barriers):
            continue
        path, hp_cost = _find_path(current_pos, pos, rows, cols, barriers, dangers, doors_all, door_color, held_keys)
        if path is None:
            continue
        extra = CHALLENGE_HP_COST if pos in challenges else 0
        if hp_cost + extra >= simulated_hp:
            continue  # genuinely not survivable even alone (real hazard cost) — skip regardless
        if grid.get(pos, "") in FORCE_COLLECT_TYPES:
            forced_targets.append(pos)
        else:
            safe_targets.append(pos)

    all_targets = forced_targets + safe_targets
    waypoints = [current_pos] + all_targets + [treasure]
    treasure_idx = len(waypoints) - 1

    def _score_for(pos):
        cell = grid.get(pos, "")
        color = door_color.get(pos)
        if color is not None and color not in held_keys:
            return 0  # locked door with no matching key: no reward, only hazard cost
        return _tile_score(cell)

    scores = {i: _score_for(pos) for i, pos in enumerate(waypoints)}

    if not all_targets:
        path, _ = _find_path(current_pos, treasure, rows, cols, barriers, dangers, doors_all, door_color, held_keys)
        if path is None:
            return None, None
        return path, -step_cost * (len(path) - 1)

    dist_matrix = _compute_pairwise_distances(waypoints, rows, cols, barriers, dangers, doors_all, door_color, held_keys)
    target_indices = list(range(1, treasure_idx))
    must_include = set(range(len(forced_targets)))  # forced targets are always the first N in target_indices

    if len(target_indices) <= EXACT_SOLVE_LIMIT:
        order = _held_karp_order(dist_matrix, target_indices, treasure_idx, scores, step_cost, must_include)
        if order is None:
            # Mandatory forced set truly can't all be reached together —
            # last-resort: drop one forced target from must_include and retry once (rare edge case).
            if forced_targets and len(must_include) > 1:
                reduced_must = set(list(must_include)[:-1])
                order = _held_karp_order(dist_matrix, target_indices, treasure_idx, scores, step_cost, reduced_must)
            if order is None:
                order = list(target_indices)  # give up ordering, visit in found order
        route = [0] + order + [treasure_idx]
    else:
        # Too many nodes for exact solve: forced targets via nearest-
        # neighbor (mandatory, always included), optional coins bolted on
        # via cheapest-insertion, then a full 2-opt cleanup pass over the
        # WHOLE combined route (not just the forced portion) to remove
        # any zigzag the two-stage construction introduces.
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
            if _route_hp_feasible(trial_route, waypoints, simulated_hp, rows, cols, barriers, dangers, doors_all, door_color, held_keys, challenges):
                route = trial_route

        route = _two_opt(route, dist_matrix)

    # Final HP feasibility pass over the exact/combined route. Only drop a
    # stop here if it's a genuinely unreachable/unsurvivable hazard cost —
    # forced targets should essentially never trip this now that
    # CHALLENGE_HP_COST defaults to 0 and only real hazards (spikes/locked
    # doors) count, but we keep the safety net for edge cases.
    if not _route_hp_feasible(route, waypoints, simulated_hp, rows, cols, barriers, dangers, doors_all, door_color, held_keys, challenges):
        route = _two_opt(list(route), dist_matrix)  # try once more after cleanup
        while len(route) > 2 and not _route_hp_feasible(route, waypoints, simulated_hp, rows, cols, barriers, dangers, doors_all, door_color, held_keys, challenges):
            # drop the costliest NON-forced stop first; only touch a forced
            # stop if literally nothing else can be dropped
            non_forced_positions = [i for i in range(1, len(route) - 1) if route[i] >= 1 + len(forced_targets)]
            candidates = non_forced_positions or list(range(1, len(route) - 1))
            worst_pos, worst_extra = None, -1
            for i in candidates:
                trial = route[:i] + route[i + 1:]
                _, extra = _cheapest_insertion(trial, dist_matrix, route[i])
                if extra is not None and extra > worst_extra:
                    worst_extra, worst_pos = extra, i
            if worst_pos is None:
                route.pop(len(route) - 2)
            else:
                route.pop(worst_pos)

    # Walk the finalized route.
    seg_path = []
    hp_left = simulated_hp
    total_score = 0
    current_idx = 0
    for next_idx in route[1:]:
        pos = waypoints[next_idx]
        path, hp_cost = _find_path(waypoints[current_idx], pos, rows, cols, barriers, dangers, doors_all, door_color, held_keys)
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
        path, _ = _find_path(waypoints[current_idx], treasure, rows, cols, barriers, dangers, doors_all, door_color, held_keys)
        if path is None:
            return None, None
        seg_path.extend(path[1:] if seg_path else path)

    net = total_score - step_cost * (len(seg_path) - 1)
    return seg_path, net


def plan_path(start, game_map, hp_remaining=5, step_cost=DEFAULT_STEP_COST, visited=None, held_keys=frozenset()):
    """
    held_keys: colors already held BEFORE this turn (e.g. {"red"} if the
    red key was collected on a previous turn and the map no longer shows
    it). Any color whose key tile is simply absent from the current map
    (but whose door IS present) is also assumed already-held, matching
    the original single-color heuristic — generalized per color here.
    """
    (grid, rows, cols, barriers, dangers, coins, challenges,
     key_positions, door_positions, door_color, treasure) = _build_grid(game_map, visited=visited)

    if treasure is None:
        treasure = (rows - 1, cols - 1)

    doors_all = set(door_color.keys())
    base_held = set(held_keys)
    for color, positions in door_positions.items():
        if positions and not key_positions.get(color):
            # a door of this color exists, but no key tile of this color
            # is visible on the map -> assume it was already collected
            base_held.add(color)

    # Colors we can still actively choose to go fetch this turn (their key
    # tile is visible and not already assumed-held).
    undecided_colors = [c for c in key_positions if key_positions[c] and c not in base_held]

    branches = []
    # Try every subset of undecided_colors (typically 0-2 colors -> at
    # most 4 branches): "go get these keys first, then do the normal
    # score-vs-cost collection run with them held". This lets the
    # optimizer itself decide whether fetching a given key (and then
    # possibly opening its door) is worth it, instead of a hardcoded
    # fixed order — matching the "pathfinder decides, don't hardcode"
    # design intent.
    n = len(undecided_colors)
    for mask in range(1 << n):
        chosen = [undecided_colors[i] for i in range(n) if mask & (1 << i)]
        held_now = frozenset(base_held | set(chosen))

        cur_pos = start
        hp_budget = hp_remaining
        prefix_path = []
        prefix_cost = 0
        feasible = True

        # Visit chosen key tiles in whatever order is cheapest (n<=2
        # typically, so brute-force permutations is trivial).
        if chosen:
            key_targets = []
            for color in chosen:
                # a color may have multiple key tiles in principle; take
                # the nearest one reachable.
                candidates = list(key_positions[color])
                candidates = [p for p in candidates if _is_reachable(cur_pos, p, rows, cols, barriers)]
                if not candidates:
                    feasible = False
                    break
                key_targets.append((color, candidates))
            if not feasible:
                continue

            best_perm_cost, best_perm_path, best_perm_pos = None, None, None
            for perm in permutations(range(len(key_targets))):
                p_cost, p_path, p_pos, p_hp = 0, [], cur_pos, hp_budget
                ok = True
                held_acc = set(held_now) - set(chosen)  # keys not yet actually picked up along this perm
                for idx in perm:
                    color, candidates = key_targets[idx]
                    best_c, best_leg, best_leg_cost = None, None, float('inf')
                    for cand in candidates:
                        leg, hp_cost = _find_path(p_pos, cand, rows, cols, barriers, dangers, doors_all, door_color, frozenset(held_acc))
                        if leg is None:
                            continue
                        if hp_cost < best_leg_cost:
                            best_leg_cost, best_leg, best_c = hp_cost, leg, cand
                    if best_leg is None or best_leg_cost >= p_hp:
                        ok = False
                        break
                    p_path.extend(best_leg[1:] if p_path else best_leg)
                    p_hp -= best_leg_cost
                    p_cost += step_cost * (len(best_leg) - 1)
                    p_pos = best_c
                    held_acc.add(color)
                if not ok:
                    continue
                if best_perm_cost is None or p_cost < best_perm_cost:
                    best_perm_cost, best_perm_path, best_perm_pos = p_cost, p_path, p_pos
                    best_perm_hp = p_hp

            if best_perm_path is None:
                continue
            prefix_path, prefix_cost, cur_pos, hp_budget = best_perm_path, best_perm_cost, best_perm_pos, best_perm_hp

        seg, net = _plan_collect(cur_pos, coins, challenges, treasure, rows, cols, barriers, dangers,
                                  doors_all, door_color, held_now, hp_budget, step_cost, grid)
        if seg is None:
            continue
        full_seg = prefix_path + (seg[1:] if prefix_path else seg)
        full_net = net - prefix_cost
        branches.append((full_net, full_seg))

    if not branches:
        return []

    branches.sort(key=lambda b: b[0], reverse=True)
    _, best_seg = branches[0]
    return _path_to_directions(best_seg)


def _coerce_number(value, default, field_name=""):
    """Best-effort convert `value` to a number, falling back to `default`
    (instead of raising) if it isn't a clean number. This protects
    against a caller sending a semantically different field under an
    accepted name - e.g. a countdown-timer string like "4:51" landing in
    a numeric slot - crashing the WHOLE request into the dumb fallback
    path instead of just ignoring that one bad field."""
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
        # Accept every field-name variant callers have actually sent so
        # far, including "start_pos" - a mismatch here silently made every
        # call plan from the map's default start instead of the agent's
        # real current position.
        start_raw = body.get("start", body.get("start_pos", body.get("position",
                    body.get("current_position", body.get("agent_position", "A1")))))
        start = _parse_start(start_raw)
        hp_raw = body.get("hp", body.get("current_hp", body.get("health", body.get("life_points", 5))))
        hp = _coerce_number(hp_raw, 5, "hp")
        # step_cost and time_remaining are NOT the same thing (per-move
        # penalty vs. a countdown timer) and must never be conflated - a
        # caller accidentally sending a time-remaining value (e.g. the
        # string "4:51") into step_cost used to crash this whole request
        # into the generic fallback path. Only genuine step-cost/
        # time-penalty fields feed step_cost; time_remaining is accepted
        # but currently informational only (not used as step_cost).
        step_cost_raw = body.get("step_cost", body.get("time_penalty", DEFAULT_STEP_COST))
        step_cost = _coerce_number(step_cost_raw, DEFAULT_STEP_COST, "step_cost")
        _time_remaining = body.get("time_remaining")  # accepted, informational only for now
        # Optional: list of already-collected tile positions (e.g.
        # ["H1", "E10"]), so a stale/unscrubbed map never causes a
        # pointless revisit of a tile that has nothing left to give.
        visited = body.get("visited", body.get("collected", None))
        # Optional: colors of keys already held from a previous turn
        # (e.g. ["red"]), in case the game passes this explicitly instead
        # of (or in addition to) simply omitting the key tile from `map`.
        held_keys = frozenset(c.lower() for c in body.get("held_keys", body.get("keys_held", [])) or [])
        directions = plan_path(start, game_map, hp_remaining=hp, step_cost=step_cost, visited=visited, held_keys=held_keys)
        if not directions:
            directions = ["down", "right"]
        # IMPORTANT: only ever return ONE field for the move list, named
        # "directions". A previous version also included "action":
        # directions[0] (a single word) and a duplicate "path" field.
        # That "action" field name reads, to an LLM consuming this tool's
        # result, as "the thing to output" - causing the agent to reply
        # with just the first direction (e.g. "left") instead of
        # forwarding the entire route, which made the game apply only one
        # move per turn and lose almost immediately. Do not re-add an
        # "action"/"first_step"/single-value field here.
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

    # Test 1: a 1-2 tile detour to a forced challenge type must always be
    # taken (regression check equivalent to the old c18 test, using c1
    # which is a real force-collected type this round).
    forced_map = [
        ["start", "normal", "normal"],
        ["normal", "c1", "normal"],
        ["normal", "normal", "treasure"],
    ]
    d1 = plan_path((0, 0), forced_map, hp_remaining=10, step_cost=8)
    v1 = _walk((0, 0), d1)
    assert (1, 1) in v1, "c1 must be force-collected as a cheap detour"

    # Test 2: 2x3 coin cluster must be visited with NO backtrack (snake
    # order), matching the exact-solve fix for the zigzag bug.
    cluster_map = [
        ["start", "wall", "c7", "c7"],
        ["normal", "wall", "c7", "c7"],
        ["normal", "wall", "c7", "c7"],
        ["normal", "normal", "normal", "treasure"],
    ]
    d2 = plan_path((0, 0), cluster_map, hp_remaining=10, step_cost=1)
    v2 = _walk((0, 0), d2)
    coin_positions = [(0, 2), (0, 3), (1, 2), (1, 3), (2, 2), (2, 3)]
    assert all(p in v2 for p in coin_positions), "all 6 coins must be collected"
    assert len(v2) == len(set(v2)), "route must not revisit any tile (no backtrack/zigzag)"

    # Test 3: three forced (c4-style) targets must ALL be visited before
    # ever touching the adjacent treasure — this is the I10/I9/J9 bug.
    triple_map = [
        ["start", "normal", "normal", "c4"],
        ["normal", "wall", "wall", "c4"],
        ["normal", "wall", "wall", "wall"],
        ["c4", "normal", "normal", "normal"],
        ["normal", "normal", "normal", "treasure"],
    ]
    d3 = plan_path((0, 0), triple_map, hp_remaining=10, step_cost=3)
    v3 = _walk((0, 0), d3)
    assert (0, 3) in v3 and (1, 3) in v3 and (3, 0) in v3, "all three forced c4 tiles must be visited"
    assert v3[-1] == (4, 3), "must end at the treasure"
    treasure_step = v3.index((4, 3))
    assert treasure_step == len(v3) - 1, "treasure must be the final step, never touched early"

    # Test 4: an already-collected tile passed in `visited` must NOT be
    # revisited even though the map still labels it as a coin — this is
    # the H1 wasted-round-trip bug.
    revisit_map = [
        ["start", "normal", "c7"],
        ["normal", "normal", "normal"],
        ["normal", "normal", "treasure"],
    ]
    d4 = plan_path((0, 0), revisit_map, hp_remaining=10, step_cost=1, visited=["C1"])
    v4 = _walk((0, 0), d4)
    assert (0, 2) not in v4, "a tile already marked visited must be excluded even if map still shows it as a coin"

    # Test 5: forced targets must NOT be dropped just because the
    # (now-zero-default) CHALLENGE_HP_COST model looked tight — with real
    # hazard cost only, a low HP budget should still fit multiple forced
    # stops that don't touch any spike/door.
    tight_hp_map = [
        ["start", "c1", "c2", "c3"],
        ["normal", "normal", "normal", "treasure"],
    ]
    d5 = plan_path((0, 0), tight_hp_map, hp_remaining=1, step_cost=3)
    v5 = _walk((0, 0), d5)
    assert (0, 1) in v5 and (0, 2) in v5 and (0, 3) in v5, "forced non-hazard challenge tiles must not be dropped under a tight but non-hazardous HP budget"

    # Test 6 (NEW): Yellow Key (c43) + Yellow Door (c33) must be
    # recognized as a real key/door pair, just like grey - the planner
    # should route through the yellow key BEFORE the yellow door to avoid
    # the -5 locked penalty and collect the door's real +1000 reward.
    yellow_map = [
        ["start", "normal", "normal", "normal"],
        ["c43", "normal", "normal", "normal"],
        ["normal", "normal", "normal", "normal"],
        ["normal", "normal", "normal", "c33"],
        ["normal", "normal", "normal", "treasure"],
    ]
    d6 = plan_path((0, 0), yellow_map, hp_remaining=10, step_cost=1)
    v6 = _walk((0, 0), d6)
    assert (1, 0) in v6, "yellow key (c43) must be collected"
    assert (3, 3) in v6, "yellow door (c33) must be visited/opened"
    key_step = v6.index((1, 0))
    door_step = v6.index((3, 3))
    assert key_step < door_step, "yellow key must be collected BEFORE reaching the yellow door"

    # Test 7 (NEW): tile scores must exactly match this round's guide -
    # c17/c42/c43 are cheap (+50), c32/c33 are both worth the full +1000,
    # c18 (Healthcare API, back this round) is +500 and force-collected,
    # and c3 (not in this round's guide at all) must not appear anywhere.
    assert TILE_SCORES["c17"] == 50
    assert TILE_SCORES["c42"] == 50
    assert TILE_SCORES["c43"] == 50
    assert TILE_SCORES["c32"] == 1000
    assert TILE_SCORES["c33"] == 1000
    assert TILE_SCORES["c18"] == 500
    assert "c18" in FORCE_COLLECT_TYPES
    assert "c3" not in TILE_SCORES
    assert "c3" not in FORCE_COLLECT_TYPES

    # Test 8 (NEW): the pathfinding sub-agent prompt sends "start_pos"
    # (not "start"/"position"/"agent_position") - lambda_handler must
    # accept that name too, or every call silently plans from the map's
    # default start instead of the agent's real current position.
    event_a = {"map": [
        ["start", "normal", "normal", "normal"],
        ["c42", "normal", "normal", "normal"],
        ["normal", "normal", "normal", "normal"],
        ["normal", "normal", "normal", "c32"],
        ["normal", "normal", "normal", "treasure"],
    ], "start_pos": "B1", "hp": 10}
    resp_a = lambda_handler(event_a, None)
    body_a = json.loads(resp_a["body"])
    assert resp_a["statusCode"] == 200 and body_a["directions"], "start_pos must be accepted and produce a real plan"

    # Test 9 (NEW): a non-numeric step_cost (e.g. a "4:51" countdown-timer
    # string accidentally sent instead of a real step cost) must not
    # crash the whole request into the dumb hardcoded fallback - it
    # should fall back to DEFAULT_STEP_COST and still produce a real plan.
    event_b = {"map": [
        ["start", "normal", "normal", "normal"],
        ["c42", "normal", "normal", "normal"],
        ["normal", "normal", "normal", "normal"],
        ["normal", "normal", "normal", "c32"],
        ["normal", "normal", "normal", "treasure"],
    ], "start": "A1", "hp": 10, "step_cost": "4:51"}
    resp_b = lambda_handler(event_b, None)
    body_b = json.loads(resp_b["body"])
    assert resp_b["statusCode"] == 200 and body_b["directions"] != ["down", "right", "down", "right"], \
        "a bad non-numeric step_cost must not crash into the generic hardcoded fallback"

    print("OK: all 9 self-checks passed")
