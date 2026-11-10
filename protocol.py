"""
protocol.py — reverse-engineered BLE protocol for the OSIM uDiva3 (OS-8211).

Pure constants and helpers with no I/O, so they can be unit-tested and shared
by the driver, the routine runner and the MCP server.

Transport
---------
BLE GATT. Commands go to WRITE_UUID as ATT Write Requests; the chair streams
14-byte status frames on NOTIFY_UUID.

Command packet: [b0] [b1] [b2] [checksum], checksum = (b0 + b1 + b2) & 0xFF.
b0 is 0xC5 for nearly all commands (0xC3 for power/sofa, 0xA1 for handshake 2).

Connection startup (every time):
    1. c5 e0 00 a5   handshake 1
    2. a1 ea 00 8b   handshake 2
    3. heartbeat c5 00 00 c5 every ~0.7 s keeps the session alive. The active
       preset must also be re-sent periodically or the chair returns to idle.

Status frame (14 bytes, starts with 0xC5):
    idle/sofa:    c5 00 08 00 41 20 14 00 88 22 30 44 00 60
    motion start: c5 00 08 c0 10 00 01 00 00 53 20 31 1d 7f
    motion hold:  c5 a0 00 c0 10 00 01 00 00 53 20 21 1d e7
    motion end:   c5 00 00 00 80 00 01 00 00 53 30 82 00 4b
    byte 1  = seat-travel flag (non-zero while the seat moves)
    byte 3  = motor-active flag (0x00 stopped; 0x40/0x80/0xC0 running)
    byte 4  = program id
    byte 8  = active zone bitmask
    byte 12 = echo of the last command's b1
    byte 13 = checksum
byte 2 is 0x08 only for the first few frames of a motion and 0x00 after, so
never filter frames on it.
"""

from __future__ import annotations

WRITE_UUID = "0000fff2-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID = "0000fff1-0000-1000-8000-00805f9b34fb"

STATUS_FRAME_LEN = 14
STATUS_HEADER = 0xC5

# Byte offsets inside a status frame.
SB_SEAT_TRAVEL = 1
SB_MOTOR = 3
SB_PROGRAM = 4
SB_ZONES = 8
SB_ECHO = 12

# Program ids (status byte 4) that mean "settled, not in a motion phase".
IDLE_PROGRAMS = frozenset({0x00, 0x08, 0x88})


def build_cmd(b0: int, b1: int, b2: int = 0x00) -> bytes:
    """Build a 4-byte command packet with its trailing checksum."""
    for b in (b0, b1, b2):
        if not 0 <= b <= 0xFF:
            raise ValueError(f"command byte out of range: {b!r}")
    return bytes([b0, b1, b2, (b0 + b1 + b2) & 0xFF])


def is_status_frame(data: bytes | bytearray) -> bool:
    return len(data) >= STATUS_FRAME_LEN and data[0] == STATUS_HEADER


def echoes(command: bytes, status: bytes | None) -> bool:
    """True if `status` reports `command` as the last command received."""
    return bool(status and len(status) > SB_ECHO and status[SB_ECHO] == command[1])


# -- Lifecycle ---------------------------------------------------------------
HANDSHAKE_1 = build_cmd(0xC5, 0xE0)
HANDSHAKE_2 = bytes([0xA1, 0xEA, 0x00, 0x8B])
HEARTBEAT = build_cmd(0xC5, 0x00)
POWER = build_cmd(0xC3, 0x01)  # run-state toggle (not a mains power switch)
STOP = build_cmd(0xC5, 0x01)  # stop the current program; stays in run state

# -- Seat posture --------------------------------------------------------------
# Upright/recline are C5 "hold" commands sent every ~100 ms; the seat moves
# while they are received and stops when they stop. Sofa uses a C3 pair: a
# short prep burst, then a hold command at the same cadence.
SEAT_SOFA_PREP = build_cmd(0xC3, 0x07)
SEAT_SOFA = bytes([0xC3, 0x08, 0xFF, 0xCA])  # checksum byte as captured
SEAT_UPRIGHT = build_cmd(0xC5, 0x1D)
SEAT_RECLINE = build_cmd(0xC5, 0x1E)

# -- Manual massage start --------------------------------------------------------
# The app's "Targeted Massage Selection" sends a different start command per
# area. Both are held at ~100 ms; they position the seat and the chair then
# deploys the rollers.
START_MANUAL_HEAD = build_cmd(0xC5, 0x0E)  # Head & Neck (V-grip)
START_MANUAL_BACK = build_cmd(0xC5, 0x16)  # Back Body (rollers)
START_MANUAL_BY_AREA = {
    "back": START_MANUAL_BACK,
    "head_neck": START_MANUAL_HEAD,
}
MANUAL_AREAS = tuple(START_MANUAL_BY_AREA)

# -- Factory programs (name -> command b1) --------------------------------------
PRESETS = {
    "spa": 0x34,
    "entertainment": 0x35,
    "sports_recovery": 0x36,
    "beauty": 0x37,
    "deep_tissue": 0x38,
    "shoulders_care": 0x5B,
    "sweet_dreams": 0x5A,
    "beautiful_mum": 0x59,
    "lumbar_delight": 0x58,
    "detox": 0x57,
    "shoulder_blades": 0x56,
    "gentle": 0x55,
    "healthy_mum": 0x54,
    "nap": 0x53,
    "immune_booster": 0x52,
    # Captured but not named in the app; reachable, hidden from list_presets.
    "preset_51": 0x51,
    "preset_50": 0x50,
    "signature": 0x33,
}


def preset_cmd(name: str) -> bytes:
    return build_cmd(0xC5, PRESETS[name])


# -- Manual techniques ------------------------------------------------------------
TECH_TAPPING = build_cmd(0xC5, 0x17)  # toggle
TECH_KNEADING = build_cmd(0xC5, 0x39)  # toggle
VGRIP_ON = build_cmd(0xC5, 0x07, 0x01)
VGRIP_OFF = build_cmd(0xC5, 0x07, 0x00)

ROLLING_LEVELS = {"off": 0, "partial": 1, "full": 2}


def rolling_cmd(level: int) -> bytes:
    return build_cmd(0xC5, 0x3B, level)


SHOULDER_POSITION_MIN, SHOULDER_POSITION_MAX = 1, 5


def shoulder_position_cmd(pos: int) -> bytes:
    pos = max(SHOULDER_POSITION_MIN, min(SHOULDER_POSITION_MAX, pos))
    return build_cmd(0xC5, 0x12, pos)


# 3D roller intensity per zone, level 0..5.
ZONE_SHOULDER = 0x44
ZONE_BACK = 0x45
ZONE_LUMBAR = 0x46
ZONES = {"shoulder": ZONE_SHOULDER, "back": ZONE_BACK, "lumbar": ZONE_LUMBAR}
INTENSITY_MIN, INTENSITY_MAX = 0, 5


def zone_intensity_cmd(zone: int, level: int) -> bytes:
    level = max(INTENSITY_MIN, min(INTENSITY_MAX, level))
    return build_cmd(0xC5, zone, level)


# Legs: param 0x00 off, 0x03 low, 0x04 high.
LEG_KNEAD = 0x20
LEG_VIBE = 0x22
LEG_LEVELS = {"off": 0x00, "low": 0x03, "high": 0x04}


def leg_cmd(part: int, level: int) -> bytes:
    return build_cmd(0xC5, part, level)


def normalize_name(name: str) -> str:
    """'Deep Tissue' / 'deep-tissue' -> 'deep_tissue'."""
    return name.strip().lower().replace(" ", "_").replace("-", "_")
