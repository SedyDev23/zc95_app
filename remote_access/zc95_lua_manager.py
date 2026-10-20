"""ZC95 Lua Manager - Windows GUI for a ZC95 connected over WiFi.

Two views, picked with the buttons along the top:
  Scripts - list / upload / delete Lua scripts (what lua_manage.py and lua_upload.py do)
  Control - start a pattern and drive it: menu options, soft button, channel power
            (what webgui.py + web_static/ do in a browser)

The layout and colours match the ZC95 Web Manager (zc95_web).
Reuses lib/ZcWs.py and lib/ZcMessages.py over one websocket connection to the ZC95.

Run:  py zc95_lua_manager.py        (or pyw zc95_lua_manager.py for no console)
"""

import base64
import ctypes
import functools
import json
import os
import queue
import re
import socket
import struct
import sys
import threading
import time
import tkinter as tk
import zlib
from tkinter import filedialog, messagebox, ttk

# Make "lib.*" importable no matter where the app is launched from
APP_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP_DIR)

import lib.ZcMessages as zc  # noqa: E402
from lib.ZcWs import ZcWs  # noqa: E402

CONNECT_TIMEOUT = 6                  # seconds to wait for the websocket to open
PING_INTERVAL_MS = 5000              # how often the availability indicator is refreshed
POWER_INTERVAL_MS = 250              # SetPower is sent at most this often, same as pattern_gui.py / WebHub.py
CHANNEL_STEP = 10                    # power is 0-1000, so 10 = 1%
SETTINGS_FILE = os.path.join(os.environ.get("APPDATA", APP_DIR), "zc95_lua_manager", "settings.json")

FONT = "Segoe UI"
MONO = "Consolas"

# Same palette as the web version (zc95_web/static/app.css)
THEMES = {
  "light": {"page": "#f5f5f7", "card": "#ffffff", "text": "#1c1c1e", "muted": "#6b6b70", "border": "#d8d8de",
            "hover": "#ececf0", "accent": "#d32f2f", "accent_active": "#b71c1c", "accent_text": "#ffffff",
            "danger": "#b3261e", "danger_active": "#8e1b15", "ok": "#2e7d43", "warn": "#a06800",
            "max": "#3b63c2", "actual": "#d6a600", "limit": "#2e7d43", "thumb": "#c4c4cc", "thumb_active": "#a0a0a8"},
  "dark":  {"page": "#14151a", "card": "#1f2129", "text": "#f0f0f2", "muted": "#9a9ba6", "border": "#33333f",
            "hover": "#2a2c36", "accent": "#d32f2f", "accent_active": "#e04848", "accent_text": "#ffffff",
            "danger": "#b3261e", "danger_active": "#cf3a30", "ok": "#3ba55c", "warn": "#e0a020",
            "max": "#4d7cf0", "actual": "#ffd23f", "limit": "#3ba55c", "thumb": "#4a4c58", "thumb_active": "#6a6c78"},
}


class LogWriter:
  """Stands in for sys.stdout/stderr so the lib's print() output lands in the log pane
  (and so pythonw, which has no stdout, doesn't crash on print)."""

  def __init__(self, log_queue):
    self.log_queue = log_queue

  def write(self, text):
    if text:
      self.log_queue.put(text)

  def flush(self):
    pass


class ZcLink:
  """The one websocket connection to the ZC95, shared by both views.

  ZcMessages calls block (up to 6s) waiting for a reply and ZcWs.recv isn't thread safe, so every
  request runs on a single worker thread, in order. Messages the ZC95 sends on its own (PowerStatus,
  MenuOptionChanged, Lua output, errors) land in rcv_queue, which the Tk thread reads."""

  def __init__(self):
    self.rcv_queue = queue.Queue()
    self.jobs = queue.Queue()
    self.conn = None
    self.ws_thread = None
    self.messages = None
    self.ip = None
    threading.Thread(target=self._worker, daemon=True).start()

  @property
  def connected(self):
    return self.ws_thread is not None and self.ws_thread.is_alive()

  def submit(self, ip, func, *args, done=None):
    """Run func(zc_messages, *args) on the worker thread, connecting (or reconnecting) to ip first."""
    self.jobs.put((ip, func, args, done))

  def disconnect(self, done=None):
    self.jobs.put((None, None, (), done))

  def _worker(self):
    while True:
      ip, func, args, done = self.jobs.get()
      try:
        if func is None:
          self._disconnect()
        else:
          func(self._connect(ip), *args)
      except Exception as e:
        print("Error: " + str(e))
      finally:
        if done:
          done()

  def _connect(self, ip):
    if self.connected and ip == self.ip:
      return self.messages
    self._disconnect()

    conn = ZcWs(ip, self.rcv_queue, False)
    thread = threading.Thread(target=conn.run_forever, daemon=True)
    thread.start()
    # ZcWs.wait_for_connection() blocks forever if the ZC95 is offline, so wait on its event with a timeout
    if not conn._ZcWs__connection_wait_event.wait(CONNECT_TIMEOUT):
      conn.stop()
      raise ConnectionError("Could not connect to ZC95 at " + ip)

    self.conn, self.ws_thread, self.ip = conn, thread, ip
    self.messages = zc.ZcMessages(conn, False)
    return self.messages

  def _disconnect(self):
    if self.conn is not None:
      self.conn.stop()
      self.ws_thread.join(2)
    self.conn = self.ws_thread = self.messages = None


# ---------- Rounded skin ----------
#
# Tk can't round corners, so the rounded buttons, cards, fields and pills are small anti-aliased PNG
# images drawn here (no image library needed) and used as ttk "image" elements. Each one is a
# 9-slice: the corners stay fixed and the middle stretches to the widget's size.

def _rgb(color):
  return tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))


def _in_rounded_rect(px, py, x0, y0, x1, y1, r):
  cx = min(max(px, x0 + r), x1 - r)
  cy = min(max(py, y0 + r), y1 - r)
  return (px - cx) ** 2 + (py - cy) ** 2 <= r * r


@functools.lru_cache(maxsize=None)   # switching back to a theme reuses its images
def rounded_png(w, h, r, fill, outline=None, line=1, samples=4, shape=None, offset=(0, 0)):
  """Base64 PNG of a w x h rounded rectangle (radius r) with an optional outline; outside is transparent.
  With shape=(width, height) and offset, it renders just that w x h window of a larger rounded shape
  (used to cut out the four corners of a panel)."""
  f = _rgb(fill)
  o = _rgb(outline) if outline else f
  sw, sh = shape or (w, h)
  ox, oy = offset
  step = 1.0 / samples
  rows = bytearray()
  solid_fill = bytes(f + (255,))
  solid_outline = bytes(o + (255,))
  for y in range(h):
    rows.append(0)   # PNG filter type: none
    for x in range(w):
      # Away from the four rounded corners the result is exact, so skip the anti-aliasing there
      x0, y0 = ox + x, oy + y
      if (r + 1 <= x0 and x0 + 1 <= sw - r - 1) or (r + 1 <= y0 and y0 + 1 <= sh - r - 1):
        edge = outline and (x0 < line or x0 + 1 > sw - line or y0 < line or y0 + 1 > sh - line)
        rows += solid_outline if edge else solid_fill
        continue
      red = green = blue = hits = 0
      for sy in range(samples):
        py = oy + y + (sy + 0.5) * step
        for sx in range(samples):
          px = ox + x + (sx + 0.5) * step
          if not _in_rounded_rect(px, py, 0, 0, sw, sh, r):
            continue
          inner = not outline or _in_rounded_rect(px, py, line, line, sw - line, sh - line, max(r - line, 0))
          c = f if inner else o
          red += c[0]; green += c[1]; blue += c[2]; hits += 1
      if hits:
        rows += bytes((red // hits, green // hits, blue // hits, 255 * hits // (samples * samples)))
      else:
        rows += b"\0\0\0\0"

  def chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)

  png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
         + chunk(b"IDAT", zlib.compress(bytes(rows))) + chunk(b"IEND", b""))
  return base64.b64encode(png).decode("ascii")


