# interview_merge.py

Baut aus mehreren Aufnahmegeräten eines Interviews automatisch **ein durchgehendes, synchrones Video** – gedacht als Sichtungs- und Transkriptionsgrundlage.

Quellen:

- **Lav Interviewer** – DJI Mic 2 (WAV)
- **Lav Befragter** – DJI Mic 2 (WAV)
- **Sony** – Kameraclips mit Ton (MP4 + XML-Sidecar)
- **GoPro** – Kameraclips mit Ton (MP4, Kapitel)

Ergebnis ist eine einzige `.mov`-Datei mit Bild, allen Tonquellen als getrennten Kanälen und eingeblendeten Dateinamen und Timecodes.

---

## Was das Skript macht

1. **Startzeiten erkennen** für jede Datei, aus Sony-XML, Broadcast-WAV-Metadaten, `creation_time`, der Uhrzeit im Dateinamen oder notfalls dem Dateidatum.
2. **Geteilte Dateien zusammensetzen.** DJI-Mic-Aufnahmen, die automatisch in 30-Minuten-Stücke geteilt wurden, werden lückenlos aneinandergehängt. GoPro-Kapitel (`GX01…`, `GX02…`) ebenso.
3. **Über den Ton synchronisieren:**
   - Lav 2 wird auf Lav 1 abgeglichen, über das Übersprechen zwischen den Mikros.
   - **Grobsuche:** Die komplette Aufnahme jeder Kamera wird gegen die kompletten Lavs abgeglichen. Das findet auch Uhrzeitfehler von Minuten oder Stunden, etwa falsche Zeitzone oder eine falsch gestellte Kamera-Uhr.
   - **Feinsuche:** Jeder Clip wird an mehreren Stellen auf Millisekunden genau abgeglichen. Dabei wird auch die **Drift** gemessen und korrigiert, also dass Geräte-Uhren minimal unterschiedlich schnell laufen.
4. **Pausen herausschneiden,** in denen weder Bild noch Ton aufgenommen wurde.
5. **Ton für Transkription aufbereiten:** Hochpass gegen Rumpeln und Brummen, Rauschunterdrückung und dynamische Lautstärkeanpassung, damit leise und laute Sprecher gleich laut sind. Das passiert erst nach dem Sync und beeinflusst ihn nicht.
6. **Rendern.** Je nach Situation ist Sony vollformatig, GoPro vollformatig, GoPro als Bild-im-Bild über der Sony, oder Schwarzbild. Dazu kommen alle Einblendungen.

---

## Schnellstart

### macOS

In jedem neuen Terminal, im Projektordner:

```bash
source setup.sh
python3 interview_merge.py . --dry-run    # Zeiten prüfen
python3 interview_merge.py .              # rendern
```

`setup.sh` prüft und installiert bei Bedarf alles Nötige: Homebrew-Check, `ffmpeg-full`, Python-Umgebung `.venv` mit numpy und Zeitzonendaten. Danach setzt es PATH und Umgebung für **dieses** Terminalfenster und zeigt, welche Quellordner gefunden wurden. Es muss mit `source` gestartet werden, sonst gelten die Einstellungen nicht im Terminal.

Die Abschnitte unter „Installation von Hand (macOS)“ beschreiben, was `setup.sh` macht, falls du es von Hand einrichten willst.

### Windows (Eingabeaufforderung / cmd)

In jedem neuen **cmd**-Fenster, im Projektordner:

```bat
setup
python interview_merge.py . --dry-run    # Zeiten prüfen
python interview_merge.py .              # rendern
```

`setup.bat` macht dasselbe wie die PowerShell-Variante unten, braucht aber keine Freigabe für Skripte. **Nicht per Doppelklick starten**: Dann öffnet sich ein eigenes Fenster, das sich danach schließt, und die Einstellungen sind weg.

cmd im Projektordner öffnen: im Explorer in die Adresszeile `cmd` tippen und Enter drücken.

### Windows (PowerShell)

In jedem neuen **PowerShell**-Fenster, im Projektordner:

```powershell
. .\setup.ps1
python interview_merge.py . --dry-run    # Zeiten prüfen
python interview_merge.py .              # rendern
```

