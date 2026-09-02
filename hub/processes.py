# -*- coding: utf-8 -*-
"""Cross-platform process supervisor.

One place that knows how to start a child, find out whether it is alive, and
stop it together with its children (npx spawns node, node spawns the MCP).
"""

import json
import os
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time

from . import config
from .events import BUS

IS_WINDOWS = os.name == "nt"
_lock = threading.RLock()


def pid_file(name):
    return config.LOGS / ("%s.pid" % name)


def log_file(name):
    return config.LOGS / ("%s.log" % name)


def read_pid(name):
    path = pid_file(name)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return int(data.get("pid")) or None
    except (ValueError, TypeError):
        try:
            return int(path.read_text(encoding="utf-8").strip())  # legacy plain pid
        except ValueError:
            return None


def read_meta(name):
    path = pid_file(name)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}


def alive(pid):
    if not pid:
        return False
    if IS_WINDOWS:
        # 0x100000 = SYNCHRONIZE: enough to learn whether the handle resolves.
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x100000, False, int(pid))
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def port_open(port, host="127.0.0.1", timeout=0.35):
    if not port:
        return False
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def spawn(name, command, cwd=None, env=None):
    """Start a detached child, append its output to logs/<name>.log."""
    with _lock:
        stop(name, quiet=True)
        config.LOGS.mkdir(parents=True, exist_ok=True)
        handle = open(log_file(name), "ab", buffering=0)
        handle.write(("\n=== %s: start %s ===\n%s\n"
                      % (name, time.strftime("%Y-%m-%d %H:%M:%S"), command)).encode("utf-8"))
        popen_kwargs = {
            "cwd": str(cwd or config.ROOT),
            "stdout": handle,
            "stderr": subprocess.STDOUT,
            "stdin": subprocess.DEVNULL,
            "env": {**os.environ, **(env or {})},
        }
        if IS_WINDOWS:
            # shell=True so that `npx ...` resolves through .cmd shims;
            # a new process group lets us kill the whole tree later.
            popen_kwargs["shell"] = True
            popen_kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | 0x08000000)  # NO_WINDOW
            args = command
        else:
            popen_kwargs["start_new_session"] = True
            args = shlex.split(command)
        proc = subprocess.Popen(args, **popen_kwargs)
        pid_file(name).write_text(json.dumps({
            "pid": proc.pid,
            "command": command,
            "startedAt": time.time(),
        }), encoding="utf-8")
        BUS.publish("process.started", {"name": name, "pid": proc.pid})
        return proc.pid


def stop(name, quiet=False):
    """Terminate the child and its subtree. Returns True when something died."""
    with _lock:
        pid = read_pid(name)
        killed = False
        if pid and alive(pid):
            killed = kill_tree(pid)
        try:
            pid_file(name).unlink()
        except OSError:
            pass
        if killed and not quiet:
            BUS.publish("process.stopped", {"name": name, "pid": pid})
        return killed


def kill_tree(pid):
    pid = int(pid)
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, creationflags=0x08000000)
        return True
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except OSError:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return False
    for _ in range(20):  # 2s grace period before SIGKILL
        if not alive(pid):
            return True
        time.sleep(0.1)
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except OSError:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    return True


def status(name, port=None):
    pid = read_pid(name)
    meta = read_meta(name)
    running = alive(pid)
    return {
        "pid": pid if running else None,
        "running": running,
        "listening": port_open(port) if port else None,
        "startedAt": meta.get("startedAt"),
        "command": meta.get("command"),
    }


def tail(path, lines=200, max_bytes=200_000):
    """Last N lines of a log file without reading the whole thing."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as handle:
            if size > max_bytes:
                handle.seek(size - max_bytes)
                handle.readline()
            data = handle.read()
    except OSError:
        return ""
    text = data.decode("utf-8", errors="replace")
    return "\n".join(text.splitlines()[-int(lines):])


def which(binary):
    """shutil.which plus the Windows .cmd/.bat shims npm installs."""
    import shutil
    found = shutil.which(binary)
    if found:
        return found
    if IS_WINDOWS:
        for suffix in (".cmd", ".exe", ".bat"):
            found = shutil.which(binary + suffix)
            if found:
                return found
    return None


def run(command, timeout=25, cwd=None):
    """Run a short command and capture its output (used by the installer)."""
    try:
        proc = subprocess.run(
            command if IS_WINDOWS else shlex.split(command) if isinstance(command, str) else command,
            shell=IS_WINDOWS and isinstance(command, str),
            capture_output=True, text=True, timeout=timeout,
            cwd=str(cwd or config.ROOT),
            creationflags=0x08000000 if IS_WINDOWS else 0,
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "Таймаут команды"
    except OSError as exc:
        return 127, str(exc)


def self_python():
    return sys.executable or "python"
