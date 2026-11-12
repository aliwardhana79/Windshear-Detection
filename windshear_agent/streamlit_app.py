#!/usr/bin/env python3
"""Streamlit interface for local Juanda radar, satellite and upper-air tools."""
from __future__ import annotations

import io
import json
import csv
import os
import shutil
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

st.set_page_config(page_title="Windshear Agent · Juanda", page_icon="🌦️", layout="wide")

ASSETS = Path(__file__).parent / "assets"
HEADER_IMAGE_NAMES = ("header.jpg", "header.jpeg", "header.png", "header.webp")


def accept(*extensions):
    """Streamlit matches upload extensions case-sensitively, so allow both cases."""
    return [form for ext in extensions for form in (ext, ext.upper())]


VOL_TYPES = accept("vol")
NC_TYPES = accept("nc", "nc4", "cdf")
# Step 2 scene uploads: 1 GB per file (a full-disk R21 NetCDF is ~600 MB).
# Keep this in sync with server.maxUploadSize in .streamlit/config.toml.
MAX_SCENE_UPLOAD_MB = 1024
CSV_TYPES = accept("csv")
OBS_TYPES = accept("csv", "txt", "metar", "speci")
SHEET_TYPES = accept("xls", "xlsx", "csv")
IMAGE_TYPES = accept("png", "jpg", "jpeg", "bmp", "webp", "tif", "tiff", "svg")


def header_image_path():
    """Return the first available header photo in assets/, if any."""
    for name in HEADER_IMAGE_NAMES:
        candidate = ASSETS / name
        if candidate.is_file():
            return candidate
    return None


header_text, header_photo = st.columns([2, 1], vertical_alignment="top")
with header_text:
    st.markdown(
        '<div class="header-block">'
        '<p style="font-size:1.3rem;font-weight:800;color:#1d4ed8;line-height:1.25;margin:0 0 0.45rem">'
        "Dr. Ali Wardhana | Innovation and Modeling of Aviation Meteorology (IMAM)"
        "</p>"
        '<h1 style="margin:0;padding:0">Read the signal.<br>Respect the uncertainty</h1>'
        '<p style="font-size:1.3rem;line-height:1.55;margin:0.5rem 0 0;opacity:0.78">'
        "&quot;Observe and Forecast with Precision, Fly with Decision.&quot; &mdash; "
        "Highlights the bridge between meteorology data and decisive flight-deck actions."
        "</p>"
        "</div>",
        unsafe_allow_html=True,
    )
with header_photo:
    photo = header_image_path()
    if photo is not None:
        st.image(str(photo), width=300)
    else:
        st.caption(f"Add a header photo as `assets/{HEADER_IMAGE_NAMES[0]}` to display it here.")
st.warning("Advisory and research only. Derived radar and satellite products are provisional, are not validated operational warnings, and must not be used alone for aviation decisions.")


def uploaded_pairs(files):
    return [(f.name, f.getvalue()) for f in files]


def home_folder():
    """Writable home directory, falling back to the app folder."""
    home = Path.home()
    if home.is_dir() and os.access(home, os.W_OK):
        return home
    return Path(__file__).resolve().parent


def foreign_path_problem(path_text):
    """Return a message when a path was written for a different operating system."""
    text = str(path_text or "").strip()
    drive_path = (len(text) >= 3 and text[0].isalpha() and text[1] == ":"
                  and text[2] in "\\/")
    unc_path = text.startswith("\\\\")
    if os.name == "nt":
        if text.startswith("/") and not drive_path and not unc_path:
            return ("This looks like a macOS/Linux path. On Windows use the drive form, "
                    "e.g. C:\\Users\\<you>\\Himawari_9 or D:\\Himawari_9.")
        return None
    if drive_path or unc_path:
        example = "/Users/<you>/Himawari_9" if sys.platform == "darwin" else "/home/<you>/Himawari_9"
        system = "macOS" if sys.platform == "darwin" else "Linux"
        return (f"This looks like a Windows path. On {system} use the form {example}.")
    if sys.platform == "darwin" and (
        text == "/home" or text.startswith("/home/")
        or text == "/root" or text.startswith("/root/")
        or text.startswith("/mnt/")
    ):
        return "This looks like a Linux path. On macOS use the form /Users/<you>/Himawari_9."
    return None


def writable_folder(path):
    """Return ``(resolved Path, problem)``; creates the folder if missing.

    ``problem`` is None when the folder can be used for downloads.
    """
    problem = foreign_path_problem(path)
    if problem:
        return None, problem
    try:
        candidate = Path(str(path)).expanduser()
    except (TypeError, ValueError) as exc:
        return None, f"Invalid folder path: {exc}"
    try:
        if candidate.exists() and not candidate.is_dir():
            return None, "That path is a file, not a folder."
        candidate.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return None, f"Cannot create or open this folder: {exc}"
    if not candidate.is_dir():
        return None, "That path is not a folder."
    try:
        with tempfile.NamedTemporaryFile(dir=candidate, prefix=".windshear_", delete=True):
            pass
    except OSError as exc:
        reason = getattr(exc, "strerror", None) or str(exc)
        return None, f"That folder is not writable ({reason})."
    return candidate.resolve(), None


def default_output_dir():
    """First writable candidate folder for Himawari downloads."""
    for candidate in (home_folder() / "Himawari_9",
                      Path(tempfile.gettempdir()) / "Himawari_9",
                      Path(__file__).resolve().parent / "Himawari_9",
                      Path.cwd() / "Himawari_9"):
        parent = candidate.parent
        if parent.is_dir() and os.access(parent, os.W_OK):
            return str(candidate)
    return str(Path.cwd())


def suggested_output_dir():
    """Writable example folder for this machine (native path format)."""
    for candidate in (home_folder() / "Himawari_9",
                      Path(tempfile.gettempdir()) / "Himawari_9"):
        if candidate.parent.is_dir() and os.access(candidate.parent, os.W_OK):
            return candidate
    return Path(tempfile.gettempdir())


def config_path():
    """JSON file that remembers the last Himawari output folder."""
    return Path.home() / ".windshear_agent.json"


def saved_output_dir():
    """Folder remembered by a previous session, or an empty string."""
    try:
        data = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    value = str(data.get("himawari_output_dir") or "").strip()
    if not value or foreign_path_problem(value):
        return ""
    return value


def save_output_dir(path):
    """Remember the chosen folder; best effort, never fails a download."""
    try:
        config_path().write_text(json.dumps({"himawari_output_dir": str(path)}, indent=2),
                                 encoding="utf-8")
    except OSError:
        pass


def hosted_deployment_note():
    """Explain that a hosted app writes to the server, not the visitor's computer."""
    try:
        host = (st.context.headers.get("host") or "").lower()
    except Exception:
        return None
    if host.endswith(".streamlit.app") or host.endswith(".streamlitapp.com"):
        return ("This app is running on Streamlit Community Cloud, so downloaded scenes are "
                "saved on the **server**, not on your own computer, and they disappear when the "
                "app restarts. To save into a folder on your Mac or Windows PC, run the app on "
                "that computer: `streamlit run streamlit_app.py`. To analyse a full disk here, "
                "split it with `split_for_upload.py` (or `split_for_upload.bat` on Windows) and "
                "use **Upload a full disk in parts** in Step 2.")
    return None


def free_disk_space(path):
    """Return free bytes for the nearest existing folder, or ``None``."""
    candidate = Path(path).expanduser()
    while not candidate.exists() and candidate.parent != candidate:
        candidate = candidate.parent
    try:
        return shutil.disk_usage(candidate).free
    except OSError:
        return None


_TK_FOLDER_SCRIPT = r'''
import sys

if len(sys.argv) < 3:
    raise SystemExit(4)

initial, title = sys.argv[1], sys.argv[2]
try:
    import tkinter
    from tkinter import filedialog
except Exception:
    raise SystemExit(3)

try:
    root = tkinter.Tk()
except Exception:
    raise SystemExit(3)

try:
    root.withdraw()
    try:
        root.attributes("-topmost", True)
        root.lift()
        root.focus_force()
    except Exception:
        pass
    root.update_idletasks()
    options = {"title": title, "mustexist": True}
    if initial:
        options["initialdir"] = initial
    chosen = filedialog.askdirectory(**options)
finally:
    try:
        root.destroy()
    except Exception:
        pass

print(chosen or "")
'''


