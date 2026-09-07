# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "tests" / "e2e_helper.py"


class DeterministicE2EHelperTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.environment = os.environ.copy()
        self.environment["FLIGHT_DECK_E2E_ROOT"] = self.temporary.name

    def tearDown(self):
        self.temporary.cleanup()

    def run_helper(self, *arguments, payload=None):
        return subprocess.run(
            [str(HELPER), *arguments], input=json.dumps(payload) + "\n" if payload is not None else None,
            text=True, capture_output=True, env=self.environment, check=False,
        )

    def json(self, *arguments, payload=None):
        result = self.run_helper(*arguments, payload=payload)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_authorization_outcomes_preserve_read_only_accounts_on_failure(self):
        self.json("demo", "seed")
        before = self.json("setup-status")
        denied = self.run_helper("enable-editing", "google", "--account", "google-denial")
        wrong = self.run_helper("enable-editing", "google", "--account", "google-wrong")
        after_failures = self.json("setup-status")
        success = self.json("enable-editing", "google", "--account", "google-read")
        after_success = self.json("setup-status")

        self.assertEqual(denied.returncode, 2)
        self.assertIn("Access was denied", denied.stderr)
        self.assertEqual(wrong.returncode, 2)
        self.assertIn("different account", wrong.stderr)
        self.assertEqual(before, after_failures)
        self.assertEqual(success["account_id"], "google-read")
        self.assertEqual(after_success["providers"][0]["editing_account_ids"], ["google-read"])

    def test_native_mode_accepts_any_read_only_test_account(self):
        self.environment["FLIGHT_DECK_E2E_NATIVE"] = "1"
        self.json("demo", "seed")

        result = self.json("enable-editing", "google", "--account", "google-wrong")

        self.assertEqual(result["account_id"], "google-wrong")
        self.assertEqual(
            self.json("setup-status")["providers"][0]["editing_account_ids"],
            ["google-wrong"],
        )

    def test_all_day_fixture_uses_an_exclusive_end_day(self):
        seeded = self.json("demo", "seed", "--date", "2031-02-17")

        event = next(row for row in self.json("view")["events"] if row["all_day"])

        self.assertEqual(seeded["date"], "2031-02-17")
        self.assertEqual((event["start_day"], event["end_day"]), ("2031-02-17", "2031-02-18"))

    def test_mutations_use_stdin_and_change_only_the_deterministic_cache(self):
        self.json("demo", "seed")
        self.json("enable-editing", "google", "--account", "google-read")
        self.json("enable-editing", "microsoft", "--account", "outlook-read")
        base = {
            "request_id": "11111111-1111-4111-a111-111111111111", "operation": "create",
            "source_uid": "", "source_revision": "", "calendar_key": "google-main",
            "title": "Created in deterministic E2E", "day": "2026-09-03", "end_day": "2026-09-03",
            "start": "16:00", "end": "17:00", "all_day": False, "location": "", "notes": "",
            "recurrence": {"frequency": "none", "weekdays": [], "end": "never"},
            "online_meeting": "none", "scope": "single",
        }
        created = self.json("create-event", payload=base)["event"]
        updated = self.json("update-event", payload={**base, "operation": "update", "source_uid": created["uid"], "title": "Updated"})["event"]
        copied = self.json("copy-event", payload={
            **base,
            "request_id": "22222222-2222-4222-a222-222222222222",
            "operation": "copy",
            "source_uid": updated["uid"],
            "calendar_key": "outlook-main",
        })
        self.json("delete-event", payload={"uid": updated["uid"], "scope": "single", "confirmed": True})
        view = self.json("view", "--from", "2026-09-03T00:00:00-05:00", "--to", "2026-09-04T00:00:00-05:00")

        self.assertEqual(updated["title"], "Updated")
        self.assertTrue(copied["delete_original_available"])
        self.assertNotIn(updated["uid"], {row["uid"] for row in view["events"]})
        records = [json.loads(line) for line in (Path(self.temporary.name) / "commands.jsonl").read_text().splitlines()]
        mutation = next(row for row in records if row["arguments"] == ["create-event"])
        self.assertEqual(mutation["stdin"]["title"], base["title"])
        self.assertNotIn(base["title"], mutation["arguments"])

    def test_delete_confirmation_and_injected_failures_fail_closed(self):
        self.json("demo", "seed")
        uid = "google:google-read:event"
        rejected = self.run_helper("delete-event", payload={"uid": uid, "scope": "single"})
        self.json("enable-editing", "google", "--account", "google-read")
        (Path(self.temporary.name) / "mutation-outcome").write_text("Event changed remotely. Draft preserved.")
        conflict = self.run_helper("delete-event", payload={"uid": uid, "scope": "single", "confirmed": True})
        current = self.json("view", "--from", "x", "--to", "y")

        self.assertEqual(rejected.returncode, 2)
        self.assertIn("explicit confirmation", rejected.stderr)
        self.assertEqual(conflict.returncode, 2)
        self.assertIn("changed remotely", conflict.stderr)
        self.assertIn(uid, {row["uid"] for row in current["events"]})


if __name__ == "__main__":
    unittest.main()
