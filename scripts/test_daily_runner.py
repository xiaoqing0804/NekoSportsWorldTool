import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import daily_runner as daily
import policy_probe as probe


class DailyRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.old_umask = os.umask(0o077)
        self.addCleanup(os.umask, self.old_umask)
        (self.root / "binary").write_bytes(b"fixture-binary")
        probe.write_json(self.root / "identity.json", {"device_id": "fixed-fixture"})
        self.env = self.root / "credentials.env"
        self.env.write_text("export NEKOSPORTS_USERNAME=fixture-user\nexport NEKOSPORTS_PASSWORD='fixture secret'\n")
        self.env.chmod(0o600)
        self.settings = {
            "first_date": "2026-10-24", "binary": str(self.root / "binary"),
            "binary_sha256": hashlib.sha256(b"fixture-binary").hexdigest(),
            "identity_template": str(self.root / "identity.json"), "credentials_env": str(self.env),
            "results_root": str(self.root / "results"), "height_cm": 156,
            "anchor_lat_bd": 28.546861820699, "anchor_lon_bd": 112.391247914315,
            "max_radius_m": 900,
            "weather": {"latitude_wgs84": 28.5445, "longitude_wgs84": 112.3793},
        }
        probe.write_json(self.root / "settings.json", self.settings)
        self.day = dt.date(2026, 10, 24)

    def prepared(self):
        state = daily.prepare(self.root, self.day)
        due = dt.datetime.fromisoformat(probe.read_json(state / "schedule.json")["scheduled_at"])
        return state, due

    def test_random_parameters_respect_all_joint_constraints(self):
        starts, distances, cadences = set(), set(), set()
        for seed in range(2000):
            p = daily.generate_plan(self.day, self.settings, seed)
            start = dt.datetime.fromtimestamp(p["start_ms"] / 1000, probe.CST)
            self.assertEqual(start.date(), self.day)
            self.assertEqual(start.hour, 6)
            self.assertTrue(2000 <= p["distance_m"] <= 3000)
            self.assertTrue(360 <= p["duration_s"] * 1000 / p["distance_m"] <= 540)
            self.assertGreater(p["duration_s"], 840)
            self.assertLessEqual(p["scheduled_ms"] + 300_000, p["school_window_end_ms"])
            self.assertEqual(p["scheduled_ms"] - p["start_ms"], (p["duration_s"] + 60) * 1000)
            self.assertTrue(140 <= p["cadence_spm"] <= 200)
            starts.add(p["start_ms"])
            distances.add(p["distance_m"])
            cadences.add(p["cadence_spm"])
        self.assertGreater(len(starts), 100)
        self.assertGreater(len(distances), 100)
        self.assertGreater(len(cadences), 10)

    def test_prepare_is_stable_and_never_restores_logged_out_session(self):
        state, _ = self.prepared()
        original = (state / "run-plan.json").read_bytes()
        (state / "runtime/session.json").unlink()
        self.assertEqual(daily.prepare(self.root, self.day), state)
        self.assertEqual(original, (state / "run-plan.json").read_bytes())
        self.assertFalse((state / "runtime/session.json").exists())
        self.assertEqual(probe.read_json(state / "runtime/identity.json")["device_id"], "fixed-fixture")
        destination = Path(self.settings["results_root"]) / self.day.isoformat()
        self.assertFalse((destination / "session.json").exists())
        self.assertFalse((destination / "credentials.env").exists())

    def test_early_late_and_previous_dates_never_submit(self):
        state, due = self.prepared()
        with patch.object(probe, "run_once") as run:
            daily.tick(self.root, clock=lambda: due - dt.timedelta(seconds=1))
            daily.tick(self.root, clock=lambda: due + dt.timedelta(minutes=5))
            daily.tick(self.root, clock=lambda: due)
            daily.tick(self.root, clock=lambda: due - dt.timedelta(days=1))
            run.assert_not_called()
        self.assertEqual(probe.read_json(state / "result.json")["status"], "missed_window_no_submission")

    def test_success_login_by_environment_logout_and_no_repeat(self):
        self.run_with_login(fail_run=False)

    def test_rain_skip_is_final_for_the_day_without_login_or_submission(self):
        state, due = self.prepared()
        report = {"decision": "rain", "reason": "precipitation_detected"}
        with patch.object(daily, "check_weather", return_value=report) as check, \
                patch.object(probe, "run_once") as run:
            daily.tick(self.root, clock=lambda: due)
            daily.tick(self.root, clock=lambda: due + dt.timedelta(minutes=1))
            check.assert_called_once()
            run.assert_not_called()
        result = probe.read_json(state / "result.json")
        self.assertEqual(result["status"], "skipped_rain")
        self.assertFalse(result["record_submitted"])
        self.assertFalse(result["login_attempted"])
        self.assertEqual(probe.read_json(state / "weather.json"), report)
        destination = Path(self.settings["results_root"]) / self.day.isoformat()
        self.assertEqual(probe.read_json(destination / "weather.json"), report)

    def test_weather_failure_skips_instead_of_assuming_dry(self):
        state, due = self.prepared()
        with patch.object(daily, "check_weather", return_value={"decision": "unavailable"}), \
                patch.object(probe, "run_once") as run:
            daily.tick(self.root, clock=lambda: due)
            run.assert_not_called()
        self.assertEqual(probe.read_json(state / "result.json")["status"], "skipped_weather_unavailable")

    def test_next_day_rechecks_weather_and_dry_result_is_saved_with_submission(self):
        state, due = self.prepared()
        next_state = daily.prepare(self.root, self.day + dt.timedelta(days=1))
        next_due = dt.datetime.fromisoformat(probe.read_json(next_state / "schedule.json")["scheduled_at"])
        events = []

        def weather(settings, now):
            events.append("weather")
            return {"decision": "rain" if now == due else "dry"}

        def run(state, clock):
            events.append("run")
            probe.write_json(state / "started.json", {})
            probe.write_json(state / "result.json", {"record_submitted": True})
            return 0

        with patch.object(daily, "check_weather", side_effect=weather), \
                patch.object(probe, "run_once", side_effect=run):
            daily.tick(self.root, clock=lambda: due)
            daily.tick(self.root, clock=lambda: next_due)
            daily.tick(self.root, clock=lambda: next_due)
        self.assertEqual(events, ["weather", "weather", "run"])
        self.assertEqual(probe.read_json(state / "result.json")["status"], "skipped_rain")
        self.assertEqual(probe.read_json(next_state / "result.json")["weather"]["decision"], "dry")

    def test_weather_is_not_queried_before_due_or_after_completion(self):
        state, due = self.prepared()
        with patch.object(daily, "check_weather") as weather:
            daily.tick(self.root, clock=lambda: due - dt.timedelta(seconds=1))
            probe.write_json(state / "started.json", {})
            daily.tick(self.root, clock=lambda: due)
            weather.assert_not_called()

    def test_failure_also_logs_out_and_server_logout_failure_is_not_hidden(self):
        self.run_with_login(fail_run=True)

    def run_with_login(self, fail_run):
        state, due = self.prepared()
        calls = []
        logged_in = False

        def invoke(args, **kwargs):
            nonlocal logged_in
            command = args[1]
            calls.append(command)
            if command == "policy":
                code, output = (0, "{}") if logged_in else (1, "未登录")
            elif command == "login":
                self.assertEqual(args[2:], ["--env"])
                self.assertEqual(kwargs["env"]["NEKOSPORTS_PASSWORD"], "fixture secret")
                logged_in = True
                probe.write_json(state / "runtime/session.json", {"uid": 1, "token": "fixture-token"})
                code, output = 0, "fixture secret"
            elif command == "fences":
                code, output = 0, '{"geoFences":[]}'
            elif command == "run-planned":
                if fail_run:
                    code, output = 1, "fixture failure"
                else:
                    receipts = state / "runtime/submission_receipts"
                    receipts.mkdir()
                    (receipts / "test.jsonl").write_text('{"uuid":"one","state":"accepted","rrid":123}\n')
                    code, output = 0, json.dumps({"status": "record_accepted", "record_submitted": True,
                                                 "record_id": 123, "verification": {"complete": True}})
            elif command == "logout":
                code, output = int(fail_run), json.dumps({"server_logout_confirmed": not fail_run})
            else:
                self.fail(command)
            return subprocess.CompletedProcess(args, code, output, "")

        probe.run_once(state, clock=lambda: due, invoke=invoke)
        self.assertEqual(calls, ["policy", "login", "policy", "fences", "run-planned", "logout"])
        result = probe.read_json(state / "result.json")
        self.assertEqual(result["logout"]["server_confirmed"], not fail_run)
        self.assertTrue(result["logout"]["local_session_cleared"])
        self.assertFalse((state / "runtime/session.json").exists())
        self.assertTrue(self.env.exists())
        previous_calls = list(calls)
        probe.run_once(state, clock=lambda: due, invoke=invoke)
        daily.tick(self.root, clock=lambda: due)
        self.assertEqual(calls, previous_calls)
        probe.export_results(state)
        for path in state.rglob("*.log"):
            self.assertNotIn("fixture secret", path.read_text())

    def test_cleanup_does_not_log_in_or_resubmit_after_termination(self):
        state, _ = self.prepared()
        probe.write_json(state / "started.json", {})
        probe.write_json(state / "runtime/session.json", {"uid": 1, "token": "fixture"})
        with patch.object(probe, "logout_runtime", return_value={"server_confirmed": True}) as logout:
            daily.cleanup_today(self.root, self.day)
            logout.assert_called_once_with(state / "runtime/nekosportsworldtool")
        self.assertTrue((state / "cleanup.json").exists())

    def test_insecure_environment_and_changed_binary_fail_before_execution(self):
        self.env.chmod(0o644)
        with self.assertRaises(ValueError):
            probe.credential_environment(self.env)
        self.env.chmod(0o600)
        (self.root / "binary").write_bytes(b"changed")
        with self.assertRaises(ValueError):
            daily.prepare(self.root, self.day)


if __name__ == "__main__":
    unittest.main()
