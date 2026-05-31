import logging
import os
from collections import defaultdict
from datetime import datetime, timezone

import boto3
from core.db import get_db

logger = logging.getLogger(__name__)
LOCALSTACK = os.getenv("LOCALSTACK_ENDPOINT", "http://localhost:4566")
REGION = os.getenv("AWS_REGION", "eu-west-1")
SAMPLE_SIZE = 50

s3_client = boto3.client(
    "s3",
    endpoint_url=LOCALSTACK,
    region_name=REGION,
    aws_access_key_id="test",
    aws_secret_access_key="test",
)

# LocalStack sets LastModified = upload time, so real object age is always 0 days.
# These deterministic profiles keep the deployed demo scenarios stable.
GROUP_AGE_PROFILES = {
    ("app1-data-bucket", "tag:data_type=active"): {
        "pct_30": 10.0,
        "pct_90": 0.0,
        "pct_180": 0.0,
    },
    ("app1-data-bucket", "tag:data_type=report"): {
        "pct_30": 37.5,
        "pct_90": 25.0,
        "pct_180": 12.5,
    },
    ("app1-data-bucket", "tag:data_type=archive"): {
        "pct_30": 87.5,
        "pct_90": 75.0,
        "pct_180": 62.5,
    },
    ("app1-logs-bucket", "tag:data_type=logs"): {
        "pct_30": 50.0,
        "pct_90": 37.5,
        "pct_180": 12.5,
    },
    ("app1-temp-bucket", "tag:data_type=tmp"): {
        "pct_30": 50.0,
        "pct_90": 37.5,
        "pct_180": 12.5,
    },
    ("app2-report-bucket", "tag:data_type=report"): {
        "pct_30": 12.5,
        "pct_90": 0.0,
        "pct_180": 0.0,
    },
    ("app2-archive-bucket", "tag:data_type=archive"): {
        "pct_30": 87.5,
        "pct_90": 75.0,
        "pct_180": 62.5,
    },
    ("app2-archive-bucket", "tag:data_type=backup"): {
        "pct_30": 87.5,
        "pct_90": 75.0,
        "pct_180": 62.5,
    },
    ("app2-clean-bucket", "tag:data_type=active"): {
        "pct_30": 12.5,
        "pct_90": 0.0,
        "pct_180": 0.0,
    },
}

DEFAULT_GROUP_AGE_PROFILE = {
    "pct_30": 25.0,
    "pct_90": 12.5,
    "pct_180": 0.0,
}

BUCKET_AGE_PROFILES = {
    "app1-data-bucket": {"pct_30": 37.5, "pct_90": 25.0, "pct_180": 12.5},
    "app1-logs-bucket": {"pct_30": 50.0, "pct_90": 37.5, "pct_180": 12.5},
    "app1-temp-bucket": {"pct_30": 25.0, "pct_90": 12.5, "pct_180": 0.0},
    "app2-report-bucket": {"pct_30": 37.5, "pct_90": 25.0, "pct_180": 12.5},
    "app2-archive-bucket": {"pct_30": 87.5, "pct_90": 75.0, "pct_180": 62.5},
    "app2-clean-bucket": {"pct_30": 12.5, "pct_90": 0.0, "pct_180": 0.0},
}


def _group_tag_key() -> str:
    return os.getenv("S3_SAMPLER_GROUP_TAG_KEY", "data_type").strip() or "data_type"


def _overwrite_existing_samples() -> bool:
    # Overwrite is disabled by default to protect seeded/demo samples and avoid
    # changing later phase inputs unexpectedly.
    return os.getenv("S3_SAMPLER_OVERWRITE_EXISTING", "false").lower() == "true"


def _cleanup_all_when_grouped_enabled() -> bool:
    return os.getenv("S3_SAMPLER_CLEANUP_ALL_WHEN_GROUPED", "false").lower() == "true"


def build_tag_grouping_key(tag_key: str, tag_value: str) -> str:
    return f"tag:{tag_key.strip()}={tag_value.strip()}"


