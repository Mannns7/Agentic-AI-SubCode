'use strict';

/**
 * PathfindingLambdaTarget for AWS Lambda Node.js 22.x.
 * Handler: pathfinding.lambda_handler. No npm dependencies are required.
 */

const assert = require('node:assert/strict');

const DANGER_COST = 1000;
const DOOR_COST_LOCKED = 5000;
const LOCKED_DOOR_HP_DAMAGE = 5;

const TILE_SCORES = new Map([
  ['c1', 400],
  ['c3', 550],
  ['c4', 800],
  ['c5', 250],
  ['c18', 500],
  ['c7', 250],
]);
const DEFAULT_CHALLENGE_SCORE = 400;
const DEFAULT_DOOR_SCORE = 1000;
const DEFAULT_KEY_SCORE = 50;
const CHALLENGE_HP_COST = 1;
const DEFAULT_STEP_COST = 3;
// ponytail: prefer one complete route of at most 48 moves over a truncated
// score route; upgrade to paged/stateful movement if the game supports chunks.
const MAX_ROUTE_DIRECTIONS = 48;
const FORCE_COLLECT_TYPES = new Set(['c4', 'c18']);

const DOOR_RE = /^c3(\d+)$/;
const KEY_RE = /^c4(\d+)$/;
const CHALLENGE_RE = /^c\d+$/;
const WALL_LABELS = new Set([
  'wall', 'walls', 'barrier', 'barriers', 'brick', 'bricks', 'block',
  'blocked', 'rock', 'stone', 'obstacle', 'impassable', 'solid', '#', 'x',
]);
const START_LABELS = new Set(['start', 'agent', 'player', 'hero', 'spawn']);
const START_ROW_KEYS = new Set(['row', 'rows', 'r', 'y', 'line']);
const START_COL_KEYS = new Set(['col', 'cols', 'column', 'c', 'x']);
const START_NESTED_KEYS = new Set([
  'position', 'pos', 'start', 'start_pos', 'cell', 'coord', 'coords',
  'coordinates', 'location', 'current_position', 'agent_position',
]);
const DIRECTIONS = [
  [1, 0, 'down'],
  [-1, 0, 'up'],
  [0, 1, 'right'],
  [0, -1, 'left'],
];

const keyOf = ([r, c]) => `${r},${c}`;
const samePos = (a, b) => a[0] === b[0] && a[1] === b[1];
const hasOwn = (object, key) => Object.prototype.hasOwnProperty.call(object, key);
const isRecord = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const firstPresent = (object, keys, fallback) => {
  for (const key of keys) {
    if (hasOwn(object, key)) return object[key];
  }
  return fallback;
};

function keyCodeForDoor(doorCode) {
  const match = DOOR_RE.exec(doorCode);
  return match ? `c4${match[1]}` : null;
}

function tileScore(cellLower, pos = null, doorMap = null, heldKeys = null) {
  if (pos !== null && doorMap !== null && doorMap.has(keyOf(pos))) {
    heldKeys = heldKeys || new Set();
    if (!heldKeys.has(keyCodeForDoor(doorMap.get(keyOf(pos))))) return 0;
  }
  if (TILE_SCORES.has(cellLower)) return TILE_SCORES.get(cellLower);
  if (DOOR_RE.test(cellLower)) return DEFAULT_DOOR_SCORE;
  if (KEY_RE.test(cellLower)) return DEFAULT_KEY_SCORE;
  if (CHALLENGE_RE.test(cellLower)) return DEFAULT_CHALLENGE_SCORE;
  return 0;
}

function cleanAlphaNumeric(value) {
  return String(value).replace(/[^A-Za-z0-9]/g, '');
}

function strictInteger(value) {
  if (!/^\d+$/.test(value)) throw new TypeError('invalid integer');
  return Number(value);
}

function parseStart(pos) {
  try {
    if (isRecord(pos)) {
      for (const [key, value] of Object.entries(pos)) {
        if (START_NESTED_KEYS.has(String(key).trim().toLowerCase())) {
          return parseStart(value);
        }
      }
      let rowValue = null;
      let colValue = null;
      for (const [key, value] of Object.entries(pos)) {
        const normalized = String(key).trim().toLowerCase();
        if (START_ROW_KEYS.has(normalized) && rowValue === null) rowValue = value;
        else if (START_COL_KEYS.has(normalized) && colValue === null) colValue = value;
      }
      if (rowValue !== null && colValue !== null) {
        const row = cleanAlphaNumeric(rowValue);
        const col = cleanAlphaNumeric(colValue);
        if (/^[A-Za-z]+$/.test(col)) {
          return [strictInteger(row) - 1, col[0].toUpperCase().charCodeAt(0) - 65];
        }
        return [strictInteger(row), strictInteger(col)];
      }
      return [0, 0];
    }
    if (Array.isArray(pos)) {
      if (pos.length === 1) return parseStart(pos[0]);
      if (pos.length >= 2) {
        const first = cleanAlphaNumeric(pos[0]);
        const second = cleanAlphaNumeric(pos[1]);
        if (/^[A-Za-z]+$/.test(first)) {
          return [strictInteger(second) - 1, first[0].toUpperCase().charCodeAt(0) - 65];
        }
        if (/^[A-Za-z]+$/.test(second)) {
          return [strictInteger(first) - 1, second[0].toUpperCase().charCodeAt(0) - 65];
        }
        return [strictInteger(first), strictInteger(second)];
      }
    }
    const value = cleanAlphaNumeric(pos);
    const cellMatch = /^([A-Za-z])(\d+)$/.exec(value);
    if (cellMatch) return [Number(cellMatch[2]) - 1, cellMatch[1].toUpperCase().charCodeAt(0) - 65];
    const numbers = value.match(/\d+/g) || [];
    if (numbers.length >= 2) return [Number(numbers[0]), Number(numbers[1])];
  } catch (error) {
    if (!(error instanceof TypeError) && !(error instanceof RangeError)) throw error;
  }
  return [0, 0];
}

