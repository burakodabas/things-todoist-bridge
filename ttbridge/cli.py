"""Command line entry point: ``ttbridge serve | sync | agent ...``."""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
import webbrowser

from . import __version__, launchd
from .config import Config, data_dir


def _setup_logging(verbose: bool) -> None:
    log_file = data_dir() / "ttbridge.log"
    handlers = [logging.StreamHandler(sys.stderr), logging.FileHandler(log_file, encoding="utf-8")]
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers)


def cmd_serve(args) -> int:
    from .service import BridgeService
    from .web import make_server

    config = Config()
    port = args.port or config.get("port", 8765)
    service = BridgeService(config)
    try:
        server = make_server(service, "127.0.0.1", port)
    except OSError as exc:
        print(f"Cannot listen on 127.0.0.1:{port}: {exc}. Is the bridge already running?", file=sys.stderr)
        if not args.no_browser:
            webbrowser.open(f"http://127.0.0.1:{port}/")
        return 1
    service.start()
    url = f"http://127.0.0.1:{port}/"
    logging.getLogger("ttbridge").info("Dashboard on %s (data in %s)", url, data_dir())
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    def _shutdown(*_):
        service.stop()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    server.serve_forever()
    return 0


def cmd_sync(args) -> int:
    from .service import BridgeService

    service = BridgeService(Config())
    result = service.sync_now()
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("ok") else 1


def cmd_agent(args) -> int:
    if args.action == "install":
        path = launchd.install(args.port or Config().get("port", 8765))
        print(f"Installed and started LaunchAgent: {path}")
    elif args.action == "uninstall":
        print("Removed LaunchAgent" if launchd.uninstall() else "LaunchAgent was not installed")
    else:
        print(launchd.status())
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="ttbridge", description="Two-way sync between Things 3 and Todoist.")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("serve", help="run the background sync and the web dashboard (default)")
    s.add_argument("--port", type=int, default=None)
    s.add_argument("--no-browser", action="store_true", help="don't open the dashboard")
    s.set_defaults(func=cmd_serve)

    y = sub.add_parser("sync", help="run one sync cycle and exit")
    y.set_defaults(func=cmd_sync)

    a = sub.add_parser("agent", help="manage the login LaunchAgent")
    a.add_argument("action", choices=["install", "uninstall", "status"])
    a.add_argument("--port", type=int, default=None)
    a.set_defaults(func=cmd_agent)

    args = p.parse_args(argv)
    _setup_logging(args.verbose)
    if not args.cmd:
        args.func, args.port, args.no_browser = cmd_serve, None, False
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
