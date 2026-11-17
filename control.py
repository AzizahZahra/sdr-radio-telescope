#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
control.py - Radio telescope control core (no GUI code in this file).

Contains everything that talks to the mount:
  * mount configuration (gearbox, pulses/rev, motor direction, limits, speed)
  * coordinate conversions (mechanical <-> sky, RA/Dec <-> Az/El)
  * Mount       : serial link to mount_control.ino (Arduino Uno + TB6600)
  * Tracker     : keeps the mount pointed at a moving sky object

Data are recorded with any SDR program (SDR#, SDR++, ...).

The GUI (main.py) only calls the classes/functions in this file.
To change how the telescope behaves, edit this file. When the program runs as
RadioTelescope.exe, a copy of control.py placed next to the .exe is loaded
instead of the built-in one, so changes take effect after restarting the exe.
"""

import datetime as dt
import json
import math
import os
import sys
import threading
import time
import warnings

import numpy as np
import serial
import serial.tools.list_ports

import astropy.units as u
from astropy.coordinates import AltAz, EarthLocation, SkyCoord, get_body, get_sun
from astropy.time import Time
from astropy.utils import iers

warnings.filterwarnings("ignore")
# Never download IERS tables (the telescope is used offline in the field).
# The difference is a few arcseconds, far below the radio beam width.
iers.conf.auto_download = False
iers.conf.auto_max_age = None
for _mode in ("ignore", "warn"):       # accepted values differ between astropy versions
    try:
        iers.conf.iers_degraded_accuracy = _mode
        break
    except Exception:
        pass


def _app_dir():
    """Folder for settings/data: next to the .exe when frozen, else next to this file."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


APP_DIR = _app_dir()

# =============================================================================
# MOUNT CONFIGURATION
# =============================================================================
BAUD_RATE = 115200

PULSES_PER_REV = 1600          # TB6600 DIP "Pulse/rev" (200 steps x microstep 8)
GEAR_RATIO_AZ = 90.0           # azimuth gearbox 1:90
GEAR_RATIO_EL = 20.0           # elevation gearbox 1:20
AZ_DIR = -1                    # -1: positive azimuth = clockwise (North -> East)
EL_DIR = -1                    # -1: lower elevation tilts the dish toward the target azimuth

STEPS_PER_DEG_AZ = PULSES_PER_REV * GEAR_RATIO_AZ / 360.0   # 400.0 steps/deg
STEPS_PER_DEG_EL = PULSES_PER_REV * GEAR_RATIO_EL / 360.0   # 88.9  steps/deg

# ---- Speed ----
# MAX_SPS = pulse rate limit per motor. Must be <= MAX_SPEED_LIMIT in mount_control.ino.
MAX_SPS = 3000.0
DEFAULT_SPEED_AZ = 3.0         # deg/s
DEFAULT_SPEED_EL = 10.0        # deg/s
MIN_SPEED = 0.05               # deg/s

# ---- Mechanical axes (degrees) ----
# HOME = dish facing NORTH and pointing at the ZENITH -> Azimuth 0, Elevation 90.
# Mechanical azimuth is cumulative from HOME (may exceed 360). Set AZ_M_LIMIT
# (e.g. 270.0) to limit cable wrap; None = unlimited.
AZ_M_LIMIT = None
AZ_M_MIN = -AZ_M_LIMIT if AZ_M_LIMIT else float("-inf")
AZ_M_MAX = AZ_M_LIMIT if AZ_M_LIMIT else float("inf")
# Wide mechanical elevation range for manual moves, so the dish can still be driven
# to the zenith if the saved position is wrong. Sky targets are always 0..90.
EL_M_MIN, EL_M_MAX = -90.0, 180.0
HOME_AZ_M, HOME_EL_M = 0.0, 90.0
DEFAULT_AZ_CENTER = 0.0

EL_MIN, EL_MAX = 0.0, 90.0     # sky elevation limits for go to / tracking

STATE_FILE = os.path.join(APP_DIR, "mount_state.json")

# ---- Site ----
DEFAULT_LAT = -6.2000
DEFAULT_LON = 106.8167
DEFAULT_HEIGHT = 10.0
DEFAULT_TZ = 7.0               # hours from UTC
SITE_FILE = os.path.join(APP_DIR, "site_config.json")

# ---- Timing ----
TRACK_INTERVAL = 1.0           # s, tracking update period
TRACK_DEADBAND = 0.05          # deg, correction sent if error is larger
ON_TARGET_TOL = 0.3            # deg, tracker reports "on target" below this error
MOVE_TIMEOUT_MARGIN = 30.0     # s, extra time when waiting for "OK"
POLL_INTERVAL = 0.25           # s, position polling period

LOG_FILE = os.path.join(APP_DIR, "telescope_log.txt")
DEBUG_SERIAL = True            # write serial TX/RX to the log file
SIM_PORT = "SIMULATION"

# =============================================================================
# OBJECT CATALOG
# =============================================================================
# ("icrs", RA, Dec J2000) | ("gal", l, b in degrees) | ("body", solar-system body, None)
SUN = "Sun"
MOON = "Moon"
CUSTOM = "Custom (RA/Dec)"
MW_PREFIX = "Milky Way"
CAT_SOLAR = "Solar system"
CAT_RADIO = "Radio sources"
CAT_MW = "Milky Way (HI 21 cm)"
CAT_CUSTOM = "Custom"

CATALOG = {
    SUN: ("body", "sun", None),
    MOON: ("body", "moon", None),
    "Sgr A* (Galactic Center)": ("icrs", "17h45m40.04s", "-29d00m28.1s"),
    "Cas A": ("icrs", "23h23m24.0s", "+58d48m54s"),
    "Cyg A": ("icrs", "19h59m28.36s", "+40d44m02.1s"),
    "Tau A (Crab)": ("icrs", "05h34m31.94s", "+22d00m52.2s"),
    "Vir A (M87)": ("icrs", "12h30m49.42s", "+12d23m28.0s"),
    "Cen A": ("icrs", "13h25m27.6s", "-43d01m09s"),
    "Orion A (M42)": ("icrs", "05h35m17.3s", "-05d23m28s"),
    "W51": ("icrs", "19h23m43.9s", "+14d30m34s"),
    "Hydra A": ("icrs", "09h18m05.65s", "-12d05m44s"),
    "Her A": ("icrs", "16h51m08.15s", "+04d59m33s"),
    "Fornax A": ("icrs", "03h22m41.7s", "-37d12m30s"),
    "Pictor A": ("icrs", "05h19m49.7s", "-45d46m44s"),
    "3C 273": ("icrs", "12h29m06.70s", "+02d03m08.6s"),
}
for _l in range(0, 360, 30):
    CATALOG["%s l=%d°" % (MW_PREFIX, _l)] = ("gal", float(_l), 0.0)
CATALOG[CUSTOM] = ("icrs", "00h00m00s", "+00d00m00s")

# ---- user objects (saved in user_objects.json next to the program) ----
CAT_USER = "My objects"
USER_OBJ_FILE = os.path.join(APP_DIR, "user_objects.json")
USER_OBJECTS = []                       # names, in the order they were added


def _load_user_objects():
    try:
        with open(USER_OBJ_FILE, "r", encoding="utf-8") as fh:
            items = json.load(fh)
    except Exception:
        return
    for it in items:
        try:
            spec = (it["kind"], it["a"], it["b"])
            if spec[0] == "gal":
                spec = ("gal", float(spec[1]), float(spec[2]))
            CATALOG[it["name"]] = spec
            if it["name"] not in USER_OBJECTS:
                USER_OBJECTS.append(it["name"])
        except Exception:
            continue


def _save_user_objects():
    items = [{"name": n, "kind": CATALOG[n][0], "a": CATALOG[n][1], "b": CATALOG[n][2]}
             for n in USER_OBJECTS]
    with open(USER_OBJ_FILE, "w", encoding="utf-8") as fh:
        json.dump(items, fh, indent=1, ensure_ascii=False)


def add_user_object(name, kind, a, b):
    """kind 'icrs' (a=RA text, b=Dec text) or 'gal' (a=l deg, b=b deg). Returns the spec."""
    name = str(name).strip()
    if not name:
        raise ValueError("Enter a name for the object")
    if name in CATALOG and name not in USER_OBJECTS:
        raise ValueError("'%s' is a built-in object name" % name)
    if kind == "gal":
        spec = ("gal", float(a) % 360.0, float(b))
        if not -90 <= spec[2] <= 90:
            raise ValueError("Galactic latitude must be -90..90")
    else:
        validate_radec(a, b)
        spec = ("icrs", str(a).strip(), str(b).strip())
    CATALOG[name] = spec
    if name not in USER_OBJECTS:
        USER_OBJECTS.append(name)
    _save_user_objects()
    return spec


def delete_user_object(name):
    if name in USER_OBJECTS:
        USER_OBJECTS.remove(name)
        CATALOG.pop(name, None)
        _save_user_objects()


def category(name):
    kind = CATALOG[name][0] if name in CATALOG else "icrs"
    if name == CUSTOM:
        return CAT_CUSTOM
    if name in USER_OBJECTS:
        return CAT_USER
    if kind == "body":
        return CAT_SOLAR
    if kind == "gal":
        return CAT_MW
    return CAT_RADIO


def short_name(name):
    if name.startswith(MW_PREFIX):
        return name[len(MW_PREFIX) + 1:]
    return name.split(" (")[0]


def coord_of(spec, now=None, loc=None):
    """SkyCoord for a catalog spec."""
    kind, a, b = spec
    if kind == "body":
        now = now or Time.now()
        return get_sun(now) if a == "sun" else get_body(a, now, loc)
    if kind == "gal":
        return SkyCoord(l=a * u.deg, b=b * u.deg, frame="galactic")
    return SkyCoord(a, b, unit=(u.hourangle, u.deg), frame="icrs")


def equatorial(coord):
    """RA/Dec frame for display: ICRS for fixed sources, geocentric (GCRS) for the
    Sun/Moon (converting a nearby body to barycentric ICRS would shift it badly)."""
    return coord if coord.frame.name == "gcrs" else coord.icrs


def radec_text(coord):
    """(RA, Dec) as 'hh:mm:ss.s' / '+dd:mm:ss'."""
    c = equatorial(coord)
    return (c.ra.to_string(unit=u.hourangle, sep=":", precision=1, pad=True),
            c.dec.to_string(unit=u.deg, sep=":", precision=0, pad=True, alwayssign=True))


def validate_radec(ra, dec):
    SkyCoord(ra, dec, unit=(u.hourangle, u.deg))


_load_user_objects()


# =============================================================================
# HELPERS
# =============================================================================
def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def fmt_num(x):
    """Number without trailing zeros (106.8167 -> '106.8167')."""
    return ("%.6f" % x).rstrip("0").rstrip(".")


def wrap180(x):
    return (x + 180.0) % 360.0 - 180.0


def mech_to_sky(az_m, el_m, center=DEFAULT_AZ_CENTER):
    """Mechanical -> (sky azimuth 0..360, sky elevation)."""
    return ((center + az_m) % 360.0, el_m)


def sky_to_mech(az, el, center, ref):
    """Sky -> (az_m, el_m) or None if unreachable. Azimuth takes the SHORTEST path
    from the current position (ref): 303° from 0° is reached via -57°."""
    if not (EL_MIN - 1e-6 <= el <= EL_MAX + 1e-6):
        return None
    base = ref[0] + wrap180(az - center - ref[0])
    cands = [base + 360.0 * k for k in (0, -1, 1)]
    ok = [a for a in cands if AZ_M_MIN <= a <= AZ_M_MAX]
    if not ok:
        return None
    return (min(ok, key=lambda a: abs(a - ref[0])), clamp(el, EL_MIN, EL_MAX))


def utc_now_iso():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]


