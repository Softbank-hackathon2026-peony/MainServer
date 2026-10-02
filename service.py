import hashlib
import hmac
import math
import mimetypes
import secrets
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Optional
from uuid import uuid4

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from config import (
    AWS_REGION,
    MAX_PARTS,
    MAX_PART_BYTES,
    MIN_PART_BYTES,
    MIB,
    PROJECTS_TABLE,
    UPLOAD_BUCKET,
)


class ServiceError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


@lru_cache
def table():
    if not PROJECTS_TABLE:
        raise ServiceError(503, "프로젝트 저장소가 설정되지 않았습니다.")
    return boto3.resource("dynamodb", region_name=AWS_REGION).Table(PROJECTS_TABLE)


@lru_cache
def s3():
    if not UPLOAD_BUCKET:
        raise ServiceError(503, "업로드 저장소가 설정되지 않았습니다.")
    return boto3.client(
        "s3",
        region_name=AWS_REGION,
        endpoint_url=f"https://s3.{AWS_REGION}.amazonaws.com",
        config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
    )


def require_project(project_id: str, project_token: str) -> None:
    try:
        item = table().get_item(Key={"pk": f"PROJECT#{project_id}", "sk": "META"}).get("Item")
    except ClientError as exc:
        raise ServiceError(503, "프로젝트를 확인할 수 없습니다.") from exc
    token_hash = hashlib.sha256(project_token.encode()).hexdigest()
    if not item or not hmac.compare_digest(item["token_hash"], token_hash):
        raise ServiceError(404, "프로젝트를 찾을 수 없습니다.")


def upload_record(project_id: str, upload_id: str) -> dict:
    try:
        item = table().get_item(Key={"pk": f"PROJECT#{project_id}", "sk": f"UPLOAD#{upload_id}"}).get("Item")
    except ClientError as exc:
        raise ServiceError(503, "업로드 작업을 확인할 수 없습니다.") from exc
    if not item:
        raise ServiceError(404, "업로드 작업을 찾을 수 없습니다.")
    return item


def create_project(name: str) -> dict:
    clean_name = name.strip()
    if not clean_name:
        raise ServiceError(422, "프로젝트 이름을 입력해 주세요.")
    project_id = f"prj_{uuid4().hex}"
    token = secrets.token_urlsafe(32)
    try:
        table().put_item(
            Item={
                "pk": f"PROJECT#{project_id}",
                "sk": "META",
                "name": clean_name,
                "token_hash": hashlib.sha256(token.encode()).hexdigest(),
                "created_at": now(),
            },
            ConditionExpression="attribute_not_exists(pk)",
        )
    except ClientError as exc:
        raise ServiceError(503, "프로젝트를 생성할 수 없습니다.") from exc
    return {"project_id": project_id, "project_token": token, "name": clean_name}


def start_upload(project_id: str, project_token: str, file_name: str, size_bytes: int, relative_path: Optional[str] = None) -> dict:
    require_project(project_id, project_token)
    original_name = Path(file_name).name
    if original_name in {"", ".", ".."} or "\\" in file_name or any(ord(char) < 32 for char in file_name):
        raise ServiceError(422, "파일 이름이 올바르지 않습니다.")
    object_path = relative_path or original_name
    segments = object_path.split("/")
    if (
        object_path.startswith("/")
        or any(segment in {"", ".", ".."} for segment in segments)
        or segments[-1] != original_name
        or "\\" in object_path
        or any(ord(char) < 32 for char in object_path)
    ):
        raise ServiceError(422, "파일 경로가 올바르지 않습니다.")
    upload_id = f"upl_{uuid4().hex}"
    object_key = f"projects/{project_id}/uploads/{upload_id}/{object_path}"
    if len(object_key.encode("utf-8")) > 1024:
        raise ServiceError(422, "파일 경로가 너무 깁니다.")
    content_type = mimetypes.guess_type(original_name)[0] or "application/octet-stream"
    if Path(original_name).suffix.lower() in {".ts", ".tgz"}:
        content_type = "application/octet-stream"
    part_size = max(MIN_PART_BYTES, math.ceil(size_bytes / MAX_PARTS / MIB) * MIB)
    if part_size > MAX_PART_BYTES:
        raise ServiceError(413, "S3에서 지원하는 최대 객체 크기를 초과했습니다.")
    total_parts = math.ceil(size_bytes / part_size)
    try:
        s3_upload_id = s3().create_multipart_upload(Bucket=UPLOAD_BUCKET, Key=object_key, ContentType=content_type)["UploadId"]
    except ClientError as exc:
        raise ServiceError(503, "S3 업로드를 시작할 수 없습니다.") from exc
    try:
        table().put_item(
            Item={
                "pk": f"PROJECT#{project_id}",
                "sk": f"UPLOAD#{upload_id}",
                "file_name": original_name,
                "relative_path": object_path,
                "size_bytes": size_bytes,
                "content_type": content_type,
                "object_key": object_key,
                "s3_upload_id": s3_upload_id,
                "part_size": part_size,
                "total_parts": total_parts,
                "status": "uploading",
                "created_at": now(),
            },
            ConditionExpression="attribute_not_exists(pk) AND attribute_not_exists(sk)",
        )
    except ClientError as exc:
        s3().abort_multipart_upload(Bucket=UPLOAD_BUCKET, Key=object_key, UploadId=s3_upload_id)
        raise ServiceError(503, "업로드 작업을 저장할 수 없습니다.") from exc
    return {"upload_id": upload_id, "part_size": part_size, "total_parts": total_parts}


