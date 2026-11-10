"""
custom_presets.py — user-defined massage routines for the OSIM uDiva3.

A routine is a JSON-serialisable list of steps, run in order over a live
UDivaDriver session. Each step is acknowledged by chair telemetry before the
next one runs, and cancelling a routine stops the chair.

Step schema:
    {"action": "manual", "area": "back"|"head_neck"}  start a manual massage:
                                                 position the seat and deploy
                                                 the rollers ("back" default)
    {"action": "seat_sofa"}                      sofa / resting position
    {"action": "seat_upright"}                   massage-upright position
    {"action": "seat_recline"}                   massage-reclined position
    {"action": "position", "value": 1-5}         shoulder roller height
    {"action": "tapping", "on": true|false}
    {"action": "kneading", "on": true|false}
    {"action": "rolling", "level": "off|partial|full"}
    {"action": "intensity", "zone": "shoulder|back|lumbar", "level": 0-5}
    {"action": "vgrip", "on": true|false}
    {"action": "leg_kneading", "level": "off|low|high"}
    {"action": "leg_vibration", "level": "off|low|high"}
    {"action": "wait", "seconds": N}             N > 0
    {"action": "preset", "name": "spa"}          run a factory program
    {"action": "stop"}

A working back routine: "manual" first, then an engaging technique (kneading
or tapping) — that is what starts the rollers — then rolling / intensity /
position adjustments.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import unicodedata
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import udiva_driver as drv
from protocol import LEG_LEVELS, MANUAL_AREAS, PRESETS, ROLLING_LEVELS, ZONES

log = logging.getLogger(__name__)

Step = dict[str, Any]
Routine = dict[str, Any]  # {"description": str, "steps": list[Step]}

STORE_PATH = Path.home() / ".udiva_presets.json"

# Limits. Routines drive a chair someone is sitting in, and their name and
# description are shown back to the AI model in later sessions, so bound both.
MAX_NAME_LEN = 64
MAX_DESCRIPTION_LEN = 300
MAX_STEPS = 100
MAX_WAIT_SECONDS = 30 * 60  # one wait step
MAX_TOTAL_WAIT_SECONDS = 90 * 60  # the whole routine
MAX_STEPS_JSON_LEN = 20_000

# -- built-in starter routines -----------------------------------------------
BUILTIN_CUSTOM: dict[str, Routine] = {
    "desk_neck_relief": {
        "description": "Targets the upper back and shoulders for desk/screen "
        "tension. Kneading focus up top. ~10 min.",
        "steps": [
            {"action": "manual", "area": "back"},
            {"action": "kneading", "on": True},
            {"action": "position", "value": 5},
            {"action": "intensity", "zone": "shoulder", "level": 4},
            {"action": "wait", "seconds": 300},
            {"action": "intensity", "zone": "shoulder", "level": 3},
            {"action": "wait", "seconds": 300},
            {"action": "stop"},
        ],
    },
    "full_back_descent": {
        "description": "Slow travel down the spine: shoulders → mid back → "
        "lumbar, tapping + kneading combined. ~15 min.",
        "steps": [
            {"action": "manual", "area": "back"},
            {"action": "kneading", "on": True},
            {"action": "tapping", "on": True},
            {"action": "position", "value": 5},
            {"action": "intensity", "zone": "shoulder", "level": 4},
            {"action": "wait", "seconds": 300},
            {"action": "position", "value": 3},
            {"action": "intensity", "zone": "back", "level": 4},
            {"action": "wait", "seconds": 300},
            {"action": "position", "value": 1},
            {"action": "intensity", "zone": "lumbar", "level": 5},
            {"action": "wait", "seconds": 300},
            {"action": "stop"},
        ],
    },
    "manual_only": {
        "description": "TEST: position for a back-body massage, then turn on "
        "kneading only and hold. Minimal massage (one technique).",
        "steps": [
            {"action": "manual", "area": "back"},
            {"action": "kneading", "on": True},
            {"action": "wait", "seconds": 120},
            {"action": "stop"},
        ],
    },
    "wind_down": {
        "description": "Gentle low-intensity relaxation: light back kneading "
        "with rolling + leg vibration. Good before sleep. ~12 min.",
        "steps": [
            {"action": "manual", "area": "back"},
            {"action": "kneading", "on": True},
            {"action": "rolling", "level": "partial"},
            {"action": "intensity", "zone": "back", "level": 2},
            {"action": "leg_vibration", "level": "low"},
            {"action": "wait", "seconds": 360},
            {"action": "intensity", "zone": "back", "level": 1},
            {"action": "wait", "seconds": 360},
            {"action": "stop"},
        ],
    },
    "leg_focus": {
        "description": "Lower-body session: calf kneading + foot vibration "
        "with gentle back kneading. ~10 min.",
        "steps": [
            {"action": "manual", "area": "back"},
            {"action": "kneading", "on": True},
            {"action": "intensity", "zone": "lumbar", "level": 2},
            {"action": "leg_kneading", "level": "high"},
            {"action": "leg_vibration", "level": "low"},
            {"action": "wait", "seconds": 300},
            {"action": "leg_kneading", "level": "low"},
            {"action": "leg_vibration", "level": "high"},
            {"action": "wait", "seconds": 300},
            {"action": "stop"},
        ],
    },
}


# -- storage -------------------------------------------------------------------
class PresetStoreError(RuntimeError):
    """The preset file exists but can't be read; refuse to overwrite it."""


