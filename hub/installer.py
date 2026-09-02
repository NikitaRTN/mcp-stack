# -*- coding: utf-8 -*-
"""Dependency detection and browser-driven installation.

The repository ships no binaries. Everything the app needs is detected at
runtime and installed from the panel with a live progress log:

  * caddy   - downloaded from the official GitHub release for this platform,
              checksum-verified, unpacked into ./bin
  * node    - detected; on Windows installed through winget when available,
              otherwise the panel shows the download link
  * MCP pkg - warmed up through `npm exec` so the first tool call is not slow;
              supergateway and Desktop Commander are managed independently
"""

import hashlib
import io
import json
import os
import platform
import re
import shutil
import tarfile
import threading
import time
import urllib.error
import urllib.request
import zipfile

from . import config
from . import processes
from .events import BUS

CADDY_API = "https://api.github.com/repos/caddyserver/caddy/releases/latest"
USER_AGENT = "MCP-Hub-Installer"
JOBS = {}
_jobs_lock = threading.Lock()


# --------------------------------------------------------------------------- #
#  detection                                                                  #
# --------------------------------------------------------------------------- #

def caddy_path():
    name = "caddy.exe" if processes.IS_WINDOWS else "caddy"
    local = config.BIN / name
    if local.exists():
        return local
    found = processes.which("caddy")
    return None if found is None else type(local)(found)


def _version(command):
    code, out = processes.run(command, timeout=20)
    if code != 0:
        return None
    first = (out or "").strip().splitlines()
    return first[0].strip() if first else None


def detect():
    """Describe every component the app depends on."""
    items = []

    caddy = caddy_path()
    items.append({
        "id": "caddy",
        "name": "Caddy",
        "purpose": "HTTPS, единый порт и автоматический сертификат",
        "required": True,
        "found": caddy is not None,
        "path": str(caddy) if caddy else None,
        "version": _version('"%s" version' % caddy) if caddy else None,
        "installable": True,
        "sizeHint": "~45 МБ, скачивается один раз",
        "group": "network",
        "dependsOn": [],
        "detail": "Публикует MCP-маршруты через один порт и выпускает HTTPS-сертификаты.",
    })

    node = processes.which("node")
    items.append({
        "id": "node",
        "name": "Node.js",
        "purpose": "среда выполнения локальных MCP-пакетов",
        "required": False,
        "found": node is not None,
        "path": node,
        "version": _version("node --version") if node else None,
        "installable": processes.IS_WINDOWS,
        "downloadUrl": "https://nodejs.org/en/download",
        "sizeHint": "~30 МБ",
        "group": "runtime",
        "dependsOn": [],
        "detail": "Нужен для Desktop Commander, supergateway и других MCP, запускаемых через npm.",
    })

    npx = processes.which("npx")
    items.append({
        "id": "npx",
        "name": "npx",
        "purpose": "запуск MCP-пакетов без глобальной установки",
        "required": False,
        "found": npx is not None,
        "path": npx,
        "version": _version("npx --version") if npx else None,
        "installable": False,
        "note": "устанавливается вместе с Node.js",
        "sizeHint": "в составе Node.js",
        "group": "runtime",
        "dependsOn": ["node"],
        "providedBy": "node",
        "detail": "Входит в Node.js и отдельно не устанавливается.",
    })

    supergateway = _npm_package_info("supergateway")
    items.append({
        "id": "supergateway",
        "name": "supergateway",
        "purpose": "превращает stdio-MCP в Streamable HTTP",
        "required": False,
        "found": supergateway is not None,
        "path": supergateway.get("path") if supergateway else None,
        "version": supergateway.get("version") if supergateway else None,
        "installable": npx is not None,
        "sizeHint": "~8 МБ в кеше npm",
        "group": "bridge",
        "dependsOn": ["node", "npx"],
        "detail": "Мост между локальным stdio-процессом и потоковым HTTP-интерфейсом MCP Hub.",
    })

    desktop = _npm_package_info("@wonderwhy-er/desktop-commander")
    items.append({
        "id": "desktop-commander",
        "name": "Desktop Commander",
        "purpose": "файлы, shell и процессы на этом компьютере",
        "required": False,
        "found": desktop is not None,
        "path": desktop.get("path") if desktop else None,
        "version": desktop.get("version") if desktop else None,
        "installable": npx is not None,
        "downloadUrl": "https://www.npmjs.com/package/@wonderwhy-er/desktop-commander",
        "sizeHint": "скачивается в кеш npm",
        "group": "mcp",
        "dependsOn": ["node", "npx", "supergateway"],
        "detail": "Локальный MCP-сервер. Устанавливается отдельно и остаётся выключенным до включения сервиса.",
    })
    return items


