#!/usr/bin/env python3
"""Platform-specific support for ytarchive (macOS, Windows, Linux).

Everything that differs between operating systems lives here: file locking,
process liveness, scheduling, detached spawning, filename rules, and revealing
files in the desktop file manager. The rest of the app stays platform-neutral.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path, PureWindowsPath

IS_WINDOWS = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")


def _detect_container() -> bool:
    """Whether this is running inside a container.

    Matters because none of the host schedulers exist in one, and paths like
    "reveal in file manager" are meaningless.
    """
    if os.environ.get("YTARCHIVE_CONTAINER"):
        return True
    if os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv"):
        return True
    try:
        with open("/proc/1/cgroup", encoding="utf-8", errors="replace") as fh:
            blob = fh.read()
        return any(m in blob for m in ("docker", "containerd", "kubepods", "lxc"))
    except OSError:
        return False


IN_CONTAINER = _detect_container()

TASK_NAME = "ytarchive"
AGENT_LABEL = "com.ytarchive.agent"

if IS_WINDOWS:
    import ctypes
    import msvcrt
else:
    import fcntl


# --------------------------------------------------------------------------
# paths
# --------------------------------------------------------------------------


def default_download_dir() -> str:
    """Where archived videos go by default, per platform convention."""
    if IS_WINDOWS:
        return str(Path("~/Videos/YouTubeArchive"))
    if IS_MAC:
        return "~/Movies/YouTubeArchive"
    return "~/Videos/YouTubeArchive"


def extra_path_dirs() -> str:
    """Directories to prepend to PATH for spawned processes.

    Scheduled tasks start with a minimal environment, so the tools installed by
    the usual package managers have to be found explicitly.
    """
    if IS_WINDOWS:
        parts = [
            # pip installs yt-dlp.exe beside the interpreter, which is by far
            # the most common way it ends up on a Windows machine.
            str(Path(sys.executable).parent / "Scripts"),
            str(Path(sys.executable).parent),
        ]
        local = os.environ.get("LOCALAPPDATA")
        if local:
            parts += [os.path.join(local, "Microsoft", "WindowsApps"),
                      os.path.join(local, "Programs", "Python", "Scripts")]
        appdata = os.environ.get("APPDATA")
        if appdata:
            parts.append(os.path.join(appdata, "Python", "Scripts"))
        parts.append(r"C:\ProgramData\chocolatey\bin")
        return ";".join(dict.fromkeys(p for p in parts if p))   # keep order, drop dupes
    return "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def with_tool_path(env: "dict | None" = None) -> dict:
    """Copy of the environment with the package-manager directories on PATH."""
    env = dict(env or os.environ)
    sep = ";" if IS_WINDOWS else ":"
    env["PATH"] = extra_path_dirs() + sep + env.get("PATH", "")
    return env


# --------------------------------------------------------------------------
# filenames
# --------------------------------------------------------------------------

# Windows rejects these characters outright, and reserves these device names
# regardless of extension. macOS and Linux only really object to "/".
WINDOWS_ILLEGAL = r'<>:"/\|?*'
WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def safe_folder(name: str) -> str:
    """Filesystem-safe folder name for a source, kept readable.

    Uses the strictest rules on every platform so an archive folder copied from
    a Mac to Windows keeps working.
    """
    name = (name or "").strip()
    # Full-width lookalikes keep titles readable instead of gutting them.
    for bad, good in (("/", "／"), ("\\", "＼"), (":", "-"), ("|", "｜"),
                      ("<", "＜"), (">", "＞"), ('"', "'"), ("?", "？"),
                      ("*", "＊")):
        name = name.replace(bad, good)
    name = re.sub(r"[\x00-\x1f\x7f]", "", name)
    name = name.strip(". ")                     # Windows drops these anyway
    if name.split(".")[0].upper() in WINDOWS_RESERVED:
        name = "_" + name
    return name[:120] or "Unsorted"


def filename_args() -> "list[str]":
    """Extra yt-dlp arguments for platform filename rules."""
    if IS_WINDOWS:
        # Sanitise titles to the Windows character set, and keep total path
        # length under control since MAX_PATH is 260 unless long paths are on.
        return ["--windows-filenames", "--trim-filenames", "200"]
    return []


# --------------------------------------------------------------------------
# process liveness
# --------------------------------------------------------------------------


def pid_alive(pid: int) -> bool:
    """Whether a process is still running.

    os.kill(pid, 0) is the usual Unix probe, but on Windows Python maps os.kill
    onto TerminateProcess - it would kill the very process being checked - so
    Windows has to go through OpenProcess instead.
    """
    if not pid:
        return False
    if IS_WINDOWS:
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return True
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------
# single-run lock
# --------------------------------------------------------------------------


class RunLock:
    """Advisory exclusive lock so overlapping runs cannot collide.

    fcntl.flock on Unix, msvcrt.locking on Windows. Both release automatically
    if the process dies, which a plain PID file would not.
    """

    def __init__(self, path: Path):
        self.path = path
        self.fh = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fh = open(self.path, "a+")
        except OSError:
            return False
        try:
            if IS_WINDOWS:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        try:
            fh.seek(0)
            fh.truncate()
            fh.write(str(os.getpid()))
            fh.flush()
        except OSError:
            pass
        self.fh = fh
        return True

    def release(self) -> None:
        if not self.fh:
            return
        try:
            if IS_WINDOWS:
                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(self.fh, fcntl.LOCK_UN)
        except OSError:
            pass
        self.fh.close()
        self.fh = None

    @staticmethod
    def is_held(path: Path) -> bool:
        """True if some other process currently holds the lock."""
        if not path.exists():
            return False
        probe = RunLock(path)
        if probe.acquire():
            probe.release()
            return False
        return True


# --------------------------------------------------------------------------
# terminal
# --------------------------------------------------------------------------


def enable_ansi() -> bool:
    """Turn on VT escape handling so the progress bar renders on Windows."""
    if not IS_WINDOWS:
        return True
    ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)      # STD_OUTPUT_HANDLE
        mode = ctypes.c_ulong()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(
            handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING))
    except (OSError, AttributeError):
        return False


# --------------------------------------------------------------------------
# subprocesses
# --------------------------------------------------------------------------


def no_window_kwargs() -> dict:
    """Keep console windows from flashing for each child process on Windows."""
    if IS_WINDOWS:
        return {"creationflags": 0x08000000}      # CREATE_NO_WINDOW
    return {}


def spawn_detached(cmd: "list[str]", cwd: str) -> None:
    """Start a process that outlives its parent."""
    kwargs = {
        "cwd": cwd,
        "env": with_tool_path(),
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "stdin": subprocess.DEVNULL,
    }
    if IS_WINDOWS:
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        CREATE_NO_WINDOW = 0x08000000
        kwargs["creationflags"] = (DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
                                   | CREATE_NO_WINDOW)
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(cmd, **kwargs)


def reveal(path: str, select: bool = True) -> None:
    """Show a file in the desktop file manager."""
    target = Path(path)
    try:
        if IS_WINDOWS:
            if select and target.is_file():
                # explorer returns 1 even on success, so the result is ignored.
                subprocess.run(["explorer", "/select,", os.path.normpath(str(target))])
            else:
                os.startfile(os.path.normpath(str(target)))   # noqa: S606
        elif IS_MAC:
            subprocess.run(["open", "-R", str(target)] if select
                           else ["open", str(target)], capture_output=True)
        else:
            folder = str(target.parent if target.is_file() else target)
            subprocess.run(["xdg-open", folder], capture_output=True)
    except OSError:
        pass


# --------------------------------------------------------------------------
# scheduling
# --------------------------------------------------------------------------


def gui_python() -> str:
    """Interpreter to use for scheduled runs - windowless where available."""
    if IS_WINDOWS:
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        if pythonw.exists():
            return str(pythonw)
    return sys.executable


# Scheduled runs must not name one particular Python installation. A schedule
# that pointed at /opt/homebrew/opt/python@3.14/bin/python3.14 stopped working
# the moment Homebrew moved to 3.15 and deleted 3.14 - and Homebrew does exactly
# that when it upgrades yt-dlp, which ytarchive now does automatically. So the
# interpreter is looked up afresh on every run instead.
UNIX_LAUNCH = ["/usr/bin/env", "python3"]


def windows_scheduled_python() -> str:
    """pyw.exe, the windowless Python launcher, always runs the newest installed
    Python 3, so it survives Python upgrades. pythonw.exe beside the current
    interpreter is the fallback when the launcher is not installed."""
    return shutil.which("pyw") or gui_python()


def schedule_paths(app_dir: Path) -> dict:
    return {
        "plist": Path.home() / "Library" / "LaunchAgents" / f"{AGENT_LABEL}.plist",
        "script": app_dir / "ytarchive.py",
    }


def schedule_installed(app_dir: Path) -> bool:
    if IN_CONTAINER:
        return False          # the container runs its own interval loop
    if IS_WINDOWS:
        res = subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME],
                             capture_output=True, text=True, **no_window_kwargs())
        return res.returncode == 0
    if IS_MAC:
        return schedule_paths(app_dir)["plist"].exists()
    return _systemd_timer_path().exists()


def scheduled_script_path(app_dir: Path) -> "str | None":
    """Path to the script the installed schedule actually runs, if any.

    Schedulers store an absolute path, so moving the app folder leaves the
    schedule pointing at a location that no longer exists - and it fails
    silently, in the background, exactly where nobody looks.
    """
    if not schedule_installed(app_dir):
        return None
    try:
        if IS_WINDOWS:
            # The task's XML rather than the "Task To Run" list field: that
            # field holds the whole command line, not the script path, and its
            # label is translated on non-English Windows.
            res = subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME, "/XML"],
                                 capture_output=True, **no_window_kwargs())
            raw = res.stdout or b""
            text = raw.decode("utf-16", "replace") if b"\x00" in raw[:200] \
                else raw.decode("utf-8", "replace")
            return _script_in_command(text)
        if IS_MAC:
            import plistlib
            with open(schedule_paths(app_dir)["plist"], "rb") as fh:
                args = plistlib.load(fh).get("ProgramArguments", [])
            for arg in args:
                if arg.endswith("ytarchive.py"):
                    return arg
            return None
        service = _systemd_dir() / "ytarchive.service"
        for line in service.read_text(encoding="utf-8").splitlines():
            if line.startswith("ExecStart="):
                for token in line.split():
                    if token.endswith("ytarchive.py"):
                        return token
    except (OSError, ValueError, KeyError):
        return None
    return None


def _script_in_command(task_xml: str) -> "str | None":
    """The ytarchive.py path inside a Windows task's <Command>/<Arguments>."""
    import html
    parts = re.findall(r"<(?:Command|Arguments)>(.*?)</(?:Command|Arguments)>",
                       task_xml, flags=re.S)
    line = html.unescape(" ".join(parts))
    m = re.search(r'"([^"]*ytarchive\.py)"', line) or \
        re.search(r"(\S*ytarchive\.py)", line)
    return m.group(1) if m else None


