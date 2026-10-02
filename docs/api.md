# Fawploy 백엔드 API (현재 구현)

Base URL: `https://fawploy.yyoungjin.com`  
API 접두사: `/api/v1`  
요청·응답: JSON (`Content-Type: application/json`)

현재 범위는 프로젝트 생성과 S3 멀티파트 직접 업로드까지다. 폴더는 내부 파일마다 업로드를 시작하며 `relative_path`로 폴더 구조를 보존한다. 파일 분석과 배포 API는 아직 구현되지 않았다. 사용자 로그인도 아직 없으며, 프로젝트 생성 시 받은 `project_token`이 해당 프로젝트에 접근하기 위한 임시 비밀값이다. 프론트는 이 값을 외부에 노출하지 말고 이후 요청의 `X-Project-Token` 헤더에 넣는다.

성공 응답은 `data`와 `request_id`를 포함한다. 서비스 오류는 `{"detail":"오류 설명"}` 형식이고, 요청값 검증 오류는 FastAPI의 `detail` 배열 형식이다. `/status`와 204 응답은 예외다.

## 1. 상태 확인

`GET /status`

```json
{
  "status": "ok",
  "service": "fawploy",
  "timestamp": "2026-10-01T00:00:00+00:00"
}
```

## 2. 프로젝트 생성

`POST /api/v1/projects` → `201 Created`

```json
{"name":"나의 웹 서비스"}
```

`name`은 공백이 아닌 1~80자다.

```json
{
  "data": {
    "project_id": "prj_...",
    "project_token": "...",
    "name": "나의 웹 서비스"
  },
  "request_id": "req_..."
}
```

## 3. 멀티파트 업로드 시작

`POST /api/v1/projects/{project_id}/uploads/presign` → `201 Created`  
필수 헤더: `X-Project-Token: {project_token}`

```json
{"file_name":"main.py","relative_path":"my-project/src/main.py","size_bytes":26214400}
```

`size_bytes`는 1 이상이며 제품별 용량 제한은 없다. 빈 파일은 현재 지원하지 않는다. S3 자체 한도인 최대 48.8 TiB와 최대 10,000개 파트는 적용된다. 확장자 제한은 없다. `relative_path`는 선택 항목이며, 폴더 업로드 시 루트 폴더 이름을 포함한 상대 경로를 전달한다. 경로의 마지막 이름은 `file_name`과 같아야 하고, 절대 경로·`..`·빈 경로 구성 요소는 허용하지 않는다. 생략하면 `file_name`을 경로로 사용한다.

```json
{
  "data": {
    "upload_id": "upl_...",
    "part_size": 8388608,
    "total_parts": 4
  },
  "request_id": "req_..."
}
```

서버는 비공개 S3 버킷에 멀티파트 업로드를 시작하고, 프로젝트·업로드 메타데이터와 상대 경로를 DynamoDB에 저장한다. S3 객체 키에도 상대 경로가 포함된다. `part_size`는 S3 파트 수 제한에 맞춰 파일 크기에 따라 커질 수 있다.

## 4. 각 파트의 업로드 URL 발급

`GET /api/v1/projects/{project_id}/uploads/{upload_id}/parts/{part_number}/presign` → `200 OK`  
필수 헤더: `X-Project-Token: {project_token}`

`part_number`는 1부터 `total_parts`까지다.

```json
{
  "data": {
    "upload_url": "https://...s3...",
    "expires_in": 900
  },
  "request_id": "req_..."
}
```

프론트는 파일의 해당 범위(`file.slice(...)`)를 `upload_url`에 HTTP `PUT`으로 직접 보낸다. 성공 응답의 `ETag` 헤더를 보관한다. URL은 15분 동안 유효하며 만료되면 같은 파트의 URL을 다시 발급받을 수 있다.

## 5. 업로드 완료

`POST /api/v1/projects/{project_id}/uploads/{upload_id}/complete` → `200 OK`  
필수 헤더: `X-Project-Token: {project_token}`

```json
{
  "parts": [
    {"part_number": 1, "etag": "\"etag-1\""},
    {"part_number": 2, "etag": "\"etag-2\""}
  ]
}
```

모든 파트의 `part_number`를 오름차순으로 빠짐없이 전달한다. `etag`는 S3 응답의 `ETag` 값을 그대로 전달한다. 서버가 S3에서 파트를 결합한 뒤 실제 파일 크기와 Content-Type을 확인하고 업로드 상태를 `uploaded`로 저장한다.

```json
{
  "data": {
    "project_id": "prj_...",
    "upload_id": "upl_...",
    "status": "uploaded"
  },
  "request_id": "req_..."
}
```

## 6. 업로드 중단

`DELETE /api/v1/projects/{project_id}/uploads/{upload_id}` → `204 No Content`  
필수 헤더: `X-Project-Token: {project_token}`

진행 중인 멀티파트 업로드를 중단한다. 미완료 업로드는 S3 수명 주기 규칙으로 시작 후 1일이 지나면 자동 정리된다.

## 대표 오류

| HTTP | 의미 |
| --- | --- |
| 400 | 잘못된 파트 번호 |
| 404 | 프로젝트 토큰 불일치 또는 업로드 작업 없음 |
| 409 | 현재 업로드 상태에서 요청 수행 불가 |
| 422 | 요청값 검증 실패(파일 크기가 S3 한도를 초과한 경우 포함) 또는 파일 파트·실제 파일 정보 불일치 |
| 503 | S3 또는 DynamoDB 작업 실패 |

업로드된 객체는 공개 URL로 읽을 수 없다. 현재 API에는 파일 다운로드, 분석, 배포 기능이 없다.
