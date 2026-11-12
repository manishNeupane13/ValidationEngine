"""Read data from administrator-approved HTTPS endpoints and authenticated webhooks."""

import hashlib
import json
import os
import re
from email.message import Message
from pathlib import PurePosixPath
from typing import Literal
from urllib.parse import unquote, urlsplit, urlunsplit

import httpx
from pydantic import BaseModel, Field, model_validator

from .engine import InputError

MONDAY_URL = "https://api.monday.com/v2"

SUPPORTED = {".json", ".csv", ".xlsx", ".parquet"}


class CustomToken(BaseModel):
    location: Literal["header", "query"]
    name: str = Field(pattern=r"^[A-Za-z0-9_.~-]+$", max_length=128)
    token_env: str = Field(pattern=r"^INTEGRATION_HTTP_TOKEN_[A-Z0-9_]+$")

    @model_validator(mode="after")
    def validate_header(self):
        reserved = {
            "host",
            "connection",
            "content-length",
            "transfer-encoding",
            "upgrade",
            "te",
            "trailer",
            "accept-encoding",
            "expect",
        }
        name = self.name.lower()
        if self.location == "header" and (
            name in reserved or name.startswith("proxy-")
        ):
            raise ValueError(
                "Choose an authentication header rather than an HTTP control header."
            )
        return self


class ConnectionSettings(BaseModel):
    monday_board_id: str | None = Field(None, pattern=r"^[0-9]{1,20}$")
    url: str | None = Field(None, max_length=4000)
    records_path: str | None = Field(None, max_length=1000)
    filename: str | None = Field(None, max_length=200)
    query_params: dict[str, str | int | float | bool | list[str]] = Field(
        default_factory=dict
    )
    bearer_env: str | None = Field(None, pattern=r"^INTEGRATION_HTTP_TOKEN_[A-Z0-9_]+$")
    custom_token: CustomToken | None = None

    @model_validator(mode="after")
    def validate_auth(self):
        if self.bearer_env and self.custom_token:
            raise ValueError("Choose one authentication method.")
        if self.monday_board_id:
            if (
                self.url != MONDAY_URL
                or not self.custom_token
                or (
                    self.custom_token.location != "header"
                    or self.custom_token.name.lower() != "authorization"
                )
                or self.query_params
            ):
                raise ValueError(
                    "Use the monday.com endpoint and an Authorization token for a board connection."
                )
            self.records_path = "/data/boards/0/items_page/items"
            self.filename = f"monday-board-{self.monday_board_id}.json"
        return self


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
    if settings.query_params:
        destination = destination.copy_merge_params(settings.query_params)
    headers = {"Accept-Encoding": "identity"}
    if settings.bearer_env:
        token = os.environ.get(settings.bearer_env)
        if not token:
            raise InputError(
                "The configured bearer token environment variable is empty."
            )
        headers["Authorization"] = f"Bearer {token}"
    if auth := settings.custom_token:
        token = os.environ.get(auth.token_env)
        if not token:
            raise InputError("The configured token environment variable is empty.")
        if auth.location == "header":
            if any(not 32 <= ord(character) <= 126 for character in token):
                raise InputError(
                    "The token environment variable contains invalid header characters."
                )
            headers[auth.name] = token
        else:
            destination = destination.copy_merge_params({auth.name: token})
    limit = upload_limit()
    payload = bytearray()
    method = "GET"
    request_body = {}
    if settings.monday_board_id:
        method = "POST"
        request_body["json"] = {
            "query": "query BoardItems($board: [ID!]!) { boards(ids: $board) { items_page(limit: 500) { cursor items { id name column_values { id text } } } } }",
            "variables": {"board": [settings.monday_board_id]},
        }
    with httpx.stream(
        method,
        destination,
        headers=headers,
        timeout=20,
        follow_redirects=False,
        trust_env=False,
        **request_body,
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
    if settings.monday_board_id:
        try:
            result = json.loads(payload)
            if result.get("errors"):
                raise InputError(
                    "monday.com rejected the board query. Check board access and the configured token."
                )
            result["data"]["boards"][0]["items_page"]["items"]
        except (
            TypeError,
            KeyError,
            IndexError,
            AttributeError,
            json.JSONDecodeError,
        ) as error:
            raise InputError(
                "monday.com did not return the requested board."
            ) from error
    return bytes(payload), filename, origin, response_name or None