def list_ports():
    return [p.device for p in serial.tools.list_ports.comports()]


# =============================================================================
# SITE & EPHEMERIS
# =============================================================================
def pc_utc_offset():
    """Time zone of this computer in hours from UTC."""
    off = dt.datetime.now().astimezone().utcoffset()
    return off.total_seconds() / 3600.0 if off is not None else 0.0


def load_site():
    """Returns (site dict, message, first_run). first_run = no saved site yet."""
    site = {"name": "", "lat": DEFAULT_LAT, "lon": DEFAULT_LON, "h": DEFAULT_HEIGHT,
            "tz": pc_utc_offset(), "ask_on_start": False}
    try:
        with open(SITE_FILE, "r") as fh:
            st = json.load(fh)
        for k in ("lat", "lon", "h", "tz"):
            if k in st:
                site[k] = float(st[k])
        site["name"] = str(st.get("name", ""))
        site["ask_on_start"] = bool(st.get("ask_on_start", False))
        return site, ("Site loaded: %s lat %.4f, lon %.4f, height %.0f m, UTC%+g"
                      % ((site["name"] + ",") if site["name"] else "", site["lat"],
                         site["lon"], site["h"], site["tz"])), False
    except FileNotFoundError:
        return site, "No site saved yet", True
    except Exception as e:
        return site, "Failed to read %s: %s" % (SITE_FILE, e), True


