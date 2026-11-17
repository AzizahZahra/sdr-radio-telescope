@echo off
REM Build RadioTelescope.exe on Windows. Run this file from the folder that
REM contains main.py, control.py and sky_data.py.

python -m pip install --upgrade pip
python -m pip install -r requirements.txt pyinstaller
if errorlevel 1 goto fail

REM The old exe cannot be replaced while it is running.
:checkrun
tasklist /FI "IMAGENAME eq RadioTelescope.exe" 2>nul | find /I "RadioTelescope.exe" >nul
if not errorlevel 1 (
  echo.
  echo RadioTelescope.exe is still running. Close it, or end it in Task Manager,
  echo then press any key to continue.
  pause >nul
  goto checkrun
)

python -m PyInstaller --noconfirm --clean --onefile --windowed --name RadioTelescope ^
  --collect-data customtkinter --collect-data astropy --collect-data astropy_iers_data ^
  main.py
if errorlevel 1 goto fail

copy /Y control.py dist\control.py >nul
echo.
echo Done: dist\RadioTelescope.exe
echo To change telescope behaviour, edit dist\control.py and restart the exe.
pause
exit /b 0

:fail
echo.
echo Build failed - see the messages above.
pause
exit /b 1
