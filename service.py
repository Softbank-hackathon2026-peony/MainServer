import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from datetime import datetime, timezone
from functools import lru_cache
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.exceptions import ClientError

from config import AGENTCORE_REGION, AGENT_RUNTIME_ARN, AWS_REGION, BUILD_MAX_ATTEMPTS, BUILD_POLL_SECONDS, BUILD_TIMEOUT_SECONDS, CODEBUILD_PROJECT, GITHUB_TOKEN, PROJECTS_TABLE, SOURCE_BUCKET, WORKER_ARTIFACT_BUCKET, WORKER_DESTROY_QUEUE_URL, WORKER_QUEUE_URL, WORKER_REGION, WORKER_STATUS_TABLE

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


@lru_cache
def codebuild():
    return boto3.client("codebuild", region_name=AWS_REGION)


@lru_cache
def logs():
    return boto3.client("logs", region_name=AWS_REGION)


@lru_cache
def sqs():
    return boto3.client("sqs", region_name=WORKER_REGION)


@lru_cache
def worker_table():
    return boto3.resource("dynamodb", region_name=WORKER_REGION).Table(WORKER_STATUS_TABLE)


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


def get_analysis(project_id: str, project_token: str, analysis_id: str, trusted: bool = False) -> dict:
    if not trusted:
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


def start_build(project_id: str, project_token: str, analysis_id: str, build_files_override: dict | None = None, attempt: int = 1, trusted: bool = False) -> dict:
    """분석 결과의 소스와 build_files를 CodeBuild에 전달해 이미지 빌드를 시작한다."""
    if not trusted:
        require_project(project_id, project_token)
    analysis = get_analysis(project_id, project_token, analysis_id, trusted=trusted)
    if analysis.get("status") not in {"analyzed", "ok"} or not analysis.get("recommendation"):
        raise ServiceError(409, "분석이 완료된 뒤 이미지를 빌드할 수 있습니다.")
    files = build_files_override or analysis.get("build_files") or {}
    source_uri = analysis.get("source_uri")
    files_uri = files.get("uri_prefix")
    if not source_uri or not files_uri:
        raise ServiceError(409, "분석 결과에 빌드 파일 경로가 없습니다.")
    commit = analysis.get("commit_sha", "")
    image_tag = f"{project_id}-{commit[:12]}"
    try:
        build_request = {
            "projectName": CODEBUILD_PROJECT,
            "environmentVariablesOverride": [
                {"name": "SOURCE_URI", "value": source_uri, "type": "PLAINTEXT"},
                {"name": "BUILD_FILES_URI", "value": files_uri, "type": "PLAINTEXT"},
                {"name": "IMAGE_TAG", "value": image_tag, "type": "PLAINTEXT"},
            ],
        }
        # AgentCore가 분석 결과와 함께 만든 buildspec을 CodeBuild에 명시적으로 전달한다.
        # 전달하지 않으면 CodeBuild 프로젝트에 고정된 단일 컨테이너 buildspec이 사용된다.
        if files.get("buildspec"):
            build_request["buildspecOverride"] = files["buildspec"]
        result = codebuild().start_build(**build_request)["build"]
    except ClientError as exc:
        logger.exception("CodeBuild start failed for %s", analysis_id)
        raise ServiceError(502, "이미지 빌드를 시작하지 못했습니다.") from exc
    build_id = result["id"]
    table().update_item(
        Key={"pk": f"PROJECT#{project_id}", "sk": f"ANALYSIS#{analysis_id}"},
        UpdateExpression="SET build_id = :id, build_status = :status, build_attempt = :attempt, build_files_uri = :files_uri, dockerfile = :dockerfile, updated_at = :updated_at",
        ExpressionAttributeValues={":id": build_id, ":status": result.get("buildStatus", "IN_PROGRESS"), ":attempt": attempt, ":files_uri": files.get("uri_prefix", ""), ":dockerfile": files.get("dockerfile", ""), ":updated_at": now()},
    )
    return {"build_id": build_id, "analysis_id": analysis_id, "status": result.get("buildStatus", "IN_PROGRESS"), "image_tag": image_tag, "attempt": attempt}


