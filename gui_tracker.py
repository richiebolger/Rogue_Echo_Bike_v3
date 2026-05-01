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

Usage:
    python3 gui_tracker.py

Install deps first:
    python3 -m pip install PyQt6 pyqtgraph bleak
"""

import asyncio
import csv
import json
import struct
import sys
import threading
from collections import deque
from datetime import datetime
from pathlib import Path

import pyqtgraph as pg
from bleak import BleakClient, BleakScanner
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QObject
from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QDoubleSpinBox, QFrame, QGridLayout, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QListWidget, QMainWindow, QMessageBox, QPushButton,
    QSizePolicy, QSpinBox, QStatusBar, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

# ─── FTMS UUIDs ───────────────────────────────────────────────────────────────
FTMS_SERVICE_UUID  = "00001826-0000-1000-8000-00805f9b34fb"
INDOOR_BIKE_DATA   = "00002ad2-0000-1000-8000-00805f9b34fb"
HEART_RATE_UUID    = "00002a37-0000-1000-8000-00805f9b34fb"

WORKOUTS_DIR       = Path.home() / "echo_bike_workouts"
ECHO_BIKE_KEYWORDS = {"rogue", "echo", "bike", "assault"}
MAX_CHART_POINTS   = 1800   # 30 min at 1 Hz

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


# ─── FTMS parser ──────────────────────────────────────────────────────────────

def parse_indoor_bike_data(raw: bytes) -> dict:
    """Decode FTMS Indoor Bike Data characteristic (0x2AD2)."""
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
        v = rd("<h");  result["resistance"]       = v
    if flags & 0x0040:
        v = rd("<h");  result["power_w"]          = v
    if flags & 0x0080:
        v = rd("<h");  result["avg_power_w"]      = v
    if flags & 0x0100:
        t = rd("<H");  h = rd("<H");  m = rd("<B")
        if t is not None: result["calories"]            = t
        if h is not None: result["calories_per_hour"]   = h
        if m is not None: result["calories_per_minute"] = m
    if flags & 0x0200:
        v = rd("<B");  result["heart_rate"]       = v
    if flags & 0x0400:
        rd("<B")
    if flags & 0x0800:
        v = rd("<H");  result["elapsed_seconds"]  = v
    if flags & 0x1000:
        v = rd("<H");  result["remaining_seconds"] = v
    return {k: v for k, v in result.items() if v is not None}


def parse_heart_rate(raw: bytes) -> int | None:
    if not raw:
        return None
    return struct.unpack_from("<H", raw, 1)[0] if (raw[0] & 0x01) and len(raw) >= 3 else (raw[1] if len(raw) >= 2 else None)


# ─── BLE worker (background asyncio thread) ───────────────────────────────────

class BleWorker(QObject):
    data_received = pyqtSignal(dict)
    connected     = pyqtSignal(str)
    disconnected  = pyqtSignal()
    error         = pyqtSignal(str)
    scan_result   = pyqtSignal(list)

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

    def scan(self):        self._go(self._scan())
    def connect_to(self, address: str): self._go(self._connect(address))
    def disconnect(self):               self._go(self._disconnect())

    async def _scan(self):
        try:
            devs = await BleakScanner.discover(timeout=10.0, service_uuids=[FTMS_SERVICE_UUID])
            if not devs:
                all_devs = await BleakScanner.discover(timeout=8.0)
                devs = [d for d in all_devs
                        if d.name and any(k in d.name.lower() for k in ECHO_BIKE_KEYWORDS)]
            self.scan_result.emit([(d.name or "Unknown", d.address) for d in devs])
        except Exception as e:
            self.error.emit(str(e))

    async def _connect(self, address: str):
        try:
            self._client = BleakClient(
                address, timeout=20.0,
                disconnected_callback=lambda _: self.disconnected.emit(),
            )
            await self._client.connect()
            self.connected.emit(address)
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

    def _on_bike(self, _s, raw: bytes):
        parsed = parse_indoor_bike_data(bytes(raw))
        if parsed:
            parsed["ts"] = datetime.now().isoformat()
            self.data_received.emit(parsed)

    def _on_hr(self, _s, raw: bytes):
        hr = parse_heart_rate(bytes(raw))
        if hr is not None:
            self.data_received.emit({"heart_rate": hr, "ts": datetime.now().isoformat()})


# ─── Metric card ──────────────────────────────────────────────────────────────

class MetricCard(QFrame):
    def __init__(self, label: str, unit: str, color: str, big: bool = False):
        super().__init__()
        self._color = color
        self.setStyleSheet(f"""
            MetricCard {{
                background: {CARD_BG};
                border: 1px solid {CARD_BORDER};
                border-radius: 14px;
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


# ─── Device picker ────────────────────────────────────────────────────────────

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
        btns.accepted.connect(self._accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def _accept(self):
        r = self._list.currentRow()
        if r >= 0:
            self.selected = self._devices[r]
        self.accept()


# ─── History dialog ───────────────────────────────────────────────────────────

class HistoryDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Workout History")
        self.setStyleSheet(f"background:{BG};color:{COL_WHITE};")
        self.resize(1100, 460)

        lay = QVBoxLayout(self)
        lay.setSpacing(10)

        # ── Tab bar: table vs CSV location ───────────────────────────────────
        info = QLabel(f"Files saved to:  {WORKOUTS_DIR}")
        info.setStyleSheet(f"color:{COL_MUTED};font-size:11px;")
        lay.addWidget(info)

        cols = [
            "Date", "Label", "Type", "Duration",
            "Distance", "Calories", "Avg W", "Max W",
            "Avg W/kg", "Avg RPM", "Notes",
        ]
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
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)   # Label stretches
        hdr.setSectionResizeMode(10, QHeaderView.ResizeMode.Stretch)  # Notes stretches
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
            tbl.setItem(row,  1, cell(s.get("label", "") or "--",
                                      align=Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter))
            tbl.setItem(row,  2, cell(s.get("type", "--")))
            tbl.setItem(row,  3, cell(dur_str))
            tbl.setItem(row,  4, cell(f"{dist/1000:.2f} km" if dist else "--"))
            tbl.setItem(row,  5, cell(f"{s['total_calories']:.0f}" if s.get("total_calories") else "--", COL_CALS))
            tbl.setItem(row,  6, cell(f"{s['avg_power_w']}" if s.get("avg_power_w") else "--", COL_POWER))
            tbl.setItem(row,  7, cell(f"{s['max_power_w']}" if s.get("max_power_w") else "--", COL_POWER))
            tbl.setItem(row,  8, cell(f"{s['avg_wkg']}" if s.get("avg_wkg") else "--", COL_WKG))
            tbl.setItem(row,  9, cell(f"{s['avg_cadence_rpm']}" if s.get("avg_cadence_rpm") else "--", COL_CADENCE))
            tbl.setItem(row, 10, cell(s.get("notes", "") or "",
                                      align=Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter))

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
        self.resize(1160, 780)
        self.setMinimumSize(900, 640)

        self._metrics:      dict       = {}
        self._session_data: list[dict] = []
        self._start_time:   datetime | None = None
        self._weight_kg:    float      = 75.0
        self._device_name:  str        = ""
        self._scan_results: list[tuple[str, str]] = []

        # Workout metadata (filled in setup panel before connecting)
        self._workout_label:  str   = ""
        self._workout_type:   str   = "Free Ride"
        self._workout_notes:  str   = ""
        self._target_type:    str   = "None"
        self._target_value:   float = 0.0

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
        lay.setSpacing(14)
        lay.addLayout(self._build_header())
        lay.addWidget(self._build_workout_setup())
        lay.addLayout(self._build_cards(),        stretch=2)
        lay.addWidget(self._build_chart_panel(),  stretch=3)
        lay.addLayout(self._build_controls())

        sb = QStatusBar()
        sb.setStyleSheet(f"color:{COL_MUTED};font-size:11px;background:{BG};")
        self.setStatusBar(sb)
        self._status = sb
        self._status.showMessage("Not connected  ·  Press  ⟳ Scan  to find your Echo Bike")

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

    def _build_workout_setup(self) -> QFrame:
        frame = QFrame()
        frame.setStyleSheet(
            f"QFrame{{background:{CARD_BG};border:1px solid {CARD_BORDER};border-radius:10px;}}"
        )
        row = QHBoxLayout(frame)
        row.setContentsMargins(14, 8, 14, 8)
        row.setSpacing(16)

        lbl_style = f"color:{COL_MUTED};font-size:11px;font-weight:700;letter-spacing:1px;"
        field_style = f"""
            QLineEdit, QComboBox, QSpinBox {{
                background:#0d0d1a;color:{COL_WHITE};
                border:1px solid {CARD_BORDER};border-radius:6px;
                padding:3px 8px;font-size:12px;
            }}
            QComboBox::drop-down {{border:none;width:14px;}}
            QComboBox QAbstractItemView {{
                background:#1a1a38;color:{COL_WHITE};
                selection-background-color:{COL_POWER};
            }}
        """

        # Label
        row.addWidget(self._muted_label("LABEL"))
        self._lbl_input = QLineEdit()
        self._lbl_input.setPlaceholderText("e.g. Morning HIIT")
        self._lbl_input.setFixedWidth(160)
        self._lbl_input.setStyleSheet(field_style)
        self._lbl_input.textChanged.connect(lambda t: setattr(self, "_workout_label", t))
        row.addWidget(self._lbl_input)

        # Type
        row.addWidget(self._muted_label("TYPE"))
        self._type_combo = QComboBox()
        self._type_combo.addItems([
            "Free Ride", "HIIT", "Steady State", "Intervals",
            "Threshold", "Recovery", "Race", "Test", "Custom",
        ])
        self._type_combo.setFixedWidth(130)
        self._type_combo.setStyleSheet(field_style)
        self._type_combo.currentTextChanged.connect(lambda t: setattr(self, "_workout_type", t))
        row.addWidget(self._type_combo)

        # Notes
        row.addWidget(self._muted_label("NOTES"))
        self._notes_input = QLineEdit()
        self._notes_input.setPlaceholderText("Optional notes…")
        self._notes_input.setStyleSheet(field_style)
        self._notes_input.textChanged.connect(lambda t: setattr(self, "_workout_notes", t))
        row.addWidget(self._notes_input, stretch=1)

        # Target type
        row.addWidget(self._muted_label("TARGET"))
        self._target_combo = QComboBox()
        self._target_combo.addItems(["None", "Time (min)", "Distance (km)", "Calories", "Power (W)"])
        self._target_combo.setFixedWidth(130)
        self._target_combo.setStyleSheet(field_style)
        self._target_combo.currentTextChanged.connect(self._on_target_type_changed)
        row.addWidget(self._target_combo)

        # Target value
        self._target_spin = QSpinBox()
        self._target_spin.setRange(0, 9999)
        self._target_spin.setValue(0)
        self._target_spin.setFixedWidth(80)
        self._target_spin.setStyleSheet(field_style)
        self._target_spin.setEnabled(False)
        self._target_spin.valueChanged.connect(lambda v: setattr(self, "_target_value", float(v)))
        row.addWidget(self._target_spin)

        return frame

    def _muted_label(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(
            f"color:{COL_MUTED};font-size:10px;font-weight:700;"
            f"letter-spacing:1px;border:none;background:transparent;"
        )
        return lbl

    def _on_target_type_changed(self, text: str):
        self._target_type = text
        self._target_spin.setEnabled(text != "None")
        hints = {
            "None": (0, 9999, 0),
            "Time (min)": (1, 300, 20),
            "Distance (km)": (1, 200, 5),
            "Calories": (50, 5000, 500),
            "Power (W)": (50, 1000, 200),
        }
        lo, hi, default = hints.get(text, (0, 9999, 0))
        self._target_spin.setRange(lo, hi)
        self._target_spin.setValue(default)

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
        series_defs = [
            ("Power (W)",   COL_POWER),
            ("HR (bpm)",    COL_HR),
            ("Cadence",     COL_CADENCE),
            ("W/kg",        COL_WKG),
            ("Speed km/h",  COL_SPEED),
        ]
        for name, color in series_defs:
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
            offset=(10, 10),
            labelTextColor=COL_WHITE,
            pen=pg.mkPen(color=CARD_BORDER),
            brush=pg.mkBrush(color="#0d0d1aCC"),
        )

        self._curves: dict[str, pg.PlotDataItem] = {}
        for name, color, w in [
            ("Power (W)",  COL_POWER,   2.5),
            ("HR (bpm)",   COL_HR,      2.0),
            ("Cadence",    COL_CADENCE, 2.0),
            ("W/kg",       COL_WKG,     2.0),
            ("Speed km/h", COL_SPEED,   2.0),
        ]:
            self._curves[name] = self._plot.plot(
                pen=pg.mkPen(color=color, width=w), name=name
            )

        lay.addWidget(self._plot)
        return frame

    def _build_controls(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(10)

        def btn(text, color, slot):
            b = QPushButton(text)
            b.setStyleSheet(_btn_style(color))
            b.clicked.connect(slot)
            return b

        self._btn_scan = btn("⟳  Scan",       COL_CADENCE, self._on_scan)
        self._btn_conn = btn("⚡  Connect",    COL_POWER,   self._on_connect)
        self._btn_disc = btn("✕  Disconnect", COL_HR,      self._on_disconnect)
        self._btn_save = btn("💾  Save",       COL_CALS,    self._on_save)
        self._btn_hist = btn("📋  History",    COL_DIST,    self._on_history)

        self._btn_conn.setEnabled(False)
        self._btn_disc.setEnabled(False)
        self._btn_save.setEnabled(False)

        for b in [self._btn_scan, self._btn_conn, self._btn_disc,
                  self._btn_save, self._btn_hist]:
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
        self._ble.start()

    def _on_scan(self):
        self._btn_scan.setEnabled(False)
        self._btn_scan.setText("Scanning…")
        self._status.showMessage(
            "Scanning… console on? BT active? (hold BT button 3 sec)"
        )
        self._ble.scan()

    def _on_scan_result(self, devices: list):
        self._btn_scan.setEnabled(True)
        self._btn_scan.setText("⟳  Scan")
        if not devices:
            self._status.showMessage(
                "No Echo Bike found. Is the console on? (Hold BT button 3 sec)"
            )
            return
        if len(devices) == 1:
            self._device_name  = devices[0][0]
            self._scan_results = devices
            self._status.showMessage(
                f"Found:  {devices[0][0]}  ({devices[0][1]})  —  press Connect"
            )
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
        self._status.showMessage("Disconnected")

    def _on_ble_error(self, msg: str):
        self._btn_scan.setEnabled(True)
        self._btn_conn.setEnabled(bool(self._scan_results))
        self._status.showMessage(f"BLE error: {msg}")

    def _on_data(self, data: dict):
        self._metrics.update(data)
        self._session_data.append(data)

    # ── Refresh ───────────────────────────────────────────────────────────────

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
        self._c_speed.set_value(speed if speed is not None else "--", decimals=1 if speed is not None else 0)
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
            self._c_time.set_value(f"{int(et)//3600:02d}:{(int(et)%3600)//60:02d}:{int(et)%60:02d}")

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

    # ── Actions ───────────────────────────────────────────────────────────────

    def _on_disconnect(self):
        self._ble.disconnect()

    def _on_save(self):
        result = self._save_workout()
        if result:
            json_path, csv_path, history_path = result
            QMessageBox.information(
                self, "Workout Saved",
                f"Session data:\n  {csv_path.name}\n\n"
                f"Full JSON:\n  {json_path.name}\n\n"
                f"History log:\n  {history_path.name}\n\n"
                f"All saved to:\n  {WORKOUTS_DIR}",
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
            "label":            self._workout_label or "",
            "type":             self._workout_type,
            "notes":            self._workout_notes or "",
            "target_type":      self._target_type,
            "target_value":     self._target_value if self._target_type != "None" else None,
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

        # JSON (full data)
        json_path = WORKOUTS_DIR / f"workout_{ts_str}.json"
        json_path.write_text(json.dumps({"summary": summary, "data": self._session_data}, indent=2))

        # CSVs
        csv_path, history_path = self._save_csv(ts_str, summary)

        self._status.showMessage(f"Saved  →  {csv_path.name}  |  {history_path.name}")
        return json_path, csv_path, history_path

    def _save_csv(self, ts_str: str, summary: dict) -> tuple[Path, Path]:
        """Write per-session data CSV and append a row to the cumulative history CSV."""

        # ── Per-session data CSV ──────────────────────────────────────────────
        data_path = WORKOUTS_DIR / f"workout_{ts_str}.csv"
        data_cols = [
            "timestamp", "elapsed_s", "power_w", "cadence_rpm",
            "speed_kmh", "heart_rate", "distance_m", "calories", "wkg",
        ]
        with data_path.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=data_cols, extrasaction="ignore")
            writer.writeheader()
            for d in self._session_data:
                pw = d.get("power_w")
                writer.writerow({
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

        # ── Cumulative history CSV (one row per workout, appended) ────────────
        history_path = WORKOUTS_DIR / "workout_history.csv"
        history_cols = [
            "date", "label", "type", "notes",
            "duration_s", "distance_km", "calories",
            "avg_power_w", "max_power_w", "avg_wkg",
            "avg_cadence_rpm", "max_cadence_rpm",
            "weight_kg", "target_type", "target_value",
            "device", "samples",
        ]
        write_header = not history_path.exists()
        with history_path.open("a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=history_cols)
            if write_header:
                writer.writeheader()
            dist = summary.get("distance_m")
            writer.writerow({
                "date":             summary.get("date", "")[:19].replace("T", " "),
                "label":            summary.get("label", ""),
                "type":             summary.get("type", ""),
                "notes":            summary.get("notes", ""),
                "duration_s":       round(summary["duration_seconds"], 1) if summary.get("duration_seconds") else "",
                "distance_km":      round(dist / 1000, 3) if dist else "",
                "calories":         summary.get("total_calories", ""),
                "avg_power_w":      summary.get("avg_power_w", ""),
                "max_power_w":      summary.get("max_power_w", ""),
                "avg_wkg":          summary.get("avg_wkg", ""),
                "avg_cadence_rpm":  summary.get("avg_cadence_rpm", ""),
                "max_cadence_rpm":  summary.get("max_cadence_rpm", ""),
                "weight_kg":        summary.get("weight_kg", ""),
                "target_type":      summary.get("target_type", ""),
                "target_value":     summary.get("target_value", ""),
                "device":           summary.get("device", ""),
                "samples":          summary.get("samples", ""),
            })

        return data_path, history_path

    def _on_history(self):
        HistoryDialog(self).exec()

    def closeEvent(self, event):
        if self._session_data:
            self._save_workout()   # auto-save on close, ignore return value
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
