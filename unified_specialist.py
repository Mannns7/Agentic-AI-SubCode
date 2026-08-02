"""
Unified Specialist Lambda
==========================
Combines THREE previously-separate sub-agent tools into ONE Lambda /
ONE tool, dispatched by an "action" field in the request body:

  action="execute_code"    -> restricted, sandboxed Python code execution
                               (was: CodeExecution)
  action="scrape_website"  -> fetch + clean-text extraction of a URL
                               (was: dark_prophet_scraper.py)
  action="plan_path"       -> dungeon-map pathfinding / route optimizer
                               (was: pathfinding.py)

Each action's request/response SHAPE is preserved EXACTLY as the
original standalone Lambda produced, so any existing prompt/agent logic
that already parses those responses keeps working unchanged - only the
entry point and file are unified.

If "action" is omitted, the handler auto-detects it from which fields
are present in the body (url -> scrape_website, code -> execute_code,
map/game_map/grid/start/start_pos -> plan_path), for backward
compatibility with callers that used to hit three separate Lambdas and
therefore never had to send an action name.

Deploying this file:
- Point ONE Lambda function's handler at unified_specialist.lambda_handler.
- Register ONE tool/action-group ("unified_specialist") in place of the
  three old ones (codeexecution_specialist, pathfinding_specialist,
  websearch_specialist), with an "action" parameter as described above.
"""

import ast
import contextlib
import heapq
import io
import json
import logging
import math
import re
import signal
import urllib.request
from collections import deque
from html.parser import HTMLParser
from itertools import permutations

logger = logging.getLogger()
logger.setLevel(logging.INFO)


# ============================================================================
# ACTION 1: execute_code  (was: CodeExecution)
# ============================================================================
# --- SAFETY: restricted execution environment ---
# Only expose a curated, safe subset of builtins and modules. No file I/O,
# no network, no process/OS access, no dynamic import of arbitrary modules.
CODE_SAFE_BUILTINS = {
    "abs": abs, "all": all, "any": any, "bin": bin, "bool": bool,
    "chr": chr, "dict": dict, "divmod": divmod, "enumerate": enumerate,
    "filter": filter, "float": float, "format": format, "frozenset": frozenset,
    "hex": hex, "int": int, "isinstance": isinstance, "len": len,
    "list": list, "map": map, "max": max, "min": min, "oct": oct,
    "ord": ord, "pow": pow, "print": print, "range": range,
    "repr": repr, "reversed": reversed, "round": round, "set": set,
    "sorted": sorted, "str": str, "sum": sum, "tuple": tuple, "zip": zip,
    "True": True, "False": False, "None": None,
}

CODE_SAFE_MODULES = {
    "math": math,
    "itertools": __import__("itertools"),
    "functools": __import__("functools"),
    "collections": __import__("collections"),
    "re": __import__("re"),
    "decimal": __import__("decimal"),
    "fractions": __import__("fractions"),
    "string": __import__("string"),  # e.g. string.ascii_uppercase — handy for
                                      # cipher/alphabet challenges (c30/c31 doors)
}

CODE_EXEC_TIMEOUT_SECONDS = 10


class _CodeTimeoutError(Exception):
    pass


def _code_timeout_handler(signum, frame):
    raise _CodeTimeoutError(f"Code execution exceeded {CODE_EXEC_TIMEOUT_SECONDS}s — likely an infinite loop.")


def _code_restricted_import(name, *args, **kwargs):
    if name in CODE_SAFE_MODULES:
        return CODE_SAFE_MODULES[name]
    raise ImportError(f"Import of '{name}' is not permitted in this sandbox. "
                       f"Allowed: {', '.join(sorted(CODE_SAFE_MODULES))}")


def _code_build_safe_globals():
    safe_builtins = dict(CODE_SAFE_BUILTINS)
    safe_builtins["__import__"] = _code_restricted_import
    return {"__builtins__": safe_builtins, **CODE_SAFE_MODULES}