def _image_digests(images: dict) -> dict:
    """Parse the multi-image CodeBuild export without breaking single-image builds."""
    value = images.get("IMAGE_DIGESTS") if isinstance(images, dict) else None
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def get_build(project_id: str, project_token: str, analysis_id: str) -> dict:
    require_project(project_id, project_token)
    item = table().get_item(Key={"pk": f"PROJECT#{project_id}", "sk": f"ANALYSIS#{analysis_id}"}).get("Item")
    if not item or not item.get("build_id"):
        raise ServiceError(404, "이미지 빌드 작업을 찾을 수 없습니다.")
    try:
        build = codebuild().batch_get_builds(ids=[item["build_id"]])["builds"][0]
    except (ClientError, IndexError) as exc:
        raise ServiceError(502, "이미지 빌드 상태를 확인하지 못했습니다.") from exc
    exported = {v["name"]: v["value"] for v in build.get("exportedEnvironmentVariables", [])}
    status = build.get("buildStatus", "IN_PROGRESS")
    result = {"build_id": item["build_id"], "analysis_id": analysis_id, "status": status,
              "phase": build.get("currentPhase"),
              "ecr_image_uri": exported.get("ECR_IMAGE_URI"), "gcp_image_uri": exported.get("GCP_IMAGE_URI")}
    result["attempt"] = item.get("build_attempt", 1)
    if exported.get("IMAGE_DIGESTS"):
        try:
            result["image_digests"] = json.loads(exported["IMAGE_DIGESTS"])
        except json.JSONDecodeError:
            result["image_digests"] = {}
    if status == "SUCCEEDED" and result.get("image_digests"):
        table().update_item(
            Key={"pk": f"PROJECT#{project_id}", "sk": f"ANALYSIS#{analysis_id}"},
            UpdateExpression="SET image_digests = :digests",
            ExpressionAttributeValues={":digests": result["image_digests"]},
        )
    table().update_item(Key={"pk": f"PROJECT#{project_id}", "sk": f"ANALYSIS#{analysis_id}"},
                        UpdateExpression="SET build_status = :status, updated_at = :updated_at",
                        ExpressionAttributeValues={":status": status, ":updated_at": now()})
    return result


