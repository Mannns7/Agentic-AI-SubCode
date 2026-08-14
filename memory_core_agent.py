"""
Memory Core Agent Lambda
=========================
Implements the "myAgentMemory" tool referenced throughout supervisor_prompt.md
(context lookups, c42/c43 key storage, c32/c33 key/code lookups for door
ciphers). Previously this tool had NO implementation at all - only a short
usage note in memory_tool_description.md. This file rebuilds it as a real,
deployable Bedrock Agent action-group Lambda.

NOTE: the key/value store itself is color-agnostic (any string key works),
so no code below needed to change when this round's map swapped Red/Green
keys+doors for Grey (c42/c32) and Yellow (c43/c33) - only the comments and
docstrings below (and the delegating supervisor prompts) referenced the
old colors/codes, and have been updated to match.

--------------------------------------------------------------------
HOW MEMORY PERSISTS ACROSS TURNS (no external database needed)
--------------------------------------------------------------------
A single Lambda invocation is stateless - nothing kept in a global/module
variable is guaranteed to survive to the NEXT turn (Lambda execution
environments can be recycled at any time). Amazon Bedrock Agents solve
exactly this problem with "session attributes": a flat string->string map
that Bedrock sends INTO the Lambda on `event["sessionAttributes"]` and
that Bedrock will keep and re-send on every subsequent turn of the SAME
session, as long as the Lambda's response echoes it back (updated) in
`response["sessionAttributes"]`. See:
https://docs.aws.amazon.com/bedrock/latest/userguide/agents-session-state.html

This agent stores its entire memory (key/value store + freeform log) as
ONE JSON-encoded string under the session attribute key
"agent_memory_json". Every invocation:
  1. reads that JSON string back into a dict,
  2. applies the requested action,
  3. re-serializes it and returns it in sessionAttributes so Bedrock
     carries it forward to the next turn automatically.

This is fully self-contained (no DynamoDB, no network, no extra infra),
consistent with the rest of this repo's "restricted, sandboxed, no
external dependency" design.

--------------------------------------------------------------------
WIRE FORMAT (Bedrock Agent action-group Lambda contract)
--------------------------------------------------------------------
Same contract fixed in unified_specialist.py - a Bedrock Agent sends
either a "function" schema event or an "OpenAPI" schema event, and
expects a response in the matching shape (NOT a plain
{"statusCode":...,"body":...} shape). This file re-implements that same
detection/wrapping logic so myAgentMemory doesn't fall into the same
"technical issue" failure loop that unified_specialist previously did.

Actions (function/apiPath names to register on the myAgentMemory action
group):
  store         - key, value            -> save/overwrite one memory slot
  retrieve      - key                    -> read one memory slot back
  retrieve_all  - (no params)            -> dump every stored slot + log
  log_note      - note, [turn]           -> append a freeform context note
                                             (for "what happened before"
                                             recall questions)
  clear         - (no params)            -> wipe all memory (new game only)

Deploying this file:
- Point a Lambda function's handler at memory_core_agent.lambda_handler.
- Register an action group named "myAgentMemory" with the 5
  functions/apiPaths above.
"""

import json
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

MEMORY_SESSION_KEY = "agent_memory_json"
VALID_ACTIONS = {"store", "retrieve", "retrieve_all", "log_note", "clear"}


# ============================================================================
# MEMORY STORE (encode/decode into/out of Bedrock sessionAttributes)
# ============================================================================
def _empty_memory():
    return {"kv": {}, "log": []}


def _decode_memory(session_attributes):
    """Best-effort decode; a corrupt/missing value never raises, it just
    starts a fresh memory store instead (never crash the whole turn over
    a memory read)."""
    if not isinstance(session_attributes, dict):
        return _empty_memory()
    raw = session_attributes.get(MEMORY_SESSION_KEY)
    if not raw:
        return _empty_memory()
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return _empty_memory()
    if not isinstance(data, dict):
        return _empty_memory()
    data.setdefault("kv", {})
    data.setdefault("log", [])
    if not isinstance(data["kv"], dict):
        data["kv"] = {}
    if not isinstance(data["log"], list):
        data["log"] = []
    return data