class Skin:
  """Creates the rounded ttk image elements once, and redraws their images in place on a theme change."""

  def __init__(self, root, style):
    self.root = root
    self.style = style
    self.images = {}
    self.created = set()

  def image(self, key, w, h, r, fill, outline=None, line=1):
    data = rounded_png(w, h, r, fill, outline, line)
    img = self.images.get(key)
    if img is None:
      img = self.images[key] = tk.PhotoImage(master=self.root, data=data)
    else:
      img.configure(data=data)
    return img

  # ttk stretches an image element by *tiling* its centre, so a 1-pixel centre means thousands of draws
  # for a wide widget (very slow). The centre is made big, and width/height keep the minimum size small.
  CENTRE = 48

  def element(self, name, r, states, padding=2, size=None, border=None, line=1, min_size=None):
    """states: [(statespec, fill, outline), ...]; the first is the normal look, e.g. ("", red, None)."""
    corner = 2 * (r + 1)
    w, h = size or (corner + self.CENTRE, corner + self.CENTRE)
    min_w, min_h = min_size or (corner + 1, corner + 1)
    images = [(spec, self.image(name + ":" + (spec or "normal"), w, h, r, fill, outline, line))
              for spec, fill, outline in states]
    if name not in self.created:
      maps = [tuple(spec.split()) + (img,) for spec, img in images[1:]]
      self.style.element_create(name, "image", images[0][1], *maps,
                                border=border if border is not None else r + 1, padding=padding,
                                width=min_w, height=min_h, sticky="nsew")
      self.created.add(name)

  def button(self, style_name, element, label="Button"):
    """Lay out a button (or Toolbutton) style as: rounded image > padding > label."""
    self.style.layout(style_name, [(element, {"sticky": "nsew", "children": [
      (label + ".padding", {"sticky": "nsew", "children": [(label + ".label", {"sticky": "nsew"})]})]})])

_corner_images = {}


def panel_corners(root, r, fill, outline):
  """The four anti-aliased corners (nw, ne, sw, se) of a rounded panel, cached by look."""
  key = (r, fill, outline)
  if key not in _corner_images:
    size = 2 * r + 2
    _corner_images[key] = [
      tk.PhotoImage(master=root, data=rounded_png(r, r, r, fill, outline, shape=(size, size), offset=(ox, oy)))
      for ox, oy in ((0, 0), (size - r, 0), (0, size - r), (size - r, size - r))]
  return _corner_images[key]


class RoundPanel(tk.Frame):
  """A rounded panel (card, list well, tab box, status pill). Put children in .inner.

  The rounded background is drawn on a canvas placed *behind* the content, so it never takes part in
  the layout. (ttk image-element frames do the same job, but got into an endless resize loop with pack.)"""

  def __init__(self, parent, app, radius, fill, outline, behind, padding=16, inner_style="TFrame"):
    super().__init__(parent, bd=0, highlightthickness=0)
    self.app = app
    self.radius = radius
    self.keys = [fill, outline, behind]   # palette keys: panel colour, outline colour, colour around it
    self.canvas = tk.Canvas(self, bd=0, highlightthickness=0)
    self.canvas.place(x=0, y=0, relwidth=1, relheight=1)
    self.tk.call("lower", self.canvas._w)   # (Canvas.lower() lowers canvas items, not the widget)
    px, py = (padding, padding) if isinstance(padding, int) else (padding[0], padding[1])
    self.inner = ttk.Frame(self, style=inner_style)
    self.inner.pack(fill="both", expand=True, padx=px, pady=py)
    self.canvas.bind("<Configure>", lambda e: self.draw())
    app.panels.append(self)
    self.draw()

  def set_outline(self, key):
    self.keys[1] = key
    self.draw()

  def draw(self):
    c = self.app.colors()
    fill, outline, behind = (c[k] for k in self.keys)
    self.configure(bg=behind)
    self.canvas.configure(bg=behind)
    self.canvas.delete("all")
    w, h, r = self.canvas.winfo_width(), self.canvas.winfo_height(), self.radius
    if w < 2 * r or h < 2 * r:
      return
    self.canvas.create_rectangle(r, 0, w - r, h, fill=fill, width=0)
    self.canvas.create_rectangle(0, r, w, h - r, fill=fill, width=0)
    self.canvas.create_line(r, 0, w - r, 0, fill=outline)
    self.canvas.create_line(r, h - 1, w - r, h - 1, fill=outline)
    self.canvas.create_line(0, r, 0, h - r, fill=outline)
    self.canvas.create_line(w - 1, r, w - 1, h - r, fill=outline)
    nw, ne, sw, se = panel_corners(self, r, fill, outline)
    self.canvas.create_image(0, 0, image=nw, anchor="nw")
    self.canvas.create_image(w, 0, image=ne, anchor="ne")
    self.canvas.create_image(0, h, image=sw, anchor="sw")
    self.canvas.create_image(w, h, image=se, anchor="se")


_thumb_images = {}


def slider_thumb(root, colors):
  """The round slider handle: accent fill with a white ring, as on the web page."""
  key = (colors["accent"], colors["accent_text"])
  if key not in _thumb_images:
    _thumb_images[key] = tk.PhotoImage(master=root, data=rounded_png(22, 22, 11, colors["accent"], colors["accent_text"], 2))
  return _thumb_images[key]


class RoundSlider(tk.Canvas):
  """A slider with a rounded track and a round red handle, like the web page's range inputs."""

  HEIGHT = 26
  TRACK = 8

  def __init__(self, parent, from_, to, command, colors, resolution=1):
    super().__init__(parent, height=self.HEIGHT, highlightthickness=0, bd=0)
    self.from_, self.to, self.resolution = from_, to, resolution
    self.command = command
    self.value = from_
    self.colors = colors
    self.bind("<Configure>", lambda e: self.draw())
    self.bind("<ButtonPress-1>", self._drag)
    self.bind("<B1-Motion>", self._drag)
    self.set_colors(colors)

  def set(self, value):
    self.value = value
    self.draw()

  def get(self):
    return self.value

  def set_colors(self, colors):
    self.colors = colors
    self.configure(bg=colors["card"])
    self.draw()

  def _x_range(self):
    pad = 11   # half the handle, so it never overhangs the ends
    return pad, max(self.winfo_width() - pad, pad + 1)

  def _drag(self, event):
    x0, x1 = self._x_range()
    frac = min(max((event.x - x0) / (x1 - x0), 0), 1)
    raw = self.from_ + frac * (self.to - self.from_)
    value = self.from_ + round((raw - self.from_) / self.resolution) * self.resolution
    value = max(self.from_, min(self.to, value))
    if value != self.value:
      self.set(value)
      self.command(value)

  def draw(self):
    self.delete("all")
    x0, x1 = self._x_range()
    cy = self.HEIGHT // 2
    self.create_line(x0, cy, x1, cy, width=self.TRACK, capstyle="round", fill=self.colors["border"])
    span = (self.to - self.from_) or 1
    x = x0 + (self.value - self.from_) / span * (x1 - x0)
    self.create_image(x, cy, image=slider_thumb(self, self.colors))


class RoundBar(tk.Canvas):
  """The upload progress bar: a rounded track filling with red."""

  HEIGHT = 8

  def __init__(self, parent, colors):
    super().__init__(parent, height=self.HEIGHT, highlightthickness=0, bd=0)
    self.value, self.maximum = 0, 1
    self.colors = colors
    self.bind("<Configure>", lambda e: self.draw())
    self.set_colors(colors)

  def set_progress(self, value, maximum=None):
    if maximum is not None:
      self.maximum = max(maximum, 1)
    self.value = value
    self.draw()

  def set_colors(self, colors):
    self.colors = colors
    self.configure(bg=colors["card"])
    self.draw()

  def draw(self):
    self.delete("all")
    w, h = self.winfo_width(), self.HEIGHT
    if w <= h:
      return
    r = h / 2
    self.create_line(r, r, w - r, r, width=h, capstyle="round", fill=self.colors["border"])
    if self.value > 0:
      x = r + (w - 2 * r) * min(self.value / self.maximum, 1)
      self.create_line(r, r, x, r, width=h, capstyle="round", fill=self.colors["accent"])