def os_native_folder_dialog(initial_dir=None, prompt="Select the Himawari output folder"):
    """OS folder chooser used when the interpreter has no Tk support.

    Finder's *choose folder* on macOS, the Shell folder dialog on Windows and
    ``zenity`` on Linux. Returns the same ``(path, status)`` tuple as
    :func:`native_folder_dialog`.
    """
    import subprocess
    import sys

    initial = Path(str(initial_dir)).expanduser() if initial_dir else None
    if initial is not None and not initial.is_dir():
        initial = None

    if sys.platform == "darwin":
        script = f'POSIX path of (choose folder with prompt "{prompt}"'
        if initial is not None:
            script += f' default location POSIX file "{str(initial).replace(chr(34), chr(92) + chr(34))}"'
        script += ")"
        try:
            result = subprocess.run(["osascript", "-e", script],
                                    capture_output=True, text=True, timeout=180)
        except (OSError, subprocess.TimeoutExpired):
            return None, "unavailable"
        if result.returncode == 0:
            chosen = (result.stdout or "").strip()
            return (str(Path(chosen)), "selected") if chosen else (None, "cancelled")
        if "-128" in (result.stderr or "") or "cancel" in (result.stderr or "").lower():
            return None, "cancelled"
        return None, "unavailable"

    if sys.platform.startswith("win"):
        quoted_prompt = prompt.replace("'", "''")
        quoted_initial = str(initial).replace("'", "''") if initial is not None else ""
        script = ("Add-Type -AssemblyName System.Windows.Forms;"
                  "$dialog = New-Object System.Windows.Forms.FolderBrowserDialog;"
                  f"$dialog.Description = '{quoted_prompt}';"
                  "$dialog.ShowNewFolderButton = $true;")
        if quoted_initial:
            script += f"$dialog.SelectedPath = '{quoted_initial}';"
        script += ("$answer = $dialog.ShowDialog();"
                   "if ($answer -eq [System.Windows.Forms.DialogResult]::OK) {"
                   " Write-Output $dialog.SelectedPath }")
        for executable in ("powershell", "pwsh"):
            try:
                result = subprocess.run([executable, "-NoProfile", "-STA", "-Command", script],
                                        capture_output=True, text=True, timeout=180)
            except (OSError, subprocess.TimeoutExpired):
                continue
            if result.returncode != 0:
                continue
            lines = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
            if lines:
                return str(Path(lines[-1])), "selected"
            return None, "cancelled"
        return None, "unavailable"

    try:
        result = subprocess.run(["zenity", "--file-selection", "--directory",
                                 f"--title={prompt}"],
                                capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.TimeoutExpired):
        return None, "unavailable"
    if result.returncode == 0:
        chosen = (result.stdout or "").strip()
        return (str(Path(chosen)), "selected") if chosen else (None, "cancelled")
    return None, "cancelled"


def native_folder_dialog(initial_dir=None, prompt="Select the Himawari output folder"):
    """Open one cross-platform folder chooser.

    The Tk dialog runs in a child process, so it never touches Streamlit's
    script thread and the same code path is used on Windows, macOS and Linux.
    If the interpreter has no Tk (common with Homebrew Python), the operating
    system's own dialog is used instead. Returns ``(path, status)`` with
    status ``selected``, ``cancelled`` or ``unavailable``; a headless machine
    returns ``unavailable`` and the caller falls back to the in-page browser.
    """
    import subprocess
    import sys

    initial = Path(str(initial_dir)).expanduser() if initial_dir else None
    if initial is not None and not initial.is_dir():
        initial = None

    try:
        extra = {}
        if sys.platform.startswith("win"):
            extra["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        result = subprocess.run(
            [sys.executable, "-c", _TK_FOLDER_SCRIPT, str(initial) if initial else "", prompt],
            capture_output=True, text=True, timeout=180, **extra,
        )
    except subprocess.TimeoutExpired:
        return None, "unavailable"
    except OSError:
        return os_native_folder_dialog(initial, prompt)

    if result.returncode != 0:
        # No Tk in this interpreter (or no display): use the OS dialog.
        return os_native_folder_dialog(initial, prompt)

    chosen = (result.stdout or "").strip()
    if not chosen:
        return None, "cancelled"
    # str(Path(...)) renders the native separator: C:/Users/... -> C:\Users\...
    # on Windows, and leaves POSIX paths unchanged on macOS.
    return str(Path(chosen)), "selected"


def make_analysis_docx(html):
    """Convert rich-editor HTML into a formatted Word document."""
    import re
    from html.parser import HTMLParser
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Inches, Pt, RGBColor

    class RichTextParser(HTMLParser):
        def __init__(self, document):
            super().__init__(convert_charrefs=True)
            self.document, self.paragraph = document, None
            self.styles, self.lists, self.skip = [], [], 0

        def new_paragraph(self, tag="p", attrs=None):
            attrs = dict(attrs or [])
            style = {"h1": "Heading 1", "h2": "Heading 2", "h3": "Heading 3"}.get(tag)
            if self.lists:
                style = "List Number" if self.lists[-1] == "ol" else "List Bullet"
            self.paragraph = self.document.add_paragraph(style=style)
            css = attrs.get("style", "").replace(" ", "").lower()
            if "text-align:center" in css:
                self.paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
            elif "text-align:right" in css:
                self.paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
            elif "text-align:justify" in css:
                self.paragraph.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY

        def handle_starttag(self, tag, attrs):
            if tag in ("script", "style"):
                self.skip += 1
            elif tag in ("p", "div", "h1", "h2", "h3", "li"):
                self.new_paragraph(tag, attrs)
            elif tag in ("ol", "ul"):
                self.lists.append(tag)
            elif tag in ("strong", "b", "em", "i", "u", "s", "strike", "sub", "sup", "span", "a"):
                self.styles.append((tag, dict(attrs).get("style", "")))
            elif tag == "br":
                self.ensure_paragraph().add_run().add_break()

        def handle_endtag(self, tag):
            if tag in ("script", "style"):
                self.skip = max(0, self.skip - 1)
            elif tag in ("ol", "ul"):
                if self.lists:
                    self.lists.pop()
            elif tag in ("strong", "b", "em", "i", "u", "s", "strike", "sub", "sup", "span", "a"):
                if self.styles:
                    self.styles.pop()
            elif tag in ("p", "div", "h1", "h2", "h3", "li"):
                self.paragraph = None

        def ensure_paragraph(self):
            if self.paragraph is None:
                self.new_paragraph()
            return self.paragraph

        def handle_data(self, data):
            if self.skip or not data:
                return
            run = self.ensure_paragraph().add_run(data)
            tags = [tag for tag, _ in self.styles]
            run.bold = any(tag in tags for tag in ("b", "strong"))
            run.italic = any(tag in tags for tag in ("i", "em"))
            run.underline = "u" in tags
            run.font.strike = any(tag in tags for tag in ("s", "strike"))
            run.font.subscript = "sub" in tags
            run.font.superscript = "sup" in tags
            css = " ".join(style for _, style in self.styles)
            color = re.search(r"color:\s*#?([0-9a-fA-F]{6})", css)
            if color:
                run.font.color.rgb = RGBColor.from_string(color.group(1))
            size = re.search(r"font-size:\s*(\d+(?:\.\d+)?)px", css)
            if size:
                run.font.size = Pt(float(size.group(1)) * 0.75)

    document = Document()
    section = document.sections[0]
    section.top_margin = Inches(0.7)
    section.bottom_margin = Inches(0.7)
    title = document.add_heading("Comprehensive Aviation Weather Analysis", 0)
    title.paragraph_format.space_after = Pt(12)
    RichTextParser(document).feed(html or "")
    warning = document.add_paragraph()
    warning.paragraph_format.space_before = Pt(12)
    run = warning.add_run("Advisory/research only. Not an operational warning or flight-deck decision.")
    run.italic = True
    run.font.color.rgb = RGBColor(128, 64, 0)
    output = io.BytesIO()
    document.save(output)
    output.seek(0)
    return output.getvalue()


def show_b64_image(value, caption=None):
    import base64
    st.image(base64.b64decode(value), caption=caption, use_container_width=True)


def radar_scan_moment(filename):
    """Return (scan timestamp, moment) from Rainbow names like 2025030700011100dBuZ.vol."""
    import re
    match = re.match(r"^(\d{14})\d*([A-Za-z]+)\.vol$", Path(filename).name, re.IGNORECASE)
    return (match.group(1), match.group(2).lower()) if match else (None, None)


def validate_attenuation_inputs(volumes):
    """Check the uploaded filenames before decoding; True means proceed."""
    parsed = [(name, *radar_scan_moment(name)) for name, _ in volumes]
    parsed = [(name, stamp, moment) for name, stamp, moment in parsed if stamp]
    if not parsed:
        return True  # Unrecognised naming: let the decoder decide.
    phidp = [(name, stamp) for name, stamp, moment in parsed if moment == "phidp"]
    dbu = [(name, stamp) for name, stamp, moment in parsed if moment == "dbuz"]
    if not phidp:
        st.error("Attenuation correction needs a PhiDP volume. Add the file ending in `PhiDP.vol` from the same scan in the **PhiDP input** box.")
        return False
    if not dbu:
        st.error("Attenuation correction needs the uncorrected reflectivity. Add the file ending in `dBuZ.vol` to the moment files.")
        return False
    if not {stamp for _, stamp in phidp} & {stamp for _, stamp in dbu}:
        labels = lambda items: ", ".join(f"`{Path(name).name}`" for name, _ in items[:3])
        st.error(
            "These files cannot be paired because the scan times differ:\n\n"
            f"- dBuZ: {labels(dbu)}\n"
            f"- PhiDP: {labels(phidp)}\n\n"
            "Select the dBuZ and PhiDP volumes with the same scan timestamp."
        )
        return False
    return True