def get_deployment(project_id: str, project_token: str, deployment_id: str) -> dict:
    require_project(project_id, project_token)
    item = table().get_item(Key={"pk": f"PROJECT#{project_id}", "sk": f"DEPLOYMENT#{deployment_id}"}).get("Item")
    if not item:
        raise ServiceError(404, "배포 작업을 찾을 수 없습니다.")
    if item.get("step") == "build":
        build = get_build(project_id, project_token, item["analysis_id"])
        if build["status"] == "SUCCEEDED":
            # 과거 배포처럼 CodeBuild는 끝났지만 백그라운드 작업이 끊긴 경우에도
            # 프론트의 상태 폴링을 계기로 gen_terraform을 반드시 시작한다.
            table().update_item(
                Key={"pk": f"PROJECT#{project_id}", "sk": f"DEPLOYMENT#{deployment_id}"},
                UpdateExpression="SET #status = :status, #step = :step, ecr_image_uri = :ecr, gcp_image_uri = :gcp, image_digests = :digests, updated_at = :updated_at",
                ExpressionAttributeNames={"#status": "status", "#step": "step"},
                ExpressionAttributeValues={":status": "running", ":step": "terraform", ":ecr": build.get("ecr_image_uri", ""), ":gcp": build.get("gcp_image_uri", ""), ":digests": build.get("image_digests", {}), ":updated_at": now()},
            )
            item = table().get_item(Key={"pk": f"PROJECT#{project_id}", "sk": f"DEPLOYMENT#{deployment_id}"}).get("Item") or item
        else:
            status = "failed" if build["status"] in {"FAILED", "FAULT", "STOPPED", "TIMED_OUT"} else "running"
            return {"deployment_id": deployment_id, "status": status, "step": "build", "target": item.get("target"),
                    "build_id": build["build_id"], "ecr_image_uri": build.get("ecr_image_uri"), "gcp_image_uri": build.get("gcp_image_uri")}

    if item.get("step") == "terraform" and not item.get("terraform_status"):
        # CodeBuild 성공 후 서버 재시작/배경 작업 중단으로 누락된 Terraform 생성을 복구한다.
        generate_deployment_terraform(
            project_id, deployment_id, item["analysis_id"],
            {"ECR_IMAGE_URI": item.get("ecr_image_uri", ""), "GCP_IMAGE_URI": item.get("gcp_image_uri", ""), "IMAGE_DIGESTS": json.dumps(item.get("image_digests", {}))},
        )
        item = table().get_item(Key={"pk": f"PROJECT#{project_id}", "sk": f"DEPLOYMENT#{deployment_id}"}).get("Item") or item
    result = {key: item[key] for key in ("deployment_id", "status", "step", "target", "ecr_image_uri", "gcp_image_uri", "reason", "url", "expires_at", "worker_deploy_id") if key in item}
    if item.get("worker_deploy_id"):
        worker = worker_table().get_item(Key={"deploy_id": item["worker_deploy_id"]}).get("Item")
        if worker:
            worker_view = _worker_status(item, worker)
            result.update(worker_view)
            # Worker가 기록한 최종 상태를 Main DB에도 반영해 새 요청/재시작 후에도
            # 같은 상태를 반환한다. 프론트는 url이 생기면 health 폴링을 종료한다.
            update_values = {
                ":status": worker_view["status"],
                ":step": worker_view["step"],
                ":updated_at": now(),
            }
            update_expression = "SET #status = :status, #step = :step, updated_at = :updated_at"
            if "url" in worker_view:
                update_expression += ", url = :url"
                update_values[":url"] = worker_view["url"]
            if "expires_at" in worker_view:
                update_expression += ", expires_at = :expires_at"
                update_values[":expires_at"] = worker_view["expires_at"]
            if "reason" in worker_view:
                update_expression += ", reason = :reason"
                update_values[":reason"] = worker_view["reason"]
            table().update_item(
                Key={"pk": f"PROJECT#{project_id}", "sk": f"DEPLOYMENT#{deployment_id}"},
                UpdateExpression=update_expression,
                ExpressionAttributeNames={"#status": "status", "#step": "step"},
                ExpressionAttributeValues=update_values,
            )
    return result


def _build_log_tail(build: dict) -> str:
    """CodeBuild 로그의 마지막 부분만 AgentCore에 전달한다."""
    log_info = build.get("logs") or {}
    group = log_info.get("groupName")
    stream = log_info.get("streamName")
    if not group or not stream:
        return f"CodeBuild {build.get('buildStatus', 'FAILED')} (로그 스트림을 찾을 수 없음)"
    try:
        events = logs().get_log_events(
            logGroupName=group, logStreamName=stream, startFromHead=False, limit=200,
        ).get("events", [])
        return "\n".join(event.get("message", "") for event in events)[-12000:]
    except Exception:
        logger.exception("CodeBuild log read failed for %s", stream)
        return f"CodeBuild {build.get('buildStatus', 'FAILED')} (로그를 읽지 못함)"