def scheduled_interpreter(app_dir: Path) -> "str | None":
    """The interpreter an installed schedule starts, or None if it is looked
    up at run time (env, or the Windows launcher) and so cannot go stale."""
    if not schedule_installed(app_dir):
        return None
    try:
        if IS_WINDOWS:
            res = subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME, "/XML"],
                                 capture_output=True, **no_window_kwargs())
            raw = res.stdout or b""
            text = raw.decode("utf-16", "replace") if b"\x00" in raw[:200] \
                else raw.decode("utf-8", "replace")
            m = re.search(r"<Command>(.*?)</Command>", text, flags=re.S)
            exe = m.group(1).strip().strip('"') if m else None
            if exe and Path(exe).name.lower() in ("pyw.exe", "py.exe"):
                return None
            return exe
        if IS_MAC:
            import plistlib
            with open(schedule_paths(app_dir)["plist"], "rb") as fh:
                args = plistlib.load(fh).get("ProgramArguments", [])
            exe = args[0] if args else None
        else:
            line = next((ln for ln in (_systemd_dir() / "ytarchive.service")
                         .read_text(encoding="utf-8").splitlines()
                         if ln.startswith("ExecStart=")), "")
            exe = line[len("ExecStart="):].split()[0] if line else None
    except (OSError, ValueError, StopIteration):
        return None
    return None if exe == UNIX_LAUNCH[0] else exe


