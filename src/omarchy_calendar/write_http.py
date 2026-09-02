# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping

from . import __version__
from .http import HttpError, MAX_RESPONSE
from .keyring import redact


MAX_WRITE_BODY = 1024 * 1024


class WriteViolation(RuntimeError):
    pass


class CalendarWriteHttp:
    def __init__(self, opener: Any | None = None, *, timeout: int = 20):
        self.opener = opener or urllib.request.build_opener()
        self.timeout = timeout

    def request_json(
        self,
        method: str,
        url: str,
        body: Mapping[str, Any] | None = None,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        method = method.upper()
        self._check_route(method, url)
        data = None
        if method != "DELETE":
            data = json.dumps(body or {}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if len(data) > MAX_WRITE_BODY:
                raise WriteViolation("Calendar write payload is too large")
        request_headers = {
            "Accept": "application/json",
            "User-Agent": f"omarchy-calendar/{__version__}",
            **({"Content-Type": "application/json"} if data is not None else {}),
            **dict(headers or {}),
        }
        request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE + 1)
                if len(raw) > MAX_RESPONSE:
                    raise HttpError(0, "Provider response exceeded the safe size limit")
                if not raw:
                    return {}
                payload = json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as error:
            raw = error.read(16_384).decode("utf-8", "replace")
            retry_after = error.headers.get("Retry-After", "") if error.headers else ""
            raise HttpError(
                error.code,
                redact(raw) or f"Provider returned HTTP {error.code}",
                retry_after,
            ) from error
        except urllib.error.URLError as error:
            raise HttpError(0, redact(str(error.reason))) from error
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise HttpError(0, "Provider returned invalid JSON") from error
        if not isinstance(payload, dict):
            raise HttpError(0, "Provider returned a non-object JSON response")
        return payload

    @staticmethod
    def _check_route(method: str, url: str) -> None:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or parsed.fragment:
            raise WriteViolation("Calendar writes require an approved HTTPS event endpoint")
        parts = [item for item in parsed.path.split("/") if item]
        if any("/" in urllib.parse.unquote(item) or "\\" in urllib.parse.unquote(item) for item in parts):
            raise WriteViolation("Calendar write endpoint contains an invalid identifier")

        google_collection = (
            parsed.netloc == "www.googleapis.com"
            and len(parts) == 5
            and parts[:3] == ["calendar", "v3", "calendars"]
            and parts[4] == "events"
        )
        google_item = google_collection is False and (
            parsed.netloc == "www.googleapis.com"
            and len(parts) == 6
            and parts[:3] == ["calendar", "v3", "calendars"]
            and parts[4] == "events"
        )
        microsoft_collection = (
            parsed.netloc == "graph.microsoft.com"
            and len(parts) == 5
            and parts[:3] == ["v1.0", "me", "calendars"]
            and parts[4] == "events"
        )
        microsoft_item = (
            parsed.netloc == "graph.microsoft.com"
            and len(parts) == 6
            and parts[:3] == ["v1.0", "me", "calendars"]
            and parts[4] == "events"
        )

        if google_collection and method == "POST":
            allowed_query = parsed.query in ("", "conferenceDataVersion=1")
        elif google_item and method == "PATCH":
            allowed_query = parsed.query in ("", "conferenceDataVersion=1")
        elif google_item and method == "DELETE":
            allowed_query = not parsed.query
        elif microsoft_collection and method == "POST":
            allowed_query = not parsed.query
        elif microsoft_item and method in ("PATCH", "DELETE"):
            allowed_query = not parsed.query
        else:
            raise WriteViolation(f"{method} is not allowed for that calendar endpoint")
        if not allowed_query:
            raise WriteViolation("Calendar write endpoint contains unsupported options")