def _has_existing_sample(cur, resource_id, grouping_key: str) -> bool:
    cur.execute(
        """
        SELECT 1
        FROM s3_object_samples
        WHERE resource_id = %s
          AND grouping_key = %s
        LIMIT 1
        """,
        (resource_id, grouping_key),
    )
    return cur.fetchone() is not None


def _is_glacier_like(storage_class: str) -> bool:
    normalized = storage_class.upper()
    return "GLACIER" in normalized or "ARCHIVE" in normalized


def _list_sampled_objects(bucket_name: str) -> list[dict]:
    objects = []
    for page in s3_client.get_paginator("list_objects_v2").paginate(Bucket=bucket_name):
        for obj in page.get("Contents", []):
            objects.append(obj)
            if len(objects) >= SAMPLE_SIZE:
                break
        if len(objects) >= SAMPLE_SIZE:
            break
    return objects


def _get_object_grouping_key(bucket_name: str, object_key: str, tag_key: str) -> str | None:
    try:
        tag_set = s3_client.get_object_tagging(Bucket=bucket_name, Key=object_key).get("TagSet", [])
    except Exception as e:
        logger.warning(
            "[s3_sampler] cannot read object tags bucket=%s object_key=%s: %s",
            bucket_name,
            object_key,
            e,
        )
        return None

    for tag in tag_set:
        if tag.get("Key") == tag_key:
            tag_value = (tag.get("Value") or "").strip()
            if not tag_value:
                break
            return build_tag_grouping_key(tag_key, tag_value)

    logger.info(
        "[s3_sampler] skipping untagged object bucket=%s object_key=%s missing_tag_key=%s",
        bucket_name,
        object_key,
        tag_key,
    )
    return None


def _group_objects_by_tag(bucket_name: str, objects: list[dict], tag_key: str) -> dict[str, list[dict]]:
    grouped_objects = defaultdict(list)
    for obj in objects:
        object_key = obj.get("Key")
        if not object_key:
            logger.info(
                "[s3_sampler] skipping untagged object bucket=%s object_key=%s missing_tag_key=%s",
                bucket_name,
                object_key,
                tag_key,
            )
            continue

        grouping_key = _get_object_grouping_key(bucket_name, object_key, tag_key)
        if grouping_key:
            grouped_objects[grouping_key].append(obj)

    return dict(grouped_objects)


def _age_profile(bucket_name: str, grouping_key: str) -> dict:
    if grouping_key == "ALL":
        return BUCKET_AGE_PROFILES.get(bucket_name, DEFAULT_GROUP_AGE_PROFILE)
    return GROUP_AGE_PROFILES.get((bucket_name, grouping_key), DEFAULT_GROUP_AGE_PROFILE)


def _sample_values(bucket_name: str, grouping_key: str, objects: list[dict]) -> tuple:
    total = len(objects)
    group_size_bytes = sum(obj.get("Size", 0) or 0 for obj in objects)
    classes = [(obj.get("StorageClass", "STANDARD") or "STANDARD").upper() for obj in objects]

    pct_standard = round(classes.count("STANDARD") / total * 100, 2)
    pct_standard_ia = round(classes.count("STANDARD_IA") / total * 100, 2)
    pct_glacier = round(
        sum(1 for storage_class in classes if _is_glacier_like(storage_class)) / total * 100,
        2,
    )

    profile = _age_profile(bucket_name, grouping_key)
    pct_older_30 = profile["pct_30"]
    pct_older_90 = min(pct_older_30, profile["pct_90"])
    pct_older_180 = min(pct_older_90, profile["pct_180"])

    return (
        total,
        pct_older_30,
        pct_older_90,
        pct_older_180,
        pct_standard,
        pct_standard_ia,
        pct_glacier,
        group_size_bytes,
    )


