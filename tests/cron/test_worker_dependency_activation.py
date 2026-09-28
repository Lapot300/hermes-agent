"""Activate only the selected dependency generation before cron package imports."""
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
    root = tmp_path / "source"
    package = root / "cron"
    package.mkdir(parents=True)
    (package / "__init__.py").write_bytes((repo / "cron/__init__.py").read_bytes())
    # Keep the leaf dependency graph tiny; the separate module smoke exercises
    # the real scheduler with the bare store interpreter and real ruamel.yaml.
    names = ("create_job get_job list_jobs remove_job update_job pause_job "
             "resume_job trigger_job rearm_oneshot JOBS_FILE").split()
    (package / "jobs.py").write_text("\n".join(f"{name} = None" for name in names))
    (package / "scheduler.py").write_text("tick = None\n")
    (root / "hermes_bootstrap.py").write_text(
        "raise AssertionError('finite worker must not run CLI update bootstrap')\n"
    )
    home = tmp_path / "home"
    from pm.environments import install_key
    state = home / "installs" / install_key(root)
    venv = state / "environments" / "selected" / "venv"
    site = venv / ("Lib/site-packages" if os.name == "nt" else
                   f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages")
    site.mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text(
        f"version = {sys.version_info.major}.{sys.version_info.minor}.0\n"
    )
    (site / "worker_only_dependency.py").write_text("VALUE = 'selected'\n")
    (state / "facts.json").write_text(json.dumps({
        "packages": {"venv": {"environment": str(venv)}}
    }))
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path / "user"),
           "HERMES_HOME": str(home), "HERMES_RUNTIME_DIR": str(tmp_path / "store"),
           "TMPDIR": str(tmp_path), "PYTHONDONTWRITEBYTECODE": "1"}
    for key in ("SYSTEMROOT", "WINDIR"):
        if key in os.environ:
            env[key] = os.environ[key]
    code = (
        "import sys, importlib.util; "
        f"sys.path[:0] = [{str(root)!r}, {str(repo)!r}]; "
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
