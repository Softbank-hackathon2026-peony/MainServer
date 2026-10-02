import hashlib
import hmac
import json
import logging
import re
import secrets
from datetime import datetime, timezone
from functools import lru_cache
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.exceptions import ClientError

from config import AGENTCORE_REGION, AGENT_RUNTIME_ARN, AWS_REGION, GITHUB_TOKEN, PROJECTS_TABLE, SOURCE_BUCKET

logger = logging.getLogger(__name__)
GITHUB_API = "https://api.github.com"
OWNER_PATTERN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?")
REPO_PATTERN = re.compile(r"[A-Za-z0-9_.-]+")
REF_PATTERN = re.compile(r"[A-Za-z0-9._/-]+")
SHA_PATTERN = re.compile(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}")
TRANSFER = TransferConfig(multipart_threshold=8 * 1024 * 1024, multipart_chunksize=16 * 1024 * 1024)


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
    if not SOURCE_BUCKET:
        raise ServiceError(503, "소스 저장소가 설정되지 않았습니다.")
    return boto3.client("s3", region_name=AWS_REGION)


@lru_cache
def agentcore():
    if not AGENT_RUNTIME_ARN:
        raise ServiceError(503, "AgentCore Runtime이 설정되지 않았습니다.")
    return boto3.client("bedrock-agentcore", region_name=AGENTCORE_REGION)


def require_project(project_id: str, project_token: str) -> None:
    try:
        item = table().get_item(Key={"pk": f"PROJECT#{project_id}", "sk": "META"}).get("Item")
    except ClientError as exc:
        raise ServiceError(503, "프로젝트를 확인할 수 없습니다.") from exc
    token_hash = hashlib.sha256(project_token.encode()).hexdigest()
    if not item or not hmac.compare_digest(item["token_hash"], token_hash):
        raise ServiceError(404, "프로젝트를 찾을 수 없습니다.")


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


def parse_github_url(github_url: str) -> tuple[str, str, str]:
    try:
        parsed = urlsplit(github_url.strip())
        valid_origin = parsed.scheme == "https" and parsed.hostname == "github.com" and parsed.port is None
    except ValueError:
        valid_origin = False
    if not valid_origin or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ServiceError(422, "공개 GitHub 저장소의 HTTPS 주소를 입력해 주세요.")
    segments = parsed.path.strip("/").split("/")
    if len(segments) != 2:
        raise ServiceError(422, "저장소 주소는 https://github.com/소유자/저장소 형식이어야 합니다.")
    owner, repo = segments
    if repo.endswith(".git"):
        repo = repo[:-4]
    if not OWNER_PATTERN.fullmatch(owner) or not REPO_PATTERN.fullmatch(repo):
        raise ServiceError(422, "GitHub 저장소 주소가 올바르지 않습니다.")
    return owner, repo, f"https://github.com/{owner}/{repo}"


def github_open(path: str):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "Fawploy-MainServer"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    request = Request(f"{GITHUB_API}{path}", headers=headers)
    try:
        return urlopen(request, timeout=30)
    except HTTPError as exc:
        if exc.code == 404:
            raise ServiceError(404, "공개 GitHub 저장소 또는 커밋을 찾을 수 없습니다.") from exc
        if exc.code in {403, 429}:
            raise ServiceError(503, "GitHub 요청이 제한되었습니다. 잠시 후 다시 시도해 주세요.") from exc
        raise ServiceError(502, "GitHub에서 소스를 가져올 수 없습니다.") from exc
    except (URLError, TimeoutError) as exc:
        raise ServiceError(502, "GitHub 연결에 실패했습니다. 다시 시도해 주세요.") from exc


def github_json(path: str) -> dict:
    try:
        with github_open(path) as response:
            return json.load(response)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ServiceError(502, "GitHub 응답을 확인할 수 없습니다.") from exc


