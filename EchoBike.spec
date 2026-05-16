# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for Echo Bike Tracker
Build:  pyinstaller EchoBike.spec
"""

import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

block_cipher = None

# Collect all bleak + PyQt6 data
datas  = collect_data_files("bleak")
datas += collect_data_files("pyqtgraph")

a = Analysis(
    ["gui_tracker.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "bleak",
        "bleak.backends.corebluetooth",
        "bleak.backends.corebluetooth.client",
        "bleak.backends.corebluetooth.scanner",
        "bleak.backends.corebluetooth.service",
        "bleak.backends.corebluetooth.characteristic",
        "bleak.backends.corebluetooth.descriptor",
        "PyQt6.QtCore",
        "PyQt6.QtGui",
        "PyQt6.QtWidgets",
        "PyQt6.sip",
        "pyqtgraph",
        "pyqtgraph.graphicsItems",
        "objc",
        "Foundation",
        "CoreBluetooth",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "unittest", "test", "distutils"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Echo Bike Tracker",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file="entitlements.plist",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Echo Bike Tracker",
)

app = BUNDLE(
    coll,
    name="Echo Bike Tracker.app",
    icon="assets/EchoBike.icns",
    bundle_identifier="com.richiebolger.echobiketracker",
    version="4.1.0",
    info_plist={
        # ── Identity ──────────────────────────────────────────────────────
        "CFBundleName":              "Echo Bike Tracker",
        "CFBundleDisplayName":       "Echo Bike Tracker",
        "CFBundleIdentifier":        "com.richiebolger.echobiketracker",
        "CFBundleVersion":           "4.1.0",
        "CFBundleShortVersionString":"4.1",
        "CFBundlePackageType":       "APPL",
        "CFBundleSignature":         "????",
        "NSPrincipalClass":          "NSApplication",
        "NSHighResolutionCapable":   True,
        "LSMinimumSystemVersion":    "12.0",
        "LSApplicationCategoryType": "public.app-category.sports",

        # ── Privacy strings (required by App Store for any API used) ──────
        "NSBluetoothAlwaysUsageDescription":
            "Echo Bike Tracker uses Bluetooth to connect to your Rogue Echo Bike "
            "and receive live workout metrics (power, cadence, heart rate, speed).",
        "NSBluetoothPeripheralUsageDescription":
            "Required to communicate with your Echo Bike's Bluetooth sensor.",

        # ── High DPI / Retina ─────────────────────────────────────────────
        "NSSupportsAutomaticGraphicsSwitching": True,

        # ── Copyright ─────────────────────────────────────────────────────
        "NSHumanReadableCopyright": "Copyright © 2025 Richie Bolger. All rights reserved.",
    },
)
