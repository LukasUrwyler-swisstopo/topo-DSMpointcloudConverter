"""
_osgeo_runner.py - Wird via OSGeo4W/QGIS Python aufgerufen (NICHT direkt starten).
Liest Parameter aus einer JSON-Datei. Punktwolken-Operationen laufen via PDAL-CLI
(pdal.exe als Subprocess), das Grid-Shape wird mit OGR gelesen.
Ausgabe geht auf stdout -> wird vom GUI live im Log angezeigt.

Aktionen:
    info    - Input-Ordner analysieren (Format, ASCII-Vorschau, Extent, CRS-Plausibilitaet),
              Ergebnis als JSON-Zeile auf stdout
    process - ASCII (xyz) oder LAZ/LAS (beliebiges Tiling / gemergt) -> LAZ/LAS-Kacheln gemaess
              Grid-Shape (TileKey aus Attributfeld 'NAME'), CRS-Tag nach Auswahl, optional
              Thinning und DSM-Raster + Hillshade, siehe Kommentar ueber _process()
    raster  - ASCII-DSM eines Gebiets (ESRI-ASCII-Grid, XYZ-Gitter oder XYZ-Punkte) direkt ->
              DSM-Raster + Hillshade fuer den GDWH-Import SB_DSM, ohne Kacheln,
              siehe Kommentar ueber _process_raster()
"""

import base64
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ASCII_EXTENSIONS = (".xyz", ".txt", ".asc", ".ascii", ".csv")
LAS_EXTENSIONS   = (".laz", ".las")
# Kopf eines ESRI-ASCII-Grids (AAIGrid, Gross-/Kleinschreibung egal)
ESRI_HEADER_KEYS = ("ncols", "nrows", "xllcorner", "xllcenter", "yllcorner", "yllcenter",
                    "cellsize", "dx", "dy", "nodata_value")

# Trennzeichen: GUI-Schluessel -> Zeichen fuer readers.text
SEPARATORS = {"space": " ", "tab": "\t", "comma": ",", "semicolon": ";"}

# ─── Zielformat der LAZ-Kacheln ────────────────────────────────────────────────
# Identisch zu SB_DSM_PUNKTWOLKE / swissSURFACE3D (topo-importDATAtoGDWHandSTAC,
# 4_SB_DSM_PUNKTWOLKE_LAS14upgrade.py) und topo-DMCdataConverter, damit die Kacheln
# ohne weiteren Upgrade-Schritt GDWH-tauglich sind.
OUT_MINOR_VERSION    = 4
OUT_POINT_FORMAT     = 6      # PF6 (ohne Farbe) - Standard fuer DSM aus xyz
OUT_POINT_FORMAT_RGB = 7      # PF7 = PF6 + RGB, nur wenn die Quelle Farbe fuehrt
OUT_GLOBAL_ENCODING  = 17     # Bit 0 (Adjusted Standard GPS Time) + Bit 4 (WKT)
OUT_SCALE            = 0.01   # Schweizer Konvention
FRAME_TOLERANCE_M    = 0.02   # Rundungsrauschen am Kachelrand nach Requantisierung

PC_FORMATS_WITH_RGB = (2, 3, 5, 7, 8, 10)

# Der Hoehenbezug kommt aus der GUI-Auswahl (LN02 | LHN95) und wird nur getaggt - ein CRS-Tag
# bzw. CRS-Hinweis im Dateinamen der Quelle wird ignoriert (Widerspruch -> nur Warnung).
# Koordinaten und Hoehen werden nie umgerechnet.
PLAUSIBLE_Z = (150.0, 4900.0)   # Schweiz: tiefster Punkt 193 m, hoechster 4634 m (+ Reserve)
HEIGHT_REFS = {"LN02": 5728, "LHN95": 5729}

# ─── Benennung der Kacheln (beide Input-Formate) ───────────────────────────────
# <Jahr>_<AREA>_TIN_DSM[_thin<NN>]_<NAME>_LV95_<LN02|LHN95>.<laz|las>
#   z.B. 2021_DIABLONS_TIN_DSM_thin02_2612_1107_LV95_LN02.laz
# <NN> = Thinning-Abstand in Dezimetern, zweistellig (0.2 m -> 02, 1.5 m -> 15).
# Die Endung _LV95_<Hoehe> entspricht dem Muster des GDWH-Imports (4_SB_DSM_PUNKTWOLKE_LAS14upgrade.py).
# Alle Dateien eines Input-Ordners ergeben EINEN Kachelsatz (je Zelle zusammengefuehrt).
LAS_TILE_TOKEN = "TIN_DSM"
THIN_OPTIONS_M = (0.1, 0.2, 0.4, 0.8, 1.0, 1.5, 2.0)
OUT_FORMATS    = ("laz", "las")

# Grosse ASCII-Dateien (gemergte Gebiete) werden fuer paralleles Einlesen an Zeilen-
# grenzen in Teile zerlegt - readers.text liest einspurig (gemessen ~0.5 Mio Punkte/s).
ASCII_CHUNK_MB = 256

# Plausible Koordinatenbereiche (m) mit Rand, zur Erkennung LV95 vs. LV03
LV95_RANGE = ((2400000.0, 2900000.0), (1000000.0, 1400000.0))
LV03_RANGE = ((400000.0, 900000.0), (0.0, 400000.0))
# Geographische Koordinaten (Grad, Laenge/Breite) im Raum Schweiz mit Rand
DEGREE_RANGE = ((5.0, 11.5), (45.0, 48.5))
NOT_LV95_HINT = ("LV03-Daten zuerst mit GeoSuite/REFRAME (FINELTRA) nach LV95 transformieren - "
                 "dieses Tool transformiert bewusst nicht (PROJ haette ohne CHENyx06-Gitter nur "
                 "eine Ballpark-Transformation).")
DEGREE_HINT = ("Koordinaten in Grad (geographisch) - ein reiner CRS-Tag EPSG:2056 waere falsch. "
               "Zuerst nach LV95 projizieren: aus CH1903+ (EPSG:4150) ist das eine exakte Abbildung "
               "ohne Datumswechsel, aus WGS84/ETRS89 (EPSG:4326/4258) eine Datumstransformation "
               "(GeoSuite/REFRAME). Aus den Werten allein ist das nicht zu unterscheiden.")

# ─── Byte-exakte CRS-VLRs LV95/LN02 ────────────────────────────────────────────
# Unveraendert uebernommen aus topo-DMCdataConverter (dort aus der verifizierten
# swissSURFACE3D-Referenzkachel 2655_1272.laz). Begruendung, warum nicht PDAL den
# WKT schreiben darf: siehe README Abschnitt "CRS-Tags".
REFERENCE_VLR_DESCRIPTION = "by LAStools of rapidlasso GmbH"
CRS_VLR_USER_IDS   = ("LASF_Projection", "liblas")
CRS_VLR_RECORD_IDS = (2111, 2112, 34735, 34736, 34737)
REFERENCE_VLR_34735_B64 = (
    "AQABAAAABQAABAAAAQABAAAMAAABAAgIBAwAAAEAKSMDEAAAAQApIwAQAAABAGAW"
)
REFERENCE_VLR_2112_B64 = (
    "Q09NUE9VTkRDUlNbIlByb2plY3RlZCBjb29yZGluYXRlIHN5c3RlbSB3aXRoIGVsZXZhdGlvbiIsUFJPSkNTWyJDSDE5MDMrIC8gTFY5"
    "NSIsR0VPR0NTWyJDSDE5MDMrIixEQVRVTVsiQ0gxOTAzKyIsU1BIRVJPSURbIkJlc3NlbCAxODQxIiw2Mzc3Mzk3LjE1NSwyOTkuMTUy"
    "ODEyOCxBVVRIT1JJVFlbIkVQU0ciLCI3MDA0Il1dLEFVVEhPUklUWVsiRVBTRyIsIjYxNTAiXV0sUFJJTUVNWyJHcmVlbndpY2giLDAs"
    "QVVUSE9SSVRZWyJFUFNHIiwiODkwMSJdXSxVTklUWyJkZWdyZWUiLDAuMDE3NDUzMjkyNTE5OTQzMyxBVVRIT1JJVFlbIkVQU0ciLCI5"
    "MTIyIl1dLEFVVEhPUklUWVsiRVBTRyIsIjQxNTAiXV0sUFJPSkVDVElPTlsiSG90aW5lX09ibGlxdWVfTWVyY2F0b3JfQXppbXV0aF9D"
    "ZW50ZXIiXSxQQVJBTUVURVJbImxhdGl0dWRlX29mX2NlbnRlciIsNDYuOTUyNDA1NTU1NTU1Nl0sUEFSQU1FVEVSWyJsb25naXR1ZGVf"
    "b2ZfY2VudGVyIiw3LjQzOTU4MzMzMzMzMzMzXSxQQVJBTUVURVJbImF6aW11dGgiLDkwXSxQQVJBTUVURVJbInJlY3RpZmllZF9ncmlk"
    "X2FuZ2xlIiw5MF0sUEFSQU1FVEVSWyJzY2FsZV9mYWN0b3IiLDFdLFBBUkFNRVRFUlsiZmFsc2VfZWFzdGluZyIsMjYwMDAwMF0sUEFS"
    "QU1FVEVSWyJmYWxzZV9ub3J0aGluZyIsMTIwMDAwMF0sVU5JVFsibWV0cmUiLDEsQVVUSE9SSVRZWyJFUFNHIiwiOTAwMSJdXSxBWElT"
    "WyJFYXN0aW5nIixFQVNUXSxBWElTWyJOb3J0aGluZyIsTk9SVEhdLEFVVEhPUklUWVsiRVBTRyIsIjIwNTYiXV0sVkVSVF9DU1siTE4w"
    "MiBoZWlnaHQiLFZFUlRfREFUVU1bIkxhbmRlc25pdmVsbGVtZW50IDE5MDIiLDIwMDUsQVVUSE9SSVRZWyJFUFNHIiwiNTEyNyJdXSxV"
    "TklUWyJtZXRyZSIsMSxBVVRIT1JJVFlbIkVQU0ciLCI5MDAxIl1dLEFYSVNbIkdyYXZpdHktcmVsYXRlZCBoZWlnaHQiLFVQXSxBVVRI"
    "T1JJVFlbIkVQU0ciLCI1NzI4Il1dXQA="
)

# LV95/LHN95: es gibt (noch) keine verifizierte Referenzkachel. Abgeleitet aus der
# LN02-Referenz, geaendert wird NUR der Hoehenteil: der VERT_CS-Block im WKT (zeichengleich
# mit der GDAL/PROJ-Definition von EPSG:5728 bzw. 5729) und der VerticalCSTypeGeoKey
# (4096) im GeoTIFF-KeyDirectory. Alles andere bleibt byte-identisch zur Referenz.
_VERT_CS_LN02 = (b'VERT_CS["LN02 height",VERT_DATUM["Landesnivellement 1902",2005,AUTHORITY["EPSG","5127"]],'
                 b'UNIT["metre",1,AUTHORITY["EPSG","9001"]],AXIS["Gravity-related height",UP],'
                 b'AUTHORITY["EPSG","5728"]]')
_VERT_CS_LHN95 = (b'VERT_CS["LHN95 height",VERT_DATUM["Landeshohennetz 1995",2005,AUTHORITY["EPSG","5128"]],'
                  b'UNIT["metre",1,AUTHORITY["EPSG","9001"]],AXIS["Gravity-related height",UP],'
                  b'AUTHORITY["EPSG","5729"]]')


def _geokeys_with_vertical(payload: bytes, vertical_epsg: int) -> bytes:
    """GeoTIFF-KeyDirectory (VLR 34735) mit ersetztem VerticalCSTypeGeoKey (4096)."""
    b = bytearray(payload)
    n_keys, = struct.unpack_from("<H", b, 6)
    for k in range(n_keys):
        key_id, location, _, _ = struct.unpack_from("<4H", b, 8 + 8 * k)
        if key_id == 4096 and location == 0:
            struct.pack_into("<H", b, 8 + 8 * k + 6, vertical_epsg)
            return bytes(b)
    raise ValueError("Referenz-KeyDirectory ohne VerticalCSTypeGeoKey (4096)")


def _reference_vlr_payloads() -> dict:
    """{Hoehenbezug: (payload 34735, payload 2112)} - byte-exakt fuer LN02, abgeleitet fuer LHN95."""
    keys_ln02 = base64.b64decode(REFERENCE_VLR_34735_B64)
    wkt_ln02 = base64.b64decode(REFERENCE_VLR_2112_B64)
    if wkt_ln02.count(_VERT_CS_LN02) != 1:
        raise ValueError("LN02-Referenz-WKT enthaelt den erwarteten VERT_CS-Block nicht genau einmal")
    return {"LN02": (keys_ln02, wkt_ln02),
            "LHN95": (_geokeys_with_vertical(keys_ln02, HEIGHT_REFS["LHN95"]),
                      wkt_ln02.replace(_VERT_CS_LN02, _VERT_CS_LHN95))}


REFERENCE_VLRS = _reference_vlr_payloads()

# Staging-Namen bewusst kurz: pdal.exe ist nicht long-path-aware (Windows MAX_PATH).
# Getestet (PDAL 2.10): Pipeline-Datei mit 262 Zeichen -> "PDAL: file not found".
PIECE_NAME_PATTERN = re.compile(r"^t(-?\d+)_(-?\d+)\.laz$", re.IGNORECASE)
PDAL_MAX_PATH      = 259     # MAX_PATH 260 inkl. Nullbyte
STAGING_NAME_RESERVE = 24    # laengster Staging-Name unter run_dir: "\p\00000\t2600_1200.laz"
NAME_FIELD_RESERVE   = 15    # Feldbreite 'NAME' in swissGRID


def _check_path_lengths(files: list, run_dir: Path, output_dir: str, max_base_len: int,
                        suffix: str = "_LV95_LHN95") -> None:
    """Bricht vor der Verarbeitung ab, wenn pdal.exe einen Pfad nicht oeffnen koennte
    (sonst scheitert der Lauf erst mittendrin mit einem irrefuehrenden 'file not found')."""
    checks = [("Input-Datei", max(len(f) for f in files)),
              ("Staging-Datei", len(str(run_dir)) + STAGING_NAME_RESERVE),
              ("Output-Kachel", len(output_dir) + 1 + max_base_len + 1 + NAME_FIELD_RESERVE
               + len(suffix) + 4)]
    too_long = [f"{label}: bis {n} Zeichen" for label, n in checks if n > PDAL_MAX_PATH]
    if too_long:
        raise ValueError("Pfad zu lang fuer pdal.exe (Windows MAX_PATH = 260 Zeichen, pdal.exe ist nicht "
                         "long-path-faehig):\n  " + "\n  ".join(too_long)
                         + "\nKuerzeren Input-/Output-/Staging-Ordner waehlen (z.B. D:\\staging).")


def _log(msg: str) -> None:
    print(msg, flush=True)


# Batch: jeder Lauf meldet 0..1, abgebildet auf seinen Anteil am Gesamtbalken (start, spanne)
_PROGRESS_RANGE = [0.0, 1.0]


def _progress(fraction: float) -> None:
    value = _PROGRESS_RANGE[0] + _PROGRESS_RANGE[1] * max(0.0, min(1.0, fraction))
    print(f"PROGRESS:{value:.6f}", flush=True)


def _fmt_count(n) -> str:
    return f"{int(n):,}".replace(",", "'")


# ─── LAS-Header (reines Python, ohne Punktdaten/Dekompression) ─────────────────
def _read_las_header(path: str) -> dict:
    """Liest die fuer Tiling/Validierung noetigen Felder des LAS-Public-Headers
    (ASPRS LAS 1.4 R15, Tabelle 3). Funktioniert auch fuer LAZ, da der Header dort
    unkomprimiert ist - schnell auch bei tausenden Dateien (kein pdal-Aufruf)."""
    with open(path, "rb") as f:
        b = f.read(375)
    if len(b) < 227 or b[:4] != b"LASF":
        raise ValueError(f"Keine gueltige LAS/LAZ-Datei (Signatur 'LASF' fehlt): {path}")
    minor = b[25]
    header_size, offset_to_point_data, n_vlr = struct.unpack_from("<HII", b, 94)
    point_format = b[104] & 0x3F          # Bits 6/7 = LASzip-Kompressionsflag
    global_encoding, = struct.unpack_from("<H", b, 6)
    count, = struct.unpack_from("<I", b, 107)
    if minor >= 4 and len(b) >= 255:
        count64, = struct.unpack_from("<Q", b, 247)
        count = count64 or count
    sx, sy, sz, ox, oy, oz = struct.unpack_from("<6d", b, 131)
    maxx, minx, maxy, miny, maxz, minz = struct.unpack_from("<6d", b, 179)
    return {
        "minor_version": minor, "header_size": header_size,
        "offset_to_point_data": offset_to_point_data, "n_vlr": n_vlr,
        "point_format": point_format, "compressed": bool(b[104] & 0xC0),
        "global_encoding": global_encoding, "count": int(count),
        "scale": (sx, sy, sz), "offset": (ox, oy, oz),
        "minx": minx, "miny": miny, "maxx": maxx, "maxy": maxy, "minz": minz, "maxz": maxz,
    }


def _las_integrity_error(path: str, header: dict) -> str:
    """'' wenn die Datei strukturell lesbar ist, sonst die Fehlerbeschreibung.
    LAZ: Die ersten 8 Bytes der Punktdaten zeigen auf die Chunk-Tabelle (U32 Version = 0,
    U32 Anzahl Chunks). Ist der Zeiger falsch (Datei abgeschnitten, Schreibvorgang
    abgebrochen), bricht pdal erst beim Lesen ab ('Invalid version N found in LAZ chunk
    table') oder haengt bei zufaellig passender Version. Getestet mit PDAL 2.10: auch der
    LASzip-Sonderfall Offset -1 (Tabelle am Dateiende) ist fuer pdal nicht lesbar.
    LAS: Dateigroesse muss alle Punkt-Records fassen."""
    size = os.path.getsize(path)
    start = header["offset_to_point_data"]
    with open(path, "rb") as f:
        if not header["compressed"]:
            f.seek(105)
            rec_len, = struct.unpack("<H", f.read(2))
            need = start + header["count"] * rec_len
            return "" if size >= need else (f"Datei abgeschnitten ({size:,} statt mind. {need:,} Bytes)"
                                            .replace(",", "'"))
        f.seek(start)
        raw = f.read(8)
        if len(raw) < 8:
            return "Datei abgeschnitten (keine Punktdaten)"
        table, = struct.unpack("<q", raw)
        if table == -1:
            return "Chunk-Tabellen-Offset -1 (Tabelle am Dateiende) - von pdal nicht lesbar"
        if not start + 8 <= table <= size - 8:
            return f"Chunk-Tabellen-Offset {table} ausserhalb der Punktdaten ({start + 8}-{size - 8})"
        f.seek(table)
        version, n_chunks = struct.unpack("<II", f.read(8))
    if version != 0:
        return f"Chunk-Tabelle ungueltig (Version {version} statt 0)"
    if n_chunks > max(1, header["count"]) or (header["count"] and not n_chunks):
        return f"Chunk-Tabelle ungueltig ({n_chunks} Chunks bei {header['count']} Punkten)"
    return ""


def _read_vlrs(path: str, header: dict = None) -> list:
    """(user_id, record_id, payload) aller VLRs - nur der unkomprimierte Kopfbereich."""
    header = header or _read_las_header(path)
    vlrs = []
    with open(path, "rb") as f:
        f.seek(header["header_size"])
        for _ in range(header["n_vlr"]):
            rec = f.read(54)
            if len(rec) < 54:
                break
            _, uid, rid, rlen, _ = struct.unpack("<H16sHH32s", rec)
            vlrs.append((uid.split(b"\x00")[0].decode("ascii", "replace"), rid, f.read(rlen)))
    return vlrs


def _read_vlr_ids(path: str, header: dict) -> list:
    """(user_id, record_id) aller VLRs - fuer die CRS-Tag-Kontrolle der Ausgabe."""
    return [(uid, rid) for uid, rid, _ in _read_vlrs(path, header)]


