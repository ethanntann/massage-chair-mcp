"""
run_routine.py — run a custom routine or factory program from the command
line, without the MCP layer. Drives the real chair.

Usage:
    python run_routine.py                      # list routines and factory presets
    python run_routine.py wind_down            # run a built-in or saved routine
    python run_routine.py --file my.json       # a JSON array of steps, or an
                                               # object with a "steps" key
    python run_routine.py --preset spa         # factory program for 300 s, then stop
    python run_routine.py --preset spa 120     # ... for 120 s
    python run_routine.py wind_down --no-sofa  # don't return to sofa afterwards

When the routine ends (or Ctrl+C), the chair returns to the sofa position,
leaves run state and disconnects.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path

import custom_presets as cp
import udiva_driver as drv
from protocol import PRESETS

SOFA_TIMEOUT = 35.0
DEFAULT_PRESET_SECONDS = 300


def _print_routines() -> None:
    print("Available routines:\n")
    for name, body in cp.all_presets().items():
        steps = body.get("steps", [])
        print(f"  {name}")
        print(f"      {len(steps)} steps, ~{cp.total_wait_seconds(steps):g}s timed massage")
        if body.get("description"):
            print(f"      {body['description']}")
    print("\nRun one with:  python run_routine.py <name>")
    print("\nFactory presets (python run_routine.py --preset <name> [seconds]):\n")
    print("  " + ", ".join(PRESETS))


def _describe(step: cp.Step) -> str:
    a = step.get("action")
    if a == "wait":
        return f"wait {step['seconds']}s"
    if a in ("seat_sofa", "seat_upright", "seat_recline"):
        return f"move seat to {a.removeprefix('seat_')} position"
    if a == "position":
        return f"set shoulder position {step['value']}/5"
    if a == "intensity":
        return f"set {step['zone']} intensity {step['level']}"
    if a in ("tapping", "kneading", "vgrip"):
        return f"{a} {'on' if step.get('on') else 'off'}"
    if a == "rolling":
        return f"rolling {step['level']}"
    if a in ("leg_kneading", "leg_vibration"):
        return f"{a.replace('_', ' ')} {step['level']}"
    if a == "preset":
        return f"start factory preset '{step['name']}'"
    if a == "manual":
        return f"start manual {step.get('area', 'back')} massage"
    return a or "?"


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run a uDiva3 routine or factory program.")
    g = p.add_mutually_exclusive_group()
    g.add_argument("routine", nargs="?", help="built-in or saved routine name")
    g.add_argument("-f", "--file", type=Path, help="routine JSON file")
    g.add_argument(
        "-p",
        "--preset",
        nargs="+",
        metavar="ARG",
        help="NAME [SECONDS]: run a factory program (default 300 s)",
    )
    p.add_argument("--no-sofa", action="store_true", help="skip the return to sofa")
    args = p.parse_args()
    args.seconds = DEFAULT_PRESET_SECONDS
    if args.preset:
        if len(args.preset) > 2 or args.preset[0] not in PRESETS:
            p.error(f"--preset NAME [SECONDS]; NAME is one of: {', '.join(PRESETS)}")
        if len(args.preset) == 2:
            try:
                args.seconds = int(args.preset[1])
            except ValueError:
                p.error(f"SECONDS must be an integer, got {args.preset[1]!r}")
        args.preset = args.preset[0]
    return args


def _load_steps(args: argparse.Namespace) -> tuple[list[cp.Step] | None, str]:
    if args.preset:
        steps = [
            {"action": "preset", "name": args.preset},
            {"action": "wait", "seconds": args.seconds},
            {"action": "stop"},
        ]
        return steps, f"preset:{args.preset}"
    if args.file:
        data = json.loads(args.file.read_text())
        steps = data["steps"] if isinstance(data, dict) and "steps" in data else data
        return steps, f"file:{args.file}"
    routines = cp.all_presets()
    if args.routine not in routines:
        return None, args.routine
    return routines[args.routine]["steps"], args.routine


async def main() -> None:
    args = _parse_args()
    if not (args.routine or args.file or args.preset):
        _print_routines()
        return

    steps, label = _load_steps(args)
    if steps is None:
        print(f"No routine named '{label}'.\n")
        _print_routines()
        return
    err = cp.validate_steps(steps)
    if err:
        print(f"Routine is invalid: {err}")
        return

    total = len(steps)
    print(f"Running '{label}': {total} steps, ~{cp.total_wait_seconds(steps):g}s of massage.")
    print("Connecting to chair...\n")

    chair = drv.UDivaDriver()
    try:
        await chair.connect()
    except drv.ChairNotFoundError as e:
        print(f"Could not find the chair: {e}")
        return

    def on_progress(i: int, step: cp.Step) -> None:
        print(f"  [{i + 1}/{total}] {_describe(step)}")

    try:
        print(f"\n{await cp.run_preset(chair, steps, on_progress=on_progress)}")
    except asyncio.CancelledError:  # Ctrl+C; run_preset already sent stop
        print("\nInterrupted — chair stopped.")
    finally:
        # Each cleanup step is guarded so one failure can't skip the rest.
        if not args.no_sofa and chair.connected:
            print("Returning seat to sofa position...")
            try:
                await asyncio.wait_for(chair.move_seat_sofa(), timeout=SOFA_TIMEOUT)
            except Exception as e:
                print(f"  (sofa return skipped: {e})")
        try:
            await chair.power_off()
        except Exception as e:
            print(f"  (power off failed: {e})")
        await chair.disconnect()
        print("Chair powered off. Done.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
