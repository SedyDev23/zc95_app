"""ZC95 Lua Manager - Windows GUI for a ZC95 connected over WiFi.

Two views, picked with the buttons along the top:
  Scripts - list / upload / delete Lua scripts (what lua_manage.py and lua_upload.py do)
  Control - start a pattern and drive it: menu options, soft button, channel power
            (what webgui.py + web_static/ do in a browser)

Reuses lib/ZcWs.py and lib/ZcMessages.py over one websocket connection to the ZC95.

Run:  py zc95_lua_manager.py        (or pyw zc95_lua_manager.py for no console)
"""

import ctypes
import json
import os
import queue
import socket
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# Make "lib.*" importable no matter where the app is launched from
APP_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP_DIR)

import lib.ZcMessages as zc  # noqa: E402
from lib.ZcWs import ZcWs  # noqa: E402

SLOTS = [str(i) for i in range(5)]  # ZC95 has Lua slots 0-4
CONNECT_TIMEOUT = 6                  # seconds to wait for the websocket to open
PING_INTERVAL_MS = 5000              # how often the availability indicator is refreshed
POWER_INTERVAL_MS = 250              # SetPower is sent at most this often, same as pattern_gui.py / WebHub.py
CHANNEL_STEP = 10                    # power is 0-1000, so 10 = 1%
SETTINGS_FILE = os.path.join(os.environ.get("APPDATA", APP_DIR), "zc95_lua_manager", "settings.json")

