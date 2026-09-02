#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MCP Hub entry point.

    python run.py              start the panel (and everything enabled)
    python run.py --render     only regenerate the Caddyfile
    python run.py --stop       stop every process the hub owns
    python run.py --status     print a short status and exit

Only the standard library is required. Caddy is downloaded from the panel.
"""

import argparse
import json
import signal
import sys
import threading
import time
import webbrowser

if sys.version_info < (3, 8):
    sys.exit("Нужен Python 3.8 или новее. Установите с python.org и повторите запуск.")

from hub import __version__, caddyfile, config, inspector, logbook, server, supervisor, telemetry
from hub.events import BUS

BANNER = """
  MCP Hub %s
  Панель:     http://127.0.0.1:%d/
  Инспектор:  127.0.0.1:%d
  %s
"""


def parse_args():
    parser = argparse.ArgumentParser(description="MCP Hub")
    parser.add_argument("--port", type=int, help="порт панели (по умолчанию из config/hub.json)")
    parser.add_argument("--render", action="store_true", help="только пересобрать Caddyfile")
    parser.add_argument("--stop", action="store_true", help="остановить всё")
    parser.add_argument("--status", action="store_true", help="краткий статус в JSON")
    parser.add_argument("--no-browser", action="store_true", help="не открывать браузер")
    parser.add_argument("--import-env", metavar="PATH",
                       help="перенести настройки из старого .env")
    return parser.parse_args()


def main():
    args = parse_args()
    logbook.capture_stdlib()
    cfg = config.load()

    if args.import_env:
        ok = config.migrate_from_env(args.import_env)
        print("Импорт из %s: %s" % (args.import_env, "готово" if ok else "нечего импортировать"))
        cfg = config.load(force=True)

    if args.render:
        path, changed = caddyfile.write(cfg)
        print("%s: %s" % (path, "обновлён" if changed else "без изменений"))
        return 0

    if args.stop:
        supervisor.stop_all()
        print("Все процессы хаба остановлены.")
        return 0

    if args.status:
        state = supervisor.build_state()
        print(json.dumps({
            "domain": state["domain"],
            "caddy": state["caddy"]["state"],
            "enabled": state["enabledCount"],
            "services": {s["id"]: s["status"]["state"] for s in state["services"]},
            "problems": [p["text"] for p in state["problems"]],
        }, ensure_ascii=False, indent=2))
        return 0

    port = args.port or int(cfg.get("adminPort") or 8765)
    telemetry.abandon_running()
    telemetry.prune()
    caddyfile.write(cfg)

    inspector.serve(int(cfg.get("inspectorPort") or 8770))
    watchdog = supervisor.Watchdog()
    watchdog.start()

    enabled = config.enabled_services(cfg)
    if enabled:
        threading.Thread(target=supervisor.start_all, name="autostart", daemon=True).start()
        summary = "Включено сервисов: %d" % len(enabled)
    else:
        summary = "Все MCP выключены — включите нужные в панели"

    logbook.LOG.info("hub", "MCP Hub запущен", event="hub.started",
                     version=__version__, port=port,
                     inspectorPort=int(cfg.get("inspectorPort") or 8770),
                     enabledServices=len(enabled))
    print(BANNER % (__version__, port, int(cfg.get("inspectorPort") or 8770), summary))

    if cfg.get("openBrowser") and not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open("http://127.0.0.1:%d/" % port)).start()

    panel, _thread = server.serve(port, blocking=False)

    stopping = threading.Event()

    def shutdown(_signum=None, _frame=None):
        if stopping.is_set():
            return
        stopping.set()
        print("\nОстанавливаю…")
        BUS.publish("hub.stopping", {})
        logbook.LOG.info("hub", "MCP Hub останавливается", event="hub.stopping")
        watchdog.stop()
        panel.shutdown()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, shutdown)
        except (ValueError, OSError):
            pass

    try:
        while not stopping.is_set():
            time.sleep(0.5)
    except KeyboardInterrupt:
        shutdown()
    print("Панель остановлена. MCP-процессы продолжают работать; `python run.py --stop` закроет их.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
