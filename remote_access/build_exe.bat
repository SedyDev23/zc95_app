@echo off
rem Builds dist\ZC95 Lua Manager.exe - a single file that runs without Python installed.
rem Needs Python 3.8+ (the "py" launcher) and an internet connection the first time.
rem It uses its own virtual environment (.build-venv), so nothing is installed into your main Python.
setlocal
cd /d "%~dp0"

if not exist ".build-venv\Scripts\python.exe" (
  echo Creating build environment...
  py -m venv .build-venv || goto :error
)
set PY=.build-venv\Scripts\python.exe

echo Installing build tools...
"%PY%" -m pip install --quiet --upgrade pip || goto :error
"%PY%" -m pip install --quiet -r requirements-build.txt || goto :error

echo Making icon...
"%PY%" build\make_icon.py build\zc95.ico || goto :error

echo Building exe...
"%PY%" -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name "ZC95 Lua Manager" ^
  --icon "%~dp0build\zc95.ico" ^
  --distpath dist --workpath build\work --specpath build\work ^
  zc95_lua_manager.py || goto :error

rem Put the example scripts next to the exe, where the app looks for them by default
if exist dist\lua rmdir /s /q dist\lua
xcopy /e /i /q lua dist\lua >nul

echo.
echo Done: %~dp0dist\ZC95 Lua Manager.exe
echo (the dist folder also has the example scripts in dist\lua)
exit /b 0

:error
echo.
echo Build failed.
exit /b 1
