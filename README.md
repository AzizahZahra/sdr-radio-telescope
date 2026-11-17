# SDR Radio Telescope – Mount Control

Control software for a portable alt-azimuth radio telescope. A Python desktop app points the dish at the Sun, the Moon, bright radio sources or the Milky Way (HI 21 cm) and keeps it tracking, while the radio data are recorded with any SDR program (SDR#, SDR++, …).

## Features

- **Sky chart**: interactive star chart (stereographic projection) with constellations, an equatorial grid, the Milky Way band and the telescope reticle. Zoom, drag, and click an object to select it.
- **Go to & tracking**: slew to any catalogue object and follow it automatically (position update every second, 0.05° deadband).
- **Object catalogue**: Sun, Moon, bright radio sources (Cas A, Cyg A, Tau A, Sgr A*, Vir A, Cen A, …), Milky Way points every 30° of galactic longitude, plus your own objects in RA/Dec or galactic l/b.
- **Manual control**: jog in steps of 0.1° / 1° / 5° / 10°, go to Az/El, return to HOME, emergency STOP.
- **Works offline**: no internet needed in the field (astropy IERS downloads are disabled).
- **Simulation mode**: try everything without hardware by choosing the `SIMULATION` port.
- **Remembers its position**: the last mount position is saved and restored after a restart or an Arduino reset.

## Hardware

| Part | Notes |
|---|---|
| Arduino Uno | runs `mount_control/mount_control.ino` |
| 2× TB6600 stepper driver | DIP switches set to 1600 pulses/rev (microstep 8) |
| 2× NEMA 23 stepper motor | azimuth and elevation |
| Azimuth gearbox 1:90 | 400 steps/degree |
| Elevation gearbox 1:20 | ≈ 88.9 steps/degree |
| SDR receiver + dish/antenna | data recorded with SDR#, SDR++, etc. |

### Wiring (TB6600, common cathode)

| TB6600 | Arduino |
|---|---|
| PUL−, DIR−, ENA− | GND |
| Azimuth PUL+ / DIR+ | D2 / D3 |
| Elevation PUL+ / DIR+ | D4 / D5 |
| ENA+ | not connected (driver enabled by default) |

## Installation

Requires Python 3.10+.

```bash
git clone https://github.com/AzizahZahra/sdr-radio-telescope
cd sdr-radio-telescope
python -m venv venv
venv\Scripts\activate          # Windows  (Linux/macOS: source venv/bin/activate)
pip install -r requirements.txt
python main.py
```

### Firmware

1. Open `mount_control/mount_control.ino` in the Arduino IDE.
2. Install the **AccelStepper** library (Library Manager).
3. Select *Arduino Uno* and upload.

### Build a Windows .exe

Run `build_exe.bat`. It creates `dist\RadioTelescope.exe` and copies `control.py` next to it. Edit `dist\control.py` to change the telescope's behaviour without rebuilding; it is loaded at the next start.

## First use

1. **Observing site**: on first start, enter latitude, longitude, height and time zone (south latitude and west longitude are negative).
2. **Connect**: pick the Arduino's COM port (or `SIMULATION`) and press *Connect*.
3. **Set HOME**: point the dish **North and straight up (zenith)**, open the settings panel (☰, top left) and press *Set HOME*. This defines Az 0°, El 90°.
4. **Observe**: select an object in the list or on the chart, then press *Go to* or switch on *Track*.
5. **Record**: start recording in your SDR program.

## Configuration

Main settings are at the top of `control.py`:

| Setting | Default | Meaning |
|---|---|---|
| `PULSES_PER_REV` | 1600 | TB6600 pulses per motor revolution |
| `GEAR_RATIO_AZ` / `GEAR_RATIO_EL` | 90 / 20 | gearbox ratios |
| `AZ_DIR` / `EL_DIR` | −1 / −1 | motor direction |
| `MAX_SPS` | 3000 | max steps/s per motor (≤ `MAX_SPEED_LIMIT` in the firmware) |
| `DEFAULT_SPEED_AZ` / `_EL` | 3 / 10 °/s | slew speed |
| `AZ_M_LIMIT` | `None` | azimuth cable-wrap limit in degrees (e.g. `270.0`); `None` = unlimited |
| `TRACK_INTERVAL` / `TRACK_DEADBAND` | 1 s / 0.05° | tracking update period and threshold |

Files created next to the program: `site_config.json`, `mount_state.json`, `user_objects.json`, `telescope_log.txt`.

## Serial protocol

115200 baud, ASCII, lines end with `\n`.

| Command | Reply | Action |
|---|---|---|
| `M <az_steps> <el_steps>` | `OK` when both motors arrive | move to absolute position |
| `P` | `POS <az> <el>` | report current position |
| `V <az_sps> <el_sps>` | `SPEED <az> <el>` | set max speed (steps/s) |
| `STOP` | `STOPPED` | stop both motors immediately |

On power-up/reset the Arduino sends `READY`. Invalid input returns `ERR CMD`, `ERR ARG` or `ERR LEN`.

## Project structure

```
main.py                         GUI (customtkinter): sky chart, controls, object list
control.py                      mount configuration, coordinates, serial link, tracker
sky_data.py                     bright stars, constellation lines and labels
mount_control/mount_control.ino Arduino firmware
build_exe.bat                   builds RadioTelescope.exe with PyInstaller
THIRD_PARTY_NOTICES.txt         licences of the components used
```

## Safety

Position is counted from motor steps only (no encoders or limit switches). Always check the dish after a power loss or a hard stop, and use **Set HOME** again if the position looks wrong. A physical emergency switch that cuts motor power is strongly recommended.

## Credits

Star and constellation data: [d3-celestial](https://github.com/ofrohn/d3-celestial) by Olaf Frohn (BSD-3-Clause), derived from the Hipparcos catalogue. See `THIRD_PARTY_NOTICES.txt` for all third-party components.