def radar_tab():
    from radar_analysis import process_uploaded_volumes
    st.subheader("Rainbow radar volumes")
    st.caption("Select matching moment files from the same scan. Quality masks only use matching fields present in the uploaded files.")
    files = st.file_uploader("Rainbow moment files (.vol)", type=VOL_TYPES, accept_multiple_files=True, key="radar_files")
    with st.expander("Quality-control thresholds", expanded=False):
        c1, c2, c3 = st.columns(3)
        thresholds = {
            "snr_min": c1.number_input("SNR minimum (dB)", value=7.0),
            "snr_max": c1.number_input("SNR maximum (dB)", value=75.0),
            "dbz_min": c2.number_input("Reflectivity minimum (dBZ)", value=0.0),
            "dbz_max": c2.number_input("Reflectivity maximum (dBZ)", value=75.0),
            "rhohv_min": c3.number_input("RhoHV minimum", min_value=0.0, max_value=1.0, value=0.85),
            "zdr_min": c1.number_input("ZDR minimum (dB)", value=0.0),
            "zdr_max": c1.number_input("ZDR maximum (dB)", value=6.0),
            "v_min": c2.number_input("V minimum (m/s)", value=-25.0),
            "v_max": c2.number_input("V maximum (m/s)", value=25.0),
            "w_min": c3.number_input("W minimum (m/s)", value=0.0),
            "w_max": c3.number_input("W maximum (m/s)", value=4.8),
            "kdp_min": c3.number_input("KDP minimum (deg/km)", value=-5.0),
            "kdp_max": c3.number_input("KDP maximum (deg/km)", value=15.0),
        }
    a, b = st.columns(2)
    moment = a.selectbox("Display moment", ["dBZ", "dBuZ", "V", "W", "RhoHV", "ZDR", "PhiDP", "KDP", "SNR", "SQI"])
    attenuation = b.selectbox("C-band attenuation correction", ["none", "linear_phidp", "zphi"], format_func=lambda x: {"none": "None", "linear_phidp": "Linear PhiDP", "zphi": "ZPHI"}[x])
    phidp = None
    if attenuation != "none":
        st.caption("Correction always uses the uncorrected **dBuZ** product plus the matching **PhiDP** volume from the same scan. Include the dBuZ file above and the PhiDP file here.")
        phidp = st.file_uploader("PhiDP input for attenuation correction (.vol)", type=VOL_TYPES, key="radar_phidp")
    st.caption("Attenuation parameters are provisional radar-specific assumptions; validate before scientific interpretation. KDP is a noisy finite-difference estimate.")
    if st.button("Process radar volumes", type="primary", key="run_radar"):
        if not files and phidp is None:
            st.error("Choose at least one .vol file.")
        else:
            volumes = uploaded_pairs(files or [])
            if phidp is not None:
                # Avoid sending the same volume twice if it was also selected above.
                existing = {(name, len(content)) for name, content in volumes}
                if (phidp.name, len(phidp.getvalue())) not in existing:
                    volumes.append((phidp.name, phidp.getvalue()))
            if attenuation != "none" and not validate_attenuation_inputs(volumes):
                return
            try:
                with st.spinner("Decoding, aligning and QC-filtering moments…"):
                    result = process_uploaded_volumes(volumes, display_moment=moment, thresholds=thresholds, attenuation_method=attenuation)
                st.session_state["comprehensive_radar"] = {
                    "files": [name for name, _ in volumes],
                    "moments": result.get("available_moments", []),
                    "display_moment": result.get("display_moment"),
                    "attenuation_method": result.get("attenuation_method"),
                    "sweeps": [{"elevation_deg": s.get("elevation_deg"), "range_max_km": s.get("range_max_km"), "statistics": s.get("statistics", {})} for s in result.get("sweeps", [])],
                    "caveat": result.get("caveat", ""),
                }
                st.info(f"Moments: {', '.join(result['available_moments'])} · Display: {result['display_moment']} · attenuation: {result['attenuation_method']}")
                st.warning(result["caveat"])
                for sweep in result["sweeps"]:
                    st.markdown(f"**Sweep {sweep['index'] + 1} · elevation {sweep['elevation_deg']:.2f}° · {sweep['range_max_km']} km**")
                    show_b64_image(sweep["preview"]["png_base64"])
                    st.json(sweep["statistics"])
            except Exception as exc:
                message = str(exc)
                st.error(f"Radar processing failed: {message}")
                if "PhiDP input is required" in message:
                    st.info("Add the file ending in `PhiDP.vol` from the same scan in the **PhiDP input** box, and keep the matching `dBuZ.vol` selected above.")