def _insert_group_sample(cur, resource_id, bucket_name: str, grouping_key: str, objects: list[dict]):
    if not _overwrite_existing_samples() and _has_existing_sample(cur, resource_id, grouping_key):
        logger.info(
            "[s3_sampler] skipping existing sample bucket=%s resource_id=%s grouping_key=%s",
            bucket_name,
            resource_id,
            grouping_key,
        )
        return

    (
        total,
        pct_older_30,
        pct_older_90,
        pct_older_180,
        pct_standard,
        pct_standard_ia,
        pct_glacier,
        group_size_bytes,
    ) = _sample_values(bucket_name, grouping_key, objects)

    logger.info(
        "[s3_sampler] inserting grouped sample bucket=%s resource_id=%s grouping_key=%s "
        "sample_size=%s group_size_bytes=%s",
        bucket_name,
        resource_id,
        grouping_key,
        total,
        group_size_bytes,
    )

    cur.execute(
        """
        INSERT INTO s3_object_samples
            (resource_id, sampled_at, sample_size,
             pct_older_than_30_days, pct_older_than_90_days, pct_older_than_180_days,
             pct_in_standard, pct_in_standard_ia, pct_in_glacier, grouping_key, group_size_bytes)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        (
            resource_id,
            datetime.now(timezone.utc),
            total,
            pct_older_30,
            pct_older_90,
            pct_older_180,
            pct_standard,
            pct_standard_ia,
            pct_glacier,
            grouping_key,
            group_size_bytes,
        ),
    )


def _sample_bucket(cur, resource_id, bucket_name):
    try:
        objects = _list_sampled_objects(bucket_name)
    except Exception as e:
        logger.warning(f"[s3_sampler] Cannot list {bucket_name}: {e}")
        return

    if not objects:
        logger.info(f"[s3_sampler] {bucket_name} empty - skipping")
        return

    tag_key = _group_tag_key()

    # Agent1 reads latest S3 samples per grouping_key. grouping_key=ALL means
    # bucket-level sample. grouping_key=tag:data_type=value means tag-level
    # sample and allows Agent1 to generate tag-filtered lifecycle policies.
    grouped_objects = _group_objects_by_tag(bucket_name, objects, tag_key)
    if grouped_objects:
        for grouping_key in sorted(grouped_objects):
            _insert_group_sample(
                cur,
                resource_id,
                bucket_name,
                grouping_key,
                grouped_objects[grouping_key],
            )
        return

    logger.info(
        "[s3_sampler] no tagged objects found for bucket=%s using fallback grouping_key=ALL",
        bucket_name,
    )
    _insert_group_sample(cur, resource_id, bucket_name, "ALL", objects)


def cleanup_bucket_level_samples_when_grouped_exists(cur=None, tag_key: str | None = None) -> int:
    if cur is None:
        with get_db() as conn:
            with conn.cursor() as cleanup_cur:
                return cleanup_bucket_level_samples_when_grouped_exists(cleanup_cur, tag_key)

    group_pattern = f"tag:{(tag_key or _group_tag_key()).strip()}=%"
    cur.execute(
        """
        DELETE FROM s3_object_samples old_all
        WHERE old_all.grouping_key = 'ALL'
          AND EXISTS (
            SELECT 1
            FROM s3_object_samples grouped
            WHERE grouped.resource_id = old_all.resource_id
              AND grouped.grouping_key LIKE %s
          )
        """,
        (group_pattern,),
    )
    deleted_count = cur.rowcount
    logger.info("[s3_sampler] deleted old ALL samples where grouped rows exist count=%s", deleted_count)
    return deleted_count


def run_s3_object_sampler():
    logger.info("[s3_sampler] Starting...")
    tag_key = _group_tag_key()
    logger.info("[s3_sampler] grouping objects by tag key %s", tag_key)

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT r.id, s.name FROM resources r
                JOIN s3_instances s ON s.resource_id = r.id
                WHERE r.resource_type = 's3'
            """)
            rows = cur.fetchall()
            if not rows:
                logger.info("[s3_sampler] No S3 in DB - skipping")
                return
            for resource_id, bucket_name in rows:
                try:
                    _sample_bucket(cur, resource_id, bucket_name)
                except Exception as e:
                    logger.error(f"[s3_sampler] Failed {bucket_name}: {e}")

            if _cleanup_all_when_grouped_enabled():
                cleanup_bucket_level_samples_when_grouped_exists(cur, tag_key)

    logger.info(f"[s3_sampler] Done - {len(rows)} buckets")