def create_github_source(project_id: str, project_token: str, github_url: str, ref: str | None) -> dict:
    require_project(project_id, project_token)
    owner, repo, canonical_url = parse_github_url(github_url)
    if not SOURCE_BUCKET:
        raise ServiceError(503, "소스 저장소가 설정되지 않았습니다.")
    if ref is not None:
        ref = ref.strip()
        if not REF_PATTERN.fullmatch(ref):
            raise ServiceError(422, "브랜치, 태그 또는 커밋 값이 올바르지 않습니다.")
    repository = github_json(f"/repos/{owner}/{repo}")
    if repository.get("private") or not repository.get("default_branch"):
        raise ServiceError(422, "공개되어 있고 비어 있지 않은 GitHub 저장소만 사용할 수 있습니다.")
    chosen_ref = ref or repository["default_branch"]
    commit = github_json(f"/repos/{owner}/{repo}/commits/{quote(chosen_ref, safe='')}")
    commit_sha = commit.get("sha", "")
    if not SHA_PATTERN.fullmatch(commit_sha):
        raise ServiceError(502, "GitHub 커밋을 확인할 수 없습니다.")
    source_id = f"src_{uuid4().hex}"
    object_key = f"projects/{project_id}/sources/{source_id}/{commit_sha}.tar.gz"
    timestamp = now()
    try:
        table().put_item(
            Item={
                "pk": f"PROJECT#{project_id}",
                "sk": f"SOURCE#{source_id}",
                "repository_url": canonical_url,
                "owner": owner,
                "repo": repo,
                "ref": chosen_ref,
                "commit_sha": commit_sha,
                "s3_key": object_key,
                "status": "queued",
                "created_at": timestamp,
                "updated_at": timestamp,
            },
            ConditionExpression="attribute_not_exists(pk) AND attribute_not_exists(sk)",
        )
    except ClientError as exc:
        raise ServiceError(503, "GitHub 소스 작업을 저장할 수 없습니다.") from exc
    return {
        "source_id": source_id,
        "status": "queued",
        "repository_url": canonical_url,
        "ref": chosen_ref,
        "commit_sha": commit_sha,
        "owner": owner,
        "repo": repo,
    }


def update_source_status(project_id: str, source_id: str, status: str, error_message: str | None = None) -> None:
    values = {":status": status, ":updated_at": now()}
    expression = "SET #status = :status, updated_at = :updated_at"
    if error_message:
        expression += ", error_message = :error_message"
        values[":error_message"] = error_message
    table().update_item(
        Key={"pk": f"PROJECT#{project_id}", "sk": f"SOURCE#{source_id}"},
        UpdateExpression=expression,
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues=values,
        ConditionExpression="attribute_exists(pk) AND attribute_exists(sk)",
    )


def ingest_github_source(project_id: str, source_id: str, owner: str, repo: str, commit_sha: str) -> None:
    object_key = f"projects/{project_id}/sources/{source_id}/{commit_sha}.tar.gz"
    try:
        update_source_status(project_id, source_id, "downloading")
        with github_open(f"/repos/{owner}/{repo}/tarball/{commit_sha}") as archive:
            s3().upload_fileobj(
                archive,
                SOURCE_BUCKET,
                object_key,
                ExtraArgs={"ContentType": "application/gzip", "Metadata": {"commit-sha": commit_sha}},
                Config=TRANSFER,
            )
        update_source_status(project_id, source_id, "ready")
    except Exception as exc:
        logger.exception("GitHub source ingestion failed for %s", source_id)
        message = exc.message if isinstance(exc, ServiceError) else "GitHub 소스를 저장하지 못했습니다. 다시 시도해 주세요."
        try:
            update_source_status(project_id, source_id, "failed", message)
        except Exception:
            logger.exception("Failed to save source failure status for %s", source_id)


def start_analysis(project_id: str, project_token: str, source_id: str) -> dict:
    require_project(project_id, project_token)
    item = table().get_item(Key={"pk": f"PROJECT#{project_id}", "sk": f"SOURCE#{source_id}"}).get("Item")
    if not item:
        raise ServiceError(404, "GitHub 소스 작업을 찾을 수 없습니다.")
    if item.get("status") != "ready":
        raise ServiceError(409, "소스 저장이 완료된 뒤 분석을 시작할 수 있습니다.")
    analysis_id = f"ana_{uuid4().hex}"
    run_analysis(project_id, source_id, item["commit_sha"], item["s3_key"], analysis_id)
    return {"analysis_id": analysis_id, "status": "running", "source_id": source_id}


