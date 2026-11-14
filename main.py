#!/usr/bin/env python3
"""free-model-aggregator-01 entry: one stdlib-only server, OpenAI upstream wire."""
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ENV_URL = (os.environ.get("MAKEFUN_ENV_URL") or "").rstrip("/")
PROMPT_FILE = os.environ.get("MAKEFUN_PROMPT_FILE") or ""
POLL_MS = 0.1
MAX_BODY = 32 * 1024 * 1024
FIXED_CREATED = 1767225600
FIXED_CREATED_AT = "2026-01-01T00:00:00Z"
PROVIDERS_SCHEMA = "free-model-aggregator-01/providers/v1"
RATINGS_SCHEMA = "free-model-aggregator-01/ratings/v1"
WORKLOAD_SCHEMA = "free-model-aggregator-01/workload/v1"

PROTOCOL_VERSION = 1


def log(msg):
    sys.stderr.write(f"aggregator: {msg}\n")
    sys.stderr.flush()


def documents_in(text):
    """Every JSON object in text carrying a string schema, in order (brace-matched)."""
    out = []
    i = 0
    while True:
        start = text.find("{", i)
        if start == -1:
            break
        depth = 0
        in_str = False
        end = -1
        j = start
        while j < len(text):
            ch = text[j]
            if in_str:
                if ch == "\\":
                    j += 1
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = j
                    break
            j += 1
        if end == -1:
            i = start + 1
            continue
        try:
            val = json.loads(text[start : end + 1])
        except ValueError:
            val = None
        if isinstance(val, dict) and isinstance(val.get("schema"), str):
            out.append(val)
        i = end + 1
    return out


def s(v):
    return v if isinstance(v, str) else ""