def _read_store(path: Path) -> dict[str, Routine]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise PresetStoreError(f"Cannot read {path}: {e}. Fix or move the file.") from e
    if not isinstance(data, dict):
        raise PresetStoreError(f"{path} must contain a JSON object.")
    return data


def _write_store(path: Path, presets: dict[str, Routine]) -> None:
    """Write atomically so a crash can't leave a half-written file."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(presets, f, indent=2)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def load_user_presets(path: Path | None = None) -> dict[str, Routine]:
    """User-saved routines. An unreadable file is logged and treated as empty
    for reading; saving refuses to overwrite it (see save_user_preset)."""
    try:
        return _read_store(path or STORE_PATH)
    except PresetStoreError as e:
        log.error("%s", e)
        return {}


def save_user_preset(
    name: str, steps: list[Step], description: str = "", path: Path | None = None
) -> None:
    path = path or STORE_PATH
    presets = _read_store(path)
    presets[name] = {"description": description, "steps": steps}
    _write_store(path, presets)


def delete_user_preset(name: str, path: Path | None = None) -> bool:
    path = path or STORE_PATH
    presets = _read_store(path)
    if name not in presets:
        return False
    del presets[name]
    _write_store(path, presets)
    return True


def all_presets(path: Path | None = None) -> dict[str, Routine]:
    """Built-in routines plus user-saved ones (a user routine overrides a
    built-in of the same name)."""
    merged = dict(BUILTIN_CUSTOM)
    merged.update(load_user_presets(path))
    return merged


def total_wait_seconds(steps: list[Step]) -> float:
    return sum(s.get("seconds", 0) for s in steps if s.get("action") == "wait")


# -- validation ----------------------------------------------------------------
def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _check_on(step: Step) -> str | None:
    return None if isinstance(step.get("on"), bool) else "'on' must be true or false."


def _check_choice(key: str, choices: Any) -> Callable[[Step], str | None]:
    def check(step: Step) -> str | None:
        if step.get(key) not in choices:
            return f"'{key}' must be one of {list(choices)}."
        return None

    return check


def _check_range(key: str, lo: int, hi: int) -> Callable[[Step], str | None]:
    def check(step: Step) -> str | None:
        v = step.get(key)
        if not (_is_int(v) and lo <= v <= hi):
            return f"'{key}' must be an integer {lo}-{hi}."
        return None

    return check


def _check_manual(step: Step) -> str | None:
    if step.get("area", "back") not in MANUAL_AREAS:
        return f"'area' must be one of {list(MANUAL_AREAS)}."
    return None


def _check_intensity(step: Step) -> str | None:
    return _check_choice("zone", ZONES)(step) or _check_range("level", 0, 5)(step)


def _check_wait(step: Step) -> str | None:
    v = step.get("seconds")
    if not (_is_number(v) and 0 < v <= MAX_WAIT_SECONDS):
        return f"'seconds' must be a number between 0 and {MAX_WAIT_SECONDS}."
    return None


def _no_params(_step: Step) -> None:
    return None


_VALIDATORS: dict[str, Callable[[Step], str | None]] = {
    "manual": _check_manual,
    "seat_sofa": _no_params,
    "seat_upright": _no_params,
    "seat_recline": _no_params,
    "position": _check_range("value", 1, 5),
    "tapping": _check_on,
    "kneading": _check_on,
    "rolling": _check_choice("level", ROLLING_LEVELS),
    "intensity": _check_intensity,
    "vgrip": _check_on,
    "leg_kneading": _check_choice("level", LEG_LEVELS),
    "leg_vibration": _check_choice("level", LEG_LEVELS),
    "wait": _check_wait,
    "preset": _check_choice("name", PRESETS),
    "stop": _no_params,
}
ACTIONS = tuple(_VALIDATORS)


def validate_steps(steps: Any) -> str | None:
    """Return an error message if `steps` is not a valid routine, else None."""
    if not isinstance(steps, list) or not steps:
        return "steps must be a non-empty list."
    if len(steps) > MAX_STEPS:
        return f"a routine can have at most {MAX_STEPS} steps."
    for i, step in enumerate(steps, start=1):
        if not isinstance(step, dict) or "action" not in step:
            return f"step {i}: each step needs an 'action'."
        validator = _VALIDATORS.get(step["action"])
        if validator is None:
            return f"step {i}: unknown action {step['action']!r}. Options: {list(ACTIONS)}."
        err = validator(step)
        if err:
            return f"step {i} ({step['action']}): {err}"
    if total_wait_seconds(steps) > MAX_TOTAL_WAIT_SECONDS:
        return f"total wait time must be at most {MAX_TOTAL_WAIT_SECONDS // 60} minutes."
    return None


def _has_control_chars(text: str) -> bool:
    return any(unicodedata.category(c) in ("Cc", "Cf") for c in text)


def validate_name(name: str) -> str | None:
    if not name or not name.strip():
        return "name must not be empty."
    if len(name) > MAX_NAME_LEN:
        return f"name must be at most {MAX_NAME_LEN} characters."
    if _has_control_chars(name):
        return "name must not contain control characters."
    return None


def validate_description(description: str) -> str | None:
    if len(description) > MAX_DESCRIPTION_LEN:
        return f"description must be at most {MAX_DESCRIPTION_LEN} characters."
    if _has_control_chars(description.replace("\n", "")):
        return "description must not contain control characters."
    return None


def parse_steps_json(steps_json: str) -> tuple[Any, str | None]:
    """Parse a routine's JSON, bounding size and nesting. Returns (steps, error)."""
    if len(steps_json) > MAX_STEPS_JSON_LEN:
        return None, f"steps_json must be at most {MAX_STEPS_JSON_LEN} characters."
    try:
        return json.loads(steps_json), None
    except (ValueError, RecursionError) as e:
        return None, f"steps_json is not valid JSON: {e}"


