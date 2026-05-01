#!/usr/bin/env python3
from __future__ import annotations

# Must happen before any PyQt6 import — tells Qt where to find the cocoa plugin on macOS.
import importlib.util, os as _os
def _fix_qt_plugin_path():
    spec = importlib.util.find_spec("PyQt6")
    if spec and spec.origin:
        p = _os.path.join(_os.path.dirname(spec.origin), "Qt6", "plugins", "platforms")
        if _os.path.isdir(p):
            _os.environ.setdefault("QT_QPA_PLATFORM_PLUGIN_PATH", p)
_fix_qt_plugin_path()

"""
Echo Bike Tracker — macOS GUI
Rogue Echo Bike v3  ·  FTMS Bluetooth

Usage:  python3 gui_tracker.py
Deps:   python3 -m pip install PyQt6 pyqtgraph bleak
"""

import asyncio
import copy
import csv
import json
import struct
import sys
import threading
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
    QStatusBar, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

# ─── FTMS UUIDs ───────────────────────────────────────────────────────────────
FTMS_SERVICE_UUID  = "00001826-0000-1000-8000-00805f9b34fb"
INDOOR_BIKE_DATA   = "00002ad2-0000-1000-8000-00805f9b34fb"
HEART_RATE_UUID    = "00002a37-0000-1000-8000-00805f9b34fb"
FTMS_CONTROL_UUID  = "00002ad9-0000-1000-8000-00805f9b34fb"   # write targets here

WORKOUTS_DIR       = Path.home() / "echo_bike_workouts"
ECHO_BIKE_KEYWORDS = {"rogue", "echo", "bike", "assault"}
MAX_CHART_POINTS   = 1800

# ─── Colour palette ───────────────────────────────────────────────────────────
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
COL_WARN    = "#ffd32a"

# ─── FTMS parser ──────────────────────────────────────────────────────────────

def parse_indoor_bike_data(raw: bytes) -> dict:
    if len(raw) < 2:
        return {}
    result: dict = {}
    off = 0
    flags = struct.unpack_from("<H", raw, off)[0]
    off += 2

    def rd(fmt: str):
        nonlocal off
        n = struct.calcsize(fmt)
        if off + n > len(raw):
            return None
        v = struct.unpack_from(fmt, raw, off)[0]
        off += n
        return v

    if not (flags & 0x0001):
        v = rd("<H");  result["speed_kmh"]       = round(v * 0.01, 2) if v is not None else None
    if flags & 0x0002:
        v = rd("<H");  result["avg_speed_kmh"]   = round(v * 0.01, 2) if v is not None else None
    if flags & 0x0004:
        v = rd("<H");  result["cadence_rpm"]     = round(v * 0.5,  1) if v is not None else None
    if flags & 0x0008:
        v = rd("<H");  result["avg_cadence_rpm"] = round(v * 0.5,  1) if v is not None else None
    if flags & 0x0010:
        if off + 3 <= len(raw):
            result["distance_m"] = struct.unpack_from("<I", raw[off:off+3] + b"\x00")[0]
            off += 3
    if flags & 0x0020:
        v = rd("<h");  result["resistance"]      = v
    if flags & 0x0040:
        v = rd("<h");  result["power_w"]         = v
    if flags & 0x0080:
        v = rd("<h");  result["avg_power_w"]     = v
    if flags & 0x0100:
        t = rd("<H");  h = rd("<H");  m = rd("<B")
        if t is not None: result["calories"]            = t
        if h is not None: result["calories_per_hour"]   = h
        if m is not None: result["calories_per_minute"] = m
    if flags & 0x0200:
        v = rd("<B");  result["heart_rate"]      = v
    if flags & 0x0400:
        rd("<B")
    if flags & 0x0800:
        v = rd("<H");  result["elapsed_seconds"] = v
    if flags & 0x1000:
        v = rd("<H");  result["remaining_seconds"] = v
    return {k: v for k, v in result.items() if v is not None}


def parse_heart_rate(raw: bytes) -> int | None:
    if not raw:
        return None
    return struct.unpack_from("<H", raw, 1)[0] if (raw[0] & 0x01) and len(raw) >= 3 \
           else (raw[1] if len(raw) >= 2 else None)


# ─── Workout segment data model ───────────────────────────────────────────────

@dataclass
class Segment:
    name:         str
    duration_s:   int
    target_type:  str   # "Open" | "Power" | "HR" | "Cadence"
    target_value: int = 0

    @property
    def duration_str(self) -> str:
        m, s = divmod(self.duration_s, 60)
        return f"{m:02d}:{s:02d}"

    @property
    def target_str(self) -> str:
        units = {"Power": "W", "HR": "bpm", "Cadence": "rpm"}
        return "Open" if self.target_type == "Open" \
               else f"{self.target_value} {units.get(self.target_type, '')}"

    @property
    def color(self) -> str:
        return {
            "Warmup": COL_MUTED, "Cooldown": COL_MUTED,
            "Work": COL_WORK,    "Sprint": COL_WORK,
            "Rest": COL_REST,    "Recovery": COL_REST,
        }.get(self.name, COL_WHITE)


SEGMENT_NAMES = ["Warmup", "Work", "Sprint", "Steady",
                 "Rest", "Recovery", "Cooldown", "Custom"]

WORKOUT_TEMPLATES: dict[str, list[Segment]] = {
    "Tabata  (20s work / 10s rest × 8)": [
        Segment("Warmup",   300, "Open"),
        *([Segment("Work",  20, "Power", 250),
           Segment("Rest",  10, "Power",  80)] * 8),
        Segment("Cooldown", 300, "Open"),
    ],
    "30/30 Intervals × 10": [
        Segment("Warmup",   300, "Open"),
        *([Segment("Work",  30, "Power", 200),
           Segment("Rest",  30, "Power",  80)] * 10),
        Segment("Cooldown", 300, "Open"),
    ],
    "40/20 Intervals × 8": [
        Segment("Warmup",   300, "Open"),
        *([Segment("Work",  40, "Power", 220),
           Segment("Rest",  20, "Power",  80)] * 8),
        Segment("Cooldown", 300, "Open"),
    ],
    "Steady State  20 min": [
        Segment("Warmup",  300, "Open"),
        Segment("Steady", 1200, "Power", 150),
        Segment("Cooldown", 300, "Open"),
    ],
    "10-20-30  × 5 rounds": [
        Segment("Warmup",    300, "Open"),
        *([Segment("Easy",    30, "Power", 100),
           Segment("Moderate",20, "Power", 180),
           Segment("Sprint",  10, "Power", 350)] * 5),
        Segment("Cooldown",  300, "Open"),
    ],
    "Pyramid  (1-2-3-2-1 min)": [
        Segment("Warmup",   300, "Open"),
        Segment("Work",      60, "Power", 230),
        Segment("Rest",      60, "Power",  80),
        Segment("Work",     120, "Power", 210),
        Segment("Rest",      60, "Power",  80),
        Segment("Work",     180, "Power", 190),
        Segment("Rest",      90, "Power",  80),
        Segment("Work",     120, "Power", 210),
        Segment("Rest",      60, "Power",  80),
        Segment("Work",      60, "Power", 230),
        Segment("Cooldown",  300, "Open"),
    ],
    "EMOM  (40s work / 20s rest × 12)": [
        Segment("Warmup",    300, "Open"),
        *([Segment("Work",    40, "Power", 200),
           Segment("Rest",    20, "Power",  70)] * 12),
        Segment("Cooldown",  300, "Open"),
    ],
}


# ─── BLE worker ───────────────────────────────────────────────────────────────

