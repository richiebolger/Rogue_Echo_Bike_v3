#!/usr/bin/env python3
from __future__ import annotations

import importlib.util, os as _os
def _fix_qt_plugin_path():
    spec = importlib.util.find_spec("PyQt6")
    if spec and spec.origin:
        p = _os.path.join(_os.path.dirname(spec.origin), "Qt6", "plugins", "platforms")
        if _os.path.isdir(p):
            _os.environ.setdefault("QT_QPA_PLATFORM_PLUGIN_PATH", p)
_fix_qt_plugin_path()

"""Echo Bike Tracker — macOS GUI v4
Rogue Echo Bike v3  ·  FTMS Bluetooth
Usage:  python3 gui_tracker.py
"""

import asyncio, copy, csv, json, struct, sys, threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pyqtgraph as pg
from bleak import BleakClient, BleakScanner
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QObject
from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QDoubleSpinBox, QFrame, QGridLayout, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QListWidget, QMainWindow, QMessageBox,
    QProgressBar, QPushButton, QScrollArea, QSizePolicy, QSpinBox,
    QStatusBar, QTableWidget, QTableWidgetItem, QTabWidget,
    QVBoxLayout, QWidget,
)

# ─── FTMS UUIDs ───────────────────────────────────────────────────────────────
FTMS_SERVICE_UUID  = "00001826-0000-1000-8000-00805f9b34fb"
INDOOR_BIKE_DATA   = "00002ad2-0000-1000-8000-00805f9b34fb"
HEART_RATE_UUID    = "00002a37-0000-1000-8000-00805f9b34fb"
FTMS_CONTROL_UUID  = "00002ad9-0000-1000-8000-00805f9b34fb"

# Mac App Store sandbox-compatible path (also works outside the sandbox)
_APP_SUPPORT       = Path.home() / "Library" / "Application Support" / "Echo Bike Tracker"
WORKOUTS_DIR       = _APP_SUPPORT / "workouts"
PROFILES_FILE      = WORKOUTS_DIR / "profiles.json"
PB_FILE            = WORKOUTS_DIR / "personal_bests.json"
ECHO_BIKE_KEYWORDS = {"rogue", "echo", "bike", "assault"}
MAX_CHART_POINTS   = 1800

# ─── Colours ──────────────────────────────────────────────────────────────────
BG          = "#0d0d1a"
CARD_BG     = "#14142a"
CARD_BORDER = "#252545"
COL_POWER   = "#ff6b35"
COL_WKG     = "#fd79a8"
COL_CADENCE = "#00d4ff"
COL_SPEED   = "#00e676"
COL_HR      = "#ff4757"
COL_CALS    = "#ffd32a"
COL_DIST    = "#a29bfe"
COL_TIME    = "#dfe6e9"
COL_MUTED   = "#6c6c9a"
COL_WHITE   = "#ffffff"
COL_WORK    = "#ff6b35"
COL_REST    = "#00d4ff"
COL_GOLD    = "#ffd700"
COL_SUCCESS = "#00e676"

# ─── Training zones ───────────────────────────────────────────────────────────
# Power zones: (min_pct_ftp, max_pct_ftp, label, colour)
POWER_ZONES = [
    (  0,  55, "Z1 Recovery",   "#6c6c9a"),
    ( 55,  75, "Z2 Endurance",  "#00d4ff"),
    ( 75,  90, "Z3 Tempo",      "#00e676"),
    ( 90, 105, "Z4 Threshold",  "#ffd32a"),
    (105, 120, "Z5 VO2 Max",    "#ff6b35"),
    (120, 150, "Z6 Anaerobic",  "#fd79a8"),
    (150, 999, "Z7 Sprint",     "#ff4757"),
]
# HR zones: HRR % (Karvonen)
HR_ZONES = [
    (  0,  50, "Z1 Rest",       "#6c6c9a"),
    ( 50,  60, "Z2 Easy",       "#00d4ff"),
    ( 60,  70, "Z3 Aerobic",    "#00e676"),
    ( 70,  80, "Z4 Threshold",  "#ffd32a"),
    ( 80,  90, "Z5 Hard",       "#ff6b35"),
    ( 90, 200, "Z6 Max",        "#ff4757"),
]

def get_power_zone(watts: int, ftp: int) -> tuple[int, str, str]:
    if ftp <= 0:
        return 0, "", COL_MUTED
    pct = watts / ftp * 100
    for i, (lo, hi, name, col) in enumerate(POWER_ZONES):
        if pct < hi:
            return i, name, col
    return len(POWER_ZONES)-1, POWER_ZONES[-1][2], POWER_ZONES[-1][3]

def get_hr_zone(hr: int, resting: int, max_hr: int) -> tuple[int, str, str]:
    hrr = max_hr - resting
    if hrr <= 0:
        return 0, "", COL_MUTED
    pct = max(0.0, (hr - resting) / hrr * 100)
    for i, (lo, hi, name, col) in enumerate(HR_ZONES):
        if pct < hi:
            return i, name, col
    return len(HR_ZONES)-1, HR_ZONES[-1][2], HR_ZONES[-1][3]

def vo2max_from_ftp(ftp_w: float, weight_kg: float) -> float:
    """Hawley & Noakes via Coggan: MAP = FTP/0.76; VO2max = (MAP×10.8/weight)+7"""
    if weight_kg <= 0 or ftp_w <= 0: return 0.0
    map_w = ftp_w / 0.76
    return round((map_w * 10.8 / weight_kg) + 7, 1)

def vo2max_from_hr(max_hr: int, resting_hr: int) -> float:
    """Uth–Sørensen formula: VO2max ≈ 15 × (HRmax / HRrest)"""
    if resting_hr <= 0 or max_hr <= 0: return 0.0
    return round(15 * max_hr / resting_hr, 1)

def vo2max_classification(v: float) -> tuple[str, str]:
    """Returns (label, colour) for a VO2max value."""
    for lo, hi, label, col in [
        (0,  30, "Poor",          "#ff4757"),
        (30, 38, "Below average", "#fd79a8"),
        (38, 46, "Average",       "#ffd32a"),
        (46, 54, "Good",          "#00e676"),
        (54, 62, "Very good",     "#00d4ff"),
        (62, 999,"Excellent",     "#a29bfe"),
    ]:
        if v < hi:
            return label, col
    return "Excellent", "#a29bfe"

# ─── Rider profiles ───────────────────────────────────────────────────────────
RIDER_NAMES = ["Clare", "Richie", "Ross", "Guest"]

DEFAULT_PROFILES: dict[str, dict] = {
    "Clare":  {"weight_kg": 65.0, "age": 35, "resting_hr": 55, "ftp_w": 180, "max_hr_override": 0},
    "Richie": {"weight_kg": 80.0, "age": 38, "resting_hr": 52, "ftp_w": 220, "max_hr_override": 0},
    "Ross":   {"weight_kg": 75.0, "age": 35, "resting_hr": 58, "ftp_w": 200, "max_hr_override": 0},
    "Guest":  {"weight_kg": 75.0, "age": 30, "resting_hr": 60, "ftp_w": 200, "max_hr_override": 0},
}

def _profile_max_hr(p: dict) -> int:
    override = p.get("max_hr_override", 0)
    return override if override > 0 else 220 - p.get("age", 30)

def load_profiles() -> dict:
    base = {k: dict(v) for k, v in DEFAULT_PROFILES.items()}
    if PROFILES_FILE.exists():
        try:
            saved = json.loads(PROFILES_FILE.read_text())
            for name, data in saved.items():
                if name in base:
                    base[name].update(data)
                else:
                    base[name] = data
        except Exception:
            pass
    return base

def save_profiles(profiles: dict):
    WORKOUTS_DIR.mkdir(parents=True, exist_ok=True)
    PROFILES_FILE.write_text(json.dumps(profiles, indent=2))

def load_pbs() -> dict:
    if PB_FILE.exists():
        try:
            return json.loads(PB_FILE.read_text())
        except Exception:
            pass
    return {}

def save_pbs(pbs: dict):
    WORKOUTS_DIR.mkdir(parents=True, exist_ok=True)
    PB_FILE.write_text(json.dumps(pbs, indent=2))

def update_pbs(rider: str, summary: dict) -> dict[str, bool]:
    """Update PBs for rider; returns {field: True} for new records."""
    if not rider:
        return {}
    pbs  = load_pbs()
    rpb  = pbs.get(rider, {})
    new: dict[str, bool] = {}

    def chk(key, val):
        if val is None: return
        if rpb.get(key) is None or val > rpb[key]:
            rpb[key] = val
            new[key] = True

    dur  = summary.get("duration_seconds") or 0
    dist = (summary.get("distance_m") or 0) / 1000
    cals = summary.get("total_calories") or 0

    rpb["total_time_s"]      = rpb.get("total_time_s", 0)      + dur
    rpb["total_distance_km"] = round(rpb.get("total_distance_km", 0) + dist, 3)
    rpb["total_calories"]    = rpb.get("total_calories", 0)     + int(cals)
    rpb["workout_count"]     = rpb.get("workout_count", 0)      + 1

    chk("max_power_w",     summary.get("max_power_w"))
    chk("best_avg_power",  summary.get("avg_power_w"))
    chk("best_avg_wkg",    summary.get("avg_wkg"))
    chk("max_hr",          summary.get("max_hr"))
    chk("max_cadence_rpm", summary.get("max_cadence_rpm"))

    pbs[rider] = rpb
    save_pbs(pbs)
    return new

# ─── FTMS parser ──────────────────────────────────────────────────────────────

def parse_indoor_bike_data(raw: bytes) -> dict:
    if len(raw) < 2: return {}
    result: dict = {}
    off = 0
    flags = struct.unpack_from("<H", raw, off)[0]; off += 2

    def rd(fmt):
        nonlocal off
        n = struct.calcsize(fmt)
        if off + n > len(raw): return None
        v = struct.unpack_from(fmt, raw, off)[0]; off += n; return v

    if not (flags & 0x0001):
        v = rd("<H"); result["speed_kmh"]       = round(v*0.01, 2) if v is not None else None
    if flags & 0x0002:
        v = rd("<H"); result["avg_speed_kmh"]   = round(v*0.01, 2) if v is not None else None
    if flags & 0x0004:
        v = rd("<H"); result["cadence_rpm"]     = round(v*0.5, 1)  if v is not None else None
    if flags & 0x0008:
        v = rd("<H"); result["avg_cadence_rpm"] = round(v*0.5, 1)  if v is not None else None
    if flags & 0x0010:
        if off+3 <= len(raw):
            result["distance_m"] = struct.unpack_from("<I", raw[off:off+3]+b"\x00")[0]; off += 3
    if flags & 0x0020:
        v = rd("<h"); result["resistance"]      = v
    if flags & 0x0040:
        v = rd("<h"); result["power_w"]         = v
    if flags & 0x0080:
        v = rd("<h"); result["avg_power_w"]     = v
    if flags & 0x0100:
        t = rd("<H"); h = rd("<H"); m = rd("<B")
        if t is not None: result["calories"]            = t
        if h is not None: result["calories_per_hour"]   = h
        if m is not None: result["calories_per_minute"] = m
    if flags & 0x0200:
        v = rd("<B"); result["heart_rate"]      = v
    if flags & 0x0400: rd("<B")
    if flags & 0x0800:
        v = rd("<H"); result["elapsed_seconds"] = v
    if flags & 0x1000:
        v = rd("<H"); result["remaining_seconds"] = v
    return {k: v for k, v in result.items() if v is not None}

def parse_heart_rate(raw: bytes) -> int | None:
    if not raw: return None
    return struct.unpack_from("<H", raw, 1)[0] if (raw[0] & 0x01) and len(raw) >= 3 \
           else (raw[1] if len(raw) >= 2 else None)

# ─── Segment model ────────────────────────────────────────────────────────────

