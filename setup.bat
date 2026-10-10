@echo off
rem setup.bat - bereitet die Eingabeaufforderung (cmd) fuer interview_merge.py vor
rem
rem   Benutzung (im Projektordner, in cmd):   setup
rem   NICHT per Doppelklick starten - sonst schliesst sich das Fenster und
rem   PATH / Python-Umgebung gehen verloren.
rem
rem Prueft bzw. installiert per winget:
rem   - ffmpeg (Gyan-Build mit Texten/drawtext, x265, Tonfiltern)
rem   - Python 3.9+
rem   - Python-Umgebung .venv mit numpy und tzdata

echo Bereite cmd fuer interview_merge.py vor ...
echo.

set "_IM_DIR=%~dp0"
if "%_IM_DIR:~-1%"=="\" set "_IM_DIR=%_IM_DIR:~0,-1%"
set "_IM_TMP=%TEMP%\im_setup_%RANDOM%.txt"

rem ---------------------------------------------------------------- 1. winget
call :have winget
if errorlevel 1 goto :no_winget
echo   [OK] winget

call :addpath "%LOCALAPPDATA%\Microsoft\WinGet\Links"

rem ---------------------------------------------------------------- 2. ffmpeg
call :have ffmpeg
if not errorlevel 1 goto :ff_have
echo   [!]  ffmpeg fehlt - installiere mit winget, das kann ein paar Minuten dauern ...
call winget install --id Gyan.FFmpeg -e --source winget --accept-package-agreements --accept-source-agreements
if errorlevel 1 goto :ff_install_failed
call :addpath "%LOCALAPPDATA%\Microsoft\WinGet\Links"
call :have ffmpeg
if errorlevel 1 goto :reopen
:ff_have
set "_IM_FFV="
for /f "tokens=3" %%v in ('ffmpeg -hide_banner -version 2^>nul ^| find "ffmpeg version"') do set "_IM_FFV=%%v"
echo   [OK] ffmpeg %_IM_FFV%

call ffmpeg -hide_banner -filters > "%_IM_TMP%" 2>nul
set "_IM_MISSING="
for %%f in (drawtext afftdn dynaudnorm highpass amerge) do call :chkfilter %%f
if defined _IM_MISSING goto :ff_missing

call ffmpeg -hide_banner -encoders > "%_IM_TMP%" 2>nul
find " libx265 " "%_IM_TMP%" >nul
if errorlevel 1 (
    echo   [!]  libx265 fehlt - in config.json z.B. "vcodec": "libx264" setzen
) else (
    echo   [OK] Texte drawtext, Tonaufbereitung, H.265
)
set "_IM_HW="
call :chkenc hevc_nvenc NVIDIA
call :chkenc hevc_qsv Intel
call :chkenc hevc_amf AMD
if defined _IM_HW echo        Hardware-Encoder im ffmpeg:%_IM_HW%  - nur mit passender Grafikkarte nutzbar

rem ---------------------------------------------------------------- 3. Python + .venv
if exist "%_IM_DIR%\.venv\Scripts\python.exe" goto :venv_have

call :findpy
if defined _IM_PY goto :py_have
echo   [!]  Python fehlt - installiere mit winget ...
call winget install --id Python.Python.3.12 -e --source winget --accept-package-agreements --accept-source-agreements
if errorlevel 1 goto :py_install_failed
call :addpath "%LOCALAPPDATA%\Programs\Python\Launcher"
call :addpath "%LOCALAPPDATA%\Programs\Python\Python312"
call :addpath "%LOCALAPPDATA%\Programs\Python\Python312\Scripts"
call :findpy
if not defined _IM_PY goto :reopen
:py_have
call %_IM_PY% -c "import sys; sys.exit(sys.version_info < (3, 9))" >nul 2>nul
if errorlevel 1 goto :py_too_old
echo   [!]  Lege Python-Umgebung .venv an ...
call %_IM_PY% -m venv "%_IM_DIR%\.venv"
if not exist "%_IM_DIR%\.venv\Scripts\python.exe" goto :venv_failed
:venv_have
call "%_IM_DIR%\.venv\Scripts\activate.bat"
if not defined VIRTUAL_ENV goto :venv_failed
for /f "tokens=*" %%v in ('python --version 2^>^&1') do set "_IM_PYV=%%v"
echo   [OK] Python-Umgebung .venv - %_IM_PYV%

call python -c "import numpy, tzdata" >nul 2>nul
if not errorlevel 1 goto :pkgs_have
echo   [!]  numpy / tzdata fehlen - installiere ...
call python -m pip install --quiet --disable-pip-version-check numpy tzdata
if errorlevel 1 goto :pip_failed
:pkgs_have
for /f "tokens=*" %%v in ('python -c "import numpy; print(numpy.__version__)"') do set "_IM_NPV=%%v"
echo   [OK] numpy %_IM_NPV%
call python -c "from zoneinfo import ZoneInfo; ZoneInfo('Europe/Berlin')" >nul 2>nul
if errorlevel 1 (
    echo   [X]  Zeitzonen-Daten fehlen - pip install tzdata
) else (
    echo   [OK] Zeitzonen
)