function resolveStart(start, rows, cols, barriers, grid) {
  const usable = pos => (
    pos[0] >= 0 && pos[0] < rows && pos[1] >= 0 && pos[1] < cols && !barriers.has(keyOf(pos))
  );
  if (usable(start)) return start;

  for (const [positionKey, label] of grid) {
    const position = positionKey.split(',').map(Number);
    if (START_LABELS.has(label) && usable(position)) {
      console.warn(`start ${JSON.stringify(start)} is out of bounds or inside a wall; using the map's own start tile ${JSON.stringify(position)} instead`);
      return position;
    }
  }

  const [r, c] = start;
  for (const candidate of [[r - 1, c - 1], [r - 1, c], [r, c - 1]]) {
    if (usable(candidate)) {
      console.warn(`start ${JSON.stringify(start)} is unusable; using off-by-one (1-indexed) reading ${JSON.stringify(candidate)} instead`);
      return candidate;
    }
  }

  const clamped = [
    Math.min(Math.max(r, 0), Math.max(rows - 1, 0)),
    Math.min(Math.max(c, 0), Math.max(cols - 1, 0)),
  ];
  if (usable(clamped)) {
    console.warn(`start ${JSON.stringify(start)} is unusable; clamped to ${JSON.stringify(clamped)}`);
    return clamped;
  }

  for (const positionKey of grid.keys()) {
    const position = positionKey.split(',').map(Number);
    if (usable(position)) {
      console.warn(`start ${JSON.stringify(start)} is unusable; falling back to first walkable tile ${JSON.stringify(position)}`);
      return position;
    }
  }
  return start;
}

function buildGrid(gameMap) {
  const grid = new Map();
  const barriers = new Set();
  const dangers = new Set();
  const coins = new Set();
  const challenges = new Set();
  const doorMap = new Map();
  const keyMap = new Map();
  let treasure = null;
  const rows = gameMap.length;
  const cols = rows > 0 ? Math.max(...gameMap.map(row => row.length)) : 0;

  for (let r = 0; r < gameMap.length; r += 1) {
    for (let c = 0; c < gameMap[r].length; c += 1) {
      const cell = gameMap[r][c];
      const cellLower = cell ? String(cell).toLowerCase().trim() : '';
      const position = [r, c];
      const positionKey = keyOf(position);
      grid.set(positionKey, cellLower);
      if (WALL_LABELS.has(cellLower)) barriers.add(positionKey);
      else if (cellLower === 'c8') dangers.add(positionKey);
      else if (cellLower === 'c7') coins.add(positionKey);
      else if (DOOR_RE.test(cellLower)) doorMap.set(positionKey, cellLower);
      else if (KEY_RE.test(cellLower)) keyMap.set(positionKey, cellLower);
      else if (cellLower === 'treasure') treasure = position;
      else if (CHALLENGE_RE.test(cellLower)) challenges.add(positionKey);
    }
  }
  return { grid, rows, cols, barriers, dangers, coins, challenges, doorMap, keyMap, treasure };
}

function bfsSimple(start, goal, rows, cols, barriers) {
  if (samePos(start, goal)) return [start];
  const queue = [[start, [start]]];
  let head = 0;
  const visited = new Set([keyOf(start)]);
  while (head < queue.length) {
    const [[r, c], path] = queue[head++];
    for (const [dr, dc] of DIRECTIONS) {
      const next = [r + dr, c + dc];
      const nextKey = keyOf(next);
      if (next[0] < 0 || next[0] >= rows || next[1] < 0 || next[1] >= cols) continue;
      if (barriers.has(nextKey) || visited.has(nextKey)) continue;
      const newPath = [...path, next];
      if (samePos(next, goal)) return newPath;
      visited.add(nextKey);
      queue.push([next, newPath]);
    }
  }
  return null;
}

function lockedDoors(doorMap, heldKeys) {
  const locked = new Set();
  for (const [positionKey, code] of doorMap) {
    if (!heldKeys.has(keyCodeForDoor(code))) locked.add(positionKey);
  }
  return locked;
}

function comparePaths(left, right) {
  const length = Math.min(left.length, right.length);
  for (let i = 0; i < length; i += 1) {
    if (left[i][0] !== right[i][0]) return left[i][0] - right[i][0];
    if (left[i][1] !== right[i][1]) return left[i][1] - right[i][1];
  }
  return left.length - right.length;
}

class MinHeap {
  constructor(compare) {
    this.items = [];
    this.compare = compare;
  }

  push(value) {
    this.items.push(value);
    let index = this.items.length - 1;
    while (index > 0) {
      const parent = Math.floor((index - 1) / 2);
      if (this.compare(this.items[index], this.items[parent]) >= 0) break;
      [this.items[index], this.items[parent]] = [this.items[parent], this.items[index]];
      index = parent;
    }
  }

  pop() {
    if (this.items.length === 1) return this.items.pop();
    const root = this.items[0];
    this.items[0] = this.items.pop();
    let index = 0;
    while (true) {
      const left = index * 2 + 1;
      const right = left + 1;
      let smallest = index;
      if (left < this.items.length && this.compare(this.items[left], this.items[smallest]) < 0) smallest = left;
      if (right < this.items.length && this.compare(this.items[right], this.items[smallest]) < 0) smallest = right;
      if (smallest === index) break;
      [this.items[index], this.items[smallest]] = [this.items[smallest], this.items[index]];
      index = smallest;
    }
    return root;
  }

