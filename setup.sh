# setup.sh – bereitet das Terminal für interview_merge.py vor
#
#   Benutzung (im Projektordner):   source setup.sh
#
# Prüft bzw. installiert:
#   - Homebrew
#   - ffmpeg-full (mit Textfunktion drawtext, x265, Rauschunterdrückung)
#   - Python-Umgebung .venv mit numpy
# und setzt PATH + Umgebung für das aktuelle Terminalfenster.

# --- muss mit "source" gestartet werden -------------------------------------
_im_sourced=0
if [ -n "$ZSH_VERSION" ]; then
  case $ZSH_EVAL_CONTEXT in *:file*) _im_sourced=1 ;; esac
  _im_dir="$(cd "$(dirname "${(%):-%x}")" && pwd)"
elif [ -n "$BASH_VERSION" ]; then
  [ "${BASH_SOURCE[0]}" != "$0" ] && _im_sourced=1
  _im_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi
if [ "$_im_sourced" != 1 ]; then
  echo "Bitte so starten:  source setup.sh"
  echo "(Sonst gelten PATH und Python-Umgebung nicht in deinem Terminal.)"
  exit 1
fi

_im_ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
_im_warn() { printf '  \033[33m!\033[0m %s\n' "$1"; }
_im_err()  { printf '  \033[31m✗\033[0m %s\n' "$1"; }

_im_setup() {
  echo "Bereite Terminal für interview_merge.py vor ..."
  echo

  # --- 1. Homebrew ----------------------------------------------------------
  if ! command -v brew >/dev/null 2>&1; then
    for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do
      [ -x "$b" ] && eval "$("$b" shellenv)" && break
    done
  fi
  if ! command -v brew >/dev/null 2>&1; then
    _im_err "Homebrew nicht gefunden."
    echo "    Installieren:  /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
    echo "    Danach Terminal neu öffnen und nochmal: source setup.sh"
    return 1
  fi
  _im_ok "Homebrew"

  # --- 2. ffmpeg-full -------------------------------------------------------
  if ! brew list ffmpeg-full >/dev/null 2>&1; then
    _im_warn "ffmpeg-full fehlt – installiere (dauert ein paar Minuten) ..."
    if ! brew install ffmpeg-full; then
      _im_err "Installation von ffmpeg-full fehlgeschlagen."
      return 1
    fi
  fi
  local ffprefix
  ffprefix="$(brew --prefix ffmpeg-full)"
  case ":$PATH:" in
    *":$ffprefix/bin:"*) ;;
    *) export PATH="$ffprefix/bin:$PATH" ;;
  esac
  hash -r 2>/dev/null; [ -n "$ZSH_VERSION" ] && rehash

  if [ "$(command -v ffmpeg)" != "$ffprefix/bin/ffmpeg" ]; then
    _im_err "Es wird das falsche ffmpeg verwendet: $(command -v ffmpeg)"
    return 1
  fi
  if ! ffmpeg -hide_banner -version >/dev/null 2>&1; then
    _im_err "ffmpeg-full startet nicht (fehlende Bibliothek?)."
    echo "    Reparieren:  brew reinstall ffmpeg-full"
    return 1
  fi
  _im_ok "ffmpeg-full  ($(ffmpeg -hide_banner -version | head -1 | awk '{print $3}'))"

  local filters encoders missing=0
  filters="$(ffmpeg -hide_banner -filters 2>/dev/null)"
  encoders="$(ffmpeg -hide_banner -encoders 2>/dev/null)"
  for f in drawtext afftdn dynaudnorm highpass amerge; do
    if ! printf '%s\n' "$filters" | grep -q " $f "; then
      _im_err "ffmpeg-Filter fehlt: $f"; missing=1
    fi
  done
  if printf '%s\n' "$encoders" | grep -q " libx265 "; then
    _im_ok "Texte (drawtext), Tonaufbereitung, H.265"
  else
    _im_warn "libx265 fehlt – nutze \"vcodec\": \"hevc_videotoolbox\" oder \"libx264\" in config.json"
  fi
  if [ "$missing" = 1 ]; then
    echo "    Reparieren:  brew reinstall ffmpeg-full"
    return 1
  fi

  # --- 3. Python + .venv + numpy -------------------------------------------
  local venv="$_im_dir/.venv"
  if [ ! -x "$venv/bin/python3" ]; then
    if ! command -v python3 >/dev/null 2>&1; then
      _im_err "python3 nicht gefunden.  Installieren:  brew install python"
      return 1
    fi
    if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))'; then
      _im_err "Python 3.9 oder neuer nötig (gefunden: $(python3 --version 2>&1))."
      echo "    Installieren:  brew install python"
      return 1
    fi
    _im_warn "Lege Python-Umgebung .venv an ..."
    python3 -m venv "$venv" || { _im_err "venv konnte nicht angelegt werden."; return 1; }
  fi
  if [ "$VIRTUAL_ENV" != "$venv" ]; then
    # shellcheck disable=SC1091
    . "$venv/bin/activate" || { _im_err "venv konnte nicht aktiviert werden."; return 1; }
  fi
  _im_ok "Python-Umgebung .venv  ($(python3 --version 2>&1))"

  if ! python3 -c 'import numpy' >/dev/null 2>&1; then
    _im_warn "numpy fehlt – installiere ..."
    python3 -m pip install --quiet --disable-pip-version-check numpy \
      || { _im_err "numpy-Installation fehlgeschlagen."; return 1; }
  fi
  _im_ok "numpy $(python3 -c 'import numpy; print(numpy.__version__)')"

  if ! python3 -c 'from zoneinfo import ZoneInfo; ZoneInfo("Europe/Berlin")' >/dev/null 2>&1; then
    _im_warn "Zeitzonendaten fehlen – installiere tzdata ..."
    python3 -m pip install --quiet --disable-pip-version-check tzdata
  fi
  _im_ok "Zeitzonen"

  # --- 4. Projektdateien ----------------------------------------------------
  echo
  if [ -f "$_im_dir/interview_merge.py" ]; then
    _im_ok "interview_merge.py"
  else
    _im_err "interview_merge.py nicht im Ordner $_im_dir"
  fi
  if [ -f "$_im_dir/config.json" ]; then
    _im_ok "config.json"
  else
    _im_warn "keine config.json – es gelten die Standardwerte (Zeitzone = Rechner-Zeitzone!)"
  fi
  for d in lav_interviewer lav_befragter sony gopro; do
    if [ -d "$_im_dir/$d" ]; then
      _im_ok "Ordner $d  ($(find "$_im_dir/$d" -maxdepth 1 -type f ! -name '.*' | wc -l | tr -d ' ') Dateien)"
    else
      _im_warn "Ordner $d fehlt"
    fi
  done

  echo
  echo "Fertig. In diesem Terminal kannst du jetzt starten:"
  echo "  python3 interview_merge.py . --dry-run    # Zeiten prüfen"
  echo "  python3 interview_merge.py .              # rendern"
  return 0
}

_im_setup
_im_rc=$?
unset -f _im_setup _im_ok _im_warn _im_err
unset _im_sourced _im_dir
return $_im_rc
