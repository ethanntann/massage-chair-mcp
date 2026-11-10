import asyncio
import json

import pytest
from conftest import FakeChair

import custom_presets as cp
import udiva_driver as drv


# -- validation ----------------------------------------------------------------
@pytest.mark.parametrize("name", list(cp.BUILTIN_CUSTOM))
def test_builtin_routines_are_valid(name):
    assert cp.validate_steps(cp.BUILTIN_CUSTOM[name]["steps"]) is None


@pytest.mark.parametrize(
    ("steps", "error"),
    [
        ([], "non-empty"),
        ("not a list", "non-empty"),
        ([{"on": True}], "needs an 'action'"),
        ([{"action": "dance"}], "unknown action"),
        ([{"action": "tapping"}], "'on' must be true or false"),
        ([{"action": "kneading", "on": "yes"}], "'on' must be true or false"),
        ([{"action": "position", "value": "3"}], "integer 1-5"),
        ([{"action": "position", "value": 6}], "integer 1-5"),
        ([{"action": "position", "value": True}], "integer 1-5"),
        ([{"action": "intensity", "zone": "neck", "level": 2}], "'zone'"),
        ([{"action": "intensity", "zone": "back", "level": 9}], "integer 0-5"),
        ([{"action": "rolling", "level": "max"}], "'level'"),
        ([{"action": "leg_kneading", "level": 3}], "'level'"),
        ([{"action": "wait", "seconds": "10"}], "between 0 and"),
        ([{"action": "wait", "seconds": 0}], "between 0 and"),
        ([{"action": "preset", "name": "nope"}], "'name'"),
        ([{"action": "manual", "area": "legs"}], "'area'"),
    ],
)
def test_invalid_steps_rejected(steps, error):
    err = cp.validate_steps(steps)
    assert err is not None and error in err


def test_valid_seat_and_wait_steps():
    steps = [{"action": "seat_recline"}, {"action": "wait", "seconds": 1.5}, {"action": "stop"}]
    assert cp.validate_steps(steps) is None


def test_validate_name():
    assert cp.validate_name("evening") is None
    assert cp.validate_name("  ")
    assert cp.validate_name("x" * 100)


# -- storage -------------------------------------------------------------------
def test_save_load_delete_roundtrip(tmp_path):
    store = tmp_path / "presets.json"
    steps = [{"action": "stop"}]
    cp.save_user_preset("mine", steps, "desc", path=store)
    assert cp.load_user_presets(store) == {"mine": {"description": "desc", "steps": steps}}
    assert "mine" in cp.all_presets(store)
    assert "wind_down" in cp.all_presets(store)
    assert cp.delete_user_preset("mine", path=store)
    assert not cp.delete_user_preset("mine", path=store)
    assert not list(tmp_path.glob("*.tmp"))


def test_corrupt_store_is_never_overwritten(tmp_path):
    store = tmp_path / "presets.json"
    store.write_text('{"mine": ')  # truncated
    assert cp.load_user_presets(store) == {}
    with pytest.raises(cp.PresetStoreError):
        cp.save_user_preset("other", [{"action": "stop"}], path=store)
    assert store.read_text() == '{"mine": '


def test_user_routine_overrides_builtin(tmp_path):
    store = tmp_path / "presets.json"
    store.write_text(json.dumps({"wind_down": {"description": "mine", "steps": []}}))
    assert cp.all_presets(store)["wind_down"]["description"] == "mine"


# -- runner --------------------------------------------------------------------
async def test_run_preset_maps_steps_to_driver_calls():
    chair = FakeChair()
    steps = [
        {"action": "manual", "area": "back"},
        {"action": "kneading", "on": True},
        {"action": "kneading", "on": True},
        {"action": "rolling", "level": "full"},
        {"action": "intensity", "zone": "lumbar", "level": 3},
        {"action": "leg_vibration", "level": "high"},
        {"action": "stop"},
    ]
    result = await cp.run_preset(chair, steps)
    assert result == "Completed routine (7 steps)."
    assert chair.calls == [
        ("enter_manual_mode", "back"),
        ("set_kneading", True),
        ("set_kneading", True),
        ("set_rolling", 2),
        ("set_zone_intensity", drv.ZONE_LUMBAR, 3),
        ("set_leg_vibration", 0x04),
        ("stop",),
    ]


async def test_run_preset_stops_chair_when_a_step_is_not_acknowledged():
    chair = FakeChair(fail={"set_rolling"})
    steps = [
        {"action": "rolling", "level": "partial"},
        {"action": "wait", "seconds": 60},
    ]
    result = await cp.run_preset(chair, steps)
    assert "Step 1" in result and "rolling" in result
    assert chair.calls[-1] == ("stop",)


async def test_cancelled_routine_stops_chair():
    chair = FakeChair()
    task = asyncio.create_task(cp.run_preset(chair, [{"action": "wait", "seconds": 60}]))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert chair.calls == [("stop",)]


async def test_run_preset_rejects_invalid_and_disconnected():
    chair = FakeChair()
    assert "Invalid routine" in await cp.run_preset(chair, [{"action": "dance"}])
    chair.connected = False
    assert await cp.run_preset(chair, [{"action": "stop"}]) == "Chair not connected."


# -- security limits -------------------------------------------------------------
def test_wait_and_routine_length_limits():
    assert "between 0 and" in cp.validate_steps([{"action": "wait", "seconds": 1e9}])
    long = [{"action": "wait", "seconds": cp.MAX_WAIT_SECONDS}] * 4
    assert "total wait time" in cp.validate_steps(long)
    assert "at most" in cp.validate_steps([{"action": "stop"}] * (cp.MAX_STEPS + 1))


def test_text_limits():
    assert cp.validate_name("evil‮name")  # bidi override
    assert cp.validate_name("a\x00b")
    assert cp.validate_description("x" * (cp.MAX_DESCRIPTION_LEN + 1))
    assert cp.validate_description("line one\nline two") is None


def test_parse_steps_json_bounds():
    _, err = cp.parse_steps_json("[" * 100_000)
    assert "at most" in err
    _, err = cp.parse_steps_json("[" * 5_000 + "]" * 5_000)
    assert err is None or "not valid JSON" in err
    steps, err = cp.parse_steps_json('[{"action": "stop"}]')
    assert err is None and steps == [{"action": "stop"}]


def test_store_file_is_private(tmp_path):
    store = tmp_path / "presets.json"
    cp.save_user_preset("mine", [{"action": "stop"}], path=store)
    assert store.stat().st_mode & 0o077 == 0