def satellite_tab():
    from satellite_analysis import (process_satellite_file, process_motion_files,
                                    process_convective_cooling_files, JUANDA_REGION, CTV699_REGION,
                                    HIMAWARI_BAND_IDS, HIMAWARI_BAND_LABELS,
                                    HIMAWARI_BAND_VARIABLES, DEFAULT_MOTION_BANDS)
    from dense_amv_ctv699 import process_uploaded_sequence
    from himawari_download import download_himawari, list_local_products
    from scene_parts import (MANIFEST_NAME, assemble, load_manifest, parse_manifest,
                             parts_status, save_manifest, save_part, validate_netcdf)

    st.subheader("Himawari satellite")
    st.caption("Two steps: load the Himawari full disk and choose its bands, then analyse scenes from that folder or an uploaded file.")

    # Restore the last folder chosen in a previous session, without creating
    # anything: a remembered folder that has since moved or become read-only
    # falls back to the default.
    initial_output_dir = saved_output_dir()
    if initial_output_dir:
        remembered = Path(initial_output_dir)
        if not (remembered.is_dir() and os.access(remembered, os.W_OK)):
            initial_output_dir = ""
    if not initial_output_dir:
        initial_output_dir = default_output_dir()
    if "hima_output_dir_text" not in st.session_state:
        st.session_state["hima_output_dir_text"] = initial_output_dir

    def _remember(path):
        """Persist the chosen folder and close the picker."""
        st.session_state["hima_output_dir_text"] = str(path)
        st.session_state["hima_browser_open"] = False
        save_output_dir(path)

    def _open_browser():
        candidate = Path(st.session_state.get("hima_output_dir_text", "")).expanduser()
        # A download folder may not exist yet: start at its nearest existing
        # parent instead of jumping to Home (which may be read-only on a server).
        start = candidate
        while not start.is_dir() and start.parent != start:
            start = start.parent
        if not start.is_dir():
            start = home_folder()
        st.session_state["hima_browse_path"] = str(start)
        st.session_state["hima_path_input"] = str(start)
        st.session_state["hima_browser_open"] = True

    def _toggle_browser():
        if st.session_state.get("hima_browser_open"):
            st.session_state["hima_browser_open"] = False
        else:
            _open_browser()

    def _go(path):
        st.session_state["hima_browse_path"] = str(path)
        st.session_state["hima_path_input"] = str(path)

    def _open_typed_path():
        target = Path((st.session_state.get("hima_path_input") or ".").strip() or ".").expanduser()
        if target.is_dir():
            st.session_state["hima_path_error"] = ""
            _go(target)
        else:
            st.session_state["hima_path_error"] = f"Not a folder: {target}"

    def _go_temp():
        """Jump to a folder this app can definitely write to."""
        target = Path(tempfile.gettempdir()) / "Himawari_9"
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError:
            target = Path(tempfile.gettempdir())
        _go(target)

    def _create_folder(parent):
        name = (st.session_state.get("hima_new_folder") or "").strip()
        if not name:
            st.session_state["hima_folder_error"] = "Enter a folder name first."
            return
        target = Path(parent) / name
        try:
            target.mkdir(parents=True, exist_ok=False)
            _go(target)
            st.session_state["hima_new_folder"] = ""
            st.session_state["hima_folder_error"] = ""
        except Exception as exc:
            st.session_state["hima_folder_error"] = str(exc)

    def _pick_native():
        candidate = Path(st.session_state.get("hima_output_dir_text", "")).expanduser()
        if st.session_state.get("hima_native_unavailable"):
            _open_browser()
            return
        path, status = native_folder_dialog(candidate)
        if status == "selected" and path:
            _remember(path)
        elif status == "unavailable":
            st.session_state["hima_native_unavailable"] = True
            _open_browser()

    def _select_all_bands():
        st.session_state["himawari_bands"] = list(HIMAWARI_BAND_IDS)

    def _clear_bands():
        st.session_state["himawari_bands"] = []

    def _render_folder_browser():
        current = Path(st.session_state.get("hima_browse_path") or home_folder()).expanduser()
        if not current.is_dir():
            current = home_folder()
        st.session_state["hima_browse_path"] = str(current)
        _, problem = writable_folder(current)
        if problem:
            st.warning(f"{problem} Choose a folder you can write to — `venv`, `etc` and other "
                       "system folders are usually read-only.")
            st.button("Go to a temporary folder", key="hima_fix_temp", on_click=_go_temp,
                      use_container_width=True,
                      help="Open a folder this app can write to on this machine.")

        head = st.columns([2, 1, 1])
        head[0].button("Use this folder", key="hima_use_folder", type="primary",
                       on_click=_remember, args=(str(current),), disabled=bool(problem),
                       use_container_width=True)
        head[1].button("Close", key="hima_close_browser", on_click=_toggle_browser,
                       use_container_width=True)
        head[2].button("System dialog\u2026", key="hima_system_dialog", on_click=_pick_native,
                       use_container_width=True,
                       help="Try the operating system folder dialog as well.")
        st.caption(f"Current folder: `{current}`")

        st.text_input("Folder path", key="hima_path_input")
        st.button("Open path", key="hima_path_open", on_click=_open_typed_path)
        if st.session_state.get("hima_path_error"):
            st.error(st.session_state["hima_path_error"])

        quick = [("Home", home_folder()),
                 ("Desktop", Path.home() / "Desktop"),
                 ("Downloads", Path.home() / "Downloads"),
                 ("Documents", Path.home() / "Documents")]
        quick = [(label, folder) for label, folder in quick if folder.is_dir()]
        if quick:
            quick_cols = st.columns(len(quick))
            for col, (label, folder) in zip(quick_cols, quick):
                col.button(label, key=f"hima_quick_{label}", on_click=_go,
                           args=(str(folder),), use_container_width=True)

        st.button("\u2191 Parent", key="hima_dir_up", on_click=_go,
                  args=(str(current.parent),), use_container_width=True)
        if os.name == "nt" and current.parent == current:
            drives = [Path(f"{letter}:\\") for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ"
                      if Path(f"{letter}:\\").exists()]
            if drives:
                st.caption("Drives")
                drive_cols = st.columns(min(len(drives), 6))
                for index, drive in enumerate(drives):
                    drive_cols[index % len(drive_cols)].button(
                        str(drive), key=f"hima_drive_{drive.drive}", on_click=_go,
                        args=(str(drive),), use_container_width=True)
        try:
            subdirs = sorted((p for p in current.iterdir() if p.is_dir() and not p.name.startswith(".")),
                             key=lambda p: p.name.lower())
        except OSError as exc:
            st.warning(f"Cannot list this folder: {exc}")
            subdirs = []
        if not subdirs:
            st.caption("No subfolders here. Create one below, or use this folder.")
        else:
            cols = st.columns(3)
            for index, folder in enumerate(subdirs[:30]):
                cols[index % 3].button(f"\U0001F4C1 {folder.name}",
                                       key=f"hima_dir_{abs(hash(str(folder)))}",
                                       on_click=_go, args=(str(folder),),
                                       use_container_width=True)
            if len(subdirs) > 30:
                st.caption(f"Showing 30 of {len(subdirs)} folders.")
        new_col, make_col = st.columns([3, 1], vertical_alignment="bottom")
        new_col.text_input("New folder name", key="hima_new_folder",
                           placeholder="e.g. Himawari_CGK")
        make_col.button("Create", key="hima_make_folder", on_click=_create_folder,
                        args=(str(current),), use_container_width=True)
        if st.session_state.get("hima_folder_error"):
            st.error(st.session_state["hima_folder_error"])
        st.button("Use this folder", key="hima_use_folder_bottom", type="primary",
                  on_click=_remember, args=(str(current),), disabled=bool(problem))

    # ---------------- Step 1: load the full disk ----------------
    st.markdown("#### Step 1 · Load Himawari full disk")
    with st.expander("JAXA P-Tree download", expanded=True):
        st.caption("Choose the UTC time window, interval and bands. The full-disk NetCDF (about 600 MB per scene) is saved on this machine and kept. Credentials are used for this request only.")
        note = hosted_deployment_note()
        if note:
            st.warning(note)
        username = st.text_input("JAXA username", key="jaxa_user")
        password = st.text_input("JAXA password", type="password", key="jaxa_password")
        c1, c2 = st.columns(2)
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None, second=0, microsecond=0)
        start = c1.datetime_input("Start time (UTC)", value=now_utc)
        end = c2.datetime_input("End time (UTC)", value=now_utc)
        interval = st.selectbox(
            "Interval (minutes)", [10, 20, 30, 60],
            help="10 minutes matches the standard Himawari full-disk cadence and is required by "
                 "the dense cloud-motion sequence. Two-scene feature motion works with any "
                 "interval of 5 minutes or more.",
        )
        band_left, band_all, band_clear = st.columns([6, 1, 1], vertical_alignment="bottom")
        if "himawari_bands" not in st.session_state:
            st.session_state["himawari_bands"] = list(HIMAWARI_BAND_IDS)
        with band_left:
            selected_bands = st.multiselect(
                "Himawari bands to make available for analysis",
                options=list(HIMAWARI_BAND_IDS),
                format_func=HIMAWARI_BAND_LABELS.get,
                key="himawari_bands",
                help="All 16 AHI bands are stored in every JAXA R21 full-disk file. "
                     "This selection controls which bands Step 2 offers for plotting and motion.",
            )
        band_all.button("All 16", key="hima_bands_all", on_click=_select_all_bands,
                        use_container_width=True)
        band_clear.button("Clear", key="hima_bands_clear", on_click=_clear_bands,
                          use_container_width=True)
        if not selected_bands:
            st.warning("No bands selected. Step 2 will fall back to Band 13; select bands above to analyse others.")
        else:
            st.caption(f"{len(selected_bands)} band(s) selected. Each JAXA file still contains all 16 bands.")
        c1, c2 = st.columns([4, 1], vertical_alignment="bottom")
        output_dir = c1.text_input("Local output folder (type a path or browse)",
                                   key="hima_output_dir_text")
        c2.button("Browse\u2026", key="hima_browse_btn", on_click=_toggle_browser,
                  use_container_width=True,
                  help="Opens the folder picker in this section of the page.")
        st.caption(f"Use this system's path format — example: `{suggested_output_dir()}`. "
                   "Files are saved on the machine running this app.")
        if st.session_state.get("hima_browser_open"):
            with st.container(border=True):
                _render_folder_browser()
        if st.button("Download full disk", key="download_hima_full", type="primary"):
            target, problem = writable_folder(output_dir)
            if problem:
                st.error(f"{problem} Choose a writable folder, for example `{suggested_output_dir()}`.")
            else:
                progress = st.progress(0, text="Starting download…")
                try:
                    def update(p):
                        progress.progress(min(100, int(p.get("percent", 0))),
                                          text=f"{p.get('stage', '')} · {p.get('current_time', '')}")
                    result = download_himawari(username, password,
                                               start.strftime("%Y%m%d%H%M"), end.strftime("%Y%m%d%H%M"),
                                               None, str(target), interval, update, crop=False)
                    save_output_dir(target)
                    st.success(f"Saved {len(result['full_disk_downloaded'])} new full-disk scene(s) to {result['output_dir']}")
                    st.caption("Full-disk files stay on disk on this machine; analyse them in Step 2.")
                    if result["not_found"]:
                        with st.expander(f"Not available at JAXA ({len(result['not_found'])})"):
                            st.dataframe(pd.DataFrame(result["not_found"]), use_container_width=True, hide_index=True)
                    if result["failed"]:
                        st.warning(f"{len(result['failed'])} download(s) failed.")
                        with st.expander("Failure detail"):
                            st.dataframe(pd.DataFrame(result["failed"]), use_container_width=True, hide_index=True)
                except Exception as exc:
                    st.error(f"Download failed: {exc}")

    # ---------------- Step 2: analyse scenes ----------------
    st.markdown("#### Step 2 · Analyse Himawari scenes")
    available_bands = [band for band in selected_bands if band in HIMAWARI_BAND_VARIABLES]
    if not available_bands:
        available_bands = ["B13"]
    if st.session_state.get("sat_band") not in available_bands:
        st.session_state["sat_band"] = "B13" if "B13" in available_bands else available_bands[0]
    st.caption("The band menus below use the Step 1 selection: "
               + ", ".join(available_bands) + ".")
    root = Path(output_dir).expanduser().resolve()
    inventory = list_local_products(root)
    local_names = [item["name"] for item in inventory["roi"] + inventory["full_disk"]]
    if not inventory["exists"]:
        st.info(f"Folder does not exist yet: {inventory['root']} — it is created on the first download.")
    elif not local_names:
        st.info(f"No scene files in {inventory['root']} yet. Download them in Step 1, or turn the toggle off to upload a file.")
    else:
        st.caption(f"{len(inventory['full_disk'])} full-disk and {len(inventory['roi'])} cropped scene(s) in {inventory['root']}")

    # After a parts upload is assembled, switch the toggle on before the widget
    # is created so the new local scene is selected in the next run.
    if "sat_use_local" not in st.session_state:
        st.session_state["sat_use_local"] = bool(local_names)
    if st.session_state.get("parts_just_assembled"):
        st.session_state["parts_just_assembled"] = False
        st.session_state["sat_use_local"] = True

    use_local = st.toggle("Read scenes from the local output folder (no upload)",
                          key="sat_use_local",
                          help="Reads scenes saved in Step 1 and any ROI crops already on disk. "
                               "Turn off to analyse a file uploaded from the browser instead.")
    if use_local and not local_names:
        st.info("No local scenes yet. Download in Step 1, or turn this toggle off to upload a file.")
    elif not use_local:
        st.caption("Browser uploads accept up to **1 GB per scene** (a full-disk R21 file is ~600 MB). "
                   "If one large upload fails with a 500 error, use **Upload a full disk in parts** "
                   "below — the scene is split on your computer and reassembled here. Scenes in the "
                   "local folder are read from disk without uploading.")

    def is_roi(name):
        return "_ROI_" in Path(name).name

    def local_region(name):
        # ROI crops already cover the analysis box (use the file's own extent);
        # full-disk scenes are sliced to the default Juanda window.
        return None if is_roi(name) else JUANDA_REGION

    def read_local(name):
        # Hand the Path to the analysis code so netCDF4 reads only the Juanda
        # slice; reading a ~600 MB full disk into memory would fail on Cloud.
        return (name, root / name)

    def pick_many(label, key):
        if use_local:
            names = st.multiselect(label, local_names, key=f"{key}_local")
            return [read_local(name) for name in names]
        files = st.file_uploader(label, type=NC_TYPES, accept_multiple_files=True, key=key,
                                 max_upload_size=MAX_SCENE_UPLOAD_MB)
        return uploaded_pairs(files or [])

    def pick_one(label, key):
        if use_local:
            name = st.selectbox(label, [""] + local_names, key=f"{key}_local")
            return read_local(name) if name else None
        uploaded = st.file_uploader(label, type=NC_TYPES, key=key,
                                    max_upload_size=MAX_SCENE_UPLOAD_MB)
        return (uploaded.name, uploaded.getvalue()) if uploaded else None

    # ---------------- Large scene upload in parts ----------------
    with st.expander("Large scene · upload a full disk in parts", expanded=False):
        st.caption(
            "Community Cloud rejects a single ~600 MB upload. Split the scene on your computer "
            "with `split_for_upload.py` (or drag it onto `split_for_upload.bat` on Windows), then "
            "upload `manifest.json` and each `.part` file here, one at a time. Parts are "
            "checksummed, joined on the server, and analysed from disk."
        )
        free = free_disk_space(root)
        if free is not None:
            st.caption(f"Temporary disk in `{root}`: **{free / 1e9:.1f} GB free**. "
                       "A 600 MB scene needs about 0.8 GB while assembling.")
        upload_dir = Path(tempfile.gettempdir()) / "windshear_upload_parts"
        st.caption(f"Incoming parts are held in `{upload_dir}` until assembly.")
        manifest_round = int(st.session_state.get("parts_manifest_round", 0))
        manifest_file = st.file_uploader(
            "1 · Upload manifest.json",
            type=accept("json"),
            key=f"parts_manifest_{manifest_round}",
            help="Created next to the .part files by split_for_upload.bat.",
        )
        manifest = None
        if manifest_file is not None:
            try:
                manifest = parse_manifest(manifest_file.getvalue())
                # Drop parts from an earlier file or an earlier split.
                listed = {entry["name"] for entry in manifest["parts"]}
                upload_dir.mkdir(parents=True, exist_ok=True)
                for path in upload_dir.glob("*.part"):
                    if path.name not in listed:
                        path.unlink(missing_ok=True)
                save_manifest(upload_dir, manifest)
            except (ValueError, OSError) as exc:
                st.error(f"Manifest rejected: {exc}")
        else:
            try:
                manifest = load_manifest(upload_dir)
                st.caption(f"Manifest already on the server for **{manifest['original_name']}**.")
            except (OSError, ValueError):
                manifest = None
        if manifest is None:
            st.info("Upload `manifest.json` from the split folder to begin.")
        else:
            received, missing = parts_status(upload_dir, manifest)
            done_bytes = sum(part["bytes"] for part in manifest["parts"] if part["index"] in received)
            st.progress(
                len(received) / float(manifest["part_count"]),
                text=(f"Parts received: {len(received)}/{manifest['part_count']} · "
                      f"{done_bytes / 1e6:.0f} MB of {manifest['total_bytes'] / 1e6:.0f} MB"),
            )
            if missing:
                st.caption("Next part(s): " + ", ".join(f"{index:03d}" for index in missing[:6])
                           + (" …" if len(missing) > 6 else ""))
                upload_round = int(st.session_state.get("parts_upload_round", 0))
                part_file = st.file_uploader(
                    "2 · Upload the next .part file",
                    type=accept("part"),
                    key=f"parts_upload_{upload_round}",
                    help="Upload one part per submission. Each part is checked with SHA-256.",
                )
                if part_file is not None:
                    try:
                        save_part(upload_dir, part_file.name, part_file.getvalue(), manifest)
                    except ValueError as exc:
                        st.error(f"Part rejected: {exc}")
                    except OSError as exc:
                        st.error(f"Could not save the part on the server: {exc}")
                    else:
                        st.session_state["parts_upload_round"] = upload_round + 1
                        st.rerun()
            else:
                st.success("All parts received and checksummed.")
                if st.button("3 · Assemble full disk", key="parts_assemble", type="primary"):
                    destination = root / manifest["original_name"]
                    try:
                        with st.spinner("Reassembling and checking the NetCDF…"):
                            assembled = assemble(upload_dir, manifest, destination)
                            validate_netcdf(assembled)
                    except Exception as exc:
                        st.error(f"Assembly failed: {exc}")
                    else:
                        (upload_dir / MANIFEST_NAME).unlink(missing_ok=True)
                        st.session_state["parts_manifest_round"] = manifest_round + 1
                        st.session_state["parts_upload_round"] = int(st.session_state.get("parts_upload_round", 0)) + 1
                        st.session_state["parts_just_assembled"] = True
                        st.rerun()

    st.markdown("##### Plot one band")
    single = pick_one("Scene", "sat_single")
    plot_band = st.selectbox("Band", options=available_bands,
                             format_func=HIMAWARI_BAND_LABELS.get, key="sat_band")
    with st.expander("Advanced: NetCDF variable override"):
        variable_override = st.text_input(
            "Variable name", key="sat_variable",
            placeholder=HIMAWARI_BAND_VARIABLES[plot_band],
            help="Leave blank to use the standard variable for the selected band.")
    variable = (variable_override or "").strip() or HIMAWARI_BAND_VARIABLES[plot_band]
    if st.button("Plot field", key="plot_sat"):
        if not single:
            st.error("Choose a scene first.")
        else:
            try:
                with st.spinner("Reading and plotting the field…"):
                    region_choice = local_region(single[0]) if use_local else JUANDA_REGION
                    out = process_satellite_file(single[0], single[1], variable, region=region_choice)
                st.session_state["comprehensive_satellite_field"] = {
                    "file": out.get("filename"), "variable": out.get("variable"),
                    "long_name": out.get("long_name"), "units": out.get("units"),
                    "grid_shape": out.get("grid_shape"), "region": out.get("region"),
                    "statistics": out.get("statistics"), "caveat": out.get("caveat", ""),
                }
                st.caption(f"{out['filename']} · {out['variable']} · {out['long_name']} · {out['units']} · grid {out['grid_shape']}")
                show_b64_image(out["preview"]["png_base64"])
                st.json(out["statistics"])
                st.warning(out["caveat"])
            except Exception as exc:
                st.error(f"Satellite plotting failed: {exc}")

    st.markdown("##### Two-scene feature-motion approximation")
    st.caption("Choose two scenes; the interval is read from their filenames (at least 5 minutes).")
    c1, c2 = st.columns(2)
    with c1:
        earlier = pick_one("Earlier scene", "motion_1")
    with c2:
        later = pick_one("Later scene", "motion_2")
    motion_defaults = [band for band in DEFAULT_MOTION_BANDS if band in available_bands]
    if not motion_defaults:
        motion_defaults = available_bands[:1]
    current_motion = st.session_state.get("sat_motion_bands")
    if current_motion is None or (current_motion and any(band not in available_bands for band in current_motion)):
        st.session_state["sat_motion_bands"] = motion_defaults
    motion_bands = st.multiselect(
        "Bands for motion", options=available_bands,
        format_func=HIMAWARI_BAND_LABELS.get, key="sat_motion_bands",
        help="Defaults to B03, B07 and B13 when available. A band that cannot be tracked "
             "is reported without stopping the other bands.",
    )
    if st.button(f"Estimate feature motion · {len(motion_bands)} band(s)", key="run_motion",
                 disabled=not motion_bands):
        if not earlier or not later:
            st.error("Choose both scenes.")
        else:
            try:
                if use_local and local_region(later[0]) != local_region(earlier[0]):
                    raise ValueError("Choose two scenes of the same kind: both full-disk or both cropped to one box.")
                with st.spinner("Tracking image features…"):
                    result = process_motion_files([earlier, later], band=tuple(motion_bands),
                                                  region=local_region(earlier[0]) if use_local else JUANDA_REGION)
                st.session_state["comprehensive_satellite_motion"] = {
                    "files": [earlier[0], later[0]],
                    "bands": [{k: v for k, v in band.items() if k not in ("preview",)} for band in result.get("bands", [])],
                    "failures": result.get("failures", []),
                    "caveat": result.get("caveat", ""),
                }
                st.warning(result["caveat"])
                for band in result["bands"]:
                    st.markdown(f"**Band B{band['band']:02d} · {band['variable']} · {band['time_delta_minutes']:g}-minute interval**")
                    show_b64_image(band["preview"]["png_base64"])
                    st.json({k: v for k, v in band.items() if k not in ("preview", "caveat")})
                    st.caption(band["caveat"])
                for failure in result.get("failures", []):
                    st.info(f"Band B{failure['band']:02d} ({failure['variable']}): {failure['error']}")
            except Exception as exc:
                st.error(f"Motion estimate failed: {exc}")

    st.markdown("##### Dense cloud-motion vectors")
    st.caption("Map overlays in this product are drawn for the Juanda area; they are not re-georeferenced for other crop boxes.")
    dense = pick_many("Select at least 3 consecutive scenes, exactly 10 minutes apart", "dense_files")
    if st.button("Process dense motion sequence", key="run_dense"):
        if len(dense) < 3:
            st.error("Select at least three scenes.")
        else:
            try:
                with st.spinner("Calculating dense cloud motion…"):
                    result = process_uploaded_sequence(dense)
                st.session_state["comprehensive_satellite_dense"] = {
                    "files": [f[0] for f in dense], "scenes": result.get("scenes"),
                    "products": [{k: v for k, v in product.items() if not k.endswith("base64")} for product in result.get("products", [])],
                    "caveat": result.get("caveat", ""),
                }
                st.warning(result["caveat"])
                for product in result["products"]:
                    st.markdown(f"**{product['time_utc']} · {product['valid_vectors']} valid vectors · {product['good_vectors']} good**")
                    for key in ("speed_png_base64", "direction_png_base64", "qc_png_base64"):
                        show_b64_image(product[key], key.replace("_png_base64", "").replace("_", " "))
            except Exception as exc:
                st.error(f"Dense motion failed: {exc}")

    st.markdown("##### Convective cooling / cold-cloud evolution")
    cooling_files = pick_many("Select 2–15 scenes with matching coverage", "cooling_files")
    if st.button("Process cooling sequence", key="run_cooling"):
        if len(cooling_files) < 2:
            st.error("Select at least two scenes.")
        else:
            try:
                with st.spinner("Calculating Band 13 diagnostics…"):
                    region_choice = None if use_local and is_roi(cooling_files[0][0]) else CTV699_REGION
                    result = process_convective_cooling_files(cooling_files, region=region_choice)
                st.session_state["comprehensive_satellite_cooling"] = {
                    "files": [f[0] for f in cooling_files],
                    "statistics": result.get("statistics", []),
                    "cold_core_centroids": result.get("cold_core_centroids", []),
                    "caveat": result.get("caveat", ""),
                }
                show_b64_image(result["summary_png_base64"], "Cold-cloud evolution")
                show_b64_image(result["cooling_png_base64"], "Apparent brightness-temperature change")
                st.dataframe(pd.DataFrame(result["statistics"]), use_container_width=True)
                st.dataframe(pd.DataFrame(result["cold_core_centroids"]), use_container_width=True)
                st.warning(result["caveat"])
            except Exception as exc:
                st.error(f"Cooling analysis failed: {exc}")