def schedule_install(app_dir: Path, interval_minutes: int, log_dir: Path) -> str:
    """Register the recurring check. Returns a human-readable description."""
    if IN_CONTAINER:
        raise RuntimeError(
            "inside a container there is no host scheduler to register with. "
            "The container checks on its own interval instead - set the "
            "YTARCHIVE_INTERVAL environment variable (minutes) and restart it.")
    script = str(app_dir / "ytarchive.py")
    log_dir.mkdir(parents=True, exist_ok=True)

    if IS_WINDOWS:
        return _windows_install(script, interval_minutes, app_dir)
    if IS_MAC:
        return _launchd_install(script, interval_minutes, app_dir, log_dir)
    return _systemd_install(script, interval_minutes, app_dir)


def schedule_uninstall(app_dir: Path) -> str:
    if IS_WINDOWS:
        subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
                       capture_output=True, **no_window_kwargs())
        return "scheduled task removed"
    if IS_MAC:
        plist = schedule_paths(app_dir)["plist"]
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{AGENT_LABEL}"],
                       capture_output=True)
        subprocess.run(["launchctl", "unload", "-w", str(plist)], capture_output=True)
        plist.unlink(missing_ok=True)
        return "launchd agent removed"
    return _systemd_uninstall()


def schedule_describe(app_dir: Path) -> str:
    if not schedule_installed(app_dir):
        return "not installed"
    if IS_WINDOWS:
        res = subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "LIST"],
                             capture_output=True, text=True, **no_window_kwargs())
        return res.stdout.strip() or "installed"
    if IS_MAC:
        res = subprocess.run(["launchctl", "list", AGENT_LABEL],
                             capture_output=True, text=True)
        return res.stdout.strip() or "installed"
    res = subprocess.run(["systemctl", "--user", "status", "ytarchive.timer"],
                         capture_output=True, text=True)
    return res.stdout.strip() or "installed"


