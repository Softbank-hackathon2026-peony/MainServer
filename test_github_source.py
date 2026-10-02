import hashlib
from io import BytesIO
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import app
from service import ServiceError


def test_frontend_cors_preflight():
    client = TestClient(app)
    response = client.options(
        "/api/v1/projects",
        headers={
            "Origin": "https://deploy-puppy.64bit.kr",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,x-project-token",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "https://deploy-puppy.64bit.kr"


def test_untrusted_origin_cors_preflight_is_rejected():
    client = TestClient(app)
    response = client.options(
        "/api/v1/projects",
        headers={"Origin": "https://unknown.example", "Access-Control-Request-Method": "POST"},
    )
    assert response.status_code == 400


class FakeTable:
    def __init__(self):
        self.items = {}

    def put_item(self, Item, **_kwargs):
        self.items[(Item["pk"], Item["sk"])] = Item

    def get_item(self, Key):
        return {"Item": self.items.get((Key["pk"], Key["sk"]))}

    def update_item(self, Key, ExpressionAttributeValues, **_kwargs):
        item = self.items[(Key["pk"], Key["sk"])]
        item["status"] = ExpressionAttributeValues[":status"]
        item["updated_at"] = ExpressionAttributeValues[":updated_at"]
        if ":error_message" in ExpressionAttributeValues:
            item["error_message"] = ExpressionAttributeValues[":error_message"]


class FakeS3:
    def __init__(self, fail=False):
        self.fail = fail
        self.objects = {}

    def upload_fileobj(self, archive, bucket, key, **kwargs):
        if self.fail:
            raise RuntimeError("S3 unavailable")
        self.objects[(bucket, key)] = (archive.read(), kwargs)


def make_project(client):
    project = client.post("/api/v1/projects", json={"name": "테스트"}).json()["data"]
    return project, {"X-Project-Token": project["project_token"]}


def github_response(path):
    if path == "/repos/owner/repo":
        return {"private": False, "default_branch": "main"}
    if path == "/repos/owner/repo/commits/main":
        return {"sha": "a" * 40}
    if path == "/repos/owner/repo/commits/v1.0":
        return {"sha": "b" * 40}
    raise AssertionError(path)


def test_public_repository_is_pinned_and_archived_in_s3():
    table, s3 = FakeTable(), FakeS3()
    with patch("service.table", return_value=table), patch("service.s3", return_value=s3), \
            patch("service.SOURCE_BUCKET", "source-bucket"), patch("service.github_json", side_effect=github_response), \
            patch("service.github_open", side_effect=lambda path: BytesIO(b"archive")) as archive_open:
        client = TestClient(app)
        project, headers = make_project(client)
        assert table.items[(f"PROJECT#{project['project_id']}", "META")]["token_hash"] == hashlib.sha256(
            project["project_token"].encode()
        ).hexdigest()
        response = client.post(
            f"/api/v1/projects/{project['project_id']}/sources/github",
            json={"github_url": "https://github.com/owner/repo"}, headers=headers,
        )
        assert response.status_code == 202
        source = response.json()["data"]
        assert source["status"] == "queued"
        assert source["commit_sha"] == "a" * 40
        assert "owner" not in source
        archive_open.assert_called_once_with(f"/repos/owner/repo/tarball/{'a' * 40}")
        status = client.get(
            f"/api/v1/projects/{project['project_id']}/sources/{source['source_id']}", headers=headers,
        )
        assert status.status_code == 200
        data = status.json()["data"]
        assert data["status"] == "ready"
        assert data["s3_key"] == f"projects/{project['project_id']}/sources/{source['source_id']}/{'a' * 40}.tar.gz"
        archive, options = s3.objects[("source-bucket", data["s3_key"])]
        assert archive == b"archive"
        assert options["ExtraArgs"]["Metadata"]["commit-sha"] == "a" * 40
        assert client.get(
            f"/api/v1/projects/{project['project_id']}/sources/{source['source_id']}",
            headers={"X-Project-Token": "wrong"},
        ).status_code == 404


def test_explicit_tag_and_dot_git_url():
    table, s3 = FakeTable(), FakeS3()
    with patch("service.table", return_value=table), patch("service.s3", return_value=s3), \
            patch("service.SOURCE_BUCKET", "source-bucket"), patch("service.github_json", side_effect=github_response), \
            patch("service.github_open", return_value=BytesIO(b"archive")):
        client = TestClient(app)
        project, headers = make_project(client)
        response = client.post(
            f"/api/v1/projects/{project['project_id']}/sources/github",
            json={"github_url": "https://github.com/owner/repo.git", "ref": "v1.0"}, headers=headers,
        )
        assert response.status_code == 202
        assert response.json()["data"]["repository_url"] == "https://github.com/owner/repo"
        assert response.json()["data"]["commit_sha"] == "b" * 40


def test_untrusted_urls_are_rejected_before_github_request():
    table = FakeTable()
    with patch("service.table", return_value=table), patch("service.SOURCE_BUCKET", "source-bucket"), \
            patch("service.github_json") as github_json:
        client = TestClient(app)
        project, headers = make_project(client)
        for url in (
            "http://github.com/owner/repo", "https://github.com.evil.test/owner/repo",
            "https://user:pass@github.com/owner/repo", "https://github.com:443/owner/repo",
            "https://github.com/owner/repo/tree/main", "https://github.com/owner/repo?x=1",
            "https://github.com/owner/repo#readme", "https://github.com/owner/repo/extra",
        ):
            response = client.post(
                f"/api/v1/projects/{project['project_id']}/sources/github",
                json={"github_url": url}, headers=headers,
            )
            assert response.status_code == 422, url
        github_json.assert_not_called()


def test_github_and_s3_failures_are_reported():
    table = FakeTable()
    with patch("service.table", return_value=table), patch("service.SOURCE_BUCKET", "source-bucket"), \
            patch("service.github_json", side_effect=ServiceError(404, "없는 저장소")):
        client = TestClient(app)
        project, headers = make_project(client)
        endpoint = f"/api/v1/projects/{project['project_id']}/sources/github"
        assert client.post(endpoint, json={"github_url": "https://github.com/owner/repo"}, headers=headers).status_code == 404

    with patch("service.table", return_value=table), patch("service.s3", return_value=FakeS3(fail=True)), \
            patch("service.SOURCE_BUCKET", "source-bucket"), patch("service.github_json", side_effect=github_response), \
            patch("service.github_open", return_value=BytesIO(b"archive")):
        response = client.post(endpoint, json={"github_url": "https://github.com/owner/repo"}, headers=headers)
        source_id = response.json()["data"]["source_id"]
        result = client.get(f"/api/v1/projects/{project['project_id']}/sources/{source_id}", headers=headers).json()["data"]
        assert result["status"] == "failed"
        assert "error_message" in result


def test_old_upload_routes_are_removed():
    client = TestClient(app)
    assert client.post("/api/v1/projects/prj_test/uploads/presign", json={}).status_code == 404
    assert client.get("/status").json()["status"] == "ok"
