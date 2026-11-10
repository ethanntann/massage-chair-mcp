"""UDivaDriver against a simulated chair (see conftest.FakeBleakClient)."""

import asyncio

import pytest
from conftest import echo_responder, frame

import protocol as p
import udiva_driver as drv


async def test_connect_runs_handshake_and_starts_keepalive(chair, fake_ble):
    client = fake_ble.instances[-1]
    assert client.writes[:2] == [p.HANDSHAKE_1, p.HANDSHAKE_2]
    assert chair.connected
    await asyncio.sleep(drv.HEARTBEAT_INTERVAL + 0.2)
    assert p.HEARTBEAT in client.writes


async def test_connect_when_already_connected_reuses_session(chair, fake_ble):
    assert await chair.connect()
    assert len(fake_ble.instances) == 1


async def test_failed_connect_tears_down(fake_ble):
    fake_ble.fail_start_notify = True
    c = drv.UDivaDriver()
    with pytest.raises(RuntimeError, match="notify failed"):
        await c.connect()
    assert not c.connected
    assert not fake_ble.instances[-1].is_connected
    assert c._keepalive is None


async def test_chair_not_found(fake_ble, monkeypatch):
    async def nothing(*_a, **_k):
        return None

    monkeypatch.setattr(drv.BleakScanner, "find_device_by_filter", nothing)
    with pytest.raises(drv.ChairNotFoundError, match=r"scan\.py"):
        await drv.UDivaDriver().connect()


async def test_writes_never_overlap(chair, fake_ble, monkeypatch):
    """Keepalive heartbeats and command writes must be serialised."""
    monkeypatch.setattr(drv, "HEARTBEAT_INTERVAL", 0.01)
    client = fake_ble.instances[-1]
    fake_ble.write_delay = 0.005
    chair._keepalive.cancel()
    chair._keepalive = asyncio.create_task(chair._keepalive_loop())
    await asyncio.gather(*(chair.set_zone_intensity(p.ZONE_BACK, i % 5) for i in range(5)))
    await asyncio.sleep(0.05)
    assert client.max_inflight == 1


async def test_write_timeout_raises(chair, fake_ble, monkeypatch):
    monkeypatch.setattr(drv, "WRITE_TIMEOUT", 0.05)
    fake_ble.write_delay = 1.0
    with pytest.raises(drv.ChairConnectionError, match="timed out"):
        await chair._write(p.HEARTBEAT)


async def test_kneading_toggle_is_idempotent(chair, fake_ble):
    client = fake_ble.instances[-1]
    assert await chair.set_kneading(True)
    assert await chair.set_kneading(True)
    assert client.writes.count(p.TECH_KNEADING) == 1
    assert chair.kneading
    assert await chair.set_kneading(False)
    assert client.writes.count(p.TECH_KNEADING) == 2
    assert not chair.kneading


async def test_unacknowledged_toggle_keeps_state(chair, fake_ble, monkeypatch):
    fake_ble.responder = staticmethod(lambda cmd: frame(echo=0x00))  # never echoes
    monkeypatch.setattr(drv.UDivaDriver, "_send_until_echo", _fast_echo_wait)
    assert not await chair.set_tapping(True)
    assert chair.tapping is False


async def _fast_echo_wait(self, command, timeout=0.3, interval=0.05):
    return await self._send_until(command, lambda s: p.echoes(command, s), timeout, interval)


async def test_start_preset_sets_mode_and_stop_clears_it(chair, fake_ble, monkeypatch):
    async def recline(self, timeout=30.0):
        return True

    monkeypatch.setattr(drv.UDivaDriver, "_recline", recline)
    fake_ble.responder = staticmethod(
        lambda cmd: frame(echo=cmd[1], program=0x34) if cmd[0] == 0xC5 else None
    )
    assert await chair.start_preset("spa")
    assert chair._mode_cmd == p.preset_cmd("spa")
    assert await chair.stop()
    assert chair._mode_cmd is None
    assert fake_ble.instances[-1].writes[-1] == p.STOP


async def test_unknown_preset_rejected(chair):
    with pytest.raises(ValueError):
        await chair.start_preset("nope")


async def test_stop_interrupts_manual_positioning(chair, fake_ble):
    """A stop() during the 90 s manual start must halt the start-command stream."""
    # Seat "travels" forever: the start loop would otherwise run until timeout.
    fake_ble.responder = staticmethod(lambda cmd: frame(echo=cmd[1], travel=0x80))
    client = fake_ble.instances[-1]
    task = asyncio.create_task(chair.start_manual_massage("head_neck"))
    await asyncio.sleep(0.5)
    assert chair.busy
    assert await chair.stop()
    with pytest.raises(drv.OperationInterrupted):
        await task
    assert not chair.busy
    assert not chair._motion_in_progress
    stop_at = len(client.writes) - 1 - client.writes[::-1].index(p.STOP)
    assert p.START_MANUAL_HEAD not in client.writes[stop_at:]