Wichtig ist der **Punkt mit Leerzeichen** vor `.\setup.ps1`. Nur so gelten PATH und Python-Umgebung danach im Fenster.

`setup.ps1` installiert bei Bedarf über `winget`:
- **ffmpeg** (Gyan-Build `Gyan.FFmpeg`, enthält Texte/drawtext, x265 und die Tonfilter)
- **Python 3.12**, falls kein Python 3.9+ vorhanden ist
- die Python-Umgebung `.venv` mit numpy und tzdata (Zeitzonendaten, die Windows-Python sonst fehlen)

Danach zeigt es, welche Quellordner gefunden wurden und welche Hardware-Encoder ffmpeg kennt.

**Einmalig vorab**, falls PowerShell meldet, dass Skripte deaktiviert sind:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

Hast du `setup.ps1` aus dem Internet heruntergeladen statt per Git geholt, blockiert Windows es zusätzlich. Dann einmal `Unblock-File .\setup.ps1` ausführen.

**Unterschiede zu macOS:**
- Der Befehl heißt `python` statt `python3`.
- Pfade schreibst du wie gewohnt mit Laufwerksbuchstaben, in Anführungszeichen, wenn Leerzeichen vorkommen: `python interview_merge.py "E:\Porsche\Interview 1" -o "E:\Export" -n "Interview 1"`.
- Statt `hevc_videotoolbox` gibt es schnelle Hardware-Encoder je nach Grafikkarte: `"vcodec": "hevc_nvenc"` (NVIDIA), `"hevc_qsv"` (Intel) oder `"hevc_amf"` (AMD). Die Bitrate stellst du über `"bitrate"` ein.

## Installation von Hand (macOS)

### 1. ffmpeg mit Textfunktion

Das Standard-`ffmpeg` von Homebrew kann keine Texte einblenden. Du brauchst `ffmpeg-full`:

```bash
brew install ffmpeg-full
```

`ffmpeg-full` ist „keg-only“, muss also in den PATH. Dauerhaft geht das so:

```bash
echo 'export PATH="$(brew --prefix ffmpeg-full)/bin:$PATH"' >> ~/.zshrc
```

Danach Terminal neu öffnen. Prüfen:

```bash
ffmpeg -hide_banner -filters | grep drawtext
```

Es muss eine Zeile mit `drawtext` erscheinen.

> Falls danach ein anderes ffmpeg mit `Library not loaded` abbricht: `brew reinstall ffmpeg`.

### 2. Python-Umgebung mit numpy

numpy wird für den Audio-Sync gebraucht. Einmalig im Projektordner:

```bash
cd ~/Programming/Vodeo-Transcript-Creator
python3 -m venv .venv
source .venv/bin/activate
pip install numpy
```

Bei jedem neuen Terminal vor dem Start:

```bash
source .venv/bin/activate
```

---

## Ordnerstruktur

```
projekt/
├── interview_merge.py
├── setup.sh                 macOS: Terminal vorbereiten (source setup.sh)
├── setup.ps1                Windows: PowerShell vorbereiten (. .\setup.ps1)
├── setup.bat                Windows: cmd vorbereiten (setup)
├── config.json              (optional, aber empfohlen)
├── lav_interviewer/         DJI Mic 2 – Interviewer
├── lav_befragter/           DJI Mic 2 – Befragte*r
├── sony/                    Sony-Clips (.MP4 + ...M01.XML)
└── gopro/                   GoPro-Clips (.MP4)
```

- Die Sony-XML-Dateien (`C0016M01.XML`) **mitkopieren**. Sie enthalten die genaueste Startzeit inklusive Zeitzone.
- `.LRV`- und `.THM`-Dateien der GoPro werden automatisch ignoriert.
- **Versehentliche Aufnahmen** gehören nicht in die Ordner, z.B. ein 18-Sekunden-Clip beim Einpacken Stunden später. Sie verlängern sonst die Timeline.
- Fehlt ein Ordner, bleibt die entsprechende Spur einfach leer bzw. stumm.

---

## Benutzung

```bash
source .venv/bin/activate
python3 interview_merge.py .                # rendern (Projekt = aktueller Ordner)
python3 interview_merge.py . --dry-run      # nur Zeiten + Timeline anzeigen, nichts rendern
python3 interview_merge.py /pfad/projekt    # anderes Projekt
```

