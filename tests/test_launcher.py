import subprocess
import sys
from unittest.mock import patch

import pytest

pytest.importorskip("starlette")
pytest.importorskip("uvicorn")

from sinbosen_la548 import launcher  # noqa: E402


def run_default(argv, capsys):
    with patch("sinbosen_la548.launcher.uvicorn.run") as run, \
            patch("sinbosen_la548.launcher.create_studio_app") as create:
        launcher.main(argv)
    return run, create, capsys.readouterr().out


def test_default_runs_the_studio_app_under_uvicorn(capsys):
    run, create, out = run_default([], capsys)
    create.assert_called_once_with(allowed_hosts=())
    run.assert_called_once_with(create.return_value, host="127.0.0.1", port=7860, log_level="warning")
    assert "http://127.0.0.1:7860" in out
    assert "no authentication" not in out


def test_demo_flag_is_accepted(capsys):
    run, _, _ = run_default(["--demo", "--port", "9000"], capsys)
    assert run.call_args.kwargs["port"] == 9000


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_loopback_hosts_need_no_allowance(host, capsys):
    _, create, out = run_default(["--host", host], capsys)
    create.assert_called_once_with(allowed_hosts=())
    assert "no authentication" not in out


def test_non_loopback_host_is_allowed_and_warned_about(capsys):
    run, create, out = run_default(["--host", "studio.lan"], capsys)
    create.assert_called_once_with(allowed_hosts=("studio.lan",))
    assert run.call_args.kwargs["host"] == "studio.lan"
    assert "http://studio.lan:7860" in out
    warning = [line for line in out.splitlines() if "no authentication" in line]
    assert len(warning) == 1 and "output levels" in warning[0]


def test_classic_launches_gradio_and_never_the_studio(capsys):
    with patch("sinbosen_la548.launcher.uvicorn.run") as run, \
            patch("sinbosen_la548.launcher.create_studio_app") as create, \
            patch("sinbosen_la548.ui.launch_classic") as classic:
        launcher.main(["--classic", "--host", "0.0.0.0", "--port", "7001"])
    classic.assert_called_once_with("0.0.0.0", 7001)
    run.assert_not_called()
    create.assert_not_called()


def test_studio_start_does_not_import_gradio_or_plotly():
    code = (
        "import sys\n"
        "from sinbosen_la548 import launcher\n"
        "launcher.create_studio_app()\n"
        "bad = sorted(m for m in sys.modules if m.split('.')[0] in ('gradio', 'plotly'))\n"
        "assert not bad, bad\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
