from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from robot.db.models import Task, TaskStatus


async def create_task(
    db: AsyncSession,
    owner_id: str,
    user_id: str,
    title: str,
    description: str | None = None,
    assigned_to: str | None = None,
    due_date: datetime | None = None,
    priority: int = 3,
) -> Task:
    task = Task(
        owner_id=owner_id,
        created_by=user_id,
        title=title,
        description=description,
        assigned_to=assigned_to,
        due_date=due_date,
        priority=priority,
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    return task


async def get_task(db: AsyncSession, task_id: str) -> Task | None:
    result = await db.execute(select(Task).where(Task.id == task_id))
    return result.scalar_one_or_none()


async def list_tasks(
    db: AsyncSession,
    owner_id: str,
    status: TaskStatus | None = None,
    assigned_to: str | None = None,
) -> list[Task]:
    stmt = select(Task).where(Task.owner_id == owner_id).order_by(Task.created_at.desc())
    if status:
        stmt = stmt.where(Task.status == status)
    if assigned_to:
        stmt = stmt.where(Task.assigned_to == assigned_to)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def update_task(db: AsyncSession, task_id: str, **kwargs) -> Task | None:
    task = await get_task(db, task_id)
    if not task:
        return None
    for key, value in kwargs.items():
        if value is not None and hasattr(task, key):
            setattr(task, key, value)
    await db.commit()
    await db.refresh(task)
    return task


async def complete_task(db: AsyncSession, task_id: str) -> Task | None:
    task = await get_task(db, task_id)
    if not task:
        return None
    task.status = TaskStatus.completed
    task.completed_at = datetime.utcnow()
    await db.commit()
    await db.refresh(task)
    return task


async def delete_task(db: AsyncSession, task_id: str) -> bool:
    task = await get_task(db, task_id)
    if not task:
        return False
    await db.delete(task)
    await db.commit()
    return True