def _classify_crs(minx: float, miny: float, maxx: float, maxy: float) -> str:
    """'LV95' | 'LV03' | 'GRAD' | 'unbekannt' anhand des Koordinatenbereichs (Plausibilitaet).
    Der CRS-Tag einer Datei spielt dafuer keine Rolle - massgebend sind die Werte."""
    def _inside(rng):
        (x0, x1), (y0, y1) = rng
        return x0 <= minx <= maxx <= x1 and y0 <= miny <= maxy <= y1
    if _inside(LV95_RANGE):
        return "LV95"
    if _inside(LV03_RANGE):
        return "LV03"
    if _inside(DEGREE_RANGE):
        return "GRAD"
    return "unbekannt"


# ─── PDAL-Aufrufe ──────────────────────────────────────────────────────────────
def _resolve_pdal_exe(cfg: dict) -> str:
    """pdal.exe aus der GUI-Config, sonst neben dem laufenden (OSGeo4W/QGIS-)Python."""
    candidates = [cfg.get("pdal_exe") or "",
                  str(Path(sys.executable).parent / "pdal.exe"),
                  shutil.which("pdal") or ""]
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    raise FileNotFoundError("pdal.exe nicht gefunden (weder in der Config noch neben "
                            f"{sys.executable} oder im PATH).")


def _pdal_env(pdal_exe: str) -> dict:
    env = os.environ.copy()
    env["PATH"] = os.path.dirname(pdal_exe) + os.pathsep + env.get("PATH", "")
    return env


def _run_pdal(pdal_exe: str, args: list) -> str:
    """Fuehrt pdal.exe aus, gibt stdout+stderr zurueck, wirft bei Exit-Code != 0."""
    result = subprocess.run([pdal_exe] + args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            universal_newlines=True, env=_pdal_env(pdal_exe))
    out = ((result.stdout or "") + (result.stderr or "")).strip()
    if result.returncode != 0:
        hint = "" if out else (" - kein stdout/stderr: deutet auf einen Prozessabsturz hin "
                               "(z.B. zu wenig RAM bei vielen parallelen pdal.exe).")
        raise RuntimeError(f"pdal {args[0]} beendet mit Exit-Code {result.returncode}{hint}"
                           + (f": {out[:800]}" if out else ""))
    return out


def _run_pipeline(pdal_exe: str, stages: list, pipeline_path: Path) -> str:
    with open(pipeline_path, "w", encoding="utf-8") as f:
        json.dump({"pipeline": stages}, f, indent=1)
    return _run_pdal(pdal_exe, ["pipeline", str(pipeline_path)])


def _pdal_srs_name(pdal_exe: str, path: str) -> str:
    """Name des CRS-Tags einer LAS/LAZ-Datei ('pdal info --metadata', nur Header)."""
    try:
        md = json.loads(_run_pdal(pdal_exe, ["info", "--metadata", path]))["metadata"]
    except Exception as e:
        return f"(nicht lesbar: {e})"
    srs = md.get("srs") or {}
    wkt = srs.get("compoundwkt") or srs.get("wkt") or ""
    if not wkt:
        return "– (kein CRS-Tag in der Datei)"
    m = re.search(r'^(?:COMPD_CS|COMPOUNDCRS|PROJCS|PROJCRS|GEOGCS)\["([^"]+)"', wkt)
    label = m.group(1) if m else wkt[:60]
    for token in ("LN02", "LHN95"):
        if token in wkt:
            return f"{label}  [{token}]"
    return label


def _crs_label(wkt: str, node: str) -> str:
    """'Name (EPSG:code)' des Teil-CRS 'node' (PROJCS | GEOGCS | VERT_CS) eines WKT, sonst ''."""
    if not wkt:
        return ""
    from osgeo import osr
    osr.UseExceptions()
    try:
        s = osr.SpatialReference()
        s.ImportFromWkt(wkt)
    except Exception:
        return ""
    name = s.GetAttrValue(node)
    if not name:
        return ""
    code = s.GetAuthorityCode(node)
    return f"{name} (EPSG:{code})" if code else f"{name} (ohne EPSG-Code)"


def _file_metadata(pdal_exe: str, path: str) -> dict:
    """Header-Metadaten EINER LAS/LAZ-Datei fuer die Datei-Info im GUI ('pdal info --metadata'
    und '--schema' lesen nur den Kopf - auch bei einem grossen gemergten LAZ sofort da)."""
    md = json.loads(_run_pdal(pdal_exe, ["info", "--metadata", path]))["metadata"]
    srs = md.get("srs") or {}
    wkt = srs.get("compoundwkt") or srs.get("wkt") or ""
    horizontal = _crs_label(wkt, "PROJCS") or _crs_label(wkt, "GEOGCS")
    if horizontal and srs.get("isgeographic"):
        horizontal += "  - geographisch (Grad)"
    try:
        schema = json.loads(_run_pdal(pdal_exe, ["info", "--schema", path])).get("schema") or {}
        dims = [d.get("name") for d in schema.get("dimensions") or []]
    except Exception:
        dims = []
    vlrs = [f"{uid}/{rid}" for uid, rid, _ in _read_vlrs(path)]
    return {
        "file": os.path.basename(path),
        "version": f"LAS {md.get('major_version', 1)}.{md.get('minor_version')}  /  "
                   f"PF{md.get('dataformat_id')}  /  {'LAZ' if md.get('compressed') else 'LAS'}",
        "count": md.get("count", 0),
        "scale": [md.get("scale_x"), md.get("scale_y"), md.get("scale_z")],
        "offset": [md.get("offset_x"), md.get("offset_y"), md.get("offset_z")],
        "global_encoding": md.get("global_encoding"),
        "software": " / ".join(v for v in (str(md.get("software_id") or "").strip(),
                                           str(md.get("system_id") or "").strip()) if v),
        "created": f"{md.get('creation_year')}, Tag {md.get('creation_doy')}" if md.get("creation_year") else "",
        "crs_horizontal": horizontal or "– (kein Lage-CRS im Tag)",
        "crs_vertical": _crs_label(wkt, "VERT_CS") or "– (kein Hoehen-CRS im Tag)",
        "vlrs": vlrs,
        "dims": dims,
    }


def _discard(path) -> None:
    try:
        if path and os.path.isfile(str(path)):
            os.remove(str(path))
    except OSError:
        pass


# ─── Byte-exakte CRS-VLR-Injektion (aus topo-DMCdataConverter) ─────────────────
def _is_crs_vlr(user_id: str, record_id: int) -> bool:
    """True, wenn dieser VLR einen Raumbezug deklariert."""
    if user_id == "LASF_Projection":
        return True
    return user_id in CRS_VLR_USER_IDS and record_id in CRS_VLR_RECORD_IDS


def _build_vlr_record(user_id: str, record_id: int, description: str, payload: bytes) -> bytes:
    """Baut einen kompletten LAS-VLR (54-Byte-Header + Payload)."""
    header = struct.pack("<H16sHH32s", 0, user_id.encode("ascii").ljust(16, b"\x00"),
                         record_id, len(payload), description.encode("ascii").ljust(32, b"\x00"))
    return header + payload


def _inject_reference_vlrs(las_path: str, height_ref: str = "LN02") -> int:
    """Ersetzt alle CRS-VLRs durch die zwei Referenz-VLRs LV95/<height_ref>
    (GeoTIFF-KeyDirectory 34735 + OGC-WKT 2112). In-place, nur auf Temp-Dateien
    aufrufen. Bei LAZ wird zusaetzlich die LASzip-Chunk-Table-Position korrigiert
    (int64 am Anfang des Punktbereichs), sonst bricht jede Dekompression ab.
    Gibt die Anzahl entfernter CRS-VLRs zurueck."""
    keys_payload, wkt_payload = REFERENCE_VLRS[height_ref]
    with open(las_path, "rb") as f:
        head = f.read(512)
        header_size, offset_to_point_data, n_vlr = struct.unpack_from("<HII", head, 94)
        f.seek(0)
        data = f.read(offset_to_point_data)

    existing = data[header_size:offset_to_point_data]
    is_laszip, n_stripped, kept, pos = False, 0, [], 0
    for _ in range(n_vlr):
        _, uid_raw, record_id, record_len, _ = struct.unpack_from("<H16sHH32s", existing, pos)
        user_id = uid_raw.split(b"\x00")[0].decode("ascii", "replace")
        vlr_len = 54 + record_len
        if _is_crs_vlr(user_id, record_id):
            n_stripped += 1
        else:
            kept.append(existing[pos:pos + vlr_len])
        if user_id == "laszip encoded" and record_id == 22204:
            is_laszip = True
        pos += vlr_len

    vlr1 = _build_vlr_record("LASF_Projection", 34735, REFERENCE_VLR_DESCRIPTION, keys_payload)
    vlr2 = _build_vlr_record("LASF_Projection", 2112, REFERENCE_VLR_DESCRIPTION, wkt_payload)
    new_vlr_block = b"".join(kept) + vlr1 + vlr2
    new_offset = header_size + len(new_vlr_block)
    shift = new_offset - offset_to_point_data

    new_header = bytearray(data[:header_size])
    struct.pack_into("<I", new_header, 96, new_offset)
    struct.pack_into("<I", new_header, 100, n_vlr - n_stripped + 2)
    ge, = struct.unpack_from("<H", new_header, 6)
    struct.pack_into("<H", new_header, 6, ge | 0x10)  # WKT-Bit setzen

    tmp_out = las_path + ".vlrtmp"
    try:
        with open(las_path, "rb") as fin, open(tmp_out, "wb") as fout:
            fout.write(new_header)
            fout.write(new_vlr_block)
            fin.seek(offset_to_point_data)
            if is_laszip:
                first8 = fin.read(8)
                if len(first8) == 8:
                    chunk_table_pos, = struct.unpack("<q", first8)
                    if chunk_table_pos != -1:
                        first8 = struct.pack("<q", chunk_table_pos + shift)
                fout.write(first8)
            shutil.copyfileobj(fin, fout, 8 * 1024 * 1024)
        os.replace(tmp_out, las_path)
        tmp_out = None
    finally:
        _discard(tmp_out)
    return n_stripped


# ─── ASCII-Analyse ─────────────────────────────────────────────────────────────
# Spaltennamen aus einer Kopfzeile -> PDAL-Dimensionsnamen (Gross-/Kleinschreibung egal)
_KNOWN_DIMS = {"x": "X", "y": "Y", "z": "Z", "h": "Z", "intensity": "Intensity",
               "classification": "Classification", "class": "Classification",
               "red": "Red", "green": "Green", "blue": "Blue"}


def _is_number(token: str) -> bool:
    try:
        float(token)
        return True
    except ValueError:
        return False


def _split_fields(line: str, sep_key: str) -> list:
    if sep_key == "space":
        return line.split()
    return [t.strip() for t in line.split(SEPARATORS[sep_key])]


def _sniff_ascii(path: str, max_lines: int = 50) -> dict:
    """Erkennt Kopfzeilen, Trennzeichen und Spaltenzahl aus den ersten Zeilen einer
    ASCII-Datei und schaetzt das Koordinatensystem aus dem Wertebereich."""
    lines = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for _ in range(max_lines):
            line = f.readline()
            if not line:
                break
            if line.strip():
                lines.append(line.rstrip("\r\n"))
    if not lines:
        raise ValueError(f"Datei ist leer: {path}")
    if lines[0].split()[0].lower() in ESRI_HEADER_KEYS:
        raise ValueError(f"{os.path.basename(path)} ist ein ESRI-ASCII-Grid (Raster mit ncols/nrows-Kopf), "
                         "keine XYZ-Punktliste - fuer DSM-Raster den Tab 'DSM.ascii -> DSM-Raster' verwenden, "
                         "fuer LAZ-Kacheln zuerst mit 'gdal_translate -of XYZ' umwandeln.")

    skip = 0
    for line in lines:
        if _is_number(re.split(r"[\s,;]+", line.strip())[0]):
            break
        skip += 1
    data = lines[skip:]
    if not data:
        raise ValueError(f"Keine numerische Datenzeile in den ersten {max_lines} Zeilen: {path}")

    sample = data[0]
    if "\t" in sample:
        sep = "tab"
    elif ";" in sample:
        sep = "semicolon"
    elif "," in sample:
        sep = "comma"
    else:
        sep = "space"
    fields = _split_fields(sample, sep)
    if len(fields) < 3 or not all(_is_number(t) for t in fields[:3]):
        raise ValueError(f"Erste Datenzeile hat keine 3 numerischen Spalten (X Y Z): '{sample[:80]}'")

    ncols = len(fields)
    columns = ["X", "Y", "Z"] + [f"Col{i}" for i in range(4, ncols + 1)]
    if skip:
        tokens = _split_fields(lines[skip - 1], sep)
        if len(tokens) == ncols:
            mapped = [_KNOWN_DIMS.get(t.strip().strip('"').lower()) for t in tokens]
            columns = [m or columns[i] for i, m in enumerate(mapped)]

    xs, ys, zs = [], [], []
    for line in data:
        parts = _split_fields(line, sep)
        if len(parts) >= 3 and all(_is_number(t) for t in parts[:3]):
            xs.append(float(parts[0]))
            ys.append(float(parts[1]))
            zs.append(float(parts[2]))
    # Mittlere Bytes je Datenzeile (+ Zeilenende, CRLF = 2 Byte) -> Schaetzung der Punktzahl
    with open(path, "rb") as fb:
        nl = 2 if b"\r\n" in fb.read(65536) else 1
    line_bytes = sum(len(line.encode("utf-8")) + nl for line in data) / len(data)
    head_bytes = sum(len(line.encode("utf-8")) + nl for line in lines[:skip])
    return {
        "separator": sep, "skip": skip, "ncols": ncols, "columns": " ".join(columns),
        "preview": lines[:4], "first_xy": [xs[0], ys[0]],
        "zrange_sample": [min(zs), max(zs)],
        "count_estimate": int(max(0, os.path.getsize(path) - head_bytes) / line_bytes),
        "crs_guess": _classify_crs(min(xs), min(ys), max(xs), max(ys)),
    }


def _list_inputs(input_dir: str, fmt: str) -> list:
    exts = ASCII_EXTENSIONS if fmt == "ascii" else LAS_EXTENSIONS
    return sorted(str(p) for p in Path(input_dir).iterdir()
                  if p.is_file() and p.suffix.lower() in exts)


# CRS-Hinweise im Dateinamen (ASCII hat keinen CRS-Tag - der Name ist oft die einzige Angabe)
_NAME_CRS_TOKENS = (("LV95", "LV95"), ("LV03", "LV03"), ("LN02", "LN02"), ("LHN95", "LHN95"),
                    ("2056", "EPSG:2056"), ("21781", "EPSG:21781"), ("WGS84", "WGS84"),
                    ("ETRS89", "ETRS89"), ("CH1903", "CH1903"))


def _name_crs_hints(filename: str) -> list:
    """CRS-Stichworte im Dateinamen, z.B. ['LV95', 'LN02'] (als eigenes Wort, _ - . als Trenner)."""
    stem = os.path.splitext(os.path.basename(filename))[0].upper()
    words = set(re.split(r"[_\-.\s+]+", stem))
    return [label for token, label in _NAME_CRS_TOKENS if token in words]


def _source_height(path: str, fmt: str) -> str:
    """Hoehenbezug laut Quelle: LAS/LAZ aus dem CRS-Tag, ASCII aus dem Dateinamen. '' = keine Angabe."""
    if fmt == "las":
        return _vertical_tag(path)
    hints = _name_crs_hints(path)
    return next((h for h in ("LHN95", "LN02") if h in hints), "")


def _ascii_metadata(path: str, sniff: dict) -> dict:
    """Metadaten der ersten ASCII-Datei fuer die Datei-Info (Gegenstueck zu _file_metadata)."""
    crs_text = {"LV95": "Werte passen zu LV95 (EPSG:2056)",
                "LV03": "Werte passen zu LV03 (EPSG:21781) - NICHT unterstuetzt",
                "GRAD": "Werte in Grad (geographisch) - NICHT unterstuetzt",
                }.get(sniff["crs_guess"], "Werte weder LV95 noch LV03 - Spalten pruefen")
    hints = _name_crs_hints(path)
    height = next((h for h in ("LHN95", "LN02") if h in hints), "")
    x, y = sniff["first_xy"]
    return {
        "file": os.path.basename(path),
        "size": f"{os.path.getsize(path) / 1024 ** 2:,.1f} MB",
        "version": f"ASCII  /  {sniff['ncols']} Spalten ({sniff['columns']})  /  {sniff['skip']} Kopfzeile(n)",
        "count": f"~ {_fmt_count(sniff['count_estimate'])}  (geschaetzt aus Dateigroesse)",
        "crs_horizontal": f"kein CRS-Tag (ASCII)  |  {crs_text}",
        "crs_vertical": f"kein CRS-Tag (ASCII)  |  Dateiname: {height}" if height
                        else "kein CRS-Tag (ASCII)  |  Dateiname ohne Hoehenangabe",
        "name_hints": ", ".join(hints) or "– (keine CRS-Angabe im Dateinamen)",
        "first_line": f"X {x:.2f}  /  Y {y:.2f}",
        "zrange": "{:.2f} – {:.2f} m (erste Zeilen)".format(*sniff["zrange_sample"]),
    }


def _overlapping_pairs(boxes: dict, min_share: float = 0.01) -> list:
    """Paare, deren BBoxen sich flaechig ueberlappen (> min_share der kleineren Flaeche).
    Aneinanderstossende Kacheln (gemeinsame Kante) zaehlen nicht."""
    items = sorted(boxes.items())
    pairs = []
    for i, (a, ba) in enumerate(items):
        for b, bb in items[i + 1:]:
            w = min(ba[2], bb[2]) - max(ba[0], bb[0])
            h = min(ba[3], bb[3]) - max(ba[1], bb[1])
            smaller = min((ba[2] - ba[0]) * (ba[3] - ba[1]), (bb[2] - bb[0]) * (bb[3] - bb[1]))
            if w > 0 and h > 0 and w * h > min_share * max(smaller, 1e-9):
                pairs.append((a, b))
    return pairs


def _vertical_tag(path: str) -> str:
    """Hoehenbezug laut CRS-VLRs einer LAS/LAZ-Datei: 'LN02' | 'LHN95' | '' (kein/anderer Tag).
    Geprueft werden WKT (2111/2112) und VerticalCSTypeGeoKey (4096) im KeyDirectory (34735)."""
    for _, record_id, payload in _read_vlrs(path):
        if record_id in (2111, 2112):
            for ref, epsg in HEIGHT_REFS.items():
                if ref.encode() in payload or f'"{epsg}"'.encode() in payload:
                    return ref
        if record_id == 34735 and len(payload) >= 8:
            n_keys, = struct.unpack_from("<H", payload, 6)
            for k in range(n_keys):
                if 16 + 8 * k > len(payload):
                    break
                key_id, location, _, value = struct.unpack_from("<4H", payload, 8 + 8 * k)
                if key_id == 4096 and location == 0:
                    for ref, epsg in HEIGHT_REFS.items():
                        if value == epsg:
                            return ref
    return ""


def _lhn95_tagged(path: str) -> bool:
    """True, wenn die CRS-VLRs einer LAS/LAZ-Datei LHN95 (EPSG:5729) deklarieren."""
    return _vertical_tag(path) == "LHN95"


def _thin_token(thin_m) -> str:
    """'thin02' fuer 0.2 m, '' ohne Thinning."""
    if not thin_m:
        return ""
    return f"thin{int(round(float(thin_m) * 10)):02d}"


def _las_tile_base(jahr: str, area: str, thin_m) -> str:
    """Basis der LAZ-Tab-Benennung: <Jahr>_<AREA>_TIN_DSM[_thin<NN>]."""
    parts = [str(jahr).strip(), str(area).strip(), LAS_TILE_TOKEN, _thin_token(thin_m)]
    return "_".join(p for p in parts if p)


def _height_suffix(height_ref: str) -> str:
    return f"_LV95_{height_ref}"