def presign_part(project_id: str, project_token: str, upload_id: str, part_number: int) -> dict:
    require_project(project_id, project_token)
    record = upload_record(project_id, upload_id)
    if record["status"] != "uploading":
        raise ServiceError(409, "업로드할 수 없는 상태입니다.")
    if not 1 <= part_number <= record["total_parts"]:
        raise ServiceError(400, "잘못된 파일 조각 번호입니다.")
    url = s3().generate_presigned_url(
        "upload_part",
        Params={
            "Bucket": UPLOAD_BUCKET,
            "Key": record["object_key"],
            "UploadId": record["s3_upload_id"],
            "PartNumber": part_number,
        },
        ExpiresIn=900,
        HttpMethod="PUT",
    )
    return {"upload_url": url, "expires_in": 900}


def complete_upload(project_id: str, project_token: str, upload_id: str, parts: list[dict]) -> dict:
    require_project(project_id, project_token)
    record = upload_record(project_id, upload_id)
    if record["status"] == "uploaded":
        return {"project_id": project_id, "upload_id": upload_id, "status": "uploaded"}
    if record["status"] != "uploading":
        raise ServiceError(409, "완료할 수 없는 업로드 상태입니다.")
    total_parts = int(record["total_parts"])
    if len(parts) != total_parts or [part["part_number"] for part in parts] != list(range(1, total_parts + 1)):
        raise ServiceError(422, "파일 조각 목록이 올바르지 않습니다.")
    try:
        s3().complete_multipart_upload(
            Bucket=UPLOAD_BUCKET,
            Key=record["object_key"],
            UploadId=record["s3_upload_id"],
            MultipartUpload={"Parts": [{"PartNumber": part["part_number"], "ETag": part["etag"]} for part in parts]},
        )
        obj = s3().head_object(Bucket=UPLOAD_BUCKET, Key=record["object_key"])
        if obj["ContentLength"] != record["size_bytes"] or obj.get("ContentType") != record["content_type"]:
            s3().delete_object(Bucket=UPLOAD_BUCKET, Key=record["object_key"])
            raise ServiceError(422, "업로드된 파일 정보가 요청과 다릅니다. 다시 시도해 주세요.")
        table().update_item(
            Key={"pk": f"PROJECT#{project_id}", "sk": f"UPLOAD#{upload_id}"},
            UpdateExpression="SET #s = :uploaded, completed_at = :completed_at",
            ConditionExpression="#s = :uploading",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":uploaded": "uploaded", ":uploading": "uploading", ":completed_at": now()},
        )
    except ClientError as exc:
        raise ServiceError(503, "업로드 완료를 확인할 수 없습니다.") from exc
    return {"project_id": project_id, "upload_id": upload_id, "status": "uploaded"}


def abort_upload(project_id: str, project_token: str, upload_id: str) -> None:
    require_project(project_id, project_token)
    record = upload_record(project_id, upload_id)
    if record["status"] != "uploading":
        raise ServiceError(409, "중단할 수 없는 업로드 상태입니다.")
    try:
        s3().abort_multipart_upload(Bucket=UPLOAD_BUCKET, Key=record["object_key"], UploadId=record["s3_upload_id"])
        table().update_item(
            Key={"pk": f"PROJECT#{project_id}", "sk": f"UPLOAD#{upload_id}"},
            UpdateExpression="SET #s = :aborted",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":aborted": "aborted"},
        )
    except ClientError as exc:
        raise ServiceError(503, "업로드를 중단할 수 없습니다.") from exc
