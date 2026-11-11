"""Split and reassemble large NetCDF scenes for the hosted Streamlit app.

A ~600 MB Himawari full disk is split on the user's computer into ~100 MB
parts.  Each part is uploaded separately, written to the server's temporary
disk, and the parts are concatenated again.  The analysis code then opens the
assembled file from disk and reads only the Juanda slice, so the whole scene
never has to fit in server memory.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path

MANIFEST_NAME = "manifest.json"
MANIFEST_FORMAT = "windshear-himawari-parts/1"
PART_RE = re.compile(r"^(?P<name>.+)\.(?P<index>\d{3})-of-(?P<count>\d{3})\.part$")
MAX_TOTAL_BYTES = 4 * 1024 ** 3
MAX_PARTS = 200
MIN_PART_MB = 10
CHUNK_BYTES = 1024 * 1024


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def safe_name(name):
    """Return a bare file name, rejecting empty or directory-like values."""
    text = str(name or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not text or text in {".", ".."} or "\x00" in text:
        raise ValueError("Invalid file name in the manifest.")
    return text


def _valid_digest(value):
    return bool(re.fullmatch(r"[0-9a-f]{64}", str(value or "").strip().lower()))


def parse_manifest(raw):
    """Validate and normalise manifest JSON supplied as bytes."""
    try:
        data = json.loads(raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError(f"manifest.json is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or data.get("format") != MANIFEST_FORMAT:
        raise ValueError("This is not a manifest created by split_for_upload.py.")
    original = safe_name(data.get("original_name"))
    try:
        count = int(data.get("part_count"))
        total = int(data.get("total_bytes"))
    except (TypeError, ValueError) as exc:
        raise ValueError("Manifest part count or file size is invalid.") from exc
    if not 1 <= count <= MAX_PARTS:
        raise ValueError("Manifest part count is out of range.")
    if not 1 <= total <= MAX_TOTAL_BYTES:
        raise ValueError("Manifest file size is out of range.")
    parts = data.get("parts")
    if not isinstance(parts, list) or len(parts) != count:
        raise ValueError("Manifest does not list every part.")
    cleaned = []
    seen = set()
    for entry in parts:
        if not isinstance(entry, dict):
            raise ValueError("Manifest part entry is invalid.")
        try:
            index = int(entry.get("index"))
            size = int(entry.get("bytes"))
        except (TypeError, ValueError) as exc:
            raise ValueError("Manifest part index or size is invalid.") from exc
        name = safe_name(entry.get("name"))
        if index < 1 or index > count or index in seen:
            raise ValueError("Manifest part indices are invalid.")
        if not 0 < size <= total:
            raise ValueError("Manifest part size is invalid.")
        match = PART_RE.match(name)
        if (not match or int(match.group("index")) != index
                or int(match.group("count")) != count):
            raise ValueError("Manifest part names are invalid.")
        digest = str(entry.get("sha256") or "").strip().lower()
        if not _valid_digest(digest):
            raise ValueError("Manifest part checksum is invalid.")
        seen.add(index)
        cleaned.append({"index": index, "name": name, "bytes": size, "sha256": digest})
    cleaned.sort(key=lambda item: item["index"])
    full = str(data.get("full_sha256") or "").strip().lower()
    if not _valid_digest(full):
        raise ValueError("Manifest full-file checksum is invalid.")
    if sum(item["bytes"] for item in cleaned) != total:
        raise ValueError("Manifest part sizes do not add up to the file size.")
    return {
        "format": MANIFEST_FORMAT,
        "original_name": original,
        "total_bytes": total,
        "part_count": count,
        "full_sha256": full,
        "parts": cleaned,
    }


def save_manifest(upload_dir, manifest):
    upload_dir = Path(upload_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)
    (upload_dir / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def load_manifest(upload_dir):
    path = Path(upload_dir) / MANIFEST_NAME
    return parse_manifest(path.read_bytes())


def save_part(upload_dir, filename, content, manifest):
    """Verify one uploaded part and write it to ``upload_dir``."""
    upload_dir = Path(upload_dir)
    name = safe_name(filename)
    match = PART_RE.match(name)
    if not match:
        raise ValueError("Choose one of the .part files created by split_for_upload.py.")
    index = int(match.group("index"))
    count = int(match.group("count"))
    original = str(match.group("name"))
    if count != int(manifest["part_count"]) or original != manifest["original_name"]:
        raise ValueError("This part belongs to a different file. Upload its manifest.json first.")
    if not 1 <= index <= count:
        raise ValueError("Part number is out of range.")
    entry = manifest["parts"][index - 1]
    if entry["name"] != name:
        raise ValueError("The part name does not match the manifest.")
    if len(content) != int(entry["bytes"]):
        raise ValueError(
            f"Part {index} is {len(content)} bytes; expected {entry['bytes']}. Upload it again."
        )
    if _sha256(content) != entry["sha256"]:
        raise ValueError(f"Part {index} failed the checksum. Upload it again.")
    upload_dir.mkdir(parents=True, exist_ok=True)
    (upload_dir / name).write_bytes(content)
    return {"index": index, "bytes": len(content), "name": name}


def parts_status(upload_dir, manifest):
    """Return ``(received_indices, missing_indices)`` for one upload."""
    upload_dir = Path(upload_dir)
    received = set()
    for entry in manifest["parts"]:
        path = upload_dir / entry["name"]
        try:
            if path.is_file() and path.stat().st_size == int(entry["bytes"]):
                received.add(entry["index"])
        except OSError:
            continue
    missing = [entry["index"] for entry in manifest["parts"] if entry["index"] not in received]
    return received, missing


def assemble(upload_dir, manifest, destination):
    """Join the verified parts into ``destination`` and return it.

    The full-file checksum is checked before any part is deleted, so a bad
    upload does not force every part to be sent again.  Parts are then deleted
    as they are copied, so peak temporary disk use is the assembled file plus
    one part, not twice the file size.
    """
    upload_dir = Path(upload_dir)
    destination = Path(destination)
    received, missing = parts_status(upload_dir, manifest)
    if missing:
        raise ValueError("Missing part(s): " + ", ".join(f"{index:03d}" for index in missing) + ".")
    total = int(manifest["total_bytes"])
    if destination.exists():
        if destination.stat().st_size == total:
            for entry in manifest["parts"]:
                (upload_dir / entry["name"]).unlink(missing_ok=True)
            return destination
        raise ValueError(f"{destination.name} already exists with a different size; remove it first.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(destination.parent).free
    peak = total + max(int(entry["bytes"]) for entry in manifest["parts"]) + 16 * 1024 ** 2
    if free < peak:
        raise ValueError(
            f"Not enough free disk space on the server: {free / 1e9:.2f} GB free, "
            f"about {peak / 1e9:.2f} GB needed while assembling."
        )

    # Pass 1: verify the whole file before removing any part.
    digest = hashlib.sha256()
    checked = 0
    for entry in manifest["parts"]:
        with (upload_dir / entry["name"]).open("rb") as source:
            for block in iter(lambda: source.read(CHUNK_BYTES), b""):
                digest.update(block)
                checked += len(block)
    if checked != total:
        raise ValueError(f"Parts hold {checked} bytes; expected {total}.")
    if digest.hexdigest() != manifest["full_sha256"]:
        raise ValueError("The parts failed the full-file checksum; split and upload the file again.")

    # Pass 2: join and delete each part as it is copied.
    temporary = destination.with_name(destination.name + ".assembling")
    written = 0
    try:
        with temporary.open("wb") as output:
            for entry in manifest["parts"]:
                part = upload_dir / entry["name"]
                with part.open("rb") as source:
                    for block in iter(lambda: source.read(CHUNK_BYTES), b""):
                        output.write(block)
                        written += len(block)
                part.unlink(missing_ok=True)
        if written != total:
            raise ValueError(f"Assembled {written} bytes; expected {total}.")
        temporary.replace(destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return destination


def validate_netcdf(path):
    """Open the assembled file and confirm it has the expected coordinates."""
    from netCDF4 import Dataset

    try:
        with Dataset(str(path), mode="r") as dataset:
            if "latitude" not in dataset.variables or "longitude" not in dataset.variables:
                raise ValueError("The file has no latitude/longitude coordinates.")
            fields = [name for name, variable in dataset.variables.items()
                      if len(variable.dimensions) == 2
                      and set(variable.dimensions) == {"latitude", "longitude"}]
            if not fields:
                raise ValueError("The file has no two-dimensional latitude/longitude fields.")
    except OSError as exc:
        raise ValueError(f"The file is not a readable NetCDF: {exc}") from exc


def split_file(source, part_mb=100, out_dir=None):
    """Split ``source`` into parts plus a manifest and return the folder."""
    source = Path(source).expanduser()
    if not source.is_file():
        raise ValueError(f"Not a file: {source}")
    if source.suffix.lower() not in {".nc", ".nc4", ".cdf"}:
        raise ValueError("Choose a Himawari NetCDF file (.nc, .nc4 or .cdf).")
    try:
        part_mb = int(part_mb)
    except (TypeError, ValueError):
        raise ValueError("Part size must be a whole number of megabytes.") from None
    if part_mb < MIN_PART_MB:
        raise ValueError(f"Part size must be at least {MIN_PART_MB} MB.")
    total = source.stat().st_size
    if total <= 0:
        raise ValueError("The file is empty.")
    if total > MAX_TOTAL_BYTES:
        raise ValueError("The file is larger than the 4 GB safety limit.")
    part_bytes = part_mb * 1024 * 1024
    count = (total + part_bytes - 1) // part_bytes
    if count > MAX_PARTS:
        raise ValueError(f"Would create {count} parts; use a larger part size.")
    target_dir = Path(out_dir) if out_dir else source.parent / f"{source.name}_parts"
    target_dir.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(target_dir).free
    if free < total + 100 * 1024 ** 2:
        raise ValueError(
            f"Not enough free disk space to create the parts: {free / 1e9:.2f} GB free, "
            f"about {total / 1e9:.2f} GB plus margin needed."
        )
    for old in target_dir.glob(f"{source.name}.*-of-*.part"):
        old.unlink()
    (target_dir / MANIFEST_NAME).unlink(missing_ok=True)

    parts = []
    full_digest = hashlib.sha256()
    with source.open("rb") as handle:
        for index in range(1, count + 1):
            name = f"{source.name}.{index:03d}-of-{count:03d}.part"
            part_path = target_dir / name
            digest = hashlib.sha256()
            written = 0
            with part_path.open("wb") as output:
                while written < part_bytes:
                    block = handle.read(min(CHUNK_BYTES, part_bytes - written))
                    if not block:
                        break
                    output.write(block)
                    digest.update(block)
                    full_digest.update(block)
                    written += len(block)
            if written == 0:
                part_path.unlink(missing_ok=True)
                break
            parts.append({"index": index, "name": name, "bytes": written,
                          "sha256": digest.hexdigest()})
    if not parts or sum(part["bytes"] for part in parts) != total:
        raise ValueError("The split did not cover the whole file; check free disk space and try again.")
    manifest = {
        "format": MANIFEST_FORMAT,
        "original_name": source.name,
        "total_bytes": total,
        "part_mb": part_mb,
        "part_count": len(parts),
        "full_sha256": full_digest.hexdigest(),
        "parts": parts,
    }
    (target_dir / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return target_dir