def _retry_failed_build(project_id: str, deployment_id: str, analysis_id: str, build: dict, attempt: int) -> str | None:
    """실패 로그를 AgentCore fix_build에 전달하고 수정된 파일로 다음 빌드를 시작한다."""
    if attempt > BUILD_MAX_ATTEMPTS:
        return None
    analysis = table().get_item(Key={"pk": f"PROJECT#{project_id}", "sk": f"ANALYSIS#{analysis_id}"}).get("Item") or {}
    source_uri = f"s3://{SOURCE_BUCKET}/{analysis.get('source_key', '')}" if analysis.get("source_key") else analysis.get("source_uri", "")
    dockerfile = analysis.get("dockerfile", "")
    if not source_uri or not dockerfile:
        logger.warning("Cannot retry build %s: source_uri or dockerfile is missing", deployment_id)
        return None
    try:
        response = agentcore().invoke_agent_runtime(
            agentRuntimeArn=AGENT_RUNTIME_ARN,
            runtimeSessionId=f"pawploy-fix-build-{project_id}-{deployment_id}-{attempt}",
            payload=json.dumps({
                "mode": "fix_build", "project_id": project_id, "analysis_id": analysis_id,
                "source_uri": source_uri, "dockerfile": dockerfile,
                "build_log": _build_log_tail(build), "failed_phase": build.get("currentPhase") or "BUILD",
                "attempt": attempt,
            }).encode(),
        )
        fixed = json.loads(response["response"].read())
        files = fixed.get("build_files") or {}
        if fixed.get("status") != "ok" or not files.get("uri_prefix"):
            logger.warning("AgentCore fix_build did not produce a retry for %s: %s", deployment_id, fixed)
            return None
        # 기존 AgentCore fix_build 응답은 새 URI와 Dockerfile만 반환한다.
        # buildspec은 최초 분석 결과의 것을 재사용해 AgentCore 변경 없이 재빌드한다.
        if not files.get("buildspec") and analysis.get("result_key"):
            try:
                original = json.loads(s3().get_object(Bucket=SOURCE_BUCKET, Key=analysis["result_key"])["Body"].read())
                original_build_files = original.get("build_files") or {}
                if original_build_files.get("buildspec"):
                    files["buildspec"] = original_build_files["buildspec"]
            except (ClientError, ValueError, UnicodeDecodeError):
                logger.warning("Original buildspec unavailable for retry %s", deployment_id)
        table().update_item(
            Key={"pk": f"PROJECT#{project_id}", "sk": f"DEPLOYMENT#{deployment_id}"},
            UpdateExpression="SET #status = :status, #step = :step, build_attempt = :attempt, build_reason = :reason, updated_at = :updated_at",
            ExpressionAttributeNames={"#status": "status", "#step": "step"},
            ExpressionAttributeValues={":status": "running", ":step": f"fix({attempt}/{BUILD_MAX_ATTEMPTS})", ":attempt": attempt, ":reason": fixed.get("cause", ""), ":updated_at": now()},
        )
        retry = start_build(project_id, "", analysis_id, files, attempt + 1, trusted=True)
        table().update_item(
            Key={"pk": f"PROJECT#{project_id}", "sk": f"DEPLOYMENT#{deployment_id}"},
            UpdateExpression="SET #status = :status, #step = :step, updated_at = :updated_at",
            ExpressionAttributeNames={"#status": "status", "#step": "step"},
            ExpressionAttributeValues={":status": "running", ":step": "build", ":updated_at": now()},
        )
        return retry["build_id"]
    except Exception:
        logger.exception("CodeBuild fix retry failed for %s", deployment_id)
        return None


