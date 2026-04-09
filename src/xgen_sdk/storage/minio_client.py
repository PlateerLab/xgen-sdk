import os
import tempfile
import mimetypes
import logging
from functools import lru_cache
from typing import Optional, List, Dict, Any, Set, Tuple
from urllib.parse import urlparse
from datetime import datetime, timedelta

from minio import Minio
from minio.error import S3Error

logger = logging.getLogger(__name__)

DEFAULT_BUCKET_NAME = os.getenv("MINIO_DOCUMENT_BUCKET", "documents")
GOVERNANCE_BUCKET = os.getenv("MINIO_GOVERNANCE_BUCKET", "governance")

def _parse_minio_endpoint(endpoint: str) -> tuple[str, bool]:
    """
    Parse the configured endpoint and determine whether to use TLS.

    Priority:
    1. Explicit MINIO_SECURE flag if provided.
    2. Scheme derived from the endpoint.
    """
    secure_env = os.getenv("MINIO_SECURE")
    secure: Optional[bool] = None
    if secure_env is not None:
        secure = secure_env.lower() in {"1", "true", "yes"}

    parsed = urlparse(endpoint)
    if parsed.scheme:
        host = parsed.netloc or parsed.path
        if secure is None:
            secure = parsed.scheme == "https"
    else:
        host = endpoint
        if secure is None:
            secure = False

    return host, bool(secure)


@lru_cache(maxsize=1)
def get_minio_client() -> Minio:
    endpoint = os.getenv("MINIO_ENDPOINT", "http://minio:9000")
    access_key = os.getenv("MINIO_ROOT_USER") or os.getenv("MINIO_DATA_ACCESS_KEY") or "minioadmin"
    secret_key = os.getenv("MINIO_ROOT_PASSWORD") or os.getenv("MINIO_DATA_SECRET_KEY") or "minioadmin"

    if not endpoint:
        raise RuntimeError("MINIO_ENDPOINT is not configured")
    if not access_key or not secret_key:
        raise RuntimeError("MINIO_ROOT_USER/MINIO_ROOT_PASSWORD (or fallback MINIO_DATA_ACCESS_KEY/MINIO_DATA_SECRET_KEY) is not configured")

    host, secure = _parse_minio_endpoint(endpoint)

    return Minio(
        host,
        access_key=access_key,
        secret_key=secret_key,
        secure=secure,
    )


def ensure_bucket_exists(client: Minio, bucket_name: str = DEFAULT_BUCKET_NAME) -> None:
    """
    Make sure the target bucket exists. It is safe to call concurrently.
    """
    try:
        if not client.bucket_exists(bucket_name):
            client.make_bucket(bucket_name)
    except S3Error as exc:
        # If bucket already exists or we lack permissions, surface the error
        # unless it indicates the bucket already exists in the target region.
        if exc.code not in {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}:
            raise RuntimeError(f"Failed to ensure MinIO bucket '{bucket_name}': {exc}") from exc


def upload_file(
    client: Minio,
    source_path: str,
    object_name: str,
    bucket_name: str = DEFAULT_BUCKET_NAME,
    content_type: Optional[str] = None,
) -> None:
    """
    Upload a local file to MinIO under the requested object name.
    """
    client.fput_object(
        bucket_name=bucket_name,
        object_name=object_name,
        file_path=source_path,
        content_type=content_type or "application/octet-stream",
    )

def download_file(
    client: Minio,
    object_name: str,
    destination_path: str,
    bucket_name: str = DEFAULT_BUCKET_NAME,
) -> None:
    """
    Download a file from MinIO to a local path.
    """
    client.fget_object(
        bucket_name=bucket_name,
        object_name=object_name,
        file_path=destination_path,
    )


def file_exists(
    client: Minio,
    object_name: str,
    bucket_name: str = DEFAULT_BUCKET_NAME,
) -> bool:
    """
    Check if a file exists in MinIO.
    """
    try:
        client.stat_object(bucket_name, object_name)
        return True
    except S3Error:
        return False


# File Storage Bucket 환경 변수
FILE_STORAGE_BUCKET = os.getenv("MINIO_FILE_STORAGE_BUCKET", "file-storage")


