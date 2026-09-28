"""Cron job scheduling for Hermes Agent: scheduled tasks (cron expressions, intervals, one-shot),
self-scheduled reminders, isolated sessions. The gateway daemon (``hermes gateway [install]``) ticks
the scheduler every 60 seconds; a file lock prevents duplicate execution across processes.
"""

import sys

# Python imports this package before ``-m cron.scheduler``. The gateway's
# sanitized worker environment pins the source tree, not PM's dependencies.
# Select and lease this install's committed generation before cron.jobs imports
# third-party packages. Do not run the CLI bootstrap: update completion/repair
# can replace the process and is not owned by a finite cron worker.
if "--external-worker-file" in sys.argv:
    from pathlib import Path
    from pm.environments import activate_dependencies

    activate_dependencies(Path(__file__).resolve().parents[1])

from cron.jobs import (
    create_job,
    get_job,
    list_jobs,
    remove_job,
    update_job,
    pause_job,
    resume_job,
    trigger_job,
    rearm_oneshot,
    JOBS_FILE,
)
from cron.scheduler import tick

__all__ = [
    "create_job",
    "get_job",
    "list_jobs",
    "remove_job",
    "update_job",
    "pause_job",
    "resume_job",
    "trigger_job",
    "rearm_oneshot",
    "tick",
    "JOBS_FILE",
]