def save_site(site):
    if not (-90 <= site["lat"] <= 90 and -180 <= site["lon"] <= 180 and -14 <= site["tz"] <= 14):
        raise ValueError("Location/time zone out of range (lat ±90, lon ±180, UTC -14..+14)")
    with open(SITE_FILE, "w") as fh:
        json.dump(site, fh, indent=1)


def location(site):
    return EarthLocation(lat=site["lat"] * u.deg, lon=site["lon"] * u.deg,
                         height=site["h"] * u.m)


def lst_deg(site, t=None):
    """Local mean sidereal time in degrees."""
    t = t or Time.now()
    return float(t.sidereal_time("mean", longitude=site["lon"] * u.deg).deg)


def altaz_of(spec, site, t=None):
    """Accurate (astropy) azimuth/elevation of a catalog spec, degrees."""
    t = t or Time.now()
    loc = location(site)
    aa = coord_of(spec, t, loc).transform_to(AltAz(obstime=t, location=loc))
    return float(aa.az.deg), float(aa.alt.deg)


def radec_deg_of(spec, site=None, t=None):
    """(RA, Dec) in degrees (J2000/ICRS) of a catalog spec."""
    loc = location(site) if site else None
    c = equatorial(coord_of(spec, t, loc))
    return float(c.ra.deg), float(c.dec.deg)


