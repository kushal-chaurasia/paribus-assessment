from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models import BatchStatus, RowStatus, RunStatus, RunTrigger, UploadJob


class CSVRowError(BaseModel):
    row: int
    field: str | None = None
    message: str


class HospitalRowPreview(BaseModel):
    row: int
    name: str
    address: str
    phone: str | None = None


class CSVValidationResponse(BaseModel):
    valid: bool
    total_hospitals: int
    errors: list[CSVRowError]
    hospitals: list[HospitalRowPreview]


class CorrectionErrorResponse(BaseModel):
    detail: str
    errors: list[CSVRowError]


class HospitalResult(BaseModel):
    row: int
    hospital_id: int | None
    name: str
    status: RowStatus
    error: str | None = None


class JobRunResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    attempt: int
    trigger: RunTrigger
    status: RunStatus
    rows_attempted: int
    rows_succeeded: int
    rows_failed: int
    corrected_rows: int
    correction_filename: str | None
    error: str | None
    started_at: datetime
    finished_at: datetime | None
    processing_time_seconds: float | None


class JobResponse(BaseModel):
    job_id: str
    batch_id: str
    filename: str
    size_bytes: int
    checksum_sha256: str
    created_at: datetime
    status: BatchStatus
    total_hospitals: int
    processed_hospitals: int
    failed_hospitals: int
    pending_hospitals: int
    processing_time_seconds: float | None
    batch_activated: bool
    error: str | None = None
    hospitals: list[HospitalResult]
    runs: list[JobRunResponse]

    @classmethod
    def from_job(cls, job: UploadJob) -> "JobResponse":
        batch = job.batch
        latest = job.latest_run
        return cls(
            job_id=job.id,
            batch_id=batch.id,
            filename=job.filename,
            size_bytes=job.size_bytes,
            checksum_sha256=job.checksum_sha256,
            created_at=job.created_at,
            status=batch.status,
            total_hospitals=batch.total_rows,
            processed_hospitals=batch.count(
                RowStatus.CREATED, RowStatus.CREATED_AND_ACTIVATED
            ),
            failed_hospitals=batch.count(RowStatus.FAILED),
            pending_hospitals=batch.count(RowStatus.PENDING),
            processing_time_seconds=latest.processing_time_seconds if latest else None,
            batch_activated=batch.activated,
            error=batch.error,
            hospitals=[
                HospitalResult(
                    row=row.row_number,
                    hospital_id=row.hospital_id,
                    name=row.name,
                    status=row.status,
                    error=row.error,
                )
                for row in batch.rows
            ],
            runs=[JobRunResponse.model_validate(run) for run in job.runs],
        )


class JobSummary(BaseModel):
    job_id: str
    batch_id: str
    filename: str
    status: BatchStatus
    total_hospitals: int
    attempts: int
    created_at: datetime

    @classmethod
    def from_job(cls, job: UploadJob) -> "JobSummary":
        return cls(
            job_id=job.id,
            batch_id=job.batch_id,
            filename=job.filename,
            status=job.batch.status,
            total_hospitals=job.batch.total_rows,
            attempts=len(job.runs),
            created_at=job.created_at,
        )


class JobAccepted(BaseModel):
    job_id: str
    batch_id: str
    status: BatchStatus
    total_hospitals: int
    status_url: str
    events_url: str
