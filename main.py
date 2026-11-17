#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.py - User interface of the portable radio telescope.

All mount control lives in control.py; this file only builds
the windows and calls into it. sky_data.py holds the star catalogue for the chart.

Run:   python main.py
Build: build_exe.bat  (creates dist/RadioTelescope.exe)
"""

import datetime as dt
import importlib.util
import math
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

import numpy as np
import customtkinter as ctk


def _load_control():
    """Use control.py next to the .exe if present (edit it without rebuilding)."""
    if getattr(sys, "frozen", False):
        ext = os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "control.py")
        if os.path.exists(ext):
            spec = importlib.util.spec_from_file_location("control", ext)
            mod = importlib.util.module_from_spec(spec)
            sys.modules["control"] = mod
            spec.loader.exec_module(mod)
            return mod
    import control
    return control


C = _load_control()
import sky_data  # noqa: E402

# =============================================================================
# LOOK
# =============================================================================
BG = "#ECEEF1"
PANEL = "#FFFFFF"
BORDER = "#D3D8DF"
TEXT = "#1D232B"
MUTED = "#66707C"
FIELD = "#FFFFFF"
FIELD_BORDER = "#BFC6CF"
BTN = "#E4E7EB"
BTN_H = "#D5DAE0"
ACCENT = "#1F5FA6"
ACCENT_H = "#184E8A"
SELECT = "#D7E4F4"
RED = "#C0392B"
RED_H = "#A2301F"
GREEN = "#2E7D32"
AMBER = "#A86200"

# sky chart (night colours, like a hand controller screen)
SKY = "#081530"
GROUND = "#1A1A14"
HORIZON = "#5C6B3A"
GRID = "#1C3360"
CONST_LINE = "#3B6AA8"
CONST_TEXT = "#4F7FBE"
MW_COLOR = "#16295A"
STAR = "#F2F4FF"
LABEL = "#9FB4D8"
RADIO = "#FFB347"
SCOPE = "#FF4040"

if sys.platform.startswith("win"):
    UI_FONT, MONO = "Segoe UI", "Consolas"
elif sys.platform == "darwin":
    UI_FONT, MONO = "Helvetica Neue", "Menlo"
else:
    UI_FONT, MONO = "DejaVu Sans", "DejaVu Sans Mono"

TOAST_KEYS = ("WARNING", "EMERGENCY", "Failed", "failed", "FAILED", "not connected",
              "out of", "below the horizon", "ignored", "lost", "must", "Invalid", "Cannot",
              "is running", "already running", "unreachable")
PORT_SCAN_INTERVAL = 1.5


# =============================================================================
# SKY CHART
# =============================================================================
class SkyChart(tk.Canvas):
    """Star chart centred on the telescope (or panned by dragging)."""

    FOVS = [5, 10, 20, 40, 60, 90, 120, 180]

    def __init__(self, master, app):
        super().__init__(master, bg=SKY, highlightthickness=0, bd=0)
        self.app = app
        self.fov = 90
        self.follow = True
        self.center = (180.0, 45.0)        # (az, alt) when not following
        self.show_const = True
        self.show_grid = True
        self.show_labels = True
        st = sky_data.STARS
        self.s_ra = np.array([s[0] for s in st])
        self.s_dec = np.array([s[1] for s in st])
        self.s_mag = np.array([s[2] for s in st])
        self.s_name = [s[3] for s in st]
        self.lines = [(np.array([p[0] for p in ln]), np.array([p[1] for p in ln]))
                      for ln in sky_data.CONSTELLATION_LINES]
        self.c_labels = sky_data.CONSTELLATION_LABELS
        self.mw_ra, self.mw_dec = self.app.milky_way
        self._targets = []                 # (x, y, name) for click selection
        self._drag = None
        self.bind("<Configure>", lambda e: self.redraw())
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<B1-Motion>", self._motion)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<MouseWheel>", lambda e: self.zoom(-1 if e.delta > 0 else 1))
        self.bind("<Button-4>", lambda e: self.zoom(-1))
        self.bind("<Button-5>", lambda e: self.zoom(1))

    # ---- view ----
    def zoom(self, step):
        i = C.clamp(self.FOVS.index(self.fov) + step, 0, len(self.FOVS) - 1)
        self.fov = self.FOVS[int(i)]
        self.app.on_chart_view()
        self.redraw()

    def set_follow(self, on=True):
        self.follow = on
        self.app.on_chart_view()
        self.redraw()

    def _center(self):
        if self.follow:
            az, el = self.app.mount.sky_position()
            return az, C.clamp(el, 1.0, 90.0)
        return self.center[0], C.clamp(self.center[1], 1.0, 90.0)

    # ---- projection (stereographic, horizon coordinates) ----
    @staticmethod
    def _vec(az, alt):
        az, alt = np.radians(az), np.radians(alt)
        return np.stack([np.cos(alt) * np.sin(az), np.cos(alt) * np.cos(az), np.sin(alt)])

    def _setup_view(self, w, h):
        az0, alt0 = self._center()
        f = self._vec(az0, alt0).reshape(3)
        zen = np.array([0.0, 0.0, 1.0])
        up = zen - f * np.dot(zen, f)
        if np.linalg.norm(up) < 1e-6:
            up = np.array([math.sin(math.radians(az0 + 180)), math.cos(math.radians(az0 + 180)), 0.0])
        up /= np.linalg.norm(up)
        right = np.cross(f, up)
        self._f, self._up, self._right = f, up, right
        self._cx, self._cy = w / 2.0, h / 2.0
        self._s = (w / 2.0) / (2.0 * math.tan(math.radians(self.fov) / 4.0))

    def project(self, az, alt):
        v = self._vec(np.asarray(az, dtype=float), np.asarray(alt, dtype=float))
        d = np.tensordot(self._f, v, axes=1)
        k = 2.0 / np.maximum(1.0 + d, 1e-4)
        x = self._cx + self._s * k * np.tensordot(self._right, v, axes=1)
        y = self._cy - self._s * k * np.tensordot(self._up, v, axes=1)
        return np.clip(x, -3e4, 3e4), np.clip(y, -3e4, 3e4), d

    def unproject(self, px, py):
        """Canvas pixel -> (az, alt)."""
        X = (px - self._cx) / self._s
        Y = (self._cy - py) / self._s
        r2 = X * X + Y * Y
        d = (4 - r2) / (4 + r2)
        k = (1 + d) / 2.0
        v = self._f * d + self._right * X * k + self._up * Y * k
        alt = math.degrees(math.asin(C.clamp(v[2], -1, 1)))
        az = math.degrees(math.atan2(v[0], v[1])) % 360.0
        return az, alt

    def _poly(self, xs, ys, ok, **kw):
        """Draw a polyline, split where points are hidden or jump."""
        seg = []
        lim = 0.6 * max(self.winfo_width(), self.winfo_height())
        for i in range(len(xs)):
            if ok[i] and (not seg or abs(xs[i] - seg[-2]) + abs(ys[i] - seg[-1]) < lim):
                seg += [xs[i], ys[i]]
            else:
                if len(seg) >= 4:
                    self.create_line(*seg, **kw)
                seg = [xs[i], ys[i]] if ok[i] else []
        if len(seg) >= 4:
            self.create_line(*seg, **kw)

    # ---- drawing ----
    def redraw(self):
        w, h = self.winfo_width(), self.winfo_height()
        if w < 60 or h < 60:
            return
        self.delete("all")
        self._setup_view(w, h)
        site = self.app.site
        lst = self.app.lst
        lat = site["lat"]
        in_view = lambda x, y, d: (d > -0.3) & (x > -50) & (x < w + 50) & (y > -50) & (y < h + 50)

        # horizon curve (the ground is painted after the sky content)
        haz = np.arange(0.0, 361.0, 1.0)
        hx, hy, _ = self.project(haz, np.zeros_like(haz))
        poly = [c for p in zip(hx, hy) for c in p]
        inside = self._inside(self._cx, self._cy, hx, hy)
        self.configure(bg=SKY)

        def radec_xy(ra, dec):
            az, alt = C.radec_to_altaz(ra, dec, lst, lat)
            x, y, d = self.project(az, alt)
            return x, y, (alt > -0.2) & in_view(x, y, d), alt

        # milky way band
        x, y, ok, _ = radec_xy(self.mw_ra, self.mw_dec)
        width = max(6, min(140, self._s * 2 * math.radians(7)))
        self._poly(x, y, ok, fill=MW_COLOR, width=width, capstyle="round", joinstyle="round")

        # equatorial grid
        if self.show_grid:
            dec_line = np.arange(-88.0, 89.0, 2.0)
            for ra in range(0, 360, 30):
                x, y, ok, _ = radec_xy(np.full_like(dec_line, ra), dec_line)
                self._poly(x, y, ok, fill=GRID, dash=(2, 3))
            ra_line = np.arange(0.0, 361.0, 3.0)
            for dec in (-60, -30, 0, 30, 60):
                x, y, ok, _ = radec_xy(ra_line, np.full_like(ra_line, dec))
                self._poly(x, y, ok, fill=GRID, dash=(2, 3), width=2 if dec == 0 else 1)

        # constellations
        if self.show_const:
            for ra, dec in self.lines:
                x, y, ok, _ = radec_xy(ra, dec)
                self._poly(x, y, ok, fill=CONST_LINE)
            if self.show_labels and self.fov <= 120:
                ra = np.array([c[0] for c in self.c_labels])
                dec = np.array([c[1] for c in self.c_labels])
                x, y, ok, _ = radec_xy(ra, dec)
                for i in np.nonzero(ok)[0]:
                    self.create_text(x[i], y[i], text=self.c_labels[i][2].upper(),
                                     fill=CONST_TEXT, font=(UI_FONT, 7))

        # stars
        limit = 4.5 if self.fov >= 120 else (5.0 if self.fov >= 60 else 5.5)
        sel = self.s_mag <= limit
        x, y, ok, _ = radec_xy(self.s_ra[sel], self.s_dec[sel])
        mags = self.s_mag[sel]
        names = [n for n, s in zip(self.s_name, sel) if s]
        zoom_gain = 1.0 + (90.0 / self.fov) ** 0.35 * 0.4
        for i in np.nonzero(ok)[0]:
            r = max(0.7, (limit + 1.0 - mags[i]) * 0.75 * zoom_gain)
            self.create_oval(x[i] - r, y[i] - r, x[i] + r, y[i] + r, fill=STAR, outline="")
            if self.show_labels and names[i] and mags[i] < 1.6:
                self.create_text(x[i] + r + 3, y[i], text=names[i], anchor="w", fill=LABEL,
                                 font=(UI_FONT, 8))

        # ground
        if inside:
            self.create_polygon(*self._keyhole(hx, hy, w, h), fill=GROUND, outline="")
        else:
            self.create_polygon(*poly, fill=GROUND, outline="")

        # cardinal points
        for lab, az in (("N", 0), ("E", 90), ("S", 180), ("W", 270)):
            cx, cy, d = self.project([az], [1.5])
            if in_view(cx, cy, d)[0]:
                self.create_text(cx[0], cy[0], text=lab, fill="#C9D18A",
                                 font=(UI_FONT, 11, "bold"))
        self.create_line(*poly, fill=HORIZON, width=2)

        # catalogue objects
        self._targets = []
        sel_name = self.app.selected
        for name, ra, dec in self.app.chart_objects:
            x, y, ok, _ = radec_xy(np.array([ra]), np.array([dec]))
            if not ok[0]:
                continue
            x, y = float(x[0]), float(y[0])
            if name == C.SUN:
                self.create_oval(x - 7, y - 7, x + 7, y + 7, fill="#FFD54A", outline="")
            elif name == C.MOON:
                self.create_oval(x - 6, y - 6, x + 6, y + 6, fill="#C8CCD4", outline="")
            elif name.startswith(C.MW_PREFIX):
                self.create_rectangle(x - 3, y - 3, x + 3, y + 3, outline="#6FA8FF")
            else:
                self.create_oval(x - 5, y - 5, x + 5, y + 5, outline=RADIO, width=2)
            if self.show_labels:
                self.create_text(x + 9, y - 1, text=C.short_name(name), anchor="w",
                                 fill=RADIO if C.category(name) == C.CAT_RADIO else LABEL,
                                 font=(UI_FONT, 8))
            if name == sel_name:
                b = 11
                for sx, sy in ((-1, -1), (1, -1), (-1, 1), (1, 1)):
                    self.create_line(x + sx * b, y + sy * (b - 5), x + sx * b, y + sy * b,
                                     x + sx * (b - 5), y + sy * b, fill="#7CFC9A", width=2)
            self._targets.append((x, y, name))

        # telescope reticle
        az, el = self.app.mount.sky_position()
        tx, ty, d = self.project([az], [el])
        if d[0] > -0.3 and el > -5:
            tx, ty = float(tx[0]), float(ty[0])
            r = 15
            self.create_oval(tx - r, ty - r, tx + r, ty + r, outline=SCOPE, width=2)
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                self.create_line(tx + dx * 6, ty + dy * 6, tx + dx * 24, ty + dy * 24,
                                 fill=SCOPE, width=2)

        # overlays
        ra, dec = C.altaz_to_radec(az, el, lst, lat)
        self._label(8, 8, "nw", "TELESCOPE\nAz %06.2f°   El %05.2f°\nRA %s\nDec %s"
                    % (az, el, C.ra_str(ra), C.dec_str(dec)))
        self._label(w - 8, 8, "ne", "FOV %d°\n%s" % (self.fov, "SCOPE" if self.follow else "CHART"))
        if not self.follow:
            caz, calt = self._center()
            self._label(8, h - 8, "sw", "CENTRE  Az %06.2f°  El %05.2f°" % (caz, calt))
        lst_h = lst / 15.0
        self._label(w - 8, h - 8, "se", "LST %02d:%02d" % (int(lst_h), int(lst_h * 60) % 60))

    def _label(self, x, y, anchor, text):
        self.create_text(x, y, text=text, anchor=anchor, fill="#C8D6F0",
                         font=(MONO, 9), justify="right" if "e" in anchor else "left")

    @staticmethod
    def _keyhole(hx, hy, w, h):
        """Polygon covering everything OUTSIDE the horizon curve."""
        m = 4000
        k = int(np.argmax(hx))
        xs = np.concatenate([hx[k:], hx[1:k + 1]])
        ys = np.concatenate([hy[k:], hy[1:k + 1]])
        area = np.sum(xs[:-1] * ys[1:] - xs[1:] * ys[:-1])
        if area > 0:                      # trace the inner loop opposite to the outer one
            xs, ys = xs[::-1], ys[::-1]
        outer = [w + m, hy[k], w + m, h + m, -m, h + m, -m, -m, w + m, -m, w + m, hy[k]]
        inner = [c for p in zip(xs, ys) for c in p]
        return outer + inner

    @staticmethod
    def _inside(px, py, xs, ys):
        inside = False
        n = len(xs)
        j = n - 1
        for i in range(n):
            if ((ys[i] > py) != (ys[j] > py)) and \
                    (px < (xs[j] - xs[i]) * (py - ys[i]) / ((ys[j] - ys[i]) or 1e-9) + xs[i]):
                inside = not inside
            j = i
        return inside

    # ---- mouse ----
    def _press(self, e):
        self._drag = (e.x, e.y, self._center(), False)

    def _motion(self, e):
        if not self._drag:
            return
        x0, y0, (az0, alt0), moved = self._drag
        dx, dy = e.x - x0, e.y - y0
        if not moved and abs(dx) + abs(dy) < 5:
            return
        deg_px = math.degrees(2 * math.atan(1.0 / self._s)) / 2.0
        self.follow = False
        self.center = ((az0 - dx * deg_px / max(0.2, math.cos(math.radians(alt0)))) % 360.0,
                       C.clamp(alt0 + dy * deg_px, 1.0, 90.0))
        self._drag = (x0, y0, (az0, alt0), True)
        self.app.on_chart_view()
        self.redraw()

    def _release(self, e):
        drag = self._drag
        self._drag = None
        if drag and drag[3]:
            return
        best, bd = None, 15.0 ** 2
        for x, y, name in self._targets:
            d = (x - e.x) ** 2 + (y - e.y) ** 2
            if d < bd:
                best, bd = name, d
        if best:
            self.app.select_object(best)


# =============================================================================
# APPLICATION
# =============================================================================
class App(ctk.CTk):
    def __init__(self):
        ctk.set_appearance_mode("light")
        super().__init__()
        self.title("Radio Telescope Control")
        self.geometry("1360x840")
        self.minsize(1180, 720)
        if sys.platform.startswith("win"):
            self.after(0, lambda: self.state("zoomed"))      # start maximised
        self.configure(fg_color=BG)
        self.F = {
            "title": ctk.CTkFont(UI_FONT, 15, "bold"),
            "head": ctk.CTkFont(UI_FONT, 12, "bold"),
            "body": ctk.CTkFont(UI_FONT, 12),
            "small": ctk.CTkFont(UI_FONT, 11),
            "tab": ctk.CTkFont(UI_FONT, 13),
            "mono": ctk.CTkFont(MONO, 12),
            "mono_big": ctk.CTkFont(MONO, 17, "bold"),
        }
        self.q = queue.Queue()
        self._alive = True
        self._ui_cache = {}
        self._toast_until = 0
        self._connecting = False
        self._ports = None
        self._ports_shown = None
        self._tick_n = 0

        # core objects (control.py)
        self.site, site_msg, first_run = C.load_site()
        self.mount = C.Mount(self.log)
        self.tracker = C.Tracker(self.mount, self.log, self.site)

        # ephemeris state
        self.lst = C.lst_deg(self.site)
        self.selected = C.SUN
        self.sel_altaz = None
        self.sel_radec = None
        self._custom = ("RA / Dec", "00:00:00", "+00:00:00")
        self.jog_step = 1.0
        self.milky_way = self._milky_way()
        self.static_objects = self._static_objects()
        self.chart_objects = list(self.static_objects)
        self.table_altaz = {}

        self._style_ttk()
        self._build_header()
        self._build_pages()
        self._refresh_ports([])
        self._rebuild_tree()
        self.select_object(C.SUN)
        if first_run or self.site.get("ask_on_start"):
            self.after(600, lambda: self.site_dialog(first=first_run))

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        threading.Thread(target=self._ephem_loop, daemon=True).start()
        threading.Thread(target=self._port_loop, daemon=True).start()
        self.log(site_msg)
        self.log("Ready. Resolution Az %.2f, El %.2f steps/deg"
                 % (C.STEPS_PER_DEG_AZ, C.STEPS_PER_DEG_EL))
        self.after(100, self._tick)

    # ------------------------------------------------------------------ helpers
    def log(self, msg):
        self.q.put(("log", "[%s] %s" % (time.strftime("%H:%M:%S"), msg)))

    def _style_ttk(self):
        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT,
                     rowheight=22, borderwidth=0, font=(UI_FONT, 10))
        st.configure("Treeview.Heading", background=BTN, foreground=TEXT, relief="flat",
                     font=(UI_FONT, 10, "bold"))
        st.map("Treeview", background=[("selected", SELECT)], foreground=[("selected", TEXT)])
        st.map("Treeview.Heading", background=[("active", BTN_H)])

    def section(self, parent, title):
        f = ctk.CTkFrame(parent, fg_color=PANEL, border_width=1, border_color=BORDER,
                         corner_radius=6)
        ctk.CTkLabel(f, text=title, font=self.F["head"], text_color=TEXT,
                     anchor="w").pack(fill="x", padx=12, pady=(9, 4))
        body = ctk.CTkFrame(f, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        return f, body

    def entry(self, parent, value="", width=120):
        e = ctk.CTkEntry(parent, height=28, width=width, corner_radius=4, border_width=1,
                         border_color=FIELD_BORDER, fg_color=FIELD, text_color=TEXT,
                         font=self.F["mono"])
        e.insert(0, str(value))
        return e

    def option(self, parent, values, value=None, command=None, width=120):
        o = ctk.CTkOptionMenu(parent, values=values, command=command, height=28, width=width,
                              corner_radius=4, fg_color=BTN, button_color=BTN,
                              button_hover_color=BTN_H, text_color=TEXT,
                              dropdown_fg_color=PANEL, dropdown_text_color=TEXT,
                              dropdown_hover_color=SELECT, font=self.F["body"],
                              dynamic_resizing=False)
        o.set(value or values[0])
        return o

    def button(self, parent, text, command, kind="secondary", **kw):
        colors = {"primary": (ACCENT, ACCENT_H, "#FFFFFF"),
                  "secondary": (BTN, BTN_H, TEXT),
                  "danger": (RED, RED_H, "#FFFFFF")}[kind]
        return ctk.CTkButton(parent, text=text, command=command, fg_color=colors[0],
                             hover_color=colors[1], text_color=colors[2], corner_radius=4,
                             border_width=0, height=kw.pop("height", 30),
                             font=kw.pop("font", self.F["body"]), **kw)

    def form(self, parent, rows, label_w=120):
        """rows: [(label, default or list of choices)] -> list of widgets."""
        fr = ctk.CTkFrame(parent, fg_color="transparent")
        fr.pack(fill="x")
        fr.grid_columnconfigure(1, weight=1)
        out = []
        for r, (lab, val) in enumerate(rows):
            ctk.CTkLabel(fr, text=lab, font=self.F["body"], text_color=MUTED, anchor="w",
                         width=label_w).grid(row=r, column=0, sticky="w", pady=3)
            w = self.option(fr, val) if isinstance(val, list) else self.entry(fr, val)
            w.grid(row=r, column=1, sticky="ew", pady=3)
            out.append(w)
        return out

    def readout(self, parent, rows, big=()):
        fr = ctk.CTkFrame(parent, fg_color="transparent")
        fr.pack(fill="x")
        fr.grid_columnconfigure(1, weight=1)
        out = {}
        for r, key in enumerate(rows):
            ctk.CTkLabel(fr, text=key, font=self.F["body"], text_color=MUTED,
                         anchor="w").grid(row=r, column=0, sticky="w", pady=1)
            lb = ctk.CTkLabel(fr, text="-", anchor="e", text_color=TEXT,
                              font=self.F["mono_big"] if key in big else self.F["mono"])
            lb.grid(row=r, column=1, sticky="e", pady=1)
            out[key] = lb
        return out

    def _set(self, key, widget, **kw):
        if self._ui_cache.get(key) != kw:
            self._ui_cache[key] = kw
            widget.configure(**kw)

    @staticmethod
    def _num(entry, name):
        try:
            return float(entry.get().strip().replace(",", "."))
        except ValueError:
            raise ValueError("Invalid value: %s" % name)

    def _milky_way(self):
        from astropy.coordinates import SkyCoord
        import astropy.units as u
        c = SkyCoord(l=np.arange(0.0, 361.0, 2.0) * u.deg, b=np.zeros(181) * u.deg,
                     frame="galactic").icrs
        return c.ra.deg, c.dec.deg

    def _static_objects(self):
        out = []
        for name, spec in C.CATALOG.items():
            if spec[0] == "body" or name == C.CUSTOM:
                continue
            ra, dec = C.radec_deg_of(spec)
            out.append((name, ra, dec))
        return out

    # ------------------------------------------------------------------ header
    def _build_header(self):
        bar = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=0, border_width=0, height=52)
        bar.pack(fill="x")
        ctk.CTkFrame(self, fg_color=BORDER, height=1, corner_radius=0).pack(fill="x")
        # three-line menu button (drawn, so it does not depend on the font)
        self.btn_menu = tk.Canvas(bar, width=38, height=34, bg=PANEL, highlightthickness=0,
                                  bd=0, cursor="hand2")
        self.btn_menu.pack(side="left", padx=(10, 0), pady=9)
        for y in (11, 17, 23):
            self.btn_menu.create_line(11, y, 27, y, fill=TEXT, width=2, capstyle="round")
        self.btn_menu.bind("<Button-1>", lambda e: self.toggle_side())
        self.btn_menu.bind("<Enter>", lambda e: self.btn_menu.configure(bg=BTN_H))
        self.btn_menu.bind("<Leave>", lambda e: self._menu_color())
        ctk.CTkLabel(bar, text="Radio Telescope Control", font=self.F["title"],
                     text_color=TEXT).pack(side="left", padx=(6, 0), pady=10)
        self.lbl_toast = ctk.CTkLabel(bar, text="", font=self.F["small"], text_color=AMBER)
        self.lbl_toast.pack(side="left", padx=16)

        self.btn_stop = self.button(bar, "STOP", self.emergency_stop, "danger", width=84,
                                    height=34, font=self.F["head"])
        self.btn_stop.pack(side="right", padx=(10, 16))
        # mount (Arduino COM port)
        self.lbl_conn = ctk.CTkLabel(bar, text="Off", font=self.F["body"], text_color=MUTED,
                                     width=80, anchor="w")
        self.lbl_conn.pack(side="right", padx=(6, 0))
        self.btn_conn = self.button(bar, "Connect", self.toggle_connect, "primary", width=86)
        self.btn_conn.pack(side="right")
        self.opt_port = self.option(bar, [C.SIM_PORT], width=130)
        self.opt_port.pack(side="right", padx=6)
        ctk.CTkLabel(bar, text="Mount", font=self.F["body"], text_color=MUTED).pack(side="right")

    # ------------------------------------------------------------------ pages
    def _build_pages(self):
        self.page_host = ctk.CTkFrame(self, fg_color=BG, corner_radius=0)
        self.page_host.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.side = ctk.CTkScrollableFrame(self.page_host, fg_color=BG, corner_radius=0,
                                           width=300)
        self._build_side(self.side)
        self.side_open = False
        self.page = ctk.CTkFrame(self.page_host, fg_color=BG, corner_radius=0)
        self._page_telescope(self.page)
        self.page.pack(side="left", fill="both", expand=True, pady=(12, 0))
        self.after(50, self.chart.redraw)

    def toggle_side(self):
        """Show / hide the settings panel (three-line button, like SDR#)."""
        if self.side_open:
            self.side.pack_forget()
        else:
            self.side.pack(side="left", fill="y", pady=(12, 0), padx=(0, 10), before=self.page)
        self.side_open = not self.side_open
        self._menu_color()

    def _menu_color(self):
        self.btn_menu.configure(bg=SELECT if self.side_open else PANEL)

    # ---------------------------------------------------------- settings panel
    def _build_side(self, col):
        f, b = self.section(col, "Observing site")
        f.pack(fill="x")
        self.e_site, self.e_lat, self.e_lon, self.e_h, self.e_tz = self.form(b, [
            ("Site name", self.site.get("name", "")),
            ("Latitude (°)", C.fmt_num(self.site["lat"])),
            ("Longitude (°)", C.fmt_num(self.site["lon"])),
            ("Height (m)", C.fmt_num(self.site["h"])),
            ("Time zone (UTC+)", C.fmt_num(self.site["tz"]))], label_w=110)
        ctk.CTkLabel(b, text="South latitude and west longitude are negative.",
                     font=self.F["small"], text_color=MUTED, anchor="w", justify="left",
                     wraplength=270).pack(fill="x", pady=(4, 0))
        self.var_ask = tk.BooleanVar(value=self.site.get("ask_on_start", False))
        ctk.CTkCheckBox(b, text="Ask for the site at every start", variable=self.var_ask,
                        font=self.F["body"], text_color=TEXT, checkbox_width=16,
                        checkbox_height=16, corner_radius=3, border_width=1, fg_color=ACCENT,
                        hover_color=ACCENT_H, border_color=FIELD_BORDER).pack(anchor="w",
                                                                             pady=(6, 0))
        self.lbl_pctz = ctk.CTkLabel(b, text="", font=self.F["small"], text_color=AMBER,
                                     anchor="w", justify="left", wraplength=270)
        self.lbl_pctz.pack(fill="x")
        self.button(b, "Save site", self.do_save_site, "primary").pack(fill="x", pady=(6, 0))

        f, b = self.section(col, "Slew speed")
        f.pack(fill="x", pady=(10, 0))
        self.e_spd_az, self.e_spd_el = self.form(b, [
            ("Azimuth (°/s)", C.fmt_num(round(self.mount.speed[0], 2))),
            ("Elevation (°/s)", C.fmt_num(round(self.mount.speed[1], 2)))], label_w=110)
        ctk.CTkLabel(b, text="Max: Az %.1f °/s, El %.1f °/s"
                     % (C.MAX_SPS / C.STEPS_PER_DEG_AZ, C.MAX_SPS / C.STEPS_PER_DEG_EL),
                     font=self.F["small"], text_color=MUTED, anchor="w").pack(fill="x",
                                                                            pady=(4, 0))
        self.button(b, "Apply", self.do_speed).pack(fill="x", pady=(6, 0))

        f, b = self.section(col, "Home position")
        f.pack(fill="x", pady=(10, 0))
        ctk.CTkLabel(b, text="Point the dish North and straight up (zenith), then press:",
                     font=self.F["body"], text_color=MUTED, anchor="w", justify="left",
                     wraplength=270).pack(fill="x", pady=(0, 6))
        self.button(b, "Set HOME  (Az 0°, El 90°)", self.confirm_set_home).pack(fill="x")

    # ---------------------------------------------------------- Telescope page
    def _page_telescope(self, page):
        page.grid_columnconfigure(1, weight=1)
        page.grid_rowconfigure(0, weight=1)

        left = ctk.CTkFrame(page, fg_color="transparent", width=290)
        left.grid(row=0, column=0, sticky="ns", padx=(0, 10))
        left.pack_propagate(False)

        f, b = self.section(left, "Telescope position")
        f.pack(fill="x")
        self.ro = self.readout(b, ["Azimuth", "Elevation", "RA", "Dec", "LST", "Local time",
                                   "State"], big=("Azimuth", "Elevation"))

        f, b = self.section(left, "Manual slew")
        f.pack(fill="x", pady=(10, 0))
        self.seg_step = ctk.CTkSegmentedButton(
            b, values=["0.1°", "1°", "5°", "10°"], command=self._on_step, height=28,
            fg_color=BTN, selected_color=ACCENT, selected_hover_color=ACCENT_H,
            unselected_color=BTN, unselected_hover_color=BTN_H, text_color=TEXT,
            corner_radius=4, font=self.F["body"])
        self.seg_step.set("1°")
        self.seg_step.pack(fill="x", pady=(0, 8))
        pad = ctk.CTkFrame(b, fg_color="transparent")
        pad.pack()
        kw = dict(width=46, height=40, font=ctk.CTkFont(UI_FONT, 15))
        self.button(pad, "▲", lambda: self.jog(0, 1), **kw).grid(row=0, column=1, padx=3, pady=3)
        self.button(pad, "◀", lambda: self.jog(-1, 0), **kw).grid(row=1, column=0, padx=3, pady=3)
        self.button(pad, "■", self.mount.stop, **kw).grid(row=1, column=1, padx=3, pady=3)
        self.button(pad, "▶", lambda: self.jog(1, 0), **kw).grid(row=1, column=2, padx=3, pady=3)
        self.button(pad, "▼", lambda: self.jog(0, -1), **kw).grid(row=2, column=1, padx=3, pady=3)

        f, b = self.section(left, "Go to Az / El")
        f.pack(fill="x", pady=(10, 0))
        self.e_goto_az, self.e_goto_el = self.form(b, [("Azimuth (°)", "180"),
                                                       ("Elevation (°)", "45")], label_w=100)
        row = ctk.CTkFrame(b, fg_color="transparent")
        row.pack(fill="x", pady=(6, 0))
        self.button(row, "Go to", self.do_goto, "primary", width=120).pack(side="left")
        self.button(row, "Home", self.do_home, width=120).pack(side="right")

        # centre: sky chart
        f, b = self.section(page, "Sky chart")
        f.grid(row=0, column=1, sticky="nsew")
        self.chart = SkyChart(b, self)
        self.chart.pack(fill="both", expand=True)
        tools = ctk.CTkFrame(b, fg_color="transparent")
        tools.pack(fill="x", pady=(8, 0))
        self.button(tools, "−", lambda: self.chart.zoom(1), width=32).pack(side="left")
        self.button(tools, "+", lambda: self.chart.zoom(-1), width=32).pack(side="left", padx=4)
        self.lbl_fov = ctk.CTkLabel(tools, text="", font=self.F["body"], text_color=MUTED,
                                    width=60)
        self.lbl_fov.pack(side="left", padx=4)
        self.btn_follow = self.button(tools, "Centre on telescope",
                                      lambda: self.chart.set_follow(True), width=150)
        self.btn_follow.pack(side="left", padx=4)
        self.var_const = tk.BooleanVar(value=True)
        self.var_grid = tk.BooleanVar(value=True)
        self.var_labels = tk.BooleanVar(value=True)
        for text, var in (("Labels", self.var_labels), ("Grid", self.var_grid),
                          ("Constellations", self.var_const)):
            ctk.CTkCheckBox(tools, text=text, variable=var, command=self._chart_opts,
                            font=self.F["body"], text_color=TEXT, checkbox_width=16,
                            checkbox_height=16, corner_radius=3, border_width=1,
                            fg_color=ACCENT, hover_color=ACCENT_H,
                            border_color=FIELD_BORDER).pack(side="right", padx=(10, 0))

        # right: catalogue + selected object
        right = ctk.CTkFrame(page, fg_color="transparent", width=340)
        right.grid(row=0, column=2, sticky="ns", padx=(10, 0))
        right.pack_propagate(False)
        f, b = self.section(right, "Objects")
        f.pack(fill="both", expand=True)
        tv_frame = tk.Frame(b, bg=BORDER, bd=0, highlightthickness=0)
        tv_frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(tv_frame, columns=("az", "el"), selectmode="browse")
        self.tree.heading("#0", text="Object", anchor="w")
        self.tree.heading("az", text="Az", anchor="e")
        self.tree.heading("el", text="El", anchor="e")
        self.tree.column("#0", width=190, stretch=True)
        self.tree.column("az", width=56, anchor="e", stretch=False)
        self.tree.column("el", width=56, anchor="e", stretch=False)
        sb = ttk.Scrollbar(tv_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=1, pady=1)
        sb.pack(side="right", fill="y")
        self.tree.tag_configure("below", foreground="#9AA3AD")
        self.tree.tag_configure("group", font=(UI_FONT, 10, "bold"))
        self.tree.bind("<<TreeviewSelect>>", self._on_tree)

        f, b = self.section(right, "Selected object")
        f.pack(fill="x", pady=(10, 0))
        self.lbl_sel = ctk.CTkLabel(b, text="", font=self.F["head"], text_color=TEXT,
                                    anchor="w")
        self.lbl_sel.pack(fill="x")
        self.custom_row = ctk.CTkFrame(b, fg_color="transparent")
        self.custom_row.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(self.custom_row, text="Name", font=self.F["body"], text_color=MUTED,
                     width=90, anchor="w").grid(row=0, column=0, sticky="w", pady=3)
        self.e_objname = self.entry(self.custom_row, "")
        self.e_objname.grid(row=0, column=1, sticky="ew", pady=3)
        ctk.CTkLabel(self.custom_row, text="Coordinates", font=self.F["body"], text_color=MUTED,
                     width=90, anchor="w").grid(row=1, column=0, sticky="w", pady=3)
        self.opt_sys = self.option(self.custom_row, ["RA / Dec", "Galactic l / b"],
                                   command=self._on_sys)
        self.opt_sys.grid(row=1, column=1, sticky="ew", pady=3)
        self.coord_box = ctk.CTkFrame(b, fg_color="transparent")
        self.coord_box.pack(fill="x")
        self.coord_box.grid_columnconfigure(1, weight=1)
        self.lbl_c1 = ctk.CTkLabel(self.coord_box, text="RA (J2000)", font=self.F["body"],
                                   text_color=MUTED, width=90, anchor="w")
        self.lbl_c1.grid(row=0, column=0, sticky="w", pady=3)
        self.e_ra = self.entry(self.coord_box, "")
        self.e_ra.grid(row=0, column=1, sticky="ew", pady=3)
        self.lbl_c2 = ctk.CTkLabel(self.coord_box, text="Dec (J2000)", font=self.F["body"],
                                   text_color=MUTED, width=90, anchor="w")
        self.lbl_c2.grid(row=1, column=0, sticky="w", pady=3)
        self.e_dec = self.entry(self.coord_box, "")
        self.e_dec.grid(row=1, column=1, sticky="ew", pady=3)
        self.ro_sel = self.readout(b, ["Az / El"])
        row = ctk.CTkFrame(b, fg_color="transparent")
        row.pack(fill="x", pady=(8, 0))
        self.button(row, "Go to", self.do_goto_object, "primary", width=110).pack(side="left")
        self.sw_track = ctk.CTkSwitch(row, text="Track", command=self._on_track,
                                      font=self.F["body"], text_color=TEXT,
                                      progress_color=GREEN, button_color=PANEL,
                                      button_hover_color=PANEL, fg_color=FIELD_BORDER)
        self.sw_track.pack(side="right")
        self.lbl_track = ctk.CTkLabel(b, text="", font=self.F["small"], text_color=MUTED,
                                      anchor="w")
        self.lbl_track.pack(fill="x", pady=(6, 0))
        self.btn_obj = self.button(b, "Save as new object", self.do_save_object, height=28)

    def _chart_opts(self):
        self.chart.show_const = self.var_const.get()
        self.chart.show_grid = self.var_grid.get()
        self.chart.show_labels = self.var_labels.get()
        self.chart.redraw()

    def on_chart_view(self):
        self._set("fov", self.lbl_fov, text="%d°" % self.chart.fov)
        self._set("follow", self.btn_follow,
                  fg_color=SELECT if self.chart.follow else BTN)

    # ================================================================= actions
    def _refresh_ports(self, real):
        old = self._ports_shown
        ports = list(real) + [C.SIM_PORT]
        self._ports_shown = list(real)
        self.opt_port.configure(values=ports)
        if old is not None:
            new = [p for p in real if p not in old]
            if new:
                self.log("Port detected: %s" % ", ".join(new))
        else:
            new = list(real)
        if self.mount.connected or self._connecting:
            return
        cur = self.opt_port.get()
        if cur not in ports or (cur == C.SIM_PORT and new):
            self.opt_port.set(ports[0])

    def _port_loop(self):
        while self._alive:
            try:
                self._ports = C.list_ports()
            except Exception:
                pass
            time.sleep(PORT_SCAN_INTERVAL)

    def toggle_connect(self):
        if self._connecting:
            return
        if self.mount.connected:
            self.emergency_stop(quiet=True)
            self.mount.disconnect()
            self.log("Disconnected")
            return
        port = self.opt_port.get()
        self._connecting = True
        self.log("Connecting to %s ..." % port)

        def work():
            try:
                self.mount.connect(port)
                self.log("Connected to %s" % port)
            except Exception as e:
                self.mount.disconnect()
                self.log("Connection failed: %s" % e)
            finally:
                self._connecting = False

        threading.Thread(target=work, daemon=True).start()

    def emergency_stop(self, quiet=False):
        self.tracker.stop()
        self.sw_track.deselect()
        self.mount.stop()
        if not quiet:
            self.log("EMERGENCY STOP - motion and tracking stopped")

    def _manual_allowed(self):
        if not self.mount.connected:
            self.log("Mount not connected")
            return False
        if self.tracker.enabled:
            self.log("Manual move ignored: turn off tracking first")
            return False
        return True

    def _on_step(self, value):
        self.jog_step = float(value.replace("°", ""))

    def jog(self, d_az, d_el):
        if not self._manual_allowed():
            return
        base = self.mount.target if self.mount.target is not None else self.mount.position()
        if self.mount.move_to(base[0] + d_az * self.jog_step, base[1] + d_el * self.jog_step,
                              sky=False):
            t = self.mount.target
            s_az, s_el = C.mech_to_sky(t[0], t[1])
            self.log("Jog -> Az %06.2f  El %05.2f" % (s_az, s_el))

    def do_goto(self):
        if not self._manual_allowed():
            return
        try:
            az = self._num(self.e_goto_az, "Azimuth")
            el = self._num(self.e_goto_el, "Elevation")
        except ValueError as e:
            self.log(str(e))
            return
        if not C.EL_MIN <= el <= C.EL_MAX:
            self.log("Elevation must be 0 - 90°")
            return
        if self.mount.move_to(az, el, sky=True):
            self.log("Go to Az %.2f  El %.2f" % (az % 360.0, el))
        else:
            self.log("Az %.2f El %.2f is out of mechanical range" % (az % 360.0, el))

    def do_home(self):
        if self._manual_allowed():
            self.mount.move_to(C.HOME_AZ_M, C.HOME_EL_M, sky=False)
            self.log("Moving to HOME")

    def do_set_home(self):
        if self.mount.moving or self.tracker.enabled:
            self.log("Set HOME only while the mount is idle (no tracking)")
            return
        self.mount.set_home()
        self.log("HOME set (Az 0°, El 90°)")

    def confirm_set_home(self):
        if messagebox.askyesno("Set HOME", "Is the dish pointing North and straight up "
                               "(zenith)?\n\nThe current position becomes Az 0°, El 90°.",
                               parent=self):
            self.do_set_home()

    def do_speed(self):
        try:
            az = self._num(self.e_spd_az, "Azimuth speed")
            el = self._num(self.e_spd_el, "Elevation speed")
            if az <= 0 or el <= 0:
                raise ValueError("Speed must be greater than 0")
        except ValueError as e:
            self.log(str(e))
            return
        sa, se = self.mount.set_speed(az, el)
        for e, v in ((self.e_spd_az, self.mount.speed[0]), (self.e_spd_el, self.mount.speed[1])):
            e.delete(0, "end")
            e.insert(0, C.fmt_num(round(v, 2)))
        self.log("Speed: Az %.2f °/s (%.0f step/s), El %.2f °/s (%.0f step/s)"
                 % (self.mount.speed[0], sa, self.mount.speed[1], se))

    def do_save_site(self, quiet=False):
        try:
            site = {"name": self.e_site.get().strip(),
                    "lat": self._num(self.e_lat, "Latitude"),
                    "lon": self._num(self.e_lon, "Longitude"),
                    "h": self._num(self.e_h, "Height"),
                    "tz": self._num(self.e_tz, "Time zone"),
                    "ask_on_start": bool(self.var_ask.get())}
            C.save_site(site)
        except Exception as e:
            if not quiet:
                self.log("Failed to save site: %s" % e)
            return
        self._set_site(site)
        if not quiet:
            self.log("Site saved: lat %.4f, lon %.4f, %.0f m, UTC%+g"
                     % (site["lat"], site["lon"], site["h"], site["tz"]))

    # ---- dialogs ----
    def _dialog(self, title):
        dlg = ctk.CTkToplevel(self)
        dlg.title(title)
        dlg.configure(fg_color=BG)
        dlg.resizable(False, False)
        dlg.transient(self)
        f, b = self.section(dlg, title)
        f.pack(fill="both", expand=True, padx=12, pady=12)
        b.configure(width=360)

        def place():
            dlg.update_idletasks()
            x = self.winfo_rootx() + (self.winfo_width() - dlg.winfo_width()) // 2
            y = self.winfo_rooty() + (self.winfo_height() - dlg.winfo_height()) // 3
            dlg.geometry("+%d+%d" % (max(0, x), max(0, y)))
            dlg.lift()
            dlg.focus_force()
            try:
                dlg.grab_set()
            except tk.TclError:
                pass

        dlg.after(80, place)
        return dlg, b

    def _dialog_buttons(self, dlg, body, text, command, cancel=True):
        row = ctk.CTkFrame(body, fg_color="transparent")
        row.pack(fill="x", pady=(10, 0))
        self.button(row, text, command, "primary", width=120).pack(side="right")
        if cancel:
            self.button(row, "Cancel", dlg.destroy, width=100).pack(side="right", padx=8)
        dlg.bind("<Return>", lambda e: command())
        dlg.bind("<Escape>", lambda e: dlg.destroy() if cancel else None)

    # ---- objects ----
    def _obj_spec(self, name):
        if name == C.CUSTOM:
            if self.opt_sys.get().startswith("Galactic"):
                try:
                    return ("gal", float(self.e_ra.get().replace(",", ".")),
                            float(self.e_dec.get().replace(",", ".")))
                except ValueError:
                    return ("gal", float("nan"), float("nan"))
            return ("icrs", self.e_ra.get().strip(), self.e_dec.get().strip())
        return C.CATALOG[name]

    def _on_tree(self, _e=None):
        sel = self.tree.selection()
        if sel and sel[0] in C.CATALOG and sel[0] != self.selected:
            self.select_object(sel[0])

    def select_object(self, name):
        """Only changes the selection; the mount moves on Go to / Track."""
        if self.selected == C.CUSTOM and name != C.CUSTOM:
            self._custom = (self.opt_sys.get(), self.e_ra.get(), self.e_dec.get())
        self.selected = name
        if self.tree.exists(name) and tuple(self.tree.selection()) != (name,):
            self.tree.selection_set(name)
            self.tree.see(name)
        self.lbl_sel.configure(text=name)
        custom = name == C.CUSTOM
        if custom:
            self.custom_row.pack(fill="x", before=self.coord_box)
        else:
            self.custom_row.pack_forget()
        kind = C.CATALOG[name][0]
        if custom:
            sysname, ra, dec = self._custom
            self.opt_sys.set(sysname)
            self._on_sys(sysname)
        else:
            self.lbl_c1.configure(text="RA (J2000)")
            self.lbl_c2.configure(text="Dec (J2000)")
            if kind == "body":
                ra = dec = "..."
            else:
                ra, dec = C.radec_text(C.coord_of(C.CATALOG[name]))
        for e, v in ((self.e_ra, ra), (self.e_dec, dec)):
            e.configure(state="normal")
            e.delete(0, "end")
            e.insert(0, v)
            if not custom:
                e.configure(state="disabled")
        if custom:
            self.btn_obj.configure(text="Save as new object", command=self.do_save_object)
            self.btn_obj.pack(fill="x", pady=(8, 0))
        elif name in C.USER_OBJECTS:
            self.btn_obj.configure(text="Delete this object", command=self.do_delete_object)
            self.btn_obj.pack(fill="x", pady=(8, 0))
        else:
            self.btn_obj.pack_forget()
        self.sel_altaz = None
        self.sel_radec = None
        self._ui_cache.pop("radec", None)
        self.chart.redraw()

    def _checked_spec(self):
        spec = self._obj_spec(self.selected)
        if spec[0] == "icrs":
            try:
                C.validate_radec(spec[1], spec[2])
            except Exception:
                self.log("Invalid RA/Dec")
                return None
        return spec

    def do_goto_object(self):
        if not self.mount.connected:
            self.log("Mount not connected")
            return
        spec = self._checked_spec()
        if spec is None:
            return
        name = self.selected
        if self.tracker.enabled:
            self.tracker.start(name, spec)
            self.log("Tracking switched to %s" % name)
            return

        def work():
            try:
                az, alt = C.altaz_of(spec, self.site)
            except Exception as e:
                self.log("Failed to compute position of %s: %s" % (name, e))
                return
            if not C.EL_MIN <= alt <= C.EL_MAX:
                self.log("%s is below the horizon (El %.1f°)" % (name, alt))
                return
            if self.mount.move_to(az, alt, sky=True):
                self.log("Go to %s: Az %.2f  El %.2f" % (name, az, alt))

        threading.Thread(target=work, daemon=True).start()

    def _on_track(self):
        if self.sw_track.get():
            if not self.mount.connected:
                self.log("Cannot start tracking: mount not connected")
                self.sw_track.deselect()
                return
            spec = self._checked_spec()
            if spec is None:
                self.sw_track.deselect()
                return
            self.tracker.start(self.selected, spec)
            self.log("Tracking ON: %s" % self.selected)
        else:
            self.tracker.stop()
            self.log("Tracking OFF")

    # ================================================================ background
    def _ephem_loop(self):
        last_bodies = 0.0
        while self._alive:
            site = dict(self.site)
            try:
                self.lst = C.lst_deg(site)
            except Exception:
                pass
            name = self.selected
            try:
                spec = self._obj_spec(name)
                az, alt = C.altaz_of(spec, site)
                self.sel_altaz = (name, az, alt)
                if spec[0] == "body":
                    loc = C.location(site)
                    self.sel_radec = (name,) + C.radec_text(C.coord_of(spec, None, loc))
            except Exception:
                self.sel_altaz = (name, None, None)
            if time.time() - last_bodies > 30:
                self.chart_objects = self._objects_now()
                last_bodies = time.time()
            time.sleep(1.0)

    def _update_table(self):
        if not self.chart_objects:
            return
        names = [o[0] for o in self.chart_objects]
        ra = np.array([o[1] for o in self.chart_objects])
        dec = np.array([o[2] for o in self.chart_objects])
        az, alt = C.radec_to_altaz(ra, dec, self.lst, self.site["lat"])
        for n, a, e in zip(names, az, alt):
            if not self.tree.exists(n):
                continue
            self.tree.item(n, values=("%.1f" % a, "%.1f" % e),
                           tags=("below",) if e < 0 else ())

    def _write_log(self, lines):
        try:
            with open(C.LOG_FILE, "a", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
        except Exception:
            pass
        for line in lines:
            if any(k in line for k in TOAST_KEYS):
                self.lbl_toast.configure(text=line.split("] ", 1)[-1][:100])
                self._toast_until = time.time() + 6.0

    # ================================================================ main tick
    def _tick(self):
        if not self._alive:
            return
        self._tick_n += 1
        lines = []
        try:
            while True:
                lines.append(self.q.get_nowait()[1])
        except queue.Empty:
            pass
        if lines:
            self._write_log(lines)
        if self._toast_until and time.time() > self._toast_until:
            self._toast_until = 0
            self.lbl_toast.configure(text="")

        m = self.mount
        # position readout
        az, el = m.sky_position()
        bad = not (C.EL_MIN - 0.01 <= el <= C.EL_MAX + 0.01)
        ra, dec = C.altaz_to_radec(az, el, self.lst, self.site["lat"])
        self._set("az", self.ro["Azimuth"], text="%06.2f°" % az)
        self._set("el", self.ro["Elevation"], text="%05.2f°" % el,
                  text_color=RED if bad else TEXT)
        self._set("ra", self.ro["RA"], text=C.ra_str(ra))
        self._set("dec", self.ro["Dec"], text=C.dec_str(dec))
        lst_h = self.lst / 15.0
        self._set("lst", self.ro["LST"], text="%02d:%02d:%02d" % (
            int(lst_h), int(lst_h * 60) % 60, int(lst_h * 3600) % 60))
        tz = self.site["tz"]
        pc_tz = C.pc_utc_offset()
        self._set("pctz", self.lbl_pctz, text="" if abs(pc_tz - tz) < 0.01 else
                  "This PC is set to UTC%+g - check the time zone." % pc_tz)
        local = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=tz)
        self._set("lt", self.ro["Local time"], text=local.strftime("%H:%M:%S"))
        if not m.connected:
            state, col = "Offline", MUTED
        elif m.moving:
            state, col = "Slewing", ACCENT
        elif self.tracker.enabled:
            state, col = "Tracking", GREEN
        else:
            state, col = "Idle", TEXT
        self._set("state", self.ro["State"], text=state, text_color=col)
        m.save_state()
        if (m.moving and not m.sim and not m.cmd_warned
                and time.time() - m.cmd_time > 3.0 and m.steps == m.cmd_steps):
            m.cmd_warned = True
            self.log("WARNING: Arduino did not execute the move command")

        # connection
        if self._connecting:
            self._set("conn", self.lbl_conn, text="Connecting…", text_color=MUTED)
        elif m.connected:
            self._set("conn", self.lbl_conn, text="● Sim" if m.sim else "● Online",
                      text_color=GREEN)
        else:
            self._set("conn", self.lbl_conn, text="Off", text_color=MUTED)
        self._set("bconn", self.btn_conn, text="Disconnect" if m.connected else "Connect",
                  fg_color=BTN if m.connected else ACCENT,
                  text_color=TEXT if m.connected else "#FFFFFF",
                  hover_color=BTN_H if m.connected else ACCENT_H)
        if self._ports is not None and self._ports != self._ports_shown:
            self._refresh_ports(self._ports)

        # selected object
        sa = self.sel_altaz
        if sa and sa[0] == self.selected and sa[1] is not None:
            self._set("selaz", self.ro_sel["Az / El"], text="%06.2f°  %+06.2f°" % (sa[1], sa[2]),
                      text_color=MUTED if sa[2] < 0 else TEXT)
        elif sa and sa[0] == self.selected:
            self._set("selaz", self.ro_sel["Az / El"], text="invalid", text_color=RED)
        rd = self.sel_radec
        if rd and rd[0] == self.selected and self._ui_cache.get("radec") != rd:
            self._ui_cache["radec"] = rd
            for e, v in ((self.e_ra, rd[1]), (self.e_dec, rd[2])):
                e.configure(state="normal")
                e.delete(0, "end")
                e.insert(0, v)
                e.configure(state="disabled")
        self._set("trk", self.lbl_track, text=self.tracker.status if self.tracker.enabled else "",
                  text_color=GREEN)
        if self.tracker.enabled != bool(self.sw_track.get()):
            (self.sw_track.select if self.tracker.enabled else self.sw_track.deselect)()

        # slower updates
        if self._tick_n % 10 == 0:
            self.on_chart_view()
            self.chart.redraw()
        if self._tick_n % 30 == 1:
            self._update_table()
        self.after(100, self._tick)

    def site_dialog(self, first=False):
        """Observing site (shown at the first start and from the menu)."""
        dlg, b = self._dialog("Observing site")
        if first:
            ctk.CTkLabel(b, text="Welcome - confirm your observing site. You can change it "
                                 "later in the settings panel (three-line button, top left).",
                         font=self.F["body"], text_color=MUTED, anchor="w", justify="left",
                         wraplength=380).pack(fill="x", pady=(0, 8))
        tz_pc = C.pc_utc_offset()
        es = self.form(b, [("Site name", self.site.get("name", "")),
                           ("Latitude (°)", C.fmt_num(self.site["lat"])),
                           ("Longitude (°)", C.fmt_num(self.site["lon"])),
                           ("Height (m)", C.fmt_num(self.site["h"])),
                           ("Time zone (UTC+)", C.fmt_num(self.site["tz"]))])
        ctk.CTkLabel(b, text="This computer is set to UTC%+g. Latitude: south is negative; "
                             "longitude: east is positive." % tz_pc,
                     font=self.F["small"], text_color=MUTED, anchor="w", justify="left",
                     wraplength=380).pack(fill="x", pady=(6, 0))
        var = tk.BooleanVar(value=self.site.get("ask_on_start", False))
        ctk.CTkCheckBox(b, text="Show this window every time the program starts",
                        variable=var, font=self.F["body"], text_color=TEXT, checkbox_width=16,
                        checkbox_height=16, corner_radius=3, border_width=1, fg_color=ACCENT,
                        hover_color=ACCENT_H, border_color=FIELD_BORDER).pack(anchor="w",
                                                                             pady=(8, 0))
        err = ctk.CTkLabel(b, text="", font=self.F["small"], text_color=RED, anchor="w")
        err.pack(fill="x")

        def save():
            try:
                site = {"name": es[0].get().strip(),
                        "lat": float(es[1].get().replace(",", ".")),
                        "lon": float(es[2].get().replace(",", ".")),
                        "h": float(es[3].get().replace(",", ".")),
                        "tz": float(es[4].get().replace(",", ".")),
                        "ask_on_start": bool(var.get())}
                C.save_site(site)
            except Exception as e:
                err.configure(text=str(e) if "range" in str(e) else "Please enter numbers")
                return
            self._set_site(site)
            self.log("Site saved: %s lat %.4f, lon %.4f, UTC%+g"
                     % (site["name"] + "," if site["name"] else "", site["lat"], site["lon"],
                        site["tz"]))
            dlg.destroy()

        self._dialog_buttons(dlg, b, "Save", save, cancel=not first)
        if first:
            dlg.protocol("WM_DELETE_WINDOW", save)

    def _set_site(self, site):
        self.site = dict(site)
        self.tracker.set_site(site)
        for e, k in ((self.e_site, "name"), (self.e_lat, "lat"), (self.e_lon, "lon"),
                     (self.e_h, "h"), (self.e_tz, "tz")):
            e.delete(0, "end")
            e.insert(0, site[k] if k == "name" else C.fmt_num(site[k]))
        self.var_ask.set(site.get("ask_on_start", False))
        self.chart_objects = self._objects_now()

    def _on_sys(self, value):
        gal = value.startswith("Galactic")
        self.lbl_c1.configure(text="l (°)" if gal else "RA (J2000)")
        self.lbl_c2.configure(text="b (°)" if gal else "Dec (J2000)")
        for e, v in ((self.e_ra, "0" if gal else "00:00:00"),
                     (self.e_dec, "0" if gal else "+00:00:00")):
            e.delete(0, "end")
            e.insert(0, v)

    def do_save_object(self):
        gal = self.opt_sys.get().startswith("Galactic")
        try:
            C.add_user_object(self.e_objname.get(), "gal" if gal else "icrs",
                              self.e_ra.get().replace(",", ".") if gal else self.e_ra.get(),
                              self.e_dec.get().replace(",", ".") if gal else self.e_dec.get())
        except Exception as e:
            self.log("Cannot save object: %s" % (e if isinstance(e, ValueError) and
                                                 "name" in str(e) else "invalid coordinates"))
            return
        name = self.e_objname.get().strip()
        self._rebuild_tree()
        self.static_objects = self._static_objects()
        self.chart_objects = self._objects_now()
        self.log("Object saved: %s" % name)
        self.select_object(name)

    def do_delete_object(self):
        name = self.selected
        C.delete_user_object(name)
        self._rebuild_tree()
        self.static_objects = self._static_objects()
        self.chart_objects = self._objects_now()
        self.log("Object deleted: %s" % name)
        self.select_object(C.SUN)

    def _rebuild_tree(self):
        for item in self.tree.get_children():
            self.tree.delete(item)
        groups = {}
        for cat in (C.CAT_SOLAR, C.CAT_RADIO, C.CAT_MW, C.CAT_USER, C.CAT_CUSTOM):
            groups[cat] = self.tree.insert("", "end", text=cat, open=True, tags=("group",))
        for name in C.CATALOG:
            self.tree.insert(groups[C.category(name)], "end", iid=name, text=name,
                             values=("", ""))
        self._update_table()

    def _objects_now(self):
        objs = list(self.static_objects)
        for body in (C.SUN, C.MOON):
            try:
                ra, dec = C.radec_deg_of(C.CATALOG[body], self.site)
                objs.insert(0, (body, ra, dec))
            except Exception:
                pass
        return objs

    def on_close(self):
        self._alive = False
        try:
            self.tracker.shutdown()
            self.mount.stop()
            self.mount.save_state(force=True)
            self.mount.disconnect()
        except Exception:
            pass
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
