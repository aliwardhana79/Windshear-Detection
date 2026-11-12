"""Upload Himawari scenes to Google Drive.

Two authentication modes are supported, configured in ``[gdrive]`` inside
``.streamlit/secrets.toml`` or the Streamlit Cloud secrets editor:

OAuth refresh token (recommended)
    client_id, client_secret, refresh_token
    The app creates/reuses a folder named ``Himawari_9`` in My Drive and uses
    the non-sensitive ``drive.file`` scope, so it can only manage files it
    created itself.

Service account (shared folder)
    service_account_json (or service_account_file), folder_id
    The service account needs Editor access to that folder. Use this for a
    Google Workspace Shared Drive; personal My Drive may return a storage
    quota error for service accounts.

Google client libraries are imported lazily so the rest of the app works when
Google Drive is not configured.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

OAUTH_SCOPES = ("https://www.googleapis.com/auth/drive.file",)
SERVICE_SCOPES = ("https://www.googleapis.com/auth/drive",)
FOLDER_NAME = "Himawari_9"
FOLDER_MIME = "application/vnd.google-apps.folder"
CHUNK_SIZE = 8 * 1024 * 1024


class DriveError(RuntimeError):
    """Google Drive is not configured, or an upload/connection failed."""


def _secret_section():
    try:
        import streamlit as st

        section = st.secrets.get("gdrive", {})
        if hasattr(section, "to_dict"):
            section = section.to_dict()
        return dict(section)
    except Exception:
        return {}


def _text(section, key, env_name):
    value = section.get(key)
    if value is None:
        value = os.environ.get(env_name, "")
    return str(value or "").strip()


def load_settings():
    """Read Google Drive settings from Streamlit secrets or environment."""
    section = _secret_section()
    settings = {
        "client_id": _text(section, "client_id", "GDRIVE_CLIENT_ID"),
        "client_secret": _text(section, "client_secret", "GDRIVE_CLIENT_SECRET"),
        "refresh_token": _text(section, "refresh_token", "GDRIVE_REFRESH_TOKEN"),
        "folder_id": _text(section, "folder_id", "GDRIVE_FOLDER_ID"),
        "service_account_file": _text(section, "service_account_file", "GDRIVE_SERVICE_ACCOUNT_FILE"),
        "service_account_info": None,
        "service_account_error": None,
    }
    raw_info = (
        section.get("service_account")
        or section.get("service_account_json")
        or os.environ.get("GDRIVE_SERVICE_ACCOUNT_JSON")
    )
    if raw_info:
        if isinstance(raw_info, dict):
            settings["service_account_info"] = dict(raw_info)
        else:
            raw_text = str(raw_info).strip()
            if raw_text.startswith("{"):
                try:
                    settings["service_account_info"] = json.loads(raw_text)
                except ValueError as exc:
                    settings["service_account_error"] = str(exc)
            elif not settings["service_account_file"]:
                # A non-JSON string is treated as a path to the key file.
                settings["service_account_file"] = raw_text
    return settings


def _oauth_ready(settings):
    return all(settings.get(key) for key in ("client_id", "client_secret", "refresh_token"))


def _service_account_info(settings):
    info = settings.get("service_account_info")
    if isinstance(info, dict):
        return info
    if info:
        return json.loads(str(info))
    path = settings.get("service_account_file")
    if path:
        return json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    raise DriveError("No Google service-account credentials were found.")


def configuration_error(settings=None):
    """Return a human-readable problem, or ``None`` when Drive is configured."""
    settings = settings or load_settings()
    if _oauth_ready(settings):
        return None
    if settings.get("service_account_error"):
        return f"Google service-account JSON is invalid: {settings['service_account_error']}"
    has_service_account = bool(
        settings.get("service_account_info") or settings.get("service_account_file")
    )
    if has_service_account:
        if not settings.get("folder_id"):
            return (
                "Service-account mode needs folder_id: the ID of the Drive folder "
                "shared with the service account."
            )
        try:
            _service_account_info(settings)
        except Exception as exc:
            return f"Google service-account credentials could not be read: {exc}"
        return None
    return (
        "Google Drive is not configured. Add [gdrive] client_id, client_secret and "
        "refresh_token to .streamlit/secrets.toml, or service_account_json and folder_id."
    )


def destination_label(settings=None):
    """Short description of where uploads will go."""
    settings = settings or load_settings()
    if _oauth_ready(settings):
        return f"My Drive \u2192 {FOLDER_NAME}"
    if settings.get("folder_id"):
        return f"Google Drive folder {settings['folder_id']}"
    return "Google Drive"


def _credentials(settings):
    try:
        if _oauth_ready(settings):
            from google.oauth2.credentials import Credentials

            return Credentials(
                token=None,
                refresh_token=settings["refresh_token"],
                token_uri="https://oauth2.googleapis.com/token",
                client_id=settings["client_id"],
                client_secret=settings["client_secret"],
                scopes=OAUTH_SCOPES,
            )
        from google.oauth2 import service_account

        return service_account.Credentials.from_service_account_info(
            _service_account_info(settings), scopes=SERVICE_SCOPES
        )
    except ImportError as exc:
        raise DriveError(
            "Google Drive libraries are missing. Install google-api-python-client and google-auth."
        ) from exc


def _service(settings):
    try:
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise DriveError(
            "Google Drive libraries are missing. Install google-api-python-client and google-auth."
        ) from exc
    return build("drive", "v3", credentials=_credentials(settings), cache_discovery=False)


def _escape_query(value):
    return str(value).replace("\\", "\\\\").replace("'", "\\'")


def _http_error_message(exc):
    try:
        payload = json.loads(exc.content.decode("utf-8", "replace"))
        error = payload.get("error", {})
        message = error.get("message") or str(exc)
        reasons = {item.get("reason") for item in error.get("errors", [])}
        if "storageQuotaExceeded" in reasons:
            return (
                "Google Drive storage quota exceeded. Use OAuth mode with your own "
                "Drive, or a Google Workspace Shared Drive. "
                f"Google said: {message}"
            )
        return f"Google Drive rejected the upload: {message}"
    except Exception:
        return f"Google Drive rejected the upload: {exc}"


def _folder_metadata(drive, folder_id):
    try:
        metadata = (
            drive.files()
            .get(
                fileId=folder_id,
                fields="id,name,mimeType,webViewLink",
                supportsAllDrives=True,
            )
            .execute()
        )
    except Exception as exc:
        raise DriveError(
            f"Cannot open the Google Drive folder {folder_id}. Check the folder ID "
            f"and that the account can edit it. {exc}"
        ) from exc
    if metadata.get("mimeType") not in (None, FOLDER_MIME):
        raise DriveError(f"Google Drive ID {folder_id} is not a folder.")
    return metadata


def _ensure_folder(drive, settings):
    if _oauth_ready(settings):
        query = (
            f"name = '{_escape_query(FOLDER_NAME)}' "
            f"and mimeType = '{FOLDER_MIME}' and trashed = false"
        )
        found = (
            drive.files()
            .list(q=query, spaces="drive", fields="files(id,name,webViewLink)", pageSize=10)
            .execute()
            .get("files", [])
        )
        if found:
            return found[0]
        return (
            drive.files()
            .create(body={"name": FOLDER_NAME, "mimeType": FOLDER_MIME},
                    fields="id,name,webViewLink")
            .execute()
        )
    folder_id = settings.get("folder_id")
    if not folder_id:
        raise DriveError("Service-account mode needs a Google Drive folder_id.")
    return _folder_metadata(drive, folder_id)


def upload_file(path, drive, folder_id, progress=None, shared_drive=False):
    """Upload or update one file in Drive.

    ``progress`` receives a fraction between 0 and 1. An existing file with the
    same name and byte size is skipped; a different size updates the file in
    place instead of creating a duplicate.
    """
    path = Path(path)
    if not path.is_file():
        raise DriveError(f"File not found: {path}")
    size = path.stat().st_size

    query = f"name = '{_escape_query(path.name)}' and trashed = false"
    if folder_id:
        query += f" and '{_escape_query(folder_id)}' in parents"
    list_args = {
        "q": query,
        "spaces": "drive",
        "fields": "files(id,name,size,webViewLink)",
        "pageSize": 10,
    }
    if shared_drive:
        list_args.update(supportsAllDrives=True, includeItemsFromAllDrives=True)
    try:
        found = drive.files().list(**list_args).execute().get("files", [])
    except Exception as exc:
        raise DriveError(f"Could not check Google Drive for an existing file: {exc}") from exc

    existing = found[0] if found else None
    if existing and str(existing.get("size")) == str(size):
        return {"status": "skipped", **existing}

    try:
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaFileUpload

        media = MediaFileUpload(
            str(path),
            mimetype="application/octet-stream",
            resumable=True,
            chunksize=CHUNK_SIZE,
        )
        fields = "id,name,size,webViewLink"
        if existing:
            request = drive.files().update(
                fileId=existing["id"],
                media_body=media,
                fields=fields,
                supportsAllDrives=True,
            )
        else:
            body = {"name": path.name}
            if folder_id:
                body["parents"] = [folder_id]
            request = drive.files().create(
                body=body,
                media_body=media,
                fields=fields,
                supportsAllDrives=True,
            )
        response = None
        while response is None:
            status, response = request.next_chunk()
            if status is not None and progress is not None:
                progress(float(status.progress()))
    except ImportError as exc:
        raise DriveError(
            "Google Drive libraries are missing. Install google-api-python-client and google-auth."
        ) from exc
    except HttpError as exc:
        raise DriveError(_http_error_message(exc)) from exc
    except Exception as exc:
        raise DriveError(f"Google Drive upload failed: {exc}") from exc

    return {"status": "updated" if existing else "uploaded", **(response or {})}


def upload_files(paths, settings=None, drive=None, progress=None):
    """Upload several local files and return a per-file report."""
    settings = settings or load_settings()
    problem = configuration_error(settings)
    if problem:
        raise DriveError(problem)
    paths = [Path(path) for path in paths]
    report = {"uploaded": [], "skipped": [], "failed": [], "destination": {}}
    if not paths:
        return report

    drive = drive or _service(settings)
    destination = _ensure_folder(drive, settings)
    folder_id = destination.get("id")
    shared_drive = not _oauth_ready(settings)
    total = len(paths)

    for index, path in enumerate(paths, start=1):
        try:
            if not path.is_file():
                raise DriveError(f"File not found: {path}")

            def item_progress(fraction, name=path.name, index=index):
                if progress is not None:
                    progress({
                        "file_index": index,
                        "file_total": total,
                        "file_name": name,
                        "fraction": fraction,
                    })

            result = upload_file(
                path,
                drive=drive,
                folder_id=folder_id,
                progress=item_progress,
                shared_drive=shared_drive,
            )
            entry = {
                "name": path.name,
                "size_bytes": path.stat().st_size,
                "id": result.get("id"),
                "link": result.get("webViewLink"),
            }
            if result.get("status") == "skipped":
                report["skipped"].append(entry)
            else:
                report["uploaded"].append(entry)
        except Exception as exc:
            report["failed"].append({"name": path.name, "error": str(exc)})

    report["destination"] = destination
    return report


def test_connection(settings=None):
    """Open/create the destination folder and return its metadata."""
    settings = settings or load_settings()
    problem = configuration_error(settings)
    if problem:
        raise DriveError(problem)
    drive = _service(settings)
    return _ensure_folder(drive, settings)
