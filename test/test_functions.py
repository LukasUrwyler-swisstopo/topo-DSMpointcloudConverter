"""Leichtgewichtige Import-/Sanity-Checks - laufen ohne OSGeo4W/PDAL."""
import base64
import importlib.util
import os
import struct

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_module_from_path(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _gui():
    return load_module_from_path("gui_module", os.path.join(PROJECT_ROOT, "GUI_DSMpointcloudConverter.py"))


def _runner():
    return load_module_from_path("runner_module",
                                 os.path.join(PROJECT_ROOT, "process_scripts", "_osgeo_runner.py"))


def _las_bytes(n_vlr=0, vlr=b""):
    """Minimaler LAS-1.4-Header (375 Byte) + optionaler VLR-Block, ohne Punkte."""
    b = bytearray(375)
    b[0:4] = b"LASF"
    struct.pack_into("<H", b, 6, 17)
    b[24], b[25] = 1, 4
    struct.pack_into("<HII", b, 94, 375, 375 + len(vlr), n_vlr)
    b[104] = 6 | 0x80
    struct.pack_into("<6d", b, 131, 0.01, 0.01, 0.01, 2600000.0, 1200000.0, 0.0)
    struct.pack_into("<6d", b, 179, 2600999.0, 2600000.0, 1200999.0, 1200000.0, 600.0, 400.0)
    struct.pack_into("<Q", b, 247, 12345)
    return bytes(b) + vlr


def test_constants():
    gui = _gui()
    assert os.path.basename(gui.RUNNER_SCRIPT) == "_osgeo_runner.py"
    assert gui.CONFIG_FILE.endswith("_dsm2laz_config.json")
    assert os.path.isfile(gui.DEFAULT_GRID_SHAPE)
    assert set(gui.SEPARATOR_LABELS) == set(_runner().SEPARATORS)


def test_detect_python_home_returns_string():
    gui = _gui()
    assert isinstance(gui._detect_python_home(os.path.join("C:", "OSGeo4W", "bin", "python3.exe")), str)


def test_naming_preview():
    gui = _gui()
    assert gui._naming_preview("2021", "DIABLONS", 0.2, "LN02", "laz") == \
        "2021_DIABLONS_TIN_DSM_thin02_2600_1200_LV95_LN02.laz"
    assert gui._naming_preview("", "", 0, "LHN95", "las", 1.0).splitlines() == [
        "<Jahr>_<AREA>_TIN_DSM_2600_1200_LV95_LHN95.las",
        "<Jahr>_<AREA>_DSM_100cm_LV95_LHN95.tif", "<Jahr>_<AREA>_hillshade_100cm_LV95_LHN95.tif"]


def test_ascii_name_hints_and_metadata(tmp_path):
    r = _runner()
    assert r._name_crs_hints("2015_RHONE_DSM_1m_LV95_LN02_CIR_low_raw.asc") == ["LV95", "LN02"]
    assert r._name_crs_hints("dom_lv95_lhn95.xyz") == ["LV95", "LHN95"]
    assert r._name_crs_hints("gebiet_2015.xyz") == []
    assert r._source_height("x_LHN95.xyz", "ascii") == "LHN95"
    p = tmp_path / "dsm_LV95_LHN95.xyz"
    p.write_text("X Y Z\n" + "".join("%d 1200000.00 %d.25\n" % (2600000 + i, 500 + i) for i in range(10)))
    s = r._sniff_ascii(str(p))
    assert s["zrange_sample"] == [500.25, 509.25] and s["count_estimate"] == 10
    m = r._ascii_metadata(str(p), s)
    assert "LHN95" in m["crs_vertical"] and "LV95" in m["crs_horizontal"] and m["name_hints"] == "LV95, LHN95"


def test_overlapping_pairs():
    r = _runner()
    touching = {"a": (0, 0, 1000, 1000), "b": (1000, 0, 2000, 1000)}
    assert r._overlapping_pairs(touching) == []
    overlapping = {"a": (0, 0, 1000, 1000), "b": (500, 0, 1500, 1000)}
    assert r._overlapping_pairs(overlapping) == [("a", "b")]


def test_classify_crs_and_cells():
    r = _runner()
    assert r._classify_crs(2600000, 1200000, 2601000, 1201000) == "LV95"
    assert r._classify_crs(600000, 200000, 601000, 201000) == "LV03"
    assert r._classify_crs(0, 0, 10, 10) == "unbekannt"
    # halb-offene Intervalle: BBox endet exakt auf der Kilometerlinie -> Nachbarzelle zaehlt mit
    assert r._cells_of_bbox(2600000, 1200000, 2600999.99, 1200999.99, 1000) == {(2600, 1200)}
    assert len(r._cells_of_bbox(2600000, 1200000, 2601000, 1200500, 1000)) == 2


def test_sniff_ascii_variants(tmp_path):
    r = _runner()
    p1 = tmp_path / "a.xyz"
    p1.write_text("2600000.00   1200000.00  512.34\r\n2600002.00 1200000.00 512.40\r\n")
    s1 = r._sniff_ascii(str(p1))
    assert (s1["separator"], s1["skip"], s1["ncols"], s1["columns"], s1["crs_guess"]) == \
        ("space", 0, 3, "X Y Z", "LV95")

    p2 = tmp_path / "b.csv"
    p2.write_text("x;y;z;class\n600000.1;200000.2;500.0;2\n")
    s2 = r._sniff_ascii(str(p2))
    assert (s2["separator"], s2["skip"], s2["columns"], s2["crs_guess"]) == \
        ("semicolon", 1, "X Y Z Classification", "LV03")

    p3 = tmp_path / "grid.asc"
    p3.write_text("ncols 3\nnrows 1\nxllcorner 2600000\nyllcorner 1200000\ncellsize 1\n500 501 502\n")
    with pytest.raises(ValueError, match="ESRI"):
        r._sniff_ascii(str(p3))


def test_split_ascii_keeps_all_lines(tmp_path):
    r = _runner()
    src = tmp_path / "big.xyz"
    content = "X Y Z\n" + "".join("%d 1200000 500\n" % (2600000 + i) for i in range(1000))
    src.write_bytes(content.encode())
    parts = r._split_ascii(str(src), tmp_path / "c", 4)
    data = [open(p, "rb").read() for p in parts]
    assert len(parts) == 4
    assert b"".join(data) == content.encode()
    assert all(d.endswith(b"\n") for d in data)
    assert data[0].startswith(b"X Y Z") and not any(d.startswith(b"X") for d in data[1:])


def test_read_las_header_and_lhn95_tag(tmp_path):
    r = _runner()
    p = tmp_path / "x.laz"
    p.write_bytes(_las_bytes())
    h = r._read_las_header(str(p))
    assert (h["minor_version"], h["point_format"], h["compressed"], h["count"]) == (4, 6, True, 12345)
    assert (h["minx"], h["maxy"], h["global_encoding"]) == (2600000.0, 1200999.0, 17)
    assert r._lhn95_tagged(str(p)) is False

    for wkt, expected in ((b'VERT_CS["LHN95 height",AUTHORITY["EPSG","5729"]]', True),
                          (b'VERT_CS["LN02 height",AUTHORITY["EPSG","5728"]]', False)):
        vlr = struct.pack("<H16sHH32s", 0, b"LASF_Projection", 2112, len(wkt), b"") + wkt
        p.write_bytes(_las_bytes(1, vlr))
        assert r._lhn95_tagged(str(p)) is expected


def test_las_tab_naming_and_choices():
    r, gui = _runner(), _gui()
    assert r._thin_token(0) == "" and r._thin_token(0.2) == "thin02" and r._thin_token(1.5) == "thin15"
    assert r._las_tile_base("2021", "DIABLONS", 0.2) == "2021_DIABLONS_TIN_DSM_thin02"
    assert r._las_tile_base("2021", "DIABLONS", 0) == "2021_DIABLONS_TIN_DSM"
    assert r._raster_names("2021", "X", 0.5, "LN02") == ("2021_X_DSM_50cm_LV95_LN02.tif",
                                                         "2021_X_hillshade_50cm_LV95_LN02.tif")
    # GUI-Auswahllisten deckungsgleich mit dem Runner
    assert set(gui.HEIGHT_LABELS) == set(r.HEIGHT_REFS)
    assert tuple(v for _, v in gui.THIN_LABELS if v) == r.THIN_OPTIONS_M
    assert tuple(gui.OUT_FORMAT_CHOICES) == r.OUT_FORMATS


def test_target_point_format():
    f = _runner()._target_point_format
    assert f([0]) == f([1]) == f([6]) == 6
    assert f([2]) == f([3]) == f([7]) == f([1, 3]) == 7
    assert f([8]) == 8


def test_reference_vlrs_lhn95_differ_only_in_height():
    r = _runner()
    keys_ln02, wkt_ln02 = r.REFERENCE_VLRS["LN02"]
    keys_lhn95, wkt_lhn95 = r.REFERENCE_VLRS["LHN95"]
    assert (keys_ln02, wkt_ln02) == (base64.b64decode(r.REFERENCE_VLR_34735_B64),
                                     base64.b64decode(r.REFERENCE_VLR_2112_B64))   # LN02 byte-exakt
    diff = [i for i in range(len(keys_ln02)) if keys_ln02[i] != keys_lhn95[i]]
    assert len(keys_ln02) == len(keys_lhn95) and len(diff) <= 2                    # nur Key 4096
    assert struct.unpack_from("<4H", keys_lhn95, 8 + 8 * 4) == (4096, 0, 1, 5729)
    assert b'AUTHORITY["EPSG","5729"]]]' in wkt_lhn95 and b"LN02" not in wkt_lhn95
    assert wkt_lhn95.split(b"VERT_CS")[0] == wkt_ln02.split(b"VERT_CS")[0]         # Lageteil identisch


def test_vertical_tag_and_degree_range(tmp_path):
    r = _runner()
    p = tmp_path / "x.laz"
    for wkt, expected in ((b'VERT_CS["LHN95 height",AUTHORITY["EPSG","5729"]]', "LHN95"),
                          (b'VERT_CS["LN02 height",AUTHORITY["EPSG","5728"]]', "LN02"),
                          (b'PROJCS["CH1903+ / LV95",AUTHORITY["EPSG","2056"]]', "")):
        vlr = struct.pack("<H16sHH32s", 0, b"LASF_Projection", 2112, len(wkt), b"") + wkt
        p.write_bytes(_las_bytes(1, vlr))
        assert r._vertical_tag(str(p)) == expected
    assert r._classify_crs(7.6, 46.1, 7.61, 46.11) == "GRAD"


def test_process_action_available():
    r = _runner()
    assert callable(r._process) and callable(r._info)


def test_tabs_per_format(tmp_path):
    gui = _gui()
    app = gui.DsmToLazApp()
    try:
        nb = app._notebook
        assert [nb.tab(t, "text") for t in nb.tabs()] == \
            ["DSM.ascii → DSM.laz-Tiles", "DSM.laz → DSM.laz-Tiles", "DSM.ascii → DSM-Raster (SB_DSM)"]
        ascii_tab, las_tab = app._tabs["ascii"], app._tabs["las"]
        assert ascii_tab._ascii_sec.winfo_manager() == "pack"
        assert las_tab._ascii_sec is None

        # Beide Tabs: dieselben Eingaben in derselben Reihenfolge (Gruppen 1-4)
        for tab in (ascii_tab, las_tab):
            headers = [w.cget("text") for w in tab._sf.winfo_children()
                       if isinstance(w, gui.ttk.Label) and w.cget("text")[:1].isdigit()]
            assert headers == ["1   Input", "2   Projekt & Referenzsystem", "3   Output",
                               "4   Staging & Parallelisierung"]
            for attr in ("_jahr_var", "_area_var", "_height_label_var", "_thin_label_var", "_out_fmt_var",
                         "_raster_var", "_info_meta"):
                assert hasattr(tab, attr), (tab.fmt, attr)

        # ASCII-Tab: gleiche Benennung, Warnung bei abweichender Hoehenangabe im Dateinamen
        ascii_tab._jahr_var.set("2015")
        ascii_tab._area_var.set("RHONE")
        assert ascii_tab._name_preview_lbl.cget("text") == "2015_RHONE_TIN_DSM_2600_1200_LV95_LN02.laz"
        ascii_info = {"format": "ascii", "n_files": 1, "n_ascii": 1, "separator": "tab", "ncols": 3,
                      "skip": 1, "columns": "X Y Z", "first_xy": [2600000, 1200000], "preview": ["X\tY\tZ"],
                      "crs_guess": "LV95", "vertical_tags": {"LHN95": 1}, "target_pf": 6,
                      "first_meta": {"file": "a_LHN95.xyz", "crs_vertical": "kein CRS-Tag (ASCII)  |  Dateiname: LHN95"}}
        ascii_tab._show_info(ascii_info)
        assert "im Dateinamen als LHN95" in ascii_tab._info_warn.cget("text")
        assert "Dateiname: LHN95" in ascii_tab._info_meta.cget("text")
        assert ascii_tab._sep_label_var.get() == gui.SEPARATOR_LABELS["tab"]
        ascii_tab._sep_label_var.set(gui.SEPARATOR_LABELS["comma"])     # Benutzer korrigiert
        ascii_tab._height_label_var.set(gui.HEIGHT_LABELS["LHN95"])
        ascii_tab._on_height_changed()
        assert not ascii_tab._info_warn.winfo_manager()
        assert ascii_tab._sep_label_var.get() == gui.SEPARATOR_LABELS["comma"]   # nicht ueberschrieben

        # LAZ-Tab: Benennung aus Jahr / AREA / Thinning / Hoehenbezug / Format
        las_tab._jahr_var.set("2021")
        las_tab._area_var.set("DIABLONS")
        las_tab._thin_label_var.set("0.2 m")
        las_tab._height_label_var.set(gui.HEIGHT_LABELS["LHN95"])
        las_tab._on_height_changed()
        las_tab._out_fmt_var.set("las")
        assert las_tab._name_preview_lbl.cget("text") == "2021_DIABLONS_TIN_DSM_thin02_2600_1200_LV95_LHN95.las"
        assert not las_tab._raster_frame.winfo_manager()          # Raster-Felder versteckt
        las_tab._raster_var.set(True)
        las_tab._update_raster_ui()
        assert las_tab._raster_frame.winfo_manager() == "grid"
        assert "2021_DIABLONS_DSM_50cm_LV95_LHN95.tif" in las_tab._name_preview_lbl.cget("text")

        # Tag-Warnung folgt der Auswahl
        info = {"format": "las", "n_files": 1, "n_las": 1, "vertical_tags": {"LN02": 1},
                "extent": [2600000, 1200000, 2601000, 1201000], "crs_guess": "LV95",
                "first_meta": {"file": "x.laz", "crs_vertical": "LN02 height (EPSG:5728)"}}
        las_tab._show_info(info)
        assert "als LN02 bezeichnet/getaggt, gewaehlt ist LHN95" in las_tab._info_warn.cget("text")
        assert "LN02 height (EPSG:5728)" in las_tab._info_meta.cget("text")
        las_tab._height_label_var.set(gui.HEIGHT_LABELS["LN02"])
        las_tab._on_height_changed()
        assert not las_tab._info_warn.winfo_manager()

        app._set_start_buttons("disabled")
        assert all(str(t._start_btn.cget("state")) == "disabled" for t in app._tabs.values())
    finally:
        app.destroy()


# ─── Raster-Tab (SB_DSM) ──────────────────────────────────────────────────────
# Namensmuster aus topo-GDWHimport (GUI_GDWHimport.NAME_FORMAT_SPECS['SB_DSM']) - hier unabhaengig
# vom Runner abgeschrieben, damit eine Aenderung dort auffaellt
IMPORTER_SB_DSM = (r'^20\d{2}_.+_DSM_.*_LV95_LN02\.(tif|tiff|tfw)$',
                   r'^20\d{2}_.+_hillshade_.*_LV95_LN02\.(tif|tiff)$')


def test_sbdsm_naming():
    import re
    r = _runner()
    assert [r._gsd_label(g) for g in (1, 2.0, 0.5, 0.25, 1.5, 0.125)] == \
        ["1m", "2m", "50cm", "25cm", "150cm", "12.5cm"]
    dsm, hs = r._sbdsm_raster_names("2014", "ALETSCH_MOOSFLUE", 1.0)
    assert (dsm, hs) == ("2014_ALETSCH_MOOSFLUE_DSM_1m_LV95_LN02.tif",
                         "2014_ALETSCH_MOOSFLUE_hillshade_1m_LV95_LN02.tif")
    assert re.match(IMPORTER_SB_DSM[0], dsm, re.I) and re.match(IMPORTER_SB_DSM[1], hs, re.I)
    # AREA wie der Import sie liest (zwischen Jahr und erstem _DSM / _hillshade)
    assert re.search(r'20\d{2}_(.+?)_DSM', dsm).group(1) == re.search(r'20\d{2}_(.+?)_hillshade', hs).group(1) \
        == "ALETSCH_MOOSFLUE"
    assert tuple(p.pattern for p in r.SBDSM_NAME_PATTERNS) == IMPORTER_SB_DSM
    assert r._sbdsm_job_name("2014", "X") == "2014_X_DSM_LV95_LN02"
    assert r._sbdsm_name_problems("2014", "ALETSCH_MOOSFLUE") == []
    assert r._sbdsm_name_problems("1999", "X")                    # Import erwartet 20xx
    assert r._sbdsm_name_problems("2014", "")
    assert r._sbdsm_name_problems("2014", "A B")
    assert r._sbdsm_name_problems("2014", "X_DSM_Y")              # AREA wuerde abgeschnitten
    assert r._sbdsm_name_problems("2014", "hillshade_X")          # DSM wuerde als Hillshade gelten
    gui = _gui()
    assert gui._raster_naming_preview("", "", None).splitlines() == [
        "<Jahr>_<AREA>_DSM_<GSD>_LV95_LN02.tif", "<Jahr>_<AREA>_hillshade_<GSD>_LV95_LN02.tif"]


def test_source_name_parts():
    r = _runner()
    assert r._source_name_parts("2014_ALETSCH_MOOSFLUE_DSM_1m_LV95_LN02_CIR_low.asc") == \
        {"jahr": "2014", "area": "ALETSCH_MOOSFLUE", "gsd": 1.0}
    assert r._source_name_parts("2016_SILVRETTA_DSM_50cm_LV95_LN02.xyz")["gsd"] == 0.5
    assert r._source_name_parts("gebiet.xyz") == {"jahr": "", "area": "", "gsd": None}


def test_ascii_kind(tmp_path):
    r = _runner()
    # ESRI-Kopf wie die Lieferungen (Grossbuchstaben, NODATA -9999.00)
    p = tmp_path / "a.asc"
    p.write_text("NCOLS        4\nNROWS        3\nXLLCORNER    2640121.00\nYLLCORNER    1132732.00\n"
                 "CELLSIZE     1.0\nNODATA_VALUE -9999.00\n" + "-9999.00 1500.25 1500.50 1500.75\n" * 3)
    k = r._ascii_kind(str(p))
    assert k["kind"] == "esri" and k["cellsize"] == 1.0 and k["header"]["nodata"] == -9999.0
    assert k["box"] == (2640121.0, 1132732.0, 2640125.0, 1132735.0) and k["header"]["header_lines"] == 6
    p.write_text("ncols 2\nnrows 2\nxllcenter 2600000.25\nyllcenter 1200000.25\ncellsize 0.5\n1 2\n3 4\n")
    assert r._ascii_kind(str(p))["box"] == (2600000.0, 1200000.0, 2600001.0, 1200001.0)
    p.write_text("ncols 2\nnrows 2\ncellsize 0.5\n1 2\n3 4\n")
    with pytest.raises(ValueError, match="unvollstaendig"):
        r._ascii_kind(str(p))

    # XYZ-Gitter zeilenweise (mit Luecke) und spaltenweise -> Gitter, Zufallspunkte -> Punkte
    g = tmp_path / "g.xyz"
    rows = ["%.2f %.2f 500.0" % (2600000 + 0.5 * c, 1200010 - 0.5 * r_) for r_ in range(20) for c in range(30)
            if (r_, c) != (5, 7)]
    g.write_text("X Y Z\n" + "\n".join(rows) + "\n")
    assert r._ascii_kind(str(g))["kind"] == "xyz_grid" and r._ascii_kind(str(g))["cellsize"] == 0.5
    g.write_text("\n".join("%.2f;%.2f;500" % (2600000 + 2 * c, 1200000 + 2 * r_)
                           for c in range(10) for r_ in range(40)) + "\n")
    assert r._ascii_kind(str(g))["cellsize"] == 2.0
    import random
    rnd = random.Random(3)
    g.write_text("".join("%.2f %.2f 500\n" % (2600000 + rnd.random() * 100, 1200000 + rnd.random() * 100)
                         for _ in range(500)))
    assert r._ascii_kind(str(g))["kind"] == "points"


def test_grid_set_check():
    r = _runner()
    a = ((2600000.0, 1200000.0, 2600100.0, 1200050.0), 1.0, 1.0)
    ok, cs, union = r._grid_set_check({"a": a, "b": ((2600100.0, 1200010.0, 2600160.0, 1200080.0), 1.0, 1.0)})
    assert ok == [] and cs == 1.0 and union == (2600000.0, 1200000.0, 2600160.0, 1200080.0)
    probs = lambda b: r._grid_set_check({"a": a, "b": b})[0]
    assert "ueberlappen" in probs(((2600050.0, 1200000.0, 2600150.0, 1200050.0), 1.0, 1.0))[0]
    assert "verschoben" in probs(((2600100.5, 1200000.0, 2600200.5, 1200050.0), 1.0, 1.0))[0]
    assert "Zellgroesse" in probs(((2600100.0, 1200000.0, 2600200.0, 1200050.0), 0.5, 0.5))[0]
    assert "nicht LV95" in r._grid_set_check({"a": ((600000.0, 200000.0, 600100.0, 200050.0), 1.0, 1.0)})[0][0]


def test_raster_tab():
    gui = _gui()
    app = gui.DsmToLazApp()
    try:
        tab = app._tabs["raster"]
        headers = [w.cget("text") for w in tab._sf.winfo_children()
                   if isinstance(w, gui.ttk.Label) and w.cget("text")[:1].isdigit()]
        assert headers == ["1   Input", "2   Projekt & Referenzsystem", "3   Output",
                           "4   Staging & Parallelisierung"]
        assert tab._height_ref() == "LN02" and not hasattr(tab, "_height_label_var")   # fix, keine Auswahl
        assert tab._name_preview_lbl.cget("text").startswith("<Jahr>_<AREA>_DSM_50cm_LV95_LN02.tif")

        # Gitter: GSD = Zellgroesse (gesperrt), Jahr/AREA aus dem Dateinamen vorbelegt
        info = {"format": "raster", "n_files": 1, "n_ascii": 1, "n_las": 0, "sample": "x.asc", "size_mb": 1.0,
                "kinds": {"esri": 1}, "mode": "grid", "cellsizes": [1.0], "grid_cells": [12000, 13057],
                "extent": [2640121.0, 1132732.0, 2652121.0, 1145789.0], "crs_guess": "LV95", "set_problems": [],
                "vertical_tags": {"LN02": 1}, "errors": [],
                "name_parts": {"jahr": "2014", "area": "ALETSCH_MOOSFLUE", "gsd": 1.0},
                "preview": ["NCOLS        12000"], "first_meta": {"file": "x.asc", "nodata": "-9999"}}
        tab._show_info(info)
        assert (tab._jahr_var.get(), tab._area_var.get()) == ("2014", "ALETSCH_MOOSFLUE")
        assert str(tab._gsd_entry.cget("state")) == "disabled" and tab._current_gsd() == 1.0
        assert tab._name_preview_lbl.cget("text").splitlines() == [
            "2014_ALETSCH_MOOSFLUE_DSM_1m_LV95_LN02.tif", "2014_ALETSCH_MOOSFLUE_hillshade_1m_LV95_LN02.tif"]
        assert not tab._info_warn.winfo_manager()
        assert "NoData      : -9999" in tab._info_meta.cget("text")

        # Punkte: GSD waehlbar, aus dem Namen vorbelegt; LHN95 im Namen -> Warnung; Eingaben bleiben
        tab._area_var.set("MEIN_GEBIET")
        info2 = dict(info, kinds={"points": 1}, mode="points", cellsizes=[], extent=None, grid_cells=None,
                     first_xy=[2600000.0, 1200000.0], count_estimate=123456, vertical_tags={"LHN95": 1},
                     name_parts={"jahr": "2014", "area": "X", "gsd": 2.0})
        tab._show_info(info2)
        assert str(tab._gsd_entry.cget("state")) == "normal" and tab._current_gsd() == 2.0
        assert tab._area_var.get() == "MEIN_GEBIET"
        assert "LHN95" in tab._info_warn.cget("text")
        tab._show_info(dict(info2, mode="mixed", kinds={"esri": 1, "points": 1}))
        assert "mischt" in tab._info_warn.cget("text")
    finally:
        app.destroy()


# ─── Raster-Tab Batch (TXT-Liste mit Ordner-Pfaden) ──────────────────────────
def _esri(path, x0=2640000.0, y0=1132000.0, n=4, cs=1.0):
    path.write_text("NCOLS {0}\nNROWS {0}\nXLLCORNER {1:.2f}\nYLLCORNER {2:.2f}\nCELLSIZE {3}\nNODATA_VALUE -9999\n"
                    .format(n, x0, y0, cs) + ("1500.25 " * n + "\n") * n)


def test_read_batch_list(tmp_path):
    r = _runner()
    (tmp_path / "rel").mkdir()
    lst = tmp_path / "liste.txt"
    a, zh = str(tmp_path / "a"), r"C:\Zürich\b"
    lines = ["# Kommentar", "", '"{}"'.format(a), "  {}  ".format(a), "rel", zh]
    lst.write_bytes("\n".join(lines).encode("cp1252"))
    folders = r._read_batch_list(str(lst))
    assert folders == [os.path.normpath(a), os.path.normpath(str(tmp_path / "rel")),
                       os.path.normpath(zh)]                                   # cp1252, Duplikat einmal
    lst.write_text("\ufeff{}\n".format(tmp_path / "a"), encoding="utf-8")      # UTF-8 mit BOM
    assert r._read_batch_list(str(lst)) == [os.path.normpath(str(tmp_path / "a"))]


def test_batch_plan(tmp_path):
    r = _runner()
    ok = tmp_path / "ok"
    ok.mkdir()
    _esri(ok / "2014_ALETSCH_MOOSFLUE_DSM_1m_LV95_LN02_CIR_low.asc")
    p = r._batch_plan(str(ok))
    assert (p["problem"], p["mode"], p["jahr"], p["area"], p["gsd"], p["name_from"]) == \
        ("", "grid", "2014", "ALETSCH_MOOSFLUE", 1.0, "Dateiname")
    assert p["out_dir"] == str(ok / "dsm") and p["names"][0] == "2014_ALETSCH_MOOSFLUE_DSM_1m_LV95_LN02.tif"
    (ok / "dsm").mkdir()
    (ok / "dsm" / p["names"][0]).write_bytes(b"")
    assert r._batch_plan(str(ok))["existing"] == [p["names"][0]]

    # Jahr/AREA aus dem Ordnernamen, wenn der Dateiname nichts hergibt
    named = tmp_path / "2019_GLETSCH_DSM_50cm"
    named.mkdir()
    _esri(named / "kachel.ascii", cs=0.5)
    p = r._batch_plan(str(named))
    assert (p["problem"], p["jahr"], p["area"], p["gsd"], p["name_from"]) == ("", "2019", "GLETSCH", 0.5, "Ordnername")

    # Probleme -> Ordner wird uebersprungen
    assert r._batch_plan(str(tmp_path / "fehlt"))["problem"] == "Ordner nicht gefunden"
    empty = tmp_path / "leer"
    empty.mkdir()
    assert "keine ASCII-Dateien" in r._batch_plan(str(empty))["problem"]
    noname = tmp_path / "ohne"
    noname.mkdir()
    _esri(noname / "a.asc")
    assert "weder aus dem Dateinamen" in r._batch_plan(str(noname))["problem"]
    two = tmp_path / "zwei"
    two.mkdir()
    _esri(two / "2014_A_DSM_1m.asc")
    _esri(two / "2015_B_DSM_1m.asc", x0=2640004.0)
    assert "verschiedene Jahr/AREA" in r._batch_plan(str(two))["problem"]
    over = tmp_path / "ueber"
    over.mkdir()
    _esri(over / "2014_A_DSM_1m_1.asc")
    _esri(over / "2014_A_DSM_1m_2.asc", x0=2640001.0)
    assert "ueberlappen" in r._batch_plan(str(over))["problem"]

    # XYZ-Punkte: GSD aus dem Namen, sonst GUI-Wert, sonst Problem
    pts = tmp_path / "pts"
    pts.mkdir()
    import random
    rnd = random.Random(1)
    (pts / "2021_PTS_DSM.xyz").write_text("".join("%.2f %.2f 500\n" % (2600000 + rnd.random() * 50,
                                                                     1200000 + rnd.random() * 50) for _ in range(300)))
    assert "ohne GSD" in r._batch_plan(str(pts))["problem"]
    p = r._batch_plan(str(pts), 2.0)
    assert (p["problem"], p["mode"], p["gsd"], p["gsd_from"]) == ("", "points", 2.0, "GUI")


def test_raster_tab_batch(tmp_path):
    gui = _gui()
    app = gui.DsmToLazApp()
    try:
        tab = app._tabs["raster"]
        assert not tab._is_batch() and str(tab._out_entry.cget("state")) == "normal"
        tab._out_var.set(r"D:\raster")
        tab._jahr_var.set("2014")
        tab._input_mode_var.set("batch")
        tab._on_input_mode(refresh=False)
        # Output-Ordner und Jahr/AREA ausgegraut, Werte des Einzellaufs bleiben erhalten
        assert str(tab._out_entry.cget("state")) == "disabled" and str(tab._out_btn.cget("state")) == "disabled"
        assert str(tab._jahr_entry.cget("state")) == "disabled" and str(tab._area_entry.cget("state")) == "disabled"
        assert tab._batch_widgets[1].winfo_manager() == "grid" and not tab._single_widgets[1].winfo_manager()
        assert tab._info_batch.winfo_manager() == "grid" and not tab._info_meta.winfo_manager()
        assert tab._name_preview_lbl.cget("text").startswith(r"<Ordner>\dsm\<Jahr>_<AREA>_DSM_<GSD>_LV95_LN02.tif")
        lst = tmp_path / "liste.txt"
        lst.write_text("C:\\x\n")
        tab._list_var.set(str(lst))
        cfg = tab._info_cfg()
        assert cfg["batch_list"] == str(lst) and cfg["format"] == "raster"

        info = {"format": "raster", "batch": True, "list": str(lst), "n_folders": 2, "n_ok": 1,
                "kinds": {"ESRI-ASCII-Grid": 1},
                "folders": [
                    {"path": r"D:\daten\2014\ALETSCH", "problem": "", "warnings": ["GSD im Dateinamen 2 m"],
                     "kind": "ESRI-ASCII-Grid", "mode": "grid", "gsd": 1.0, "gsd_from": "Zellgroesse",
                     "names": ["2014_ALETSCH_DSM_1m_LV95_LN02.tif", "2014_ALETSCH_hillshade_1m_LV95_LN02.tif"],
                     "existing": ["2014_ALETSCH_DSM_1m_LV95_LN02.tif"]},
                    {"path": r"D:\daten\RHONE", "problem": "keine ASCII-Dateien", "warnings": [], "kind": "",
                     "mode": "", "gsd": None, "gsd_from": "", "names": [], "existing": []}]}
        tab._show_info(info)
        table = tab._info_batch.cget("text")
        assert r"OK  …\2014\ALETSCH" in table and "→ 2014_ALETSCH_DSM_1m_LV95_LN02.tif  [vorhanden]" in table
        assert r"!!  D:\daten\RHONE" in table and "keine ASCII-Dateien" in table   # kurzer Pfad ganz
        warn = tab._info_warn.cget("text")
        assert "1 Ordner mit Problem" in warn and "GSD im Dateinamen 2 m" in warn and "ueberschrieben" in warn

        tab._input_mode_var.set("single")
        tab._on_input_mode(refresh=False)
        assert tab._out_entry.get() == r"D:\raster" and tab._jahr_entry.get() == "2014"
        assert str(tab._out_entry.cget("state")) == "normal" and tab._info_meta.winfo_manager() == "grid"
    finally:
        app.destroy()
