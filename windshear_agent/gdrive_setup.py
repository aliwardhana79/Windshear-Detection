"""Create the Google Drive OAuth refresh token for windshear_agent.

Run this on your own computer (not on Streamlit Cloud), sign in with the Google
account that should receive the Himawari scenes, then paste the printed block
into ``.streamlit/secrets.toml`` or the Streamlit Cloud secrets editor.

Prerequisite: a *Desktop app* OAuth client ID from Google Cloud Console with the
Google Drive API enabled. Download its JSON file and pass it with
``--client-secrets``.

    python gdrive_setup.py --client-secrets client_secret_XXXX.json

The requested scope is ``drive.file``: the app can only see and manage files it
created, not the rest of your Drive.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def _client_config(path: Path):
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"Cannot read the OAuth client JSON: {exc}") from exc
    config = raw.get("installed") or raw.get("web")
    if not isinstance(config, dict):
        raise SystemExit("That JSON is not an OAuth client file. Download the "
                         "*Desktop app* client from Google Cloud Console.")
    return config


def _prompt(text, secret=False):
    if secret:
        import getpass

        return getpass.getpass(text).strip()
    return input(text).strip()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Generate a Google Drive refresh token for windshear_agent.")
    parser.add_argument("--client-secrets", type=Path,
                        help="Path to the OAuth *Desktop app* client JSON.")
    parser.add_argument("--client-id", help="OAuth client ID, if not using a JSON file.")
    parser.add_argument("--client-secret", help="OAuth client secret.")
    parser.add_argument("--port", type=int, default=0,
                        help="Local callback port (default: choose a free port). "
                             "Use a fixed port when forwarding it over SSH.")
    parser.add_argument("--no-browser", action="store_true",
                        help="Do not open a browser automatically; print the URL instead.")
    args = parser.parse_args(argv)

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        raise SystemExit(
            "google-auth-oauthlib is missing. Install it with:\n"
            "  pip install google-auth-oauthlib google-api-python-client"
        )

    if args.client_secrets:
        path = args.client_secrets.expanduser()
        if not path.is_file():
            raise SystemExit(f"File not found: {path}")
        config = _client_config(path)
        flow = InstalledAppFlow.from_client_secrets_file(str(path), scopes=SCOPES)
    else:
        client_id = (args.client_id or _prompt("OAuth client ID: ")).strip()
        client_secret = (args.client_secret or _prompt("OAuth client secret: ", True)).strip()
        if not client_id or not client_secret:
            raise SystemExit("A client ID and client secret are required.")
        config = {"client_id": client_id, "client_secret": client_secret}
        flow = InstalledAppFlow.from_client_config(
            {"installed": {
                **config,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost"],
            }},
            scopes=SCOPES,
        )

    print("Opening Google sign-in. Use the account whose Drive should receive "
          "the Himawari scenes.")
    try:
        credentials = flow.run_local_server(
            port=args.port,
            access_type="offline",
            prompt="consent",
            open_browser=not args.no_browser,
            authorization_prompt_message="Open this URL to authorise Google Drive: {url}",
        )
    except Exception as exc:
        raise SystemExit(f"Google sign-in failed: {exc}") from exc

    if not credentials.refresh_token:
        raise SystemExit(
            "Google did not return a refresh token. Remove this app's access at "
            "https://myaccount.google.com/permissions, then run the script again."
        )

    client_id = config.get("client_id", "")
    client_secret = config.get("client_secret", "")
    print("\nSuccess. Add this block to .streamlit/secrets.toml, or to the "
          "Streamlit Cloud app under Settings -> Secrets:\n")
    print("[gdrive]")
    print(f'client_id = "{client_id}"')
    print(f'client_secret = "{client_secret}"')
    print(f'refresh_token = "{credentials.refresh_token}"')
    print("\nKeep this file private. Do not commit .streamlit/secrets.toml to Git.")
    print("The app creates/reuses a folder named Himawari_9 in My Drive.")


if __name__ == "__main__":
    main(sys.argv[1:])
