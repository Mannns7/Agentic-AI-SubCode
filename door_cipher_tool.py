"""
Deterministic door-cipher tool for c30 (Red Door) and c31 (Green Door).

Why this exists:
LLMs are unreliable at exact character-by-character manipulation done purely
through reasoning (e.g. reversing "MalaysiaBoleh" by "thinking about it" can
silently produce a wrong result like "holeBoysalyaM" instead of the correct
"heloBaisyalaM"). This tool performs the transform in real code so the result
is always exactly correct, then the supervisor should output ONLY what this
tool returns - no re-typing/re-deriving the value itself.

Usage (event payload):
    {"door": "red",   "key": "MalaysiaBoleh"}   -> reversed string
    {"door": "green", "key": "NasiLeM@K000"}    -> letter positions, "-" joined

Call this for EVERY c30/c31 challenge. Never hand-transform the key yourself.
"""
import json
from typing import Dict, Any


def reverse_key(key: str) -> str:
    """c30 Red Door: reverse the string character by character, exactly."""
    return key[::-1]


def key_to_numbers(key: str) -> str:
    """c31 Green Door: replace each letter with its 1-indexed alphabet
    position (A/a=1 ... Z/z=26). Non-letters (digits, symbols, spaces) are
    kept AS-IS but still emitted as their own token so the output is
    unambiguous. Tokens are joined with '-' to avoid merging multi-digit
    numbers together (e.g. 14 and 1 must not read as 141)."""
    tokens = []
    for ch in key:
        if ch.isalpha():
            base = ord('A') if ch.isupper() else ord('a')
            tokens.append(str(ord(ch) - base + 1))
        else:
            tokens.append(ch)
    return '-'.join(tokens)


def solve_door(door: str, key: str) -> str:
    door = (door or '').strip().lower()
    if door in ('red', 'c30', 'reverse'):
        return reverse_key(key)
    if door in ('green', 'c31', 'letters_to_numbers', 'numbers'):
        return key_to_numbers(key)
    raise ValueError(f"Unknown door type: {door!r}. Use 'red' or 'green'.")


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    try:
        if 'body' in event and isinstance(event.get('body'), str):
            params = json.loads(event['body'])
        else:
            params = event

        door = params.get('door', '')
        key = params.get('key', '')
        if not key:
            return _resp(400, {'success': False, 'error': "Missing 'key'"})

        result = solve_door(door, key)
        return _resp(200, {'success': True, 'door': door, 'key': key, 'result': result})
    except Exception as e:
        return _resp(500, {'success': False, 'error': str(e)})


def _resp(code: int, body: Dict) -> Dict[str, Any]:
    return {
        'statusCode': code,
        'headers': {'Content-Type': 'application/json'},
        'body': json.dumps(body, ensure_ascii=False)
    }


if __name__ == '__main__':
    # Quick self-check
    print(reverse_key('MalaysiaBoleh'))          # -> heloBaisyalaM
    print(key_to_numbers('NasiLeM@K000'))         # -> 14-1-19-9-12-5-13-@-11-0-0-0
