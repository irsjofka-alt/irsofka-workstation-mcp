# IRSofka Workstation MCP

The machine side of the Irsofka station bridge: a stdio [Model Context Protocol](https://modelcontextprotocol.io)
server that forwards to the editor's own TCP socket, so an engine can drive a running editor
without anyone typing into it.

```
engine (MCP client) ── stdio JSON-RPC ──> server.py ── newline JSON ──> editor bridge 127.0.0.1:9877
```

- **License**: Business Source License 1.1, Licensor PT IRSofka Data Teknologi, Change License the Apache License, Version 2.0,
  effective at each version's Change Date. `UPSTREAM` states what this is derived from — nothing third-party;
  it is a client of a protocol documented from the operator's own plugin source and confirmed by a
  live round trip.
- **Requires**: Python 3.9+ (stdlib only) and the editor running with its bridge plugin loaded.

## Why the name is not the engine's name

The repository owns the machine side of the bridge — server, transport, gates. The editor plugin
stays an installed artifact of the engine project, and putting a third party's product name in a
repository we publish adds a trademark question without adding information.

## Tools

| Tool | Surface |
|---|---|
| `ue_world` | spawn, bulk_spawn, duplicate, set_transform, get_transform, attach, delete, get_all_actors, select_actors, set_lighting, set_physics, set_visibility, set_folder, set_tags, set_material, level_streaming |
| `ue_blueprint` | create, add_variable, add_function, add_call_function, connect_pins, set_pin_default, compile, get_summary |
| `ue_material` | create_material, create_instance, set_parameter, set_texture, set_static_switch, get_parameters |
| `ue_media` | import_sound, bind_sound, play_sound, spawn_niagara, set_niagara_parameter, create_sequence |
| `ue_vision` | capture (viewport frame), frame (aim the camera at an actor) |
| `ue_pipeline` | ordered steps in one call, with PARTIAL reporting |
| `ue_status` | endpoint, whether the editor answers, the class table, the step cap |

## The three rules this server adds over the raw socket

All three come from defects measured on 2026-10-15 (`tools/ue_bridge_probe.py` in the workstation
repository), not from preference.

**1. Class names are resolved, or the command is refused.** The engine's `LoadClass` wants
`/Script/Engine.StaticMeshActor`. Given a bare name it returns null and the handler falls back to
`AActor::StaticClass()` — and a bare `AActor` has no RootComponent, so every later transform is a
silent no-op while spawn, set and get all answer `SUCCESS`. Measured: spawn reports
`location [10,20,30]`, `get_transform` reads `[0,0,0]`, before and after a `set_transform` that
also said SUCCESS. This server maps known short names to their full paths, refuses to send a name
it cannot resolve (`CLASS_UNRESOLVED`, `sent: false`), and flags an answer that comes back as
`Actor` as `DEGRADED` rather than passing it on as success.

**2. A write is verified before it is reported.** `ActionSetTransform` returns
`{"status":"SUCCESS"}` and nothing else. So this server reads the transform back and returns
`before` / `after` / `asked` / `match` and a `changed` flag computed from the editor's own
numbers. A SUCCESS that did not move anything is reported as `DEGRADED`, not as done.

**3. Unreachable is a word.** If the bridge is not listening, tools answer `UNAVAILABLE` with the
endpoint and the fact that nothing was sent. A call the editor never saw is never reported as one
that happened.

Two words travel in every reply, and they answer to different owners: `status` is what the **engine**
said, passed through untouched; `state` is this server's word for the same fact, mapped by one table
(`SUCCESS` → `DONE`, `UNAVAILABLE` → `UNAVAILABLE`). `ok` is derived from that mapping and MCP
`isError` is derived from `ok`, so the payload and the protocol flag cannot disagree. The first
version computed `isError` as `not out.get("ok")` while the engine's own success reply contains no
`ok` key — which meant every successful domain call reached a client labelled an error, and 24 green
offline checks never noticed because none of them drove `tools/call` on a domain tool. The suite now
does, at the JSON-RPC layer, in both directions.

## Thread and timing facts that shape the design

The editor runs every handler on the GameThread through `FIRSofkaTaskRunner::ExecuteSync`. One
command therefore blocks the socket, and a pipeline of them holds the editor's main thread for the
sum of their times — which is why `ue_pipeline` caps at 64 steps and aborts the remainder the
moment the editor stops answering, rather than continuing to send into a dead socket.

`ue_pipeline` reports partial truth on purpose: a step that fails does not undo the steps that
already ran, so the reply names which ones succeeded and which did not.

## Overlap with the server that already ships inside the plugin

The plugin installs its own MCP front-end (`.../IRSofkaStationCore/Content/Python/mcp_server.py`),
and on this workstation that one is already registered in the engines under the name
`irsofka-unreal-mcp`. Two servers forwarding to the same editor socket is a real condition, not a
typo, so say what distinguishes them and what it costs:

- the plugin's front-end is a thin, complete passthrough: it exposes every action the C++ accepts,
  in the shape the C++ wants, including the bare class names that silently produce an immovable
  actor. It is the right tool when you want exactly the plugin's surface.
- this server is the station's product surface: it resolves class names or refuses, verifies a
  write by reading it back, caps pipelines so one call cannot hold the GameThread indefinitely,
  and reports UNAVAILABLE rather than a success the editor never saw. It is the right tool when an
  unattended run is the caller.

Both may be registered at once. What must not happen is an engine choosing between them by name
alone, because their answers differ on the failure paths — which is the whole reason this one
exists. Register the one that matches the caller, or name the difference in the prompt.


```bash
python3 server.py --selftest     # 28 checks, no editor needed (a fake bridge on an ephemeral port)
python3 server.py --status       # honest: ok=false and state=UNAVAILABLE when the editor is closed
```

Measured against a live editor on 2026-10-15: `spawn StaticMeshActor` resolved to
`/Script/Engine.StaticMeshActor`, reported `class=StaticMeshActor`, and `get_transform` read the
spawn location back as `[5.0, 6.0, 7.0]`; `set_transform` to `[9.5, -8.25, 3.125]` with scale
`[3,1,1]` returned `changed: true` with `all_match: true`; an unresolvable class was refused with
`sent: false`; a three-step pipeline reported `PARTIAL` with `1` succeeded; the test actor was
deleted and no leftover matched the probe prefix.

## Not verified

`ue_blueprint`, `ue_material`, `ue_media` and `ue_vision` are forwarded and their framing is
exercised by the selftest against a fake bridge, but no blueprint compile, material parameter,
sound, Niagara system, sequence or viewport capture was driven against the live editor in this
session. Treat their action lists as the plugin's contract, not as measurements. The `CLASS_PATHS`
table is also partial: entries are names this machine's engine was checked to load, and anything
outside it is refused rather than guessed.