def radec_to_altaz(ra_deg, dec_deg, lst, lat):
    """Fast (approximate) RA/Dec -> Az/El for drawing charts. Arrays in degrees."""
    ra = np.radians(np.asarray(ra_deg, dtype=float))
    dec = np.radians(np.asarray(dec_deg, dtype=float))
    ha = np.radians(lst) - ra
    phi = math.radians(lat)
    sin_alt = np.sin(dec) * math.sin(phi) + np.cos(dec) * math.cos(phi) * np.cos(ha)
    alt = np.arcsin(np.clip(sin_alt, -1, 1))
    y = -np.cos(dec) * np.sin(ha)
    x = np.sin(dec) * math.cos(phi) - np.cos(dec) * math.sin(phi) * np.cos(ha)
    az = np.degrees(np.arctan2(y, x)) % 360.0
    return az, np.degrees(alt)


def altaz_to_radec(az_deg, alt_deg, lst, lat):
    """Fast (approximate) Az/El -> RA/Dec in degrees."""
    az, alt, phi = math.radians(az_deg), math.radians(alt_deg), math.radians(lat)
    sin_dec = math.sin(alt) * math.sin(phi) + math.cos(alt) * math.cos(phi) * math.cos(az)
    dec = math.asin(clamp(sin_dec, -1, 1))
    y = -math.sin(az) * math.cos(alt)
    x = math.sin(alt) * math.cos(phi) - math.cos(alt) * math.sin(phi) * math.cos(az)
    ha = math.atan2(y, x)
    return (lst - math.degrees(ha)) % 360.0, math.degrees(dec)


def ra_str(ra_deg):
    h = (ra_deg % 360.0) / 15.0
    hh = int(h)
    mm = int((h - hh) * 60)
    ss = (h - hh - mm / 60.0) * 3600
    return "%02dh %02dm %04.1fs" % (hh, mm, ss)


def dec_str(dec_deg):
    s = "+" if dec_deg >= 0 else "-"
    d = abs(dec_deg)
    dd = int(d)
    mm = int((d - dd) * 60)
    ss = (d - dd - mm / 60.0) * 3600
    return "%s%02d° %02d' %02d\"" % (s, dd, mm, int(ss))


