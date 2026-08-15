"""
Unified Specialist Lambda
==========================
Combines THREE previously-separate sub-agent tools into ONE Lambda /
ONE tool, dispatched by an "action" (execute_code / scrape_website /
plan_path):

  action="execute_code"    -> restricted, sandboxed Python code execution
                               (was: CodeExecution)
  action="scrape_website"  -> fetch + clean-text extraction of a URL
                               (was: dark_prophet_scraper.py)
  action="plan_path"       -> dungeon-map pathfinding / route optimizer
                               (was: pathfinding.py)

Each action's INTERNAL LOGIC is preserved EXACTLY as the original
standalone Lambda produced it - only the transport/dispatch layer
(lambda_handler) is unified and, critically, FIXED to speak the actual
wire format Amazon Bedrock Agents uses when it invokes an action-group
Lambda as a tool.

--------------------------------------------------------------------
WHY THIS FIX WAS NEEDED (root cause of "technical issue with the
pathfinding specialist" / repeated retries / lost game):
--------------------------------------------------------------------
A Bedrock Agent action group does NOT send/expect a plain API-Gateway
style event (`{"body": "...json..."}` in / `{"statusCode":200,"body":
"..."}` out). It sends ONE of two real shapes, and requires a response
in the MATCHING shape - see AWS docs "Configure Lambda functions for
action groups in Amazon Bedrock Agents":
https://docs.aws.amazon.com/bedrock/latest/userguide/agents-lambda.html

1. Function-details schema (what a tool named "...___unified_specialist"
   in the combat log indicates is in use here):
     IN:  {"messageVersion": "1.0", "actionGroup": "...",
           "function": "execute_code",
           "parameters": [{"name": "code", "type": "string", "value": "..."}],
           "sessionAttributes": {...}, "promptSessionAttributes": {...}}
     OUT: {"messageVersion": "1.0",
           "response": {"actionGroup": "...", "function": "execute_code",
                         "functionResponse": {"responseBody": {"TEXT": {"body": "<json>"}}}},
           "sessionAttributes": {...}, "promptSessionAttributes": {...}}

2. OpenAPI schema:
     IN:  {"messageVersion": "1.0", "actionGroup": "...",
           "apiPath": "/execute_code", "httpMethod": "POST",
           "requestBody": {"content": {"application/json": {"properties": [...]}}}}
     OUT: {"messageVersion": "1.0",
           "response": {"actionGroup": "...", "apiPath": "...", "httpMethod": "...",
                         "httpStatusCode": 200,
                         "responseBody": {"application/json": {"body": "<json>"}}}}

The previous version of this Lambda (and the original 3 separate
Lambdas before it) only spoke a THIRD, unrelated shape
(`{"statusCode":200,"body":"..."}`), which Bedrock's agent runtime does
not recognize as a valid tool result. Every call looked like a failure
to the orchestrator -> it retried -> still got a shape it couldn't
parse -> gave up and fell back to the model guessing a "manual
solution" in plain text, which is exactly what produced the lost game
in the combat log (0 coins collected, wrong path, game over).

This file auto-detects which of the 3 shapes the incoming `event` is in
(function-schema / OpenAPI-schema / plain-body, e.g. for local testing
or a raw Lambda-console test event) and replies in that SAME shape, so
it works correctly no matter which schema type the action group was
configured with - without needing any AWS console/IaC change for this
fix beyond pointing the Lambda handler at this file.

Deploying this file:
- Point ONE Lambda function's handler at unified_specialist.lambda_handler.
- Register ONE action group ("unified_specialist") with THREE functions
  (or 3 apiPaths) named exactly: execute_code, scrape_website, plan_path
  - in place of the three old separate specialists.
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
                                      # cipher/alphabet challenges (c32/c33 doors)
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


def _run_execute_code(body):
    """
    Body params:
      code: string — Python code to execute (required)
      timeout_seconds: int — optional override, default 10, capped at 25
                        (stay under typical Lambda timeout with margin)

    Returns (result_dict, http_status_code).

    Challenge types (from the game guide) meant to be delegated here:

    - c2  Blue Brain (code challenge): any exact large-number computation,
      e.g. "Tell me the 3000th Fibonacci number, last 10 digits":
          def fib(n):
              a, b = 0, 1
              for _ in range(n):
                  a, b = b, a + b
              return a
          result = str(fib(3000))[-10:]

    - c32 Grey Door: cipher is "combine the first two characters and the
      last two characters of the key".
          result = given_code[:2] + given_code[-2:]

    - c33 Yellow Door: cipher is "give the 5th and 7th character of the
      key" (1-indexed, per the door's own wording).
          result = given_code[4] + given_code[6]
      (These transforms are stated by each door's own challenge text and
      can change between rounds - always follow what the door itself
      says, do not assume these examples are the current rule.)
    """
    try:
        code = body.get("code")
        if not code or not isinstance(code, str):
            return {"stdout": "", "result": None, "error": "Missing required 'code' string parameter."}, 200

        timeout_raw = body.get("timeout_seconds", CODE_EXEC_TIMEOUT_SECONDS)
        try:
            timeout_seconds = min(int(timeout_raw), 25)
        except (TypeError, ValueError):
            timeout_seconds = CODE_EXEC_TIMEOUT_SECONDS

        result = execute_code(code, timeout_seconds=timeout_seconds)
        return result, 200
    except Exception as e:
        return {"stdout": "", "result": None, "error": f"Fallback mode: {e}"}, 200


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


def _run_scrape_website(body):
    """
    Body params:
      url: string — required
      max_length: int — optional, default 4000

    Returns (result_dict, http_status_code).
    """
    url = body.get("url")
    max_length = body.get("max_length", 4000)
    try:
        max_length = int(max_length)
    except (TypeError, ValueError):
        max_length = 4000

    if not url:
        return {"error": "Missing required parameter 'url'."}, 400

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
            "url": url,
            "content": final_text,
            "truncated": is_truncated,
            "char_count": len(final_text),
        }, 200

    except Exception as e:
        return {"url": url, "error": f"Failed to scrape website: {str(e)}"}, 500


# ============================================================================
# ACTION 3: plan_path  (was: pathfinding.py)
# ============================================================================
DANGER_COST = 1000
DOOR_COST_LOCKED = 5000
LOCKED_DOOR_HP_DAMAGE = 5  # guide: crossing a locked c32/c33 door does -5 real damage

# Tile scores, matched exactly to the challenge guide for this round.
# NOTE (map update): this round's guide swapped Red/Green key+door for
# Grey (c42 key / c32 door) and Yellow (c43 key / c33 door), and dropped
# "c3 Memento" entirely (not present in this round's guide). "c18" is
# back this round as Healthcare API (+500).
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
# optimizer decides whether they're worth taking. c18 (Healthcare API) is
# a real quest challenge this round, so it's forced too; c3 (Memento) is
# not in this round's guide at all, so it has been removed.
FORCE_COLLECT_TYPES = {"c1", "c2", "c4", "c5", "c18"}

# Max nodes (forced+optional combined) for the EXACT Held-Karp solve.
EXACT_SOLVE_LIMIT = 15


def _tile_score(cell_lower):
    if cell_lower in TILE_SCORES:
        return TILE_SCORES[cell_lower]
    if re.match(r"^c\d+$", cell_lower):
        return DEFAULT_CHALLENGE_SCORE
    return 0


# Labels a game map may use for an IMPASSABLE tile. Only the exact string
# "wall" used to be recognized here - any other spelling silently became a
# walkable "normal" tile, so a planned route could run straight THROUGH a
# wall. The game then refuses that move, and every direction after it is
# applied from the wrong tile: the classic "pressed start and immediately
# walked into a wall" symptom, where the whole turn's route is desynced.
WALL_LABELS = {"wall", "walls", "barrier", "barriers", "brick", "bricks",
               "block", "blocked", "rock", "stone", "obstacle", "impassable",
               "solid", "#", "x"}

# Labels marking the agent's own tile on the map. Used ONLY as a fallback
# when the caller-supplied start position turns out to be unusable.
START_LABELS = {"start", "agent", "player", "hero", "spawn"}

# Dict-key spellings the game has used for a structured position.
_START_ROW_KEYS = ("row", "rows", "r", "y", "line")
_START_COL_KEYS = ("col", "cols", "column", "c", "x")
_START_NESTED_KEYS = ("position", "pos", "start", "start_pos", "cell", "coord",
                      "coords", "coordinates", "location", "current_position",
                      "agent_position")


def _parse_start(pos):
    """
    Parse a position from every shape the game has actually sent.

    A dict position used to fall through to a regex over str(dict), which
    produced silently WRONG coordinates instead of failing loudly - e.g.
    {"x": 1, "y": 5} stringifies to "x1y5", the regex read "x1" as
    "column X, row 1" and returned column 23 on a 10-column map. Planning
    from a bogus origin desyncs every move that follows, so dict input is
    now destructured by key name instead of by accident.
    """
    try:
        if isinstance(pos, dict):
            # a nested {"position": "A5"} style wrapper
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

    The game has sent this field in several shapes and indexings over time
    ("A5", [4, 0], {"row": 5, "col": 1}, ...), so a parsed position that
    lands outside the map or on top of a wall is definitely wrong - and
    planning from a wrong origin makes the agent walk into a wall on its
    very first step, desyncing the entire turn. When that happens, prefer
    (in order): the map's own start/agent tile, the common off-by-one
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
            if (r, c) in visited_set and cell_lower not in WALL_LABELS and cell_lower != "treasure":
                cell_lower = "normal"  # already collected — no value left here
            grid[(r, c)] = cell_lower
            if cell_lower in WALL_LABELS:
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

    # Calibrate the start against the real grid BEFORE planning anything -
    # a start inside a wall/off the map makes every subsequent move wrong.
    start = _resolve_start(start, rows, cols, barriers, grid)

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


def _run_plan_path(body):
    """Returns (result_dict, http_status_code)."""
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
        held_keys_raw = body.get("held_keys", body.get("keys_held", [])) or []
        held_keys = frozenset(c.lower() for c in held_keys_raw)
        directions = plan_path(start, game_map, hp_remaining=hp, step_cost=step_cost, visited=visited, held_keys=held_keys)
        if not directions:
            directions = ["down", "right"]
        # IMPORTANT: only ever return ONE field for the move list, named
        # "directions". Do not re-add an "action"/"first_step" single-value
        # field here — that previously caused the agent to forward only
        # the first move per turn instead of the whole route.
        return {"directions": directions}, 200
    except Exception as e:
        return {"directions": ["down", "right", "down", "right"], "message": f"Fallback mode: {str(e)}"}, 200


# ============================================================================
# UNIFIED DISPATCH  —  Bedrock-Agent-compatible transport layer
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


def _detect_schema_type(event):
    """Identify which of the 3 known event shapes this is."""
    if not isinstance(event, dict):
        return "plain"
    if "function" in event and "parameters" in event:
        return "function"
    if "apiPath" in event and "httpMethod" in event:
        return "openapi"
    return "plain"


def _coerce_param_value(value, ptype=None):
    """Bedrock Agent parameter values arrive as strings even for
    non-string types (and complex types like arrays/objects arrive as a
    JSON-encoded string). Decode JSON-looking strings and cast scalars
    according to the declared type, without ever raising - fall back to
    the raw value on any parse failure so a single odd parameter can't
    crash the whole request."""
    if not isinstance(value, str):
        return value
    s = value.strip()
    if s.startswith("[") or s.startswith("{"):
        try:
            return json.loads(s)
        except (ValueError, TypeError):
            pass
    if ptype in ("integer", "number"):
        try:
            return int(s) if ptype == "integer" else float(s)
        except (TypeError, ValueError):
            return value
    if ptype == "boolean":
        return s.lower() in ("true", "1", "yes")
    return value


def _extract_request(event):
    """
    Returns (action, body, schema_type).

    Handles all 3 real shapes an action-group Lambda can receive from
    Amazon Bedrock Agents, plus a "plain" shape for local/manual testing:
      - "function" : Function-details action group (parameters is a list
                      of {"name","type","value"} dicts; action == the
                      function name)
      - "openapi"  : OpenAPI-schema action group (params come from
                      requestBody.content['application/json'].properties;
                      action == the last path segment of apiPath)
      - "plain"    : a raw dict already shaped like the tool's own body
                      (or a legacy API-Gateway-style {"body": "..."}),
                      used for local self-checks and manual invocation
    """
    schema_type = _detect_schema_type(event)

    if schema_type == "function":
        action = event.get("function")
        body = {}
        for p in (event.get("parameters") or []):
            name = p.get("name")
            if name is None:
                continue
            body[name] = _coerce_param_value(p.get("value"), p.get("type"))
        return action, body, schema_type

    if schema_type == "openapi":
        api_path = event.get("apiPath", "") or ""
        action = api_path.strip("/").split("/")[-1] if api_path else None
        body = {}
        req_body = event.get("requestBody", {}) or {}
        content = req_body.get("content", {}) or {}
        app_json = content.get("application/json", {}) or {}
        props = app_json.get("properties", [])
        if isinstance(props, list):
            for p in props:
                name = p.get("name")
                if name is None:
                    continue
                body[name] = _coerce_param_value(p.get("value"), p.get("type"))
        elif isinstance(props, dict):
            body = dict(props)
        return action, body, schema_type

    # "plain": legacy API-Gateway-style test event, or a raw body dict
    if isinstance(event, dict) and "body" in event and isinstance(event["body"], str):
        try:
            body = json.loads(event["body"])
        except (ValueError, TypeError):
            body = {}
    elif isinstance(event, dict):
        inner = event.get("body", event)
        body = inner if isinstance(inner, dict) else event
    else:
        body = {}
    if not isinstance(body, dict):
        body = {}
    action = str(body.get("action", "")).strip().lower() or _detect_action(body)
    return action, body, schema_type


def _wrap_response(event, schema_type, action, result_dict, status_code=200):
    """Build the response in the SAME shape the request came in, per the
    AWS Bedrock Agent Lambda contract (see module docstring)."""
    body_json = json.dumps(result_dict)
    session_attributes = event.get("sessionAttributes", {}) if isinstance(event, dict) else {}
    prompt_session_attributes = event.get("promptSessionAttributes", {}) if isinstance(event, dict) else {}

    if schema_type == "function":
        return {
            "messageVersion": "1.0",
            "response": {
                "actionGroup": event.get("actionGroup", "unified_specialist"),
                "function": event.get("function", action),
                "functionResponse": {
                    "responseBody": {"TEXT": {"body": body_json}}
                },
            },
            "sessionAttributes": session_attributes,
            "promptSessionAttributes": prompt_session_attributes,
        }

    if schema_type == "openapi":
        return {
            "messageVersion": "1.0",
            "response": {
                "actionGroup": event.get("actionGroup", "unified_specialist"),
                "apiPath": event.get("apiPath", f"/{action}" if action else "/unknown"),
                "httpMethod": event.get("httpMethod", "POST"),
                "httpStatusCode": status_code,
                "responseBody": {"application/json": {"body": body_json}},
            },
            "sessionAttributes": session_attributes,
            "promptSessionAttributes": prompt_session_attributes,
        }

    # "plain": keep the original API-Gateway-style shape for backward
    # compatibility with local testing / manual invocation.
    return {"statusCode": status_code, "body": body_json}


def lambda_handler(event, context):
    """
    Single entry point for all three merged specialist actions
    (execute_code / scrape_website / plan_path), speaking whichever of
    the 3 known Bedrock-Agent-compatible wire shapes the request used
    (see module docstring for the full shape reference).
    """
    try:
        if not isinstance(event, dict):
            event = {}

        action, body, schema_type = _extract_request(event)
        action_norm = str(action or "").strip().lower()

        if action_norm == "execute_code":
            result, status = _run_execute_code(body)
        elif action_norm == "scrape_website":
            result, status = _run_scrape_website(body)
        elif action_norm == "plan_path":
            result, status = _run_plan_path(body)
        else:
            result, status = (
                {"error": f"Missing or unrecognized action '{action}'. "
                          f"Expected one of {sorted(VALID_ACTIONS)}, and could not "
                          f"auto-detect from the given fields."},
                400,
            )

        return _wrap_response(event, schema_type, action_norm, result, status)

    except Exception as e:
        # Never let an unhandled exception produce a malformed/empty
        # Lambda response - that is indistinguishable from a crash to the
        # Bedrock orchestrator and triggers the same silent-retry loop
        # that caused the original "technical issue" failure. Always
        # reply in a shape the caller can parse, with the error inside
        # the payload instead.
        try:
            schema_type = _detect_schema_type(event) if isinstance(event, dict) else "plain"
            action_norm = str(event.get("function") or "") if isinstance(event, dict) else ""
            return _wrap_response(event if isinstance(event, dict) else {}, schema_type, action_norm,
                                   {"error": f"Fallback mode: {e}"}, 200)
        except Exception:
            return {"statusCode": 200, "body": json.dumps({"error": f"Fallback mode: {e}"})}


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

    r_missing_url, status_missing_url = _run_scrape_website({})
    assert status_missing_url == 400, (status_missing_url, r_missing_url)

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

    assert TILE_SCORES["c17"] == 50
    assert TILE_SCORES["c42"] == 50
    assert TILE_SCORES["c43"] == 50
    assert TILE_SCORES["c32"] == 1000
    assert TILE_SCORES["c33"] == 1000
    assert TILE_SCORES["c18"] == 500
    assert "c18" in FORCE_COLLECT_TYPES
    assert "c3" not in TILE_SCORES
    assert "c3" not in FORCE_COLLECT_TYPES

    sample_map = [
        ["start", "normal", "normal", "normal"],
        ["c42", "normal", "normal", "normal"],
        ["normal", "normal", "normal", "normal"],
        ["normal", "normal", "normal", "c32"],
        ["normal", "normal", "normal", "treasure"],
    ]

    r_a, status_a = _run_plan_path({"map": sample_map, "start_pos": "B1", "hp": 10})
    assert status_a == 200 and r_a["directions"], "start_pos must be accepted and produce a real plan"

    r_b, status_b = _run_plan_path({"map": sample_map, "start": "A1", "hp": 10, "step_cost": "4:51"})
    assert status_b == 200 and r_b["directions"] != ["down", "right", "down", "right"], \
        "a bad non-numeric step_cost must not crash into the generic hardcoded fallback"

    # A dict position must be destructured BY KEY, not by a regex over
    # str(dict). {"x": 1, "y": 5} used to stringify to "x1y5", get read as
    # "column X, row 1", and return column 23 on a 10-column map -
    # planning from that bogus origin desyncs every move after it.
    assert _parse_start({"row": 5, "col": "A"}) == (4, 0), _parse_start({"row": 5, "col": "A"})
    assert _parse_start({"position": "A5"}) == (4, 0), _parse_start({"position": "A5"})
    r_xy, c_xy = _parse_start({"x": 1, "y": 5})
    assert 0 <= c_xy < 10, f"dict position produced an out-of-range column: {(r_xy, c_xy)}"
    assert _parse_start("A5") == (4, 0), "the plain 'A5' form must keep working"

    # A start that lands inside a wall (wrong indexing from the caller)
    # must be calibrated back onto a real walkable tile, or the agent walks
    # into a wall on its very first step.
    walled_map = [
        ["wall", "wall", "wall"],
        ["wall", "start", "normal"],
        ["wall", "normal", "treasure"],
    ]
    g11 = _build_grid(walled_map)
    grid11, rows11, cols11, barriers11 = g11[0], g11[1], g11[2], g11[3]
    assert _resolve_start((0, 0), rows11, cols11, barriers11, grid11) == (1, 1), \
        "a start inside a wall must fall back to the map's own start tile"
    assert _resolve_start((99, 99), rows11, cols11, barriers11, grid11) == (1, 1), \
        "a start off the map must fall back to the map's own start tile"
    assert _resolve_start((1, 2), rows11, cols11, barriers11, grid11) == (1, 2), \
        "a start that IS valid must be trusted as-is (agent isn't on the start tile after turn 1)"

    # Walls labelled with anything other than the exact string "wall" must
    # still be impassable. Previously "brick" fell through to a walkable
    # "normal" tile, so the planner routed straight through it and the game
    # rejected the move mid-route.
    brick_map = [
        ["start", "brick", "normal"],
        ["normal", "brick", "normal"],
        ["normal", "normal", "treasure"],
    ]
    d12 = plan_path((0, 0), brick_map, hp_remaining=10, step_cost=1)
    v12 = _walk((0, 0), d12)
    assert (0, 1) not in v12 and (1, 1) not in v12, \
        f"route walked through a 'brick' wall: {v12}"
    assert v12[-1] == (2, 2), "must still reach the treasure around the wall"

    print("OK: plan_path self-checks passed (12)")

    # ---- unified dispatch self-checks: "plain" testing shape ----
    resp_c = lambda_handler({"action": "execute_code", "code": "result = 6 * 7"}, None)
    assert resp_c["statusCode"] == 200, resp_c
    body_c = json.loads(resp_c["body"])
    assert body_c["result"] == 42, body_c

    resp_d = lambda_handler({"code": "result = 1 + 1"}, None)  # auto-detected action
    body_d = json.loads(resp_d["body"])
    assert body_d["result"] == 2, body_d

    resp_e = lambda_handler({}, None)  # no recognizable fields at all
    assert resp_e["statusCode"] == 400, resp_e

    resp_f = lambda_handler({"map": sample_map, "start_pos": "B1", "hp": 10}, None)  # auto-detected plan_path
    body_f = json.loads(resp_f["body"])
    assert resp_f["statusCode"] == 200 and body_f["directions"], "auto-detected plan_path must still work without 'action'"

    print("OK: unified dispatch self-checks (plain shape) passed (4)")

    # ---- unified dispatch self-checks: REAL Bedrock "function" schema ----
    # This is the shape a tool named "...___unified_specialist" (as seen
    # in the combat log) actually receives - the exact case that was
    # broken before this fix.
    event_fn_code = {
        "messageVersion": "1.0",
        "actionGroup": "unified_specialist",
        "function": "execute_code",
        "parameters": [{"name": "code", "type": "string", "value": "result = 6 * 7"}],
        "sessionAttributes": {}, "promptSessionAttributes": {},
    }
    resp_fn = lambda_handler(event_fn_code, None)
    assert resp_fn["messageVersion"] == "1.0", resp_fn
    assert resp_fn["response"]["function"] == "execute_code", resp_fn
    fn_body = json.loads(resp_fn["response"]["functionResponse"]["responseBody"]["TEXT"]["body"])
    assert fn_body["result"] == 42, fn_body

    event_fn_path = {
        "messageVersion": "1.0",
        "actionGroup": "unified_specialist",
        "function": "plan_path",
        "parameters": [
            {"name": "map", "type": "array", "value": json.dumps(sample_map)},
            {"name": "start_pos", "type": "string", "value": "B1"},
            {"name": "hp", "type": "integer", "value": "10"},
        ],
        "sessionAttributes": {"turn": "1"}, "promptSessionAttributes": {},
    }
    resp_fn2 = lambda_handler(event_fn_path, None)
    assert resp_fn2["sessionAttributes"] == {"turn": "1"}, resp_fn2
    fn_body2 = json.loads(resp_fn2["response"]["functionResponse"]["responseBody"]["TEXT"]["body"])
    assert fn_body2["directions"], "function-schema plan_path must return a real directions list"

    event_fn_missing_url = {
        "messageVersion": "1.0",
        "actionGroup": "unified_specialist",
        "function": "scrape_website",
        "parameters": [],
    }
    resp_fn3 = lambda_handler(event_fn_missing_url, None)
    fn_body3 = json.loads(resp_fn3["response"]["functionResponse"]["responseBody"]["TEXT"]["body"])
    assert "error" in fn_body3, "function-schema must still surface a clear error, never crash/empty-reply"

    print("OK: unified dispatch self-checks (Bedrock function schema) passed (3)")

    # ---- unified dispatch self-checks: REAL Bedrock "OpenAPI" schema ----
    event_oa_code = {
        "messageVersion": "1.0",
        "actionGroup": "unified_specialist",
        "apiPath": "/execute_code",
        "httpMethod": "POST",
        "requestBody": {"content": {"application/json": {"properties": [
            {"name": "code", "type": "string", "value": "result = 100 - 58"},
        ]}}},
    }
    resp_oa = lambda_handler(event_oa_code, None)
    assert resp_oa["messageVersion"] == "1.0", resp_oa
    assert resp_oa["response"]["httpStatusCode"] == 200, resp_oa
    oa_body = json.loads(resp_oa["response"]["responseBody"]["application/json"]["body"])
    assert oa_body["result"] == 42, oa_body

    event_oa_path = {
        "messageVersion": "1.0",
        "actionGroup": "unified_specialist",
        "apiPath": "/plan_path",
        "httpMethod": "POST",
        "requestBody": {"content": {"application/json": {"properties": [
            {"name": "map", "type": "array", "value": json.dumps(sample_map)},
            {"name": "start_pos", "type": "string", "value": "B1"},
            {"name": "hp", "type": "integer", "value": "10"},
        ]}}},
    }
    resp_oa2 = lambda_handler(event_oa_path, None)
    oa_body2 = json.loads(resp_oa2["response"]["responseBody"]["application/json"]["body"])
    assert resp_oa2["response"]["httpStatusCode"] == 200 and oa_body2["directions"], \
        "OpenAPI-schema plan_path must return a real directions list"

    event_oa_missing_url = {
        "messageVersion": "1.0",
        "actionGroup": "unified_specialist",
        "apiPath": "/scrape_website",
        "httpMethod": "POST",
        "requestBody": {"content": {"application/json": {"properties": []}}},
    }
    resp_oa3 = lambda_handler(event_oa_missing_url, None)
    assert resp_oa3["response"]["httpStatusCode"] == 400, resp_oa3

    print("OK: unified dispatch self-checks (Bedrock OpenAPI schema) passed (3)")

    # ---- unrecognized/garbage event must never crash into a bare exception ----
    resp_garbage = lambda_handler({"totally": "unrelated", "fields": 123}, None)
    assert resp_garbage["statusCode"] == 400, resp_garbage

    print("OK: unrecognized-event safety check passed (1)")
    print("ALL SELF-CHECKS PASSED (37 total)")
