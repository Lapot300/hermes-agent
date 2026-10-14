"""Real cron package entry activates the selected generation before worker imports."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("external", [False, True])
def test_package_entry_activates_dependencies_without_cli_bootstrap(tmp_path, external):
    repo = Path(__file__).resolve().parents[2]
    home = tmp_path / "home"
    from pm.environments import install_key
    state = home / "installs" / install_key(repo)
    venv = state / "environments" / "selected" / "venv"
    site = venv / ("Lib/site-packages" if os.name == "nt" else
                   f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages")
    site.mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text(
        f"version = {sys.version_info.major}.{sys.version_info.minor}.0\n",
        encoding="utf-8",
    )
    dependencies = [p for p in sys.path if Path(p).name in ("site-packages", "dist-packages")]
    # Real locked dependencies remain available after activation replaces sys.path;
    # only the selected generation exposes the independent sentinel.
    (site / "dependencies.pth").write_text("\n".join(dependencies) + "\n", encoding="utf-8")
    (site / "worker_only_dependency.py").write_text("VALUE = 'selected'\n", encoding="utf-8")
    (state / "facts.json").write_text(json.dumps({
        "packages": {"venv": {"environment": str(venv)}}
    }), encoding="utf-8")
    user = tmp_path / "user"
    user.mkdir()
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(user),
           "USERPROFILE": str(user),
           "HERMES_HOME": str(home), "HERMES_RUNTIME_DIR": str(tmp_path / "store"),
           "TMPDIR": str(tmp_path), "PYTHONDONTWRITEBYTECODE": "1"}
    for key in ("SYSTEMROOT", "WINDIR"):
        if key in os.environ:
            env[key] = os.environ[key]
    if external:
        # The current launcher's explicit marker owns bootstrap, not an inherited argv flag.
        env["_HERMES_CRON_WORKER_BOOT"] = "1"
    code = (
        "import sys, importlib.util; "
        f"sys.path[:0] = {[str(repo), *dependencies]!r}; "
        f"sys.argv += {['--external-worker-file'] if external else []!r}; "
        "import cron; "
        "print(importlib.util.find_spec('worker_only_dependency') is not None); "
        "assert 'hermes_bootstrap' not in sys.modules"
    )
    child = subprocess.run(
        [str(Path(getattr(sys, "_base_executable", sys.executable)).resolve()),
         "-I", "-B", "-c", code], env=env, cwd=tmp_path,
        capture_output=True, text=True, timeout=30,
    )
    assert child.returncode == 0, child.stderr
    assert child.stdout.strip() == str(external)
