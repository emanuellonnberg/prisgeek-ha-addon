"""Combined Home Assistant Add-on entrypoint running the background watcher and dashboard."""

from __future__ import annotations

import logging
import os
import threading
import time

import uvicorn

from dashboard import app
from db import init_db
from notifier import load_notifier_config
from watcher import run_check_cycle

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("prisgeek.addon")


def _scheduler_loop() -> None:
    """Wait briefly on startup, then run periodic price checks and push HA sensors."""
    time.sleep(10)
    while True:
        cfg = load_notifier_config()
        interval_minutes = int(cfg.get("check_interval_minutes", 60))
        sleep_seconds = max(300, interval_minutes * 60)
        try:
            logger.info("Running scheduled PrisGeek price check cycle...")
            report = run_check_cycle(send_webhooks=True)
            logger.info(
                "Check complete: products=%s queries=%s alerts=%s sensors=%s",
                report.get("checked_product_watches"),
                report.get("checked_query_watches"),
                len(report.get("triggered_alerts") or []),
                report.get("ha_sensors"),
            )
        except Exception:
            logger.exception("Scheduled check cycle failed")
        time.sleep(sleep_seconds)


def main() -> None:
    """Initialize SQLite DB, launch background check thread, and serve dashboard."""
    init_db()
    worker = threading.Thread(target=_scheduler_loop, name="prisgeek-scheduler", daemon=True)
    worker.start()

    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8050"))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
