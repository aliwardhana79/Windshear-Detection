# Juanda Windshear Agent

A local prototype for drafting advisory windshear assessments from manually entered observations and uploaded files. It uses an OpenAI-compatible chat-completions API; it does **not** provide live alerts.

## Run

Requires Python 3.9+, `pip install -r requirements.txt`, and an API key for a compatible model provider:

```sh
cd windshear_agent
export OPENAI_API_KEY="your-api-key"
# Optional; defaults to OpenAI-compatible defaults shown below
export OPENAI_BASE_URL="https://api.openai.com/v1"
export OPENAI_MODEL="gpt-4o-mini"
python3 app.py
```

Open <http://localhost:8080>. Set `PORT` to use another port. For the OpenAI-compatible provider configured in the parent `opencode.json`, use its base URL and an appropriate model ID with `OPENAI_BASE_URL` and `OPENAI_MODEL`; provide the key only through the environment. The observations typed into the app are sent to that configured provider for analysis.

## Current upload support (prototype)

- METAR/SPECI-like text: partial extraction of wind, visibility, temperature/dewpoint and QNH groups; the original report is retained as context.
- CSV: column names, row count and first 30 rows are passed as context (may include sensitive or irrelevant data; inspect before upload).
- Rainbow `.vol`: uses `wradlib` to inspect sweeps and moment metadata. This prototype does not yet apply the project-specific Rainbow scaling, quality masks, geo-referencing, or windshear calculations, so the result is only a file/moment inventory—not measured radar evidence.
- Local case analysis: reads timestamped files under `/Users/aliwardhana/Downloads/Training_TL/00_data/Radar/2025-03-07`, pairs the available Rainbow moment files, applies Rainbow min/max/depth scaling and summarizes the 0–10 km extended RWY10 final corridor at 0.4°, 0.8°, and 1.8°. It applies SNR/SQI/RhoHV masks only when those matching moment files are available. Radial-velocity spread is explicitly not interpreted as runway headwind or as a standalone microburst decision.
- NPZ: small numeric arrays are reduced to shape/range summaries; this is not a substitute for decoding the source radar volume.
- Images (PNG/JPEG/WebP/TIFF): attached for qualitative review if the chosen model supports vision. Do not expect scientific Himawari retrievals, calibrated cloud-top temperatures or wind derivation from images.
- Text files: passed as text with light METAR/SPECI-like group extraction. PIREP and radiosonde interpretation is not yet a validated parser.

Uploads are processed in memory and sent to the configured model provider. Up to 100 MB per request and 12 files are accepted; at most four images (8 MB each) are forwarded. Use sanitized, non-sensitive test data. Large posts are still subject to memory limits of this simple development server.

## Satellite workflow (Streamlit "Satellite Analyze" tab)

The tab runs in two explicit steps:

1. **Download the full disk** from JAXA P-Tree into a local folder. The default is `~/Himawari_9`, and the last folder you chose is remembered across restarts (`~/.windshear_agent.json`). Type the path directly, or press **Browse…** to open the folder picker inside the page: quick locations (Home, Desktop, Downloads, Documents), single-click navigation with **↑ Parent**, Windows drive buttons at a drive root, an editable path bar, new-folder creation, and **Use this folder** at the top and bottom. A **System dialog…** button inside the picker also tries the operating system dialog (Tk in a child process, Finder *choose folder* on macOS, the Shell folder dialog on Windows, `zenity` on Linux), but the in-page picker works without it — no Tk package is required. On Windows any drive can be selected, including a secondary data drive such as `D:\Himawari_9`. Paths are shown in the native format — `C:\Users\<you>\Himawari_9` on Windows, `/Users/<you>/Himawari_9` on macOS — and a path written for another operating system (for example a Linux `/home/...` path on macOS) is rejected with a hint instead of creating a stray folder. Folders the current user cannot write to are rejected before the download starts. The full-disk NetCDF is kept on disk (roughly 600 MB per scene); only missing scenes are fetched again.
2. **Analyse** scenes directly from that folder — no browser upload — or upload a file from the browser instead. Files in the local folder are opened from disk and only the Juanda slice is read, so a ~600 MB full disk does not have to fit in memory. Browser uploads in Step 2 accept up to **1 GB per scene** (a full-disk R21 NetCDF is ~600 MB), set by `maxUploadSize = 1024` in `.streamlit/config.toml` and `max_upload_size=1024` in `streamlit_app.py`. A single 600 MB upload to a hosted app may still fail with HTTP 500 because the platform holds the whole request in memory; use the parts workflow below in that case. Full-disk scenes are sliced to the default Juanda window; any cropped `*_ROI_*.nc` files already in the folder are analysed on their own extent, so crops produced by `crop_saved_products()` or the Flask app work too and appear automatically in the scene list. The dense cloud-motion overlay is still drawn for Juanda.

