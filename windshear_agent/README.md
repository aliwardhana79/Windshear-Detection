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

The tab runs in three explicit steps:

1. **Download the full disk** from JAXA P-Tree into a local folder. The full-disk NetCDF is kept on disk (roughly 600 MB per scene); only missing scenes are fetched again.
2. **Crop** any saved full disk to a south/north/west/east box. The ROI copy is written next to the full disk as `..._ROI_<south>_<west>_<north>_<east>.nc`; the original file is left untouched, so a new box can be cropped later without re-downloading.
3. **Analyse** the cropped scenes directly from that folder — no browser upload. Plotting, two-scene motion, and cooling use the crop's own extent, so boxes outside the Juanda area also work. The dense cloud-motion overlay is still drawn for Juanda.

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
