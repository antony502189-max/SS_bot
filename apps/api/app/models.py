import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql import func


class Base(DeclarativeBase):
    pass


class Role(enum.StrEnum):
    PARTICIPANT = "participant"
    SECTOR_HEAD = "sector_head"
    ADMIN = "admin"


class UserStatus(enum.StrEnum):
    PENDING_PROFILE = "pending_profile"
    NEEDS_USERNAME = "needs_username"
    ACTIVE = "active"
    INACTIVE = "inactive"
    BLOCKED = "blocked"


class TaskKind(enum.StrEnum):
    INDIVIDUAL = "individual"
    GROUP = "group"


class TaskStatus(enum.StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    SUBMITTED = "submitted"
    RETURNED = "returned"
    COMPLETED = "completed"
    OVERDUE = "overdue"
    CANCELLED = "cancelled"


class ChatStatus(enum.StrEnum):
    PENDING = "pending_creation"
    CREATING = "creating"
    READY = "ready"
    DEGRADED = "degraded"
    CLEANUP_PENDING = "cleanup_pending"
    DELETED = "deleted"
    FAILED = "failed"


class MembershipState(enum.StrEnum):
    PENDING = "pending"
    INVITED = "invited"
    JOINED = "joined"
    NOT_JOINED = "not_joined"
    FAILED = "failed"


class UUIDTimestampMixin:
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class User(UUIDTimestampMixin, Base):
    __tablename__ = "users"
    telegram_id: Mapped[int] = mapped_column(unique=True, index=True)
    telegram_username: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    full_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    normalized_full_name: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    role: Mapped[Role] = mapped_column(default=Role.PARTICIPANT)
    status: Mapped[UserStatus] = mapped_column(default=UserStatus.PENDING_PROFILE)
    sector_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("sectors.id"), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("ix_users_full_name_trgm", "normalized_full_name"),)


class Sector(UUIDTimestampMixin, Base):
    __tablename__ = "sectors"
    name: Mapped[str] = mapped_column(String(120), unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Event(UUIDTimestampMixin, Base):
    __tablename__ = "events"
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    budget: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    sector_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("sectors.id"), nullable=True)
    created_by_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))


class EventParticipant(UUIDTimestampMixin, Base):
    __tablename__ = "event_participants"
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    __table_args__ = (UniqueConstraint("event_id", "user_id", name="uq_event_participant"),)


class Task(UUIDTimestampMixin, Base):
    __tablename__ = "tasks"
    event_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("events.id"), nullable=True)
    sector_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("sectors.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    kind: Mapped[TaskKind] = mapped_column(default=TaskKind.INDIVIDUAL)
    status: Mapped[TaskStatus] = mapped_column(default=TaskStatus.ACTIVE, index=True)
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    creator_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    leader_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    cleanup_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TaskMember(UUIDTimestampMixin, Base):
    __tablename__ = "task_members"
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    is_creator: Mapped[bool] = mapped_column(Boolean, default=False)
    is_leader: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (UniqueConstraint("task_id", "user_id", name="uq_task_member"),)


class TaskChecklistItem(UUIDTimestampMixin, Base):
    __tablename__ = "task_checklist_items"
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(500))
    position: Mapped[int] = mapped_column(Integer)
    is_completed: Mapped[bool] = mapped_column(Boolean, default=False)
    completed_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (UniqueConstraint("task_id", "position", name="uq_task_checklist_position"),)


class TaskReport(UUIDTimestampMixin, Base):
    __tablename__ = "task_reports"
    task_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), unique=True
    )
    submitted_by_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    approval_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TaskPhoto(UUIDTimestampMixin, Base):
    __tablename__ = "task_photos"
    report_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("task_reports.id", ondelete="CASCADE"))
    object_key: Mapped[str] = mapped_column(String(500), unique=True)
    content_type: Mapped[str] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(Integer)
    uploaded_by_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))


class TaskChat(UUIDTimestampMixin, Base):
    __tablename__ = "task_chats"
    task_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), unique=True
    )
    telegram_chat_id: Mapped[int | None] = mapped_column(unique=True, nullable=True)
    status: Mapped[ChatStatus] = mapped_column(default=ChatStatus.PENDING)
    cleanup_warned_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class TaskChatMember(UUIDTimestampMixin, Base):
    __tablename__ = "task_chat_members"
    task_chat_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("task_chats.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    state: Mapped[MembershipState] = mapped_column(default=MembershipState.PENDING, index=True)
    invite_link: Mapped[str | None] = mapped_column(String(500), nullable=True)
    invite_link_created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_reminder_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    last_reminder_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reminder_count: Mapped[int] = mapped_column(Integer, default=0)
    joined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    __table_args__ = (UniqueConstraint("task_chat_id", "user_id", name="uq_task_chat_member"),)


class Notification(UUIDTimestampMixin, Base):
    __tablename__ = "notifications"
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tasks.id"), nullable=True, index=True
    )
    event_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("events.id"), nullable=True, index=True
    )
    type: Mapped[str] = mapped_column(String(80))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    scheduled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class AuditLog(UUIDTimestampMixin, Base):
    __tablename__ = "audit_logs"
    actor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(100), index=True)
    entity_type: Mapped[str] = mapped_column(String(80))
    entity_id: Mapped[str] = mapped_column(String(64))
    details: Mapped[dict] = mapped_column(JSON, default=dict)


class OutboxEvent(UUIDTimestampMixin, Base):
    __tablename__ = "outbox_events"
    event_type: Mapped[str] = mapped_column(String(100), index=True)
    aggregate_type: Mapped[str] = mapped_column(String(80))
    aggregate_id: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
