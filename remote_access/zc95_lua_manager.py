"""ZC95 Lua Manager - Windows GUI for a ZC95 connected over WiFi.

Two views, picked with the buttons along the top:
  Scripts - list / upload / delete Lua scripts (what lua_manage.py and lua_upload.py do)
  Control - start a pattern and drive it: menu options, soft button, channel power
            (what webgui.py + web_static/ do in a browser)

The layout and colours match the ZC95 Web Manager (zc95_web).
Reuses lib/ZcWs.py and lib/ZcMessages.py over one websocket connection to the ZC95.

Run:  py zc95_lua_manager.py        (or pyw zc95_lua_manager.py for no console)
"""

import ctypes
import json
import os
import queue
import re
import socket
import sys
import threading
import time
import tkinter as tk
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
            "max": "#3b63c2", "actual": "#d6a600", "limit": "#2e7d43"},
  "dark":  {"page": "#14151a", "card": "#1f2129", "text": "#f0f0f2", "muted": "#9a9ba6", "border": "#33333f",
            "hover": "#2a2c36", "accent": "#d32f2f", "accent_active": "#e04848", "accent_text": "#ffffff",
            "danger": "#b3261e", "danger_active": "#cf3a30", "ok": "#3ba55c", "warn": "#e0a020",
            "max": "#4d7cf0", "actual": "#ffd23f", "limit": "#3ba55c"},
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


# ---------- Control view widgets ----------

def make_slider(parent, from_, to, command, colors, resolution=1):
  """A flat slider: grey track with a red handle, like the web page's range inputs.
  (A classic tk.Scale, because ttk's clam Scale won't take these colours cleanly.)"""
  scale = tk.Scale(parent, from_=from_, to=to, orient="horizontal", command=command, resolution=resolution,
                   showvalue=0, bd=0, highlightthickness=0, sliderrelief="flat", width=12, sliderlength=24)
  color_slider(scale, colors)
  return scale


def color_slider(scale, colors):
  scale.configure(bg=colors["accent"], troughcolor=colors["border"], activebackground=colors["accent_active"])


