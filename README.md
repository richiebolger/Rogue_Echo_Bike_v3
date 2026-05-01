# Rogue Echo Bike v3 — Workout Tracker

A native macOS app for tracking workouts on the **Rogue Echo Bike v3** (and other FTMS-compatible assault bikes) via Bluetooth Low Energy.

> Built because the official Rogue app doesn't support the Echo Bike. This app connects directly to the bike's FTMS Bluetooth service and gives you live metrics, structured workouts, training zones, personal bests, and more.

---

## Screenshots

| Live Dashboard | Workout Builder | Post-Session Summary |
|---|---|---|
| Real-time power, HR, cadence, zones | Segment editor with 14 templates | Stats, zones, VO2max estimates |

---

## Features

- **Live metrics** — Power (W), W/kg, Cadence (rpm), Speed (km/h), Heart Rate (bpm), Calories, Distance, Elapsed time
- **Training zones** — Power zones Z1–Z7 (% FTP, Coggan) and HR zones Z1–Z6 (Heart Rate Reserve / Karvonen), updated live on each metric card
- **Min / Mean / Max** — Session stats shown directly on each card as you ride
- **Structured workout builder** — Build interval sessions from segments; 14 built-in templates (Tabata, FTP Test, Ramp Test, Pyramid, VO2 Max intervals, and more)
- **FTMS Control Point** — Sends target power / HR / cadence to the bike console (if supported by your firmware)
- **Rider profiles** — Per-rider weight, age, resting HR, and FTP stored locally; select from Clare / Richie / Ross / Guest dropdown
- **Personal bests** — Tracks max power, best avg power, best W/kg, max HR, and lifetime distance / calories / time per rider
- **VO2max estimates** — Power-based (Hawley & Noakes) and HR-based (Uth formula) shown after each session
- **Session export** — Per-session CSV + cumulative history CSV + full JSON saved to `~/echo_bike_workouts/`
- **Workout history** — Browse past sessions and personal bests in-app

---

## Requirements

| Dependency | Version |
|---|---|
| macOS | 12 Monterey or later (BLE requirement) |
| Python | 3.9 or later |
| PyQt6 | ≥ 6.5 |
| pyqtgraph | ≥ 0.13 |
| bleak | ≥ 0.21 |

> **Anaconda / conda users:** install into your conda environment, not the system Python — see the install note below.

---

## Installation

### 1 — Clone the repo

```bash
git clone https://github.com/richiebolger/Rogue_Echo_Bike_v3.git
cd Rogue_Echo_Bike_v3
```

### 2 — Install Python dependencies

**Standard Python (recommended):**
```bash
pip3 install PyQt6 pyqtgraph bleak
```

**Anaconda / conda:**
```bash
# Make sure PySide6 is NOT installed — it conflicts with PyQt6
pip uninstall PySide6 PySide6-Addons PySide6-Essentials shiboken6 -y 2>/dev/null
pip install PyQt6 pyqtgraph bleak
```

### 3 — Grant Bluetooth permission

On first run macOS will ask for Bluetooth access — click **Allow**.  
If the prompt doesn't appear: **System Settings → Privacy & Security → Bluetooth** → add Terminal (or your Python interpreter).

---

## Running

```bash
python3 gui_tracker.py
```

If you use Anaconda and `python3` points to the system Python, either activate your conda environment first or use the full path:

```bash
/Users/YOUR_USERNAME/opt/anaconda3/bin/python3 gui_tracker.py
```

You can also add an alias to `~/.zshrc`:
```bash
echo 'alias python3=/Users/YOUR_USERNAME/opt/anaconda3/bin/python3' >> ~/.zshrc
```

---

## How to use

### Connecting to the bike

1. Turn on the Echo Bike console
2. **Hold the Bluetooth button** on the console for ~3 seconds until the BT icon flashes
3. Press **⟳ Scan** in the app
4. Press **⚡ Connect** when your bike appears
5. Start pedalling — metrics will appear immediately

### Rider profiles

- Select your name from the **RIDER** dropdown in the setup bar
- Click **⚙** to edit your profile: weight, age, resting HR, and FTP
- These values power the training zone display and VO2max estimates
- Profiles are saved to `~/echo_bike_workouts/profiles.json`

> **FTP (Functional Threshold Power):** the highest average power you can sustain for ~60 minutes. If you don't know yours yet, use the **FTP Test** workout template to estimate it (take 95% of your 20-minute average power).

### Training zones

