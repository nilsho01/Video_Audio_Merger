# Video_Audio_Merger (`interview_merge.py`)

Synchronisiert automatisch **beliebig viele Tonaufnahmen** (z.B. DJI-Mic-Lavs, Recorder) mit **Kameraclips** (z.B. Sony, GoPro) – auch wenn die Geräte-Uhren falsch gehen, in einer anderen Zeitzone stehen oder leicht unterschiedlich schnell laufen.

Drei Modi:

| Modus | Ergebnis |
|---|---|
| `merge` | **Ein** durchgehendes Video: Hauptkamera vollformatig, optional eine zweite Kamera als Bild-im-Bild, Schwarzbild wo keine Kamera lief. Alle Tonquellen als getrennte Kanäle. |
| `sync-to-audio` | Wie `merge`, aber mit **genau einer** Kamera. Die Zeitachse richtet sich nach dem Ton; Lücken ohne Bild sind schwarz. |
| `sync-to-video` | **Pro Kameraclip eine Datei** mit allen dazu synchronen Tonspuren. Nur Zeiten mit Bild, kein Schwarzbild. Ideal zum Weiterschneiden. |

---

## Schnellstart

### macOS

In jedem neuen Terminal, im Ordner mit dem Skript:

```bash
source setup.sh
python3 interview_merge.py merge \
    --audio Interviewer=/Volumes/Platte/US_01/lav_interviewer \
    --audio Gast=/Volumes/Platte/US_01/lav_befragter \
    --video /Volumes/Platte/US_01/sony \
    --pip /Volumes/Platte/US_01/gopro \
    -o /Volumes/Export/US_01 -n US_01_MERGED
```

`setup.sh` prüft und installiert bei Bedarf alles Nötige (Homebrew-Check, `ffmpeg-full`, Python-Umgebung `.venv` mit numpy und Zeitzonendaten) und setzt PATH und Umgebung für **dieses** Terminalfenster. Es muss mit `source` gestartet werden.

### Windows (Eingabeaufforderung / cmd)

```bat
setup
python interview_merge.py sync-to-video --audio "E:\US_01\lav1" --audio "E:\US_01\lav2" --video "E:\US_01\sony" -o "E:\Export"
```

`setup.bat` installiert per `winget` ffmpeg (Gyan-Build) und bei Bedarf Python, legt `.venv` mit numpy und tzdata an und aktiviert sie. **Nicht per Doppelklick starten**, sonst schließt sich das Fenster und die Einstellungen sind weg. cmd im richtigen Ordner öffnen: im Explorer in die Adresszeile `cmd` tippen.

### Windows (PowerShell)

```powershell
. .\setup.ps1
python interview_merge.py merge --audio "E:\US_01\lav1" --video "E:\US_01\sony" -o "E:\Export"
```

