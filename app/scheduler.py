"""APScheduler setup for periodic scans (default every 6 hours)."""

from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from . import scanner
from .db import load_runtime_settings, session

logger = logging.getLogger("forumagent.scheduler")

_scheduler: BackgroundScheduler | None = None
_JOB_ID = "global_scan"


def _run_scan() -> None:
    logger.info("Scheduled scan starting…")
    try:
        summary = scanner.scan_all()
        logger.info(
            "Scheduled scan done: %s new posts across %s forums.",
            summary.total_new,
            len(summary.results),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Scheduled scan failed: %s", exc)


def start_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    with session() as db:
        settings = load_runtime_settings(db)
    interval_hours = max(1, settings.scan_interval_hours)

    _scheduler = BackgroundScheduler(daemon=True)
    _scheduler.add_job(
        _run_scan,
        trigger=IntervalTrigger(hours=interval_hours),
        id=_JOB_ID,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    _scheduler.start()
    logger.info("Scheduler started (every %s h).", interval_hours)
    return _scheduler


def reschedule(interval_hours: int) -> None:
    if _scheduler is None:
        return
    _scheduler.reschedule_job(_JOB_ID, trigger=IntervalTrigger(hours=max(1, interval_hours)))
    logger.info("Rescheduled scan interval to %s h.", interval_hours)


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