| Zone | Power (% FTP) | HR (% HRR) | Feel |
|---|---|---|---|
| Z1 Recovery | < 55% | < 50% | Easy spin |
| Z2 Endurance | 55–75% | 50–60% | Conversational |
| Z3 Tempo | 75–90% | 60–70% | Comfortably hard |
| Z4 Threshold | 90–105% | 70–80% | Hard, sustainable ~60 min |
| Z5 VO2 Max | 105–120% | 80–90% | Very hard, 3–8 min efforts |
| Z6 Anaerobic | 120–150% | 90%+ | Short, maximal efforts |
| Z7 Sprint | > 150% | — | All-out, seconds only |

HR zones use the **Karvonen / Heart Rate Reserve** method: set your resting HR and the app calculates zones relative to your reserve (Max HR − Resting HR). Max HR defaults to 220 − age; you can override it in the profile editor.

### Workout builder

1. Click **🏋 Build Workout**
2. Choose a template from the dropdown and click **Load Template**, or build from scratch
3. Adjust segment durations and target values
4. Click **✓ Load Workout** then **▶ Start Workout** (must be connected)

The app sends target power / cadence / HR to the bike console via the FTMS Control Point characteristic. Not all firmware versions honour these writes — the app falls back gracefully if unsupported.

### Saving & exporting

Click **💾 Save** after your ride. A summary dialog shows:
- Full session stats with personal best callouts 🏆
- Time in each training zone (power + HR)
- VO2max estimates

Three files are saved to `~/echo_bike_workouts/`:

| File | Contents |
|---|---|
| `workout_YYYYMMDD_HHMMSS.json` | Full session data + summary |
| `workout_YYYYMMDD_HHMMSS.csv` | Per-sample data (1 row/notification) |
| `workout_history.csv` | Cumulative one-row-per-session log |

### Workout history & personal bests

Click **📋 History** to browse past sessions and view lifetime personal bests per rider.

---

## Workout templates

| Template | Description |
|---|---|
| Tabata (20s/10s × 8) | Classic Tabata — brutal, 4 min of work |
| 30/30 Intervals × 10 | 30s hard / 30s easy, 10 rounds |
| 40/20 Intervals × 8 | 40s work / 20s rest |
| Steady State 20 min | Sustained aerobic effort |
| Zone 2 45 min Easy | Long easy aerobic session |
| 10-20-30 × 5 rounds | 30s easy / 20s moderate / 10s sprint |
| Pyramid (1-2-3-2-1 min) | Ascending then descending intervals |
| EMOM (40s/20s × 12) | Every minute on the minute |
| 4×8 min Threshold | Classic threshold work |
| 6×1 min VO2 Max | Short, very hard VO2 max efforts |
| Sprint Repeats 8×20s | Max-effort sprint repeats |
| 5×5 min Tempo | Tempo/sweetspot intervals |
| FTP Test (20 min) | Guided 20-min FTP test (use 95% of avg power) |
| Ramp Test | 1-min steps of increasing power to exhaustion |

---

## Data files

All data is stored locally on your machine:

```
~/echo_bike_workouts/
├── profiles.json           # Rider profiles (weight, age, FTP, HR)
├── personal_bests.json     # Per-rider PBs and lifetime totals
├── workout_history.csv     # Cumulative session log
├── workout_20250501_*.json # Individual session files
└── workout_20250501_*.csv  # Individual session CSVs
```

---

## Technical notes

The Rogue Echo Bike v3 uses the **FTMS (Fitness Machine Service)** Bluetooth profile, a standard BLE protocol:

| UUID | Characteristic | Direction |
|---|---|---|
| `00002ad2-...` | Indoor Bike Data | Bike → App (notifications) |
| `00002ad9-...` | FTMS Control Point | App → Bike (write) |
| `00002a37-...` | Heart Rate Measurement | Sensor → App |

FTMS control writes (target power, cadence, HR) are fire-and-forget — the app works fully even if your firmware doesn't implement the control point.

---

## Troubleshooting

**"No Echo Bike found" after scanning**
- Make sure the console is on and Bluetooth is active (BT icon visible)
- Hold the Bluetooth button for ~3 seconds to put the console into pairing mode
- Check macOS Bluetooth is on and the app has permission

**"Qt platform plugin not found" error**
- This means PyQt6 is installed in a different Python than the one you're running
- Make sure you installed with the same `python3` you use to run the script

**Metrics showing "--" after connecting**
- Start pedalling — the bike only broadcasts when in use
- Check the FTMS service UUID matches your console firmware

---

## Contributing

Pull requests welcome. If you have a different assault bike model that uses FTMS, open an issue with the BLE service UUIDs and any protocol differences — happy to add support.

---

## Licence

MIT — free to use, modify, and distribute.
