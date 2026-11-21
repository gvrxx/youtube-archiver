#!/usr/bin/env python3
"""Exercise ytarchive's platform code on the machine it runs on.

Run by CI on real Windows and macOS runners, so the OS-specific paths -
file locking, process checks, the scheduler, the launchers - execute for real
rather than being simulated. No YouTube traffic: CI machines sit on datacenter
IPs that YouTube bot-checks, so a download result there would mean nothing.

    python tests/native_check.py
"""

from __future__ import annotations

import json
import os
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
DATA = Path(tempfile.mkdtemp(prefix="yta-data-"))
DOWNLOADS = Path(tempfile.mkdtemp(prefix="yta-dl-"))
os.environ["YTARCHIVE_DATA"] = str(DATA)
os.environ["YTARCHIVE_DOWNLOADS"] = str(DOWNLOADS)
sys.path.insert(0, str(APP))

import platforms as plat      # noqa: E402
import ytarchive as ya        # noqa: E402

failures: "list[str]" = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"\n          {detail}" if detail else ""))
    if not ok:
        failures.append(label)


def skip(label: str, why: str) -> None:
    print(f"  SKIP  {label}\n          {why}")


def app(*args: str, timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(APP / "ytarchive.py"), *args],
                          capture_output=True, text=True, timeout=timeout,
                          errors="replace")


def wait_for(url: str, seconds: int = 30) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                return r.status == 200
        except OSError:
            time.sleep(0.5)
    return False


def kill_tree(proc: subprocess.Popen) -> None:
    # A launcher starts Python as its own child. Stopping just the launcher
    # would leave the server running and holding the port.
    if plat.IS_WINDOWS:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                       capture_output=True)
    else:
        # The launcher was started in its own process group, so this stops
        # the Python it started as well - terminating only the launcher left
        # the server running.
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except OSError:
            proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()


print(f"platform: {sys.platform}   python: {sys.version.split()[0]}")
print(f"windows={plat.IS_WINDOWS} mac={plat.IS_MAC} container={plat.IN_CONTAINER}")

print("\n== basics")
res = app("status")
check("status runs", res.returncode == 0, res.stderr.strip()[-300:])
check("config written to the data folder", (DATA / "config.json").exists())

print("\n== process checks")
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
time.sleep(1)
check("a running process is reported alive", plat.pid_alive(child.pid))
# On Windows os.kill(pid, 0) would terminate the process; the check must not.
time.sleep(0.5)
check("checking it did not kill it", child.poll() is None)
child.terminate()
child.wait(timeout=15)
check("a finished process is reported dead", not plat.pid_alive(child.pid))

print("\n== run lock")
lock = plat.RunLock(DATA / ".run.lock")
check("lock acquired", lock.acquire())
probe = ("import sys, pathlib; sys.path.insert(0, sys.argv[1]); import platforms as p;"
         "print(p.RunLock.is_held(pathlib.Path(sys.argv[2])))")
held = subprocess.run([sys.executable, "-c", probe, str(APP), str(DATA / ".run.lock")],
                      capture_output=True, text=True).stdout.strip()
check("another process sees it held", held == "True", held)
lock.release()
held = subprocess.run([sys.executable, "-c", probe, str(APP), str(DATA / ".run.lock")],
                      capture_output=True, text=True).stdout.strip()
check("released lock is free again", held == "False", held)

print("\n== filenames")
name = plat.safe_folder('CON')
try:
    (DOWNLOADS / name).mkdir()
    (DOWNLOADS / plat.safe_folder('a<b>c:d"e?f*g|h')).mkdir()
    check(f"reserved and illegal names become creatable folders ({name})", True)
except OSError as exc:
    check("reserved and illegal names become creatable folders", False, str(exc))

print("\n== yt-dlp and its updater")
exe = shutil.which("yt-dlp", path=plat.with_tool_path()["PATH"])
check("yt-dlp found on PATH", bool(exe), str(exe))
if exe:
    kind = plat.ytdlp_install_kind(exe)
    if os.environ.get("CI"):          # CI installs yt-dlp with pip
        check("install method detected as pip", kind == "pip", kind)
    else:
        print(f"  INFO  install method on this machine: {kind}")
    check("deno found", bool(ya.js_runtime()), str(ya.js_runtime()))
    res = app("update-ytdlp", timeout=600)
    out = (res.stdout + res.stderr).strip()
    check("update-ytdlp succeeds", res.returncode == 0 and "failed" not in out, out[-400:])
    info = ya.ytdlp_update_info(ya.load_config())
    check("update check recorded", bool(info["checked_at"]), json.dumps(info))