  get length() {
    return this.items.length;
  }
}

function weightedBfs(start, goal, rows, cols, barriers, dangers, doorMap, heldKeys) {
  if (samePos(start, goal)) return [[start], 0];
  const locked = lockedDoors(doorMap, heldKeys);
  const compare = (left, right) => (
    left.cost - right.cost || left.position[0] - right.position[0] ||
    left.position[1] - right.position[1] || comparePaths(left.path, right.path)
  );
  const queue = new MinHeap(compare);
  queue.push({ cost: 0, position: start, path: [start] });
  const bestCost = new Map([[keyOf(start), 0]]);

  while (queue.length > 0) {
    const { cost, position: [r, c], path } = queue.pop();
    if (r === goal[0] && c === goal[1]) return [path, cost];
    if (cost > (bestCost.get(`${r},${c}`) ?? Infinity)) continue;
    for (const [dr, dc] of DIRECTIONS) {
      const next = [r + dr, c + dc];
      const nextKey = keyOf(next);
      if (next[0] < 0 || next[0] >= rows || next[1] < 0 || next[1] >= cols || barriers.has(nextKey)) continue;
      const moveCost = locked.has(nextKey) ? DOOR_COST_LOCKED : dangers.has(nextKey) ? DANGER_COST : 1;
      const newCost = cost + moveCost;
      if (newCost < (bestCost.get(nextKey) ?? Infinity)) {
        bestCost.set(nextKey, newCost);
        queue.push({ cost: newCost, position: next, path: [...path, next] });
      }
    }
  }
  return [null, Infinity];
}

function hpCostOfPath(path, dangers, doorMap, heldKeys) {
  const locked = lockedDoors(doorMap, heldKeys);
  let cost = 0;
  for (const position of path.slice(1)) {
    const positionKey = keyOf(position);
    if (dangers.has(positionKey)) cost += 1;
    else if (locked.has(positionKey)) cost += LOCKED_DOOR_HP_DAMAGE;
  }
  return cost;
}

function findPath(start, goal, rows, cols, barriers, dangers, doorMap, heldKeys, treasure = null) {
  const effectiveBarriers = new Set(barriers);
  if (treasure !== null && !samePos(treasure, goal)) effectiveBarriers.add(keyOf(treasure));
  const hazards = new Set([...dangers, ...lockedDoors(doorMap, heldKeys)]);
  const safePath = bfsSimple(start, goal, rows, cols, new Set([...effectiveBarriers, ...hazards]));
  if (safePath !== null) return [safePath, 0];
  const [path] = weightedBfs(start, goal, rows, cols, effectiveBarriers, dangers, doorMap, heldKeys);
  if (path === null) return [null, null];
  return [path, hpCostOfPath(path, dangers, doorMap, heldKeys)];
}

function isReachable(start, goal, rows, cols, barriers, treasure = null) {
  const effectiveBarriers = new Set(barriers);
  if (treasure !== null && !samePos(treasure, goal)) effectiveBarriers.add(keyOf(treasure));
  return bfsSimple(start, goal, rows, cols, effectiveBarriers) !== null;
}

function routeHpFeasible(route, waypoints, hpStart, rows, cols, barriers, dangers, doorMap, heldKeys, challenges, treasure = null) {
  let hpLeft = hpStart;
  let current = route[0];
  for (const next of route.slice(1)) {
    const [path, hpCost] = findPath(
      waypoints[current], waypoints[next], rows, cols, barriers, dangers,
      doorMap, heldKeys, treasure,
    );
    if (path === null) return false;
    const extra = challenges.has(keyOf(waypoints[next])) ? CHALLENGE_HP_COST : 0;
    const cost = hpCost + extra;
    if (cost >= hpLeft) return false;
    hpLeft -= cost;
    current = next;
  }
  return true;
}

function twoOpt(inputRoute, distMatrix, maxPasses = 6) {
  const route = [...inputRoute];
  const n = route.length;
  if (n < 4) return route;
  for (let pass = 0; pass < maxPasses; pass += 1) {
    let improved = false;
    for (let i = 1; i < n - 2; i += 1) {
      for (let j = i + 1; j < n - 1; j += 1) {
        const [a, b, c, d] = [route[i - 1], route[i], route[j], route[j + 1]];
        const [dAB, dCD, dAC, dBD] = [distMatrix[a][b], distMatrix[c][d], distMatrix[a][c], distMatrix[b][d]];
        if ([dAB, dCD, dAC, dBD].some(value => value === Infinity)) continue;
        if (dAC + dBD < dAB + dCD - 1e-9) {
          route.splice(i, j - i + 1, ...route.slice(i, j + 1).reverse());
          improved = true;
        }
      }
    }
    if (!improved) break;
  }
  return route;
}

function cheapestInsertion(routeIndices, distMatrix, candidateIndex) {
  let bestPosition = null;
  let bestExtra = Infinity;
  for (let i = 0; i < routeIndices.length - 1; i += 1) {
    const [a, b] = [routeIndices[i], routeIndices[i + 1]];
    const [dAB, dAC, dCB] = [distMatrix[a][b], distMatrix[a][candidateIndex], distMatrix[candidateIndex][b]];
    if (dAC === Infinity || dCB === Infinity) continue;
    const extra = dAC + dCB - (dAB !== Infinity ? dAB : 0);
    if (extra < bestExtra) {
      bestExtra = extra;
      bestPosition = i + 1;
    }
  }
  return [bestPosition, bestExtra];
}

