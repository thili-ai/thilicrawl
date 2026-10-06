"""A small HTTP/1.1 server (on ``asyncio`` streams) that serves the test site and misbehaves on demand.

It speaks just enough HTTP for ``httpx``: persistent connections, ``GET`` and ``HEAD``, a handful of
status codes. Everything interesting is injected behaviour:

* **latency** — every request takes ``latency_ms``; slow pages take ``slow_ms``;
* **hangs** — a hung request is read, then never answered, until the client gives up and disconnects;
* **overload** — with ``capacity`` set, a request that arrives while that many are already in flight
  is answered ``503`` (header ``X-Overloaded: 1``), the way a real server buckles under too much at once;
* **flakiness** — a flaky page answers ``503`` for its first few requests, then works;
* **rate limiting** — requests to ``/limited/*`` beyond a per-window quota get ``429`` + ``Retry-After``.

``SiteServer`` runs it all on a background thread with its own event loop, so a test or a notebook can
start a site with one line and talk to it from its own event loop.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any

import httpx

from .site import Site, SiteConfig, big_page_html, build_site

REASONS = {
    200: "OK",
    301: "Moved Permanently",
    302: "Found",
    404: "Not Found",
    410: "Gone",
    429: "Too Many Requests",
    503: "Service Unavailable",
}
CHUNK = b"\0" * 65_536


@dataclass
class Resp:
    status: int
    body: bytes = b""
    content_type: str = "text/html; charset=utf-8"
    headers: dict[str, str] = field(default_factory=dict)
    delay: float = 0.0
    hang: bool = False
    stream_bytes: int = 0  # > 0: the body is this many zero bytes, streamed rather than held in memory


@dataclass
class Stats:
    requests: int = 0
    overloaded: int = 0
    max_inflight: int = 0
    private_hits: int = 0
    by_status: Counter[int] = field(default_factory=Counter)
    by_path: Counter[str] = field(default_factory=Counter)

    def as_dict(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "overloaded": self.overloaded,
            "max_inflight": self.max_inflight,
            "private_hits": self.private_hits,
            "by_status": {str(k): v for k, v in self.by_status.items()},
        }


class SiteApp:
    """Routes one host's requests. Holds the (small) mutable state: flaky counters, rate-limit window."""

    def __init__(self, site: Site, *, external: bool, clock: Any = time.monotonic) -> None:
        self.site = site
        self.cfg = site.config
        self.external = external
        self.clock = clock
        self.stats = Stats()
        self.inflight = 0
        self._flaky_hits: Counter[str] = Counter()
        self._window_start = clock()
        self._window_count = 0
        self._big_page: bytes | None = None

    def prepare(self) -> None:
        """Do the expensive one-off work up front (the huge page), before any client is waiting."""
        if self._big_page is None:
            self._big_page = big_page_html(self.cfg.big_page_links, self.cfg.pages, self.site.main_base)

    @property
    def base_delay(self) -> float:
        return self.cfg.latency_ms / 1000

    def route(self, path: str) -> Resp:
        site, cfg = self.site, self.cfg
        if path == "/__stats":  # for benchmarks: what did this server see?
            return Resp(200, json.dumps(self.stats.as_dict()).encode(), "application/json")
        if self.external:
            return self._route_external(path)
        if path == "/robots.txt":
            return Resp(200, site.robots_txt.encode(), "text/plain", delay=self.base_delay)
        if path in site.private_paths:
            self.stats.private_hits += 1
        if path in site.pages:
            slow = path in site.slow_paths
            if path in site.flaky_paths:
                self._flaky_hits[path] += 1
                if self._flaky_hits[path] <= cfg.flaky_failures:
                    return Resp(503, b"try again", "text/plain", delay=self.base_delay)
            if path in site.limited_paths:
                limited = self._rate_limit()
                if limited is not None:
                    return limited
            delay = cfg.slow_ms / 1000 if slow else self.base_delay
            return Resp(200, site.pages[path], delay=delay)
        if path in site.gone:
            return Resp(site.gone[path], b"gone", "text/plain", delay=self.base_delay)
        if path in site.redirects:
            status, location = site.redirects[path]
            return Resp(status, b"", headers={"Location": location}, delay=self.base_delay)
        if path == site.blob_path:
            return Resp(
                200,
                content_type="application/octet-stream",
                delay=self.base_delay,
                stream_bytes=cfg.big_blob_mb * 1_000_000,
            )
        if path == site.big_page_path:
            self.prepare()  # (already done at startup; this is only a safety net)
            assert self._big_page is not None
            return Resp(200, self._big_page, delay=self.base_delay)
        return Resp(404, b"not found", "text/plain", delay=self.base_delay)

    def _route_external(self, path: str) -> Resp:
        site = self.site
        if path in site.ext_hang:
            return Resp(200, hang=True)
        if path in site.ext_slow:
            return Resp(200, b"<html><body>slow</body></html>", delay=self.cfg.slow_ms / 1000)
        if path in site.ext_ok:
            return Resp(200, b"<html><body>external</body></html>", delay=self.base_delay)
        return Resp(404, b"not found", "text/plain", delay=self.base_delay)

    def _rate_limit(self) -> Resp | None:
        now = self.clock()
        if now - self._window_start >= self.cfg.limit_window_s:
            self._window_start, self._window_count = now, 0
        self._window_count += 1
        if self._window_count <= self.cfg.limit_requests:
            return None
        retry_after = max(1, math.ceil(self.cfg.limit_window_s - (now - self._window_start)))
        return Resp(
            429,
            b"slow down",
            "text/plain",
            headers={"Retry-After": str(retry_after)},
            delay=self.base_delay,
        )


