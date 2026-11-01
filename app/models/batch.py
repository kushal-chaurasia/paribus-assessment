from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.database import Base
from core.domain.enum import BatchStatus, RowStatus


def utcnow() -> datetime:
    return datetime.now(UTC)


def enum_column(enum_cls: type[StrEnum]) -> Enum:
    return Enum(enum_cls, native_enum=False, length=32, values_callable=lambda e: [m.value for m in e])


class Batch(Base):
    __tablename__ = "batches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    status: Mapped[BatchStatus] = mapped_column(enum_column(BatchStatus), default=BatchStatus.PENDING)
    total_rows: Mapped[int] = mapped_column(Integer)
    activated: Mapped[bool] = mapped_column(default=False)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    rows: Mapped[list["BatchRow"]] = relationship(
        back_populates="batch",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="BatchRow.row_number",
    )

    def count(self, *statuses: RowStatus) -> int:
        return sum(1 for row in self.rows if row.status in statuses)


class BatchRow(Base):
    __tablename__ = "batch_rows"
    __table_args__ = (UniqueConstraint("batch_id", "row_number"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    batch_id: Mapped[str] = mapped_column(ForeignKey("batches.id", ondelete="CASCADE"), index=True)
    row_number: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(255))
    address: Mapped[str] = mapped_column(String(500))
    phone: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[RowStatus] = mapped_column(enum_column(RowStatus), default=RowStatus.PENDING)
    hospital_id: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)

    batch: Mapped[Batch] = relationship(back_populates="rows")
