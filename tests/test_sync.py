# SPDX-License-Identifier: GPL-3.0-or-later
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import omarchy_calendar.settings as settings_module
from omarchy_calendar.cache import CalendarStore
from omarchy_calendar.http import HttpError
from omarchy_calendar.models import Account, Event, ProviderHealth
from omarchy_calendar.settings import ProviderSettings
from omarchy_calendar.sync import SyncEngine


class FakeKeyring:
    def __init__(self, token, app_credential=""):
        self.token = token
        self.app_credential = app_credential
        self.puts = []

    def get(self, provider, account_id):
        return dict(self.token) if self.token else None

    def put(self, provider, account_id, token):
        self.token = dict(token)
        self.puts.append((provider, account_id, token))

    def get_app_credential(self, provider):
        return self.app_credential if provider == "google" else ""

    def clear(self, provider, account_id):
        self.token = None

class FakeProvider:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error

    def fetch_window(self, token, start, end):
        self.last_token = token
        if self.error:
            raise self.error
        return self.result


class FakeHttp:
    def __init__(self, response):
        self.response = response
        self.posts = []

    def post_token(self, url, form):
        self.posts.append((url, form))
        return dict(self.response)


def sample_event(uid="google:a:c:fresh"):
    return Event(
        uid=uid, provider="google", account_id="a", account_label="a@example.com",
        calendar_id="c", calendar_name="Work", calendar_color="#7aa2f7",
        title="Fresh", start="2026-08-25T15:00:00+00:00", end="2026-08-25T16:00:00+00:00",
        all_day=False, status="confirmed", location="", description="", organizer="",
        meeting_url="", provider_url="https://calendar.google.com/fresh", updated="2026-08-25T14:00:00Z",
    )


class SyncEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = CalendarStore(Path(self.temp.name) / "calendar.db")
        self.window = ("2026-07-26T12:00:00+00:00", "2026-11-23T12:00:00+00:00")
        old = sample_event("google:a:c:old")
        self.store.replace_window("google", "a", *self.window, [old], ProviderHealth.ok("google", "a", self.window[0]))

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_sync_replaces_provider_window_transactionally(self):
        provider = FakeProvider((Account("google", "a", "a@example.com"), [sample_event()]))
        keyring = FakeKeyring({"access_token": "access", "refresh_token": "refresh", "expires_at": 9999999999})
        engine = SyncEngine(
            self.store, keyring=keyring, settings=ProviderSettings(),
            providers={"google": provider}, now=lambda: datetime(2026, 8, 25, 12, tzinfo=timezone.utc),
        )

        result = engine.sync("google")

        self.assertEqual(result["synced"], 1)
        view = self.store.view(*self.window)
        self.assertEqual([item["uid"] for item in view["events"]], ["google:a:c:fresh"])
        self.assertFalse(view["providers"][0]["stale"])

    def test_network_failure_preserves_cache_and_marks_provider_stale(self):
        provider = FakeProvider(error=HttpError(0, "offline"))
        keyring = FakeKeyring({"access_token": "access", "refresh_token": "refresh", "expires_at": 9999999999})
        engine = SyncEngine(
            self.store, keyring=keyring, settings=ProviderSettings(),
            providers={"google": provider}, now=lambda: datetime(2026, 8, 25, 12, tzinfo=timezone.utc),
        )

        result = engine.sync("google")

        self.assertEqual(result["failed"], 1)
        view = self.store.view(*self.window)
        self.assertEqual(view["events"][0]["uid"], "google:a:c:old")
        self.assertTrue(view["providers"][0]["stale"])
        self.assertEqual(view["providers"][0]["last_error"], "offline")

    def test_refresh_failure_preserves_cache_and_redacts_provider_secrets(self):
        class FailingHttp:
            def post_token(self, _url, _form):
                raise HttpError(0, "access_token=refreshed-secret provider offline")

        keyring = FakeKeyring(
            {"access_token": "old", "refresh_token": "refresh", "expires_at": 0},
            app_credential="desktop-credential",
        )
        engine = SyncEngine(
            self.store,
            keyring=keyring,
            settings=ProviderSettings(google_client_id="google-client"),
            providers={"google": FakeProvider()},
            http=FailingHttp(),
            now=lambda: datetime(2026, 8, 25, 12, tzinfo=timezone.utc),
        )

        result = engine.sync("google")

        self.assertEqual(result["failed"], 1)
        self.assertNotIn("refreshed-secret", str(result))
        self.assertEqual(keyring.puts, [])
        view = self.store.view(*self.window)
        self.assertEqual(view["events"][0]["uid"], "google:a:c:old")
        self.assertTrue(view["providers"][0]["stale"])
        self.assertNotIn("refreshed-secret", view["providers"][0]["last_error"])

    def test_revoked_refresh_token_requires_reconnection_and_preserves_cache(self):
        class RevokedHttp:
            def post_token(self, _url, _form):
                raise HttpError(
                    400,
                    '{"error":"invalid_grant","error_description":"Token revoked"}',
                )

        keyring = FakeKeyring(
            {"access_token": "old", "refresh_token": "refresh", "expires_at": 0},
            app_credential="desktop-credential",
        )
        engine = SyncEngine(
            self.store,
            keyring=keyring,
            settings=ProviderSettings(google_client_id="google-client"),
            providers={"google": FakeProvider()},
            http=RevokedHttp(),
            now=lambda: datetime(2026, 8, 25, 12, tzinfo=timezone.utc),
        )

        result = engine.sync("google")

        self.assertEqual(result["failed"], 1)
        self.assertEqual(keyring.puts, [])
        view = self.store.view(*self.window)
        self.assertEqual(view["events"][0]["uid"], "google:a:c:old")
        self.assertFalse(view["providers"][0]["connected"])
        self.assertTrue(view["providers"][0]["stale"])
        self.assertEqual(
            view["providers"][0]["last_error"],
            "Calendar credentials need browser reconnection",
        )

    def test_expired_token_refreshes_before_provider_read(self):
        provider = FakeProvider((Account("google", "a", "a@example.com"), [sample_event()]))
        keyring = FakeKeyring(
            {"access_token": "old", "refresh_token": "refresh", "expires_at": 0},
            app_credential="desktop-credential",
        )
        http = FakeHttp({"access_token": "new", "expires_in": 3600})
        engine = SyncEngine(
            self.store, keyring=keyring,
            settings=ProviderSettings(google_client_id="google-client"),
            providers={"google": provider}, http=http,
            now=lambda: datetime(2026, 8, 25, 12, tzinfo=timezone.utc),
        )

        result = engine.sync("google")

        self.assertEqual(result["synced"], 1)
        self.assertEqual(provider.last_token, "new")
        self.assertEqual(http.posts[0][0], "https://oauth2.googleapis.com/token")
        self.assertEqual(http.posts[0][1]["grant_type"], "refresh_token")
        self.assertEqual(http.posts[0][1]["client_secret"], "desktop-credential")
        self.assertEqual(keyring.puts[-1][2]["refresh_token"], "refresh")

    def test_expired_google_token_refreshes_with_bundled_registration_metadata(self):
        class NoLocalCredentialKeyring(FakeKeyring):
            def get_app_credential(self, _provider):
                raise AssertionError("bundled refresh must not read a local app credential")

        provider = FakeProvider((Account("google", "a", "a@example.com"), [sample_event()]))
        keyring = NoLocalCredentialKeyring({
            "access_token": "old", "refresh_token": "refresh", "expires_at": 0
        })
        http = FakeHttp({"access_token": "new", "expires_in": 3600})
        with patch.multiple(
            settings_module,
            BUNDLED_PUBLIC_CLIENT_IDS={
                "google": "bundled.apps.googleusercontent.com",
                "microsoft": "",
            },
            BUNDLED_GOOGLE_DESKTOP_APP_CREDENTIAL="bundled-google-credential",
            create=True,
        ):
            engine = SyncEngine(
                self.store, keyring=keyring, settings=ProviderSettings(),
                providers={"google": provider}, http=http,
                now=lambda: datetime(2026, 8, 25, 12, tzinfo=timezone.utc),
            )

            result = engine.sync("google")

        self.assertEqual(result["synced"], 1)
        self.assertEqual(
            http.posts[0][1]["client_id"],
            "bundled.apps.googleusercontent.com",
        )
        self.assertEqual(
            http.posts[0][1]["client_secret"],
            "bundled-google-credential",
        )

    def test_local_google_refresh_requires_its_own_credential_without_bundle_mixing(self):
        provider = FakeProvider((Account("google", "a", "a@example.com"), [sample_event()]))
        keyring = FakeKeyring({
            "access_token": "old", "refresh_token": "refresh", "expires_at": 0
        })
        http = FakeHttp({"access_token": "must-not-be-used", "expires_in": 3600})
        with patch.multiple(
            settings_module,
            BUNDLED_PUBLIC_CLIENT_IDS={
                "google": "bundled.apps.googleusercontent.com",
                "microsoft": "",
            },
            BUNDLED_GOOGLE_DESKTOP_APP_CREDENTIAL="bundled-google-credential",
            create=True,
        ):
            result = SyncEngine(
                self.store, keyring=keyring,
                settings=ProviderSettings(
                    google_client_id="local.apps.googleusercontent.com"
                ),
                providers={"google": provider}, http=http,
                now=lambda: datetime(2026, 8, 25, 12, tzinfo=timezone.utc),
            ).sync("google")

        self.assertEqual(result["failed"], 1)
        self.assertEqual(http.posts, [])
        self.assertIn("Desktop credentials", result["accounts"][0]["error"])

    def test_valid_existing_token_sync_does_not_consult_new_bundled_registration(self):
        class RegistrationGuardKeyring(FakeKeyring):
            def get_app_credential(self, _provider):
                raise AssertionError("a valid token must not consult app registration")

        provider = FakeProvider((Account("google", "a", "a@example.com"), [sample_event()]))
        keyring = RegistrationGuardKeyring({
            "access_token": "existing", "refresh_token": "refresh", "expires_at": 9999999999
        })
        with patch.multiple(
            settings_module,
            BUNDLED_PUBLIC_CLIENT_IDS={
                "google": "bundled.apps.googleusercontent.com",
                "microsoft": "00001111-aaaa-2222-bbbb-3333cccc4444",
            },
            BUNDLED_GOOGLE_DESKTOP_APP_CREDENTIAL="bundled-google-credential",
            create=True,
        ):
            result = SyncEngine(
                self.store, keyring=keyring, settings=ProviderSettings(),
                providers={"google": provider},
                now=lambda: datetime(2026, 8, 25, 12, tzinfo=timezone.utc),
            ).sync("google")

        self.assertEqual(result["synced"], 1)
        self.assertEqual(provider.last_token, "existing")

    def test_microsoft_refresh_never_uses_google_desktop_credential(self):
        self.store.replace_window(
            "microsoft", "m", *self.window, [],
            ProviderHealth.ok("microsoft", "m", self.window[0]),
        )
        provider = FakeProvider((Account("microsoft", "m", "Personal Outlook"), []))
        keyring = FakeKeyring(
            {"access_token": "old", "refresh_token": "refresh", "expires_at": 0},
            app_credential="desktop-credential",
        )
        http = FakeHttp({"access_token": "new", "expires_in": 3600})
        engine = SyncEngine(
            self.store, keyring=keyring,
            settings=ProviderSettings(microsoft_client_id="11111111-2222-3333-4444-555555555555"),
            providers={"microsoft": provider}, http=http,
            now=lambda: datetime(2026, 8, 25, 12, tzinfo=timezone.utc),
        )

        result = engine.sync("microsoft")

        self.assertEqual(result["synced"], 1)
        self.assertNotIn("client_secret", http.posts[0][1])


