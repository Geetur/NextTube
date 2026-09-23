"""MinIO / S3-compatible object storage helpers."""

import boto3

from app.config import settings


def client():
    return boto3.client(
        "s3",
        region_name=settings.s3_region,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        endpoint_url=settings.s3_endpoint,
        config=boto3.session.Config(signature_version="s3v4"),
    )


def put_bytes(key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
    client().put_object(Bucket=settings.s3_bucket, Key=key, Body=data, ContentType=content_type)
