#!/usr/bin/env python3
"""One scheduled policy probe, optionally followed by one explicitly configured run."""

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

CST = dt.timezone(dt.timedelta(hours=8))


def read_json(path):
    return json.loads(path.read_text())


def write_json(path, value):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def preflight(state):
    manifest = read_json(state / "schedule.json")
    due = dt.datetime.fromisoformat(manifest["scheduled_at"])
    if due.utcoffset() != dt.timedelta(hours=8):
        raise ValueError("Schedule must specify Beijing time")
    binary = state / "runtime/nekosportsworldtool"
    if hashlib.sha256(binary.read_bytes()).hexdigest() != manifest["binary_sha256"]:
        raise ValueError("Pinned diagnostic binary changed")
    for name in ("identity.json", "session.json", "config.json"):
        path = binary.parent / name
        read_json(path)
        if path.stat().st_mode & 0o077:
            raise ValueError("Runtime files must be private")
    credential = state / "login-once.json"
    if credential.exists() and credential.stat().st_mode & 0o077:
        raise ValueError("One-use credentials must be private")
    if "submission_plan_sha256" in manifest:
        plan_path = state / "run-plan.json"
        if hashlib.sha256(plan_path.read_bytes()).hexdigest() != manifest["submission_plan_sha256"]:
            raise ValueError("Pinned submission plan changed")
        plan = read_json(plan_path)
        if plan["scheduled_ms"] != int(due.timestamp() * 1000):
            raise ValueError("Run and scheduler times differ")
        if manifest.get("max_submissions") != 1:
            raise ValueError("Only one submission is allowed")
        if any(path.stat().st_size for path in (binary.parent / "submission_receipts").glob("*.jsonl")):
            raise ValueError("Scheduled runtime already contains a submission attempt")
    return due, binary


def receipt_status(state):
    """A timeout is not a rejection: inspect durable receipts without resubmitting."""
    attempts = {}
    for path in (state / "runtime/submission_receipts").glob("*.jsonl"):
        raw = path.read_text()
        if raw and not raw.endswith("\n"):
            raise ValueError("Incomplete submission receipt")
        for line in raw.splitlines():
            event = json.loads(line)
            attempts[event["uuid"]] = event
    accepted = [a for a in attempts.values() if a["state"] == "accepted"]
    if accepted:
        return {"record_submitted": True, "record_id": accepted[-1]["rrid"],
                "status": "record_accepted_followup_incomplete"}
    if any(a["state"] == "pending" for a in attempts.values()):
        return {"record_submitted": None, "status": "submission_unknown_no_retry"}
    return {"record_submitted": False}


def export_results(state):
    """Copy result artifacts to the configured workspace folder, excluding login state."""
    destination = read_json(state / "schedule.json").get("results_dir")
    if not destination:
        return
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    names = (
        "result.json", "schedule.json", "run-plan.json", "started.json",
        "location.json", "parameter-generation.json", "preview-summary.json",
        "offline-preview.json", "points-before-schedule.json", "policy.log",
        "policy-before-login.log", "fences.log", "run.log", "run-timeout.log",
    )
    files = [(state / name, destination / name) for name in names]
    files.extend((state / "runtime" / name, destination / name) for name in (
        "planned-policy.json", "planned-points.json", "planned-fences.json", "planned-track.json",
    ))
    files.extend((path, destination / "submission_receipts" / path.name)
                 for path in (state / "runtime/submission_receipts").glob("*.jsonl"))
    for source, target in files:
        if not source.is_file():
            continue
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = target.with_suffix(target.suffix + ".tmp")
        shutil.copyfile(source, temporary)
        temporary.chmod(0o600)
        temporary.replace(target)


