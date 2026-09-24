"""Run one shard's cells durably.

A shard is all cells for one ``(tier, approach)`` pair. The loop itself is
mundane; everything interesting here is about making sure no work is ever more
than ``upload_every_seconds`` away from durable storage, because the full sweep
is ~12 hours of live Gemini calls.

Durability, in layers:

- Each finished cell is appended to a local file and ``fsync``'d immediately.
- The whole file is uploaded every 25 cells or 60 seconds, whichever first.
- ``SIGTERM`` triggers an immediate upload. Vertex sends it before killing a
  task on cancellation or preemption, which turns a cancelled run into a
  resumable one rather than a lost hour.
- A ``_SUCCESS`` or ``_FAILED`` marker records the terminal state.

The cell executor is injected. That keeps this loop testable without ADK, a
network, or a GCP project — the parts most likely to break are the parts that
have nothing to do with running an agent.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Iterator

from bq_context.runner.backoff import CircuitBreaker
from bq_context.runner.models import Cell, ShardResult
from bq_context.runner.resume import (
    completed_keys,
    load_shard_records,
    next_attempt_path,
    shard_prefix,
)
from bq_context.runner.summaries import write_summary

if TYPE_CHECKING:
    from collections.abc import Mapping

    from bq_context.runner.models import ShardSpec
    from bq_context.runner.store import ArtifactStore

logger = logging.getLogger(__name__)

__all__ = ["CellExecutor", "ShardRunner"]

UPLOAD_EVERY_CELLS = 25
UPLOAD_EVERY_SECONDS = 60.0
HEARTBEAT_SECONDS = 30.0


class CellExecutor(Protocol):
    """Runs one approach-run and returns its record.

    Implementations must not raise for an ordinary failure — they return a Cell
    with ``status="error"``. An exception escaping here is treated as a bug and
    still recorded, but it does not stop the shard.
    """

    async def __call__(self, question: Mapping[str, object], run_idx: int) -> Cell: ...


class ShardRunner:
    """Executes the cells of one shard, checkpointing as it goes."""

    def __init__(  # noqa: PLR0913 - a runner needs its collaborators and its cadences
        self,
        spec: ShardSpec,
        store: ArtifactStore,
        executor: CellExecutor,
        questions: Mapping[str, Mapping[str, object]],
        *,
        upload_every_cells: int = UPLOAD_EVERY_CELLS,
        upload_every_seconds: float = UPLOAD_EVERY_SECONDS,
        heartbeat_seconds: float = HEARTBEAT_SECONDS,
        cache_warm_s: float = 0.0,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self.spec = spec
        self.store = store
        self.executor = executor
        self.questions = questions
        self.upload_every_cells = upload_every_cells
        self.upload_every_seconds = upload_every_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.cache_warm_s = cache_warm_s
        self.breaker = breaker if breaker is not None else CircuitBreaker()
        self._abort_reason = ""

        self._buffer: Path | None = None
        self._attempt_path = ""
        self._since_upload = 0
        self._last_upload = 0.0
        self._done = 0
        self._failed = 0
        self._total = 0
        self._started = 0.0

    # -- durability ---------------------------------------------------------

    def _append(self, cell: Cell) -> None:
        """Append one record locally and force it to disk."""
        assert self._buffer is not None
        with self._buffer.open("a") as fh:
            fh.write(cell.to_jsonl())
            fh.flush()
            os.fsync(fh.fileno())

    def _upload(self) -> None:
        """Overwrite the remote attempt object with the full local buffer."""
        assert self._buffer is not None
        self.store.write_text(self._attempt_path, self._buffer.read_text())
        self._since_upload = 0
        self._last_upload = time.monotonic()

    def _maybe_upload(self) -> None:
        due_by_count = self._since_upload >= self.upload_every_cells
        due_by_time = (time.monotonic() - self._last_upload) >= self.upload_every_seconds
        if due_by_count or due_by_time:
            self._upload()

    # -- observability ------------------------------------------------------

    async def _heartbeat(self) -> None:
        """Log progress periodically.

        On Vertex there is no stdout to tail, so without this a healthy
        90-minute shard is indistinguishable from a hung one.
        """
        while True:
            await asyncio.sleep(self.heartbeat_seconds)
            elapsed = time.monotonic() - self._started
            rate = self._done / elapsed if elapsed and self._done else 0.0
            remaining = (self._total - self._done) / rate if rate else float("nan")
            logger.info(
                "[%s] %d/%d cells (%d failed) · %.0fs elapsed · eta %.0fs",
                self.spec.shard_id,
                self._done,
                self._total,
                self._failed,
                elapsed,
                remaining,
            )

    # -- main loop ----------------------------------------------------------

    async def run(self) -> ShardResult:
        """Execute every not-yet-complete cell in this shard."""
        planned = self.spec.planned_cells()
        finished = completed_keys(load_shard_records(self.store, self.spec))
        todo = [key for key in planned if key not in finished]

        self._attempt_path = next_attempt_path(self.store, self.spec)
        self._total = len(todo)
        self._started = time.monotonic()
        self._last_upload = time.monotonic()

        logger.info(
            "[%s] %d planned, %d already done, %d to run -> %s",
            self.spec.shard_id,
            len(planned),
            len(finished),
            len(todo),
            self.store.uri(self._attempt_path),
        )

        if not todo:
            return self._finish(success=True, planned=len(planned), already_done=len(finished))

        with tempfile.TemporaryDirectory(prefix="bq-context-shard-") as tmpdir:
            self._buffer = Path(tmpdir) / "shard.jsonl"
            self._buffer.touch()
            with self._sigterm_flush():
                heartbeat = asyncio.create_task(self._heartbeat())
                try:
                    await self._run_cells(todo)
                finally:
                    heartbeat.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await heartbeat
                    self._upload()

        return self._finish(
            success=self._failed == 0 and not self._abort_reason,
            planned=len(planned),
            already_done=len(finished),
        )

    async def _run_cells(self, todo: list[str]) -> None:
        for key in todo:
            question_id, _, _, run_part = key.split("|")
            run_idx = int(run_part.removeprefix("run"))
            question = self.questions[question_id]

            cell = await self._execute_one(key, question, run_idx)
            self._append(cell)
            self._done += 1
            self._since_upload += 1
            ok = cell.status == "ok"
            if not ok:
                self._failed += 1
            self._maybe_upload()

            self.breaker.record(ok=ok)
            if self.breaker.tripped:
                # Stop early, but keep everything finished so far. Without this,
                # a shard broken by bad IAM would spend its whole retry budget
                # rediscovering the same failure.
                self._abort_reason = self.breaker.reason
                logger.error(
                    "[%s] circuit breaker tripped: %s — abandoning shard after %d/%d cells",
                    self.spec.shard_id,
                    self.breaker.reason,
                    self._done,
                    self._total,
                )
                return

    async def _execute_one(self, key: str, question: Mapping[str, object], run_idx: int) -> Cell:
        """Run one cell, converting an unexpected exception into an error cell.

        A cell failure costs one cell, never the shard. The executor is expected
        to handle ordinary failures itself; this catch is for bugs.
        """
        try:
            return await self.executor(question, run_idx)
        except Exception as exc:
            logger.exception("[%s] cell %s raised", self.spec.shard_id, key)
            return Cell(
                cell_key=key,
                question_id=str(question.get("id", "")),
                approach=self.spec.approach,
                tier=self.spec.tier,
                run_idx=run_idx,
                status="error",
                code_version=self.spec.code_version,
                category=str(question.get("category", "")),
                question=str(question.get("question", "")),
                error_type=type(exc).__name__,
                error_message=str(exc),
            )

    # -- lifecycle ----------------------------------------------------------

    @contextlib.contextmanager
    def _sigterm_flush(self) -> Iterator[None]:
        """Upload whatever is buffered when Vertex asks us to stop.

        Only installable from the main thread; in a worker thread we simply skip
        it rather than failing the shard.
        """

        def handler(signum, frame) -> None:  # noqa: ANN001, ARG001
            logger.warning("[%s] SIGTERM — flushing buffer", self.spec.shard_id)
            with contextlib.suppress(Exception):
                self._upload()
            raise SystemExit(143)

        try:
            previous = signal.signal(signal.SIGTERM, handler)
        except ValueError:
            logger.debug("Not on the main thread; SIGTERM flush not installed.")
            yield
            return
        try:
            yield
        finally:
            signal.signal(signal.SIGTERM, previous)

    def _write_marker(self, *, success: bool) -> None:
        """Record the terminal state, and clear the state it is no longer in.

        The clear is the whole point. Writing one marker and leaving the other
        made every ``_FAILED`` in the results bucket a lie: a shard that fails
        is retried by KFP, the retry resumes into the same directory and writes
        ``_SUCCESS``, and the stale ``_FAILED`` stays. All six ``_FAILED``
        markers across two experiments had a newer ``_SUCCESS`` beside them.

        Nothing in the code reads these, which is why it survived. The consumer
        is a human reading the bucket to find what broke, and for them the
        signal was wrong in precisely the case they open it for: it could not
        distinguish a shard that failed from one that failed and recovered.

        The opposite direction matters more. A shard that passed and later
        started failing kept advertising success, which is the marker someone
        trusts to mean the data is complete.
        """
        name, stale = ("_SUCCESS", "_FAILED") if success else ("_FAILED", "_SUCCESS")
        body = f"{self._done - self._failed} ok, {self._failed} failed"
        if self._abort_reason:
            body += f"\naborted: {self._abort_reason}"
        prefix = shard_prefix(self.spec)
        self.store.write_text(f"{prefix}/{name}", body + "\n")
        # After the write, not before: a crash between the two leaves both
        # markers, which is the state we already know how to read. Clearing
        # first would leave a completed shard with none at all.
        self.store.delete(f"{prefix}/{stale}")

    def _finish(self, *, success: bool, planned: int, already_done: int) -> ShardResult:
        """Write the marker and the summary, then return the result.

        One exit point rather than two: the summary is the only durable record
        of cache_warm_s and abort_reason, and a second return path is how a
        write like this gets forgotten on the branch nobody tests.
        """
        self._write_marker(success=success)
        result = self._result(planned=planned, already_done=already_done)
        write_summary(self.store, result)
        return result

    def _result(self, *, planned: int, already_done: int) -> ShardResult:
        return ShardResult(
            shard_id=self.spec.shard_id,
            experiment_id=self.spec.experiment_id,
            tier=self.spec.tier,
            approach=self.spec.approach,
            code_version=self.spec.code_version,
            # Copied from the spec, like code_version. Omitting it left every
            # summary recording "" -- the value is computed in preflight and
            # threaded into each shard as a cache-key input, then thrown away, so
            # nothing in the stored results said which corpus produced them.
            corpus_fingerprint=self.spec.corpus_fingerprint,
            planned=planned,
            already_done=already_done,
            executed=self._done,
            succeeded=self._done - self._failed,
            failed=self._failed,
            aborted=bool(self._abort_reason),
            abort_reason=self._abort_reason,
            cache_warm_s=self.cache_warm_s,
            elapsed_s=round(time.monotonic() - self._started, 3),
            attempt_path=self._attempt_path,
        )