def execute_code(code, timeout_seconds=CODE_EXEC_TIMEOUT_SECONDS):
    """
    Execute a snippet of Python code in a restricted sandbox and return:
      { "stdout": <captured print output>,
        "result": <value of a `result = ...` variable if set, else the
                   value of the last top-level expression, if any>,
        "error": <None or an error message> }

    Any code that references disallowed names (open, os, sys, subprocess,
    eval, exec, __import__ of unlisted modules, etc.) will raise a
    NameError/ImportError rather than execute, since they simply aren't in
    the restricted global namespace.
    """
    global CODE_EXEC_TIMEOUT_SECONDS
    CODE_EXEC_TIMEOUT_SECONDS = timeout_seconds

    stdout_buffer = io.StringIO()
    safe_globals = _code_build_safe_globals()
    safe_locals = {}

    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as e:
        return {"stdout": "", "result": None, "error": f"SyntaxError: {e}"}

    # If the last top-level statement is a bare expression, capture its
    # value automatically (REPL-style), like Jupyter/Code Interpreter does.
    last_expr = None
    if tree.body and isinstance(tree.body[-1], ast.Expr):
        last_expr = tree.body.pop()

    old_handler = None
    try:
        old_handler = signal.signal(signal.SIGALRM, _code_timeout_handler)
        signal.alarm(timeout_seconds)

        with contextlib.redirect_stdout(stdout_buffer):
            exec(compile(tree, "<sandbox>", "exec"), safe_globals, safe_locals)
            last_value = None
            if last_expr is not None:
                last_value = eval(compile(ast.Expression(last_expr.value), "<sandbox>", "eval"),
                                   safe_globals, safe_locals)

        signal.alarm(0)

        result = safe_locals.get("result", last_value)
        return {"stdout": stdout_buffer.getvalue(), "result": _code_jsonable(result), "error": None}

    except _CodeTimeoutError as e:
        return {"stdout": stdout_buffer.getvalue(), "result": None, "error": str(e)}
    except Exception as e:
        return {"stdout": stdout_buffer.getvalue(), "result": None, "error": f"{type(e).__name__}: {e}"}
    finally:
        signal.alarm(0)
        if old_handler is not None:
            signal.signal(signal.SIGALRM, old_handler)


def _code_jsonable(value):
    """Make the result JSON-safe. Big ints stay exact via str(); anything
    else that can't be serialized falls back to repr()."""
    if value is None or isinstance(value, (bool, int, float, str)):
        if isinstance(value, int) and not isinstance(value, bool) and abs(value) > 10**15:
            return str(value)  # preserve exact precision for huge integers
        return value
    if isinstance(value, (list, tuple)):
        return [_code_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _code_jsonable(v) for k, v in value.items()}
    try:
        json.dumps(value)
        return value
    except TypeError:
        return repr(value)


def _handle_execute_code(body):
    """
    Body params:
      code: string — Python code to execute (required)
      timeout_seconds: int — optional override, default 10, capped at 25
                        (stay under typical Lambda timeout with margin)

    Challenge types (from the game guide) meant to be delegated here:

    - c2  Blue Brain (code challenge): any exact large-number computation,
      e.g. "Tell me the 3000th Fibonacci number, last 10 digits":
          def fib(n):
              a, b = 0, 1
              for _ in range(n):
                  a, b = b, a + b
              return a
          result = str(fib(3000))[-10:]

    - c30 Red Door: cipher is "read the received code backwards".
          result = given_code[::-1]

    - c31 Green Door: cipher is "replace each letter with the number
      representing its position in the alphabet (A=1, B=2, ... Z=26)".
          result = "-".join(str(ord(ch.upper()) - ord('A') + 1)
                             for ch in given_code if ch.isalpha())
      (Adjust the join/format to match whatever the game literally asks
      for — join with spaces, dashes, or no separator as specified.)
    """
    try:
        code = body.get("code")
        if not code or not isinstance(code, str):
            return _code_err("Missing required 'code' string parameter.")

        timeout_seconds = min(int(body.get("timeout_seconds", CODE_EXEC_TIMEOUT_SECONDS)), 25)
        result = execute_code(code, timeout_seconds=timeout_seconds)
        return {"statusCode": 200, "body": json.dumps(result)}
    except Exception as e:
        return _code_err(f"Fallback mode: {e}")


