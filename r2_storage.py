"""
Cloudflare R2 storage layer (S3-compatible).
Provides upload / download / delete / list operations for the Zone Inspect app.
"""

import io
import json
import os
import time
import threading
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

# ─── Init ─────────────────────────────────────────────────────────────────────
# Priority: environment variables → r2_config.json fallback

_BASE_DIR = Path(__file__).resolve().parent
_cfg_path = _BASE_DIR / "r2_config.json"
_cfg = {}
if _cfg_path.exists():
    with open(_cfg_path) as _f:
        _cfg = json.load(_f)

_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID", _cfg.get("account_id", ""))
_BUCKET = os.environ.get("R2_BUCKET", _cfg.get("bucket", ""))
_ACCESS_KEY = os.environ.get("R2_ACCESS_KEY_ID", _cfg.get("access_key_id", ""))
_SECRET_KEY = os.environ.get(
    "R2_SECRET_ACCESS_KEY", _cfg.get("secret_access_key", ""))

if not all([_ACCOUNT_ID, _BUCKET, _ACCESS_KEY, _SECRET_KEY]):
    raise RuntimeError("R2 credentials missing — set R2_ACCOUNT_ID, R2_BUCKET, "
                       "R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY env vars "
                       "or provide r2_config.json")

_s3 = boto3.client(
    "s3",
    endpoint_url=f"https://{_ACCOUNT_ID}.r2.cloudflarestorage.com",
    aws_access_key_id=_ACCESS_KEY,
    aws_secret_access_key=_SECRET_KEY,
    config=Config(signature_version="s3v4", retries={
                  "max_attempts": 3, "mode": "standard"}),
    region_name="auto",
)

print(f"☁️  R2 storage: bucket={_BUCKET}")

# ─── In-memory cache ──────────────────────────────────────────────────────────
_cache_lock = threading.Lock()
_cache: dict[str, tuple[float, object]] = {}   # key → (expires_at, value)
_DEFAULT_TTL = 0  # кеш отключён — всегда свежие данные из R2


def _cache_get(ck: str):
    with _cache_lock:
        entry = _cache.get(ck)
        if entry and entry[0] > time.monotonic():
            return True, entry[1]
        _cache.pop(ck, None)
    return False, None


def _cache_set(ck: str, value, ttl: int = _DEFAULT_TTL):
    with _cache_lock:
        _cache[ck] = (time.monotonic() + ttl, value)


def invalidate_cache(prefix: str = ""):
    """Drop cached entries whose key starts with prefix (or all if empty)."""
    with _cache_lock:
        if not prefix:
            _cache.clear()
        else:
            for k in [k for k in _cache if k.startswith(prefix)]:
                del _cache[k]


# ─── Core operations ──────────────────────────────────────────────────────────

def upload_bytes(key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
    """Upload raw bytes to R2."""
    _s3.put_object(Bucket=_BUCKET, Key=key, Body=data,
                   ContentType=content_type)
    invalidate_cache()


def upload_json(key: str, obj: dict) -> None:
    """Upload a JSON-serializable dict to R2."""
    data = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
    upload_bytes(key, data, "application/json")


def download_bytes(key: str) -> bytes | None:
    """Download raw bytes from R2. Returns None if not found."""
    try:
        resp = _s3.get_object(Bucket=_BUCKET, Key=key)
        return resp["Body"].read()
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchKey":
            return None
        raise


def download_json(key: str) -> dict | None:
    """Download and parse a JSON object from R2. Returns None if not found. Cached."""
    ck = f"json:{key}"
    hit, val = _cache_get(ck)
    if hit:
        return val
    data = download_bytes(key)
    if data is None:
        return None
    obj = json.loads(data)
    _cache_set(ck, obj)
    return obj


def delete_key(key: str) -> None:
    """Delete a single key from R2 (no error if missing)."""
    _s3.delete_object(Bucket=_BUCKET, Key=key)
    invalidate_cache()


def delete_prefix(prefix: str) -> int:
    """Delete all objects under a prefix. Returns count deleted."""
    keys = list_keys(prefix)
    if not keys:
        return 0
    for i in range(0, len(keys), 1000):
        batch = keys[i:i + 1000]
        _s3.delete_objects(
            Bucket=_BUCKET,
            Delete={"Objects": [{"Key": k} for k in batch], "Quiet": True},
        )
    invalidate_cache()
    return len(keys)


def list_keys(prefix: str, max_keys: int = 10000) -> list[str]:
    """List all keys under a prefix. Cached."""
    ck = f"list:{prefix}:{max_keys}"
    hit, val = _cache_get(ck)
    if hit:
        return val
    keys = []
    continuation = None
    while True:
        kwargs = {"Bucket": _BUCKET, "Prefix": prefix,
                  "MaxKeys": min(1000, max_keys - len(keys))}
        if continuation:
            kwargs["ContinuationToken"] = continuation
        resp = _s3.list_objects_v2(**kwargs)
        for obj in resp.get("Contents", []):
            keys.append(obj["Key"])
        if not resp.get("IsTruncated") or len(keys) >= max_keys:
            break
        continuation = resp["NextContinuationToken"]
    _cache_set(ck, keys)
    return keys


def append_line(key: str, line: str) -> None:
    """Append a line to an existing object (download + append + re-upload).
    Creates the object if it doesn't exist."""
    existing = download_bytes(key)
    if existing:
        new_data = existing + (line + "\n").encode("utf-8")
    else:
        new_data = (line + "\n").encode("utf-8")
    upload_bytes(key, new_data, "text/plain")


def key_exists(key: str) -> bool:
    """Check if a key exists in R2."""
    try:
        _s3.head_object(Bucket=_BUCKET, Key=key)
        return True
    except ClientError:
        return False
