"""Complete-collection pathfinding tool for an AWS Lambda/SageMaker agent.

The planner collects every cNN tile except c8, treats c8 and walls as hard
barriers, enforces c42 -> c32 and c43 -> c33, and enters treasure only after
all required tiles have been visited.
"""

from __future__ import annotations

import json
import re
from collections import deque
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

Position = Tuple[int, int]

CHALLENGE_RE = re.compile(r"^c\d+$", re.IGNORECASE)
CHALLENGE_SEARCH_RE = re.compile(r"\bc\d+\b", re.IGNORECASE)
COORDINATE_RE = re.compile(r"^([A-Za-z]+)([1-9]\d*)$")
NUMBER_PAIR_RE = re.compile(r"^\s*(-?\d+)\s*[,;:]\s*(-?\d+)\s*$")

KEY_BY_TILE = {"c42": "grey", "c43": "yellow"}
DOOR_BY_TILE = {"c32": "grey", "c33": "yellow"}

WALL_TILES = {
    "#",
    "wall",
    "walls",
    "w",
    "obstacle",
    "blocked",
    "block",
    "rock",
}
TREASURE_TILES = {"treasure", "treasure_chest", "treasure chest", "chest", "goal"}
EMPTY_TILES = {"", ".", "empty", "floor", "path", "none", "null"}
PAD_WALL = "__padding_wall__"

# Deterministic tie-breaking for equally short routes.
MOVES: Tuple[Tuple[str, int, int], ...] = (
    ("up", -1, 0),
    ("right", 0, 1),
    ("down", 1, 0),
    ("left", 0, -1),
)


