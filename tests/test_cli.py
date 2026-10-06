from __future__ import annotations

import json
import signal
import subprocess
import sys
import time

import pytest
from typer.testing import CliRunner

from thilicrawl.cli import app
from thilicrawl.testsite import SiteConfig, SiteServer

runner = CliRunner()


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0 and result.output.startswith("thilicrawl ")


def test_no_arguments_shows_help() -> None:
    assert "Usage" in runner.invoke(app, []).output


@pytest.mark.parametrize("bad", ["ftp://x.test/", "localhost:8000", "not a url"])
def test_a_non_http_url_is_a_usage_error(bad: str) -> None:
    assert runner.invoke(app, ["check", bad]).exit_code == 2


def test_an_unknown_option_is_a_usage_error() -> None:
    assert runner.invoke(app, ["check", "http://x.test/", "--bogus"]).exit_code == 2


def test_an_unreachable_start_url_exits_3_with_a_reason() -> None:
    result = runner.invoke(app, ["check", "http://127.0.0.1:9/", "--attempts", "1", "--timeout", "1"])
    assert result.exit_code == 3
    assert "could not reach" in result.output


def test_a_clean_site_exits_0() -> None:
    config = SiteConfig(pages=15, latency_ms=1, faults=False)
    with SiteServer(config) as server:
        result = runner.invoke(app, ["check", server.main_url + "/"])
    assert result.exit_code == 0, result.output


def test_a_site_with_problems_exits_1_and_json_is_valid() -> None:
    config = SiteConfig(
        pages=40,
        latency_ms=1,
        slow=0,
        flaky=0,
        limited=0,
        external_hang=0,
        external_slow=0,
        external_ok=2,
        external_broken=1,
        broken_404=3,
        broken_410=1,
        big_page_links=50,
        big_blob_mb=1,
    )
    with SiteServer(config) as server:
        result = runner.invoke(app, ["check", server.main_url + "/", "--json", "--attempts", "2"])
    assert result.exit_code == 1
    data = json.loads(result.stdout)
    assert {"start_url", "results", "elapsed"} <= data.keys()


def test_ctrl_c_prints_partial_results_and_exits_130() -> None:
    if sys.platform == "win32":
        pytest.skip("SIGINT delivery to a child process works differently on Windows")
    config = SiteConfig(
        pages=60, latency_ms=300, slow=0, flaky=0, limited=0, big_page_links=50, big_blob_mb=1
    )
    with SiteServer(config) as server:
        proc = subprocess.Popen(
            [sys.executable, "-m", "thilicrawl", "check", server.main_url + "/", "--concurrency", "2"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        time.sleep(2.0)
        proc.send_signal(signal.SIGINT)
        out, err = proc.communicate(timeout=20)
    assert proc.returncode == 130, (out, err)
    assert "interrupted" in err
    assert out.strip()  # the partial report was still printed