rem ---------------------------------------------------------------- 4. Projektdateien
echo.
if exist "%_IM_DIR%\interview_merge.py" (
    echo   [OK] interview_merge.py
) else (
    echo   [X]  interview_merge.py nicht im Ordner %_IM_DIR%
)
if exist "%_IM_DIR%\config.json" (
    echo   [OK] config.json
) else (
    echo   [!]  keine config.json - es gelten die Standardwerte, Zeitzone = Rechner-Zeitzone!
)
for %%d in (lav_interviewer lav_befragter sony gopro) do call :chkdir %%d

echo.
echo Fertig. In diesem Fenster kannst du jetzt starten:
echo   python interview_merge.py . --dry-run
echo   python interview_merge.py .
echo   python interview_merge.py "E:\Porsche\Interview 1" -o "E:\Export" -n "Interview 1"
goto :cleanup

rem ---------------------------------------------------------------- Fehler
:no_winget
echo   [X]  winget nicht gefunden.
echo        Microsoft Store oeffnen und "App-Installer" installieren bzw. aktualisieren,
echo        danach cmd neu oeffnen und nochmal: setup
goto :cleanup
:ff_install_failed
echo   [X]  Installation von ffmpeg fehlgeschlagen.
goto :cleanup
:ff_missing
echo   [X]  Diesem ffmpeg fehlen Filter:%_IM_MISSING%
echo        Vermutlich ein Minimal-Build. Vollen Build installieren:
echo          winget install --id Gyan.FFmpeg -e
call :have ffmpeg
echo        und sicherstellen, dass er im PATH vor diesem ffmpeg steht:
echo          %_IM_FOUND%
goto :cleanup
:py_install_failed
echo   [X]  Installation von Python fehlgeschlagen.
goto :cleanup
:py_too_old
echo   [X]  Python 3.9 oder neuer noetig.  Installieren:  winget install --id Python.Python.3.12 -e
goto :cleanup
:venv_failed
echo   [X]  Python-Umgebung .venv konnte nicht angelegt oder aktiviert werden.
goto :cleanup
:pip_failed
echo   [X]  Installation von numpy/tzdata fehlgeschlagen.
goto :cleanup
:reopen
echo   [!]  Installation abgeschlossen, aber noch nicht im PATH dieses Fensters.
echo        Bitte cmd schliessen, neu oeffnen und nochmal: setup
goto :cleanup

:cleanup
if exist "%_IM_TMP%" del "%_IM_TMP%" >nul 2>nul
set "_IM_DIR="
set "_IM_TMP="
set "_IM_FFV="
set "_IM_MISSING="
set "_IM_HW="
set "_IM_PY="
set "_IM_PYV="
set "_IM_NPV="
set "_IM_N="
set "_IM_FOUND="
goto :eof

rem ---------------------------------------------------------------- Hilfsroutinen
:have
rem Programm im PATH suchen, ohne where.exe (Ergebnis in _IM_FOUND, errorlevel 0/1)
set "_IM_FOUND="
for %%i in (%~1.exe) do if not "%%~$PATH:i"=="" set "_IM_FOUND=%%~$PATH:i"
if not defined _IM_FOUND for %%i in (%~1.cmd) do if not "%%~$PATH:i"=="" set "_IM_FOUND=%%~$PATH:i"
if not defined _IM_FOUND for %%i in (%~1.bat) do if not "%%~$PATH:i"=="" set "_IM_FOUND=%%~$PATH:i"
if not defined _IM_FOUND for %%i in (%~1.com) do if not "%%~$PATH:i"=="" set "_IM_FOUND=%%~$PATH:i"
if defined _IM_FOUND exit /b 0
exit /b 1

:addpath
if not exist "%~1" goto :eof
echo ";%PATH%;" | find /i ";%~1;" >nul
if not errorlevel 1 goto :eof
set "PATH=%~1;%PATH%"
goto :eof

:chkfilter
find " %~1 " "%_IM_TMP%" >nul
if errorlevel 1 set "_IM_MISSING=%_IM_MISSING% %~1"
goto :eof

:chkenc
find " %~1 " "%_IM_TMP%" >nul
if not errorlevel 1 set "_IM_HW=%_IM_HW% %~1=%~2"
goto :eof

:findpy
set "_IM_PY="
call py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 (
    set "_IM_PY=py -3"
    goto :eof
)
call python -c "import sys" >nul 2>nul
if not errorlevel 1 set "_IM_PY=python"
goto :eof

:chkdir
if not exist "%_IM_DIR%\%~1\" (
    echo   [!]  Ordner %~1 fehlt - ok, wenn das Material woanders liegt
    goto :eof
)
set "_IM_N=0"
for /f "delims=" %%n in ('dir /a-d /b "%_IM_DIR%\%~1" 2^>nul') do set /a _IM_N+=1
echo   [OK] Ordner %~1 - %_IM_N% Dateien
goto :eof