def monitor_deployment_build(project_id: str, deployment_id: str, analysis_id: str, build_id: str) -> None:
    """CodeBuild 완료를 반영하고 실패하면 AgentCore fix_build를 최대 3회 수행한다."""
    deadline = time.monotonic() + BUILD_TIMEOUT_SECONDS
    attempt = 1
    while time.monotonic() < deadline:
        try:
            build = codebuild().batch_get_builds(ids=[build_id])["builds"][0]
            status = build.get("buildStatus", "IN_PROGRESS")
            if status == "SUCCEEDED":
                exported = {v["name"]: v["value"] for v in build.get("exportedEnvironmentVariables", [])}
                table().update_item(
                    Key={"pk": f"PROJECT#{project_id}", "sk": f"DEPLOYMENT#{deployment_id}"},
                    UpdateExpression="SET #status = :status, #step = :step, build_status = :build_status, ecr_image_uri = :ecr, gcp_image_uri = :gcp, image_digests = :digests, updated_at = :updated_at",
                    ExpressionAttributeNames={"#status": "status", "#step": "step"},
                    ExpressionAttributeValues={":status": "succeeded", ":step": "build", ":build_status": "SUCCEEDED", ":ecr": exported.get("ECR_IMAGE_URI", ""), ":gcp": exported.get("GCP_IMAGE_URI", ""), ":digests": _image_digests(exported), ":updated_at": now()},
                )
                generate_deployment_terraform(project_id, deployment_id, analysis_id, exported)
                return
            if status in {"FAILED", "FAULT", "STOPPED", "TIMED_OUT"}:
                if attempt <= BUILD_MAX_ATTEMPTS:
                    # AgentCore를 호출하는 동안에도 프론트가 실패로 확정하지 않도록
                    # 먼저 자동 복구 단계로 공개한다.
                    table().update_item(
                        Key={"pk": f"PROJECT#{project_id}", "sk": f"DEPLOYMENT#{deployment_id}"},
                        UpdateExpression="SET #status = :status, #step = :step, updated_at = :updated_at",
                        ExpressionAttributeNames={"#status": "status", "#step": "step"},
                        ExpressionAttributeValues={":status": "running", ":step": f"fix({attempt}/{BUILD_MAX_ATTEMPTS})", ":updated_at": now()},
                    )
                    retry_id = _retry_failed_build(project_id, deployment_id, analysis_id, build, attempt)
                    if retry_id:
                        build_id = retry_id
                        attempt += 1
                        continue
                table().update_item(
                    Key={"pk": f"PROJECT#{project_id}", "sk": f"DEPLOYMENT#{deployment_id}"},
                    UpdateExpression="SET #status = :status, reason = :reason, updated_at = :updated_at",
                    ExpressionAttributeNames={"#status": "status"},
                    ExpressionAttributeValues={":status": "failed", ":reason": f"CodeBuild {status} (자동 수정 {attempt}/{BUILD_MAX_ATTEMPTS})", ":updated_at": now()},
                )
                return
        except Exception:
            logger.exception("CodeBuild monitor failed for %s", build_id)
        time.sleep(BUILD_POLL_SECONDS)
    table().update_item(
        Key={"pk": f"PROJECT#{project_id}", "sk": f"DEPLOYMENT#{deployment_id}"},
        UpdateExpression="SET #status = :status, reason = :reason, updated_at = :updated_at",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":status": "failed", ":reason": "CodeBuild 시간 초과", ":updated_at": now()},
    )