def _job_name(jahr: str, area: str, thin_m, height_ref: str) -> str:
    """Kachelname ohne TileKey (<NAME>) und Endung - Name des Staging-Job-Ordners und
    (im GUI) der Log-Datei, z.B. 2021_PERROC_TIN_DSM_thin04_LV95_LN02."""
    return _las_tile_base(jahr, area, thin_m) + _height_suffix(height_ref)


def _unique_dir(parent: Path, name: str) -> Path:
    """parent/name, bei bestehendem Ordner (behaltenes Staging, paralleler Lauf) name_2, name_3 ..."""
    path, n = parent / name, 1
    while path.exists():
        n += 1
        path = parent / f"{name}_{n}"
    return path


def _target_point_format(source_formats) -> int:
    """Ziel-PF (LAS 1.4) nach den Feldern der Quelle: PF6, PF7 falls RGB, PF8 falls RGB+NIR.
    PF0/1 -> 6, PF2/3 -> 7, PF6/7/8 bleiben. Waveform (PF4/5/9/10) schreibt PDAL nicht."""
    formats = set(source_formats)
    if formats & {8, 10}:
        return 8
    if formats & set(PC_FORMATS_WITH_RGB):
        return OUT_POINT_FORMAT_RGB
    return OUT_POINT_FORMAT