function orOpt(inputRoute, distMatrix, maxPasses = 6) {
  let route = [...inputRoute];
  const n = route.length;
  if (n < 4) return route;
  for (let pass = 0; pass < maxPasses; pass += 1) {
    let improved = false;
    for (let i = 1; i < n - 1; i += 1) {
      const node = route[i];
      const [previous, next] = [route[i - 1], route[i + 1]];
      const [dPreviousNode, dNodeNext, dPreviousNext] = [
        distMatrix[previous][node], distMatrix[node][next], distMatrix[previous][next],
      ];
      if ([dPreviousNode, dNodeNext, dPreviousNext].some(value => value === Infinity)) continue;
      const removedCost = dPreviousNode + dNodeNext - dPreviousNext;
      if (removedCost <= 1e-9) continue;
      const trial = [...route.slice(0, i), ...route.slice(i + 1)];
      const [position, extra] = cheapestInsertion(trial, distMatrix, node);
      if (position !== null && extra < removedCost - 1e-9) {
        trial.splice(position, 0, node);
        route = trial;
        improved = true;
        break;
      }
    }
    if (!improved) break;
  }
  return route;
}

function computePairwiseDistances(waypoints, rows, cols, barriers, dangers, doorMap, heldKeys, treasure = null) {
  const size = waypoints.length;
  const distances = Array.from({ length: size }, () => Array(size).fill(Infinity));
  for (let i = 0; i < size; i += 1) {
    for (let j = 0; j < size; j += 1) {
      if (i === j) distances[i][j] = 0;
      else {
        const [path] = findPath(
          waypoints[i], waypoints[j], rows, cols, barriers, dangers,
          doorMap, heldKeys, treasure,
        );
        distances[i][j] = path ? path.length - 1 : Infinity;
      }
    }
  }
  return distances;
}

function pathToDirections(path) {
  const directions = [];
  for (let i = 0; i < path.length - 1; i += 1) {
    const dr = path[i + 1][0] - path[i][0];
    const dc = path[i + 1][1] - path[i][1];
    if (dr === 1) directions.push('down');
    else if (dr === -1) directions.push('up');
    else if (dc === 1) directions.push('right');
    else if (dc === -1) directions.push('left');
  }
  return directions;
}

function planCollect(
  currentPosition, coins, challenges, treasure, rows, cols, barriers, dangers,
  doorMap, heldKeys, simulatedHp, stepCost, grid, doorTargets = new Set(),
) {
  const collectibles = new Set([...coins, ...challenges, ...doorTargets]);
  const safeTargets = [];
  const forcedTargets = [];
  for (const positionKey of collectibles) {
    const position = positionKey.split(',').map(Number);
    if (!isReachable(currentPosition, position, rows, cols, barriers, treasure)) continue;
    if (!isReachable(position, treasure, rows, cols, barriers, treasure)) continue;
    const [path, hpCost] = findPath(
      currentPosition, position, rows, cols, barriers, dangers, doorMap, heldKeys, treasure,
    );
    if (path === null) continue;
    const extra = challenges.has(positionKey) ? CHALLENGE_HP_COST : 0;
    if (hpCost + extra >= simulatedHp) continue;
    if (FORCE_COLLECT_TYPES.has(grid.get(positionKey) || '')) forcedTargets.push(position);
    else safeTargets.push(position);
  }

  const allTargets = [...forcedTargets, ...safeTargets];
  const waypoints = [currentPosition, ...allTargets, treasure];
  const treasureIndex = waypoints.length - 1;
  const scores = waypoints.map(position => tileScore(
    grid.get(keyOf(position)) || '', position, doorMap, heldKeys,
  ));

  if (allTargets.length === 0) {
    const [path] = findPath(
      currentPosition, treasure, rows, cols, barriers, dangers, doorMap, heldKeys, treasure,
    );
    if (path === null) return [null, null];
    return [path, -stepCost * (path.length - 1)];
  }

  const distances = computePairwiseDistances(
    waypoints, rows, cols, barriers, dangers, doorMap, heldKeys, treasure,
  );
  const forcedIndices = Array.from({ length: forcedTargets.length }, (_, index) => index + 1);
  const optionalIndices = Array.from(
    { length: treasureIndex - 1 - forcedTargets.length },
    (_, index) => index + 1 + forcedTargets.length,
  );
  let route;

  if (forcedIndices.length > 0) {
    const remaining = new Set(forcedIndices);
    route = [0];
    let current = 0;
    while (remaining.size > 0) {
      let next = null;
      let nextDistance = Infinity;
      for (const target of remaining) {
        const distance = distances[current][target] !== Infinity ? distances[current][target] : 1e9;
        if (next === null || distance < nextDistance) {
          next = target;
          nextDistance = distance;
        }
      }
      route.push(next);
      remaining.delete(next);
      current = next;
    }
    route.push(treasureIndex);

    while (route.length > 2 && !routeHpFeasible(
      route, waypoints, simulatedHp, rows, cols, barriers, dangers,
      doorMap, heldKeys, challenges, treasure,
    )) {
      let worstPosition = null;
      let worstExtra = -1;
      for (let i = 1; i < route.length - 1; i += 1) {
        const trial = [...route.slice(0, i), ...route.slice(i + 1)];
        const [, extra] = cheapestInsertion(trial, distances, route[i]);
        if (extra !== null && extra > worstExtra) {
          worstExtra = extra;
          worstPosition = i;
        }
      }
      route.splice(worstPosition === null ? route.length - 2 : worstPosition, 1);
    }
  } else {
    route = [0, treasureIndex];
  }

  const optionalSorted = [...optionalIndices].sort((left, right) => scores[right] - scores[left]);
  for (const candidate of optionalSorted) {
    const [position, extra] = cheapestInsertion(route, distances, candidate);
    if (position === null || scores[candidate] - stepCost * extra <= 0) continue;
    const trial = [...route.slice(0, position), candidate, ...route.slice(position)];
    if (routeHpFeasible(
      trial, waypoints, simulatedHp, rows, cols, barriers, dangers,
      doorMap, heldKeys, challenges, treasure,
    )) route = trial;
  }

  const optimized = twoOpt(route, distances);
  if (!arraysEqual(optimized, route) && routeHpFeasible(
    optimized, waypoints, simulatedHp, rows, cols, barriers, dangers,
    doorMap, heldKeys, challenges, treasure,
  )) route = optimized;

  const relocated = orOpt(route, distances);
  if (!arraysEqual(relocated, route) && routeHpFeasible(
    relocated, waypoints, simulatedHp, rows, cols, barriers, dangers,
    doorMap, heldKeys, challenges, treasure,
  )) route = relocated;

  const segmentPath = [];
  let hpLeft = simulatedHp;
  let totalScore = 0;
  let currentIndex = 0;
  for (const nextIndex of route.slice(1)) {
    const position = waypoints[nextIndex];
    const [path, hpCost] = findPath(
      waypoints[currentIndex], position, rows, cols, barriers, dangers,
      doorMap, heldKeys, treasure,
    );
    if (path === null) continue;
    const extra = challenges.has(keyOf(position)) ? CHALLENGE_HP_COST : 0;
    const cost = hpCost + extra;
    if (nextIndex !== treasureIndex && cost >= hpLeft) continue;
    segmentPath.push(...(segmentPath.length > 0 ? path.slice(1) : path));
    hpLeft -= cost;
    if (nextIndex !== treasureIndex) totalScore += scores[nextIndex];
    currentIndex = nextIndex;
  }

  if (currentIndex !== treasureIndex) {
    const [path] = findPath(
      waypoints[currentIndex], treasure, rows, cols, barriers, dangers,
      doorMap, heldKeys, treasure,
    );
    if (path === null) return [null, null];
    segmentPath.push(...(segmentPath.length > 0 ? path.slice(1) : path));
  }
  return [segmentPath, totalScore - stepCost * (segmentPath.length - 1)];
}

