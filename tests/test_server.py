"""MCP tool surface and tool behaviour, with a fake driver behind the session."""

import asyncio
from types import SimpleNamespace

import pytest
from conftest import FakeChair

import custom_presets as cp
import server

# The tool names the Claude Desktop setup already uses. Changing one breaks
# existing conversations and prompts, so it must be deliberate.
EXPECTED_TOOLS = {
    "connect_chair", "power_on", "power_off", "stop", "return_to_sofa",
    "disconnect_chair", "get_status", "list_presets", "start_preset",
    "enter_manual_mode", "set_tapping", "set_kneading", "set_rolling",
    "set_shoulder_position", "set_zone_intensity", "set_vgrip",
    "set_leg_kneading", "set_leg_vibration", "quick_start",
    "list_custom_presets", "get_custom_preset", "save_custom_preset",
    "delete_custom_preset", "run_custom_preset", "stop_custom_preset",
}  # fmt: skip


@pytest.fixture
def session():
    return server.ChairSession(chair=FakeChair())


@pytest.fixture
def ctx(session):
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=session))


@pytest.fixture(autouse=True)
def tmp_store(monkeypatch, tmp_path):
    monkeypatch.setattr(cp, "STORE_PATH", tmp_path / "presets.json")


async def test_tool_names_unchanged():
    tools = await server.mcp.list_tools()
    assert {t.name for t in tools} == EXPECTED_TOOLS


async def test_enum_parameters_in_schema():
    tools = {t.name: t for t in await server.mcp.list_tools()}
    rolling = tools["set_rolling"].inputSchema["properties"]["level"]
    assert rolling["enum"] == ["off", "partial", "full"]
    zone = tools["set_zone_intensity"].inputSchema["properties"]
    assert zone["zone"]["enum"] == ["shoulder", "back", "lumbar"]
    assert (zone["level"]["minimum"], zone["level"]["maximum"]) == (0, 5)
    assert tools["get_status"].annotations.readOnlyHint is True
    assert tools["start_preset"].annotations.readOnlyHint is False


async def _start_long_routine(ctx, session):
    cp.save_user_preset("long", [{"action": "wait", "seconds": 60}])
    msg = await server.run_custom_preset(ctx, "long")
    assert "Started custom routine 'long'" in msg
    await asyncio.sleep(0.01)
    assert session.routine_running


async def test_stop_cancels_running_routine(ctx, session):
    await _start_long_routine(ctx, session)
    assert await server.stop(ctx) == "Massage stopped; chair is idle."
    assert not session.routine_running
    assert "custom_routine: none" in await server.get_status(ctx)


@pytest.mark.parametrize(
    "call",
    [
        lambda ctx: server.start_preset(ctx, "spa"),
        lambda ctx: server.enter_manual_mode(ctx, "back"),
        lambda ctx: server.quick_start(ctx, "nap"),
        lambda ctx: server.return_to_sofa(ctx),
        lambda ctx: server.power_off(ctx),
        lambda ctx: server.disconnect_chair(ctx),
    ],
)
async def test_mode_changes_cancel_routine(ctx, session, call):
    await _start_long_routine(ctx, session)
    await call(ctx)
    assert not session.routine_running


async def test_routine_failure_is_reported_in_status(ctx, session):
    session.chair.fail = {"set_rolling"}
    cp.save_user_preset("bad", [{"action": "rolling", "level": "full"}])
    await server.run_custom_preset(ctx, "bad")
    await session.routine_task
    status = await server.get_status(ctx)
    assert "last_routine_result: 'bad': Step 1: chair did not acknowledge 'rolling'" in status


async def test_unacknowledged_command_is_reported(ctx, session):
    session.chair.fail = {"set_vgrip"}
    assert "didn't acknowledge" in await server.set_vgrip(ctx, True)
    session.chair.fail = {"set_kneading"}
    assert "didn't acknowledge" in await server.set_kneading(ctx, True)
    assert session.chair.kneading is False


async def test_technique_state_shared_with_routines(ctx, session):
    """A routine that turned kneading on must not make set_kneading toggle it off."""
    cp.save_user_preset("k", [{"action": "kneading", "on": True}])
    await server.run_custom_preset(ctx, "k")
    await session.routine_task
    assert await server.set_kneading(ctx, True) == "Kneading already on."


async def test_start_preset_normalises_and_rejects(ctx, session):
    assert "confirmed running" in await server.start_preset(ctx, "Deep Tissue")
    assert ("start_preset", "deep_tissue") in session.chair.calls
    assert "Unknown preset" in await server.start_preset(ctx, "neck_and_shoulders")


async def test_quick_start_manual(ctx, session):
    assert "Manual back massage positioned" in await server.quick_start(ctx, "Manual")


async def test_save_custom_preset_validation(ctx):
    assert "not valid JSON" in await server.save_custom_preset(ctx, "x", "[{")
    assert "Invalid routine" in await server.save_custom_preset(ctx, "x", '[{"action":"tapping"}]')
    assert "Invalid name" in await server.save_custom_preset(ctx, " ", '[{"action":"stop"}]')
    assert "Saved" in await server.save_custom_preset(ctx, "x", '[{"action":"stop"}]')
    assert "- x (1 steps)" in await server.list_custom_presets(ctx)
    assert "Deleted" in await server.delete_custom_preset(ctx, "x")


async def test_save_refuses_corrupt_store(ctx):
    cp.STORE_PATH.write_text("{broken")
    msg = await server.save_custom_preset(ctx, "x", '[{"action":"stop"}]')
    assert msg.startswith("Could not save")
    assert cp.STORE_PATH.read_text() == "{broken"


async def test_driver_errors_become_messages(ctx, session):
    from udiva_driver import ChairConnectionError

    async def boom(_level):
        raise ChairConnectionError("link lost")

    session.chair.set_leg_kneading = boom
    assert await server.set_leg_kneading(ctx, "low") == "Chair command failed: link lost"


async def test_list_presets_hides_unnamed(ctx):
    out = await server.list_presets(ctx)
    assert "- spa — relaxing" in out
    assert "preset_51" not in out


async def test_save_rejects_oversized_and_injected_text(ctx):
    big = "[" + ",".join(['{"action":"stop"}'] * 2000) + "]"
    assert "at most" in await server.save_custom_preset(ctx, "x", big)
    msg = await server.save_custom_preset(ctx, "x", '[{"action":"stop"}]', "d" * 1000)
    assert msg.startswith("Invalid description")