print("\n== scheduler")
# Installing and removing the schedule uses the same task name as a real
# install, so on anyone's own machine this would delete their schedule. Only
# throwaway CI machines run it.
IN_CI = bool(os.environ.get("CI"))
res = app("schedule", "install", "--interval", "60") if IN_CI else None
if not IN_CI:
    skip("scheduler", "only runs in CI - it would replace this machine's real schedule")
elif res.returncode != 0 and plat.IS_MAC and "bootstrap" in (res.stdout + res.stderr).lower():
    skip("schedule install", "no logged-in GUI session on this runner for launchd")
else:
    check("schedule install", res.returncode == 0, (res.stdout + res.stderr).strip()[-300:])
    check("schedule reported installed", plat.schedule_installed(APP))
    target = plat.scheduled_script_path(APP)
    check("schedule points at this checkout", bool(target) and
          Path(target).resolve() == (APP / "ytarchive.py").resolve(), str(target))
    check("not reported as stale", ya.stale_schedule_target() is None,
          str(ya.stale_schedule_target()))
    # A schedule naming one Python install breaks when that install is
    # upgraded away - Homebrew did exactly that, and the schedule stopped.
    exe = plat.scheduled_interpreter(APP)
    check("schedule finds Python at run time, not one fixed install",
          exe is None or (plat.IS_WINDOWS and os.path.exists(exe)), str(exe))
    check("schedule reported healthy", ya.schedule_problem() is None,
          str(ya.schedule_problem()))
    res = app("schedule", "uninstall")
    check("schedule uninstall", res.returncode == 0 and not plat.schedule_installed(APP),
          (res.stdout + res.stderr).strip()[-300:])

print("\n== broken-schedule detection")
# Pure logic: pretend the schedule names an interpreter that has been removed.
real_interp, real_stale = plat.scheduled_interpreter, ya.stale_schedule_target
plat.scheduled_interpreter = lambda app_dir: "/gone/python@3.14/bin/python3.14"
ya.stale_schedule_target = lambda: None
problem = ya.schedule_problem()
check("a removed Python is reported as a broken schedule",
      bool(problem) and "no longer exists" in problem, str(problem))
plat.scheduled_interpreter, ya.stale_schedule_target = real_interp, real_stale

print("\n== web interface")
server = subprocess.Popen([sys.executable, str(APP / "ytarchive.py"), "ui", "--no-open",
                           "--port", "8899"], stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True)
up = wait_for("http://127.0.0.1:8899/api/state")
check("interface serves", up)
if up:
    with urllib.request.urlopen("http://127.0.0.1:8899/api/state", timeout=5) as r:
        state = json.load(r)
    check("state reports yt-dlp and its update method",
          bool(state.get("ytdlp", {}).get("installed")), json.dumps(state.get("ytdlp")))
kill_tree(server)

print("\n== launcher")
launcher = APP / ("start.bat" if plat.IS_WINDOWS else "start.command")
if plat.IS_WINDOWS:
    raw = launcher.read_bytes()
    check("start.bat checked out with CRLF line endings", b"\r\n" in raw)
env = dict(os.environ, BROWSER="true" if not plat.IS_WINDOWS else "")
# Started from somewhere else, as a double-click would be.
proc = subprocess.Popen(["cmd", "/c", str(launcher)] if plat.IS_WINDOWS else [str(launcher)],
                        cwd=tempfile.gettempdir(), env=env,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                        start_new_session=not plat.IS_WINDOWS)
up = wait_for("http://127.0.0.1:8800/api/state", 45)
check(f"{launcher.name} starts the interface", up)
kill_tree(proc)

print()
if failures:
    print(f"{len(failures)} CHECK(S) FAILED: {failures}")
    sys.exit(1)
print("ALL CHECKS PASSED")