class BleWorker(QObject):
    data_received = pyqtSignal(dict)
    connected     = pyqtSignal(str)
    disconnected  = pyqtSignal()
    error         = pyqtSignal(str)
    scan_result   = pyqtSignal(list)
    control_sent  = pyqtSignal(str)   # description of last control write

    def __init__(self):
        super().__init__()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: BleakClient | None = None

    def start(self):
        threading.Thread(target=self._run_loop, daemon=True).start()

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _go(self, coro):
        if self._loop:
            asyncio.run_coroutine_threadsafe(coro, self._loop)

    # ── Discovery / connection ────────────────────────────────────────────────
    def scan(self):                   self._go(self._scan())
    def connect_to(self, addr: str):  self._go(self._connect(addr))
    def disconnect(self):             self._go(self._disconnect())

    async def _scan(self):
        try:
            devs = await BleakScanner.discover(timeout=10.0, service_uuids=[FTMS_SERVICE_UUID])
            if not devs:
                all_d = await BleakScanner.discover(timeout=8.0)
                devs  = [d for d in all_d
                         if d.name and any(k in d.name.lower() for k in ECHO_BIKE_KEYWORDS)]
            self.scan_result.emit([(d.name or "Unknown", d.address) for d in devs])
        except Exception as e:
            self.error.emit(str(e))

    async def _connect(self, addr: str):
        try:
            self._client = BleakClient(
                addr, timeout=20.0,
                disconnected_callback=lambda _: self.disconnected.emit(),
            )
            await self._client.connect()
            self.connected.emit(addr)
            await self._client.start_notify(INDOOR_BIKE_DATA, self._on_bike)
            try:
                await self._client.start_notify(HEART_RATE_UUID, self._on_hr)
            except Exception:
                pass
        except Exception as e:
            self.error.emit(str(e))

    async def _disconnect(self):
        if self._client and self._client.is_connected:
            try:
                await self._client.stop_notify(INDOOR_BIKE_DATA)
            except Exception:
                pass
            await self._client.disconnect()

    # ── Data callbacks ────────────────────────────────────────────────────────
    def _on_bike(self, _s, raw: bytes):
        parsed = parse_indoor_bike_data(bytes(raw))
        if parsed:
            parsed["ts"] = datetime.now().isoformat()
            self.data_received.emit(parsed)

    def _on_hr(self, _s, raw: bytes):
        hr = parse_heart_rate(bytes(raw))
        if hr is not None:
            self.data_received.emit({"heart_rate": hr, "ts": datetime.now().isoformat()})

    # ── FTMS Control Point writes ─────────────────────────────────────────────
    # The Echo Bike v3 may not honour all ops — writes are fire-and-forget.
    def request_control(self):
        self._go(self._ctrl_write(bytes([0x00]), "Request Control"))

    def send_target_power(self, watts: int):
        w = max(0, min(watts, 4000))
        self._go(self._ctrl_write(bytes([0x05, w & 0xFF, (w >> 8) & 0xFF]),
                                  f"Target Power → {w} W"))

    def send_target_hr(self, bpm: int):
        self._go(self._ctrl_write(bytes([0x06, max(0, min(bpm, 255))]),
                                  f"Target HR → {bpm} bpm"))

    def send_target_cadence(self, rpm: int):
        r = max(0, min(rpm, 9999))
        self._go(self._ctrl_write(bytes([0x14, r & 0xFF, (r >> 8) & 0xFF]),
                                  f"Target Cadence → {r} rpm"))

    def ftms_start(self):
        self._go(self._ctrl_write(bytes([0x07]), "Start / Resume"))

    def ftms_stop(self):
        self._go(self._ctrl_write(bytes([0x08, 0x01]), "Stop"))

    async def _ctrl_write(self, data: bytes, description: str):
        if not (self._client and self._client.is_connected):
            return
        try:
            await self._client.write_gatt_char(FTMS_CONTROL_UUID, data, response=True)
            self.control_sent.emit(description)
        except Exception:
            pass   # bike may not implement control point — silent fallback


# ─── Metric card ──────────────────────────────────────────────────────────────

class MetricCard(QFrame):
    def __init__(self, label: str, unit: str, color: str, big: bool = False):
        super().__init__()
        self._color = color
        self.setStyleSheet(f"""
            MetricCard {{
                background:{CARD_BG};border:1px solid {CARD_BORDER};border-radius:14px;
            }}
        """)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(110, 110)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(1)

        self._lbl = QLabel(label)
        self._lbl.setStyleSheet(
            f"color:{COL_MUTED};font-size:10px;font-weight:700;"
            f"letter-spacing:1.5px;border:none;background:transparent;"
        )
        self._lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)

        fs = 40 if big else 34
        self._val = QLabel("--")
        self._val.setStyleSheet(
            f"color:{color};font-size:{fs}px;font-weight:800;"
            f"border:none;background:transparent;"
        )
        self._val.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._unit_lbl = QLabel(unit)
        self._unit_lbl.setStyleSheet(
            f"color:{COL_MUTED};font-size:11px;border:none;background:transparent;"
        )
        self._unit_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)

        lay.addWidget(self._lbl)
        lay.addStretch(1)
        lay.addWidget(self._val)
        lay.addStretch(1)
        lay.addWidget(self._unit_lbl)

    def set_value(self, v, decimals: int = 0):
        if isinstance(v, (int, float)):
            self._val.setText(f"{v:.{decimals}f}" if decimals else f"{v:.0f}")
        else:
            self._val.setText(str(v))

    def highlight(self, color: str | None = None):
        c = color or self._color
        self._val.setStyleSheet(
            self._val.styleSheet().split("color:")[0]
            + f"color:{c};" + ";".join(self._val.styleSheet().split(";")[1:])
        )


# ─── Workout Builder Dialog ───────────────────────────────────────────────────