class PathfindingError(ValueError):
    """Raised when a safe complete route cannot be produced."""

    def __init__(self, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.status_code = status_code


def _cell_code(cell: Any) -> str:
    """Convert common map-cell representations to one normalized tile code."""
    if cell is None:
        return "empty"

    if isinstance(cell, Mapping):
        for key in (
            "type",
            "tile",
            "code",
            "challenge_id",
            "challenge",
            "value",
            "content",
            "id",
            "name",
        ):
            if key in cell and cell[key] is not None:
                return _cell_code(cell[key])
        return "empty"

    text = str(cell).strip().lower()
    challenge = CHALLENGE_SEARCH_RE.search(text)
    if challenge:
        return challenge.group(0).lower()
    if "treasure" in text or text == "chest":
        return "treasure"
    if "spike" in text:
        return "c8"
    return text


def _normalize_grid(raw_grid: Any) -> List[List[str]]:
    """Validate a 2-D grid and pad short rows with impassable walls."""
    if isinstance(raw_grid, str):
        try:
            raw_grid = json.loads(raw_grid)
        except json.JSONDecodeError as exc:
            raise PathfindingError("map/grid must be a two-dimensional JSON array", 400) from exc

    if not isinstance(raw_grid, Sequence) or isinstance(raw_grid, (str, bytes)) or not raw_grid:
        raise PathfindingError("map/grid must be a non-empty two-dimensional array", 400)

    rows: List[List[Any]] = []
    for row in raw_grid:
        if not isinstance(row, Sequence) or isinstance(row, (str, bytes)):
            raise PathfindingError("every map row must be an array", 400)
        rows.append(list(row))

    width = max((len(row) for row in rows), default=0)
    if width == 0:
        raise PathfindingError("map/grid rows cannot all be empty", 400)

    return [
        [_cell_code(cell) for cell in row] + [PAD_WALL] * (width - len(row))
        for row in rows
    ]


def _letters_to_column(letters: str) -> int:
    column = 0
    for letter in letters.upper():
        column = column * 26 + (ord(letter) - ord("A") + 1)
    return column - 1


def _parse_position(value: Any) -> Optional[Position]:
    """Parse {row,col}, [row,col], 'row,col', or spreadsheet-style 'A1'."""
    if isinstance(value, Mapping):
        row_value = next((value[k] for k in ("row", "r", "y") if k in value), None)
        col_value = next((value[k] for k in ("col", "column", "c", "x") if k in value), None)
        if row_value is not None and col_value is not None:
            try:
                return int(row_value), int(col_value)
            except (TypeError, ValueError):
                return None
        for key in ("position", "pos", "location", "coordinate", "coordinates"):
            if key in value:
                parsed = _parse_position(value[key])
                if parsed is not None:
                    return parsed
        return None

    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        if len(value) == 2:
            try:
                if isinstance(value[0], bool) or isinstance(value[1], bool):
                    return None
                return int(value[0]), int(value[1])
            except (TypeError, ValueError):
                return None
        return None

    if isinstance(value, str):
        text = value.strip()
        # Lowercase cNN values are challenge IDs, not spreadsheet coordinates.
        if CHALLENGE_RE.fullmatch(text) and text.startswith("c"):
            return None
        pair = NUMBER_PAIR_RE.fullmatch(text)
        if pair:
            return int(pair.group(1)), int(pair.group(2))
        coordinate = COORDINATE_RE.fullmatch(text)
        if coordinate:
            return int(coordinate.group(2)) - 1, _letters_to_column(coordinate.group(1))

    return None


def _normalize_collected(value: Any) -> Tuple[Set[Position], Set[str]]:
    """Extract explicit collected positions and cNN IDs from flexible input."""
    positions: Set[Position] = set()
    codes: Set[str] = set()

    def visit(item: Any) -> None:
        if item is None:
            return

        position = _parse_position(item)
        if position is not None:
            positions.add(position)
            if isinstance(item, Mapping):
                for key in ("type", "tile", "code", "challenge_id", "challenge", "id"):
                    if key in item:
                        code = _cell_code(item[key])
                        if CHALLENGE_RE.fullmatch(code):
                            codes.add(code)
            return

        if isinstance(item, str):
            text = item.strip()
            if not text:
                return
            try:
                decoded = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                decoded = None
            if decoded is not None and decoded != item:
                visit(decoded)
                return
            code = _cell_code(text)
            if CHALLENGE_RE.fullmatch(code):
                codes.add(code)
            return

        if isinstance(item, Mapping):
            for key, nested in item.items():
                key_position = _parse_position(key)
                if key_position is not None and nested not in (False, None, 0, ""):
                    positions.add(key_position)
                elif key in (
                    "positions",
                    "visited",
                    "collected",
                    "items",
                    "challenges",
                    "tiles",
                    "path",
                ):
                    visit(nested)
                elif key in ("type", "tile", "code", "challenge_id", "challenge", "id"):
                    code = _cell_code(nested)
                    if CHALLENGE_RE.fullmatch(code):
                        codes.add(code)
            return

        if isinstance(item, Iterable) and not isinstance(item, (bytes, bytearray)):
            for nested in item:
                visit(nested)

    visit(value)
    # c8 is never collectible and collected/visited data can never make it safe.
    codes.discard("c8")
    return positions, codes


def _normalize_held_keys(value: Any) -> Set[str]:
    held: Set[str] = set()

    def visit(item: Any) -> None:
        if item is None:
            return
        if isinstance(item, str):
            text = item.strip().lower()
            try:
                decoded = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                decoded = None
            if decoded is not None and decoded != item:
                visit(decoded)
                return
            if text in ("grey", "gray", "grey_key", "gray_key", "c42"):
                held.add("grey")
            elif text in ("yellow", "yellow_key", "c43"):
                held.add("yellow")
            return
        if isinstance(item, Mapping):
            for key, nested in item.items():
                normalized_key = str(key).strip().lower()
                if normalized_key in ("grey", "gray", "yellow", "c42", "c43") and nested not in (
                    False,
                    None,
                    0,
                    "",
                ):
                    visit(normalized_key)
                else:
                    visit(nested)
            return
        if isinstance(item, Iterable) and not isinstance(item, (bytes, bytearray)):
            for nested in item:
                visit(nested)

    visit(value)
    return held


def _pick(payload: Mapping[str, Any], aliases: Sequence[str], required: bool = False) -> Any:
    for alias in aliases:
        if alias in payload and payload[alias] is not None:
            return payload[alias]
    if required:
        raise PathfindingError(f"missing required input: one of {', '.join(aliases)}", 400)
    return None


def _unwrap_event(event: Any) -> Dict[str, Any]:
    """Accept direct Lambda payloads and API Gateway JSON bodies."""
    if not isinstance(event, Mapping):
        raise PathfindingError("Lambda event must be a JSON object", 400)

    payload: Dict[str, Any] = dict(event)
    body = payload.get("body")
    if body is not None:
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except json.JSONDecodeError as exc:
                raise PathfindingError("event.body must contain valid JSON", 400) from exc
        if isinstance(body, Mapping):
            payload.update(body)
        else:
            raise PathfindingError("event.body must be a JSON object", 400)

    parameters = payload.get("parameters")
    if isinstance(parameters, Sequence) and not isinstance(parameters, (str, bytes)):
        for parameter in parameters:
            if isinstance(parameter, Mapping) and "name" in parameter and "value" in parameter:
                payload.setdefault(str(parameter["name"]), parameter["value"])

    return payload


def _in_bounds(grid: Sequence[Sequence[str]], position: Position) -> bool:
    row, col = position
    return 0 <= row < len(grid) and 0 <= col < len(grid[0])


def _is_wall(tile: str) -> bool:
    return tile == PAD_WALL or tile in WALL_TILES


def _is_treasure(tile: str) -> bool:
    return tile in TREASURE_TILES or "treasure" in tile


def _is_walkable(
    grid: Sequence[Sequence[str]],
    position: Position,
    held_keys: Set[str],
    opened_doors: Set[Position],
    allow_treasure: bool,
) -> bool:
    if not _in_bounds(grid, position):
        return False

    tile = grid[position[0]][position[1]]
    if _is_wall(tile) or tile == "c8":
        return False
    if _is_treasure(tile) and not allow_treasure:
        return False

    required_key = DOOR_BY_TILE.get(tile)
    if required_key and position not in opened_doors and required_key not in held_keys:
        return False

    return True


def _bfs(
    grid: Sequence[Sequence[str]],
    start: Position,
    goals: Set[Position],
    held_keys: Set[str],
    opened_doors: Set[Position],
    allow_treasure: bool,
) -> Optional[List[Tuple[Position, str]]]:
    """Return a shortest path as (position, direction) steps to any goal."""
    if start in goals:
        return []

    queue = deque([start])
    previous: Dict[Position, Tuple[Position, str]] = {}
    seen = {start}
    found: Optional[Position] = None

    while queue:
        current = queue.popleft()
        for direction, row_delta, col_delta in MOVES:
            nxt = (current[0] + row_delta, current[1] + col_delta)
            if nxt in seen or not _is_walkable(
                grid, nxt, held_keys, opened_doors, allow_treasure
            ):
                continue
            seen.add(nxt)
            previous[nxt] = (current, direction)
            if nxt in goals:
                found = nxt
                queue.clear()
                break
            queue.append(nxt)
        if found is not None:
            break

    if found is None:
        return None

    reversed_path: List[Tuple[Position, str]] = []
    cursor = found
    while cursor != start:
        parent, direction = previous[cursor]
        reversed_path.append((cursor, direction))
        cursor = parent
    reversed_path.reverse()
    return reversed_path


def _consume_tile(
    grid: Sequence[Sequence[str]],
    position: Position,
    required: Set[Position],
    held_keys: Set[str],
    opened_doors: Set[Position],
) -> None:
    """Update collection/key/door state after physically occupying a cell."""
    tile = grid[position[0]][position[1]]

    key_color = KEY_BY_TILE.get(tile)
    if key_color:
        held_keys.add(key_color)

    door_color = DOOR_BY_TILE.get(tile)
    if door_color:
        if door_color not in held_keys and position not in opened_doors:
            raise PathfindingError(f"cannot enter {door_color} door before collecting its key")
        opened_doors.add(position)

    if CHALLENGE_RE.fullmatch(tile) and tile != "c8":
        required.discard(position)


def plan_path(payload: Mapping[str, Any]) -> List[str]:
    """Build a safe route that completes every required tile before treasure."""
    raw_grid = _pick(payload, ("map", "game_map", "grid"), required=True)
    raw_start = _pick(
        payload,
        ("start", "start_pos", "position", "current_position", "agent_position"),
        required=True,
    )
    grid = _normalize_grid(raw_grid)
    start = _parse_position(raw_start)
    if start is None:
        raise PathfindingError("start position must be {row,col}, [row,col], or A1", 400)
    if not _in_bounds(grid, start):
        raise PathfindingError("start position is outside the map", 400)

    collected_value = _pick(payload, ("visited", "collected"))
    collected_positions, collected_codes = _normalize_collected(collected_value)
    held_keys = _normalize_held_keys(_pick(payload, ("held_keys", "keys_held")))

    required: Set[Position] = set()
    treasures: Set[Position] = set()
    positions_by_code: Dict[str, Set[Position]] = {}

    for row_index, row in enumerate(grid):
        for col_index, tile in enumerate(row):
            position = (row_index, col_index)
            if _is_treasure(tile):
                treasures.add(position)
            if CHALLENGE_RE.fullmatch(tile):
                positions_by_code.setdefault(tile, set()).add(position)
                if tile != "c8":
                    required.add(position)

    if not treasures:
        raise PathfindingError("map contains no treasure", 400)

    opened_doors: Set[Position] = set()

    # Position-based history is unambiguous. A stale c8 entry is still ignored.
    for position in collected_positions:
        if _in_bounds(grid, position):
            tile = grid[position[0]][position[1]]
            if tile == "c8":
                continue
            if tile in KEY_BY_TILE:
                held_keys.add(KEY_BY_TILE[tile])
            if tile in DOOR_BY_TILE:
                opened_doors.add(position)
            required.discard(position)

    # Code-only history is useful for unique challenges/keys/doors. Never let a
    # single "c7" value erase multiple still-visible coin tiles.
    for code in collected_codes:
        if code in KEY_BY_TILE:
            held_keys.add(KEY_BY_TILE[code])
        if code == "c7":
            continue
        for position in positions_by_code.get(code, set()):
            required.discard(position)
            if code in DOOR_BY_TILE:
                opened_doors.add(position)

    start_tile = grid[start[0]][start[1]]
    if _is_wall(start_tile) or start_tile == "c8":
        raise PathfindingError("start position is on a wall or spike", 400)
    if _is_treasure(start_tile) and required:
        raise PathfindingError("start position is treasure before required challenges are complete")
    start_door_color = DOOR_BY_TILE.get(start_tile)
    if start_door_color and start not in opened_doors and start_door_color not in held_keys:
        raise PathfindingError(f"start position is a locked {start_door_color} door", 400)

    directions: List[str] = []
    current = start
    _consume_tile(grid, current, required, held_keys, opened_doors)

    while required:
        path = _bfs(
            grid=grid,
            start=current,
            goals=required,
            held_keys=held_keys,
            opened_doors=opened_doors,
            allow_treasure=False,
        )
        if path is None:
            unresolved = sorted(
                f"{grid[row][col]}@({row},{col})" for row, col in required
            )
            raise PathfindingError(
                "required tiles are unreachable without crossing a wall, spike, treasure, "
                "or unmatched locked door: " + ", ".join(unresolved)
            )

        for position, direction in path:
            if _is_treasure(grid[position[0]][position[1]]):
                raise PathfindingError("internal safety check: treasure entered too early")
            directions.append(direction)
            current = position
            _consume_tile(grid, current, required, held_keys, opened_doors)

    treasure_path = _bfs(
        grid=grid,
        start=current,
        goals=treasures,
        held_keys=held_keys,
        opened_doors=opened_doors,
        allow_treasure=True,
    )
    if treasure_path is None:
        raise PathfindingError("treasure is unreachable after completing all required tiles")

    for position, direction in treasure_path:
        directions.append(direction)
        current = position

    if current not in treasures:
        raise PathfindingError("internal safety check: route did not finish on treasure")
    return directions


def _response(status_code: int, body: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "statusCode": status_code,
        "body": json.dumps(body, separators=(",", ":")),
    }


def lambda_handler(event: Any, context: Any) -> Dict[str, Any]:
    """AWS Lambda entry point. Never emits guessed or arbitrary fallback moves."""
    del context
    try:
        payload = _unwrap_event(event)
        directions = plan_path(payload)
        return _response(200, {"directions": directions})
    except PathfindingError as exc:
        return _response(exc.status_code, {"directions": [], "error": str(exc)})
    except Exception as exc:  # Safe failure: report the issue but never invent movement.
        return _response(500, {"directions": [], "error": f"pathfinding failed: {exc}"})