**Where downloads are saved.** The folder is always on the machine that runs this app. On a hosted deployment (for example Streamlit Community Cloud) that is the server container, not the computer you view the page from: the picker can only reach server folders such as `/tmp/Himawari_9`, the ~600 MB scenes use the server's ephemeral disk, and they are lost when the app restarts. To save into a folder on your own Mac or Windows PC, run `streamlit run streamlit_app.py` on that computer and open `http://localhost:8501`. System folders such as `/etc`, `/usr` or a Python `venv` are read-only; the picker warns and offers a temporary folder when you land in one.

### Upload a full disk to a hosted app in parts

Community Cloud rejects a single ~600 MB browser upload. To analyse a full disk there without running the app locally:

1. On your computer, drag the `NC_H09_...nc` file onto `split_for_upload.bat` (or run `python split_for_upload.py FILE`). A `FILE_parts` folder is created next to it containing `manifest.json` and 100 MB `.part` files. Use `python split_for_upload.py --part-mb=50 FILE` if the server still rejects a part.
2. In **Satellite Analyze → Step 2**, open **Large scene · upload a full disk in parts**. Upload `manifest.json`, then each `.part` file one at a time. Each part is checked with SHA-256 as it arrives; the panel shows how many parts and megabytes are still missing.
3. When all parts are present, press **Assemble full disk**. The parts are joined into the output folder, the full-file checksum is verified, the NetCDF is opened once to confirm it has latitude/longitude fields, and the scene appears in the local scene list.

The parts and the assembled scene live on the server's temporary disk. The panel shows the free space before you upload; about 0.8 GB is needed for one 600 MB scene. A container restart loses the parts, in which case you upload them again. This workflow moves the whole file through several smaller requests — it does not make the scene persist on your PC, and it does not bypass the server's disk limit.

### Run on Windows (to save into `C:\`)

1. Install Python 3.11 from <https://www.python.org/downloads/> and tick **Add python.exe to PATH** during setup.
2. Copy this project folder to the PC, for example `C:\Windshear`.
3. Double-click `run_windows.bat`. The first start creates `.venv` and installs the requirements (a few minutes); later starts are quick.
4. In **Satellite Analyze → Step 1**, set *Local output folder* to `C:\Himawari_9`. **Browse…** shows this PC's drives — go to `C:\`, use **New folder** to create `Himawari_9`, then **Use this folder**. Typing the path directly also works; the folder is created on the first download.

Run the app on the same PC that should hold the files. A hosted deployment writes to its Linux server, not to `C:\`, no matter what path is typed.

JAXA credentials are used for the FTP request only and are not written to disk.

## Standalone Rainbow `.vol` viewer

For reliable direct display without the web API, run the `wradlib` viewer:

```sh
python3 view_rainbow_vol.py "/path/to/2025030707191200dBZ.vol"
python3 view_rainbow_vol.py "/path/to/file.vol" --sweep 0 --save ppi.png
```

By default it displays all elevation sweeps with `matplotlib`; `--sweep` selects one zero-based sweep and `--save` writes an image. It prints a measurement summary per sweep. One Rainbow `.vol` file is one moment/product: the viewer does not combine moments, perform quality control or make a windshear decision.

## Next validated-data work

Before using this for decisions, apply and test the site's Rainbow moment scaling and QC using representative `.vol` samples; implement georeferencing and deterministic feature calculations; add radiosonde/PIREP schemas, geospatial/time alignment, provider vision checks, and a labeled case dataset with meteorologist review. Himawari products should be ingested as calibrated scientific data through an authoritative source—not inferred from arbitrary screenshots. Do not produce an automatic operational decision or alert.

## Safety and scope

- Advisory/training aid only; never an operational alert, clearance or go-around recommendation.
- Uses thresholds and Juanda-specific caveats from `../rwy10_lecture/README_lecture.md`.
- No live-data ingestion, authentication, or persistence. Keep it bound to localhost; do not expose this prototype to a network.
- Review every output against current approved procedures and qualified meteorological/ATC judgment.
