"""
server.py — MCP server for the OSIM uDiva3 massage chair.

Exposes the reverse-engineered chair controls as MCP tools so an MCP client
(e.g. Claude Desktop) can run the chair in natural language. See README.md for
setup. Logs go to stderr (stdout carries the MCP stdio protocol).

* One BLE connection per server process; every action tool auto-connects.
* Mode and preset changes are confirmed from chair telemetry; tools report
  whether the chair actually acknowledged.
* Custom routines run in the background. Anything that changes what the chair
  is doing (stop, a preset, manual mode, sofa, power off, disconnect) cancels
  the running routine first.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Literal

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

import custom_presets as cp
from protocol import LEG_LEVELS, PRESETS, ROLLING_LEVELS, ZONES, normalize_name
from udiva_driver import ChairError, ChairNotFoundError, OperationInterrupted, UDivaDriver

log = logging.getLogger("osim-udiva3")

SOFA_ON_SHUTDOWN_TIMEOUT = 40.0
WATCHDOG_INTERVAL = 15.0  # how often the massage time limit is checked

RollingLevel = Literal["off", "partial", "full"]
LegLevel = Literal["off", "low", "high"]
Zone = Literal["shoulder", "back", "lumbar"]
Area = Literal["back", "head_neck"]


@dataclass
class ChairSession:
    chair: UDivaDriver
    routine_task: asyncio.Task[str] | None = None
    routine_name: str = ""
    last_routine_result: str = ""
    last_auto_stop: str = ""

    @property
    def routine_running(self) -> bool:
        return self.routine_task is not None and not self.routine_task.done()


async def _cancel_routine(s: ChairSession) -> None:
    """Cancel the background routine, if any, and wait for it to stop."""
    task = s.routine_task
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as e:  # the routine's own failure
            log.warning("Routine '%s' ended with an error while cancelling: %s", s.routine_name, e)
        s.last_routine_result = f"'{s.routine_name}' cancelled."
    s.routine_task = None
    s.routine_name = ""


async def enforce_time_limit(s: ChairSession) -> bool:
    """Stop the chair if the current massage has run past its limit.

    A hard cap rather than an idle timeout: a factory program runs for a long
    time with no tool calls, and that's normal. Returns True if it stopped.
    """
    elapsed = s.chair.massage_elapsed()
    limit_min = s.chair.max_massage_minutes
    if elapsed is None or elapsed < limit_min * 60:
        return False
    log.warning("Massage reached the %d-minute limit; stopping the chair", limit_min)
    await _cancel_routine(s)
    if s.chair.connected:
        await s.chair.stop()
    s.last_auto_stop = (
        f"Stopped automatically after {elapsed / 60:.0f} minutes (limit: {limit_min} minutes)."
    )
    return True


async def _watchdog(s: ChairSession) -> None:
    while True:
        await asyncio.sleep(WATCHDOG_INTERVAL)
        try:
            await enforce_time_limit(s)
        except Exception:
            log.exception("Time-limit check failed; will retry")


@asynccontextmanager
async def lifespan(_server: FastMCP) -> AsyncIterator[ChairSession]:
    session = ChairSession(chair=UDivaDriver())
    watchdog = asyncio.create_task(_watchdog(session), name="massage-time-limit")
    try:
        yield session
    finally:
        watchdog.cancel()
        # Leave the chair resting: stop any routine, return to sofa, then
        # disconnect. Each step is bounded so shutdown can't hang.
        try:
            await _cancel_routine(session)
            if session.chair.connected:
                await asyncio.wait_for(
                    session.chair.move_seat_sofa(), timeout=SOFA_ON_SHUTDOWN_TIMEOUT
                )
        except Exception as e:
            log.warning("Sofa return on shutdown failed: %s", e)
        try:
            await session.chair.disconnect()
        except Exception as e:
            log.warning("Disconnect on shutdown failed: %s", e)


INSTRUCTIONS = """\
Controls an OSIM uDiva3 massage chair over Bluetooth.

