import os, boto3

S3_ENDPOINT = os.getenv("S3_ENDPOINT", "http://minio:9000")
S3_REGION   = os.getenv("S3_REGION", "us-east-1")
S3_ACCESS   = os.getenv("S3_ACCESS_KEY", "minioadmin")
S3_SECRET   = os.getenv("S3_SECRET_KEY", "minioadmin")
S3_BUCKET   = os.getenv("S3_BUCKET", "media")

def client():
    return boto3.client(
        "s3",
        region_name=S3_REGION,
        aws_access_key_id=S3_ACCESS,
        aws_secret_access_key=S3_SECRET,
        endpoint_url=S3_ENDPOINT,
        config=boto3.session.Config(signature_version="s3v4"),
    )

def put_bytes(key: str, data: bytes, content_type="application/octet-stream"):
    client().put_object(Bucket=S3_BUCKET, Key=key, Body=data, ContentType=content_type)