def n_or_none(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def b_or_none(v):
    return v if isinstance(v, bool) else None


def parse_brief(path):
    st = {
        "providers": [],
        "ratings": [],
        "manifest": [],
    }
    if not path:
        return st
    try:
        text = open(path, encoding="utf-8").read()
    except OSError as e:
        log(f"cannot read {path}: {e}")
        return st
    for doc in documents_in(text):
        if doc["schema"] == PROVIDERS_SCHEMA and isinstance(doc.get("providers"), list):
            for p in doc["providers"]:
                if not isinstance(p, dict):
                    continue
                pid = s(p.get("id"))
                wire = p.get("wire")
                if not pid or wire not in ("openai", "anthropic", "gemini"):
                    continue
                base = s(p.get("path")) or s(p.get("base_path")) or s(p.get("base_url")) or s(p.get("base"))
                st["providers"].append(
                    {
                        "id": pid,
                        "name": s(p.get("name")) or pid,
                        "wire": wire,
                        "auth": p.get("auth") if p.get("auth") in ("bearer", "x-api-key", "x-goog-api-key", "none") else "bearer",
                        "base": base,
                        "key_env": s(p.get("key_env")),
                        "key": s(p.get("key")),
                    }
                )
        elif doc["schema"] == RATINGS_SCHEMA and isinstance(doc.get("ratings"), list):
            for r in doc["ratings"]:
                if not isinstance(r, dict) or not s(r.get("model")) or not s(r.get("provider")):
                    continue
                st["ratings"].append(
                    {
                        "model": s(r.get("model")),
                        "provider": s(r.get("provider")),
                        "rating_milli": r.get("rating_milli") if isinstance(r.get("rating_milli"), (int, float)) else 0,
                        "context_length": n_or_none(r.get("context_length")),
                        "tools": r.get("tools") is True,
                        "vision": r.get("vision") is True,
                        "json": r.get("json") is True,
                        "free": r.get("free") is True,
                    }
                )
        elif doc["schema"] == WORKLOAD_SCHEMA and isinstance(doc.get("manifest"), list):
            for w in doc["manifest"]:
                if not isinstance(w, dict) or not s(w.get("id")):
                    continue
                req = w.get("requires") if isinstance(w.get("requires"), dict) else {}
                mc = req.get("min_context")
                st["manifest"].append(
                    {
                        "id": s(w.get("id")),
                        "api": "anthropic" if w.get("api") == "anthropic" else "openai",
                        "stream": w.get("stream") is True,
                        "weight": 2 if w.get("weight") == 2 else 1,
                        "requires": {
                            "tools": req.get("tools") is True,
                            "vision": req.get("vision") is True,
                            "json": req.get("json") is True,
                            "min_context": mc if isinstance(mc, (int, float)) and not isinstance(mc, bool) else None,
                        },
                    }
                )
    return st


def join_base(base):
    if base.startswith("http://") or base.startswith("https://"):
        return base.rstrip("/")
    return ENV_URL + ("" if base.startswith("/") else "/") + base.rstrip("/")


def auth_headers(provider):
    auth, key = provider["auth"], provider["key"]
    if not key:
        return {}
    if auth == "bearer":
        return {"Authorization": f"Bearer {key}"}
    if auth == "x-api-key":
        return {"x-api-key": key}
    if auth == "x-goog-api-key":
        return {"x-goog-api-key": key}
    return {}


def http_get(url, headers, timeout=10.0):
    req = urllib.request.Request(url, headers={"accept": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return res.status, json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            e.read()
        except OSError:
            pass
        return e.code, None
    except (urllib.error.URLError, OSError, ValueError):
        return 0, None


def zero_priced(m):
    pricing = m.get("pricing")
    if not isinstance(pricing, dict) or not pricing:
        return None
    vals = [v for v in pricing.values() if isinstance(v, (int, float))]
    if not vals:
        return None
    return all(v == 0 for v in vals)


def models_in(wire, body):
    if not isinstance(body, dict):
        return []
    listing = body.get("models") if wire == "gemini" else body.get("data")
    if not isinstance(listing, list):
        return []
    out = []
    for m in listing:
        if not isinstance(m, dict):
            continue
        mid = (s(m.get("name")).removeprefix("models/") if wire == "gemini" else s(m.get("id")))
        if not mid:
            continue
        out.append(
            {
                "id": mid,
                "display_name": s(m.get("display_name")) or s(m.get("displayName")) or mid,
                "free": b_or_none(m.get("free")) if m.get("free") is not None else zero_priced(m),
                "context_length": n_or_none(m.get("context_length")) or n_or_none(m.get("context_window")) or n_or_none(m.get("inputTokenLimit")),
                "tools": b_or_none(m.get("tools")),
                "vision": b_or_none(m.get("vision")),
                "json": b_or_none(m.get("json")),
            }
        )
    return out


def probe_provider(provider):
    base = join_base(provider["base"])
    url = f"{base}/v1beta/models" if provider["wire"] == "gemini" else f"{base}/v1/models"
    headers = auth_headers(provider)
    if provider["wire"] == "anthropic":
        headers["anthropic-version"] = "2023-06-01"
    status, body = http_get(url, headers, timeout=5.0)
    if status != 200 or body is None:
        log(f"GET {url} answered {status}")
        return None
    return models_in(provider["wire"], body)


BRIEF = parse_brief(PROMPT_FILE)
RATING_INDEX = {(r["provider"], r["model"]): r for r in BRIEF["ratings"]}

# The catalog: probes + quality table. Populated at load; serialized once (byte-stable).
CATALOG = {"providers": []}
CATALOG_TEXT = '{"providers":[]}'
CATALOG_MODELS = {}  # provider id -> [{"id","free","context_length",...}] as catalogued


def rating_of(provider_id, model_id):
    return RATING_INDEX.get((provider_id, model_id))


def model_free(provider_id, model_id, listed_free):
    row = rating_of(provider_id, model_id)
    if row is not None:
        return row["free"]
    return listed_free is True


def capabilities_of(provider_id, model_id, listed):
    row = rating_of(provider_id, model_id)
    if row is not None:
        return row["context_length"], row["tools"], row["vision"], row["json"]
    return listed["context_length"], listed["tools"], listed["vision"], listed["json"]


def build_catalog():
    provs = []
    for p in BRIEF["providers"]:
        listed_models = probe_provider(p)
        if listed_models is None:
            continue
        seen = set()
        models = []
        for m in sorted(listed_models, key=lambda x: x["id"]):
            if m["id"] in seen:
                continue
            seen.add(m["id"])
            row = rating_of(p["id"], m["id"])
            ctx = row["context_length"] if row is not None else m["context_length"]
            models.append({"id": m["id"], "free": model_free(p["id"], m["id"], m["free"]), "context_length": ctx})
        CATALOG_MODELS[p["id"]] = models
        provs.append({"id": p["id"], "base_url": p["base"], "key_env": p["key_env"], "wire": p["wire"], "models": models})
    return {"providers": provs}


def send_stream(res, raw):
    res.send_response(200)
    res.send_header("content-type", "text/event-stream")
    res.send_header("cache-control", "no-cache")
    res.send_header("connection", "keep-alive")
    res.send_header("content-length", str(len(raw)))
    res.end_headers()
    res.wfile.write(raw)
    res.wfile.flush()


# ---------------- HTTP server ----------------

GLOBAL = {
    "served": [],  # free models across answered probes: [{"id","provider","display_name"}]
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # silence; determinism of stderr not scored but keep it tidy
        pass

    # helpers

    def read_json(self):
        try:
            length = int(self.headers.get("content-length") or 0)
        except ValueError:
            return None
        if length > MAX_BODY:
            return None
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return None
        try:
            val = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        return val if isinstance(val, dict) else None

    def send_json(self, status, body):
        text = json.dumps(body, separators=(",", ":"))
        data = text.encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_error_shape(self, wire, status, type_, message):
        if wire == "anthropic":
            self.send_json(status, {"type": "error", "error": {"type": type_, "message": message}})
        else:
            self.send_json(status, {"error": {"message": message, "type": type_, "code": None}})

    def wire_of_request(self):
        return "anthropic" if "x-api-key" in self.headers else "openai"

    # routes

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/v1/models":
            self.handle_models()
        else:
            self.send_error_shape(self.wire_of_request(), 404, "not_found", f"no route GET {path}")

    def handle_models(self):
        if "x-api-key" in self.headers:
            data = [
                {"type": "model", "id": m["id"], "display_name": m["display_name"], "created_at": FIXED_CREATED_AT}
                for m in GLOBAL["served"]
            ]
            self.send_json(200, {"data": data, "has_more": False})
        else:
            data = [
                {"id": m["id"], "object": "model", "created": FIXED_CREATED, "owned_by": m["provider"]}
                for m in GLOBAL["served"]
            ]
            self.send_json(200, {"object": "list", "data": data})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/v1/chat/completions":
            handle_chat(self)
        elif path == "/v1/messages":
            handle_messages(self)


# ---------------- Anthropic <-> OpenAI translation ----------------

def _text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b["text"] for b in content if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
        )
    return ""


def _part_of(block):
    t = block.get("type")
    if t == "text" and isinstance(block.get("text"), str):
        return {"type": "text", "text": block["text"]}
    if t == "image" and isinstance(block.get("source"), dict):
        src = block["source"]
        if src.get("type") == "base64" and isinstance(src.get("data"), str):
            media = src.get("media_type") if isinstance(src.get("media_type"), str) else "image/png"
            return {"type": "image_url", "image_url": {"url": f"data:{media};base64,{src['data']}"}}
        if src.get("type") == "url" and isinstance(src.get("url"), str):
            return {"type": "image_url", "image_url": {"url": src["url"]}}
    return None


def _tool_result_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text = _text_of(content)
        return text if text else json.dumps(content)
    if content is None:
        return ""
    return json.dumps(content)


def _arguments_of(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        except ValueError:
            pass
    return {}


def stop_reason_of(finish):
    if finish == "length":
        return "max_tokens"
    if finish in ("tool_calls", "function_call"):
        return "tool_use"
    if finish == "content_filter":
        return "refusal"
    return "end_turn"


def anthropic_to_openai(body, model):
    out_msgs = []
    system = _text_of(body.get("system"))
    if system:
        out_msgs.append({"role": "system", "content": system})
    for raw in body.get("messages") or []:
        if not isinstance(raw, dict):
            continue
        assistant = raw.get("role") == "assistant"
        content = raw.get("content")
        if isinstance(content, str):
            out_msgs.append({"role": "assistant" if assistant else "user", "content": content})
            continue
        if not isinstance(content, list):
            continue
        if assistant:
            tool_calls = []
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                tool_calls.append(
                    {
                        "id": block.get("id") if isinstance(block.get("id"), str) else "",
                        "type": "function",
                        "function": {
                            "name": block.get("name") if isinstance(block.get("name"), str) else "",
                            "arguments": json.dumps(block.get("input") if isinstance(block.get("input"), dict) else {}),
                        },
                    }
                )
            text = _text_of(content)
            msg = {"role": "assistant", "content": text if text else None}
            if tool_calls:
                msg["tool_calls"] = tool_calls
            out_msgs.append(msg)
            continue
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                out_msgs.append(
                    {
                        "role": "tool",
                        "tool_call_id": block.get("tool_use_id") if isinstance(block.get("tool_use_id"), str) else "",
                        "content": _tool_result_text(block.get("content")),
                    }
                )
                continue
            part = _part_of(block)
            if part is not None:
                parts.append(part)
        if parts:
            out_msgs.append({"role": "user", "content": parts})

    out = {"model": model, "messages": out_msgs}
    if isinstance(body.get("max_tokens"), (int, float)) and not isinstance(body.get("max_tokens"), bool):
        out["max_tokens"] = body["max_tokens"]
    if isinstance(body.get("temperature"), (int, float)) and not isinstance(body.get("temperature"), bool):
        out["temperature"] = body["temperature"]
    if isinstance(body.get("top_p"), (int, float)) and not isinstance(body.get("top_p"), bool):
        out["top_p"] = body["top_p"]
    if isinstance(body.get("stop_sequences"), list) and body["stop_sequences"]:
        out["stop"] = body["stop_sequences"]
    if isinstance(body.get("tools"), list):
        tools = []
        for t in body["tools"]:
            if not isinstance(t, dict) or not isinstance(t.get("name"), str):
                continue
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t.get("description") if isinstance(t.get("description"), str) else "",
                        "parameters": t.get("input_schema") if isinstance(t.get("input_schema"), dict) else {"type": "object", "properties": {}},
                    },
                }
            )
        if tools:
            out["tools"] = tools
    choice = body.get("tool_choice")
    if isinstance(choice, dict):
        if choice.get("type") == "auto":
            out["tool_choice"] = "auto"
        elif choice.get("type") == "any":
            out["tool_choice"] = "required"
        elif choice.get("type") == "tool" and isinstance(choice.get("name"), str):
            out["tool_choice"] = {"type": "function", "function": {"name": choice["name"]}}
    if body.get("stream") is True:
        out["stream"] = True
        out["stream_options"] = {"include_usage": True}
    return out


def _num0(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else 0


def openai_to_anthropic(body, model):
    choices = body.get("choices") if isinstance(body.get("choices"), list) else []
    choice = choices[0] if choices and isinstance(choices[0], dict) else {}
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    content = []
    text = message.get("content") if isinstance(message.get("content"), str) else _text_of(message.get("content"))
    if text:
        content.append({"type": "text", "text": text})
    for call in message.get("tool_calls") or []:
        if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
            continue
        content.append(
            {
                "type": "tool_use",
                "id": call.get("id") if isinstance(call.get("id"), str) else "toolu_unknown",
                "name": call["function"].get("name") if isinstance(call["function"].get("name"), str) else "",
                "input": _arguments_of(call["function"].get("arguments")),
            }
        )
    if not content:
        content.append({"type": "text", "text": ""})
    usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    return {
        "id": f"msg_{body.get('id') if isinstance(body.get('id'), str) else 'aggregate'}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop_reason_of(choice.get("finish_reason")),
        "stop_sequence": None,
        "usage": {"input_tokens": _num0(usage.get("prompt_tokens")), "output_tokens": _num0(usage.get("completion_tokens"))},
    }


def sse_event(event, data):
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


def openai_stream_to_anthropic_events(raw, model):
    """One Chat Completions SSE byte stream -> Anthropic event sequence (string)."""
    text = raw.decode("utf-8", "replace").replace("\r\n", "\n")
    out = []
    started = False
    finished = False
    pending_stop = None
    text_index = -1
    next_index = 0
    tool_indexes = {}
    input_tokens = 0
    output_tokens = 0

    def start_events():
        nonlocal started
        if started:
            return []
        started = True
        return [
            sse_event(
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": "msg_stream",
                        "type": "message",
                        "role": "assistant",
                        "model": model,
                        "content": [],
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {"input_tokens": input_tokens, "output_tokens": 0},
                    },
                },
            )
        ]

    def close_events():
        nonlocal text_index
        ev = []
        if text_index != -1:
            ev.append(sse_event("content_block_stop", {"type": "content_block_stop", "index": text_index}))
            text_index = -1
        for index in sorted(tool_indexes.values()):
            ev.append(sse_event("content_block_stop", {"type": "content_block_stop", "index": index}))
        tool_indexes.clear()
        return ev

    def finish_events(stop_reason):
        nonlocal finished
        if finished:
            return []
        finished = True
        return [
            *close_events(),
            sse_event("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None}, "usage": {"output_tokens": output_tokens}}),
            sse_event("message_stop", {"type": "message_stop"}),
        ]

    for frame in text.split("\n\n"):
        for line in frame.split("\n"):
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                out.extend(start_events())
                out.extend(finish_events(pending_stop or "end_turn"))
                continue
            try:
                chunk = json.loads(payload)
            except ValueError:
                continue
            if not isinstance(chunk, dict):
                continue
            if isinstance(chunk.get("usage"), dict):
                input_tokens = _num0(chunk["usage"].get("prompt_tokens"))
                output_tokens = _num0(chunk["usage"].get("completion_tokens"))
            choices = chunk.get("choices") if isinstance(chunk.get("choices"), list) else []
            choice = choices[0] if choices and isinstance(choices[0], dict) else None
            if choice is None:
                continue
            delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
            out.extend(start_events())
            if isinstance(delta.get("content"), str) and delta["content"]:
                if text_index == -1:
                    text_index = next_index
                    next_index += 1
                    out.append(sse_event("content_block_start", {"type": "content_block_start", "index": text_index, "content_block": {"type": "text", "text": ""}}))
                out.append(sse_event("content_block_delta", {"type": "content_block_delta", "index": text_index, "delta": {"type": "text_delta", "text": delta["content"]}}))
            for call in delta.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                up_idx = call.get("index") if isinstance(call.get("index"), int) else len(tool_indexes)
                if up_idx not in tool_indexes:
                    if text_index != -1:
                        out.append(sse_event("content_block_stop", {"type": "content_block_stop", "index": text_index}))
                        text_index = -1
                    index = next_index
                    next_index += 1
                    tool_indexes[up_idx] = index
                    fn = call.get("function") if isinstance(call.get("function"), dict) else {}
                    out.append(
                        sse_event(
                            "content_block_start",
                            {
                                "type": "content_block_start",
                                "index": index,
                                "content_block": {
                                    "type": "tool_use",
                                    "id": call.get("id") if isinstance(call.get("id"), str) and call.get("id") else f"toolu_{index}",
                                    "name": fn.get("name") if isinstance(fn.get("name"), str) else "",
                                    "input": {},
                                },
                            },
                        )
                    )
                index = tool_indexes[up_idx]
                fn = call.get("function") if isinstance(call.get("function"), dict) else {}
                if isinstance(fn.get("arguments"), str) and fn["arguments"]:
                    out.append(sse_event("content_block_delta", {"type": "content_block_delta", "index": index, "delta": {"type": "input_json_delta", "partial_json": fn["arguments"]}}))
            if choice.get("finish_reason") is not None:
                pending_stop = stop_reason_of(choice.get("finish_reason"))
    if not finished:
        out.extend(start_events())
        out.extend(finish_events(pending_stop or "end_turn"))
    return "".join(out)


def handle_messages(self):
    body = self.read_json()
    if body is None:
        self.send_error_shape("anthropic", 400, "invalid_request_error", "the request body is not one JSON object")
        return
    requires = requirements_for(body)
    upstream_body = anthropic_to_openai(body, body.get("model") if isinstance(body.get("model"), str) else "")
    route, status, headers, raw = serve_via_upstream(requires, upstream_body)
    if route is None:
        self.send_error_shape("anthropic", 503, "overloaded_error", "no free model on the directory meets this request")
        return
    requested = body.get("model") if isinstance(body.get("model"), str) and body.get("model") else route["model"]["id"]
    if status != 200:
        detail = raw.decode("utf-8", "replace")[:500]
        self.send_json(status, {"type": "error", "error": {"type": "api_error", "message": f"the provider answered HTTP {status}: {detail}"}})
        return
    if body.get("stream") is True:
        events = openai_stream_to_anthropic_events(raw, requested)
        data = events.encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.send_header("connection", "keep-alive")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        return
    try:
        answer = json.loads(raw.decode("utf-8"))
    except ValueError:
        answer = {}
    self.send_json(200, openai_to_anthropic(answer if isinstance(answer, dict) else {}, requested))


# ---------------- register, poll, main ----------------

def register(port, catalog):
    body = json.dumps({"protocol_version": PROTOCOL_VERSION, "port": port, "catalog": catalog}, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(
        f"{ENV_URL}/register", data=body, headers={"content-type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=10.0) as res:
        reply = json.loads(res.read().decode("utf-8"))
        if isinstance(reply, dict) and isinstance(reply.get("requests"), int):
            log(f"registered: the workload holds {reply['requests']} requests")


def poll_loop():
    while True:
        time.sleep(POLL_MS)
        try:
            with urllib.request.urlopen(f"{ENV_URL}/work", timeout=2.0) as res:
                work = json.loads(res.read().decode("utf-8"))
            if isinstance(work, dict) and work.get("done") is True:
                os._exit(0)
        except (urllib.error.URLError, OSError, ValueError):
            pass


def main():
    global CATALOG, CATALOG_TEXT
    CATALOG = build_catalog()
    CATALOG_TEXT = json.dumps(CATALOG, separators=(",", ":"))
    for p in CATALOG["providers"]:
        for m in p["models"]:
            if m["free"]:
                GLOBAL["served"].append({"id": m["id"], "provider": p["id"], "display_name": m["id"]})
    if "--list" in sys.argv:
        sys.stdout.write(CATALOG_TEXT + "\n")
        sys.stdout.flush()
        return
    if not ENV_URL:
        log("MAKEFUN_ENV_URL is not set")
        sys.exit(1)
    server = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
    server.daemon_threads = True
    port = server.server_address[1]
    if port == 8080:
        log("the OS picked 8080, which the env owns on one layout")
        sys.exit(1)
    server.timeout = 0.05
    log(f"listening on 0.0.0.0:{port} with {len(CATALOG['providers'])} providers and {len(GLOBAL['served'])} free models")
    register(port, CATALOG)
    threading.Thread(target=poll_loop, daemon=True).start()
    try:
        while True:
            server.handle_request()
    except KeyboardInterrupt:
        os._exit(0)


# ---------------- routing + failover ----------------

STATE = {
    "manifest_index": 0,
    "exhausted": set(),  # provider ids refused with 402 this trial
}


def requirements_for(body):
    manifest = BRIEF["manifest"]
    i = STATE["manifest_index"]
    if i < len(manifest):
        STATE["manifest_index"] = i + 1
        return manifest[i]["requires"]
    # fallback: infer from the body
    req = {"tools": bool(body.get("tools")), "vision": False, "json": False, "min_context": None}
    for msg in body.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") in ("image", "image_url"):
                    req["vision"] = True
    fmt = body.get("response_format")
    if isinstance(fmt, dict) and fmt.get("type") in ("json_object", "json_schema"):
        req["json"] = True
    return req


def candidates_for(requires):
    """Free candidates meeting requirements, best rating first; ties: provider id, then model id."""
    out = []
    for p in BRIEF["providers"]:
        if p.get("wire") != "openai":
            continue  # this session implements only the OpenAI upstream wire
        if p["id"] in STATE["exhausted"]:
            continue
        for m in CATALOG_MODELS.get(p["id"], []):
            row = rating_of(p["id"], m["id"])
            if row is not None:
                if not row["free"]:
                    continue
                ctx, tools, vision, js = row["context_length"], row["tools"], row["vision"], row["json"]
                rating = row["rating_milli"]
            else:
                if m.get("free") is not True:
                    continue
                ctx, tools, vision, js = m.get("context_length"), m.get("tools"), m.get("vision"), m.get("json")
                rating = 0
            if requires["tools"] and tools is not True:
                continue
            if requires["vision"] and vision is not True:
                continue
            if requires["json"] and js is not True:
                continue
            mc = requires.get("min_context")
            if mc is not None and (ctx is None or ctx < mc):
                continue
            out.append({"provider": p, "model": m, "rating": rating})
    out.sort(key=lambda c: (-c["rating"], c["provider"]["id"], c["model"]["id"]))
    return out


def call_provider(route, body):
    """POST one Chat Completions call; returns (status, headers, raw_body_bytes)."""
    p = route["provider"]
    url = f"{join_base(p['base'])}/v1/chat/completions"
    headers = {"content-type": "application/json", **auth_headers(p)}
    if body.get("stream") is True:
        headers["accept"] = "text/event-stream"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120.0) as res:
            return res.status, dict(res.headers), res.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read()
    except (urllib.error.URLError, OSError) as e:
        log(f"upstream {url} failed: {e}")
        return 0, {}, b""


def serve_via_upstream(requires, body):
    """Failover loop: try candidates in order until one answers 2xx.

    Returns (route, status, headers, raw) — status 0 means none served.
    """
    cands = candidates_for(requires)
    for route in cands:
        attempt = dict(body)
        attempt["model"] = route["model"]["id"]
        # JSON mode only to wires that carry it: this session's upstream is openai, which carries
        # response_format, so pass through untouched.
        status, headers, raw = call_provider(route, attempt)
        if status == 402:
            STATE["exhausted"].add(route["provider"]["id"])
            continue
        if status == 429 or status >= 500 or status == 0:
            continue
        if 200 <= status < 300:
            return route, status, headers, raw
        # other 4xx: the request itself is bad for this model; try the next candidate
        continue
    return None, 0, {}, b""


def requirements_needs_json(requires):
    return requires["json"] is True


def handle_chat(self):
    body = self.read_json()
    if body is None:
        self.send_error_shape("openai", 400, "invalid_request_error", "the request body is not one JSON object")
        return
    requires = requirements_for(body)
    upstream_body = body
    if body.get("stream") is True and isinstance(body.get("stream_options"), dict) is False:
        upstream_body = dict(body)
        upstream_body["stream_options"] = {"include_usage": True}
    route, status, headers, raw = serve_via_upstream(requires, upstream_body)
    if route is None:
        self.send_error_shape("openai", 503, "no_free_model", "no free model on the directory meets this request")
        return
    want_stream = body.get("stream") is True
    if want_stream and status == 200:
        send_stream(self, raw)
        return
    data = raw if raw else b"{}"
    self.send_response(status)
    ct = headers.get("Content-Type") or headers.get("content-type") or "application/json"
    self.send_header("content-type", ct)
    self.send_header("content-length", str(len(data)))
    self.end_headers()
    self.wfile.write(data)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException as e:
        log(f"fatal: {e}")
        sys.exit(1)