def sounding_panel(prefix):
    st.caption("CSV profile plots run locally on the server. Height is measured when present, otherwise estimated from pressure using a standard atmosphere.")
    file = st.file_uploader("Radiosonde / pilot-balloon CSV", type=CSV_TYPES, key=f"{prefix}_sounding_csv")
    if file:
        try:
            def normalize(value): return "".join(ch for ch in str(value).lower() if ch.isalnum())
            # Sounding exports may contain metadata and diagnostics with a
            # different field count from the actual profile. csv.reader keeps
            # those ragged records intact; pandas' default C parser rejects them.
            rows = list(csv.reader(io.StringIO(file.getvalue().decode("utf-8-sig", errors="replace"))))
            special_header = next((i for i, row in enumerate(rows) if any("obstime" in normalize(v) for v in row) and any("press0" in normalize(v) for v in row)), None)
            if special_header is not None:
                header = [v.strip() for v in rows[special_header]]
                records = rows[special_header + 1:]
            else:
                header_index = next((i for i, row in enumerate(rows) if row and not row[0].lstrip().startswith("#")), None)
                if header_index is None: raise ValueError("CSV is empty or contains only comments.")
                header = [v.strip() for v in rows[header_index]]
                records = rows[header_index + 1:]
            width = len(header)
            records = [row[:width] + [None] * max(0, width - len(row)) for row in records if any(cell.strip() for cell in row)]
            frame = pd.DataFrame(records, columns=header)
            normalized = {normalize(c): c for c in frame.columns}
            def col(*names): return next((normalized[n] for n in names if n in normalized), None)
            pcol, tcol = col("pressure", "pressurehpa", "press", "press0", "pmb"), col("temperature", "temperaturec", "temp", "temp0", "tdegc")
            if pcol is None or tcol is None: raise ValueError("Could not identify pressure and temperature columns in this CSV format.")
            dcol, rhcol = col("dewpoint", "dewpointc", "dewpt", "td"), col("relativehumidity", "humidity", "rh", "humi0")
            zcol, wscol, wdcol = col("height", "heightm", "altitude", "altitudem"), col("windspeed", "windspeedms", "ws"), col("winddirection", "winddirectiondeg", "wd")
            profile = pd.DataFrame({"p": pd.to_numeric(frame[pcol], errors="coerce"), "T": pd.to_numeric(frame[tcol], errors="coerce")})
            profile["Td"] = pd.to_numeric(frame[dcol], errors="coerce") if dcol else np.nan
            profile["RH"] = pd.to_numeric(frame[rhcol], errors="coerce") if rhcol else np.nan
            if dcol is None and rhcol:
                t, rh = profile["T"], profile["RH"]
                valid = rh.gt(0) & rh.le(100)
                gamma = np.log(rh[valid] / 100) + 17.625 * t[valid] / (243.04 + t[valid])
                profile.loc[valid, "Td"] = 243.04 * gamma / (17.625 - gamma)
            profile["z"] = pd.to_numeric(frame[zcol], errors="coerce") if zcol else np.nan
            profile["ws"] = pd.to_numeric(frame[wscol], errors="coerce") if wscol else np.nan
            profile["wd"] = pd.to_numeric(frame[wdcol], errors="coerce") if wdcol else np.nan
            profile = profile[(profile["p"] > 0) & profile["p"].le(1100) & profile["T"].between(-100, 60)].sort_values("p", ascending=False)
            if len(profile) < 5: raise ValueError(f"Only {len(profile)} valid pressure levels found.")
            profile["height_km"] = profile.z / 1000 if profile.z.notna().any() else 44330 * (1 - (profile.p / 1013.25) ** 0.1903) / 1000
            st.session_state["comprehensive_sounding"] = {
                "file": file.name, "valid_levels": len(profile),
                "pressure_hpa": [float(profile.p.min()), float(profile.p.max())],
                "height_km": [float(profile.height_km.min()), float(profile.height_km.max())],
                "wind_levels": int(profile.dropna(subset=["ws", "wd"]).shape[0]),
                "wind_speed_ms": ([float(profile.ws.min()), float(profile.ws.max())] if profile.ws.notna().any() else None),
            }
            st.caption(f"{file.name} · {len(profile)} valid levels")
            fig, ax = plt.subplots(figsize=(10, 8))
            fig.subplots_adjust(left=0.10, right=0.76, bottom=0.10, top=0.90)
            ax.set_yscale("log"); ax.set_ylim(min(1050, profile.p.max()), max(10, profile.p.min()))
            ax.set_ylabel("Pressure (hPa)"); ax.set_xlabel("Temperature (°C; skewed x-coordinate)"); ax.grid(True, which="both", alpha=.3)
            for temp_col, color, label in (("T", "tab:red", "Temperature"), ("Td", "tab:green", "Dewpoint")):
                good = profile[temp_col].notna()
                ax.plot(profile.loc[good, temp_col] + 35 * np.log(1000 / profile.loc[good, "p"]), profile.loc[good, "p"], color=color, label=label)
            ax.legend(loc="best"); ax.set_title("Skew-T Log-P (temperature axis skewed 35°C per natural-log pressure unit)")
            ax2 = ax.twinx(); ticks = [1000, 900, 800, 700, 600, 500, 400, 300, 200, 100]
            ticks = [p for p in ticks if profile.p.min() <= p <= profile.p.max()]
            ax2.set_yscale("log"); ax2.set_ylim(ax.get_ylim()); ax2.set_yticks(ticks); ax2.set_yticklabels([f"{np.interp(p, profile.p[::-1], profile.height_km[::-1]):.1f}" for p in ticks]); ax2.set_ylabel("Height (km; approximate if not measured)")
            wind = profile.dropna(subset=["ws", "wd", "p"]).copy()
            wind = wind[(wind.ws >= 0) & (wind.wd >= 0) & (wind.wd <= 360)]
            if not wind.empty:
                # Thin very high-resolution soundings to a readable set of levels.
                if len(wind) > 35:
                    keep = np.linspace(0, len(wind) - 1, 35).round().astype(int)
                    wind = wind.iloc[np.unique(keep)]
                direction = np.deg2rad(wind.wd.to_numpy(dtype=float))
                speed = wind.ws.to_numpy(dtype=float)
                # Meteorological direction is the direction the wind comes from.
                u = -speed * np.sin(direction)
                v = -speed * np.cos(direction)
                wind_ax = fig.add_axes([0.81, 0.10, 0.17, 0.80])
                wind_ax.set_yscale("log")
                wind_ax.set_ylim(ax.get_ylim())
                wind_ax.set_xlim(0, 1.7)
                wind_ax.set_xticks([])
                wind_ax.tick_params(axis="y", left=False, labelleft=False, right=False, labelright=False)
                for spine in wind_ax.spines.values(): spine.set_visible(False)
                wind_ax.barbs(np.full(len(wind), 0.48), wind.p.to_numpy(dtype=float), u, v,
                              length=5.5, linewidth=0.7, pivot="middle", barb_increments={"half": 2, "full": 4, "flag": 20})
                for pressure, value in zip(wind.p.to_numpy(dtype=float), speed):
                    wind_ax.annotate(f"{value:g}", (0.98, pressure), xycoords="data",
                                     xytext=(2, 0), textcoords="offset points", va="center", fontsize=7)
                wind_ax.set_title("Wind\nbarbs · m/s", fontsize=9, pad=5)
            st.pyplot(fig, use_container_width=True); plt.close(fig)
            charts = st.columns(3)
            for axis, value, title, color, limits in zip(charts, ["ws", "wd", "RH"], ["Wind speed (m/s)", "Wind direction (°)", "Relative humidity (%)"], ["tab:blue", "purple", "teal"], [(0, None), (0, 360), (0, 100)]):
                fig, ax = plt.subplots(figsize=(4.4, 5.5)); data = profile.dropna(subset=[value]);
                if len(data): ax.plot(data[value], data.height_km, color=color); ax.set_xlim(*limits) if limits[1] is not None else None
                ax.set_xlabel(title); ax.set_ylabel("Height (km)"); ax.grid(alpha=.3); axis.pyplot(fig, use_container_width=True); plt.close(fig)
            with st.expander("Parsed sounding table"): st.dataframe(profile, use_container_width=True)
        except Exception as exc: st.error(f"Could not plot sounding: {exc}")
    for label, key in (("Wind profiler CSV", f"{prefix}_profiler_csv"), ("LIDAR CSV", f"{prefix}_lidar_csv")):
        f = st.file_uploader(label, type=CSV_TYPES, key=key)
        if f:
            try: st.dataframe(pd.read_csv(io.BytesIO(f.getvalue())), use_container_width=True)
            except Exception as exc: st.error(f"Could not read {label}: {exc}")


