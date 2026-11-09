"""Download and crop JAXA P-Tree Himawari NetCDFs for a requested region."""
from __future__ import annotations

from datetime import datetime, timedelta
from ftplib import FTP
from pathlib import Path
import os
import re

import numpy as np
from netCDF4 import Dataset

HOST = "ftp.ptree.jaxa.jp"
REMOTE_ROOT = "/jma/netcdf"
PRODUCT_SUFFIX = "_R21_FLDK.07001_06001.nc"


def download_himawari(username, password, start, end, corners=None, output_dir=".", interval_minutes=10,
                      progress_callback=None, crop=True):
    """Fetch full-disk timestamps from JAXA and optionally crop an ROI copy.

    ``start`` and ``end`` are UTC ``YYYYMMDDHHMM`` strings. The full-disk
    NetCDF is always written to ``output_dir`` and kept; when ``crop`` is true,
    four (lat, lon) corners also define an axis-aligned ROI copy in WGS84.
    """
    if not username or not password:
        raise ValueError("Enter your JAXA P-Tree username and password.")
    if not re.fullmatch(r"\d{12}", start or "") or not re.fullmatch(r"\d{12}", end or ""):
        raise ValueError("Start and end must be UTC date/time in YYYYMMDDHHMM format.")
    start_dt = datetime.strptime(start, "%Y%m%d%H%M")
    end_dt = datetime.strptime(end, "%Y%m%d%H%M")
    if end_dt < start_dt:
        raise ValueError("End time must be at or after start time.")
    if interval_minutes not in (10, 20, 30, 60):
        raise ValueError("Download interval must be 10, 20, 30, or 60 minutes.")
    if crop:
        if corners is None or len(corners) != 4:
            raise ValueError("Enter exactly four area corners.")
        points = np.asarray(corners, dtype=float)
        if points.shape != (4, 2) or not np.isfinite(points).all():
            raise ValueError("Each corner must have valid latitude and longitude.")
        if np.any((points[:, 0] < -90) | (points[:, 0] > 90)) or np.any((points[:, 1] < -180) | (points[:, 1] > 180)):
            raise ValueError("Latitude must be -90..90 and longitude -180..180.")
        south, north = float(points[:, 0].min()), float(points[:, 0].max())
        west, east = float(points[:, 1].min()), float(points[:, 1].max())
        if south == north or west == east:
            raise ValueError("The four corners must span a non-zero latitude/longitude area.")
    else:
        south = north = west = east = None
    if (end_dt - start_dt).total_seconds() / 60 > 24 * 60:
        raise ValueError("A single download request is limited to 24 hours.")

    output = Path(output_dir).expanduser()
    try:
        output.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ValueError(
            f"Output folder is not usable: {exc}. Choose a folder you can write to, "
            "for example your home folder or Downloads."
        ) from exc
    if not output.is_dir():
        raise ValueError(f"Output path is not a folder: {output}")
    if not os.access(output, os.W_OK):
        raise ValueError(f"Output folder is not writable: {output}")
    output = output.resolve()
    stamps = []
    current = start_dt
    while current <= end_dt:
        stamps.append(current)
        current += timedelta(minutes=interval_minutes)
    full_disk_downloaded, cropped_downloaded, existing, missing, failed = [], [], [], [], []

    ftp = FTP()
    try:
        ftp.connect(HOST, 21, timeout=180)
        ftp.login(username, password)
        ftp.set_pasv(True)
        for stamp_index, stamp in enumerate(stamps):
            if progress_callback:
                progress_callback({"completed": stamp_index, "total": len(stamps), "percent": round(100 * stamp_index / len(stamps)),
                                   "current_time": stamp.strftime("%Y-%m-%d %H:%M UTC"), "stage": "Checking JAXA listing"})
            remote_dir = f"{REMOTE_ROOT}/{stamp:%Y%m}/{stamp:%d}"
            try:
                ftp.cwd(remote_dir)
                entries = ftp.nlst()
            except Exception as exc:
                missing.append({"time_utc": stamp.strftime("%Y-%m-%d %H:%M"), "reason": f"Cannot list JAXA directory: {exc}"})
                continue
            target = stamp.strftime("%Y%m%d_%H%M")
            matches = sorted({Path(name.rstrip("/")).name for name in entries
                              if name.rstrip("/").endswith(PRODUCT_SUFFIX) and target in Path(name.rstrip("/")).name})
            if not matches:
                missing.append({"time_utc": stamp.strftime("%Y-%m-%d %H:%M"), "reason": "Product not listed by JAXA"})
                continue
            for remote_name in matches:
                full_path = output / remote_name
                final_name = _roi_name(remote_name, south, west, north, east) if crop else None
                final_path = output / final_name if crop else None
                full_part = output / (remote_name + ".part")
                roi_part = output / (final_name + ".part") if crop else None
                try:
                    if not full_path.exists() or full_path.stat().st_size == 0:
                        # Keep an interrupted transfer as a .part file; only
                        # rename it to the final full-disk name after success.
                        if full_part.exists():
                            full_part.unlink()
                        ftp.cwd(remote_dir)
                        ftp.voidcmd("TYPE I")
                        if progress_callback:
                            progress_callback({"completed": stamp_index, "total": len(stamps), "percent": round(100 * stamp_index / len(stamps)),
                                               "current_time": stamp.strftime("%Y-%m-%d %H:%M UTC"), "stage": f"Downloading full-disk {remote_name}"})
                        with full_part.open("wb") as dest:
                            ftp.retrbinary(f"RETR {remote_name}", dest.write, blocksize=1024 * 1024)
                        # Validate NetCDF before retaining it as a completed file.
                        with Dataset(full_part, "r") as check:
                            if "latitude" not in check.variables or "longitude" not in check.variables:
                                raise ValueError("Downloaded full-disk file is missing latitude/longitude coordinates")
                        full_part.replace(full_path)
                        full_disk_downloaded.append(remote_name)
                    if crop:
                        if final_path.exists() and final_path.stat().st_size > 0:
                            existing.append(final_name)
                        else:
                            if progress_callback:
                                progress_callback({"completed": stamp_index, "total": len(stamps), "percent": round(100 * stamp_index / len(stamps)),
                                                   "current_time": stamp.strftime("%Y-%m-%d %H:%M UTC"), "stage": f"Cropping {remote_name} to selected area"})
                            crop_netcdf(full_path, roi_part, south, north, west, east)
                            roi_part.replace(final_path)
                            cropped_downloaded.append(final_name)
                except Exception as exc:
                    failed.append({"file": remote_name, "reason": str(exc)})
                    if full_part.exists():
                        full_part.unlink()
                    if roi_part is not None and roi_part.exists():
                        roi_part.unlink()
        if progress_callback:
            progress_callback({"completed": len(stamps), "total": len(stamps), "percent": 100,
                               "current_time": "", "stage": "Download complete"})
    finally:
        try:
            ftp.quit()
        except Exception:
            ftp.close()

    return {"downloaded": cropped_downloaded, "full_disk_downloaded": full_disk_downloaded,
            "cropped_downloaded": cropped_downloaded, "already_existing": existing, "not_found": missing,
            "full_disk_files": [p.name for p in output.glob(f"*{PRODUCT_SUFFIX}") if p.is_file()],
            "failed": failed, "output_dir": str(output),
            "region": {"south": south, "north": north, "west": west, "east": east} if crop else None,
            "start_utc": start_dt.strftime("%Y-%m-%d %H:%M"), "end_utc": end_dt.strftime("%Y-%m-%d %H:%M"),
            "interval_minutes": interval_minutes, "timestamps_requested": len(stamps),
            "variables": ["albedo_03", "tbb_07", "tbb_13"],
            "message": "Credentials were used only for this request and were not saved."}