def _code_err(message):
    return {"statusCode": 200, "body": json.dumps({"stdout": "", "result": None, "error": message})}


# ============================================================================
# ACTION 2: scrape_website  (was: dark_prophet_scraper.py)
# ============================================================================
class CleanHTMLParser(HTMLParser):
    """
    A lightweight HTML parser using only built-in Python modules.
    Strips out JavaScript, CSS, and extracts readable text.
    """
    def __init__(self):
        super().__init__()
        self.text_parts = []
        self.ignore_tag = False
        self.ignored_tags = {"script", "style", "nav", "footer", "header", "aside", "noscript", "form"}

    def handle_starttag(self, tag, attrs):
        if tag.lower() in self.ignored_tags:
            self.ignore_tag = True

    def handle_endtag(self, tag):
        if tag.lower() in self.ignored_tags:
            self.ignore_tag = False

    def handle_data(self, data):
        if not self.ignore_tag:
            cleaned = data.strip()
            if cleaned:
                self.text_parts.append(cleaned)

    def get_text(self):
        return "\n".join(self.text_parts)


def _handle_scrape_website(body):
    """
    Body params:
      url: string — required
      max_length: int — optional, default 4000
    """
    url = body.get("url")
    max_length = body.get("max_length", 4000)

    if not url:
        return {
            "statusCode": 400,
            "body": json.dumps({"error": "Missing required parameter 'url'."})
        }

    # Browser user-agent header to reduce basic blocking
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as response:
            html_bytes = response.read()
            html_content = html_bytes.decode("utf-8", errors="ignore")

        # Parse HTML using Python standard library
        parser = CleanHTMLParser()
        parser.feed(html_content)
        raw_text = parser.get_text()

        # Clean up double line breaks and empty spaces
        clean_text = re.sub(r"\n\s*\n", "\n\n", raw_text)

        # Truncate content for agent budget
        is_truncated = len(clean_text) > max_length
        final_text = clean_text[:max_length]

        return {
            "statusCode": 200,
            "body": json.dumps({
                "url": url,
                "content": final_text,
                "truncated": is_truncated,
                "char_count": len(final_text)
            })
        }

    except Exception as e:
        return {
            "statusCode": 500,
            "body": json.dumps({
                "url": url,
                "error": f"Failed to scrape website: {str(e)}"
            })
        }


# ============================================================================
# ACTION 3: plan_path  (was: pathfinding.py)
# ============================================================================
DANGER_COST = 1000
DOOR_COST_LOCKED = 5000
LOCKED_DOOR_HP_DAMAGE = 5  # guide: crossing a locked c30/c31 door does -5 real damage

# Tile scores, matched exactly to the challenge guide for this round.
TILE_SCORES = {
    "c1": 400,   # Violent Violet
    "c2": 600,   # Blue Brain / Code Challenge
    "c3": 550,   # Memento / Memory Trial
    "c4": 800,   # Dark Prophet / Web Search
    "c5": 250,   # Bonehead / Simple Question
    "c7": 250,   # Coins
    "c17": 50,   # A Distraction
    "c30": 1000,  # Red Door
    "c31": 1000,  # Green Door
    "c40": 50,   # Red Key
    "c41": 50,   # Green Key
}
DEFAULT_CHALLENGE_SCORE = 400

# Which challenge-tile type is a KEY, and which color it unlocks; and
# which type is a DOOR, and which color it requires. Extend these two
# dicts if a future round adds more key/door colors (e.g. c42/c32 blue) -
# no other code needs to change, everything below is colour-generic.
KEY_COLOR_MAP = {"c40": "red", "c41": "green"}
DOOR_COLOR_MAP = {"c30": "red", "c31": "green"}