METAR_PATTERNS = {
    "wind": r"\b(?:\d{3}|VRB)\d{2,3}(?:G\d{2,3})?(?:KT|MPS|KMH)\b",
    "visibility": r"\b(?:\d{4}|CAVOK)\b",
    "temperature_dewpoint_C": r"\bM?\d{2}/M?\d{2}\b",
    "qnh_hPa": r"\bQ\d{4}\b",
}


def parse_metar(report):
    """Extract the common groups from one raw METAR/SPECI string; keep it unmodified."""
    import re
    text = str(report).strip()
    parsed = {"raw": text}
    for field, pattern in METAR_PATTERNS.items():
        found = re.findall(pattern, text)
        parsed[field] = found[0] if found else ""
    return parsed


def show_observation_table(frame):
    st.dataframe(frame, use_container_width=True, hide_index=True)
    st.caption("Parsing is partial. Compare against the original report before using any field.")


LLWAS_STATIONS = [
    ("A2 · Tambak Cemandi", -7.3931670, 112.8114620),
    ("A3 · LANUDAL Timur / Wisma Perwira", -7.3882496, 112.7950262),
    ("A4 · LANUDAL Selatan / Pos Gerbang", -7.3855002, 112.7772954),
    ("A5 · Sedati Agung", -7.3843075, 112.7595432),
    ("A6 · Semambung", -7.3785123, 112.7418114),
    ("B2 · Banjar Kemuning", -7.3774845, 112.8150496),
    ("B3 · OTBAN Taman / Terminal 1 parking", -7.3720400, 112.7979481),
    ("B4 · OTBAN Timur / Basarnas", -7.3688443, 112.7803874),
    ("B5 · Tropodo", -7.3652068, 112.7619150),
    ("B6 · Sawotratap", -7.3651160, 112.7443973),
]


