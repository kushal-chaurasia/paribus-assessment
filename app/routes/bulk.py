import asyncio
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import JSONResponse, Response
from fastapi.sse import EventSourceResponse, ServerSentEvent

from app.models import BatchStatus, UploadJob
from app.schemas import (
    CorrectionErrorResponse,
    CSVValidationResponse,
    JobAccepted,
    JobResponse,
    JobSummary,
)
from app.services import Correction, UploadedFile, bulk_service, parse_hospital_csv
from core.config import get_settings
from core.database import db

router = APIRouter(prefix="/hospitals/bulk", tags=["bulk"])

TERMINAL_STATUSES = [
    BatchStatus.COMPLETED,
    BatchStatus.FAILED,
    BatchStatus.ACTIVATION_FAILED,
    BatchStatus.ROLLED_BACK,
]
ASYNC_QUERY = Query(
    False,
    alias="async",
    description="Return 202 immediately and process in the background",
)


def _job_url(job_id: str, suffix: str = "") -> str:
    return f"{router.prefix}/jobs/{job_id}{suffix}"


async def _read_upload(file: UploadFile) -> UploadedFile:
    settings = get_settings()
    if file.filename and not file.filename.lower().endswith(".csv"):
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "Upload must be a .csv file"
        )
    content = await file.read(settings.max_csv_bytes + 1)
    if len(content) > settings.max_csv_bytes:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            f"CSV exceeds {settings.max_csv_bytes} bytes",
        )
    return UploadedFile(
        filename=file.filename or "upload.csv",
        content_type=file.content_type,
        content=content,
    )


def _invalid_csv(body: CSVValidationResponse) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content=body.model_dump(mode="json"),
    )


def _accepted(job: UploadJob) -> JSONResponse:
    body = JobAccepted(
        job_id=job.id,
        batch_id=job.batch_id,
        status=job.batch.status,
        total_hospitals=job.batch.total_rows,
        status_url=_job_url(job.id),
        events_url=_job_url(job.id, "/events"),
    )
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED, content=body.model_dump(mode="json")
    )


@router.post(
    "",
    response_model=JobResponse,
    responses={202: {"model": JobAccepted}, 422: {"model": CSVValidationResponse}},
)
async def bulk_create_hospitals(
    file: UploadFile = File(...), run_async: bool = ASYNC_QUERY
):
    upload = await _read_upload(file)
    parsed = parse_hospital_csv(upload.content, get_settings().max_csv_rows)
    if not parsed.valid:
        return _invalid_csv(parsed.to_response())

    job = await bulk_service.create_job(upload, parsed.rows)
    job = await bulk_service.start(job.id, background=run_async)
    if run_async:
        return _accepted(job)
    return JobResponse.from_job(job)


@router.post("/validate", response_model=CSVValidationResponse)
async def validate_csv(file: UploadFile = File(...)):
    upload = await _read_upload(file)
    return parse_hospital_csv(upload.content, get_settings().max_csv_rows).to_response()


@router.get("/jobs", response_model=list[JobSummary])
async def list_jobs(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)):
    jobs = await bulk_service.list_jobs(limit, offset)
    return [JobSummary.from_job(job) for job in jobs]


@router.get("/jobs/{job_id}", response_model=JobResponse)
async def get_job(job_id: str):
    return JobResponse.from_job(await bulk_service.get_job(job_id))


@router.get(
    "/jobs/{job_id}/file",
    response_class=Response,
    responses={200: {"content": {"text/csv": {}}}},
)
async def download_job_file(job_id: str):
    job = await bulk_service.get_job(job_id)
    filename = job.filename.replace('"', "")
    return Response(
        content=job.csv_content,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post(
    "/jobs/{job_id}/resume",
    response_model=JobResponse,
    responses={
        202: {"model": JobAccepted},
        422: {"model": CSVValidationResponse | CorrectionErrorResponse},
    },
)
async def resume_job(
    job_id: str,
    file: UploadFile | None = File(
        None,
        description=(
            "Optional corrected CSV: every row in the original order, or only the "
            "rows to fix with a 'row' column holding the original row number."
        ),
    ),
    run_async: bool = ASYNC_QUERY,
):
    correction = None
    if file is not None:
        upload = await _read_upload(file)
        parsed = parse_hospital_csv(
            upload.content, get_settings().max_csv_rows, allow_row_column=True
        )
        if not parsed.valid:
            return _invalid_csv(parsed.to_response())
        correction = Correction(file=upload, parsed=parsed)

    job = await bulk_service.resume(job_id, correction, background=run_async)
    if run_async:
        return _accepted(job)
    return JobResponse.from_job(job)


@router.delete("/jobs/{job_id}", response_model=JobResponse)
async def rollback_job(job_id: str):
    return JobResponse.from_job(await bulk_service.rollback(job_id))


async def _existing_job_id(job_id: str) -> str:
    # Own short scope: the request session would otherwise sit in an open
    # transaction for as long as the stream stays connected.
    async with db():
        await bulk_service.get_job(job_id)
    return job_id


@router.get("/jobs/{job_id}/events", response_class=EventSourceResponse)
async def job_events(
    job_id: str = Depends(_existing_job_id),
) -> AsyncIterator[ServerSentEvent]:
    """Streams job progress as Server-Sent Events until the job settles.

    Emits ``progress`` whenever the snapshot changes and a final ``complete``
    event before closing. Idle keep-alive pings are sent by FastAPI.
    """
    interval = get_settings().progress_poll_interval_seconds
    last_sent: JobResponse | None = None
    event_id = 0
    while True:
        async with db():
            snapshot = JobResponse.from_job(await bulk_service.get_job(job_id))

        finished = snapshot.status in TERMINAL_STATUSES and not bulk_service.is_active(
            job_id
        )
        if finished or snapshot != last_sent:
            event_id += 1
            yield ServerSentEvent(
                event="complete" if finished else "progress",
                id=str(event_id),
                data=snapshot,
                retry=3000 if event_id == 1 else None,
            )
            last_sent = snapshot
        if finished:
            return
        await asyncio.sleep(interval)