def _npm_roots():
    """Known npm locations without recursively scanning the whole home folder."""
    home = os.path.expanduser("~")
    roots = [
        os.path.join(home, ".npm", "_npx"),
        os.path.join(os.environ.get("LOCALAPPDATA", home), "npm-cache", "_npx"),
    ]
    if processes.which("npm"):
        code, out = processes.run("npm root -g", timeout=8)
        if code == 0 and (out or "").strip():
            roots.append((out or "").strip().splitlines()[0])
    seen = set()
    for root in roots:
        key = os.path.normcase(os.path.abspath(root))
        if key not in seen:
            seen.add(key)
            yield root


def _read_npm_package(folder, package):
    manifest = os.path.join(folder, "package.json")
    try:
        with open(manifest, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    if data.get("name") != package:
        return None
    return {"path": folder, "version": data.get("version")}


def _npm_package_info(package):
    """Return path/version for a package in the npx cache or global npm root."""
    parts = package.split("/")
    for base in _npm_roots():
        if not os.path.isdir(base):
            continue
        direct = os.path.join(base, *parts)
        found = _read_npm_package(direct, package)
        if found:
            return found
        try:
            children = list(os.scandir(base))
        except OSError:
            children = []
        for child in children:
            try:
                is_dir = child.is_dir()
            except OSError:
                is_dir = False
            if not is_dir:
                continue
            folder = os.path.join(child.path, "node_modules", *parts)
            found = _read_npm_package(folder, package)
            if found:
                return found
    return None


def _npm_cached(package):
    return _npm_package_info(package) is not None


def summary():
    items = detect()
    missing = [i for i in items if i["required"] and not i["found"]]
    return {"components": items, "ready": not missing,
            "missing": [i["id"] for i in missing]}


# --------------------------------------------------------------------------- #
#  jobs                                                                       #
# --------------------------------------------------------------------------- #

class Job:
    """A background installation with a streamed log and live progress state."""

    def __init__(self, component):
        self.id = "%s-%d" % (component, int(time.time() * 1000))
        self.component = component
        self.status = "running"
        self.percent = 0
        self.lines = []
        self.started = time.time()
        self.updated = self.started
        self.finished = None
        self.detail = "Подготовка установки…"
        self.phase = "starting"
        self.indeterminate = True
        self.downloaded_bytes = 0
        self.total_bytes = 0
        self.speed_bps = 0

    def _apply(self, percent=None, detail=None, phase=None, indeterminate=None,
               downloadedBytes=None, totalBytes=None, speedBps=None):
        if percent is not None:
            self.percent = max(0, min(100, int(percent)))
        if detail is not None:
            self.detail = str(detail)
        if phase is not None:
            self.phase = str(phase)
        if indeterminate is not None:
            self.indeterminate = bool(indeterminate)
        if downloadedBytes is not None:
            self.downloaded_bytes = max(0, int(downloadedBytes))
        if totalBytes is not None:
            self.total_bytes = max(0, int(totalBytes))
        if speedBps is not None:
            self.speed_bps = max(0, int(speedBps))
        self.updated = time.time()

    def _state(self):
        ended = self.finished or time.time()
        return {
            "jobId": self.id,
            "component": self.component,
            "status": self.status,
            "percent": self.percent,
            "detail": self.detail,
            "phase": self.phase,
            "indeterminate": self.indeterminate,
            "downloadedBytes": self.downloaded_bytes,
            "totalBytes": self.total_bytes,
            "speedBps": self.speed_bps,
            "elapsedSec": max(0, int(ended - self.started)),
            "startedAt": self.started,
            "updatedAt": self.updated,
            "finishedAt": self.finished,
        }

    def log(self, text, percent=None, **progress):
        detail = progress.pop("detail", text)
        self._apply(percent=percent, detail=detail, **progress)
        stamp = time.strftime("%H:%M:%S")
        line = "[%s] %s" % (stamp, text)
        self.lines.append(line)
        del self.lines[:-400]
        payload = self._state()
        payload["line"] = line
        BUS.publish("install.progress", payload)

    def pulse(self, detail, percent=None, **progress):
        """Publish visible activity without adding heartbeat noise to the log."""
        self._apply(percent=percent, detail=detail, **progress)
        payload = self._state()
        payload["line"] = None
        BUS.publish("install.progress", payload)

    def done(self, ok, message):
        self.status = "ok" if ok else "error"
        self.percent = 100 if ok else self.percent
        self.finished = time.time()
        self.log(message, self.percent, detail=message, phase="done",
                 indeterminate=False, speedBps=0)
        payload = self._state()
        payload.update({"message": message, "components": detect()})
        BUS.publish("install.finished", payload)

    def snapshot(self):
        state = self._state()
        state["lines"] = self.lines[-200:]
        return state


def start_job(component):
    handlers = {
        "caddy": _install_caddy,
        "node": _install_node,
        "supergateway": _warm_supergateway,
        "desktop-commander": _warm_desktop_commander,
    }
    handler = handlers.get(component)
    if handler is None:
        raise ValueError("Нет автоматического установщика для %s" % component)
    job = Job(component)
    with _jobs_lock:
        JOBS[job.id] = job
        for old_id, old in list(JOBS.items()):   # keep the map small
            if old.finished and time.time() - old.finished > 3600:
                JOBS.pop(old_id, None)

    def runner():
        try:
            handler(job)
        except Exception as exc:                 # noqa: BLE001
            job.done(False, "Ошибка: %s" % exc)

    threading.Thread(target=runner, name="install-%s" % component, daemon=True).start()
    return job


def job(job_id):
    with _jobs_lock:
        found = JOBS.get(job_id)
    return found.snapshot() if found else None


# --------------------------------------------------------------------------- #
#  caddy                                                                      #
# --------------------------------------------------------------------------- #

def _platform_keys():
    machine = platform.machine().lower()
    if machine in ("amd64", "x86_64"):
        arch = "amd64"
    elif machine in ("arm64", "aarch64"):
        arch = "arm64"
    elif machine.startswith("armv7"):
        arch = "armv7"
    else:
        arch = machine
    system = {"windows": "windows", "darwin": "mac", "linux": "linux"}.get(
        platform.system().lower(), platform.system().lower())
    return system, arch


def _fetch(url, timeout=30):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                  "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _pick_asset(assets, system, arch):
    suffix = ".zip" if system == "windows" else ".tar.gz"
    wanted = re.compile(r"caddy_.*_%s_%s%s$" % (system, arch, re.escape(suffix)))
    for asset in assets:
        if wanted.match(asset.get("name", "")):
            return asset
    return None


def _install_caddy(job):
    system, arch = _platform_keys()
    job.log("Платформа: %s/%s" % (system, arch), 3)
    job.log("Запрашиваю последний релиз Caddy…", 6)
    try:
        release = json.loads(_fetch(CADDY_API))
    except urllib.error.URLError as exc:
        return job.done(False, "Нет доступа к GitHub: %s" % exc)

    assets = release.get("assets", [])
    asset = _pick_asset(assets, system, arch)
    if asset is None:
        return job.done(False, "Для %s/%s готового бинарника нет" % (system, arch))

    checksums = next((a for a in assets if a.get("name", "").endswith("checksums.txt")), None)
    expected = None
    algorithm = None
    if checksums:
        try:
            text = _fetch(checksums["browser_download_url"]).decode("utf-8", "replace")
            expected, algorithm = _parse_checksums(text, asset["name"])
            if expected is None:
                job.log("В файле контрольных сумм нет строки для %s" % asset["name"])
            elif algorithm is None:
                job.log("Неизвестная длина хеша (%d символов) — проверка пропущена"
                        % len(expected))
        except urllib.error.URLError:
            job.log("Не удалось скачать файл контрольных сумм")

    job.log("%s (%s) — %.1f МБ" % (release.get("tag_name", "?"), asset["name"],
                                     asset.get("size", 0) / 1048576.0), 10)
    blob, digests = _download(job, asset["browser_download_url"], asset.get("size", 0))
    if expected and algorithm:
        actual = digests[algorithm]
        if actual != expected:
            return job.done(False, (
                "Контрольная сумма не совпала — файл не установлен. "
                "%s ожидался %s, получен %s (скачано %d байт)"
            ) % (algorithm.upper(), expected[:16] + "…", actual[:16] + "…", len(blob)))
        job.log("%s проверена: совпадает" % algorithm.upper(), 88)
    else:
        job.log("Проверка пропущена. SHA256 файла: %s" % digests["sha256"], 88)

    config.BIN.mkdir(parents=True, exist_ok=True)
    name = "caddy.exe" if system == "windows" else "caddy"
    target = config.BIN / name
    job.log("Распаковываю в %s" % target, 92)
    try:
        _extract(blob, name, target, system)
    except KeyError:
        return job.done(False, "В архиве нет исполняемого файла caddy")
    if not processes.IS_WINDOWS:
        os.chmod(target, 0o755)

    version = _version('"%s" version' % target)
    if not version:
        return job.done(False, "Файл скачан, но не запускается")
    job.done(True, "Готово: %s" % version)


def _parse_checksums(text, asset_name):
    """Find the hash for asset_name and detect which algorithm produced it.

    Release checksum files are not all sha256: Caddy publishes sha512 digests,
    other projects use sha256 or sha1. The hex length identifies the algorithm,
    so the digest is compared with the matching hash instead of assuming one.
    """
    by_length = {40: "sha1", 64: "sha256", 96: "sha384", 128: "sha512"}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        name = parts[-1].lstrip("*").rsplit("/", 1)[-1]
        if name != asset_name:
            continue
        digest = parts[0].strip().lower()
        if not re.fullmatch(r"[0-9a-f]+", digest):
            continue
        return digest, by_length.get(len(digest))
    return None, None


def _download(job, url, expected_size=0):
    hashes = {name: hashlib.new(name) for name in ("sha1", "sha256", "sha384", "sha512")}
    buffer = io.BytesIO()
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    last_report = 0.0
    started = time.time()
    job.pulse("Подключаюсь к серверу загрузки…", 10, phase="download", indeterminate=True)
    with urllib.request.urlopen(request, timeout=60) as response:
        total = int(response.headers.get("Content-Length") or expected_size or 0)
        read = 0
        while True:
            chunk = response.read(262144)
            if not chunk:
                break
            buffer.write(chunk)
            for digest in hashes.values():
                digest.update(chunk)
            read += len(chunk)
            now = time.time()
            if now - last_report > 0.4:      # keep the SSE stream readable
                last_report = now
                percent = 10 + (read * 75 // total) if total else 40
                elapsed = max(0.001, now - started)
                speed = int(read / elapsed)
                text = "Скачано %.1f / %.1f МБ" % (
                    read / 1048576.0, (total or read) / 1048576.0)
                job.log(text, percent, detail="Скачивание файла…", phase="download",
                        indeterminate=not bool(total), downloadedBytes=read,
                        totalBytes=total, speedBps=speed)
    blob = buffer.getvalue()
    if total and len(blob) != total:
        raise IOError("Загрузка обрывалась: получено %d из %d байт" % (len(blob), total))
    elapsed = max(0.001, time.time() - started)
    job.log("Загрузка завершена: %.1f МБ" % (len(blob) / 1048576.0), 85,
            detail="Загрузка завершена, проверяю файл…", phase="verify",
            indeterminate=False, downloadedBytes=len(blob), totalBytes=total or len(blob),
            speedBps=int(len(blob) / elapsed))
    return blob, {name: h.hexdigest() for name, h in hashes.items()}


def _extract(blob, member_name, target, system):
    stream = io.BytesIO(blob)
    if system == "windows":
        with zipfile.ZipFile(stream) as archive:
            name = next((n for n in archive.namelist()
                         if n.rsplit("/", 1)[-1] == member_name), None)
            if name is None:
                raise KeyError(member_name)
            with archive.open(name) as source, open(target, "wb") as sink:
                shutil.copyfileobj(source, sink)
        return
    with tarfile.open(fileobj=stream, mode="r:gz") as archive:
        member = next((m for m in archive.getmembers()
                       if m.name.rsplit("/", 1)[-1] == member_name), None)
        if member is None:
            raise KeyError(member_name)
        extracted = archive.extractfile(member)
        with open(target, "wb") as sink:
            shutil.copyfileobj(extracted, sink)


# --------------------------------------------------------------------------- #
#  node / npm packages                                                        #
# --------------------------------------------------------------------------- #

def _run_with_pulse(job, callback, detail, phase="install", interval=0.8):
    """Keep the UI visibly alive while an external installer owns stdout."""
    stop = threading.Event()

    def heartbeat():
        while not stop.wait(interval):
            elapsed = max(0, int(time.time() - job.started))
            job.pulse("%s · %d с" % (detail, elapsed), phase=phase,
                      indeterminate=True)

    job.pulse(detail, phase=phase, indeterminate=True)
    thread = threading.Thread(target=heartbeat, name="install-heartbeat", daemon=True)
    thread.start()
    try:
        return callback()
    finally:
        stop.set()
        thread.join(0.2)


def _install_node(job):
    if processes.which("node"):
        return job.done(True, "Node.js уже установлен")
    if not processes.IS_WINDOWS:
        return job.done(False, "Установите Node.js через пакетный менеджер системы")
    if not processes.which("winget"):
        return job.done(False, "winget недоступен — скачайте с nodejs.org")
    detail = "winget скачивает и устанавливает Node.js"
    job.log("winget install OpenJS.NodeJS.LTS — это может занять пару минут…", 20,
            detail=detail, phase="install", indeterminate=True)
    code, out = _run_with_pulse(
        job,
        lambda: processes.run(
            "winget install --id OpenJS.NodeJS.LTS --silent "
            "--accept-package-agreements --accept-source-agreements", timeout=900),
        detail,
    )
    for line in (out or "").splitlines()[-25:]:
        if line.strip():
            job.log(line.strip(), detail="Обрабатываю результат winget…",
                    phase="install", indeterminate=True)
    if code != 0:
        return job.done(False, "winget вернул код %d" % code)
    job.log("Проверяю…", 90, detail="Проверяю установленный Node.js…",
            phase="verify", indeterminate=False)
    version = _version("node --version")
    if not version:
        # winget updates the user PATH for new processes, not this already
        # running hub. Discover the standard install folder and adopt it now.
        candidates = [
            os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "nodejs"),
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "nodejs"),
        ]
        for folder in candidates:
            if folder and os.path.isfile(os.path.join(folder, "node.exe")):
                os.environ["PATH"] = folder + os.pathsep + os.environ.get("PATH", "")
                version = _version("node --version")
                if version:
                    break
    if not version:
        return job.done(False, "Node установлен; перезапустите MCP Hub, чтобы обновился PATH")
    job.done(True, "Node.js %s готов" % version)


