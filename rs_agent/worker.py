"""Worker process: runs queued jobs with retries, timeouts and circuit breakers.

A LinkedIn outage only affects LinkedIn jobs (its breaker opens); everything
else keeps working. Failed actions are marked failed, never silently retried forever.
"""

from __future__ import annotations

import logging
import signal
import time
from typing import Any

from .app_context import AppContext, build_context, finish_run, start_run
from .errors import AgentError, ErrorCategory
from .executor import execute_action, mark_failed
from .jobs import CircuitBreaker, JobRunner
from .logging_setup import configure_logging, get_logger, log, run_id_var

_logger = get_logger("rs_agent.worker")


def build_runner(ctx: AppContext) -> JobRunner:
    def handle_execute(payload: dict[str, Any]) -> Any:
        action_id = str(payload.get("action_id", ""))
        try:
            return execute_action(ctx.db, ctx.linkedin, action_id)
        except AgentError as exc:
            if not exc.retryable:
                mark_failed(ctx.db, action_id, str(exc))
            raise

    def handle_refresh_scopes(payload: dict[str, Any]) -> Any:
        token = ctx.linkedin.access_token()
        return ctx.linkedin.detect_scopes(token)

    return JobRunner(
        ctx.db,
        handlers={"execute_action": handle_execute, "refresh_scopes": handle_refresh_scopes},
        breakers={"linkedin": CircuitBreaker(threshold=5, cooldown_s=600)},
        provider_of={"execute_action": "linkedin", "refresh_scopes": "linkedin"},
        timeout_s=ctx.settings.job_timeout_s,
    )


def run_forever(ctx: AppContext | None = None, max_iterations: int | None = None) -> None:
    ctx = ctx or build_context()
    runner = build_runner(ctx)
    stopping = {"flag": False}

    def _stop(*_: Any) -> None:
        stopping["flag"] = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    run_id = start_run(ctx.db, "worker")
    run_id_var.set(run_id)
    log(_logger, logging.INFO, "worker started")
    processed = 0
    iterations = 0
    try:
        while not stopping["flag"]:
            iterations += 1
            try:
                status = runner.run_once()
            except Exception as exc:  # noqa: BLE001 - keep the worker alive on DB hiccups
                log(_logger, logging.ERROR, "worker iteration failed", error=str(exc), category=ErrorCategory.INTERNAL.value)
                status = None
                time.sleep(ctx.settings.worker_poll_s)
            if status:
                processed += 1
            else:
                time.sleep(ctx.settings.worker_poll_s)
            if max_iterations is not None and iterations >= max_iterations:
                break
    finally:
        runner.shutdown()
        finish_run(ctx.db, run_id, "stopped", {"processed": processed})
        log(_logger, logging.INFO, "worker stopped", processed=processed)


def main() -> None:  # pragma: no cover - process entrypoint
    configure_logging()
    run_forever()


if __name__ == "__main__":  # pragma: no cover
    main()
