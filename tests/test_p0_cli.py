"""V0 — scaffolding & dependency checks."""

from __future__ import annotations

import subprocess
import sys

from subcast import __version__
from subcast.cli import main


def run_cli(args, env=None):
    return subprocess.run(
        [sys.executable, "-m", "subcast", *args],
        capture_output=True, text=True, env=env,
    )


def test_v0_1_version(capsys):
    # --version exits 0 and prints a semver.
    code = main(["--version"])
    out = capsys.readouterr().out
    assert code == 0
    assert __version__ in out


def test_v0_2_help_lists_commands(capsys):
    code = main(["--help"])
    out = capsys.readouterr().out
    assert code == 0
    for cmd in ("cast", "devices", "probe", "stop"):
        assert cmd in out


def test_v0_3_missing_ffmpeg_exits_5(monkeypatch):
    # Hide ffmpeg/ffprobe from PATH; a real command must exit 5 with a hint.
    import subcast.config as config

    monkeypatch.setattr(config.shutil, "which", lambda name: None)
    code = main(["probe", "/nonexistent"])
    assert code == 5


def test_v0_4_python_m_equivalent():
    proc = run_cli(["--version"])
    assert proc.returncode == 0
    assert __version__ in proc.stdout