# ---------- Control view widgets ----------

class ScrollFrame(ttk.Frame):
  """A frame with a vertical scrollbar; put children in .inner"""

  def __init__(self, parent):
    super().__init__(parent)
    self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0)
    scroll = ttk.Scrollbar(self, command=self.canvas.yview, style="Slim.Vertical.TScrollbar")
    self.canvas.configure(yscrollcommand=scroll.set)
    scroll.pack(side="right", fill="y")
    self.canvas.pack(side="left", fill="both", expand=True)

    self.inner = ttk.Frame(self.canvas)
    window = self.canvas.create_window(0, 0, window=self.inner, anchor="nw")
    self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
    self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(window, width=e.width))
    self.bind("<Enter>", lambda e: self.bind_all("<MouseWheel>", self._on_wheel))
    self.bind("<Leave>", lambda e: self.unbind_all("<MouseWheel>"))

  def _on_wheel(self, event):
    if self.inner.winfo_height() > self.canvas.winfo_height():
      self.canvas.yview_scroll(int(-event.delta / 120), "units")


class MinMaxControl:
  """A MIN_MAX menu item: title and value, then a -/slider/+ row."""

  def __init__(self, parent, item, on_change, colors):
    self.item = item
    self.on_change = on_change
    self.min, self.max = item["Min"], item["Max"]
    self.step = item.get("IncrementStep") or 1
    self.value = item["Default"]
    self.dragging = False
    self.updating = False

    self.frame = ttk.Frame(parent, padding=(0, 0, 0, 12))
    self.frame.columnconfigure(1, weight=1)
    title = item["Title"] + (" (" + item["UoM"] + ")" if item.get("UoM") else "")
    ttk.Label(self.frame, text=title).grid(row=0, column=0, columnspan=2, sticky="w")
    self.value_label = ttk.Label(self.frame, text=str(self.value), style="Muted.TLabel")
    self.value_label.grid(row=0, column=2, sticky="e")

    ttk.Button(self.frame, text="−", style="Step.TButton", command=lambda: self.step_by(-self.step)).grid(row=1, column=0, pady=(6, 0))
    self.scale = RoundSlider(self.frame, self.min, self.max, self._on_scale, colors)
    self.scale.grid(row=1, column=1, sticky="ew", padx=10, pady=(6, 0))
    ttk.Button(self.frame, text="+", style="Step.TButton", command=lambda: self.step_by(self.step)).grid(row=1, column=2, pady=(6, 0))
    self.scale.bind("<ButtonPress-1>", lambda e: setattr(self, "dragging", True), add="+")
    self.scale.bind("<ButtonRelease-1>", self._on_release, add="+")
    self._show(self.value)

  def _snap(self, value):
    value = self.min + round((float(value) - self.min) / self.step) * self.step
    return int(max(self.min, min(self.max, value)))

  def _show(self, value):
    self.value = value
    self.value_label.configure(text=str(value))
    self.updating = True
    self.scale.set(value)
    self.updating = False

  def _on_scale(self, raw):
    if not self.updating and self._snap(raw) != self.value:
      self.value = self._snap(raw)
      self.value_label.configure(text=str(self.value))

  def _on_release(self, event):
    self.dragging = False
    self._show(self.value)
    self.on_change(self.item["Id"], self.value)

  def step_by(self, delta):
    self._show(self._snap(self.value + delta))
    self.on_change(self.item["Id"], self.value)

  def set_from_zc95(self, value):
    # Don't yank the slider out from under the mouse while it's being dragged
    if not self.dragging:
      self._show(value)


class ChannelControl:
  """One channel: label, %, output bar (blue = max power, yellow = actual, hatched = beyond the
  front-panel limit) and a -/slider/+ power row."""

  BAR_HEIGHT = 10

  def __init__(self, parent, number, on_change, colors):
    self.on_change = on_change
    self.value = 0
    self.max_out = self.actual = 0
    self.limit = 1000
    self.colors = colors
    self.updating = False

    self.frame = ttk.Frame(parent, padding=(0, 0, 0, 12))
    self.frame.columnconfigure(1, weight=1)
    ttk.Label(self.frame, text="Channel " + str(number), style="Bold.TLabel").grid(row=0, column=0, columnspan=2, sticky="w")
    self.pct_label = ttk.Label(self.frame, text="0%", style="Muted.TLabel")
    self.pct_label.grid(row=0, column=2, sticky="e")

    self.bar = tk.Canvas(self.frame, height=self.BAR_HEIGHT, highlightthickness=0, borderwidth=0)
    self.bar.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(6, 2))
    self.bar.bind("<Configure>", lambda e: self.draw())

    ttk.Button(self.frame, text="−", style="Step.TButton", command=lambda: self.step_by(-CHANNEL_STEP)).grid(row=2, column=0, pady=(6, 0))
    self.scale = RoundSlider(self.frame, 0, 1000, self._on_scale, colors, resolution=CHANNEL_STEP)
    self.scale.grid(row=2, column=1, sticky="ew", padx=10, pady=(6, 0))
    ttk.Button(self.frame, text="+", style="Step.TButton", command=lambda: self.step_by(CHANNEL_STEP)).grid(row=2, column=2, pady=(6, 0))
    self.set_colors(colors)

  def _on_scale(self, raw):
    value = int(round(float(raw) / CHANNEL_STEP) * CHANNEL_STEP)
    if not self.updating and value != self.value:
      self.set(value, notify=True)

  def step_by(self, delta):
    self.set(max(0, min(1000, self.value + delta)), notify=True)

  def set(self, value, notify=False):
    self.value = value
    self.pct_label.configure(text=str(round(value / 10)) + "%")
    self.updating = True
    self.scale.set(value)
    self.updating = False
    if notify:
      self.on_change()

  def set_status(self, channel):
    self.max_out, self.actual, self.limit = channel["MaxOutputPower"], channel["OutputPower"], channel["PowerLimit"]
    self.draw()

  def set_colors(self, colors):
    self.colors = colors
    self.bar.configure(bg=colors["card"])
    self.scale.set_colors(colors)
    self.draw()

  def draw(self):
    # Rounded bars: lines with round caps, so the ends are semicircles
    c, w, h = self.colors, self.bar.winfo_width(), self.BAR_HEIGHT
    self.bar.delete("all")
    if w <= h:
      return
    r = h / 2
    span = w - 2 * r
    self.bar.create_line(r, r, w - r, r, width=h, capstyle="round", fill=c["border"])
    if self.limit < 1000:
      x = r + span * self.limit / 1000
      for hx in range(int(x) - h, int(w - r), 5):   # diagonal hatching beyond the front-panel limit
        self.bar.create_line(max(hx, x), h - max(0, x - hx), min(hx + h, w - r), max(0, hx + h - (w - r)),
                             fill=c["limit"], width=2)
      self.bar.create_line(x, 0, x, h, fill=c["limit"], width=2)
    if self.max_out > 0:
      self.bar.create_line(r, r, r + span * self.max_out / 1000, r, width=h, capstyle="round", fill=c["max"])
    if self.actual > 0:
      self.bar.create_line(r, r, r + span * self.actual / 1000, r, width=h - 4, capstyle="round", fill=c["actual"])


