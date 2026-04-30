#!/usr/bin/env python3
from __future__ import annotations
"""
Echo Bike Tracker
Connects to the Rogue Echo Bike v3 via Bluetooth using the FTMS standard protocol.
Displays live workout metrics and saves session data on exit.

Usage:
    python3 tracker.py              # scan and connect
    python3 tracker.py --history    # view saved workouts
    python3 tracker.py --address AA:BB:CC:DD:EE:FF   # connect directly by BT address

Requirements:
    pip install bleak rich
"""

import argparse
import asyncio
import json
import struct
import sys
from datetime import datetime
from pathlib import Path

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from rich import box
from rich.columns import Columns
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# ---------------------------------------------------------------------------
# FTMS standard Bluetooth UUIDs (Bluetooth Assigned Numbers)
# ---------------------------------------------------------------------------
FTMS_SERVICE_UUID       = "00001826-0000-1000-8000-00805f9b34fb"
INDOOR_BIKE_DATA_UUID   = "00002ad2-0000-1000-8000-00805f9b34fb"
FTMS_STATUS_UUID        = "00002ada-0000-1000-8000-00805f9b34fb"
FTMS_CONTROL_UUID       = "00002ad9-0000-1000-8000-00805f9b34fb"
HEART_RATE_UUID         = "00002a37-0000-1000-8000-00805f9b34fb"

WORKOUTS_DIR = Path.home() / "echo_bike_workouts"
ECHO_BIKE_KEYWORDS = {"rogue", "echo", "bike", "assault", "ftms"}

console = Console()

# ---------------------------------------------------------------------------
# FTMS Indoor Bike Data parser  (Bluetooth spec 0x2AD2)
# ---------------------------------------------------------------------------

def parse_indoor_bike_data(data: bytes) -> dict:
    """
    Decode an FTMS Indoor Bike Data notification.

    Flag bits (little-endian uint16):
      0  More Data          – when 0, Instantaneous Speed is present
      1  Average Speed
      2  Instantaneous Cadence
      3  Average Cadence
      4  Total Distance
      5  Resistance Level
      6  Instantaneous Power
      7  Average Power
      8  Expended Energy    – Total kcal (uint16) + per-hour (uint16) + per-min (uint8)
      9  Heart Rate
      10 Metabolic Equivalent
      11 Elapsed Time
      12 Remaining Time
    """
    if len(data) < 2:
        return {}

    result: dict = {}
    off = 0

    flags = struct.unpack_from("<H", data, off)[0]
    off += 2

    def read(fmt: str) -> int | float:
        nonlocal off
        size = struct.calcsize(fmt)
        if off + size > len(data):
            return None
        val = struct.unpack_from(fmt, data, off)[0]
        off += size
        return val

    # bit 0 = More Data; when 0 → speed present
    if not (flags & 0x0001):
        v = read("<H")
        if v is not None:
            result["speed_kmh"] = round(v * 0.01, 2)

    if flags & 0x0002:
        v = read("<H")
        if v is not None:
            result["avg_speed_kmh"] = round(v * 0.01, 2)

    if flags & 0x0004:
        v = read("<H")
        if v is not None:
            result["cadence_rpm"] = round(v * 0.5, 1)

    if flags & 0x0008:
        v = read("<H")
        if v is not None:
            result["avg_cadence_rpm"] = round(v * 0.5, 1)

    if flags & 0x0010:
        # uint24 (3 bytes), little-endian
        if off + 3 <= len(data):
            d24 = data[off : off + 3] + b"\x00"
            result["distance_m"] = struct.unpack_from("<I", d24)[0]
            off += 3

    if flags & 0x0020:
        v = read("<h")
        if v is not None:
            result["resistance"] = v

    if flags & 0x0040:
        v = read("<h")
        if v is not None:
            result["power_w"] = v

    if flags & 0x0080:
        v = read("<h")
        if v is not None:
            result["avg_power_w"] = v

    if flags & 0x0100:
        total = read("<H")
        per_hour = read("<H")
        per_min = read("<B")
        if total is not None:
            result["calories"] = total
        if per_hour is not None:
            result["calories_per_hour"] = per_hour
        if per_min is not None:
            result["calories_per_minute"] = per_min

    if flags & 0x0200:
        v = read("<B")
        if v is not None:
            result["heart_rate"] = v

    if flags & 0x0400:
        read("<B")   # metabolic equivalent – skip

    if flags & 0x0800:
        v = read("<H")
        if v is not None:
            result["elapsed_seconds"] = v

    if flags & 0x1000:
        v = read("<H")
        if v is not None:
            result["remaining_seconds"] = v

    return result


