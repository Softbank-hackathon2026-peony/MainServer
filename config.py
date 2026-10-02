import os

SOURCE_BUCKET = os.environ.get("SOURCE_BUCKET", "")
PROJECTS_TABLE = os.environ.get("PROJECTS_TABLE", "")
AWS_REGION = os.environ.get("AWS_REGION", "ap-northeast-2")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
CORS_ORIGINS = [origin.strip() for origin in os.environ.get("CORS_ORIGINS", "https://deploy-puppy.64bit.kr,http://localhost:5173,http://127.0.0.1:5173").split(",") if origin.strip()]
