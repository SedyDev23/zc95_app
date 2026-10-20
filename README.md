# ZC95 Lua Manager

A Windows desktop app for a [ZC95](https://github.com/CrashOverride85/zc95) connected over WiFi.

- **Scripts**: upload Lua scripts from a folder on your PC to the ZC95's slots 0-4, and list or delete the
  scripts already on the box.
- **Control**: start a pattern and drive it (menu options, soft button, channel power), like the browser
  remote in `webgui.py`.

Also included: dark mode, and a log pane that shows ZC95 errors and Lua `print()` output.

## Quick start

1. Install **Python 3.8+** from [python.org](https://www.python.org/downloads/windows/). Tick
   "Add python.exe to PATH" and keep "tcl/tk" selected.
2. Install the one extra package:

   ```
   cd remote_access
   py -m pip install -r requirements-manager.txt
   ```

3. Double-click `remote_access\ZC95 Lua Manager.bat`, or run `py zc95_lua_manager.py`.

See [remote_access/ZC95_LUA_MANAGER.md](remote_access/ZC95_LUA_MANAGER.md) for full instructions.

## What's in here

| Path | |
|---|---|
| `remote_access/zc95_lua_manager.py` | The desktop app (Tkinter) |
| `remote_access/ZC95 Lua Manager.bat` | Launcher with no console window |
| `remote_access/ZC95_LUA_MANAGER.md` | How to install and use the app |
| `remote_access/lib/`, `lua_manage.py`, `lua_upload.py`, `pattern_list.py`, `pattern_gui.py`, `webgui.py`, `web_static/` | Remote-access code from the ZC95 project, which the app uses |
| `remote_access/lua/` | Example Lua scripts from the ZC95 project |
| `remote_access/LuaNotes.md` | The ZC95 project's Lua scripting reference |

## Credits

The ZC95 and its remote-access code (`lib/`, the command-line scripts, the web remote and the example Lua
scripts) are from the [ZC95 project](https://github.com/CrashOverride85/zc95). This repository adds the
Windows desktop app (`zc95_lua_manager.py` and its launcher and documentation).

## Licence

TODO: add a `LICENSE` file that matches the ZC95 project's licence before publishing. The files copied from
that project stay under its licence.