# =============================================================================
# MOUNT (Arduino serial link)
# =============================================================================
class Mount:
    def __init__(self, log):
        self.log = log
        self.ser = None
        self.sim = False
        self.connected = False
        self.moving = False
        self.steps = [0, 0]            # posisi terakhir yang dilaporkan Arduino
        self.zero = [HOME_AZ_M, HOME_EL_M]   # sudut mekanik (deg) pada step 0
        self.az_center = DEFAULT_AZ_CENTER   # azimuth langit arah depan mount
        self.target = None             # (az_m, el_m) yang sedang dituju
        self._wlock = threading.Lock()
        self._done = threading.Event()
        self._done.set()
        self._aborted = False
        self._run = False
        self._sim_pos = [0.0, 0.0]
        self._sim_target = [0, 0]
        self._saved = None
        self.speed = [DEFAULT_SPEED_AZ, DEFAULT_SPEED_EL]   # derajat/detik
        self._move_eta = 0.0
        self.cmd_time = 0.0
        self.cmd_steps = [0, 0]
        self.cmd_warned = True
        self._load_state()

    # ---- penyimpanan posisi ----
    def _load_state(self):
        try:
            import json
            with open(STATE_FILE, "r") as fh:
                st = json.load(fh)
            self.zero = [float(st["az_m"]), float(st["el_m"])]
            self.az_center = DEFAULT_AZ_CENTER   # HOME selalu menghadap Utara
            self.log("Last position loaded: Azimuth %.2f°, Elevation %.2f°"
                     % (self.zero[0] % 360.0, self.zero[1]))
        except FileNotFoundError:
            self.log("No saved position - mount assumed at HOME (North, zenith)")
        except Exception as e:
            self.log("Failed to read %s (%s) - mount assumed at HOME" % (STATE_FILE, e))

    def save_state(self, force=False):
        az_m, el_m = self.position()
        st = (round(az_m, 4), round(el_m, 4), round(self.az_center, 4))
        if st == self._saved and not force:
            return
        try:
            import json
            with open(STATE_FILE, "w") as fh:
                json.dump({"az_m": st[0], "el_m": st[1], "az_center": st[2]}, fh)
            self._saved = st
        except Exception:
            pass

    # ---- konversi ----
    def position(self):
        """(az_m, el_m) mekanik saat ini dalam derajat."""
        return (round(self.zero[0] + self.steps[0] * AZ_DIR / STEPS_PER_DEG_AZ, 6),
                round(self.zero[1] + self.steps[1] * EL_DIR / STEPS_PER_DEG_EL, 6))

    def sky_position(self):
        """(az, el) langit saat ini."""
        az_m, el_m = self.position()
        return mech_to_sky(az_m, el_m, self.az_center)

    def set_home(self):
        """Nyatakan posisi fisik sekarang sebagai HOME (az_m=0, el_m=90/zenit)."""
        self.zero[0] = HOME_AZ_M - self.steps[0] * AZ_DIR / STEPS_PER_DEG_AZ
        self.zero[1] = HOME_EL_M - self.steps[1] * EL_DIR / STEPS_PER_DEG_EL
        self.az_center = DEFAULT_AZ_CENTER
        self.target = None
        self.save_state(force=True)

    # ---- koneksi ----
    def connect(self, port):
        """Blocking (~2 detik). Panggil dari thread latar."""
        last = self.position()
        self.disconnect()
        if port == SIM_PORT:
            self.sim = True
        else:
            self.sim = False
            self.ser = serial.Serial(port, BAUD_RATE, timeout=0.05, write_timeout=1.0)
            time.sleep(2.0)            # Uno reset otomatis saat port dibuka
            self.ser.reset_input_buffer()
        # Arduino mulai dari step 0 -> pertahankan sudut yang terakhir ditampilkan
        self.steps = [0, 0]
        self._sim_pos = [0.0, 0.0]
        self._sim_target = [0, 0]
        self.zero = [last[0], last[1]]
        self.target = None
        self.moving = False
        self._aborted = False
        self._done.set()
        self._run = True
        self.connected = True
        worker = self._sim_loop if self.sim else self._reader
        threading.Thread(target=worker, daemon=True).start()
        self.set_speed(*self.speed)     # Arduino reset -> kirim ulang kecepatan

    def speed_sps(self):
        """Kecepatan (step/detik) per sumbu, dibatasi MAX_SPS."""
        return (clamp(self.speed[0] * STEPS_PER_DEG_AZ, 1.0, MAX_SPS),
                clamp(self.speed[1] * STEPS_PER_DEG_EL, 1.0, MAX_SPS))

    def set_speed(self, az_dps, el_dps):
        """Atur kecepatan maksimum (derajat/detik). Return (az_sps, el_sps) aktual."""
        self.speed = [max(MIN_SPEED, az_dps), max(MIN_SPEED, el_dps)]
        sa, se = self.speed_sps()
        self.speed = [sa / STEPS_PER_DEG_AZ, se / STEPS_PER_DEG_EL]
        if self.connected and not self.sim:
            try:
                self._send("V %d %d" % (int(round(sa)), int(round(se))))
            except Exception as e:
                self.log("Failed to send speed: %s" % e)
        return sa, se

    def disconnect(self):
        self._run = False
        self.connected = False
        self.moving = False
        self._aborted = True
        self._done.set()
        ser, self.ser = self.ser, None
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass
        time.sleep(0.1)

    def _send(self, line):
        ser = self.ser
        if ser is None:
            raise IOError("serial port closed")
        with self._wlock:
            ser.write((line + "\n").encode("ascii"))
        if DEBUG_SERIAL and line != "P":
            self.log("TX → Arduino: %s" % line)

    def _reader(self):
        ser = self.ser
        buf = b""
        last_poll = 0.0
        try:
            while self._run and self.ser is ser:
                now = time.time()
                if now - last_poll >= POLL_INTERVAL:
                    self._send("P")
                    last_poll = now
                data = ser.read(64)
                if not data:
                    continue
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    self._handle(line.decode("ascii", "ignore").strip())
        except Exception as e:
            if self._run and self.ser is ser:
                self._run = False
                self.connected = False
                self.moving = False
                self._aborted = True
                self._done.set()
                self.log("Serial connection lost: %s" % e)

    def _handle(self, line):
        if not line:
            return
        if DEBUG_SERIAL and not line.startswith("POS"):
            self.log("RX ← Arduino: %s" % line)
        if line == "OK":
            self.moving = False
            self._done.set()
        elif line.startswith("POS"):
            parts = line.split()
            try:
                self.steps = [int(parts[1]), int(parts[2])]
            except (IndexError, ValueError):
                pass
        elif line.startswith("SPEED"):
            self.log("Arduino: speed set to %s step/s (Az / El)" % " / ".join(line.split()[1:]))
        elif line == "STOPPED":
            self.moving = False
            self.log("Arduino: motors stopped")
        elif line == "READY":
            # Arduino ter-reset (hitungan step kembali 0) -> pertahankan sudut terakhir
            last = self.position()
            self.steps = [0, 0]
            self.zero = [last[0], last[1]]
            if self.moving:
                self.moving = False
                self._aborted = True
                self._done.set()
                self.target = None
                self.log("WARNING: Arduino reset while moving - position may be "
                         "inaccurate, check and Set HOME again")
            else:
                self.log("Arduino: READY")
        elif line.startswith("ERR"):
            self.log("Arduino: %s" % line)

    def _sim_loop(self):
        last = time.time()
        while self._run and self.sim:
            time.sleep(0.02)
            now = time.time()
            sps = self.speed_sps()
            dt_ = now - last
            last = now
            tgt = list(self._sim_target)
            for k in (0, 1):
                d = tgt[k] - self._sim_pos[k]
                self._sim_pos[k] += clamp(d, -sps[k] * dt_, sps[k] * dt_)
            self.steps = [int(round(self._sim_pos[0])), int(round(self._sim_pos[1]))]
            if self.moving and tgt == self._sim_target and self.steps == tgt:
                self.moving = False
                self._done.set()

    # ---- perintah gerak ----
    def move_to(self, az, el, sky=True):
        """Kirim perintah gerak.
        sky=True : (az, el) koordinat langit -> mekanik, azimuth lewat jalur terpendek.
        sky=False: (az, el) koordinat mekanik langsung (dipotong ke batas).
        Return False jika belum terhubung atau target di luar jangkauan mekanik."""
        if not self.connected:
            return False
        ref = self.target if self.target is not None else self.position()
        if sky:
            sol = sky_to_mech(az, el, self.az_center, ref)
            if sol is None:
                return False
            maz, mel = sol
        else:
            maz = clamp(az, AZ_M_MIN, AZ_M_MAX)
            mel = clamp(el, EL_M_MIN, EL_M_MAX)
        sa = int(round((maz - self.zero[0]) * STEPS_PER_DEG_AZ)) * AZ_DIR
        se = int(round((mel - self.zero[1]) * STEPS_PER_DEG_EL)) * EL_DIR
        cur = self.position()
        self._move_eta = max(abs(maz - cur[0]) / self.speed[0],
                             abs(mel - cur[1]) / self.speed[1]) + 2.0
        self.target = (maz, mel)
        self.cmd_time = time.time()
        self.cmd_steps = list(self.steps)
        self.cmd_warned = False
        self._aborted = False
        self._done.clear()
        try:
            if self.sim:
                self._sim_target = [sa, se]
            else:
                self._send("M %d %d" % (sa, se))
            self.moving = True
        except Exception as e:
            self._done.set()
            self.log("Failed to send command: %s" % e)
            return False
        return True

    def wait_done(self, timeout=None):
        """Tunggu balasan OK. False jika dibatalkan, timeout, atau terputus."""
        if timeout is None:
            timeout = self._move_eta * 1.5 + MOVE_TIMEOUT_MARGIN
        ok = self._done.wait(timeout)
        return bool(ok and not self._aborted and self.connected)

    def stop(self):
        self._aborted = True
        self.target = None
        try:
            if self.sim:
                self._sim_target = [int(round(self._sim_pos[0])), int(round(self._sim_pos[1]))]
            elif self.connected:
                self._send("STOP")
        except Exception as e:
            self.log("Failed to send STOP: %s" % e)
        self.moving = False
        self._done.set()


