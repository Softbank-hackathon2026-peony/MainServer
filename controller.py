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


class AnalysisCreate(BaseModel):
    source_id: str = Field(min_length=1, max_length=100)


class AnalysisDecision(BaseModel):
    action: str = Field(pattern="^(revise|approve)$")
    target: Optional[str] = Field(default=None, max_length=100)
    revision_message: Optional[str] = Field(default=None, min_length=1, max_length=2000)


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


@router.get("/api/v1/projects/{project_id}/analyses/{analysis_id}")
def get_analysis(project_id: str, analysis_id: str, x_project_token: str = Header(...)) -> dict:
    return response(service.get_analysis(project_id, x_project_token, analysis_id))


@router.post("/api/v1/projects/{project_id}/analyses/{analysis_id}/build", status_code=status.HTTP_202_ACCEPTED)
def start_build(project_id: str, analysis_id: str, x_project_token: str = Header(...)) -> dict:
    return response(service.start_build(project_id, x_project_token, analysis_id))


@router.get("/api/v1/projects/{project_id}/analyses/{analysis_id}/build")
def get_build(project_id: str, analysis_id: str, x_project_token: str = Header(...)) -> dict:
    return response(service.get_build(project_id, x_project_token, analysis_id))


@router.post("/api/v1/projects/{project_id}/analyses", status_code=status.HTTP_202_ACCEPTED)
def start_analysis(
    project_id: str,
    body: AnalysisCreate,
    background_tasks: BackgroundTasks,
    x_project_token: str = Header(...),
) -> dict:
    service.require_project(project_id, x_project_token)
    item = service.table().get_item(Key={"pk": f"PROJECT#{project_id}", "sk": f"SOURCE#{body.source_id}"}).get("Item")
    if not item:
        raise service.ServiceError(404, "GitHub 소스 작업을 찾을 수 없습니다.")
    if item.get("status") != "ready":
        raise service.ServiceError(409, "소스 저장이 완료된 뒤 분석을 시작할 수 있습니다.")
    analysis_id = f"ana_{uuid4().hex}"
    timestamp = service.now()
    service.table().put_item(Item={
        "pk": f"PROJECT#{project_id}", "sk": f"ANALYSIS#{analysis_id}",
        "analysis_id": analysis_id, "source_id": body.source_id, "commit_sha": item["commit_sha"],
        "source_uri": f"s3://{service.SOURCE_BUCKET}/{item['s3_key']}",
        "result_key": f"projects/{project_id}/analyses/{analysis_id}.json",
        "status": "running", "created_at": timestamp, "updated_at": timestamp,
    })
    background_tasks.add_task(service.run_analysis, project_id, body.source_id, item["commit_sha"], item["s3_key"], analysis_id)
    return response({"analysis_id": analysis_id, "status": "running", "source_id": body.source_id})


@router.post("/api/v1/projects/{project_id}/analyses/{analysis_id}/decision", status_code=status.HTTP_202_ACCEPTED)
def decide_analysis(
    project_id: str,
    analysis_id: str,
    body: AnalysisDecision,
    background_tasks: BackgroundTasks,
    x_project_token: str = Header(...),
) -> dict:
    service.require_project(project_id, x_project_token)
    if body.action == "approve":
        if not body.target:
            raise service.ServiceError(422, "배포 대상을 선택해 주세요.")
        analysis = service.get_analysis(project_id, x_project_token, analysis_id)
        build = service.start_build(project_id, x_project_token, analysis_id)
        deployment_id = f"dep-{uuid4().hex}"
        service.table().put_item(Item={
            "pk": f"PROJECT#{project_id}", "sk": f"DEPLOYMENT#{deployment_id}",
            "deployment_id": deployment_id, "analysis_id": analysis_id,
            "status": "running", "step": "build", "target": body.target,
            "build_id": build["build_id"], "created_at": service.now(), "updated_at": service.now(),
        })
        background_tasks.add_task(service.monitor_deployment_build, project_id, deployment_id, analysis_id, build["build_id"])
        return response({"deployment_id": deployment_id, "status": "running"})
    if body.action != "revise" or not body.revision_message:
        raise service.ServiceError(422, "수정 요청 내용을 입력해 주세요.")
    previous = service.get_analysis(project_id, x_project_token, analysis_id)
    source_id = previous.get("source_id")
    source = service.table().get_item(Key={"pk": f"PROJECT#{project_id}", "sk": f"SOURCE#{source_id}"}).get("Item")
    if not source or source.get("status") != "ready":
        raise service.ServiceError(409, "원본 소스를 찾을 수 없습니다.")
    new_id = f"ana_{uuid4().hex}"
    timestamp = service.now()
    service.table().put_item(Item={
        "pk": f"PROJECT#{project_id}", "sk": f"ANALYSIS#{new_id}",
        "analysis_id": new_id, "source_id": source_id, "commit_sha": source["commit_sha"],
        "source_uri": f"s3://{service.SOURCE_BUCKET}/{source['s3_key']}",
        "result_key": f"projects/{project_id}/analyses/{new_id}.json",
        "status": "running", "created_at": timestamp, "updated_at": timestamp,
    })
    background_tasks.add_task(
        service.run_analysis, project_id, source_id, source["commit_sha"], source["s3_key"], new_id,
        body.revision_message, previous.get("recommendation"),
    )
    return response({"analysis_id": new_id, "status": "running", "source_id": source_id})


@router.get("/api/v1/projects/{project_id}/deployments/{deployment_id}")
def get_deployment(project_id: str, deployment_id: str, x_project_token: str = Header(...)) -> dict:
    return response(service.get_deployment(project_id, x_project_token, deployment_id))


@router.delete("/api/v1/projects/{project_id}/deployments/{deployment_id}", status_code=status.HTTP_204_NO_CONTENT)
def stop_deployment(project_id: str, deployment_id: str, x_project_token: str = Header(...)) -> None:
    service.stop_deployment(project_id, x_project_token, deployment_id)
    return None
