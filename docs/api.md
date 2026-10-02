# Fawploy 백엔드 API (현재 구현)

Base URL: `https://fawploy.teampeony.net`
API 접두사: `/api/v1`  
요청·응답: JSON (`Content-Type: application/json`)

현재 구현 범위는 상태 확인, 프로젝트 생성, **공개 GitHub 저장소의 특정 커밋을 플랫폼 S3에 보관**하는 기능이다. 파일·폴더 업로드, 코드 분석, 빌드, 배포는 구현되지 않았다. 로그인 대신 프로젝트 생성 시 발급되는 `project_token`을 이후 요청의 `X-Project-Token` 헤더에 넣는다. 토큰은 응답 시 한 번만 제공되므로 안전하게 보관해야 한다.

성공 응답은 `data`와 `request_id`를 포함한다. 서비스 오류는 `{"detail":"오류 설명"}`, 요청 검증 오류는 FastAPI의 `detail` 배열 형식이다. `/status`는 예외다.

## 상태 확인

`GET /status` → `200 OK`

```json
{"status":"ok","service":"fawploy","timestamp":"2026-10-01T00:00:00+00:00"}
```

## 프로젝트 생성

`POST /api/v1/projects` → `201 Created`

```json
{"name":"나의 웹 서비스"}
```

```json
{"data":{"project_id":"prj_...","project_token":"...","name":"나의 웹 서비스"},"request_id":"req_..."}
```

`name`은 공백이 아닌 1~80자다.

## GitHub 소스 등록

`POST /api/v1/projects/{project_id}/sources/github` → `202 Accepted`

필수 헤더: `X-Project-Token: {project_token}`

```json
{"github_url":"https://github.com/owner/repo","ref":"main"}
```

`github_url`은 공개 저장소의 HTTPS 루트 주소만 허용한다. `.git` 접미사는 허용하지만 브랜치/파일 경로, 쿼리, 다른 호스트는 허용하지 않는다. `ref`는 선택이며 브랜치·태그·커밋 SHA를 지정할 수 있다. 생략하면 기본 브랜치를 사용한다. 서버가 즉시 커밋 SHA를 확인해 고정한 뒤 비동기 보관 작업을 시작한다.

```json
{"data":{"source_id":"src_...","status":"queued","repository_url":"https://github.com/owner/repo","ref":"main","commit_sha":"0123456789abcdef0123456789abcdef01234567"},"request_id":"req_..."}
```

## 소스 상태 조회

`GET /api/v1/projects/{project_id}/sources/{source_id}` → `200 OK`

필수 헤더: `X-Project-Token: {project_token}`

상태는 `queued` → `downloading` → `ready` 또는 `failed`다. 프론트는 `ready`/`failed`가 될 때까지 폴링한다.

```json
{"data":{"source_id":"src_...","status":"ready","repository_url":"https://github.com/owner/repo","ref":"main","commit_sha":"0123456789abcdef0123456789abcdef01234567","created_at":"2026-10-01T00:00:00+00:00","updated_at":"2026-10-01T00:00:01+00:00","s3_key":"projects/prj_.../sources/src_.../0123456789abcdef0123456789abcdef01234567.tar.gz"},"request_id":"req_..."}
```

실패 시 `error_message`가 포함된다. `s3_key`는 플랫폼 내부 경로이며 다운로드용 공개 URL은 아니다. GitHub 소스 아카이브에는 Git LFS 실제 바이너리와 서브모듈 내용이 포함되지 않을 수 있다.

## 대표 오류

| HTTP | 의미 |
| --- | --- |
| 404 | 프로젝트·소스 없음/토큰 불일치 또는 공개 GitHub 저장소·커밋 없음 |
| 422 | URL·ref·프로젝트 이름이 올바르지 않음 또는 비공개/빈 저장소 |
| 502 | GitHub 연결·응답 문제 |
| 503 | GitHub 요청 제한 또는 S3/DynamoDB 설정·작업 문제 |

환경 변수: `AWS_REGION`(기본 `ap-northeast-2`), `SOURCE_BUCKET`, `PROJECTS_TABLE`, `GITHUB_TOKEN`(선택, GitHub API 요청 제한 완화용). Terraform 리소스 정의는 `infra`에 있다.

브라우저 호출을 위해 `https://deploy-puppy.64bit.kr`, 로컬 Vite 개발 주소(`http://localhost:5173`, `http://127.0.0.1:5173`)의 CORS를 허용한다. 다른 프론트엔드 도메인을 사용하면 `CORS_ORIGINS` 환경 변수에 쉼표로 구분해 지정해야 한다.

현재 백그라운드 다운로드는 API 프로세스에서 실행된다. 프로세스 재시작 중 작업이 중단되면 상태가 `queued`/`downloading`에 남을 수 있으므로, 운영용 작업 큐·워커·재시도는 후속 구현이 필요하다.
