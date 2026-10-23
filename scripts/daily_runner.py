#!/usr/bin/env python3
"""Prepare one random plan per Beijing date and execute it once after its end time."""

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import random
import secrets
import shutil

import policy_probe as probe


def generate_plan(day, settings, seed=None):
    seed = secrets.randbits(64) if seed is None else seed
    rng = random.Random(seed)
    midnight = dt.datetime.combine(day, dt.time(), probe.CST)
    start = midnight + dt.timedelta(hours=6, seconds=rng.randrange(3600))
    distance = rng.randint(2000, 3000)
    # Sample only combinations satisfying both the requested pace and the observed time rule.
    minimum = max(841, math.ceil(distance * 360 / 1000))
    duration = rng.randint(minimum, math.floor(distance * 540 / 1000))
    speed = distance / duration
    stride = settings["height_cm"] / 100 * (0.52 + 0.04 * (speed - 2.3))
    cadence = max(140, min(200, round(speed / stride * 60 + rng.uniform(-3, 3))))
    due = start + dt.timedelta(seconds=duration + 60)
    plan = {
        "scheduled_ms": int(due.timestamp() * 1000),
        "start_ms": int(start.timestamp() * 1000),
        "school_window_start_ms": int((midnight + dt.timedelta(hours=6)).timestamp() * 1000),
        "school_window_end_ms": int((midnight + dt.timedelta(hours=8)).timestamp() * 1000),
        "distance_m": distance, "duration_s": duration,
        "height_cm": settings["height_cm"], "cadence_spm": cadence, "seed": seed,
        "anchor_lat_bd": settings["anchor_lat_bd"], "anchor_lon_bd": settings["anchor_lon_bd"],
        "max_radius_m": settings["max_radius_m"],
        "min_duration_s": 841, "min_ordinary_points": 2, "randomize_optional_points": True,
    }
    return plan


def prepare(root, day):
    settings = probe.read_json(root / "settings.json")
    if day < dt.date.fromisoformat(settings["first_date"]):
        raise ValueError("Date precedes the authorized first daily run")
    state = root / "runs" / day.isoformat()
    if (state / "schedule.json").exists():
        return state
    source = Path(settings["binary"])
    if hashlib.sha256(source.read_bytes()).hexdigest() != settings["binary_sha256"]:
        raise ValueError("Daily binary changed")
    runtime = state / "runtime"
    runtime.mkdir(parents=True, mode=0o700, exist_ok=True)
    plan_path = state / "run-plan.json"
    if plan_path.exists():
        plan = probe.read_json(plan_path)
    else:
        plan = generate_plan(day, settings)
        probe.write_json(plan_path, plan)
    binary = runtime / "nekosportsworldtool"
    if not binary.exists():
        # A hard link keeps exe-relative data isolated without a full binary copy every day.
        try:
            os.link(source, binary)
        except OSError:
            shutil.copy2(source, binary)
    if not (runtime / "identity.json").exists():
        shutil.copyfile(settings["identity_template"], runtime / "identity.json")
        (runtime / "identity.json").chmod(0o600)
    probe.write_json(runtime / "config.json", {"update_check": "off", "remember": False})
    probe.write_json(runtime / "session.json", {})
    due = dt.datetime.fromtimestamp(plan["scheduled_ms"] / 1000, probe.CST)
    manifest = {
        "scheduled_at": due.isoformat(), "binary_sha256": settings["binary_sha256"],
        "submission_plan_sha256": hashlib.sha256(plan_path.read_bytes()).hexdigest(),
        "max_submissions": 1, "logout_after_run": True,
        "credentials_env": settings["credentials_env"],
        "results_dir": str(Path(settings["results_root"]) / day.isoformat()),
    }
    probe.write_json(state / "schedule.json", manifest)
    probe.write_json(state / "parameter-generation.json", {
        "date": day.isoformat(), "seed": plan["seed"], "timezone": "Asia/Shanghai",
        "distance_range_m": [2000, 3000], "pace_range_s_per_km": [360, 540],
        "start_range": ["06:00:00", "06:59:59"], "minimum_duration_s": 841,
        "actual_pace_s_per_km": plan["duration_s"] * 1000 / plan["distance_m"],
        "cadence_is_measured": False,
        "point_selection": "All required/fixed points, random subset of at least two live ordinary points",
        "submission_delay_after_end_s": 60,
    })
    probe.preflight(state)
    probe.export_results(state)
    return state


def tick(root, clock=lambda: dt.datetime.now(probe.CST)):
    now = clock().astimezone(probe.CST)
    settings = probe.read_json(root / "settings.json")
    if now.date() < dt.date.fromisoformat(settings["first_date"]) or now.hour not in (6, 7):
        return 0
    exported = Path(settings["results_root"]) / now.date().isoformat() / "result.json"
    if exported.exists() and probe.read_json(exported).get("record_submitted") is True:
        return 0
    state = prepare(root, now.date())
    if (state / "started.json").exists():
        return 0
    due = dt.datetime.fromisoformat(probe.read_json(state / "schedule.json")["scheduled_at"])
    if now < due:
        return 0
    if now >= due + dt.timedelta(minutes=5):
        probe.write_json(state / "started.json", {"skipped_at": now.isoformat()})
        probe.write_json(state / "result.json", {
            "status": "missed_window_no_submission", "record_submitted": False,
            "scheduled_at": due.isoformat(), "finished_at": now.isoformat(),
        })
        probe.export_results(state)
        return 0
    try:
        return probe.run_once(state, clock=clock)
    finally:
        probe.export_results(state)


def cleanup_today(root, day=None):
    """ExecStopPost handles termination after login without ever starting another run."""
    day = day or dt.datetime.now(probe.CST).date()
    state = root / "runs" / day.isoformat()
    if not (state / "started.json").exists() or not (state / "runtime/session.json").exists():
        return
    result = probe.logout_runtime(state / "runtime/nekosportsworldtool")
    probe.write_json(state / "cleanup.json", result)
    probe.export_results(state)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--prepare-date", type=dt.date.fromisoformat)
    action.add_argument("--cleanup-today", action="store_true")
    args = parser.parse_args()
    with (args.state_dir / "daily.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.cleanup_today:
            cleanup_today(args.state_dir)
            return 0
        if args.prepare_date:
            state = prepare(args.state_dir, args.prepare_date)
            plan = probe.read_json(state / "run-plan.json")
            print(json.dumps({"state": str(state), "plan": plan, "network_requests": False}))
            return 0
        return tick(args.state_dir)


if __name__ == "__main__":
    raise SystemExit(main())