| Option | Wirkung |
|---|---|
| `--dry-run` | Erkennt Zeiten, zeigt Timeline und Schnitte. Kein Sync, kein Rendern, dauert Sekunden. |
| `--no-sync` | Kein Audio-Sync. Clips liegen nur nach erkannter Uhrzeit. |
| `--no-cut` | Pausen ohne Bild und Ton **nicht** herausschneiden. |
| `--config PFAD` | Andere Config-Datei verwenden (Standard: `projekt/config.json`). |
| `--raw-audio` | Ton **nicht** aufbereiten, Originalpegel behalten. |
| `-n NAME`, `--name NAME` | Dateiname des Videos, z.B. `-n interview_kim`. Endung `.mov` wird ergänzt, falls sie fehlt. Der Report heißt dann `interview_kim_report.txt`. Standard: `output.file` aus der Config. |
| `-o PFAD`, `--output PFAD` | Ausgabeordner für Video, Report und Zwischendateien. Wird angelegt, falls er fehlt. Standard: Projektordner. |
| `--keep-temp` | Zwischendateien (WAVs, Segmente) nicht löschen, zur Fehlersuche. |

**Empfohlener Ablauf:** Erst `--dry-run` und prüfen, ob die Uhrzeiten grob plausibel sind. Danach richtig rendern.

---

## Ergebnis

### Datei

- `interview_merged.mov`: das Video
- `interview_merge_report.txt`: Report mit allen Dateien, erkannten Zeiten und Sync-Korrekturen

### Bild

| Situation | Darstellung |
|---|---|
| Nur Sony läuft | Sony vollformatig |
| Nur GoPro läuft | GoPro vollformatig |
| Beide laufen | Sony vollformatig, GoPro klein unten rechts |
| Keine Kamera, aber Ton | Schwarz, „kein Videomaterial“ |
| Herausgeschnittene Pause | 2 s Schwarz mit Uhrzeit und Länge der Pause |

### Einblendungen

```
+-----------------------------------------------------------------+
| Lav Interviewer: DJI_…   00:41:12.480    Lav Befragter: DJI_…   |
| 00:12:34.567                                    00:12:30.120    |
|                                                                 |
|                                             +---------------+   |
| C0016.MP4                                   | GX030055.MP4  |   |
| 00:33:50.120                                | 00:05:12.000  |   |
+---------------------------------------------+---------------+---+
```

- **Oben mittig:** Position im fertigen Video. Sie passt zum Player und fürs Transkript.
- **Oben links/rechts:** Lav-Datei und Position **in dieser Originaldatei**.
- **Unten links:** Kameradatei und Position im Clip.
- **Bild-im-Bild:** GoPro-Datei und Position im Clip.

Über die Positionen findest du jede Stelle direkt im Originalmaterial wieder.

### Ton

Standard (`"audio_tracks": "single"`): **eine Tonspur mit 4 Kanälen**.

| Kanal | Inhalt |
|---|---|
| 1 | Lav Interviewer |
| 2 | Lav Befragter |
| 3 | Sony-Kameraton |
| 4 | GoPro-Kameraton |

---

## In DaVinci Resolve importieren

Resolve liest mehrere separate Tonspuren aus ffmpeg-Dateien oft nicht vollständig. Deshalb gibt es standardmäßig eine Spur mit 4 Kanälen. So bekommst du 4 einzelne Spuren:

1. Datei in den Media Pool importieren, **noch nicht** in eine Timeline ziehen.
2. Rechtsklick → *Clip Attributes* → Tab *Audio*.
3. Oben *Format: Mono*, *Tracks: 4*, **Add**, falls noch nicht 4 Zeilen da sind.
4. Zeile 1 → Embedded Channel 1, Zeile 2 → Channel 2, usw.
5. OK → Rechtsklick auf den Clip → *Create New Timeline Using Selected Clips*.

> Änderungen in den Clip Attributes gelten nur für Clips, die **danach** in eine Timeline gezogen werden. Clips, die schon in einer Timeline liegen, behalten die alte Belegung. Liegt der Clip bereits in einer Timeline, ist oben im Dialog alles ausgegraut.

---

## Der Report

Beispielzeile:

```
C0016.MP4   18:44:31  @ 00:33:50.12  Dauer 01:12:18.24  [Sony-XML]  Grob -50.7s; Fein +0.213s, Drift +12 ppm (17 Messpunkte)
```

| Teil | Bedeutung |
|---|---|
| `18:44:31` | Echte Uhrzeit des Clipstarts nach dem Sync |
| `@ 00:33:50.12` | Position im fertigen Video |
| `[Sony-XML]` | Woher die ursprüngliche Startzeit kam |
| `Grob -50.7s` | Uhrzeitfehler der ganzen Kamera (hier: Sony-Uhr 51 s vor) |
| `Fein +0.213s` | Feinkorrektur dieses Clips |
| `Drift +12 ppm` | Gangabweichung der Kamera-Uhr gegenüber den Lavs. Bis ca. ±100 ppm ist normal. |
| `keine sichere Messung` | Ton nicht eindeutig zuordenbar, z.B. nur Wind oder Stille. Der Clip übernimmt den Mittelwert der anderen Clips derselben Kamera. |

Bei der Grobsuche steht in der Terminalausgabe pro Kamera:

```
Versatz +7200.0s (02:00:00.00), Treffer 0.45, Abstand 3.20x, Überlappung 00:22:00
```

**Abstand** ist die Eindeutigkeit, also wie viel besser der beste Treffer als der zweitbeste ist. Ab `coarse_min_ratio` (Standard 1,5×) wird der Versatz übernommen.

Oben im Report stehen außerdem alle herausgeschnittenen Pausen mit Uhrzeit und Länge.

---

## Konfiguration (`config.json`)

Die Datei liegt im Projektordner. Es müssen nur die Werte drinstehen, die vom Standard abweichen. Fehlende Werte nimmt das Skript aus seinen eingebauten Standards. Eine vollständige Beispiel-Config liegt bei.

### Zeitzone

| Schlüssel | Standard | Bedeutung |
|---|---|---|
| `timezone` | `null` | Zeitzone, auf der die **Geräte-Uhren** laufen, z.B. `"Europe/Berlin"`. Zeitstempel mit Zonenangabe (Sony-XML, UTC-Tags) werden in diese Zone umgerechnet. Bei `null` gilt die Zeitzone des Rechners, und das geht schief, wenn der Rechner woanders steht als beim Dreh. |

### `folders`

Namen der vier Unterordner, falls du sie anders nennen willst.

### `track_names`

Bezeichnungen der Tonspuren bzw. Kanäle, auch für die Einblendungen der Lavs.

### `output`

| Schlüssel | Standard | Bedeutung |
|---|---|---|
| `file` | `interview_merged.mov` | Name der Ausgabedatei |
| `dir` | `null` | Ausgabeordner (`null` = Projektordner). `--output` hat Vorrang. |
| `width` / `height` | `1920` / `1080` | Auflösung. `1280` / `720` halbiert ungefähr die Dateigröße. |
| `fps` | `25` | Bildrate der Ausgabe |
| `vcodec` | `"x265"` | `"x265"`: klein bei guter Qualität (Standard)<br>`"hevc_videotoolbox"`: Mac-Hardware, deutlich schneller, etwas größer<br>`"hevc_nvenc"` / `"hevc_qsv"` / `"hevc_amf"`: Windows-Hardware (NVIDIA / Intel / AMD), deutlich schneller<br>`"libx264"`: H.264<br>`"h264_videotoolbox"`: H.264 per Hardware<br>`"prores"`: ProRes Proxy, groß, ideal zum Weiterschneiden |
| `crf` | `null` | Qualität bei x265/x264. `null` = Standard (x265: 28, x264: 23). Höher heißt kleiner und schlechter; ab ca. 32 sichtbar. |
| `preset` | `"medium"` | Encoder-Geschwindigkeit bei x265/x264: `ultrafast` … `slow`. Langsamer heißt kleiner. |
| `bitrate` | `"3M"` | Nur für die Hardware-Codecs (`*_videotoolbox`, `*_nvenc`, `*_qsv`, `*_amf`) |
| `audio_rate` | `48000` | Abtastrate des Tons |
| `audio_codec` | `"aac"` | `"aac"` (klein) oder `"pcm"` (verlustfrei, ca. 10 GB pro 3 h zusätzlich) |
| `audio_tracks` | `"single"` | `"single"`: 1 Spur mit 4 Kanälen (Resolve-sicher)<br>`"separate"`: 4 einzelne Spuren |
| `aac_single` | `"384k"` | AAC-Bitrate bei `single` |
| `aac_mono` / `aac_stereo` | `"96k"` / `"160k"` | AAC-Bitraten pro Spur bei `separate` |