def list_folders_in_path(
    client: Minio,
    prefix: str,
    bucket_name: str = FILE_STORAGE_BUCKET,
) -> list[str]:
    """
    List all folders (directories) at the given prefix path in MinIO.
    Returns a list of folder names (not full paths).

    Args:
        client: MinIO client instance
        prefix: The path prefix to list folders from (e.g., "1/" for user_id=1)
        bucket_name: The bucket name to search in

    Returns:
        List of folder names at the given prefix
    """
    folders = []
    try:
        # Ensure prefix ends with /
        if prefix and not prefix.endswith('/'):
            prefix = prefix + '/'

        # List objects with the given prefix, using delimiter to get only direct children
        objects = client.list_objects(
            bucket_name=bucket_name,
            prefix=prefix,
            recursive=False
        )

        for obj in objects:
            # obj.is_dir indicates if it's a folder (prefix)
            if obj.is_dir:
                # Extract just the folder name from the full path
                folder_name = obj.object_name.rstrip('/').split('/')[-1]
                if folder_name:
                    folders.append(folder_name)

    except S3Error as exc:
        logger.warning(f"Failed to list folders in '{bucket_name}/{prefix}': {exc}")

    return folders


def list_files_in_path(
    client: Minio,
    prefix: str,
    bucket_name: str = FILE_STORAGE_BUCKET,
    extensions: Optional[Set[str]] = None,
) -> List[Dict[str, Any]]:
    """
    List all files (not folders) at the given prefix path in MinIO.

    Args:
        client: MinIO client instance
        prefix: The path prefix to list files from (e.g., "1/zz/" for user_id=1, storage=zz)
        bucket_name: The bucket name to search in
        extensions: Optional set of file extensions to filter (e.g., {'.xlsx', '.csv'})

    Returns:
        List of file info dicts: [{"name": str, "path": str, "size": int, "modified": datetime, "etag": str}]
    """
    files = []
    try:
        # Ensure prefix ends with /
        if prefix and not prefix.endswith('/'):
            prefix = prefix + '/'

        # List objects with the given prefix
        objects = client.list_objects(
            bucket_name=bucket_name,
            prefix=prefix,
            recursive=False
        )

        for obj in objects:
            # Skip directories
            if obj.is_dir:
                continue

            # Extract filename from full path
            filename = obj.object_name.split('/')[-1]
            if not filename:
                continue

            # Filter by extension if specified
            if extensions:
                ext = '.' + filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
                if ext not in extensions:
                    continue

            files.append({
                "name": filename,
                "path": obj.object_name,
                "size": obj.size,
                "modified": obj.last_modified,
                "etag": obj.etag,
            })

    except S3Error as exc:
        logger.warning(f"Failed to list files in '{bucket_name}/{prefix}': {exc}")

    return files


def delete_file(
    client: Minio,
    object_name: str,
    bucket_name: str = FILE_STORAGE_BUCKET,
) -> bool:
    """
    Delete a file from MinIO.

    Args:
        client: MinIO client instance
        object_name: Full path to the object (e.g., "1/zz/data.xlsx")
        bucket_name: The bucket name

    Returns:
        True if deleted successfully, False otherwise
    """
    try:
        client.remove_object(bucket_name=bucket_name, object_name=object_name)
        return True
    except S3Error as exc:
        logger.warning(f"Failed to delete file '{bucket_name}/{object_name}': {exc}")
        return False


def copy_file(
    client: Minio,
    source_object: str,
    dest_object: str,
    bucket_name: str = FILE_STORAGE_BUCKET,
) -> bool:
    """
    Copy a file within the same bucket in MinIO.

    Args:
        client: MinIO client instance
        source_object: Source object path (e.g., "1/zz/data.xlsx")
        dest_object: Destination object path (e.g., "1/zz/data_copy.xlsx")
        bucket_name: The bucket name

    Returns:
        True if copied successfully, False otherwise
    """
    try:
        from minio.commonconfig import CopySource
        client.copy_object(
            bucket_name=bucket_name,
            object_name=dest_object,
            source=CopySource(bucket_name, source_object)
        )
        return True
    except S3Error as exc:
        logger.warning(f"Failed to copy file '{source_object}' to '{dest_object}': {exc}")
        return False


def get_file_info(
    client: Minio,
    object_name: str,
    bucket_name: str = FILE_STORAGE_BUCKET,
) -> Optional[Dict[str, Any]]:
    """
    Get metadata for a file in MinIO.

    Args:
        client: MinIO client instance
        object_name: Full path to the object
        bucket_name: The bucket name

    Returns:
        Dict with file info or None if not found
    """
    try:
        stat = client.stat_object(bucket_name=bucket_name, object_name=object_name)
        return {
            "name": object_name.split('/')[-1],
            "path": object_name,
            "size": stat.size,
            "modified": stat.last_modified,
            "etag": stat.etag,
            "content_type": stat.content_type,
        }
    except S3Error:
        return None