class WorkoutBuilderDialog(QDialog):
    """
    Lets the user build a structured interval workout from segments,
    choose from preset templates, and repeat blocks.
    """

    def __init__(self, initial_plan: list[Segment] | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Workout Builder")
        self.setStyleSheet(f"background:{BG};color:{COL_WHITE};")
        self.resize(860, 560)
        self.result_plan: list[Segment] = []
        self._segs: list[Segment] = copy.deepcopy(initial_plan or [])

        lay = QVBoxLayout(self)
        lay.setSpacing(10)
        lay.setContentsMargins(16, 14, 16, 14)

        lay.addLayout(self._build_template_row())
        lay.addWidget(self._build_table())
        lay.addLayout(self._build_add_row())
        lay.addLayout(self._build_bottom_row())

        self._rebuild_table()

    # ── Template row ──────────────────────────────────────────────────────────

    def _build_template_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(10)

        lbl = QLabel("PRESET TEMPLATES")
        lbl.setStyleSheet(f"color:{COL_MUTED};font-size:10px;font-weight:700;letter-spacing:1px;")
        row.addWidget(lbl)

        self._tmpl_combo = QComboBox()
        self._tmpl_combo.addItem("— choose a template —")
        self._tmpl_combo.addItems(list(WORKOUT_TEMPLATES.keys()))
        self._tmpl_combo.setMinimumWidth(280)
        self._tmpl_combo.setStyleSheet(self._field_ss())
        row.addWidget(self._tmpl_combo)

        load_btn = QPushButton("Load Template")
        load_btn.setStyleSheet(_btn_style(COL_CADENCE, 130))
        load_btn.clicked.connect(self._load_template)
        row.addWidget(load_btn)

        row.addStretch()

        self._total_lbl = QLabel("Total: 00:00")
        self._total_lbl.setStyleSheet(f"color:{COL_MUTED};font-size:12px;font-weight:700;")
        row.addWidget(self._total_lbl)
        return row

    # ── Segment table ─────────────────────────────────────────────────────────

    def _build_table(self) -> QTableWidget:
        self._tbl = QTableWidget()
        self._tbl.setColumnCount(7)
        self._tbl.setHorizontalHeaderLabels(
            ["#", "Segment", "Duration", "Target", "Value", "↑ ↓", "✕"]
        )
        self._tbl.setStyleSheet(f"""
            QTableWidget {{
                background:{CARD_BG};color:{COL_WHITE};
                border:1px solid {CARD_BORDER};border-radius:8px;
                gridline-color:{CARD_BORDER};font-size:12px;
            }}
            QHeaderView::section {{
                background:#1a1a38;color:{COL_MUTED};
                border:none;padding:5px 4px;
                font-size:10px;font-weight:700;letter-spacing:1px;
            }}
            QTableWidget::item:selected {{background:#2a2a5a;}}
            QComboBox, QSpinBox, QLineEdit {{
                background:#0d0d1a;color:{COL_WHITE};
                border:1px solid {CARD_BORDER};border-radius:4px;
                padding:2px 4px;font-size:11px;
            }}
            QComboBox::drop-down {{border:none;}}
        """)
        hdr = self._tbl.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(6, QHeaderView.ResizeMode.ResizeToContents)
        self._tbl.verticalHeader().setVisible(False)
        self._tbl.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._tbl.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        return self._tbl

    def _build_add_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(10)

        add_lbl = QLabel("ADD")
        add_lbl.setStyleSheet(f"color:{COL_MUTED};font-size:10px;font-weight:700;letter-spacing:1px;")
        row.addWidget(add_lbl)

        self._add_combo = QComboBox()
        self._add_combo.addItems(SEGMENT_NAMES)
        self._add_combo.setFixedWidth(120)
        self._add_combo.setStyleSheet(self._field_ss())
        row.addWidget(self._add_combo)

        add_btn = QPushButton("+ Add Segment")
        add_btn.setStyleSheet(_btn_style(COL_SPEED, 130))
        add_btn.clicked.connect(self._add_segment)
        row.addWidget(add_btn)

        row.addSpacing(20)

        rep_lbl = QLabel("REPEAT selected rows")
        rep_lbl.setStyleSheet(f"color:{COL_MUTED};font-size:10px;font-weight:700;letter-spacing:1px;")
        row.addWidget(rep_lbl)

        self._rep_spin = QSpinBox()
        self._rep_spin.setRange(2, 50)
        self._rep_spin.setValue(8)
        self._rep_spin.setFixedWidth(60)
        self._rep_spin.setStyleSheet(self._field_ss())
        row.addWidget(self._rep_spin)

        rep_lbl2 = QLabel("×")
        rep_lbl2.setStyleSheet(f"color:{COL_MUTED};font-size:14px;")
        row.addWidget(rep_lbl2)

        rep_btn = QPushButton("Repeat")
        rep_btn.setStyleSheet(_btn_style(COL_CALS, 90))
        rep_btn.clicked.connect(self._repeat_selected)
        row.addWidget(rep_btn)

        row.addStretch()

        clr_btn = QPushButton("Clear All")
        clr_btn.setStyleSheet(_btn_style(COL_HR, 90))
        clr_btn.clicked.connect(self._clear_all)
        row.addWidget(clr_btn)

        return row

    def _build_bottom_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setStyleSheet(_btn_style(COL_MUTED, 100))
        cancel_btn.clicked.connect(self.reject)
        row.addWidget(cancel_btn)
        row.addStretch()

        info = QLabel("Targets are sent to bike console via FTMS Control Point (if supported)")
        info.setStyleSheet(f"color:{COL_MUTED};font-size:10px;")
        row.addWidget(info)
        row.addStretch()

        ok_btn = QPushButton("✓  Load Workout")
        ok_btn.setStyleSheet(_btn_style(COL_POWER, 150))
        ok_btn.clicked.connect(self._accept)
        row.addWidget(ok_btn)
        return row

    # ── Table population ──────────────────────────────────────────────────────

    def _rebuild_table(self):
        self._tbl.setRowCount(0)
        for i, seg in enumerate(self._segs):
            self._insert_row(i, seg)
        self._update_total()

    def _insert_row(self, i: int, seg: Segment):
        r = self._tbl.rowCount()
        self._tbl.insertRow(r)

        # Col 0: index
        num = QTableWidgetItem(str(i + 1))
        num.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        num.setForeground(QColor(COL_MUTED))
        self._tbl.setItem(r, 0, num)

        # Col 1: name combo
        name_cb = QComboBox()
        name_cb.addItems(SEGMENT_NAMES)
        name_cb.setCurrentText(seg.name if seg.name in SEGMENT_NAMES else "Custom")
        name_cb.setStyleSheet(f"color:{seg.color};font-weight:700;")
        self._tbl.setCellWidget(r, 1, name_cb)

        # Col 2: duration (MM:SS editable)
        dur_edit = QLineEdit(seg.duration_str)
        dur_edit.setAlignment(Qt.AlignmentFlag.AlignCenter)
        dur_edit.setPlaceholderText("MM:SS")
        self._tbl.setCellWidget(r, 2, dur_edit)

        # Col 3: target type combo
        tgt_cb = QComboBox()
        tgt_cb.addItems(["Open", "Power", "HR", "Cadence"])
        tgt_cb.setCurrentText(seg.target_type)
        val_spin = QSpinBox()   # created here so tgt_cb can reference it
        val_spin.setRange(0, 5000)
        val_spin.setValue(seg.target_value)
        val_spin.setEnabled(seg.target_type != "Open")
        tgt_cb.currentTextChanged.connect(
            lambda t, s=val_spin: s.setEnabled(t != "Open")
        )
        self._tbl.setCellWidget(r, 3, tgt_cb)

        # Col 4: target value
        self._tbl.setCellWidget(r, 4, val_spin)

        # Col 5: up/down
        ud_widget = QWidget()
        ud_lay = QHBoxLayout(ud_widget)
        ud_lay.setContentsMargins(2, 2, 2, 2)
        ud_lay.setSpacing(2)
        up_btn = QPushButton("▲")
        up_btn.setFixedSize(24, 24)
        up_btn.setStyleSheet(f"background:#1a1a38;color:{COL_MUTED};border:none;border-radius:4px;")
        dn_btn = QPushButton("▼")
        dn_btn.setFixedSize(24, 24)
        dn_btn.setStyleSheet(f"background:#1a1a38;color:{COL_MUTED};border:none;border-radius:4px;")
        up_btn.clicked.connect(lambda _, idx=i: self._move_seg(idx, -1))
        dn_btn.clicked.connect(lambda _, idx=i: self._move_seg(idx,  1))
        ud_lay.addWidget(up_btn)
        ud_lay.addWidget(dn_btn)
        self._tbl.setCellWidget(r, 5, ud_widget)

        # Col 6: remove
        del_btn = QPushButton("✕")
        del_btn.setFixedSize(28, 24)
        del_btn.setStyleSheet(
            f"background:{COL_HR}22;color:{COL_HR};"
            f"border:1px solid {COL_HR}55;border-radius:4px;font-weight:700;"
        )
        del_btn.clicked.connect(lambda _, idx=i: self._remove_seg(idx))
        self._tbl.setCellWidget(r, 6, del_btn)

        self._tbl.setRowHeight(r, 36)

    # ── Actions ───────────────────────────────────────────────────────────────

    def _collect_segs(self) -> list[Segment]:
        """Read current widget state from table into a list of Segments."""
        segs = []
        for r in range(self._tbl.rowCount()):
            name_w = self._tbl.cellWidget(r, 1)
            dur_w  = self._tbl.cellWidget(r, 2)
            tgt_w  = self._tbl.cellWidget(r, 3)
            val_w  = self._tbl.cellWidget(r, 4)

            name = name_w.currentText() if name_w else "Custom"
            ttype = tgt_w.currentText() if tgt_w else "Open"
            tval  = val_w.value()       if val_w else 0

            dur_s = 60
            if dur_w:
                txt = dur_w.text().strip()
                try:
                    parts = txt.split(":")
                    dur_s = int(parts[0]) * 60 + int(parts[1]) if len(parts) == 2 \
                            else int(parts[0])
                except (ValueError, IndexError):
                    dur_s = 60

            segs.append(Segment(name, max(1, dur_s), ttype, tval))
        return segs

    def _load_template(self):
        key = self._tmpl_combo.currentText()
        if key in WORKOUT_TEMPLATES:
            self._segs = copy.deepcopy(WORKOUT_TEMPLATES[key])
            self._rebuild_table()

    def _add_segment(self):
        self._segs = self._collect_segs()
        preset_map = {
            "Warmup":   Segment("Warmup",   300, "Open"),
            "Work":     Segment("Work",       20, "Power", 250),
            "Sprint":   Segment("Sprint",     15, "Power", 350),
            "Steady":   Segment("Steady",    600, "Power", 160),
            "Rest":     Segment("Rest",       10, "Power",  80),
            "Recovery": Segment("Recovery",   90, "Power",  80),
            "Cooldown": Segment("Cooldown",  300, "Open"),
            "Custom":   Segment("Custom",     60, "Open"),
        }
        name = self._add_combo.currentText()
        self._segs.append(copy.copy(preset_map.get(name, Segment(name, 60, "Open"))))
        self._rebuild_table()

    def _remove_seg(self, idx: int):
        self._segs = self._collect_segs()
        if 0 <= idx < len(self._segs):
            self._segs.pop(idx)
            self._rebuild_table()

    def _move_seg(self, idx: int, direction: int):
        self._segs = self._collect_segs()
        j = idx + direction
        if 0 <= j < len(self._segs):
            self._segs[idx], self._segs[j] = self._segs[j], self._segs[idx]
            self._rebuild_table()

    def _repeat_selected(self):
        self._segs = self._collect_segs()
        rows = sorted({idx.row() for idx in self._tbl.selectedIndexes()})
        if not rows:
            QMessageBox.information(self, "Repeat", "Select rows first, then press Repeat.")
            return
        block = [copy.copy(self._segs[r]) for r in rows if r < len(self._segs)]
        n = self._rep_spin.value()
        insert_at = max(rows) + 1
        for i in range(1, n):
            for j, seg in enumerate(block):
                self._segs.insert(insert_at + (i - 1) * len(block) + j, copy.copy(seg))
        self._rebuild_table()

    def _clear_all(self):
        self._segs.clear()
        self._rebuild_table()

    def _update_total(self):
        total = sum(s.duration_s for s in self._segs)
        h, rem = divmod(total, 3600)
        m, s = divmod(rem, 60)
        t_str = f"{h}h {m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
        self._total_lbl.setText(f"Total: {t_str}  ({len(self._segs)} segments)")

    def _accept(self):
        self.result_plan = self._collect_segs()
        if not self.result_plan:
            QMessageBox.warning(self, "Empty Workout", "Add at least one segment.")
            return
        self.accept()

    @staticmethod
    def _field_ss() -> str:
        return (f"background:#0d0d1a;color:{COL_WHITE};"
                f"border:1px solid {CARD_BORDER};border-radius:6px;"
                f"padding:3px 8px;font-size:12px;")


# ─── Misc dialogs ─────────────────────────────────────────────────────────────

class DevicePickerDialog(QDialog):
    def __init__(self, devices: list[tuple[str, str]], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Select Echo Bike")
        self.setStyleSheet(f"background:{BG};color:{COL_WHITE};")
        self.resize(420, 220)
        self._devices = devices
        self.selected: tuple[str, str] | None = None

        lay = QVBoxLayout(self)
        lbl = QLabel("Multiple FTMS devices found — select your Echo Bike:")
        lbl.setStyleSheet(f"color:{COL_MUTED};font-size:12px;")
        lay.addWidget(lbl)

        self._list = QListWidget()
        self._list.setStyleSheet(
            f"background:{CARD_BG};color:{COL_WHITE};border:1px solid {CARD_BORDER};"
            f"border-radius:8px;font-size:13px;padding:4px;"
        )
        for name, addr in devices:
            self._list.addItem(f"  {name}   ({addr})")
        self._list.setCurrentRow(0)
        lay.addWidget(self._list)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btns.setStyleSheet(f"color:{COL_WHITE};")
        btns.accepted.connect(self._ok)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def _ok(self):
        r = self._list.currentRow()
        if r >= 0:
            self.selected = self._devices[r]
        self.accept()


class HistoryDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Workout History")
        self.setStyleSheet(f"background:{BG};color:{COL_WHITE};")
        self.resize(1120, 460)

        lay = QVBoxLayout(self)
        lay.setSpacing(10)

        info = QLabel(f"Files saved to:  {WORKOUTS_DIR}")
        info.setStyleSheet(f"color:{COL_MUTED};font-size:11px;")
        lay.addWidget(info)

        cols = ["Date", "Rider", "Label", "Type", "Duration",
                "Distance", "Calories", "Avg W", "Max W",
                "Avg W/kg", "Avg RPM", "Notes"]
        tbl = QTableWidget()
        tbl.setColumnCount(len(cols))
        tbl.setHorizontalHeaderLabels(cols)
        tbl.setStyleSheet(f"""
            QTableWidget {{
                background:{CARD_BG};color:{COL_WHITE};
                border:none;gridline-color:{CARD_BORDER};font-size:12px;
            }}
            QHeaderView::section {{
                background:#1a1a38;color:{COL_MUTED};
                border:none;padding:6px 4px;
                font-size:10px;font-weight:700;letter-spacing:1px;
            }}
            QTableWidget::item:selected {{background:#2a2a5a;}}
        """)
        hdr = tbl.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(2,  QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(11, QHeaderView.ResizeMode.Stretch)
        tbl.verticalHeader().setVisible(False)
        tbl.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        tbl.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        tbl.setAlternatingRowColors(True)

        files = sorted(WORKOUTS_DIR.glob("workout_*.json"), reverse=True) \
                if WORKOUTS_DIR.exists() else []

        for f in files[:80]:
            try:
                s = json.loads(f.read_text()).get("summary", {})
            except Exception:
                continue
            row = tbl.rowCount()
            tbl.insertRow(row)

            left = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter

            def cell(v, color=COL_WHITE, align=Qt.AlignmentFlag.AlignCenter):
                it = QTableWidgetItem(str(v))
                it.setTextAlignment(align)
                it.setForeground(QColor(color))
                return it

            dur  = s.get("duration_seconds")
            dist = s.get("distance_m")
            dur_str = (f"{int(dur)//3600:02d}:{(int(dur)%3600)//60:02d}:{int(dur)%60:02d}"
                       if dur else "--")

            tbl.setItem(row,  0, cell(s.get("date", "")[:19].replace("T", " "), COL_MUTED))
            tbl.setItem(row,  1, cell(s.get("rider", "") or "--"))
            tbl.setItem(row,  2, cell(s.get("label", "") or "--", align=left))
            tbl.setItem(row,  3, cell(s.get("type", "--")))
            tbl.setItem(row,  4, cell(dur_str))
            tbl.setItem(row,  5, cell(f"{dist/1000:.2f} km" if dist else "--"))
            tbl.setItem(row,  6, cell(f"{s['total_calories']:.0f}" if s.get("total_calories") else "--", COL_CALS))
            tbl.setItem(row,  7, cell(f"{s['avg_power_w']}"        if s.get("avg_power_w")    else "--", COL_POWER))
            tbl.setItem(row,  8, cell(f"{s['max_power_w']}"        if s.get("max_power_w")    else "--", COL_POWER))
            tbl.setItem(row,  9, cell(f"{s['avg_wkg']}"            if s.get("avg_wkg")        else "--", COL_WKG))
            tbl.setItem(row, 10, cell(f"{s['avg_cadence_rpm']}"    if s.get("avg_cadence_rpm")else "--", COL_CADENCE))
            tbl.setItem(row, 11, cell(s.get("notes", "") or "", align=left))

        lay.addWidget(tbl)
        if not files:
            lay.addWidget(QLabel("No workouts saved yet."))

        btn_row = QHBoxLayout()
        open_btn = QPushButton("📂  Open Folder")
        open_btn.setStyleSheet(_btn_style(COL_DIST, 140))
        open_btn.clicked.connect(lambda: __import__("subprocess").run(
            ["open", str(WORKOUTS_DIR)], check=False
        ))
        btn_row.addWidget(open_btn)
        btn_row.addStretch()
        close_btn = QPushButton("Close")
        close_btn.setStyleSheet(_btn_style(COL_MUTED, 100))
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)
        lay.addLayout(btn_row)


# ─── Button style helper ──────────────────────────────────────────────────────

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


# ─── Main window ──────────────────────────────────────────────────────────────

class EchoBikeWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Echo Bike Tracker")
        self.resize(1200, 840)
        self.setMinimumSize(960, 700)

        # Session state
        self._metrics:      dict       = {}
        self._session_data: list[dict] = []
        self._start_time:   datetime | None = None
        self._weight_kg:    float      = 75.0
        self._device_name:  str        = ""
        self._scan_results: list[tuple[str, str]] = []

        # Workout metadata
        self._rider_name:    str   = ""
        self._workout_label: str   = ""
        self._workout_type:  str   = "Free Ride"
        self._workout_notes: str   = ""
        self._target_type:   str   = "None"
        self._target_value:  float = 0.0

        # Structured workout plan
        self._workout_plan:    list[Segment] = []
        self._plan_idx:        int           = -1   # -1 = not running
        self._segment_start:   datetime | None = None
        self._plan_timer = QTimer(self, timeout=self._tick_plan, interval=500)

        # Chart buffers
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

    # ── Palette ───────────────────────────────────────────────────────────────

    def _apply_palette(self):
        QApplication.instance().setStyle("Fusion")
        pal = QPalette()
        cr  = QPalette.ColorRole
        pal.setColor(cr.Window,          QColor(BG))
        pal.setColor(cr.WindowText,      QColor(COL_WHITE))
        pal.setColor(cr.Base,            QColor(CARD_BG))
        pal.setColor(cr.AlternateBase,   QColor("#18182e"))
        pal.setColor(cr.Text,            QColor(COL_WHITE))
        pal.setColor(cr.Button,          QColor(CARD_BG))
        pal.setColor(cr.ButtonText,      QColor(COL_WHITE))
        pal.setColor(cr.Highlight,       QColor(COL_POWER))
        pal.setColor(cr.HighlightedText, QColor("#000000"))
        QApplication.instance().setPalette(pal)

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        lay = QVBoxLayout(root)
        lay.setContentsMargins(18, 14, 18, 14)
        lay.setSpacing(10)

        lay.addLayout(self._build_header())
        lay.addWidget(self._build_workout_setup())
        lay.addWidget(self._build_plan_bar())         # segment progress — hidden until plan starts
        lay.addLayout(self._build_cards(),   stretch=2)
        lay.addWidget(self._build_chart_panel(), stretch=3)
        lay.addLayout(self._build_controls())

        sb = QStatusBar()
        sb.setStyleSheet(f"color:{COL_MUTED};font-size:11px;background:{BG};")
        self.setStatusBar(sb)
        self._status = sb
        self._status.showMessage("Not connected  ·  Press  ⟳ Scan  to find your Echo Bike")

    # ── Header ────────────────────────────────────────────────────────────────

    def _build_header(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(14)

        title = QLabel("🚴  ECHO BIKE")
        title.setStyleSheet(
            f"color:{COL_POWER};font-size:22px;font-weight:900;letter-spacing:3px;"
        )
        row.addWidget(title)
        row.addStretch()

        self._dot = QLabel("●")
        self._dot.setStyleSheet("color:#333355;font-size:20px;")
        row.addWidget(self._dot)

        self._conn_lbl = QLabel("Disconnected")
        self._conn_lbl.setStyleSheet(f"color:{COL_MUTED};font-size:13px;")
        row.addWidget(self._conn_lbl)
        row.addSpacing(24)

        self._timer_lbl = QLabel("00:00:00")
        self._timer_lbl.setStyleSheet(
            f"color:{COL_WHITE};font-size:24px;font-weight:800;font-family:monospace;"
        )
        row.addWidget(self._timer_lbl)
        row.addSpacing(24)

        w_lbl = QLabel("Rider weight:")
        w_lbl.setStyleSheet(f"color:{COL_MUTED};font-size:12px;")
        row.addWidget(w_lbl)

        self._weight_spin = QDoubleSpinBox()
        self._weight_spin.setRange(30.0, 250.0)
        self._weight_spin.setValue(self._weight_kg)
        self._weight_spin.setSuffix(" kg")
        self._weight_spin.setDecimals(1)
        self._weight_spin.setFixedWidth(95)
        self._weight_spin.setStyleSheet(f"""
            QDoubleSpinBox {{
                background:{CARD_BG};color:{COL_WHITE};
                border:1px solid {CARD_BORDER};border-radius:7px;
                padding:4px 8px;font-size:13px;
            }}
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
        row = QHBoxLayout(frame)
        row.setContentsMargins(14, 8, 14, 8)
        row.setSpacing(14)

        fs = (f"background:#0d0d1a;color:{COL_WHITE};"
              f"border:1px solid {CARD_BORDER};border-radius:6px;"
              f"padding:3px 8px;font-size:12px;")

        def mlbl(t):
            l = QLabel(t)
            l.setStyleSheet(
                f"color:{COL_MUTED};font-size:10px;font-weight:700;"
                f"letter-spacing:1px;border:none;background:transparent;"
            )
            return l

        # Rider
        row.addWidget(mlbl("RIDER"))
        self._name_input = QLineEdit()
        self._name_input.setPlaceholderText("Your name")
        self._name_input.setFixedWidth(120)
        self._name_input.setStyleSheet(fs)
        self._name_input.textChanged.connect(lambda t: setattr(self, "_rider_name", t))
        row.addWidget(self._name_input)

        # Label
        row.addWidget(mlbl("LABEL"))
        self._lbl_input = QLineEdit()
        self._lbl_input.setPlaceholderText("e.g. Morning HIIT")
        self._lbl_input.setFixedWidth(150)
        self._lbl_input.setStyleSheet(fs)
        self._lbl_input.textChanged.connect(lambda t: setattr(self, "_workout_label", t))
        row.addWidget(self._lbl_input)

        # Type
        row.addWidget(mlbl("TYPE"))
        self._type_combo = QComboBox()
        self._type_combo.addItems([
            "Free Ride", "HIIT", "Steady State", "Intervals",
            "Threshold", "Recovery", "Race", "Test", "Custom",
        ])
        self._type_combo.setFixedWidth(120)
        self._type_combo.setStyleSheet(fs)
        self._type_combo.currentTextChanged.connect(lambda t: setattr(self, "_workout_type", t))
        row.addWidget(self._type_combo)

        # Notes
        row.addWidget(mlbl("NOTES"))
        self._notes_input = QLineEdit()
        self._notes_input.setPlaceholderText("Optional notes…")
        self._notes_input.setStyleSheet(fs)
        self._notes_input.textChanged.connect(lambda t: setattr(self, "_workout_notes", t))
        row.addWidget(self._notes_input, stretch=1)

        # Simple target (for free workouts without a plan)
        row.addWidget(mlbl("TARGET"))
        self._target_combo = QComboBox()
        self._target_combo.addItems(["None", "Time (min)", "Distance (km)", "Calories", "Power (W)"])
        self._target_combo.setFixedWidth(120)
        self._target_combo.setStyleSheet(fs)
        self._target_combo.currentTextChanged.connect(self._on_target_type_changed)
        row.addWidget(self._target_combo)

        self._target_spin = QSpinBox()
        self._target_spin.setRange(0, 9999)
        self._target_spin.setFixedWidth(75)
        self._target_spin.setEnabled(False)
        self._target_spin.setStyleSheet(fs)
        self._target_spin.valueChanged.connect(lambda v: setattr(self, "_target_value", float(v)))
        row.addWidget(self._target_spin)

        return frame

    def _on_target_type_changed(self, text: str):
        self._target_type = text
        self._target_spin.setEnabled(text != "None")
        hints = {
            "Time (min)": (1, 300, 20), "Distance (km)": (1, 200, 5),
            "Calories": (50, 5000, 500), "Power (W)": (50, 1000, 200),
        }
        if text in hints:
            lo, hi, d = hints[text]
            self._target_spin.setRange(lo, hi)
            self._target_spin.setValue(d)

    # ── Segment progress bar (hidden when no plan running) ────────────────────

    def _build_plan_bar(self) -> QFrame:
        self._plan_bar_frame = QFrame()
        self._plan_bar_frame.setVisible(False)
        self._plan_bar_frame.setStyleSheet(
            f"QFrame{{background:{CARD_BG};border:1px solid {CARD_BORDER};border-radius:10px;}}"
        )
        lay = QVBoxLayout(self._plan_bar_frame)
        lay.setContentsMargins(14, 8, 14, 8)
        lay.setSpacing(4)

        top = QHBoxLayout()

        self._seg_name_lbl = QLabel("WARMUP")
        self._seg_name_lbl.setStyleSheet(
            f"color:{COL_POWER};font-size:16px;font-weight:900;"
            f"letter-spacing:2px;border:none;background:transparent;"
        )
        top.addWidget(self._seg_name_lbl)

        self._seg_idx_lbl = QLabel("Segment 1 / 1")
        self._seg_idx_lbl.setStyleSheet(f"color:{COL_MUTED};font-size:12px;border:none;background:transparent;")
        top.addWidget(self._seg_idx_lbl)

        top.addStretch()

        self._seg_countdown_lbl = QLabel("00:00")
        self._seg_countdown_lbl.setStyleSheet(
            f"color:{COL_WHITE};font-size:22px;font-weight:800;"
            f"font-family:monospace;border:none;background:transparent;"
        )
        top.addWidget(self._seg_countdown_lbl)

        top.addSpacing(20)

        self._seg_target_lbl = QLabel("Target: Open")
        self._seg_target_lbl.setStyleSheet(f"color:{COL_MUTED};font-size:12px;border:none;background:transparent;")
        top.addWidget(self._seg_target_lbl)

        top.addSpacing(20)

        self._seg_next_lbl = QLabel("")
        self._seg_next_lbl.setStyleSheet(f"color:{COL_MUTED};font-size:11px;border:none;background:transparent;")
        top.addWidget(self._seg_next_lbl)

        top.addStretch()

        stop_plan_btn = QPushButton("■  Stop Plan")
        stop_plan_btn.setStyleSheet(_btn_style(COL_HR, 110))
        stop_plan_btn.clicked.connect(self._stop_plan)
        top.addWidget(stop_plan_btn)

        lay.addLayout(top)

        self._seg_progress = QProgressBar()
        self._seg_progress.setRange(0, 1000)
        self._seg_progress.setValue(0)
        self._seg_progress.setTextVisible(False)
        self._seg_progress.setFixedHeight(6)
        self._seg_progress.setStyleSheet(f"""
            QProgressBar {{
                background:{CARD_BORDER};border-radius:3px;border:none;
            }}
            QProgressBar::chunk {{
                background:qlineargradient(x1:0,y1:0,x2:1,y2:0,
                    stop:0 {COL_CADENCE}, stop:1 {COL_POWER});
                border-radius:3px;
            }}
        """)
        lay.addWidget(self._seg_progress)

        return self._plan_bar_frame

    # ── Metric cards ──────────────────────────────────────────────────────────

    def _build_cards(self) -> QGridLayout:
        grid = QGridLayout()
        grid.setSpacing(10)

        self._c_power   = MetricCard("POWER",      "watts", COL_POWER,   big=True)
        self._c_wkg     = MetricCard("W / KG",     "w/kg",  COL_WKG,     big=True)
        self._c_cadence = MetricCard("CADENCE",    "rpm",   COL_CADENCE, big=True)
        self._c_speed   = MetricCard("SPEED",      "km/h",  COL_SPEED,   big=True)
        self._c_hr      = MetricCard("HEART RATE", "bpm",   COL_HR)
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
        frame.setStyleSheet(
            f"QFrame{{background:{CARD_BG};border:1px solid {CARD_BORDER};border-radius:14px;}}"
        )
        frame.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        lay = QVBoxLayout(frame)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(8)

        tog = QHBoxLayout()
        tog.setSpacing(18)
        hdr = QLabel("LIVE CHART")
        hdr.setStyleSheet(
            f"color:{COL_MUTED};font-size:10px;font-weight:700;"
            f"letter-spacing:2px;border:none;background:transparent;"
        )
        tog.addWidget(hdr)

        self._series_cbs: dict[str, QCheckBox] = {}
        for name, color in [
            ("Power (W)",   COL_POWER),
            ("HR (bpm)",    COL_HR),
            ("Cadence",     COL_CADENCE),
            ("W/kg",        COL_WKG),
            ("Speed km/h",  COL_SPEED),
        ]:
            cb = QCheckBox(name)
            cb.setChecked(name in ("Power (W)", "HR (bpm)", "Cadence"))
            cb.setStyleSheet(
                f"color:{color};font-size:12px;font-weight:700;"
                f"border:none;background:transparent;"
            )
            cb.stateChanged.connect(self._redraw_chart)
            tog.addWidget(cb)
            self._series_cbs[name] = cb

        tog.addStretch()
        tog.addWidget(QLabel("Window:"))

        self._win_combo = QComboBox()
        self._win_combo.addItems(["1 min", "5 min", "10 min", "30 min", "All"])
        self._win_combo.setCurrentIndex(1)
        self._win_combo.setFixedWidth(90)
        self._win_combo.setStyleSheet(f"""
            QComboBox {{
                background:{CARD_BG};color:{COL_WHITE};
                border:1px solid {CARD_BORDER};border-radius:6px;
                padding:3px 8px;font-size:11px;
            }}
            QComboBox::drop-down {{border:none;width:14px;}}
            QComboBox QAbstractItemView {{
                background:#1a1a38;color:{COL_WHITE};
                selection-background-color:{COL_POWER};
            }}
        """)
        self._win_combo.currentIndexChanged.connect(self._redraw_chart)
        tog.addWidget(self._win_combo)
        lay.addLayout(tog)

        pg.setConfigOptions(background=CARD_BG, foreground=COL_MUTED, antialias=True)

        self._plot = pg.PlotWidget()
        self._plot.setStyleSheet("border:none;background:transparent;")
        self._plot.showGrid(x=True, y=True, alpha=0.12)
        self._plot.setLabel("bottom", "Elapsed", units="s", color=COL_MUTED, size="10pt")
        self._plot.setLabel("left",   "Value",              color=COL_MUTED, size="10pt")
        self._plot.getPlotItem().getAxis("bottom").setTextPen(pg.mkPen(color=COL_MUTED))
        self._plot.getPlotItem().getAxis("left").setTextPen(pg.mkPen(color=COL_MUTED))
        self._plot.addLegend(
            offset=(10, 10), labelTextColor=COL_WHITE,
            pen=pg.mkPen(color=CARD_BORDER), brush=pg.mkBrush(color="#0d0d1aCC"),
        )

        self._curves: dict[str, pg.PlotDataItem] = {}
        for name, color, w in [
            ("Power (W)",  COL_POWER,   2.5),
            ("HR (bpm)",   COL_HR,      2.0),
            ("Cadence",    COL_CADENCE, 2.0),
            ("W/kg",       COL_WKG,     2.0),
            ("Speed km/h", COL_SPEED,   2.0),
        ]:
            self._curves[name] = self._plot.plot(pen=pg.mkPen(color=color, width=w), name=name)

        lay.addWidget(self._plot)
        return frame

    # ── Controls row ──────────────────────────────────────────────────────────

    def _build_controls(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(10)

        def btn(text, color, slot):
            b = QPushButton(text)
            b.setStyleSheet(_btn_style(color))
            b.clicked.connect(slot)
            return b

        self._btn_scan  = btn("⟳  Scan",          COL_CADENCE, self._on_scan)
        self._btn_conn  = btn("⚡  Connect",        COL_POWER,   self._on_connect)
        self._btn_disc  = btn("✕  Disconnect",     COL_HR,      self._on_disconnect)
        self._btn_save  = btn("💾  Save",           COL_CALS,    self._on_save)
        self._btn_hist  = btn("📋  History",        COL_DIST,    self._on_history)
        self._btn_build = btn("🏋  Build Workout",  COL_WKG,     self._on_build_workout)
        self._btn_start = btn("▶  Start Workout",  COL_SPEED,   self._start_plan)

        self._btn_conn.setEnabled(False)
        self._btn_disc.setEnabled(False)
        self._btn_save.setEnabled(False)
        self._btn_start.setEnabled(False)

        for b in [self._btn_scan, self._btn_conn, self._btn_disc,
                  self._btn_save, self._btn_hist, self._btn_build, self._btn_start]:
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
        self._ble.control_sent.connect(
            lambda msg: self._status.showMessage(f"FTMS → {msg}")
        )
        self._ble.start()

    def _on_scan(self):
        self._btn_scan.setEnabled(False)
        self._btn_scan.setText("Scanning…")
        self._status.showMessage("Scanning… console on? BT active? (hold BT button 3 sec)")
        self._ble.scan()

    def _on_scan_result(self, devices: list):
        self._btn_scan.setEnabled(True)
        self._btn_scan.setText("⟳  Scan")
        if not devices:
            self._status.showMessage("No Echo Bike found. Is the console on? (Hold BT button 3 sec)")
            return
        if len(devices) == 1:
            self._device_name  = devices[0][0]
            self._scan_results = devices
            self._status.showMessage(f"Found:  {devices[0][0]}  ({devices[0][1]})  —  press Connect")
            self._btn_conn.setEnabled(True)
        else:
            dlg = DevicePickerDialog(devices, self)
            if dlg.exec() == QDialog.DialogCode.Accepted and dlg.selected:
                self._device_name, _ = dlg.selected
                self._scan_results   = devices
                self._btn_conn.setEnabled(True)
                self._status.showMessage(f"Selected:  {self._device_name}  —  press Connect")

    def _on_connect(self):
        addr = next(
            (a for n, a in self._scan_results if n == self._device_name),
            self._scan_results[0][1] if self._scan_results else None,
        )
        if not addr:
            return
        self._btn_conn.setEnabled(False)
        self._btn_scan.setEnabled(False)
        self._status.showMessage(f"Connecting to  {self._device_name}…")
        self._ble.connect_to(addr)

    def _on_connected(self, address: str):
        self._dot.setStyleSheet(f"color:{COL_SPEED};font-size:20px;")
        self._conn_lbl.setText(self._device_name or address)
        self._conn_lbl.setStyleSheet(f"color:{COL_SPEED};font-size:13px;font-weight:700;")
        self._btn_disc.setEnabled(True)
        self._btn_save.setEnabled(True)
        self._btn_scan.setEnabled(False)
        self._btn_start.setEnabled(bool(self._workout_plan))

        self._start_time = datetime.now()
        self._metrics.clear()
        self._session_data.clear()
        for buf in (self._t_buf, self._power_buf, self._hr_buf,
                    self._cadence_buf, self._wkg_buf, self._speed_buf):
            buf.clear()
        self._status.showMessage(
            f"Connected  ·  {self._device_name or address}  ·  Workout in progress"
        )

    def _on_disconnected(self):
        self._dot.setStyleSheet("color:#333355;font-size:20px;")
        self._conn_lbl.setText("Disconnected")
        self._conn_lbl.setStyleSheet(f"color:{COL_MUTED};font-size:13px;")
        self._btn_disc.setEnabled(False)
        self._btn_scan.setEnabled(True)
        self._btn_start.setEnabled(False)
        self._stop_plan()
        self._status.showMessage("Disconnected")

    def _on_ble_error(self, msg: str):
        self._btn_scan.setEnabled(True)
        self._btn_conn.setEnabled(bool(self._scan_results))
        self._status.showMessage(f"BLE error: {msg}")

    def _on_data(self, data: dict):
        self._metrics.update(data)
        self._session_data.append(data)

    # ── Card refresh (4 Hz) ───────────────────────────────────────────────────

    def _refresh_cards(self):
        if self._start_time:
            e = int((datetime.now() - self._start_time).total_seconds())
            self._timer_lbl.setText(f"{e//3600:02d}:{(e%3600)//60:02d}:{e%60:02d}")

        m = self._metrics
        if not m:
            return

        power = m.get("power_w")
        self._c_power.set_value(power if power is not None else "--")
        self._c_wkg.set_value(
            round(power / self._weight_kg, 2) if power is not None and self._weight_kg else "--",
            decimals=2 if power is not None else 0,
        )
        cadence = m.get("cadence_rpm")
        self._c_cadence.set_value(cadence if cadence is not None else "--")
        speed = m.get("speed_kmh")
        self._c_speed.set_value(speed if speed is not None else "--",
                                decimals=1 if speed is not None else 0)
        hr = m.get("heart_rate")
        self._c_hr.set_value(hr if hr is not None else "--")
        cal = m.get("calories")
        self._c_cals.set_value(cal if cal is not None else "--")
        dist = m.get("distance_m")
        self._c_dist.set_value(dist / 1000 if dist is not None else "--",
                                decimals=2 if dist is not None else 0)
        et = m.get("elapsed_seconds") or (
            int((datetime.now() - self._start_time).total_seconds()) if self._start_time else None
        )
        if et is not None:
            self._c_time.set_value(
                f"{int(et)//3600:02d}:{(int(et)%3600)//60:02d}:{int(et)%60:02d}"
            )

    # ── Chart (1 Hz) ──────────────────────────────────────────────────────────

    def _push_chart(self):
        if not self._start_time or not self._metrics:
            return
        elapsed = (datetime.now() - self._start_time).total_seconds()
        m = self._metrics
        self._t_buf.append(elapsed)
        self._power_buf.append(m.get("power_w"))
        self._hr_buf.append(m.get("heart_rate"))
        self._cadence_buf.append(m.get("cadence_rpm"))
        w = m.get("power_w")
        self._wkg_buf.append(w / self._weight_kg if w is not None and self._weight_kg else None)
        self._speed_buf.append(m.get("speed_kmh"))
        self._redraw_chart()

    def _redraw_chart(self):
        if not self._t_buf:
            return
        t_all = list(self._t_buf)
        win_map = {"1 min": 60, "5 min": 300, "10 min": 600, "30 min": 1800, "All": None}
        win = win_map.get(self._win_combo.currentText())

        def windowed(buf):
            vals = list(buf)
            if win is None or not t_all:
                return t_all, vals
            cutoff = t_all[-1] - win
            idx = next((i for i, t in enumerate(t_all) if t >= cutoff), 0)
            return t_all[idx:], vals[idx:]

        buf_map = {
            "Power (W)":  self._power_buf,
            "HR (bpm)":   self._hr_buf,
            "Cadence":    self._cadence_buf,
            "W/kg":       self._wkg_buf,
            "Speed km/h": self._speed_buf,
        }
        for name, curve in self._curves.items():
            if not self._series_cbs[name].isChecked():
                curve.setData([], [])
                continue
            t_w, v_w = windowed(buf_map[name])
            pairs = [(t, v) for t, v in zip(t_w, v_w) if v is not None]
            if pairs:
                ts, vs = zip(*pairs)
                curve.setData(list(ts), list(vs))
            else:
                curve.setData([], [])

    # ── Structured workout plan ───────────────────────────────────────────────

    def _on_build_workout(self):
        dlg = WorkoutBuilderDialog(self._workout_plan, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._workout_plan = dlg.result_plan
            total_s = sum(s.duration_s for s in self._workout_plan)
            h, rem  = divmod(total_s, 3600)
            m, s    = divmod(rem, 60)
            t_str   = f"{h}h {m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
            n       = len(self._workout_plan)
            self._status.showMessage(
                f"Workout loaded: {n} segments  ·  {t_str} total"
                + ("  ·  Press  ▶ Start Workout  when connected" if not self._btn_disc.isEnabled() else "")
            )
            # Enable start button if already connected
            self._btn_start.setEnabled(bool(self._workout_plan) and self._btn_disc.isEnabled())

    def _start_plan(self):
        if not self._workout_plan:
            return
        self._plan_idx = 0
        self._plan_bar_frame.setVisible(True)
        self._plan_timer.start()
        self._enter_segment(0)

    def _enter_segment(self, idx: int):
        if idx >= len(self._workout_plan):
            self._finish_plan()
            return
        self._plan_idx    = idx
        self._segment_start = datetime.now()
        seg = self._workout_plan[idx]

        # Send FTMS control commands to bike
        self._ble.request_control()
        if seg.target_type == "Power":
            self._ble.send_target_power(seg.target_value)
        elif seg.target_type == "HR":
            self._ble.send_target_hr(seg.target_value)
        elif seg.target_type == "Cadence":
            self._ble.send_target_cadence(seg.target_value)
        self._ble.ftms_start()

        self._update_plan_display(0.0)

    def _tick_plan(self):
        if self._plan_idx < 0 or not self._segment_start:
            return
        elapsed = (datetime.now() - self._segment_start).total_seconds()
        seg = self._workout_plan[self._plan_idx]
        if elapsed >= seg.duration_s:
            self._enter_segment(self._plan_idx + 1)
        else:
            self._update_plan_display(elapsed)

    def _update_plan_display(self, seg_elapsed: float):
        idx = self._plan_idx
        if idx < 0 or idx >= len(self._workout_plan):
            return
        seg  = self._workout_plan[idx]
        remaining = max(0.0, seg.duration_s - seg_elapsed)
        m, s = divmod(int(remaining), 60)

        # Colour by segment type
        self._seg_name_lbl.setText(seg.name.upper())
        self._seg_name_lbl.setStyleSheet(
            f"color:{seg.color};font-size:16px;font-weight:900;"
            f"letter-spacing:2px;border:none;background:transparent;"
        )
        self._seg_idx_lbl.setText(f"Segment {idx + 1} / {len(self._workout_plan)}")
        self._seg_countdown_lbl.setText(f"{m:02d}:{s:02d}")
        self._seg_target_lbl.setText(f"Target: {seg.target_str}")

        # Next segment preview
        if idx + 1 < len(self._workout_plan):
            nxt = self._workout_plan[idx + 1]
            self._seg_next_lbl.setText(f"Next → {nxt.name}  {nxt.duration_str}  {nxt.target_str}")
        else:
            self._seg_next_lbl.setText("Last segment")

        # Overall workout progress bar
        total_s  = sum(s.duration_s for s in self._workout_plan)
        done_s   = sum(self._workout_plan[i].duration_s for i in range(idx)) + seg_elapsed
        progress = int(done_s / total_s * 1000) if total_s else 0
        self._seg_progress.setValue(progress)

    def _stop_plan(self):
        self._plan_timer.stop()
        self._plan_idx = -1
        self._segment_start = None
        self._plan_bar_frame.setVisible(False)
        self._ble.ftms_stop()

    def _finish_plan(self):
        self._plan_timer.stop()
        self._plan_idx = -1
        self._segment_start = None
        self._plan_bar_frame.setVisible(False)
        self._ble.ftms_stop()
        self._status.showMessage("✓  Workout complete!")
        QMessageBox.information(self, "Workout Complete",
                                "All segments finished!\n\nPress 💾 Save to record this session.")

    # ── Save ──────────────────────────────────────────────────────────────────

    def _on_disconnect(self):
        self._ble.disconnect()

    def _on_save(self):
        result = self._save_workout()
        if result:
            _, csv_path, history_path = result
            QMessageBox.information(
                self, "Workout Saved",
                f"Session data:\n  {csv_path.name}\n\n"
                f"History log:\n  {history_path.name}\n\n"
                f"Saved to:  {WORKOUTS_DIR}",
            )

    def _save_workout(self) -> tuple[Path, Path, Path] | None:
        if not self._session_data:
            return None
        WORKOUTS_DIR.mkdir(parents=True, exist_ok=True)
        ts_str = (self._start_time or datetime.now()).strftime("%Y%m%d_%H%M%S")

        powers   = [d["power_w"]     for d in self._session_data if "power_w"     in d]
        cadences = [d["cadence_rpm"] for d in self._session_data if "cadence_rpm" in d]
        last = self._session_data[-1]
        dur  = last.get("elapsed_seconds") or (
            (datetime.now() - self._start_time).total_seconds() if self._start_time else None
        )
        summary = {
            "date":             (self._start_time or datetime.now()).isoformat(),
            "rider":            self._rider_name or "",
            "label":            self._workout_label or "",
            "type":             self._workout_type,
            "notes":            self._workout_notes or "",
            "target_type":      self._target_type,
            "target_value":     self._target_value if self._target_type != "None" else None,
            "workout_plan":     [
                {"name": s.name, "duration_s": s.duration_s,
                 "target_type": s.target_type, "target_value": s.target_value}
                for s in self._workout_plan
            ],
            "device":           self._device_name,
            "weight_kg":        self._weight_kg,
            "duration_seconds": dur,
            "distance_m":       last.get("distance_m"),
            "total_calories":   last.get("calories"),
            "avg_power_w":      round(sum(powers)   / len(powers),   1) if powers   else None,
            "max_power_w":      max(powers)                              if powers   else None,
            "avg_wkg":          round(sum(powers) / len(powers) / self._weight_kg, 2)
                                    if powers and self._weight_kg else None,
            "avg_cadence_rpm":  round(sum(cadences) / len(cadences), 1) if cadences else None,
            "max_cadence_rpm":  max(cadences)                           if cadences else None,
            "samples":          len(self._session_data),
        }

        json_path = WORKOUTS_DIR / f"workout_{ts_str}.json"
        json_path.write_text(json.dumps({"summary": summary, "data": self._session_data}, indent=2))

        csv_path, history_path = self._save_csv(ts_str, summary)
        self._status.showMessage(f"Saved  →  {csv_path.name}  |  {history_path.name}")
        return json_path, csv_path, history_path

    def _save_csv(self, ts_str: str, summary: dict) -> tuple[Path, Path]:
        data_path = WORKOUTS_DIR / f"workout_{ts_str}.csv"
        data_cols = ["timestamp", "elapsed_s", "power_w", "cadence_rpm",
                     "speed_kmh", "heart_rate", "distance_m", "calories", "wkg"]
        with data_path.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=data_cols, extrasaction="ignore")
            w.writeheader()
            for d in self._session_data:
                pw = d.get("power_w")
                w.writerow({
                    "timestamp":   d.get("ts", ""),
                    "elapsed_s":   d.get("elapsed_seconds", ""),
                    "power_w":     pw if pw is not None else "",
                    "cadence_rpm": d.get("cadence_rpm", ""),
                    "speed_kmh":   d.get("speed_kmh", ""),
                    "heart_rate":  d.get("heart_rate", ""),
                    "distance_m":  d.get("distance_m", ""),
                    "calories":    d.get("calories", ""),
                    "wkg":         round(pw / self._weight_kg, 3)
                                   if pw is not None and self._weight_kg else "",
                })

        history_path = WORKOUTS_DIR / "workout_history.csv"
        history_cols = ["date", "rider", "label", "type", "notes",
                        "duration_s", "distance_km", "calories",
                        "avg_power_w", "max_power_w", "avg_wkg",
                        "avg_cadence_rpm", "max_cadence_rpm",
                        "weight_kg", "target_type", "target_value",
                        "device", "samples"]
        write_header = not history_path.exists()
        with history_path.open("a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=history_cols)
            if write_header:
                w.writeheader()
            dist = summary.get("distance_m")
            w.writerow({
                "date":            summary.get("date", "")[:19].replace("T", " "),
                "rider":           summary.get("rider", ""),
                "label":           summary.get("label", ""),
                "type":            summary.get("type", ""),
                "notes":           summary.get("notes", ""),
                "duration_s":      round(summary["duration_seconds"], 1) if summary.get("duration_seconds") else "",
                "distance_km":     round(dist / 1000, 3) if dist else "",
                "calories":        summary.get("total_calories", ""),
                "avg_power_w":     summary.get("avg_power_w", ""),
                "max_power_w":     summary.get("max_power_w", ""),
                "avg_wkg":         summary.get("avg_wkg", ""),
                "avg_cadence_rpm": summary.get("avg_cadence_rpm", ""),
                "max_cadence_rpm": summary.get("max_cadence_rpm", ""),
                "weight_kg":       summary.get("weight_kg", ""),
                "target_type":     summary.get("target_type", ""),
                "target_value":    summary.get("target_value", ""),
                "device":          summary.get("device", ""),
                "samples":         summary.get("samples", ""),
            })
        return data_path, history_path

    def _on_history(self):
        HistoryDialog(self).exec()

    def closeEvent(self, event):
        if self._session_data:
            self._save_workout()
        self._stop_plan()
        self._ble.disconnect()
        event.accept()


# ─── Entry point ──────────────────────────────────────────────────────────────

def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Echo Bike Tracker")
    win = EchoBikeWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