# Real, certain HP loss only comes from c8 spikes and locked doors (any
# color); challenge-tile HP cost is a tunable "risk buffer" that defaults
# to 0 based on observed telemetry (correct answers don't cost HP). Raise
# via the challenge_hp_cost body param if your game DOES sometimes
# penalize correct answers.
CHALLENGE_HP_COST = 0

DEFAULT_STEP_COST = 3

# Tile types that must ALWAYS be visited when safely reachable, regardless
# of whether the profit-maximizing selection thinks it's "worth" the
# detour. Doors/keys are deliberately NOT forced — the score-vs-cost
# optimizer decides whether they're worth taking.
FORCE_COLLECT_TYPES = {"c1", "c2", "c3", "c4", "c5"}

# Max nodes (forced+optional combined) for the EXACT Held-Karp solve.
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
    map can never make the planner route back to a tile that has nothing
    left to give.

    Returns key_positions / door_positions as {color: set(pos)} dicts so
    ANY number of key/door colors defined in KEY_COLOR_MAP/DOOR_COLOR_MAP
    is handled uniformly (currently red + green).
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
    CHALLENGE_HP_COST, which defaults to 0."""
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
    are given a huge score bonus so the optimizer always selects them
    (mandatory quest tiles), while still finding the globally optimal
    VISIT ORDER for the combined set (forced + optional).
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
    must_mask = 0
    for i in must_include:
        must_mask |= (1 << i)
    for mask in range(1, 1 << n):
        if (mask & must_mask) != must_mask:
            continue
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
    challenge types, AND keys/doors). The Held-Karp/2-opt search decides
    whether the reward is worth the detour, exactly like any other
    optional tile.
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
            continue
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
    must_include = set(range(len(forced_targets)))

    if len(target_indices) <= EXACT_SOLVE_LIMIT:
        order = _held_karp_order(dist_matrix, target_indices, treasure_idx, scores, step_cost, must_include)
        if order is None:
            if forced_targets and len(must_include) > 1:
                reduced_must = set(list(must_include)[:-1])
                order = _held_karp_order(dist_matrix, target_indices, treasure_idx, scores, step_cost, reduced_must)
            if order is None:
                order = list(target_indices)
        route = [0] + order + [treasure_idx]
    else:
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

    if not _route_hp_feasible(route, waypoints, simulated_hp, rows, cols, barriers, dangers, doors_all, door_color, held_keys, challenges):
        route = _two_opt(list(route), dist_matrix)
        while len(route) > 2 and not _route_hp_feasible(route, waypoints, simulated_hp, rows, cols, barriers, dangers, doors_all, door_color, held_keys, challenges):
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
    (but whose door IS present) is also assumed already-held.
    """
    (grid, rows, cols, barriers, dangers, coins, challenges,
     key_positions, door_positions, door_color, treasure) = _build_grid(game_map, visited=visited)

    if treasure is None:
        treasure = (rows - 1, cols - 1)

    doors_all = set(door_color.keys())
    base_held = set(held_keys)
    for color, positions in door_positions.items():
        if positions and not key_positions.get(color):
            base_held.add(color)

    undecided_colors = [c for c in key_positions if key_positions[c] and c not in base_held]

    branches = []
    n = len(undecided_colors)
    for mask in range(1 << n):
        chosen = [undecided_colors[i] for i in range(n) if mask & (1 << i)]
        held_now = frozenset(base_held | set(chosen))

        cur_pos = start
        hp_budget = hp_remaining
        prefix_path = []
        prefix_cost = 0
        feasible = True

        if chosen:
            key_targets = []
            for color in chosen:
                candidates = list(key_positions[color])
                candidates = [p for p in candidates if _is_reachable(cur_pos, p, rows, cols, barriers)]
                if not candidates:
                    feasible = False
                    break
                key_targets.append((color, candidates))
            if not feasible:
                continue

            best_perm_cost, best_perm_path, best_perm_pos = None, None, None
            best_perm_hp = None
            for perm in permutations(range(len(key_targets))):
                p_cost, p_path, p_pos, p_hp = 0, [], cur_pos, hp_budget
                ok = True
                held_acc = set(held_now) - set(chosen)
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


