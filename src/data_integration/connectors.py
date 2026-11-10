"""Read data from administrator-approved HTTPS endpoints and authenticated webhooks."""

import hashlib
import os
import re
from email.message import Message
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit, urlunsplit

import httpx
from pydantic import BaseModel, Field

from .engine import InputError

SUPPORTED = {".json", ".csv", ".xlsx", ".parquet"}


class ConnectionSettings(BaseModel):
    url: str | None = Field(None, max_length=4000)
    records_path: str | None = Field(None, max_length=1000)
    filename: str | None = Field(None, max_length=200)
    bearer_env: str | None = Field(None, pattern=r"^INTEGRATION_HTTP_TOKEN_[A-Z0-9_]+$")


def upload_limit():
    return int(os.environ.get("INTEGRATION_MAX_UPLOAD_MB", "50")) * 1024 * 1024


def original_filename(name):
    return PurePosixPath((name or "").replace("\\", "/")).name


def approved_url(url):
    if not url:
        raise InputError("Enter an HTTPS endpoint URL.")
    try:
        parsed = urlsplit(url)
        allowed = {
            host.strip().lower()
            for host in os.environ.get("INTEGRATION_HTTP_ALLOWED_HOSTS", "").split(",")
            if host.strip()
        }
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in {None, 443}
            or parsed.fragment
            or re.search(r"[\x00-\x20\x7f\\]", url)
        ):
            raise InputError(
                "Use an HTTPS URL on port 443 with no embedded credentials or fragment."
            )
        destination = httpx.URL(url)
        if (
            destination.host.lower() != parsed.hostname.lower()
            or destination.host.lower() not in allowed
        ):
            raise InputError(
                "This API host is not configured in INTEGRATION_HTTP_ALLOWED_HOSTS."
            )
    except (ValueError, httpx.InvalidURL) as error:
        if isinstance(error, InputError):
            raise
        raise InputError("Invalid endpoint URL.") from error
    return destination, urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def token_digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def get_payload(settings):
    destination, origin = approved_url(settings.url)
    headers = {"Accept-Encoding": "identity"}
    if settings.bearer_env:
        token = os.environ.get(settings.bearer_env)
        if not token:
            raise InputError(
                "The configured bearer token environment variable is empty."
            )
        headers["Authorization"] = f"Bearer {token}"
    limit = upload_limit()
    payload = bytearray()
    with httpx.stream(
        "GET",
        destination,
        headers=headers,
        timeout=20,
        follow_redirects=False,
        trust_env=False,
    ) as response:
        response.raise_for_status()
        disposition = Message()
        disposition["Content-Disposition"] = response.headers.get(
            "content-disposition", ""
        )
        response_name = original_filename(disposition.get_filename())
        url_name = original_filename(unquote(destination.path))
        candidates = [
            name
            for name in (response_name, url_name)
            if PurePosixPath(name).suffix.lower() in SUPPORTED
        ]
        filename = settings.filename or next(iter(candidates), "response.json")
        for chunk in response.iter_bytes(chunk_size=64 * 1024):
            if len(payload) + len(chunk) > limit:
                raise InputError("The response exceeds the configured upload limit.")
            payload.extend(chunk)
    return bytes(payload), filename, origin, response_name or None
