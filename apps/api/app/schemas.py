import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from .models import Role, TaskKind, TaskStatus, UserStatus


class TelegramIdentity(BaseModel):
    telegram_id: int
    username: str | None = Field(default=None, max_length=64)


class CompleteProfile(BaseModel):
    full_name: str = Field(min_length=2, max_length=200)


class UserOut(BaseModel):
    id: uuid.UUID
    telegram_id: int
    telegram_username: str | None
    full_name: str | None
    role: Role
    status: str
    sector_id: uuid.UUID | None

    model_config = {"from_attributes": True}


class AuthenticatedUser(BaseModel):
    user: UserOut
    access_token: str
    token_type: str = "bearer"


class UserAdminUpdate(BaseModel):
    role: Role | None = None
    sector_id: uuid.UUID | None = None
    status: UserStatus | None = None


class UserSearchResult(UserOut):
    sector_name: str | None = None


class SectorCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    description: str | None = Field(default=None, max_length=2000)


class SectorOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    model_config = {"from_attributes": True}


class SectorUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    is_active: bool | None = None


class EventCreate(BaseModel):
    title: str = Field(min_length=2, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    starts_at: datetime
    ends_at: datetime | None = None
    budget: float | None = Field(default=None, ge=0)
    sector_id: uuid.UUID | None = None
    participant_ids: list[uuid.UUID] = Field(default_factory=list)


class EventOut(BaseModel):
    id: uuid.UUID
    title: str
    description: str | None
    starts_at: datetime
    ends_at: datetime | None
    budget: float | None
    sector_id: uuid.UUID | None
    model_config = {"from_attributes": True}


class EventUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=2, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    budget: float | None = Field(default=None, ge=0)
    sector_id: uuid.UUID | None = None


class EventParticipantCreate(BaseModel):
    user_id: uuid.UUID


class TaskCreate(BaseModel):
    title: str = Field(min_length=2, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    kind: TaskKind = TaskKind.INDIVIDUAL
    deadline: datetime
    event_id: uuid.UUID | None = None
    sector_id: uuid.UUID | None = None
    leader_id: uuid.UUID | None = None
    member_ids: list[uuid.UUID] = Field(default_factory=list)
    checklist: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("checklist")
    @classmethod
    def checklist_items_are_not_empty(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value):
            raise ValueError("Checklist items cannot be empty")
        return value


class TaskOut(BaseModel):
    id: uuid.UUID
    title: str
    description: str | None
    kind: TaskKind
    status: TaskStatus
    deadline: datetime
    creator_id: uuid.UUID
    leader_id: uuid.UUID | None
    cleanup_at: datetime | None
    model_config = {"from_attributes": True}


class TaskUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=2, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    deadline: datetime | None = None
    leader_id: uuid.UUID | None = None


class TaskMemberOut(BaseModel):
    user: UserOut
    is_creator: bool
    is_leader: bool


class TaskDetail(TaskOut):
    members: list[TaskMemberOut]
    checklist: list["ChecklistItemOut"]


class TaskChatMemberOut(BaseModel):
    user: UserOut
    state: str
    next_reminder_at: datetime | None
    last_error: str | None


class TaskChatOut(BaseModel):
    id: uuid.UUID
    telegram_chat_id: int | None
    status: str
    last_error: str | None
    cleanup_warned_at: datetime | None
    members: list[TaskChatMemberOut]


class TaskMemberCreate(BaseModel):
    user_id: uuid.UUID


class ChecklistItemCreate(BaseModel):
    title: str = Field(min_length=1, max_length=500)


class ChecklistItemOut(BaseModel):
    id: uuid.UUID
    title: str
    position: int
    is_completed: bool
    completed_by_id: uuid.UUID | None
    completed_at: datetime | None
    model_config = {"from_attributes": True}


class ChecklistUpdate(BaseModel):
    is_completed: bool


class ReportCreate(BaseModel):
    comment: str | None = Field(default=None, max_length=5000)


class ReportDecision(BaseModel):
    approved: bool
    reason: str | None = Field(default=None, max_length=2000)


class UploadRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=180)
    content_type: str
    size_bytes: int = Field(gt=0, le=10 * 1024 * 1024)


class UploadTarget(BaseModel):
    object_key: str
    upload_url: str
    fields: dict[str, str] = Field(default_factory=dict)