def _encode_memory(memory, session_attributes):
    """Return a NEW session_attributes dict with the memory written back
    in, preserving any other session attributes the caller had set."""
    out = dict(session_attributes) if isinstance(session_attributes, dict) else {}
    out[MEMORY_SESSION_KEY] = json.dumps(memory)
    return out


# ============================================================================
# ACTIONS
# ============================================================================
def _run_store(body, memory):
    """
    Params: key (string, required), value (any - stored as given)

    Use for c42 Grey Key / c43 Yellow Key on receipt: store(key="grey_key",
    value="<exact value received>"). The supervisor's own reply text must
    restate the exact value (see supervisor_prompt.md) - this action just
    persists it for later c32/c33 retrieval.
    """
    key = body.get("key")
    value = body.get("value")
    if not key or not isinstance(key, str):
        return {"error": "Missing required 'key' string parameter."}, 400

    memory["kv"][key] = value
    memory["log"].append({"type": "store", "key": key, "value": value, "turn": body.get("turn")})
    return {"stored": True, "key": key, "value": value}, 200


def _run_retrieve(body, memory):
    """
    Params: key (string, required)

    Use for c32 Grey Door / c33 Yellow Door: retrieve(key="grey_key") to
    get back the exact value stored earlier by c42/c43, before applying
    whatever cipher/transform that specific door's own question states.
    """
    key = body.get("key")
    if not key or not isinstance(key, str):
        return {"error": "Missing required 'key' string parameter."}, 400

    if key in memory["kv"]:
        return {"key": key, "value": memory["kv"][key], "found": True}, 200
    return {"key": key, "value": None, "found": False}, 200


def _run_retrieve_all(body, memory):
    """
    No params required.

    Use whenever the supervisor needs to recall prior map/interaction
    context: returns every stored key/value plus the full freeform
    context log, so the supervisor can answer "what happened earlier"
    style questions. This action never computes/counts anything itself -
    any exact counting over the returned data must still be delegated to
    execute_code, per supervisor_prompt.md.
    """
    return {"kv": dict(memory["kv"]), "log": list(memory["log"])}, 200


def _run_log_note(body, memory):
    """
    Params: note (string, required), turn (optional, any)

    Use for recording general map/interaction context as it happens
    (e.g. "Saw a counting challenge at B2", "Opened grey door at F5"),
    independent of the key/value store, so later "what happened before"
    questions have real prior context to query via retrieve_all.
    """
    note = body.get("note")
    if not note or not isinstance(note, str):
        return {"error": "Missing required 'note' string parameter."}, 400

    entry = {"type": "note", "note": note, "turn": body.get("turn")}
    memory["log"].append(entry)
    return {"logged": True, "entry": entry}, 200


def _run_clear(body, memory):
    """
    No params required. Wipes all stored key/values and the log.

    Only call this at the very start of a brand-new game - never mid-game,
    or c32/c33 door lookups and prior context recall will silently lose
    everything collected so far.
    """
    memory["kv"].clear()
    memory["log"].clear()
    return {"cleared": True}, 200


_ACTION_HANDLERS = {
    "store": _run_store,
    "retrieve": _run_retrieve,
    "retrieve_all": _run_retrieve_all,
    "log_note": _run_log_note,
    "clear": _run_clear,
}


# ============================================================================
# UNIFIED DISPATCH — Bedrock-Agent-compatible transport layer
# (same contract as unified_specialist.py's fix; duplicated here because
#  this is a separate deployable Lambda/action group.)
# ============================================================================
def _detect_schema_type(event):
    if not isinstance(event, dict):
        return "plain"
    if "function" in event and "parameters" in event:
        return "function"
    if "apiPath" in event and "httpMethod" in event:
        return "openapi"
    return "plain"


