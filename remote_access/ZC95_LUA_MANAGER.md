# ZC95 Lua Manager (Windows)

A small Windows GUI for a ZC95 connected over WiFi, with two views picked by the buttons along the top:

- **Scripts** (shown when the app opens): list, upload and delete Lua scripts, like `lua_manage.py` / `lua_upload.py`.
- **Control**: start a pattern and drive it, like the browser remote in `webgui.py`.

It uses `lib/ZcWs.py` and `lib/ZcMessages.py` over a single connection to the ZC95.

## What to install

1. **Python 3.8 or newer** from <https://www.python.org/downloads/windows/>.
   During install tick **"Add python.exe to PATH"** and keep the **"tcl/tk and IDLE"** option ticked
   (Tkinter, the GUI toolkit, comes with it). The Microsoft Store Python also works.
2. **websocket-client**, the only extra package. From this folder run:

   ```
   py -m pip install -r requirements-manager.txt
   ```

Nothing else is needed: `pyserial` is only needed for the serial versions of the command-line scripts,
and the `fastapi`/`uvicorn`/`websockets` packages in `requirements-webgui.txt` are only for running `webgui.py` itself.

## Running

- Double-click **`ZC95 Lua Manager.bat`** (no console window), or
- from a terminal in this folder: `py zc95_lua_manager.py`

Keep `zc95_lua_manager.py` next to the `lib` folder.

## Common to both views

- **ZC95 IP**: the dot at the top turns green when the ZC95 answers (checked every 5 seconds).
- **Save settings** stores the current IP and scripts folder so they're filled in next time the app opens.
- **Dark mode** (top right) switches the theme; the choice is remembered straight away.
- The **Log** pane shows connection messages, errors from the ZC95, and `print()` output from running Lua scripts.

Settings are kept in `%APPDATA%\zc95_lua_manager\settings.json`.

## Scripts view

1. When the ZC95 comes online, the **Scripts on ZC95** list on the right fills in automatically. **Refresh** reloads it.
2. Enter or **Browse...** to a folder of `.lua` scripts and click **Read**. The scripts are listed on the left.
3. Select a script, pick a slot (0-4) from the **Slot** drop-down, and click **Upload**
   (double-clicking a script also uploads it). Clicking a slot on the right also selects it in the drop-down.
4. **Delete slot** removes the selected script from the ZC95 (asks first).

## Control view

1. Pick a pattern from the **Pattern** list and click **Start**. The list is reloaded each time you open the
   Control view (so newly uploaded scripts appear); **Reload list** refreshes it by hand.
2. While a pattern runs:
   - **Pattern settings** shows its menu options: sliders with -/+ for numeric values, buttons for choices.
     Changes made on the ZC95's own front panel show up here too.
   - The **soft button** (if the pattern has one) is held for as long as you hold the mouse button down on it.
   - **Channel power**: each channel has a power slider (-/+ step 1%). The bar shows the power the box is
     using (blue), the actual output (yellow) and, if set, the front-panel power limit (green, hatched beyond it).
     Power changes are sent at most every 250 ms, the same as the web UI.
   - Patterns marked **(!) triphase** show a warning, as in the web UI.
3. **Stop** stops the pattern and returns to the pattern list.

Closing the app stops any running pattern before disconnecting. If the connection drops, the controls close
and a message appears in the Log.

Differences from `webgui.py`: this is a desktop app, so only this PC can control the ZC95. It doesn't run a
web server, so phones and other devices can't connect to it.