def generate_deployment_terraform(project_id: str, deployment_id: str, analysis_id: str, images: dict) -> None:
    """CodeBuild 성공 결과를 AgentCore gen_terraform에 전달하고 모듈 위치를 저장한다."""
    try:
        table().update_item(
            Key={"pk": f"PROJECT#{project_id}", "sk": f"DEPLOYMENT#{deployment_id}"},
            UpdateExpression="SET #status = :status, #step = :step, terraform_status = :tfstatus, updated_at = :updated_at",
            ExpressionAttributeNames={"#status": "status", "#step": "step"},
            ExpressionAttributeValues={":status": "running", ":step": "terraform", ":tfstatus": "running", ":updated_at": now()},
        )
        analysis = table().get_item(Key={"pk": f"PROJECT#{project_id}", "sk": f"ANALYSIS#{analysis_id}"}).get("Item") or {}
        result_key = analysis.get("result_key")
        if not result_key:
            raise ServiceError(409, "분석 결과를 찾을 수 없습니다.")
        payload = s3().get_object(Bucket=SOURCE_BUCKET, Key=result_key)["Body"].read()
        stored = json.loads(payload)
        recommendation = stored.get("recommendation")
        if not recommendation:
            raise ServiceError(409, "분석 recommendation이 없습니다.")
        cloud = recommendation.get("cloud", "aws")
        architecture = recommendation.get("architecture", "ec2")
        response = agentcore().invoke_agent_runtime(
            agentRuntimeArn=AGENT_RUNTIME_ARN,
            runtimeSessionId=f"pawploy-tf-{project_id}-{deployment_id}",
            payload=json.dumps({"mode": "gen_terraform", "project_id": project_id, "deploy_id": deployment_id,
                                "recommendation": recommendation,
                                "architectures": {cloud: architecture},
                                "image_uris": images}).encode(),
        )
        result = json.loads(response["response"].read())
        result_key = f"projects/{project_id}/deployments/{deployment_id}/terraform.json"
        s3().put_object(Bucket=SOURCE_BUCKET, Key=result_key, Body=json.dumps(result, ensure_ascii=False).encode(), ContentType="application/json")
        if result.get("status") not in {"ok", "partial"}:
            raise ServiceError(502, "Terraform을 생성하지 못했습니다.")
        table().update_item(
            Key={"pk": f"PROJECT#{project_id}", "sk": f"DEPLOYMENT#{deployment_id}"},
            UpdateExpression="SET terraform_uri = :uri, terraform_status = :tfstatus, updated_at = :updated_at",
            ExpressionAttributeValues={":uri": f"s3://{SOURCE_BUCKET}/{result_key}", ":tfstatus": result.get("status"), ":updated_at": now()},
        )
        enqueue_worker_deployment(project_id, deployment_id, analysis_id, recommendation, result, images)
    except Exception as exc:
        logger.exception("Terraform generation failed for %s", deployment_id)
        table().update_item(
            Key={"pk": f"PROJECT#{project_id}", "sk": f"DEPLOYMENT#{deployment_id}"},
            UpdateExpression="SET #status = :status, reason = :reason, updated_at = :updated_at",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":status": "failed", ":reason": str(exc)[:500], ":updated_at": now()},
        )


def _worker_status(item: dict, worker: dict) -> dict:
    """Terraform-worker result.json을 Main Server 배포 응답으로 매핑한다."""
    target = item.get("target", "")
    cloud = "gcp" if target.startswith("gcp") else "aws"
    worker_target = (worker.get("targets") or {}).get(cloud) or {}
    status = worker.get("status", "deploying")
    step = worker_target.get("status") or "terraform"
    if step == "health_check":
        step = "health"
    mapped = {"status": status, "step": step}
    if worker_target.get("health_url") or worker_target.get("endpoint"):
        mapped["url"] = worker_target.get("health_url") or worker_target.get("endpoint")
    if worker.get("expires_at"):
        mapped["expires_at"] = worker["expires_at"]
    reason = worker_target.get("error") or worker.get("error")
    if reason:
        mapped["reason"] = reason
    return mapped


