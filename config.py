import os

SOURCE_BUCKET = os.environ.get("SOURCE_BUCKET", "")
PROJECTS_TABLE = os.environ.get("PROJECTS_TABLE", "")
AWS_REGION = os.environ.get("AWS_REGION", "ap-northeast-2")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
AGENT_RUNTIME_ARN = os.environ.get("AGENT_RUNTIME_ARN", "")
AGENTCORE_REGION = os.environ.get("AGENTCORE_REGION", AWS_REGION)
CORS_ORIGINS = [origin.strip() for origin in os.environ.get("CORS_ORIGINS", "https://app.pawploy.teampeony.net,https://deploy-puppy.64bit.kr,http://localhost:5173,http://127.0.0.1:5173").split(",") if origin.strip()]