class ScrollFrame(ttk.Frame):
  """A frame with a vertical scrollbar; put children in .inner"""

  def __init__(self, parent):
    super().__init__(parent)
    self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0)
    scroll = ttk.Scrollbar(self, command=self.canvas.yview)
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
    self.scale = make_slider(self.frame, self.min, self.max, self._on_scale, colors)
    self.scale.grid(row=1, column=1, sticky="ew", padx=10, pady=(6, 0))
    ttk.Button(self.frame, text="+", style="Step.TButton", command=lambda: self.step_by(self.step)).grid(row=1, column=2, pady=(6, 0))
    self.scale.bind("<ButtonPress-1>", lambda e: setattr(self, "dragging", True))
    self.scale.bind("<ButtonRelease-1>", self._on_release)
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

  BAR_HEIGHT = 12

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
    self.scale = make_slider(self.frame, 0, 1000, self._on_scale, colors, resolution=CHANNEL_STEP)
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
    self.bar.configure(bg=colors["border"])
    color_slider(self.scale, colors)
    self.draw()

  def draw(self):
    c, w, h = self.colors, self.bar.winfo_width(), self.BAR_HEIGHT
    self.bar.delete("all")
    if w <= 1:
      return
    self.bar.create_rectangle(0, 0, w * self.max_out / 1000, h, fill=c["max"], width=0)
    self.bar.create_rectangle(0, 3, w * self.actual / 1000, h - 3, fill=c["actual"], width=0)
    if self.limit < 1000:
      x = w * self.limit / 1000
      for hx in range(int(x) - h, w, 6):   # diagonal hatching beyond the limit
        self.bar.create_line(max(hx, x), h - max(0, x - hx), hx + h, 0, fill=c["limit"], width=2)
      self.bar.create_rectangle(x, 0, x + 2, h, fill=c["limit"], width=0)


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
    self.pattern_combo.pack(side="left", fill="x", expand=True, ipady=3)
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
      color_slider(control.scale, colors)
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
    self.cards = []

    settings = self.load_settings()
    self.ip_var = tk.StringVar(value=settings.get("ip", ""))
    self.path_var = tk.StringVar(value=settings.get("path", os.path.join(APP_DIR, "lua")))
    self.dark_var = tk.BooleanVar(value=settings.get("dark", False))
    self.view_var = tk.StringVar(value="scripts")  # always opens on the Scripts view
    self.status_var = tk.StringVar(value="Checking...")

    # Both themes use "clam" restyled with the web palette (the native "vista" theme ignores colours)
    self.style = ttk.Style(self)
    self.style.theme_use("clam")
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

  def card(self, parent):
    """A bordered card like the web page's <section>. Returns (outer, inner): place outer, fill inner."""
    c = self.colors()
    outer = tk.Frame(parent, bd=0, highlightthickness=1, bg=c["card"],
                     highlightbackground=c["border"], highlightcolor=c["border"])
    inner = ttk.Frame(outer, padding=16)
    inner.pack(fill="both", expand=True)
    self.cards.append(outer)
    return outer, inner

  def build_ui(self):
    # Header: view buttons, then the heading; status and theme toggle on the right (same as the web page)
    header = ttk.Frame(self, style="Page.TFrame", padding=(16, 12))
    header.pack(fill="x")

    tabs_outer, tabs = self.card(header)
    tabs.configure(padding=3)
    for text, value in (("Scripts", "scripts"), ("Control", "control")):
      ttk.Radiobutton(tabs, text=text, value=value, variable=self.view_var, style="Tab.Toolbutton",
                      command=self.show_view).pack(side="left", padx=1)
    tabs_outer.pack(side="left")
    ttk.Label(header, text="ZC95 Lua Manager", style="Heading.TLabel").pack(side="left", padx=14)

    self.theme_btn = ttk.Button(header, style="Icon.TButton", command=self.toggle_dark)
    self.theme_btn.pack(side="right")
    status = ttk.Frame(header, style="Page.TFrame")
    status.pack(side="right", padx=12)
    self.led = tk.Canvas(status, width=12, height=12, highlightthickness=0)
    self.led_dot = self.led.create_oval(1, 1, 11, 11, fill="grey", outline="")
    self.led.pack(side="left", padx=(0, 6))
    self.status_label = ttk.Label(status, textvariable=self.status_var, style="Status.TLabel")
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
    ip_entry.pack(side="left", padx=10, ipady=3)
    ip_entry.bind("<Return>", lambda e: self.connect_now())
    ttk.Button(ip_card, text="Connect", command=self.connect_now).pack(side="left")
    ttk.Button(ip_card, text="Save settings", command=self.save_settings_clicked).pack(side="right")

    # Log card, pinned to the bottom so a tall Control view can't push it off screen
    log_outer, log_card = self.card(main)
    log_outer.pack(side="bottom", fill="x")
    log_card.configure(padding=(16, 10))
    log_head = ttk.Frame(log_card)
    log_head.pack(fill="x")
    ttk.Label(log_head, text="Log", style="H3.TLabel").pack(side="left")
    ttk.Button(log_head, text="Clear", style="Link.TButton", command=self.clear_log).pack(side="right")
    log_body = ttk.Frame(log_card)
    log_body.pack(fill="both", expand=True, pady=(6, 0))
    self.log = tk.Text(log_body, height=6, state="disabled", wrap="word", font=(MONO, 9), bd=0,
                       highlightthickness=0, padx=0, pady=0)
    log_scroll = ttk.Scrollbar(log_body, command=self.log.yview)
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
    path_entry.pack(side="left", fill="x", expand=True, ipady=3)
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
    self.progress = ttk.Progressbar(upload_row, mode="determinate")
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
    frame = ttk.Frame(parent)
    frame.pack(fill="both", expand=True)
    listbox = tk.Listbox(frame, exportselection=False, font=(FONT, 10), activestyle="none", bd=0,
                         highlightthickness=1, relief="flat")
    scroll = ttk.Scrollbar(frame, command=listbox.yview)
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
      color, text = c["danger"], "no ZC95 IP set"
    elif self.link.connected and self.link.ip == ip:
      color, text = c["ok"], "ZC95 connected (" + ip + ")"
    elif self.available:
      color, text = c["ok"], "ZC95 available (" + ip + ")"
    elif self.available is None:
      color, text = c["warn"], "checking..."
    else:
      color, text = c["danger"], "ZC95 not reachable (" + ip + ")"
    self.status_var.set(text)
    self.style.configure("Status.TLabel", foreground=color)
    self.led.itemconfigure(self.led_dot, fill=color)

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
    self.on_ui(self.progress.configure, {"maximum": max(len(lines), 1), "value": 0})

    if msgs.SendLuaStart(index) is None:
      print("Upload failed: ZC95 rejected start of upload")
      return

    for line_number, line in enumerate(lines):
      if msgs.SendLuaLine(line_number, line) is None:
        print("Upload failed on line " + str(line_number + 1))
        return
      self.on_ui(self.progress.configure, {"value": line_number + 1})

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
    """Configure every ttk style from the current palette."""
    c = self.colors()
    s = self.style
    base = (FONT, 10)

    # Default: widgets sit on cards
    s.configure(".", background=c["card"], foreground=c["text"], fieldbackground=c["page"], font=base,
                bordercolor=c["border"], lightcolor=c["card"], darkcolor=c["card"], troughcolor=c["border"],
                selectbackground=c["accent"], selectforeground=c["accent_text"], insertcolor=c["text"],
                arrowcolor=c["muted"], focuscolor=c["card"])
    s.map(".", foreground=[("disabled", c["muted"])])

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

    # Buttons: secondary (default), primary (red), danger, step (-/+), link, icon
    s.configure("TButton", background=c["page"], foreground=c["text"], bordercolor=c["border"],
                lightcolor=c["page"], darkcolor=c["page"], padding=(14, 6), font=(FONT, 10, "bold"))
    s.map("TButton", background=[("disabled", c["page"]), ("pressed", c["border"]), ("active", c["hover"])],
          lightcolor=[("active", c["hover"])], darkcolor=[("active", c["hover"])])
    s.configure("Primary.TButton", background=c["accent"], foreground=c["accent_text"], bordercolor=c["accent"],
                lightcolor=c["accent"], darkcolor=c["accent"])
    s.map("Primary.TButton",
          background=[("disabled", c["border"]), ("pressed", c["accent_active"]), ("active", c["accent_active"])],
          lightcolor=[("active", c["accent_active"])], darkcolor=[("active", c["accent_active"])],
          bordercolor=[("disabled", c["border"]), ("active", c["accent_active"])],
          foreground=[("disabled", c["muted"])])
    s.configure("Danger.TButton", background=c["danger"], foreground="#ffffff", bordercolor=c["danger"],
                lightcolor=c["danger"], darkcolor=c["danger"])
    s.map("Danger.TButton", background=[("active", c["danger_active"])], lightcolor=[("active", c["danger_active"])],
          darkcolor=[("active", c["danger_active"])], bordercolor=[("active", c["danger_active"])])
    s.configure("Hold.TButton", padding=(14, 6))
    s.map("Hold.TButton", background=[("pressed", c["accent"]), ("active", c["hover"])],
          foreground=[("pressed", c["accent_text"])])
    s.configure("Step.TButton", padding=(0, 4), width=3, font=(FONT, 12))
    s.configure("Link.TButton", background=c["card"], foreground=c["muted"], borderwidth=0, padding=(2, 0),
                lightcolor=c["card"], darkcolor=c["card"], bordercolor=c["card"], font=(FONT, 9))
    s.map("Link.TButton", background=[("active", c["card"])], foreground=[("active", c["text"])],
          lightcolor=[("active", c["card"])], darkcolor=[("active", c["card"])])
    s.configure("Icon.TButton", background=c["card"], padding=(8, 4), width=2, font=(FONT, 11))

    # Tabs (Scripts / Control) and the pattern choice buttons: red when selected
    s.configure("Tab.Toolbutton", background=c["card"], foreground=c["muted"], padding=(18, 6),
                font=(FONT, 10, "bold"), borderwidth=0)
    s.map("Tab.Toolbutton", background=[("selected", c["accent"]), ("active", c["hover"])],
          foreground=[("selected", c["accent_text"]), ("active", c["text"])])
    s.configure("Choice.Toolbutton", background=c["page"], foreground=c["text"], padding=(14, 5),
                bordercolor=c["border"], relief="solid", borderwidth=1)
    s.map("Choice.Toolbutton", background=[("selected", c["accent"]), ("active", c["hover"])],
          foreground=[("selected", c["accent_text"])], bordercolor=[("selected", c["accent"])])

    s.configure("TEntry", fieldbackground=c["page"], bordercolor=c["border"], lightcolor=c["page"],
                darkcolor=c["page"], padding=6)
    s.map("TEntry", bordercolor=[("focus", c["accent"])], lightcolor=[("focus", c["page"])])
    s.configure("TCombobox", fieldbackground=c["page"], background=c["page"], bordercolor=c["border"],
                lightcolor=c["page"], darkcolor=c["page"], padding=6, arrowcolor=c["text"])
    s.map("TCombobox", fieldbackground=[("readonly", c["page"])], foreground=[("readonly", c["text"])],
          selectbackground=[("readonly", c["page"])], selectforeground=[("readonly", c["text"])],
          background=[("active", c["hover"]), ("readonly", c["page"])])
    s.configure("TScrollbar", background=c["card"], troughcolor=c["card"], bordercolor=c["card"],
                lightcolor=c["card"], darkcolor=c["card"], arrowcolor=c["muted"])
    s.map("TScrollbar", background=[("active", c["hover"])])
    s.configure("Horizontal.TProgressbar", background=c["accent"], troughcolor=c["border"], bordercolor=c["border"],
                lightcolor=c["accent"], darkcolor=c["accent"], thickness=8)

  def apply_theme(self):
    dark = self.dark_var.get()
    c = self.colors()
    self.apply_styles()

    self.configure(bg=c["page"])
    self.header_rule.configure(bg=c["border"])
    self.led.configure(bg=c["page"])
    self.theme_btn.configure(text="☾" if dark else "☀")   # moon / sun, as on the web page
    for outer in self.cards:
      if outer.winfo_exists():
        outer.configure(bg=c["card"], highlightbackground=c["border"], highlightcolor=c["border"])
    for listbox in (self.local_list, self.remote_list):
      listbox.configure(bg=c["page"], fg=c["text"], selectbackground=c["accent"], selectforeground=c["accent_text"],
                        highlightbackground=c["border"], highlightcolor=c["border"])
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
