import os

MIB = 1024 * 1024
MAX_PARTS = 10_000
MIN_PART_BYTES = 8 * MIB
MAX_PART_BYTES = 5 * 1024 * MIB
MAX_OBJECT_BYTES = MAX_PARTS * MAX_PART_BYTES  # S3 provider limit, not a product quota.
UPLOAD_BUCKET = os.environ.get("UPLOAD_BUCKET", "")
PROJECTS_TABLE = os.environ.get("PROJECTS_TABLE", "")
AWS_REGION = os.environ.get("AWS_REGION", "ap-northeast-2")
