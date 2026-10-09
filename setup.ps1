# setup.ps1 - bereitet PowerShell unter Windows fuer interview_merge.py vor
#
#   Benutzung (im Projektordner, in PowerShell):   . .\setup.ps1
#   (Punkt, Leerzeichen, dann der Pfad - sonst gilt die Umgebung nicht im Fenster.)
#
# Prueft bzw. installiert:
#   - winget (Windows-Paketmanager)
#   - ffmpeg (Gyan "full"-Build: Texte/drawtext, x265, Rauschunterdrueckung)
#   - Python 3.9+
#   - Python-Umgebung .venv mit numpy und tzdata
# und setzt PATH + Umgebung fuer das aktuelle PowerShell-Fenster.

if ($MyInvocation.InvocationName -ne '.') {
    Write-Host "Bitte so starten:  . .\setup.ps1" -ForegroundColor Yellow
    Write-Host "(Punkt + Leerzeichen davor, sonst gelten PATH und Python-Umgebung nicht in diesem Fenster.)"
    return
}

function _im_ok   ($m) { Write-Host "  [OK] $m"     -ForegroundColor Green }
function _im_warn ($m) { Write-Host "  [!]  $m"     -ForegroundColor Yellow }
function _im_err  ($m) { Write-Host "  [X]  $m"     -ForegroundColor Red }

function _im_refresh_path {
    # PATH aus der Registry neu laden (nach winget-Installationen) - .venv bleibt vorne
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user    = [Environment]::GetEnvironmentVariable('Path', 'User')
    $links   = Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links'
    $parts   = @()
    if ($env:VIRTUAL_ENV) { $parts += (Join-Path $env:VIRTUAL_ENV 'Scripts') }
    $parts  += @($links, $user, $machine)
    $env:Path = ($parts | Where-Object { $_ }) -join ';'
}

function _im_winget_install ($id, $label) {
    _im_warn "$label fehlt - installiere mit winget (kann ein paar Minuten dauern) ..."
    winget install --id $id -e --source winget --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) {
        _im_err "Installation von $label fehlgeschlagen (winget-Code $LASTEXITCODE)."
        return $false
    }
    _im_refresh_path
    return $true
}

function _im_find_python {
    # Bevorzugt den Python-Launcher "py"; ignoriert den Microsoft-Store-Platzhalter
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $v = & py -3 -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0 -and $v) { return @{ Cmd = 'py'; Args = @('-3'); Version = $v } }
    }
    $p = Get-Command python -ErrorAction SilentlyContinue
    if ($p -and $p.Source -notmatch 'WindowsApps') {
        $v = & python -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0 -and $v) { return @{ Cmd = 'python'; Args = @(); Version = $v } }
    }
    return $null
}