### `sources`

Pro Quelle (`lav_interviewer`, `lav_befragter`, `sony`, `gopro`):

| Schlüssel | Bedeutung |
|---|---|
| `offset_s` | Manuelle Korrektur in Sekunden. Positiv heißt später. Für Fälle, in denen der automatische Sync nichts findet. Liegt der Wert auf wenige Sekunden genau, erledigt der Feinsync den Rest. |
| `tags_are_utc` | Ob der `creation_time`-Tag echte UTC ist (`true`, z.B. Sony) oder nur die Ortszeit mit falschem „Z“ (`false`). Standard ist überall `true`; die eingesetzte GoPro schreibt nachweislich echte UTC. Ist eine Kamera um genau die Zeitzonendifferenz verschoben, diesen Wert umdrehen. Die Grobsuche korrigiert das aber meist automatisch. |
| `channels` | `"mono"` oder `"stereo"`. Bei `"single"` wird ohnehin jede Quelle mono. |
| `use_timecode` | Nur Sony: Uhrzeit aus dem Timecode nehmen. Nur sinnvoll, wenn die Kamera auf Time-of-Day-Timecode steht. |

### `join_tolerance_s`

Standard `3.0`. Lav-Dateien, deren Start höchstens so weit vom Ende der vorherigen entfernt ist, werden lückenlos angehängt. Gilt nur für die Lavs, nicht für Kameras.

### `cut_gaps`

| Schlüssel | Standard | Bedeutung |
|---|---|---|
| `enabled` | `true` | Pausen ohne Bild und Ton herausschneiden |
| `min_gap_s` | `10` | Ab dieser Pausenlänge wird geschnitten |
| `keep_s` | `2` | So viele Sekunden Schwarzbild mit Hinweis bleiben stehen |

### `audio_processing`

Aufbereitung der Tonspuren für automatische Transkription. Sie wird nach dem Sync angewendet; mit `--raw-audio` wird sie für einen Lauf übersprungen.

| Schlüssel | Standard | Bedeutung |
|---|---|---|
| `enabled` | `true` | Aufbereitung an/aus |
| `apply_to` | alle vier | Welche Quellen aufbereitet werden |
| `highpass_hz` | `90` | Entfernt alles unterhalb, z.B. Rumpeln, Wind, Netzbrummen. `0` = aus. |
| `denoise` | `"afftdn"` | `"afftdn"`: lernt das Grundrauschen laufend und zieht es ab<br>`"rnnoise"`: neuronales Netz, braucht `rnnoise_model`<br>`"none"`: aus |
| `denoise_db` | `12` | Stärke von afftdn: 6 leicht, 12 mittel, 20 stark. Zu stark erzeugt Artefakte und kann die Transkription verschlechtern. |
| `rnnoise_model` | `null` | Pfad zu einer `.rnnn`-Modelldatei für `"rnnoise"` |
| `normalize` | `true` | Dynamische Lautstärkeanpassung (dynaudnorm, RMS-basiert) |
| `max_gain_db` | `20` | Maximale Anhebung leiser Stellen |
| `target_rms` | `0.12` | Ziel-Lautheit (≈ −18 dBFS) |

> Tipp: Bei sehr ruhiger Umgebung reicht `"denoise_db": 6`. Bei Straßenlärm oder Wind kann `"rnnoise"` besser sein; Modelle gibt es z.B. im GitHub-Repo `richardpl/arnndn-models`.

### `pip`

| Schlüssel | Standard | Bedeutung |
|---|---|---|
| `width_fraction` | `0.3` | Breite des GoPro-Bildes relativ zur Bildbreite |
| `margin` | `40` | Abstand zum Bildrand in Pixeln (bei 1080p) |

### `text`

