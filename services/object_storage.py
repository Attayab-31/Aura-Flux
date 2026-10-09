"""Private Supabase Storage access for generated user media."""

import os
from pathlib import Path
from urllib.parse import quote

import requests


def is_configured() -> bool:
    return bool(os.getenv("SUPABASE_URL", "").strip() and
                os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip())


def _settings():
    base = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    bucket = os.getenv("SUPABASE_STORAGE_BUCKET", "fluxstory-media").strip()
    if not base or not key:
        raise RuntimeError("Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY for persistent media storage.")
    return base, key, bucket


if os.getenv("RENDER", "").lower() == "true" and not is_configured():
    raise RuntimeError("Configure SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in Render; local media is ephemeral.")


def _object_url(base: str, bucket: str, key: str) -> str:
    parts = "/".join(quote(part, safe="") for part in key.strip("/").split("/"))
    return f"{base}/storage/v1/object/{quote(bucket, safe='')}/{parts}"


def upload_file(key: str, filename: str | Path, content_type: str) -> str:
    base, secret, bucket = _settings()
    with open(filename, "rb") as source:
        response = requests.post(
            _object_url(base, bucket, key),
            headers={"apikey": secret, "Authorization": f"Bearer {secret}",
                     "Content-Type": content_type, "x-upsert": "true"},
            data=source, timeout=(10, 300),
        )
    if not response.ok:
        raise RuntimeError(f"Supabase Storage upload failed ({response.status_code}).")
    return key


def download_file(key: str, filename: str | Path) -> Path:
    base, secret, bucket = _settings()
    target = Path(filename)
    target.parent.mkdir(parents=True, exist_ok=True)
    response = requests.get(
        _object_url(base, bucket, key),
        headers={"apikey": secret, "Authorization": f"Bearer {secret}"},
        timeout=(10, 300),
    )
    if not response.ok:
        raise RuntimeError(f"Supabase Storage download failed ({response.status_code}).")
    partial = target.with_name(target.name + ".partial")
    partial.write_bytes(response.content)
    partial.replace(target)
    return target


def create_signed_url(key: str, expires_in: int = 3600) -> str:
    base, secret, bucket = _settings()
    path = "/".join(quote(part, safe="") for part in key.strip("/").split("/"))
    response = requests.post(
        f"{base}/storage/v1/object/sign/{quote(bucket, safe='')}/{path}",
        headers={"apikey": secret, "Authorization": f"Bearer {secret}", "Content-Type": "application/json"},
        json={"expiresIn": expires_in}, timeout=(10, 30),
    )
    if not response.ok:
        raise RuntimeError(f"Supabase Storage signed URL request failed ({response.status_code}).")
    data = response.json()
    signed_path = data.get("signedURL") or data.get("signedUrl")
    if not signed_path:
        raise RuntimeError("Supabase Storage returned no signed URL.")
    return signed_path if signed_path.startswith("http") else base + signed_path
