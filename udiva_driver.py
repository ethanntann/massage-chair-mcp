"""
udiva_driver.py — async BLE driver for the OSIM uDiva3 (OS-8211) massage chair.

The wire protocol (commands, status frame layout) lives in protocol.py. This
module owns one live BLE session:

* connection lifecycle: discovery, handshake, keepalive, unexpected-disconnect
  handling (session state is reset so nothing stale survives a reconnect);
* write serialisation: every GATT write goes through one lock with a timeout,
  so the keepalive loop can never interleave with a command write;
* operation serialisation: high-level operations (start a preset, position the
  seat, toggle a technique) run one at a time, and stop() / disconnect()
  cancel whatever is running or queued before sending STOP;
* the technique state we know about (tapping, kneading, rolling), because the
  tapping/kneading commands are toggles and must not be sent blindly.

Readiness and completion are detected from status notifications rather than
fixed sleeps, so operations report whether the chair actually engaged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, TypeVar

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice

# Re-exported so existing `import udiva_driver as drv; drv.PRESETS` keeps working.
from protocol import (  # noqa: F401
    HANDSHAKE_1,
    HANDSHAKE_2,
    HEARTBEAT,
    IDLE_PROGRAMS,
    LEG_KNEAD,
    LEG_LEVELS,
    LEG_VIBE,
    MANUAL_AREAS,
    NOTIFY_UUID,
    POWER,
    PRESETS,
    ROLLING_LEVELS,
    SB_MOTOR,
    SB_PROGRAM,
    SB_SEAT_TRAVEL,
    SEAT_RECLINE,
    SEAT_SOFA,
    SEAT_SOFA_PREP,
    SEAT_UPRIGHT,
    START_MANUAL_BACK,
    START_MANUAL_BY_AREA,
    START_MANUAL_HEAD,
    STOP,
    TECH_KNEADING,
    TECH_TAPPING,
    VGRIP_OFF,
    VGRIP_ON,
    WRITE_UUID,
    ZONE_BACK,
    ZONE_LUMBAR,
    ZONE_SHOULDER,
    ZONES,
    build_cmd,
    echoes,
    is_status_frame,
    leg_cmd,
    preset_cmd,
    rolling_cmd,
    shoulder_position_cmd,
    zone_intensity_cmd,
)

log = logging.getLogger(__name__)

T = TypeVar("T")

# -- Device identification ---------------------------------------------------
# uDiva3 units advertise names like "OS8211 <serial>". Match by prefix unless
# ~/.udiva_config.json pins an exact name.
DEFAULT_NAME_PREFIXES = ["OS8211", "OSIM", "uDiva"]
CONFIG_PATH = Path.home() / ".udiva_config.json"
SCAN_TIMEOUT = 10.0

# Safety cap on one continuous massage; the MCP server stops the chair when it
# is reached. Long enough for any factory program or custom routine (at most
# 90 min of waits), configurable as "max_massage_minutes" in the config file.
DEFAULT_MAX_MASSAGE_MINUTES = 120
MAX_MASSAGE_MINUTES_RANGE = (10, 240)

# -- Timing ------------------------------------------------------------------
HEARTBEAT_INTERVAL = 0.7
HANDSHAKE_GAP = 0.3  # protocol pacing between handshake packets
WRITE_TIMEOUT = 5.0  # one ATT Write Request; far above normal (~tens of ms)
# Re-send the active preset every Nth keepalive tick (~3.5 s) rather than every
# tick, so one-shot commands aren't immediately overwritten.
MODE_REASSERT_EVERY = 5
MOTION_PULSE = 0.1  # cadence of a held command (the app uses ~100 ms)
STABLE_FRAMES = 4  # consecutive frames needed to call a state settled
OP_CANCEL_GRACE = 3.0  # how long stop() waits for a cancelled op to unwind


class ChairError(RuntimeError):
    """Base class for driver errors."""


class ChairNotFoundError(ChairError):
    pass


class ChairConnectionError(ChairError):
    """Not connected, or the BLE link failed mid-operation."""


class OperationInterrupted(ChairError):
    """The operation was cancelled by stop(), power_off() or disconnect()."""


def load_config(path: Path | None = None) -> dict[str, Any]:
    """Load optional user config from ~/.udiva_config.json.

    Keys (all optional):
      "address"  : the chair's BLE address (on macOS, the CoreBluetooth UUID
                   that `python scan.py` prints). Most specific: a device
                   spoofing the chair's name can't be picked up.
      "name"     : exact advertised name.
      "prefixes" : list of name prefixes, replacing the defaults.
    A missing file means defaults; an unreadable file or an invalid key is
    logged and ignored.
    """
    path = path or CONFIG_PATH
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        log.warning("Ignoring unreadable config %s: %s", path, e)
        return {}
    if not isinstance(data, dict):
        log.warning("Ignoring config %s: expected a JSON object", path)
        return {}

    cfg: dict[str, Any] = {}
    for key in ("address", "name"):
        v = data.get(key)
        if v is None:
            continue
        if isinstance(v, str) and v.strip():
            cfg[key] = v.strip()
        else:
            log.warning("Ignoring config %r: expected a non-empty string", key)
    prefixes = data.get("prefixes")
    if prefixes is not None:
        # A prefix of "" (or a bare string, iterated per character) would match
        # nearly every BLE device in range.
        if (
            isinstance(prefixes, list)
            and prefixes
            and all(isinstance(p, str) and len(p.strip()) >= 3 for p in prefixes)
        ):
            cfg["prefixes"] = [p.strip() for p in prefixes]
        else:
            log.warning("Ignoring config 'prefixes': expected a list of strings, 3+ chars each")
    limit = data.get("max_massage_minutes")
    if limit is not None:
        lo, hi = MAX_MASSAGE_MINUTES_RANGE
        if isinstance(limit, int) and not isinstance(limit, bool) and lo <= limit <= hi:
            cfg["max_massage_minutes"] = limit
        else:
            log.warning("Ignoring config 'max_massage_minutes': expected an integer %d-%d", lo, hi)
    return cfg


def name_matches(name: str | None, prefixes: list[str]) -> bool:
    if not name:
        return False
    low = name.lower()
    return any(low.startswith(p.lower()) for p in prefixes)


async def discover_chairs(
    timeout: float = SCAN_TIMEOUT, prefixes: list[str] | None = None
) -> list[dict[str, str]]:
    """Scan and return nearby chairs as [{"name": ..., "address": ...}]."""
    if prefixes is None:
        prefixes = load_config().get("prefixes", DEFAULT_NAME_PREFIXES)
    devices = await BleakScanner.discover(timeout=timeout)
    return [
        {"name": d.name, "address": d.address} for d in devices if name_matches(d.name, prefixes)
    ]


class UDivaDriver:
    """One live BLE session with the uDiva3."""

    def __init__(self, name: str | None = None):
        cfg = load_config()
        self._address: str | None = cfg.get("address")
        self._name: str | None = name or cfg.get("name")
        self._prefixes: list[str] = cfg.get("prefixes", DEFAULT_NAME_PREFIXES)
        self.max_massage_minutes: int = cfg.get("max_massage_minutes", DEFAULT_MAX_MASSAGE_MINUTES)
        self._client: BleakClient | None = None
        self._keepalive: asyncio.Task[None] | None = None

        self._connect_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._op_lock = asyncio.Lock()
        self._ops: set[asyncio.Task[Any]] = set()

        self._status: bytes | None = None
        self._status_seq = 0
        self._status_seen = asyncio.Event()
        # While True the keepalive stays silent: the real app sends no
        # heartbeats during a held motion command, and an injected heartbeat
        # makes the seat stop and restart.
        self._motion_in_progress = False
        self._reset_session_state()

    def _reset_session_state(self) -> None:
        """Forget everything we believe about the chair's state."""
        self._powered = False  # whether we've sent the run-state toggle
        self._mode_cmd: bytes | None = None  # preset re-sent by the keepalive
        self._status = None
        self._status_seen.clear()
        self._massage_started_at: float | None = None
        self.tapping = False
        self.kneading = False
        self.rolling_level = 0

    def _reset_techniques(self) -> None:
        self.tapping = False
        self.kneading = False
        self.rolling_level = 0

    # -- status ----------------------------------------------------------
    def _on_notify(self, _sender: Any, data: bytearray) -> None:
        # Do not filter on byte[2]: it is 0x08 only for the first few frames
        # of a motion, so filtering on it drops almost every motion update.
        if is_status_frame(data):
            self._status = bytes(data)
            self._status_seq += 1
            self._status_seen.set()

    def _on_disconnected(self, client: BleakClient) -> None:
        if client is not self._client:
            return  # a previous session's client
        log.warning("Chair disconnected unexpectedly; resetting session state")
        self._reset_session_state()

    @property
    def connected(self) -> bool:
        return bool(self._client and self._client.is_connected)

    def massage_elapsed(self) -> float | None:
        """Seconds since the current massage was started, or None if none is
        running as far as the driver knows."""
        if self._massage_started_at is None:
            return None
        return time.monotonic() - self._massage_started_at

    def _mark_massage_started(self) -> None:
        self._massage_started_at = time.monotonic()

    def _mark_massage_ended(self) -> None:
        self._massage_started_at = None

    @property
    def busy(self) -> bool:
        """True while an operation is running or queued."""
        return any(not t.done() for t in self._ops)

    def status(self) -> dict[str, Any]:
        st: dict[str, Any] = {"connected": self.connected, "powered": self._powered}
        s = self._status
        if not s:
            st["running"] = False
        else:
            prog = s[SB_PROGRAM]
            st.update(
                running=prog != 0x00,
                program_id=prog,
                active_zones=s[8],
                last_command=s[12],
                raw=s.hex(" "),
            )
        elapsed = self.massage_elapsed()
        if elapsed is not None:
            st["massage_minutes"] = round(elapsed / 60, 1)
            st["auto_stop_after_minutes"] = self.max_massage_minutes
        st["manual_techniques"] = {
            "tapping": self.tapping,
            "kneading": self.kneading,
            "rolling_level": self.rolling_level,
        }
        return st

    def _b(self, i: int) -> int | None:
        """Status byte i, or None if there is no frame yet."""
        s = self._status
        return s[i] if s and len(s) > i else None

    # -- waiting on telemetry --------------------------------------------
    async def _wait_for_status(self, timeout: float = 8.0) -> bool:
        if self._status is not None:
            return True
        try:
            await asyncio.wait_for(self._status_seen.wait(), timeout)
            return True
        except TimeoutError:
            return False

    async def _wait_for_fresh_status(self, after_seq: int, timeout: float = 1.0) -> bool:
        if self._status_seq > after_seq:
            return True
        self._status_seen.clear()
        if self._status_seq > after_seq:
            return True
        try:
            await asyncio.wait_for(self._status_seen.wait(), timeout)
            return self._status_seq > after_seq
        except TimeoutError:
            return False

    # -- writes ------------------------------------------------------------
    async def _write(self, payload: bytes) -> None:
        client = self._client
        if not (client and client.is_connected):
            raise ChairConnectionError("Not connected to chair")
        # Shielded so a cancelled caller never aborts an ATT write halfway: the
        # lock stays held until the chair's Write Response arrives, which keeps
        # responses matched to the right request.
        await asyncio.shield(self._locked_write(client, payload))

    async def _locked_write(self, client: BleakClient, payload: bytes) -> None:
        async with self._write_lock:
            try:
                # response=True (ATT Write Request) matches the OSIM app and
                # gives natural flow control.
                await asyncio.wait_for(
                    client.write_gatt_char(WRITE_UUID, payload, response=True),
                    WRITE_TIMEOUT,
                )
            except TimeoutError as e:
                raise ChairConnectionError(
                    f"Write {payload.hex(' ')} timed out after {WRITE_TIMEOUT}s"
                ) from e

    # -- operation serialisation -----------------------------------------
    async def _exclusive(self, fn: Callable[..., Awaitable[T]], *args: Any) -> T:
        """Run one high-level operation at a time.

        The operation runs in its own task so stop() can cancel it without
        cancelling the caller; the caller then gets OperationInterrupted.
        """

        async def run() -> T:
            async with self._op_lock:
                return await fn(*args)

        task = asyncio.create_task(run())
        self._ops.add(task)
        task.add_done_callback(self._ops.discard)
        try:
            return await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise  # our caller is being cancelled; propagate
            raise OperationInterrupted("Operation interrupted by stop") from None

    async def _cancel_ops(self) -> None:
        """Cancel every running or queued operation and let them unwind."""
        current = asyncio.current_task()
        pending = [t for t in self._ops if not t.done() and t is not current]
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.wait(pending, timeout=OP_CANCEL_GRACE)

    # -- connection ------------------------------------------------------
    async def _find_device(self) -> BLEDevice | None:
        """Locate the chair by pinned address, else exact name, else name prefix."""
        if self._address:
            return await BleakScanner.find_device_by_address(self._address, timeout=SCAN_TIMEOUT)
        if self._name:
            return await BleakScanner.find_device_by_name(self._name, timeout=SCAN_TIMEOUT)
        device = await BleakScanner.find_device_by_filter(
            lambda d, adv: name_matches(d.name or adv.local_name, self._prefixes),
            timeout=SCAN_TIMEOUT,
        )
        if device is not None and device.name:
            self._name = device.name  # faster, stable reconnects
        return device

    async def connect(self, ready_timeout: float = 8.0) -> bool:
        """Connect, handshake and start the keepalive.

        Returns True once the chair has sent a status frame. Raises
        ChairNotFoundError if no chair is advertising.
        """
        async with self._connect_lock:
            if self.connected:
                return await self._wait_for_status(timeout=ready_timeout)
            await self._teardown()  # drop any half-dead previous session

            device = await self._find_device()
            if device is None:
                target = self._address or self._name or f"any of {self._prefixes}"
                raise ChairNotFoundError(
                    f"No chair matching '{target}' found. Check that it is "
                    f"plugged in, in range, and that the OSIM app is "
                    f"disconnected (the chair accepts one BLE connection). "
                    f"`python scan.py` lists what is nearby."
                )

            self._reset_session_state()
            client = BleakClient(device, disconnected_callback=self._on_disconnected)
            self._client = client
            try:
                await client.connect()
                await client.start_notify(NOTIFY_UUID, self._on_notify)
                await self._write(HANDSHAKE_1)
                await asyncio.sleep(HANDSHAKE_GAP)
                await self._write(HANDSHAKE_2)
                await asyncio.sleep(HANDSHAKE_GAP)
            except BaseException:
                await self._teardown()
                raise
            self._keepalive = asyncio.create_task(self._keepalive_loop())
            log.info("Connected to %s", device.name or device.address)
            return await self._wait_for_status(timeout=ready_timeout)

    async def _teardown(self) -> None:
        if self._keepalive:
            self._keepalive.cancel()
            try:
                await self._keepalive
            except asyncio.CancelledError:
                pass
            self._keepalive = None
        client, self._client = self._client, None
        if client is not None and client.is_connected:
            try:
                await client.stop_notify(NOTIFY_UUID)
            except Exception as e:  # best effort; we're disconnecting anyway
                log.debug("stop_notify failed: %s", e)
            await client.disconnect()
        self._reset_session_state()

    async def disconnect(self) -> None:
        await self._cancel_ops()
        await self._teardown()

    async def _keepalive_loop(self) -> None:
        """Heartbeat every tick; re-send the active preset every Nth tick.

        Silent while a held motion command owns the channel.
        """
        tick = 0
        while self.connected:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            tick += 1
            if self._motion_in_progress:
                continue
            try:
                await self._write(HEARTBEAT)
                if self._mode_cmd is not None and (tick - 1) % MODE_REASSERT_EVERY == 0:
                    await self._write(self._mode_cmd)
            except ChairConnectionError as e:
                if not self.connected:
                    break
                log.warning("Keepalive write failed: %s", e)
            except Exception:
                log.exception("Keepalive write failed")
                if not self.connected:
                    break
        log.info("Keepalive stopped")

    # -- power -----------------------------------------------------------
    async def power_on(self) -> bool:
        """Put the chair into its run state (the c3 01 toggle).

        Not a mains switch: the chair is already powered (its radio answered).
        No status frame is required; some units only stream status once a
        program runs, and start_preset / manual mode confirm via telemetry.
        """
        if not self.connected:
            return False
        await self._exclusive(self._ensure_run_state)
        return True

    async def power_off(self) -> None:
        await self._cancel_ops()
        self._mode_cmd = None
        if self.connected:
            await self._write(POWER)
        self._powered = False
        self._reset_techniques()
        self._mark_massage_ended()

    async def stop(self, timeout: float = 3.0) -> bool:
        """Stop whatever is running (any in-flight or queued operation, then
        the chair's program). The chair stays in run state.

        Returns True if the chair sent a status frame after STOP.
        """
        await self._cancel_ops()
        self._mode_cmd = None
        self._reset_techniques()
        before = self._status_seq
        await self._write(STOP)
        self._mark_massage_ended()
        await self._wait_for_fresh_status(before, timeout)
        return self._status_seq > before

    # -- run state ---------------------------------------------------------
    async def _ensure_run_state(self) -> None:
        """Send the c3 01 run-state toggle once per session.

        Tracked by _powered so we never double-toggle (which would stop a
        running chair).
        """
        if not self._powered:
            before = self._status_seq
            await self._write(POWER)
            self._powered = True
            await self._wait_for_fresh_status(before, timeout=2.0)  # best effort

    async def _re_enter_run_state(self) -> None:
        """Re-send c3 01 after seat motion.

        Seat motion (upright, recline, sofa) leaves run state; the OSIM app
        sends c3 01 again right after every motion sequence. Without it,
        manual commands are echoed but have no physical effect.
        """
        self._powered = False
        self._motion_in_progress = True
        try:
            await self._write(POWER)
            self._powered = True
        finally:
            self._motion_in_progress = False

    # -- command helpers -------------------------------------------------
    async def _send_until(
        self,
        command: bytes,
        predicate: Callable[[bytes | None], bool],
        timeout: float = 8.0,
        interval: float = 0.7,
    ) -> bool:
        """Send `command` until a fresh status frame satisfies `predicate`."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if echoes(command, self._status):
                # Clear a stale echo first so a matching echo must come from
                # this attempt, not from an earlier command.
                before_clear = self._status_seq
                await self._write(HEARTBEAT)
                remaining = max(0.0, deadline - time.monotonic())
                await self._wait_for_fresh_status(before_clear, min(interval, remaining))
                if echoes(command, self._status):
                    continue
            before = self._status_seq
            await self._write(command)
            remaining = max(0.0, deadline - time.monotonic())
            await self._wait_for_fresh_status(before, min(interval, remaining))
            if self._status_seq > before and predicate(self._status):
                return True
        return False

    async def _send_until_echo(
        self, command: bytes, timeout: float = 4.0, interval: float = 0.4
    ) -> bool:
        return await self._send_until(
            command, lambda s: echoes(command, s), timeout=timeout, interval=interval
        )

    # -- seat motion -------------------------------------------------------
    async def _drive_motion_until_settled(
        self,
        command: bytes,
        timeout: float = 30.0,
        min_hold: float = 2.0,
        motor_stable_frames: int = STABLE_FRAMES,
    ) -> bool:
        """Hold a posture command (upright / recline) until its motor stops.

        byte[3] is the motor flag for posture moves (non-zero running, 0x00
        stopped) and does return to 0x00 at the travel limit while the
        command is still held. Done once min_hold has elapsed and either
        (a) the motor ran and has been stopped for motor_stable_frames frames,
        or (b) the chair echoed the command but the motor never ran (already
        at the destination).
        """
        self._motion_in_progress = True
        try:
            deadline = time.monotonic() + timeout
            started = time.monotonic()
            saw_echo = False
            motor_was_active = False
            motor_stopped_frames = 0

            while time.monotonic() < deadline:
                before = self._status_seq
                await self._write(command)
                remaining = max(0.0, deadline - time.monotonic())
                await self._wait_for_fresh_status(before, min(MOTION_PULSE, remaining))

                elapsed = time.monotonic() - started
                if self._status and len(self._status) > SB_PROGRAM:
                    if echoes(command, self._status):
                        saw_echo = True
                    if self._status[SB_MOTOR] != 0x00:
                        motor_was_active = True
                        motor_stopped_frames = 0
                    elif motor_was_active:
                        motor_stopped_frames += 1

                    if (
                        saw_echo
                        and motor_was_active
                        and motor_stopped_frames >= motor_stable_frames
                        and elapsed >= min_hold
                    ):
                        return True
                    if saw_echo and not motor_was_active and elapsed >= min_hold:
                        return True
            return False
        finally:
            self._motion_in_progress = False

    async def _settle_until(self, predicate: Callable[[bytes], bool], deadline: float) -> bool:
        """Heartbeat at protocol cadence until `predicate(status)` holds for
        STABLE_FRAMES consecutive fresh frames, or `deadline` passes. Caller
        must hold _motion_in_progress."""
        hits = 0
        while time.monotonic() < deadline:
            before = self._status_seq
            await self._write(HEARTBEAT)
            await self._wait_for_fresh_status(before, MOTION_PULSE)
            if self._status_seq > before and self._status and predicate(self._status):
                hits += 1
                if hits >= STABLE_FRAMES:
                    return True
            else:
                hits = 0
        return False

    async def _recline(self, timeout: float = 30.0) -> bool:
        await self._ensure_run_state()
        result = await self._drive_motion_until_settled(SEAT_RECLINE, timeout=timeout)
        await self._re_enter_run_state()
        return result

    async def _upright(self, timeout: float = 30.0) -> bool:
        await self._ensure_run_state()
        result = await self._drive_motion_until_settled(SEAT_UPRIGHT, timeout=timeout)
        await self._re_enter_run_state()
        return result

    async def _sofa(self, timeout: float = 30.0, motor_stable_frames: int = STABLE_FRAMES) -> bool:
        """Return to the sofa/resting position.

        Protocol: c3 01 (always: stop() leaves the chair out of run state while
        _powered is still True), a prep burst of c3 07 00 ca x3, then hold
        c3 08 ff ca at ~100 ms.

        C3 commands get only 1-byte ATT acks, not status frames, so the motor
        is observed by interleaving one heartbeat probe (which does get a full
        frame) every `motor_stable_frames` sends. Done when the motor ran and
        then read stopped for `motor_stable_frames` consecutive probes, or
        never ran within that many probes (already at sofa).
        """
        self._reset_techniques()
        self._mark_massage_ended()
        self._motion_in_progress = True
        try:
            await self._write(POWER)
            self._powered = True
            await asyncio.sleep(MOTION_PULSE)

            for _ in range(3):
                await self._write(SEAT_SOFA_PREP)
                await asyncio.sleep(MOTION_PULSE)

            deadline = time.monotonic() + timeout
            send_count = 0
            probe_count = 0
            motor_was_active = False
            motor_stopped = 0
            result = False

            while time.monotonic() < deadline:
                await self._write(SEAT_SOFA)
                await asyncio.sleep(MOTION_PULSE)
                send_count += 1
                if send_count % motor_stable_frames:
                    continue

                before_probe = self._status_seq
                await self._write(HEARTBEAT)
                await self._wait_for_fresh_status(before_probe, 0.4)
                probe_count += 1
                if not (self._status and len(self._status) > SB_MOTOR):
                    continue
                if self._status[SB_MOTOR] != 0x00:
                    motor_was_active = True
                    motor_stopped = 0
                elif motor_was_active:
                    motor_stopped += 1
                if motor_was_active and motor_stopped >= motor_stable_frames:
                    result = True
                    break
                if not motor_was_active and probe_count >= motor_stable_frames:
                    result = True
                    break
        finally:
            self._motion_in_progress = False
        await self._re_enter_run_state()
        return result

    async def recline_for_massage(self, timeout: float = 30.0) -> bool:
        """Move to the massage-reclined position (c5 1e)."""
        return await self._exclusive(self._recline, timeout)

    async def move_seat_upright(self, timeout: float = 30.0) -> bool:
        """Posture only: move the seat upright (c5 1d). Does not start a
        massage; use start_manual_massage for that."""
        return await self._exclusive(self._upright, timeout)

    async def move_seat_sofa(self, timeout: float = 30.0) -> bool:
        """Return the chair to the sofa/resting position."""
        return await self._exclusive(self._sofa, timeout)

    # -- programs ----------------------------------------------------------
    async def _start_preset(self, name: str, timeout: float) -> bool:
        # Clock starts on the attempt: even an unconfirmed start may leave the
        # chair running, and the auto-stop's STOP is harmless on an idle chair.
        self._mark_massage_started()
        await self._ensure_run_state()
        if not await self._recline():
            return False
        cmd = preset_cmd(name)
        ok = await self._send_until(
            cmd,
            predicate=lambda s: bool(s and s[SB_PROGRAM] != 0x00 and echoes(cmd, s)),
            timeout=timeout,
        )
        self._reset_techniques()
        if ok:
            self._mode_cmd = cmd
        return ok

    async def start_preset(self, name: str, timeout: float = 10.0) -> bool:
        """Start a factory program. Returns True once the chair reports a
        program running. Enters run state and reclines first as needed."""
        if name not in PRESETS:
            raise ValueError(f"Unknown preset '{name}'. Options: {list(PRESETS)}")
        return await self._exclusive(self._start_preset, name, timeout)

    async def _start_manual(self, area: str, timeout: float) -> bool:
        start_cmd = START_MANUAL_BY_AREA[area]
        self._mode_cmd = None
        self._reset_techniques()
        self._mark_massage_started()
        await self._ensure_run_state()
        deadline = time.monotonic() + min(timeout, 90.0)

        # Back Body: c5 16 only drives the footrest, so position the backrest
        # (and footrest) with c5 1d first, which has reliable motor-stop
        # detection. Head & Neck's c5 0e drives the backrest itself.
        if area == "back":
            await self._drive_motion_until_settled(SEAT_UPRIGHT, timeout=30.0)

        self._motion_in_progress = True
        try:
            # Hold the start command continuously; pausing makes the motor
            # stutter. byte[1] is the seat-travel flag: non-zero while moving,
            # back to 0x00 at the limit while still held. If it never goes
            # non-zero the seat is already positioned; proceed after a short
            # context-setting burst. (byte[4] is unusable here: c5 16 travels
            # with byte[4]=0x08, which reads as idle.)
            moving = False
            stopped_frames = 0
            quiet_frames = 0
            reached = False
            while time.monotonic() < deadline:
                before = self._status_seq
                await self._write(start_cmd)
                await self._wait_for_fresh_status(before, MOTION_PULSE)
                if self._b(SB_SEAT_TRAVEL):
                    moving = True
                    stopped_frames = 0
                elif moving:
                    stopped_frames += 1
                    if stopped_frames >= STABLE_FRAMES:
                        reached = True
                        break
                else:
                    quiet_frames += 1
                    if quiet_frames >= 2 * STABLE_FRAMES:
                        reached = True
                        break
            if not reached:
                return False
            # After release the rollers deploy (byte[4] -> 0x80) and must settle
            # to an idle program before a technique toggle has any effect.
            return await self._settle_until(
                lambda s: s[SB_MOTOR] == 0x00 and s[SB_PROGRAM] in IDLE_PROGRAMS,
                deadline,
            )
        finally:
            self._motion_in_progress = False

    async def start_manual_massage(self, area: str = "back", timeout: float = 120.0) -> bool:
        """Start a manual massage and leave the chair settled for technique steps.

        area: "back" (Back Body, c5 16) or "head_neck" (Head & Neck, c5 0e).

        The start command held at ~100 ms drives the seat to its limit; the
        roller deploy begins only after it is released, and only if the seat
        reached the limit. So: hold until the seat-travel flag shows the limit
        was reached, release, then wait for the deploy to settle to idle.
        Returns False if that doesn't happen within the timeout.
        """
        if area not in START_MANUAL_BY_AREA:
            raise ValueError(f"area must be one of {list(START_MANUAL_BY_AREA)}")
        return await self._exclusive(self._start_manual, area, timeout)

    async def enter_manual_mode(self, area: str = "back") -> bool:
        """Alias of start_manual_massage (the routine's "manual" step)."""
        return await self.start_manual_massage(area=area)

    # -- manual techniques -------------------------------------------------
    async def _toggle(self, command: bytes) -> bool:
        return await self._exclusive(self._send_until_echo, command)

    async def toggle_tapping(self) -> bool:
        ok = await self._toggle(TECH_TAPPING)
        if ok:
            self.tapping = not self.tapping
        return ok

    async def toggle_kneading(self) -> bool:
        ok = await self._toggle(TECH_KNEADING)
        if ok:
            self.kneading = not self.kneading
        return ok

    async def set_tapping(self, on: bool) -> bool:
        """Turn tapping on/off. The command is a toggle, so it is only sent
        when the known state differs. Returns whether the chair acknowledged
        (True without sending if already in that state)."""
        if self.tapping == on:
            return True
        return await self.toggle_tapping()

    async def set_kneading(self, on: bool) -> bool:
        """Turn kneading on/off; see set_tapping."""
        if self.kneading == on:
            return True
        return await self.toggle_kneading()

    async def set_rolling(self, level: int) -> bool:
        """Rolling: 0 off, 1 partial, 2 full."""
        ok = await self._toggle(rolling_cmd(level))
        if ok:
            self.rolling_level = level
        return ok

    async def set_shoulder_position(self, pos: int) -> bool:
        """Shoulder roller height, 1..5."""
        return await self._toggle(shoulder_position_cmd(pos))

    async def set_zone_intensity(self, zone: int, level: int) -> bool:
        """3D intensity 0..5 for a roller zone (ZONE_* constants)."""
        return await self._toggle(zone_intensity_cmd(zone, level))

    async def set_vgrip(self, on: bool) -> bool:
        return await self._toggle(VGRIP_ON if on else VGRIP_OFF)

    async def set_leg_kneading(self, level: int) -> bool:
        """0 off, 3 low, 4 high."""
        return await self._toggle(leg_cmd(LEG_KNEAD, level))

    async def set_leg_vibration(self, level: int) -> bool:
        """0 off, 3 low, 4 high."""
        return await self._toggle(leg_cmd(LEG_VIBE, level))


# -- standalone smoke test (drives the real chair) -----------------------------
async def _demo() -> None:
    chair = UDivaDriver()
    print("Connecting...")
    await chair.connect()
    print("Spa preset...")
    print("  engaged:", await chair.start_preset("spa"))
    print("Status:", chair.status())
    await asyncio.sleep(15)
    print("Stop...")
    print("  stopped:", await chair.stop())
    await chair.disconnect()
    print("Done.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_demo())