async def test_operations_run_one_at_a_time(chair, fake_ble):
    order = []

    async def slow(tag):
        order.append(f"{tag}-start")
        await asyncio.sleep(0.05)
        order.append(f"{tag}-end")

    await asyncio.gather(chair._exclusive(slow, "a"), chair._exclusive(slow, "b"))
    assert order == ["a-start", "a-end", "b-start", "b-end"]


async def test_unexpected_disconnect_resets_state(chair, fake_ble):
    await chair.power_on()
    assert chair.status()["powered"]
    assert await chair.set_kneading(True)
    fake_ble.instances[-1].drop()
    st = chair.status()
    assert st["connected"] is False
    assert st["powered"] is False
    assert st["running"] is False
    assert "program_id" not in st
    assert st["manual_techniques"]["kneading"] is False

    # Reconnect: the run-state toggle must be sent again on the new link.
    assert await chair.connect()
    assert len(fake_ble.instances) == 2
    await chair.power_on()
    assert p.POWER in fake_ble.instances[-1].writes


async def test_stale_status_not_reused_after_reconnect(chair, fake_ble):
    await chair.disconnect()
    assert chair._status is None
    fake_ble.responder = staticmethod(lambda cmd: None)  # chair stays silent
    assert await chair.connect(ready_timeout=0.1) is False


async def test_disconnect_cancels_running_operation(chair, fake_ble):
    fake_ble.responder = staticmethod(lambda cmd: frame(echo=cmd[1], travel=0x80))
    task = asyncio.create_task(chair.start_manual_massage("head_neck"))
    await asyncio.sleep(0.3)
    await chair.disconnect()
    with pytest.raises((drv.OperationInterrupted, drv.ChairConnectionError)):
        await task
    assert not chair.connected


async def test_posture_motion_completes_on_motor_stop(chair, fake_ble):
    """Upright: motor runs for a few frames then stops -> settled."""
    state = {"frames": 0}

    def responder(cmd):
        if cmd == p.SEAT_UPRIGHT:
            state["frames"] += 1
            return frame(echo=cmd[1], motor=0x80 if state["frames"] < 5 else 0x00)
        return echo_responder(cmd)

    fake_ble.responder = staticmethod(responder)
    assert await chair.move_seat_upright(timeout=5.0)
    assert not chair._motion_in_progress
    assert fake_ble.instances[-1].writes[-1] == p.POWER  # re-enters run state


def test_load_config_ignores_bad_file(tmp_path, caplog):
    bad = tmp_path / "cfg.json"
    bad.write_text("{not json")
    assert drv.load_config(bad) == {}
    assert "Ignoring unreadable config" in caplog.text
    bad.write_text('["a list"]')
    assert drv.load_config(bad) == {}


def test_name_matching():
    assert drv.name_matches("OS8211 1234", ["os8211"])
    assert not drv.name_matches(None, ["os8211"])
    assert not drv.name_matches("AirPods", drv.DEFAULT_NAME_PREFIXES)


def test_config_rejects_overbroad_prefixes(tmp_path):
    cfg = tmp_path / "cfg.json"
    for bad in ('{"prefixes": [""]}', '{"prefixes": "OS"}', '{"prefixes": []}'):
        cfg.write_text(bad)
        assert "prefixes" not in drv.load_config(cfg)
    cfg.write_text('{"prefixes": ["OS8211"], "name": "  ", "address": "ABC-123"}')
    assert drv.load_config(cfg) == {"prefixes": ["OS8211"], "address": "ABC-123"}


async def test_address_pin_takes_priority(fake_ble, monkeypatch, tmp_path):
    cfg = tmp_path / "cfg.json"
    cfg.write_text('{"address": "PINNED", "prefixes": ["OS8211"]}')
    monkeypatch.setattr(drv, "CONFIG_PATH", cfg)
    seen = []

    async def by_address(addr, timeout=10.0):
        seen.append(addr)

    monkeypatch.setattr(drv.BleakScanner, "find_device_by_address", by_address, raising=False)
    with pytest.raises(drv.ChairNotFoundError, match="PINNED"):
        await drv.UDivaDriver().connect()
    assert seen == ["PINNED"]
