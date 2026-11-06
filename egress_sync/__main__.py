"""Entry point: resident ticker + `/metrics`, or one round with `--once`."""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__
from .app import RoundOutcome, assert_config, build_from_config, config_summary
from .config import Config, ConfigError
from .metrics import Metrics
from .transport import UrllibTransport

log = logging.getLogger("egress_sync")

METRICS_PATH = "/metrics"
HEALTH_PATH = "/healthz"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="egress-sync",
        description="Publish the mihomo direct-egress sets derived from runtime state.",
    )
    parser.add_argument("--once", action="store_true", help="run exactly one round and exit (exit 1 unless published)")
    parser.add_argument("--dry-run", action="store_true", help="derive and gate, but do not publish or refresh")
    parser.add_argument("--version", action="version", version=f"egress-sync {__version__}")
    return parser.parse_args(argv)


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
        stream=sys.stderr,
    )
    # The SA token is a bearer credential; never log request headers.
    logging.getLogger("urllib").setLevel(logging.WARNING)


class MetricsServer:
    """`/metrics` (Prometheus text format) and `/healthz`, on a daemon thread."""

    def __init__(self, metrics: Metrics, host: str, port: int) -> None:
        self._metrics = metrics
        metrics_ref = metrics

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:
                if self.path.split("?", 1)[0] == METRICS_PATH:
                    self._send(200, "text/plain; version=0.0.4; charset=utf-8", metrics_ref.render().encode("utf-8"))
                elif self.path.split("?", 1)[0] == HEALTH_PATH:
                    # Liveness only: the process serves metrics, which is all a
                    # failed round cannot take away (failures are metrics).
                    self._send(200, "text/plain; charset=utf-8", b"ok\n")
                else:
                    self._send(404, "text/plain; charset=utf-8", b"not found\n")

            def _send(self, status: int, content_type: str, body: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt: str, *args: object) -> None:
                log.debug("metrics server: " + fmt, *args)

        self._httpd = ThreadingHTTPServer((host, port), Handler)
        self._httpd.daemon_threads = True

    def start(self) -> None:
        thread = threading.Thread(target=self._httpd.serve_forever, name="metrics", daemon=True)
        thread.start()
        host, port = self._httpd.server_address[:2]
        log.info("metrics listening on http://%s:%s%s", host, port, METRICS_PATH)

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


def _stop_event() -> threading.Event:
    stop = threading.Event()

    def handler(signum: int, _frame: object) -> None:
        log.info("received signal %s; stopping after the current round", signum)
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, handler)
    return stop


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config = Config.from_env(os.environ)
        assert_config(config)
    except (ConfigError, ValueError) as exc:
        print(f"egress-sync: configuration error: {exc}", file=sys.stderr)
        return 2

    configure_logging(config.log_level)
    app = build_from_config(config, UrllibTransport(timeout=config.http_timeout_seconds, ca_file=config.kube_ca_file))
    log.info("egress-sync %s starting", __version__)
    log.info("configuration: %s", json.dumps(config_summary(config), sort_keys=True))

    if args.once:
        outcome = app.run_round(dry_run=args.dry_run)
        _report(outcome)
        return 0 if outcome.ok else 1

    server = MetricsServer(app.metrics, config.listen_host, config.listen_port)
    server.start()
    stop = _stop_event()

    while not stop.is_set():
        started = time.monotonic()
        _report(app.run_round())
        elapsed = time.monotonic() - started
        wait = max(config.interval_seconds - elapsed, 0.0)
        log.info("next round in %.0fs", wait)
        stop.wait(wait)

    server.stop()
    log.info("stopped")
    return 0


def _report(outcome: RoundOutcome) -> None:
    record = {
        "result": outcome.result,
        "detail": outcome.detail,
        "entries": outcome.entries,
        "dry_run": outcome.dry_run,
    }
    print(json.dumps(record, sort_keys=True), file=sys.stderr, flush=True)
    log.log(logging.INFO if outcome.ok else logging.WARNING, "%s: %s", outcome.result, outcome.detail or "ok")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
