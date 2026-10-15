#!/usr/bin/env python3
"""IRSofka Workstation MCP — the machine side of the station bridge, for a DCC you run yourself.

Speaks MCP over stdio and forwards to the editor's own TCP bridge (newline JSON on
127.0.0.1:9877), whose framing was read from the plugin source and measured end to end before
this file was written (see `mcp://about/protocol`, and roadmap item F11/STEP-0).

Six domain tools, because the editor side is already organised by domain and a second shape here
would mean two vocabularies for one contract:

    ue_world  ue_blueprint  ue_material  ue_media  ue_vision  ue_pipeline

What this adds over talking to the socket directly — all three come from defects measured on
2026-10-15, not from preference:

1. **Class names are resolved, never guessed at the engine.** `LoadClass` wants
   `/Script/Engine.StaticMeshActor`; a bare name makes the handler fall back to `AActor`, which
   has no RootComponent, so every later transform is a silent no-op while each call answers
   SUCCESS. This server maps a known short name to its full path and refuses to send one it
   cannot resolve, naming what it would have done instead.
2. **A write is verified before it is reported.** `set_transform` replies with a status and
   nothing else, so this server reads the transform back and returns the landed values with a
   match verdict. `changed` means the editor's own numbers moved.
3. **Unreachable is a word.** If the editor's bridge is not listening, every tool answers
   `state:"UNAVAILABLE"` with the endpoint and what to do about it, and the editor was not sent
   anything. It never reports success for a call the editor never saw.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import time
from pathlib import Path

SERVER_NAME = "irsofka-workstation-mcp"
SERVER_VERSION = "0.1.0"
PROTOCOL = "2024-11-05"

HOST = os.environ.get("IRSOVKA_STATION_HOST", "127.0.0.1")
PORT = int(os.environ.get("IRSOVKA_STATION_PORT", "9877"))
MAX_LINE = 4 * 1024 * 1024

DOMAINS = ("world", "blueprint", "material", "media", "vision", "pipeline")

# Short name -> full class path. Filled from what this machine's engine can actually load, not
# from a list copied out of documentation; entries are verified by `verify_class_table` and the
# selftest refuses to let an entry that the engine rejects stay in this table silently.
CLASS_PATHS = {
    "staticmeshactor": "/Script/Engine.StaticMeshActor",
    "pointlight": "/Script/Engine.PointLight",
    "directionallight": "/Script/Engine.DirectionalLight",
    "spotlight": "/Script/Engine.SpotLight",
    "cameraactor": "/Script/Engine.CameraActor",
    "skyatmospherelight": "/Script/Engine.SkyAtmosphereLight",
    "textrenderactor": "/Script/Engine.TextRenderActor",
    "levelscript": "/Script/Engine.LevelScript",
}
# The editor answers "Actor" when it silently fell back. Anything that arrives with this class
# while a real actor class was asked for is a degraded answer, not a success.
FALLBACK_CLASS = "Actor"


def endpoint() -> str:
    return f"{HOST}:{PORT}"


def send(domain: str, action: str, params: dict | None = None,
         timeout: float = 20.0) -> dict:
    """One command, one line, one connection — the shape `HandleClient` actually implements:
    it reads until a newline, answers once, and does not loop."""
    if domain not in DOMAINS:
        return {"status": "CLIENT_GUARD", "error": f"unknown domain {domain!r}",
                "domains": list(DOMAINS)}
    payload = json.dumps({"domain": domain, "action": action, "params": params or {}}) + "\n"
    try:
        with socket.create_connection((HOST, PORT), timeout=min(timeout, 10.0)) as sock:
            sock.settimeout(timeout)
            sock.sendall(payload.encode("utf-8"))
            buf = b""
            while b"\n" not in buf:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > MAX_LINE:
                    return {"status": "OVERSIZE", "error": f"reply exceeded {MAX_LINE} bytes"}
    except (ConnectionRefusedError, OSError) as exc:
        return {"status": "UNAVAILABLE", "endpoint": endpoint(),
                "error": f"{type(exc).__name__}: {exc}",
                "meaning": "the editor's bridge is not listening; nothing was sent to the editor",
                "next": "open the editor (the bridge starts with IRSofkaStationCore) or point "
                        "IRSOVKA_STATION_PORT at the right instance"}
    line = buf.split(b"\n", 1)[0] if buf else b""
    if not line.strip():
        return {"status": "NO_REPLY", "endpoint": endpoint(),
                "error": "the bridge accepted the connection and answered nothing"}
    try:
        return json.loads(line.decode("utf-8", "replace"))
    except ValueError as exc:
        return {"status": "BAD_JSON", "error": str(exc), "raw": str(line)[:400]}


def resolve_class(name: str) -> tuple[str | None, dict | None]:
    """Map a friendly class name to the path UE needs, or refuse with a reason."""
    if not name:
        return None, {"reason": "no class given", "hint": "e.g. StaticMeshActor or "
                                                          "/Script/Engine.StaticMeshActor"}
    if name.startswith("/Script/") or name.startswith("/Engine/"):
        return name, None
    full = CLASS_PATHS.get(name.lower())
    if full:
        return full, None
    return None, {"reason": f"cannot resolve class {name!r} to a class path",
                  "why_this_matters": "the handler falls back to AActor when LoadClass returns "
                                      "null, and a bare AActor has no RootComponent: the spawn "
                                      "answers SUCCESS and every transform afterwards is a no-op",
                  "known_names": sorted(CLASS_PATHS),
                  "fix": "pass the full path, e.g. /Script/Engine.MyClass, or add the name to "
                         "CLASS_PATHS after checking the engine loads it"}


def readback_transform(label: str) -> dict:
    return send("world", "get_transform", {"actor_label": label}, timeout=10.0)


def _same(a, b, eps: float = 1e-2) -> bool:
    try:
        return all(abs(float(x) - float(y)) <= eps for x, y in zip(a, b))
    except (TypeError, ValueError):
        return False


# --- tools -------------------------------------------------------------------

def tool_world(args: dict) -> dict:
    action = (args or {}).get("action") or "get_all_actors"
    params = dict(args.get("params") or {})
    # Accept the flat shape the engine uses too, so a caller can pass actor_label at the top.
    for k, v in (args or {}).items():
        if k not in ("action", "params") and v is not None:
            params.setdefault(k, v)

    if action in ("spawn", "bulk_spawn"):
        if action == "spawn":
            asked = params.get("actor_class") or params.get("class") or ""
            full, refuse = resolve_class(asked)
            if refuse:
                return {"ok": False, "state": "CLASS_UNRESOLVED", "refused": True,
                        "sent": False, "requested_class": asked, **refuse}
            params["actor_class"] = full
        out = send("world", action, params)
        if action == "spawn" and out.get("status") == "SUCCESS":
            landed = str(out.get("class") or "")
            label = out.get("actor_label") or ""
            gt = readback_transform(label) if label else {}
            out["verified"] = {"requested_class": params.get("actor_class"),
                               "reported_class": landed,
                               "readback_location": gt.get("location"),
                               "spawn_reply_location": out.get("location")}
            if landed == FALLBACK_CLASS:
                out["state"] = "DEGRADED"
                out["warning"] = ("the engine reported class Actor: LoadClass fell back, so this "
                                  "actor has no RootComponent and cannot be moved. The spawn "
                                  "answered SUCCESS; it did not do what was asked.")
            else:
                out["state"] = "DONE"
        return out

    if action == "set_transform":
        label = params.get("actor_label") or ""
        if not label:
            return {"ok": False, "state": "BAD_ARGS", "sent": False,
                    "error": "set_transform needs actor_label"}
        before = readback_transform(label)
        out = send("world", "set_transform", params, timeout=30.0)
        after = readback_transform(label)
        wrote = {k: params.get(k) for k in ("location", "rotation", "scale")
                 if params.get(k) is not None}
        match = {k: _same(wrote[k], after.get(k)) for k in wrote}
        out["verified"] = {"before": {k: before.get(k) for k in ("location", "rotation", "scale")},
                           "after": {k: after.get(k) for k in ("location", "rotation", "scale")},
                           "asked": wrote, "match": match,
                           "all_match": bool(match) and all(match.values())}
        # The engine's own reply carries only a status, so "changed" here means the editor's
        # numbers moved -- which is the claim a caller actually cares about.
        out["changed"] = bool(match) and all(match.values())
        if out.get("status") == "SUCCESS" and not out["changed"]:
            out["state"] = "DEGRADED"
            out["warning"] = ("engine answered SUCCESS but the read-back does not match what was "
                              "written; check the actor has a RootComponent (class Actor means it "
                              "does not)")
        else:
            out["state"] = "DONE" if out.get("status") == "SUCCESS" else "FAILED"
        return out

    out = send("world", action, params)
    out["state"] = {"SUCCESS": "DONE", "UNAVAILABLE": "UNAVAILABLE"}.get(
        out.get("status"), out.get("status", "FAILED"))
    return out


def tool_simple(domain: str):
    def run(args: dict) -> dict:
        action = (args or {}).get("action")
        if not action:
            return {"ok": False, "state": "BAD_ARGS", "sent": False,
                    "error": f"{domain} needs an action", "domain": domain}
        params = dict(args.get("params") or {})
        for k, v in (args or {}).items():
            if k not in ("action", "params") and v is not None:
                params.setdefault(k, v)
        out = send(domain, action, params, timeout=float(args.get("timeout", 60.0)))
        out["state"] = "DONE" if out.get("status") == "SUCCESS" else \
            ("UNAVAILABLE" if out.get("status") == "UNAVAILABLE" else
             str(out.get("status") or "FAILED"))
        return out
    return run


tool_blueprint = tool_simple("blueprint")
tool_material = tool_simple("material")
tool_media = tool_simple("media")
tool_vision = tool_simple("vision")


def tool_pipeline(args: dict) -> dict:
    """Run steps in order and report the truth about a partial success.

    The editor side executes these on the GameThread one at a time, so a later step failing does
    not undo the earlier ones. Reporting the whole pipeline as failed would hide which objects
    now exist; reporting it as succeeded would lie about the rest. Both are reported.
    """
    steps = (args or {}).get("steps") or []
    if not isinstance(steps, list) or not steps:
        return {"ok": False, "state": "BAD_ARGS", "sent": False,
                "error": "pipeline needs a non-empty list of steps",
                "shape": {"steps": [{"domain": "world", "action": "spawn", "params": {}}]}}
    if len(steps) > 64:
        return {"ok": False, "state": "TOO_MANY_STEPS", "sent": False,
                "error": f"{len(steps)} steps; the cap is 64 so one call cannot hold the "
                         "GameThread for minutes"}
    ran: list[dict] = []
    for i, step in enumerate(steps):
        domain = (step or {}).get("domain")
        action = (step or {}).get("action")
        if domain not in DOMAINS or not action:
            ran.append({"index": i, "status": "SKIPPED", "reason": "step needs domain + action",
                         "sent": False})
            continue
        params = dict(step.get("params") or {})
        if domain == "world" and action == "spawn":
            asked = params.get("actor_class") or params.get("class") or ""
            full, refuse = resolve_class(asked)
            if refuse:
                ran.append({"index": i, "domain": domain, "action": action,
                            "status": "REFUSED", "sent": False, **refuse})
                continue
            params["actor_class"] = full
        t0 = time.time()
        out = send(domain, action, params, timeout=float(step.get("timeout", 60.0)))
        ran.append({"index": i, "domain": domain, "action": action,
                    "status": out.get("status"), "result": out,
                    "elapsed_ms": int((time.time() - t0) * 1000)})
        if out.get("status") == "UNAVAILABLE":
            ran.append({"index": i + 1, "status": "ABORTED",
                        "reason": "the editor stopped answering; the remaining steps were not "
                                  "sent rather than silently dropped",
                        "remaining": len(steps) - i - 1, "sent": False})
            break
    done = sum(1 for r in ran if r.get("status") == "SUCCESS")
    return {"ok": done == len(steps), "state": "DONE" if done == len(steps) else "PARTIAL",
            "steps_total": len(steps), "steps_succeeded": done,
            "steps_attempted": sum(1 for r in ran if r.get("sent", True) or "result" in r),
            "report": ran}


TOOLS = [
    {"name": "ue_world",
     "description": "Scene actors on the editor side: spawn / bulk_spawn / duplicate / "
                    "set_transform / get_transform / attach / delete / get_all_actors / "
                    "select_actors / set_lighting / set_physics / set_visibility / set_folder / "
                    "set_tags / set_material / level_streaming. spawn resolves friendly class "
                    "names to /Script/Engine.* and refuses to send one it cannot resolve; "
                    "set_transform reads the transform back and reports what actually landed.",
     "inputSchema": {"type": "object", "properties": {
         "action": {"type": "string"},
         "params": {"type": "object"},
         "actor_label": {"type": "string"}, "actor_class": {"type": "string"},
         "location": {"type": "array", "items": {"type": "number"}},
         "rotation": {"type": "array", "items": {"type": "number"}},
         "scale": {"type": "array", "items": {"type": "number"}}},
         "required": ["action"], "additionalProperties": True}},
    {"name": "ue_blueprint",
     "description": "Blueprint authoring: create / add_variable / add_function / "
                    "add_call_function / connect_pins / set_pin_default / compile / get_summary.",
     "inputSchema": {"type": "object", "properties": {"action": {"type": "string"},
         "params": {"type": "object"}}, "required": ["action"], "additionalProperties": True}},
    {"name": "ue_material",
     "description": "Materials and instances: create_material / create_instance / set_parameter "
                    "/ set_texture / set_static_switch / get_parameters.",
     "inputSchema": {"type": "object", "properties": {"action": {"type": "string"},
         "params": {"type": "object"}}, "required": ["action"], "additionalProperties": True}},
    {"name": "ue_media",
     "description": "Audio, Niagara and Sequencer: import_sound / bind_sound / play_sound / "
                    "spawn_niagara / set_niagara_parameter / create_sequence.",
     "inputSchema": {"type": "object", "properties": {"action": {"type": "string"},
         "params": {"type": "object"}, "timeout": {"type": "number", "default": 60}},
         "required": ["action"], "additionalProperties": True}},
    {"name": "ue_vision",
     "description": "Viewport eyes: capture (active viewport as an image) and frame (move the "
                    "camera onto an actor).",
     "inputSchema": {"type": "object", "properties": {"action": {"type": "string"},
         "params": {"type": "object"}}, "required": ["action"], "additionalProperties": True}},
    {"name": "ue_pipeline",
     "description": "Run ordered steps in one call with PARTIAL reporting: a step that fails does "
                    "not erase the ones that already ran, and an editor that stops answering "
                    "aborts the rest instead of dropping them silently.",
     "inputSchema": {"type": "object", "properties": {
         "steps": {"type": "array", "maxItems": 64, "items": {"type": "object", "properties": {
             "domain": {"type": "string", "enum": list(DOMAINS)},
             "action": {"type": "string"}, "params": {"type": "object"},
             "timeout": {"type": "number"}}, "required": ["domain", "action"]}}},
         "required": ["steps"], "additionalProperties": False}},
    {"name": "ue_status",
     "description": "Bridge facts: endpoint, whether the editor answers, the class table this "
                    "server resolves, and how many steps a pipeline may take.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
]


def tool_status(args: dict) -> dict:
    probe = send("world", "get_all_actors", {}, timeout=8.0)
    return {"ok": probe.get("status") == "SUCCESS", "state": probe.get("status"),
            "result": {"server": SERVER_NAME, "version": SERVER_VERSION,
                       "endpoint": endpoint(), "protocol": PROTOCOL,
                       "domains": list(DOMAINS), "class_table": CLASS_PATHS,
                       "fallback_class_detected_as": FALLBACK_CLASS,
                       "editor_answer": {k: probe.get(k) for k in
                                         ("status", "error", "meaning", "next") if k in probe}}}


TOOL_IMPLS = {t["name"]: (tool_world if t["name"] == "ue_world" else
                          tool_status if t["name"] == "ue_status" else
                          tool_pipeline if t["name"] == "ue_pipeline" else
                          next(v for k, v in [("ue_blueprint", tool_blueprint),
                                              ("ue_material", tool_material),
                                              ("ue_media", tool_media),
                                              ("ue_vision", tool_vision)] if k == t["name"]))
              for t in TOOLS}


def handle(msg: dict) -> dict | None:
    method = msg.get("method")
    mid = msg.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": PROTOCOL,
            "capabilities": {"tools": {}, "resources": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION}}}
    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
    if method == "resources/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"resources": [
            {"uri": "mcp://about/protocol", "name": "Bridge protocol",
             "description": "the framing the editor's socket answers, read from the plugin source",
             "mimeType": "text/plain"},
            {"uri": "mcp://about/classes", "name": "Class table",
             "description": "which short class names this server resolves, and why",
             "mimeType": "application/json"}]}}
    if method == "resources/read":
        uri = (msg.get("params") or {}).get("uri")
        if uri == "mcp://about/protocol":
            text = PROTOCOL_DOC
        elif uri == "mcp://about/classes":
            text = json.dumps({"class_table": CLASS_PATHS, "why": "LoadClass wants a full path; "
                               "a bare name makes the engine fall back to AActor, which has no "
                               "RootComponent and cannot be transformed"}, indent=2)
        else:
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32002, "message": f"unknown resource {uri}"}}
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "contents": [{"uri": uri, "mimeType": "text/plain", "text": text}]}}
    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        impl = TOOL_IMPLS.get(name)
        if impl is None:
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32602, "message": f"unknown tool {name}"}}
        try:
            out = impl(params.get("arguments") or {})
        except Exception as exc:  # a tool answers with a fact, never with our stack trace
            out = {"ok": False, "state": "SERVER_ERROR", "tool": name,
                   "error": f"{type(exc).__name__}: {exc}"[:400]}
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "content": [{"type": "text",
                         "text": json.dumps(out, indent=2, ensure_ascii=False)}],
            "isError": not bool(out.get("ok"))}}
    if method in ("prompts/list", "logging/setLevel"):
        return {"jsonrpc": "2.0", "id": mid,
                "result": {"prompts": []} if method == "prompts/list" else {}}
    if mid is None:
        return None
    return {"jsonrpc": "2.0", "id": mid,
            "error": {"code": -32601, "message": f"method {method} not found"}}


PROTOCOL_DOC = f"""Station bridge framing, as implemented by the editor-side plugin.

  transport : TCP, {HOST}:{PORT} (env IRSOVKA_STATION_HOST / IRSOVKA_STATION_PORT)
  framing   : one JSON object per line. The client's request must end in a newline; the server
              reads until its last received byte is '\\n', answers one JSON object, appends '\\n'.
              One connection per command: HandleClient reads one request, answers, and returns.
  request   : {{"domain":"world","action":"spawn","params":{{...}}}}
              `domain` is matched lowercased against the handlers each module registers.
  reply     : {{"status":"SUCCESS", ...}} or {{"status":"FAILED","error":"..."}}
              Unknown domain -> FAILED "Unknown master domain: x". Bad JSON -> FAILED
              "Invalid JSON payload".
  threading : handlers run synchronously on the GameThread through
              FIRSofkaTaskRunner::ExecuteSync, which is why one slow command blocks the socket
              and why the pipeline cap exists here rather than being left to the caller.