def load_kkop_lines():
    """Load packaged KKOP boundaries as Leaflet [lat, lon] polyline arrays."""
    path = Path(__file__).parent / "assets" / "kkop.geojson"
    if not path.is_file():
        return []
    try:
        collection = json.loads(path.read_text(encoding="utf-8"))
        lines = []
        for feature in collection.get("features", []):
            geometry = feature.get("geometry") or {}
            kind, coordinates = geometry.get("type"), geometry.get("coordinates", [])
            if kind == "LineString":
                candidates = [coordinates]
            elif kind == "MultiLineString":
                candidates = coordinates
            else:
                continue
            for line in candidates:
                points = [[float(point[1]), float(point[0])] for point in line if len(point) >= 2]
                if len(points) >= 2:
                    lines.append(points)
        return lines
    except (OSError, ValueError, TypeError, IndexError):
        return []


def show_llwas_station_map():
    import json

    stations_json = json.dumps([
        {"name": name, "lat": lat, "lon": lon} for name, lat, lon in LLWAS_STATIONS
    ])
    kkop_json = json.dumps(load_kkop_lines())
    map_html = f"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>html,body,#map{{height:100%;width:100%;margin:0}} .legend{{background:white;padding:7px 10px;border-radius:4px;font:13px Arial;line-height:1.7;box-shadow:0 1px 5px #777}} .station-dot{{color:#1769aa;font-size:24px;font-weight:bold;line-height:20px;text-shadow:0 0 3px white}} .airport-star{{color:#d62728;font-size:23px;text-shadow:0 0 3px white}}</style>
</head><body><div id="map"></div>
<script>
const stations = {stations_json};
const kkopLines = {kkop_json};
const map = L.map('map').setView([-7.3797, 112.7873], 12);
L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
  maxZoom: 19,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
}}).addTo(map);
kkopLines.forEach(line => L.polyline(line, {{color:'#e32636', weight:3, opacity:0.95, dashArray:'8 5'}})
  .addTo(map).bindPopup('<b>KKOP Juanda</b><br>Reference boundary'));