# Colours for the plain tk widgets (listboxes, log, canvases); ttk widgets are styled in apply_theme()
THEMES = {
  "light": {"bg": "SystemButtonFace", "field": "white", "fg": "black", "select": "#0078d7", "select_fg": "white",
            "accent": "#1f6fd1", "actual": "#e6b800", "limit": "#2e9e44", "trough": "#e4e4e4", "warn": "#c77700",
            "danger": "#c0392b"},
  "dark":  {"bg": "#1e1e1e", "field": "#2d2d30", "fg": "#e0e0e0", "select": "#094771", "select_fg": "#ffffff",
            "button": "#333337", "button_active": "#3e3e42", "border": "#3c3c3c", "disabled": "#7a7a7a",
            "accent": "#2e8bff", "actual": "#ffd23f", "limit": "#3fbf5f", "trough": "#2d2d30", "warn": "#ffb347",
            "danger": "#a8322a", "danger_active": "#c0392b"},
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
  """A MIN_MAX menu item: title, value, and a -/slider/+ row."""

  def __init__(self, parent, item, on_change):
    self.item = item
    self.on_change = on_change
    self.min, self.max = item["Min"], item["Max"]
    self.step = item.get("IncrementStep") or 1
    self.value = item["Default"]
    self.dragging = False
    self.updating = False

    self.frame = ttk.Frame(parent, padding=(0, 6))
    self.frame.columnconfigure(1, weight=1)
    title = item["Title"] + (" (" + item["UoM"] + ")" if item.get("UoM") else "")
    ttk.Label(self.frame, text=title).grid(row=0, column=0, columnspan=2, sticky="w")
    self.value_label = ttk.Label(self.frame, text=str(self.value), font=("Segoe UI", 10, "bold"))
    self.value_label.grid(row=0, column=2, sticky="e")

    ttk.Button(self.frame, text="−", width=3, command=lambda: self.step_by(-self.step)).grid(row=1, column=0)
    self.scale = ttk.Scale(self.frame, from_=self.min, to=self.max, command=self._on_scale)
    self.scale.grid(row=1, column=1, sticky="ew", padx=6)
    ttk.Button(self.frame, text="+", width=3, command=lambda: self.step_by(self.step)).grid(row=1, column=2)
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
    if not self.updating:
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
  """One channel: label, %, output bar (blue = max power, yellow = actual, green = limit) and power slider."""

  BAR_HEIGHT = 16

  def __init__(self, parent, number, on_change):
    self.on_change = on_change
    self.value = 0
    self.max_out = self.actual = 0
    self.limit = 1000
    self.colors = THEMES["light"]
    self.updating = False

    self.frame = ttk.Frame(parent, padding=(0, 6))
    self.frame.columnconfigure(1, weight=1)
    ttk.Label(self.frame, text="Channel " + str(number), font=("Segoe UI", 10, "bold")).grid(row=0, column=0, columnspan=2, sticky="w")
    self.pct_label = ttk.Label(self.frame, text="0%", font=("Segoe UI", 10, "bold"))
    self.pct_label.grid(row=0, column=2, sticky="e")

    self.bar = tk.Canvas(self.frame, height=self.BAR_HEIGHT, highlightthickness=0, borderwidth=0)
    self.bar.grid(row=1, column=0, columnspan=3, sticky="ew", pady=4)
    self.bar.bind("<Configure>", lambda e: self.draw())

    ttk.Button(self.frame, text="−", width=3, command=lambda: self.step_by(-CHANNEL_STEP)).grid(row=2, column=0)
    self.scale = ttk.Scale(self.frame, from_=0, to=1000, command=self._on_scale)
    self.scale.grid(row=2, column=1, sticky="ew", padx=6)
    ttk.Button(self.frame, text="+", width=3, command=lambda: self.step_by(CHANNEL_STEP)).grid(row=2, column=2)

  def _on_scale(self, raw):
    if not self.updating:
      self.set(int(round(float(raw) / CHANNEL_STEP) * CHANNEL_STEP), notify=True)

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
    self.bar.configure(bg=colors["trough"])
    self.draw()

  def draw(self):
    c, w, h = self.colors, self.bar.winfo_width(), self.BAR_HEIGHT
    self.bar.delete("all")
    if w <= 1:
      return
    self.bar.create_rectangle(0, 0, w * self.max_out / 1000, h, fill=c["accent"], width=0)
    self.bar.create_rectangle(0, h * 0.3, w * self.actual / 1000, h * 0.7, fill=c["actual"], width=0)
    if self.limit < 1000:
      x = w * self.limit / 1000
      self.bar.create_rectangle(x, 0, w, h, fill=c["limit"], stipple="gray25", width=0)
      self.bar.create_rectangle(x - 1, 0, x + 2, h, fill=c["limit"], width=0)


class ControlView(ttk.Frame):
  """Native version of the web UI (webgui.py + web_static/): pick and start a pattern, then drive it."""

  def __init__(self, parent, app):
    super().__init__(parent)
    self.app = app
    self.patterns = []
    self.detail = None        # PatternDetail of the running pattern, None when stopped
    self.minmax = {}          # MenuId -> MinMaxControl
    self.choices = {}         # MenuId -> IntVar
    self.channels = []
    self.menu_scroll = None
    self.power_dirty = False
    self.power_job_pending = False

    # Pattern picker
    self.picker = ttk.Frame(self)
    self.picker.pack(fill="x", padx=6, pady=6)
    ttk.Label(self.picker, text="Pattern:").pack(side="left")
    self.pattern_combo = ttk.Combobox(self.picker, state="readonly", width=40)
    self.pattern_combo.pack(side="left", padx=6)
    ttk.Button(self.picker, text="Start", command=self.start).pack(side="left")
    ttk.Button(self.picker, text="Reload list", command=self.load_patterns).pack(side="left", padx=6)
    self.picker_hint = ttk.Label(self.picker, text="")
    self.picker_hint.pack(side="left", padx=6)

    # Running pattern (built in show_running)
    self.running = ttk.Frame(self)

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
    self.picker_hint.configure(text=str(len(names)) + " patterns")

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
    for child in self.running.winfo_children():
      child.destroy()
    self.picker.pack(fill="x", padx=6, pady=6)

  def show_running(self, detail):
    self.show_picker()
    self.picker.pack_forget()
    self.detail = detail
    self.minmax, self.choices, self.channels = {}, {}, []
    self.power_dirty = False
    colors = self.app.colors()

    header = ttk.Frame(self.running)
    header.pack(fill="x", pady=(0, 6))
    ttk.Label(header, text=detail["Name"], style="Title.TLabel").pack(side="left")
    # PatternDetail's Name lacks the "(!)" triphase prefix that PatternList has, so check the list (same as app.js)
    entry = next((p for p in self.patterns if p["Id"] == detail.get("Id")), None)
    if entry and entry["Name"].startswith("(!)"):
      ttk.Label(header, text="(!) triphase - channel isolation is disabled", style="Warn.TLabel").pack(side="left", padx=10)
    ttk.Button(header, text="Stop", style="Danger.TButton", command=self.stop).pack(side="right")

    if detail.get("ButtonA"):
      soft = ttk.Button(self.running, text=detail["ButtonA"])
      soft.pack(anchor="w", pady=(0, 6))
      soft.bind("<ButtonPress-1>", lambda e: self.soft_button(True))
      soft.bind("<ButtonRelease-1>", lambda e: self.soft_button(False))

    body = ttk.Frame(self.running)
    body.pack(fill="both", expand=True)
    body.columnconfigure(0, weight=1, uniform="col")
    body.columnconfigure(1, weight=1, uniform="col")
    body.rowconfigure(0, weight=1)

    menu_box = ttk.LabelFrame(body, text="Pattern settings")
    menu_box.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
    scroll = self.menu_scroll = ScrollFrame(menu_box)
    scroll.pack(fill="both", expand=True, padx=6, pady=4)
    scroll.canvas.configure(bg=colors["bg"])
    for item in detail.get("MenuItems", []):
      if item["Type"] == "MIN_MAX":
        control = MinMaxControl(scroll.inner, item, self.menu_min_max)
        control.frame.pack(fill="x", padx=(0, 8))
        self.minmax[item["Id"]] = control
      elif item["Type"] == "MULTI_CHOICE":
        self.build_multi_choice(scroll.inner, item)
    if not detail.get("MenuItems"):
      ttk.Label(scroll.inner, text="This pattern has no settings.").pack(anchor="w", pady=6)

    chan_box = ttk.LabelFrame(body, text="Channel power")
    chan_box.grid(row=0, column=1, sticky="nsew", padx=(4, 0))
    for number in range(1, 5):
      channel = ChannelControl(chan_box, number, self.power_changed)
      channel.frame.pack(fill="x", padx=8)
      channel.set_colors(colors)
      self.channels.append(channel)

    self.running.pack(fill="both", expand=True, padx=6, pady=6)

  def build_multi_choice(self, parent, item):
    frame = ttk.Frame(parent, padding=(0, 6))
    frame.pack(fill="x")
    ttk.Label(frame, text=item["Title"]).pack(anchor="w")
    row = ttk.Frame(frame)
    row.pack(anchor="w", pady=(2, 0))
    var = tk.IntVar(value=item["Default"])
    for choice in item["Choices"]:
      ttk.Radiobutton(row, text=choice["Name"], value=choice["Id"], variable=var, style="Toolbutton",
                      command=lambda: self.menu_multi_choice(item["Id"], var.get())).pack(side="left", padx=(0, 4))
    self.choices[item["Id"]] = var

  def apply_colors(self, colors):
    for channel in self.channels:
      channel.set_colors(colors)
    if self.menu_scroll is not None:
      self.menu_scroll.canvas.configure(bg=colors["bg"])

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
      print("[lua] Script stopped due to an error")

  def on_disconnected(self):
    if self.detail is not None:
      print("Connection to ZC95 lost - pattern controls closed")
      self.show_picker()


# ---------- Main window ----------

class App(tk.Tk):
  def __init__(self):
    super().__init__()
    self.title("ZC95 Lua Manager")
    self.geometry("980x700")
    self.minsize(760, 520)

    self.ui_queue = queue.Queue()   # callables to run on the Tk thread
    self.log_queue = queue.Queue()  # text for the log pane
    sys.stdout = sys.stderr = LogWriter(self.log_queue)

    self.link = ZcLink()
    self.was_connected = False
    self.busy = False
    self.available = None
    self.local_files = []
    self.remote_scripts = []

    settings = self.load_settings()
    self.ip_var = tk.StringVar(value=settings.get("ip", ""))
    self.path_var = tk.StringVar(value=settings.get("path", os.path.join(APP_DIR, "lua")))
    self.dark_var = tk.BooleanVar(value=settings.get("dark", False))
    self.view_var = tk.StringVar(value="scripts")  # always opens on the Scripts view
    self.slot_var = tk.StringVar(value="0")
    self.status_var = tk.StringVar(value="Checking...")

    self.style = ttk.Style(self)
    self.light_theme = self.style.theme_use()  # "vista" on Windows

    self.build_ui()
    self.apply_theme()
    self.show_view()
    self.protocol("WM_DELETE_WINDOW", self.on_close)

    self.after(100, self.pump_queues)
    self.after(200, self.check_available)
    if os.path.isdir(self.path_var.get()):
      self.read_folder()

  # ---------- UI ----------

  def build_ui(self):
    pad = {"padx": 6, "pady": 4}

    # Along the top: view buttons, availability indicator, dark mode
    top = ttk.Frame(self)
    top.pack(fill="x", **pad)
    ttk.Radiobutton(top, text="Scripts", value="scripts", variable=self.view_var, style="Nav.Toolbutton",
                    command=self.show_view).pack(side="left")
    ttk.Radiobutton(top, text="Control", value="control", variable=self.view_var, style="Nav.Toolbutton",
                    command=self.show_view).pack(side="left", padx=(4, 16))
    self.led = tk.Canvas(top, width=18, height=18, highlightthickness=0)
    self.led_dot = self.led.create_oval(2, 2, 16, 16, fill="grey", outline="")
    self.led.pack(side="left")
    ttk.Label(top, textvariable=self.status_var, font=("Segoe UI", 10, "bold")).pack(side="left", padx=6)
    ttk.Checkbutton(top, text="Dark mode", variable=self.dark_var, command=self.toggle_dark).pack(side="right")

    # IP row, shared by both views
    ip_row = ttk.Frame(self)
    ip_row.pack(fill="x", **pad)
    ttk.Label(ip_row, text="ZC95 IP:").pack(side="left")
    ip_entry = ttk.Entry(ip_row, textvariable=self.ip_var, width=20)
    ip_entry.pack(side="left", padx=4)
    ip_entry.bind("<Return>", lambda e: self.check_available(reschedule=False))
    ttk.Button(ip_row, text="Save settings", command=self.save_settings_clicked).pack(side="right", padx=4)

    # The two views share this space
    self.view_area = ttk.Frame(self)
    self.view_area.pack(fill="both", expand=True)
    self.scripts_view = ttk.Frame(self.view_area)
    self.build_scripts_view(self.scripts_view)
    self.control_view = ControlView(self.view_area, self)

    # Log
    log_frame = ttk.LabelFrame(self, text="Log")
    log_frame.pack(fill="both", **pad)
    self.log = tk.Text(log_frame, height=7, state="disabled", wrap="word", font=("Consolas", 9))
    log_scroll = ttk.Scrollbar(log_frame, command=self.log.yview)
    self.log.configure(yscrollcommand=log_scroll.set)
    log_scroll.pack(side="right", fill="y")
    self.log.pack(fill="both", expand=True)

  def build_scripts_view(self, parent):
    pad = {"padx": 6, "pady": 4}

    folder_row = ttk.Frame(parent)
    folder_row.pack(fill="x", **pad)
    ttk.Label(folder_row, text="Scripts folder:").pack(side="left")
    path_entry = ttk.Entry(folder_row, textvariable=self.path_var)
    path_entry.pack(side="left", fill="x", expand=True, padx=4)
    path_entry.bind("<Return>", lambda e: self.read_folder())
    ttk.Button(folder_row, text="Browse...", command=self.browse).pack(side="left")
    ttk.Button(folder_row, text="Read", command=self.read_folder).pack(side="left", padx=4)

    # Local scripts (left) and ZC95 slots (right)
    main = ttk.PanedWindow(parent, orient="horizontal")
    main.pack(fill="both", expand=True, **pad)

    left = ttk.LabelFrame(main, text="Local scripts")
    main.add(left, weight=3)
    self.local_list = self.make_listbox(left)
    self.local_list.bind("<Double-Button-1>", lambda e: self.upload())

    upload_row = ttk.Frame(left)
    upload_row.pack(fill="x", padx=4, pady=4)
    ttk.Label(upload_row, text="Slot:").pack(side="left")
    self.slot_combo = ttk.Combobox(upload_row, textvariable=self.slot_var, values=SLOTS, width=4, state="readonly")
    self.slot_combo.pack(side="left", padx=4)
    self.upload_btn = ttk.Button(upload_row, text="Upload", command=self.upload)
    self.upload_btn.pack(side="left", padx=4)
    self.progress = ttk.Progressbar(upload_row, mode="determinate")
    self.progress.pack(side="left", fill="x", expand=True, padx=4)

    right = ttk.LabelFrame(main, text="Scripts on ZC95")
    main.add(right, weight=2)
    self.remote_list = self.make_listbox(right)
    self.remote_list.bind("<<ListboxSelect>>", self.on_remote_select)

    remote_btns = ttk.Frame(right)
    remote_btns.pack(fill="x", padx=4, pady=4)
    self.refresh_btn = ttk.Button(remote_btns, text="Refresh", command=self.refresh_remote)
    self.refresh_btn.pack(side="left")
    self.delete_btn = ttk.Button(remote_btns, text="Delete slot", command=self.delete_slot)
    self.delete_btn.pack(side="left", padx=4)

  def make_listbox(self, parent):
    frame = ttk.Frame(parent)
    frame.pack(fill="both", expand=True, padx=4, pady=4)
    listbox = tk.Listbox(frame, exportselection=False, font=("Segoe UI", 10), activestyle="none")
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
    if self.was_connected and not connected:
      self.control_view.on_disconnected()
    self.was_connected = connected

    text = ""
    while not self.log_queue.empty():
      text += self.log_queue.get_nowait()
    if text:
      self.log.configure(state="normal")
      self.log.insert("end", text)
      self.log.see("end")
      self.log.configure(state="disabled")
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

  # ---------- Availability indicator ----------

  def check_available(self, reschedule=True):
    ip = self.ip_var.get().strip()

    def worker():
      ok = False
      if ip:
        try:
          with socket.create_connection((ip.split(":")[0], 80), timeout=2):
            ok = True
        except OSError:
          pass
      self.on_ui(self.set_available, ip, ok)

    threading.Thread(target=worker, daemon=True).start()
    if reschedule:
      self.after(PING_INTERVAL_MS, self.check_available)

  def set_available(self, ip, ok):
    if not ip:
      self.led.itemconfigure(self.led_dot, fill="grey")
      self.status_var.set("Enter the ZC95 IP address")
    elif ok:
      self.led.itemconfigure(self.led_dot, fill="#2ecc40")
      self.status_var.set("ZC95 available (" + ip + ")")
    else:
      self.led.itemconfigure(self.led_dot, fill="#e74c3c")
      self.status_var.set("ZC95 not reachable (" + ip + ")")

    # Load the lists the first time the box comes online
    if ok and not self.available:
      if not self.busy:
        self.refresh_remote()
      if self.view_var.get() == "control":
        self.control_view.load_patterns()
    self.available = ok

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
    print("Found " + str(len(self.local_files)) + " .lua script(s) in " + folder)

  # ---------- ZC95 script operations ----------

  def refresh_remote(self):
    self.run_job(self.do_refresh)

  def do_refresh(self, msgs):
    scripts = msgs.SendGetLuaScripts()
    self.on_ui(self.show_remote, scripts)

  def show_remote(self, scripts):
    self.remote_list.delete(0, "end")
    self.remote_scripts = scripts
    for script in scripts:
      name = script.get("Name") or "(empty)"
      self.remote_list.insert("end", str(script["Index"]) + " - " + name)

  def on_remote_select(self, event):
    # Selecting a slot on the right also picks it as the upload target
    sel = self.remote_list.curselection()
    if sel:
      self.slot_var.set(str(self.remote_scripts[sel[0]]["Index"]))

  def upload(self):
    sel = self.local_list.curselection()
    if not sel:
      messagebox.showwarning("No script", "Select a script to upload.")
      return
    path = os.path.join(self.path_var.get().strip(), self.local_files[sel[0]])
    try:
      with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    except OSError as e:
      messagebox.showerror("Can't read script", str(e))
      return
    self.run_job(self.do_upload, os.path.basename(path), lines, int(self.slot_var.get()))

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

    print("Uploaded " + name + " to slot " + str(index) + ".")
    self.on_ui(self.show_remote, msgs.SendGetLuaScripts())

  def delete_slot(self):
    sel = self.remote_list.curselection()
    if not sel:
      messagebox.showwarning("No slot", "Select a slot on the right to delete.")
      return
    index = self.remote_scripts[sel[0]]["Index"]
    if messagebox.askyesno("Delete script", "Delete the script in slot " + str(index) + " on the ZC95?"):
      self.run_job(self.do_delete, index)

  def do_delete(self, msgs, index):
    if msgs.SendDeleteLuaScript(index) is None:
      print("Delete failed")
      return
    print("Deleted slot " + str(index) + ".")
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
    self.apply_theme()
    self.save_settings(dark=self.dark_var.get())  # the theme choice is remembered straight away

  def apply_theme(self):
    dark = self.dark_var.get()
    c = self.colors()
    style = self.style

    if dark:
      # The native "vista" theme ignores colours, so dark mode uses "clam" restyled
      style.theme_use("clam")
      style.configure(".", background=c["bg"], foreground=c["fg"], fieldbackground=c["field"],
                      bordercolor=c["border"], darkcolor=c["bg"], lightcolor=c["bg"], troughcolor=c["field"],
                      selectbackground=c["select"], selectforeground=c["select_fg"],
                      insertcolor=c["fg"], arrowcolor=c["fg"], focuscolor=c["border"])
      style.map(".", foreground=[("disabled", c["disabled"])])
      style.configure("TButton", background=c["button"], padding=(8, 3))
      style.map("TButton", background=[("disabled", c["bg"]), ("pressed", c["select"]), ("active", c["button_active"])])
      style.configure("Toolbutton", background=c["button"], padding=(10, 4))
      style.map("Toolbutton", background=[("selected", c["select"]), ("active", c["button_active"])])
      style.map("TCombobox", fieldbackground=[("readonly", c["field"])], foreground=[("readonly", c["fg"])],
                selectbackground=[("readonly", c["field"])], selectforeground=[("readonly", c["fg"])],
                background=[("active", c["button_active"])])
      style.configure("TCheckbutton", indicatorbackground=c["field"], indicatorforeground=c["fg"])
      style.map("TCheckbutton", background=[("active", c["bg"])],
                indicatorbackground=[("selected", c["select"]), ("active", c["button_active"])])
      style.configure("TLabelframe", bordercolor=c["border"])
      style.configure("TScrollbar", background=c["button"], troughcolor=c["bg"])
      style.map("TScrollbar", background=[("active", c["button_active"])])
      style.configure("TScale", background=c["button"], troughcolor=c["field"])
      style.map("TScale", background=[("active", c["button_active"])])
      style.configure("Horizontal.TProgressbar", background=c["accent"])
      style.configure("Danger.TButton", background=c["danger"], foreground="white", font=("Segoe UI", 10, "bold"))
      style.map("Danger.TButton", background=[("pressed", c["danger"]), ("active", c["danger_active"])])
    else:
      style.theme_use(self.light_theme)
      style.configure("Toolbutton", padding=(10, 4))
      style.configure("Danger.TButton", foreground=c["danger"], font=("Segoe UI", 10, "bold"))
    style.configure("Nav.Toolbutton", font=("Segoe UI", 10, "bold"), padding=(14, 5))
    style.configure("Title.TLabel", font=("Segoe UI", 14, "bold"))
    style.configure("Warn.TLabel", foreground=c["warn"])

    self.configure(bg=c["bg"])
    self.led.configure(bg=c["bg"])
    for listbox in (self.local_list, self.remote_list):
      listbox.configure(bg=c["field"], fg=c["fg"], selectbackground=c["select"], selectforeground=c["select_fg"],
                        highlightbackground=c["bg"], highlightcolor=c["select"])
    self.log.configure(bg=c["field"], fg=c["fg"], insertbackground=c["fg"],
                       highlightbackground=c["bg"], highlightcolor=c["select"])

    # Drop-down popup lists are plain tk Listboxes
    for combo in (self.slot_combo, self.control_view.pattern_combo):
      popdown = self.tk.call("ttk::combobox::PopdownWindow", combo)
      self.tk.call(popdown + ".f.l", "configure", "-background", c["field"], "-foreground", c["fg"],
                   "-selectbackground", c["select"], "-selectforeground", c["select_fg"])

    self.control_view.apply_colors(c)
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