CHOOSING A MASSAGE — prefer a built-in factory program:
- When the user wants a massage, FIRST try to match their intent to a built-in
  factory program. Call list_presets to see them and start_preset to run one.
  Factory programs are the most reliable: the chair runs them autonomously
  (full positioning + technique sequencing) and confirms they engaged.
  Rough matching: sleep/wind-down -> sweet_dreams or nap; intense/deep ->
  deep_tissue or sports_recovery; neck & shoulders -> shoulders_care or
  shoulder_blades; lower back -> lumbar_delight; light/relaxing -> gentle, spa,
  or beauty; general/full-body -> signature or spa.
- Use a CUSTOM routine (list_custom_presets / run_custom_preset) or MANUAL
  control (enter_manual_mode + technique tools) only when no factory program
  fits, or the user explicitly asks for custom/manual control or a specific
  combination of techniques/zones. Manual is less reliable and slower to set up.

SESSION FLOW: you do NOT need to call connect_chair or power_on manually —
every action tool auto-connects and auto-powers-on as needed. Simply ask for
a massage and everything is handled. To switch programs mid-session just call
stop then start the next one — do NOT return to sofa between programs. Only use
return_to_sofa when the user is finished / leaving the chair (then optionally
disconnect_chair). disconnect_chair and server shutdown leave the chair resting
on their own. The OSIM phone app must be disconnected first — the chair allows
only one Bluetooth connection at a time.