function _im_setup {
    Write-Host "Bereite PowerShell fuer interview_merge.py vor ..."
    Write-Host ""
    $dir = $PSScriptRoot

    # --- 1. winget ------------------------------------------------------------
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        _im_err "winget nicht gefunden."
        Write-Host "    Installieren: Microsoft Store -> 'App-Installer' aktualisieren/installieren."
        Write-Host "    Danach PowerShell neu oeffnen und nochmal: . .\setup.ps1"
        return $false
    }
    _im_ok "winget"

    # --- 2. ffmpeg --------------------------------------------------------------
    _im_refresh_path
    if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
        if (-not (_im_winget_install 'Gyan.FFmpeg' 'ffmpeg')) { return $false }
    }
    $ff = Get-Command ffmpeg -ErrorAction SilentlyContinue
    if (-not $ff) {
        _im_err "ffmpeg nach der Installation nicht gefunden. PowerShell neu oeffnen und nochmal starten."
        return $false
    }
    $ver = ((& ffmpeg -hide_banner -version 2>$null) | Select-Object -First 1) -replace '^ffmpeg version (\S+).*', '$1'
    _im_ok "ffmpeg $ver  ($($ff.Source))"

    $filters  = (& ffmpeg -hide_banner -filters  2>$null) -join "`n"
    $encoders = (& ffmpeg -hide_banner -encoders 2>$null) -join "`n"
    $missing = @()
    foreach ($f in 'drawtext', 'afftdn', 'dynaudnorm', 'highpass', 'amerge') {
        if ($filters -notmatch "\s$f\s") { $missing += $f }
    }
    if ($missing.Count -gt 0) {
        _im_err "Diesem ffmpeg fehlen Filter: $($missing -join ', ')"
        Write-Host "    Vermutlich ein Minimal-Build. Vollen Build installieren:"
        Write-Host "      winget install --id Gyan.FFmpeg -e"
        Write-Host "    und sicherstellen, dass er im PATH vor dem anderen ffmpeg steht ($($ff.Source))."
        return $false
    }
    if ($encoders -match '\slibx265\s') {
        _im_ok "Texte (drawtext), Tonaufbereitung, H.265"
    } else {
        _im_warn "libx265 fehlt - in config.json z.B. `"vcodec`": `"libx264`" setzen"
    }
    $hw = @()
    if ($encoders -match '\shevc_nvenc\s') { $hw += 'hevc_nvenc (NVIDIA)' }
    if ($encoders -match '\shevc_qsv\s')   { $hw += 'hevc_qsv (Intel)' }
    if ($encoders -match '\shevc_amf\s')   { $hw += 'hevc_amf (AMD)' }
    if ($hw.Count -gt 0) {
        Write-Host "       Hardware-Encoder im ffmpeg vorhanden: $($hw -join ', ')" -ForegroundColor DarkGray
        Write-Host "       (funktionieren nur mit passender Grafikkarte; Einsatz ueber `"vcodec`" in config.json)" -ForegroundColor DarkGray
    }

    # --- 3. Python --------------------------------------------------------------
    $venv = Join-Path $dir '.venv'
    $venvPy = Join-Path $venv 'Scripts\python.exe'
    if (-not (Test-Path $venvPy)) {
        $py = _im_find_python
        if (-not $py) {
            if (-not (_im_winget_install 'Python.Python.3.12' 'Python')) { return $false }
            $py = _im_find_python
            if (-not $py) {
                _im_err "Python nach der Installation nicht gefunden. PowerShell neu oeffnen und nochmal starten."
                return $false
            }
        }
        $maj, $min = $py.Version.Split('.') | ForEach-Object { [int]$_ }
        if ($maj -lt 3 -or ($maj -eq 3 -and $min -lt 9)) {
            _im_err "Python 3.9 oder neuer noetig (gefunden: $($py.Version))."
            Write-Host "    Installieren:  winget install --id Python.Python.3.12 -e"
            return $false
        }
        _im_warn "Lege Python-Umgebung .venv an (Python $($py.Version)) ..."
        & $py.Cmd @($py.Args + @('-m', 'venv', $venv))
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path $venvPy)) {
            _im_err "venv konnte nicht angelegt werden."
            return $false
        }
    }

    if ($env:VIRTUAL_ENV -ne $venv) {
        $activate = Join-Path $venv 'Scripts\Activate.ps1'
        try {
            . $activate
        } catch {
            _im_err "venv konnte nicht aktiviert werden: $($_.Exception.Message)"
            Write-Host "    Wahrscheinlich blockiert die Skript-Richtlinie. Einmalig ausfuehren:"
            Write-Host "      Set-ExecutionPolicy -Scope CurrentUser RemoteSigned"
            return $false
        }
    }
    $pyver = (& python --version 2>&1)
    _im_ok "Python-Umgebung .venv  ($pyver)"

    & python -c "import numpy, tzdata" 2>$null
    if ($LASTEXITCODE -ne 0) {
        _im_warn "numpy / tzdata fehlen - installiere ..."
        & python -m pip install --quiet --disable-pip-version-check numpy tzdata
        if ($LASTEXITCODE -ne 0) {
            _im_err "Installation von numpy/tzdata fehlgeschlagen."
            return $false
        }
    }
    $npv = (& python -c "import numpy; print(numpy.__version__)")
    _im_ok "numpy $npv"

    & python -c "from zoneinfo import ZoneInfo; ZoneInfo('Europe/Berlin')" 2>$null
    if ($LASTEXITCODE -eq 0) { _im_ok "Zeitzonen" } else { _im_err "Zeitzonen-Daten fehlen (pip install tzdata)" }

    # --- 4. Projektdateien ------------------------------------------------------
    Write-Host ""
    if (Test-Path (Join-Path $dir 'interview_merge.py')) { _im_ok "interview_merge.py" }
    else { _im_err "interview_merge.py nicht im Ordner $dir" }
    if (Test-Path (Join-Path $dir 'config.json')) { _im_ok "config.json" }
    else { _im_warn "keine config.json - es gelten die Standardwerte (Zeitzone = Rechner-Zeitzone!)" }
    foreach ($d in 'lav_interviewer', 'lav_befragter', 'sony', 'gopro') {
        $p = Join-Path $dir $d
        if (Test-Path $p -PathType Container) {
            $n = @(Get-ChildItem $p -File | Where-Object { -not $_.Name.StartsWith('.') }).Count
            _im_ok "Ordner $d  ($n Dateien)"
        } else {
            _im_warn "Ordner $d fehlt (ok, wenn das Material woanders liegt)"
        }
    }

    Write-Host ""
    Write-Host "Fertig. In diesem Fenster kannst du jetzt starten:"
    Write-Host "  python interview_merge.py . --dry-run    # Zeiten pruefen"
    Write-Host "  python interview_merge.py .              # rendern"
    Write-Host '  python interview_merge.py "E:\Porsche\Interview 1" -o "E:\Export" -n "Interview 1"'
    return $true
}

$null = _im_setup
Remove-Item -Path function:_im_setup, function:_im_ok, function:_im_warn, function:_im_err,
                  function:_im_refresh_path, function:_im_winget_install, function:_im_find_python `
            -ErrorAction SilentlyContinue
