import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import select

from app.models import (
    Batch,
    BatchRow,
    BatchStatus,
    JobRun,
    RowStatus,
    RunStatus,
    RunTrigger,
    UploadJob,
)
from app.models.batch import utcnow
from app.schemas import CSVRowError
from app.services.csv_service import HospitalRow, ParsedCSV
from app.services.hospital_api import HospitalAPI, UpstreamError
from core.database import db

logger = logging.getLogger(__name__)

NOT_RESUMABLE = (BatchStatus.COMPLETED, BatchStatus.ROLLED_BACK)
UNFINISHED = (BatchStatus.PENDING, BatchStatus.PROCESSING)


class JobNotFoundError(Exception):
    pass


class JobConflictError(Exception):
    pass


class InvalidCorrectionsError(Exception):
    def __init__(self, errors: list[CSVRowError]) -> None:
        super().__init__("Corrected CSV cannot be applied to this job")
        self.errors = errors


@dataclass(frozen=True, slots=True)
class UploadedFile:
    filename: str
    content_type: str | None
    content: bytes

    @property
    def text(self) -> str:
        return self.content.decode("utf-8-sig")

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


@dataclass(frozen=True, slots=True)
class Correction:
    file: UploadedFile
    parsed: ParsedCSV


@dataclass(frozen=True, slots=True)
class RunRequest:
    trigger: RunTrigger
    corrected_rows: int = 0
    correction: UploadedFile | None = None


