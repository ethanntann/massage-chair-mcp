# Massage Chair MCP

[![CI](https://github.com/ethanntann/massage-chair-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/ethanntann/massage-chair-mcp/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**An AI assistant that can give you a massage.**

This is an [MCP](https://modelcontextprotocol.io) server that lets an AI agent
such as Claude control an OSIM uDiva3 massage chair over Bluetooth LE. Ask for
"something to help me wind down" and the agent picks a program, reclines the
seat and starts it; ask for "kneading on my shoulders, a bit harder" and it
drives the rollers directly. The chair has no public API, so I reverse-engineered
its Bluetooth protocol from packet captures of the official phone app.

## The problem, and why

I was curious about MCP servers and whether agent tools could control physical
hardware, not just files and web APIs. Most agent demos stay on a screen. I
wanted to bridge the gap between AI agents and hardware, and to have a physical
demonstration of what an agent can do in the real world: something you can sit
in and feel respond to a sentence.

That meant solving three problems:

1. **No API.** The chair only talks to OSIM's app, over an undocumented BLE
   protocol.
2. **Hardware is slow and stateful.** The seat takes up to 30 s to move, some
   commands are toggles, and the chair drops back to idle if it stops hearing
   from you. A tool call that returns "done" has to mean the chair really did it.
3. **Agents make mistakes.** The tools have to be hard to misuse, and an agent
   (or a prompt injection) must not be able to leave the chair running.

## How it works

```
 Claude / any MCP client
        │  stdio (JSON-RPC)
        ▼
 server.py ─────────── 25 tools: presets, manual techniques, seat, custom routines
        │                 enum-typed parameters, read-only vs actuating annotations
        ▼
 custom_presets.py ─── routines: validated JSON steps, run in the background
        │
        ▼
 udiva_driver.py ───── one BLE session: handshake, 0.7 s keepalive,
        │                 one write at a time, one operation at a time,
        │                 every command confirmed from status frames
        ▼
 protocol.py ───────── 4-byte commands + checksum, 14-byte status frame layout
        │
        ▼  BLE GATT (bleak): write fff2, notify fff1
 OSIM uDiva3 chair
```

- **Protocol** (`protocol.py`): commands are `[b0 b1 b2 checksum]` written to
  characteristic `fff2`; the chair streams 14-byte status frames on `fff1`
  carrying a motor flag, a seat-travel flag, the running program and an echo
  of the last command. Every byte is pinned by tests. How I decoded it is in
  [`docs/protocol.md`](docs/protocol.md).
- **Driver** (`udiva_driver.py`): an asyncio session over
  [bleak](https://github.com/hbldh/bleak). It finds the chair by name or
  address, runs the handshake, keeps the session alive, and turns each request
  into a command loop that waits for the chair's telemetry to confirm it.
- **Routines** (`custom_presets.py`): user-defined sequences ("knead the
  shoulders at 4 for five minutes, then move down to the lumbar") stored as
  JSON and run in the background, so the agent can check on them or stop them.
- **MCP server** (`server.py`): FastMCP tools with instructions that steer the
  agent toward the chair's built-in programs, which are the most reliable, and
  toward manual control only when asked.

## Results

| What | Result |
|---|---|
| Protocol decoded | 4-byte command format and checksum, 14-byte status frame, 18 factory programs, seat posture, manual techniques, 3D intensity, legs |
| Source material | 4 PacketLogger captures of the OSIM app (~24,000 log lines), one action at a time, cross-referenced with app screens |
| Tools exposed | 25 (presets, manual techniques, seat positions, custom routines, status) |
| Built-in routines | 5 multi-step routines (desk neck relief, full back descent, wind down, leg focus, manual test) |
| Tests | 97, offline, against a simulated chair (`pytest`), on every push (CI) |

## Key decisions

- **Confirm from telemetry, never from sleeps.** The first version used fixed
  waits and reported success while the seat was still moving or had never
  started. Now a toggle waits for its echo, a preset waits for a non-zero
  program id, and a seat move holds its command at the app's ~100 ms cadence
  until the motor flag shows the travel limit. Tools say when the chair didn't
  acknowledge, so the agent can tell the user the truth.
- **Match the app's timing exactly.** The captures showed the app sends no
  heartbeats while it holds a motion command, and a heartbeat injected into
  that stream makes the seat stop and restart. The keepalive goes silent
  during motion.
- **One write, one operation at a time.** All GATT writes go through a single
  lock with a timeout, because two in-flight writes can leave one waiting
  forever. High-level operations are serialised too, and `stop` cancels
  whatever is running or queued before sending STOP, so it works even in the
  middle of a 60 s positioning sequence.
- **The driver owns the state.** Tapping and kneading are toggles on the chair.
  The driver tracks what it believes is on and only sends a toggle when the
  state must change; everything is reset if the Bluetooth link drops, so a
  reconnect starts from a known state.
- **Make tools hard to misuse.** Parameters with a fixed set of values are
  enums in the schema, ranges are bounded, and the server instructions tell
  the agent which tool fits which request. Anything that changes what the
  chair is doing first cancels a running routine instead of fighting it.

## Safety and security

The tools move a chair that someone is sitting in, and the agent calling them
may have read untrusted text. So:

- **Bounded routines.** At most 100 steps, 30 minutes per wait and 90 minutes
  in total. Names and descriptions, which are shown back to the agent later,
  are length-limited and can't contain control characters.
- **Clear tool annotations.** Read-only tools are marked read-only, so a client
  can auto-approve those and ask the user before anything that moves the chair.
- **The right chair.** A config file can pin the chair's Bluetooth address, so
  a nearby device spoofing its name can't be picked up.
- **Local only.** The server speaks MCP over stdio; there is no network
  listener and nothing leaves your machine except Bluetooth packets to the
  chair.
- **Stop always works.** `stop` cancels in-flight work first, and a cancelled
  routine sends STOP on its way out.

See [SECURITY.md](SECURITY.md) to report a problem.

## Run it yourself

You need an OSIM uDiva3 (OS-8211), a computer with Bluetooth LE, Python 3.11+
and an MCP client such as Claude Desktop.

```bash
git clone https://github.com/ethanntann/massage-chair-mcp
cd massage-chair-mcp
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest                      # offline, no chair needed
.venv/bin/python scan.py              # with the chair on: find its name
```

Add the server to Claude Desktop's `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "osim-udiva3": {
      "command": "/absolute/path/to/massage-chair-mcp/.venv/bin/python",
      "args": ["/absolute/path/to/massage-chair-mcp/server.py"]
    }
  }
}
```

Close the OSIM phone app first (the chair accepts one Bluetooth connection),
restart Claude Desktop and ask for a massage. To try things without an agent:

```bash
.venv/bin/python run_routine.py                 # list routines and programs
.venv/bin/python run_routine.py --preset spa 120
.venv/bin/mcp dev server.py                     # MCP Inspector
```

Optional `~/.udiva_config.json`: `{"address": "..."}` to pin the chair,
`{"name": "OS8211 ..."}` for an exact name, or `{"prefixes": [...]}`.
Custom routines are saved to `~/.udiva_presets.json`.

## Limitations

- **One chair model.** Only the OSIM uDiva3 (OS-8211) has been decoded and
  tested. Other OSIM chairs likely use a different command set.
- **Partly decoded.** Two factory program ids (`0x50`, `0x51`) are unnamed in
  the app, and the meaning of some status bytes is still a best guess.
  `running` in `get_status` means "the program id is non-zero".
- **One connection.** The chair accepts a single Bluetooth connection, so the
  phone app and this server can't be connected at the same time.
- **Run-state is a toggle.** The driver tracks whether it has entered the
  chair's run state; if someone uses the chair's own remote meanwhile, the
  driver can't know.
- **Unauthenticated chair.** The chair's protocol has no pairing or
  encryption, so anyone in Bluetooth range can control it when no one else is
  connected. That's the product's design and outside this project's control.
- **Tested on macOS.** bleak also supports Linux and Windows, but I have only
  run it against the chair from a Mac.

## License

[MIT](LICENSE). OSIM and uDiva are trademarks of OSIM International; this
project is not affiliated with or endorsed by OSIM. Use it at your own risk.

## Layout

```
protocol.py          wire protocol: commands, checksums, status frame layout
udiva_driver.py      BLE session: connect, keepalive, confirmed operations
custom_presets.py    routine schema, validation, storage, runner
server.py            MCP server and tools
run_routine.py       command-line runner (no MCP)
scan.py              list nearby BLE devices
tests/               simulated chair + 97 tests
docs/protocol.md     how the protocol was reverse-engineered
```
