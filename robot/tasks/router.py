from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from robot.auth.dependencies import get_current_user
from robot.db.database import get_db
from robot.db.models import TaskStatus, User
from robot.i18n import _
from robot.tasks.models import CreateTaskRequest, TaskResponse, UpdateTaskRequest
from robot.tasks.service import (
    complete_task,
    create_task,
    delete_task,
    get_task,
    list_tasks,
    update_task,
)

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


@router.get("", response_model=list[TaskResponse])
async def get_tasks(
    owner_id: str = Query(...),
    status: TaskStatus | None = Query(None),
    assigned_to: str | None = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await list_tasks(db, owner_id, status, assigned_to)


@router.post("", response_model=TaskResponse, status_code=201)
async def create(
    owner_id: str = Query(...),
    req: CreateTaskRequest = ...,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await create_task(
        db,
        owner_id=owner_id,
        user_id=user.id,
        title=req.title,
        description=req.description,
        assigned_to=req.assigned_to,
        due_date=req.due_date,
        priority=req.priority,
    )


@router.get("/{task_id}", response_model=TaskResponse)
async def get_one(
    task_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    task = await get_task(db, task_id)
    if not task:
        raise HTTPException(status_code=404, detail=_("TASK_NOT_FOUND"))
    return task


@router.put("/{task_id}", response_model=TaskResponse)
async def update(
    task_id: str,
    req: UpdateTaskRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    task = await update_task(db, task_id, **req.model_dump(exclude_none=True))
    if not task:
        raise HTTPException(status_code=404, detail=_("TASK_NOT_FOUND"))
    return task


@router.patch("/{task_id}/complete", response_model=TaskResponse)
async def mark_complete(
    task_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    task = await complete_task(db, task_id)
    if not task:
        raise HTTPException(status_code=404, detail=_("TASK_NOT_FOUND"))
    return task


@router.delete("/{task_id}", status_code=204)
async def delete(
    task_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not await delete_task(db, task_id):
        raise HTTPException(status_code=404, detail=_("TASK_NOT_FOUND"))