class LegacyV1UpgradeTests(unittest.TestCase):
    def test_v1_profile_migrates_and_syncs_with_its_existing_token(self):
        class RegistrationGuardSettings:
            def client_id(self, _provider):
                raise AssertionError("a valid v1 token must not consult registration metadata")

            def google_app_credential(self, _keyring):
                raise AssertionError("a valid v1 token must not consult registration metadata")

        class TokenExchangeGuard:
            def post_token(self, _url, _form):
                raise AssertionError("a valid v1 token must not start reauthentication")

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "calendar.db"
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE events (
                  uid TEXT PRIMARY KEY, provider TEXT NOT NULL, account_id TEXT NOT NULL,
                  account_label TEXT NOT NULL, calendar_id TEXT NOT NULL,
                  calendar_name TEXT NOT NULL, calendar_color TEXT NOT NULL,
                  title TEXT NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL,
                  all_day INTEGER NOT NULL CHECK (all_day IN (0, 1)), status TEXT NOT NULL,
                  location TEXT NOT NULL, description TEXT NOT NULL, organizer TEXT NOT NULL,
                  meeting_url TEXT NOT NULL, provider_url TEXT NOT NULL, updated TEXT NOT NULL
                );
                CREATE INDEX events_window ON events (start, end);
                CREATE INDEX events_account ON events (provider, account_id);
                CREATE TABLE provider_health (
                  provider TEXT NOT NULL, account_id TEXT NOT NULL,
                  connected INTEGER NOT NULL CHECK (connected IN (0, 1)),
                  last_sync TEXT NOT NULL, last_error TEXT NOT NULL, retry_after TEXT NOT NULL,
                  stale INTEGER NOT NULL CHECK (stale IN (0, 1)),
                  demo INTEGER NOT NULL CHECK (demo IN (0, 1)), skipped INTEGER NOT NULL,
                  PRIMARY KEY (provider, account_id)
                );
                INSERT INTO events VALUES (
                  'google:a:c:legacy', 'google', 'a', 'a@example.com', 'c', 'Work', '#7aa2f7',
                  'Cached before upgrade', '2026-08-25T15:00:00+00:00',
                  '2026-08-25T16:00:00+00:00', 0, 'confirmed', '', '', '', '',
                  'https://calendar.google.com/legacy', '2026-08-25T14:00:00Z'
                );
                INSERT INTO provider_health VALUES (
                  'google', 'a', 1, '2026-08-25T14:00:00Z', '', '', 0, 0, 0
                );
                """
            )
            connection.commit()
            connection.close()

            original_token = {
                "access_token": "existing-read-token",
                "refresh_token": "existing-refresh",
                "expires_at": 9999999999,
            }
            keyring = FakeKeyring(dict(original_token))
            provider = FakeProvider((
                Account("google", "a", "a@example.com"),
                [sample_event()],
            ))
            with CalendarStore(path) as store:
                migrated = store.get_event("google:a:c:legacy")
                result = SyncEngine(
                    store,
                    keyring=keyring,
                    settings=RegistrationGuardSettings(),
                    providers={"google": provider},
                    http=TokenExchangeGuard(),
                    now=lambda: datetime(2026, 8, 25, 12, tzinfo=timezone.utc),
                ).sync("google")
                view = store.view(
                    "2026-08-25T00:00:00Z", "2026-08-26T00:00:00Z"
                )
                event_columns = {
                    row[1] for row in store.connection.execute("PRAGMA table_info(events)")
                }

            self.assertEqual(result["synced"], 1)
            self.assertEqual(provider.last_token, "existing-read-token")
            self.assertEqual(keyring.token, original_token)
            self.assertEqual(keyring.puts, [])
            self.assertTrue(migrated["has_attendees"])
            self.assertEqual([event["uid"] for event in view["events"]], ["google:a:c:fresh"])
            self.assertFalse(view["events"][0]["has_attendees"])
            self.assertIn("provider_event_id", event_columns)
            self.assertEqual(len(view["calendars"]), 1)
            self.assertFalse(view["calendars"][0]["writable"])


if __name__ == "__main__":
    unittest.main()
