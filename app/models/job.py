from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.batch import Batch, enum_column, utcnow
from core.database import Base
from core.domain.enum import RunStatus, RunTrigger


class UploadJob(Base):
    """One uploaded CSV file and the batch it produced."""

    __tablename__ = "upload_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    batch_id: Mapped[str] = mapped_column(
        ForeignKey("batches.id", ondelete="CASCADE"), unique=True
    )
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str | None] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(Integer)
    checksum_sha256: Mapped[str] = mapped_column(String(64), index=True)
    csv_content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    batch: Mapped[Batch] = relationship(lazy="joined", innerjoin=True)
    runs: Mapped[list["JobRun"]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="JobRun.attempt",
    )

    @property
    def latest_run(self) -> "JobRun | None":
        return self.runs[-1] if self.runs else None


class JobRun(Base):
    """One processing attempt of a job: the initial run or a resume."""

    __tablename__ = "job_runs"
    __table_args__ = (UniqueConstraint("job_id", "attempt"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("upload_jobs.id", ondelete="CASCADE"), index=True
    )
    attempt: Mapped[int] = mapped_column(Integer)
    trigger: Mapped[RunTrigger] = mapped_column(enum_column(RunTrigger))
    status: Mapped[RunStatus] = mapped_column(
        enum_column(RunStatus), default=RunStatus.RUNNING
    )
    rows_attempted: Mapped[int] = mapped_column(Integer, default=0)
    rows_succeeded: Mapped[int] = mapped_column(Integer, default=0)
    rows_failed: Mapped[int] = mapped_column(Integer, default=0)
    corrected_rows: Mapped[int] = mapped_column(Integer, default=0)
    correction_filename: Mapped[str | None] = mapped_column(String(255))
    correction_csv: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processing_time_seconds: Mapped[float | None] = mapped_column(Float)

    job: Mapped[UploadJob] = relationship(back_populates="runs")
