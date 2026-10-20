@echo off
cd /d "%~dp0"
echo [1/3] Installing libraries...
python -m pip install pygame-ce websocket-client pyinstaller
if errorlevel 1 goto :error
echo [2/3] Building exe... (this takes a few minutes)
python -m PyInstaller --noconsole --onefile --name osu_beat_sound nc_beat.py
if errorlevel 1 goto :error
echo [3/3] Done!
echo Your exe is here: dist\osu_beat_sound.exe
echo Put the "sounds" folder next to the exe when you share it.
goto :end
:error
echo.
echo *** An error occurred. Please copy the messages above and send them. ***
:end
echo.
pause
