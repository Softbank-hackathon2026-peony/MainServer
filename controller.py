from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, Header, status
from pydantic import BaseModel, Field

import service
from config import MAX_OBJECT_BYTES, MAX_PARTS

router = APIRouter()


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class UploadCreate(BaseModel):
    file_name: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(ge=1, le=MAX_OBJECT_BYTES)
    relative_path: Optional[str] = Field(default=None, min_length=1, max_length=900)


class CompletedPart(BaseModel):
    part_number: int = Field(ge=1, le=MAX_PARTS)
    etag: str = Field(min_length=1, max_length=200)


class UploadComplete(BaseModel):
    parts: list[CompletedPart] = Field(min_length=1, max_length=MAX_PARTS)


def response(data: dict) -> dict:
    return {"data": data, "request_id": f"req_{uuid4().hex}"}


@router.get("/status")
def get_status() -> dict[str, str]:
    return {"status": "ok", "service": "fawploy", "timestamp": datetime.now(timezone.utc).isoformat()}


@router.post("/api/v1/projects", status_code=status.HTTP_201_CREATED)
def create_project(body: ProjectCreate) -> dict:
    return response(service.create_project(body.name))


@router.post("/api/v1/projects/{project_id}/uploads/presign", status_code=status.HTTP_201_CREATED)
def presign_upload(project_id: str, body: UploadCreate, x_project_token: str = Header(...)) -> dict:
    return response(service.start_upload(project_id, x_project_token, body.file_name, body.size_bytes, body.relative_path))


@router.get("/api/v1/projects/{project_id}/uploads/{upload_id}/parts/{part_number}/presign")
def presign_part(project_id: str, upload_id: str, part_number: int, x_project_token: str = Header(...)) -> dict:
    return response(service.presign_part(project_id, x_project_token, upload_id, part_number))


@router.post("/api/v1/projects/{project_id}/uploads/{upload_id}/complete")
def complete_upload(project_id: str, upload_id: str, body: UploadComplete, x_project_token: str = Header(...)) -> dict:
    return response(service.complete_upload(project_id, x_project_token, upload_id, [part.model_dump() for part in body.parts]))


@router.delete("/api/v1/projects/{project_id}/uploads/{upload_id}", status_code=204)
def abort_upload(project_id: str, upload_id: str, x_project_token: str = Header(...)) -> None:
    service.abort_upload(project_id, x_project_token, upload_id)
