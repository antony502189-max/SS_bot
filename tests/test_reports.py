from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from apps.api.app.models import (
    ReportStatus,
    Role,
    Task,
    TaskKind,
    TaskMember,
    TaskReport,
    TaskStatus,
    User,
    UserStatus,
)
from apps.api.app.routers.reports import decide_report, save_report_draft, submit_report
from apps.api.app.schemas import ReportCreate, ReportDecision
from apps.api.app.services import task_cleanup_at


async def group_task(session):
    creator = User(
        telegram_id=801,
        full_name="Руководитель Тест",
        normalized_full_name="руководитель тест",
        status=UserStatus.ACTIVE,
        role=Role.ADMIN,
    )
    participant = User(
        telegram_id=802,
        full_name="Исполнитель Тест",
        normalized_full_name="исполнитель тест",
        status=UserStatus.ACTIVE,
    )
    session.add_all([creator, participant])
    await session.flush()
    task = Task(
        title="Групповая задача",
        kind=TaskKind.GROUP,
        deadline=datetime.now(UTC) + timedelta(days=1),
        creator_id=creator.id,
        leader_id=creator.id,
        idempotency_key="report-lifecycle",
    )
    session.add(task)
    await session.flush()
    session.add_all(
        [
            TaskMember(task_id=task.id, user_id=creator.id, is_creator=True, is_leader=True),
            TaskMember(task_id=task.id, user_id=participant.id),
        ]
    )
    await session.commit()
    return creator, participant, task


@pytest.mark.asyncio
async def test_report_can_be_drafted_returned_resubmitted_and_approved(session) -> None:
    leader, participant, task = await group_task(session)

    draft = await save_report_draft(
        task.id, ReportCreate(comment="Черновик с фотографиями"), participant, session
    )
    assert draft.status == ReportStatus.DRAFT
    assert (await session.get(Task, task.id)).status == TaskStatus.ACTIVE

    submitted = await submit_report(
        task.id, ReportCreate(comment="Первая версия"), participant, session
    )
    assert submitted["status"] == TaskStatus.SUBMITTED
    await decide_report(
        task.id, ReportDecision(approved=False, reason="Добавьте фото"), leader, session
    )
    report = await session.scalar(select(TaskReport).where(TaskReport.task_id == task.id))
    assert report.status == ReportStatus.RETURNED
    assert (await session.get(Task, task.id)).status == TaskStatus.RETURNED

    resubmitted = await submit_report(
        task.id, ReportCreate(comment="Исправленная версия"), participant, session
    )
    assert resubmitted["status"] == TaskStatus.SUBMITTED
    await decide_report(task.id, ReportDecision(approved=True), leader, session)
    await session.refresh(report)
    assert report.status == ReportStatus.APPROVED
    assert report.comment == "Исправленная версия"
    assert (await session.get(Task, task.id)).status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_individual_submission_approves_and_closes_the_task(session) -> None:
    creator = User(
        telegram_id=811,
        full_name="Создатель Тест",
        normalized_full_name="создатель тест",
        status=UserStatus.ACTIVE,
        role=Role.ADMIN,
    )
    participant = User(
        telegram_id=812,
        full_name="Исполнитель Тест",
        normalized_full_name="исполнитель тест",
        status=UserStatus.ACTIVE,
    )
    session.add_all([creator, participant])
    await session.flush()
    deadline = datetime.now(UTC) + timedelta(days=1)
    task = Task(
        title="Индивидуальная задача",
        kind=TaskKind.INDIVIDUAL,
        deadline=deadline,
        creator_id=creator.id,
        idempotency_key="individual-report-lifecycle",
    )
    session.add(task)
    await session.flush()
    session.add_all(
        [
            TaskMember(task_id=task.id, user_id=creator.id, is_creator=True),
            TaskMember(task_id=task.id, user_id=participant.id),
        ]
    )
    await session.commit()

    result = await submit_report(
        task.id, ReportCreate(comment="Работа выполнена"), participant, session
    )
    report = await session.scalar(select(TaskReport).where(TaskReport.task_id == task.id))
    await session.refresh(task)

    assert result["status"] == TaskStatus.COMPLETED
    assert task.status == TaskStatus.COMPLETED
    assert task.completed_at is not None
    expected_cleanup = task_cleanup_at(task.completed_at, deadline)
    assert task.cleanup_at is not None
    assert task.cleanup_at.replace(tzinfo=UTC) == expected_cleanup
    assert report is not None
    assert report.status == ReportStatus.APPROVED
    assert report.approved_at == task.completed_at