def run_once(state, clock=lambda: dt.datetime.now(CST), invoke=subprocess.run):
    os.umask(0o077)
    with (state / "probe.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (state / "started.json").exists():
            return 0
        due, binary = preflight(state)
        manifest = read_json(state / "schedule.json")
        submit_enabled = "submission_plan_sha256" in manifest
        deadline = due + dt.timedelta(minutes=5)
        if not due <= clock() < deadline:
            print("Outside scheduled 07:00–07:05 window; no request sent.")
            return 1
        started = clock().isoformat()
        write_json(state / "started.json", {"started_at": started})
        credential_path = state / "login-once.json"
        result = {"started_at": started, "scheduled_at": due.isoformat(),
                  "record_submitted": False, "location_sent_by_policy": False}
        run_invoked = False

        def call(command, arguments=(), timeout=45):
            if command not in ("policy", "fences", "login") and not (command == "run-planned" and submit_enabled):
                raise ValueError("Command is not enabled for this task")
            if not due <= clock() < deadline:
                raise TimeoutError("Diagnostic window ended")
            return invoke([str(binary), command, *arguments], capture_output=True,
                          text=True, timeout=timeout, cwd=binary.parent)

        try:
            response = call("policy")
            output = response.stdout + response.stderr
            auth_failure = any(marker in output for marker in
                               ("HTTP 401", "未登录", "请先登录", "登录已失效"))
            if response.returncode and auth_failure and credential_path.exists():
                (state / "policy-before-login.log").write_text(output)
                credential = read_json(credential_path)
                login = call("login", ("--user", credential["username"],
                                      "--pass", credential["password"]), timeout=150)
                # Login logs can contain account data; retain only its outcome.
                result["login_retry_exit"] = login.returncode
                if login.returncode:
                    raise RuntimeError("Authentication retry failed")
                response = call("policy")
                output = response.stdout + response.stderr
            (state / "policy.log").write_text(output)
            codes = re.findall(r"业务错误\s+(\d+)", output)
            result["policy_exit"] = response.returncode
            result["business_code"] = int(codes[0]) if codes else None
            result["status"] = "policy_ok" if response.returncode == 0 else "policy_rejected"
            if response.returncode == 0:
                fences = call("fences")
                (state / "fences.log").write_text(fences.stdout + fences.stderr)
                result["fences_exit"] = fences.returncode
                if fences.returncode == 0:
                    result["fence_count"] = len(json.loads(fences.stdout)["geoFences"])
                    if submit_enabled:
                        # Recheck pinned files immediately before the sole run invocation.
                        preflight(state)
                        result["status"] = "run_started"
                        result["record_submitted"] = None
                        write_json(state / "result.json", result)
                        run_invoked = True
                        run = call("run-planned", ("--file", str(state / "run-plan.json"), "--execute"), timeout=180)
                        (state / "run.log").write_text(run.stdout + run.stderr)
                        result["run_exit"] = run.returncode
                        if run.returncode == 0:
                            output = json.loads(run.stdout)
                            if output.get("status") != "record_accepted" or not output.get("record_submitted"):
                                raise ValueError("Run command did not confirm acceptance")
                            result.update(output)
                        else:
                            result["status"] = "run_failed_no_retry"
                elif submit_enabled:
                    result["status"] = "fences_failed_no_submission"
            return 0
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
            result["status"] = "probe_failed"
            result["error_type"] = type(error).__name__
            if isinstance(error, subprocess.TimeoutExpired):
                output = "".join(part.decode(errors="replace") if isinstance(part, bytes) else part or ""
                                 for part in (error.stdout, error.stderr))
                # Never persist partial login output, which may contain credentials.
                if run_invoked:
                    (state / "run-timeout.log").write_text(output)
            return 1
        finally:
            if run_invoked:
                try:
                    outcome = receipt_status(state)
                    if result.get("record_submitted") is not True:
                        result.update(outcome)
                    elif outcome["record_submitted"] is not True:
                        result["receipt_warning"] = "Server accepted; local receipt unavailable"
                except (OSError, ValueError, KeyError):
                    if result.get("record_submitted") is not True:
                        result.update(record_submitted=None, status="submission_unknown_no_retry")
            credential_path.unlink(missing_ok=True)
            result["finished_at"] = clock().isoformat()
            write_json(state / "result.json", result)
            print(json.dumps(result, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--export-results", action="store_true", help="Copy existing results without network requests")
    args = parser.parse_args()
    if args.export_results:
        export_results(args.state_dir)
        return 0
    if args.preflight:
        due, _ = preflight(args.state_dir)
        print(f"Preflight OK; scheduled={due.isoformat()}; no network requests")
        return 0
    try:
        return run_once(args.state_dir)
    finally:
        export_results(args.state_dir)


if __name__ == "__main__":
    raise SystemExit(main())