# -- Windows ---------------------------------------------------------------


def _windows_install(script: str, interval: int, app_dir: Path) -> str:
    # /SC MINUTE with /MO keeps one schedule form for any interval, and the task
    # runs only while the user is logged on, which avoids storing a password.
    command = f'"{windows_scheduled_python()}" "{script}" --quiet run --no-progress'
    args = ["schtasks", "/Create", "/TN", TASK_NAME, "/TR", command,
            "/SC", "MINUTE", "/MO", str(interval), "/F"]
    res = subprocess.run(args, capture_output=True, text=True, **no_window_kwargs())
    if res.returncode != 0:
        raise RuntimeError((res.stderr or res.stdout).strip()
                           or "schtasks refused to create the task")
    return f"Windows scheduled task '{TASK_NAME}' every {interval} minute(s)"


# -- macOS -----------------------------------------------------------------


def _launchd_install(script: str, interval: int, app_dir: Path, log_dir: Path) -> str:
    import plistlib

    plist_path = schedule_paths(app_dir)["plist"]
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist = {
        "Label": AGENT_LABEL,
        # --quiet keeps stdout clean so the scheduler's own log file does not
        # duplicate ytarchive.log line for line. Errors still reach stderr.
        "ProgramArguments": UNIX_LAUNCH + [script, "--quiet", "run", "--no-progress"],
        "WorkingDirectory": str(app_dir),
        "StartInterval": interval * 60,
        "RunAtLoad": False,
        "StandardOutPath": str(log_dir / "schedule.out.log"),
        "StandardErrorPath": str(log_dir / "schedule.err.log"),
        "EnvironmentVariables": {
            "PATH": extra_path_dirs(),
            "LC_ALL": "en_US.UTF-8", "LANG": "en_US.UTF-8",
        },
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "Nice": 5,
    }
    with open(plist_path, "wb") as fh:
        plistlib.dump(plist, fh)

    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{AGENT_LABEL}"],
                   capture_output=True)
    res = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(plist_path)],
                         capture_output=True, text=True)
    if res.returncode != 0:
        res = subprocess.run(["launchctl", "load", "-w", str(plist_path)],
                             capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or "launchctl refused the agent")
    return f"launchd agent every {interval} minute(s)"