def _handle_plan_path(body):
    try:
        game_map = body.get("map", body.get("game_map", body.get("grid", [])))
        if game_map:
            max_cols = max(len(row) for row in game_map)
            game_map = [row + ["normal"] * (max_cols - len(row)) for row in game_map]
        start_raw = body.get("start", body.get("start_pos", body.get("position",
                    body.get("current_position", body.get("agent_position", "A1")))))
        start = _parse_start(start_raw)
        hp_raw = body.get("hp", body.get("current_hp", body.get("health", body.get("life_points", 5))))
        hp = _coerce_number(hp_raw, 5, "hp")
        step_cost_raw = body.get("step_cost", body.get("time_penalty", DEFAULT_STEP_COST))
        step_cost = _coerce_number(step_cost_raw, DEFAULT_STEP_COST, "step_cost")
        _time_remaining = body.get("time_remaining")  # accepted, informational only for now
        visited = body.get("visited", body.get("collected", None))
        held_keys = frozenset(c.lower() for c in body.get("held_keys", body.get("keys_held", [])) or [])
        directions = plan_path(start, game_map, hp_remaining=hp, step_cost=step_cost, visited=visited, held_keys=held_keys)
        if not directions:
            directions = ["down", "right"]
        # IMPORTANT: only ever return ONE field for the move list, named
        # "directions". Do not re-add an "action"/"first_step" single-value
        # field here — that previously caused the agent to forward only
        # the first move per turn instead of the whole route.
        return {"statusCode": 200, "body": json.dumps({"directions": directions})}
    except Exception as e:
        return {"statusCode": 200, "body": json.dumps({"directions": ["down", "right", "down", "right"], "message": f"Fallback mode: {str(e)}"})}


# ============================================================================
# UNIFIED DISPATCH
# ============================================================================
VALID_ACTIONS = {"execute_code", "scrape_website", "plan_path"}


def _detect_action(body):
    """Backward-compatible auto-detection when 'action' is omitted, based
    on which fields are present (matches what each of the three original
    standalone Lambdas used to receive on their own dedicated endpoint)."""
    if "url" in body:
        return "scrape_website"
    if "code" in body:
        return "execute_code"
    if any(k in body for k in ("map", "game_map", "grid", "start", "start_pos")):
        return "plan_path"
    return None


def lambda_handler(event, context):
    """
    Single entry point for all three merged specialist actions.

    Body params (send exactly one action's params, plus "action"):
      action: "execute_code" | "scrape_website" | "plan_path"  (recommended)
      ... plus that action's own params (see each _handle_* docstring above)

    If "action" is omitted, it is auto-detected from which fields are
    present, for backward compatibility with old single-purpose callers.
    """
    try:
        body = json.loads(event["body"]) if "body" in event and isinstance(event["body"], str) else event.get("body", event)
        if not isinstance(body, dict):
            body = {}

        action = str(body.get("action", "")).strip().lower() or _detect_action(body)

        if action == "execute_code":
            return _handle_execute_code(body)
        if action == "scrape_website":
            return _handle_scrape_website(body)
        if action == "plan_path":
            return _handle_plan_path(body)

        return {
            "statusCode": 400,
            "body": json.dumps({
                "error": f"Missing or unrecognized 'action'. Expected one of {sorted(VALID_ACTIONS)}, "
                         f"and could not auto-detect from the given fields."
            })
        }
    except Exception as e:
        return {
            "statusCode": 200,
            "body": json.dumps({"error": f"Fallback mode: {e}"})
        }