stations.forEach(s => L.marker([s.lat, s.lon], {{
  icon: L.divIcon({{className:'station-dot', html:'▲', iconSize:[22,22], iconAnchor:[11,11]}})
}}).addTo(map).bindPopup('<b>'+s.name+'</b><br>LLWAS reference location'));
L.marker([-7.3797, 112.7873], {{
  icon: L.divIcon({{className:'airport-star', html:'★', iconSize:[24,24], iconAnchor:[12,12]}})
}}).addTo(map).bindPopup('<b>Juanda Airport</b> (reference)');
L.control.scale({{metric:true, imperial:false}}).addTo(map);
L.control({{position:'topright'}}).onAdd = function() {{
  const div = L.DomUtil.create('div','legend');
  div.innerHTML = '<span style="color:#1769aa">▲</span> LLWAS station<br><span style="color:#d62728">★</span> Juanda Airport<br><span style="color:#e32636">━ ━</span> KKOP boundary';
  return div;
}}.addTo(map);
</script></body></html>"""
    components.html(map_html, height=540, scrolling=False)
    st.caption("Map tiles require an internet connection and are provided by OpenStreetMap. LLWAS markers are static locations digitized from the [user-provided Google My Maps layer](https://www.google.com/maps/d/viewer?mid=1qYkHx_INlEsNiOI2Pn4jjgvwfLZA7aZG&hl=en&usp=sharing); the red dashed line is the KKOP reference boundary from the supplied GeoJSON. Neither layer displays live winds.")


def surface_tab():
    st.subheader("Surface Analyze")
    st.caption("Surface observations only: METAR/SPECI reports and LLWAS data. Radiosonde, wind profiler and LIDAR sources are in Upper Air Analyze. METAR parsing is partial; LLWAS is displayed as supplied.")
    st.markdown("##### METAR / SPECI reports")
    metar_file = st.file_uploader("METAR/SPECI file (.csv or .txt)", type=OBS_TYPES, key="surface_metar")
    if metar_file:
        try:
            text = metar_file.getvalue().decode("utf-8-sig", errors="replace")
            if metar_file.name.lower().endswith(".csv"):
                source = pd.read_csv(io.StringIO(text), dtype=object, encoding="utf-8-sig")
                column = st.selectbox("Column containing the raw report", list(source.columns), key="surface_metar_column")
                reports = source[column].dropna().astype(str).tolist()
                st.dataframe(source, use_container_width=True, hide_index=True)
            else:
                reports = [line.strip() for line in text.splitlines() if line.strip()]
            parsed = pd.DataFrame([parse_metar(report) for report in reports])
            st.session_state["comprehensive_metar"] = {
                "file": metar_file.name, "reports": len(parsed),
                "parsed_fields": [name for name in ("wind", "visibility", "temperature_dewpoint_C", "qnh_hPa") if name in parsed and parsed[name].astype(str).str.len().gt(0).any()],
            }
            if parsed.empty:
                st.info("No report lines found in this file.")
            else:
                st.markdown(f"**{len(parsed)} report(s) parsed**")
                show_observation_table(parsed)
        except Exception as exc:
            st.error(f"Could not read METAR/SPECI file: {exc}")

    st.markdown("##### LLWAS observations")
    st.caption("Low-level windshear alerting system data. Threshold/mid/far and tower winds should be fused with radar and pilot reports before any interpretation.")
    show_llwas_station_map()
    llwas_file = st.file_uploader("LLWAS CSV", type=CSV_TYPES, key="surface_llwas")
    if llwas_file:
        try:
            frame = pd.read_csv(io.BytesIO(llwas_file.getvalue()), encoding="utf-8-sig")
            if frame.empty:
                st.info("The LLWAS file has no data rows.")
            else:
                st.session_state["comprehensive_llwas"] = {
                    "file": llwas_file.name, "rows": len(frame),
                    "columns": [str(c) for c in frame.columns],
                    "numeric_summary": frame.select_dtypes("number").describe().round(2).to_dict(),
                }
                show_observation_table(frame)
                numeric = frame.select_dtypes("number")
                if not numeric.empty:
                    st.markdown("**Numeric summary**")
                    st.dataframe(numeric.describe().T, use_container_width=True)
                st.caption("Column meanings vary by LLWAS export; confirm sensor, height and units before use.")
        except Exception as exc:
            st.error(f"Could not read LLWAS CSV: {exc}")


def upper_air_tab():
    st.subheader("Upper Air Analyze")
    st.caption("Skew-T sounding plots and upper-air CSV previews.")
    sounding_panel("upper")


def pilot_tab():
    st.subheader("Pilot report date lookup")
    file = st.file_uploader("Pilot report file (.xls, .xlsx, .csv)", type=SHEET_TYPES, key="pilot_file")
    if not file: return
    try:
        if Path(file.name).suffix.lower() == ".csv": frame = pd.read_csv(io.BytesIO(file.getvalue()), dtype=object, encoding="utf-8-sig")
        else: frame = pd.read_excel(io.BytesIO(file.getvalue()), engine="xlrd" if file.name.lower().endswith(".xls") else "openpyxl", dtype=object)
        if frame.empty: st.info("No data rows in file."); return
        date_column = st.selectbox("Report date column", list(frame.columns))
        accident_date = st.date_input("Accident date", value=date.today())
        st.session_state["comprehensive_pirep_upload"] = {
            "file": file.name, "rows": len(frame), "date_column": str(date_column),
            "selected_date": accident_date.isoformat(),
        }
        if st.button("Show reports for selected date", type="primary"):
            dates = pd.to_datetime(frame[date_column], errors="coerce", dayfirst=False).dt.date
            matches = frame.loc[dates == accident_date]
            st.session_state["comprehensive_pirep"] = {
                **st.session_state["comprehensive_pirep_upload"],
                "matching_rows": len(matches),
            }
            st.success(f"{len(matches)} matching row(s) in {file.name} for {accident_date.isoformat()}.")
            if matches.empty: st.info("No rows matched the selected date.")
            else: st.dataframe(matches, use_container_width=True, hide_index=True)
    except Exception as exc: st.error(f"Could not read pilot report: {exc}")


def comprehensive_tab():
    st.subheader("Cross-source evidence overview")
    st.caption("Attach files here for the cross-source inventory, or analyze them in their dedicated tabs. Attachments here are not automatically processed or fused into a forecast or operational decision.")
    state = st.session_state
    with st.expander("Attach source files", expanded=True):
        st.caption("Image attachments appear in the Status table as attached, but are not decoded as source measurements. Use the dedicated source tabs to analyze raw radar, satellite, or sounding data.")
        c1, c2, c3 = st.columns(3)
        with c1:
            radar_upload = st.file_uploader("Radar plot/image (.jpg, .jpeg, .png, .bmp, .svg, .webp, .tif)", type=IMAGE_TYPES, accept_multiple_files=True, key="comprehensive_radar_files")
            metar_upload = st.file_uploader("METAR/SPECI (.csv, .txt)", type=OBS_TYPES, key="comprehensive_metar_file")
        with c2:
            satellite_upload = st.file_uploader("Satellite image (.jpg, .jpeg, .png, .bmp, .svg, .webp, .tif)", type=IMAGE_TYPES, accept_multiple_files=True, key="comprehensive_satellite_files")
            llwas_upload = st.file_uploader("LLWAS observations (.csv)", type=CSV_TYPES, key="comprehensive_llwas_file")
        with c3:
            sounding_upload = st.file_uploader("Upper-air plot/image (.jpg, .jpeg, .png, .bmp, .svg, .webp, .tif)", type=IMAGE_TYPES, key="comprehensive_sounding_file")
            pirep_upload = st.file_uploader("Pilot reports (.xls, .xlsx, .csv)", type=SHEET_TYPES, key="comprehensive_pirep_file")

    attached = {
        "Radar": [f.name for f in (radar_upload or [])],
        "Satellite": [f.name for f in (satellite_upload or [])],
        "Surface · METAR/SPECI": [metar_upload.name] if metar_upload else [],
        "Surface · LLWAS": [llwas_upload.name] if llwas_upload else [],
        "Upper air sounding": [sounding_upload.name] if sounding_upload else [],
        "Pilot report": [pirep_upload.name] if pirep_upload else [],
    }
    with st.expander("Preview attached images and tables", expanded=False):
        image_groups = [
            ("Radar image", radar_upload or []),
            ("Satellite image", satellite_upload or []),
            ("Upper-air image", [sounding_upload] if sounding_upload else []),
        ]
        for label, uploads in image_groups:
            for uploaded in uploads:
                st.markdown(f"**{label}: {uploaded.name}**")
                if Path(uploaded.name).suffix.lower() == ".svg":
                    st.info("SVG attached. For safety, active SVG content is not rendered in-page; open the downloaded file in a trusted viewer.")
                    st.download_button("Download SVG", uploaded.getvalue(), file_name=Path(uploaded.name).name,
                                       mime="image/svg+xml", key=f"preview_svg_{label}_{uploaded.name}")
                else:
                    try:
                        from PIL import Image
                        image = Image.open(io.BytesIO(uploaded.getvalue()))
                        image.thumbnail((1600, 1200))
                        if image.mode not in ("RGB", "RGBA"):
                            image = image.convert("RGBA" if "transparency" in image.info else "RGB")
                        st.image(image, caption=uploaded.name, use_container_width=True)
                    except Exception as exc:
                        st.warning(f"Could not preview this image: {exc}")

        table_uploads = [
            ("METAR/SPECI", metar_upload),
            ("LLWAS", llwas_upload),
            ("Pilot report", pirep_upload),
        ]
        for label, uploaded in table_uploads:
            if uploaded is None:
                continue
            st.markdown(f"**{label}: {uploaded.name}**")
            suffix = Path(uploaded.name).suffix.lower()
            try:
                if suffix == ".csv":
                    preview = pd.read_csv(io.BytesIO(uploaded.getvalue()), encoding="utf-8-sig", nrows=100)
                    st.dataframe(preview, use_container_width=True, hide_index=True)
                    st.caption("Showing up to the first 100 rows.")
                elif suffix in (".xls", ".xlsx"):
                    engine = "xlrd" if suffix == ".xls" else "openpyxl"
                    preview = pd.read_excel(io.BytesIO(uploaded.getvalue()), engine=engine, nrows=100)
                    st.dataframe(preview, use_container_width=True, hide_index=True)
                    st.caption("Showing up to the first 100 rows.")
                else:
                    text = uploaded.getvalue().decode("utf-8-sig", errors="replace")
                    st.code("\n".join(text.splitlines()[:100]), language="text")
                    st.caption("Showing up to the first 100 lines.")
            except Exception as exc:
                st.warning(f"Could not preview this file: {exc}")

    sources = [
        ("Radar", "comprehensive_radar", "Radar Analyze"),
        ("Satellite", ("comprehensive_satellite_field", "comprehensive_satellite_motion", "comprehensive_satellite_dense", "comprehensive_satellite_cooling"), "Satellite Analyze"),
        ("Surface · METAR/SPECI", "comprehensive_metar", "Surface Analyze"),
        ("Surface · LLWAS", "comprehensive_llwas", "Surface Analyze"),
        ("Upper air sounding", "comprehensive_sounding", "Upper Air Analyze"),
        ("Pilot report", ("comprehensive_pirep", "comprehensive_pirep_upload"), "Pilot Report"),
    ]
    rows = []
    for label, keys, menu in sources:
        keys = (keys,) if isinstance(keys, str) else keys
        analyzed = [key for key in keys if key in state]
        files = attached[label]
        if analyzed:
            status = "Analyzed / available"
            details = "; ".join(str(state[key].get("file", ", ".join(state[key].get("files", [])))) for key in analyzed)
        elif files:
            status = "File attached · not analyzed"
            details = ", ".join(files)
        else:
            status = "Not attached / analyzed"
            details = f"Attach above or use {menu}"
        rows.append({"Source": label, "Status": status, "Details": details})
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    st.markdown("#### Your written analysis")
    st.caption("Use the toolbar to format text like a Word document: paragraph styles, bold/italic/underline, font size/color, alignment, lists, indentation and links.")
    from streamlit_quill import st_quill
    analysis_html = st_quill(
        value=state.get("comprehensive_analysis_html", ""),
        placeholder="Enter event context, observations, source-by-source interpretation, alignment, uncertainty, and follow-up checks…",
        html=True,
        toolbar=[
            [{"header": [1, 2, 3, False]}, {"size": ["small", False, "large", "huge"]}],
            ["bold", "italic", "underline", "strike"],
            [{"color": []}, {"background": []}],
            [{"list": "ordered"}, {"list": "bullet"}, {"indent": "-1"}, {"indent": "+1"}],
            [{"align": []}],
            ["blockquote", "link", "clean"],
        ],
        key="comprehensive_analysis_editor",
    ) or ""
    state["comprehensive_analysis_html"] = analysis_html
    if analysis_html.strip():
        import bleach
        preview_html = bleach.clean(
            analysis_html,
            tags={"p", "br", "strong", "b", "em", "i", "u", "s", "ol", "ul", "li", "h1", "h2", "h3", "span", "blockquote", "a"},
            attributes={"span": ["style"], "a": ["href", "title"], "p": ["style"], "h1": ["style"], "h2": ["style"], "h3": ["style"]},
            protocols={"http", "https", "mailto"},
            strip=True,
        )
        with st.expander("Formatted preview", expanded=True):
            st.markdown(preview_html, unsafe_allow_html=True)
        st.download_button(
            "Save analysis as Word document (.docx)",
            data=make_analysis_docx(analysis_html),
            file_name=f"comprehensive_analysis_{date.today().isoformat()}.docx",
            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            key="download_comprehensive_docx",
        )

    if not any(row["Status"] == "Analyzed / available" for row in rows):
        st.info("No source analysis results are available yet. Files attached above are inventoried only; run the relevant source analysis to generate summaries.")

    for title, key in (
        ("Radar analysis", "comprehensive_radar"),
        ("Satellite single-field plot", "comprehensive_satellite_field"),
        ("Satellite two-scene motion", "comprehensive_satellite_motion"),
        ("Satellite dense motion", "comprehensive_satellite_dense"),
        ("Satellite cooling diagnostics", "comprehensive_satellite_cooling"),
        ("METAR/SPECI summary", "comprehensive_metar"),
        ("LLWAS summary", "comprehensive_llwas"),
        ("Upper-air sounding summary", "comprehensive_sounding"),
        ("Pilot report date lookup", "comprehensive_pirep"),
    ):
        if key in state:
            with st.expander(title, expanded=False):
                st.json(state[key])

    st.warning("Time and location alignment is not inferred across sources. Confirm observation times, station/sensor identities, units, coverage, and quality independently before comparing evidence. Missing inputs must not be interpreted as normal conditions.")
    st.error("Research and advisory use only. Radar attenuation, satellite motion/cooling, and parsed observations are provisional; this overview is not a validated operational warning, clearance, or flight-deck decision.")


tabs = st.tabs(["Radar Analyze", "Satellite Analyze", "Surface Analyze", "Upper Air Analyze", "Pilot Report", "Comprehensive Analysis"])
with tabs[0]: radar_tab()
with tabs[1]: satellite_tab()
with tabs[2]: surface_tab()
with tabs[3]: upper_air_tab()
with tabs[4]: pilot_tab()
with tabs[5]: comprehensive_tab()

st.caption("Derived radar attenuation and satellite motion/cooling displays are provisional research products only—not validated operational decisions.")
