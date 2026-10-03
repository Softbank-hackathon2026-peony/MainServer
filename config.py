import os

SOURCE_BUCKET = os.environ.get("SOURCE_BUCKET", "")
PROJECTS_TABLE = os.environ.get("PROJECTS_TABLE", "")
AWS_REGION = os.environ.get("AWS_REGION", "ap-northeast-2")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
AGENT_RUNTIME_ARN = os.environ.get("AGENT_RUNTIME_ARN", "")
AGENTCORE_REGION = os.environ.get("AGENTCORE_REGION", AWS_REGION)
CODEBUILD_PROJECT = os.environ.get("CODEBUILD_PROJECT", "pawploy-build")
WORKER_REGION = os.environ.get("WORKER_REGION", AWS_REGION)
WORKER_QUEUE_URL = os.environ.get("WORKER_QUEUE_URL", "https://sqs.ap-northeast-2.amazonaws.com/135808950984/pawploy-jobs.fifo")
WORKER_DESTROY_QUEUE_URL = os.environ.get("WORKER_DESTROY_QUEUE_URL", "https://sqs.ap-northeast-2.amazonaws.com/135808950984/pawploy-destroy")
WORKER_ARTIFACT_BUCKET = os.environ.get("WORKER_ARTIFACT_BUCKET", "pawploy-tf-135808950984-ap-northeast-2")
WORKER_STATUS_TABLE = os.environ.get("WORKER_STATUS_TABLE", "pawploy-deployments")
BUILD_POLL_SECONDS = int(os.environ.get("BUILD_POLL_SECONDS", "5"))
BUILD_TIMEOUT_SECONDS = int(os.environ.get("BUILD_TIMEOUT_SECONDS", "1800"))
CORS_ORIGINS = [origin.strip() for origin in os.environ.get("CORS_ORIGINS", "https://app.pawploy.teampeony.net,https://deploy-puppy.64bit.kr,http://localhost:5173,http://127.0.0.1:5173").split(",") if origin.strip()]