def run_analysis(project_id: str, source_id: str, commit_sha: str, source_key: str, analysis_id: str,
                 revision_message: str | None = None, previous_recommendation: dict | None = None) -> None:
    result_key = f"projects/{project_id}/analyses/{analysis_id}.json"
    source_uri = f"s3://{SOURCE_BUCKET}/{source_key}"
    timestamp = now()
    table().put_item(Item={
        "pk": f"PROJECT#{project_id}", "sk": f"ANALYSIS#{analysis_id}",
        "analysis_id": analysis_id, "source_id": source_id, "commit_sha": commit_sha,
        "source_uri": source_uri, "result_key": result_key, "status": "analyzing",
        "created_at": timestamp, "updated_at": timestamp,
    })
    try:
        response = agentcore().invoke_agent_runtime(
            agentRuntimeArn=AGENT_RUNTIME_ARN,
            runtimeSessionId=f"pawploy-{project_id}-{uuid4().hex}",
            payload=json.dumps({"mode": "analyze", "project_id": project_id,
                                "source_uri": source_uri, "commit_sha": commit_sha,
                                "analysis_id": analysis_id,
                                **({"revision_message": revision_message, "previous_recommendation": previous_recommendation}
                                   if revision_message else {})}).encode(),
        )
        result = json.loads(response["response"].read())
        s3().put_object(Bucket=SOURCE_BUCKET, Key=result_key,
                        Body=json.dumps(result, ensure_ascii=False).encode(),
                        ContentType="application/json")
        table().update_item(
            Key={"pk": f"PROJECT#{project_id}", "sk": f"ANALYSIS#{analysis_id}"},
            UpdateExpression="SET #status = :status, updated_at = :updated_at",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":status": "analyzed" if result.get("status") == "ok" else "failed", ":updated_at": now()},
        )
        table().update_item(
            Key={"pk": f"PROJECT#{project_id}", "sk": f"SOURCE#{source_id}"},
            UpdateExpression="SET analysis_status = :status, updated_at = :updated_at",
            ExpressionAttributeValues={":status": "analyzed" if result.get("status") == "ok" else "failed", ":updated_at": now()},
        )
    except Exception as exc:
        logger.exception("AgentCore analysis failed for %s", analysis_id)
        table().update_item(
            Key={"pk": f"PROJECT#{project_id}", "sk": f"ANALYSIS#{analysis_id}"},
            UpdateExpression="SET #status = :status, error_message = :error, updated_at = :updated_at",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":status": "failed", ":error": str(exc)[:500], ":updated_at": now()},
        )
        table().update_item(
            Key={"pk": f"PROJECT#{project_id}", "sk": f"SOURCE#{source_id}"},
            UpdateExpression="SET analysis_status = :status, analysis_error = :error, updated_at = :updated_at",
            ExpressionAttributeValues={":status": "failed", ":error": str(exc)[:500], ":updated_at": now()},
        )


def get_analysis(project_id: str, project_token: str, analysis_id: str) -> dict:
    require_project(project_id, project_token)
    item = table().get_item(Key={"pk": f"PROJECT#{project_id}", "sk": f"ANALYSIS#{analysis_id}"}).get("Item")
    if not item:
        raise ServiceError(404, "분석 작업을 찾을 수 없습니다.")
    result = {key: item[key] for key in ("analysis_id", "source_id", "commit_sha", "source_uri", "status", "created_at", "updated_at") if key in item}
    if item.get("result_key"):
        result["result_uri"] = f"s3://{SOURCE_BUCKET}/{item['result_key']}"
        if item.get("status") == "analyzed":
            try:
                payload = s3().get_object(Bucket=SOURCE_BUCKET, Key=item["result_key"])["Body"].read()
                stored = json.loads(payload)
                result.update(stored)
                result["analysis_id"] = analysis_id
            except (ClientError, ValueError, UnicodeDecodeError) as exc:
                logger.warning("Analysis result unavailable for %s: %s", analysis_id, exc)
    if item.get("error_message"):
        result["error_message"] = item["error_message"]
    return result


def get_github_source(project_id: str, project_token: str, source_id: str) -> dict:
    require_project(project_id, project_token)
    try:
        item = table().get_item(Key={"pk": f"PROJECT#{project_id}", "sk": f"SOURCE#{source_id}"}).get("Item")
    except ClientError as exc:
        raise ServiceError(503, "GitHub 소스 작업을 확인할 수 없습니다.") from exc
    if not item:
        raise ServiceError(404, "GitHub 소스 작업을 찾을 수 없습니다.")
    result = {
        "source_id": source_id,
        "status": item["status"],
        "repository_url": item["repository_url"],
        "ref": item["ref"],
        "commit_sha": item["commit_sha"],
        "created_at": item["created_at"],
        "updated_at": item["updated_at"],
    }
    if item.get("analysis_id"):
        result["analysis_id"] = item["analysis_id"]
        result["analysis_status"] = item.get("analysis_status", "analyzing")
    if item["status"] == "ready":
        result["s3_key"] = item["s3_key"]
    if item["status"] == "failed":
        result["error_message"] = item.get("error_message", "GitHub 소스를 저장하지 못했습니다.")
    return result