def _coerce_param_value(value, ptype=None):
    """Bedrock Agent parameter values arrive as strings even for
    non-string types. Decode JSON-looking strings, cast scalars per the
    declared type, and never raise - fall back to the raw value."""
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
    """Returns (action, body, schema_type, session_attributes,
    prompt_session_attributes)."""
    schema_type = _detect_schema_type(event)

    if schema_type == "function":
        action = event.get("function")
        body = {}
        for p in (event.get("parameters") or []):
            name = p.get("name")
            if name is None:
                continue
            body[name] = _coerce_param_value(p.get("value"), p.get("type"))
        return (action, body, schema_type,
                event.get("sessionAttributes", {}) or {},
                event.get("promptSessionAttributes", {}) or {})

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
        return (action, body, schema_type,
                event.get("sessionAttributes", {}) or {},
                event.get("promptSessionAttributes", {}) or {})

    # "plain": local/manual testing shape - a raw dict already shaped
    # like the tool's own body, optionally carrying its own
    # session_attributes for test continuity across calls.
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
    action = str(body.get("action", "")).strip().lower() or None
    session_attributes = event.get("session_attributes", event.get("sessionAttributes", {})) or {}
    return action, body, schema_type, session_attributes, {}


def _wrap_response(event, schema_type, action, result_dict, status_code, session_attributes, prompt_session_attributes):
    """Build the response in the SAME shape the request came in, echoing
    back the (possibly updated) sessionAttributes so Bedrock persists
    memory to the next turn."""
    body_json = json.dumps(result_dict)

    if schema_type == "function":
        return {
            "messageVersion": "1.0",
            "response": {
                "actionGroup": event.get("actionGroup", "myAgentMemory"),
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
                "actionGroup": event.get("actionGroup", "myAgentMemory"),
                "apiPath": event.get("apiPath", f"/{action}" if action else "/unknown"),
                "httpMethod": event.get("httpMethod", "POST"),
                "httpStatusCode": status_code,
                "responseBody": {"application/json": {"body": body_json}},
            },
            "sessionAttributes": session_attributes,
            "promptSessionAttributes": prompt_session_attributes,
        }

    # "plain": echo session_attributes back for local test continuity.
    return {"statusCode": status_code, "body": body_json, "session_attributes": session_attributes}


def lambda_handler(event, context):
    """
    Single entry point for the myAgentMemory action group
    (store / retrieve / retrieve_all / log_note / clear), speaking
    whichever of the 3 known Bedrock-Agent-compatible wire shapes the
    request used.
    """
    try:
        if not isinstance(event, dict):
            event = {}

        action, body, schema_type, session_attributes, prompt_session_attributes = _extract_request(event)
        action_norm = str(action or "").strip().lower()

        memory = _decode_memory(session_attributes)

        handler = _ACTION_HANDLERS.get(action_norm)
        if handler is None:
            result, status = (
                {"error": f"Missing or unrecognized action '{action}'. "
                          f"Expected one of {sorted(VALID_ACTIONS)}."},
                400,
            )
            new_session_attributes = _encode_memory(memory, session_attributes)
            return _wrap_response(event, schema_type, action_norm, result, status,
                                   new_session_attributes, prompt_session_attributes)

        result, status = handler(body, memory)
        new_session_attributes = _encode_memory(memory, session_attributes)
        return _wrap_response(event, schema_type, action_norm, result, status,
                               new_session_attributes, prompt_session_attributes)

    except Exception as e:
        # Never return an unparseable/crashed response - that looks like
        # a tool failure to the Bedrock orchestrator and triggers a
        # silent retry loop (exactly the bug fixed in unified_specialist).
        try:
            schema_type = _detect_schema_type(event) if isinstance(event, dict) else "plain"
            action_norm = str(event.get("function") or "") if isinstance(event, dict) else ""
            session_attributes = event.get("sessionAttributes", event.get("session_attributes", {})) if isinstance(event, dict) else {}
            prompt_session_attributes = event.get("promptSessionAttributes", {}) if isinstance(event, dict) else {}
            return _wrap_response(event if isinstance(event, dict) else {}, schema_type, action_norm,
                                   {"error": f"Fallback mode: {e}"}, 200,
                                   session_attributes, prompt_session_attributes)
        except Exception:
            return {"statusCode": 200, "body": json.dumps({"error": f"Fallback mode: {e}"})}


