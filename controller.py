from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Header, status
from pydantic import BaseModel, Field

import service

router = APIRouter()


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class GitHubSourceCreate(BaseModel):
    github_url: str = Field(min_length=1, max_length=2048)
    ref: Optional[str] = Field(default=None, min_length=1, max_length=200)


def response(data: dict) -> dict:
    return {"data": data, "request_id": f"req_{uuid4().hex}"}


@router.get("/status")
def get_status() -> dict[str, str]:
    return {"status": "ok", "service": "fawploy", "timestamp": datetime.now(timezone.utc).isoformat()}


@router.post("/api/v1/projects", status_code=status.HTTP_201_CREATED)
def create_project(body: ProjectCreate) -> dict:
    return response(service.create_project(body.name))


@router.post("/api/v1/projects/{project_id}/sources/github", status_code=status.HTTP_202_ACCEPTED)
def create_github_source(
    project_id: str,
    body: GitHubSourceCreate,
    background_tasks: BackgroundTasks,
    x_project_token: str = Header(...),
) -> dict:
    data = service.create_github_source(project_id, x_project_token, body.github_url, body.ref)
    background_tasks.add_task(
        service.ingest_github_source,
        project_id,
        data["source_id"],
        data["owner"],
        data["repo"],
        data["commit_sha"],
    )
    return response({key: value for key, value in data.items() if key not in {"owner", "repo"}})


@router.get("/api/v1/projects/{project_id}/sources/{source_id}")
def get_github_source(project_id: str, source_id: str, x_project_token: str = Header(...)) -> dict:
    return response(service.get_github_source(project_id, x_project_token, source_id))