Wichtig ist der **Punkt mit Leerzeichen** vor `.\setup.ps1`. Einmalig vorab, falls Skripte blockiert sind: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` (bei heruntergeladener Datei zusätzlich `Unblock-File .\setup.ps1`).

Unter Windows heißt der Befehl `python` statt `python3`. Als schnelle Hardware-Encoder gibt es dort je nach Grafikkarte `hevc_nvenc` (NVIDIA), `hevc_qsv` (Intel) oder `hevc_amf` (AMD); `"vcodec": "auto"` wählt automatisch.

---

## Aufruf

```
python3 interview_merge.py MODUS --audio [NAME=]PFAD [--audio ...] --video [NAME=]PFAD [--pip [NAME=]PFAD] [Optionen]
```

| Angabe | Bedeutung |
|---|---|
| `MODUS` | `merge`, `sync-to-audio` oder `sync-to-video` |
| `--audio [NAME=]PFAD` | Ordner (oder einzelne Datei) mit Tonaufnahmen. **Beliebig oft.** Der **erste** ist die Referenz, auf die alles synchronisiert wird. |
| `--video [NAME=]PFAD` | Ordner (oder Datei) der Kamera. Pflicht, genau einer. |
| `--pip [NAME=]PFAD` | Nur `merge`: zweite Kamera, erscheint als Bild-im-Bild, wenn beide laufen (sonst vollformatig). |

`NAME=` ist optional und gibt der Quelle einen Anzeigenamen für Tonkanäle, Einblendungen und Report, z.B. `--audio Interviewer=/pfad/lav1`. Ohne Namen gilt der Ordnername.

### Optionen

| Option | Wirkung |
|---|---|
| `--info` | Dateinamen und Timecodes einblenden (sonst sauberes Bild ohne Text). |
| `--only_new_audio` | Kameraton weglassen. Er wird intern trotzdem zum Synchronisieren benutzt. |
| `--full_quality_video` | Originalauflösung und -bildrate der Kamera, hohe Bitrate. Bei `sync-to-video` **ohne** `--info` wird das Bild sogar **bitgenau unverändert** kopiert (keine Neukodierung, sehr schnell). |
| `--full_quality_audio` | Ton **unbearbeitet** als PCM (24 bit), ohne Rauschunterdrückung und Lautstärkeanpassung; Kameraton bleibt Stereo. |
| `-o PFAD` | Ausgabeordner (wird angelegt). Standard: der Ordner über dem Kameraordner. |
| `-n NAME` | Name des Videos (`merge`, `sync-to-audio`). Bei `sync-to-video` Präfix vor jedem Dateinamen. |
| `--dry-run` | Nur Dateien und Zeiten erkennen und anzeigen, nichts rendern. Dauert Sekunden. |
| `--fresh` | Zwischenstand eines abgebrochenen Laufs ignorieren und komplett neu rechnen. |
| `--config PFAD` | Andere `config.json` verwenden (Standard: die neben dem Skript). |

Ohne die beiden `--full_quality_*`-Optionen ist das Ergebnis auf Sichtung und Transkription ausgelegt: 1080p, H.265 per Hardware, Ton aufbereitet (Hochpass, Rauschunterdrückung, Lautstärkeangleich) als AAC.

### Beispiele

```bash
# Ein Video mit beiden Kameras, zum Sichten/Transkribieren, mit Einblendungen
python3 interview_merge.py merge --audio lav1 --audio lav2 --video sony --pip gopro -o export -n Interview --info

# Ein Video aus einer Kamera, nur Lav-Ton
python3 interview_merge.py sync-to-audio --audio lav1 --audio lav2 --video sony -o export --only_new_audio

# Jeden Sony-Clip mit synchronem Lav-Ton, Bild unverändert, Ton unbearbeitet – zum Schneiden
python3 interview_merge.py sync-to-video --audio lav1 --audio lav2 --video sony -o export \
    --full_quality_video --full_quality_audio
