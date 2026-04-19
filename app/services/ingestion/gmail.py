import json
import logging
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

from app.config import GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REDIRECT_URI

logger = logging.getLogger(__name__)

# Scopes required for Gmail ingestion
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
]


def get_oauth_flow() -> Flow:
    client_config = {
        "web": {
            "client_id": GMAIL_CLIENT_ID,
            "client_secret": GMAIL_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }
    return Flow.from_client_config(
        client_config, scopes=SCOPES, redirect_uri=GMAIL_REDIRECT_URI
    )


def get_gmail_service(credentials_json: str) -> Any:
    creds_dict = json.loads(credentials_json)
    creds = Credentials.from_authorized_user_info(creds_dict, SCOPES)

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())

    return build("gmail", "v1", credentials=creds)


def fetch_raw_message(service: Any, message_id: str) -> bytes:
    import base64

    msg = (
        service.users()
        .messages()
        .get(userId="me", id=message_id, format="raw")
        .execute()
    )
    return base64.urlsafe_b64decode(msg["raw"])
