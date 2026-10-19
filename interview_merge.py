#!/usr/bin/env python3
"""
interview_merge.py – synchronisiert beliebig viele Tonaufnahmen mit Kameraclips.

Drei Modi:

  merge          Alles in EIN durchgehendes Video: Hauptkamera vollformatig, optional eine
                 zweite Kamera als Bild-im-Bild, schwarz wo keine Kamera lief.
  sync-to-audio  Wie merge, aber mit genau einer Kamera (kein Bild-im-Bild).
  sync-to-video  Pro Kameraclip eine eigene Datei mit allen dazu synchronen Tonspuren.
                 Nur Zeiten mit Bild, kein Schwarzbild.

Beispiele:
  python3 interview_merge.py merge --audio lav1 --audio lav2 --video sony --pip gopro -o export -n Interview
  python3 interview_merge.py sync-to-audio --audio lav1 --audio lav2 --video sony -o export
  python3 interview_merge.py sync-to-video --audio Interviewer=lav1 --audio Gast=lav2 --video sony -o export

Optionen:
  --info                 Dateinamen und Timecodes einblenden
  --only_new_audio       Kameraton weglassen (wird intern trotzdem zum Synchronisieren genutzt)
  --full_quality_video   Originalauflösung/-bildrate; sync-to-video ohne --info kopiert das Bild unverändert
  --full_quality_audio   Ton unbearbeitet als PCM (keine Rauschunterdrückung/Lautstärkeanpassung)
  -o / -n / --fresh / --dry-run / --config

Voraussetzungen: ffmpeg/ffprobe (mit drawtext für --info), Python 3.9+, numpy (für Sync).
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
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

MODES = ("merge", "sync-to-audio", "sync-to-video")

DEFAULT_CONFIG = {
    # Zeitzone, in der die Geräte-Uhren laufen, z.B. "Europe/Berlin".
    # Zeitstempel mit Zonenangabe (Sony-XML, UTC-Tags) werden in diese Zone umgerechnet.
    "timezone": None,
    # Standardwerte pro Quellart; einzelne Ordner lassen sich unter "sources" überschreiben
    # offset_s: manuelle Korrektur in Sekunden; tags_are_utc: creation_time ist echte UTC
    "defaults": {
        "audio": {"offset_s": 0.0, "tags_are_utc": True},
        "video": {"offset_s": 0.0, "tags_are_utc": True, "use_timecode": False},
    },
    # Überschreibungen pro Ordner, Schlüssel = Anzeigename (Ordnername oder NAME= aus dem Aufruf)
    "sources": {},
    "output": {
        "width": 1920, "height": 1080, "fps": 25,
        # "auto" = schnellster funktionierender Hardware-Encoder, sonst x265
        "vcodec": "auto", "crf": None, "preset": "fast", "bitrate": "3M",
        "hwaccel": "auto",
        "jobs": "auto",
        "audio_rate": 48000,
        "aac_per_channel": "96k",
        # "single": eine Tonspur mit allen Kanälen (Resolve-sicher) | "separate": einzelne Spuren
        "audio_tracks": "single",
    },
    # Audio-Dateien, deren Start so nah am Ende der vorherigen liegt, werden lückenlos angehängt
    "join_tolerance_s": 3.0,
    # merge/sync-to-audio: Pausen ohne Bild und Ton herausschneiden
    "cut_gaps": {"enabled": True, "min_gap_s": 10.0, "keep_s": 2.0},
    # Tonaufbereitung für Transkription (aus bei --full_quality_audio)
    "audio_processing": {
        "enabled": True, "highpass_hz": 90, "denoise": "afftdn", "denoise_db": 12,
        "rnnoise_model": None, "normalize": True, "max_gain_db": 20, "target_rms": 0.12,
    },
    "pip": {"width_fraction": 0.3, "margin": 40},
    "text": {"fontfile": None, "size": 34},
    "audio_sync": {"enabled": True, "audio_to_audio": True,
                   "coarse": True, "coarse_min_ratio": 1.5, "coarse_min_overlap_s": 120,
                   "coarse_local_s": 300.0,
                   "medium": True, "medium_window_s": 300.0, "medium_excerpts": 6, "medium_excerpt_s": 60.0,
                   "search_s": 10.0, "analyze_s": 30.0, "window_step_s": 240.0,
                   "min_confidence": 8.0, "drift_min_span_s": 600.0},
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
ABORT = threading.Event()        # gesetzt bei Strg+C / Fehler: keine neuen Versuche mehr


class FFmpegError(SystemExit):
    def __init__(self, msg, returncode):
        super().__init__(msg)
        self.returncode = returncode


def run(cmd):
    r = subprocess.run([str(c) for c in cmd], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if r.returncode != 0:
        err = r.stderr.decode(errors="replace")[-3000:]
        raise FFmpegError(f"\nFehler bei:\n  {' '.join(map(str, cmd))[:1500]}\n\n{err}", r.returncode)
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
    """Pfad sicher in einen ffmpeg-Filter einsetzen (auch C:/..., Leerzeichen, Apostrophe)."""
    p = str(path).replace("\\", "/")
    p = p.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:")
    return "'" + p.replace("'", "'\\''") + "'"


def safe_name(s):
    return re.sub(r"[^\w.-]+", "_", s).strip("_") or "x"


def concat_line(p):
    return f"file '{Path(p).as_posix().replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'\n"


# --------------------------------------------------------------------------- Quellen + Clips
@dataclass
class Source:
    key: str        # eindeutiger Kurzname (Dateinamen, Cache)
    name: str       # Anzeigename (Tonspur, Einblendung, Report)
    kind: str       # "audio" | "video" | "pip"
    path: Path
    cfg: dict = field(default_factory=dict)

    @property
    def is_video(self):
        return self.kind in ("video", "pip")


@dataclass
class Clip:
    path: Path
    source: str
    start_dt: datetime
    method: str
    duration: float
    has_video: bool
    has_audio: bool
    width: int = 0
    height: int = 0
    fps_str: str = ""
    channels: int = 1
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


def parse_sources(args, cfg):
    """--audio/--video/--pip in Source-Objekte übersetzen. Erlaubt 'Name=Pfad'."""
    out, keys = [], set()

    def add(arg, kind):
        name = None
        if "=" in arg and not Path(arg).expanduser().exists():
            name, arg = arg.split("=", 1)
        p = Path(arg).expanduser()
        if not p.exists():
            sys.exit(f"Nicht gefunden: {p}")
        p = p.resolve()
        name = name or (p.name if p.is_dir() else p.stem)
        key = safe_name(name)
        while key in keys:
            key += "_2"
        keys.add(key)
        base = cfg["defaults"]["audio" if kind == "audio" else "video"]
        scfg = deep_merge(base, cfg["sources"].get(name, cfg["sources"].get(key, {})))
        out.append(Source(key, name, kind, p, scfg))

    for a in args.audio or []:
        add(a, "audio")
    if args.video:
        add(args.video, "video")
    if args.pip:
        add(args.pip, "pip")
    return out


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
                method = "Sony-XML"
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


def load_clips(sources):
    clips = {s.key: [] for s in sources}
    for s in sources:
        files = [s.path] if s.path.is_file() else sorted(s.path.iterdir())
        for p in files:
            if p.name.startswith(".") or p.suffix.lower() not in AUDIO_EXT | VIDEO_EXT:
                continue
            info = probe(p)
            streams = info.get("streams", [])
            vst = next((st for st in streams if st.get("codec_type") == "video"
                        and not st.get("disposition", {}).get("attached_pic")), None)
            ast = next((st for st in streams if st.get("codec_type") == "audio"), None)
            if s.is_video and not vst:
                print(f"  ! {p.name}: kein Video, übersprungen")
                continue
            if s.kind == "audio" and not ast:
                print(f"  ! {p.name}: kein Ton, übersprungen")
                continue
            dur = float(vst["duration"]) if s.is_video and vst.get("duration") else None
            dur = dur or float(info["format"].get("duration", 0))
            if dur <= 0:
                print(f"  ! {p.name}: Dauer unbekannt, übersprungen")
                continue
            dt, method = detect_start(p, info, s.cfg, dur)
            off = float(s.cfg.get("offset_s", 0))
            if off:
                dt += timedelta(seconds=off)
                method += f", Offset {off:+.2f}s"
            c = Clip(p, s.key, dt, method, dur, s.is_video, ast is not None)
            if vst:
                c.width, c.height = int(vst.get("width", 0)), int(vst.get("height", 0))
                c.fps_str = vst.get("r_frame_rate", "25/1")
            if ast:
                c.channels = int(ast.get("channels", 1))
            clips[s.key].append(c)
        clips[s.key].sort(key=lambda c: c.start_dt)
        if not clips[s.key]:
            print(f"  ! {s.name}: keine passenden Dateien in {s.path}")
    return clips


def chain_and_join(clips, sources, tol):
    for s in sources:
        lst = clips[s.key]
        if s.is_video:   # GoPro-Kapitel (GX01xxxx, GX02xxxx ...) direkt aneinander
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
            continue
        # automatisch geteilte Tonaufnahmen (z.B. DJI Mic 2 alle 30 min) lückenlos anhängen
        for prev, cur in zip(lst, lst[1:]):
            gap = (cur.start_dt - prev.end_dt).total_seconds()
            if abs(gap) <= tol:
                cur.start_dt = prev.end_dt
                cur.method += f" | angehängt (Lücke war {gap:+.2f}s)"


# --------------------------------------------------------------------------- audio
def build_audio_track(clips, total_s, out, channels, sr, piece_s=120.0):
    layout = "mono" if channels == 1 else "stereo"
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
        if not model or not Path(model).expanduser().exists():
            sys.exit(f"rnnoise_model nicht gefunden: {model}")
        chain.append(f"arnndn=m={filter_quote(Path(model).expanduser())}")
    elif d == "afftdn":
        chain.append(f"afftdn=nr={pcfg.get('denoise_db', 12)}:nf=-45:tn=1")
    if pcfg.get("normalize", True):
        m = 10 ** (float(pcfg.get("max_gain_db", 18)) / 20)
        chain.append(f"dynaudnorm=f=250:g=15:p=0.95:m={m:.2f}:r={pcfg.get('target_rms', 0.12)}")
    return ",".join(chain)


def read_mono(path, start, dur, sr):
    if dur <= 0.05:
        return np.zeros(0)
    raw = run(["ffmpeg", "-v", "error", "-ss", f"{max(0.0, start):.3f}", "-t", f"{dur:.3f}", "-i", path,
               "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"])
    return np.frombuffer(raw, dtype=np.float32).astype(np.float64)


def read_ref(wavs, start, dur, sr):
    refs = [read_mono(w, start, dur, sr) for w in wavs]
    L = max(len(r) for r in refs)
    return sum(np.pad(r, (0, L - len(r))) for r in refs)


def best_match(ref, sig):
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
    return k, z


def envelope(x, sr, rate):
    b = sr // rate
    n = len(x) // b
    return np.log(np.sqrt((x[:n * b].reshape(n, b) ** 2).mean(axis=1)) + 1e-4)


def xcorr_full(a, b):
    """c[k] = sum_i a[i+k]*b[i] für k = -(len(b)-1) ... len(a)-1."""
    n = 1 << int(np.ceil(np.log2(len(a) + len(b))))
    r = np.fft.irfft(np.fft.rfft(a, n) * np.conj(np.fft.rfft(b, n)), n)
    return np.concatenate([r[n - (len(b) - 1):], r[:len(a)]])


def coarse_offset(clips, ref_clips, ref_wavs, total, scfg):
    """Gesamte Aufnahme einer Quelle gegen die gesamte Referenz (Lautstärke-Hüllkurve).
    Erlaubt Teil-Überlappung und beliebig große Uhrzeit-Fehler."""
    sr, rate = 2000, 50
    refs = [read_mono(w, 0, total + 60, sr) for w in ref_wavs]
    L = max(len(r) for r in refs)
    ref = envelope(sum(np.pad(r, (0, L - len(r))) for r in refs), sr, rate)
    ml = np.zeros(len(ref))
    for c in ref_clips:
        ml[max(0, int(c.start * rate)):max(0, int(c.end * rate))] = 1
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
        if (m > 0).any():
            x[m > 0] -= x[m > 0].mean()
        x[m == 0] = 0
    num = xcorr_full(ref, cam)
    e_ref = xcorr_full(ref ** 2, mc)
    e_cam = xcorr_full(ml, cam ** 2)
    ov = xcorr_full(ml, mc) / rate
    ncc = num / np.sqrt(np.maximum(e_ref * e_cam, 1e-9))
    # Zufallstreffer bei kurzer Überlappung erreichen leicht hohe ncc-Werte -> mit sqrt(Überlappung) gewichten
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

    sc = score.copy()
    cands = []
    for _ in range(5):
        i = int(np.argmax(sc))
        if not np.isfinite(sc[i]):
            break
        cands.append(float(offs[i]))
        if len(cands) <= 3:
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
    return (glob, local, cands) if glob else None


def medium_offset(group, ref_wavs, scfg, total, centers):
    """Wellenform-Vergleich mehrerer Ausschnitte mit der Referenz (±medium_window_s und um Kandidaten).
    Ergebnis nur, wenn mehrere Ausschnitte unabhängig auf denselben Versatz kommen."""
    sr, L = 4000, float(scfg.get("medium_excerpt_s", 60))
    W = float(scfg.get("medium_window_s", 300))
    n_ex = int(scfg.get("medium_excerpts", 6))
    cams = sorted((c for c in group if c.has_audio and c.duration >= 20), key=lambda c: -c.duration)
    if not cams:
        return None
    tot = sum(c.duration for c in cams)
    excerpts = []
    for k in range(n_ex):
        pos = (k + 0.5) / n_ex * tot
        for c in cams:
            if pos < c.duration:
                l = min(L, c.duration)
                excerpts.append((c, max(0.0, min(pos, c.duration - l)), l))
                break
            pos -= c.duration
    windows = [(0.0, W)] + [(cd, 20.0) for cd in centers if abs(cd) > W - 20]
    results = []
    for c, tau, l in excerpts:
        sig = np.diff(read_mono(c.path, tau, l, sr))
        if len(sig) < sr * 10 or np.std(sig) < 1e-6:
            continue
        best = None
        for center, w in windows:
            T = c.timeline(tau) + center
            ref_start, ref_end = max(0.0, T - w), min(total, T + l + w)
            if ref_end - ref_start < l + 1:
                continue
            ref = np.diff(read_ref(ref_wavs, ref_start, ref_end - ref_start, sr))
            if len(ref) <= len(sig):
                continue
            k, z = best_match(ref, sig)
            off = (ref_start + k / sr) - c.timeline(tau)
            if best is None or z > best[1]:
                best = (off, z)
        if best:
            results.append(best)
    thr = scfg.get("min_confidence", 8.0)
    for off, z in results:
        print(f"       Ausschnitt: Versatz {off:+8.2f}s  (z={z:.1f}){'' if z >= thr else '  – unsicher'}")
    good = [r for r in results if r[1] >= thr]
    if len(good) < 2:
        return None
    best_cluster = max(([g for g in good if abs(g[0] - r[0]) <= 0.15] for r in good), key=len)
    if len(best_cluster) < max(2, (len(results) + 1) // 2):
        return None
    return float(np.median([g[0] for g in best_cluster])), len(best_cluster), len(results)


def refine_sync(groups, ref_wavs, ref_clips, scfg, total):
    """groups: [(Name, Clips)] – jede Gruppe wird als Ganzes grob, dann pro Clip fein synchronisiert."""
    S = float(scfg["search_s"])
    if scfg.get("coarse", True):
        for name, group in groups:
            if not group:
                continue
            print(f"  Grobsuche {name} ...")
            res = coarse_offset(group, ref_clips, ref_wavs, total, scfg)
            if res is None:
                print(f"  -> {name}: zu wenig Überlappung mit der Referenz")
                continue
            glob, local, cands = res
            min_ratio = scfg.get("coarse_min_ratio", 1.5)
            good = lambda r: r is not None and r["ratio"] >= min_ratio and r["peak"] >= 0.15
            print(f"     Global: Versatz {glob['off']:+.1f}s ({hms(glob['off'])}), Treffer {glob['peak']:.2f}, "
                  f"Abstand {glob['ratio']:.2f}x, Überlappung {hms(glob['ov'], False)}")
            chosen = glob if good(glob) else None
            if chosen is None and local is not None:
                W = float(scfg.get("coarse_local_s") or 0)
                print(f"     Global nicht eindeutig – suche nur ±{hms(W, False)} um die Geräte-Uhrzeit:")
                print(f"     Lokal:  Versatz {local['off']:+.1f}s ({hms(local['off'])}), Treffer {local['peak']:.2f}, "
                      f"Abstand {local['ratio']:.2f}x, Überlappung {hms(local['ov'], False)}")
                if good(local):
                    chosen = local
            if chosen is None and scfg.get("medium", True):
                print("     Grobsuche nicht eindeutig – vergleiche Ausschnitte als Wellenform ...")
                med = medium_offset(group, ref_wavs, scfg, total, cands)
                if med:
                    chosen = {"off": med[0]}
                    print(f"     Mittlere Suche: Versatz {med[0]:+.2f}s, {med[1]} von {med[2]} Ausschnitten stimmen überein")
            if chosen is None:
                print(f"  -> {name}: kein eindeutiger Treffer – Zeiten bleiben wie erkannt "
                      f"(ggf. tags_are_utc oder offset_s in config.json prüfen)")
            elif abs(chosen["off"]) > S / 2:
                print(f"  -> {name}: alle Dateien um {chosen['off']:+.1f}s verschoben")
                for c in group:
                    c.start += chosen["off"]
                    c.sync_note = f"Grob {chosen['off']:+.1f}s; "
            else:
                print(f"  -> {name}: Uhrzeit passt, Rest macht der Feinsync")
    fine_sync([c for _, g in groups for c in g], ref_wavs, scfg)


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
    drifts = {}
    for c in clips:                                 # lange Clips: Versatz + Drift
        meas = results.get(id(c)) or []
        fit = fit_line(meas) if len(meas) >= 2 else None
        if fit and fit[3] >= min_span:
            a, b, n, span = fit
            c.start += a
            c.drift = (1 + c.drift) * (1 + b) - 1
            c.sync_note += f"Fein {a:+.3f}s, Drift {c.drift * 1e6:+.0f} ppm ({n} Messpunkte)"
            drifts.setdefault(c.source, []).append(c.drift)
            results[id(c)] = "done"
    offsets = {}
    for c in clips:                                 # kurze Clips: Drift der Quelle übernehmen
        meas = results.get(id(c))
        if meas == "done" or meas is None:
            continue
        d = float(np.median(drifts[c.source])) if drifts.get(c.source) else 0.0
        vals = [m[1] - (d - c.drift) * m[0] for m in meas] if meas else []
        if len(vals) >= 2 and max(vals) - min(vals) > 0.2:
            c.drift = d
            c.sync_note += (f"Fein: Messungen widersprüchlich ({min(vals):+.2f}…{max(vals):+.2f}s) – "
                            f"keine sichere Messung")
            continue
        if meas:
            corr = float(np.median(vals))
            c.start += corr
            c.drift = d
            c.sync_note += f"Fein {corr:+.3f}s ({len(meas)} Messpunkt(e))"
            offsets.setdefault(c.source, []).append(corr)
        else:
            c.drift = d
            c.sync_note += "Fein: keine sichere Messung"
    for c in clips:                                 # ganz ohne Messung: Median der Quelle
        if "keine sichere Messung" in c.sync_note and offsets.get(c.source):
            med = float(np.median(offsets[c.source]))
            c.start += med
            c.sync_note += f" -> Median der Quelle {med:+.3f}s"


# --------------------------------------------------------------------------- video
HW_CANDIDATES = ["hevc_videotoolbox", "hevc_nvenc", "hevc_qsv", "hevc_amf"]
ENCODER_NAMES = {"libx265": "x265", "libx264": "libx264", "prores_ks": "prores"}
SOFTWARE_ENCODERS = {"x265", "libx264", "prores"}


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
    if v == "x265":
        return ["-c:v", "libx265", "-crf", str(o.get("crf") or 28), "-preset", o["preset"],
                "-pix_fmt", "yuv420p", "-tag:v", "hvc1", "-x265-params", "log-level=error"]
    if v == "hevc_videotoolbox":
        return ["-c:v", "hevc_videotoolbox", "-b:v", o.get("bitrate", "3M"), "-pix_fmt", "yuv420p", "-tag:v", "hvc1"]
    if v == "prores":
        return ["-c:v", "prores_ks", "-profile:v", "0", "-pix_fmt", "yuv422p10le", "-vendor", "apl0"]
    if v.endswith(("_nvenc", "_qsv", "_amf")):
        args = ["-c:v", v, "-b:v", o.get("bitrate", "3M"), "-pix_fmt", "yuv420p"]
        return args + (["-tag:v", "hvc1"] if v.startswith("hevc") else [])
    if v == "h264_videotoolbox":
        return ["-c:v", "h264_videotoolbox", "-b:v", o.get("bitrate", "6M"), "-pix_fmt", "yuv420p"]
    return ["-c:v", "libx264", "-crf", str(o.get("crf") or 23), "-preset", o["preset"], "-pix_fmt", "yuv420p"]


def fps_value(s):
    num, _, den = str(s).partition("/")
    return float(num) / float(den or 1)


def full_quality_settings(o, clip):
    """Originalauflösung/-bildrate der Kamera, hohe Bitrate."""
    o["width"], o["height"] = clip.width or o["width"], clip.height or o["height"]
    o["fps_str"] = clip.fps_str or str(o["fps"])
    o["fps"] = fps_value(o["fps_str"])
    mbit = max(8, round(o["width"] * o["height"] * o["fps"] * 0.1 / 1e6))
    o["bitrate"] = f"{mbit}M"
    o["crf"] = o.get("crf") or 18


class Overlay:
    """drawtext-Einblendungen (nur mit --info)."""

    def __init__(self, cfg, tmp, enabled):
        self.cfg, self.tmp, self.enabled = cfg, tmp, enabled
        self.font = cfg["text"]["fontfile"] or next((f for f in FONT_CANDIDATES if Path(f).exists()), None)
        self.n = 0
        self.lock = threading.Lock()

    def text(self, content, size, x, y, expand=False, enable=None):
        if not self.enabled:
            return ""
        with self.lock:
            self.n += 1
            tf = self.tmp / f"txt_{self.n}.txt"
        tf.write_text(content, encoding="utf-8")
        opts = [f"textfile={filter_quote(tf)}", f"expansion={'normal' if expand else 'none'}",
                "fontcolor=white", f"fontsize={size}", "box=1", "boxcolor=black@0.6",
                f"boxborderw={max(4, size // 3)}", f"x={x}", f"y={y}"]
        if enable:
            opts.append(f"enable='{enable}'")
        if self.font:
            opts.insert(0, f"fontfile={filter_quote(self.font)}")
        return ",drawtext=" + ":".join(opts)

    @staticmethod
    def tc(offset):
        return "%{pts:hms:" + f"{max(0.0, offset):.4f}" + "}"

    def sizes(self, H):
        k = H / 1080
        fs = max(12, int(self.cfg["text"]["size"] * k))
        return k, fs, max(10, int(fs * 0.8)), max(10, int(40 * k))

    @staticmethod
    def audio_pos(i, mg, small, W):
        """Audio-Einblendungen abwechselnd oben links/rechts, weitere Zeilen darunter."""
        x = str(mg) if i % 2 == 0 else f"w-tw-{mg}"
        y = int(mg + (i // 2) * small * 3.4)
        return x, y


class SegmentRenderer:
    """Rendert ein Stück der Timeline (merge / sync-to-audio)."""

    def __init__(self, cfg, tmp, overlay, audio_srcs, markers=()):
        self.o, self.cfg, self.tmp, self.ov = cfg["output"], cfg, tmp, overlay
        self.audio_srcs = audio_srcs
        self.markers = list(markers)
        hw = self.o.get("hwaccel", "auto")
        self.hw = [] if not hw or hw == "none" else ["-hwaccel", hw]

    def marker_at(self, t):
        return next((m for m in self.markers if m.pos - 0.1 <= t < m.pos + m.keep), None)

    def signature(self, a, b, main, pip, auds):
        o, fps = self.o, self.o["fps"]
        t = a / fps
        black = None
        if main is None:
            m = self.marker_at(t)
            black = f"{m.real_start:%H:%M:%S}-{m.real_end:%H:%M:%S}" if m else "leer"
        clip = lambda c: None if c is None else [str(c.path), round(c.local(t), 3)]
        data = [5, a, b, clip(main), clip(pip), [clip(x) for x in auds] if self.ov.enabled else None, black,
                o["width"], o["height"], o.get("fps_str", fps), vcodec_args(o), self.cfg["pip"],
                self.ov.enabled, self.cfg["text"] if self.ov.enabled else None,
                [s.name for s in self.audio_srcs] if self.ov.enabled else None]
        return hashlib.sha1(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()[:16]

    def render(self, out, a, b, main, pip, auds):
        """Mit Hardware-Decoding rendern; schlägt das fehl, mit Software-Decoding wiederholen."""
        if self.hw and main is not None:
            try:
                return self._render(out, a, b, main, pip, auds, self.hw)
            except FFmpegError as e:
                time.sleep(0.5)
                if ABORT.is_set() or e.returncode < 0:
                    raise
                names = main.path.name + (f" / {pip.path.name}" if pip else "")
                print(f"  ! Hardware-Decoding fehlgeschlagen bei {names} – wiederhole mit Software-Decoding")
        return self._render(out, a, b, main, pip, auds, [])

    def _render(self, out, a, b, main, pip, auds, hw):
        o, ov = self.o, self.ov
        W, H, fps = o["width"], o["height"], o["fps"]
        rate = o.get("fps_str", fps)
        k, fs, small, mg = ov.sizes(H)
        n, t = b - a, a / fps
        cmd = ["ffmpeg", "-y", "-v", "error"]
        if main is None:
            cmd += ["-f", "lavfi", "-i", f"color=c=black:s={W}x{H}:r={rate}"]
            graph = "[0:v]null"
            m = self.marker_at(t)
            if m:
                dur = (m.real_end - m.real_start).total_seconds()
                texts = ov.text(f"Pause herausgeschnitten\n{m.real_start:%H:%M:%S} - {m.real_end:%H:%M:%S}"
                                f"  ({hms(dur, False)})", fs, "(w-tw)/2", "(h-th)/2")
            else:
                texts = ov.text("kein Videomaterial", fs, "(w-tw)/2", "(h-th)/2")
        else:
            fit = (f"fps={rate},scale={W}:{H}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
                   f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black,setsar=1,tpad=stop_mode=clone:stop_duration=2")
            cmd += [*hw, "-ss", f"{max(0.0, main.local(t)):.4f}", "-i", main.path]
            graph = f"[0:v]{fit}"
            texts = (ov.text(main.path.name, fs, mg, f"h-th-{mg}-{int(fs * 1.6)}")
                     + ov.text(ov.tc(main.local(t)), fs, mg, f"h-th-{mg}", expand=True))
            if pip is not None:
                pw = int(W * self.cfg["pip"]["width_fraction"]) // 2 * 2
                pm = int(self.cfg["pip"]["margin"] * k)
                ps = max(10, int(fs * 0.6))
                cmd += [*hw, "-ss", f"{max(0.0, pip.local(t)):.4f}", "-i", pip.path]
                graph += (f"[m];[1:v]fps={rate},scale={pw}:-2,setsar=1,tpad=stop_mode=clone:stop_duration=2"
                          + ov.text(pip.path.name, ps, 8, 8)
                          + ov.text(ov.tc(pip.local(t)), ps, 8, int(8 + ps * 1.6), expand=True)
                          + f"[p];[m][p]overlay=W-w-{pm}:H-h-{pm}")
        texts += ov.text(ov.tc(t), int(fs * 1.3), "(w-tw)/2", mg, expand=True)
        for i, (src, c) in enumerate(zip(self.audio_srcs, auds)):
            x, y = ov.audio_pos(i, mg, small, W)
            if c is None:
                texts += ov.text(f"{src.name}: -", small, x, y)
            else:
                texts += ov.text(f"{src.name}: {c.path.name}", small, x, y)
                texts += ov.text(ov.tc(c.local(t)), small, x, int(y + small * 1.6), expand=True)
        graph += texts + "[v]"
        cmd += ["-filter_complex", graph, "-map", "[v]", "-frames:v", str(n), "-an",
                *vcodec_args(o), "-video_track_timescale", "90000", out]
        run(cmd)


def build_segments(mains, pips, audio_lists, total_f, fps, piece_s=120.0):
    def frames(c):
        return max(0, round(c.start * fps)), min(total_f, round(c.end * fps))

    def active(lst, f):
        for c in lst:
            f0, f1 = frames(c)
            if f0 <= f < f1:
                return c
        return None

    pts, forced = {0, total_f}, set()
    for c in mains + pips + [c for lst in audio_lists for c in lst]:
        pts.update(frames(c))
    for c in mains + pips:   # Drift: Video alle piece_s neu ansetzen
        for tau, _ in c.pieces(piece_s)[1:]:
            forced.add(round(c.timeline(tau) * fps))
    pts |= forced
    pts = sorted(p for p in pts if 0 <= p <= total_f)
    segs = []
    for a, b in zip(pts, pts[1:]):
        if b <= a:
            continue
        mo, po = active(mains, a), active(pips, a)
        main, pip = (mo, po) if mo else (po, None)
        auds = tuple(active(lst, a) for lst in audio_lists)
        if segs and a not in forced and segs[-1][2] is main and segs[-1][3] is pip and segs[-1][4] == auds:
            segs[-1][1] = b
        else:
            segs.append([a, b, main, pip, auds])
    return segs


# --------------------------------------------------------------------------- Lücken schneiden
@dataclass
class GapMarker:
    pos: float          # Position in der geschnittenen Timeline
    keep: float         # davon stehen gelassen (Schwarzbild)
    real_start: datetime
    real_end: datetime


def cut_gaps(every, t0, gcfg):
    """Entfernt Zeiten, in denen weder Bild noch Ton aufgenommen wurde."""
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
    markers, removed, cuts = [], 0.0, []
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


# --------------------------------------------------------------------------- Zwischenstand
def probe_frames(path):
    """Fertige Videodatei prüfen. None = fehlt oder unvollständig."""
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-count_packets", "-select_streams", "v:0",
                            "-show_entries", "stream=nb_read_packets", "-of", "json", str(path)],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=120)
        return int(json.loads(r.stdout)["streams"][0]["nb_read_packets"])
    except Exception:
        return None


def load_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_json(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
    os.replace(tmp, path)


ANALYSE_VERSION = 3


def analyse_key(every, sources, cfg, mode):
    files = []
    for c in sorted(every, key=lambda c: (c.source, str(c.path))):
        st = c.path.stat()
        files.append([c.source, str(c.path), st.st_size, int(st.st_mtime)])
    srcs = [[s.key, s.kind, str(s.path), s.cfg] for s in sources]
    rel = {k: cfg.get(k) for k in ("timezone", "join_tolerance_s", "audio_sync")}
    cut = cfg["cut_gaps"] if mode != "sync-to-video" else None
    data = [ANALYSE_VERSION, mode, files, srcs, rel, cut, cfg["output"]["audio_rate"]]
    return hashlib.sha1(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()


# --------------------------------------------------------------------------- report
def report(sources, clips, t0, total, markers=()):
    lines = [f"Timeline-Start: {t0:%Y-%m-%d %H:%M:%S}   Länge: {hms(total)}", ""]
    if markers:
        lines.append(f"== Herausgeschnittene Pausen ({len(markers)})")
        for m in markers:
            dur = (m.real_end - m.real_start).total_seconds()
            lines.append(f"  @ {hms(m.pos)}  {m.real_start:%H:%M:%S} – {m.real_end:%H:%M:%S}  ({hms(dur, False)} entfernt)")
        lines.append("")
    kinds = {"audio": "Ton", "video": "Kamera", "pip": "Kamera Bild-im-Bild"}
    for s in sources:
        lines.append(f"== {s.name} ({kinds[s.kind]}, {len(clips[s.key])} Dateien)")
        for c in clips[s.key]:
            real = c.real_dt or (t0 + timedelta(seconds=c.start))
            lines.append(f"  {c.path.name:<28} {real:%H:%M:%S}  @ {hms(c.start)}  Dauer {hms(c.duration)}"
                         f"  [{c.method}]" + (f"  {c.sync_note}" if c.sync_note else ""))
        lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- Ton ausgeben
def channel_labels(names, chans):
    """Kanalbelegung als Text, z.B. '1 Interviewer | 2 Gast | 3–4 sony (Kamera)'."""
    out, k = [], 1
    for nm, ch in zip(names, chans):
        out.append(f"{k}{'–' + str(k + ch - 1) if ch > 1 else ''} {nm}")
        k += ch
    return " | ".join(out)


def audio_out_args(n_channels, full_audio, o):
    if full_audio:
        return ["-c:a", "pcm_s24le"]
    rate = int(str(o["aac_per_channel"]).rstrip("k"))
    return ["-c:a", "aac", "-b:a", f"{min(rate * n_channels, 1024)}k"]


def mux_audio_graph(inputs, names, first_idx, o, full_audio, chans=None):
    """Tonquellen (Eingänge ab first_idx) zu einer Spur mit n Kanälen bzw. n Spuren zusammenfassen."""
    n = len(inputs)
    if n == 0:
        return [], []
    chans = chans or [1] * n
    if o["audio_tracks"] == "single":
        lay = lambda i: ",aformat=channel_layouts=" + ("stereo" if full_audio and chans[i] >= 2 else "mono")
        graph = ";".join(f"[{first_idx + i}:a]anull{lay(i)}[m{i}]" for i in range(n))
        if n == 1:
            graph = graph.replace("[m0]", "[a]")
        else:
            graph += ";" + "".join(f"[m{i}]" for i in range(n)) + f"amerge=inputs={n}[a]"
        title = channel_labels(names, [c if full_audio else 1 for c in chans])
        return ["-filter_complex", graph, "-map", "[a]", *audio_out_args(n, full_audio, o)], \
            ["-metadata:s:a:0", f"title={title}"]
    maps, meta = [], []
    for i, nm in enumerate(names):
        maps += ["-map", f"{first_idx + i}:a"]
        meta += [f"-metadata:s:a:{i}", f"title={nm}", f"-metadata:s:a:{i}", f"handler_name={nm}"]
    return maps + audio_out_args(1, full_audio, o), meta


# --------------------------------------------------------------------------- Hauptprogramm
def _on_sigint(signum, frame):
    ABORT.set()
    raise KeyboardInterrupt


def parse_args():
    ap = argparse.ArgumentParser(
        description="Ton- und Kameraaufnahmen automatisch synchronisieren.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Modi:\n"
               "  merge          ein Video: Hauptkamera + optional Bild-im-Bild (--pip), schwarz ohne Kamera\n"
               "  sync-to-audio  ein Video aus genau einer Kamera, Zeitachse am Ton\n"
               "  sync-to-video  pro Kameraclip eine Datei mit allen synchronen Tonspuren\n\n"
               "Ordner mit Anzeigenamen: --audio Interviewer=/pfad/lav1")
    ap.add_argument("mode", choices=MODES)
    ap.add_argument("--audio", action="append", metavar="[NAME=]PFAD",
                    help="Tonordner (beliebig oft); der erste ist die Sync-Referenz")
    ap.add_argument("--video", metavar="[NAME=]PFAD", help="Ordner der (Haupt-)Kamera")
    ap.add_argument("--pip", metavar="[NAME=]PFAD", help="nur merge: zweite Kamera als Bild-im-Bild")
    ap.add_argument("--info", action="store_true", help="Dateinamen und Timecodes einblenden")
    ap.add_argument("--only_new_audio", "--only-new-audio", action="store_true",
                    help="Kameraton weglassen (wird zum Synchronisieren trotzdem verwendet)")
    ap.add_argument("--full_quality_video", "--full-quality-video", action="store_true",
                    help="Originalauflösung/-bildrate; sync-to-video ohne --info kopiert das Bild unverändert")
    ap.add_argument("--full_quality_audio", "--full-quality-audio", action="store_true",
                    help="Ton unbearbeitet als PCM, ohne Rauschunterdrückung/Lautstärkeanpassung")
    ap.add_argument("-o", "--output", type=Path, help="Ausgabeordner (Standard: Ordner über dem Kameraordner)")
    ap.add_argument("-n", "--name", help="Name der Ausgabe (sync-to-video: Präfix der Dateinamen)")
    ap.add_argument("--fresh", action="store_true", help="Zwischenstand ignorieren, komplett neu")
    ap.add_argument("--dry-run", action="store_true", help="nur Zeiten erkennen und anzeigen")
    ap.add_argument("--config", type=Path, help="config.json (Standard: neben dem Skript)")
    args = ap.parse_args()
    if not args.audio:
        ap.error("mindestens ein --audio-Ordner ist nötig")
    if not args.video:
        ap.error("--video fehlt")
    if args.pip and args.mode != "merge":
        ap.error("--pip gibt es nur im Modus merge")
    return args


def main():
    try:
        signal.signal(signal.SIGINT, _on_sigint)
    except (ValueError, OSError):
        pass
    args = parse_args()
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            sys.exit(f"{tool} nicht gefunden – bitte installieren (Mac: source setup.sh).")

    cfg = DEFAULT_CONFIG
    cfg_path = args.config or Path(__file__).resolve().with_name("config.json")
    if cfg_path.exists():
        cfg = deep_merge(cfg, json.loads(cfg_path.read_text(encoding="utf-8-sig")))
        print(f"Config: {cfg_path}")
    elif args.config:
        sys.exit(f"Config nicht gefunden: {cfg_path}")
    if "lav_to_lav" in cfg["audio_sync"]:
        cfg["audio_sync"]["audio_to_audio"] = cfg["audio_sync"]["lav_to_lav"]

    global TARGET_TZ
    if cfg.get("timezone"):
        if ZoneInfo is None:
            sys.exit("timezone braucht Python 3.9+")
        TARGET_TZ = ZoneInfo(cfg["timezone"])
        print(f"Geräte-Zeitzone: {cfg['timezone']}")

    mode = args.mode
    o = cfg["output"]
    full_v, full_a = args.full_quality_video, args.full_quality_audio
    sources = parse_sources(args, cfg)
    audio_srcs = [s for s in sources if s.kind == "audio"]
    video_src = next(s for s in sources if s.kind == "video")
    pip_src = next((s for s in sources if s.kind == "pip"), None)
    vids = [video_src] + ([pip_src] if pip_src else [])

    print(f"Modus: {mode}")
    print("Lese Dateien ...")
    clips = load_clips(sources)
    chain_and_join(clips, sources, cfg["join_tolerance_s"])
    audio_srcs = [s for s in audio_srcs if clips[s.key]]
    if not audio_srcs:
        sys.exit("Keine Tondateien gefunden.")
    if not clips[video_src.key]:
        sys.exit(f"Keine Videodateien in {video_src.path}")
    every = [c for s in sources for c in clips[s.key]]
    t0 = min(c.start_dt for c in every)
    for c in every:
        c.start = (c.start_dt - t0).total_seconds()
    total = max(c.end for c in every)

    project = video_src.path if video_src.path.is_dir() else video_src.path.parent
    project = project.parent
    out_dir = (args.output or project).expanduser().resolve()
    base = args.name or (f"{project.name}_{mode.replace('-', '_')}" if mode != "sync-to-video" else "")
    if mode == "sync-to-video":
        prefix = f"{args.name}_" if args.name else ""
        work_name = safe_name(f"{prefix}{video_src.name}_synced")
        report_name = f"{work_name}_report.txt"
        out_name = None
    else:
        out_name = Path(base).name
        if Path(out_name).suffix.lower() not in (".mov", ".mp4", ".mkv"):
            out_name += ".mov"
        work_name = safe_name(Path(out_name).stem)
        report_name = Path(out_name).stem + "_report.txt"

    if args.dry_run:
        markers = cut_gaps(every, t0, cfg["cut_gaps"]) if mode != "sync-to-video" else []
        total = max(c.end for c in every)
        print("\n" + report(sources, clips, t0, total, markers))
        if mode == "sync-to-video":
            print("Geplante Ausgaben:")
            for c in clips[video_src.key]:
                print(f"  {out_dir / (prefix + c.path.stem + '_synced.mov')}")
        else:
            print(f"Geplante Ausgabe: {out_dir / out_name}")
        return
    if np is None and cfg["audio_sync"]["enabled"]:
        sys.exit("numpy fehlt – ohne numpy kein Audio-Sync. Terminal vorbereiten: source setup.sh")

    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out_dir / ("imerge_" + work_name)
    tmp.mkdir(exist_ok=True)
    lock = tmp / "läuft.lock"
    try:
        age = time.time() - lock.stat().st_mtime
    except FileNotFoundError:
        age = None
    if age is not None and age < 90:
        sys.exit(f"Für {work_name} läuft bereits ein anderer Durchgang (Sperrdatei {lock}).\n"
                 f"Warte, bis er fertig ist. Falls sicher keiner mehr läuft: Datei löschen und neu starten.")
    lock.write_text(f"pid {os.getpid()}\n", encoding="utf-8")
    stop_beat = threading.Event()

    def heartbeat():
        while not stop_beat.wait(20):
            try:
                os.utime(lock)
            except OSError:
                pass
    threading.Thread(target=heartbeat, daemon=True).start()
    if not args.fresh and ((tmp / "analyse.json").exists() or (tmp / "segments.json").exists()):
        print(f"Zwischenstand gefunden in {tmp.name} – wird übernommen, soweit Quellen und Einstellungen gleich sind")
    finished = False
    sr = o["audio_rate"]
    scfg = cfg["audio_sync"]
    wavs = {s.key: tmp / f"{s.key}.wav" for s in sources}
    cam_ch = {s.key: (min(2, max((c.channels for c in clips[s.key]), default=1)) if full_a else 1) for s in vids}

    try:
        # ------------------------------------------------------------- Analyse + Sync
        sfile = tmp / "analyse.json"
        key = analyse_key(every, sources, cfg, mode)
        state = None if args.fresh else load_json(sfile)
        if state and state.get("key") != key:
            print("  Quelldateien oder Einstellungen haben sich geändert – Analyse läuft neu")
            state = None
        cid = lambda c: f"{c.source}|{c.path}"

        def wav_ok(name, path):
            return state is not None and path.exists() and state.get("wavs", {}).get(name) == path.stat().st_size

        def wav_done(name, path):
            state.setdefault("wavs", {})[name] = path.stat().st_size
            save_json(sfile, state)

        def build_aud(s):
            build_audio_track(clips[s.key], total, wavs[s.key], 1, sr)

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
            for s in audio_srcs:
                if not wav_ok(s.key, wavs[s.key]):
                    print(f"Baue Tonspur {s.name} neu ...")
                    build_aud(s)
                    wav_done(s.key, wavs[s.key])
        else:
            do_sync = scfg["enabled"]
            ref = audio_srcs[0]
            print(f"Baue Tonspuren (Referenz: {ref.name}) ...")
            build_aud(ref)
            if do_sync and scfg.get("audio_to_audio", True) and len(audio_srcs) > 1:
                print(f"Synchronisiere weitere Tonquellen auf {ref.name} ...")
                refine_sync([(s.name, clips[s.key]) for s in audio_srcs[1:]], [wavs[ref.key]],
                            clips[ref.key], scfg, total)
            for s in audio_srcs[1:]:
                build_aud(s)
            if do_sync:
                print("Synchronisiere Kameras über den Ton ...")
                refine_sync([(s.name, clips[s.key]) for s in vids], [wavs[s.key] for s in audio_srcs],
                            [c for s in audio_srcs for c in clips[s.key]], scfg, total)
            shift = -min(c.start for c in every)
            for c in every:
                c.start += shift
            t0 -= timedelta(seconds=shift)
            if mode == "sync-to-video":
                for c in every:
                    c.real_dt = t0 + timedelta(seconds=c.start)
                markers = []
            else:
                markers = cut_gaps(every, t0, cfg["cut_gaps"])
                if markers:
                    print(f"{len(markers)} Pause(n) ohne Bild und Ton herausgeschnitten.")
            total = max(c.end for c in every)
            print("Baue Tonspuren an den endgültigen Positionen ...")
            for s in audio_srcs:
                build_aud(s)
            state = {"key": key, "t0": t0.isoformat(), "total": total,
                     "clips": {cid(c): {"start": c.start, "drift": c.drift, "note": c.sync_note,
                                        "real": c.real_dt.isoformat() if c.real_dt else None} for c in every},
                     "markers": [{"pos": m.pos, "keep": m.keep, "rs": m.real_start.isoformat(),
                                  "re": m.real_end.isoformat()} for m in markers],
                     "wavs": {s.key: wavs[s.key].stat().st_size for s in audio_srcs}}
            save_json(sfile, state)

        rep = report(sources, clips, t0, total, markers)
        print("\n" + rep)
        (out_dir / report_name).write_text(rep, encoding="utf-8")

        # ------------------------------------------------------------- Tonaufbereitung
        pcfg = cfg["audio_processing"]
        chain = audio_chain(pcfg) if pcfg.get("enabled") and not full_a else ""
        chain_tag = hashlib.sha1(chain.encode()).hexdigest()[:10]
        final = {}
        if chain:
            print("Bereite Ton für Transkription auf (Hochpass, Rauschunterdrückung, Lautstärke) ...")
        for s in audio_srcs:
            final[s.key] = wavs[s.key]
            if chain:
                p = tmp / f"{s.key}_processed.wav"
                pname = f"{s.key}_processed|{chain_tag}"
                if not wav_ok(pname, p):
                    print(f"  {s.name} ...")
                    run(["ffmpeg", "-y", "-v", "error", "-i", wavs[s.key], "-af", chain,
                         "-c:a", "pcm_s24le", "-rf64", "auto", p])
                    wav_done(pname, p)
                final[s.key] = p

        overlay = Overlay(cfg, tmp, args.info)
        if args.info:
            filters = run(["ffmpeg", "-hide_banner", "-filters"]).decode(errors="replace")
            if " drawtext " not in filters:
                print("  ! ffmpeg ohne drawtext – Einblendungen nicht möglich (source setup.sh)")
                overlay.enabled = False

        if mode == "sync-to-video":
            run_sync_to_video(args, cfg, clips, video_src, audio_srcs, final, chain, tmp, out_dir, prefix,
                              overlay, state, sfile)
        else:
            run_merge(args, cfg, clips, sources, video_src, pip_src, audio_srcs, vids, final, chain,
                      tmp, out_dir, out_name, total, markers, overlay, wav_ok, wav_done, cam_ch, wavs)
        finished = True
    finally:
        stop_beat.set()
        try:
            lock.unlink()
        except OSError:
            pass
        if finished:
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            print(f"\nAbgebrochen. Der Zwischenstand bleibt in {tmp}.\n"
                  f"Zum Fortsetzen einfach denselben Befehl nochmal starten (mit --fresh komplett neu).")


def pick_jobs(o, enc):
    jobs = o.get("jobs", "auto")
    if jobs == "auto":
        jobs = 1 if enc in SOFTWARE_ENCODERS else 3
    return max(1, int(jobs))


def run_parallel(jobs, items, fn):
    if jobs == 1:
        for it in items:
            fn(it)
        return
    ex = ThreadPoolExecutor(max_workers=jobs)
    futures = [ex.submit(fn, it) for it in items]
    try:
        for f in futures:
            f.result()
    except BaseException:
        ABORT.set()
        for f in futures:
            f.cancel()
        ex.shutdown(wait=False)
        raise
    ex.shutdown()


def setup_encoder(o, manifest):
    want_auto = o["vcodec"] == "auto"
    enc = resolve_encoder(o)
    prev = manifest.get("__encoder__")
    if want_auto and prev and prev != enc and (prev in SOFTWARE_ENCODERS or encoder_works(prev)):
        print(f"  Vorheriger Lauf nutzte {prev} – bleibe dabei, damit der Zwischenstand passt")
        enc = prev
    o["vcodec"] = enc
    manifest["__encoder__"] = enc
    return enc


# --------------------------------------------------------------------------- merge / sync-to-audio
def run_merge(args, cfg, clips, sources, video_src, pip_src, audio_srcs, vids, final, chain,
              tmp, out_dir, out_name, total, markers, overlay, wav_ok, wav_done, cam_ch, wavs):
    o = cfg["output"]
    sr = o["audio_rate"]
    full_a = args.full_quality_audio
    if args.full_quality_video:
        full_quality_settings(o, max(clips[video_src.key], key=lambda c: c.duration))

    # Kameraton als Timeline-Spur
    cam_tracks = []
    if not args.only_new_audio:
        for s in vids:
            if not any(c.has_audio for c in clips[s.key]):
                continue
            name = f"{s.key}|ch{cam_ch[s.key]}"
            if not wav_ok(name, wavs[s.key]):
                print(f"Baue Kamera-Tonspur {s.name} ...")
                build_audio_track(clips[s.key], total, wavs[s.key], cam_ch[s.key], sr)
                wav_done(name, wavs[s.key])
            p = wavs[s.key]
            if chain:
                pp = tmp / f"{s.key}_processed.wav"
                pname = f"{s.key}_processed|{hashlib.sha1(chain.encode()).hexdigest()[:10]}"
                if not wav_ok(pname, pp):
                    run(["ffmpeg", "-y", "-v", "error", "-i", p, "-af", chain,
                         "-c:a", "pcm_s24le", "-rf64", "auto", pp])
                    wav_done(pname, pp)
                p = pp
            cam_tracks.append((s.name, p, cam_ch[s.key]))

    fps = o["fps"]
    total_f = int(round(total * fps))
    segs = build_segments(clips[video_src.key], clips[pip_src.key] if pip_src else [],
                          [clips[s.key] for s in audio_srcs], total_f, fps)
    mfile = tmp / "segments.json"
    manifest = {} if args.fresh else load_json(mfile)
    print(f"Arbeitsordner: {tmp}")
    enc = setup_encoder(o, manifest)
    jobs = pick_jobs(o, enc)
    hw = o.get("hwaccel", "auto")
    print(f"Video-Encoder: {enc}  |  {o['width']}x{o['height']} @ {o.get('fps_str', fps)}  |  Decoding: "
          f"{'Software' if not hw or hw == 'none' else 'Hardware'}  |  parallel: {jobs}  |  Einblendungen: "
          f"{'ja' if overlay.enabled else 'nein'}")

    r = SegmentRenderer(cfg, tmp, overlay, audio_srcs, markers)
    sigs = [r.signature(*sg) for sg in segs]
    seg_files = [tmp / f"seg_{sg}.mov" for sg in sigs]
    total_frames = sum(b - a for a, b, *_ in segs)
    todo = []
    for i, (sg, f) in enumerate(zip(sigs, seg_files)):
        if not (manifest.get(sg) == f.name and f.exists() and probe_frames(f) == segs[i][1] - segs[i][0]):
            todo.append(i)
    saved = [k for k in manifest if not k.startswith("__")]
    if saved:
        match = len(set(saved) & set(sigs))
        print(f"  Gespeicherter Zwischenstand: {len(saved)} Segmente, {match} passen zum aktuellen Plan")
    if len(todo) < len(segs):
        done_f = total_frames - sum(segs[i][1] - segs[i][0] for i in todo)
        print(f"Fortsetzen: {len(segs) - len(todo)}/{len(segs)} Segmente schon fertig "
              f"({done_f / max(total_frames, 1) * 100:.0f}%), rendere die restlichen {len(todo)}")
    todo_frames = sum(segs[i][1] - segs[i][0] for i in todo)
    done = {"frames": 0}
    t_start = time.time()
    plock = threading.Lock()

    def work(i):
        a, b, main, pip, auds = segs[i]
        part = seg_files[i].with_name(seg_files[i].stem + ".part.mov")
        r.render(part, a, b, main, pip, auds)
        os.replace(part, seg_files[i])
        with plock:
            manifest[sigs[i]] = seg_files[i].name
            save_json(mfile, manifest)
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

    run_parallel(jobs, todo, work)
    if todo:
        print(f"  Video gerendert in {hms(time.time() - t_start, False)}")
    listfile = tmp / "segments.txt"
    listfile.write_text("".join(concat_line(f) for f in seg_files), encoding="utf-8")
    print("Füge zusammen ...")
    video = tmp / "video.mov"
    run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", listfile, "-c", "copy", video])

    tracks = [(s.name, final[s.key], 1) for s in audio_srcs] + cam_tracks
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", video]
    for _, p, _ in tracks:
        cmd += ["-i", p]
    amap, meta = mux_audio_graph([t[1] for t in tracks], [t[0] for t in tracks], 1, o, full_a,
                                 [t[2] for t in tracks])
    cmd += ["-map", "0:v", "-c:v", "copy"] + amap + meta
    out = out_dir / out_name
    part_out = out.with_name(out.stem + ".part" + out.suffix)
    run(cmd + [part_out])
    os.replace(part_out, out)
    print(f"\nFertig: {out}\n  Tonkanäle: " + channel_labels([t[0] for t in tracks],
                                                              [t[2] if full_a else 1 for t in tracks]))


# --------------------------------------------------------------------------- sync-to-video
def run_sync_to_video(args, cfg, clips, video_src, audio_srcs, final, chain, tmp, out_dir, prefix,
                      overlay, state, sfile):
    o = cfg["output"]
    sr = o["audio_rate"]
    full_v, full_a = args.full_quality_video, args.full_quality_audio
    copy_video = full_v and not overlay.enabled
    mfile = tmp / "segments.json"
    manifest = {} if args.fresh else load_json(mfile)
    print(f"Arbeitsordner: {tmp}")
    if copy_video:
        enc = "copy"
        jobs = 2
    else:
        enc = setup_encoder(o, manifest)
        jobs = pick_jobs(o, enc)
    hw = o.get("hwaccel", "auto")
    hwargs = [] if not hw or hw == "none" else ["-hwaccel", hw]
    vclips = clips[video_src.key]
    res_txt = "Originalauflösung" if full_v else "{}x{} @ {}".format(o["width"], o["height"], o["fps"])
    print(f"Video: {'unverändert kopiert' if copy_video else enc}  |  {res_txt}  |  "
          f"parallel: {jobs}  |  Einblendungen: {'ja' if overlay.enabled else 'nein'}")

    def out_path(c):
        return out_dir / f"{prefix}{c.path.stem}_synced.mov"

    def signature(c):
        data = [6, state["key"], str(c.path), copy_video, full_v, full_a, args.only_new_audio, overlay.enabled,
                chain, o["audio_tracks"], [s.name for s in audio_srcs],
                None if copy_video else vcodec_args(o), None if full_v else [o["width"], o["height"], o["fps"]],
                cfg["text"] if overlay.enabled else None]
        return hashlib.sha1(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()[:16]

    todo, skipped = [], 0
    for c in vclips:
        f = out_path(c)
        if manifest.get(signature(c)) == f.name and f.exists() and probe_frames(f):
            skipped += 1
        else:
            todo.append(c)
    if skipped:
        print(f"Fortsetzen: {skipped}/{len(vclips)} Clips schon fertig, bearbeite die restlichen {len(todo)}")
    plock = threading.Lock()
    t_start = time.time()
    done = {"n": 0, "dur": 0.0}
    todo_dur = sum(c.duration for c in todo)

    def video_args(c, hw_in):
        """Eingang 0 = Video. Liefert (Eingangsargumente, Video-Map/Codec-Argumente)."""
        if copy_video:
            return ["-i", c.path], ["-map", "0:v:0", "-c:v", "copy"]
        W, H = (c.width, c.height) if full_v else (o["width"], o["height"])
        rate = c.fps_str if full_v else str(o["fps"])
        oo = dict(o)
        if full_v:
            full_quality_settings(oo, c)
        vf = f"fps={rate}"
        if not full_v:
            vf += (f",scale={W}:{H}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
                   f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black,setsar=1")
        k, fs, small, mg = overlay.sizes(H)
        texts = (overlay.text(c.path.name, fs, mg, f"h-th-{mg}-{int(fs * 1.6)}")
                 + overlay.text(overlay.tc(0), fs, mg, f"h-th-{mg}", expand=True))
        # pro Tonquelle die jeweils laufende Datei + Position darin (wechselt z.B. alle 30 min bei DJI)
        for i, s in enumerate(audio_srcs):
            x, y = overlay.audio_pos(i, mg, small, W)
            parts = [a for a in clips[s.key] if a.end > c.start and a.start < c.end]
            if not parts:
                texts += overlay.text(f"{s.name}: -", small, x, y)
            for a in parts:
                t_a = max(0.0, (a.start - c.start) / (1 + c.drift))
                t_b = (a.end - c.start) / (1 + c.drift)
                en = f"between(t,{t_a:.3f},{t_b:.3f})"
                texts += overlay.text(f"{s.name}: {a.path.name}", small, x, y, enable=en)
                texts += overlay.text(overlay.tc(a.local(c.start)), small, x, int(y + small * 1.6),
                                      expand=True, enable=en)
        return [*hw_in, "-i", c.path], ["-filter_complex", f"[0:v]{vf}{texts}[v]", "-map", "[v]",
                                        *vcodec_args(oo)]

    def work(c):
        dur = c.duration
        n = int(round(dur * sr))
        inputs, graphs, labels, names = [], [], [], []
        mono = ",aformat=channel_layouts=mono"
        cam_lay = ",aformat=channel_layouts=" + ("stereo" if full_a and c.channels >= 2 else "mono")
        for i, s in enumerate(audio_srcs):
            # Ton der Timeline ab Clipstart; Drift: Timeline-Länge auf Clip-Länge strecken
            inputs += ["-ss", f"{c.start:.6f}", "-t", f"{dur * (1 + c.drift) + 1:.6f}", "-i", final[s.key]]
            graphs.append(f"[{i + 1}:a]asetrate={sr * (1 + c.drift):.6f},aresample={sr},asetpts=N/SR/TB,"
                          f"apad,atrim=end_sample={n}{mono}[t{i}]")
            labels.append(f"[t{i}]")
            names.append(s.name)
        if not args.only_new_audio and c.has_audio:
            proc = f"{chain}," if chain else ""
            graphs.append(f"[0:a:0]{proc}aresample={sr},asetpts=N/SR/TB,apad,atrim=end_sample={n}{cam_lay}[cam]")
            labels.append("[cam]")
            names.append(f"{video_src.name} (Kamera)")
        nch = len(labels)
        if o["audio_tracks"] == "single":
            if nch > 1:
                graphs.append("".join(labels) + f"amerge=inputs={nch}[aout]")
            else:
                graphs[-1] = graphs[-1].rsplit("[", 1)[0] + "[aout]"
            amaps = ["-map", "[aout]"]
            chs = [1] * len(audio_srcs) + ([2 if "stereo" in cam_lay else 1] if len(names) > len(audio_srcs) else [])
            meta = ["-metadata:s:a:0", "title=" + channel_labels(names, chs)]
            acodec = audio_out_args(nch, full_a, o)
        else:
            amaps = [x for lb in labels for x in ("-map", lb)]
            meta = [x for i, nm in enumerate(names) for x in (f"-metadata:s:a:{i}", f"title={nm}")]
            acodec = audio_out_args(1, full_a, o)
        out = out_path(c)
        part = out.with_name(out.stem + ".part.mov")

        def attempt(hw_in):
            vin, vmap = video_args(c, hw_in)
            agraph = ";".join(graphs)
            if "-filter_complex" in vmap:
                j = vmap.index("-filter_complex")
                vmap = vmap[:j + 1] + [vmap[j + 1] + ";" + agraph] + vmap[j + 2:]
                fc = []
            else:
                fc = ["-filter_complex", agraph]
            run(["ffmpeg", "-y", "-v", "error", *vin, *inputs, *fc, *vmap, *amaps, *acodec, *meta,
                 "-map_metadata", "0", "-t", f"{dur:.6f}", part])

        if hwargs and not copy_video:
            try:
                attempt(hwargs)
            except FFmpegError as e:
                time.sleep(0.5)
                if ABORT.is_set() or e.returncode < 0:
                    raise
                print(f"  ! Hardware-Decoding fehlgeschlagen bei {c.path.name} – wiederhole mit Software-Decoding")
                attempt([])
        else:
            attempt([])
        os.replace(part, out)
        with plock:
            manifest[signature(c)] = out.name
            save_json(mfile, manifest)
            done["n"] += 1
            done["dur"] += dur
            el = time.time() - t_start
            frac = done["dur"] / max(todo_dur, 1e-6)
            eta = el / frac - el if frac > 0 else 0
            print(f"  [{done['n']}/{len(todo)}] {out.name}  ({hms(dur, False)}, Tempo {done['dur'] / max(el, 1e-6):.1f}x,"
                  f" Rest ca. {hms(eta, False)})")

    run_parallel(jobs, todo, work)
    names = [s.name for s in audio_srcs] + ([] if args.only_new_audio else [f"{video_src.name} (Kamera)"])
    cam_c = 2 if full_a and max(c.channels for c in vclips) >= 2 else 1
    chs = [1] * len(audio_srcs) + ([] if args.only_new_audio else [cam_c])
    print(f"\nFertig: {len(vclips)} Dateien in {out_dir}\n  Tonkanäle: " + channel_labels(names, chs))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)