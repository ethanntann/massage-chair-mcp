# Reverse-engineering the uDiva3 protocol

The OSIM uDiva3 is controlled only by OSIM's phone app over Bluetooth LE. There
is no published API, so the protocol below was recovered from packet captures.

## Method

1. **Capture.** Apple's PacketLogger records an iPhone's Bluetooth traffic
   (Bluetooth logging profile + USB, *File ▸ New iOS Trace*). I ran the OSIM
   app on the phone and recorded each session.
2. **One action per capture.** Each trace starts from the same seat position
   (sofa), idles for ~5 s, then presses exactly one button, then idles again.
   The quiet gaps (nothing but heartbeats) mark where one action ends and the
   next begins; pressing STOP between phases gives a distinctive separator.
   I noted the order of actions and matched them against screenshots of the
   app's screens.
3. **Filter.** A trace holds every Bluetooth packet on the phone. The signal is
   two things: ATT *Write Requests* to handle `0x0011` (commands) and
   *Handle Value Notifications* from handle `0x000E` (status). A small parser
   reads the `.pklg` records (4-byte little-endian length, type `0x02` sent /
   `0x03` received) and keeps only those.
4. **Diff.** For each action, compare the commands sent and the status bytes
   that changed. Commands share a fixed shape, so a new button is usually one
   new value in `b1`; status bytes that change only while the seat moves
   reveal the motor and travel flags.
5. **Replay and confirm.** Send the command from Python (bleak) and check that
   the chair does the same thing and reports the same status.

## Transport

| | UUID | Handle | Use |
|---|---|---|---|
| Write | `0000fff2-0000-1000-8000-00805f9b34fb` | `0x0011` | commands, as ATT Write Requests |
| Notify | `0000fff1-0000-1000-8000-00805f9b34fb` | `0x000E` | 14-byte status frames |

## Commands

Every command is 4 bytes: `[b0] [b1] [b2] [checksum]`, where
`checksum = (b0 + b1 + b2) & 0xFF`. `b0` is `0xC5` for almost everything.

| Command | Bytes | Notes |
|---|---|---|
| Handshake 1 | `c5 e0 00 a5` | on connect |
| Handshake 2 | `a1 ea 00 8b` | on connect, 300 ms later |
| Heartbeat | `c5 00 00 c5` | every ~0.7 s, or the chair drops to idle |
| Run state | `c3 01 00 c4` | a toggle; needed again after every seat motion |
| Stop | `c5 01 00 c6` | stops the program, stays in run state |
| Factory program | `c5 <id> 00 <ck>` | 18 ids: `0x33`–`0x38`, `0x50`–`0x5B` |
| Seat upright / recline | `c5 1d` / `c5 1e` | held every ~100 ms while the seat moves |
| Seat to sofa | `c3 07 00 ca` ×3, then hold `c3 08 ff ca` | |
| Manual start | `c5 16` back, `c5 0e` head & neck | held until the seat reaches its limit |
| Tapping / kneading | `c5 17` / `c5 39` | toggles |
| Rolling | `c5 3b <0..2>` | off / partial / full |
| Shoulder height | `c5 12 <1..5>` | |
| 3D intensity | `c5 <44/45/46> <0..5>` | shoulder / back / lumbar |
| V-grip | `c5 07 <0/1>` | |
| Legs | `c5 20 <lvl>` kneading, `c5 22 <lvl>` vibration | level `00` off, `03` low, `04` high |

## Status frame

```
idle/sofa:    c5 00 08 00 41 20 14 00 88 22 30 44 00 60
motion start: c5 00 08 c0 10 00 01 00 00 53 20 31 1d 7f
motion hold:  c5 a0 00 c0 10 00 01 00 00 53 20 21 1d e7
```

| Byte | Meaning |
|---|---|
| 0 | `0xC5` header |
| 1 | seat-travel flag: non-zero while the seat moves, back to `00` at the limit |
| 2 | `0x08` for only the first few frames of a motion, then `0x00` (don't filter on it) |
| 3 | motor flag: `00` stopped, `40`/`80`/`C0` running |
| 4 | program id |
| 8 | active zone bitmask |
| 12 | echo of the last command's `b1` (how a command is confirmed) |
| 13 | checksum |

## Things that only showed up in the captures

- The app sends **no heartbeats while holding a motion command**. A heartbeat
  in the middle of the 100 ms stream makes the seat stop and restart.
- A manual start **deploys the rollers only after the start command is
  released**, and only if the seat reached its limit. Releasing early just
  stops the seat.
- **Back massage needs the backrest positioned first:** `c5 16` moves only the
  footrest, so the app runs the upright move before it.
- Seat moves leave the run state, so the app re-sends `c3 01` after every one;
  without it, manual commands are echoed but do nothing.