@dataclass
class Segment:
    name:         str
    duration_s:   int
    target_type:  str   # "Open"|"Power"|"HR"|"Cadence"
    target_value: int = 0

    @property
    def duration_str(self) -> str:
        m, s = divmod(self.duration_s, 60); return f"{m:02d}:{s:02d}"

    @property
    def target_str(self) -> str:
        u = {"Power": "W", "HR": "bpm", "Cadence": "rpm"}
        return "Open" if self.target_type == "Open" \
               else f"{self.target_value} {u.get(self.target_type,'')}"

    @property
    def color(self) -> str:
        return {"Warmup": COL_MUTED, "Cooldown": COL_MUTED,
                "Work": COL_WORK,    "Sprint": COL_WORK,
                "Rest": COL_REST,    "Recovery": COL_REST,
                }.get(self.name, COL_WHITE)

SEGMENT_NAMES = ["Warmup","Work","Sprint","Steady","Rest","Recovery","Cooldown","Custom"]

WORKOUT_TEMPLATES: dict[str, list[Segment]] = {
    "Tabata  (20s/10s × 8)": [
        Segment("Warmup",   300, "Open"),
        *([Segment("Work",   20, "Power", 250), Segment("Rest",   10, "Power",  80)] * 8),
        Segment("Cooldown", 300, "Open"),
    ],
    "30/30 Intervals × 10": [
        Segment("Warmup",   300, "Open"),
        *([Segment("Work",   30, "Power", 200), Segment("Rest",   30, "Power",  80)] * 10),
        Segment("Cooldown", 300, "Open"),
    ],
    "40/20 Intervals × 8": [
        Segment("Warmup",   300, "Open"),
        *([Segment("Work",   40, "Power", 220), Segment("Rest",   20, "Power",  80)] * 8),
        Segment("Cooldown", 300, "Open"),
    ],
    "Steady State  20 min": [
        Segment("Warmup",  300, "Open"),
        Segment("Steady", 1200, "Power", 150),
        Segment("Cooldown", 300, "Open"),
    ],
    "Zone 2  45 min Easy": [
        Segment("Warmup",   300, "Open"),
        Segment("Steady",  2700, "Power", 130),
        Segment("Cooldown", 300, "Open"),
    ],
    "10-20-30  × 5 rounds": [
        Segment("Warmup",     300, "Open"),
        *([Segment("Easy",     30, "Power", 100),
           Segment("Moderate", 20, "Power", 180),
           Segment("Sprint",   10, "Power", 350)] * 5),
        Segment("Cooldown",   300, "Open"),
    ],
    "Pyramid  (1-2-3-2-1 min)": [
        Segment("Warmup",   300, "Open"),
        Segment("Work",      60, "Power", 230), Segment("Rest",  60, "Power", 80),
        Segment("Work",     120, "Power", 210), Segment("Rest",  60, "Power", 80),
        Segment("Work",     180, "Power", 190), Segment("Rest",  90, "Power", 80),
        Segment("Work",     120, "Power", 210), Segment("Rest",  60, "Power", 80),
        Segment("Work",      60, "Power", 230),
        Segment("Cooldown",  300, "Open"),
    ],
    "EMOM  (40s work / 20s rest × 12)": [
        Segment("Warmup",    300, "Open"),
        *([Segment("Work",    40, "Power", 200), Segment("Rest",   20, "Power",  70)] * 12),
        Segment("Cooldown",  300, "Open"),
    ],
    "4×8 min Threshold": [
        Segment("Warmup",    600, "Open"),
        *([Segment("Work",   480, "Power", 200), Segment("Rest",  240, "Power",  80)] * 4),
        Segment("Cooldown",  600, "Open"),
    ],
    "6×1 min VO2 Max": [
        Segment("Warmup",    600, "Open"),
        *([Segment("Sprint",  60, "Power", 280), Segment("Rest",  120, "Power",  70)] * 6),
        Segment("Cooldown",  600, "Open"),
    ],
    "Sprint Repeats  8×20s": [
        Segment("Warmup",    300, "Open"),
        *([Segment("Sprint",  20, "Power", 400), Segment("Rest",   40, "Power",  60)] * 8),
        Segment("Cooldown",  300, "Open"),
    ],
    "5×5 min Tempo": [
        Segment("Warmup",    600, "Open"),
        *([Segment("Work",   300, "Power", 185), Segment("Rest",   60, "Power",  80)] * 5),
        Segment("Cooldown",  600, "Open"),
    ],
    "FTP Test  (20 min all-out)": [
        Segment("Warmup",    600, "Open"),
        Segment("Easy",      300, "Power", 100),
        Segment("Sprint",     30, "Power", 300),
        Segment("Rest",      300, "Open"),
        Segment("TEST",     1200, "Open"),
        Segment("Cooldown",  600, "Open"),
    ],
    "Ramp Test  (1 min steps)": [
        Segment("Warmup",   300, "Power",  80),
        *[Segment(f"Step {i+1}", 60, "Power", 100 + i*20) for i in range(13)],
        Segment("Cooldown", 300, "Open"),
    ],
}

# ─── BLE worker ───────────────────────────────────────────────────────────────

class BleWorker(QObject):
    data_received = pyqtSignal(dict)
    connected     = pyqtSignal(str)
    disconnected  = pyqtSignal()
    error         = pyqtSignal(str)
    scan_result   = pyqtSignal(list)
    control_sent  = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: BleakClient | None = None

    def start(self): threading.Thread(target=self._run_loop, daemon=True).start()

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _go(self, coro):
        if self._loop: asyncio.run_coroutine_threadsafe(coro, self._loop)

    def scan(self):                  self._go(self._scan())
    def connect_to(self, addr: str): self._go(self._connect(addr))
    def disconnect(self):            self._go(self._disconnect())

    async def _scan(self):
        try:
            devs = await BleakScanner.discover(timeout=10.0, service_uuids=[FTMS_SERVICE_UUID])
            if not devs:
                all_d = await BleakScanner.discover(timeout=8.0)
                devs  = [d for d in all_d
                         if d.name and any(k in d.name.lower() for k in ECHO_BIKE_KEYWORDS)]
            self.scan_result.emit([(d.name or "Unknown", d.address) for d in devs])
        except Exception as e: self.error.emit(str(e))

    async def _connect(self, addr: str):
        try:
            self._client = BleakClient(
                addr, timeout=20.0,
                disconnected_callback=lambda _: self.disconnected.emit(),
            )
            await self._client.connect()
            self.connected.emit(addr)
            await self._client.start_notify(INDOOR_BIKE_DATA, self._on_bike)
            try: await self._client.start_notify(HEART_RATE_UUID, self._on_hr)
            except Exception: pass
        except Exception as e: self.error.emit(str(e))

    async def _disconnect(self):
        if self._client and self._client.is_connected:
            try: await self._client.stop_notify(INDOOR_BIKE_DATA)
            except Exception: pass
            await self._client.disconnect()

    def _on_bike(self, _s, raw: bytes):
        parsed = parse_indoor_bike_data(bytes(raw))
        if parsed: parsed["ts"] = datetime.now().isoformat(); self.data_received.emit(parsed)

    def _on_hr(self, _s, raw: bytes):
        hr = parse_heart_rate(bytes(raw))
        if hr is not None: self.data_received.emit({"heart_rate": hr, "ts": datetime.now().isoformat()})

    def request_control(self):      self._go(self._ctrl_write(bytes([0x00]), "Request Control"))
    def send_target_power(self, w): self._go(self._ctrl_write(bytes([0x05, w&0xFF, (w>>8)&0xFF]), f"Power→{w}W"))
    def send_target_hr(self, b):    self._go(self._ctrl_write(bytes([0x06, max(0,min(b,255))]), f"HR→{b}bpm"))
    def send_target_cadence(self, r): self._go(self._ctrl_write(bytes([0x14, r&0xFF, (r>>8)&0xFF]), f"Cadence→{r}rpm"))
    def ftms_start(self):           self._go(self._ctrl_write(bytes([0x07]), "Start"))
    def ftms_stop(self):            self._go(self._ctrl_write(bytes([0x08, 0x01]), "Stop"))

    async def _ctrl_write(self, data: bytes, desc: str):
        if not (self._client and self._client.is_connected): return
        try:
            await self._client.write_gatt_char(FTMS_CONTROL_UUID, data, response=True)
            self.control_sent.emit(desc)
        except Exception: pass

# ─── Metric card ──────────────────────────────────────────────────────────────

class MetricCard(QFrame):
    def __init__(self, label: str, unit: str, color: str,
                 big: bool = False, show_zone: bool = False, show_stats: bool = False):
        super().__init__()
        self._color = color
        self._has_zone  = show_zone
        self._has_stats = show_stats
        self.setStyleSheet(
            f"MetricCard{{background:{CARD_BG};border:1px solid {CARD_BORDER};border-radius:14px;}}"
        )
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(110, 110)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(1)

        lbl = QLabel(label)
        lbl.setStyleSheet(
            f"color:{COL_MUTED};font-size:10px;font-weight:700;"
            f"letter-spacing:1.5px;border:none;background:transparent;"
        )
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(lbl)
        lay.addStretch(1)

        fs = 40 if big else 34
        self._val = QLabel("--")
        self._val.setStyleSheet(
            f"color:{color};font-size:{fs}px;font-weight:800;"
            f"border:none;background:transparent;"
        )
        self._val.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._val)

        unit_lbl = QLabel(unit)
        unit_lbl.setStyleSheet(f"color:{COL_MUTED};font-size:11px;border:none;background:transparent;")
        unit_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(unit_lbl)
        lay.addStretch(1)

        if show_zone:
            self._zone_lbl = QLabel("")
            self._zone_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._zone_lbl.setStyleSheet(
                f"color:{COL_MUTED};font-size:9px;font-weight:700;"
                f"letter-spacing:0.5px;border:none;background:transparent;"
            )
            lay.addWidget(self._zone_lbl)

        if show_stats:
            sr = QHBoxLayout(); sr.setSpacing(0)
            self._min_lbl = QLabel("↓--")
            self._avg_lbl = QLabel("~--")
            self._max_lbl = QLabel("↑--")
            for l in (self._min_lbl, self._avg_lbl, self._max_lbl):
                l.setStyleSheet(
                    f"color:{COL_MUTED};font-size:9px;border:none;background:transparent;"
                )
                l.setAlignment(Qt.AlignmentFlag.AlignCenter)
                sr.addWidget(l)
            lay.addLayout(sr)

    def set_value(self, v, decimals: int = 0):
        if isinstance(v, (int, float)):
            self._val.setText(f"{v:.{decimals}f}" if decimals else f"{v:.0f}")
        else:
            self._val.setText(str(v))

    def set_zone(self, name: str, color: str):
        if self._has_zone:
            self._zone_lbl.setText(name)
            self._zone_lbl.setStyleSheet(
                f"color:{color};font-size:9px;font-weight:700;"
                f"letter-spacing:0.5px;border:none;background:transparent;"
            )

    def set_stats(self, mn, avg, mx, fmt="{:.0f}"):
        if not self._has_stats: return
        self._min_lbl.setText(f"↓{fmt.format(mn)}" if mn is not None else "↓--")
        self._avg_lbl.setText(f"~{fmt.format(avg)}" if avg is not None else "~--")
        self._max_lbl.setText(f"↑{fmt.format(mx)}" if mx is not None else "↑--")

# ─── Button style ─────────────────────────────────────────────────────────────

def _btn_style(color: str, width: int | None = None) -> str:
    w = f"min-width:{width}px;" if width else ""
    return f"""
        QPushButton {{
            background:{color}20;color:{color};
            border:1px solid {color}60;border-radius:9px;
            font-size:13px;font-weight:700;
            padding:0 18px;height:38px;{w}
        }}
        QPushButton:hover   {{background:{color}40;}}
        QPushButton:pressed {{background:{color}60;}}
        QPushButton:disabled {{background:transparent;color:#3a3a5a;border-color:#2a2a4a;}}
    """

def _field_ss() -> str:
    return (f"background:#0d0d1a;color:{COL_WHITE};"
            f"border:1px solid {CARD_BORDER};border-radius:6px;"
            f"padding:3px 8px;font-size:12px;")

# ─── Profile dialog ───────────────────────────────────────────────────────────

