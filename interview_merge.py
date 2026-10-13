#!/usr/bin/env python3
"""
interview_merge.py – baut aus Lavs, Sony und GoPro ein durchgehendes Interview-Video.

Projektordner:
  projekt/
    lav_interviewer/   DJI Mic 2 – Interviewer
    lav_befragter/     DJI Mic 2 – Befragte*r
    sony/              Sony-Clips (Video + Ton, ggf. mit ...M01.XML)
    gopro/             GoPro-Clips (Video + Ton; .LRV/.THM werden ignoriert)
    config.json        optional, überschreibt DEFAULT_CONFIG

Ergebnis: eine MOV mit 1 Videospur + 4 getrennten Audiospuren
  Ton: 1 Spur mit 4 Kanälen: Ch1 Lav Interviewer | Ch2 Lav Befragter | Ch3 Sony | Ch4 GoPro
  Video standardmäßig H.265 (klein bei guter Qualität)

Bild: Sony vollformatig; GoPro vollformatig, wenn nur GoPro läuft;
      laufen beide, GoPro als Bild-im-Bild unten rechts; sonst schwarz.
      Einblendungen: Gesamt-Timecode oben mittig, Lav 1 oben links und Lav 2 oben rechts
      (Datei + Position), Kameradatei + Position unten links, GoPro-Name im Bild-im-Bild.

Benutzung:
  python3 interview_merge.py /pfad/projekt --dry-run   # nur Timeline prüfen
  python3 interview_merge.py /pfad/projekt              # rendern
  python3 interview_merge.py /pfad/projekt --no-sync    # ohne Audio-Feinsync

Voraussetzungen: ffmpeg/ffprobe (mit drawtext), Python 3.9+, numpy (für Sync).
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

try:
    import numpy as np
except ImportError:
    np = None

SOURCES = ["lav_interviewer", "lav_befragter", "sony", "gopro"]
LAV_SOURCES = ["lav_interviewer", "lav_befragter"]
VIDEO_SOURCES = ["sony", "gopro"]

DEFAULT_CONFIG = {
    "folders": {s: s for s in SOURCES},
    "track_names": {
        "lav_interviewer": "Lav Interviewer",
        "lav_befragter": "Lav Befragter",
        "sony": "Sony Kamera-Ton",
        "gopro": "GoPro-Ton",
    },
    "output": {
        "file": "interview_merged.mov",
        "dir": None,   # Ausgabeordner (null = Projektordner); --output überschreibt das
        "width": 1920, "height": 1080, "fps": 25,
        # "x265" (klein, Standard) | "hevc_videotoolbox" (Mac, schnell) |
        # "hevc_nvenc" / "hevc_qsv" / "hevc_amf" (Windows: NVIDIA / Intel / AMD, schnell) |
        # "libx264" | "h264_videotoolbox" | "prores" (Proxy, groß, zum Schneiden)
        # crf: null = Standard (x265: 28, x264: 23). Höher = kleiner/schlechter.
        # "auto" = schnellster funktionierender Hardware-Encoder (Mac: hevc_videotoolbox,
        #          Windows: hevc_nvenc/qsv/amf), sonst x265
        "vcodec": "auto", "crf": None, "preset": "fast", "bitrate": "3M",
        # Quellen per Hardware decodieren ("auto" = ffmpeg wählt, fällt sonst auf Software zurück; "none" = aus)
        "hwaccel": "auto",
        # Segmente gleichzeitig rendern ("auto" = 3 bei Hardware-Encoder, 1 bei x265)
        "jobs": "auto",
        "audio_rate": 48000,
        # Endgültige Tonspuren: "aac" (klein) oder "pcm" (verlustfrei, sehr groß)
        "audio_codec": "aac", "aac_mono": "96k", "aac_stereo": "160k", "aac_single": "384k",
        # "single": eine Tonspur mit 4 Kanälen (Resolve-sicher) | "separate": 4 einzelne Spuren
        "audio_tracks": "single",
    },
    # offset_s: manuelle Korrektur in Sekunden (z.B. Kamera-Uhr 3 s vor -> -3)
    # tags_are_utc: creation_time-Tag ist echte UTC (Sony) oder nur lokale Zeit mit "Z" (GoPro)
    "sources": {
        "lav_interviewer": {"offset_s": 0.0, "tags_are_utc": True, "channels": "mono"},
        "lav_befragter": {"offset_s": 0.0, "tags_are_utc": True, "channels": "mono"},
        "sony": {"offset_s": 0.0, "tags_are_utc": True, "channels": "mono", "use_timecode": False},
        "gopro": {"offset_s": 0.0, "tags_are_utc": True, "channels": "mono"},
    },
    # Zeitzone, in der die Geräte-Uhren (DJI, GoPro) laufen, z.B. "Europe/Berlin".
    # Zeitstempel mit Zonenangabe (Sony-XML, UTC-Tags) werden in diese Zone umgerechnet.
    # null = Zeitzone des Rechners (falsch, wenn der Rechner woanders steht als die Kameras!)
    "timezone": None,
    # Lav-Dateien, deren Start so nah am Ende der vorherigen liegt, werden lückenlos angehängt
    "join_tolerance_s": 3.0,
    # Pausen, in denen weder Bild noch Ton aufgenommen wurde, herausschneiden.
    # keep_s Sekunden Schwarzbild bleiben mit Hinweis (Uhrzeit, Länge) stehen.
    "cut_gaps": {"enabled": True, "min_gap_s": 10.0, "keep_s": 2.0},
    # Aufbereitung der Tonspuren für automatische Transkription (nach dem Sync)
    "audio_processing": {
        "enabled": True,
        "apply_to": ["lav_interviewer", "lav_befragter", "sony", "gopro"],
        "highpass_hz": 90,          # Rumpeln/Wind/Netzbrummen unter der Stimme entfernen (0 = aus)
        "denoise": "afftdn",        # "afftdn" | "rnnoise" (braucht rnnoise_model) | "none"
        "denoise_db": 12,           # Stärke afftdn in dB (6 = leicht, 12 = mittel, 20 = stark)
        "rnnoise_model": None,      # Pfad zu einer .rnnn-Modelldatei für "rnnoise"
        "normalize": True,          # dynamische Lautstärkeanpassung
        "max_gain_db": 20,          # maximale Anhebung leiser Stellen
        "target_rms": 0.12,         # Ziel-Lautheit (RMS, 0.12 ≈ -18 dBFS)
    },
    "pip": {"width_fraction": 0.3, "margin": 40},
    "text": {"fontfile": None, "size": 34},
    "audio_sync": {"enabled": True, "coarse": True, "coarse_min_ratio": 1.5, "coarse_min_overlap_s": 120,
                   "coarse_local_s": 300.0,
                   "search_s": 10.0, "analyze_s": 30.0, "window_step_s": 240.0,
                   "min_confidence": 8.0, "drift_min_span_s": 600.0,
                   "lav_to_lav": True},
}

AUDIO_EXT = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".aif", ".aiff"}
VIDEO_EXT = {".mp4", ".mov", ".mxf", ".mts", ".m2ts", ".avi", ".mkv"}
FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/segoeui.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
]
FNAME_RE = re.compile(r"(20\d{2})(\d{2})(\d{2})[_\-T ]?(\d{2})(\d{2})(\d{2})")
GOPRO_RE = re.compile(r"^G[HXLSP](\d{2})(\d{4})", re.I)


# --------------------------------------------------------------------------- helpers
def run(cmd):
    r = subprocess.run([str(c) for c in cmd], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if r.returncode != 0:
        err = r.stderr.decode(errors="replace")[-3000:]
        sys.exit(f"\nFehler bei:\n  {' '.join(map(str, cmd))[:1500]}\n\n{err}")
    return r.stdout


def deep_merge(base, over):
    out = dict(base)
    for k, v in over.items():
        out[k] = deep_merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def parse_iso(s):
    s = s.strip().replace("Z", "+00:00").replace(" ", "T", 1)
    m = re.match(r"(.*?\d{2}:\d{2}:\d{2})(\.\d+)?(.*)$", s)
    if m:
        frac = "." + (m.group(2)[1:] + "000000")[:6] if m.group(2) else ""
        s = m.group(1) + frac + m.group(3)
    return datetime.fromisoformat(s)


def hms(sec, frac=True):
    sign = "-" if sec < 0 else ""
    sec = round(abs(sec), 2 if frac else 0)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{sign}{int(h):02d}:{int(m):02d}:" + (f"{s:05.2f}" if frac else f"{int(s):02d}")


def filter_quote(path):
    """Pfad sicher in einen ffmpeg-Filter einsetzen (auch C:/..., Leerzeichen, Apostrophe).
    Zwei Ebenen: Filter-Optionen (\\ ' : escapen), dann Filtergraph (in '...' einpacken)."""
    p = str(path).replace("\\", "/")
    p = p.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:")
    return "'" + p.replace("'", "'\\''") + "'"


# --------------------------------------------------------------------------- clips
@dataclass
class Clip:
    path: Path
    source: str
    start_dt: datetime
    method: str
    duration: float
    has_video: bool
    has_audio: bool
    start: float = 0.0        # Sekunden ab Timeline-Beginn
    sync_note: str = ""
    drift: float = 0.0        # Gangabweichung der Geräte-Uhr (0.00002 = 20 ppm)
    real_dt: datetime = None  # echte Uhrzeit des Clipstarts nach dem Sync

    @property
    def end(self):
        return self.timeline(self.duration)

    def timeline(self, tau):
        """Position im Clip -> Position in der Timeline."""
        return self.start + tau * (1.0 + self.drift)

    def local(self, t):
        """Position in der Timeline -> Position im Clip."""
        return (t - self.start) / (1.0 + self.drift)

    def pieces(self, piece_s):
        """Clip in Abschnitte zerlegen (nur nötig, wenn Drift korrigiert wird)."""
        if abs(self.drift) < 1e-7:
            return [(0.0, self.duration)]
        out, tau = [], 0.0
        while tau < self.duration - 1e-6:
            out.append((tau, min(piece_s, self.duration - tau)))
            tau += piece_s
        return out

    @property
    def end_dt(self):
        return self.start_dt + timedelta(seconds=self.duration)


def probe(path):
    return json.loads(run(["ffprobe", "-v", "error", "-print_format", "json",
                           "-show_format", "-show_streams", path]))


TARGET_TZ = None


def to_wall(d):
    """Zeitstempel mit Zone -> naive Uhrzeit in der Geräte-Zeitzone."""
    return d.astimezone(TARGET_TZ).replace(tzinfo=None)


def detect_start(path, info, scfg, duration):
    """Startzeit (lokale Uhrzeit, naiv) + Beschreibung der Quelle."""
    tags = {k.lower(): v for k, v in info.get("format", {}).get("tags", {}).items()}
    for st in info.get("streams", []):
        for k, v in st.get("tags", {}).items():
            tags.setdefault(k.lower(), v)
    digits = lambda x: re.sub(r"\D", "", x or "")
    dt, method = None, None

    # 1) Sony XML-Sidecar (enthält Zeitzone -> zuverlässig)
    for xml in (path.with_name(path.stem + "M01.XML"), path.with_name(path.stem + "M01.xml")):
        if xml.exists():
            m = re.search(r'CreationDate\s+value="([^"]+)"', xml.read_text(errors="ignore"))
            if m:
                d = parse_iso(m.group(1))
                dt = to_wall(d) if d.tzinfo else d
                method = f"Sony-XML"
                break

    # 2) Broadcast-WAV (bext) – typisch für Audiorecorder
    if dt is None:
        od, ot, tr = tags.get("origination_date"), tags.get("origination_time"), tags.get("time_reference")
        sr = next((int(s["sample_rate"]) for s in info["streams"] if s.get("codec_type") == "audio"), 0)
        if len(digits(od)) == 8:
            day = datetime.strptime(digits(od), "%Y%m%d")
            if tr and sr and int(tr) > 0:
                dt, method = day + timedelta(seconds=int(tr) / sr), "BWF time_reference"
            elif len(digits(ot)) == 6:
                dt, method = datetime.strptime(digits(od) + digits(ot), "%Y%m%d%H%M%S"), "BWF origination"

    # 3) creation_time
    if dt is None and tags.get("creation_time"):
        try:
            d = parse_iso(tags["creation_time"])
            if d.year >= 2000:
                if d.tzinfo and scfg.get("tags_are_utc", True):
                    dt, method = to_wall(d), "creation_time (UTC->lokal)"
                else:
                    dt, method = d.replace(tzinfo=None), "creation_time (lokal)"
        except ValueError:
            pass

    # 4) Dateiname (z.B. DJI_..._20260930_143022.WAV)
    if dt is None:
        m = FNAME_RE.search(path.name)
        if m:
            try:
                dt, method = datetime(*map(int, m.groups())), "Dateiname"
            except ValueError:
                pass

    # 5) Notlösung: Änderungsdatum = Aufnahmeende
    if dt is None:
        dt = datetime.fromtimestamp(path.stat().st_mtime) - timedelta(seconds=duration)
        method = "Dateidatum (UNSICHER!)"

    # optional: Uhrzeit aus Timecode (wenn Kamera auf Time-of-Day-TC steht)
    tc = tags.get("timecode")
    if scfg.get("use_timecode") and tc:
        m = re.match(r"(\d+):(\d+):(\d+)[:;.](\d+)", tc)
        vs = next((s for s in info["streams"] if s.get("codec_type") == "video"), None)
        if m and vs:
            num, den = vs.get("r_frame_rate", "25/1").split("/")
            fps = float(num) / float(den or 1)
            h, mi, s, f = map(int, m.groups())
            dt = datetime(dt.year, dt.month, dt.day, h, mi, s) + timedelta(seconds=f / fps)
            method = "Timecode"
    return dt, method


def load_clips(root, cfg):
    clips = {s: [] for s in SOURCES}
    for s in SOURCES:
        folder = root / cfg["folders"][s]
        if not folder.is_dir():
            print(f"  ! Ordner fehlt: {folder} (Spur bleibt stumm/leer)")
            continue
        scfg = cfg["sources"][s]
        for p in sorted(folder.iterdir()):
            if p.name.startswith(".") or p.suffix.lower() not in AUDIO_EXT | VIDEO_EXT:
                continue
            info = probe(p)
            streams = info.get("streams", [])
            has_v = any(st.get("codec_type") == "video" and not st.get("disposition", {}).get("attached_pic")
                        for st in streams)
            has_a = any(st.get("codec_type") == "audio" for st in streams)
            if s in VIDEO_SOURCES and not has_v:
                print(f"  ! {p.name}: kein Video, übersprungen")
                continue
            dur = None
            if has_v:
                dur = next((float(st["duration"]) for st in streams
                            if st.get("codec_type") == "video" and st.get("duration")), None)
            dur = dur or float(info["format"].get("duration", 0))
            if dur <= 0:
                print(f"  ! {p.name}: Dauer unbekannt, übersprungen")
                continue
            dt, method = detect_start(p, info, scfg, dur)
            off = float(scfg.get("offset_s", 0))
            if off:
                dt += timedelta(seconds=off)
                method += f", Offset {off:+.2f}s"
            clips[s].append(Clip(p, s, dt, method, dur, has_v, has_a))
        clips[s].sort(key=lambda c: c.start_dt)
    return clips


def chain_and_join(clips, tol):
    for s, lst in clips.items():
        if s == "gopro":  # GoPro-Kapitel (GX01xxxx, GX02xxxx ...) direkt aneinander
            groups = {}
            for c in lst:
                m = GOPRO_RE.match(c.path.name)
                if m:
                    groups.setdefault(m.group(2), []).append((int(m.group(1)), c))
            for g in groups.values():
                g.sort(key=lambda x: x[0])
                for (_, p), (_, c) in zip(g, g[1:]):
                    c.start_dt, c.method = p.end_dt, f"GoPro-Kapitel nach {p.path.name}"
            lst.sort(key=lambda c: c.start_dt)
        # automatisch geteilte Aufnahmen (nur DJI Mic 2) lückenlos anhängen –
        # Kameraclips sind echte Start/Stopps und werden NICHT angehängt
        if s not in LAV_SOURCES:
            continue
        for prev, cur in zip(lst, lst[1:]):
            gap = (cur.start_dt - prev.end_dt).total_seconds()
            if abs(gap) <= tol and "Kapitel" not in cur.method:
                cur.start_dt = prev.end_dt
                cur.method += f" | angehängt (Lücke war {gap:+.2f}s)"


# --------------------------------------------------------------------------- audio
def build_audio_track(clips, total_s, out, channels, sr, piece_s=120.0):
    layout = "mono" if channels == "mono" else "stereo"
    fmt = f"aformat=sample_fmts=fltp:sample_rates={sr}:channel_layouts={layout}"
    total = int(round(total_s * sr))
    snap = int(0.05 * sr)        # Mini-Lücken zwischen Drift-Abschnitten schließen
    inputs, chains, labels = [], [], []
    cur, prev = 0, None

    def silence(n):
        k = len(labels)
        chains.append(f"anullsrc=r={sr}:cl={layout},{fmt},atrim=end_sample={n}[s{k}]")
        labels.append(f"[s{k}]")

    for c in sorted((c for c in clips if c.has_audio), key=lambda c: c.start):
        for tau, length in c.pieces(piece_s):
            s, n = int(round(c.timeline(tau) * sr)), int(round(length * sr))
            if prev is c and 0 < s - cur <= snap:
                s = cur
            skip = max(0, cur - s)
            s, n = s + skip, n - skip
            if n <= 0:
                continue
            if s > cur:
                silence(s - cur)
            idx = len(inputs) // 4
            inputs += ["-ss", f"{tau:.6f}", "-i", c.path]
            k = len(labels)
            chains.append(f"[{idx}:a:0]aresample={sr},{fmt},asetpts=N/SR/TB,atrim=start_sample={skip},"
                          f"asetpts=N/SR/TB,apad,atrim=end_sample={n}[a{k}]")
            labels.append(f"[a{k}]")
            cur, prev = s + n, c
    if total > cur or not labels:
        silence(max(total - cur, sr))
    graph = ";".join(chains) + ";" + "".join(labels) + f"concat=n={len(labels)}:v=0:a=1[out]"
    run(["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex", graph, "-map", "[out]",
         "-c:a", "pcm_s24le", "-rf64", "auto", out])


def audio_chain(pcfg):
    chain = []
    if pcfg.get("highpass_hz"):
        chain.append(f"highpass=f={pcfg['highpass_hz']}:poles=2")
    d = pcfg.get("denoise", "afftdn")
    if d == "rnnoise":
        model = pcfg.get("rnnoise_model")
        if not model or not Path(model).exists():
            sys.exit(f"rnnoise_model nicht gefunden: {model}")
        chain.append(f"arnndn=m={filter_quote(Path(model).expanduser())}")
    elif d == "afftdn":
        chain.append(f"afftdn=nr={pcfg.get('denoise_db', 12)}:nf=-45:tn=1")
    if pcfg.get("normalize", True):
        m = 10 ** (float(pcfg.get("max_gain_db", 18)) / 20)
        # RMS-basiert: gleicht unterschiedlich laute Sprecher deutlich besser an als peak-basiert
        chain.append(f"dynaudnorm=f=250:g=15:p=0.95:m={m:.2f}:r={pcfg.get('target_rms', 0.12)}")
    return ",".join(chain)


def read_mono(path, start, dur, sr):
    raw = run(["ffmpeg", "-v", "error", "-ss", f"{max(0.0, start):.3f}", "-t", f"{dur:.3f}", "-i", path,
               "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"])
    return np.frombuffer(raw, dtype=np.float32).astype(np.float64)


def best_match(ref, sig, want_ratio=False, guard=100):
    """Normierte Kreuzkorrelation: Position von sig in ref + Konfidenz (z-Wert)."""
    sig = sig - sig.mean()
    L = len(sig)
    n = 1 << int(np.ceil(np.log2(len(ref) + L)))
    cc = np.fft.irfft(np.fft.rfft(ref, n) * np.conj(np.fft.rfft(sig, n)), n)[:len(ref) - L + 1]
    c1 = np.concatenate([[0.0], np.cumsum(ref)])
    c2 = np.concatenate([[0.0], np.cumsum(ref ** 2)])
    s1, s2 = c1[L:] - c1[:-L], c2[L:] - c2[:-L]
    var = np.maximum(s2 - s1 ** 2 / L, 1e-12)
    ncc = cc / (np.sqrt(var) * (np.linalg.norm(sig) + 1e-12))
    k = int(np.argmax(ncc))
    z = (ncc[k] - ncc.mean()) / (ncc.std() + 1e-12)
    if not want_ratio:
        return k, z
    # zweitbester Treffer außerhalb von ±guard um den besten
    masked = ncc.copy()
    masked[max(0, k - guard):k + guard + 1] = -np.inf
    second = masked.max() if np.isfinite(masked.max()) else 0.0
    return k, float(ncc[k]), float(second)


def envelope(x, sr, rate):
    b = sr // rate
    n = len(x) // b
    return np.log(np.sqrt((x[:n * b].reshape(n, b) ** 2).mean(axis=1)) + 1e-4)


def xcorr_full(a, b):
    """c[k] = sum_i a[i+k]*b[i] für k = -(len(b)-1) ... len(a)-1."""
    n = 1 << int(np.ceil(np.log2(len(a) + len(b))))
    r = np.fft.irfft(np.fft.rfft(a, n) * np.conj(np.fft.rfft(b, n)), n)
    return np.concatenate([r[n - (len(b) - 1):], r[:len(a)]])


def coarse_offset(clips, lav_clips, lav_wavs, total, scfg):
    """Gesamte Aufnahme einer Kamera gegen die gesamten Lavs (Lautstärke-Hüllkurve).
    Erlaubt Teil-Überlappung und beliebig große Uhrzeit-Fehler."""
    sr, rate = 2000, 50
    # Lav-Hüllkurve + Maske (wo überhaupt Lavs laufen)
    refs = [read_mono(w, 0, total + 60, sr) for w in lav_wavs]
    L = max(len(r) for r in refs)
    ref = envelope(sum(np.pad(r, (0, L - len(r))) for r in refs), sr, rate)
    ml = np.zeros(len(ref))
    for c in lav_clips:
        ml[max(0, int(c.start * rate)):max(0, int(c.end * rate))] = 1
    # Kamera-Hüllkurve auf eigener Zeitachse, Clips an ihren aktuellen Positionen
    cams = [c for c in clips if c.has_audio]
    if not cams:
        return None
    c0 = min(c.start for c in cams)
    cam = np.zeros(int((max(c.end for c in cams) - c0) * rate) + rate)
    mc = np.zeros(len(cam))
    for c in cams:
        e = envelope(read_mono(c.path, 0, c.duration, sr), sr, rate)
        i = int(round((c.start - c0) * rate))
        e = e[:len(cam) - i]
        cam[i:i + len(e)], mc[i:i + len(e)] = e, 1
    for x, m in ((ref, ml), (cam, mc)):
        x[m > 0] -= x[m > 0].mean()
        x[m == 0] = 0
    num = xcorr_full(ref, cam)
    e_ref = xcorr_full(ref ** 2, mc)
    e_cam = xcorr_full(ml, cam ** 2)
    ov = xcorr_full(ml, mc) / rate
    ncc = num / np.sqrt(np.maximum(e_ref * e_cam, 1e-9))
    # Signifikanz: Zufallstreffer bei kurzer Überlappung erreichen leicht hohe ncc-Werte,
    # deshalb mit der Wurzel der Überlappung gewichten (~ z-Wert)
    score = ncc * np.sqrt(np.maximum(ov, 0))
    score[ov < scfg.get("coarse_min_overlap_s", 120)] = -np.inf
    offs = (np.arange(len(score)) - (len(cam) - 1)) / rate - c0

    def pick(sc):
        j = int(np.argmax(sc))
        if not np.isfinite(sc[j]):
            return None
        g = rate * 5
        masked = sc.copy()
        masked[max(0, j - g):j + g + 1] = -np.inf
        second = float(masked.max()) if np.isfinite(masked.max()) else 0.0
        return {"off": float(offs[j]), "ratio": float(sc[j]) / max(second, 1e-3),
                "peak": float(ncc[j]), "ov": float(ov[j])}

    # Top-3-Kandidaten zur Kontrolle ausgeben
    sc = score.copy()
    for _ in range(3):
        i = int(np.argmax(sc))
        if not np.isfinite(sc[i]):
            break
        print(f"       Kandidat {offs[i]:+9.1f}s ({hms(offs[i], False)})  Score {sc[i]:6.1f}  "
              f"Treffer {ncc[i]:.2f}  Überlappung {hms(ov[i], False)}")
        sc[max(0, i - rate * 30):i + rate * 30 + 1] = -np.inf

    glob = pick(score)
    local = None
    W = float(scfg.get("coarse_local_s") or 0)
    if W > 0:
        sc = score.copy()
        sc[np.abs(offs) > W] = -np.inf
        local = pick(sc)
    return (glob, local) if glob else None


def refine_sync(cam_clips, lav_wavs, scfg, total, lav_clips=()):
    """Verschiebt Kameraclips so, dass ihr Ton mit den Lavs übereinstimmt."""
    S = float(scfg["search_s"])
    # 1) Grobsuche: ganze Kamera-Aufnahme gegen ganze Lav-Aufnahme
    if scfg.get("coarse", True):
        for src in VIDEO_SOURCES:
            group = [c for c in cam_clips if c.source == src]
            if not group:
                continue
            print(f"  Grobsuche {src} ...")
            res = coarse_offset(group, lav_clips, lav_wavs, total, scfg)
            if res is None:
                print(f"  -> {src}: zu wenig Überlappung mit den Lavs")
                continue
            glob, local = res
            min_ratio = scfg.get("coarse_min_ratio", 1.5)
            good = lambda r: r is not None and r["ratio"] >= min_ratio and r["peak"] >= 0.15
            print(f"     Global: Versatz {glob['off']:+.1f}s ({hms(glob['off'])}), Treffer {glob['peak']:.2f}, "
                  f"Abstand {glob['ratio']:.2f}x, Überlappung {hms(glob['ov'], False)}")
            chosen = glob if good(glob) else None
            if chosen is None and local is not None:
                W = float(scfg.get("coarse_local_s") or 0)
                print(f"     Global nicht eindeutig – suche nur ±{hms(W, False)} um die Kamera-Uhrzeit:")
                print(f"     Lokal:  Versatz {local['off']:+.1f}s ({hms(local['off'])}), Treffer {local['peak']:.2f}, "
                      f"Abstand {local['ratio']:.2f}x, Überlappung {hms(local['ov'], False)}")
                if good(local):
                    chosen = local
            if chosen is None:
                print(f"  -> {src}: kein eindeutiger Treffer – Zeiten bleiben wie erkannt "
                      f"(ggf. tags_are_utc oder offset_s in config.json prüfen)")
            elif abs(chosen["off"]) > S / 2:
                print(f"  -> {src}: alle Clips um {chosen['off']:+.1f}s verschoben")
                for c in group:
                    c.start += chosen["off"]
                    c.sync_note = f"Grob {chosen['off']:+.1f}s; "
            else:
                print(f"  -> {src}: Uhrzeit passt, Rest macht der Feinsync")
    # 2) Feinsync über den ganzen Clip inkl. Drift
    fine_sync(cam_clips, lav_wavs, scfg)


def read_ref(wavs, start, dur, sr):
    refs = [read_mono(w, start, dur, sr) for w in wavs]
    L = max(len(r) for r in refs)
    return sum(np.pad(r, (0, L - len(r))) for r in refs)


def measure(c, ref_wavs, scfg, sr=8000):
    """Versatz an mehreren Stellen im Clip messen -> Liste (tau, versatz)."""
    S, A, step = float(scfg["search_s"]), float(scfg["analyze_s"]), float(scfg["window_step_s"])
    if c.duration <= A + 1:
        taus = [0.0]
    else:
        taus = list(np.arange(0.0, c.duration - A, step)) + [c.duration - A]
    out = []
    for tau in taus:
        a_len = min(A, c.duration - tau)
        if a_len < 3:
            continue
        T = c.timeline(tau)
        if T + a_len < 1:
            continue
        ref_start = max(0.0, T - S)
        ref = read_ref(ref_wavs, ref_start, (T - ref_start) + a_len + S, sr)
        clip = read_mono(c.path, tau, a_len, sr)
        if len(clip) < 2 * sr or len(ref) <= len(clip):
            continue
        k, z = best_match(np.diff(ref), np.diff(clip))      # Pre-Emphasis gegen Brummen
        corr = (ref_start + k / sr) - T
        if z >= scfg["min_confidence"] and abs(corr) <= S:
            out.append((float(tau), float(corr)))
    return out


def fit_line(meas):
    """Robuste Gerade versatz = a + b*tau (Ausreißer > 40 ms werden verworfen)."""
    t = np.array([m[0] for m in meas])
    y = np.array([m[1] for m in meas])
    keep = np.abs(y - np.median(y)) < 0.5
    for _ in range(3):
        if keep.sum() < 2:
            break
        b, a = np.polyfit(t[keep], y[keep], 1)
        keep = np.abs(y - (a + b * t)) < 0.04
    if keep.sum() < 2:
        return None
    b, a = np.polyfit(t[keep], y[keep], 1)
    return a, b, int(keep.sum()), float(t[keep].max() - t[keep].min())


def fine_sync(clips, ref_wavs, scfg):
    min_span = float(scfg["drift_min_span_s"])
    results = {}
    for c in clips:
        if not c.has_audio:
            c.sync_note += "kein Ton"
            continue
        results[id(c)] = measure(c, ref_wavs, scfg)
    # 1) lange Clips: Versatz + Drift fitten
    drifts = {}
    for c in clips:
        meas = results.get(id(c)) or []
        fit = fit_line(meas) if len(meas) >= 2 else None
        if fit and fit[3] >= min_span:
            a, b, n, span = fit
            c.start += a
            c.drift = (1 + c.drift) * (1 + b) - 1
            c.sync_note += f"Fein {a:+.3f}s, Drift {c.drift * 1e6:+.0f} ppm ({n} Messpunkte)"
            drifts.setdefault(c.source, []).append(c.drift)
            results[id(c)] = "done"
    # 2) kurze Clips: Drift der Kamera übernehmen, nur Versatz messen
    offsets = {}
    for c in clips:
        meas = results.get(id(c))
        if meas == "done" or meas is None:
            continue
        d = float(np.median(drifts[c.source])) if drifts.get(c.source) else 0.0
        if meas:
            # Start so wählen, dass die Messpunkte mit der Kamera-Drift passen
            corr = float(np.median([m[1] - (d - c.drift) * m[0] for m in meas]))
            c.start += corr
            c.drift = d
            c.sync_note += f"Fein {corr:+.3f}s ({len(meas)} Messpunkt(e))"
            offsets.setdefault(c.source, []).append(corr)
        else:
            c.drift = d
            c.sync_note += "Fein: keine sichere Messung"
    # 3) ganz ohne Messung: Median der Kamera
    for c in clips:
        if c.sync_note.endswith("keine sichere Messung") and offsets.get(c.source):
            med = float(np.median(offsets[c.source]))
            c.start += med
            c.sync_note += f" -> Kamera-Median {med:+.3f}s"


# --------------------------------------------------------------------------- video
HW_CANDIDATES = ["hevc_videotoolbox", "hevc_nvenc", "hevc_qsv", "hevc_amf"]


def encoder_works(name):
    """Kurzer Probelauf – ein Encoder kann gelistet sein, ohne dass die Hardware da ist."""
    r = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=640x360:r=25:d=0.4",
                        "-c:v", name, "-b:v", "1M", "-pix_fmt", "yuv420p", "-f", "null", "-"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return r.returncode == 0


def resolve_encoder(o):
    v = o["vcodec"]
    if v != "auto":
        return v
    enc = run(["ffmpeg", "-hide_banner", "-encoders"]).decode(errors="replace")
    for name in HW_CANDIDATES:
        if f" {name} " in enc and encoder_works(name):
            return name
    return "x265"


def vcodec_args(o):
    v = o["vcodec"]
    if v == "x265":   # klein bei guter Qualität (Standard)
        return ["-c:v", "libx265", "-crf", str(o.get("crf") or 28), "-preset", o["preset"],
                "-pix_fmt", "yuv420p", "-tag:v", "hvc1", "-x265-params", "log-level=error"]
    if v == "hevc_videotoolbox":   # Mac-Hardware: viel schneller, etwas größer
        return ["-c:v", "hevc_videotoolbox", "-b:v", o.get("bitrate", "3M"), "-pix_fmt", "yuv420p", "-tag:v", "hvc1"]
    if v == "prores":
        return ["-c:v", "prores_ks", "-profile:v", "0", "-pix_fmt", "yuv422p10le", "-vendor", "apl0"]
    if v.endswith(("_nvenc", "_qsv", "_amf")):   # Windows/Linux-Hardware: NVIDIA, Intel, AMD
        args = ["-c:v", v, "-b:v", o.get("bitrate", "3M"), "-pix_fmt", "yuv420p"]
        return args + (["-tag:v", "hvc1"] if v.startswith("hevc") else [])
    if v == "h264_videotoolbox":
        return ["-c:v", "h264_videotoolbox", "-b:v", o.get("bitrate", "6M"), "-pix_fmt", "yuv420p"]
    return ["-c:v", "libx264", "-crf", str(o.get("crf") or 23), "-preset", o["preset"], "-pix_fmt", "yuv420p"]


class SegmentRenderer:
    def __init__(self, cfg, tmp, use_text, markers=()):
        self.o, self.cfg, self.tmp, self.use_text = cfg["output"], cfg, tmp, use_text
        self.markers = list(markers)
        self.font = cfg["text"]["fontfile"] or next((f for f in FONT_CANDIDATES if Path(f).exists()), None)
        self.n_txt = 0
        self.lock = threading.Lock()
        hw = cfg["output"].get("hwaccel", "auto")
        self.hw = [] if not hw or hw == "none" else ["-hwaccel", hw]

    def text(self, content, size, x, y, expand=False):
        """drawtext über Textdatei (keine Escaping-Probleme). expand=True für %{pts...}."""
        if not self.use_text:
            return ""
        with self.lock:
            self.n_txt += 1
            tf = self.tmp / f"txt_{self.n_txt}.txt"
        tf.write_text(content, encoding="utf-8")
        opts = [f"textfile={filter_quote(tf)}", f"expansion={'normal' if expand else 'none'}",
                "fontcolor=white", f"fontsize={size}", "box=1", "boxcolor=black@0.6",
                f"boxborderw={max(4, size // 3)}", f"x={x}", f"y={y}"]
        if self.font:
            opts.insert(0, f"fontfile={filter_quote(self.font)}")
        return ",drawtext=" + ":".join(opts)

    @staticmethod
    def tc(offset):
        # laufende Zeit ab offset Sekunden, Format HH:MM:SS.mmm
        return "%{pts:hms:" + f"{max(0.0, offset):.4f}" + "}"

    def signature(self, a, b, main, pip, lavs):
        """Fingerabdruck eines Segments: alles, was das Bild bestimmt (nicht der Decoder)."""
        o, cfg, fps = self.o, self.cfg, self.o["fps"]
        t = a / fps
        if main is None:
            m = next((m for m in self.markers if m.pos - 0.1 <= t < m.pos + m.keep), None)
            black = (f"{m.real_start:%H:%M:%S}-{m.real_end:%H:%M:%S}" if m else "leer")
        else:
            black = None
        clip = lambda c: None if c is None else [str(c.path), round(c.local(t), 3)]
        data = [4, a, b, clip(main), clip(pip), [clip(l) for l in lavs], black,
                o["width"], o["height"], fps, vcodec_args(o), cfg["text"], cfg["pip"],
                cfg["track_names"], self.use_text, self.font]
        return hashlib.sha1(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()[:16]

    def render(self, out, a, b, main, pip, lavs):
        o, cfg = self.o, self.cfg
        W, H, fps = o["width"], o["height"], o["fps"]
        k = H / 1080                       # Textgrößen mit der Auflösung skalieren
        fs = max(12, int(cfg["text"]["size"] * k))
        small = max(10, int(fs * 0.8))
        mg = max(10, int(40 * k))
        n, t = b - a, a / fps
        cmd = ["ffmpeg", "-y", "-v", "error"]
        if main is None:
            cmd += ["-f", "lavfi", "-i", f"color=c=black:s={W}x{H}:r={fps}"]
            graph = "[0:v]null"
            m = next((m for m in self.markers if m.pos - 0.1 <= t < m.pos + m.keep), None)
            if m:
                dur = (m.real_end - m.real_start).total_seconds()
                texts = self.text(f"Pause herausgeschnitten\n{m.real_start:%H:%M:%S} - {m.real_end:%H:%M:%S}"
                                  f"  ({hms(dur, False)})", fs, "(w-tw)/2", "(h-th)/2")
            else:
                texts = self.text("kein Videomaterial", fs, "(w-tw)/2", "(h-th)/2")
        else:
            fit = (f"fps={fps},scale={W}:{H}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
                   f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black,setsar=1,tpad=stop_mode=clone:stop_duration=2")
            cmd += [*self.hw, "-ss", f"{max(0.0, main.local(t)):.4f}", "-i", main.path]
            graph = f"[0:v]{fit}"
            # Kamera-Dateiname + Position im Clip unten links
            texts = (self.text(main.path.name, fs, mg, f"h-th-{mg}-{int(fs * 1.6)}")
                     + self.text(self.tc(main.local(t)), fs, mg, f"h-th-{mg}", expand=True))
            if pip is not None:
                pw = int(W * cfg["pip"]["width_fraction"]) // 2 * 2
                pm = int(cfg["pip"]["margin"] * k)
                ps = max(10, int(fs * 0.6))
                cmd += [*self.hw, "-ss", f"{max(0.0, pip.local(t)):.4f}", "-i", pip.path]
                graph += (f"[m];[1:v]fps={fps},scale={pw}:-2,setsar=1,tpad=stop_mode=clone:stop_duration=2"
                          + self.text(pip.path.name, ps, 8, 8)
                          + self.text(self.tc(pip.local(t)), ps, 8, int(8 + ps * 1.6), expand=True)
                          + f"[p];[m][p]overlay=W-w-{pm}:H-h-{pm}")
        # laufender Timecode des Gesamtvideos oben mittig
        texts += self.text(self.tc(t), int(fs * 1.3), "(w-tw)/2", mg, expand=True)
        # Lav 1 oben links, Lav 2 oben rechts: Dateiname + Position in der Datei
        for lav, src, x in zip(lavs, LAV_SOURCES, (str(mg), f"w-tw-{mg}")):
            label = cfg["track_names"][src]
            if lav is None:
                texts += self.text(f"{label}: -", small, x, mg)
            else:
                texts += self.text(f"{label}: {lav.path.name}", small, x, mg)
                texts += self.text(self.tc(lav.local(t)), small, x, int(mg + small * 1.6), expand=True)
        graph += texts + "[v]"
        cmd += ["-filter_complex", graph, "-map", "[v]", "-frames:v", str(n), "-an",
                *vcodec_args(o), "-video_track_timescale", "90000", out]
        run(cmd)


def build_segments(sony, gopro, lav1, lav2, total_f, fps, piece_s=120.0):
    def frames(c):
        return max(0, round(c.start * fps)), min(total_f, round(c.end * fps))

    def active(lst, f):
        for c in lst:
            f0, f1 = frames(c)
            if f0 <= f < f1:
                return c
        return None

    pts, forced = {0, total_f}, set()
    for c in sony + gopro + lav1 + lav2:
        pts.update(frames(c))
    for c in sony + gopro:   # Drift: Video alle piece_s neu ansetzen
        for tau, _ in c.pieces(piece_s)[1:]:
            forced.add(round(c.timeline(tau) * fps))
    pts |= forced
    pts = sorted(p for p in pts if 0 <= p <= total_f)
    segs = []
    for a, b in zip(pts, pts[1:]):
        if b <= a:
            continue
        so, go = active(sony, a), active(gopro, a)
        main, pip = (so, go) if so else (go, None)
        lavs = (active(lav1, a), active(lav2, a))
        if segs and a not in forced and segs[-1][2] is main and segs[-1][3] is pip and segs[-1][4] == lavs:
            segs[-1][1] = b
        else:
            segs.append([a, b, main, pip, lavs])
    return segs


# --------------------------------------------------------------------------- Lücken schneiden
@dataclass
class GapMarker:
    pos: float          # Position in der geschnittenen Timeline
    keep: float         # davon stehen gelassen (Schwarzbild mit Hinweis)
    real_start: datetime
    real_end: datetime


def cut_gaps(every, t0, gcfg):
    """Entfernt Zeiten, in denen weder Bild noch Ton aufgenommen wurde.
    Gibt Marker für die verbleibenden kurzen Pausen zurück."""
    for c in every:
        c.real_dt = t0 + timedelta(seconds=c.start)
    if not gcfg.get("enabled", True):
        return []
    min_gap, keep = float(gcfg["min_gap_s"]), float(gcfg["keep_s"])
    spans = sorted((c.start, c.end) for c in every)
    gaps, cur_end = [], spans[0][1]
    for a, b in spans[1:]:
        if a - cur_end > min_gap:
            gaps.append((cur_end, a))
        cur_end = max(cur_end, b)
    markers, removed = [], 0.0
    cuts = []   # (Lückenende in alter Zeit, kumulativ entfernt)
    for a, b in gaps:
        pos = a - removed
        removed += (b - a) - keep
        cuts.append((b, removed))
        markers.append(GapMarker(pos, keep, t0 + timedelta(seconds=a), t0 + timedelta(seconds=b)))
    for c in every:
        shift = 0.0
        for b, rem in cuts:
            if c.start >= b - 1e-6:
                shift = rem
        c.start -= shift
    return markers


# --------------------------------------------------------------------------- Fortsetzen
ENCODER_NAMES = {"libx265": "x265", "libx264": "libx264", "prores_ks": "prores"}
SOFTWARE_ENCODERS = {"x265", "libx264", "prores"}


def probe_segment(path):
    """Fertiges Segment prüfen (Bildanzahl, Format). None = fehlt oder unvollständig."""
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-count_packets", "-select_streams", "v:0",
                            "-show_entries", "stream=codec_name,width,height,r_frame_rate,nb_read_packets"
                            ":stream_tags=encoder", "-of", "json", str(path)],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=60)
        st = json.loads(r.stdout)["streams"][0]
        num, den = st["r_frame_rate"].split("/")
        enc = st.get("tags", {}).get("encoder", "").split()
        enc = enc[-1] if enc else ""
        return {"frames": int(st["nb_read_packets"]), "w": int(st["width"]), "h": int(st["height"]),
                "fps": float(num) / float(den or 1), "encoder": ENCODER_NAMES.get(enc, enc)}
    except Exception:
        return None


def load_manifest(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_manifest(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def find_legacy(out_dir, work, segs, o):
    """Alte Arbeitsordner (Zufallsname, ohne Fingerabdrücke) nach wiederverwendbaren Segmenten durchsuchen.
    Übernommen wird nur ein lückenloser Anfang, dessen Segmentlängen exakt zum neuen Plan passen."""
    best = None
    for d in out_dir.glob("imerge_*"):
        if not d.is_dir() or d == work or not re.fullmatch(r"imerge_[a-z0-9_]{8}", d.name):
            continue
        files = sorted(d.glob("seg_[0-9][0-9][0-9][0-9][0-9].mov"))
        if not files:
            continue
        newest = max(f.stat().st_mtime for f in d.iterdir())
        if time.time() - newest < 180:
            print(f"  ! {d.name} wird gerade noch beschrieben – übersprungen")
            continue
        found, enc, why = [], None, ""
        for i, (a, b, *_) in enumerate(segs):
            f = d / f"seg_{i + 1:05d}.mov"
            info = probe_segment(f) if f.exists() else None
            if not f.exists():
                why = f"Segment {i + 1} fehlt"
            elif not info:
                why = f"Segment {i + 1} unvollständig"
            elif info["frames"] != b - a:
                why = f"Segment {i + 1} hat {info['frames']} statt {b - a} Bilder (anderer Plan/Sync)"
            elif info["w"] != o["width"] or info["h"] != o["height"] or abs(info["fps"] - o["fps"]) > 0.01:
                why = f"andere Auflösung/Bildrate ({info['w']}x{info['h']})"
            elif enc and info["encoder"] != enc:
                why = f"Segment {i + 1} mit anderem Encoder"
            if why:
                break
            enc = info["encoder"]
            found.append(f)
        print(f"  Alter Zwischenstand {d.name}: {len(files)} Segmentdateien, {len(found)} passend"
              + (f" – {why}" if why and len(found) < len(files) else ""))
        if found and (best is None or len(found) > len(best[1])):
            best = (d, found, enc)
    return best


ANALYSE_VERSION = 1


def analyse_key(every, cfg, args):
    """Fingerabdruck von allem, was Analyse/Sync beeinflusst: Quelldateien + relevante Einstellungen."""
    files = []
    for c in sorted(every, key=lambda c: (c.source, str(c.path))):
        st = c.path.stat()
        files.append([c.source, str(c.path), st.st_size, int(st.st_mtime)])
    rel = {k: cfg.get(k) for k in ("timezone", "sources", "join_tolerance_s", "audio_sync", "cut_gaps")}
    data = [ANALYSE_VERSION, files, rel, cfg["output"]["audio_rate"], bool(args.no_sync), bool(args.no_cut)]
    return hashlib.sha1(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()


def save_state(path, state):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, default=str), encoding="utf-8")
    os.replace(tmp, path)


# --------------------------------------------------------------------------- report
def report(clips, t0, total, markers=()):
    lines = [f"Timeline-Start: {t0:%Y-%m-%d %H:%M:%S}   Länge: {hms(total)}", ""]
    if markers:
        lines.append(f"== Herausgeschnittene Pausen ({len(markers)})")
        for m in markers:
            dur = (m.real_end - m.real_start).total_seconds()
            lines.append(f"  @ {hms(m.pos)}  {m.real_start:%H:%M:%S} – {m.real_end:%H:%M:%S}  ({hms(dur, False)} entfernt)")
        lines.append("")
    for s in SOURCES:
        lines.append(f"== {s} ({len(clips[s])} Dateien)")
        for c in clips[s]:
            real = c.real_dt or (t0 + timedelta(seconds=c.start))
            lines.append(f"  {c.path.name:<28} {real:%H:%M:%S}  @ {hms(c.start)}  Dauer {hms(c.duration)}"
                         f"  [{c.method}]" + (f"  {c.sync_note}" if c.sync_note else ""))
        lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Lavs + Sony + GoPro -> ein Interview-Video")
    ap.add_argument("project", type=Path)
    ap.add_argument("--dry-run", action="store_true", help="nur Zeiten erkennen und Timeline ausgeben")
    ap.add_argument("--no-sync", action="store_true", help="keine Audio-Feinsynchronisierung")
    ap.add_argument("--config", type=Path, help="Pfad zu config.json (Standard: projekt/config.json)")
    ap.add_argument("--keep-temp", action="store_true")
    ap.add_argument("-n", "--name",
                    help="Dateiname des fertigen Videos, z.B. interview_kim (.mov wird ergänzt)")
    ap.add_argument("-o", "--output", type=Path,
                    help="Ausgabeordner für Video, Report und Zwischendateien (Standard: Projektordner)")
    ap.add_argument("--no-cut", action="store_true", help="Pausen ohne Bild und Ton NICHT herausschneiden")
    ap.add_argument("--raw-audio", action="store_true", help="Ton NICHT aufbereiten (Originalpegel)")
    ap.add_argument("--fresh", action="store_true",
                    help="Zwischenstand eines abgebrochenen Laufs ignorieren und komplett neu rendern")
    args = ap.parse_args()

    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            sys.exit(f"{tool} nicht gefunden – bitte installieren (Mac: brew install ffmpeg).")
    root = args.project.resolve()
    if np is None and not args.no_sync and not args.dry_run:
        sys.exit("numpy fehlt – ohne numpy kein Audio-Sync.\n"
                 "Installieren (siehe Anleitung) oder bewusst ohne Sync starten: --no-sync")
    cfg = DEFAULT_CONFIG
    cfg_path = args.config or root / "config.json"
    if cfg_path.exists():
        cfg = deep_merge(cfg, json.loads(cfg_path.read_text(encoding="utf-8-sig")))
        print(f"Config: {cfg_path}")

    global TARGET_TZ
    if cfg.get("timezone"):
        if ZoneInfo is None:
            sys.exit("timezone braucht Python 3.9+")
        TARGET_TZ = ZoneInfo(cfg["timezone"])
        print(f"Geräte-Zeitzone: {cfg['timezone']}")

    print("Lese Dateien ...")
    clips = load_clips(root, cfg)
    chain_and_join(clips, cfg["join_tolerance_s"])
    every = [c for s in SOURCES for c in clips[s]]
    if not every:
        sys.exit("Keine Mediendateien gefunden.")
    t0 = min(c.start_dt for c in every)
    for c in every:
        c.start = (c.start_dt - t0).total_seconds()
    total = max(c.end for c in every)

    if args.dry_run:
        markers = [] if args.no_cut else cut_gaps(every, t0, cfg["cut_gaps"])
        total = max(c.end for c in every)
        print("\n" + report(clips, t0, total, markers))
        return

    sr = cfg["output"]["audio_rate"]
    fps = cfg["output"]["fps"]
    out_dir = args.output or (Path(cfg["output"]["dir"]).expanduser() if cfg["output"].get("dir") else root)
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_name = cfg["output"]["file"]
    report_name = "interview_merge_report.txt"
    if args.name:
        out_name = Path(args.name).name
        if Path(out_name).suffix.lower() not in (".mov", ".mp4", ".mkv"):
            out_name += ".mov"
        report_name = Path(out_name).stem + "_report.txt"
    print(f"Ausgabe: {out_dir / out_name}")
    # fester Arbeitsordner pro Ausgabedatei -> nach Abbruch kann derselbe Befehl fortsetzen
    tmp = out_dir / ("imerge_" + re.sub(r"[^\w.-]+", "_", Path(out_name).stem))
    tmp.mkdir(exist_ok=True)
    lock = tmp / "läuft.lock"
    try:
        age = time.time() - lock.stat().st_mtime
    except FileNotFoundError:
        age = None
    if age is not None and age < 90:
        sys.exit(f"Für {out_name} läuft bereits ein anderer Durchgang (Sperrdatei {lock}).\n"
                 f"Warte, bis er fertig ist. Falls sicher keiner mehr läuft: Datei löschen und neu starten.")
    lock.write_text(f"pid {os.getpid()}\n", encoding="utf-8")
    stop_beat = threading.Event()

    def heartbeat():                         # Sperre aktuell halten, solange dieser Lauf lebt
        while not stop_beat.wait(20):
            try:
                os.utime(lock)
            except OSError:
                pass
    threading.Thread(target=heartbeat, daemon=True).start()
    _prev = [k for k in load_manifest(tmp / "segments.json") if not k.startswith("__")]
    _ana = (tmp / "analyse.json").exists()
    if (_prev or _ana) and not args.fresh:
        print(f"Zwischenstand gefunden in {tmp.name}: "
              + ("Analyse/Sync" if _ana else "")
              + (", " if _ana and _prev else "")
              + (f"{len(_prev)} fertige Video-Segmente" if _prev else "")
              + " – wird übernommen, soweit Quellen und Einstellungen gleich sind")
    elif not args.fresh and any(re.fullmatch(r"imerge_[a-z0-9_]{8}", d.name) for d in out_dir.glob("imerge_*")):
        print("Alter Zwischenordner der Vorversion gefunden – wird nach Analyse und Sync geprüft")
    finished = False
    legacy_used = []
    wavs = {s: tmp / f"{s}.wav" for s in SOURCES}
    try:
        sfile = tmp / "analyse.json"
        key = analyse_key(every, cfg, args)
        state = None if args.fresh else load_manifest(sfile)
        if state and state.get("key") != key:
            print("  Quelldateien oder Einstellungen haben sich geändert – Analyse läuft neu")
            state = None
        cid = lambda c: f"{c.source}|{c.path}"

        def wav_ok(name, path):
            return state is not None and path.exists() and state.get("wavs", {}).get(name) == path.stat().st_size

        def wav_done(name, path):
            state.setdefault("wavs", {})[name] = path.stat().st_size
            save_state(sfile, state)

        if state:
            print("Analyse und Sync aus dem vorherigen Lauf übernommen (Quellen und Einstellungen unverändert)")
            for c in every:
                d = state["clips"][cid(c)]
                c.start, c.drift, c.sync_note = d["start"], d["drift"], d["note"]
                c.real_dt = datetime.fromisoformat(d["real"]) if d.get("real") else None
            t0 = datetime.fromisoformat(state["t0"])
            total = state["total"]
            markers = [GapMarker(m["pos"], m["keep"], datetime.fromisoformat(m["rs"]),
                                 datetime.fromisoformat(m["re"])) for m in state["markers"]]
            for s in LAV_SOURCES:
                if not wav_ok(s, wavs[s]):
                    print(f"Baue {cfg['track_names'][s]} neu ...")
                    build_audio_track(clips[s], total, wavs[s], cfg["sources"][s]["channels"], sr)
                    wav_done(s, wavs[s])
        else:
            do_sync = cfg["audio_sync"]["enabled"] and not args.no_sync and np is not None
            l1, l2 = LAV_SOURCES
            print("Baue Lav-Spuren ...")
            build_audio_track(clips[l1], total, wavs[l1], cfg["sources"][l1]["channels"], sr)
            if do_sync and cfg["audio_sync"]["lav_to_lav"] and clips[l1] and clips[l2]:
                print("Synchronisiere Lav 2 auf Lav 1 ...")
                fine_sync(clips[l2], [wavs[l1]], cfg["audio_sync"])
            build_audio_track(clips[l2], total + 5, wavs[l2], cfg["sources"][l2]["channels"], sr)

            cams = clips["sony"] + clips["gopro"]
            if cfg["audio_sync"]["enabled"] and not args.no_sync and cams:
                if np is None:
                    print("  ! numpy fehlt – Audio-Sync übersprungen (pip install numpy)")
                elif not any(clips[s] for s in LAV_SOURCES):
                    print("  ! keine Lavs – Audio-Sync übersprungen")
                else:
                    print("Synchronisiere Kameraclips über den Ton ...")
                    refine_sync(cams, [wavs[s] for s in LAV_SOURCES if clips[s]], cfg["audio_sync"], total,
                                [c for s in LAV_SOURCES for c in clips[s]])
            shift = -min(c.start for c in every)
            for c in every:
                c.start += shift
            t0 -= timedelta(seconds=shift)
            markers = [] if args.no_cut else cut_gaps(every, t0, cfg["cut_gaps"])
            total = max(c.end for c in every)
            if abs(shift) > 1e-3 or markers:
                if markers:
                    print(f"{len(markers)} Pause(n) ohne Bild und Ton herausgeschnitten.")
                print("Baue Lav-Spuren an den neuen Positionen ...")
                for s in LAV_SOURCES:
                    build_audio_track(clips[s], total, wavs[s], cfg["sources"][s]["channels"], sr)
            state = {"key": key, "t0": t0.isoformat(), "total": total,
                     "clips": {cid(c): {"start": c.start, "drift": c.drift, "note": c.sync_note,
                                        "real": c.real_dt.isoformat() if c.real_dt else None} for c in every},
                     "markers": [{"pos": m.pos, "keep": m.keep, "rs": m.real_start.isoformat(),
                                  "re": m.real_end.isoformat()} for m in markers], "wavs": {}}
            for s in LAV_SOURCES:
                state["wavs"][s] = wavs[s].stat().st_size if wavs[s].exists() else None
            save_state(sfile, state)

        rep = report(clips, t0, total, markers)
        print("\n" + rep)
        (out_dir / report_name).write_text(rep, encoding="utf-8")

        if all(wav_ok(s, wavs[s]) for s in VIDEO_SOURCES):
            print("Kamera-Tonspuren aus dem vorherigen Lauf übernommen")
        else:
            print("Baue Kamera-Tonspuren ...")
            for s in VIDEO_SOURCES:
                if not wav_ok(s, wavs[s]):
                    build_audio_track(clips[s], total, wavs[s], cfg["sources"][s]["channels"], sr)
                    wav_done(s, wavs[s])

        filters = run(["ffmpeg", "-hide_banner", "-filters"]).decode(errors="replace")
        use_text = " drawtext " in filters
        if not use_text:
            print("  ! ffmpeg ohne drawtext – Dateinamen werden NICHT eingeblendet")
        total_f = int(round(total * fps))
        segs = build_segments(clips["sony"], clips["gopro"], clips["lav_interviewer"],
                              clips["lav_befragter"], total_f, fps)
        mfile = tmp / "segments.json"
        manifest = {} if args.fresh else load_manifest(mfile)
        print(f"Arbeitsordner: {tmp}")
        want_auto = cfg["output"]["vcodec"] == "auto"
        enc = resolve_encoder(cfg["output"])
        prev_enc = manifest.get("__encoder__")
        if want_auto and prev_enc and prev_enc != enc and (prev_enc in SOFTWARE_ENCODERS or encoder_works(prev_enc)):
            print(f"  Vorheriger Lauf nutzte {prev_enc} – bleibe dabei, damit der Zwischenstand passt")
            enc = prev_enc
        total_frames = sum(b - a for a, b, *_ in segs)

        # alte Arbeitsordner der Vorversion (Zufallsname) nach brauchbaren Segmenten durchsuchen
        legacy = None if args.fresh else find_legacy(out_dir, tmp, segs, cfg["output"])
        if legacy:
            ldir, lfiles, lenc = legacy
            lframes = sum(segs[i][1] - segs[i][0] for i in range(len(lfiles)))
            frac = lframes / max(total_frames, 1)
            if lenc != enc and not (lenc in SOFTWARE_ENCODERS and enc not in SOFTWARE_ENCODERS and frac < 0.75):
                print(f"  Alter Lauf mit {lenc}: übernehme dessen Encoder, damit die Segmente zusammenpassen")
                enc = lenc
            if lenc == enc:
                print(f"Vorheriger Lauf gefunden ({ldir.name}): {len(lfiles)} Segmente "
                      f"({frac * 100:.0f}%) werden übernommen")
            else:
                print(f"Vorheriger Lauf ({ldir.name}, {lenc}) nur zu {frac * 100:.0f}% fertig – "
                      f"mit {enc} komplett neu zu rendern ist schneller")
                legacy = None
        cfg["output"]["vcodec"] = enc

        jobs = cfg["output"].get("jobs", "auto")
        if jobs == "auto":
            jobs = 1 if enc in SOFTWARE_ENCODERS else 3
        jobs = max(1, int(jobs))
        hw = cfg["output"].get("hwaccel", "auto")
        print(f"Video-Encoder: {enc}  |  Decoding: {'Software' if not hw or hw == 'none' else 'Hardware (' + hw + ')'}"
              f"  |  parallel: {jobs}")

        r = SegmentRenderer(cfg, tmp, use_text, markers)
        listfile = tmp / "segments.txt"
        sigs = [r.signature(*sg) for sg in segs]
        seg_files = [tmp / f"seg_{sg}.mov" for sg in sigs]

        if legacy:
            ldir, lfiles, _ = legacy
            for i, f in enumerate(lfiles):
                os.replace(f, seg_files[i])
                manifest[sigs[i]] = seg_files[i].name
            save_manifest(mfile, manifest)
            legacy_used.append(ldir)

        manifest["__encoder__"] = enc
        # was ist schon fertig?
        saved = {k for k in manifest if not k.startswith("__")}
        todo, broken = [], 0
        for i, (sg, f) in enumerate(zip(sigs, seg_files)):
            ok = False
            if manifest.get(sg) == f.name and f.exists():
                info = probe_segment(f)
                ok = info is not None and info["frames"] == segs[i][1] - segs[i][0]
                broken += not ok
            if not ok:
                todo.append(i)
        if saved:
            match = len(saved & set(sigs))
            print(f"  Gespeicherter Zwischenstand: {len(saved)} Segmente, {match} passen zum aktuellen Plan"
                  + (f", {broken} davon beschädigt" if broken else ""))
            if match < len(saved):
                print("  (nicht passende stammen aus einem Lauf mit anderen Einstellungen, anderem Encoder "
                      "oder anderem Sync-Ergebnis)")
        elif not legacy:
            print("  Noch keine fertigen Video-Segmente – rendere alle")
        reused = len(segs) - len(todo)
        if reused:
            done_f = total_frames - sum(segs[i][1] - segs[i][0] for i in todo)
            print(f"Fortsetzen: {reused}/{len(segs)} Segmente schon fertig "
                  f"({done_f / max(total_frames, 1) * 100:.0f}%), rendere die restlichen {len(todo)}")

        todo_frames = sum(segs[i][1] - segs[i][0] for i in todo)
        done = {"frames": 0}
        t_start = time.time()
        plock = threading.Lock()

        def work(i):
            a, b, main, pip, lavs = segs[i]
            part = seg_files[i].with_name(seg_files[i].stem + ".part.mov")
            r.render(part, a, b, main, pip, lavs)
            os.replace(part, seg_files[i])
            with plock:
                manifest[sigs[i]] = seg_files[i].name
                save_manifest(mfile, manifest)
                done["frames"] += b - a
                el = time.time() - t_start
                frac = done["frames"] / max(todo_frames, 1)
                eta = el / frac - el if frac > 0 else 0
                overall = (total_frames - todo_frames + done["frames"]) / max(total_frames, 1)
                name = main.path.name if main else "schwarz"
                if pip:
                    name += f" + PiP {pip.path.name}"
                print(f"  [{overall * 100:5.1f}%] Segment {i + 1}/{len(segs)}  {hms(a / fps)} – {hms(b / fps)}  {name}"
                      f"   (Tempo {(done['frames'] / fps) / max(el, 1e-6):.1f}x, Rest ca. {hms(eta, False)})")

        if jobs == 1:
            for i in todo:
                work(i)
        else:
            ex = ThreadPoolExecutor(max_workers=jobs)
            futures = [ex.submit(work, i) for i in todo]
            try:
                for f in futures:
                    f.result()
            except BaseException:
                for f in futures:          # Strg+C oder Fehler: nichts Neues mehr anfangen
                    f.cancel()
                ex.shutdown(wait=False)
                raise
            ex.shutdown()
        if todo:
            print(f"  Video gerendert in {hms(time.time() - t_start, False)}")
        with listfile.open("w") as lf:
            for seg in seg_files:
                lf.write(f"file '{seg.as_posix().replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'\n")

        print("Füge zusammen ...")
        video = tmp / "video.mov"
        run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", listfile, "-c", "copy", video])
        out = out_dir / out_name
        final_wavs = dict(wavs)
        pcfg = cfg["audio_processing"]
        if pcfg.get("enabled") and not args.raw_audio:
            chain = audio_chain(pcfg)
            if chain:
                print("Bereite Ton für Transkription auf (Hochpass, Rauschunterdrückung, Lautstärke) ...")
                for s in SOURCES:
                    if s in pcfg.get("apply_to", SOURCES) and clips[s]:
                        print(f"  {cfg['track_names'][s]} ...")
                        final_wavs[s] = tmp / f"{s}_processed.wav"
                        pname = f"{s}_processed|{hashlib.sha1(chain.encode()).hexdigest()[:10]}"
                        if wav_ok(pname, final_wavs[s]):
                            print("    (aus dem vorherigen Lauf übernommen)")
                            continue
                        run(["ffmpeg", "-y", "-v", "error", "-i", wavs[s], "-af", chain,
                             "-c:a", "pcm_s24le", "-rf64", "auto", final_wavs[s]])
                        wav_done(pname, final_wavs[s])
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", video]
        for s in SOURCES:
            cmd += ["-i", final_wavs[s]]
        if cfg["output"]["audio_tracks"] == "single":
            # eine Spur mit 4 Kanälen – liest Resolve zuverlässig als Embedded Channel 1–4
            graph = ";".join(f"[{i + 1}:a]aformat=channel_layouts=mono[m{i}]" for i in range(len(SOURCES)))
            graph += ";" + "".join(f"[m{i}]" for i in range(len(SOURCES)))
            graph += f"amerge=inputs={len(SOURCES)}[a]"
            cmd += ["-filter_complex", graph, "-map", "0:v", "-map", "[a]", "-c:v", "copy"]
            if cfg["output"]["audio_codec"] == "pcm":
                cmd += ["-c:a", "pcm_s24le"]
            else:
                cmd += ["-c:a", "aac", "-b:a", cfg["output"]["aac_single"]]
            names = " | ".join(f"Ch{i + 1} {cfg['track_names'][s]}" for i, s in enumerate(SOURCES))
            cmd += ["-metadata:s:a:0", f"title={names}"]
        else:
            cmd += ["-map", "0:v"] + [x for i in range(len(SOURCES)) for x in ("-map", f"{i + 1}:a")]
            cmd += ["-c:v", "copy"]
            if cfg["output"]["audio_codec"] == "pcm":
                cmd += ["-c:a", "pcm_s24le"]
            else:
                cmd += ["-c:a", "aac"]
                for i, s in enumerate(SOURCES):
                    mono = cfg["sources"][s]["channels"] == "mono"
                    cmd += [f"-b:a:{i}", cfg["output"]["aac_mono"] if mono else cfg["output"]["aac_stereo"]]
            for i, s in enumerate(SOURCES):
                name = cfg["track_names"][s]
                cmd += [f"-metadata:s:a:{i}", f"title={name}", f"-metadata:s:a:{i}", f"handler_name={name}"]
        part_out = out.with_name(out.stem + ".part" + out.suffix)
        run(cmd + [part_out])
        os.replace(part_out, out)
        finished = True
        print(f"\nFertig: {out}\nReport: {out_dir / report_name}")
    finally:
        stop_beat.set()
        try:
            lock.unlink()
        except OSError:
            pass
        if args.keep_temp:
            print(f"Arbeitsordner behalten: {tmp}")
        elif finished:
            shutil.rmtree(tmp, ignore_errors=True)
            for d in legacy_used:
                shutil.rmtree(d, ignore_errors=True)
            rest = [d.name for d in out_dir.glob("imerge_*") if d.is_dir()]
            if rest:
                print(f"Hinweis: In {out_dir} liegen noch Zwischenordner anderer bzw. alter Läufe: "
                      f"{', '.join(rest)}. Wenn dort gerade nichts mehr läuft, kannst du sie löschen.")
        else:
            print(f"\nAbgebrochen. Der Zwischenstand bleibt in {tmp}.\n"
                  f"Zum Fortsetzen einfach denselben Befehl nochmal starten (mit --fresh komplett neu).")


if __name__ == "__main__":
    main()