class BulkService:
    """Runs upload jobs against the Hospital Directory API.

    Each job owns one upstream batch; every processing attempt is recorded as a
    ``JobRun``. Row state is persisted as it changes, so a job can be streamed
    while it runs and resumed (optionally with a corrected CSV) after failures.
    """

    def __init__(self, api: HospitalAPI) -> None:
        self._api = api
        self._active: set[str] = set()
        self._tasks: set[asyncio.Task] = set()

    def is_active(self, job_id: str) -> bool:
        return job_id in self._active

    async def create_job(self, upload: UploadedFile, rows: list[HospitalRow]) -> UploadJob:
        batch = Batch(
            id=str(uuid4()),
            status=BatchStatus.PENDING,
            total_rows=len(rows),
            activated=False,
            rows=[
                BatchRow(
                    row_number=r.row,
                    name=r.name,
                    address=r.address,
                    phone=r.phone,
                    status=RowStatus.PENDING,
                    attempts=0,
                )
                for r in rows
            ],
        )
        job = UploadJob(
            id=str(uuid4()),
            batch=batch,
            filename=upload.filename,
            content_type=upload.content_type,
            size_bytes=len(upload.content),
            checksum_sha256=upload.checksum,
            csv_content=upload.text,
            runs=[],
        )
        db.session.add(job)
        await db.session.commit()
        return job

    async def get_job(self, job_id: str) -> UploadJob:
        job = await db.session.get(UploadJob, job_id)
        if job is None:
            raise JobNotFoundError(f"Job {job_id} not found")
        return job

    async def list_jobs(self, limit: int, offset: int) -> list[UploadJob]:
        result = await db.session.scalars(
            select(UploadJob)
            .order_by(UploadJob.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(result)

    async def start(self, job_id: str, *, background: bool) -> UploadJob:
        self._claim(job_id)
        return await self._dispatch(job_id, RunRequest(RunTrigger.INITIAL), background)

    async def resume(
        self, job_id: str, correction: Correction | None, *, background: bool
    ) -> UploadJob:
        self._claim(job_id)
        try:
            job = await self.get_job(job_id)
            if job.batch.status in NOT_RESUMABLE:
                raise JobConflictError(f"Job is {job.batch.status} and cannot be resumed")
            await self._reconcile(job.batch)
            corrected = (
                self._apply_corrections(job.batch, correction.parsed) if correction else 0
            )
            await db.session.commit()
        except BaseException:
            self._active.discard(job_id)
            raise

        request = RunRequest(
            trigger=RunTrigger.CORRECTION if correction else RunTrigger.RESUME,
            corrected_rows=corrected,
            correction=correction.file if correction else None,
        )
        return await self._dispatch(job_id, request, background)

    async def rollback(self, job_id: str) -> UploadJob:
        job = await self.get_job(job_id)
        if self.is_active(job_id):
            raise JobConflictError("Job is being processed; retry once it finishes")
        await self._api.delete_batch(job.batch_id)
        job.batch.status = BatchStatus.ROLLED_BACK
        job.batch.activated = False
        for row in job.batch.rows:
            if row.hospital_id is not None:
                row.status = RowStatus.ROLLED_BACK
        await db.session.commit()
        return job

    async def recover_interrupted(self) -> None:
        """Mark work left unfinished by a previous process as interrupted.

        Called at startup, when nothing can be running yet; interrupted jobs
        become resumable instead of appearing stuck in ``processing``.
        """
        message = "Interrupted by a server restart; resume to continue"
        runs = await db.session.scalars(
            select(JobRun).where(JobRun.status == RunStatus.RUNNING)
        )
        for run in runs:
            self._interrupt_run(run, message)
        batches = await db.session.scalars(
            select(Batch).where(Batch.status.in_(UNFINISHED))
        )
        for batch in batches:
            batch.status = BatchStatus.FAILED
            batch.error = message
        await db.session.commit()

    async def shutdown(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def _claim(self, job_id: str) -> None:
        if job_id in self._active:
            raise JobConflictError("Job is already being processed")
        self._active.add(job_id)

    async def _dispatch(
        self, job_id: str, request: RunRequest, background: bool
    ) -> UploadJob:
        if background:
            task = asyncio.create_task(self._run_in_background(job_id, request))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            return await self.get_job(job_id)
        try:
            return await self._execute(job_id, request)
        except Exception as exc:
            await self._mark_interrupted(job_id, f"Run aborted: {exc!r}")
            raise
        finally:
            self._active.discard(job_id)

    async def _run_in_background(self, job_id: str, request: RunRequest) -> None:
        try:
            async with db():
                await self._execute(job_id, request)
        except Exception as exc:
            logger.exception("Background processing failed for job %s", job_id)
            await self._mark_interrupted(job_id, f"Run aborted: {exc!r}")
        finally:
            self._active.discard(job_id)

    async def _execute(self, job_id: str, request: RunRequest) -> UploadJob:
        job = await self.get_job(job_id)
        batch = job.batch
        correction = request.correction
        run = JobRun(
            attempt=len(job.runs) + 1,
            trigger=request.trigger,
            status=RunStatus.RUNNING,
            rows_attempted=0,
            rows_succeeded=0,
            rows_failed=0,
            corrected_rows=request.corrected_rows,
            correction_filename=correction.filename if correction else None,
            correction_csv=correction.text if correction else None,
            started_at=utcnow(),
        )
        job.runs.append(run)
        batch.status = BatchStatus.PROCESSING
        batch.error = None
        await db.session.commit()
        started = time.perf_counter()

        # Rows already holding a hospital_id were created on an earlier run.
        pending = [row for row in batch.rows if row.hospital_id is None]
        session_lock = asyncio.Lock()
        await asyncio.gather(
            *(self._create_row(batch.id, row, session_lock) for row in pending)
        )

        succeeded = sum(1 for row in pending if row.hospital_id is not None)
        run.rows_attempted = len(pending)
        run.rows_succeeded = succeeded
        run.rows_failed = len(pending) - succeeded

        failed = batch.count(RowStatus.FAILED)
        if failed:
            batch.status = BatchStatus.FAILED
            batch.error = (
                f"{failed} hospital(s) failed; batch left inactive. "
                "Resume, optionally with a corrected CSV, to retry."
            )
        else:
            await self._activate(batch)

        run.status = RunStatus(batch.status.value)
        run.error = batch.error
        run.processing_time_seconds = round(time.perf_counter() - started, 3)
        run.finished_at = utcnow()
        await db.session.commit()
        return job

    async def _create_row(
        self, batch_id: str, row: BatchRow, session_lock: asyncio.Lock
    ) -> None:
        try:
            hospital = await self._api.create_hospital(
                name=row.name, address=row.address, phone=row.phone, batch_id=batch_id
            )
            hospital_id, error = int(hospital["id"]), None
        except UpstreamError as exc:
            hospital_id, error = None, str(exc)
        except (KeyError, TypeError, ValueError) as exc:
            hospital_id, error = None, f"Unexpected upstream response: {exc!r}"

        # An AsyncSession is not safe for concurrent use; the HTTP calls run in
        # parallel but writes are serialised. Committing per row keeps SSE live.
        async with session_lock:
            row.attempts += 1
            row.hospital_id = hospital_id
            row.error = error
            row.status = RowStatus.CREATED if hospital_id is not None else RowStatus.FAILED
            await db.session.commit()

    async def _activate(self, batch: Batch) -> None:
        try:
            await self._api.activate_batch(batch.id)
        except UpstreamError as exc:
            batch.status = BatchStatus.ACTIVATION_FAILED
            batch.error = f"All hospitals created but activation failed: {exc}"
            return
        batch.status = BatchStatus.COMPLETED
        batch.activated = True
        for row in batch.rows:
            row.status = RowStatus.CREATED_AND_ACTIVATED

    def _apply_corrections(self, batch: Batch, parsed: ParsedCSV) -> int:
        """Replace data of not-yet-created rows with values from a corrected CSV.

        Without a ``row`` column the file must contain every row, in order;
        with one it may list only the rows being fixed. Hospitals that already
        exist upstream cannot be changed. Nothing is modified if any row is
        invalid. Returns the number of rows changed.
        """
        if not parsed.explicit_rows and parsed.total_rows != batch.total_rows:
            raise InvalidCorrectionsError(
                [
                    CSVRowError(
                        row=0,
                        message=(
                            f"Corrected CSV has {parsed.total_rows} rows but the job "
                            f"has {batch.total_rows}; include every row or add a "
                            "'row' column"
                        ),
                    )
                ]
            )

        rows = {row.row_number: row for row in batch.rows}
        errors: list[CSVRowError] = []
        updates: list[tuple[BatchRow, HospitalRow]] = []
        for fix in parsed.rows:
            row = rows.get(fix.row)
            if row is None:
                errors.append(
                    CSVRowError(row=fix.row, field="row", message="Job has no such row")
                )
            elif (fix.name, fix.address, fix.phone) == (row.name, row.address, row.phone):
                continue
            elif row.hospital_id is not None:
                errors.append(
                    CSVRowError(
                        row=fix.row,
                        message="Hospital was already created and cannot be changed",
                    )
                )
            else:
                updates.append((row, fix))
        if errors:
            raise InvalidCorrectionsError(errors)

        for row, fix in updates:
            row.name, row.address, row.phone = fix.name, fix.address, fix.phone
            row.status = RowStatus.PENDING
            row.error = None
        return len(updates)

    async def _reconcile(self, batch: Batch) -> None:
        """Adopt hospitals that exist upstream but were never recorded locally.

        This covers a create that timed out after the upstream committed it, or a
        crash mid-run, so resuming does not create duplicates.
        """
        missing = [row for row in batch.rows if row.hospital_id is None]
        if not missing:
            return
        try:
            upstream = await self._api.list_batch(batch.id)
        except UpstreamError as exc:
            logger.warning("Skipping reconciliation for batch %s: %s", batch.id, exc)
            return

        known_ids = {row.hospital_id for row in batch.rows if row.hospital_id is not None}
        unclaimed = [h for h in upstream if h.get("id") not in known_ids]
        for row in missing:
            match = next(
                (
                    h
                    for h in unclaimed
                    if (h.get("name"), h.get("address"), h.get("phone") or None)
                    == (row.name, row.address, row.phone)
                ),
                None,
            )
            if match is not None:
                unclaimed.remove(match)
                row.hospital_id = int(match["id"])
                row.status = RowStatus.CREATED
                row.error = None

    async def _mark_interrupted(self, job_id: str, message: str) -> None:
        # Fresh session: the caller's may be mid-transaction or about to roll back.
        try:
            async with db():
                job = await self.get_job(job_id)
                for run in job.runs:
                    self._interrupt_run(run, message)
                if job.batch.status in UNFINISHED:
                    job.batch.status = BatchStatus.FAILED
                    job.batch.error = message
        except Exception:
            logger.exception("Could not mark job %s as interrupted", job_id)

    @staticmethod
    def _interrupt_run(run: JobRun, message: str) -> None:
        if run.status == RunStatus.RUNNING:
            run.status = RunStatus.INTERRUPTED
            run.error = message
            run.finished_at = utcnow()


bulk_service = BulkService(HospitalAPI())