ROI_PATTERN = re.compile(r"_ROI_(-?\d+\.\d+)_(-?\d+\.\d+)_(-?\d+\.\d+)_(-?\d+\.\d+)\.nc$")


def _roi_name(full_name, south, west, north, east):
    """ROI file name that records the crop bounds."""
    return f"{full_name[:-3]}_ROI_{south:.4f}_{west:.4f}_{north:.4f}_{east:.4f}.nc"


def _parse_roi_bounds(name):
    match = ROI_PATTERN.search(name)
    if not match:
        return None
    south, west, north, east = (float(value) for value in match.groups())
    return {"south": south, "west": west, "north": north, "east": east}


def list_local_products(output_dir):
    """Inventory full-disk and ROI NetCDF files already stored on this machine."""
    root = Path(output_dir).expanduser().resolve()
    full_disk, roi = [], []
    if root.is_dir():
        for path in sorted(root.glob("*.nc")):
            if not path.is_file():
                continue
            stat = path.stat()
            item = {"name": path.name, "size_bytes": stat.st_size,
                    "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")}
            if path.name.endswith(PRODUCT_SUFFIX):
                full_disk.append(item)
            elif "_ROI_" in path.name:
                item["bounds"] = _parse_roi_bounds(path.name)
                roi.append(item)
    return {
        "root": str(root),
        "exists": root.is_dir(),
        "full_disk": full_disk,
        "roi": roi,
        "total_bytes": sum(item["size_bytes"] for item in full_disk + roi),
    }