def enqueue_worker_deployment(project_id: str, deployment_id: str, analysis_id: str, recommendation: dict, terraform_result: dict, images: dict) -> None:
    """AgentCore 모듈 생성 후 작업 JSON을 S3에 저장하고 Terraform-worker 큐에 넣는다."""
    cloud = recommendation.get("cloud", "aws")
    architecture = recommendation.get("architecture", "ec2")
    targets = terraform_result.get("targets") or []
    if isinstance(targets, dict):
        targets = [{"cloud": key, **value} for key, value in targets.items()]
    generated = next((target for target in targets if target.get("cloud") == cloud and target.get("architecture", architecture) == architecture), None)
    if not generated:
        generated = next((target for target in targets if target.get("cloud") == cloud), None)
    terraform_uri = (generated or {}).get("module_uri") or (generated or {}).get("terraform_uri")
    if architecture == "ec2_compose":
        digests = _image_digests(images)
        if not digests or not terraform_uri:
            raise ServiceError(502, "Terraform-worker에 전달할 이미지 또는 Terraform 모듈 경로가 없습니다.")
        target = {"cloud": "aws", "architecture": architecture, "images": digests, "terraform_uri": terraform_uri}
    else:
        image_uri = images.get("ECR_IMAGE_URI") if cloud == "aws" else images.get("GCP_IMAGE_URI")
        if not image_uri or not terraform_uri:
            raise ServiceError(502, "Terraform-worker에 전달할 이미지 또는 Terraform 모듈 경로가 없습니다.")
        target = {"cloud": cloud, "architecture": architecture, "image_uri": image_uri, "terraform_uri": terraform_uri}

    if not terraform_uri:
        raise ServiceError(502, "Terraform-worker에 전달할 이미지 또는 Terraform 모듈 경로가 없습니다.")

    # Worker의 ID 규칙(소문자·숫자·하이픈)에 맞춘 별도 ID를 사용한다.
    worker_deploy_id = deployment_id.replace("_", "-")[:40]
    request_id = uuid4().hex
    key = f"jobs/{worker_deploy_id}/{request_id}.json"
    job = {
        "deploy_id": worker_deploy_id,
        "project_id": project_id,
        "container_port": recommendation.get("container_port", 8080),
        "size": recommendation.get("size", "small"),
        "health_path": recommendation.get("health_path", "/"),
        "env": recommendation.get("env") or {},
        "targets": [target],
    }
    boto3.client("s3", region_name=WORKER_REGION).put_object(
        Bucket=WORKER_ARTIFACT_BUCKET, Key=key,
        Body=json.dumps(job, ensure_ascii=False).encode(), ContentType="application/json",
    )
    sqs().send_message(
        QueueUrl=WORKER_QUEUE_URL,
        MessageBody=json.dumps({"action": "deploy", "job_uri": f"s3://{WORKER_ARTIFACT_BUCKET}/{key}"}),
        MessageGroupId=worker_deploy_id,
        MessageDeduplicationId=request_id,
    )
    table().update_item(
        Key={"pk": f"PROJECT#{project_id}", "sk": f"DEPLOYMENT#{deployment_id}"},
        UpdateExpression="SET worker_deploy_id = :id, worker_job_uri = :uri, #status = :status, #step = :step, updated_at = :updated_at",
        ExpressionAttributeNames={"#status": "status", "#step": "step"},
        ExpressionAttributeValues={":id": worker_deploy_id, ":uri": f"s3://{WORKER_ARTIFACT_BUCKET}/{key}", ":status": "running", ":step": "terraform", ":updated_at": now()},
    )


def stop_deployment(project_id: str, project_token: str, deployment_id: str) -> None:
    require_project(project_id, project_token)
    item = table().get_item(Key={"pk": f"PROJECT#{project_id}", "sk": f"DEPLOYMENT#{deployment_id}"}).get("Item")
    if not item:
        raise ServiceError(404, "배포 작업을 찾을 수 없습니다.")
    worker_deploy_id = item.get("worker_deploy_id") or deployment_id.replace("_", "-")[:40]
    sqs().send_message(
        QueueUrl=WORKER_DESTROY_QUEUE_URL,
        MessageBody=json.dumps({"action": "destroy", "deploy_id": worker_deploy_id, "project_id": project_id}),
    )
    table().update_item(
        Key={"pk": f"PROJECT#{project_id}", "sk": f"DEPLOYMENT#{deployment_id}"},
        UpdateExpression="SET #status = :status, #step = :step, updated_at = :updated_at",
        ExpressionAttributeNames={"#status": "status", "#step": "step"},
        ExpressionAttributeValues={":status": "destroying", ":step": "health", ":updated_at": now()},
    )


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