@lru_cache(maxsize=1)
def _get_public_minio_client() -> Optional[Minio]:
    """
    외부 presigned URL 발급 전용 MinIO 클라이언트.
    MINIO_PUBLIC_ENDPOINT 환경변수가 설정된 경우에만 생성.
    region을 명시하여 SDK가 버킷 region 확인 API 호출을 건너뛰도록 함.
    """
    public_endpoint = os.getenv("MINIO_PUBLIC_ENDPOINT")
    if not public_endpoint:
        return None

    access_key = os.getenv("MINIO_ROOT_USER") or os.getenv("MINIO_DATA_ACCESS_KEY")
    secret_key = os.getenv("MINIO_ROOT_PASSWORD") or os.getenv("MINIO_DATA_SECRET_KEY")
    if not access_key or not secret_key:
        return None

    parsed = urlparse(public_endpoint)
    host = parsed.netloc or parsed.path
    secure = parsed.scheme == "https"
    return Minio(host, access_key=access_key, secret_key=secret_key, secure=secure, region="us-east-1")


def get_presigned_url(
    bucket_name: str,
    object_name: str,
    expires: timedelta = timedelta(hours=1),
) -> Optional[str]:
    """
    외부에서 접근 가능한 presigned download URL 발급.
    MINIO_PUBLIC_ENDPOINT가 미설정이면 None 반환.
    """
    client = _get_public_minio_client()
    if client is None:
        logger.warning("MINIO_PUBLIC_ENDPOINT not configured, cannot generate presigned URL")
        return None
    try:
        return client.presigned_get_object(bucket_name, object_name, expires=expires)
    except Exception as exc:
        logger.error(f"Failed to generate presigned URL for '{bucket_name}/{object_name}': {exc}")
        return None


# =============================================================================
# Documents-origin utilities
# =============================================================================

CACHE_DIR = os.path.join(tempfile.gettempdir(), "xgen_minio_cache")
IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp', '.svg', '.tiff', '.ico'}


def parse_minio_path(minio_path: str) -> Tuple[str, str]:
    """
    MinIO 경로를 파싱하여 버킷 이름과 객체 이름을 반환합니다.
    형식: bucket_name/object_name
    """
    parts = minio_path.split("/", 1)
    if len(parts) != 2:
        raise ValueError(f"잘못된 MinIO 경로 형식입니다: {minio_path}")
    return parts[0], parts[1]


def download_file_from_minio(minio_path: str) -> Dict[str, Any]:
    """
    MinIO 경로(bucket/object_name)에서 파일을 다운로드하거나 정보를 반환합니다.
    이미지 파일인 경우: 다운로드하여 로컬 경로 반환
    그 외 파일인 경우: 다운로드하지 않고 MinIO 정보 반환

    Args:
        minio_path: 'bucket_name/object_name' 형식의 문자열

    Returns:
        dict: {
            "temp_path": str,       # (이미지인 경우) 파일의 로컬 경로
            "minio_bucket": str,    # (이미지가 아닌 경우) 버킷 이름
            "minio_object_name": str, # (이미지가 아닌 경우) 객체 이름
            "file_type": str,       # MIME 타입
            "extension": str,       # 파일 확장자
            "original_name": str    # 원본 파일명
        }
    """
    local_path = None
    try:
        bucket_name, object_name = parse_minio_path(minio_path)
        filename = os.path.basename(object_name)
        _, ext = os.path.splitext(filename)
        mime_type, _ = mimetypes.guess_type(filename)
        if not mime_type:
            mime_type = "application/octet-stream"

        if ext.lower() in IMAGE_EXTENSIONS:
            os.makedirs(CACHE_DIR, exist_ok=True)
            local_path = os.path.join(CACHE_DIR, filename)
            if os.path.exists(local_path):
                logger.info(f"[FILE_DOWNLOAD] 캐시 히트: {local_path} (다운로드 스킵)")
            else:
                client = get_minio_client()
                logger.info(f"[FILE_DOWNLOAD] MinIO 다운로드 시작: bucket={bucket_name}, object={object_name} -> {local_path}")
                download_file(client, object_name, local_path, bucket_name=bucket_name)
                logger.info(f"[FILE_DOWNLOAD] 다운로드 완료: {local_path}")

            return {
                "temp_path": local_path,
                "file_type": mime_type,
                "extension": ext,
                "original_name": filename,
            }
        else:
            logger.info(f"[FILE_PROCESS] 문서 파일 감지 (다운로드 건너뜀): {minio_path}")
            return {
                "minio_bucket": bucket_name,
                "minio_object_name": object_name,
                "file_type": mime_type,
                "extension": ext,
                "original_name": filename,
            }

    except Exception as e:
        logger.error(f"[FILE_DOWNLOAD] 파일 처리 실패: {minio_path}, error: {e}")
        if local_path and os.path.exists(local_path):
            try:
                if os.path.getsize(local_path) == 0:
                    os.remove(local_path)
            except OSError:
                pass
        raise e
