"""One-shot verification service.

Runs, in order:
  1. code tests   -- the unit-test suite (stdlib unittest)
  2. build check  -- byte-compile every source tree and import the modules
  3. API smoke    -- POST a multi-track, tempo-changing format-1 file plus
                     structural-error cases to a healthy app container

Every step is reported on stdout; the process exit code is 0 only when all
steps pass, so `docker compose up --exit-code-from verify verify` surfaces
the verdict directly.
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

APP_URL = os.environ.get("APP_URL", "http://127.0.0.1:8000").rstrip("/")
HEALTH_TIMEOUT_S = float(os.environ.get("HEALTH_TIMEOUT", "60"))


# -- SMF builders (self-contained on purpose) -------------------------------


def varlen(n):
    out = bytes([n & 0x7F])
    n >>= 7
    while n:
        out = bytes([(n & 0x7F) | 0x80]) + out
        n >>= 7
    return out


def track(payload):
    return b"MTrk" + len(payload).to_bytes(4, "big") + payload


def header(fmt, ntracks, division):
    return (
        b"MThd"
        + (6).to_bytes(4, "big")
        + fmt.to_bytes(2, "big")
        + ntracks.to_bytes(2, "big")
        + division.to_bytes(2, "big")
    )


def tempo(delta, us):
    return varlen(delta) + bytes([0xFF, 0x51, 0x03]) + us.to_bytes(3, "big")


def ev(delta, *bs):
    return varlen(delta) + bytes(bs)


EOT = ev(0, 0xFF, 0x2F, 0x00)


def build_smoke_file():
    """Format 1, PPQN 480, three tracks, three tempo changes on track 0.

    Tempo map (track 0 only): 500000 us/qn until tick 480, 250000 until
    tick 960, 1000000 afterwards.  Track 2 carries a decoy tempo event that
    must be ignored, and one event uses running status.
    """
    conductor = (
        tempo(0, 500000)
        + tempo(480, 250000)
        + tempo(480, 1000000)  # absolute tick 960
        + EOT
    )
    piano = (
        ev(0, 0x90, 60, 100)        # tick 0    note on
        + ev(1, 0x80, 60, 0)        # tick 1    note off
        + ev(479, 0x90, 62, 100)    # tick 480  note on
        + ev(480, 0x80, 62, 0)      # tick 960  note off
        + ev(240, 0x90, 64, 100)    # tick 1200 note on
        + ev(60, 64, 0)             # tick 1260 running-status note on, vel 0
        + EOT
    )
    strings = (
        tempo(0, 1)                 # decoy: must NOT affect the timeline
        + ev(240, 0xC3, 5)          # tick 240  program change, channel 3
        + ev(0, 0x91, 65, 100)      # tick 240  note on, channel 1
        + EOT
    )
    return header(1, 3, 480) + track(conductor) + track(piano) + track(strings)


# Expected timeline for build_smoke_file(): (tick, track, order, type,
# channel, data, fraction-of-microseconds).
EXPECTED_EVENTS = [
    (0, 1, 0, "note_on", 0, [60, 100], "0/1"),
    (1, 1, 1, "note_off", 0, [60, 0], "3125/3"),
    (240, 2, 0, "program_change", 3, [5], "250000/1"),
    (240, 2, 1, "note_on", 1, [65, 100], "250000/1"),
    (480, 1, 2, "note_on", 0, [62, 100], "500000/1"),
    (960, 1, 3, "note_off", 0, [62, 0], "750000/1"),
    (1200, 1, 4, "note_on", 0, [64, 100], "1250000/1"),
    (1260, 1, 5, "note_on", 0, [64, 0], "1375000/1"),
]


# -- helpers -----------------------------------------------------------------


def post(body):
    request = urllib.request.Request(
        APP_URL + "/api/midi/normalize",
        data=body,
        headers={"Content-Type": "application/octet-stream"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def wait_for_app():
    deadline = time.monotonic() + HEALTH_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(APP_URL + "/health", timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(1)
    return False


class Suite:
    def __init__(self):
        self.failures = []

    def check(self, label, condition, detail=""):
        status = "ok" if condition else "FAIL"
        line = f"    [{status}] {label}"
        if detail and not condition:
            line += f" -- {detail}"
        print(line, flush=True)
        if not condition:
            self.failures.append(label)


# -- steps -------------------------------------------------------------------


def step_unit_tests():
    print("[1/4] code tests: unittest suite", flush=True)
    result = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", ".", "-v"],
        capture_output=True,
        text=True,
    )
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    print(f"      unittest exited with {result.returncode}", flush=True)
    return result.returncode == 0


def step_build_check():
    print("[2/4] build check: byte-compile and import all modules", flush=True)
    result = subprocess.run(
        [sys.executable, "-m", "compileall", "-q", "app", "tests", "verify"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        return False
    try:
        import app.midi  # noqa: F401
        import app.server  # noqa: F401
    except Exception as exc:  # pragma: no cover - defensive
        print(f"      import failed: {exc}", file=sys.stderr)
        return False
    print("      compileall + imports ok", flush=True)
    return True


def step_wait_for_app():
    print(f"[3/4] waiting for app health at {APP_URL}/health", flush=True)
    ok = wait_for_app()
    print("      healthy" if ok else "      NOT healthy", flush=True)
    return ok


def step_api_smoke():
    print("[4/4] API smoke: multi-track tempo-changing file + error cases",
          flush=True)
    suite = Suite()

    # -- happy path: format 1, three tracks, tempo changes ----------------
    status, body = post(build_smoke_file())
    suite.check("status 200", status == 200, f"got {status}: {body}")
    if status != 200:
        return False
    suite.check("format is 1", body.get("format") == 1)
    suite.check("ppqn is 480", body.get("ppqn") == 480)
    suite.check("track_count is 3", body.get("track_count") == 3)
    suite.check(
        "channel_event_count is 8",
        body.get("channel_event_count") == 8,
        f"got {body.get('channel_event_count')}",
    )
    events = body.get("events", [])
    suite.check("8 events returned", len(events) == 8, f"got {len(events)}")
    for i, expected in enumerate(EXPECTED_EVENTS):
        if i >= len(events):
            break
        tick, trk, order, etype, channel, data, fraction = expected
        got = events[i]
        want = {
            "tick": tick,
            "track": trk,
            "order": order,
            "type": etype,
            "channel": channel,
            "data": data,
        }
        actual = {k: got.get(k) for k in want}
        suite.check(f"event {i} identity", actual == want,
                    f"want {want}, got {actual}")
        suite.check(
            f"event {i} time {fraction}",
            got.get("time_us", {}).get("fraction") == fraction,
            f"got {got.get('time_us')}",
        )
    keys = [(e["tick"], e["track"], e["order"]) for e in events]
    suite.check("events sorted by (tick, track, order)", keys == sorted(keys))

    # -- structural errors: offset reported, no partial timeline ----------
    valid = build_smoke_file()
    cases = [
        ("truncated file", valid[:30], "truncated_track"),
        ("trailing garbage", valid + b"\x00", "trailing_bytes"),
        (
            "illegal status",
            header(0, 1, 480) + track(ev(0, 0xF8)),
            "illegal_status",
        ),
        (
            "orphan running status",
            header(0, 1, 480) + track(ev(0, 60, 100)),
            "orphan_running_status",
        ),
    ]
    for label, payload, code in cases:
        status, body = post(payload)
        err = body.get("error", {})
        suite.check(f"{label}: HTTP 400", status == 400, f"got {status}")
        suite.check(f"{label}: code {code}", err.get("code") == code,
                    f"got {err.get('code')}")
        suite.check(
            f"{label}: locating offset",
            isinstance(err.get("offset"), int) and err["offset"] >= 0,
            f"got {err.get('offset')}",
        )
        suite.check(f"{label}: no partial timeline", "events" not in body)

    # -- size limit ---------------------------------------------------------
    status, body = post(b"\x00" * ((1 << 20) + 1))
    suite.check("oversize body: HTTP 413", status == 413, f"got {status}")
    suite.check(
        "oversize body: code",
        body.get("error", {}).get("code") == "payload_too_large",
        f"got {body}",
    )

    return not suite.failures


def main():
    print(f"verify: target app at {APP_URL}", flush=True)
    steps = [
        ("code tests", step_unit_tests),
        ("build check", step_build_check),
        ("app health", step_wait_for_app),
        ("api smoke", step_api_smoke),
    ]
    failed = []
    for name, fn in steps:
        try:
            ok = fn()
        except Exception as exc:  # pragma: no cover - defensive
            print(f"      step raised: {exc!r}", file=sys.stderr)
            ok = False
        if not ok:
            failed.append(name)
    if failed:
        print(f"VERIFY FAILED: {', '.join(failed)}", flush=True)
        return 1
    print("VERIFY PASSED", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