# -- Linux -----------------------------------------------------------------


def _systemd_dir() -> Path:
    return Path.home() / ".config" / "systemd" / "user"


def _systemd_timer_path() -> Path:
    return _systemd_dir() / "ytarchive.timer"


def _systemd_install(script: str, interval: int, app_dir: Path) -> str:
    if not shutil.which("systemctl"):
        raise RuntimeError("systemctl not found; add a cron entry instead:\n"
                           f"  */{interval} * * * * {sys.executable} {script} run")
    unit_dir = _systemd_dir()
    unit_dir.mkdir(parents=True, exist_ok=True)
    (unit_dir / "ytarchive.service").write_text(
        "[Unit]\nDescription=ytarchive check\n\n"
        "[Service]\nType=oneshot\n"
        f"WorkingDirectory={app_dir}\n"
        f"Environment=PATH={extra_path_dirs()}\n"
        f"ExecStart={' '.join(UNIX_LAUNCH)} {script} --quiet run --no-progress\n",
        encoding="utf-8")
    (unit_dir / "ytarchive.timer").write_text(
        "[Unit]\nDescription=ytarchive periodic check\n\n"
        f"[Timer]\nOnBootSec=5min\nOnUnitActiveSec={interval}min\nPersistent=true\n\n"
        "[Install]\nWantedBy=timers.target\n",
        encoding="utf-8")
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    res = subprocess.run(["systemctl", "--user", "enable", "--now", "ytarchive.timer"],
                         capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or "systemctl refused the timer")
    return f"systemd user timer every {interval} minute(s)"


def _systemd_uninstall() -> str:
    subprocess.run(["systemctl", "--user", "disable", "--now", "ytarchive.timer"],
                   capture_output=True)
    for name in ("ytarchive.timer", "ytarchive.service"):
        (_systemd_dir() / name).unlink(missing_ok=True)
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    return "systemd timer removed"


# --------------------------------------------------------------------------
# install hints
# --------------------------------------------------------------------------


def install_hint(tool: str = "yt-dlp") -> str:
    if IS_WINDOWS:
        return f"winget install {tool}    (or: pip install -U {tool})"
    if IS_MAC:
        return f"brew install {tool}"
    return f"sudo apt install {tool}    (or: pip install -U {tool})"


def protected_dirs() -> "tuple[str, ...]":
    """Folders the OS shields from background tasks."""
    if IS_MAC:
        return ("Desktop", "Documents", "Downloads")
    return ()