```

---

## Was das Skript macht

1. **Startzeiten erkennen** für jede Datei: Sony-XML, Broadcast-WAV-Metadaten, `creation_time`, Uhrzeit im Dateinamen oder notfalls das Dateidatum.
2. **Geteilte Dateien zusammensetzen.** Tonaufnahmen, die automatisch geteilt wurden (DJI Mic 2 alle 30 Minuten), werden lückenlos aneinandergehängt; GoPro-Kapitel (`GX01…`, `GX02…`) ebenso.
3. **Über den Ton synchronisieren**, jeweils gegen die Referenz (erster `--audio`-Ordner):
   - **Grobsuche:** Die komplette Aufnahme einer Quelle wird gegen die komplette Referenz abgeglichen. Findet auch Uhrzeitfehler von Minuten oder Stunden.
   - **Mittlere Suche:** Ist die Grobsuche nicht eindeutig (typisch bei leisem, verrauschtem GoPro-Ton), werden mehrere 60-Sekunden-Ausschnitte als Wellenform verglichen. Übernommen wird nur, wenn mehrere Ausschnitte unabhängig übereinstimmen.
   - **Feinsuche:** Jeder Clip wird an mehreren Stellen auf Millisekunden genau abgeglichen, inklusive **Drift** (Uhren laufen minimal unterschiedlich schnell). Widersprüchliche Messungen werden verworfen.
   - Weitere Tonquellen werden genauso auf die Referenz synchronisiert, danach alle Kameras auf die Summe aller Tonquellen.
4. **Pausen herausschneiden** (`merge`, `sync-to-audio`), in denen weder Bild noch Ton aufgenommen wurde.
5. **Ton aufbereiten** für Transkription (außer bei `--full_quality_audio`).
6. **Rendern** bzw. bei `sync-to-video` pro Clip die Datei bauen; der Ton wird dabei drift-genau auf die Clip-Länge gestreckt.

---

## Ergebnis

### Dateien

| Modus | Dateien im Ausgabeordner |
|---|---|
| `merge`, `sync-to-audio` | `NAME.mov` und `NAME_report.txt` |
| `sync-to-video` | pro Clip `[NAME_]CLIP_synced.mov` (z.B. `C0047_synced.mov`) und ein Report |

### Ton

Standard (`"audio_tracks": "single"`): **eine Tonspur mit mehreren Kanälen**, in dieser Reihenfolge:

1. die `--audio`-Quellen in der Reihenfolge des Aufrufs
2. der Kameraton (`--video`, dann `--pip`), außer bei `--only_new_audio`

Die Belegung steht am Ende der Ausgabe, z.B. `Tonkanäle: 1 Interviewer | 2 Gast | 3–4 sony | 5–6 gopro`. Mono pro Quelle; mit `--full_quality_audio` bleibt Kameraton Stereo (zwei Kanäle).

### Bild (`merge` / `sync-to-audio`)

| Situation | Darstellung |
|---|---|
| Nur Hauptkamera läuft | vollformatig |
| Nur Bild-im-Bild-Kamera läuft | vollformatig |
| Beide laufen | Hauptkamera vollformatig, zweite klein unten rechts |
| Keine Kamera, aber Ton | schwarz |
| Herausgeschnittene Pause | 2 s schwarz |

### Einblendungen (`--info`)

- **Oben mittig:** Position im fertigen Video (nicht bei `sync-to-video`).
- **Oben links/rechts, weitere darunter:** pro Tonquelle die gerade laufende Datei und die Position **in dieser Originaldatei**. Wechselt die Datei (DJI-Split), wechselt auch die Anzeige.
- **Unten links:** Kameradatei und Position im Clip; im Bild-im-Bild entsprechend.
- Schwarzbild: „kein Videomaterial“ bzw. „Pause herausgeschnitten“ mit Uhrzeit und Länge.

---

## In DaVinci Resolve importieren

Resolve liest mehrere separate Tonspuren aus ffmpeg-Dateien oft nicht vollständig. Deshalb gibt es standardmäßig eine Spur mit mehreren Kanälen. So bekommst du einzelne Spuren:

1. Datei in den Media Pool importieren, **noch nicht** in eine Timeline ziehen.
2. Rechtsklick → *Clip Attributes* → Tab *Audio*.
3. Oben *Format: Mono*, *Tracks* = Anzahl der Kanäle, **Add**.
4. Zeile 1 → Embedded Channel 1, Zeile 2 → Channel 2, usw.
5. OK → Rechtsklick auf den Clip → *Create New Timeline Using Selected Clips*.

> Änderungen in den Clip Attributes gelten nur für Clips, die **danach** in eine Timeline gezogen werden. Liegt der Clip bereits in einer Timeline, ist oben im Dialog alles ausgegraut.

---

## Abbrechen und Fortsetzen

Jeder Lauf arbeitet in einem festen Ordner `imerge_<Name>` im Ausgabeordner. Bei einem Abbruch (Strg+C, Terminal geschlossen, Ruhezustand, Platte getrennt) bleibt er liegen. **Denselben Befehl nochmal starten** genügt:

- Analyse, Sync und Tonspuren werden übernommen, solange Quelldateien und Einstellungen gleich sind.
- `merge`/`sync-to-audio`: Fertige Segmente werden übernommen (Fingerabdruck aus Zeitbereich, Clips, Positionen, Einblendungen, Encoder).
- `sync-to-video`: Fertige Clip-Dateien werden übersprungen; ein halb fertiger Clip wird neu gebaut.
- Halb geschriebene Dateien (`.part.mov`) werden nie übernommen. Nach erfolgreichem Abschluss wird der Arbeitsordner gelöscht.
- Zweimal derselbe Befehl gleichzeitig wird verhindert (Sperrdatei).
- Scheitert das Hardware-Decoding bei einem Clip, wird er automatisch per Software decodiert.

Damit der Mac bei langen Läufen nicht schläft: `caffeinate -i python3 interview_merge.py …`

---

## Der Report

```
C0047.MP4   18:44:31  @ 00:33:50.12  Dauer 00:46:49.92  [Sony-XML]  Grob -51.1s; Fein -0.325s, Drift -4 ppm (12 Messpunkte)
```

| Teil | Bedeutung |
|---|---|
| `18:44:31` | Echte Uhrzeit des Clipstarts nach dem Sync |
| `@ 00:33:50.12` | Position in der Timeline |
| `[Sony-XML]` | Woher die ursprüngliche Startzeit kam |
| `Grob -51.1s` | Uhrzeitfehler der ganzen Quelle (hier: Sony-Uhr 51 s vor) |
| `Fein -0.325s` | Feinkorrektur dieses Clips |
| `Drift -4 ppm` | Gangabweichung gegenüber der Referenz; bis ca. ±100 ppm normal |
| `Messungen widersprüchlich` / `keine sichere Messung` | Ton nicht eindeutig zuordenbar; der Clip übernimmt den Median der anderen Clips derselben Quelle |

In der Terminalausgabe zeigt die Grobsuche pro Quelle Kandidaten und den **Abstand** (Eindeutigkeit). Ab `coarse_min_ratio` (1,5×) wird übernommen; sonst greift die mittlere Suche:

```
Mittlere Suche: Versatz -150.00s, 6 von 6 Ausschnitten stimmen überein
```

---

## Konfiguration (`config.json`)

Liegt neben dem Skript. Es müssen nur abweichende Werte drinstehen.

### Zeitzone

| Schlüssel | Standard | Bedeutung |
|---|---|---|
| `timezone` | `null` | Zeitzone der **Geräte-Uhren**, z.B. `"Europe/Berlin"`. Zeitstempel mit Zonenangabe werden darin umgerechnet. Bei `null` gilt die Zeitzone des Rechners – falsch, wenn der Rechner woanders steht als beim Dreh. |

### `defaults` und `sources`

`defaults.audio` gilt für alle `--audio`-Quellen, `defaults.video` für `--video`/`--pip`. Unter `sources` kann man einzelne Quellen über ihren Anzeigenamen (Ordnername oder `NAME=`) überschreiben, z.B.:

```json
"sources": { "gopro": { "offset_s": -3.0 } }
```

| Schlüssel | Bedeutung |
|---|---|
| `offset_s` | Manuelle Korrektur in Sekunden (positiv = später). Für Fälle, in denen der automatische Sync nichts findet; auf wenige Sekunden genau reicht. |
| `tags_are_utc` | `creation_time` ist echte UTC (`true`, Sony, die eingesetzte GoPro) oder nur Ortszeit mit falschem „Z“ (`false`). |
| `use_timecode` | Nur Video: Uhrzeit aus dem Timecode nehmen (bei Time-of-Day-Timecode). |

### `output`

| Schlüssel | Standard | Bedeutung |
|---|---|---|
| `width` / `height` / `fps` | `1920` / `1080` / `25` | Auflösung und Bildrate (ohne `--full_quality_video`) |
| `vcodec` | `"auto"` | `"auto"`: schnellster funktionierender Hardware-Encoder (Mac `hevc_videotoolbox`, Windows `hevc_nvenc`/`qsv`/`amf`), sonst x265. Fest wählbar: `"x265"`, `"hevc_videotoolbox"`, `"hevc_nvenc"`, `"hevc_qsv"`, `"hevc_amf"`, `"libx264"`, `"h264_videotoolbox"`, `"prores"` |
| `crf` | `null` | Qualität bei x265/x264 (`null` = 28 bzw. 23; mit `--full_quality_video` 18) |
| `preset` | `"fast"` | Geschwindigkeit bei x265/x264 |
| `bitrate` | `"3M"` | Bitrate der Hardware-Encoder (mit `--full_quality_video` automatisch passend zur Auflösung erhöht) |
| `hwaccel` | `"auto"` | Quellvideos per Hardware decodieren; `"none"` = aus |
| `jobs` | `"auto"` | Gleichzeitig gerenderte Segmente/Clips (`auto` = 3 bei Hardware-Encoder, 1 bei x265) |
| `audio_rate` | `48000` | Abtastrate |
| `aac_per_channel` | `"96k"` | AAC-Bitrate pro Kanal (ohne `--full_quality_audio`) |
| `audio_tracks` | `"single"` | `"single"`: eine Spur mit allen Kanälen (Resolve-sicher); `"separate"`: einzelne Spuren |

### `join_tolerance_s`

Standard `3.0`. Tondateien, deren Start höchstens so weit vom Ende der vorherigen entfernt ist, werden lückenlos angehängt.

### `cut_gaps` (`merge`, `sync-to-audio`)

| Schlüssel | Standard | Bedeutung |
|---|---|---|
| `enabled` | `true` | Pausen ohne Bild und Ton herausschneiden |
| `min_gap_s` | `10` | Ab dieser Länge wird geschnitten |
| `keep_s` | `2` | So viele Sekunden Schwarzbild bleiben stehen |

### `audio_processing` (aus bei `--full_quality_audio`)

| Schlüssel | Standard | Bedeutung |
|---|---|---|
| `enabled` | `true` | Aufbereitung an/aus |
| `highpass_hz` | `90` | Rumpeln/Wind/Netzbrummen entfernen; `0` = aus |
| `denoise` | `"afftdn"` | `"afftdn"`, `"rnnoise"` (braucht `rnnoise_model`) oder `"none"` |
| `denoise_db` | `12` | Stärke: 6 leicht, 12 mittel, 20 stark |
| `rnnoise_model` | `null` | Pfad zu einer `.rnnn`-Datei (z.B. aus `richardpl/arnndn-models`) |
| `normalize` | `true` | Dynamische Lautstärkeanpassung |
| `max_gain_db` | `20` | Maximale Anhebung leiser Stellen |
| `target_rms` | `0.12` | Ziel-Lautheit (≈ −18 dBFS) |

### `pip`, `text`

| Schlüssel | Standard | Bedeutung |
|---|---|---|
| `pip.width_fraction` | `0.3` | Breite des Bild-im-Bilds relativ zur Bildbreite |
| `pip.margin` | `40` | Abstand zum Rand (bei 1080p) |
| `text.fontfile` | `null` | Schriftdatei; `null` = automatisch |
| `text.size` | `34` | Schriftgröße bei 1080p, skaliert mit der Auflösung |

### `audio_sync`

| Schlüssel | Standard | Bedeutung |
|---|---|---|
| `enabled` | `true` | Audio-Sync an/aus (aus = nur Uhrzeiten) |
| `audio_to_audio` | `true` | Weitere Tonquellen auf die erste synchronisieren |
| `coarse` | `true` | Grobsuche über die ganze Aufnahme |
| `coarse_min_ratio` | `1.5` | Mindest-Eindeutigkeit der Grobsuche |
| `coarse_min_overlap_s` | `120` | Mindestüberlappung für einen Treffer |
| `coarse_local_s` | `300` | Zusätzlich nur ±so viele Sekunden um die Geräte-Uhrzeit suchen, wenn global nicht eindeutig |
| `medium` | `true` | Mittlere Suche (Wellenform-Ausschnitte) |
| `medium_window_s` | `300` | Suchbereich der mittleren Suche (±s) |
| `medium_excerpts` / `medium_excerpt_s` | `6` / `60` | Anzahl und Länge der Ausschnitte |
| `search_s` | `10` | Suchbereich der Feinsuche (±s) |
| `analyze_s` / `window_step_s` | `30` / `240` | Länge und Abstand der Feinmessungen |
| `min_confidence` | `8` | Mindest-Konfidenz einer Messung |
| `drift_min_span_s` | `600` | Drift wird nur aus Clips berechnet, deren Messpunkte mindestens so weit auseinanderliegen |

---

## Installation von Hand (macOS)

Was `setup.sh` automatisch erledigt:

```bash
brew install ffmpeg-full                                  # ffmpeg mit Texten (drawtext)
export PATH="$(brew --prefix ffmpeg-full)/bin:$PATH"      # dauerhaft: in ~/.zshrc eintragen
python3 -m venv .venv && source .venv/bin/activate
pip install numpy tzdata
```

Prüfen: `ffmpeg -hide_banner -filters | grep drawtext` muss eine Zeile liefern. Die Python-Umgebung verlässt du mit `deactivate`.

---

## Fehlerbehebung

| Problem | Ursache / Lösung |
|---|---|
| `ffmpeg ohne drawtext – Einblendungen nicht möglich` | Es läuft das Standard-ffmpeg: `source setup.sh` (macOS). Betrifft nur `--info`. |
| `numpy fehlt` | Terminal nicht vorbereitet: `source setup.sh`, `setup` (cmd) bzw. `. .\setup.ps1`. |
| Eine Quelle ist um Stunden verschoben | Falsche `timezone` oder `tags_are_utc` passt bei dieser Quelle nicht. Mit `--dry-run` prüfen. |
| „kein eindeutiger Treffer“ und die Ausschnitte der mittleren Suche zeigen verschiedene Werte | Ton der Quelle zu schlecht. Versatz von Hand bestimmen (z.B. Klatschen) und als `offset_s` unter `sources` eintragen. |
| Timeline viel zu lang | Versehentliche Aufnahmen weit außerhalb des Drehs aus den Ordnern nehmen. |
| Resolve zeigt zu wenige Tonspuren | Siehe „In DaVinci Resolve importieren“. |
| Ton klingt blechern | Rauschunterdrückung zu stark: `denoise_db` senken, `"denoise": "none"` oder `--full_quality_audio`. |
| Rendern dauert lange | In der Ausgabe muss `Video-Encoder: hevc_videotoolbox` (bzw. ein anderer Hardware-Encoder) stehen. Für `sync-to-video` ist `--full_quality_video` ohne `--info` am schnellsten (keine Neukodierung). |
| „Hardware-Decoding fehlgeschlagen … wiederhole mit Software-Decoding“ | Harmlos, z.B. bei sehr kurzen Fehlstart-Clips. |
| Mac lädt beim Rendern kaum | Netzteil hat sich nur auf wenige Watt geeinigt: `system_profiler SPPowerDataType \| grep Wattage`; Stecker an beiden Enden neu einstecken. |
| Windows cmd: „Sprungmarke nicht gefunden“ | `setup.bat` hat falsche Zeilenenden; die `.gitattributes` verhindert das bei Git. |
| Windows: „Ausführung von Skripts ist deaktiviert“ | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, ggf. `Unblock-File .\setup.ps1`. |