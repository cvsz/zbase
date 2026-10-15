"""S3Vault background worker.

Runs the periodic maintenance the API does not do inline:

* usage measurement and snapshots (engine listing, since the engine exposes no
  usage API)
* quota reservation expiry, so a crashed upload cannot leak capacity forever
* expired session purge
* quota reservation reconciliation against measured usage

Jobs are claimed with ``FOR UPDATE SKIP LOCKED`` so several worker replicas can
run against one database without processing the same job twice.
"""

from __future__ import annotations

import os
import signal
import structlog
import time

from .app import Services
from .config import ConfigError, load_config

log = structlog.get_logger("s3vault.worker")

USAGE_INTERVAL_S = 900
HOUSEKEEPING_INTERVAL_S = 300
JOB_INTERVAL_S = 30


class Worker:
    def __init__(self, config=None) -> None:  # noqa: ANN001
        self.config = config or load_config()
        self.services = Services(self.config)
        self._stop = False
        self._last_usage = 0.0
        self._last_housekeeping = 0.0
        self._last_jobs = 0.0

    # -- lifecycle ------------------------------------------------------
    def install_signal_handlers(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, self._handle_signal)

    def _handle_signal(self, signum, _frame) -> None:  # noqa: ANN001
        log.info("worker.signal", signal=signum)
        self._stop = True

    def run_forever(self) -> None:
        log.info(
            "worker.started",
            env=self.config.env,
            engine=self.services.capabilities.engine_name,
        )
        self.install_signal_handlers()
        while not self._stop:
            try:
                now = time.monotonic()
                if now - self._last_usage >= USAGE_INTERVAL_S:
                    self._last_usage = now
                    self.run_usage_aggregation()
                if now - self._last_housekeeping >= HOUSEKEEPING_INTERVAL_S:
                    self._last_housekeeping = now
                    self.run_housekeeping()
                if now - self._last_jobs >= JOB_INTERVAL_S:
                    self._last_jobs = now
                    self.run_jobs()
            except Exception:  # noqa: BLE001 - a worker must not die on one bad cycle
                log.exception("worker.cycle_failed")
            # Poll often so shutdown stays responsive.
            for _ in range(5):
                if self._stop:
                    break
                time.sleep(0.2)
        self.close()
        log.info("worker.stopped")

    def close(self) -> None:
        self.services.close()

    # -- jobs ------------------------------------------------------------
    def run_usage_aggregation(self) -> dict[str, int]:
        """Measure the engine and record snapshots for global, bucket and user scopes."""
        usage = self.services.usage
        totals = usage.measure_all()
        summary = usage.snapshot_global(totals)

        for bucket_name, figures in totals.items():
            usage.snapshot_bucket(bucket_name, figures["bytes"], figures["objects"])

        # Roll bucket figures up per owner so quota accounting has a user view.
        for row in self.services.db.fetch_all("SELECT id, owner_id FROM buckets"):
            owned = [
                figures
                for name, figures in totals.items()
                if self.services.buckets().exists_in_metadata(name)
                and str(self.services.buckets().get(name)["owner_id"]) == str(row["owner_id"])
            ]
            usage.snapshot_user(
                str(row["owner_id"]),
                sum(f["bytes"] for f in owned),
                sum(f["objects"] for f in owned),
            )

        log.info(
            "worker.usage_snapshot",
            buckets=summary["bucket_count"],
            bytes=summary["storage_bytes"],
            objects=summary["object_count"],
        )
        return summary

    def run_housekeeping(self) -> dict[str, int]:
        expired_reservations = self.services.quotas.expire_stale()
        purged_sessions = self.services.users.purge_expired_sessions()
        log.info(
            "worker.housekeeping",
            expired_reservations=expired_reservations,
            purged_sessions=purged_sessions,
        )
        return {
            "expired_reservations": expired_reservations,
            "purged_sessions": purged_sessions,
        }

    def run_jobs(self) -> int:
        """Claim and execute pending background jobs."""
        processed = 0
        while True:
            job = self._claim_job()
            if job is None:
                break
            processed += 1
            self._execute_job(job)
        return processed

    def _claim_job(self) -> dict | None:
        with self.services.db.transaction() as conn:
            row = conn.execute(
                """
                UPDATE background_jobs
                   SET state = 'running', locked_at = now(), locked_by = %s,
                       attempts = attempts + 1
                 WHERE id = (
                       SELECT id FROM background_jobs
                        WHERE state IN ('pending', 'running')
                          AND run_after <= now()
                          AND attempts < max_attempts
                        ORDER BY run_after ASC
                        FOR UPDATE SKIP LOCKED
                        LIMIT 1
                 )
                RETURNING id, job_type, payload, attempts, max_attempts
                """,
                (os.uname().nodename if hasattr(os, "uname") else "worker",),
            ).fetchone()
        return dict(row) if row else None

    def _execute_job(self, job: dict) -> None:
        job_id = job["id"]
        try:
            handler = getattr(self, f"_job_{job['job_type']}", None)
            if handler is None:
                raise ValueError(f"no handler for job type {job['job_type']!r}")
            detail = handler(job.get("payload") or {})
            self.services.db.execute(
                "UPDATE background_jobs SET state = 'done', finished_at = now() WHERE id = %s",
                (job_id,),
            )
            log.info("worker.job_done", job_type=job["job_type"], job_id=job_id, detail=detail)
        except Exception as exc:  # noqa: BLE001 - recorded for retry/inspection
            log.error("worker.job_failed", job_type=job["job_type"], job_id=job_id, error=str(exc))
            self.services.db.execute(
                """
                UPDATE background_jobs
                   SET state = 'failed', last_error = %s, finished_at = now()
                 WHERE id = %s
                """,
                (str(exc)[:1000], job_id),
            )

    def _job_measure_usage(self, payload: dict) -> dict:  # noqa: ARG002
        return self.run_usage_aggregation()

    def _job_reconcile_quotas(self, payload: dict) -> dict:  # noqa: ARG002
        return self.run_housekeeping()

    def _job_purge_sessions(self, payload: dict) -> dict:  # noqa: ARG002
        return {"purged": self.services.users.purge_expired_sessions()}


def main() -> int:
    try:
        worker = Worker()
    except ConfigError as exc:
        # Fail closed: a worker with bad configuration must not run half-configured.
        print(f"worker refused to start: {exc}", flush=True)
        return 2
    try:
        worker.run_forever()
    except KeyboardInterrupt:  # pragma: no cover
        worker.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())