def _split_ascii(src: str, chunk_dir: Path, n_parts: int) -> list:
    """Zerlegt eine grosse ASCII-Datei an Zeilengrenzen in bis zu n_parts Teildateien
    (binaere Kopie, kein Parsen), damit readers.text die Teile parallel liest.
    Kopfzeilen bleiben im ersten Teil."""
    size = os.path.getsize(src)
    cuts = [0]
    chunk_dir.mkdir(parents=True, exist_ok=True)
    parts = []
    with open(src, "rb") as f:
        for k in range(1, n_parts):
            f.seek(size * k // n_parts)
            f.readline()                      # bis zum naechsten Zeilenende
            if cuts[-1] < f.tell() < size:
                cuts.append(f.tell())
        cuts.append(size)
        for i in range(len(cuts) - 1):
            part = chunk_dir / f"c{i:02d}.txt"
            f.seek(cuts[i])
            remaining = cuts[i + 1] - cuts[i]
            with open(part, "wb") as out:
                while remaining > 0:
                    buf = f.read(min(remaining, 16 * 1024 * 1024))
                    if not buf:
                        break
                    out.write(buf)
                    remaining -= len(buf)
            parts.append(str(part))
    return parts


def _cells_of_bbox(minx, miny, maxx, maxy, size: float) -> set:
    """Grid-Zellen (ix, iy), die eine BBox beruehrt - halb-offene Intervalle [a, a+size)
    wie bei 'pdal tile'."""
    import math
    return {(ix, iy)
            for ix in range(math.floor(minx / size), math.floor(maxx / size) + 1)
            for iy in range(math.floor(miny / size), math.floor(maxy / size) + 1)}


def _info(cfg: dict) -> None:
    """Analysiert den Input-Ordner und gibt das Ergebnis als JSON-Zeile auf stdout aus."""
    if cfg.get("format") == "raster":
        if cfg.get("batch_list"):
            _raster_batch_info(cfg)
        else:
            _raster_info(cfg)
        return
    input_dir = cfg["input_dir"]
    grid_size = float(cfg.get("grid_size") or 1000.0)
    if not os.path.isdir(input_dir):
        raise FileNotFoundError(f"Input-Ordner nicht gefunden: {input_dir}")

    ascii_files = _list_inputs(input_dir, "ascii")
    las_files   = _list_inputs(input_dir, "las")
    fmt = cfg.get("format") or ""
    if fmt not in ("ascii", "las"):
        fmt = "las" if len(las_files) > len(ascii_files) else "ascii"
    files = ascii_files if fmt == "ascii" else las_files

    result = {"format": fmt, "n_ascii": len(ascii_files), "n_las": len(las_files),
              "n_files": len(files), "sample": os.path.basename(files[0]) if files else "",
              "size_mb": round(sum(os.path.getsize(p) for p in files) / 1024 ** 2, 1)}
    if not files:
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return

    if fmt == "ascii":
        sniff = _sniff_ascii(files[0])
        result.update(sniff)
        heights = {}
        for p in files:
            h = _source_height(p, "ascii") or "ohne"
            heights[h] = heights.get(h, 0) + 1
        result.update({
            "vertical_tags": heights,   # ASCII: aus dem Dateinamen
            "target_pf": OUT_POINT_FORMAT_RGB if {"Red", "Green", "Blue"} & set(sniff["columns"].split())
                         else OUT_POINT_FORMAT,
            "first_meta": _ascii_metadata(files[0], sniff),
        })
    else:
        headers = [_read_las_header(p) for p in files]
        minx = min(h["minx"] for h in headers)
        miny = min(h["miny"] for h in headers)
        maxx = max(h["maxx"] for h in headers)
        maxy = max(h["maxy"] for h in headers)
        cells = set()
        for h in headers:
            cells |= _cells_of_bbox(h["minx"], h["miny"], h["maxx"], h["maxy"], grid_size)
        h0 = headers[0]
        pdal_exe = ""
        try:
            pdal_exe = _resolve_pdal_exe(cfg)
        except FileNotFoundError:
            pass
        vertical_tags = {}
        for p in files:
            tag = _vertical_tag(p) or "ohne"
            vertical_tags[tag] = vertical_tags.get(tag, 0) + 1
        formats = sorted({h["point_format"] for h in headers})
        result.update({
            "version": f"LAS 1.{h0['minor_version']}  /  PF{'/'.join(str(f) for f in formats)}"
                       + ("  (LAZ)" if h0["compressed"] else "  (LAS)"),
            "count_total": sum(h["count"] for h in headers),
            "extent": [minx, miny, maxx, maxy],
            "zrange": [min(h["minz"] for h in headers), max(h["maxz"] for h in headers)],
            "has_rgb": any(h["point_format"] in PC_FORMATS_WITH_RGB for h in headers),
            "target_pf": _target_point_format(formats),
            "crs_guess": _classify_crs(minx, miny, maxx, maxy),
            "crs_tag": _pdal_srs_name(pdal_exe, files[0]) if pdal_exe else "(pdal.exe nicht gefunden)",
            "vertical_tags": vertical_tags,
            "n_cells_max": len(cells),
            "broken": [f"{os.path.basename(p)}: {err}" for p, h in zip(files, headers)
                       for err in [_las_integrity_error(p, h)] if err],
        })
        if pdal_exe:
            try:
                result["first_meta"] = _file_metadata(pdal_exe, files[0])
            except Exception as e:
                result["first_meta"] = {"file": os.path.basename(files[0]), "error": str(e)}
    print(json.dumps(result, ensure_ascii=False), flush=True)


# ─── Grid-Shape ────────────────────────────────────────────────────────────────
def _load_grid_cells(grid_shape_path: str, bbox: tuple) -> tuple:
    """Liest die Grid-Zellen, die die Daten-BBox beruehren.

    Erwartet ein regelmaessiges, achsparalleles Quadratraster in EPSG:2056, dessen
    Zellen auf Vielfachen der Zellgroesse liegen (wie swissGRID 1km2). Nur dann
    entspricht die Kachelung von 'pdal tile' (Ursprung 0/0, halb-offene Intervalle)
    exakt den Grid-Zellen - das wird hier fuer jede beteiligte Zelle geprueft.
    Rueckgabe: (zellgroesse_m, {(ix, iy): NAME})."""
    from collections import Counter
    from osgeo import ogr, osr
    ogr.UseExceptions()
    osr.UseExceptions()

    ds = ogr.Open(grid_shape_path, 0)
    if ds is None:
        raise FileNotFoundError(f"OGR konnte das Grid-Shape nicht oeffnen: {grid_shape_path}")
    layer = ds.GetLayer()
    defn = layer.GetLayerDefn()
    if defn.GetFieldIndex("NAME") < 0:
        fields = [defn.GetFieldDefn(i).GetName() for i in range(defn.GetFieldCount())]
        raise ValueError(f"Grid-Shape enthaelt kein Feld 'NAME' - vorhandene Felder: {fields}")

    srs = layer.GetSpatialRef()
    target = osr.SpatialReference()
    target.ImportFromEPSG(2056)
    if srs is None:
        _log("  WARNUNG       : Grid-Shape hat kein CRS (.prj fehlt) - wird als EPSG:2056 angenommen")
    elif srs.GetAuthorityCode(None) != "2056" and not srs.IsSame(target):
        raise ValueError(f"Grid-Shape ist nicht in EPSG:2056 (LV95), sondern '{srs.GetName()}'. "
                         "Das Tiling rechnet mit LV95-Kilometerzellen - bitte ein LV95-Grid verwenden.")
    else:
        _log("  Grid-Shape CRS: EPSG:2056 (passend)")

    layer.SetSpatialFilterRect(*bbox)
    size, cells, irregular = None, {}, []
    for feat in layer:
        name = feat.GetField("NAME")
        geom = feat.GetGeometryRef()
        if name is None or not str(name).strip() or geom is None:
            continue
        x0, x1, y0, y1 = geom.GetEnvelope()
        if size is None:
            size = round(x1 - x0, 6)
        if (abs((x1 - x0) - size) > 1e-6 or abs((y1 - y0) - size) > 1e-6
                or abs(geom.GetArea() - size * size) > 1e-3
                or abs(x0 / size - round(x0 / size)) > 1e-9
                or abs(y0 / size - round(y0 / size)) > 1e-9):
            irregular.append(str(name))
            continue
        cells[(int(round(x0 / size)), int(round(y0 / size)))] = str(name).strip()
    ds = None

    if size is None or not cells:
        raise ValueError("Grid-Shape hat keine Zelle im Bereich der Daten - Extent/CRS pruefen.")
    if irregular:
        raise ValueError(f"{len(irregular)} Grid-Zelle(n) sind keine achsparallelen {size:g} m-Quadrate "
                         f"auf dem {size:g} m-Raster (z.B. {irregular[:3]}) - dieses Tool erwartet "
                         "ein regelmaessiges Grid wie swissGRID 1km2.")
    dupes = [n for n, c in Counter(cells.values()).items() if c > 1]
    if dupes:
        raise ValueError(f"Doppelte NAME-Werte im Grid-Shape (Ausgaben wuerden sich ueberschreiben): {dupes[:5]}")
    return size, cells


# ─── Worker (laufen parallel in Threads, die Arbeit macht pdal.exe) ────────────
def _las_writer(filename: str, point_format: int, offset=(0.0, 0.0, 0.0), srs="EPSG:2056",
                global_encoding=None, compress: bool = True) -> dict:
    writer = {"type": "writers.las", "filename": filename,
              # Kompression explizit, nicht ueber die Dateiendung erraten lassen
              "compression": "laszip" if compress else "false",
              "minor_version": OUT_MINOR_VERSION, "dataformat_id": point_format,
              "scale_x": OUT_SCALE, "scale_y": OUT_SCALE, "scale_z": OUT_SCALE,
              "offset_x": offset[0], "offset_y": offset[1], "offset_z": offset[2],
              "a_srs": srs}
    if global_encoding is not None:
        writer["global_encoding"] = global_encoding
    return writer


def _ascii_to_laz_worker(args) -> tuple:
    """ASCII -> Zwischen-LAZ (scale 0.01, offset 0 -> dasselbe Zentimeter-Gitter wie die
    Endkacheln mit Offset = Kachelursprung, die Requantisierung dort ist eine Identitaet).
    drop_nodata (nur Raster-Tab): Punkte mit Z <= -9999 (NoData-Platzhalter) verwerfen.
    Rueckgabe: (quelle, ziel, punktzahl, anzahl_ignorierter_zeilen)."""
    src, dst, pipeline_path, pdal_exe, columns, sep_key, skip, point_format, drop_nodata = args
    sep = SEPARATORS[sep_key]
    stages = [{"type": "readers.text", "filename": src, "separator": sep,
               "header": sep.join(columns), "skip": int(skip)}]
    if drop_nodata:
        stages.append({"type": "filters.range", "limits": f"Z({GRID_NODATA_SENTINEL:g}:]"})
    stages.append(_las_writer(dst, point_format))
    try:
        out = _run_pipeline(pdal_exe, stages, pipeline_path)
    finally:
        _discard(pipeline_path)
    # readers.text ueberspringt unpassende Zeilen nur mit Warnung (Exit-Code 0) -
    # passt die Spaltenzahl gar nicht, entsteht still eine leere Datei.
    ignored = out.count("Ignoring")
    count = _read_las_header(dst)["count"] if os.path.isfile(dst) else 0
    if count == 0:
        first = out.splitlines()[0] if out else ""
        raise ValueError(f"0 Punkte gelesen - Spalten '{' '.join(columns)}' / Trennzeichen passen "
                         f"nicht zur Datei" + (" (oder nur NoData-Hoehen)" if drop_nodata else "")
                         + f". PDAL: {first[:300]}")
    return src, dst, count, ignored


def _ascii_to_laz_all(files: list, read_fmt: dict, run_dir: Path, pdal_exe: str, workers: int,
                      chunk_bytes: int, point_format: int, p0: float, pspan: float,
                      drop_nodata: bool = False) -> tuple:
    """Schritt 'ASCII -> LAZ' beider ASCII-Wege (Kacheln, Raster aus Punkten): jede Datei ->
    Zwischen-LAZ, grosse Dateien vorher an Zeilengrenzen in Teile zerlegt und parallel gelesen.
    read_fmt: {datei: (spalten, trennzeichen, kopfzeilen)}.
    Rueckgabe: (sortierte Zwischen-LAZ, {zwischen_laz: input_datei})."""
    names = {f: os.path.basename(f) for f in files}
    tmp_dir = run_dir / "a"
    tmp_dir.mkdir(exist_ok=True)
    jobs = []   # (teil_oder_datei, input_datei, kopfzeilen)
    for i, f in enumerate(files):
        skip = read_fmt[f][2]
        size_b = os.path.getsize(f)
        n_parts = min(workers, -(-size_b // chunk_bytes)) if workers > 1 else 1
        if n_parts > 1:
            parts = _split_ascii(f, run_dir / "c" / f"{i:05d}", n_parts)
            _log(f"  {names[f]}: {size_b / 1024 ** 2:,.0f} MB -> {len(parts)} Teile (paralleles Einlesen)")
            jobs += [(p, f, skip if k == 0 else 0) for k, p in enumerate(parts)]
        else:
            jobs.append((f, f, skip))
    sources, failed, origin_of = [], [], {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_ascii_to_laz_worker,
                          (src, str(tmp_dir / f"s{j:05d}.laz"), tmp_dir / f"p{j:05d}.json", pdal_exe,
                           read_fmt[orig][0], read_fmt[orig][1], sk, point_format, drop_nodata)): (src, orig)
                for j, (src, orig, sk) in enumerate(jobs)}
        for done, fut in enumerate(as_completed(futs), 1):
            src, orig = futs[fut]
            label = names[orig] + (f" [{os.path.basename(src)}]" if src != orig else "")
            try:
                _, dst, count, ignored = fut.result()
                sources.append(dst)
                origin_of[dst] = orig
                _log(f"  [{done}/{len(jobs)}] {label}: {_fmt_count(count)} Punkte"
                     + (f"  - WARNUNG: {ignored} Zeile(n) ignoriert (Spaltenzahl)" if ignored else ""))
            except Exception as e:
                failed.append(label)
                _log(f"  [{done}/{len(jobs)}] {label}: FEHLER - {e}")
            finally:
                if src != orig:
                    _discard(src)   # Teildatei sofort freigeben (Platz im Staging)
            _progress(p0 + pspan * done / len(jobs))
    if failed:
        raise RuntimeError(f"{len(failed)} ASCII-Datei(en)/Teil(e) nicht lesbar - Abbruch.")
    return sorted(sources), origin_of


def _split_worker(args) -> tuple:
    """Zerlegt EINE Quelldatei mit 'pdal tile' (Streaming, ein Lesedurchlauf) in
    Kachelstuecke 't<ix>_<iy>.laz' - ix/iy = floor(koordinate / zellgroesse),
    also halb-offene Intervalle: ein Punkt auf der Kilometerlinie gehoert genau
    einer Kachel. Rueckgabe: (quelle, [((ix, iy), pfad, punktzahl), ...])."""
    src, piece_dir, pdal_exe, size, point_format = args
    Path(piece_dir).mkdir(parents=True, exist_ok=True)
    _run_pdal(pdal_exe, [
        "tile", src, str(Path(piece_dir) / "t#.laz"),
        "--length", f"{size:g}", "--origin_x", "0", "--origin_y", "0",
        "--writers.las.compression=laszip",
        f"--writers.las.minor_version={OUT_MINOR_VERSION}",
        f"--writers.las.dataformat_id={point_format}",
        f"--writers.las.scale_x={OUT_SCALE}", f"--writers.las.scale_y={OUT_SCALE}",
        f"--writers.las.scale_z={OUT_SCALE}",
        "--writers.las.offset_x=0", "--writers.las.offset_y=0", "--writers.las.offset_z=0",
    ])
    pieces = []
    for p in sorted(Path(piece_dir).iterdir()):
        m = PIECE_NAME_PATTERN.match(p.name)
        if m:
            pieces.append(((int(m.group(1)), int(m.group(2))), str(p),
                           _read_las_header(str(p))["count"]))
    return src, pieces


def _validate_tile(path: str, h: dict, origin: tuple, size: float, point_format: int,
                   expected_count: int, height_ref: str = "LN02", thinned: bool = False,
                   compressed: bool = True) -> list:
    """Kontrolle statt Annahme: Header-Zielwerte, Punktbilanz, Kachelrahmen, CRS-VLRs.
    Mit Thinning muss die Punktzahl nur 1 .. expected_count sein (ausgeduennt)."""
    problems = []
    if h["minor_version"] != OUT_MINOR_VERSION or h["point_format"] != point_format:
        problems.append(f"LAS 1.{h['minor_version']}/PF{h['point_format']}, erwartet "
                        f"LAS 1.{OUT_MINOR_VERSION}/PF{point_format}")
    if h["header_size"] != 375:
        problems.append(f"header_size={h['header_size']}, erwartet 375")
    if h["global_encoding"] != OUT_GLOBAL_ENCODING:
        problems.append(f"global_encoding={h['global_encoding']}, erwartet {OUT_GLOBAL_ENCODING}")
    if any(abs(s - OUT_SCALE) > 1e-12 for s in h["scale"]):
        problems.append(f"scale={h['scale']}, erwartet {OUT_SCALE}")
    if abs(h["offset"][0] - origin[0]) > 1e-6 or abs(h["offset"][1] - origin[1]) > 1e-6:
        problems.append(f"offset={h['offset'][:2]}, erwartet Kachelursprung {origin}")
    if h["compressed"] != compressed:
        problems.append("LAZ-komprimiert" if h["compressed"] else "nicht LAZ-komprimiert")
    if thinned:
        if not 0 < h["count"] <= expected_count:
            problems.append(f"Punktzahl {h['count']} nach Thinning, erwartet 1..{expected_count}")
    elif h["count"] != expected_count:
        problems.append(f"Punktzahl {h['count']} statt {expected_count} (Summe der Kachelstuecke)")
    eps = FRAME_TOLERANCE_M
    if (h["minx"] < origin[0] - eps or h["maxx"] > origin[0] + size + eps or
            h["miny"] < origin[1] - eps or h["maxy"] > origin[1] + size + eps):
        problems.append(f"Punkte ausserhalb des Kachelrahmens (BBox X {h['minx']:.2f}-{h['maxx']:.2f}, "
                        f"Y {h['miny']:.2f}-{h['maxy']:.2f})")
    keys_payload, wkt_payload = REFERENCE_VLRS[height_ref]
    crs_vlrs = sorted((uid, rid, payload) for uid, rid, payload in _read_vlrs(path, h)
                      if _is_crs_vlr(uid, rid))
    if crs_vlrs != [("LASF_Projection", 2112, wkt_payload), ("LASF_Projection", 34735, keys_payload)]:
        problems.append(f"CRS-VLRs {[v[:2] for v in crs_vlrs]}, erwartet genau die "
                        f"LV95/{height_ref}-Referenz (2112 + 34735, byte-exakt)")
    return problems


def _tile_worker(args) -> tuple:
    """Fuegt die Kachelstuecke EINER Grid-Zelle eines Kachelsatzes zusammen und schreibt
    die Endkachel im Zielformat. Atomar: Temp-Datei im Output-Ordner, erst nach
    bestandener Validierung per os.replace an ihren Platz.
    Mit thin_m wird die zusammengefuehrte Zelle vor dem Schreiben ausgeduennt
    (filters.sample, Mindestabstand thin_m - wie im Projekt topo-DMCdataConverter).
    Rueckgabe: (status, dateiname, punktzahl, fehler, ueberschrieben)."""
    (key, out_path, pieces, expected_count, pipeline_path, pdal_exe, size, point_format,
     height_ref, thin_m) = args
    origin = (key[0] * size, key[1] * size)
    out_name = os.path.basename(out_path)
    ext = os.path.splitext(out_path)[1].lower()
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", suffix=ext, dir=os.path.dirname(out_path))
    os.close(fd)
    os.remove(tmp_path)  # writers.las legt die Datei selbst an

    stages = [{"type": "readers.las", "filename": p, "tag": f"r{i}"} for i, p in enumerate(pieces)]
    if len(stages) > 1:
        stages.append({"type": "filters.merge", "inputs": [s["tag"] for s in stages]})
    if thin_m:
        stages.append({"type": "filters.sample", "radius": float(thin_m)})
    # PDAL schreibt vorerst nur LV95 - die autoritativen LV95/<Hoehe>-VLRs kommen danach per Byte-Patch
    stages.append(_las_writer(tmp_path, point_format, (origin[0], origin[1], 0.0), "EPSG:2056",
                              OUT_GLOBAL_ENCODING, compress=(ext == ".laz")))
    try:
        _run_pipeline(pdal_exe, stages, Path(pipeline_path))
        if not os.path.isfile(tmp_path) or _read_las_header(tmp_path)["count"] == 0:
            return ("empty", out_name, 0, None, False)
        _inject_reference_vlrs(tmp_path, height_ref)
        h = _read_las_header(tmp_path)
        problems = _validate_tile(tmp_path, h, origin, size, point_format, expected_count,
                                  height_ref, bool(thin_m), compressed=(ext == ".laz"))
        if problems:
            return ("error", out_name, 0, "; ".join(problems), False)
        existed = os.path.isfile(out_path)
        os.replace(tmp_path, out_path)
        tmp_path = None
        return ("written", out_name, h["count"], None, existed)
    except Exception as e:
        return ("error", out_name, 0, str(e), False)
    finally:
        _discard(tmp_path)
        _discard(pipeline_path)


# ─── DSM-Raster + Hillshade (optional, aus den fertigen Kacheln) ───────────────
# Uebernommen aus topo-DMCdataConverter (_raster_cell_worker, _fill_raster_nodata,
# _prepare_hillshade_values, _mosaic_las_raster) - Aenderungen dort hier nachziehen.
# Abweichungen: kein AOI-Shape (NoData nur, wo keine Punkte liegen), das DSM traegt das
# zusammengesetzte CRS LV95 + Hoehe, der Hillshade ist dort NoData, wo das DSM NoData ist.
# NoData des FERTIGEN Float32-DSM: exakt -FLT_MAX (GDWH-Konvention SB_DSM).
RASTER_NODATA = -3.4028234663852886e+38
# NoData der Zwischen-Zellraster aus writers.gdal: PDAL lehnt -FLT_MAX als nodata ab
# ("Invalid nodata value ... for output data_type 'float'"), gdal.Warp setzt ihn danach um.
CELL_NODATA = -9999.0
# Hillshade (Byte): 255 = NoData. gdaldem liefert 1..255 - ein voll beleuchtetes Pixel wird
# auf 254 gezogen, damit 255 eindeutig "keine Daten" bedeutet.
HILLSHADE_NODATA    = 255
HILLSHADE_VALID_MAX = 254
# Kleine NoData-Loecher (Rauschen) werden interpoliert, grosse bleiben NoData.
FILL_MAX_HOLE_AREA_M2   = 900.0
FILL_HOLE_CONNECTEDNESS = 8
RASTER_WORK_CELL_M      = 1000.0   # Arbeitszellen (RAM-Begrenzung), werden danach mosaikiert
RASTER_OUT_CO = ["TILED=YES", "BLOCKXSIZE=512", "BLOCKYSIZE=512", "COMPRESS=LZW", "BIGTIFF=YES", "TFW=YES"]


def _raster_names(jahr: str, area: str, gsd: float, height_ref: str) -> tuple:
    """(DSM, Hillshade): <Jahr>_<AREA>_DSM_<GSD>cm_LV95_<Hoehe>.tif - wie topo-DMCdataConverter."""
    label = f"{int(round(float(gsd) * 100))}cm"
    return (f"{jahr}_{area}_DSM_{label}{_height_suffix(height_ref)}.tif",
            f"{jahr}_{area}_hillshade_{label}{_height_suffix(height_ref)}.tif")


def _raster_cell_buffer(gsd: float) -> float:
    """Puffer um eine Arbeitszelle: IDW-Nachbarschaft am Zellrand vollstaendig, keine Naht."""
    return max(3.0 * float(gsd), 2.0)


def _dsm_cell_jobs(tile_bboxes: list, snap_bounds: tuple, gsd: float) -> list:
    """Zerlegt den Datenbereich in Arbeitszellen (auf snap_bounds beschnitten, auf das
    GSD-Raster gelegt). Jede Zelle bekommt die Kacheln, die ihren GEPUFFERTEN Ausschnitt
    beruehren. tile_bboxes: [(pfad, minx, miny, maxx, maxy), ...]."""
    import math
    buf = _raster_cell_buffer(gsd)
    ox, oy = snap_bounds[0], snap_bounds[1]
    size = RASTER_WORK_CELL_M
    jobs = []
    for e in range(int(math.floor(snap_bounds[0] / size)), int(math.ceil(snap_bounds[2] / size))):
        for n in range(int(math.floor(snap_bounds[1] / size)), int(math.ceil(snap_bounds[3] / size))):
            cminx, cmaxx = max(e * size, snap_bounds[0]), min((e + 1) * size, snap_bounds[2])
            cminy, cmaxy = max(n * size, snap_bounds[1]), min((n + 1) * size, snap_bounds[3])
            if cmaxx <= cminx or cmaxy <= cminy:
                continue
            # Epsilon gegen Float-Rauschen (sonst gelegentlich eine Pixelspalte Ueberlappung)
            rb = (ox + math.floor((cminx - ox) / gsd + 1e-6) * gsd,
                  oy + math.floor((cminy - oy) / gsd + 1e-6) * gsd,
                  ox + math.ceil((cmaxx - ox) / gsd - 1e-6) * gsd,
                  oy + math.ceil((cmaxy - oy) / gsd - 1e-6) * gsd)
            tiles = [b[0] for b in tile_bboxes
                     if not (b[3] <= rb[0] - buf or b[1] >= rb[2] + buf or
                             b[4] <= rb[1] - buf or b[2] >= rb[3] + buf)]
            if tiles:
                jobs.append({"cell": f"{e}_{n}", "raster_bounds": rb, "tiles": tiles})
    return jobs


def _raster_cell_worker(args) -> tuple:
    """Rastert EINE Arbeitszelle (PDAL writers.gdal, IDW) auf den gesnappten Ausschnitt.
    Rueckgabe: (status, zelle, fehler)."""
    job, cells_dir, pipeline_path, pdal_exe, gsd, srs = args
    r_minx, r_miny, r_maxx, r_maxy = job["raster_bounds"]
    tif_out = str(Path(cells_dir) / f"dsm_{job['cell']}.tif")
    buf = _raster_cell_buffer(gsd)
    stages = [{"type": "readers.las", "filename": t, "tag": f"r{i}", "override_srs": srs}
              for i, t in enumerate(job["tiles"])]
    if len(stages) > 1:
        stages.append({"type": "filters.merge", "inputs": [s["tag"] for s in stages]})
    stages.append({"type": "filters.crop",
                   "bounds": f"([{r_minx - buf:.3f},{r_maxx + buf:.3f}],[{r_miny - buf:.3f},{r_maxy + buf:.3f}])"})
    stages.append({"type": "writers.gdal", "filename": tif_out, "resolution": float(gsd),
                   "output_type": "idw", "gdaldriver": "GTiff", "data_type": "float32",
                   "bounds": f"([{r_minx:.3f},{r_maxx:.3f}],[{r_miny:.3f},{r_maxy:.3f}])",
                   "nodata": CELL_NODATA})
    try:
        _run_pipeline(pdal_exe, stages, Path(pipeline_path))
        return ("written" if os.path.isfile(tif_out) else "empty", job["cell"], None)
    except Exception as e:
        _discard(tif_out)
        return ("error", job["cell"], str(e))
    finally:
        _discard(pipeline_path)


def _fill_raster_nodata(vrt_path: Path, work_dir: Path, gsd: float, num_threads: str,
                        raw_hillshade_path: str) -> str:
    """Interpoliert KLEINE NoData-Loecher (bis FILL_MAX_HOLE_AREA_M2), grosse bleiben NoData.
    Nebenbei entsteht der rohe Hillshade aus dem Stand, in dem alle erreichbaren Loecher
    gefuellt sind. Rueckgabe: Pfad des DSM mit gefuellten kleinen Loechern.
    Ablauf: Mosaik materialisieren -> Loch-Maske sichern -> SieveFilter trennt grosse von
    kleinen Loechern -> FillNodata fuellt alles -> Hillshade -> grosse Loecher zuruecksetzen."""
    from osgeo import gdal
    import numpy as np

    filled_path = work_dir / "dsm_filled.tif"
    mask_path   = work_dir / "holes_mask.tif"
    sieve_path  = work_dir / "holes_large.tif"
    max_hole_px = max(1, int(round(FILL_MAX_HOLE_AREA_M2 / (gsd * gsd))))
    _log(f"  Kleine NoData-Loecher fuellen: bis {FILL_MAX_HOLE_AREA_M2:g} m2 = {max_hole_px} Pixel")

    float_co = ["TILED=YES", "BLOCKXSIZE=512", "BLOCKYSIZE=512", "COMPRESS=NONE", "BIGTIFF=YES",
                f"NUM_THREADS={num_threads}"]
    byte_co = ["TILED=YES", "BLOCKXSIZE=512", "BLOCKYSIZE=512", "COMPRESS=LZW", "PREDICTOR=2",
               "BIGTIFF=YES", f"NUM_THREADS={num_threads}"]
    gdal.Translate(str(filled_path), str(vrt_path),
                   options=gdal.TranslateOptions(format="GTiff", noData=CELL_NODATA,
                                                 creationOptions=float_co)).FlushCache()

    ds = mask_ds = sieve_ds = None
    try:
        ds = gdal.Open(str(filled_path), gdal.GA_Update)
        band = ds.GetRasterBand(1)
        xs, ys = band.XSize, band.YSize
        rows_per_chunk = max(1, (64 * 1024 * 1024) // max(1, xs * 4))
        _log(f"  Rastergroesse: {xs} x {ys} = {xs * ys / 1e6:.1f} Mio. Pixel")

        def _byte_raster(path):
            out = gdal.GetDriverByName("GTiff").Create(str(path), xs, ys, 1, gdal.GDT_Byte, options=byte_co)
            out.SetGeoTransform(ds.GetGeoTransform())
            out.SetProjection(ds.GetProjection())
            return out

        mask_ds = _byte_raster(mask_path)
        mask_band = mask_ds.GetRasterBand(1)
        holes_total = 0
        for y0 in range(0, ys, rows_per_chunk):
            rows = min(rows_per_chunk, ys - y0)
            hole = (band.ReadAsArray(0, y0, xs, rows) == CELL_NODATA)
            holes_total += int(np.count_nonzero(hole))
            mask_band.WriteArray(hole.astype("uint8"), 0, y0)
        mask_band.FlushCache()

        # SieveFilter entfernt Polygone KLEINER als die Schwelle -> +1, damit ein Loch von
        # exakt max_hole_px noch gefuellt wird
        sieve_ds = _byte_raster(sieve_path)
        sieve_band = sieve_ds.GetRasterBand(1)
        gdal.SieveFilter(mask_band, None, sieve_band, max_hole_px + 1, FILL_HOLE_CONNECTEDNESS)
        sieve_band.FlushCache()

        old_tmpdir = gdal.GetConfigOption("CPL_TMPDIR")
        gdal.SetConfigOption("CPL_TMPDIR", str(work_dir))
        try:
            gdal.FillNodata(band, None, float(max_hole_px), 0)
        finally:
            gdal.SetConfigOption("CPL_TMPDIR", old_tmpdir)
        band.FlushCache()

        # Hillshade aus DIESEM Stand (alles gefuellt) - vor dem Zuruecksetzen
        ds.FlushCache()
        gdal.DEMProcessing(str(raw_hillshade_path), ds, "hillshade",
                           options=gdal.DEMProcessingOptions(computeEdges=True)).FlushCache()

        # Grosse Loecher (nur was vorher schon NoData war) wieder auf NoData
        kept = remaining = 0
        for y0 in range(0, ys, rows_per_chunk):
            rows = min(rows_per_chunk, ys - y0)
            big = ((sieve_band.ReadAsArray(0, y0, xs, rows) != 0) &
                   (mask_band.ReadAsArray(0, y0, xs, rows) != 0))
            arr = band.ReadAsArray(0, y0, xs, rows)
            n = int(np.count_nonzero(big))
            if n:
                arr[big] = CELL_NODATA
                band.WriteArray(arr, 0, y0)
                kept += n
            remaining += int(np.count_nonzero(arr == CELL_NODATA))
        band.FlushCache()
    finally:
        ds = mask_ds = sieve_ds = None

    _log(f"  Interpoliert: {holes_total - kept} Pixel  |  NoData belassen (grosse Loecher, "
         f"ausserhalb der Daten): {kept} Pixel")
    if remaining != kept:
        _log(f"  WARNUNG: {remaining - kept} Pixel blieben NoData, obwohl ihr Loch unter der "
             "Schwelle liegt - kein gueltiger Nachbar in Reichweite.")
    return str(filled_path)


def _prepare_hillshade_values(path: str) -> int:
    """Zieht im rohen Hillshade 255 (voll beleuchtet) und gdaldem-NoData auf 254 (in-place),
    damit 255 im Endprodukt ausschliesslich NoData bedeutet. Gibt die Anzahl zurueck."""
    from osgeo import gdal
    import numpy as np
    ds = gdal.Open(path, gdal.GA_Update)
    try:
        band = ds.GetRasterBand(1)
        nd = band.GetNoDataValue()
        nd = None if nd is None else int(nd)
        rows_per_chunk = max(1, (64 * 1024 * 1024) // max(1, band.XSize))
        changed = 0
        for y0 in range(0, band.YSize, rows_per_chunk):
            rows = min(rows_per_chunk, band.YSize - y0)
            arr = band.ReadAsArray(0, y0, band.XSize, rows)
            hit = arr == HILLSHADE_NODATA
            if nd is not None and nd != HILLSHADE_NODATA:
                hit |= arr == nd
            n = int(np.count_nonzero(hit))
            if n:
                arr[hit] = HILLSHADE_VALID_MAX
                band.WriteArray(arr, 0, y0)
                changed += n
        band.FlushCache()
    finally:
        ds = None
    return changed


def _mask_hillshade_by_dsm(hs_path: str, dsm_path: str) -> int:
    """Setzt den Hillshade dort auf NoData (255), wo das DSM NoData traegt. Ohne AOI-Shape
    ist das die einzige Aussage darueber, wo Daten liegen. Blockweise, gibt Anzahl zurueck."""
    from osgeo import gdal
    import numpy as np
    hs_ds = gdal.Open(hs_path, gdal.GA_Update)
    dsm_ds = gdal.Open(dsm_path, gdal.GA_ReadOnly)
    try:
        hs_band, dsm_band = hs_ds.GetRasterBand(1), dsm_ds.GetRasterBand(1)
        nd = np.float32(RASTER_NODATA)
        rows_per_chunk = max(1, (64 * 1024 * 1024) // max(1, dsm_band.XSize * 4))
        masked = 0
        for y0 in range(0, dsm_band.YSize, rows_per_chunk):
            rows = min(rows_per_chunk, dsm_band.YSize - y0)
            hole = dsm_band.ReadAsArray(0, y0, dsm_band.XSize, rows) == nd
            n = int(np.count_nonzero(hole))
            if n:
                arr = hs_band.ReadAsArray(0, y0, hs_band.XSize, rows)
                arr[hole] = HILLSHADE_NODATA
                hs_band.WriteArray(arr, 0, y0)
                masked += n
        hs_band.FlushCache()
    finally:
        hs_ds = dsm_ds = None
    return masked


def _build_dsm_raster(tile_paths: list, raster_dir: str, jahr: str, area: str, gsd: float,
                      height_ref: str, work_dir: Path, workers: int, pdal_exe: str,
                      p0: float, pspan: float, names: tuple = None) -> tuple:
    """DSM (Float32, NoData -FLT_MAX, CRS LV95 + Hoehe) und Hillshade (Byte, NoData 255,
    CRS LV95) aus den fertigen Kacheln. names: (DSM, Hillshade), Standard _raster_names().
    Rueckgabe: (dsm_pfad, hillshade_pfad)."""
    import math
    from osgeo import gdal, osr
    gdal.UseExceptions()
    osr.UseExceptions()

    srs_compound = f"EPSG:2056+{HEIGHT_REFS[height_ref]}"
    dsm_name, hs_name = names or _raster_names(jahr, area, gsd, height_ref)
    dsm_path = os.path.join(raster_dir, dsm_name)
    hs_path = os.path.join(raster_dir, hs_name)
    Path(raster_dir).mkdir(parents=True, exist_ok=True)
    cells_dir = work_dir / "c"
    cells_dir.mkdir(parents=True, exist_ok=True)

    boxes = []
    for t in tile_paths:
        h = _read_las_header(t)
        boxes.append((t, h["minx"], h["miny"], h["maxx"], h["maxy"]))
    snap = (math.floor(min(b[1] for b in boxes) / gsd) * gsd,
            math.floor(min(b[2] for b in boxes) / gsd) * gsd,
            math.ceil(max(b[3] for b in boxes) / gsd) * gsd,
            math.ceil(max(b[4] for b in boxes) / gsd) * gsd)
    jobs = _dsm_cell_jobs(boxes, snap, gsd)
    _log(f"  GSD {gsd:g} m  |  CRS DSM {srs_compound}, Hillshade EPSG:2056  |  {len(jobs)} Arbeitszelle(n)")
    _log(f"  Ziel-Gitter: {snap[0]:.2f}, {snap[1]:.2f} - {snap[2]:.2f}, {snap[3]:.2f}")

    args = [(job, str(cells_dir), work_dir / f"p{i:05d}.json", pdal_exe, gsd, srs_compound)
            for i, job in enumerate(jobs)]
    failed = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_raster_cell_worker, a): a for a in args}
        for done, fut in enumerate(as_completed(futs), 1):
            status, cell, err = fut.result()
            if status == "error":
                failed.append(futs[fut])
                _log(f"  [{done}/{len(args)}] Zelle {cell}: FEHLER (Wiederholung folgt) - {err}")
            _progress(p0 + pspan * 0.6 * done / len(args))
    errors = []
    for a in failed:
        # Abgestuerzte pdal.exe sind meist Speicherdruck paralleler Jobs -> seriell wiederholen
        status, cell, err = _raster_cell_worker(a)
        if status == "error":
            errors.append(f"{cell}: {err}")
    if errors:
        raise RuntimeError(f"{len(errors)} Raster-Zelle(n) fehlgeschlagen:\n  " + "\n  ".join(errors[:10]))
    cell_rasters = sorted(str(p) for p in cells_dir.glob("dsm_*.tif"))
    if not cell_rasters:
        raise RuntimeError("Keine Raster-Zelle erzeugt - DSM nicht moeglich.")

    vrt_path = work_dir / "dsm_mosaic.vrt"
    gdal.BuildVRT(str(vrt_path), cell_rasters,
                  options=gdal.BuildVRTOptions(srcNodata=CELL_NODATA, VRTNodata=CELL_NODATA)).FlushCache()
    raw_hs = work_dir / "hillshade_raw.tif"
    filled = _fill_raster_nodata(vrt_path, work_dir, gsd, str(workers), str(raw_hs))
    _progress(p0 + pspan * 0.8)
    _finish_rasters(filled, str(raw_hs), dsm_path, hs_path, snap, gsd, height_ref, workers)
    _progress(p0 + pspan)
    return dsm_path, hs_path


def _finish_rasters(src_dsm: str, raw_hs: str, dsm_path: str, hs_path: str, bounds: tuple,
                    gsd: float, height_ref: str, workers: int) -> None:
    """Gemeinsamer Abschluss aller Raster-Wege: DSM aus src_dsm (Float32, NoData CELL_NODATA)
    mit NoData -FLT_MAX und CRS LV95 + Hoehe schreiben, Hillshade aus raw_hs (gdaldem) mit
    255 = NoData, wo das DSM NoData ist, danach Kontrolle. Beide mit .tfw."""
    from osgeo import gdal
    gdal.UseExceptions()
    srs_compound = f"EPSG:2056+{HEIGHT_REFS[height_ref]}"
    for p in (dsm_path, hs_path):
        for side in (p, os.path.splitext(p)[0] + ".tfw", p + ".aux.xml"):
            if os.path.isfile(side):
                _log(f"  vorhanden, wird ueberschrieben: {os.path.basename(side)}")
                _discard(side)

    common = dict(format="GTiff", outputBounds=bounds, xRes=gsd, yRes=gsd, multithread=True,
                  warpOptions=[f"NUM_THREADS={workers}"])
    # Quelle = Ziel-CRS und Ziel-Gitter = Quell-Gitter: es wird nichts reprojiziert, nicht
    # resampelt und keine Hoehe umgerechnet, nur NoData umgesetzt und getaggt
    gdal.Warp(dsm_path, src_dsm, options=gdal.WarpOptions(
        srcSRS=srs_compound, dstSRS=srs_compound, srcNodata=CELL_NODATA, dstNodata=RASTER_NODATA,
        creationOptions=RASTER_OUT_CO + ["PREDICTOR=3"], **common)).FlushCache()

    n_clamped = _prepare_hillshade_values(raw_hs)
    hs_ds = gdal.Warp(hs_path, raw_hs, options=gdal.WarpOptions(
        srcSRS="EPSG:2056", dstSRS="EPSG:2056", dstNodata=HILLSHADE_NODATA,
        creationOptions=RASTER_OUT_CO + ["PREDICTOR=2"], **common))
    hs_ds.GetRasterBand(1).SetUnitType("")   # Grauwert, keine Einheit (erbt sonst 'metre')
    hs_ds.FlushCache()
    hs_ds = None
    n_masked = _mask_hillshade_by_dsm(hs_path, dsm_path)
    _log(f"  Hillshade: {n_clamped} Pixel 255/NoData -> {HILLSHADE_VALID_MAX}, "
         f"{n_masked} Pixel ohne DSM -> NoData {HILLSHADE_NODATA}")

    # Kontrolle statt Annahme: NoData, CRS, deckungsgleiches Gitter
    problems = []
    dsm_ds, hs_ds = gdal.Open(dsm_path), gdal.Open(hs_path)
    nd = dsm_ds.GetRasterBand(1).GetNoDataValue()
    if nd is None or struct.unpack("<f", struct.pack("<f", nd))[0] != RASTER_NODATA:
        problems.append(f"DSM-NoData {nd!r}, erwartet {RASTER_NODATA!r}")
    if hs_ds.GetRasterBand(1).GetNoDataValue() != HILLSHADE_NODATA:
        problems.append(f"Hillshade-NoData {hs_ds.GetRasterBand(1).GetNoDataValue()!r}, erwartet 255")
    dsm_srs = dsm_ds.GetSpatialRef()
    if (dsm_srs is None or not dsm_srs.IsCompound() or dsm_srs.GetAuthorityCode("PROJCS") != "2056"
            or dsm_srs.GetAuthorityCode("VERT_CS") != str(HEIGHT_REFS[height_ref])):
        problems.append(f"DSM-CRS {dsm_srs.GetName() if dsm_srs else None!r}, erwartet {srs_compound}")
    hs_srs = hs_ds.GetSpatialRef()
    if hs_srs is None or hs_srs.GetAuthorityCode(None) != "2056":
        problems.append(f"Hillshade-CRS {hs_srs.GetName() if hs_srs else None!r}, erwartet EPSG:2056")
    geom = [(d.RasterXSize, d.RasterYSize, d.GetGeoTransform()) for d in (dsm_ds, hs_ds)]
    if geom[0] != geom[1]:
        problems.append(f"DSM und Hillshade nicht deckungsgleich: {geom}")
    dsm_label = dsm_srs.GetName() if dsm_srs else "?"
    dsm_ds = hs_ds = None
    if problems:
        raise RuntimeError("Raster-Kontrolle fehlgeschlagen:\n  " + "\n  ".join(problems))
    _log(f"  Kontrolle OK: NoData DSM {RASTER_NODATA:g} / Hillshade {HILLSHADE_NODATA}, "
         f"CRS '{dsm_label}', Gitter deckungsgleich")


# ─── ASCII-DSM -> DSM-Raster + Hillshade fuer SB_DSM (Aktion 'raster') ─────────
# Ein Gebiet (alle ASCII-Dateien eines Ordners = EIN Mosaik) direkt zu DSM + Hillshade, ohne
# LAZ-Kacheln. Ziel: der GDWH-Import (topo-GDWHimport, GDS 'SB_DSM') nimmt beide ohne Nacharbeit
# an - DSM Float32 / NoData -FLT_MAX / EPSG:2056+5728, Hillshade Byte / 255 / EPSG:2056.
# Hoehenbezug fix LN02: SB_DSM kennt nur LV95_LN02, der Import bricht bei LHN95 ab
# (topo-GDWHimport/processingScripts/_tiff_crs.py, decide_crs_action).
SBDSM_HEIGHT_REF = "LN02"
# Namensmuster 1:1 aus topo-GDWHimport (GUI_GDWHimport.NAME_FORMAT_SPECS['SB_DSM'])
SBDSM_NAME_PATTERNS = (
    re.compile(r'^20\d{2}_.+_DSM_.*_LV95_LN02\.(tif|tiff|tfw)$', re.IGNORECASE),
    re.compile(r'^20\d{2}_.+_hillshade_.*_LV95_LN02\.(tif|tiff)$', re.IGNORECASE),
)
# Namensteile, die der Import selbst auswertet (AREA = Text zwischen Jahr und erstem '_DSM' bzw.
# '_hillshade'; Namensteil 'hillshade' = Hillshade) - in der AREA nicht zulaessig
SBDSM_RESERVED_PARTS = ("DSM", "HILLSHADE", "LV95")
# NoData-Platzhalter alter DSM-Lieferungen: wird nie als Hoehe uebernommen (= CELL_NODATA)
GRID_NODATA_SENTINEL = -9999.0
# Gitter-Pruefungen: Toleranz relativ zur Zellgroesse
GRID_TOLERANCE = 1e-3
KIND_LABELS = {"esri": "ESRI-ASCII-Grid", "xyz_grid": "XYZ-Gitter", "points": "XYZ-Punkte"}


def _gsd_label(gsd: float) -> str:
    """GSD im Dateinamen wie in den Lieferungen: ganze Meter '1m', sonst Zentimeter '50cm'."""
    gsd = float(gsd)
    if gsd >= 1 and abs(gsd - round(gsd)) < 1e-9:
        return f"{int(round(gsd))}m"
    return f"{round(gsd * 100, 4):g}cm"


def _sbdsm_raster_names(jahr: str, area: str, gsd: float) -> tuple:
    """(DSM, Hillshade): <Jahr>_<AREA>_DSM_<GSD>_LV95_LN02.tif / <Jahr>_<AREA>_hillshade_<GSD>_LV95_LN02.tif"""
    label, suffix = _gsd_label(gsd), _height_suffix(SBDSM_HEIGHT_REF)
    return (f"{jahr}_{area}_DSM_{label}{suffix}.tif", f"{jahr}_{area}_hillshade_{label}{suffix}.tif")


def _sbdsm_job_name(jahr: str, area: str) -> str:
    """Name des Staging-Job-Ordners und der Log-Datei, z.B. 2014_ALETSCH_DSM_LV95_LN02."""
    return f"{str(jahr).strip()}_{str(area).strip()}_DSM{_height_suffix(SBDSM_HEIGHT_REF)}"


def _sbdsm_name_problems(jahr: str, area: str) -> list:
    """Gruende, warum Jahr/AREA keinen vom SB_DSM-Import lesbaren Namen ergeben ([] = ok)."""
    jahr, area = str(jahr).strip(), str(area).strip()
    problems = []
    if not re.fullmatch(r"20\d{2}", jahr):
        problems.append(f"Jahr '{jahr}' ungueltig - der SB_DSM-Import erwartet 20xx (z.B. 2014)")
    if not area:
        problems.append("AREA fehlt")
    elif re.search(r'[<>:"/\\|?*\s]', area):
        problems.append(f"AREA '{area}' enthaelt Leerzeichen oder unzulaessige Zeichen")
    else:
        reserved = [p for p in area.upper().split("_") if p in SBDSM_RESERVED_PARTS]
        if reserved:
            problems.append(f"AREA '{area}' enthaelt den Namensteil '{reserved[0]}' - der SB_DSM-Import "
                            "liest die AREA nur bis zum ersten '_DSM' bzw. '_hillshade'")
    if not problems:
        dsm, hs = _sbdsm_raster_names(jahr, area, 1.0)
        for name in (dsm, os.path.splitext(dsm)[0] + ".tfw", hs):
            if not any(p.match(name) for p in SBDSM_NAME_PATTERNS):
                problems.append(f"'{name}' passt nicht zum Namensmuster des SB_DSM-Imports")
    return problems


def _source_name_parts(filename: str, is_file: bool = True) -> dict:
    """Jahr, AREA und GSD aus einem Lieferungsnamen, z.B. 2014_ALETSCH_MOOSFLUE_DSM_1m_LV95_LN02_CIR_low.asc
    -> 2014 / ALETSCH_MOOSFLUE / 1.0 (Vorbelegung im GUI, Kontrolle der Zellgroesse). Fehlt -> '' / None.
    is_file=False: Ordnername (keine Endung abschneiden, '..._1.5m' bliebe sonst nicht ganz)."""
    stem = os.path.basename(os.path.normpath(filename))
    if is_file:
        stem = os.path.splitext(stem)[0]
    m_jahr = re.match(r"(\d{4})_", stem)
    m_area = re.match(r"\d{4}_(.+?)_DSM(?:_|$)", stem, re.IGNORECASE)
    m_gsd = re.search(r"(?:^|_)(\d+(?:[.,]\d+)?)(m|cm)(?=_|$)", stem, re.IGNORECASE)
    gsd = None
    if m_gsd:
        gsd = float(m_gsd.group(1).replace(",", ".")) * (0.01 if m_gsd.group(2).lower() == "cm" else 1.0)
    return {"jahr": m_jahr.group(1) if m_jahr else "", "area": m_area.group(1) if m_area else "",
            "gsd": gsd}


def _read_esri_header(path: str) -> dict:
    """Kopf eines ESRI-ASCII-Grids, ohne die Daten zu lesen; {} wenn die Datei keinen hat.
    box = Aussenkante des Gitters (bei xllcenter/yllcenter um eine halbe Zelle verschoben)."""
    values, n_lines = {}, 0
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for _ in range(12):
            line = f.readline(4096)   # Datenzeilen koennen sehr lang sein - nur den Anfang lesen
            if not line:
                break
            parts = line.split()
            if not parts:
                continue
            key = parts[0].lower()
            if key not in ESRI_HEADER_KEYS:
                break
            if len(parts) < 2 or not _is_number(parts[1]):
                raise ValueError(f"{os.path.basename(path)}: ESRI-Kopfzeile ohne Zahl: '{line.strip()[:60]}'")
            values[key] = float(parts[1])
            n_lines += 1
    if not values:
        return {}
    cs_x = values.get("cellsize", values.get("dx"))
    cs_y = values.get("cellsize", values.get("dy"))
    missing = [k for k, ok in (("ncols", "ncols" in values), ("nrows", "nrows" in values),
                               ("xllcorner/xllcenter", "xllcorner" in values or "xllcenter" in values),
                               ("yllcorner/yllcenter", "yllcorner" in values or "yllcenter" in values),
                               ("cellsize", cs_x is not None and cs_y is not None)) if not ok]
    if missing:
        raise ValueError(f"{os.path.basename(path)}: unvollstaendiger ESRI-Kopf, es fehlt {', '.join(missing)}")
    xll = values["xllcorner"] if "xllcorner" in values else values["xllcenter"] - cs_x / 2
    yll = values["yllcorner"] if "yllcorner" in values else values["yllcenter"] - cs_y / 2
    ncols, nrows = int(values["ncols"]), int(values["nrows"])
    return {"ncols": ncols, "nrows": nrows, "cellsize_x": cs_x, "cellsize_y": cs_y,
            "box": (xll, yll, xll + ncols * cs_x, yll + nrows * cs_y),
            "corner": "xllcorner" in values, "nodata": values.get("nodata_value"), "header_lines": n_lines}


def _xyz_spacing(path: str, sniff: dict, max_lines: int = 5000) -> float:
    """Zellgroesse, wenn die ersten Zeilen einer XYZ-Datei ein regelmaessiges Gitter bilden, sonst 0.0
    (= unregelmaessige Punkte). Gitter: aufeinanderfolgende Zeilen laufen entlang einer Gitterzeile
    oder -spalte, fast jeder Schritt ist genau eine Zelle, alle Koordinaten liegen auf demselben
    Raster. Nur Vorab-Erkennung - beim Verarbeiten prueft der GDAL-XYZ-Treiber die ganze Datei."""
    pts = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for _ in range(int(sniff["skip"])):
            f.readline()
        for line in f:
            parts = _split_fields(line.strip(), sniff["separator"])
            if len(parts) < 3:
                continue
            try:
                pts.append((float(parts[0]), float(parts[1])))
            except ValueError:
                continue
            if len(pts) >= max_lines:
                break
    steps = []
    for (xa, ya), (xb, yb) in zip(pts, pts[1:]):
        dx, dy = abs(xb - xa), abs(yb - ya)
        if dy < 1e-6 < dx:
            steps.append(dx)
        elif dx < 1e-6 < dy:
            steps.append(dy)
    # Zeilenwechsel und einzelne Luecken sind erlaubt, Zufallspunkte haben kaum achsparallele Schritte
    if len(pts) < 3 or len(steps) < 0.8 * (len(pts) - 1):
        return 0.0
    d = sorted(steps)[len(steps) // 2]
    tol = max(GRID_TOLERANCE * d, 0.006)   # Koordinaten im ASCII oft auf cm gerundet
    if d < 0.01 or sum(1 for s in steps if abs(s - d) <= tol) < 0.9 * len(steps):
        return 0.0
    x0, y0 = pts[0]
    for x, y in pts:
        for v, v0 in ((x, x0), (y, y0)):
            k = (v - v0) / d
            if abs(k - round(k)) * d > tol:
                return 0.0
    return round(d, 6)


def _ascii_kind(path: str) -> dict:
    """Art einer ASCII-DSM-Datei aus Kopf bzw. Stichprobe: 'esri' (ESRI-ASCII-Grid), 'xyz_grid'
    (XYZ, regelmaessiges Gitter) oder 'points' (XYZ, unregelmaessige Punkte)."""
    header = _read_esri_header(path)
    if header:
        return {"kind": "esri", "header": header, "cellsize": header["cellsize_x"], "box": header["box"]}
    sniff = _sniff_ascii(path)
    d = _xyz_spacing(path, sniff)
    return {"kind": "xyz_grid" if d else "points", "sniff": sniff, "cellsize": d}


def _grid_set_check(grids: dict) -> tuple:
    """Prueft, ob Gitter-Dateien EIN Mosaik ergeben: quadratische, gleich grosse Zellen auf
    demselben Raster, keine flaechige Ueberlappung, Lage LV95.
    grids: {name: (box, zelle_x, zelle_y)}. Rueckgabe: (probleme, zellgroesse, gesamt_box)."""
    items = sorted(grids.items())
    ref_name, (ref_box, cs, _) = items[0]
    problems = []
    for name, (box, cx, cy) in items:
        if abs(cx - cy) > GRID_TOLERANCE * cx:
            problems.append(f"{name}: Zellen nicht quadratisch ({cx:g} x {cy:g} m)")
        elif abs(cx - cs) > GRID_TOLERANCE * cs:
            problems.append(f"{name}: Zellgroesse {cx:g} m, {ref_name} hat {cs:g} m")
        crs = _classify_crs(*box)
        if crs != "LV95":
            problems.append(f"{name}: Koordinaten nicht LV95 ({crs}: X {box[0]:.6g}-{box[2]:.6g}, "
                            f"Y {box[1]:.6g}-{box[3]:.6g})")
    for name, (box, _, _) in items[1:]:
        shift = [((v - o) / cs - round((v - o) / cs)) * cs for v, o in ((box[0], ref_box[0]), (box[1], ref_box[1]))]
        if any(abs(s) > GRID_TOLERANCE * cs for s in shift):
            problems.append(f"{name}: Gitter um {shift[0]:+.3f} / {shift[1]:+.3f} m gegen {ref_name} "
                            "verschoben - nicht auf demselben Raster")
    for i, (a, (ba, _, _)) in enumerate(items):
        for b, (bb, _, _) in items[i + 1:]:
            w = min(ba[2], bb[2]) - max(ba[0], bb[0])
            h = min(ba[3], bb[3]) - max(ba[1], bb[1])
            if w > cs / 2 and h > cs / 2:
                problems.append(f"{a}  <->  {b}: ueberlappen ({w:g} x {h:g} m)")
    union = (min(v[0][0] for _, v in items), min(v[0][1] for _, v in items),
             max(v[0][2] for _, v in items), max(v[0][3] for _, v in items))
    return problems, cs, union


def _read_lines(path: str, n: int, width: int = 120) -> list:
    """Die ersten n nicht-leeren Zeilen, je hoechstens width Zeichen (ESRI-Zeilen sind sehr lang)."""
    lines = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        while len(lines) < n:
            line = f.readline(width)
            if not line:
                break
            if not line.endswith("\n"):
                f.readline()                     # Rest der langen Zeile ueberspringen
            if line.strip():
                lines.append(line.rstrip("\r\n")[:width])
    return lines


def _raster_info(cfg: dict) -> None:
    """Datei-Info fuer den Raster-Tab (Aktion 'info', format 'raster') als JSON-Zeile auf stdout.
    Liest nur Koepfe bzw. die ersten Zeilen - auch bei GB-grossen Dateien sofort da."""
    input_dir = cfg["input_dir"]
    if not os.path.isdir(input_dir):
        raise FileNotFoundError(f"Input-Ordner nicht gefunden: {input_dir}")
    files = _list_inputs(input_dir, "ascii")
    result = {"format": "raster", "n_ascii": len(files), "n_las": len(_list_inputs(input_dir, "las")),
              "n_files": len(files), "sample": os.path.basename(files[0]) if files else "",
              "size_mb": round(sum(os.path.getsize(p) for p in files) / 1024 ** 2, 1)}
    if not files:
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return

    kinds, errors, counts = {}, [], {}
    for f in files:
        try:
            kinds[f] = _ascii_kind(f)
            counts[kinds[f]["kind"]] = counts.get(kinds[f]["kind"], 0) + 1
        except Exception as e:
            errors.append(f"{os.path.basename(f)}: {e}")
    mode = ""
    if len(counts) > 1:
        mode = "mixed"
    elif counts:
        mode = "points" if "points" in counts else "grid"
    heights = {}
    for f in files:
        h = _source_height(f, "ascii") or "ohne"
        heights[h] = heights.get(h, 0) + 1
    result.update({"kinds": counts, "mode": mode, "errors": errors, "vertical_tags": heights,
                   "name_parts": _source_name_parts(files[0]),
                   "cellsizes": sorted({round(k["cellsize"], 6) for k in kinds.values() if k["cellsize"]})})

    first = kinds.get(files[0])
    if first is None:
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return
    meta = {"file": os.path.basename(files[0]), "size": f"{os.path.getsize(files[0]) / 1024 ** 2:,.1f} MB"}
    if first["kind"] == "esri":
        h = first["header"]
        esri = {os.path.basename(f): (k["box"], k["header"]["cellsize_x"], k["header"]["cellsize_y"])
                for f, k in kinds.items() if k["kind"] == "esri"}
        problems, cs, union = _grid_set_check(esri)
        result.update({"extent": list(union), "set_problems": problems,
                       "grid_cells": [int(round((union[2] - union[0]) / cs)), int(round((union[3] - union[1]) / cs))],
                       "crs_guess": _classify_crs(*union),
                       "count_estimate": sum(k["header"]["ncols"] * k["header"]["nrows"] for k in kinds.values()
                                             if k["kind"] == "esri"),
                       "preview": _read_lines(files[0], h["header_lines"] + 2, 90)})
        nd = h["nodata"]
        meta.update({
            "version": "ESRI-ASCII-Grid (AAIGrid)",
            "grid": f"{h['ncols']} x {h['nrows']} Zellen a {h['cellsize_x']:g} m",
            "origin": "{} {:.2f} / {:.2f}".format("Ecke unten links" if h["corner"] else "Zellmitte unten links",
                                                 h["box"][0], h["box"][1]),
            "nodata": f"{nd:g}" if nd is not None else f"- (kein NODATA_value, {GRID_NODATA_SENTINEL:g} gilt als NoData)",
        })
    else:
        sniff = first["sniff"]
        result.update({"crs_guess": sniff["crs_guess"], "first_xy": sniff["first_xy"],
                       "count_estimate": sum(k["sniff"]["count_estimate"] for k in kinds.values() if "sniff" in k),
                       "preview": [line[:90] for line in sniff["preview"]]})
        meta.update(_ascii_metadata(files[0], sniff))
        meta["version"] = f"{KIND_LABELS[first['kind']]}  /  " + meta["version"]
        if first["kind"] == "xyz_grid":
            meta["grid"] = f"Abstand {first['cellsize']:g} m (aus den ersten Zeilen)"
    hints = _name_crs_hints(files[0])
    crs_text = {"LV95": "Werte passen zu LV95 (EPSG:2056)", "LV03": "Werte passen zu LV03 - NICHT unterstuetzt",
                "GRAD": "Werte in Grad - NICHT unterstuetzt"}.get(result["crs_guess"],
                                                                 "Werte weder LV95 noch LV03")
    height = next((x for x in ("LHN95", "LN02") if x in hints), "")
    meta.update({"crs_horizontal": f"kein CRS-Tag (ASCII)  |  {crs_text}",
                 "crs_vertical": "kein CRS-Tag (ASCII)  |  " + (f"Dateiname: {height}" if height
                                                                 else "Dateiname ohne Hoehenangabe"),
                 "name_hints": ", ".join(hints) or "- (keine CRS-Angabe im Dateinamen)"})
    result["first_meta"] = meta
    print(json.dumps(result, ensure_ascii=False), flush=True)


def _open_grids(files: list, kinds: dict) -> tuple:
    """Oeffnet die Gitter-Dateien mit GDAL (AAIGrid bzw. XYZ). Rueckgabe: ([(name, dataset)], grund);
    grund != '', wenn GDAL eine XYZ-Datei nicht als Gitter liest (dann Weg 'Punkte')."""
    from osgeo import gdal
    gdal.UseExceptions()
    grids = []
    for f in files:
        name = os.path.basename(f)
        driver = "AAIGrid" if kinds[f]["kind"] == "esri" else "XYZ"
        if driver == "XYZ":
            _log(f"  {name}: GDAL-XYZ-Treiber prueft das Gitter (liest die ganze Datei) ...")
        try:
            ds = gdal.OpenEx(f, gdal.OF_RASTER, allowed_drivers=[driver])
        except Exception as e:
            if driver == "XYZ":
                return [], f"{name}: {e}"
            raise ValueError(f"{name}: als ESRI-ASCII-Grid nicht lesbar - {e}")
        gt = ds.GetGeoTransform()
        if gt[2] or gt[4] or gt[5] >= 0:
            raise ValueError(f"{name}: Gitter gedreht oder nicht nordorientiert (Geotransform {gt})")
        grids.append((name, ds))
    return grids, ""


def _grid_box(ds) -> tuple:
    gt = ds.GetGeoTransform()
    return (gt[0], gt[3] + gt[5] * ds.RasterYSize, gt[0] + gt[1] * ds.RasterXSize, gt[3])


def _mosaic_grids(grids: list, bounds: tuple, cs: float, out_path: str, p0: float, pspan: float) -> dict:
    """Kopiert die Gitter-Quellen 1:1 (blockweise, ohne Resampling) auf ein gemeinsames Float32-Raster
    = Quellgitter ueber die Gesamtausdehnung. NoData der Quelle, NaN und -9999 -> CELL_NODATA.
    grids: [(name, gdal.Dataset)]. Rueckgabe: {valid, nodata, sentinel, zmin, zmax}."""
    from osgeo import gdal, osr
    import numpy as np
    xs = int(round((bounds[2] - bounds[0]) / cs))
    ys = int(round((bounds[3] - bounds[1]) / cs))
    out = gdal.GetDriverByName("GTiff").Create(
        out_path, xs, ys, 1, gdal.GDT_Float32,
        options=["TILED=YES", "BLOCKXSIZE=512", "BLOCKYSIZE=512", "COMPRESS=NONE", "BIGTIFF=YES"])
    out.SetGeoTransform((bounds[0], cs, 0.0, bounds[3], 0.0, -cs))
    srs = osr.SpatialReference()
    srs.SetFromUserInput(f"EPSG:2056+{HEIGHT_REFS[SBDSM_HEIGHT_REF]}")
    out.SetSpatialRef(srs)
    band = out.GetRasterBand(1)
    band.SetNoDataValue(CELL_NODATA)
    if sum(ds.RasterXSize * ds.RasterYSize for _, ds in grids) < xs * ys:
        band.Fill(CELL_NODATA)   # Flaeche zwischen den Dateien

    sentinel = np.float32(GRID_NODATA_SENTINEL)
    stats = {"valid": 0, "nodata": 0, "sentinel": 0, "zmin": None, "zmax": None}
    total_rows, done_rows = sum(ds.RasterYSize for _, ds in grids), 0
    try:
        for name, ds in grids:
            gt = ds.GetGeoTransform()
            xoff = int(round((gt[0] - bounds[0]) / cs))
            yoff = int(round((bounds[3] - gt[3]) / cs))
            src = ds.GetRasterBand(1)
            nd = src.GetNoDataValue()
            nx, ny = ds.RasterXSize, ds.RasterYSize
            rows_per_chunk = max(1, (64 * 1024 * 1024) // max(1, nx * 8))
            for y0 in range(0, ny, rows_per_chunk):
                rows = min(rows_per_chunk, ny - y0)
                raw = src.ReadAsArray(0, y0, nx, rows)
                # NoData im Quelltyp vergleichen, erst danach nach Float32
                bad = np.zeros(raw.shape, dtype=bool) if nd is None else (raw == nd)
                if raw.dtype.kind == "f":
                    bad |= np.isnan(raw)
                arr = raw.astype(np.float32)
                sent = (arr == sentinel) & ~bad
                bad |= sent
                n_bad = int(np.count_nonzero(bad))
                stats["sentinel"] += int(np.count_nonzero(sent))
                stats["nodata"] += n_bad
                stats["valid"] += arr.size - n_bad
                if n_bad < arr.size:
                    v = arr[~bad]
                    lo, hi = float(v.min()), float(v.max())
                    stats["zmin"] = lo if stats["zmin"] is None else min(stats["zmin"], lo)
                    stats["zmax"] = hi if stats["zmax"] is None else max(stats["zmax"], hi)
                arr[bad] = CELL_NODATA
                band.WriteArray(arr, xoff, yoff + y0)
                done_rows += rows
                _progress(p0 + pspan * done_rows / total_rows)
            _log(f"  {name}: {nx} x {ny} Zellen kopiert (Versatz {xoff} / {yoff})")
        band.FlushCache()
    finally:
        band = None
        out = None
    return stats


def _compare_dsm(src_path: str, dsm_path: str) -> tuple:
    """Kontrolle des Gitter-Wegs: (gueltige Zellen im DSM, Zellen mit anderem Wert oder anderem
    NoData-Status als im Mosaik). Mosaik: NoData CELL_NODATA, DSM: NoData -FLT_MAX."""
    from osgeo import gdal
    import numpy as np
    a_ds, b_ds = gdal.Open(src_path), gdal.Open(dsm_path)
    try:
        if (a_ds.RasterXSize, a_ds.RasterYSize) != (b_ds.RasterXSize, b_ds.RasterYSize):
            raise RuntimeError(f"DSM {b_ds.RasterXSize} x {b_ds.RasterYSize} statt "
                               f"{a_ds.RasterXSize} x {a_ds.RasterYSize} Zellen")
        a, b = a_ds.GetRasterBand(1), b_ds.GetRasterBand(1)
        xs, ys = a_ds.RasterXSize, a_ds.RasterYSize
        rows_per_chunk = max(1, (64 * 1024 * 1024) // max(1, xs * 4))
        nd_a, nd_b = np.float32(CELL_NODATA), np.float32(RASTER_NODATA)
        valid = diff = 0
        for y0 in range(0, ys, rows_per_chunk):
            rows = min(rows_per_chunk, ys - y0)
            va, vb = a.ReadAsArray(0, y0, xs, rows), b.ReadAsArray(0, y0, xs, rows)
            ma, mb = va != nd_a, vb != nd_b
            valid += int(np.count_nonzero(mb))
            diff += int(np.count_nonzero(ma != mb)) + int(np.count_nonzero(ma & mb & (va != vb)))
    finally:
        a_ds = b_ds = None
    return valid, diff


def _z_warning(zmin, zmax) -> None:
    if zmin is not None and (zmin < PLAUSIBLE_Z[0] or zmax > PLAUSIBLE_Z[1]):
        _log(f"  WARNUNG       : Z ausserhalb des fuer die Schweiz plausiblen Bereichs "
             f"({PLAUSIBLE_Z[0]:g}-{PLAUSIBLE_Z[1]:g} m) - andere NoData-Werte oder Ausreisser in der "
             "Quelle? Die Werte werden unveraendert uebernommen.")


def _raster_from_grids(grids: list, bounds: tuple, cs: float, run_dir: Path, dsm_path: str,
                       hs_path: str, workers: int) -> None:
    """Weg 'Gitter': Werte 1:1 (GSD = Zellgroesse, keine Interpolation, keine Lochfuellung)."""
    from osgeo import gdal
    gdal.UseExceptions()
    xs, ys = int(round((bounds[2] - bounds[0]) / cs)), int(round((bounds[3] - bounds[1]) / cs))
    _log(f"\n[2/3] Mosaik 1:1 auf dem Quellgitter: {xs} x {ys} Zellen a {cs:g} m "
         f"({xs * ys / 1e6:.1f} Mio.) ...")
    mosaic = str(run_dir / "dsm_grid.tif")
    stats = _mosaic_grids(grids, bounds, cs, mosaic, 0.0, 0.55)
    _log(f"  Zellen mit Hoehe : {_fmt_count(stats['valid'])}  |  NoData: {_fmt_count(stats['nodata'])}")
    if stats["sentinel"]:
        _log(f"  davon {_fmt_count(stats['sentinel'])} Zelle(n) mit {GRID_NODATA_SENTINEL:g} ohne NoData-"
             "Deklaration in der Quelle -> NoData")
    if not stats["valid"]:
        raise RuntimeError("Keine gueltige Hoehe in den Quellen - nur NoData.")
    _log(f"  Z-Bereich        : {stats['zmin']:.2f} - {stats['zmax']:.2f} m")
    _z_warning(stats["zmin"], stats["zmax"])

    _log(f"\n[3/3] DSM + Hillshade ...")
    raw_hs = str(run_dir / "hillshade_raw.tif")
    gdal.DEMProcessing(raw_hs, mosaic, "hillshade",
                       options=gdal.DEMProcessingOptions(computeEdges=True)).FlushCache()
    _progress(0.7)
    _finish_rasters(mosaic, raw_hs, dsm_path, hs_path, bounds, cs, SBDSM_HEIGHT_REF, workers)
    _progress(0.9)
    valid, diff = _compare_dsm(mosaic, dsm_path)
    if diff or valid != stats["valid"]:
        raise RuntimeError(f"Kontrolle 1:1 fehlgeschlagen: {diff} Zelle(n) weichen vom Mosaik ab, "
                           f"{valid} statt {stats['valid']} gueltige Zellen im DSM")
    _log(f"  Kontrolle OK: alle {_fmt_count(valid)} Hoehen 1:1 aus den Quellen, NoData unveraendert")
    _progress(1.0)


def _raster_from_points(files: list, kinds: dict, run_dir: Path, pdal_exe: str, workers: int,
                        chunk_bytes: int, gsd: float, raster_dir: str, names: tuple) -> None:
    """Weg 'Punkte': ASCII -> LAZ (ohne Z <= -9999) -> 1-km-Stuecke -> IDW mit der gewaehlten GSD,
    kleine Loecher interpoliert - derselbe Raster-Code wie 'Create DSM-Raster' der Kachel-Tabs."""
    labels = {f: os.path.basename(f) for f in files}
    read_fmt = {f: (kinds[f]["sniff"]["columns"].split(), kinds[f]["sniff"]["separator"],
                    int(kinds[f]["sniff"]["skip"])) for f in files}
    _log(f"\n[2/3] ASCII -> LAZ (readers.text, Z <= {GRID_NODATA_SENTINEL:g} = NoData wird verworfen) ...")
    sources, origin_of = _ascii_to_laz_all(files, read_fmt, run_dir, pdal_exe, workers, chunk_bytes,
                                           OUT_POINT_FORMAT, 0.0, 0.3, drop_nodata=True)
    headers = {s: _read_las_header(s) for s in sources}
    not_lv95, file_box = [], {}
    for s, h in headers.items():
        crs = _classify_crs(h["minx"], h["miny"], h["maxx"], h["maxy"])
        if crs != "LV95":
            not_lv95.append(f"{labels[origin_of[s]]}: {crs}")
        o, box = origin_of[s], (h["minx"], h["miny"], h["maxx"], h["maxy"])
        old = file_box.get(o, box)
        file_box[o] = (min(old[0], box[0]), min(old[1], box[1]), max(old[2], box[2]), max(old[3], box[3]))
    if not_lv95:
        raise ValueError("Quelldaten nicht in LV95 (EPSG:2056):\n  " + "\n  ".join(sorted(not_lv95)[:10])
                         + "\n" + NOT_LV95_HINT)
    clashes = _overlapping_pairs(file_box)
    if clashes:
        raise ValueError("Input-Dateien ueberlappen raeumlich - im Mosaik entstuenden doppelte Punkte:\n  "
                         + "\n  ".join(f"{labels[a]}  <->  {labels[b]}" for a, b in clashes[:10])
                         + "\nDateien getrennt verarbeiten.")
    zmin = min(h["minz"] for h in headers.values())
    zmax = max(h["maxz"] for h in headers.values())
    _log(f"  Punkte        : {_fmt_count(sum(h['count'] for h in headers.values()))}  |  "
         f"Z {zmin:.2f} - {zmax:.2f} m")
    _z_warning(zmin, zmax)

    _log(f"  in {RASTER_WORK_CELL_M:g}-m-Stuecke zerlegen (pdal tile) ...")
    pieces, failed = [], []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_split_worker, (s, str(run_dir / "p" / f"{i:05d}"), pdal_exe,
                                          RASTER_WORK_CELL_M, OUT_POINT_FORMAT)): s
                for i, s in enumerate(sources)}
        for done, fut in enumerate(as_completed(futs), 1):
            try:
                _, ps = fut.result()
                pieces += [p for _, p, count in ps if count]
            except Exception as e:
                failed.append(f"{labels[origin_of[futs[fut]]]}: {e}")
            _progress(0.3 + 0.15 * done / len(sources))
    if failed:
        raise RuntimeError("Zerlegen fehlgeschlagen:\n  " + "\n  ".join(failed[:10]))
    for s in sources:
        _discard(s)   # Zwischen-LAZ nicht mehr noetig (Platz im Staging)

    _log(f"\n[3/3] DSM + Hillshade aus {len(pieces)} Stueck(en) (IDW) ...")
    _build_dsm_raster(sorted(pieces), raster_dir, "", "", gsd, SBDSM_HEIGHT_REF, run_dir / "r", workers,
                      pdal_exe, 0.45, 0.55, names=names)


# ─── Hauptablauf (Aktion 'raster') ─────────────────────────────────────────────
#   1) Dateien analysieren: ESRI-ASCII-Grid / XYZ-Gitter / XYZ-Punkte. Alle Dateien eines
#      Ordners = EIN Mosaik derselben Art; Lage LV95, flaechige Ueberlappung -> Abbruch.
#   2a) Gitter: Werte 1:1 auf dem Quellgitter (GSD = Zellgroesse, keine Interpolation, keine
#       Lochfuellung); NoData der Quelle und -9999 -> NoData
#   2b) Punkte: ASCII -> LAZ -> 1-km-Stuecke -> IDW mit gewaehlter GSD, Loecher <= 900 m2 gefuellt
#   3) DSM (Float32, NoData -FLT_MAX, EPSG:2056+5728) + Hillshade (Byte, NoData 255, EPSG:2056),
#      je mit .tfw, Namen <Jahr>_<AREA>_DSM|hillshade_<GSD>_LV95_LN02.tif, Kontrolle
# Hoehen werden nie umgerechnet, nur als LN02 getaggt (Hoehenangabe im Dateinamen -> nur Warnung).
def _process_raster(cfg: dict) -> None:
    t_start     = time.time()
    input_dir   = cfg["input_dir"]
    raster_dir  = str(cfg.get("raster_dir") or "").strip()
    jahr        = str(cfg.get("jahr") or "").strip()
    area        = str(cfg.get("area") or "").strip()
    gsd_points  = float(cfg.get("gsd") or 0.0)
    workers     = max(1, int(cfg.get("num_workers") or 4))
    keep        = bool(cfg.get("keep_staging"))
    chunk_bytes = int(float(cfg.get("ascii_chunk_mb") or ASCII_CHUNK_MB) * 1024 ** 2)
    staging_dir = cfg.get("staging_dir") or os.path.join(raster_dir, "_staging")

    # --- Eingaben pruefen, bevor irgendetwas geschrieben wird ---
    problems = _sbdsm_name_problems(jahr, area)
    if problems:
        raise ValueError("Benennung fuer SB_DSM nicht moeglich:\n  " + "\n  ".join(problems))
    if not raster_dir:
        raise ValueError("Raster-Output-Ordner fehlt.")
    if not os.path.isdir(input_dir):
        raise FileNotFoundError(f"Input-Ordner nicht gefunden: {input_dir}")
    files = _list_inputs(input_dir, "ascii")
    if not files:
        raise FileNotFoundError(f"Keine ASCII-Dateien ({'/'.join(ASCII_EXTENSIONS)}) gefunden in: {input_dir}")
    labels = {f: os.path.basename(f) for f in files}

    _log(f"Input-Ordner   : {input_dir}  ({len(files)} Datei(en))")
    _log(f"Raster-Output  : {raster_dir}")
    _log(f"Referenzsystem : DSM EPSG:2056+{HEIGHT_REFS[SBDSM_HEIGHT_REF]} (LV95 / {SBDSM_HEIGHT_REF}), "
         "Hillshade EPSG:2056 - fix fuer SB_DSM, nur CRS-Tag, keine Umrechnung")

    _log("\n[1/3] Input analysieren ...")
    kinds = {}
    for f in files:
        k = kinds[f] = _ascii_kind(f)
        if k["kind"] == "esri":
            h = k["header"]
            detail = (f"{h['ncols']} x {h['nrows']} Zellen a {h['cellsize_x']:g} m, NoData "
                      + (f"{h['nodata']:g}" if h["nodata"] is not None else "-"))
        elif k["kind"] == "xyz_grid":
            detail = f"Abstand {k['cellsize']:g} m (erste Zeilen), Spalten {k['sniff']['columns']}"
        else:
            detail = f"Spalten {k['sniff']['columns']}, ~{_fmt_count(k['sniff']['count_estimate'])} Punkte"
        _log(f"  {labels[f]}: {KIND_LABELS[k['kind']]}  |  {detail}")
    found = sorted({k["kind"] for k in kinds.values()})
    if len(found) > 1:
        raise ValueError("Der Ordner mischt " + " und ".join(KIND_LABELS[k] for k in found) + " - alle "
                         "Dateien eines Ordners ergeben EIN Mosaik derselben Art. Dateien getrennt verarbeiten.")
    mode = "points" if found == ["points"] else "grid"

    grids = []
    if mode == "grid":
        grids, reason = _open_grids(files, kinds)
        if reason:
            _log(f"  WARNUNG: GDAL liest die XYZ-Datei nicht als regelmaessiges Gitter ({reason}) - "
                 "sie wird als Punktwolke behandelt (IDW, GSD aus dem GUI).")
            mode = "points"
    if mode == "grid":
        problems, gsd, union = _grid_set_check(
            {name: (_grid_box(ds), ds.GetGeoTransform()[1], -ds.GetGeoTransform()[5]) for name, ds in grids})
        if problems:
            raise ValueError("Die Gitter ergeben kein gemeinsames Mosaik:\n  " + "\n  ".join(problems[:10])
                             + ("\n" + NOT_LV95_HINT if any("nicht LV95" in p for p in problems) else ""))
        _log(f"  Weg           : Gitter - Werte 1:1, GSD = Zellgroesse {gsd:g} m, keine Interpolation")
    else:
        lv03 = [labels[f] for f in files if kinds[f]["sniff"]["crs_guess"] == "LV03"]
        if lv03:
            raise ValueError(f"Quelldaten in LV03 statt LV95: {lv03[:10]}\n{NOT_LV95_HINT}")
        gsd = gsd_points or (_source_name_parts(files[0])["gsd"] or 0.0)
        if not 0 < gsd <= 100:
            raise ValueError(f"Raster-Aufloesung (GSD) {gsd:g} m ungueltig - fuer Punkte im GUI angeben.")
        _log(f"  Weg           : Punkte - IDW mit GSD {gsd:g} m, Loecher <= {FILL_MAX_HOLE_AREA_M2:g} m2 interpoliert")

    # Hoehenangabe und GSD im Dateinamen der Quelle gegen das Ergebnis - nur Warnung
    for f in files:
        h = _source_height(f, "ascii")
        if h and h != SBDSM_HEIGHT_REF:
            _log(f"  WARNUNG: {labels[f]} ist im Dateinamen als {h} bezeichnet - die Hoehen werden NICHT "
                 f"umgerechnet, nur als {SBDSM_HEIGHT_REF} getaggt. Hoehenbezug pruefen, sonst zuerst mit "
                 "GeoSuite/REFRAME nach LN02 transformieren!")
        name_gsd = _source_name_parts(f)["gsd"]
        if name_gsd and abs(name_gsd - gsd) > GRID_TOLERANCE * gsd:
            _log(f"  WARNUNG: {labels[f]}: GSD im Dateinamen {name_gsd:g} m, Raster wird mit {gsd:g} m erstellt")

    dsm_name, hs_name = _sbdsm_raster_names(jahr, area, gsd)
    dsm_path, hs_path = os.path.join(raster_dir, dsm_name), os.path.join(raster_dir, hs_name)
    pdal_exe = _resolve_pdal_exe(cfg) if mode == "points" else ""
    Path(raster_dir).mkdir(parents=True, exist_ok=True)
    staging_drive = Path(os.path.abspath(staging_dir)).anchor
    if not os.path.isdir(staging_drive):
        raise FileNotFoundError(f"Laufwerk des Staging-Ordners nicht verfuegbar: {staging_dir}")
    run_dir = _unique_dir(Path(staging_dir), _sbdsm_job_name(jahr, area))
    if mode == "points":
        _check_path_lengths(files, run_dir, "", 0, "")
    run_dir.mkdir(parents=True)
    _log(f"  DSM           : {dsm_name}  (+ .tfw)")
    _log(f"  Hillshade     : {hs_name}  (+ .tfw)")
    _log(f"  Staging       : {run_dir}  |  Parallel: {workers}" + (f"  |  pdal.exe: {pdal_exe}" if pdal_exe else ""))

    outputs = [p for base in (dsm_path, hs_path)
               for p in (base, os.path.splitext(base)[0] + ".tfw", base + ".aux.xml")]
    before = {p: os.path.getmtime(p) for p in outputs if os.path.isfile(p)}
    try:
        try:
            if mode == "grid":
                _raster_from_grids(grids, union, gsd, run_dir, dsm_path, hs_path, workers)
            else:
                _raster_from_points(files, kinds, run_dir, pdal_exe, workers, chunk_bytes, gsd, raster_dir,
                                    (dsm_name, hs_name))
        except Exception:
            # Abbruch nach dem Schreiben (z.B. Kontrolle): in DIESEM Lauf geschriebene, ungepruefte Raster
            # nicht liegen lassen, sonst nimmt sie ein spaeterer GDWH-Import mit. Unberuehrte bleiben.
            removed = [p for p in outputs if os.path.isfile(p) and before.get(p) != os.path.getmtime(p)]
            for p in removed:
                _discard(p)
            if removed:
                _log(f"  Unvollstaendige Raster entfernt: {', '.join(os.path.basename(p) for p in removed)}")
            raise
        _log("\nZusammenfassung:")
        _log(f"  DSM       : {dsm_path}  (+ .tfw)")
        _log(f"  Hillshade : {hs_path}  (+ .tfw)")
        _log(f"  -> GDWH-Import als SB_DSM (TileKey 1000)")
        dt = time.time() - t_start
        _log(f"\nLaufzeit: {int(dt // 60)}m {int(dt % 60):02d}s")
        _log("Fertig.")
    finally:
        grids = None
        if keep:
            _log(f"\nStaging-Dateien behalten: {run_dir}")
        else:
            shutil.rmtree(run_dir, ignore_errors=True)
            if not cfg.get("staging_dir"):
                try:
                    os.rmdir(staging_dir)  # Standard-Unterordner '_staging', nur falls leer
                except OSError:
                    pass


# ─── Batch (Aktion 'raster' mit 'batch_list') ──────────────────────────────────
# TXT-Liste mit Ordner-Pfaden, je Zeile ein Ordner = EIN Mosaik wie im Einzellauf. Output je
# Ordner in <Ordner>\dsm, Jahr/AREA aus dem Dateinamen (sonst Ordnername), GSD bei Gittern die
# Zellgroesse, bei Punkten aus dem Dateinamen (sonst GUI-Wert). Ordner mit Problemen werden
# uebersprungen, ein Fehler stoppt den Batch nicht - Zusammenfassung am Schluss.
BATCH_OUT_SUBDIR = "dsm"


class BatchIncomplete(RuntimeError):
    """Batch beendet, aber nicht alle Ordner verarbeitet (Details in der Zusammenfassung)."""


def _read_batch_list(path: str) -> list:
    """Ordner-Pfade aus einer TXT-Liste: eine Zeile je Ordner; leere Zeilen und '#'-Kommentare
    ignoriert, Anfuehrungszeichen (Windows 'Als Pfad kopieren') entfernt, relative Pfade bezogen
    auf den Ordner der Liste, doppelte Eintraege nur einmal. Reine Standardbibliothek (auch GUI)."""
    raw = open(path, "rb").read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252")          # Notepad 'ANSI' mit Umlauten
    base = os.path.dirname(os.path.abspath(path))
    folders, seen = [], set()
    for line in text.splitlines():
        entry = line.strip().strip('"').strip("'").strip()
        if not entry or entry.startswith("#"):
            continue
        full = os.path.normpath(entry if os.path.isabs(entry) else os.path.join(base, entry))
        key = os.path.normcase(full)
        if key not in seen:
            seen.add(key)
            folders.append(full)
    return folders


def _batch_plan(folder: str, gsd_fallback: float = 0.0) -> dict:
    """Vorab-Analyse EINES Batch-Ordners (nur Koepfe bzw. erste Zeilen): Art, Jahr/AREA, GSD,
    Zielnamen. problem != '' -> Ordner wird uebersprungen."""
    plan = {"path": folder, "out_dir": os.path.join(folder, BATCH_OUT_SUBDIR), "problem": "", "warnings": [],
            "n_files": 0, "mode": "", "kind": "", "jahr": "", "area": "", "name_from": "", "gsd": None,
            "gsd_from": "", "names": [], "existing": []}
    if not os.path.isdir(folder):
        plan["problem"] = ("ist eine Datei - erwartet wird ein Ordner" if os.path.isfile(folder)
                           else "Ordner nicht gefunden")
        return plan
    files = _list_inputs(folder, "ascii")
    plan["n_files"] = len(files)
    if not files:
        plan["problem"] = f"keine ASCII-Dateien ({'/'.join(ASCII_EXTENSIONS)})"
        return plan
    try:
        kinds = {f: _ascii_kind(f) for f in files}
    except Exception as e:
        plan["problem"] = str(e)
        return plan
    found = sorted({k["kind"] for k in kinds.values()})
    if len(found) > 1:
        plan["problem"] = "mischt " + " und ".join(KIND_LABELS[k] for k in found)
        return plan
    plan["kind"] = KIND_LABELS[found[0]]
    plan["mode"] = "points" if found == ["points"] else "grid"

    if plan["mode"] == "grid":
        cellsizes = sorted({round(k["cellsize"], 6) for k in kinds.values()})
        if len(cellsizes) > 1:
            plan["problem"] = "verschiedene Zellgroessen: " + ", ".join(f"{c:g} m" for c in cellsizes)
            return plan
        esri = {os.path.basename(f): (k["box"], k["header"]["cellsize_x"], k["header"]["cellsize_y"])
                for f, k in kinds.items() if k["kind"] == "esri"}
        if esri:
            problems = _grid_set_check(esri)[0]
            if problems:
                plan["problem"] = problems[0] + (f" (+{len(problems) - 1} weitere)" if len(problems) > 1 else "")
                return plan
        plan["gsd"], plan["gsd_from"] = cellsizes[0], "Zellgroesse"
    not_lv95 = [k["sniff"]["crs_guess"] for k in kinds.values() if "sniff" in k and k["sniff"]["crs_guess"] != "LV95"]
    if not_lv95:
        plan["problem"] = f"Koordinaten nicht LV95 ({not_lv95[0]})"
        return plan

    parts = [_source_name_parts(f) for f in files]
    if len({(p["jahr"], p["area"]) for p in parts}) > 1:
        plan["problem"] = "Dateinamen nennen verschiedene Jahr/AREA - ein Ordner ergibt EIN SB_DSM"
        return plan
    jahr, area, plan["name_from"] = parts[0]["jahr"], parts[0]["area"], "Dateiname"
    if not (jahr and area):
        dir_parts = _source_name_parts(folder, is_file=False)
        if dir_parts["jahr"] and dir_parts["area"]:
            jahr, area, plan["name_from"] = dir_parts["jahr"], dir_parts["area"], "Ordnername"
    plan["jahr"], plan["area"] = jahr, area
    if not (jahr and area):
        plan["problem"] = ("Jahr/AREA weder aus dem Dateinamen noch aus dem Ordnernamen ableitbar "
                           "(erwartet <Jahr>_<AREA>_DSM_...)")
        return plan
    problems = _sbdsm_name_problems(jahr, area)
    if problems:
        plan["problem"] = f"Name aus {plan['name_from']}: {problems[0]}"
        return plan
    if plan["mode"] == "points":
        if parts[0]["gsd"]:
            plan["gsd"], plan["gsd_from"] = parts[0]["gsd"], "Dateiname"
        elif gsd_fallback and gsd_fallback > 0:
            plan["gsd"], plan["gsd_from"] = float(gsd_fallback), "GUI"
        else:
            plan["problem"] = "XYZ-Punkte ohne GSD im Dateinamen und keine GSD im GUI"
            return plan
    elif parts[0]["gsd"] and abs(parts[0]["gsd"] - plan["gsd"]) > GRID_TOLERANCE * plan["gsd"]:
        plan["warnings"].append(f"GSD im Dateinamen {parts[0]['gsd']:g} m, Zellgroesse {plan['gsd']:g} m")
    lhn95 = [os.path.basename(f) for f in files if _source_height(f, "ascii") == "LHN95"]
    if lhn95:
        plan["warnings"].append(f"{len(lhn95)} Datei(en) im Namen als LHN95 bezeichnet - wird NICHT umgerechnet")
    plan["names"] = list(_sbdsm_raster_names(jahr, area, plan["gsd"]))
    plan["existing"] = [n for n in plan["names"] if os.path.isfile(os.path.join(plan["out_dir"], n))]
    return plan


def _raster_batch_info(cfg: dict) -> None:
    """Datei-Info fuer den Batch: Vorab-Analyse aller Ordner der Liste, JSON-Zeile auf stdout."""
    path = cfg["batch_list"]
    if not os.path.isfile(path):
        raise FileNotFoundError(f"TXT-Liste nicht gefunden: {path}")
    plans = [_batch_plan(f, float(cfg.get("gsd") or 0.0)) for f in _read_batch_list(path)]
    kinds = {}
    for p in plans:
        if p["kind"]:
            kinds[p["kind"]] = kinds.get(p["kind"], 0) + 1
    print(json.dumps({"format": "raster", "batch": True, "list": path, "gsd": float(cfg.get("gsd") or 0.0),
                      "folders": plans,
                      "n_folders": len(plans), "n_ok": sum(1 for p in plans if not p["problem"]),
                      "kinds": kinds}, ensure_ascii=False), flush=True)


def _process_raster_batch(cfg: dict) -> None:
    t_start = time.time()
    path = cfg["batch_list"]
    if not os.path.isfile(path):
        raise FileNotFoundError(f"TXT-Liste nicht gefunden: {path}")
    gsd_fallback = float(cfg.get("gsd") or 0.0)
    plans = [_batch_plan(f, gsd_fallback) for f in _read_batch_list(path)]
    if not plans:
        raise ValueError(f"TXT-Liste enthaelt keinen Ordner-Pfad: {path}")
    n = len(plans)
    _log(f"Batch-Liste    : {path}  ({n} Ordner)")
    _log(f"Output         : je Ordner <Ordner>\\{BATCH_OUT_SUBDIR}")
    for i, p in enumerate(plans, 1):
        state = f"UEBERSPRINGEN - {p['problem']}" if p["problem"] else \
            f"{p['kind']}, {p['names'][0]}  (Name aus {p['name_from']}, GSD {p['gsd_from']})"
        _log(f"  [{i}/{n}] {p['path']}: {state}")

    results = []   # (ordner, status, text)
    for i, p in enumerate(plans):
        _log(f"\n{'=' * 78}\n[{i + 1}/{n}] {p['path']}\n{'=' * 78}")
        if p["problem"]:
            _log(f"UEBERSPRUNGEN: {p['problem']}")
            results.append((p["path"], "UEBERSPRUNGEN", p["problem"]))
            _progress((i + 1) / n)
            continue
        _PROGRESS_RANGE[:] = [i / n, 1.0 / n]
        try:
            _process_raster(dict(cfg, input_dir=p["path"], raster_dir=p["out_dir"], jahr=p["jahr"],
                                 area=p["area"], gsd=p["gsd"]))
            results.append((p["path"], "OK", os.path.join(BATCH_OUT_SUBDIR, p["names"][0])))
        except Exception as e:
            _log(f"\n[FEHLER] {e}")
            if not isinstance(e, (ValueError, FileNotFoundError)):
                _log(traceback.format_exc())   # unerwarteter Fehler: Details fuers Log
            results.append((p["path"], "FEHLER", str(e).strip().splitlines()[0] if str(e).strip() else repr(e)))
            try:
                os.rmdir(p["out_dir"])   # nur falls leer geblieben
            except OSError:
                pass
        finally:
            _PROGRESS_RANGE[:] = [0.0, 1.0]
            _progress((i + 1) / n)

    n_ok = sum(1 for _, s, _ in results if s == "OK")
    _log(f"\n{'=' * 78}\nBatch-Zusammenfassung: {n_ok} von {n} Ordner(n) OK")
    for folder, status, text in results:
        _log(f"  {status:<13} {folder}  ->  {text}")
    dt = time.time() - t_start
    _log(f"\nLaufzeit Batch: {int(dt // 3600)}h {int(dt % 3600 // 60):02d}m {int(dt % 60):02d}s")
    if n_ok < n:
        raise BatchIncomplete(f"{n - n_ok} von {n} Ordner(n) nicht verarbeitet (uebersprungen/Fehler) - "
                              "siehe Batch-Zusammenfassung.")
    _log("Batch fertig.")


# ─── Hauptablauf (Aktion 'process') ────────────────────────────────────────────
#   1) nur ASCII: jede Datei -> Zwischen-LAZ (readers.text, parallel; grosse gemergte
#      Dateien vorher an Zeilengrenzen in Teile zerlegt)
#   2) Header: Punktzahl, LV95-/Z-Plausibilitaet, Ueberlappung innerhalb eines
#      Kachelsatzes, Grid-Zellen im Datenbereich laden
#   3) jede Quelle mit 'pdal tile' in Kachelstuecke zerlegen (parallel, Streaming)
#   4) je Kachelsatz und Grid-Zelle alle Stuecke mergen (+ optional Thinning) -> Endkachel
#      im Zielformat, validiert und atomar geschrieben (parallel)
#   5) Punktbilanz Input = Output (+ ausserhalb Grid, - ausgeduennt)
#   6) optional: DSM-Raster + Hillshade aus den fertigen Kacheln, Staging aufraeumen
# Jede Quelldatei wird genau einmal gelesen - auch ein grosses gemergtes Gebiet wird
# nicht pro Kachel erneut eingelesen.
#
# Benennung / CRS (beide Formate): <Jahr>_<AREA>_TIN_DSM[_thinNN]_<NAME>_LV95_<LN02|LHN95>.<laz|las>,
# ALLE Dateien eines Ordners = EIN Kachelsatz. Der Hoehenbezug kommt aus 'height_ref'; CRS-Tag
# (LAS/LAZ) bzw. CRS-Angabe im Dateinamen (ASCII) der Quelle wird ignoriert - Widerspruch -> nur
# Warnung, so laesst sich eine falsche Angabe der Quelle korrigieren. Nichts wird umgerechnet.
def _process(cfg: dict) -> None:
    t_start       = time.time()
    input_dir     = cfg["input_dir"]
    output_dir    = cfg["output_dir"]
    grid_path     = cfg["grid_shape_path"]
    fmt           = cfg.get("format", "ascii")
    columns       = (cfg.get("columns") or "X Y Z").split()
    sep_key       = cfg.get("separator", "space")
    skip          = int(cfg.get("skip") or 0)
    jahr          = str(cfg.get("jahr") or "").strip()
    area          = str(cfg.get("area") or "").strip()
    height_ref    = str(cfg.get("height_ref") or "LN02").strip().upper()
    thin_m        = float(cfg.get("thin_m") or 0.0)
    out_format    = str(cfg.get("out_format") or "laz").strip().lower()
    create_raster = bool(cfg.get("create_raster"))
    raster_dir    = str(cfg.get("raster_dir") or "").strip()
    gsd           = float(cfg.get("gsd") or 0.5)
    workers       = max(1, int(cfg.get("num_workers") or 4))
    keep          = bool(cfg.get("keep_staging"))
    chunk_bytes   = int(float(cfg.get("ascii_chunk_mb") or ASCII_CHUNK_MB) * 1024 ** 2)
    staging_dir   = cfg.get("staging_dir") or os.path.join(output_dir, "_staging")
    pdal_exe      = _resolve_pdal_exe(cfg)

    # --- Eingaben pruefen, bevor irgendetwas geschrieben wird ---
    if fmt not in ("ascii", "las"):
        raise ValueError(f"Unbekanntes Input-Format '{fmt}' (ascii | las)")
    if fmt == "ascii":
        if sep_key not in SEPARATORS:
            raise ValueError(f"Trennzeichen '{sep_key}' ungueltig ({' | '.join(SEPARATORS)})")
        if not {"X", "Y", "Z"} <= set(columns):
            raise ValueError(f"Spalten '{' '.join(columns)}' muessen X, Y und Z enthalten")
    if not re.fullmatch(r"\d{4}", jahr):
        raise ValueError(f"Jahr '{jahr}' ungueltig - vierstellig erwartet, z.B. 2021")
    if not area or re.search(r'[<>:"/\\|?*\s]', area):
        raise ValueError(f"AREA '{area}' fehlt oder enthaelt unzulaessige Zeichen/Leerzeichen")
    if height_ref not in HEIGHT_REFS:
        raise ValueError(f"Hoehenbezug '{height_ref}' unbekannt ({' | '.join(HEIGHT_REFS)})")
    if thin_m and not any(abs(thin_m - t) < 1e-9 for t in THIN_OPTIONS_M):
        raise ValueError(f"Thinning {thin_m:g} m nicht vorgesehen ({', '.join(f'{t:g}' for t in THIN_OPTIONS_M)})")
    if out_format not in OUT_FORMATS:
        raise ValueError(f"Output-Format '{out_format}' ungueltig ({' | '.join(OUT_FORMATS)})")
    if create_raster:
        if not raster_dir:
            raise ValueError("Raster-Output-Ordner fehlt (Option 'Create DSM-Raster').")
        if not 0 < gsd <= 100:
            raise ValueError(f"Raster-Aufloesung {gsd:g} m ungueltig")
    if not os.path.isdir(input_dir):
        raise FileNotFoundError(f"Input-Ordner nicht gefunden: {input_dir}")
    if os.path.normcase(os.path.abspath(input_dir)) == os.path.normcase(os.path.abspath(output_dir)):
        raise ValueError("Output-Ordner muss sich vom Input-Ordner unterscheiden "
                         "(gleiche Namenskonvention -> Quelldateien koennten ueberschrieben werden).")
    files = _list_inputs(input_dir, fmt)
    if not files:
        raise FileNotFoundError(f"Keine {fmt.upper()}-Dateien gefunden in: {input_dir}")
    if fmt == "ascii":
        # LV03 frueh erkennen (erste Zeilen je Datei), bevor alles eingelesen wird.
        # Massgebend bleibt die Header-Pruefung in Schritt 2.
        lv03 = [os.path.basename(f) for f in files if _sniff_ascii(f)["crs_guess"] == "LV03"]
        if lv03:
            raise ValueError(f"Quelldaten in LV03 statt LV95: {lv03[:10]}\n{NOT_LV95_HINT}")
    groups   = {_las_tile_base(jahr, area, thin_m): list(files)}
    out_ext  = out_format
    suffix   = _height_suffix(height_ref)
    group_of = {f: b for b, members in groups.items() for f in members}

    point_format = OUT_POINT_FORMAT
    if fmt == "ascii" and {"Red", "Green", "Blue"} & set(columns):
        point_format = OUT_POINT_FORMAT_RGB

    Path(output_dir).mkdir(parents=True, exist_ok=True)
    staging_drive = Path(os.path.abspath(staging_dir)).anchor
    if not os.path.isdir(staging_drive):
        raise FileNotFoundError(f"Laufwerk des Staging-Ordners nicht verfuegbar: {staging_dir}")
    run_dir = _unique_dir(Path(staging_dir), _job_name(jahr, area, thin_m, height_ref))
    _check_path_lengths(files, run_dir, output_dir, max(len(b) for b in groups), suffix)
    run_dir.mkdir(parents=True)

    _log(f"Input-Ordner   : {input_dir}")
    _log(f"Input-Format   : {fmt.upper()}  ({len(files)} Datei(en))")
    if fmt == "ascii":
        _log(f"  Spalten      : {' '.join(columns)}  |  Trennzeichen: {sep_key}  |  Kopfzeilen: {skip}")
    _log(f"Output-Ordner  : {output_dir}")
    _log(f"Grid-Shape     : {grid_path}")
    _log(f"Referenzsystem : EPSG:2056 + {HEIGHT_REFS[height_ref]} (LV95 / {height_ref}) - nur CRS-Tag, "
         "keine Umrechnung; CRS-Angaben der Quelle werden ersetzt")
    _log(f"Thinning       : " + (f"{thin_m:g} m (filters.sample, Mindestabstand)" if thin_m else "kein"))
    _log(f"Raster         : " + (f"DSM + Hillshade, GSD {gsd:g} m -> {raster_dir}" if create_raster else "nein"))
    _log(f"pdal.exe       : {pdal_exe}")
    _log(f"Parallel       : {workers} Prozess(e)  |  Staging: {run_dir}")
    _log(f"Kachelsaetze   : {len(groups)}")
    for b, members in groups.items():
        _log(f"  {b}_<NAME>{suffix}.{out_ext}  <-  " + (os.path.basename(members[0]) if len(members) == 1
                                                         else f"{len(members)} Dateien"))

    names = {f: os.path.basename(f) for f in files}
    origin_of = {f: f for f in files}   # Quelle (Zwischen-LAZ bzw. Input-LAZ) -> Input-Datei
    try:
        # 1) ASCII -> Zwischen-LAZ (grosse Dateien in Teilen parallel)
        if fmt == "ascii":
            _log("\n[1/4] ASCII -> LAZ (readers.text) ...")
            sources, laz_origin = _ascii_to_laz_all(files, {f: (columns, sep_key, skip) for f in files},
                                                    run_dir, pdal_exe, workers, chunk_bytes, point_format,
                                                    0.0, 0.3)
            origin_of.update(laz_origin)
            p0, pspan = 0.3, 0.7
        else:
            sources, p0, pspan = list(files), 0.0, 1.0
        if create_raster:
            pspan *= 0.7   # Rest des Fortschrittsbalkens fuer DSM + Hillshade

        # 2) Header, Koordinaten-/Hoehenplausibilitaet, Ueberlappungen
        _log("\n[2/4] Quell-Header pruefen ...")
        headers = {s: _read_las_header(s) for s in sources}
        if fmt == "las":
            # Defekte Dateien jetzt melden - sonst bricht erst 'pdal tile' ab, nachdem alle
            # anderen Quellen bereits zerlegt sind
            broken = [f"{names[s]}: {err}" for s in sources
                      for err in [_las_integrity_error(s, headers[s])] if err]
            if broken:
                raise ValueError(f"{len(broken)} Input-Datei(en) defekt (fuer pdal nicht lesbar):\n  "
                                 + "\n  ".join(broken[:20])
                                 + "\nDatei neu exportieren bzw. aus dem Original neu erzeugen. Reparaturversuch "
                                   "mit LAStools: 'laszip -i <datei>.laz -o <neu>.laz', danach Punktzahl mit "
                                   "dem Original vergleichen.")
        not_lv95, kinds = set(), set()
        for s, h in headers.items():
            crs = _classify_crs(h["minx"], h["miny"], h["maxx"], h["maxy"])
            if crs != "LV95":
                kinds.add(crs)
                not_lv95.add(f"{names[origin_of[s]]}: {crs}  (X {h['minx']:.6g}-{h['maxx']:.6g}, "
                             f"Y {h['miny']:.6g}-{h['maxy']:.6g})")
        if not_lv95:
            raise ValueError("Quelldaten nicht in LV95 (EPSG:2056):\n  " + "\n  ".join(sorted(not_lv95)[:10])
                             + "\n" + (DEGREE_HINT if kinds == {"GRAD"} else NOT_LV95_HINT))
        # Hoehenangabe der Quelle (LAS: CRS-Tag, ASCII: Dateiname) gegen die Auswahl - nur Warnung
        if fmt == "las":
            _log(f"  CRS-Tag (1. Datei): {_pdal_srs_name(pdal_exe, sources[0])}  -> wird ersetzt durch "
                 f"LV95/{height_ref}")
        where = "getaggt" if fmt == "las" else "im Dateinamen bezeichnet"
        tags = {}
        for f in files:
            tags.setdefault(_source_height(f, fmt) or "ohne", []).append(names[f])
        for tag, members in sorted(tags.items()):
            if tag not in ("ohne", height_ref):
                _log(f"  WARNUNG: {len(members)} Datei(en) sind als {tag} {where}, gewaehlt ist "
                     f"{height_ref} (z.B. {members[0]}). Die Hoehen werden NICHT umgerechnet, nur "
                     f"als {height_ref} getaggt - Hoehenbezug der Quelle pruefen!")
        if "ohne" in tags:
            _log(f"  Hinweis: {len(tags['ohne'])} Datei(en) ohne Hoehenangabe -> werden als {height_ref} getaggt")
        if fmt == "las":
            formats = sorted({h["point_format"] for h in headers.values()})
            point_format = _target_point_format(formats)
            _log(f"  Punktformat Quelle PF{'/'.join(str(f) for f in formats)} -> Ausgabe PF{point_format}")
            if set(formats) & {4, 5, 9, 10}:
                _log("  WARNUNG: Quelle enthaelt Waveform-Formate - Waveform-Daten werden nicht uebernommen.")

        # Ueberlappen sich zwei Input-Dateien desselben Kachelsatzes, entstuenden beim
        # Zusammenfuehren doppelte Punkte (z.B. Kachel-Buffer oder zwei Varianten eines Gebiets)
        file_box = {}
        for s, h in headers.items():
            o, box = origin_of[s], (h["minx"], h["miny"], h["maxx"], h["maxy"])
            old = file_box.get(o, box)
            file_box[o] = (min(old[0], box[0]), min(old[1], box[1]), max(old[2], box[2]), max(old[3], box[3]))
        clashes = []
        for members in groups.values():
            clashes += _overlapping_pairs({m: file_box[m] for m in members})
        if clashes:
            raise ValueError("Input-Dateien desselben Kachelsatzes ueberlappen raeumlich - beim Zusammenfuehren "
                             "entstuenden doppelte Punkte:\n  "
                             + "\n  ".join(f"{names[a]}  <->  {names[b]}" for a, b in clashes[:10])
                             + "\nUeberlappende Kachelraender (Buffer) oder zwei Varianten desselben Gebiets "
                               "(z.B. merged.laz UND Einzelkacheln im selben Ordner)? Dateien getrennt "
                               "verarbeiten.")

        total_in = sum(h["count"] for h in headers.values())
        bbox = (min(h["minx"] for h in headers.values()), min(h["miny"] for h in headers.values()),
                max(h["maxx"] for h in headers.values()), max(h["maxy"] for h in headers.values()))
        zmin = min(h["minz"] for h in headers.values())
        zmax = max(h["maxz"] for h in headers.values())
        _log(f"  Koordinaten   : LV95 plausibel  |  Extent X {bbox[0]:.1f}-{bbox[2]:.1f}, "
             f"Y {bbox[1]:.1f}-{bbox[3]:.1f}")
        _log(f"  Z-Bereich     : {zmin:.2f} - {zmax:.2f} m")
        if zmin < PLAUSIBLE_Z[0] or zmax > PLAUSIBLE_Z[1]:
            _log(f"  WARNUNG       : Z ausserhalb des fuer die Schweiz plausiblen Bereichs "
                 f"({PLAUSIBLE_Z[0]:g}-{PLAUSIBLE_Z[1]:g} m) - NoData-Werte (z.B. -9999) oder Ausreisser "
                 "in der Quelle? Die Punkte werden unveraendert uebernommen.")
        _log(f"  Punkte Input  : {_fmt_count(total_in)}")
        size, cells = _load_grid_cells(grid_path, bbox)
        _log(f"  Grid          : {len(cells)} Zelle(n) im Datenbereich, Zellgroesse {size:g} m")
        _log(f"  Zielformat    : LAS 1.{OUT_MINOR_VERSION} / PF{point_format} / {out_ext.upper()}, "
             f"scale {OUT_SCALE}, Offset = Kachelursprung, global_encoding {OUT_GLOBAL_ENCODING}")

        # 3) Zerlegen in Kachelstuecke
        _log("\n[3/4] Quellen in Kachelstuecke zerlegen (pdal tile) ...")
        by_cell, failed = {}, []   # (basis, ix, iy) -> [(pfad, punktzahl)]
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_split_worker, (s, str(run_dir / "p" / f"{i:05d}"), pdal_exe,
                                              size, point_format)): s
                    for i, s in enumerate(sources)}
            for done, fut in enumerate(as_completed(futs), 1):
                s = futs[fut]
                label = names[origin_of[s]]
                try:
                    _, pieces = fut.result()
                    for (ix, iy), path, count in pieces:
                        by_cell.setdefault((group_of[origin_of[s]], ix, iy), []).append((path, count))
                    _log(f"  [{done}/{len(sources)}] {label}: {len(pieces)} Kachelstueck(e)")
                except Exception as e:
                    failed.append(label)
                    _log(f"  [{done}/{len(sources)}] {label}: FEHLER - {e}")
                _progress(p0 + pspan * 0.4 * done / len(sources))
        if failed:
            raise RuntimeError(f"{len(failed)} Quelle(n) konnten nicht zerlegt werden - Abbruch.")

        outside = sorted(k for k in by_cell if k[1:] not in cells)
        outside_points = sum(c for k in outside for _, c in by_cell[k])
        if outside:
            _log(f"  WARNUNG: {_fmt_count(outside_points)} Punkt(e) in {len(outside)} Zelle(n) ausserhalb "
                 f"des Grid-Shapes - werden nicht geschrieben (z.B. {[k[1:] for k in outside[:3]]})")

        # 4) Endkacheln: je Kachelsatz (Basis) und Grid-Zelle
        jobs = []
        for j, key in enumerate(sorted(k for k in by_cell if k[1:] in cells)):
            b, ix, iy = key
            expected = sum(c for _, c in by_cell[key])
            if expected == 0:
                continue
            out_path = os.path.join(output_dir, f"{b}_{cells[(ix, iy)]}{suffix}.{out_ext}")
            jobs.append(((ix, iy), out_path, [p for p, _ in by_cell[key]], expected,
                         run_dir / f"t{j:05d}.json", pdal_exe, size, point_format, height_ref, thin_m))
        _log(f"\n[4/4] {len(jobs)} Kachel(n) schreiben" + (f" (Thinning {thin_m:g} m)" if thin_m else "") + " ...")
        written = empty = overwritten = total_out = merged_in = 0
        errors, out_paths = [], []
        expected_of = {os.path.basename(job[1]): job[3] for job in jobs}
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(_tile_worker, job) for job in jobs]
            for done, fut in enumerate(as_completed(futs), 1):
                status, out_name, count, err, existed = fut.result()
                prefix_log = f"  [{done}/{len(jobs)}] {out_name}"
                if status == "written":
                    written += 1
                    total_out += count
                    merged_in += expected_of[out_name]
                    overwritten += int(existed)
                    out_paths.append(os.path.join(output_dir, out_name))
                    _log(f"{prefix_log}  ({_fmt_count(count)} Punkte"
                         + (f" von {_fmt_count(expected_of[out_name])}" if thin_m else "") + ")"
                         + ("  - ueberschrieben" if existed else ""))
                elif status == "empty":
                    empty += 1
                    _log(f"{prefix_log}  - leer, nicht geschrieben")
                else:
                    errors.append(f"{out_name}: {err}")
                    _log(f"{prefix_log}  - FEHLER: {err}")
                _progress(p0 + pspan * (0.4 + 0.6 * done / len(jobs)))

        # 5) Zusammenfassung + Punktbilanz. Mit Thinning wird die Bilanz VOR dem Ausduennen
        # gezogen (Summe der Kachelstuecke je geschriebener Kachel), die Differenz ist gewollt.
        balance = total_in - (merged_in if thin_m else total_out) - outside_points
        _log("\nZusammenfassung:")
        _log(f"  Kacheln geschrieben : {written}" + (f"  (davon {overwritten} ueberschrieben)" if overwritten else ""))
        if empty:
            _log(f"  leere Kacheln       : {empty}")
        _log(f"  Punkte Input        : {_fmt_count(total_in)}")
        if thin_m:
            share = 100.0 * total_out / merged_in if merged_in else 0.0
            _log(f"  Punkte vor Thinning : {_fmt_count(merged_in)}")
            _log(f"  Punkte Output       : {_fmt_count(total_out)}  ({share:.1f} % nach Thinning {thin_m:g} m)")
        else:
            _log(f"  Punkte Output       : {_fmt_count(total_out)}")
        if outside_points:
            _log(f"  ausserhalb Grid     : {_fmt_count(outside_points)}")
        _log(f"  Punktbilanz         : " + ("OK (kein Punkt verloren/doppelt)" if balance == 0
                                            else f"DIFFERENZ {_fmt_count(balance)}"))
        if errors:
            raise RuntimeError(f"{len(errors)} Kachel(n) fehlgeschlagen:\n  " + "\n  ".join(errors[:20]))
        if written == 0:
            raise RuntimeError("Keine Kachel geschrieben - Grid-Shape und Input pruefen.")
        if balance != 0:
            raise RuntimeError("Punktbilanz stimmt nicht - Ergebnis pruefen (siehe Log).")

        # 6) optional DSM-Raster + Hillshade aus genau den geschriebenen Kacheln
        if create_raster:
            _log(f"\n[Raster] DSM + Hillshade aus {len(out_paths)} Kachel(n) ...")
            dsm_path, hs_path = _build_dsm_raster(sorted(out_paths), raster_dir, jahr, area, gsd,
                                                  height_ref, run_dir / "r", workers, pdal_exe,
                                                  p0 + pspan, 1.0 - (p0 + pspan))
            _log(f"  DSM       : {dsm_path}  (+ .tfw)")
            _log(f"  Hillshade : {hs_path}  (+ .tfw)")
        dt = time.time() - t_start
        _log(f"\nLaufzeit: {int(dt // 60)}m {int(dt % 60):02d}s")
        _log("Fertig.")
    finally:
        if keep:
            _log(f"\nStaging-Dateien behalten: {run_dir}")
        else:
            shutil.rmtree(run_dir, ignore_errors=True)
            if not cfg.get("staging_dir"):
                try:
                    os.rmdir(staging_dir)  # Standard-Unterordner '_staging', nur falls leer
                except OSError:
                    pass


def main() -> None:
    if len(sys.argv) < 2:
        print("[FEHLER] Kein Konfigurationspfad uebergeben.", flush=True)
        sys.exit(1)

    with open(sys.argv[1], encoding="utf-8") as f:
        cfg = json.load(f)

    action = cfg.get("action", "")
    try:
        if action == "info":
            _info(cfg)
        elif action == "process":
            _process(cfg)
        elif action == "raster":
            if cfg.get("batch_list"):
                _process_raster_batch(cfg)
            else:
                _process_raster(cfg)
        else:
            print(f"[FEHLER] Unbekannte Aktion: '{action}'", flush=True)
            sys.exit(1)
    except BatchIncomplete as e:
        print(f"\n[FEHLER] {e}", flush=True)   # Details stehen bereits in der Zusammenfassung
        sys.exit(1)
    except Exception as e:
        print(f"\n[FEHLER] {e}", flush=True)
        print(traceback.format_exc(), flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
