"""Golden bytes for every command, taken from the captured app traffic.

These lock the wire protocol: a refactor that changes any byte sent to the
chair fails here.
"""

import pytest

import protocol as p


def h(s: str) -> bytes:
    return bytes.fromhex(s)


@pytest.mark.parametrize(
    ("cmd", "expected"),
    [
        (p.HANDSHAKE_1, "c5e000a5"),
        (p.HANDSHAKE_2, "a1ea008b"),
        (p.HEARTBEAT, "c50000c5"),
        (p.POWER, "c30100c4"),
        (p.STOP, "c50100c6"),
        (p.SEAT_SOFA_PREP, "c30700ca"),
        (p.SEAT_SOFA, "c308ffca"),
        (p.SEAT_UPRIGHT, "c51d00e2"),
        (p.SEAT_RECLINE, "c51e00e3"),
        (p.START_MANUAL_HEAD, "c50e00d3"),
        (p.START_MANUAL_BACK, "c51600db"),
        (p.TECH_TAPPING, "c51700dc"),
        (p.TECH_KNEADING, "c53900fe"),
        (p.VGRIP_ON, "c50701cd"),
        (p.VGRIP_OFF, "c50700cc"),
    ],
)
def test_fixed_commands(cmd, expected):
    assert cmd == h(expected)


def test_checksum_wraps():
    assert p.build_cmd(0xC5, 0xE0) == h("c5e000a5")  # 0x1A5 & 0xFF


def test_build_cmd_rejects_out_of_range():
    with pytest.raises(ValueError):
        p.build_cmd(0xC5, 0x100)


def test_parameterised_commands():
    assert p.preset_cmd("spa") == h("c53400f9")
    assert p.preset_cmd("shoulders_care") == h("c55b0020")
    assert p.rolling_cmd(2) == h("c53b0202")
    assert p.shoulder_position_cmd(5) == h("c51205dc")
    assert p.zone_intensity_cmd(p.ZONE_LUMBAR, 3) == h("c546030e")
    assert p.leg_cmd(p.LEG_KNEAD, p.LEG_LEVELS["high"]) == h("c52004e9")
    assert p.leg_cmd(p.LEG_VIBE, p.LEG_LEVELS["low"]) == h("c52203ea")


def test_parameter_clamping():
    assert p.shoulder_position_cmd(9) == p.shoulder_position_cmd(5)
    assert p.shoulder_position_cmd(0) == p.shoulder_position_cmd(1)
    assert p.zone_intensity_cmd(p.ZONE_BACK, 7) == p.zone_intensity_cmd(p.ZONE_BACK, 5)


def test_preset_ids_unchanged():
    assert p.PRESETS == {
        "spa": 0x34, "entertainment": 0x35, "sports_recovery": 0x36, "beauty": 0x37,
        "deep_tissue": 0x38, "shoulders_care": 0x5B, "sweet_dreams": 0x5A,
        "beautiful_mum": 0x59, "lumbar_delight": 0x58, "detox": 0x57,
        "shoulder_blades": 0x56, "gentle": 0x55, "healthy_mum": 0x54, "nap": 0x53,
        "immune_booster": 0x52, "preset_51": 0x51, "preset_50": 0x50, "signature": 0x33,
    }  # fmt: skip


def test_status_frame_helpers():
    idle = h("c500080041201400882230440060")
    assert p.is_status_frame(idle)
    assert not p.is_status_frame(h("c50000c5"))
    assert not p.is_status_frame(h("c3") + bytes(13))
    assert p.echoes(p.HEARTBEAT, idle)
    assert not p.echoes(p.TECH_KNEADING, idle)
    assert not p.echoes(p.HEARTBEAT, None)


@pytest.mark.parametrize(
    ("raw", "norm"), [("Deep Tissue", "deep_tissue"), (" deep-tissue ", "deep_tissue")]
)
def test_normalize_name(raw, norm):
    assert p.normalize_name(raw) == norm