async def _read_request(reader: asyncio.StreamReader) -> tuple[str, str, dict[str, str]] | None:
    line = await reader.readline()
    if not line:
        return None
    parts = line.decode("latin-1").split()
    if len(parts) != 3:
        return None
    method, target, _version = parts
    headers: dict[str, str] = {}
    while True:
        raw = await reader.readline()
        if raw in (b"\r\n", b"\n", b""):
            break
        name, _, value = raw.decode("latin-1").partition(":")
        headers[name.strip().lower()] = value.strip()
    return method, target, headers


async def _handle(app: SiteApp, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            request = await _read_request(reader)
            if request is None:
                return
            method, target, headers = request
            keep_alive = headers.get("connection", "").lower() != "close"
            path = target.split("?", 1)[0]
            app.stats.requests += 1
            app.stats.by_path[path] += 1

            if app.cfg.capacity is not None and not app.external and app.inflight >= app.cfg.capacity:
                app.stats.overloaded += 1
                resp = Resp(503, b"overloaded", "text/plain", headers={"X-Overloaded": "1"})
            else:
                resp = app.route(path)
                app.inflight += 1
                app.stats.max_inflight = max(app.stats.max_inflight, app.inflight)
                try:
                    if resp.hang:
                        while await reader.read(1024):  # wait until the client gives up and disconnects
                            pass
                        return
                    await asyncio.sleep(resp.delay)
                finally:
                    app.inflight -= 1
            app.stats.by_status[resp.status] += 1

            length = resp.stream_bytes or len(resp.body)
            head = [
                f"HTTP/1.1 {resp.status} {REASONS.get(resp.status, 'Status')}",
                f"Content-Type: {resp.content_type}",
                f"Content-Length: {length}",
                f"Connection: {'keep-alive' if keep_alive else 'close'}",
            ]
            head += [f"{k}: {v}" for k, v in resp.headers.items()]
            writer.write(("\r\n".join(head) + "\r\n\r\n").encode("latin-1"))
            if method != "HEAD":
                if resp.stream_bytes:
                    sent = 0
                    while sent < resp.stream_bytes:
                        chunk = CHUNK[: resp.stream_bytes - sent]
                        writer.write(chunk)
                        await writer.drain()
                        sent += len(chunk)
                else:
                    writer.write(resp.body)
            await writer.drain()
            if not keep_alive:
                return
    except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
        return  # the client went away mid-response (it only wanted the headers): perfectly normal
    finally:
        with contextlib.suppress(Exception):
            writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


def _free_port() -> int:
    """A port nothing is listening on (bind one, note it, close it)."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class SiteServer:
    """Start the test site on a background thread. Use as a context manager."""

    def __init__(self, config: SiteConfig | None = None, host: str = "127.0.0.1") -> None:
        self.config = config or SiteConfig()
        self.host = host
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self.site: Site | None = None
        self.main_app: SiteApp | None = None
        self.ext_app: SiteApp | None = None

    # -- the things callers want ---------------------------------------------------------------
    @property
    def main_url(self) -> str:
        assert self.site is not None, "server not started"
        return self.site.main_base

    @property
    def ext_url(self) -> str:
        assert self.site is not None, "server not started"
        return self.site.ext_base

    @property
    def manifest(self) -> dict[str, Any]:
        assert self.site is not None, "server not started"
        return self.site.manifest

    @property
    def stats(self) -> Stats:
        assert self.main_app is not None, "server not started"
        return self.main_app.stats

    # -- lifecycle -----------------------------------------------------------------------------
    def start(self) -> SiteServer:
        self._thread = threading.Thread(target=self._run, name="thilicrawl-testsite", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=30):
            raise RuntimeError("the test site did not start")
        if self._error is not None:
            raise RuntimeError("the test site failed to start") from self._error
        return self

    def stop(self) -> None:
        if self._loop is not None and self._stop is not None:
            self._loop.call_soon_threadsafe(self._stop.set)
        if self._thread is not None:
            self._thread.join(timeout=10)

    def __enter__(self) -> SiteServer:
        return self.start()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        try:
            self._loop.run_until_complete(self._serve())
        except BaseException as exc:
            self._error = exc
            self._ready.set()
        finally:
            # Connections still open (a hung page, say) have handler tasks waiting; end them cleanly
            # rather than leaving them to complain when the loop is closed under them.
            leftover = asyncio.all_tasks(self._loop)
            for task in leftover:
                task.cancel()

            async def drain() -> None:
                await asyncio.gather(*leftover, return_exceptions=True)

            self._loop.run_until_complete(drain())
            self._loop.close()

    async def _serve(self) -> None:
        self._stop = asyncio.Event()
        holder: dict[str, SiteApp] = {}

        async def on_main(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            await _handle(holder["main"], r, w)

        async def on_ext(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            await _handle(holder["ext"], r, w)

        main = await asyncio.start_server(on_main, self.host, 0)
        ext = await asyncio.start_server(on_ext, self.host, 0)
        main_port = main.sockets[0].getsockname()[1]
        ext_port = ext.sockets[0].getsockname()[1]
        dead_url = f"http://{self.host}:{_free_port()}/nothing-listens-here"
        self.site = build_site(
            self.config,
            f"http://{self.host}:{main_port}",
            f"http://{self.host}:{ext_port}",
            dead_url,
        )
        self.main_app = holder["main"] = SiteApp(self.site, external=False)
        self.ext_app = holder["ext"] = SiteApp(self.site, external=True)
        if self.config.faults:  # build the 4 MB page now, so no *request* ever has to stall the loop doing it
            self.main_app.prepare()
        self._ready.set()
        async with main, ext:
            await self._stop.wait()


class SiteProcess:
    """The same test site, in a **separate process** (use this for measurements).

    ``SiteServer`` runs the site on a thread inside *your* process, which is convenient for tests, but
    the two then share one interpreter lock: when your code is busy computing, the server's thread is
    starved, and the latencies you measure include delays that real servers would not cause. A separate
    process cannot interfere that way.
    """

    def __init__(self, config: SiteConfig | None = None) -> None:
        self.config = config or SiteConfig()
        self._proc: subprocess.Popen[str] | None = None
        self._manifest: dict[str, Any] = {}
        self._urls: dict[str, str] = {}

    def start(self) -> SiteProcess:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            manifest_path = Path(handle.name)
        self._proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "thilicrawl.testsite",
                "--config-json",
                json.dumps(self.config.__dict__),
                "--manifest",
                str(manifest_path),
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        assert self._proc.stdout is not None
        line = self._proc.stdout.readline()
        if not line:
            raise RuntimeError("the test site process did not start")
        self._urls = json.loads(line)
        self._manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest_path.unlink(missing_ok=True)
        return self

    def stop(self) -> None:
        if self._proc is not None:
            self._proc.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                self._proc.wait(timeout=10)
            if self._proc.stdout is not None:
                self._proc.stdout.close()
            self._proc = None

    def __enter__(self) -> SiteProcess:
        return self.start()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()

    @property
    def main_url(self) -> str:
        return self._urls["start_url"].rstrip("/")

    @property
    def ext_url(self) -> str:
        return self._urls["external"]

    @property
    def manifest(self) -> dict[str, Any]:
        return self._manifest

    def stats(self) -> dict[str, Any]:
        """What the server saw so far (request counts, peak in-flight, how many it refused as overloaded)."""
        reply = httpx.get(self.main_url + "/__stats", timeout=5)
        stats: dict[str, Any] = reply.json()
        return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m thilicrawl.testsite", description="Serve the test site until Ctrl-C."
    )
    parser.add_argument("--pages", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--latency-ms", type=float, default=8.0)
    parser.add_argument("--capacity", type=int, default=None, help="in-flight requests before 503")
    parser.add_argument("--config-json", default=None, help="any SiteConfig fields, as a JSON object")
    parser.add_argument("--manifest", type=Path, default=None, help="also write the manifest here")
    args = parser.parse_args(argv)
    fields: dict[str, Any] = {
        "pages": args.pages,
        "seed": args.seed,
        "latency_ms": args.latency_ms,
        "capacity": args.capacity,
    }
    if args.config_json:
        fields.update(json.loads(args.config_json))
    config = SiteConfig(**fields)
    with SiteServer(config) as server:
        if args.manifest:
            args.manifest.write_text(json.dumps(server.manifest, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"start_url": server.main_url + "/", "external": server.ext_url}), flush=True)
        with contextlib.suppress(KeyboardInterrupt):
            threading.Event().wait()
    return 0


if __name__ == "__main__":
    sys.exit(main())
