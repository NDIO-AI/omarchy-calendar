# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import json
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "tests" / "live_write_e2e.py"
HELPER = ROOT / "tests" / "e2e_helper.py"


class LiveWriteE2EContractTests(unittest.TestCase):
    def test_sync_retries_a_transient_provider_timeout(self):
        environment = {
            "FLIGHT_DECK_GOOGLE_SOURCE_CALENDAR": "Google Verification",
            "FLIGHT_DECK_GOOGLE_DESTINATION_CALENDAR": "Google Transfer",
            "FLIGHT_DECK_OUTLOOK_SOURCE_CALENDAR": "Outlook Verification",
            "FLIGHT_DECK_OUTLOOK_DESTINATION_CALENDAR": "Outlook Transfer",
        }
        with patch.dict(os.environ, environment):
            spec = importlib.util.spec_from_file_location("live_write_e2e_retry", RUNNER)
            module = importlib.util.module_from_spec(spec)
            assert spec.loader is not None
            spec.loader.exec_module(module)

        responses = iter([
            RuntimeError("TimeoutError: The read operation timed out"),
            {"synced": 1, "failed": 0},
        ])
        attempts = []

        def transient_call(*arguments, **_kwargs):
            attempts.append(arguments)
            response = next(responses)
            if isinstance(response, Exception):
                raise response
            return response

        module.call = transient_call
        module.sync("microsoft")
        self.assertEqual(attempts, [("sync", "--provider", "microsoft")] * 2)

    def test_complete_matrix_runs_against_the_test_only_calendarctl_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            environment = os.environ.copy()
            environment.update({
                "FLIGHT_DECK_LIVE_HELPER": str(HELPER),
                "FLIGHT_DECK_E2E_ROOT": temporary,
                "FLIGHT_DECK_LIVE_CONTRACT_ONLY": "1",
                "TZ": "UTC",
                "FLIGHT_DECK_GOOGLE_SOURCE_CALENDAR": "Google Verification",
                "FLIGHT_DECK_GOOGLE_DESTINATION_CALENDAR": "Google Transfer",
                "FLIGHT_DECK_OUTLOOK_SOURCE_CALENDAR": "Outlook Verification",
                "FLIGHT_DECK_OUTLOOK_DESTINATION_CALENDAR": "Outlook Transfer",
            })

            result = subprocess.run(
                [sys.executable, str(RUNNER)], cwd=ROOT, env=environment,
                text=True, capture_output=True, check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("PASS live disposable-calendar provider matrix", result.stdout)
            records = [
                json.loads(line)
                for line in (Path(temporary) / "commands.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            commands = [row["arguments"][0] for row in records]
            self.assertEqual(
                [row["arguments"][:2] for row in records].count(["auth", "google"]),
                1,
                "One live run must not force repeated Google warning bypasses",
            )
            self.assertEqual(
                [row["arguments"][:2] for row in records].count(["enable-editing", "google"]),
                1,
                "The combined matrix must reuse the verified Google edit grant",
            )
            self.assertGreaterEqual(commands.count("create-event"), 6)
            self.assertGreaterEqual(commands.count("update-event"), 5)
            self.assertGreaterEqual(commands.count("copy-event"), 5)
            self.assertGreaterEqual(commands.count("delete-event"), 8)
            self.assertEqual(commands.count("reset-local-data"), 2)
            for provider_title in ("Google", "Outlook"):
                occurrence = next(
                    row["stdin"] for row in records
                    if row["arguments"] == ["update-event"]
                    and str((row.get("stdin") or {}).get("title") or "").endswith(
                        f"{provider_title} occurrence"
                    )
                )
                series = next(
                    row["stdin"] for row in records
                    if row["arguments"] == ["update-event"]
                    and str((row.get("stdin") or {}).get("title") or "").endswith(
                        f"{provider_title} series"
                    )
                )
                self.assertNotEqual(
                    occurrence["source_uid"], series["source_uid"],
                    "Entire-series verification must use an unmodified occurrence, "
                    "because Outlook preserves occurrence-specific exceptions",
                )
            google_disconnect = next(
                index for index, row in enumerate(records)
                if row["arguments"][:2] == ["disconnect", "google"]
            )
            self.assertTrue(any(
                row["arguments"] == ["delete-event"]
                and str((row.get("stdin") or {}).get("uid") or "").startswith("e2e:")
                for row in records[:google_disconnect]
            ), "Google test events must be removed before its isolated token is disconnected")

    def test_shell_runner_names_the_first_missing_four_calendar_input(self):
        environment = os.environ.copy()
        environment["FLIGHT_DECK_LIVE_WRITE_E2E"] = "1"
        for name in tuple(environment):
            if name.startswith("FLIGHT_DECK_GOOGLE_") or name.startswith("FLIGHT_DECK_OUTLOOK_"):
                environment.pop(name)

        result = subprocess.run(
            [str(ROOT / "scripts" / "e2e"), "live"], cwd=ROOT, env=environment,
            text=True, capture_output=True, check=False,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("FLIGHT_DECK_GOOGLE_SOURCE_CALENDAR", result.stderr)

    def test_existing_microsoft_edit_grant_runs_without_reauthorization(self):
        with tempfile.TemporaryDirectory() as temporary:
            environment = os.environ.copy()
            environment.update({
                "FLIGHT_DECK_LIVE_HELPER": str(HELPER),
                "FLIGHT_DECK_E2E_ROOT": temporary,
                "FLIGHT_DECK_E2E_PREGRANTED_MICROSOFT_EDIT": "1",
                "FLIGHT_DECK_LIVE_CONTRACT_ONLY": "1",
                "TZ": "UTC",
                "FLIGHT_DECK_GOOGLE_SOURCE_CALENDAR": "Google Verification",
                "FLIGHT_DECK_GOOGLE_DESTINATION_CALENDAR": "Google Transfer",
                "FLIGHT_DECK_OUTLOOK_SOURCE_CALENDAR": "Outlook Verification",
                "FLIGHT_DECK_OUTLOOK_DESTINATION_CALENDAR": "Outlook Transfer",
            })

            result = subprocess.run(
                [sys.executable, str(RUNNER)], cwd=ROOT, env=environment,
                text=True, capture_output=True, check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            records = [
                json.loads(line)
                for line in (Path(temporary) / "commands.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertNotIn(
                ["enable-editing", "microsoft", "--account", "outlook-read"],
                [row["arguments"] for row in records],
            )

    def test_unsupported_outlook_meetings_and_cleanup_revision_drift_fail_safe(self):
        with tempfile.TemporaryDirectory() as temporary:
            environment = os.environ.copy()
            environment.update({
                "FLIGHT_DECK_LIVE_HELPER": str(HELPER),
                "FLIGHT_DECK_E2E_ROOT": temporary,
                "FLIGHT_DECK_E2E_MICROSOFT_NO_MEETINGS": "1",
                "FLIGHT_DECK_E2E_CLEANUP_REVISION_DRIFT": "1",
                "FLIGHT_DECK_LIVE_CONTRACT_ONLY": "1",
                "TZ": "UTC",
                "FLIGHT_DECK_GOOGLE_SOURCE_CALENDAR": "Google Verification",
                "FLIGHT_DECK_GOOGLE_DESTINATION_CALENDAR": "Google Transfer",
                "FLIGHT_DECK_OUTLOOK_SOURCE_CALENDAR": "Outlook Verification",
                "FLIGHT_DECK_OUTLOOK_DESTINATION_CALENDAR": "Outlook Transfer",
            })

            result = subprocess.run(
                [sys.executable, str(RUNNER)], cwd=ROOT, env=environment,
                text=True, capture_output=True, check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            records = [
                json.loads(line)
                for line in (Path(temporary) / "commands.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            outlook_meeting = next(
                row["stdin"] for row in records
                if row["arguments"] == ["create-event"]
                and str((row.get("stdin") or {}).get("title") or "").endswith("Outlook meeting")
            )
            self.assertEqual(outlook_meeting["online_meeting"], "none")
            self.assertTrue((Path(temporary) / "cleanup-drift-used").is_file())

    def test_transfer_delete_conflict_refreshes_before_reconfirmation(self):
        with tempfile.TemporaryDirectory() as temporary:
            environment = os.environ.copy()
            environment.update({
                "FLIGHT_DECK_LIVE_HELPER": str(HELPER),
                "FLIGHT_DECK_E2E_ROOT": temporary,
                "FLIGHT_DECK_E2E_TRANSFER_DELETE_REVISION_DRIFT": "1",
                "FLIGHT_DECK_LIVE_CONTRACT_ONLY": "1",
                "TZ": "UTC",
                "FLIGHT_DECK_GOOGLE_SOURCE_CALENDAR": "Google Verification",
                "FLIGHT_DECK_GOOGLE_DESTINATION_CALENDAR": "Google Transfer",
                "FLIGHT_DECK_OUTLOOK_SOURCE_CALENDAR": "Outlook Verification",
                "FLIGHT_DECK_OUTLOOK_DESTINATION_CALENDAR": "Outlook Transfer",
            })

            result = subprocess.run(
                [sys.executable, str(RUNNER)], cwd=ROOT, env=environment,
                text=True, capture_output=True, check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            records = [
                json.loads(line)
                for line in (Path(temporary) / "commands.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            deleted_uids = [
                str(row["stdin"]["uid"])
                for row in records
                if row["arguments"] == ["delete-event"] and row.get("stdin")
            ]
            self.assertTrue((Path(temporary) / "transfer-delete-drift-used").is_file())
            self.assertTrue(any(deleted_uids.count(uid) == 2 for uid in set(deleted_uids)))


if __name__ == "__main__":
    unittest.main()
