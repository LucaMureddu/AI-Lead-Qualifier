"""
services/storage.py
-------------------
Object Storage client — V2.1.

Astrae l'accesso a qualsiasi endpoint S3-compatible (MinIO in dev, AWS S3
in prod) tramite aioboto3, l'unico client S3 completamente asincrono per
Python. Nessuna chiamata boto3 sincrona: tutto il I/O passa per await.

Design
------
- Singleton lazy: il client aioboto3 viene creato alla prima chiamata e
  riusato per tutta la vita del processo (context manager interno all'helper).
- Zero PII nei log: viene loggata solo la S3 Object Key, mai il contenuto
  del file o il nome originale dell'utente.
- Object Key sicura: tenant_id sanitizzato + UUID hex + estensione originale.
  Nessun path traversal possibile perché il nome utente non entra mai
  nella key.
- Fail-fast: ogni chiamata S3 è avvolta in try/except con log strutturato
  (structlog) e rilancia l'eccezione per lasciarla gestire al chiamante.

Funzioni pubbliche
------------------
upload_file(contents, content_type, tenant_id, extension) → str (object key)
    Carica bytes su S3 e restituisce la S3 Object Key.

get_presigned_url(object_key, expires_in) → str
    Genera un URL pre-firmato per il download temporaneo.

delete_file(object_key) → None
    Cancella un oggetto dal bucket.

close_storage() → None
    Chiude la sessione aioboto3 (da chiamare nello shutdown del lifespan).
"""

from __future__ import annotations

import re
import uuid
from typing import TYPE_CHECKING, Optional

import aioboto3
import structlog
from botocore.exceptions import BotoCoreError, ClientError

if TYPE_CHECKING:
    from types_aiobotocore_s3.client import S3Client

from core.config import get_settings

log = structlog.get_logger()

# ── Singleton session ──────────────────────────────────────────────────────────
# aioboto3.Session è thread-safe e pensato per essere creato una volta sola.
# Il client S3 viene invece creato per ogni operazione (è un context manager
# asincrono) per rispettare il pattern raccomandato da aioboto3.

_session: Optional[aioboto3.Session] = None


def get_session() -> aioboto3.Session:
    """
    Return the shared aioboto3 Session, initialising it on first call.

    Public so that callers outside this module (e.g. the /health endpoint)
    can reuse the same session without creating a second one.  The Session
    itself holds no open network connections — only clients do — so sharing
    it is safe and avoids double-initialisation overhead.
    """
    global _session
    if _session is None:
        settings = get_settings()
        _session = aioboto3.Session(
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
        )
        log.info("storage.session_created", endpoint=settings.s3_endpoint_url)
    return _session


def init_storage() -> None:
    """
    Eagerly initialise the aioboto3 Session at application startup.

    Call this from the FastAPI lifespan so the session is created once during
    startup rather than lazily on the first upload request or health check.
    This makes startup logs deterministic and prevents a cold-start delay on
    the first request after deployment.
    """
    get_session()
    log.info("storage.session_ready")


def close_storage() -> None:
    """
    Release the aioboto3 Session (call from FastAPI lifespan shutdown).

    aioboto3.Session has no async close method — releasing the reference is
    sufficient to allow GC to clean up any underlying resources.
    """
    global _session
    if _session is not None:
        _session = None
        log.info("storage.session_closed")


# ── Key generation ─────────────────────────────────────────────────────────────

_SAFE_TENANT_RE: re.Pattern[str] = re.compile(r"[^A-Za-z0-9_-]")


def _make_object_key(tenant_id: str, extension: str) -> str:
    """
    Build a safe, unguessable S3 Object Key.

    Format: ``<safe_tenant>/<uuid_hex><.ext>``

    - ``tenant_id`` is sanitised to [A-Za-z0-9_-] to prevent path traversal.
    - A UUID4 hex string ensures global uniqueness and hides the original
      filename from the key (no PII leak via key names).
    - ``extension`` must include the leading dot (e.g. '.csv', '.xlsx').
    """
    safe_tenant: str = _SAFE_TENANT_RE.sub("", tenant_id) or "unknown"
    return f"{safe_tenant}/{uuid.uuid4().hex}{extension}"


# ── Public API ─────────────────────────────────────────────────────────────────