# =============================================================================
# TRACKER
# =============================================================================
class Tracker:
    """Keeps the mount on a moving sky object (runs in its own thread)."""

    def __init__(self, mount, log, site):
        self.mount = mount
        self.log = log
        self.site = dict(site)
        self.active = None             # (name, spec)
        self.enabled = False
        self.paused = False            # e.g. while a raster scan runs
        self.status = ""
        self.target_altaz = None
        self.last_cmd = None
        self._alive = True
        threading.Thread(target=self._loop, daemon=True).start()

    def set_site(self, site):
        self.site = dict(site)
        self.last_cmd = None

    def start(self, name, spec):
        self.active = (name, spec)
        self.last_cmd = None
        self.target_altaz = None
        self.enabled = True
        self.status = "Tracking %s" % name

    def stop(self):
        self.enabled = False
        self.last_cmd = None
        self.status = ""

    def shutdown(self):
        self._alive = False

    @property
    def on_target(self):
        """True when the mount has reached the tracked object and is following it."""
        if not (self.enabled and self.active and self.last_cmd and self.target_altaz):
            return False
        az, el = self.target_altaz
        m_az, m_el = self.mount.sky_position()
        err = max(abs(wrap180(az - m_az)) * math.cos(math.radians(el)), abs(el - m_el))
        return err < ON_TARGET_TOL

    def _loop(self):
        while self._alive:
            if self.enabled and self.active is not None:
                name, spec = self.active
                try:
                    az, alt = altaz_of(spec, self.site)
                    self.target_altaz = (az, alt)
                except Exception:
                    self.target_altaz = None
                    self.status = "Invalid coordinates"
                    time.sleep(TRACK_INTERVAL)
                    continue
                if self.paused:
                    self.status = "Paused"
                elif not self.mount.connected:
                    self.status = "Mount disconnected"
                elif not EL_MIN <= alt <= EL_MAX:
                    self.status = "%s is below the horizon" % name
                else:
                    last = self.last_cmd
                    need = last is None or max(
                        abs(wrap180(az - last[0])) * math.cos(math.radians(alt)),
                        abs(alt - last[1])) > TRACK_DEADBAND
                    if need and self.mount.move_to(az, alt, sky=True):
                        self.last_cmd = (az, alt)
                    self.status = "Tracking %s" % name
            time.sleep(TRACK_INTERVAL)