# -- runner --------------------------------------------------------------------
def _step_call(chair: drv.UDivaDriver, step: Step) -> Awaitable[bool] | None:
    """The driver call for a step, or None for steps handled by the runner."""
    a = step["action"]
    if a == "manual":
        return chair.enter_manual_mode(step.get("area", "back"))
    if a == "seat_sofa":
        return chair.move_seat_sofa()
    if a == "seat_upright":
        return chair.move_seat_upright()
    if a == "seat_recline":
        return chair.recline_for_massage()
    if a == "preset":
        return chair.start_preset(step["name"])
    if a == "position":
        return chair.set_shoulder_position(step["value"])
    if a == "tapping":
        return chair.set_tapping(step["on"])
    if a == "kneading":
        return chair.set_kneading(step["on"])
    if a == "rolling":
        return chair.set_rolling(ROLLING_LEVELS[step["level"]])
    if a == "intensity":
        return chair.set_zone_intensity(ZONES[step["zone"]], step["level"])
    if a == "vgrip":
        return chair.set_vgrip(step["on"])
    if a == "leg_kneading":
        return chair.set_leg_kneading(LEG_LEVELS[step["level"]])
    if a == "leg_vibration":
        return chair.set_leg_vibration(LEG_LEVELS[step["level"]])
    return None


async def run_preset(
    chair: drv.UDivaDriver,
    steps: list[Step],
    on_progress: Callable[[int, Step], None] | None = None,
) -> str:
    """Run a routine on a connected chair and return a one-line summary.

    `on_progress(index, step)` is called before each step. Steps that need
    run state enter it themselves. Cancelling the task stops the chair.
    """
    err = validate_steps(steps)
    if err:
        return f"Invalid routine: {err}"
    if not chair.connected:
        return "Chair not connected."

    try:
        for i, step in enumerate(steps):
            if on_progress:
                on_progress(i, step)
            a = step["action"]
            if a == "wait":
                await asyncio.sleep(step["seconds"])
            elif a == "stop":
                await chair.stop()
            else:
                call = _step_call(chair, step)
                if call is None:  # unreachable: validate_steps covers every action
                    raise RuntimeError(f"no driver call for action {a!r}")
                if not await call:
                    await chair.stop()
                    return f"Step {i + 1}: chair did not acknowledge '{a}'; stopped."
        return f"Completed routine ({len(steps)} steps)."
    except drv.OperationInterrupted:
        return "Routine interrupted by stop."
    except asyncio.CancelledError:
        try:
            await chair.stop()
        except Exception as e:  # leaving anyway; don't mask the cancellation
            log.warning("stop() after cancelling routine failed: %s", e)
        raise