| Schlüssel | Standard | Bedeutung |
|---|---|---|
| `fontfile` | `null` | Pfad zu einer Schrift (.ttf/.ttc). `null` heißt automatisch, z.B. Arial oder Helvetica. |
| `size` | `34` | Schriftgröße bei 1080p. Sie skaliert mit der Auflösung. |

### `audio_sync`

| Schlüssel | Standard | Bedeutung |
|---|---|---|
| `enabled` | `true` | Audio-Sync an/aus |
| `lav_to_lav` | `true` | Lav 2 auf Lav 1 synchronisieren |
| `coarse` | `true` | Grobsuche über die ganze Aufnahme (findet große Uhrzeitfehler) |
| `coarse_min_ratio` | `1.5` | Mindest-Eindeutigkeit der Grobsuche. Niedriger übernimmt mehr, aber riskanter. |
| `coarse_min_overlap_s` | `120` | Mindestüberlappung zwischen Kamera und Lavs für einen Treffer |
| `coarse_local_s` | `300` | Ist die globale Suche nicht eindeutig, wird zusätzlich nur in diesem Fenster (±Sekunden) um die Kamera-Uhrzeit gesucht. Setzt voraus, dass Zeitzone und `tags_are_utc` stimmen. `0` = aus. |
| `search_s` | `10` | Suchbereich der Feinsuche (±Sekunden) |
| `analyze_s` | `30` | Länge eines Messfensters |
| `window_step_s` | `240` | Abstand der Messfenster im Clip |
| `min_confidence` | `8` | Mindest-Konfidenz eines Feinsync-Messpunkts |
| `drift_min_span_s` | `600` | Drift wird nur aus Clips berechnet, deren Messpunkte mindestens so weit auseinanderliegen. Kürzere Clips übernehmen die Drift ihrer Kamera. |

---

## Fehlerbehebung

| Problem | Ursache / Lösung |
|---|---|
| `ffmpeg ohne drawtext – Dateinamen werden NICHT eingeblendet` | Es läuft das Standard-ffmpeg. `source setup.sh` ausführen. |
| `numpy fehlt` | Terminal nicht vorbereitet: `source setup.sh` (macOS), `setup` (Windows cmd) bzw. `. .\setup.ps1` (Windows PowerShell). |
| Windows cmd: Fehler wie „Sprungmarke nicht gefunden“ | `setup.bat` hat Mac-/Linux-Zeilenenden bekommen. Die beiliegende `.gitattributes` verhindert das bei Git; sonst die Datei in einem Editor mit Windows-Zeilenenden (CRLF) speichern. |
| Windows: „Ausführung von Skripts ist auf diesem System deaktiviert“ | Einmalig `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, ggf. `Unblock-File .\setup.ps1`. |
| Windows: `python` öffnet den Microsoft Store | Das ist nur ein Platzhalter. `. .\setup.ps1` installiert echtes Python; danach PowerShell neu öffnen. |
| Eine Kamera ist um mehrere Stunden verschoben | Falsche `timezone`, oder `tags_are_utc` passt bei dieser Kamera nicht. Mit `--dry-run` prüfen. |
| Kamera um feste Sekunden/Minuten daneben, Grobsuche sagt „kein eindeutiger Treffer“ | Kamera-Ton zu schlecht (Gehäuse, Wind). Versatz von Hand bestimmen, z.B. über ein Klatschen, und als `offset_s` eintragen. |
| Timeline viel zu lang | Versehentliche Aufnahmen weit außerhalb des Drehs (z.B. eine kurze DJI-Datei Stunden später) aus den Ordnern nehmen. Pausen dazwischen werden sonst geschnitten, aber die Datei selbst bleibt drin. |
| Resolve zeigt nur 2 Tonspuren | Siehe Abschnitt „In DaVinci Resolve importieren“. |
| Anfang des Videos ist stumm | Normal, wenn eine Kamera schon vor den Lavs lief. Der Lav-Ton beginnt erst später. |
| Ton klingt „blechern“ oder verwaschen | Rauschunterdrückung zu stark: `denoise_db` senken oder `"denoise": "none"`. |
| Rendern dauert sehr lange | `"vcodec": "hevc_videotoolbox"` oder `"preset": "fast"` setzen, ggf. 720p. |