def crop_saved_products(output_dir, filenames, south, north, west, east):
    """Crop already-downloaded full-disk files without contacting JAXA again.

    Each result reports ``status`` as ``cropped``, ``already_exists``,
    ``missing``, ``not_a_full_disk`` or ``failed``.
    """
    if not (isinstance(south, (int, float)) and isinstance(north, (int, float))
            and isinstance(west, (int, float)) and isinstance(east, (int, float))):
        raise ValueError("Enter numeric latitude/longitude bounds.")
    if not (-90 <= south < north <= 90) or not (-180 <= west < east <= 180):
        raise ValueError("Bounds must satisfy -90 <= south < north <= 90 and -180 <= west < east <= 180.")
    root = Path(output_dir).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"Folder does not exist: {root}")
    results = []
    for filename in filenames:
        name = Path(str(filename)).name
        if name != str(filename):
            results.append({"file": str(filename), "status": "failed", "reason": "Refused a path outside the output folder."})
            continue
        source = root / name
        if not source.is_file():
            results.append({"file": name, "status": "missing"})
            continue
        if not name.endswith(PRODUCT_SUFFIX):
            results.append({"file": name, "status": "not_a_full_disk"})
            continue
        destination = root / _roi_name(name, south, west, north, east)
        if destination.exists() and destination.stat().st_size > 0:
            results.append({"file": name, "status": "already_exists", "output": destination.name})
            continue
        part = root / (destination.name + ".part")
        try:
            crop_netcdf(source, part, south, north, west, east)
            part.replace(destination)
            results.append({"file": name, "status": "cropped", "output": destination.name,
                            "size_bytes": destination.stat().st_size})
        except Exception as exc:
            if part.exists():
                part.unlink()
            results.append({"file": name, "status": "failed", "reason": str(exc)})
    return results


def crop_netcdf(source, destination, south, north, west, east):
    """Write a cropped NetCDF while preserving its variables and attributes."""
    with Dataset(source, "r") as src:
        if "latitude" not in src.variables or "longitude" not in src.variables:
            raise ValueError("Downloaded NetCDF has no latitude/longitude coordinate variables.")
        lat = np.asarray(src.variables["latitude"][:], dtype=float)
        lon = np.asarray(src.variables["longitude"][:], dtype=float)
        yi = np.flatnonzero((lat >= south) & (lat <= north))
        xi = np.flatnonzero((lon >= west) & (lon <= east))
        if not yi.size or not xi.size:
            raise ValueError("Requested four-corner area is outside the downloaded product coverage.")
        y0, y1 = int(yi.min()), int(yi.max()) + 1
        x0, x1 = int(xi.min()), int(xi.max()) + 1
        selected_dims = {"latitude": y1-y0, "longitude": x1-x0}
        with Dataset(destination, "w", format="NETCDF4") as dst:
            for dim_name, dim in src.dimensions.items():
                dst.createDimension(dim_name, selected_dims.get(dim_name, len(dim)))
            for attr in src.ncattrs():
                dst.setncattr(attr, src.getncattr(attr))
            dst.setncattr("roi_crop_bounds_wgs84", f"south={south},north={north},west={west},east={east}")
            for name, var in src.variables.items():
                options = {}
                if var.ndim and getattr(var, "filters", None):
                    try:
                        f = var.filters()
                        if f.get("zlib"):
                            options.update(zlib=True, complevel=int(f.get("complevel", 4)))
                    except Exception:
                        pass
                fill = getattr(var, "_FillValue", None)
                if fill is not None:
                    options["fill_value"] = fill
                out_var = dst.createVariable(name, var.datatype, var.dimensions, **options)
                var.set_auto_maskandscale(False)
                for attr in var.ncattrs():
                    if attr != "_FillValue":
                        out_var.setncattr(attr, var.getncattr(attr))
                if "latitude" in var.dimensions or "longitude" in var.dimensions:
                    source_selection = [slice(None)] * var.ndim
                    destination_selection = [slice(None)] * var.ndim
                    if "latitude" in var.dimensions:
                        axis = var.dimensions.index("latitude")
                        source_selection[axis] = slice(y0, y1)
                        destination_selection[axis] = slice(0, y1 - y0)
                    if "longitude" in var.dimensions:
                        axis = var.dimensions.index("longitude")
                        source_selection[axis] = slice(x0, x1)
                        destination_selection[axis] = slice(0, x1 - x0)
                    out_var[tuple(destination_selection)] = var[tuple(source_selection)]
                else:
                    out_var[:] = var[:]