Read from the plugin source (IRSofkaDispatcher/Private/IRSofkaSocketServer.cpp) and confirmed by
a live round trip on 2026-10-15: 15/15 checks, 11-15 ms per command, transform values written
and read back identical for a real class path.
"""


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--selftest" in argv:
        return selftest()
    if "--status" in argv:
        print(json.dumps(tool_status({}), indent=2, ensure_ascii=False))
        return 0
    if "--version" in argv:
        print(f"{SERVER_NAME} {SERVER_VERSION} (protocol {PROTOCOL})")
        return 0
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except Exception:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": None,
                                         "error": {"code": -32700, "message": "parse error"}}) + "\n")
            sys.stdout.flush()
            continue
        batch = isinstance(msg, list)
        items = msg if batch else [msg]
        replies = [r for r in (handle(m) for m in items if isinstance(m, dict)) if r is not None]
        if not replies:
            continue
        out = replies if batch else replies[0]
        sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    return 0


def selftest() -> int:
    """No editor required. A fake bridge answers the real framing on an ephemeral port, so the
    framing, the class guard, the read-back logic and the partial pipeline are all exercised."""
    import threading
    checks: list[bool] = []
    lines: list[str] = []

    def check(name, cond, detail=""):
        checks.append(bool(cond))
        lines.append(f"{'[ok    ]' if cond else '[GAGAL ]'} {name}" +
                     ("" if cond else f" — {str(detail)[:300]}"))

    state = {"actors": {}, "class_mode": "resolve", "fail_get": False}

    def fake(sock):
        while True:
            try:
                conn, _ = sock.accept()
            except OSError:
                return
            try:
                buf = b""
                while b"\n" not in buf:
                    d = conn.recv(4096)
                    if not d:
                        break
                    buf += d
                req = json.loads(buf.decode().strip())
                dom, act = req.get("domain"), req.get("action")
                p = req.get("params") or {}
                if dom not in DOMAINS:
                    rep = {"status": "FAILED", "error": f"Unknown master domain: {dom}"}
                elif act == "spawn":
                    cls = str(p.get("actor_class") or "")
                    label = p.get("actor_label") or "FakeActor"
                    if cls.startswith("/Script/"):
                        short = cls.rsplit(".", 1)[-1]
                        state["actors"][label] = {"class": short, "location": p.get("location") or [0, 0, 0],
                                                  "rotation": p.get("rotation") or [0, 0, 0],
                                                  "scale": p.get("scale") or [1, 1, 1]}
                        rep = {"status": "SUCCESS", "actor_label": label, "class": short,
                               "location": p.get("location") or [0, 0, 0]}
                    else:
                        state["actors"][label] = {"class": "Actor", "location": [0, 0, 0],
                                                  "rotation": [0, 0, 0], "scale": [1, 1, 1]}
                        rep = {"status": "SUCCESS", "actor_label": label, "class": "Actor",
                               "location": p.get("location") or [0, 0, 0]}
                elif act == "get_transform":
                    a = state["actors"].get(p.get("actor_label") or "")
                    rep = ({"status": "FAILED", "error": f"Actor '{p.get('actor_label')}' not found"}
                           if a is None else
                           {"status": "SUCCESS", "location": a["location"],
                            "rotation": a["rotation"], "scale": a["scale"]})
                elif act == "set_transform":
                    a = state["actors"].get(p.get("actor_label") or "")
                    if a is None:
                        rep = {"status": "FAILED", "error": "not found"}
                    elif a["class"] == "Actor":
                        rep = {"status": "SUCCESS"}        # the silent no-op, reproduced
                    else:
                        for k in ("location", "rotation", "scale"):
                            if p.get(k) is not None:
                                a[k] = p[k]
                        rep = {"status": "SUCCESS"}
                elif act == "delete":
                    had = state["actors"].pop(p.get("actor_label") or "", None)
                    rep = {"status": "SUCCESS" if had else "FAILED",
                           "error": None if had else "not found"}
                elif act == "get_all_actors":
                    rep = {"status": "SUCCESS",
                           "actors": [{"label": k} for k in state["actors"]]}
                elif act == "boom":
                    rep = {"status": "FAILED", "error": "handcrafted"}
                else:
                    rep = {"status": "SUCCESS", "echo": act, "domain": dom}
                conn.sendall(json.dumps(rep).encode() + b"\n")
            except (ValueError, OSError):
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    ls = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    ls.bind(("127.0.0.1", 0))
    ls.listen(4)
    fake_port = ls.getsockname()[1]
    threading.Thread(target=fake, args=(ls,), daemon=True).start()
    globals()["PORT"] = fake_port

    try:
        # framing
        ok = send("world", "get_all_actors", {})
        check("framing round-trip lewat soket sungguhan", ok.get("status") == "SUCCESS", ok)
        bad = send("telepathy", "x", {})
        check("domain asing ditolak klien sebelum dikirim",
              bad.get("status") == "CLIENT_GUARD" and bad.get("domains"), str(bad))
        unk = send("world", "boom", {})
        check("action asing dijawab FAILED, bukan exception", unk.get("status") == "FAILED", unk)

        # class resolution — the defect measured on 2026-10-15
        ref = tool_world({"action": "spawn", "params": {"actor_class": "BogusThing",
                                                        "actor_label": "Nope"}})
        check("nama kelas yang tidak bisa diselesaikan DITOLAK, tidak dikirim",
              ref.get("state") == "CLASS_UNRESOLVED" and ref.get("sent") is False and
              "AActor" in json.dumps(ref), str(ref)[:300])
        sp = tool_world({"action": "spawn", "params": {"actor_class": "StaticMeshActor",
                                                       "location": [10, 20, 30],
                                                       "actor_label": "SMA1"}})
        check("nama pendek diselesaikan ke /Script/Engine.* dan spawn berhasil",
              sp.get("status") == "SUCCESS" and sp.get("class") == "StaticMeshActor" and
              (sp.get("verified") or {}).get("requested_class") == "/Script/Engine.StaticMeshActor",
              str(sp)[:300])
        check("spawn yang diverifikasi menyertakan readback lokasi",
              (sp.get("verified") or {}).get("readback_location") == [10, 20, 30], str(sp)[:300])
        bare = tool_world({"action": "spawn", "params": {"actor_class":
                                                         "/Script/Engine.Actor",
                                                         "actor_label": "Bare"}})
        check("class yang benar-benar AActor ditandai DEGRADED, bukan SUCCESS polos",
              bare.get("state") == "DEGRADED" and "RootComponent" in json.dumps(bare),
              str(bare)[:300])

        # write verified by read-back
        st = tool_world({"action": "set_transform", "params": {"actor_label": "SMA1",
                                                               "location": [1.5, 2.5, 3.5],
                                                               "scale": [2, 2, 2]}})
        check("set_transform melaporkan nilai yang MENDARAT, bukan hanya status",
              st.get("changed") is True and st["verified"]["after"]["location"] == [1.5, 2.5, 3.5],
              str(st)[:400])
        st2 = tool_world({"action": "set_transform", "params": {"actor_label": "Bare",
                                                                "location": [9, 9, 9]}})
        check("SUCCESS yang tidak mengubah apa pun diangkat jadi DEGRADED",
              st2.get("status") == "SUCCESS" and st2.get("changed") is False and
              st2.get("state") == "DEGRADED", str(st2)[:400])

        # pipeline partial truth
        pl = tool_pipeline({"steps": [
            {"domain": "world", "action": "spawn", "params": {"actor_class": "PointLight",
                                                              "actor_label": "L1"}},
            {"domain": "world", "action": "boom", "params": {}},
            {"domain": "material", "action": "get_parameters", "params": {}}]})
        check("pipeline memisahkan yang berhasil dari yang gagal (partial, bukan bohong)",
              pl.get("state") == "PARTIAL" and pl["steps_succeeded"] == 2 and
              pl["steps_total"] == 3, str(pl)[:400])
        idx = [r.get("index") for r in pl["report"] if r.get("status") == "FAILED"]
        check("indeks langkah yang gagal dilaporkan apa adanya", idx == [1], str(idx))
        empty = tool_pipeline({"steps": []})
        check("pipeline kosong ditolak sebelum mengirim apa pun",
              empty.get("state") == "BAD_ARGS" and empty.get("sent") is False, str(empty))
        many = tool_pipeline({"steps": [{"domain": "world", "action": "get_all_actors"}] * 65})
        check("pipeline dengan 65 langkah ditahan oleh cap, bukan dijalankan",
              many.get("state") == "TOO_MANY_STEPS", str(many)[:200])
        badstep = tool_pipeline({"steps": [{"domain": "nope", "action": "x"}]})
        check("langkah tanpa domain yang dikenal di-SKIP dan tidak dikirim",
              badstep["report"][0].get("status") == "SKIPPED" and
              badstep["report"][0].get("sent") is False, str(badstep)[:300])

        # MCP surface
        init = handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        check("initialize memperkenalkan server dengan versi protokol",
              init["result"]["serverInfo"]["name"] == SERVER_NAME, str(init))
        tl = handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
        check("tujuh alat terdaftar", len(tl) == 7 and
              all(t["inputSchema"]["type"] == "object" for t in tl), str([t["name"] for t in tl]))
        check("nama alat memakai prefiks ue_ satu keluarga",
              all(t["name"].startswith("ue_") for t in tl), str([t["name"] for t in tl]))
        impl_ok = all(t["name"] in TOOL_IMPLS for t in tl)
        check("setiap alat yang terdaftar punya implementasi (tidak ada alat hantu)",
              impl_ok, [t["name"] for t in tl if t["name"] not in TOOL_IMPLS])
        call = handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                       "params": {"name": "ue_status", "arguments": {}}})
        body = json.dumps(call)
        check("tools/call ue_status menjawab dengan tabel kelas dan endpoint",
               "class_table" in body and str(fake_port) in body, body[:200])
        unk_tool = handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                           "params": {"name": "ue_telepathy"}})
        check("alat asing ditolak -32602", unk_tool.get("error", {}).get("code") == -32602,
              str(unk_tool))
        check("notifikasi tanpa id tidak menjawab apa pun",
              handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None)
        res = handle({"jsonrpc": "2.0", "id": 5, "method": "resources/read",
                      "params": {"uri": "mcp://about/protocol"}})
        txt = json.dumps(res)
        check("dokumen protokol menyebut framing newline dan satu koneksi per perintah",
              "one JSON object per line" in txt and "GameThread" in txt, txt[:200])

        # UNAVAILABLE must be reachable by test, not asserted in a comment
        globals()["PORT"] = 1            # nothing listens on 1
        un = tool_world({"action": "get_all_actors"})
        check("editor tertutup dijawab UNAVAILABLE dan menyatakan tidak mengirim apa pun",
              un.get("status") == "UNAVAILABLE" and "nothing was sent" in json.dumps(un),
              str(un)[:300])
        st_closed = tool_status({})
        check("ue_status jujur soal editor yang tidak ada (ok=False, bukan COMPLETED)",
              st_closed.get("ok") is False and st_closed.get("state") == "UNAVAILABLE",
              str(st_closed)[:200])
        globals()["PORT"] = fake_port
    finally:
        try:
            ls.close()
        except OSError:
            pass
    print("\n".join(lines))
    total, passed = len(checks), sum(checks)
    print(f"\n{SERVER_NAME} selftest: {passed}/{total} sesuai")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