def _warm_npm_package(job, package, label):
    current = _npm_package_info(package)
    if current:
        version = current.get("version") or "установлен"
        return job.done(True, "%s %s уже готов" % (label, version))
    if not processes.which("npm"):
        return job.done(False, "Сначала установите Node.js")
    detail = "npm скачивает и распаковывает %s" % label
    job.log("Скачиваю %s из npm в локальный кеш…" % label, 20,
            detail=detail, phase="download", indeterminate=True)
    command = ("npm exec --yes --package=\"%s@latest\" -- "
               "node -e \"console.log('package-ready')\"") % package
    code, out = _run_with_pulse(
        job, lambda: processes.run(command, timeout=600), detail, phase="download")
    tail = [line.strip() for line in (out or "").splitlines() if line.strip()][-12:]
    for line in tail:
        job.log(line, detail="Обрабатываю результат npm…",
                phase="install", indeterminate=True)
    if code != 0:
        return job.done(False, "npm вернул код %d" % code)
    job.log("Проверяю пакет…", 92, detail="Проверяю установленный пакет…",
            phase="verify", indeterminate=False)
    current = _npm_package_info(package)
    version = current.get("version") if current else None
    job.done(True, "%s%s готов — первый запуск будет быстрым" %
             (label, " " + version if version else ""))


def _warm_supergateway(job):
    return _warm_npm_package(job, "supergateway", "supergateway")


def _warm_desktop_commander(job):
    return _warm_npm_package(
        job, "@wonderwhy-er/desktop-commander", "Desktop Commander")