function arraysEqual(left, right) {
  return left.length === right.length && left.every((value, index) => value === right[index]);
}

function planRecursive(
  position, heldKeys, hp, coins, challenges, treasure, rows, cols, barriers,
  dangers, doorMap, keyGroups, stepCost, grid, prefixPath = [], depth = 0,
  maxDepth = 4,
) {
  const candidates = [];
  const doorTargets = new Set(doorMap.keys());
  const [segment, net] = planCollect(
    position, coins, challenges, treasure, rows, cols, barriers, dangers,
    doorMap, heldKeys, hp, stepCost, grid, doorTargets,
  );
  if (segment !== null) candidates.push([net, [...prefixPath, ...segment]]);

  if (depth < maxDepth) {
    for (const [keyCode, keyPositions] of keyGroups) {
      if (heldKeys.has(keyCode)) continue;
      for (const keyPositionKey of keyPositions) {
        const keyPosition = keyPositionKey.split(',').map(Number);
        if (!isReachable(position, keyPosition, rows, cols, barriers, treasure)) continue;
        const [keyPath, keyHpCost] = findPath(
          position, keyPosition, rows, cols, barriers, dangers, doorMap, heldKeys, treasure,
        );
        if (keyPath === null || keyHpCost >= hp) continue;
        const newPrefix = [...prefixPath, ...(prefixPath.length > 0 ? keyPath.slice(1) : keyPath)];
        const newHeldKeys = new Set(heldKeys);
        newHeldKeys.add(keyCode);
        const [subSegment, subNet] = planRecursive(
          keyPosition, newHeldKeys, hp - keyHpCost, coins, challenges, treasure,
          rows, cols, barriers, dangers, doorMap, keyGroups, stepCost, grid,
          newPrefix, depth + 1, maxDepth,
        );
        if (subSegment !== null) {
          candidates.push([subNet - stepCost * (keyPath.length - 1), subSegment]);
        }
      }
    }
  }

  if (candidates.length === 0) return [null, null];
  candidates.sort((left, right) => right[0] - left[0]);
  return [candidates[0][1], candidates[0][0]];
}

function plan_path(start, gameMap, hpRemaining = 5, stepCost = DEFAULT_STEP_COST) {
  const built = buildGrid(gameMap);
  const {
    grid, rows, cols, barriers, dangers, coins, challenges, doorMap, keyMap,
  } = built;
  const treasure = built.treasure === null ? [rows - 1, cols - 1] : built.treasure;
  start = resolveStart(start, rows, cols, barriers, grid);

  const keyGroups = new Map();
  for (const [positionKey, code] of keyMap) {
    if (!keyGroups.has(code)) keyGroups.set(code, new Set());
    keyGroups.get(code).add(positionKey);
  }

  const [bestPath] = planRecursive(
    start, new Set(), hpRemaining, coins, challenges, treasure, rows, cols,
    barriers, dangers, doorMap, keyGroups, stepCost, grid,
  );
  if (!bestPath) return [];
  const directions = pathToDirections(bestPath);
  if (directions.length <= MAX_ROUTE_DIRECTIONS) return directions;

  const [directPath, directHpCost] = findPath(
    start, treasure, rows, cols, barriers, dangers, doorMap, new Set(), treasure,
  );
  const directMoves = directPath ? directPath.length - 1 : 0;
  if (directPath === null || directHpCost >= hpRemaining || directMoves > MAX_ROUTE_DIRECTIONS) {
    console.error(`no atomic safe route: direct route has ${directMoves} moves (limit ${MAX_ROUTE_DIRECTIONS})`);
    return [];
  }
  console.warn(`optimized route has ${directions.length} moves (limit ${MAX_ROUTE_DIRECTIONS}); using ${directMoves}-move safe treasure route`);
  return pathToDirections(directPath);
}

