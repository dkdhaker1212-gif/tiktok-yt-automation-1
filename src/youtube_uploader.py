"""YouTube Data API v3 upload + OAuth token refresh.

Scope: youtube.upload only. The refresh token must come from a consent screen
published to PRODUCTION -- in "Testing" it dies after 7 days (Automation.md S10).
"""
from __future__ import annotations

import json
import os
import time
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.force-ssl",
]


class ChannelSuspended(RuntimeError):
    """403 authenticatedUserAccountSuspended -- stop, do not retry (Automation.md S13)."""


def _load_credentials(client_secret_file: str, token_file: str) -> Credentials:
    if not os.path.isfile(token_file):
        raise FileNotFoundError(
            f"{token_file} missing -- mint it with reauth_nobrowser.py"
        )
    with open(token_file, "r", encoding="utf-8") as fh:
        data = json.load(fh)

    with open(client_secret_file, "r", encoding="utf-8") as fh:
        secret = json.load(fh)
    inst = secret.get("installed") or secret.get("web") or {}

    creds = Credentials(
        token=data.get("token"),
        refresh_token=data.get("refresh_token"),
        token_uri=data.get("token_uri", inst.get("token_uri",
                            "https://oauth2.googleapis.com/token")),
        client_id=data.get("client_id", inst.get("client_id")),
        client_secret=data.get("client_secret", inst.get("client_secret")),
        scopes=data.get("scopes", SCOPES),
    )
    if not creds.refresh_token:
        raise RuntimeError(f"{token_file} has no refresh_token -- re-mint it")

    if not creds.valid:
        creds.refresh(Request())
        # persist the rotated access token
        with open(token_file, "w", encoding="utf-8") as fh:
            json.dump({
                "token": creds.token,
                "refresh_token": creds.refresh_token,
                "token_uri": creds.token_uri,
                "client_id": creds.client_id,
                "client_secret": creds.client_secret,
                "scopes": creds.scopes,
            }, fh, indent=2)
    return creds


def upload(
    *,
    file_path: str,
    title: str,
    description: str,
    category_id: str,
    tags: list[str],
    is_short: bool,
    client_secret_file: str,
    token_file: str,
    dry_run: bool = False,
) -> Optional[str]:
    """Upload one video as public. Returns the YouTube video id (None on dry run)."""
    title = (title or "Untitled").strip()[:100]
    tags = list(dict.fromkeys([*(tags or []), *(["Shorts"] if is_short else [])]))
    body = {
        "snippet": {
            "title": title,
            "description": description.strip()[:4900],
            "categoryId": str(category_id),
            "tags": tags[:60],
        },
        "status": {
            "privacyStatus": "public",
            "selfDeclaredMadeForKids": False,
        },
    }

    if dry_run:
        print(f"[upload] DRY RUN -- would upload {file_path!r}")
        print(f"         title      : {title}")
        print(f"         categoryId : {category_id}")
        print(f"         tags       : {tags[:60]}")
        print(f"         short      : {is_short}")
        return None

    creds = _load_credentials(client_secret_file, token_file)
    youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)

    media = MediaFileUpload(file_path, chunksize=-1, resumable=True, mimetype="video/*")
    request = youtube.videos().insert(
        part="snippet,status", body=body, media_body=media
    )

    response = None
    tries = 0
    while response is None:
        try:
            _status, response = request.next_chunk()
        except HttpError as exc:
            msg = str(exc)
            if "authenticatedUserAccountSuspended" in msg:
                raise ChannelSuspended(msg) from exc
            tries += 1
            if exc.resp.status in (500, 502, 503, 504) and tries <= 5:
                time.sleep(2 ** tries)
                continue
            raise
    vid = response["id"]
    print(f"[upload] done -> https://www.youtube.com/watch?v={vid}")
    return vid


def set_thumbnail(*, video_id: str, thumb_path: str,
                  client_secret_file: str, token_file: str) -> bool:
    """Best-effort custom thumbnail. Needs scope youtube.force-ssl / youtube --
    NOT youtube.upload. Returns False (and logs) if the scope is missing or the
    channel lacks thumbnail privileges; never raises."""
    if not (thumb_path and os.path.isfile(thumb_path)):
        return False
    try:
        creds = _load_credentials(client_secret_file, token_file)
        youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)
        media = MediaFileUpload(thumb_path, mimetype="image/jpeg")
        youtube.thumbnails().set(videoId=video_id, media_body=media).execute()
        print(f"[thumbnail] set on {video_id}")
        return True
    except HttpError as exc:
        msg = str(exc)
        if "insufficient" in msg.lower() or "scope" in msg.lower() or exc.resp.status == 403:
            print("[thumbnail] skipped: token lacks youtube.force-ssl scope "
                  "(re-mint with the broader scope to enable thumbnails)")
        else:
            print(f"[thumbnail] API error: {msg[:200]}")
        return False
    except Exception as exc:  # noqa: BLE001
        print(f"[thumbnail] failed: {exc}")
        return False