def parse_heart_rate(data: bytes) -> int | None:
    """Decode a standard Heart Rate Measurement notification."""
    if not data:
        return None
    flags = data[0]
    if flags & 0x01:
        return struct.unpack_from("<H", data, 1)[0] if len(data) >= 3 else None
    return data[1] if len(data) >= 2 else None


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def fmt_time(seconds: int | float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _val(metrics: dict, key: str, fmt: str, unit: str, scale: float = 1.0) -> str:
    v = metrics.get(key)
    if v is None:
        return "[dim]--[/dim]"
    return f"{v * scale:{fmt}}{unit}"


def build_dashboard(
    metrics: dict,
    wall_elapsed: float,
    device_name: str,
    n_points: int,
) -> Panel:
    t = Table(show_header=False, box=None, padding=(0, 2))
    t.add_column(style="bold cyan", width=18)
    t.add_column(style="bold white", width=16, justify="right")

    bike_time = metrics.get("elapsed_seconds", wall_elapsed)
    power     = metrics.get("power_w")
    cadence   = metrics.get("cadence_rpm")
    speed     = metrics.get("speed_kmh")
    distance  = metrics.get("distance_m")
    calories  = metrics.get("calories")
    hr        = metrics.get("heart_rate")

    t.add_row("⏱  TIME",       fmt_time(bike_time))
    t.add_row("⚡  POWER",      f"{power:.0f} W"      if power    is not None else "[dim]--[/dim]")
    t.add_row("🚴  CADENCE",    f"{cadence:.0f} RPM"  if cadence  is not None else "[dim]--[/dim]")
    t.add_row("💨  SPEED",      f"{speed:.1f} km/h"   if speed    is not None else "[dim]--[/dim]")
    t.add_row(
        "📏  DISTANCE",
        f"{distance / 1000:.2f} km" if distance is not None else "[dim]--[/dim]",
    )
    t.add_row("🔥  CALORIES",   f"{calories:.0f} kcal" if calories is not None else "[dim]--[/dim]")
    t.add_row(
        "❤️   HEART RATE",
        f"{hr:.0f} bpm" if hr is not None else "[dim]-- (no HR sensor)[/dim]",
    )
    t.add_row("", "")
    t.add_row("[dim]samples[/dim]",    f"[dim]{n_points}[/dim]")
    t.add_row("[dim]Ctrl-C to stop[/dim]", "[dim]auto-saved[/dim]")

    return Panel(
        t,
        title=f"[bold green]ECHO BIKE TRACKER[/bold green]  [dim]{device_name}[/dim]",
        box=box.DOUBLE_EDGE,
        padding=(1, 3),
    )


# ---------------------------------------------------------------------------
# Device discovery
# ---------------------------------------------------------------------------

async def find_bike(address: str | None = None) -> BLEDevice | None:
    if address:
        console.print(f"[yellow]Connecting directly to {address}...[/yellow]")
        devices = await BleakScanner.discover(timeout=8.0)
        for d in devices:
            if d.address.upper() == address.upper():
                return d
        console.print(f"[red]Device {address} not found in scan.[/red]")
        return None

    console.print("[yellow]Scanning for Echo Bike (FTMS)...[/yellow]")
    console.print("[dim]Make sure the console is on and BT is active (hold BT button 3 sec).[/dim]\n")

    # First pass: filter by FTMS service UUID advertisement
    found = await BleakScanner.discover(timeout=10.0, service_uuids=[FTMS_SERVICE_UUID])

    # Second pass: broader scan filtered by name keywords
    if not found:
        console.print("[dim]No FTMS device found — trying broader name scan...[/dim]")
        all_devs = await BleakScanner.discover(timeout=10.0)
        found = [
            d for d in all_devs
            if d.name and any(kw in d.name.lower() for kw in ECHO_BIKE_KEYWORDS)
        ]

    if not found:
        console.print("\n[red]No Echo Bike found.[/red]")
        console.print("  • Is the console powered on?")
        console.print("  • Is Bluetooth enabled on the console? (hold BT button 3 sec)")
        console.print("  • Is the bike within range?")
        console.print("\n[dim]Tip: run with --address AA:BB:CC:DD:EE:FF to target a specific device.[/dim]")
        return None

    if len(found) == 1:
        console.print(f"[green]Found: {found[0].name}  ({found[0].address})[/green]\n")
        return found[0]

    console.print("[green]Multiple FTMS devices found:[/green]")
    for i, d in enumerate(found):
        console.print(f"  [{i}] {d.name or 'Unknown':30s}  {d.address}")
    idx = int(input("\nSelect device number: ").strip())
    return found[idx]


# ---------------------------------------------------------------------------
# Main session loop
# ---------------------------------------------------------------------------

async def run_session(device: BLEDevice) -> list[dict]:
    workout: list[dict] = []
    metrics: dict = {}
    start = datetime.now()

    def on_bike_data(_sender, data: bytes) -> None:
        nonlocal metrics
        parsed = parse_indoor_bike_data(bytes(data))
        if parsed:
            parsed["ts"] = datetime.now().isoformat()
            metrics = parsed
            workout.append(parsed)

    def on_heart_rate(_sender, data: bytes) -> None:
        hr = parse_heart_rate(bytes(data))
        if hr is not None:
            metrics["heart_rate"] = hr

    async with BleakClient(device, timeout=20.0) as client:
        console.print("[green]Connected![/green]  Starting workout…\n")

        await client.start_notify(INDOOR_BIKE_DATA_UUID, on_bike_data)

        # Optional: subscribe to heart rate if the service is present
        try:
            await client.start_notify(HEART_RATE_UUID, on_heart_rate)
        except Exception:
            pass

        try:
            with Live(console=console, refresh_per_second=4, screen=False) as live:
                while True:
                    elapsed = (datetime.now() - start).total_seconds()
                    live.update(
                        build_dashboard(metrics, elapsed, device.name or device.address, len(workout))
                    )
                    await asyncio.sleep(0.25)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            try:
                await client.stop_notify(INDOOR_BIKE_DATA_UUID)
            except Exception:
                pass

    return workout


# ---------------------------------------------------------------------------
# Save + summary
# ---------------------------------------------------------------------------

def save_workout(workout: list[dict], device_name: str) -> Path | None:
    if not workout:
        console.print("[yellow]No data recorded.[/yellow]")
        return None

    WORKOUTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = WORKOUTS_DIR / f"workout_{ts}.json"

    powers    = [d["power_w"]     for d in workout if "power_w"     in d]
    cadences  = [d["cadence_rpm"] for d in workout if "cadence_rpm" in d]
    last      = workout[-1]

    summary = {
        "date":             workout[0].get("ts", ""),
        "device":           device_name,
        "duration_seconds": last.get("elapsed_seconds") or len(workout),
        "distance_m":       last.get("distance_m"),
        "total_calories":   last.get("calories"),
        "avg_power_w":      round(sum(powers)   / len(powers),   1) if powers   else None,
        "max_power_w":      max(powers)                              if powers   else None,
        "avg_cadence_rpm":  round(sum(cadences) / len(cadences), 1) if cadences else None,
        "max_cadence_rpm":  max(cadences)                           if cadences else None,
        "samples":          len(workout),
    }

    path.write_text(json.dumps({"summary": summary, "data": workout}, indent=2))
    return path, summary


def print_summary(path: Path, summary: dict) -> None:
    console.print()
    console.rule("[bold green]WORKOUT COMPLETE[/bold green]")

    t = Table(show_header=False, box=box.SIMPLE, padding=(0, 2))
    t.add_column(style="cyan", width=20)
    t.add_column(style="bold white")

    dur = summary.get("duration_seconds")
    if dur:
        t.add_row("Duration",  fmt_time(dur))
    dist = summary.get("distance_m")
    if dist:
        t.add_row("Distance",  f"{dist / 1000:.2f} km  ({dist:.0f} m)")
    cal = summary.get("total_calories")
    if cal:
        t.add_row("Calories",  f"{cal:.0f} kcal")
    ap = summary.get("avg_power_w")
    if ap:
        t.add_row("Avg Power", f"{ap} W")
    mp = summary.get("max_power_w")
    if mp:
        t.add_row("Max Power", f"{mp} W")
    ac = summary.get("avg_cadence_rpm")
    if ac:
        t.add_row("Avg RPM",   f"{ac}")
    mc = summary.get("max_cadence_rpm")
    if mc:
        t.add_row("Max RPM",   f"{mc}")

    console.print(t)
    console.print(f"[dim]Saved → {path}[/dim]")


# ---------------------------------------------------------------------------
# History viewer
# ---------------------------------------------------------------------------

def show_history() -> None:
    files = sorted(WORKOUTS_DIR.glob("workout_*.json"), reverse=True) if WORKOUTS_DIR.exists() else []

    if not files:
        console.print("[yellow]No workouts saved yet.[/yellow]")
        return

    t = Table(title="Echo Bike — Workout History", box=box.SIMPLE_HEAD)
    t.add_column("Date",       style="cyan")
    t.add_column("Duration",   justify="right")
    t.add_column("Distance",   justify="right")
    t.add_column("Calories",   justify="right")
    t.add_column("Avg W",      justify="right")
    t.add_column("Max W",      justify="right")
    t.add_column("Avg RPM",    justify="right")

    for f in files[:30]:
        try:
            d = json.loads(f.read_text())
            s = d.get("summary", {})
        except Exception:
            continue

        dur  = fmt_time(s["duration_seconds"]) if s.get("duration_seconds") else "--"
        dist = f"{s['distance_m']/1000:.2f} km" if s.get("distance_m") else "--"
        cal  = f"{s['total_calories']:.0f}"      if s.get("total_calories") else "--"
        ap   = f"{s['avg_power_w']}"             if s.get("avg_power_w")   else "--"
        mp   = f"{s['max_power_w']}"             if s.get("max_power_w")   else "--"
        ac   = f"{s['avg_cadence_rpm']}"         if s.get("avg_cadence_rpm") else "--"

        date_str = s.get("date", f.stem)[:19].replace("T", " ")
        t.add_row(date_str, dur, dist, cal, ap, mp, ac)

    console.print(t)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def async_main(args: argparse.Namespace) -> None:
    if args.history:
        show_history()
        return

    device = await find_bike(args.address)
    if not device:
        sys.exit(1)

    workout = await run_session(device)
    result  = save_workout(workout, device.name or device.address)
    if result:
        path, summary = result
        print_summary(path, summary)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Echo Bike Tracker — connect to Rogue Echo Bike v3 via Bluetooth"
    )
    parser.add_argument(
        "--address", "-a",
        metavar="ADDR",
        help="Bluetooth address to connect directly (e.g. AA:BB:CC:DD:EE:FF)",
    )
    parser.add_argument(
        "--history",
        action="store_true",
        help="Show saved workout history and exit",
    )
    args = parser.parse_args()
    asyncio.run(async_main(args))


if __name__ == "__main__":
    main()