def ytdlp_install_kind(exe: str) -> str:
    """How yt-dlp was installed: homebrew, winget, pip or standalone.

    Judged from where the executable really lives. Order matters: Homebrew's
    yt-dlp is itself a pip script inside a Homebrew-owned environment, so it has
    to be recognised before the generic pip check - updating it with pip would
    leave Homebrew's own records out of step with what is installed.
    """
    real = os.path.realpath(exe)
    low = real.replace("\\", "/").lower()
    if "/cellar/yt-dlp/" in low:
        return "homebrew"
    if "/winget/" in low:
        return "winget"
    if IS_WINDOWS:
        # pip puts a small launcher in the interpreter's Scripts folder; the
        # official yt-dlp.exe is a standalone build that can update itself.
        return "pip" if PureWindowsPath(real).parent.name.lower() == "scripts" \
            else "standalone"
    try:
        size = os.path.getsize(real)
        with open(real, "rb") as fh:
            head = fh.read(2)
    except OSError:
        return "standalone"
    # A pip console script is a few hundred bytes of Python behind a shebang.
    # The official zipapp also starts with a shebang but runs to megabytes.
    return "pip" if head == b"#!" and size < 64 * 1024 else "standalone"


def ytdlp_pip_python(exe: str) -> str:
    """The interpreter a pip-installed yt-dlp belongs to."""
    real = os.path.realpath(exe)
    if IS_WINDOWS:
        candidate = PureWindowsPath(real).parent.parent / "python.exe"
        return str(candidate) if os.path.exists(str(candidate)) else sys.executable
    try:
        with open(real, "rb") as fh:
            first = fh.readline(512).decode("utf-8", "replace")
    except OSError:
        return sys.executable
    parts = first[2:].split() if first.startswith("#!") else []
    if len(parts) >= 2 and parts[0].endswith("/env"):          # #!/usr/bin/env python3
        return shutil.which(parts[1]) or sys.executable
    if parts and os.path.exists(parts[0]):
        return parts[0]
    return sys.executable


def ytdlp_update_commands(exe: str, channel: str = "stable") -> "list[list[str]]":
    """Commands that update yt-dlp the way it was installed.

    pip may decide by itself to install into the user's site-packages when the
    system location is not writable, so no --user handling is needed here.
    """
    kind = ytdlp_install_kind(exe)
    nightly = channel == "nightly"
    if kind == "homebrew":
        # Refresh the formula list first, or brew can believe a stale release
        # is the newest one and report "already installed".
        return [["brew", "update", "--quiet"], ["brew", "upgrade", "yt-dlp"]]
    if kind == "winget":
        return [["winget", "upgrade", "--id", "yt-dlp.yt-dlp", "--exact",
                 "--silent", "--disable-interactivity"]]
    if kind == "pip":
        cmd = [ytdlp_pip_python(exe), "-m", "pip", "install", "--upgrade",
               "--disable-pip-version-check"]
        if nightly:
            cmd.append("--pre")
        # deno is the JavaScript runtime YouTube's challenges need; keeping it
        # in the same install keeps the solver and runtime compatible.
        return [cmd + ["yt-dlp[default,deno]"]]
    return [[exe, "--update-to", "nightly"] if nightly else [exe, "-U"]]


# Output meaning "nothing to do", from tools that exit non-zero when there is
# no newer version (winget does). Treated as success rather than failure.
ALREADY_CURRENT = (
    "already installed", "no available upgrade found",
    "no newer package versions are available", "is up to date",
    "requirement already satisfied",
)


def update_command_hint() -> "list[str]":
    """A sensible ytdlp_update_command for this platform's usual install."""
    if IS_WINDOWS:
        return ["winget", "upgrade", "--id", "yt-dlp.yt-dlp", "--silent"]
    if IS_MAC:
        return ["brew", "upgrade", "yt-dlp"]
    return [sys.executable, "-m", "pip", "install", "-U", "yt-dlp[default,deno]"]


def deno_hint() -> str:
    if IS_WINDOWS:
        return "winget install DenoLand.Deno"
    if IS_MAC:
        return "brew install deno"
    return "pip install -U 'yt-dlp[default,deno]'"