# ============================================================================
# SELF-CHECKS
# ============================================================================
if __name__ == "__main__":
    # ---- execute_code self-checks (from CodeExecution) ----
    r = execute_code("print(2 + 2)")
    assert r["stdout"].strip() == "4", r
    assert r["error"] is None

    r = execute_code("result = 4 ** 10")
    assert r["result"] == 1048576, r

    r = execute_code("2 + 2")
    assert r["result"] == 4, r

    r = execute_code("""
def fib(n):
    a, b = 0, 1
    for _ in range(n):
        a, b = b, a + b
    return a
result = fib(3000)
""")
    assert len(str(r["result"])) == 627, r
    assert str(r["result"])[-10:] == "6709796000", r

    r = execute_code("import os\nos.system('echo hacked')")
    assert r["error"] is not None and "os" in r["error"], r

    r = execute_code("open('/etc/passwd').read()")
    assert r["error"] is not None, r

    r = execute_code("eval('1+1')")
    assert r["error"] is not None, r

    r = execute_code("while True:\n    pass", timeout_seconds=2)
    assert r["error"] is not None and "exceeded" in r["error"], r

    r = execute_code("import math\nresult = math.factorial(10)")
    assert r["result"] == 3628800, r

    r = execute_code("import string\nresult = string.ascii_uppercase")
    assert r["result"] == "ABCDEFGHIJKLMNOPQRSTUVWXYZ", r

    r = execute_code("code = 'open'\nresult = code[::-1]")
    assert r["result"] == "nepo", r

    r = execute_code("""
code = 'open'
result = "-".join(str(ord(ch.upper()) - ord('A') + 1) for ch in code if ch.isalpha())
""")
    assert r["result"] == "15-16-5-14", r

    print("OK: execute_code self-checks passed (11)")

    # ---- scrape_website self-check (offline parser-only, no network) ----
    sample_html = """
    <html><head><style>.x{color:red}</style><script>alert(1)</script></head>
    <body><nav>Home | About</nav>
    <h1>Real Title</h1><p>Real paragraph content.</p>
    <footer>copyright 2026</footer></body></html>
    """
    parser = CleanHTMLParser()
    parser.feed(sample_html)
    extracted = parser.get_text()
    assert "Real Title" in extracted and "Real paragraph content." in extracted, extracted
    assert "alert(1)" not in extracted, extracted
    assert "Home | About" not in extracted, extracted
    assert "copyright 2026" not in extracted, extracted

    resp_missing_url = _handle_scrape_website({})
    assert resp_missing_url["statusCode"] == 400, resp_missing_url

    print("OK: scrape_website self-checks passed (2, offline only)")

    # ---- plan_path self-checks (from pathfinding.py) ----
    mv = {"up": (-1, 0), "down": (1, 0), "left": (0, -1), "right": (0, 1)}

    def _walk(start, directions):
        r, c = start
        visited = []
        for d in directions:
            dr, dc = mv[d]
            r, c = r + dr, c + dc
            visited.append((r, c))
        return visited

    forced_map = [
        ["start", "normal", "normal"],
        ["normal", "c1", "normal"],
        ["normal", "normal", "treasure"],
    ]
    d1 = plan_path((0, 0), forced_map, hp_remaining=10, step_cost=8)
    v1 = _walk((0, 0), d1)
    assert (1, 1) in v1, "c1 must be force-collected as a cheap detour"

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

    revisit_map = [
        ["start", "normal", "c7"],
        ["normal", "normal", "normal"],
        ["normal", "normal", "treasure"],
    ]
    d4 = plan_path((0, 0), revisit_map, hp_remaining=10, step_cost=1, visited=["C1"])
    v4 = _walk((0, 0), d4)
    assert (0, 2) not in v4, "a tile already marked visited must be excluded even if map still shows it as a coin"

    tight_hp_map = [
        ["start", "c1", "c2", "c3"],
        ["normal", "normal", "normal", "treasure"],
    ]
    d5 = plan_path((0, 0), tight_hp_map, hp_remaining=1, step_cost=3)
    v5 = _walk((0, 0), d5)
    assert (0, 1) in v5 and (0, 2) in v5 and (0, 3) in v5, "forced non-hazard challenge tiles must not be dropped under a tight but non-hazardous HP budget"

    green_map = [
        ["start", "normal", "normal", "normal"],
        ["c41", "normal", "normal", "normal"],
        ["normal", "normal", "normal", "normal"],
        ["normal", "normal", "normal", "c31"],
        ["normal", "normal", "normal", "treasure"],
    ]
    d6 = plan_path((0, 0), green_map, hp_remaining=10, step_cost=1)
    v6 = _walk((0, 0), d6)
    assert (1, 0) in v6, "green key (c41) must be collected"
    assert (3, 3) in v6, "green door (c31) must be visited/opened"
    key_step = v6.index((1, 0))
    door_step = v6.index((3, 3))
    assert key_step < door_step, "green key must be collected BEFORE reaching the green door"

    assert TILE_SCORES["c17"] == 50
    assert TILE_SCORES["c40"] == 50
    assert TILE_SCORES["c41"] == 50
    assert TILE_SCORES["c30"] == 1000
    assert TILE_SCORES["c31"] == 1000
    assert "c18" not in TILE_SCORES
    assert "c18" not in FORCE_COLLECT_TYPES

    event_a = {"action": "plan_path", "map": [
        ["start", "normal", "normal", "normal"],
        ["c40", "normal", "normal", "normal"],
        ["normal", "normal", "normal", "normal"],
        ["normal", "normal", "normal", "c30"],
        ["normal", "normal", "normal", "treasure"],
    ], "start_pos": "B1", "hp": 10}
    resp_a = lambda_handler(event_a, None)
    body_a = json.loads(resp_a["body"])
    assert resp_a["statusCode"] == 200 and body_a["directions"], "start_pos must be accepted and produce a real plan"

    event_b = {"action": "plan_path", "map": [
        ["start", "normal", "normal", "normal"],
        ["c40", "normal", "normal", "normal"],
        ["normal", "normal", "normal", "normal"],
        ["normal", "normal", "normal", "c30"],
        ["normal", "normal", "normal", "treasure"],
    ], "start": "A1", "hp": 10, "step_cost": "4:51"}
    resp_b = lambda_handler(event_b, None)
    body_b = json.loads(resp_b["body"])
    assert resp_b["statusCode"] == 200 and body_b["directions"] != ["down", "right", "down", "right"], \
        "a bad non-numeric step_cost must not crash into the generic hardcoded fallback"

    print("OK: plan_path self-checks passed (9)")

    # ---- unified dispatch self-checks (NEW: verifies the merge itself) ----
    # explicit action="execute_code" through the single lambda_handler
    resp_c = lambda_handler({"action": "execute_code", "code": "result = 6 * 7"}, None)
    body_c = json.loads(resp_c["body"])
    assert body_c["result"] == 42, body_c

    # auto-detected action (no "action" field, has "code" -> execute_code)
    resp_d = lambda_handler({"code": "result = 1 + 1"}, None)
    body_d = json.loads(resp_d["body"])
    assert body_d["result"] == 2, body_d

    # auto-detected action (no "action" field, has "url" -> scrape_website)
    resp_e = lambda_handler({}, None)  # no recognizable fields at all
    assert resp_e["statusCode"] == 400, resp_e

    # explicit action="plan_path" through the single lambda_handler,
    # reusing event_a's body but forcing dispatch through auto-detect too
    resp_f = lambda_handler({"map": event_a["map"], "start_pos": "B1", "hp": 10}, None)
    body_f = json.loads(resp_f["body"])
    assert resp_f["statusCode"] == 200 and body_f["directions"], "auto-detected plan_path must still work without 'action'"

    print("OK: unified dispatch self-checks passed (4)")
    print("ALL SELF-CHECKS PASSED (26 total)")
