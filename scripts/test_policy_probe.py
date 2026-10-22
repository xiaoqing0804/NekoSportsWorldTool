import datetime as dt
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

import policy_probe as probe


class PolicyProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        runtime = self.state / "runtime"
        runtime.mkdir()
        (runtime / "nekosportsworldtool").write_bytes(b"fixture")
        for name in ("identity.json", "session.json", "config.json"):
            path = runtime / name
            path.write_text("{}")
            path.chmod(0o600)
        self.due = dt.datetime(2026, 10, 23, 7, tzinfo=probe.CST)
        (self.state / "schedule.json").write_text(json.dumps({
            "scheduled_at": self.due.isoformat(),
            "binary_sha256": hashlib.sha256(b"fixture").hexdigest(),
        }))

    def run_probe(self, replies, now=None):
        calls = []

        def invoke(args, **kwargs):
            calls.append(args[1])
            code, out = replies.pop(0)
            return subprocess.CompletedProcess(args, code, out, "")

        status = probe.run_once(self.state, clock=lambda: now or self.due, invoke=invoke)
        return status, calls

    def enable_submission(self):
        path = self.state / "run-plan.json"
        path.write_text(json.dumps({"scheduled_ms": int(self.due.timestamp() * 1000)}))
        manifest = probe.read_json(self.state / "schedule.json")
        manifest.update(submission_plan_sha256=hashlib.sha256(path.read_bytes()).hexdigest(), max_submissions=1)
        (self.state / "schedule.json").write_text(json.dumps(manifest))

    def test_success_invokes_only_one_run_and_reports_acceptance_separately_from_grade(self):
        self.enable_submission()
        reply = json.dumps({"status":"record_accepted", "record_submitted":True,
                            "record_id":123, "verification":{"complete":False}})
        self.assertEqual(self.run_probe([(0,"{}"),(0,'{"geoFences":[]}'),(0,reply)]),
                         (0,["policy","fences","run-planned"]))
        result = probe.read_json(self.state / "result.json")
        self.assertTrue(result["record_submitted"])
        self.assertFalse(result["verification"]["complete"])
        self.assertEqual(self.run_probe([]),(0,[]))

    def test_policy_or_fence_rejection_never_submits(self):
        self.enable_submission()
        self.assertEqual(self.run_probe([(1,"业务错误 11016")]),(0,["policy"]))
        (self.state / "started.json").unlink()
        self.assertEqual(self.run_probe([(0,"{}"),(1,"围栏读取失败")]),(0,["policy","fences"]))

    def test_changed_plan_stops_before_any_network_request(self):
        self.enable_submission()
        (self.state / "run-plan.json").write_text("{}")
        with self.assertRaises(ValueError):
            self.run_probe([])
        self.assertFalse((self.state / "started.json").exists())

    def test_timeout_with_pending_receipt_is_unknown_and_never_retried(self):
        self.enable_submission()
        calls = []
        def invoke(args, **kwargs):
            calls.append(args[1])
            if args[1] == "run-planned":
                directory = self.state / "runtime/submission_receipts"
                directory.mkdir()
                (directory / "fixture.jsonl").write_text('{"state":"pending","uuid":"fixture"}\n')
                raise subprocess.TimeoutExpired(args,180,output=b"partial")
            return subprocess.CompletedProcess(args,0,'{"geoFences":[]}',"")
        self.assertEqual(probe.run_once(self.state,clock=lambda:self.due,invoke=invoke),1)
        self.assertEqual(calls,["policy","fences","run-planned"])
        result = probe.read_json(self.state / "result.json")
        self.assertIsNone(result["record_submitted"])
        self.assertEqual(result["status"],"submission_unknown_no_retry")
        self.assertEqual(self.run_probe([]),(0,[]))

    def test_existing_attempt_blocks_scheduled_run(self):
        self.enable_submission()
        directory = self.state / "runtime/submission_receipts"
        directory.mkdir()
        (directory / "fixture.jsonl").write_text('{"state":"pending","uuid":"fixture"}\n')
        with self.assertRaises(ValueError):
            self.run_probe([])

    def test_partial_receipt_is_not_treated_as_rejection(self):
        directory = self.state / "runtime/submission_receipts"
        directory.mkdir()
        (directory / "fixture.jsonl").write_text('{"state":')
        with self.assertRaises(ValueError):
            probe.receipt_status(self.state)

    def test_wrong_time_never_contacts_server(self):
        for now in (self.due - dt.timedelta(seconds=1), self.due + dt.timedelta(minutes=5)):
            self.assertEqual(self.run_probe([], now), (1, []))
        self.assertFalse((self.state / "started.json").exists())

    def test_success_reads_fences_and_cannot_repeat(self):
        self.assertEqual(self.run_probe([(0, "{}"), (0, '{"geoFences":[]}')]),
                         (0, ["policy", "fences"]))
        self.assertEqual(self.run_probe([]), (0, []))
        result = probe.read_json(self.state / "result.json")
        self.assertFalse(result["record_submitted"])
        self.assertEqual(result["fence_count"], 0)

    def test_11016_does_not_retry_login_or_submit(self):
        self.assertEqual(self.run_probe([(1, "业务错误 11016: 不在有效跑步时间内")]),
                         (0, ["policy"]))
        self.assertEqual(probe.read_json(self.state / "result.json")["business_code"], 11016)

    def test_expired_session_gets_one_login_then_erases_password(self):
        secret = self.state / "login-once.json"
        secret.write_text('{"username":"fixture-user","password":"fixture-secret"}')
        secret.chmod(0o600)
        replies = [(1, "登录已失效（HTTP 401）"), (0, "fixture-secret"),
                   (0, "{}"), (0, '{"geoFences":[]}')]
        self.assertEqual(self.run_probe(replies), (0, ["policy", "login", "policy", "fences"]))
        self.assertFalse(secret.exists())
        for path in self.state.glob("*.log"):
            self.assertNotIn("fixture-secret", path.read_text())


if __name__ == "__main__":
    unittest.main()
