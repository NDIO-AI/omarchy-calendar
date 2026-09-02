# SPDX-License-Identifier: GPL-3.0-or-later
import io
import json
import unittest
import urllib.error
from email.message import Message

from omarchy_calendar.http import HttpError, ReadOnlyHttp, ReadOnlyViolation
from omarchy_calendar.write_http import CalendarWriteHttp, WriteViolation


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload if isinstance(payload, bytes) else json.dumps(payload).encode()

    def read(self, amount=-1):
        return self.payload if amount < 0 else self.payload[:amount]

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


class FakeOpener:
    def __init__(self, payload=None, error=None):
        self.payload = {} if payload is None else payload
        self.error = error
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        if self.error:
            raise self.error
        return FakeResponse(self.payload)


class CalendarWriteHttpTests(unittest.TestCase):
    def test_read_transport_remains_read_only(self):
        with self.assertRaises(ReadOnlyViolation):
            ReadOnlyHttp(opener=FakeOpener()).request_json(
                "POST", "https://www.googleapis.com/calendar/v3/calendars/a/events", {"summary": "No"}
            )

    def test_google_write_transport_allows_only_exact_event_routes(self):
        opener = FakeOpener({"id": "created"})
        http = CalendarWriteHttp(opener=opener)
        create = "https://www.googleapis.com/calendar/v3/calendars/work/events?conferenceDataVersion=1"
        update = "https://www.googleapis.com/calendar/v3/calendars/work/events/event-1"

        self.assertEqual(http.request_json("POST", create, {"summary": "Review"})["id"], "created")
        http.request_json("PATCH", update, {"summary": "Updated"}, headers={"If-Match": '"etag"'})
        http.request_json("DELETE", update)

        self.assertEqual([item[0].get_method() for item in opener.requests], ["POST", "PATCH", "DELETE"])
        self.assertEqual(opener.requests[0][0].get_header("Content-type"), "application/json")
        self.assertEqual(opener.requests[1][0].get_header("If-match"), '"etag"')
        self.assertIsNone(opener.requests[2][0].data)

        rejected = (
            ("POST", "https://www.googleapis.com/calendar/v3/calendars/work"),
            ("POST", "https://www.googleapis.com/calendar/v3/users/me/calendarList"),
            ("PUT", update),
            ("DELETE", update + "?conferenceDataVersion=1"),
            ("POST", create + "&sendUpdates=all"),
            ("POST", "https://evil.example/calendar/v3/calendars/work/events"),
        )
        for method, url in rejected:
            with self.subTest(method=method, url=url), self.assertRaises(WriteViolation):
                http.request_json(method, url, {})

    def test_microsoft_write_transport_allows_only_exact_event_routes(self):
        opener = FakeOpener({"id": "created"})
        http = CalendarWriteHttp(opener=opener)
        create = "https://graph.microsoft.com/v1.0/me/calendars/work/events"
        event = create + "/event-1"

        http.request_json("POST", create, {"subject": "Review"})
        http.request_json("PATCH", event, {"subject": "Updated"})
        http.request_json("DELETE", event)

        for url in (
            "https://graph.microsoft.com/v1.0/me/events",
            "https://graph.microsoft.com/v1.0/users/person/events",
            event + "?$select=id",
        ):
            with self.subTest(url=url), self.assertRaises(WriteViolation):
                http.request_json("POST", url, {})

    def test_payload_size_and_provider_errors_are_safe(self):
        http = CalendarWriteHttp(opener=FakeOpener())
        url = "https://graph.microsoft.com/v1.0/me/calendars/work/events"
        with self.assertRaisesRegex(WriteViolation, "too large"):
            http.request_json("POST", url, {"notes": "x" * 1_100_000})

        headers = Message()
        headers["Retry-After"] = "15"
        error = urllib.error.HTTPError(
            url, 412, "Precondition Failed", headers,
            io.BytesIO(b'{"error":"access_token=private-value conflict"}'),
        )
        with self.assertRaises(HttpError) as caught:
            CalendarWriteHttp(opener=FakeOpener(error=error)).request_json("POST", url, {})
        self.assertEqual(caught.exception.status, 412)
        self.assertEqual(caught.exception.retry_after, "15")
        self.assertNotIn("private-value", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
