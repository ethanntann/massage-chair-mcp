"""Test doubles: a simulated chair behind a fake BleakClient, and a fake driver."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

import udiva_driver as drv

Responder = Callable[[bytes], "bytes | None"]


def frame(*, echo: int = 0x00, program: int = 0x00, motor: int = 0x00, travel: int = 0x00) -> bytes:
    """A 14-byte status frame with the fields the driver reads."""
    f = bytearray(14)
    f[0] = 0xC5
    f[1] = travel
    f[3] = motor
    f[4] = program
    f[12] = echo
    return bytes(f)


def echo_responder(cmd: bytes) -> bytes | None:
    """Default chair: acknowledge every C5 command with an echo, idle state."""
    return frame(echo=cmd[1]) if cmd[0] == 0xC5 else None


class FakeBleakClient:
    """Stands in for bleak.BleakClient. Records writes and answers each one
    with whatever status frame `responder` returns."""

    instances: ClassVar[list[FakeBleakClient]] = []
    responder: Responder = staticmethod(echo_responder)
    write_delay = 0.0
    fail_start_notify = False

    def __init__(self, device: Any, disconnected_callback=None):
        self.device = device
        self.disconnected_callback = disconnected_callback
        self.is_connected = False
        self.writes: list[bytes] = []
        self.inflight = 0
        self.max_inflight = 0
        self._notify = None
        FakeBleakClient.instances.append(self)

    async def connect(self) -> None:
        self.is_connected = True

    async def disconnect(self) -> None:
        self.is_connected = False
        if self.disconnected_callback:
            self.disconnected_callback(self)

    async def start_notify(self, _uuid, callback) -> None:
        if FakeBleakClient.fail_start_notify:
            raise RuntimeError("notify failed")
        self._notify = callback

    async def stop_notify(self, _uuid) -> None:
        self._notify = None

    async def write_gatt_char(self, _uuid, data: bytes, response: bool = True) -> None:
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            await asyncio.sleep(FakeBleakClient.write_delay)
            self.writes.append(bytes(data))
            reply = FakeBleakClient.responder(bytes(data))
            if reply is not None and self._notify:
                asyncio.get_running_loop().call_soon(self._notify, None, bytearray(reply))
        finally:
            self.inflight -= 1

    def drop(self) -> None:
        """Simulate the link dropping (out of range, chair unplugged)."""
        self.is_connected = False
        if self.disconnected_callback:
            self.disconnected_callback(self)


class FakeScanner:
    @staticmethod
    async def find_device_by_name(name, timeout=10.0):
        return SimpleNamespace(name=name, address="AA:BB")

    @staticmethod
    async def find_device_by_filter(fn, timeout=10.0):
        d = SimpleNamespace(name="OS8211 TEST", address="AA:BB")
        return d if fn(d, SimpleNamespace(local_name=d.name)) else None

    @staticmethod
    async def discover(timeout=10.0):
        return [SimpleNamespace(name="OS8211 TEST", address="AA:BB")]


@pytest.fixture
def fake_ble(monkeypatch, tmp_path):
    FakeBleakClient.instances = []
    FakeBleakClient.responder = staticmethod(echo_responder)
    FakeBleakClient.write_delay = 0.0
    FakeBleakClient.fail_start_notify = False
    monkeypatch.setattr(drv, "BleakClient", FakeBleakClient)
    monkeypatch.setattr(drv, "BleakScanner", FakeScanner)
    monkeypatch.setattr(drv, "CONFIG_PATH", tmp_path / "no-config.json")
    monkeypatch.setattr(drv, "HANDSHAKE_GAP", 0.0)
    return FakeBleakClient


@pytest.fixture
async def chair(fake_ble):
    c = drv.UDivaDriver()
    assert await c.connect(ready_timeout=1.0)
    yield c
    await c.disconnect()


class FakeChair:
    """A UDivaDriver stand-in for routine/server tests. Records calls; any
    method listed in `fail` returns False (not acknowledged)."""

    def __init__(self, fail: set[str] | None = None):
        self.connected = True
        self.calls: list[tuple] = []
        self.fail = fail or set()
        self.tapping = self.kneading = False
        self.rolling_level = 0
        self.block = asyncio.Event()  # set_* waits on this if `hold` is True
        self.hold = False
        self.max_massage_minutes = 120
        self.elapsed: float | None = None

    def massage_elapsed(self):
        return self.elapsed

    async def _do(self, name: str, *args) -> bool:
        self.calls.append((name, *args))
        if self.hold:
            await self.block.wait()
        return name not in self.fail

    async def connect(self):
        self.connected = True
        return True

    async def disconnect(self):
        self.calls.append(("disconnect",))
        self.connected = False

    async def stop(self):
        self.calls.append(("stop",))
        self.elapsed = None
        self.tapping = self.kneading = False
        return True

    async def power_off(self):
        self.calls.append(("power_off",))

    async def set_tapping(self, on):
        ok = await self._do("set_tapping", on)
        if ok:
            self.tapping = on
        return ok

    async def set_kneading(self, on):
        ok = await self._do("set_kneading", on)
        if ok:
            self.kneading = on
        return ok

    def __getattr__(self, name):
        if name.startswith(("set_", "enter_", "start_", "move_", "recline")):
            return lambda *a: self._do(name, *a)
        raise AttributeError(name)

    def status(self):
        return {
            "connected": self.connected,
            "manual_techniques": {"tapping": self.tapping, "kneading": self.kneading},
        }
