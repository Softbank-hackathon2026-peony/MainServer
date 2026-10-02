import hashlib
import math
from decimal import Decimal
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import app
from config import MAX_PARTS, MIB


class FakeTable:
    def __init__(self):
        self.items = {}

    def put_item(self, Item, **_kwargs):
        Item = {key: Decimal(value) if type(value) is int else value for key, value in Item.items()}
        self.items[(Item["pk"], Item["sk"])] = Item

    def get_item(self, Key):
        return {"Item": self.items.get((Key["pk"], Key["sk"]))}

    def update_item(self, Key, ExpressionAttributeValues, **_kwargs):
        item = self.items[(Key["pk"], Key["sk"])]
        item["status"] = ExpressionAttributeValues.get(":uploaded", ExpressionAttributeValues.get(":aborted"))


class FakeS3:
    def __init__(self):
        self.parts = []
        self.object_size = 0
        self.content_type = ""
        self.object_key = ""

    def create_multipart_upload(self, **kwargs):
        self.content_type = kwargs["ContentType"]
        self.object_key = kwargs["Key"]
        return {"UploadId": "s3-multipart-id"}

    def generate_presigned_url(self, operation, Params, **_kwargs):
        assert operation == "upload_part"
        return f"https://s3.example/{Params['PartNumber']}"

    def complete_multipart_upload(self, MultipartUpload, **_kwargs):
        self.parts = MultipartUpload["Parts"]

    def head_object(self, **_kwargs):
        return {"ContentLength": self.object_size, "ContentType": self.content_type}

    def abort_multipart_upload(self, **_kwargs):
        pass


def test_multipart_flow_has_no_ten_mb_cap():
    table = FakeTable()
    s3 = FakeS3()
    with patch("service.table", return_value=table), patch("service.s3", return_value=s3):
        client = TestClient(app)
        project = client.post("/api/v1/projects", json={"name": "테스트 프로젝트"}).json()["data"]
        project_id = project["project_id"]
        token = project["project_token"]
        assert table.items[(f"PROJECT#{project_id}", "META")]["token_hash"] == hashlib.sha256(token.encode()).hexdigest()
        headers = {"X-Project-Token": token}
        size = 25 * MIB
        upload_response = client.post(
            f"/api/v1/projects/{project_id}/uploads/presign",
            json={"file_name": "app.zip", "size_bytes": size},
            headers=headers,
        )
        assert upload_response.status_code == 201
        upload = upload_response.json()["data"]
        assert upload["total_parts"] == math.ceil(size / upload["part_size"])
        upload_id = upload["upload_id"]
        assert client.get(
            f"/api/v1/projects/{project_id}/uploads/{upload_id}/parts/1/presign",
            headers={"X-Project-Token": "wrong"},
        ).status_code == 404
        assert client.get(
            f"/api/v1/projects/{project_id}/uploads/{upload_id}/parts/1/presign",
            headers=headers,
        ).json()["data"]["upload_url"].endswith("/1")
        s3.object_size = size
        parts = [{"part_number": n, "etag": f'"etag-{n}"'} for n in range(1, upload["total_parts"] + 1)]
        completed = client.post(
            f"/api/v1/projects/{project_id}/uploads/{upload_id}/complete",
            json={"parts": parts},
            headers=headers,
        )
        assert completed.status_code == 200
        assert completed.json()["data"]["status"] == "uploaded"
        assert len(s3.parts) == upload["total_parts"]


def test_provider_limit_uses_at_most_ten_thousand_parts():
    table = FakeTable()
    s3 = FakeS3()
    with patch("service.table", return_value=table), patch("service.s3", return_value=s3):
        client = TestClient(app)
        project = client.post("/api/v1/projects", json={"name": "큰 프로젝트"}).json()["data"]
        result = client.post(
            f"/api/v1/projects/{project['project_id']}/uploads/presign",
            json={"file_name": "large.zip", "size_bytes": 40 * 1024 ** 4},
            headers={"X-Project-Token": project["project_token"]},
        )
        assert result.status_code == 201
        assert result.json()["data"]["total_parts"] <= MAX_PARTS


def test_folder_file_keeps_relative_path_and_accepts_project_sources():
    table = FakeTable()
    s3 = FakeS3()
    with patch("service.table", return_value=table), patch("service.s3", return_value=s3):
        client = TestClient(app)
        project = client.post("/api/v1/projects", json={"name": "폴더"}).json()["data"]
        endpoint = f"/api/v1/projects/{project['project_id']}/uploads/presign"
        headers = {"X-Project-Token": project["project_token"]}
        response = client.post(endpoint, json={
            "file_name": "main.py", "relative_path": "project/src/main.py", "size_bytes": 123,
        }, headers=headers)
        assert response.status_code == 201
        upload_id = response.json()["data"]["upload_id"]
        assert s3.object_key == f"projects/{project['project_id']}/uploads/{upload_id}/project/src/main.py"
        record = table.items[(f"PROJECT#{project['project_id']}", f"UPLOAD#{upload_id}")]
        assert record["relative_path"] == "project/src/main.py"


def test_folder_path_rejects_traversal_and_wrong_filename():
    table = FakeTable()
    s3 = FakeS3()
    with patch("service.table", return_value=table), patch("service.s3", return_value=s3):
        client = TestClient(app)
        project = client.post("/api/v1/projects", json={"name": "폴더"}).json()["data"]
        endpoint = f"/api/v1/projects/{project['project_id']}/uploads/presign"
        headers = {"X-Project-Token": project["project_token"]}
        for path in ("../main.py", "/main.py", "src//main.py", "src/other.py", "src\\main.py"):
            response = client.post(endpoint, json={
                "file_name": "main.py", "relative_path": path, "size_bytes": 123,
            }, headers=headers)
            assert response.status_code == 422
        assert s3.object_key == ""