TIME LIMIT: for safety the server stops any massage automatically once it has
run for a set time (get_status shows the minutes so far and the limit; default
120). If that happens and the user wants more, just start a new one.
"""

mcp = FastMCP("osim-udiva3", lifespan=lifespan, instructions=INSTRUCTIONS)

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
# Tools that move or change the chair. Not "destructive" in MCP's sense (no
# data is lost), but not read-only and not safe to repeat blindly.
ACTUATES = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)
EDITS_LOCAL = ToolAnnotations(readOnlyHint=False, openWorldHint=False)


def _s(ctx: Context) -> ChairSession:
    return ctx.request_context.lifespan_context


async def _ensure_connected(s: ChairSession) -> str | None:
    """Connect if needed. Returns an error message, or None when connected."""
    if s.chair.connected:
        return None
    try:
        if not await s.chair.connect():
            return (
                "Connected to the chair but no status received yet — "
                "it may still be booting. Try again in a moment."
            )
    except ChairNotFoundError as e:
        return f"Could not find the chair over Bluetooth. {e}"
    except Exception as e:
        log.exception("Connect failed")
        return f"Auto-connect failed: {e}"
    return None


def _ack(ok: bool, done: str, what: str) -> str:
    return done if ok else f"Sent {what} but the chair didn't acknowledge it — check get_status."


async def _chair_call(
    s: ChairSession, action: Callable[[], Awaitable[str]], *, cancel_routine: bool = False
) -> str:
    """Optionally cancel the routine, connect, then run `action()` -> message.

    Driver errors become readable messages instead of tool exceptions.
    """
    if cancel_routine:
        await _cancel_routine(s)
    err = await _ensure_connected(s)
    if err:
        return err
    try:
        return await action()
    except OperationInterrupted:
        return "Interrupted by a stop request."
    except ChairError as e:
        log.warning("Chair command failed: %s", e)
        return f"Chair command failed: {e}"


# -- connection & power -----------------------------------------------------
@mcp.tool(annotations=ACTUATES)
async def connect_chair(ctx: Context) -> str:
    """Connect to the chair over Bluetooth and run the required handshake.
    Usually not needed — all action tools auto-connect. Call this explicitly
    only if you want to pre-warm the connection. The OSIM phone app must be
    disconnected (the chair allows only one Bluetooth connection)."""
    s = _s(ctx)
    if s.chair.connected:
        return "Already connected."
    return await _ensure_connected(s) or "Connected and handshake complete. Ready to use."


@mcp.tool(annotations=ACTUATES)
async def power_on(ctx: Context) -> str:
    """Move the chair into its active run-state so it's ready to start a
    program. (The chair is already powered at the wall — this is the 'begin'
    toggle, not a true power switch.) Auto-connects if not already connected.
    Usually not needed explicitly: start_preset and enter_manual_mode do this
    themselves."""
    s = _s(ctx)

    async def act() -> str:
        await s.chair.power_on()
        return "Chair is in run-state and ready to start a program."

    return await _chair_call(s, act)


@mcp.tool(annotations=ACTUATES)
async def power_off(ctx: Context) -> str:
    """Stop all massage (including a running custom routine) and take the
    chair out of its run-state."""
    s = _s(ctx)
    await _cancel_routine(s)
    if not s.chair.connected:
        return "Not connected; nothing to power off."
    try:
        await s.chair.power_off()
    except ChairError as e:
        return f"Chair command failed: {e}"
    return "Chair powered off."


@mcp.tool(annotations=ACTUATES)
async def stop(ctx: Context) -> str:
    """Stop the current massage — a factory program, manual massage, or custom
    routine — and return the chair to idle (it stays powered, ready for
    another program)."""
    s = _s(ctx)

    async def act() -> str:
        ok = await s.chair.stop()
        return (
            "Massage stopped; chair is idle."
            if ok
            else "Sent stop but got no acknowledgement from the chair — check get_status."
        )

    return await _chair_call(s, act, cancel_routine=True)


@mcp.tool(annotations=ACTUATES)
async def return_to_sofa(ctx: Context) -> str:
    """Return the chair to its default sofa/resting position (backrest up,
    footrest down). Stops any running massage/routine first. Takes up to ~35s
    while the seat moves."""
    s = _s(ctx)

    async def act() -> str:
        ok = await s.chair.move_seat_sofa()
        return _ack(ok, "Chair returned to the sofa position.", "the sofa return")

    return await _chair_call(s, act, cancel_routine=True)


@mcp.tool(annotations=ACTUATES)
async def disconnect_chair(ctx: Context) -> str:
    """Disconnect from the chair over Bluetooth, freeing it for the OSIM phone
    app or another client. Stops any running routine first. Call connect_chair
    again to reconnect."""
    s = _s(ctx)
    await _cancel_routine(s)
    if not s.chair.connected:
        return "Already disconnected."
    try:
        await s.chair.disconnect()
    except Exception as e:
        log.exception("Disconnect failed")
        return f"Disconnect error: {e}"
    return "Disconnected from the chair."


@mcp.tool(annotations=READ_ONLY)
async def get_status(ctx: Context) -> str:
    """Report the chair's state: connected, powered, running, program id,
    which manual techniques are active, and the custom routine (running, or
    how the last one ended)."""
    s = _s(ctx)
    st = s.chair.status()
    st["custom_routine"] = s.routine_name if s.routine_running else "none"
    if s.last_routine_result and not s.routine_running:
        st["last_routine_result"] = s.last_routine_result
    if s.last_auto_stop and s.chair.massage_elapsed() is None:
        st["last_auto_stop"] = s.last_auto_stop
    return "Chair status:\n" + "\n".join(f"{k}: {v}" for k, v in st.items())


# -- presets ----------------------------------------------------------------
# Short intent hints so the model can match a request to a factory program.
PRESET_DESCRIPTIONS = {
    "spa": "relaxing full-body spa session",
    "entertainment": "lighter massage for while watching/reading",
    "sports_recovery": "post-workout muscle recovery",
    "beauty": "gentle wellness program",
    "deep_tissue": "intense deep-pressure massage",
    "shoulders_care": "neck and shoulder focus",
    "sweet_dreams": "calming wind-down for sleep",
    "beautiful_mum": "gentle program (prenatal/mum)",
    "lumbar_delight": "lower back / lumbar focus",
    "detox": "circulation / detox program",
    "shoulder_blades": "upper back / shoulder-blade focus",
    "gentle": "light, low-intensity relaxation",
    "healthy_mum": "gentle program (mum)",
    "nap": "short relaxation, good for a nap",
    "immune_booster": "wellness / immune program",
    "signature": "OSIM signature full-body auto-massage",
}


@mcp.tool(annotations=READ_ONLY)
async def list_presets(ctx: Context) -> str:
    """List the built-in factory massage programs with a short hint for each.

    PREFER these over manual/custom control when one matches the user's intent —
    they are the most reliable (the chair positions and sequences them itself)."""
    lines = []
    for n in PRESETS:
        if n.startswith("preset_"):
            continue
        desc = PRESET_DESCRIPTIONS.get(n, "")
        lines.append(f"- {n}" + (f" — {desc}" if desc else ""))
    return "Built-in factory programs:\n" + "\n".join(lines)


async def _start_preset(s: ChairSession, name: str) -> str:
    async def act() -> str:
        ok = await s.chair.start_preset(name)
        return (
            f"Started the '{name}' program and confirmed running."
            if ok
            else f"Sent '{name}' but the chair didn't report a running program — check get_status."
        )

    return await _chair_call(s, act, cancel_routine=True)


@mcp.tool(annotations=ACTUATES)
async def start_preset(ctx: Context, name: str) -> str:
    """Start a named built-in massage program. Powers on first if needed and
    confirms the program engaged. Stops any running custom routine.

    Args:
        name: e.g. 'spa', 'deep_tissue', 'sweet_dreams', 'shoulders_care',
              'lumbar_delight', 'sports_recovery', 'nap', 'detox', 'gentle'.
              Use list_presets for the full set."""
    name = normalize_name(name)
    if name not in PRESETS:
        return f"Unknown preset '{name}'. Use list_presets to see options."
    return await _start_preset(_s(ctx), name)


# -- manual mode ------------------------------------------------------------
async def _enter_manual(s: ChairSession, area: str) -> str:
    async def act() -> str:
        ok = await s.chair.enter_manual_mode(area)
        return (
            f"Manual {area} massage positioned and ready. Turn on a technique "
            f"(set_kneading) to start the rollers."
            if ok
            else "Manual positioning did not complete — check get_status."
        )

    return await _chair_call(s, act, cancel_routine=True)


@mcp.tool(annotations=ACTUATES)
async def enter_manual_mode(ctx: Context, area: Area = "back") -> str:
    """Start a manual massage and position the chair, ready for technique steps.

    PREFER a factory program (list_presets / start_preset) when one fits the
    user's intent — use manual only for specific technique/zone combinations or
    when the user explicitly wants manual control.

    This drives the chair to the massage position and deploys the rollers
    (auto shoulder-detect), then settles. It does NOT start any technique by
    itself — after this, turn on a technique (set_kneading/set_tapping) to
    actually start the rollers, then adjust rolling / zone intensity / shoulder
    position.

    Args:
        area: 'back' for a Back Body roller massage (default), or 'head_neck'
              for a Head & Neck (V-grip) massage.

    Powers on first if needed. Stops any running custom routine. Takes up to
    ~60s while the chair positions."""
    return await _enter_manual(_s(ctx), area)


@mcp.tool(annotations=ACTUATES)
async def set_tapping(ctx: Context, on: bool) -> str:
    """Turn back TAPPING on/off (combinable with kneading and rolling).
    Requires manual mode."""
    s = _s(ctx)
    state = "on" if on else "off"

    async def act() -> str:
        if s.chair.tapping == on:
            return f"Tapping already {state}."
        return _ack(await s.chair.set_tapping(on), f"Tapping {state}.", f"tapping {state}")

    return await _chair_call(s, act)


@mcp.tool(annotations=ACTUATES)
async def set_kneading(ctx: Context, on: bool) -> str:
    """Turn back KNEADING on/off (combinable with tapping and rolling).
    Requires manual mode."""
    s = _s(ctx)
    state = "on" if on else "off"

    async def act() -> str:
        if s.chair.kneading == on:
            return f"Kneading already {state}."
        return _ack(await s.chair.set_kneading(on), f"Kneading {state}.", f"kneading {state}")

    return await _chair_call(s, act)


@mcp.tool(annotations=ACTUATES)
async def set_rolling(ctx: Context, level: RollingLevel) -> str:
    """Set back ROLLING. level: 'off', 'partial', or 'full'. Combinable with
    tapping and kneading. Requires manual mode."""
    s = _s(ctx)

    async def act() -> str:
        ok = await s.chair.set_rolling(ROLLING_LEVELS[level])
        return _ack(ok, f"Rolling set to {level}.", f"rolling {level}")

    return await _chair_call(s, act)


@mcp.tool(annotations=ACTUATES)
async def set_shoulder_position(ctx: Context, position: Annotated[int, Field(ge=1, le=5)]) -> str:
    """Set the shoulder roller height, 1 (lowest) to 5 (highest). Requires
    manual mode."""
    s = _s(ctx)

    async def act() -> str:
        ok = await s.chair.set_shoulder_position(position)
        return _ack(ok, f"Shoulder position set to {position}/5.", "the shoulder position")

    return await _chair_call(s, act)


@mcp.tool(annotations=ACTUATES)
async def set_zone_intensity(
    ctx: Context, zone: Zone, level: Annotated[int, Field(ge=0, le=5)]
) -> str:
    """Set 3D roller intensity for a back zone (requires manual mode with a
    technique active).

    Args:
        zone: 'shoulder', 'back', or 'lumbar'.
        level: 0 (off) to 5 (max).

    Intensity scales whichever technique is active at that zone; the shoulder
    zone is the most reliably responsive."""
    s = _s(ctx)

    async def act() -> str:
        ok = await s.chair.set_zone_intensity(ZONES[zone], level)
        return _ack(ok, f"{zone.capitalize()} intensity set to {level}.", f"{zone} intensity")

    return await _chair_call(s, act)


@mcp.tool(annotations=ACTUATES)
async def set_vgrip(ctx: Context, on: bool) -> str:
    """Turn the head/neck V-grip on/off. Requires manual mode. Rollers take a
    few seconds to reach the neck."""
    s = _s(ctx)
    state = "on" if on else "off"

    async def act() -> str:
        return _ack(await s.chair.set_vgrip(on), f"V-grip {state}.", f"V-grip {state}")

    return await _chair_call(s, act)


@mcp.tool(annotations=ACTUATES)
async def set_leg_kneading(ctx: Context, level: LegLevel) -> str:
    """Set leg/calf kneading: 'off', 'low', or 'high'."""
    s = _s(ctx)

    async def act() -> str:
        ok = await s.chair.set_leg_kneading(LEG_LEVELS[level])
        return _ack(ok, f"Leg kneading {level}.", f"leg kneading {level}")

    return await _chair_call(s, act)


@mcp.tool(annotations=ACTUATES)
async def set_leg_vibration(ctx: Context, level: LegLevel) -> str:
    """Set leg/foot vibration: 'off', 'low', or 'high'."""
    s = _s(ctx)

    async def act() -> str:
        ok = await s.chair.set_leg_vibration(LEG_LEVELS[level])
        return _ack(ok, f"Leg vibration {level}.", f"leg vibration {level}")

    return await _chair_call(s, act)


# -- convenience ------------------------------------------------------------
@mcp.tool(annotations=ACTUATES)
async def quick_start(ctx: Context, preset: str = "spa") -> str:
    """One-shot: connect (if needed), power on, and start a preset program.

    Args:
        preset: a preset name (default 'spa'), or 'manual' for a manual
                back massage."""
    s = _s(ctx)
    name = normalize_name(preset)
    if name == "manual":
        return await _enter_manual(s, "back")
    if name not in PRESETS:
        return f"Unknown preset '{preset}'. Use list_presets."
    return await _start_preset(s, name)


# -- custom presets ---------------------------------------------------------
@mcp.tool(annotations=READ_ONLY)
async def list_custom_presets(ctx: Context) -> str:
    """List user-defined and built-in custom massage routines (distinct from
    the chair's factory presets)."""
    presets = cp.all_presets()
    if not presets:
        return "No custom presets defined yet. Use save_custom_preset to add one."
    lines = []
    for name, body in presets.items():
        desc = body.get("description", "")
        n = len(body.get("steps", []))
        lines.append(f"- {name} ({n} steps){': ' + desc if desc else ''}")
    return "Custom routines:\n" + "\n".join(lines)


@mcp.tool(annotations=READ_ONLY)
async def get_custom_preset(ctx: Context, name: str) -> str:
    """Show the step-by-step definition of a custom routine as JSON."""
    presets = cp.all_presets()
    if name not in presets:
        return f"No custom preset '{name}'. Use list_custom_presets."
    return json.dumps(presets[name], indent=2)


@mcp.tool(annotations=EDITS_LOCAL)
async def save_custom_preset(
    ctx: Context, name: str, steps_json: str, description: str = ""
) -> str:
    """Save a custom massage routine.

    Args:
        name: a short name (e.g. 'my_evening').
        steps_json: a JSON array of step objects. Supported actions:
            {"action":"manual","area":"back"}   (or "head_neck"; positions the
                                                 chair + deploys the rollers)
            {"action":"seat_sofa"} / {"action":"seat_upright"} /
            {"action":"seat_recline"}           (seat posture only)
            {"action":"tapping","on":true|false}
            {"action":"kneading","on":true|false}
            {"action":"rolling","level":"off|partial|full"}
            {"action":"intensity","zone":"shoulder|back|lumbar","level":0-5}
            {"action":"position","value":1-5}    (shoulder roller height)
            {"action":"vgrip","on":true|false}
            {"action":"leg_kneading","level":"off|low|high"}
            {"action":"leg_vibration","level":"off|low|high"}
            {"action":"wait","seconds":N}
            {"action":"preset","name":"spa"}
            {"action":"stop"}
        description: optional human description.

    Pattern that works: start with {"action":"manual","area":"back"} (it
    positions the chair and deploys the rollers), then turn on an ENGAGING
    technique FIRST — {"action":"kneading","on":true} (or tapping) — which is
    what actually starts the rollers. Only after that do rolling / intensity /
    position adjustments take effect."""
    err = cp.validate_name(name)
    if err:
        return f"Invalid name: {err}"
    err = cp.validate_description(description)
    if err:
        return f"Invalid description: {err}"
    steps, err = cp.parse_steps_json(steps_json)
    if err:
        return err
    err = cp.validate_steps(steps)
    if err:
        return f"Invalid routine: {err}"
    try:
        cp.save_user_preset(name, steps, description)
    except (cp.PresetStoreError, OSError) as e:
        return f"Could not save: {e}"
    return f"Saved custom preset '{name}' ({len(steps)} steps)."


@mcp.tool(annotations=EDITS_LOCAL)
async def delete_custom_preset(ctx: Context, name: str) -> str:
    """Delete a user-saved custom routine (built-ins cannot be deleted)."""
    try:
        deleted = cp.delete_user_preset(name)
    except (cp.PresetStoreError, OSError) as e:
        return f"Could not delete: {e}"
    return (
        f"Deleted '{name}'."
        if deleted
        else f"No user preset named '{name}' (built-ins are protected)."
    )


def _record_routine_result(s: ChairSession, name: str, task: asyncio.Task[str]) -> None:
    if task.cancelled():
        return  # _cancel_routine records it
    exc = task.exception()
    if exc is not None:
        log.error("Routine '%s' failed", name, exc_info=exc)
        s.last_routine_result = f"'{name}' failed: {exc}"
    else:
        s.last_routine_result = f"'{name}': {task.result()}"


@mcp.tool(annotations=ACTUATES)
async def run_custom_preset(ctx: Context, name: str) -> str:
    """Start a custom routine. It runs in the background so you can check
    status or stop it at any time. Connects/powers on first if needed."""
    s = _s(ctx)
    presets = cp.all_presets()
    if name not in presets:
        return f"No custom preset '{name}'. Use list_custom_presets."
    if s.routine_running:
        return f"A routine ('{s.routine_name}') is already running. Stop it first."
    steps = presets[name]["steps"]
    err = cp.validate_steps(steps)
    if err:
        return f"Routine '{name}' is invalid: {err}"
    err = await _ensure_connected(s)
    if err:
        return err

    task = asyncio.create_task(cp.run_preset(s.chair, steps), name=f"routine:{name}")
    task.add_done_callback(lambda t: _record_routine_result(s, name, t))
    s.routine_task = task
    s.routine_name = name
    s.last_routine_result = ""
    return (
        f"Started custom routine '{name}' in the background "
        f"(~{cp.total_wait_seconds(steps):g}s of timed massage). Use get_status to "
        f"monitor or stop_custom_preset / stop to end it."
    )


@mcp.tool(annotations=ACTUATES)
async def stop_custom_preset(ctx: Context) -> str:
    """Stop a running custom routine and bring the chair to idle."""
    s = _s(ctx)
    if s.routine_running:
        await _cancel_routine(s)  # the routine stops the chair as it exits
        return "Custom routine stopped; chair is idle."
    if not s.chair.connected:
        return "No routine was running and the chair is not connected."
    try:
        await s.chair.stop()
    except ChairError as e:
        return f"Chair command failed: {e}"
    return "No routine was running. Sent stop to the chair anyway."


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    mcp.run()


if __name__ == "__main__":
    main()