class ControlView(ttk.Frame):
  """Native version of the web UI's Control tab: pick and start a pattern, then drive it."""

  def __init__(self, parent, app):
    super().__init__(parent, style="Page.TFrame")
    self.app = app
    self.patterns = []
    self.detail = None        # PatternDetail of the running pattern, None when stopped
    self.minmax = {}          # MenuId -> MinMaxControl
    self.choices = {}         # MenuId -> IntVar
    self.channels = []
    self.menu_scroll = None
    self.power_dirty = False
    self.power_job_pending = False

    # Pattern picker card
    self.picker, inner = app.card(self)
    ttk.Label(inner, text="PATTERN", style="Section.TLabel").pack(anchor="w", pady=(0, 6))
    row = ttk.Frame(inner)
    row.pack(fill="x")
    self.pattern_combo = ttk.Combobox(row, state="readonly", font=(FONT, 10))
    self.pattern_combo.pack(side="left", fill="x", expand=True)
    ttk.Button(row, text="Start", style="Primary.TButton", command=self.start).pack(side="left", padx=(10, 0))
    ttk.Button(row, text="↻", style="Step.TButton", command=self.load_patterns).pack(side="left", padx=(10, 0))
    self.picker_hint = ttk.Label(inner, text="", style="Hint.TLabel")
    self.picker_hint.pack(anchor="w", pady=(6, 0))
    self.picker.pack(fill="x")

    # Running pattern card (filled in show_running)
    self.running, self.running_inner = app.card(self)

    self.after(POWER_INTERVAL_MS, self.power_tick)

  # ----- patterns -----

  def load_patterns(self):
    if self.detail is None:
      self.picker_hint.configure(text="Loading...")
      self.app.zc_job(self._do_load_patterns)

  def _do_load_patterns(self, msgs):
    patterns = msgs.GetPatterns()
    self.app.on_ui(self.show_patterns, patterns)

  def show_patterns(self, patterns):
    previous = self.pattern_combo.get()
    self.patterns = patterns
    names = [p["Name"] for p in patterns]
    self.pattern_combo.configure(values=names)
    if previous in names:
      self.pattern_combo.set(previous)
    elif names:
      self.pattern_combo.current(0)
    self.picker_hint.configure(text=str(len(names)) + " patterns on the ZC95")

  def start(self):
    index = self.pattern_combo.current()
    if index < 0:
      messagebox.showwarning("No pattern", "Load the pattern list and pick a pattern first.")
      return
    self.app.zc_job(self._do_start, self.patterns[index]["Id"])

  def _do_start(self, msgs, pattern_id):
    detail = msgs.GetPatternDetails(pattern_id)
    if detail is None:
      raise RuntimeError("Couldn't get pattern details")
    msgs.PatternStart(pattern_id)
    self.app.on_ui(self.show_running, detail)

  def stop(self):
    self.app.zc_job(self._do_stop)

  def _do_stop(self, msgs):
    msgs.SendPatternStopMessage()
    self.app.on_ui(self.show_picker)

  def stop_on_exit(self):
    """Queue a PatternStop if a pattern is running, so closing the app doesn't leave it going."""
    if self.detail is not None and self.app.link.connected:
      self.app.link.submit(self.app.link.ip, lambda msgs: msgs.SendPatternStopMessage())

  # ----- running pattern UI -----

  def show_picker(self):
    self.detail = None
    self.menu_scroll = None
    self.channels = []
    self.running.pack_forget()
    for child in self.running_inner.winfo_children():
      child.destroy()
    self.picker.pack(fill="x")

  def show_running(self, detail):
    self.show_picker()
    self.picker.pack_forget()
    self.detail = detail
    self.minmax, self.choices, self.channels = {}, {}, []
    self.power_dirty = False
    colors = self.app.colors()
    inner = self.running_inner

    header = ttk.Frame(inner)
    header.pack(fill="x")
    ttk.Label(header, text=detail["Name"], style="H2.TLabel").pack(side="left")
    # PatternDetail's Name lacks the "(!)" triphase prefix that PatternList has, so check the list (same as app.js)
    entry = next((p for p in self.patterns if p["Id"] == detail.get("Id")), None)
    if entry and entry["Name"].startswith("(!)"):
      ttk.Label(header, text="(!) triphase", style="Warn.TLabel").pack(side="left", padx=10)
    ttk.Button(header, text="Stop", style="Danger.TButton", command=self.stop).pack(side="right")

    if detail.get("ButtonA"):
      soft = ttk.Button(inner, text=detail["ButtonA"], style="Hold.TButton")
      soft.pack(fill="x", pady=(14, 0), ipady=8)
      soft.bind("<ButtonPress-1>", lambda e: self.soft_button(True))
      soft.bind("<ButtonRelease-1>", lambda e: self.soft_button(False))

    # Settings (left) and channels (right) scroll together when the window is too short for them
    scroll = self.menu_scroll = ScrollFrame(inner)
    scroll.pack(fill="both", expand=True, pady=(16, 0))
    scroll.canvas.configure(bg=colors["card"])
    body = scroll.inner
    body.columnconfigure(0, weight=1, uniform="col")
    body.columnconfigure(1, weight=1, uniform="col")

    ttk.Label(body, text="Pattern settings", style="H3.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 8))
    menu_box = ttk.Frame(body)
    menu_box.grid(row=1, column=0, sticky="new", padx=(0, 14))
    for item in detail.get("MenuItems", []):
      if item["Type"] == "MIN_MAX":
        control = MinMaxControl(menu_box, item, self.menu_min_max, colors)
        control.frame.pack(fill="x")
        self.minmax[item["Id"]] = control
      elif item["Type"] == "MULTI_CHOICE":
        self.build_multi_choice(menu_box, item)
    if not detail.get("MenuItems"):
      ttk.Label(menu_box, text="This pattern has no settings.", style="Muted.TLabel").pack(anchor="w")

    ttk.Label(body, text="Channel power", style="H3.TLabel").grid(row=0, column=1, sticky="w", padx=(14, 8), pady=(0, 8))
    chan_box = ttk.Frame(body)
    chan_box.grid(row=1, column=1, sticky="new", padx=(14, 8))
    for number in range(1, 5):
      channel = ChannelControl(chan_box, number, self.power_changed, colors)
      channel.frame.pack(fill="x")
      self.channels.append(channel)

    self.running.pack(fill="both", expand=True)

  def build_multi_choice(self, parent, item):
    frame = ttk.Frame(parent, padding=(0, 0, 0, 12))
    frame.pack(fill="x")
    ttk.Label(frame, text=item["Title"]).pack(anchor="w")
    row = ttk.Frame(frame)
    row.pack(anchor="w", pady=(6, 0))
    var = tk.IntVar(value=item["Default"])
    for choice in item["Choices"]:
      ttk.Radiobutton(row, text=choice["Name"], value=choice["Id"], variable=var, style="Choice.Toolbutton",
                      command=lambda: self.menu_multi_choice(item["Id"], var.get())).pack(side="left", padx=(0, 8))
    self.choices[item["Id"]] = var

  def apply_colors(self, colors):
    for channel in self.channels:
      channel.set_colors(colors)
    for control in self.minmax.values():
      control.scale.set_colors(colors)
    if self.menu_scroll is not None:
      self.menu_scroll.canvas.configure(bg=colors["card"])

  # ----- app -> ZC95 -----

  def menu_min_max(self, menu_id, value):
    self.app.zc_job(lambda msgs: msgs.PatternMinMaxChange(menu_id, value))

  def menu_multi_choice(self, menu_id, choice_id):
    self.app.zc_job(lambda msgs: msgs.PatternMultiChoiceChange(menu_id, choice_id))

  def soft_button(self, pressed):
    if self.detail is not None:
      self.app.zc_job(lambda msgs: msgs.PatternSoftButton(pressed))

  def power_changed(self):
    self.power_dirty = True

  def power_tick(self):
    # Coalesce slider movement into at most one SetPower per tick, and never queue more than one at a time
    if self.power_dirty and self.detail is not None and not self.power_job_pending:
      self.power_dirty = False
      self.power_job_pending = True
      values = [channel.value for channel in self.channels]
      self.app.zc_job(lambda msgs: msgs.SendSetPowerMessage(*values),
                      done=lambda: self.app.on_ui(setattr, self, "power_job_pending", False))
    self.after(POWER_INTERVAL_MS, self.power_tick)

  # ----- ZC95 -> app -----

  def handle_message(self, message):
    msg_type = message.get("Type")
    if msg_type == "PowerStatus" and self.channels:
      for channel in message.get("Channels", []):
        if 1 <= channel["Channel"] <= len(self.channels):
          self.channels[channel["Channel"] - 1].set_status(channel)
    elif msg_type == "MenuOptionChanged":
      menu_id, value = message["MenuId"], message["Value"]
      if menu_id in self.minmax:
        self.minmax[menu_id].set_from_zc95(value)
      elif menu_id in self.choices:
        self.choices[menu_id].set(value)
    elif msg_type == "LuaScriptOutput":
      prefix = "[lua error] " if message.get("TextType") == "Error" else "[lua] "
      print(prefix + str(message.get("Text", "")).rstrip())
    elif msg_type == "LuaScriptError":
      print("[lua error] Script stopped due to an error")

  def on_disconnected(self):
    if self.detail is not None:
      print("Error: connection to ZC95 lost - pattern controls closed")
      self.show_picker()


# ---------- Main window ----------

class App(tk.Tk):
  def __init__(self):
    super().__init__()
    self.title("ZC95 Lua Manager")
    self.geometry("1000x760")
    self.minsize(780, 560)

    self.ui_queue = queue.Queue()   # callables to run on the Tk thread
    self.log_queue = queue.Queue()  # text for the log pane
    self.log_partial = ""           # an unfinished log line, waiting for its newline
    sys.stdout = sys.stderr = LogWriter(self.log_queue)

    self.link = ZcLink()
    self.was_connected = False
    self.busy = False
    self.available = None
    self.local_files = []
    self.remote_scripts = []
    self.selected_slot = None       # ZC95 slot (0-4) picked in the list, for upload and delete

    settings = self.load_settings()
    self.ip_var = tk.StringVar(value=settings.get("ip", ""))
    self.path_var = tk.StringVar(value=settings.get("path", os.path.join(APP_DIR, "lua")))
    self.dark_var = tk.BooleanVar(value=settings.get("dark", False))
    self.view_var = tk.StringVar(value="scripts")  # always opens on the Scripts view
    self.status_var = tk.StringVar(value="Checking...")

    # Both themes use "clam" restyled with the web palette (the native "vista" theme ignores colours)
    self.style = ttk.Style(self)
    self.style.theme_use("clam")
    self.skin = Skin(self, self.style)
    self.panels = []   # RoundPanels, redrawn on a theme change
    self.apply_styles()

    self.build_ui()
    self.apply_theme()
    self.show_view()
    self.protocol("WM_DELETE_WINDOW", self.on_close)

    self.after(100, self.pump_queues)
    self.after(200, self.check_available)
    if os.path.isdir(self.path_var.get()):
      self.read_folder()

  # ---------- UI ----------

  def card(self, parent, padding=16):
    """A rounded card like the web page's <section>. Returns (outer, inner): place outer, fill inner."""
    outer = RoundPanel(parent, self, 14, "card", "border", "page", padding=padding)
    return outer, outer.inner

  def build_ui(self):
    # Header: view buttons, then the heading; status and theme toggle on the right (same as the web page)
    header = ttk.Frame(self, style="Page.TFrame", padding=(16, 12))
    header.pack(fill="x")

    tabs = RoundPanel(header, self, 16, "card", "border", "page", padding=4)
    for text, value in (("Scripts", "scripts"), ("Control", "control")):
      ttk.Radiobutton(tabs.inner, text=text, value=value, variable=self.view_var, style="Tab.Toolbutton",
                      command=self.show_view).pack(side="left", padx=1)
    tabs.pack(side="left")
    ttk.Label(header, text="ZC95 Lua Manager", style="Heading.TLabel").pack(side="left", padx=14)

    self.theme_btn = ttk.Button(header, style="Icon.TButton", command=self.toggle_dark)
    self.theme_btn.pack(side="right")
    self.status_pill = RoundPanel(header, self, 13, "page", "warn", "page", padding=(12, 6), inner_style="Page.TFrame")
    self.status_pill.pack(side="right", padx=12)
    self.led = ttk.Label(self.status_pill.inner, style="StatusDot.TLabel")
    self.led.pack(side="left", padx=(0, 7))
    self.status_label = ttk.Label(self.status_pill.inner, textvariable=self.status_var, style="Status.TLabel")
    self.status_label.pack(side="left")

    self.header_rule = tk.Frame(self, height=1, bd=0)
    self.header_rule.pack(fill="x")

    main = ttk.Frame(self, style="Page.TFrame", padding=16)
    main.pack(fill="both", expand=True)

    # IP card, shared by both views
    ip_outer, ip_card = self.card(main)
    ip_outer.pack(fill="x")
    ttk.Label(ip_card, text="ZC95 IP", style="Section.TLabel").pack(side="left")
    ip_entry = ttk.Entry(ip_card, textvariable=self.ip_var, width=22, font=(FONT, 10))
    ip_entry.pack(side="left", padx=10)
    ip_entry.bind("<Return>", lambda e: self.connect_now())
    ttk.Button(ip_card, text="Connect", command=self.connect_now).pack(side="left")
    ttk.Button(ip_card, text="Save settings", command=self.save_settings_clicked).pack(side="right")

    # Log card, pinned to the bottom so a tall Control view can't push it off screen
    log_outer, log_card = self.card(main, padding=(16, 10))
    log_outer.pack(side="bottom", fill="x")
    log_head = ttk.Frame(log_card)
    log_head.pack(fill="x")
    ttk.Label(log_head, text="Log", style="H3.TLabel").pack(side="left")
    ttk.Button(log_head, text="Clear", style="Link.TButton", command=self.clear_log).pack(side="right")
    log_body = ttk.Frame(log_card)
    log_body.pack(fill="both", expand=True, pady=(6, 0))
    self.log = tk.Text(log_body, height=6, state="disabled", wrap="word", font=(MONO, 9), bd=0,
                       highlightthickness=0, padx=0, pady=0)
    log_scroll = ttk.Scrollbar(log_body, command=self.log.yview, style="Slim.Vertical.TScrollbar")
    self.log.configure(yscrollcommand=log_scroll.set)
    log_scroll.pack(side="right", fill="y")
    self.log.pack(fill="both", expand=True)

    # The two views share the space between the IP card and the log
    self.view_area = ttk.Frame(main, style="Page.TFrame")
    self.view_area.pack(fill="both", expand=True, pady=16)
    self.scripts_view = ttk.Frame(self.view_area, style="Page.TFrame")
    self.build_scripts_view(self.scripts_view)
    self.control_view = ControlView(self.view_area, self)

  def build_scripts_view(self, parent):
    # Folder card
    folder_outer, folder_card = self.card(parent)
    folder_outer.pack(fill="x")
    ttk.Label(folder_card, text="SCRIPTS FOLDER ON THIS PC", style="Section.TLabel").pack(anchor="w", pady=(0, 6))
    row = ttk.Frame(folder_card)
    row.pack(fill="x")
    path_entry = ttk.Entry(row, textvariable=self.path_var, font=(MONO, 10))
    path_entry.pack(side="left", fill="x", expand=True)
    path_entry.bind("<Return>", lambda e: self.read_folder())
    ttk.Button(row, text="Browse...", command=self.browse).pack(side="left", padx=(10, 0))
    ttk.Button(row, text="Read", style="Primary.TButton", command=self.read_folder).pack(side="left", padx=(10, 0))

    # Local scripts (left, wider) and ZC95 slots (right)
    columns = ttk.Frame(parent, style="Page.TFrame")
    columns.pack(fill="both", expand=True, pady=(16, 0))
    columns.columnconfigure(0, weight=3, uniform="col")
    columns.columnconfigure(1, weight=2, uniform="col")
    columns.rowconfigure(0, weight=1)

    left_outer, left = self.card(columns)
    left_outer.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
    ttk.Label(left, text="Local scripts", style="H2.TLabel").pack(anchor="w", pady=(0, 10))
    self.local_list = self.make_listbox(left)
    self.local_list.bind("<<ListboxSelect>>", lambda e: self.update_upload_button())
    self.local_list.bind("<Double-Button-1>", lambda e: self.upload())
    ttk.Label(left, text="Select a script to upload", style="Hint.TLabel").pack(anchor="w", pady=(6, 0))
    upload_row = ttk.Frame(left)
    upload_row.pack(fill="x", pady=(10, 0))
    self.upload_btn = ttk.Button(upload_row, text="Upload", style="Primary.TButton", command=self.upload)
    self.upload_btn.pack(side="left")
    self.progress = RoundBar(upload_row, self.colors())
    self.progress.pack(side="left", fill="x", expand=True, padx=(12, 0))

    right_outer, right = self.card(columns)
    right_outer.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
    ttk.Label(right, text="Scripts on ZC95", style="H2.TLabel").pack(anchor="w", pady=(0, 10))
    self.remote_list = self.make_listbox(right)
    self.remote_list.bind("<<ListboxSelect>>", self.on_remote_select)
    ttk.Label(right, text="Select the index position to upload the script to", style="Hint.TLabel").pack(anchor="w", pady=(6, 0))
    remote_btns = ttk.Frame(right)
    remote_btns.pack(fill="x", pady=(10, 0))
    self.refresh_btn = ttk.Button(remote_btns, text="Refresh", command=self.refresh_remote)
    self.refresh_btn.pack(side="left")
    self.delete_btn = ttk.Button(remote_btns, text="Delete slot", command=self.delete_slot)
    self.delete_btn.pack(side="left", padx=(10, 0))

  def make_listbox(self, parent):
    # The list sits in a rounded "well", like the web page's list boxes
    well = RoundPanel(parent, self, 10, "page", "border", "card", padding=(8, 8), inner_style="Well.TFrame")
    well.pack(fill="both", expand=True)
    frame = well.inner
    listbox = tk.Listbox(frame, exportselection=False, font=(FONT, 10), activestyle="none", bd=0,
                         highlightthickness=0, relief="flat")
    scroll = ttk.Scrollbar(frame, command=listbox.yview, style="WellSlim.Vertical.TScrollbar")
    listbox.configure(yscrollcommand=scroll.set)
    scroll.pack(side="right", fill="y")
    listbox.pack(side="left", fill="both", expand=True)
    return listbox

  def show_view(self):
    if self.view_var.get() == "control":
      self.scripts_view.pack_forget()
      self.control_view.pack(fill="both", expand=True)
      if self.ip_var.get().strip():
        self.control_view.load_patterns()  # picks up any scripts just uploaded
    else:
      self.control_view.pack_forget()
      self.scripts_view.pack(fill="both", expand=True)

  # ---------- Log ----------

  def write_log(self, text):
    # Timestamp each complete line (like the web log) and colour errors red
    text = self.log_partial + text
    lines = text.split("\n")
    self.log_partial = lines.pop()
    if not lines:
      return
    self.log.configure(state="normal")
    for line in lines:
      if not line.strip():
        continue
      tag = "error" if re.search(r"error|fail|lost", line, re.IGNORECASE) else ""
      self.log.insert("end", time.strftime("%H:%M:%S") + "  " + line + "\n", tag)
    self.log.see("end")
    self.log.configure(state="disabled")

  def clear_log(self):
    self.log.configure(state="normal")
    self.log.delete("1.0", "end")
    self.log.configure(state="disabled")

  # ---------- Threading helpers ----------

  def pump_queues(self):
    while not self.ui_queue.empty():
      self.ui_queue.get_nowait()()

    while not self.link.rcv_queue.empty():
      message = self.link.rcv_queue.get_nowait()
      if message.get("Result") == "ERROR" and message.get("Error"):
        print("ZC95 error: " + message["Error"])
      self.control_view.handle_message(message)

    connected = self.link.connected
    if connected != self.was_connected:
      if not connected:
        self.control_view.on_disconnected()
      self.update_status()
    self.was_connected = connected

    text = ""
    while not self.log_queue.empty():
      text += self.log_queue.get_nowait()
    if text:
      self.write_log(text)
    self.after(50, self.pump_queues)

  def on_ui(self, func, *args):
    self.ui_queue.put(lambda: func(*args))

  def zc_job(self, func, *args, done=None):
    """Queue func(zc_messages, *args) to run against the ZC95. Returns False if there's no IP."""
    ip = self.ip_var.get().strip()
    if not ip:
      messagebox.showwarning("No IP", "Enter the ZC95's IP address first.")
      return False
    self.link.submit(ip, func, *args, done=done)
    return True

  def run_job(self, func, *args):
    """Scripts-view operation: one at a time, with the buttons disabled while it runs."""
    if self.busy:
      messagebox.showinfo("Busy", "Please wait for the current operation to finish.")
      return
    if self.zc_job(func, *args, done=lambda: self.on_ui(self.set_busy, False)):
      self.set_busy(True)

  def set_busy(self, busy):
    self.busy = busy
    state = "disabled" if busy else "normal"
    for btn in (self.upload_btn, self.refresh_btn, self.delete_btn):
      btn.configure(state=state)
    self.configure(cursor="watch" if busy else "")

  # ---------- Connection status ----------

  def connect_now(self):
    """Connect to the IP in the box now and load the ZC95's lists."""
    if not self.ip_var.get().strip():
      messagebox.showwarning("No IP", "Enter the ZC95's IP address first.")
      return
    print("Connecting to ZC95 at " + self.ip_var.get().strip() + "...")
    self.check_available(reschedule=False)
    if not self.busy:
      self.refresh_remote()
    if self.view_var.get() == "control":
      self.control_view.load_patterns()

  def check_available(self, reschedule=True):
    ip = self.ip_var.get().strip()

    def worker():
      ok = False
      if ip:
        host, _, port = ip.partition(":")
        try:
          with socket.create_connection((host, int(port or 80)), timeout=2):
            ok = True
        except (OSError, ValueError):
          pass
      self.on_ui(self.set_available, ip, ok)

    threading.Thread(target=worker, daemon=True).start()
    if reschedule:
      self.after(PING_INTERVAL_MS, self.check_available)

  def set_available(self, ip, ok):
    first_online = ok and not self.available
    self.available = ok
    self.update_status()

    # Load the lists the first time the box comes online
    if first_online:
      if not self.busy:
        self.refresh_remote()
      if self.view_var.get() == "control":
        self.control_view.load_patterns()

  def update_status(self):
    c = self.colors()
    ip = self.ip_var.get().strip()
    if not ip:
      kind, text = "danger", "no ZC95 IP set"
    elif self.link.connected and self.link.ip == ip:
      kind, text = "ok", "ZC95 connected (" + ip + ")"
    elif self.available:
      kind, text = "ok", "ZC95 available (" + ip + ")"
    elif self.available is None:
      kind, text = "warn", "checking..."
    else:
      kind, text = "danger", "ZC95 not reachable (" + ip + ")"
    self.status_var.set(text)
    # A pill outlined in the status colour, with a matching dot, like the web page
    self.status_pill.set_outline(kind)
    self.style.configure("Status.TLabel", foreground=c[kind])
    self.led.configure(image=self.skin.image("dot:" + kind, 10, 10, 5, c[kind]))

  # ---------- Local scripts ----------

  def browse(self):
    folder = filedialog.askdirectory(initialdir=self.path_var.get() or APP_DIR)
    if folder:
      self.path_var.set(os.path.normpath(folder))
      self.read_folder()

  def read_folder(self):
    folder = self.path_var.get().strip()
    if not os.path.isdir(folder):
      messagebox.showerror("Folder not found", "Folder not found:\n" + folder)
      return
    self.local_files = sorted((f for f in os.listdir(folder) if f.lower().endswith(".lua")), key=str.lower)
    self.local_list.delete(0, "end")
    for name in self.local_files:
      self.local_list.insert("end", name)
    self.update_upload_button()
    print("Found " + str(len(self.local_files)) + " .lua script(s) in " + folder)

  def selected_script(self):
    sel = self.local_list.curselection()
    return self.local_files[sel[0]] if sel else None

  def update_upload_button(self):
    # The button says what will happen, e.g. "Upload fire.lua to slot 2" (same as the web page)
    name = self.selected_script()
    if name and self.selected_slot is not None:
      text = "Upload " + name + " to slot " + str(self.selected_slot)
    elif name:
      text = "Upload " + name + " to…"
    else:
      text = "Upload"
    self.upload_btn.configure(text=text)

  # ---------- ZC95 script operations ----------

  def refresh_remote(self):
    self.run_job(self.do_refresh)

  def do_refresh(self, msgs):
    scripts = msgs.SendGetLuaScripts()
    self.on_ui(self.show_remote, scripts)

  def show_remote(self, scripts):
    c = self.colors()
    self.remote_list.delete(0, "end")
    self.remote_scripts = scripts
    for i, script in enumerate(scripts):
      self.remote_list.insert("end", str(script["Index"]) + " - " + (script.get("Name") or "(empty)"))
      if not script.get("Name"):
        self.remote_list.itemconfigure(i, foreground=c["muted"])
      if script["Index"] == self.selected_slot:
        self.remote_list.selection_set(i)   # keep the chosen slot highlighted after a reload
    self.update_upload_button()

  def on_remote_select(self, event):
    # Clicking a slot picks it as the upload target (and the slot Delete acts on)
    sel = self.remote_list.curselection()
    if sel:
      self.selected_slot = self.remote_scripts[sel[0]]["Index"]
      self.update_upload_button()

  def upload(self):
    name = self.selected_script()
    if not name:
      messagebox.showwarning("No script", "Select a script to upload.")
      return
    if self.selected_slot is None:
      messagebox.showwarning("No slot", "Select the index position on the right to upload the script to.")
      return
    path = os.path.join(self.path_var.get().strip(), name)
    try:
      with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    except OSError as e:
      messagebox.showerror("Can't read script", str(e))
      return
    self.run_job(self.do_upload, name, lines, self.selected_slot)

  def do_upload(self, msgs, name, lines, index):
    print("Uploading " + name + " to slot " + str(index) + "...")
    self.on_ui(self.progress.set_progress, 0, len(lines))

    if msgs.SendLuaStart(index) is None:
      print("Upload failed: ZC95 rejected start of upload")
      return

    for line_number, line in enumerate(lines):
      if msgs.SendLuaLine(line_number, line) is None:
        print("Upload failed on line " + str(line_number + 1))
        return
      self.on_ui(self.progress.set_progress, line_number + 1)

    # This step fails if the script is invalid Lua
    if msgs.SendLuaEnd() is None:
      print("Upload failed: script rejected by ZC95 (check the Lua for errors)")
      return

    print("Uploaded " + name + " to slot " + str(index))
    self.on_ui(self.show_remote, msgs.SendGetLuaScripts())

  def delete_slot(self):
    if self.selected_slot is None:
      messagebox.showwarning("No slot", "Select a slot on the right to delete.")
      return
    index = self.selected_slot
    if messagebox.askyesno("Delete script", "Delete the script in slot " + str(index) + " on the ZC95?"):
      self.run_job(self.do_delete, index)

  def do_delete(self, msgs, index):
    if msgs.SendDeleteLuaScript(index) is None:
      print("Delete failed")
      return
    print("Deleted slot " + str(index))
    self.on_ui(self.show_remote, msgs.SendGetLuaScripts())

  # ---------- Settings ----------

  def load_settings(self):
    try:
      with open(SETTINGS_FILE, "r") as f:
        return json.load(f)
    except (OSError, ValueError):
      return {}

  def save_settings(self, **values):
    """Merge values into the settings file. Returns an error message, or None on success."""
    settings = self.load_settings()
    settings.update(values)
    try:
      os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
      with open(SETTINGS_FILE, "w") as f:
        json.dump(settings, f, indent=2)
    except OSError as e:
      return str(e)

  def save_settings_clicked(self):
    ip = self.ip_var.get().strip()
    path = self.path_var.get().strip()
    error = self.save_settings(ip=ip, path=path)
    if error:
      messagebox.showerror("Save settings", "Couldn't save settings:\n" + error)
    else:
      print("Settings saved (IP " + (ip or "<blank>") + ", folder " + (path or "<blank>") + ")")

  # ---------- Theme ----------

  def colors(self):
    return THEMES["dark" if self.dark_var.get() else "light"]

  def toggle_dark(self):
    self.dark_var.set(not self.dark_var.get())
    self.apply_theme()
    self.save_settings(dark=self.dark_var.get())  # the theme choice is remembered straight away

  def apply_styles(self):
    """Configure every ttk style from the current palette, and (re)draw the rounded skin."""
    c = self.colors()
    s = self.style
    k = self.skin
    base = (FONT, 10)

    # Default: widgets sit on cards
    s.configure(".", background=c["card"], foreground=c["text"], fieldbackground=c["page"], font=base,
                bordercolor=c["border"], lightcolor=c["card"], darkcolor=c["card"], troughcolor=c["border"],
                selectbackground=c["accent"], selectforeground=c["accent_text"], insertcolor=c["text"],
                arrowcolor=c["muted"], focuscolor=c["card"])
    s.map(".", foreground=[("disabled", c["muted"])])

    # (Rounded panels - cards, list wells, the tab box, the status pill - are RoundPanels, drawn on canvases)
    s.configure("Well.TFrame", background=c["page"])
    s.configure("StatusDot.TLabel", background=c["page"], padding=0)

    # ----- rounded buttons: secondary (default), primary, danger, hold, step, icon -----
    bold = (FONT, 10, "bold")
    k.element("Secondary.border", 9, [("", c["page"], c["border"]), ("disabled", c["page"], c["border"]),
                                      ("pressed", c["border"], c["border"]), ("active", c["hover"], c["border"])])
    k.button("TButton", "Secondary.border")
    s.configure("TButton", background=c["card"], foreground=c["text"], padding=(14, 5), font=bold, anchor="center")
    # clam changes a button's background on hover/press; it shows through the rounded corners, so pin it
    s.map("TButton", foreground=[("disabled", c["muted"])], background=[("disabled", c["card"]), ("pressed", c["card"]), ("selected", c["card"]), ("active", c["card"])])

    k.element("Primary.border", 9, [("", c["accent"], None), ("disabled", c["border"], None),
                                    ("pressed", c["accent_active"], None), ("active", c["accent_active"], None)])
    k.button("Primary.TButton", "Primary.border")
    s.configure("Primary.TButton", foreground=c["accent_text"])
    s.map("Primary.TButton", foreground=[("disabled", c["muted"])], background=[("disabled", c["card"]), ("pressed", c["card"]), ("selected", c["card"]), ("active", c["card"])])

    k.element("Danger.border", 9, [("", c["danger"], None), ("pressed", c["danger_active"], None),
                                   ("active", c["danger_active"], None)])
    k.button("Danger.TButton", "Danger.border")
    s.configure("Danger.TButton", foreground="#ffffff")

    k.element("Hold.border", 12, [("", c["page"], c["border"]), ("pressed", c["accent"], None),
                                  ("active", c["hover"], c["border"])])
    k.button("Hold.TButton", "Hold.border")
    s.map("Hold.TButton", foreground=[("pressed", c["accent_text"])], background=[("disabled", c["card"]), ("pressed", c["card"]), ("selected", c["card"]), ("active", c["card"])])

    k.button("Step.TButton", "Secondary.border")
    s.configure("Step.TButton", padding=(0, 3), width=3, font=(FONT, 12))

    k.element("Icon.border", 15, [("", c["card"], c["border"]), ("active", c["hover"], c["border"])])
    k.button("Icon.TButton", "Icon.border")
    s.configure("Icon.TButton", background=c["page"], padding=(9, 4), width=2, font=(FONT, 11))
    s.map("Icon.TButton", background=[("disabled", c["page"]), ("pressed", c["page"]), ("selected", c["page"]), ("active", c["page"])])

    s.layout("Link.TButton", [("Button.padding", {"sticky": "nsew", "children": [("Button.label", {"sticky": "nsew"})]})])
    s.configure("Link.TButton", background=c["card"], foreground=c["muted"], borderwidth=0, padding=(2, 0),
                lightcolor=c["card"], darkcolor=c["card"], bordercolor=c["card"], font=(FONT, 9))
    s.map("Link.TButton", background=[("active", c["card"])], foreground=[("active", c["text"])],
          lightcolor=[("active", c["card"])], darkcolor=[("active", c["card"])])

    # ----- pill toggles: the Scripts / Control tabs and pattern choices, red when selected -----
    k.element("Tab.border", 13, [("", c["card"], None), ("selected", c["accent"], None), ("active", c["hover"], None)])
    k.button("Tab.Toolbutton", "Tab.border", label="Toolbutton")
    s.configure("Tab.Toolbutton", background=c["card"], foreground=c["muted"], padding=(18, 5), font=bold)
    s.map("Tab.Toolbutton", foreground=[("selected", c["accent_text"]), ("active", c["text"])], background=[("disabled", c["card"]), ("pressed", c["card"]), ("selected", c["card"]), ("active", c["card"])])

    k.element("Choice.border", 14, [("", c["page"], c["border"]), ("selected", c["accent"], None),
                                    ("active", c["hover"], c["border"])])
    k.button("Choice.Toolbutton", "Choice.border", label="Toolbutton")
    s.configure("Choice.Toolbutton", background=c["card"], foreground=c["text"], padding=(16, 5))
    s.map("Choice.Toolbutton", foreground=[("selected", c["accent_text"])], background=[("disabled", c["card"]), ("pressed", c["card"]), ("selected", c["card"]), ("active", c["card"])])

    # ----- rounded text fields (Entry, Combobox) with a red outline when focused -----
    k.element("Field.field", 9, [("", c["page"], c["border"]), ("focus", c["page"], c["accent"])], padding=(10, 6))
    s.layout("TEntry", [("Field.field", {"sticky": "nsew", "children": [
      ("Entry.padding", {"sticky": "nsew", "children": [("Entry.textarea", {"sticky": "nsew"})]})]})])
    s.configure("TEntry", padding=(0, 1), foreground=c["text"])
    s.layout("TCombobox", [("Field.field", {"sticky": "nsew", "children": [
      ("Combobox.downarrow", {"side": "right", "sticky": "ns"}),
      ("Combobox.padding", {"sticky": "nsew", "children": [("Combobox.textarea", {"sticky": "nsew"})]})]})])
    s.configure("TCombobox", padding=(0, 1), arrowcolor=c["text"], background=c["page"], bordercolor=c["page"],
                lightcolor=c["page"], darkcolor=c["page"], arrowsize=13)
    s.map("TCombobox", fieldbackground=[("readonly", c["page"])], foreground=[("readonly", c["text"])],
          selectbackground=[("readonly", c["page"])], selectforeground=[("readonly", c["text"])],
          background=[("active", c["page"]), ("readonly", c["page"])])

    # ----- slim rounded scrollbars (on cards, and inside the list wells) -----
    k.element("Slim.thumb", 4, [("", c["thumb"], None), ("active", c["thumb_active"], None)],
              size=(8, 10 + self.skin.CENTRE), border=(0, 5), padding=0, min_size=(8, 11))
    for name, trough in (("Slim", c["card"]), ("WellSlim", c["page"])):
      s.layout(name + ".Vertical.TScrollbar", [("Vertical.Scrollbar.trough", {"sticky": "ns", "children": [
        ("Slim.thumb", {"expand": "1", "sticky": "nswe"})]})])
      s.configure(name + ".Vertical.TScrollbar", troughcolor=trough, background=trough, bordercolor=trough,
                  lightcolor=trough, darkcolor=trough, width=8)

    s.configure("Page.TFrame", background=c["page"])
    s.configure("Heading.TLabel", background=c["page"], font=(FONT, 13, "bold"))
    s.configure("Status.TLabel", background=c["page"], font=(FONT, 9))
    s.configure("H2.TLabel", font=(FONT, 11, "bold"))
    s.configure("H3.TLabel", font=(FONT, 10, "bold"), foreground=c["muted"])
    s.configure("Bold.TLabel", font=(FONT, 10, "bold"))
    s.configure("Muted.TLabel", foreground=c["muted"])
    s.configure("Section.TLabel", foreground=c["muted"], font=(FONT, 8))
    s.configure("Hint.TLabel", foreground=c["muted"], font=(FONT, 8))
    s.configure("Warn.TLabel", foreground=c["warn"], font=(FONT, 8, "bold"))

  def apply_theme(self):
    dark = self.dark_var.get()
    c = self.colors()
    self.apply_styles()

    self.configure(bg=c["page"])
    self.header_rule.configure(bg=c["border"])
    self.theme_btn.configure(text="☾" if dark else "☀")   # moon / sun, as on the web page
    self.progress.set_colors(c)
    self.panels = [p for p in self.panels if p.winfo_exists()]
    for panel in self.panels:
      panel.draw()
    for listbox in (self.local_list, self.remote_list):
      listbox.configure(bg=c["page"], fg=c["text"], selectbackground=c["accent"], selectforeground=c["accent_text"])
    for i, script in enumerate(self.remote_scripts):
      if not script.get("Name"):
        self.remote_list.itemconfigure(i, foreground=c["muted"])
    self.log.configure(bg=c["card"], fg=c["muted"], insertbackground=c["text"])
    self.log.tag_configure("error", foreground=c["danger"] if not dark else "#ff6b5e")

    # The drop-down's popup list is a plain tk Listbox
    popdown = self.tk.call("ttk::combobox::PopdownWindow", self.control_view.pattern_combo)
    self.tk.call(popdown + ".f.l", "configure", "-background", c["card"], "-foreground", c["text"],
                 "-selectbackground", c["accent"], "-selectforeground", c["accent_text"])

    self.control_view.apply_colors(c)
    self.update_status()
    self.set_title_bar_dark(dark)

  def set_title_bar_dark(self, dark):
    """Ask Windows 10/11 to draw the window's title bar dark (ignored on older Windows)."""
    try:
      self.update_idletasks()
      hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
      value = ctypes.c_int(1 if dark else 0)
      ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(value), ctypes.sizeof(value))  # 20 = DWMWA_USE_IMMERSIVE_DARK_MODE
      # Windows only repaints the title bar on a resize, so nudge the window
      w, h = self.winfo_width(), self.winfo_height()
      if w > 1:
        self.geometry(f"{w + 1}x{h}")
        self.after(10, lambda: self.geometry(f"{w}x{h}"))
    except (AttributeError, OSError):
      pass

  def on_close(self):
    # Stop any running pattern and close the connection, but don't hang if the ZC95 isn't answering
    self.control_view.stop_on_exit()
    closed = threading.Event()
    self.link.disconnect(done=closed.set)
    closed.wait(4)
    self.destroy()


if __name__ == "__main__":
  App().mainloop()