async def upload_file(
    contents: bytes,
    content_type: str,
    tenant_id: str,
    extension: str,
) -> str:
    """
    Upload ``contents`` to S3 and return the Object Key.

    Parameters
    ----------
    contents:
        Raw file bytes. Never logged.
    content_type:
        MIME type (e.g. "text/csv", "application/json",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet").
    tenant_id:
        Caller's tenant identifier, used to scope the object key.
    extension:
        File extension including the leading dot (e.g. ".csv").

    Returns
    -------
    str
        The S3 Object Key of the uploaded object.

    Raises
    ------
    ClientError | BotoCoreError
        Re-raised after logging so the caller (route handler) can return 500.
    """
    settings = get_settings()
    object_key: str = _make_object_key(tenant_id, extension)

    log.info(
        "storage.upload_start",
        tenant_id=tenant_id,
        object_key=object_key,
        content_type=content_type,
        size_bytes=len(contents),
    )

    try:
        async with get_session().client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
        ) as client:
            client: S3Client  # type: ignore[no-redef]
            await client.put_object(
                Bucket=settings.s3_bucket_name,
                Key=object_key,
                Body=contents,
                ContentType=content_type,
            )
    except (ClientError, BotoCoreError) as exc:
        log.error(
            "storage.upload_failed",
            tenant_id=tenant_id,
            object_key=object_key,
            error=str(exc),
        )
        raise

    log.info(
        "storage.upload_complete",
        tenant_id=tenant_id,
        object_key=object_key,
    )
    return object_key


async def get_presigned_url(
    object_key: str,
    expires_in: int = 3600,
) -> str:
    """
    Generate a pre-signed URL for temporary download of ``object_key``.

    Parameters
    ----------
    object_key:
        The S3 Object Key returned by :func:`upload_file`.
    expires_in:
        URL validity in seconds. Default: 3600 (1 hour).

    Returns
    -------
    str
        Pre-signed HTTPS URL valid for ``expires_in`` seconds.

    Raises
    ------
    ClientError | BotoCoreError
        Re-raised after logging.
    """
    settings = get_settings()

    log.debug("storage.presign_start", object_key=object_key, expires_in=expires_in)

    try:
        async with get_session().client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
        ) as client:
            client: S3Client  # type: ignore[no-redef]
            url: str = await client.generate_presigned_url(
                "get_object",
                Params={
                    "Bucket": settings.s3_bucket_name,
                    "Key": object_key,
                },
                ExpiresIn=expires_in,
            )
    except (ClientError, BotoCoreError) as exc:
        log.error(
            "storage.presign_failed",
            object_key=object_key,
            error=str(exc),
        )
        raise

    log.debug("storage.presign_complete", object_key=object_key)
    return url


async def download_file(object_key: str) -> bytes:
    """
    Download an object from S3 and return its raw bytes.

    Used by the ingestion chunker to read catalogue files in memory
    without writing them to the local filesystem (stateless design).

    Parameters
    ----------
    object_key:
        The S3 Object Key to download (as returned by :func:`upload_file`).

    Returns
    -------
    bytes
        Raw file content.

    Raises
    ------
    ClientError | BotoCoreError
        Re-raised after logging so the caller can surface a meaningful error.
    """
    settings = get_settings()

    log.info("storage.download_start", object_key=object_key)

    try:
        async with get_session().client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
        ) as client:
            client: S3Client  # type: ignore[no-redef]
            response = await client.get_object(
                Bucket=settings.s3_bucket_name,
                Key=object_key,
            )
            body: bytes = await response["Body"].read()
    except (ClientError, BotoCoreError) as exc:
        log.error(
            "storage.download_failed",
            object_key=object_key,
            error=str(exc),
        )
        raise

    log.info(
        "storage.download_complete",
        object_key=object_key,
        size_bytes=len(body),
    )
    return body


async def delete_file(object_key: str) -> None:
    """
    Delete an object from the S3 bucket.

    Idempotent: if the key does not exist, S3 returns success anyway.

    Parameters
    ----------
    object_key:
        The S3 Object Key to delete.

    Raises
    ------
    ClientError | BotoCoreError
        Re-raised after logging.
    """
    settings = get_settings()

    log.info("storage.delete_start", object_key=object_key)

    try:
        async with get_session().client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
        ) as client:
            client: S3Client  # type: ignore[no-redef]
            await client.delete_object(
                Bucket=settings.s3_bucket_name,
                Key=object_key,
            )
    except (ClientError, BotoCoreError) as exc:
        log.error(
            "storage.delete_failed",
            object_key=object_key,
            error=str(exc),
        )
        raise

    log.info("storage.delete_complete", object_key=object_key)