# ============================================================================
# SELF-CHECKS
# ============================================================================
if __name__ == "__main__":
    # ---- decode/encode round-trip ----
    mem = _decode_memory({})
    assert mem == {"kv": {}, "log": []}, mem

    mem = _decode_memory({MEMORY_SESSION_KEY: "not valid json{{"})
    assert mem == {"kv": {}, "log": []}, "corrupt memory must never crash, just reset"

    mem = _decode_memory(None)
    assert mem == {"kv": {}, "log": []}, "missing sessionAttributes must never crash"

    print("OK: decode/encode self-checks passed (3)")

    # ---- store / retrieve round trip (plain shape) ----
    resp1 = lambda_handler({"action": "store", "key": "grey_key", "value": "MalaysiaBoleh"}, None)
    assert resp1["statusCode"] == 200, resp1
    body1 = json.loads(resp1["body"])
    assert body1["stored"] is True and body1["value"] == "MalaysiaBoleh", body1
    carried_session = resp1["session_attributes"]

    resp2 = lambda_handler({"action": "retrieve", "key": "grey_key", "session_attributes": carried_session}, None)
    body2 = json.loads(resp2["body"])
    assert body2["found"] is True and body2["value"] == "MalaysiaBoleh", \
        "value stored in turn 1 must be retrievable in turn 2 via carried session_attributes"

    resp3 = lambda_handler({"action": "retrieve", "key": "yellow_key", "session_attributes": carried_session}, None)
    body3 = json.loads(resp3["body"])
    assert body3["found"] is False and body3["value"] is None, "unknown key must report found=False, never error"

    print("OK: store/retrieve round-trip self-checks passed (3)")

    # ---- log_note + retrieve_all ----
    resp4 = lambda_handler({"action": "log_note", "note": "Collected yellow key on B2", "turn": 3,
                             "session_attributes": carried_session}, None)
    body4 = json.loads(resp4["body"])
    assert body4["logged"] is True, body4
    carried_session2 = resp4["session_attributes"]

    resp5 = lambda_handler({"action": "store", "key": "yellow_key", "value": "OpenSesame",
                             "session_attributes": carried_session2}, None)
    carried_session3 = json.loads(resp5["body"]) and resp5["session_attributes"]

    resp6 = lambda_handler({"action": "retrieve_all", "session_attributes": carried_session3}, None)
    body6 = json.loads(resp6["body"])
    assert body6["kv"] == {"grey_key": "MalaysiaBoleh", "yellow_key": "OpenSesame"}, body6
    assert any(entry.get("note") == "Collected yellow key on B2" for entry in body6["log"]), body6
    assert any(entry.get("key") == "grey_key" for entry in body6["log"]), "store actions must also land in the log for later recall"

    print("OK: log_note/retrieve_all self-checks passed (2)")

    # ---- clear ----
    resp7 = lambda_handler({"action": "clear", "session_attributes": carried_session3}, None)
    body7 = json.loads(resp7["body"])
    assert body7["cleared"] is True, body7
    resp8 = lambda_handler({"action": "retrieve_all", "session_attributes": resp7["session_attributes"]}, None)
    body8 = json.loads(resp8["body"])
    assert body8["kv"] == {} and body8["log"] == [], "clear must wipe both kv and log"

    print("OK: clear self-check passed (1)")

    # ---- missing/invalid params never crash ----
    resp9 = lambda_handler({"action": "store", "value": "no key given"}, None)
    assert resp9["statusCode"] == 400, resp9

    resp10 = lambda_handler({"action": "retrieve"}, None)
    assert resp10["statusCode"] == 400, resp10

    resp11 = lambda_handler({"action": "unknown_action"}, None)
    assert resp11["statusCode"] == 400, resp11

    resp12 = lambda_handler({}, None)
    assert resp12["statusCode"] == 400, resp12

    print("OK: invalid-input safety self-checks passed (4)")

    # ---- REAL Bedrock "function" schema round trip ----
    event_fn_store = {
        "messageVersion": "1.0",
        "actionGroup": "myAgentMemory",
        "function": "store",
        "parameters": [
            {"name": "key", "type": "string", "value": "grey_key"},
            {"name": "value", "type": "string", "value": "MalaysiaBoleh"},
        ],
        "sessionAttributes": {}, "promptSessionAttributes": {},
    }
    resp_fn1 = lambda_handler(event_fn_store, None)
    assert resp_fn1["messageVersion"] == "1.0", resp_fn1
    assert resp_fn1["response"]["function"] == "store", resp_fn1
    fn_body1 = json.loads(resp_fn1["response"]["functionResponse"]["responseBody"]["TEXT"]["body"])
    assert fn_body1["stored"] is True, fn_body1
    fn_session = resp_fn1["sessionAttributes"]
    assert MEMORY_SESSION_KEY in fn_session, "memory must be persisted into sessionAttributes for Bedrock to carry forward"

    event_fn_retrieve = {
        "messageVersion": "1.0",
        "actionGroup": "myAgentMemory",
        "function": "retrieve",
        "parameters": [{"name": "key", "type": "string", "value": "grey_key"}],
        "sessionAttributes": fn_session, "promptSessionAttributes": {"turn": "2"},
    }
    resp_fn2 = lambda_handler(event_fn_retrieve, None)
    assert resp_fn2["promptSessionAttributes"] == {"turn": "2"}, "promptSessionAttributes must pass through unchanged"
    fn_body2 = json.loads(resp_fn2["response"]["functionResponse"]["responseBody"]["TEXT"]["body"])
    assert fn_body2["found"] is True and fn_body2["value"] == "MalaysiaBoleh", \
        "function-schema retrieve must see the value stored by a prior function-schema store call"

    print("OK: Bedrock function-schema self-checks passed (2)")

    # ---- REAL Bedrock "OpenAPI" schema round trip ----
    event_oa_store = {
        "messageVersion": "1.0",
        "actionGroup": "myAgentMemory",
        "apiPath": "/store",
        "httpMethod": "POST",
        "requestBody": {"content": {"application/json": {"properties": [
            {"name": "key", "type": "string", "value": "yellow_key"},
            {"name": "value", "type": "string", "value": "OpenSesame"},
        ]}}},
    }
    resp_oa1 = lambda_handler(event_oa_store, None)
    assert resp_oa1["response"]["httpStatusCode"] == 200, resp_oa1
    oa_body1 = json.loads(resp_oa1["response"]["responseBody"]["application/json"]["body"])
    assert oa_body1["stored"] is True, oa_body1
    oa_session = resp_oa1["sessionAttributes"]

    event_oa_retrieve = {
        "messageVersion": "1.0",
        "actionGroup": "myAgentMemory",
        "apiPath": "/retrieve",
        "httpMethod": "POST",
        "requestBody": {"content": {"application/json": {"properties": [
            {"name": "key", "type": "string", "value": "yellow_key"},
        ]}}},
        "sessionAttributes": oa_session,
    }
    resp_oa2 = lambda_handler(event_oa_retrieve, None)
    oa_body2 = json.loads(resp_oa2["response"]["responseBody"]["application/json"]["body"])
    assert oa_body2["found"] is True and oa_body2["value"] == "OpenSesame", \
        "OpenAPI-schema retrieve must see the value stored by a prior OpenAPI-schema store call"

    print("OK: Bedrock OpenAPI-schema self-checks passed (2)")

    print("ALL SELF-CHECKS PASSED (17 total)")