class ProfileDialog(QDialog):
    def __init__(self, name: str, profile: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Profile: {name}")
        self.setStyleSheet(f"background:{BG};color:{COL_WHITE};")
        self.resize(360, 280)
        self._result: dict | None = None

        lay = QVBoxLayout(self)
        lay.setSpacing(12); lay.setContentsMargins(20, 16, 20, 16)

        grid = QGridLayout(); grid.setSpacing(10)

        def row(label, widget, r):
            lbl = QLabel(label)
            lbl.setStyleSheet(f"color:{COL_MUTED};font-size:11px;font-weight:700;")
            grid.addWidget(lbl, r, 0)
            grid.addWidget(widget, r, 1)

        self._weight = QDoubleSpinBox(); self._weight.setRange(30, 250); self._weight.setDecimals(1)
        self._weight.setSuffix(" kg"); self._weight.setValue(profile.get("weight_kg", 75))
        self._weight.setStyleSheet(_field_ss())

        self._age = QSpinBox(); self._age.setRange(10, 90)
        self._age.setValue(profile.get("age", 30)); self._age.setStyleSheet(_field_ss())

        self._rest_hr = QSpinBox(); self._rest_hr.setRange(30, 120)
        self._rest_hr.setSuffix(" bpm"); self._rest_hr.setValue(profile.get("resting_hr", 60))
        self._rest_hr.setStyleSheet(_field_ss())

        self._ftp = QSpinBox(); self._ftp.setRange(50, 2000)
        self._ftp.setSuffix(" W"); self._ftp.setValue(profile.get("ftp_w", 200))
        self._ftp.setStyleSheet(_field_ss())

        self._max_hr_ov = QSpinBox(); self._max_hr_ov.setRange(0, 250)
        self._max_hr_ov.setSuffix(" bpm (0=auto)")
        self._max_hr_ov.setValue(profile.get("max_hr_override", 0))
        self._max_hr_ov.setStyleSheet(_field_ss())

        row("Weight",           self._weight,   0)
        row("Age",              self._age,       1)
        row("Resting HR",       self._rest_hr,   2)
        row("FTP",              self._ftp,       3)
        row("Max HR override",  self._max_hr_ov, 4)
        lay.addLayout(grid)

        note = QLabel("FTP & zones power your training zone display and VO2max estimates.")
        note.setStyleSheet(f"color:{COL_MUTED};font-size:10px;")
        note.setWordWrap(True); lay.addWidget(note)

        btns = QHBoxLayout()
        cancel = QPushButton("Cancel"); cancel.setStyleSheet(_btn_style(COL_MUTED, 90))
        cancel.clicked.connect(self.reject)
        save   = QPushButton("Save");   save.setStyleSheet(_btn_style(COL_SUCCESS, 90))
        save.clicked.connect(self._save)
        btns.addWidget(cancel); btns.addStretch(); btns.addWidget(save)
        lay.addLayout(btns)

    def _save(self):
        self._result = {
            "weight_kg":        self._weight.value(),
            "age":              self._age.value(),
            "resting_hr":       self._rest_hr.value(),
            "ftp_w":            self._ftp.value(),
            "max_hr_override":  self._max_hr_ov.value(),
        }
        self.accept()

# ─── Post-workout summary dialog ──────────────────────────────────────────────

class PostWorkoutSummaryDialog(QDialog):
    def __init__(self, summary: dict, new_pbs: dict, rider_totals: dict,
                 profile: dict, zone_p: list[int], zone_hr: list[int], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Workout Summary")
        self.setStyleSheet(f"background:{BG};color:{COL_WHITE};")
        self.resize(700, 580)

        main = QVBoxLayout(self)
        main.setSpacing(10); main.setContentsMargins(20, 16, 20, 16)

        # ── Header ────────────────────────────────────────────────────────────
        rider = summary.get("rider", "") or "Guest"
        dt    = summary.get("date", "")[:16].replace("T", "  ")
        title = QLabel(f"🏁  {rider}  ·  {dt}")
        title.setStyleSheet(f"color:{COL_POWER};font-size:18px;font-weight:900;")
        main.addWidget(title)

        tabs = QTabWidget()
        tabs.setStyleSheet(f"""
            QTabWidget::pane {{border:1px solid {CARD_BORDER};border-radius:8px;background:{CARD_BG};}}
            QTabBar::tab {{background:{BG};color:{COL_MUTED};padding:6px 18px;
                           border-radius:6px;font-size:12px;font-weight:700;}}
            QTabBar::tab:selected {{background:{CARD_BG};color:{COL_WHITE};}}
        """)
        main.addWidget(tabs, stretch=1)

        tabs.addTab(self._build_stats_tab(summary, new_pbs), "Stats")
        tabs.addTab(self._build_zones_tab(zone_p, zone_hr, profile), "Zones")
        tabs.addTab(self._build_totals_tab(rider_totals, profile, summary), "Totals & VO2")

        close = QPushButton("Close")
        close.setStyleSheet(_btn_style(COL_MUTED, 100))
        close.clicked.connect(self.accept)
        row = QHBoxLayout(); row.addStretch(); row.addWidget(close)
        main.addLayout(row)

    # ── Stats tab ─────────────────────────────────────────────────────────────
    def _build_stats_tab(self, s: dict, new_pbs: dict) -> QWidget:
        w = QWidget(); lay = QVBoxLayout(w); lay.setSpacing(8)

        def dur_str(secs):
            if not secs: return "--"
            h, r = divmod(int(secs), 3600); m, s = divmod(r, 60)
            return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

        rows = [
            ("Duration",      dur_str(s.get("duration_seconds")),        COL_TIME),
            ("Distance",      f"{s['distance_m']/1000:.2f} km" if s.get("distance_m") else "--", COL_DIST),
            ("Calories",      f"{s.get('total_calories', '--')} kcal",   COL_CALS),
            ("Avg Power",     f"{s.get('avg_power_w', '--')} W",         COL_POWER),
            ("Max Power",     f"{s.get('max_power_w', '--')} W",         COL_POWER),
            ("Avg W/kg",      f"{s.get('avg_wkg', '--')}",               COL_WKG),
            ("Avg HR",        f"{s.get('avg_hr', '--')} bpm",            COL_HR),
            ("Max HR",        f"{s.get('max_hr', '--')} bpm",            COL_HR),
            ("Avg Cadence",   f"{s.get('avg_cadence_rpm', '--')} rpm",   COL_CADENCE),
            ("Max Cadence",   f"{s.get('max_cadence_rpm', '--')} rpm",   COL_CADENCE),
        ]

        grid = QGridLayout(); grid.setSpacing(6)
        for i, (label, val, color) in enumerate(rows):
            is_pb = new_pbs.get(label.lower().replace(" ", "_"))
            pb_tag = "  🏆 NEW PB!" if is_pb else ""

            lbl = QLabel(label + ":"); lbl.setStyleSheet(f"color:{COL_MUTED};font-size:12px;")
            v   = QLabel(val + pb_tag)
            v.setStyleSheet(f"color:{COL_GOLD if is_pb else color};font-size:13px;font-weight:700;")
            grid.addWidget(lbl, i, 0)
            grid.addWidget(v,   i, 1)

        grid.setColumnStretch(1, 1)
        lay.addLayout(grid); lay.addStretch()
        return w

    # ── Zones tab ─────────────────────────────────────────────────────────────
    def _build_zones_tab(self, zone_p: list[int], zone_hr: list[int], profile: dict) -> QWidget:
        w = QWidget(); lay = QVBoxLayout(w); lay.setSpacing(12)

        total_p  = sum(zone_p)  or 1
        total_hr = sum(zone_hr) or 1

        def zone_block(title, counts, zones, total):
            frame = QFrame()
            frame.setStyleSheet(
                f"QFrame{{background:{BG};border:1px solid {CARD_BORDER};border-radius:8px;}}"
            )
            fl = QVBoxLayout(frame); fl.setContentsMargins(12, 8, 12, 8); fl.setSpacing(4)
            hdr = QLabel(title)
            hdr.setStyleSheet(f"color:{COL_MUTED};font-size:10px;font-weight:700;letter-spacing:1px;")
            fl.addWidget(hdr)
            for i, (lo, hi, name, color) in enumerate(zones):
                secs = counts[i] if i < len(counts) else 0
                pct  = secs / total * 100
                m, s = divmod(secs, 60)
                row  = QHBoxLayout(); row.setSpacing(8)
                nl = QLabel(name); nl.setFixedWidth(130)
                nl.setStyleSheet(f"color:{color};font-size:11px;font-weight:700;")
                bar = QProgressBar(); bar.setRange(0, 100); bar.setValue(int(pct))
                bar.setTextVisible(False); bar.setFixedHeight(12)
                bar.setStyleSheet(f"""
                    QProgressBar {{background:{CARD_BORDER};border-radius:6px;border:none;}}
                    QProgressBar::chunk {{background:{color};border-radius:6px;}}
                """)
                tl = QLabel(f"{m:02d}:{s:02d}  ({pct:.0f}%)")
                tl.setFixedWidth(90)
                tl.setStyleSheet(f"color:{COL_MUTED};font-size:10px;")
                row.addWidget(nl); row.addWidget(bar, 1); row.addWidget(tl)
                fl.addLayout(row)
            return frame

        lay.addWidget(zone_block("POWER ZONES  (% FTP)", zone_p, POWER_ZONES, total_p))
        lay.addWidget(zone_block("HR ZONES  (HRR Karvonen)", zone_hr, HR_ZONES, total_hr))
        ftp = profile.get("ftp_w", 0)
        note = QLabel(f"FTP: {ftp} W   Resting HR: {profile.get('resting_hr',0)} bpm   "
                      f"Max HR: {_profile_max_hr(profile)} bpm")
        note.setStyleSheet(f"color:{COL_MUTED};font-size:10px;")
        lay.addWidget(note); lay.addStretch()
        return w

    # ── Totals & VO2 tab ──────────────────────────────────────────────────────
    def _build_totals_tab(self, totals: dict, profile: dict, summary: dict) -> QWidget:
        w = QWidget(); lay = QVBoxLayout(w); lay.setSpacing(10)

        def fmt_time(secs):
            h = int(secs) // 3600; m = (int(secs) % 3600) // 60
            return f"{h}h {m:02d}m"

        total_rows = [
            ("Workouts completed",  str(totals.get("workout_count", 0)),          COL_WHITE),
            ("Total time",          fmt_time(totals.get("total_time_s", 0)),       COL_TIME),
            ("Total distance",      f"{totals.get('total_distance_km', 0):.1f} km", COL_DIST),
            ("Total calories",      f"{totals.get('total_calories', 0):,} kcal",   COL_CALS),
            ("Best avg power",      f"{totals.get('best_avg_power', '--')} W",     COL_POWER),
            ("All-time max power",  f"{totals.get('max_power_w', '--')} W",        COL_POWER),
            ("Best avg W/kg",       f"{totals.get('best_avg_wkg', '--')}",         COL_WKG),
        ]

        hdr = QLabel("LIFETIME TOTALS")
        hdr.setStyleSheet(f"color:{COL_MUTED};font-size:10px;font-weight:700;letter-spacing:1px;")
        lay.addWidget(hdr)

        grid = QGridLayout(); grid.setSpacing(6)
        for i, (label, val, color) in enumerate(total_rows):
            lbl = QLabel(label + ":"); lbl.setStyleSheet(f"color:{COL_MUTED};font-size:12px;")
            v   = QLabel(val);         v.setStyleSheet(f"color:{color};font-size:13px;font-weight:700;")
            grid.addWidget(lbl, i, 0); grid.addWidget(v, i, 1)
        grid.setColumnStretch(1, 1)
        lay.addLayout(grid)

        # VO2max estimates
        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color:{CARD_BORDER};"); lay.addWidget(sep)

        v2_hdr = QLabel("VO2 MAX ESTIMATES")
        v2_hdr.setStyleSheet(f"color:{COL_MUTED};font-size:10px;font-weight:700;letter-spacing:1px;")
        lay.addWidget(v2_hdr)

        ftp     = profile.get("ftp_w", 0)
        wt      = profile.get("weight_kg", 75)
        rest    = profile.get("resting_hr", 60)
        prof_mhr = _profile_max_hr(profile)
        # Use the higher of session-recorded max HR vs age-predicted — actual measurement wins
        sess_mhr = summary.get("max_hr") or 0
        best_mhr = max(sess_mhr, prof_mhr)
        mhr_source = "recorded session max" if sess_mhr >= prof_mhr else f"estimated (220−age)"

        shown_any = False

        if ftp > 0:
            v2p = vo2max_from_ftp(ftp, wt)
            cls, cls_col = vo2max_classification(v2p)
            ftp_wkg = round(ftp / wt, 2)
            row = QHBoxLayout()
            lbl = QLabel(f"Power  {v2p} ml/kg/min")
            lbl.setStyleSheet(f"color:{COL_POWER};font-size:14px;font-weight:800;")
            cls_lbl = QLabel(f"  {cls}")
            cls_lbl.setStyleSheet(f"color:{cls_col};font-size:13px;font-weight:700;")
            src_lbl = QLabel(f"   via FTP {ftp} W  ({ftp_wkg} W/kg)")
            src_lbl.setStyleSheet(f"color:{COL_MUTED};font-size:11px;")
            row.addWidget(lbl); row.addWidget(cls_lbl); row.addWidget(src_lbl); row.addStretch()
            lay.addLayout(row)
            shown_any = True

        if rest > 0 and best_mhr > rest:
            v2h = vo2max_from_hr(best_mhr, rest)
            cls, cls_col = vo2max_classification(v2h)
            row = QHBoxLayout()
            lbl = QLabel(f"HR      {v2h} ml/kg/min")
            lbl.setStyleSheet(f"color:{COL_HR};font-size:14px;font-weight:800;")
            cls_lbl = QLabel(f"  {cls}")
            cls_lbl.setStyleSheet(f"color:{cls_col};font-size:13px;font-weight:700;")
            src_lbl = QLabel(f"   max HR {best_mhr} bpm ({mhr_source}), rest {rest} bpm")
            src_lbl.setStyleSheet(f"color:{COL_MUTED};font-size:11px;")
            row.addWidget(lbl); row.addWidget(cls_lbl); row.addWidget(src_lbl); row.addStretch()
            lay.addLayout(row)
            shown_any = True

        if not shown_any:
            lay.addWidget(QLabel("Set your FTP and resting HR in the profile (⚙) to see estimates."))

        note = QLabel(
            "Power estimate: uses your FTP via Coggan / Hawley & Noakes (MAP = FTP ÷ 0.76).\n"
            "HR estimate: Uth–Sørensen formula — uses the higher of session-recorded or age-predicted max HR.\n"
            "Both are estimates. Lab VO2max testing gives the most accurate result."
        )
        note.setStyleSheet(f"color:{COL_MUTED};font-size:10px;"); note.setWordWrap(True)
        lay.addWidget(note); lay.addStretch()
        return w

# ─── Workout Builder dialog ───────────────────────────────────────────────────

class WorkoutBuilderDialog(QDialog):
    def __init__(self, initial_plan: list[Segment] | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Workout Builder")
        self.setStyleSheet(f"background:{BG};color:{COL_WHITE};")
        self.resize(860, 560)
        self.result_plan: list[Segment] = []
        self._segs: list[Segment] = copy.deepcopy(initial_plan or [])

        lay = QVBoxLayout(self); lay.setSpacing(10); lay.setContentsMargins(16,14,16,14)
        lay.addLayout(self._build_template_row())
        lay.addWidget(self._build_table())
        lay.addLayout(self._build_add_row())
        lay.addLayout(self._build_bottom_row())
        self._rebuild_table()

    def _build_template_row(self) -> QHBoxLayout:
        row = QHBoxLayout(); row.setSpacing(10)
        lbl = QLabel("PRESET TEMPLATES")
        lbl.setStyleSheet(f"color:{COL_MUTED};font-size:10px;font-weight:700;letter-spacing:1px;")
        row.addWidget(lbl)
        self._tmpl_combo = QComboBox()
        self._tmpl_combo.addItem("— choose a template —")
        self._tmpl_combo.addItems(list(WORKOUT_TEMPLATES.keys()))
        self._tmpl_combo.setMinimumWidth(280); self._tmpl_combo.setStyleSheet(_field_ss())
        row.addWidget(self._tmpl_combo)
        load_btn = QPushButton("Load Template")
        load_btn.setStyleSheet(_btn_style(COL_CADENCE, 130))
        load_btn.clicked.connect(self._load_template); row.addWidget(load_btn)
        row.addStretch()
        self._total_lbl = QLabel("Total: 00:00")
        self._total_lbl.setStyleSheet(f"color:{COL_MUTED};font-size:12px;font-weight:700;")
        row.addWidget(self._total_lbl)
        return row

    def _build_table(self) -> QTableWidget:
        self._tbl = QTableWidget(); self._tbl.setColumnCount(7)
        self._tbl.setHorizontalHeaderLabels(["#","Segment","Duration","Target","Value","↑↓","✕"])
        self._tbl.setStyleSheet(f"""
            QTableWidget {{background:{CARD_BG};color:{COL_WHITE};
                border:1px solid {CARD_BORDER};border-radius:8px;
                gridline-color:{CARD_BORDER};font-size:12px;}}
            QHeaderView::section {{background:#1a1a38;color:{COL_MUTED};
                border:none;padding:5px 4px;font-size:10px;font-weight:700;letter-spacing:1px;}}
            QTableWidget::item:selected {{background:#2a2a5a;}}
            QComboBox,QSpinBox,QLineEdit {{background:#0d0d1a;color:{COL_WHITE};
                border:1px solid {CARD_BORDER};border-radius:4px;padding:2px 4px;font-size:11px;}}
            QComboBox::drop-down {{border:none;}}
        """)
        hdr = self._tbl.horizontalHeader()
        for i, m in [(0, QHeaderView.ResizeMode.ResizeToContents),
                     (1, QHeaderView.ResizeMode.Stretch),
                     (2, QHeaderView.ResizeMode.ResizeToContents),
                     (3, QHeaderView.ResizeMode.ResizeToContents),
                     (4, QHeaderView.ResizeMode.ResizeToContents),
                     (5, QHeaderView.ResizeMode.ResizeToContents),
                     (6, QHeaderView.ResizeMode.ResizeToContents)]:
            hdr.setSectionResizeMode(i, m)
        self._tbl.verticalHeader().setVisible(False)
        self._tbl.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._tbl.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        return self._tbl

    def _build_add_row(self) -> QHBoxLayout:
        row = QHBoxLayout(); row.setSpacing(10)
        add_lbl = QLabel("ADD"); add_lbl.setStyleSheet(f"color:{COL_MUTED};font-size:10px;font-weight:700;letter-spacing:1px;")
        row.addWidget(add_lbl)
        self._add_combo = QComboBox(); self._add_combo.addItems(SEGMENT_NAMES)
        self._add_combo.setFixedWidth(120); self._add_combo.setStyleSheet(_field_ss())
        row.addWidget(self._add_combo)
        add_btn = QPushButton("+ Add Segment"); add_btn.setStyleSheet(_btn_style(COL_SPEED, 130))
        add_btn.clicked.connect(self._add_segment); row.addWidget(add_btn)
        row.addSpacing(20)
        rep_lbl = QLabel("REPEAT selected rows"); rep_lbl.setStyleSheet(f"color:{COL_MUTED};font-size:10px;font-weight:700;letter-spacing:1px;")
        row.addWidget(rep_lbl)
        self._rep_spin = QSpinBox(); self._rep_spin.setRange(2,50); self._rep_spin.setValue(8)
        self._rep_spin.setFixedWidth(60); self._rep_spin.setStyleSheet(_field_ss())
        row.addWidget(self._rep_spin)
        row.addWidget(QLabel("×"))
        rep_btn = QPushButton("Repeat"); rep_btn.setStyleSheet(_btn_style(COL_CALS, 90))
        rep_btn.clicked.connect(self._repeat_selected); row.addWidget(rep_btn)
        row.addStretch()
        clr_btn = QPushButton("Clear All"); clr_btn.setStyleSheet(_btn_style(COL_HR, 90))
        clr_btn.clicked.connect(self._clear_all); row.addWidget(clr_btn)
        return row

    def _build_bottom_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        cancel = QPushButton("Cancel"); cancel.setStyleSheet(_btn_style(COL_MUTED, 100))
        cancel.clicked.connect(self.reject); row.addWidget(cancel)
        row.addStretch()
        ok = QPushButton("✓  Load Workout"); ok.setStyleSheet(_btn_style(COL_POWER, 150))
        ok.clicked.connect(self._accept); row.addWidget(ok)
        return row

    def _rebuild_table(self):
        self._tbl.setRowCount(0)
        for i, seg in enumerate(self._segs): self._insert_row(i, seg)
        self._update_total()

    def _insert_row(self, i: int, seg: Segment):
        r = self._tbl.rowCount(); self._tbl.insertRow(r)
        num = QTableWidgetItem(str(i+1)); num.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        num.setForeground(QColor(COL_MUTED)); self._tbl.setItem(r, 0, num)
        name_cb = QComboBox(); name_cb.addItems(SEGMENT_NAMES)
        name_cb.setCurrentText(seg.name if seg.name in SEGMENT_NAMES else "Custom")
        name_cb.setStyleSheet(f"color:{seg.color};font-weight:700;"); self._tbl.setCellWidget(r, 1, name_cb)
        dur_edit = QLineEdit(seg.duration_str); dur_edit.setAlignment(Qt.AlignmentFlag.AlignCenter)
        dur_edit.setPlaceholderText("MM:SS"); self._tbl.setCellWidget(r, 2, dur_edit)
        tgt_cb = QComboBox(); tgt_cb.addItems(["Open","Power","HR","Cadence"])
        tgt_cb.setCurrentText(seg.target_type)
        val_spin = QSpinBox(); val_spin.setRange(0, 5000); val_spin.setValue(seg.target_value)
        val_spin.setEnabled(seg.target_type != "Open")
        tgt_cb.currentTextChanged.connect(lambda t, s=val_spin: s.setEnabled(t != "Open"))
        self._tbl.setCellWidget(r, 3, tgt_cb); self._tbl.setCellWidget(r, 4, val_spin)
        ud = QWidget(); ul = QHBoxLayout(ud); ul.setContentsMargins(2,2,2,2); ul.setSpacing(2)
        for sym, d in [("▲",-1),("▼",1)]:
            b = QPushButton(sym); b.setFixedSize(24,24)
            b.setStyleSheet(f"background:#1a1a38;color:{COL_MUTED};border:none;border-radius:4px;")
            b.clicked.connect(lambda _, idx=i, dd=d: self._move_seg(idx, dd)); ul.addWidget(b)
        self._tbl.setCellWidget(r, 5, ud)
        del_btn = QPushButton("✕"); del_btn.setFixedSize(28, 24)
        del_btn.setStyleSheet(f"background:{COL_HR}22;color:{COL_HR};border:1px solid {COL_HR}55;border-radius:4px;font-weight:700;")
        del_btn.clicked.connect(lambda _, idx=i: self._remove_seg(idx)); self._tbl.setCellWidget(r, 6, del_btn)
        self._tbl.setRowHeight(r, 36)

    def _collect_segs(self) -> list[Segment]:
        segs = []
        for r in range(self._tbl.rowCount()):
            name_w = self._tbl.cellWidget(r, 1); dur_w = self._tbl.cellWidget(r, 2)
            tgt_w  = self._tbl.cellWidget(r, 3); val_w = self._tbl.cellWidget(r, 4)
            name   = name_w.currentText() if name_w else "Custom"
            ttype  = tgt_w.currentText()  if tgt_w  else "Open"
            tval   = val_w.value()        if val_w  else 0
            dur_s  = 60
            if dur_w:
                txt = dur_w.text().strip()
                try:
                    parts = txt.split(":")
                    dur_s = int(parts[0])*60+int(parts[1]) if len(parts)==2 else int(parts[0])
                except (ValueError, IndexError): dur_s = 60
            segs.append(Segment(name, max(1, dur_s), ttype, tval))
        return segs

    def _load_template(self):
        key = self._tmpl_combo.currentText()
        if key in WORKOUT_TEMPLATES:
            self._segs = copy.deepcopy(WORKOUT_TEMPLATES[key]); self._rebuild_table()

    def _add_segment(self):
        self._segs = self._collect_segs()
        pm = {"Warmup": Segment("Warmup",300,"Open"), "Work": Segment("Work",20,"Power",250),
              "Sprint": Segment("Sprint",15,"Power",350), "Steady": Segment("Steady",600,"Power",160),
              "Rest": Segment("Rest",10,"Power",80), "Recovery": Segment("Recovery",90,"Power",80),
              "Cooldown": Segment("Cooldown",300,"Open"), "Custom": Segment("Custom",60,"Open")}
        name = self._add_combo.currentText()
        self._segs.append(copy.copy(pm.get(name, Segment(name,60,"Open")))); self._rebuild_table()

    def _remove_seg(self, idx: int):
        self._segs = self._collect_segs()
        if 0 <= idx < len(self._segs): self._segs.pop(idx); self._rebuild_table()

    def _move_seg(self, idx: int, d: int):
        self._segs = self._collect_segs(); j = idx + d
        if 0 <= j < len(self._segs):
            self._segs[idx], self._segs[j] = self._segs[j], self._segs[idx]; self._rebuild_table()

    def _repeat_selected(self):
        self._segs = self._collect_segs()
        rows = sorted({idx.row() for idx in self._tbl.selectedIndexes()})
        if not rows: QMessageBox.information(self, "Repeat", "Select rows first."); return
        block = [copy.copy(self._segs[r]) for r in rows if r < len(self._segs)]
        n = self._rep_spin.value(); insert_at = max(rows)+1
        for i in range(1, n):
            for j, seg in enumerate(block):
                self._segs.insert(insert_at+(i-1)*len(block)+j, copy.copy(seg))
        self._rebuild_table()

    def _clear_all(self): self._segs.clear(); self._rebuild_table()

    def _update_total(self):
        total = sum(s.duration_s for s in self._segs)
        h, rem = divmod(total, 3600); m, s = divmod(rem, 60)
        t = f"{h}h {m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
        self._total_lbl.setText(f"Total: {t}  ({len(self._segs)} segments)")

    def _accept(self):
        self.result_plan = self._collect_segs()
        if not self.result_plan: QMessageBox.warning(self, "Empty", "Add at least one segment."); return
        self.accept()

# ─── Device picker ────────────────────────────────────────────────────────────

class DevicePickerDialog(QDialog):
    def __init__(self, devices: list[tuple[str,str]], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Select Echo Bike")
        self.setStyleSheet(f"background:{BG};color:{COL_WHITE};")
        self.resize(420, 220)
        self._devices = devices; self.selected: tuple[str,str] | None = None
        lay = QVBoxLayout(self)
        lbl = QLabel("Multiple FTMS devices found — select your Echo Bike:")
        lbl.setStyleSheet(f"color:{COL_MUTED};font-size:12px;"); lay.addWidget(lbl)
        self._list = QListWidget()
        self._list.setStyleSheet(
            f"background:{CARD_BG};color:{COL_WHITE};border:1px solid {CARD_BORDER};"
            f"border-radius:8px;font-size:13px;padding:4px;"
        )
        for name, addr in devices: self._list.addItem(f"  {name}   ({addr})")
        self._list.setCurrentRow(0); lay.addWidget(self._list)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.setStyleSheet(f"color:{COL_WHITE};"); btns.accepted.connect(self._ok); btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def _ok(self):
        r = self._list.currentRow()
        if r >= 0: self.selected = self._devices[r]
        self.accept()

# ─── History dialog ───────────────────────────────────────────────────────────

class HistoryDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Workout History & Personal Bests")
        self.setStyleSheet(f"background:{BG};color:{COL_WHITE};")
        self.resize(1160, 520)

        lay = QVBoxLayout(self); lay.setSpacing(10)
        tabs = QTabWidget()
        tabs.setStyleSheet(f"""
            QTabWidget::pane {{border:1px solid {CARD_BORDER};border-radius:8px;background:{CARD_BG};}}
            QTabBar::tab {{background:{BG};color:{COL_MUTED};padding:6px 18px;border-radius:6px;
                           font-size:12px;font-weight:700;}}
            QTabBar::tab:selected {{background:{CARD_BG};color:{COL_WHITE};}}
        """)
        tabs.addTab(self._build_history_tab(), "Session History")
        tabs.addTab(self._build_pb_tab(), "Personal Bests")
        lay.addWidget(tabs)

        btn_row = QHBoxLayout()
        open_btn = QPushButton("📂  Open Folder"); open_btn.setStyleSheet(_btn_style(COL_DIST, 140))
        open_btn.clicked.connect(lambda: __import__("subprocess").run(["open", str(WORKOUTS_DIR)], check=False))
        btn_row.addWidget(open_btn); btn_row.addStretch()
        close_btn = QPushButton("Close"); close_btn.setStyleSheet(_btn_style(COL_MUTED, 100))
        close_btn.clicked.connect(self.accept); btn_row.addWidget(close_btn)
        lay.addLayout(btn_row)

    def _make_table(self, cols: list[str]) -> QTableWidget:
        tbl = QTableWidget(); tbl.setColumnCount(len(cols))
        tbl.setHorizontalHeaderLabels(cols)
        tbl.setStyleSheet(f"""
            QTableWidget {{background:{CARD_BG};color:{COL_WHITE};border:none;
                gridline-color:{CARD_BORDER};font-size:12px;}}
            QHeaderView::section {{background:#1a1a38;color:{COL_MUTED};border:none;
                padding:6px 4px;font-size:10px;font-weight:700;letter-spacing:1px;}}
            QTableWidget::item:selected {{background:#2a2a5a;}}
        """)
        tbl.verticalHeader().setVisible(False)
        tbl.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        tbl.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        tbl.setAlternatingRowColors(True)
        return tbl

    def _build_history_tab(self) -> QWidget:
        w = QWidget(); lay = QVBoxLayout(w); lay.setSpacing(8)
        info = QLabel(f"Files saved to:  {WORKOUTS_DIR}")
        info.setStyleSheet(f"color:{COL_MUTED};font-size:11px;"); lay.addWidget(info)

        cols = ["Date","Rider","Label","Type","Duration","Distance","Calories",
                "Avg W","Max W","Avg W/kg","Avg HR","Max HR","Avg RPM","Notes"]
        tbl = self._make_table(cols)
        hdr = tbl.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(13, QHeaderView.ResizeMode.Stretch)

        files = sorted(WORKOUTS_DIR.glob("workout_*.json"), reverse=True) if WORKOUTS_DIR.exists() else []
        for f in files[:80]:
            try: s = json.loads(f.read_text()).get("summary", {})
            except Exception: continue
            row = tbl.rowCount(); tbl.insertRow(row)
            left = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter

            def cell(v, color=COL_WHITE, align=Qt.AlignmentFlag.AlignCenter):
                it = QTableWidgetItem(str(v)); it.setTextAlignment(align)
                it.setForeground(QColor(color)); return it

            dur = s.get("duration_seconds")
            dist = s.get("distance_m")
            dur_str = (f"{int(dur)//3600:02d}:{(int(dur)%3600)//60:02d}:{int(dur)%60:02d}" if dur else "--")
            tbl.setItem(row,  0, cell(s.get("date","")[:19].replace("T"," "), COL_MUTED))
            tbl.setItem(row,  1, cell(s.get("rider","") or "--"))
            tbl.setItem(row,  2, cell(s.get("label","") or "--", align=left))
            tbl.setItem(row,  3, cell(s.get("type","--")))
            tbl.setItem(row,  4, cell(dur_str))
            tbl.setItem(row,  5, cell(f"{dist/1000:.2f} km" if dist else "--"))
            tbl.setItem(row,  6, cell(f"{s['total_calories']:.0f}" if s.get("total_calories") else "--", COL_CALS))
            tbl.setItem(row,  7, cell(f"{s['avg_power_w']}" if s.get("avg_power_w") else "--", COL_POWER))
            tbl.setItem(row,  8, cell(f"{s['max_power_w']}" if s.get("max_power_w") else "--", COL_POWER))
            tbl.setItem(row,  9, cell(f"{s['avg_wkg']}"     if s.get("avg_wkg")     else "--", COL_WKG))
            tbl.setItem(row, 10, cell(f"{s['avg_hr']}"      if s.get("avg_hr")      else "--", COL_HR))
            tbl.setItem(row, 11, cell(f"{s['max_hr']}"      if s.get("max_hr")      else "--", COL_HR))
            tbl.setItem(row, 12, cell(f"{s['avg_cadence_rpm']}" if s.get("avg_cadence_rpm") else "--", COL_CADENCE))
            tbl.setItem(row, 13, cell(s.get("notes","") or "", align=left))

        lay.addWidget(tbl)
        if not files: lay.addWidget(QLabel("No workouts saved yet."))
        return w

    def _build_pb_tab(self) -> QWidget:
        w = QWidget(); lay = QVBoxLayout(w)
        pbs = load_pbs()
        if not pbs:
            lay.addWidget(QLabel("No personal bests recorded yet.")); lay.addStretch(); return w

        cols = ["Rider","Workouts","Total Time","Total km","Total kcal",
                "Best Avg W","Max W","Best W/kg","Max HR"]
        tbl = self._make_table(cols)
        tbl.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)

        def fmt_t(s): h=int(s)//3600; m=(int(s)%3600)//60; return f"{h}h {m:02d}m"
        def cell(v, color=COL_WHITE):
            it = QTableWidgetItem(str(v)); it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            it.setForeground(QColor(color)); return it

        for rider, rpb in pbs.items():
            r = tbl.rowCount(); tbl.insertRow(r)
            tbl.setItem(r,0, cell(rider, COL_CADENCE))
            tbl.setItem(r,1, cell(rpb.get("workout_count",0)))
            tbl.setItem(r,2, cell(fmt_t(rpb.get("total_time_s",0)), COL_TIME))
            tbl.setItem(r,3, cell(f"{rpb.get('total_distance_km',0):.1f}", COL_DIST))
            tbl.setItem(r,4, cell(f"{rpb.get('total_calories',0):,}", COL_CALS))
            tbl.setItem(r,5, cell(f"{rpb.get('best_avg_power','--')} W", COL_POWER))
            tbl.setItem(r,6, cell(f"{rpb.get('max_power_w','--')} W", COL_POWER))
            tbl.setItem(r,7, cell(f"{rpb.get('best_avg_wkg','--')}", COL_WKG))
            tbl.setItem(r,8, cell(f"{rpb.get('max_hr','--')}", COL_HR))

        lay.addWidget(tbl)
        return w

# ─── Main window ──────────────────────────────────────────────────────────────

class EchoBikeWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Echo Bike Tracker")
        self.resize(1240, 880)
        self.setMinimumSize(960, 720)

        self._metrics:      dict  = {}
        self._session_data: list  = []
        self._start_time:   datetime | None = None
        self._weight_kg:    float = 75.0
        self._device_name:  str   = ""
        self._scan_results: list  = []

        self._rider_name:    str   = ""
        self._workout_label: str   = ""
        self._workout_type:  str   = "Free Ride"
        self._workout_notes: str   = ""
        self._target_type:   str   = "None"
        self._target_value:  float = 0.0

        self._workout_plan:   list[Segment] = []
        self._plan_idx:       int           = -1
        self._segment_start:  datetime | None = None
        self._plan_timer = QTimer(self, timeout=self._tick_plan, interval=500)

        self._profiles       = load_profiles()
        self._current_profile: dict = dict(DEFAULT_PROFILES["Guest"])

        self._session_stats: dict[str, dict] = {}
        self._power_zone_counts = [0] * len(POWER_ZONES)
        self._hr_zone_counts    = [0] * len(HR_ZONES)
        self._reset_session_stats()

        self._t_buf       = deque(maxlen=MAX_CHART_POINTS)
        self._power_buf   = deque(maxlen=MAX_CHART_POINTS)
        self._hr_buf      = deque(maxlen=MAX_CHART_POINTS)
        self._cadence_buf = deque(maxlen=MAX_CHART_POINTS)
        self._wkg_buf     = deque(maxlen=MAX_CHART_POINTS)
        self._speed_buf   = deque(maxlen=MAX_CHART_POINTS)

        self._apply_palette()
        self._build_ui()
        self._setup_ble()

        QTimer(self, timeout=self._refresh_cards, interval=250).start()
        QTimer(self, timeout=self._push_chart,    interval=1000).start()

    def _reset_session_stats(self):
        for key in ("power_w", "heart_rate", "cadence_rpm", "speed_kmh"):
            self._session_stats[key] = {"min": None, "max": None, "sum": 0.0, "n": 0}
        self._power_zone_counts = [0] * len(POWER_ZONES)
        self._hr_zone_counts    = [0] * len(HR_ZONES)

    # ── Palette ───────────────────────────────────────────────────────────────

    def _apply_palette(self):
        QApplication.instance().setStyle("Fusion")
        pal = QPalette(); cr = QPalette.ColorRole
        pal.setColor(cr.Window,        QColor(BG))
        pal.setColor(cr.WindowText,    QColor(COL_WHITE))
        pal.setColor(cr.Base,          QColor(CARD_BG))
        pal.setColor(cr.AlternateBase, QColor("#18182e"))
        pal.setColor(cr.Text,          QColor(COL_WHITE))
        pal.setColor(cr.Button,        QColor(CARD_BG))
        pal.setColor(cr.ButtonText,    QColor(COL_WHITE))
        pal.setColor(cr.Highlight,     QColor(COL_POWER))
        pal.setColor(cr.HighlightedText, QColor("#000000"))
        QApplication.instance().setPalette(pal)

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QWidget(); self.setCentralWidget(root)
        lay = QVBoxLayout(root); lay.setContentsMargins(18,14,18,14); lay.setSpacing(10)
        lay.addLayout(self._build_header())
        lay.addWidget(self._build_workout_setup())
        lay.addWidget(self._build_plan_bar())
        lay.addLayout(self._build_cards(), stretch=2)
        lay.addWidget(self._build_chart_panel(), stretch=3)
        lay.addLayout(self._build_controls())
        sb = QStatusBar(); sb.setStyleSheet(f"color:{COL_MUTED};font-size:11px;background:{BG};")
        self.setStatusBar(sb); self._status = sb
        self._status.showMessage("Not connected  ·  Press  ⟳ Scan  to find your Echo Bike")

    # ── Header ────────────────────────────────────────────────────────────────

    def _build_header(self) -> QHBoxLayout:
        row = QHBoxLayout(); row.setSpacing(14)
        title = QLabel("🚴  ECHO BIKE")
        title.setStyleSheet(f"color:{COL_POWER};font-size:22px;font-weight:900;letter-spacing:3px;")
        row.addWidget(title); row.addStretch()
        self._dot = QLabel("●"); self._dot.setStyleSheet("color:#333355;font-size:20px;")
        row.addWidget(self._dot)
        self._conn_lbl = QLabel("Disconnected")
        self._conn_lbl.setStyleSheet(f"color:{COL_MUTED};font-size:13px;")
        row.addWidget(self._conn_lbl); row.addSpacing(24)
        self._timer_lbl = QLabel("00:00:00")
        self._timer_lbl.setStyleSheet(
            f"color:{COL_WHITE};font-size:24px;font-weight:800;font-family:monospace;"
        )
        row.addWidget(self._timer_lbl); row.addSpacing(24)
        w_lbl = QLabel("Weight:")
        w_lbl.setStyleSheet(f"color:{COL_MUTED};font-size:12px;"); row.addWidget(w_lbl)
        self._weight_spin = QDoubleSpinBox()
        self._weight_spin.setRange(30.0, 250.0); self._weight_spin.setValue(self._weight_kg)
        self._weight_spin.setSuffix(" kg"); self._weight_spin.setDecimals(1)
        self._weight_spin.setFixedWidth(95)
        self._weight_spin.setStyleSheet(f"""
            QDoubleSpinBox {{background:{CARD_BG};color:{COL_WHITE};
                border:1px solid {CARD_BORDER};border-radius:7px;padding:4px 8px;font-size:13px;}}
        """)
        self._weight_spin.valueChanged.connect(lambda v: setattr(self, "_weight_kg", v))
        row.addWidget(self._weight_spin)
        return row

    # ── Workout setup bar ─────────────────────────────────────────────────────

    def _build_workout_setup(self) -> QFrame:
        frame = QFrame()
        frame.setStyleSheet(
            f"QFrame{{background:{CARD_BG};border:1px solid {CARD_BORDER};border-radius:10px;}}"
        )
        row = QHBoxLayout(frame); row.setContentsMargins(14,8,14,8); row.setSpacing(14)

        def mlbl(t):
            l = QLabel(t)
            l.setStyleSheet(f"color:{COL_MUTED};font-size:10px;font-weight:700;"
                            f"letter-spacing:1px;border:none;background:transparent;")
            return l

        # Rider dropdown
        row.addWidget(mlbl("RIDER"))
        self._name_combo = QComboBox()
        self._name_combo.addItems(RIDER_NAMES)
        self._name_combo.setFixedWidth(110)
        self._name_combo.setStyleSheet(_field_ss())
        self._name_combo.currentTextChanged.connect(self._on_rider_changed)
        row.addWidget(self._name_combo)

        edit_profile_btn = QPushButton("⚙")
        edit_profile_btn.setToolTip("Edit rider profile")
        edit_profile_btn.setFixedSize(32, 32)
        edit_profile_btn.setStyleSheet(
            f"QPushButton{{background:{CARD_BG};color:{COL_MUTED};"
            f"border:1px solid {CARD_BORDER};border-radius:6px;font-size:14px;}}"
            f"QPushButton:hover{{color:{COL_WHITE};}}"
        )
        edit_profile_btn.clicked.connect(self._on_edit_profile)
        row.addWidget(edit_profile_btn)

        # Label
        row.addWidget(mlbl("LABEL"))
        self._lbl_input = QLineEdit(); self._lbl_input.setPlaceholderText("e.g. Morning HIIT")
        self._lbl_input.setFixedWidth(150); self._lbl_input.setStyleSheet(_field_ss())
        self._lbl_input.textChanged.connect(lambda t: setattr(self, "_workout_label", t))
        row.addWidget(self._lbl_input)

        # Type
        row.addWidget(mlbl("TYPE"))
        self._type_combo = QComboBox()
        self._type_combo.addItems(["Free Ride","HIIT","Steady State","Intervals",
                                   "Threshold","Recovery","Race","Test","Custom"])
        self._type_combo.setFixedWidth(120); self._type_combo.setStyleSheet(_field_ss())
        self._type_combo.currentTextChanged.connect(lambda t: setattr(self, "_workout_type", t))
        row.addWidget(self._type_combo)

        # Notes
        row.addWidget(mlbl("NOTES"))
        self._notes_input = QLineEdit(); self._notes_input.setPlaceholderText("Optional notes…")
        self._notes_input.setStyleSheet(_field_ss())
        self._notes_input.textChanged.connect(lambda t: setattr(self, "_workout_notes", t))
        row.addWidget(self._notes_input, stretch=1)

        # Target
        row.addWidget(mlbl("TARGET"))
        self._target_combo = QComboBox()
        self._target_combo.addItems(["None","Time (min)","Distance (km)","Calories","Power (W)"])
        self._target_combo.setFixedWidth(120); self._target_combo.setStyleSheet(_field_ss())
        self._target_combo.currentTextChanged.connect(self._on_target_type_changed)
        row.addWidget(self._target_combo)
        self._target_spin = QSpinBox(); self._target_spin.setRange(0, 9999)
        self._target_spin.setFixedWidth(75); self._target_spin.setEnabled(False)
        self._target_spin.setStyleSheet(_field_ss())
        self._target_spin.valueChanged.connect(lambda v: setattr(self, "_target_value", float(v)))
        row.addWidget(self._target_spin)

        # Trigger initial profile load
        self._on_rider_changed(self._name_combo.currentText())
        return frame

    def _on_rider_changed(self, name: str):
        self._rider_name     = name
        self._current_profile = dict(self._profiles.get(name, DEFAULT_PROFILES.get(name, DEFAULT_PROFILES["Guest"])))
        self._weight_kg      = self._current_profile.get("weight_kg", 75.0)
        self._weight_spin.blockSignals(True)
        self._weight_spin.setValue(self._weight_kg)
        self._weight_spin.blockSignals(False)

    def _on_edit_profile(self):
        name = self._name_combo.currentText()
        dlg  = ProfileDialog(name, self._current_profile, self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg._result:
            self._current_profile.update(dlg._result)
            self._profiles[name] = dict(self._current_profile)
            save_profiles(self._profiles)
            self._weight_kg = self._current_profile["weight_kg"]
            self._weight_spin.setValue(self._weight_kg)
            self._status.showMessage(f"Profile saved for {name}")

    def _on_target_type_changed(self, text: str):
        self._target_type = text
        self._target_spin.setEnabled(text != "None")
        hints = {"Time (min)": (1,300,20), "Distance (km)": (1,200,5),
                 "Calories": (50,5000,500), "Power (W)": (50,1000,200)}
        if text in hints:
            lo, hi, d = hints[text]; self._target_spin.setRange(lo, hi); self._target_spin.setValue(d)

    # ── Segment progress bar ──────────────────────────────────────────────────

    def _build_plan_bar(self) -> QFrame:
        self._plan_bar_frame = QFrame(); self._plan_bar_frame.setVisible(False)
        self._plan_bar_frame.setStyleSheet(
            f"QFrame{{background:{CARD_BG};border:1px solid {CARD_BORDER};border-radius:10px;}}"
        )
        lay = QVBoxLayout(self._plan_bar_frame); lay.setContentsMargins(14,8,14,8); lay.setSpacing(4)
        top = QHBoxLayout()
        self._seg_name_lbl = QLabel("WARMUP")
        self._seg_name_lbl.setStyleSheet(
            f"color:{COL_POWER};font-size:16px;font-weight:900;"
            f"letter-spacing:2px;border:none;background:transparent;"
        )
        top.addWidget(self._seg_name_lbl)
        self._seg_idx_lbl = QLabel("Segment 1 / 1")
        self._seg_idx_lbl.setStyleSheet(f"color:{COL_MUTED};font-size:12px;border:none;background:transparent;")
        top.addWidget(self._seg_idx_lbl); top.addStretch()
        self._seg_countdown_lbl = QLabel("00:00")
        self._seg_countdown_lbl.setStyleSheet(
            f"color:{COL_WHITE};font-size:22px;font-weight:800;"
            f"font-family:monospace;border:none;background:transparent;"
        )
        top.addWidget(self._seg_countdown_lbl); top.addSpacing(20)
        self._seg_target_lbl = QLabel("Target: Open")
        self._seg_target_lbl.setStyleSheet(f"color:{COL_MUTED};font-size:12px;border:none;background:transparent;")
        top.addWidget(self._seg_target_lbl); top.addSpacing(20)
        self._seg_next_lbl = QLabel("")
        self._seg_next_lbl.setStyleSheet(f"color:{COL_MUTED};font-size:11px;border:none;background:transparent;")
        top.addWidget(self._seg_next_lbl); top.addStretch()
        stop_btn = QPushButton("■  Stop Plan"); stop_btn.setStyleSheet(_btn_style(COL_HR, 110))
        stop_btn.clicked.connect(self._stop_plan); top.addWidget(stop_btn)
        lay.addLayout(top)
        self._seg_progress = QProgressBar(); self._seg_progress.setRange(0,1000)
        self._seg_progress.setValue(0); self._seg_progress.setTextVisible(False)
        self._seg_progress.setFixedHeight(6)
        self._seg_progress.setStyleSheet(f"""
            QProgressBar {{background:{CARD_BORDER};border-radius:3px;border:none;}}
            QProgressBar::chunk {{background:qlineargradient(x1:0,y1:0,x2:1,y2:0,
                stop:0 {COL_CADENCE},stop:1 {COL_POWER});border-radius:3px;}}
        """)
        lay.addWidget(self._seg_progress)
        return self._plan_bar_frame

    # ── Metric cards ──────────────────────────────────────────────────────────

    def _build_cards(self) -> QGridLayout:
        grid = QGridLayout(); grid.setSpacing(10)
        self._c_power   = MetricCard("POWER",      "watts", COL_POWER,   big=True, show_zone=True, show_stats=True)
        self._c_wkg     = MetricCard("W / KG",     "w/kg",  COL_WKG,     big=True, show_stats=True)
        self._c_cadence = MetricCard("CADENCE",    "rpm",   COL_CADENCE, big=True, show_stats=True)
        self._c_speed   = MetricCard("SPEED",      "km/h",  COL_SPEED,   big=True)
        self._c_hr      = MetricCard("HEART RATE", "bpm",   COL_HR,      show_zone=True, show_stats=True)
        self._c_cals    = MetricCard("CALORIES",   "kcal",  COL_CALS)
        self._c_dist    = MetricCard("DISTANCE",   "km",    COL_DIST)
        self._c_time    = MetricCard("ELAPSED",    "",      COL_TIME)
        for col, card in enumerate([self._c_power, self._c_wkg, self._c_cadence, self._c_speed]):
            grid.addWidget(card, 0, col)
        for col, card in enumerate([self._c_hr, self._c_cals, self._c_dist, self._c_time]):
            grid.addWidget(card, 1, col)
        return grid

    # ── Chart panel ───────────────────────────────────────────────────────────

    def _build_chart_panel(self) -> QFrame:
        frame = QFrame()
        frame.setStyleSheet(f"QFrame{{background:{CARD_BG};border:1px solid {CARD_BORDER};border-radius:14px;}}")
        frame.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        lay = QVBoxLayout(frame); lay.setContentsMargins(14,10,14,10); lay.setSpacing(8)
        tog = QHBoxLayout(); tog.setSpacing(18)
        hdr = QLabel("LIVE CHART")
        hdr.setStyleSheet(f"color:{COL_MUTED};font-size:10px;font-weight:700;letter-spacing:2px;border:none;background:transparent;")
        tog.addWidget(hdr)
        self._series_cbs: dict[str, QCheckBox] = {}
        for name, color in [("Power (W)", COL_POWER),("HR (bpm)", COL_HR),
                             ("Cadence", COL_CADENCE),("W/kg", COL_WKG),("Speed km/h", COL_SPEED)]:
            cb = QCheckBox(name); cb.setChecked(name in ("Power (W)","HR (bpm)","Cadence"))
            cb.setStyleSheet(f"color:{color};font-size:12px;font-weight:700;border:none;background:transparent;")
            cb.stateChanged.connect(self._redraw_chart); tog.addWidget(cb); self._series_cbs[name] = cb
        tog.addStretch(); tog.addWidget(QLabel("Window:"))
        self._win_combo = QComboBox(); self._win_combo.addItems(["1 min","5 min","10 min","30 min","All"])
        self._win_combo.setCurrentIndex(1); self._win_combo.setFixedWidth(90)
        self._win_combo.setStyleSheet(f"""
            QComboBox {{background:{CARD_BG};color:{COL_WHITE};border:1px solid {CARD_BORDER};
                border-radius:6px;padding:3px 8px;font-size:11px;}}
            QComboBox::drop-down {{border:none;width:14px;}}
            QComboBox QAbstractItemView {{background:#1a1a38;color:{COL_WHITE};selection-background-color:{COL_POWER};}}
        """)
        self._win_combo.currentIndexChanged.connect(self._redraw_chart); tog.addWidget(self._win_combo)
        lay.addLayout(tog)
        pg.setConfigOptions(background=CARD_BG, foreground=COL_MUTED, antialias=True)
        self._plot = pg.PlotWidget(); self._plot.setStyleSheet("border:none;background:transparent;")
        self._plot.showGrid(x=True, y=True, alpha=0.12)
        self._plot.setLabel("bottom","Elapsed",units="s",color=COL_MUTED,size="10pt")
        self._plot.setLabel("left","Value",color=COL_MUTED,size="10pt")
        self._plot.getPlotItem().getAxis("bottom").setTextPen(pg.mkPen(color=COL_MUTED))
        self._plot.getPlotItem().getAxis("left").setTextPen(pg.mkPen(color=COL_MUTED))
        self._plot.addLegend(offset=(10,10), labelTextColor=COL_WHITE,
                             pen=pg.mkPen(color=CARD_BORDER), brush=pg.mkBrush(color="#0d0d1aCC"))
        self._curves: dict[str, pg.PlotDataItem] = {}
        for name, color, w in [("Power (W)",COL_POWER,2.5),("HR (bpm)",COL_HR,2.0),
                                ("Cadence",COL_CADENCE,2.0),("W/kg",COL_WKG,2.0),("Speed km/h",COL_SPEED,2.0)]:
            self._curves[name] = self._plot.plot(pen=pg.mkPen(color=color, width=w), name=name)
        lay.addWidget(self._plot)
        return frame

    # ── Controls ──────────────────────────────────────────────────────────────

    def _build_controls(self) -> QHBoxLayout:
        row = QHBoxLayout(); row.setSpacing(10)

        def btn(text, color, slot):
            b = QPushButton(text); b.setStyleSheet(_btn_style(color)); b.clicked.connect(slot); return b

        self._btn_scan  = btn("⟳  Scan",          COL_CADENCE, self._on_scan)
        self._btn_conn  = btn("⚡  Connect",        COL_POWER,   self._on_connect)
        self._btn_disc  = btn("✕  Disconnect",     COL_HR,      self._on_disconnect)
        self._btn_save  = btn("💾  Save",           COL_CALS,    self._on_save)
        self._btn_hist  = btn("📋  History",        COL_DIST,    self._on_history)
        self._btn_build = btn("🏋  Build Workout",  COL_WKG,     self._on_build_workout)
        self._btn_start = btn("▶  Start Workout",  COL_SPEED,   self._start_plan)

        self._btn_conn.setEnabled(False); self._btn_disc.setEnabled(False)
        self._btn_save.setEnabled(False); self._btn_start.setEnabled(False)
        for b in [self._btn_scan, self._btn_conn, self._btn_disc, self._btn_save,
                  self._btn_hist, self._btn_build, self._btn_start]:
            row.addWidget(b)
        row.addStretch()
        return row

    # ── BLE ───────────────────────────────────────────────────────────────────

    def _setup_ble(self):
        self._ble = BleWorker()
        self._ble.data_received.connect(self._on_data)
        self._ble.connected.connect(self._on_connected)
        self._ble.disconnected.connect(self._on_disconnected)
        self._ble.error.connect(self._on_ble_error)
        self._ble.scan_result.connect(self._on_scan_result)
        self._ble.control_sent.connect(lambda msg: self._status.showMessage(f"FTMS → {msg}"))
        self._ble.start()

    def _on_scan(self):
        self._btn_scan.setEnabled(False); self._btn_scan.setText("Scanning…")
        self._status.showMessage("Scanning… (hold BT button on console 3 sec)")
        self._ble.scan()

    def _on_scan_result(self, devices: list):
        self._btn_scan.setEnabled(True); self._btn_scan.setText("⟳  Scan")
        if not devices:
            self._status.showMessage("No Echo Bike found. Is the console on?"); return
        if len(devices) == 1:
            self._device_name = devices[0][0]; self._scan_results = devices
            self._status.showMessage(f"Found:  {devices[0][0]}  —  press Connect")
            self._btn_conn.setEnabled(True)
        else:
            dlg = DevicePickerDialog(devices, self)
            if dlg.exec() == QDialog.DialogCode.Accepted and dlg.selected:
                self._device_name, _ = dlg.selected; self._scan_results = devices
                self._btn_conn.setEnabled(True)

    def _on_connect(self):
        addr = next((a for n,a in self._scan_results if n == self._device_name),
                    self._scan_results[0][1] if self._scan_results else None)
        if not addr: return
        self._btn_conn.setEnabled(False); self._btn_scan.setEnabled(False)
        self._status.showMessage(f"Connecting to  {self._device_name}…")
        self._ble.connect_to(addr)

    def _on_connected(self, address: str):
        self._dot.setStyleSheet(f"color:{COL_SPEED};font-size:20px;")
        self._conn_lbl.setText(self._device_name or address)
        self._conn_lbl.setStyleSheet(f"color:{COL_SPEED};font-size:13px;font-weight:700;")
        self._btn_disc.setEnabled(True); self._btn_save.setEnabled(True)
        self._btn_scan.setEnabled(False); self._btn_start.setEnabled(bool(self._workout_plan))
        self._start_time = datetime.now(); self._metrics.clear(); self._session_data.clear()
        self._reset_session_stats()
        for buf in (self._t_buf, self._power_buf, self._hr_buf,
                    self._cadence_buf, self._wkg_buf, self._speed_buf): buf.clear()
        self._status.showMessage(f"Connected  ·  {self._device_name or address}")

    def _on_disconnected(self):
        self._dot.setStyleSheet("color:#333355;font-size:20px;")
        self._conn_lbl.setText("Disconnected")
        self._conn_lbl.setStyleSheet(f"color:{COL_MUTED};font-size:13px;")
        self._btn_disc.setEnabled(False); self._btn_scan.setEnabled(True)
        self._btn_start.setEnabled(False); self._stop_plan()
        self._status.showMessage("Disconnected")

    def _on_ble_error(self, msg: str):
        self._btn_scan.setEnabled(True); self._btn_conn.setEnabled(bool(self._scan_results))
        self._status.showMessage(f"BLE error: {msg}")

    def _on_data(self, data: dict):
        self._metrics.update(data)
        self._session_data.append(data)
        for key, st in self._session_stats.items():
            v = data.get(key)
            if v is not None and v > 0:
                if st["min"] is None or v < st["min"]: st["min"] = v
                if st["max"] is None or v > st["max"]: st["max"] = v
                st["sum"] += v; st["n"] += 1

    # ── Card refresh (4 Hz) ───────────────────────────────────────────────────

    def _refresh_cards(self):
        if self._start_time:
            e = int((datetime.now() - self._start_time).total_seconds())
            self._timer_lbl.setText(f"{e//3600:02d}:{(e%3600)//60:02d}:{e%60:02d}")

        m = self._metrics
        if not m: return

        power = m.get("power_w")
        self._c_power.set_value(power if power is not None else "--")
        wkg = round(power / self._weight_kg, 2) if power is not None and self._weight_kg else None
        self._c_wkg.set_value(wkg if wkg is not None else "--", decimals=2 if wkg is not None else 0)
        cadence = m.get("cadence_rpm")
        self._c_cadence.set_value(cadence if cadence is not None else "--")
        speed = m.get("speed_kmh")
        self._c_speed.set_value(speed if speed is not None else "--",
                                decimals=1 if speed is not None else 0)
        hr = m.get("heart_rate")
        self._c_hr.set_value(hr if hr is not None else "--")
        self._c_cals.set_value(m.get("calories", "--"))
        dist = m.get("distance_m")
        self._c_dist.set_value(dist/1000 if dist is not None else "--",
                                decimals=2 if dist is not None else 0)
        et = m.get("elapsed_seconds") or (
            int((datetime.now()-self._start_time).total_seconds()) if self._start_time else None
        )
        if et is not None:
            self._c_time.set_value(f"{int(et)//3600:02d}:{(int(et)%3600)//60:02d}:{int(et)%60:02d}")

        # Zone indicators
        ftp      = self._current_profile.get("ftp_w", 200)
        rest_hr  = self._current_profile.get("resting_hr", 60)
        max_hr   = _profile_max_hr(self._current_profile)
        if power is not None and power > 0:
            _, zn, zc = get_power_zone(int(power), ftp)
            self._c_power.set_zone(zn, zc)
        if hr is not None and hr > 0:
            _, zn, zc = get_hr_zone(int(hr), rest_hr, max_hr)
            self._c_hr.set_zone(zn, zc)

        # Min/mean/max stats
        def _stats(key):
            st = self._session_stats[key]
            avg = round(st["sum"]/st["n"], 1) if st["n"] else None
            return st["min"], avg, st["max"]

        p_mn, p_av, p_mx = _stats("power_w")
        self._c_power.set_stats(p_mn, p_av, p_mx)

        w_mn = round(p_mn / self._weight_kg, 2) if p_mn else None
        w_av = round(p_av / self._weight_kg, 2) if p_av else None
        w_mx = round(p_mx / self._weight_kg, 2) if p_mx else None
        self._c_wkg.set_stats(w_mn, w_av, w_mx, fmt="{:.2f}")

        h_mn, h_av, h_mx = _stats("heart_rate")
        self._c_hr.set_stats(h_mn, h_av, h_mx)

        c_mn, c_av, c_mx = _stats("cadence_rpm")
        self._c_cadence.set_stats(c_mn, c_av, c_mx)

    # ── Chart (1 Hz) ──────────────────────────────────────────────────────────

    def _push_chart(self):
        if not self._start_time or not self._metrics: return
        elapsed = (datetime.now() - self._start_time).total_seconds()
        m = self._metrics
        self._t_buf.append(elapsed)
        self._power_buf.append(m.get("power_w"))
        self._hr_buf.append(m.get("heart_rate"))
        self._cadence_buf.append(m.get("cadence_rpm"))
        pw = m.get("power_w")
        self._wkg_buf.append(pw/self._weight_kg if pw is not None and self._weight_kg else None)
        self._speed_buf.append(m.get("speed_kmh"))
        self._redraw_chart()

        # Zone time tracking (1 sample/sec)
        ftp = self._current_profile.get("ftp_w", 200)
        rest_hr = self._current_profile.get("resting_hr", 60)
        mhr = _profile_max_hr(self._current_profile)
        if pw and pw > 0:
            idx, _, _ = get_power_zone(int(pw), ftp)
            self._power_zone_counts[idx] += 1
        hr = m.get("heart_rate")
        if hr and hr > 0:
            idx, _, _ = get_hr_zone(int(hr), rest_hr, mhr)
            self._hr_zone_counts[idx] += 1

    def _redraw_chart(self):
        if not self._t_buf: return
        t_all = list(self._t_buf)
        win_map = {"1 min":60,"5 min":300,"10 min":600,"30 min":1800,"All":None}
        win = win_map.get(self._win_combo.currentText())

        def windowed(buf):
            vals = list(buf)
            if win is None or not t_all: return t_all, vals
            cutoff = t_all[-1] - win
            idx = next((i for i,t in enumerate(t_all) if t >= cutoff), 0)
            return t_all[idx:], vals[idx:]

        buf_map = {"Power (W)": self._power_buf,"HR (bpm)": self._hr_buf,
                   "Cadence": self._cadence_buf,"W/kg": self._wkg_buf,"Speed km/h": self._speed_buf}
        for name, curve in self._curves.items():
            if not self._series_cbs[name].isChecked(): curve.setData([],[]); continue
            t_w, v_w = windowed(buf_map[name])
            pairs = [(t,v) for t,v in zip(t_w, v_w) if v is not None]
            if pairs: ts, vs = zip(*pairs); curve.setData(list(ts), list(vs))
            else: curve.setData([],[])

    # ── Workout plan ──────────────────────────────────────────────────────────

    def _on_build_workout(self):
        dlg = WorkoutBuilderDialog(self._workout_plan, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._workout_plan = dlg.result_plan
            total_s = sum(s.duration_s for s in self._workout_plan)
            h, rem  = divmod(total_s, 3600); m, s = divmod(rem, 60)
            t = f"{h}h {m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
            self._status.showMessage(f"Workout loaded: {len(self._workout_plan)} segments  ·  {t}")
            self._btn_start.setEnabled(bool(self._workout_plan) and self._btn_disc.isEnabled())

    def _start_plan(self):
        if not self._workout_plan: return
        self._plan_bar_frame.setVisible(True); self._plan_timer.start(); self._enter_segment(0)

    def _enter_segment(self, idx: int):
        if idx >= len(self._workout_plan): self._finish_plan(); return
        self._plan_idx = idx; self._segment_start = datetime.now()
        seg = self._workout_plan[idx]
        self._ble.request_control()
        if   seg.target_type == "Power":   self._ble.send_target_power(seg.target_value)
        elif seg.target_type == "HR":      self._ble.send_target_hr(seg.target_value)
        elif seg.target_type == "Cadence": self._ble.send_target_cadence(seg.target_value)
        self._ble.ftms_start()
        self._update_plan_display(0.0)

    def _tick_plan(self):
        if self._plan_idx < 0 or not self._segment_start: return
        elapsed = (datetime.now() - self._segment_start).total_seconds()
        seg = self._workout_plan[self._plan_idx]
        if elapsed >= seg.duration_s: self._enter_segment(self._plan_idx + 1)
        else: self._update_plan_display(elapsed)

    def _update_plan_display(self, seg_elapsed: float):
        idx = self._plan_idx
        if idx < 0 or idx >= len(self._workout_plan): return
        seg = self._workout_plan[idx]; remaining = max(0.0, seg.duration_s - seg_elapsed)
        m, s = divmod(int(remaining), 60)
        self._seg_name_lbl.setText(seg.name.upper())
        self._seg_name_lbl.setStyleSheet(
            f"color:{seg.color};font-size:16px;font-weight:900;"
            f"letter-spacing:2px;border:none;background:transparent;"
        )
        self._seg_idx_lbl.setText(f"Segment {idx+1} / {len(self._workout_plan)}")
        self._seg_countdown_lbl.setText(f"{m:02d}:{s:02d}")
        self._seg_target_lbl.setText(f"Target: {seg.target_str}")
        if idx+1 < len(self._workout_plan):
            nxt = self._workout_plan[idx+1]
            self._seg_next_lbl.setText(f"Next → {nxt.name}  {nxt.duration_str}  {nxt.target_str}")
        else: self._seg_next_lbl.setText("Last segment")
        total_s = sum(sg.duration_s for sg in self._workout_plan)
        done_s  = sum(self._workout_plan[i].duration_s for i in range(idx)) + seg_elapsed
        self._seg_progress.setValue(int(done_s/total_s*1000) if total_s else 0)

    def _stop_plan(self):
        self._plan_timer.stop(); self._plan_idx = -1; self._segment_start = None
        self._plan_bar_frame.setVisible(False); self._ble.ftms_stop()

    def _finish_plan(self):
        self._plan_timer.stop(); self._plan_idx = -1; self._segment_start = None
        self._plan_bar_frame.setVisible(False); self._ble.ftms_stop()
        self._status.showMessage("✓  Workout complete!")
        QMessageBox.information(self, "Workout Complete",
                                "All segments finished!\n\nPress 💾 Save to record this session.")

    # ── Save ──────────────────────────────────────────────────────────────────

    def _on_disconnect(self): self._ble.disconnect()

    def _on_save(self):
        result = self._save_workout()
        if result:
            summary, _, _ = result
            rider = summary.get("rider", "")
            new_pbs = update_pbs(rider, summary)
            totals  = load_pbs().get(rider, {})
            dlg = PostWorkoutSummaryDialog(
                summary, new_pbs, totals, self._current_profile,
                list(self._power_zone_counts), list(self._hr_zone_counts), self
            )
            dlg.exec()

    def _save_workout(self) -> tuple[dict, Path, Path] | None:
        if not self._session_data: return None
        WORKOUTS_DIR.mkdir(parents=True, exist_ok=True)
        ts_str = (self._start_time or datetime.now()).strftime("%Y%m%d_%H%M%S")

        powers   = [d["power_w"]     for d in self._session_data if "power_w"     in d and d["power_w"]     > 0]
        cadences = [d["cadence_rpm"] for d in self._session_data if "cadence_rpm" in d and d["cadence_rpm"] > 0]
        hrs      = [d["heart_rate"]  for d in self._session_data if "heart_rate"  in d and d["heart_rate"]  > 0]
        last     = self._session_data[-1]
        dur      = last.get("elapsed_seconds") or (
            (datetime.now()-self._start_time).total_seconds() if self._start_time else None
        )

        summary = {
            "date":             (self._start_time or datetime.now()).isoformat(),
            "rider":            self._rider_name or "",
            "label":            self._workout_label or "",
            "type":             self._workout_type,
            "notes":            self._workout_notes or "",
            "target_type":      self._target_type,
            "target_value":     self._target_value if self._target_type != "None" else None,
            "workout_plan":     [{"name":s.name,"duration_s":s.duration_s,
                                  "target_type":s.target_type,"target_value":s.target_value}
                                 for s in self._workout_plan],
            "device":           self._device_name,
            "weight_kg":        self._weight_kg,
            "ftp_w":            self._current_profile.get("ftp_w", 0),
            "duration_seconds": dur,
            "distance_m":       last.get("distance_m"),
            "total_calories":   last.get("calories"),
            "avg_power_w":      round(sum(powers)/len(powers), 1)   if powers   else None,
            "max_power_w":      max(powers)                          if powers   else None,
            "avg_wkg":          round(sum(powers)/len(powers)/self._weight_kg, 2)
                                    if powers and self._weight_kg else None,
            "avg_hr":           round(sum(hrs)/len(hrs), 1)         if hrs      else None,
            "max_hr":           max(hrs)                             if hrs      else None,
            "avg_cadence_rpm":  round(sum(cadences)/len(cadences),1) if cadences else None,
            "max_cadence_rpm":  max(cadences)                        if cadences else None,
            "samples":          len(self._session_data),
            "power_zone_secs":  list(self._power_zone_counts),
            "hr_zone_secs":     list(self._hr_zone_counts),
        }

        json_path = WORKOUTS_DIR / f"workout_{ts_str}.json"
        json_path.write_text(json.dumps({"summary": summary, "data": self._session_data}, indent=2))
        csv_path, history_path = self._save_csv(ts_str, summary)
        self._status.showMessage(f"Saved  →  {csv_path.name}")
        return summary, csv_path, history_path

    def _save_csv(self, ts_str: str, summary: dict) -> tuple[Path, Path]:
        data_path = WORKOUTS_DIR / f"workout_{ts_str}.csv"
        data_cols = ["timestamp","elapsed_s","power_w","cadence_rpm",
                     "speed_kmh","heart_rate","distance_m","calories","wkg"]
        with data_path.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=data_cols, extrasaction="ignore")
            w.writeheader()
            for d in self._session_data:
                pw = d.get("power_w")
                w.writerow({
                    "timestamp":   d.get("ts",""),
                    "elapsed_s":   d.get("elapsed_seconds",""),
                    "power_w":     pw if pw is not None else "",
                    "cadence_rpm": d.get("cadence_rpm",""),
                    "speed_kmh":   d.get("speed_kmh",""),
                    "heart_rate":  d.get("heart_rate",""),
                    "distance_m":  d.get("distance_m",""),
                    "calories":    d.get("calories",""),
                    "wkg":         round(pw/self._weight_kg, 3) if pw and self._weight_kg else "",
                })

        history_path = WORKOUTS_DIR / "workout_history.csv"
        history_cols = ["date","rider","label","type","notes","duration_s","distance_km",
                        "calories","avg_power_w","max_power_w","avg_wkg","avg_hr","max_hr",
                        "avg_cadence_rpm","max_cadence_rpm","weight_kg","ftp_w",
                        "target_type","target_value","device","samples"]
        write_header = not history_path.exists()
        with history_path.open("a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=history_cols)
            if write_header: w.writeheader()
            dist = summary.get("distance_m")
            w.writerow({
                "date":            summary.get("date","")[:19].replace("T"," "),
                "rider":           summary.get("rider",""),
                "label":           summary.get("label",""),
                "type":            summary.get("type",""),
                "notes":           summary.get("notes",""),
                "duration_s":      round(summary["duration_seconds"],1) if summary.get("duration_seconds") else "",
                "distance_km":     round(dist/1000,3) if dist else "",
                "calories":        summary.get("total_calories",""),
                "avg_power_w":     summary.get("avg_power_w",""),
                "max_power_w":     summary.get("max_power_w",""),
                "avg_wkg":         summary.get("avg_wkg",""),
                "avg_hr":          summary.get("avg_hr",""),
                "max_hr":          summary.get("max_hr",""),
                "avg_cadence_rpm": summary.get("avg_cadence_rpm",""),
                "max_cadence_rpm": summary.get("max_cadence_rpm",""),
                "weight_kg":       summary.get("weight_kg",""),
                "ftp_w":           summary.get("ftp_w",""),
                "target_type":     summary.get("target_type",""),
                "target_value":    summary.get("target_value",""),
                "device":          summary.get("device",""),
                "samples":         summary.get("samples",""),
            })
        return data_path, history_path

    def _on_history(self): HistoryDialog(self).exec()

    def closeEvent(self, event):
        if self._session_data: self._save_workout()
        self._stop_plan(); self._ble.disconnect(); event.accept()


# ─── Entry point ──────────────────────────────────────────────────────────────

def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Echo Bike Tracker")
    win = EchoBikeWindow()
    win.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
