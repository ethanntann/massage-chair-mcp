"""scan.py — list nearby BLE devices to find the chair's advertised name.

Run with the chair plugged in and the OSIM phone app disconnected. Names that
match the driver's prefixes are marked; put another prefix (or the exact name)
in ~/.udiva_config.json if yours isn't matched.
"""

import asyncio

from bleak import BleakScanner

import udiva_driver as drv


async def main() -> None:
    prefixes = drv.load_config().get("prefixes", drv.DEFAULT_NAME_PREFIXES)
    print("Scanning 8s... (chair on, phone app disconnected)\n")
    devices = await BleakScanner.discover(timeout=8.0)
    if not devices:
        print("No devices found.")
        return
    for d in sorted(devices, key=lambda d: d.name or "~"):
        mark = "  <- chair" if drv.name_matches(d.name, prefixes) else ""
        print(f"  name={d.name!r:35} address={d.address}{mark}")
    print(f"\nMatching prefixes: {prefixes}")


if __name__ == "__main__":
    asyncio.run(main())