function coerceNumber(value, fallback, fieldName = '') {
  if (value === null || value === undefined) return fallback;
  if (typeof value === 'number' || typeof value === 'boolean') return value;
  if (typeof value === 'string') {
    try {
      return parsePythonFloat(value);
    } catch {
      // Fall through to the default and warning below.
    }
  }
  console.warn(`Ignoring non-numeric value for ${JSON.stringify(fieldName)}: ${JSON.stringify(value)}, using default ${fallback}`);
  return fallback;
}

function detectSchemaType(event) {
  if (!isRecord(event)) return 'plain';
  if (hasOwn(event, 'function') && hasOwn(event, 'parameters')) return 'function';
  if (hasOwn(event, 'apiPath') && hasOwn(event, 'httpMethod')) return 'openapi';
  return 'plain';
}

function parsePythonFloat(value) {
  const normalized = value.trim();
  if (normalized === '') throw new TypeError('invalid number');
  const special = /^([+-]?)(inf(?:inity)?|nan)$/i.exec(normalized);
  if (special) {
    if (special[2].toLowerCase() === 'nan') return NaN;
    return special[1] === '-' ? -Infinity : Infinity;
  }
  if (!/^[+-]?(?:(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?)$/i.test(normalized)) throw new TypeError('invalid number');
  return Number(normalized);
}

function coerceParamValue(value, parameterType = null) {
  if (typeof value !== 'string') return value;
  const normalized = value.trim();
  if (normalized.startsWith('[') || normalized.startsWith('{')) {
    try {
      return JSON.parse(normalized);
    } catch {
      return value;
    }
  }
  if (parameterType === 'integer') {
    try {
      if (!/^[+-]?\d+$/.test(normalized)) throw new TypeError('invalid integer');
      return Number(normalized);
    } catch {
      return value;
    }
  }
  if (parameterType === 'number') {
    try {
      return parsePythonFloat(normalized);
    } catch {
      return value;
    }
  }
  return value;
}

function extractRequest(event) {
  const schemaType = detectSchemaType(event);
  if (schemaType === 'function') {
    const body = {};
    for (const parameter of event.parameters || []) {
      const name = parameter.name;
      if (name) body[name] = coerceParamValue(parameter.value, parameter.type);
    }
    return [body, schemaType];
  }
  if (schemaType === 'openapi') {
    const properties = event.requestBody?.content?.['application/json']?.properties ?? [];
    if (isRecord(properties)) return [{ ...properties }, schemaType];
    const body = {};
    for (const parameter of properties) {
      const name = parameter.name;
      if (name) body[name] = coerceParamValue(parameter.value, parameter.type);
    }
    return [body, schemaType];
  }
  if (isRecord(event) && typeof event.body === 'string') {
    try {
      return [JSON.parse(event.body), schemaType];
    } catch {
      return [{}, schemaType];
    }
  }
  if (isRecord(event)) {
    const body = hasOwn(event, 'body') ? event.body : event;
    return [isRecord(body) ? body : {}, schemaType];
  }
  return [{}, schemaType];
}

function wrapResponse(event, schemaType, result, statusCode = 200) {
  const body = JSON.stringify(result);
  const eventValue = (key, fallback) => hasOwn(event, key) ? event[key] : fallback;
  if (schemaType === 'function') {
    return {
      messageVersion: '1.0',
      response: {
        actionGroup: eventValue('actionGroup', 'PathfindingLambdaTarget'),
        function: eventValue('function', 'plan_path'),
        functionResponse: { responseBody: { TEXT: { body } } },
      },
      sessionAttributes: eventValue('sessionAttributes', {}),
      promptSessionAttributes: eventValue('promptSessionAttributes', {}),
    };
  }
  if (schemaType === 'openapi') {
    return {
      messageVersion: '1.0',
      response: {
        actionGroup: eventValue('actionGroup', 'PathfindingLambdaTarget'),
        apiPath: eventValue('apiPath', '/plan_path'),
        httpMethod: eventValue('httpMethod', 'POST'),
        httpStatusCode: statusCode,
        responseBody: { 'application/json': { body } },
      },
      sessionAttributes: eventValue('sessionAttributes', {}),
      promptSessionAttributes: eventValue('promptSessionAttributes', {}),
    };
  }
  return { statusCode, body };
}

async function lambda_handler(event, context) {
  void context;
  try {
    if (!isRecord(event)) event = {};
    const [body, schemaType] = extractRequest(event);
    const gameMap = firstPresent(body, ['map', 'game_map', 'grid'], []);
    if (!Array.isArray(gameMap) || gameMap.length === 0) {
      return wrapResponse(
        event, schemaType,
        { directions: [], error: 'Missing non-empty map/game_map/grid.' }, 400,
      );
    }
    if (!gameMap.every(Array.isArray)) {
      return wrapResponse(event, schemaType, { directions: [], error: 'Map rows must be arrays.' }, 400);
    }
    const maxColumns = Math.max(...gameMap.map(row => row.length));
    const normalizedMap = gameMap.map(row => [...row, ...Array(maxColumns - row.length).fill('normal')]);
    const start = parseStart(firstPresent(
      body, ['start', 'start_pos', 'position', 'current_position', 'agent_position'], 'A1',
    ));
    const hp = coerceNumber(firstPresent(body, ['hp', 'current_hp', 'health', 'life_points'], 5), 5, 'hp');
    const stepCost = coerceNumber(firstPresent(body, ['step_cost', 'time_penalty'], DEFAULT_STEP_COST), DEFAULT_STEP_COST, 'step_cost');
    const directions = plan_path(start, normalizedMap, hp, stepCost);
    if (directions.length === 0) {
      return wrapResponse(
        event, schemaType,
        { directions: [], error: 'No safe route found; no movement was issued.' }, 422,
      );
    }
    return wrapResponse(event, schemaType, { directions });
  } catch (error) {
    console.error('plan_path failed', error);
    const schemaType = detectSchemaType(event);
    return wrapResponse(
      event, schemaType,
      { directions: [], error: `plan_path failed: ${error.message}` }, 500,
    );
  }
}

module.exports = { plan_path, lambda_handler };

if (require.main === module) {
  (async () => {
    const moves = { up: [-1, 0], down: [1, 0], left: [0, -1], right: [0, 1] };
    const walk = (start, directions) => {
      let [r, c] = start;
      return directions.map(direction => {
        r += moves[direction][0];
        c += moves[direction][1];
        return [r, c];
      });
    };

    const singleDoorMap = [
      ['start', 'normal', 'c40'],
      ['normal', 'wall', 'c30'],
      ['normal', 'wall', 'c7'],
      ['normal', 'wall', 'normal'],
      ['normal', 'normal', 'treasure'],
    ];
    const singleVisited = walk([0, 0], plan_path([0, 0], singleDoorMap, 5));
    assert(singleVisited.some(position => samePos(position, [1, 2])));
    assert(singleVisited.findIndex(position => samePos(position, [0, 2])) < singleVisited.findIndex(position => samePos(position, [1, 2])));
    assert.deepEqual(singleVisited.at(-1), [4, 2]);

    const twoDoorMap = [
      ['start', 'normal', 'c40', 'normal', 'c41'],
      ['normal', 'wall', 'c30', 'wall', 'c31'],
      ['normal', 'wall', 'c7', 'wall', 'c7'],
      ['normal', 'wall', 'normal', 'wall', 'normal'],
      ['normal', 'normal', 'normal', 'normal', 'treasure'],
    ];
    const twoVisited = walk([0, 0], plan_path([0, 0], twoDoorMap, 10));
    for (const [key, door] of [[[0, 2], [1, 2]], [[0, 4], [1, 4]]]) {
      const keyIndex = twoVisited.findIndex(position => samePos(position, key));
      const doorIndex = twoVisited.findIndex(position => samePos(position, door));
      assert(keyIndex >= 0 && keyIndex < doorIndex);
    }
    assert.deepEqual(twoVisited.at(-1), [4, 4]);

    const unreachableKeyMap = [
      ['start', 'normal', 'normal'],
      ['normal', 'wall', 'c30'],
      ['normal', 'wall', 'normal'],
      ['normal', 'normal', 'treasure'],
    ];
    const lockedVisited = walk([0, 0], plan_path([0, 0], unreachableKeyMap, 10));
    assert(!lockedVisited.some(position => samePos(position, [1, 2])));
    assert.deepEqual(lockedVisited.at(-1), [3, 2]);
    const scoringDoorMap = new Map([['1,2', 'c30']]);
    assert.equal(tileScore('c30', [1, 2], scoringDoorMap, new Set()), 0);
    assert.equal(tileScore('c30', [1, 2], scoringDoorMap, new Set(['c40'])), DEFAULT_DOOR_SCORE);

    assert.deepEqual(parseStart({ row: 4, col: 0 }), [4, 0]);
    assert.deepEqual(parseStart({ col: 0, row: 4 }), [4, 0]);
    assert.deepEqual(parseStart({ row: 5, col: 'A' }), [4, 0]);
    assert.deepEqual(parseStart('A5'), [4, 0]);

    const walledMap = [
      ['wall', 'wall', 'wall'],
      ['wall', 'start', 'normal'],
      ['wall', 'normal', 'treasure'],
    ];
    const walledGrid = buildGrid(walledMap);
    assert.deepEqual(resolveStart([0, 0], walledGrid.rows, walledGrid.cols, walledGrid.barriers, walledGrid.grid), [1, 1]);

    const brickMap = [
      ['start', 'brick', 'normal'],
      ['normal', 'brick', 'normal'],
      ['normal', 'normal', 'treasure'],
    ];
    const brickVisited = walk([0, 0], plan_path([0, 0], brickMap, 10));
    assert(!brickVisited.some(position => samePos(position, [0, 1]) || samePos(position, [1, 1])));
    assert.deepEqual(brickVisited.at(-1), [2, 2]);

    const suppliedMap = [
      ['c42', 'c5', 'normal', 'normal', 'c1', 'normal', 'c7', 'normal', 'normal', 'treasure'],
      ['c17', 'normal', 'normal', 'c4', 'wall', 'normal', 'normal', 'normal', 'normal', 'normal'],
      ['normal', 'normal', 'normal', 'normal', 'wall', 'c43', 'normal', 'normal', 'normal', 'normal'],
      ['wall', 'wall', 'wall', 'c2', 'wall', 'wall', 'c8', 'wall', 'wall', 'c33'],
      ['start', 'normal', 'normal', 'normal', 'c8', 'normal', 'normal', 'normal', 'normal', 'normal'],
      ['wall', 'wall', 'wall', 'c8', 'wall', 'wall', 'wall', 'wall', 'wall', 'c32'],
      ['c8', 'normal', 'normal', 'normal', 'wall', 'c7', 'c7', 'c7', 'c7', 'c1'],
      ['c4', 'normal', 'normal', 'c17', 'wall', 'c5', 'c7', 'c7', 'c7', 'c7'],
      ['normal', 'normal', 'normal', 'normal', 'wall', 'wall', 'wall', 'wall', 'wall', 'normal'],
      ['c8', 'normal', 'normal', 'c5', 'c2', 'c7', 'c7', 'c7', 'c7', 'c7'],
    ];
    const suppliedEvent = {
      messageVersion: '1.0',
      actionGroup: 'PathfindingLambdaTarget',
      function: 'plan_path',
      parameters: [
        { name: 'map', type: 'array', value: JSON.stringify(suppliedMap) },
        { name: 'start_pos', type: 'object', value: JSON.stringify({ row: 4, col: 0 }) },
        { name: 'hp', type: 'integer', value: '5' },
        { name: 'step_cost', type: 'number', value: '3' },
      ],
      sessionAttributes: {},
      promptSessionAttributes: {},
    };
    const functionResponse = await lambda_handler(suppliedEvent, null);
    const expectedDirections = [...Array(3).fill('right'), ...Array(4).fill('up'), ...Array(6).fill('right')];
    const expectedFunctionBody = JSON.stringify({ directions: expectedDirections });
    assert.deepEqual(functionResponse, {
      messageVersion: '1.0',
      response: {
        actionGroup: 'PathfindingLambdaTarget',
        function: 'plan_path',
        functionResponse: { responseBody: { TEXT: { body: expectedFunctionBody } } },
      },
      sessionAttributes: {},
      promptSessionAttributes: {},
    });
    const functionBodyString = functionResponse.response.functionResponse.responseBody.TEXT.body;
    assert.equal(typeof functionBodyString, 'string');
    const functionBody = JSON.parse(functionBodyString);
    assert.deepEqual(functionBody, { directions: expectedDirections });
    const suppliedVisited = walk([4, 0], functionBody.directions);
    const suppliedGrid = buildGrid(suppliedMap);
    assert(suppliedVisited.every(([r, c]) => r >= 0 && r < 10 && c >= 0 && c < 10 && !suppliedGrid.barriers.has(`${r},${c}`)));
    assert.deepEqual(suppliedVisited.at(-1), [0, 9]);
    assert(suppliedVisited.some(position => samePos(position, [0, 6])));

    const serpentineMap = [
      Array(10).fill('normal'),
      [...Array(9).fill('wall'), 'normal'],
      Array(10).fill('normal'),
      ['normal', ...Array(9).fill('wall')],
      Array(10).fill('normal'),
      [...Array(9).fill('wall'), 'normal'],
      Array(10).fill('normal'),
      ['normal', ...Array(9).fill('wall')],
      Array(10).fill('normal'),
    ];
    serpentineMap[0][0] = 'start';
    serpentineMap[8][9] = 'treasure';
    assert.deepEqual(plan_path([0, 0], serpentineMap, 10), []);

    const malformedFunctionEvent = {
      messageVersion: '1.0', actionGroup: 'PathfindingLambdaTarget',
      function: 'plan_path', parameters: [],
    };
    const malformedResponse = await lambda_handler(malformedFunctionEvent, null);
    const malformedBody = JSON.parse(malformedResponse.response.functionResponse.responseBody.TEXT.body);
    assert.deepEqual(malformedBody.directions, []);
    assert(malformedBody.error);

    const openApiEvent = {
      messageVersion: '1.0', actionGroup: 'PathfindingLambdaTarget',
      apiPath: '/plan_path', httpMethod: 'POST',
      requestBody: { content: { 'application/json': { properties: {
        map: [['start', 'treasure']], start: 'A1', hp: 5, step_cost: 3,
      } } } },
      sessionAttributes: { session: 'openapi' }, promptSessionAttributes: { prompt: 'openapi' },
    };
    const openApiResponse = await lambda_handler(openApiEvent, null);
    assert.deepEqual(openApiResponse, {
      messageVersion: '1.0',
      response: {
        actionGroup: 'PathfindingLambdaTarget',
        apiPath: '/plan_path',
        httpMethod: 'POST',
        httpStatusCode: 200,
        responseBody: { 'application/json': { body: JSON.stringify({ directions: ['right'] }) } },
      },
      sessionAttributes: { session: 'openapi' },
      promptSessionAttributes: { prompt: 'openapi' },
    });
    assert.equal(typeof openApiResponse.response.responseBody['application/json'].body, 'string');

    const plainResponse = await lambda_handler({
      body: JSON.stringify({ map: [['start', 'treasure']], start: 'A1' }),
    }, null);
    assert.deepEqual(plainResponse, {
      statusCode: 200,
      body: JSON.stringify({ directions: ['right'] }),
    });
    assert.equal(typeof plainResponse.body, 'string');

    assert.deepEqual(Object.keys(module.exports).sort(), ['lambda_handler', 'plan_path']);
    console.log('OK: all pathfinding.js self-checks passed');
  })().catch(error => {
    console.error(error);
    process.exitCode = 1;
  });
}
