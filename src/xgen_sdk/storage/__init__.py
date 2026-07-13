"""
xgen_sdk.storage — 통합 MinIO Storage 모듈

파일 업로드/다운로드, 목록 조회, 복사, presigned URL 등
+ 클라이언트측 암복호화 (crypto — 업로드 이전 암호화 / 다운로드 시 복호화)
"""

from xgen_sdk.storage.minio_client import (
    get_minio_client,
    ensure_bucket_exists,
    upload_file,
    download_file,
    file_exists,
    delete_file,
    get_file_info,
    list_folders_in_path,
    list_files_in_path,
    copy_file,
    get_presigned_url,
    parse_minio_path,
    download_file_from_minio,
    DEFAULT_BUCKET_NAME,
    FILE_STORAGE_BUCKET,
    GOVERNANCE_BUCKET,
    CACHE_DIR,
    IMAGE_EXTENSIONS,
)
from xgen_sdk.storage.crypto import (
    # 추상 계약 / 확장 지점
    FileCipher,
    register_cipher,
    get_cipher,
    # 기본 구현 (AES-256-GCM)
    Aes256GcmCipher,
    DEFAULT_ALGORITHM,
    # 전역 토글 (쓰기 측 — 읽기는 항상 자동 sniff)
    # 판정 우선순위: 서비스 설정 resolver(app_config) > env
    DEFAULT_ENABLED_ENV,
    encryption_enabled,
    resolve_encrypt_flag,
    set_encryption_enabled_resolver,
    # 키 관리
    DEFAULT_KEY_ENV,
    generate_key,
    decode_key,
    load_key_from_env,
    # 암복호화 (복호화는 알고리즘 자동 식별)
    encrypt_bytes,
    decrypt_bytes,
    encrypt_file,
    decrypt_file,
    decrypt_stream,
    is_encrypted_data,
    is_encrypted_file,
    # MinIO 결합 — 업로드 이전 암호화 / 다운로드 시 복호화
    upload_file_encrypted,
    download_file_decrypted,
    # 직접 구현부(서비스 자체 client 호출)용 프리미티브
    decrypt_file_inplace,
    encrypt_bytes_if_enabled,
    decrypt_bytes_if_encrypted,
    put_bytes_encrypted,
    get_object_bytes_decrypted,
    stream_object_decrypted,
    # 예외
    StorageCryptoError,
    EncryptionKeyError,
    UnsupportedAlgorithmError,
    DecryptionError,
)

__all__ = [
    "get_minio_client",
    "ensure_bucket_exists",
    "upload_file",
    "download_file",
    "file_exists",
    "delete_file",
    "get_file_info",
    "list_folders_in_path",
    "list_files_in_path",
    "copy_file",
    "get_presigned_url",
    "parse_minio_path",
    "download_file_from_minio",
    "DEFAULT_BUCKET_NAME",
    "FILE_STORAGE_BUCKET",
    "GOVERNANCE_BUCKET",
    "CACHE_DIR",
    "IMAGE_EXTENSIONS",
    # crypto
    "FileCipher",
    "register_cipher",
    "get_cipher",
    "Aes256GcmCipher",
    "DEFAULT_ALGORITHM",
    "DEFAULT_ENABLED_ENV",
    "encryption_enabled",
    "resolve_encrypt_flag",
    "set_encryption_enabled_resolver",
    "DEFAULT_KEY_ENV",
    "generate_key",
    "decode_key",
    "load_key_from_env",
    "encrypt_bytes",
    "decrypt_bytes",
    "encrypt_file",
    "decrypt_file",
    "decrypt_stream",
    "is_encrypted_data",
    "is_encrypted_file",
    "upload_file_encrypted",
    "download_file_decrypted",
    "decrypt_file_inplace",
    "encrypt_bytes_if_enabled",
    "decrypt_bytes_if_encrypted",
    "put_bytes_encrypted",
    "get_object_bytes_decrypted",
    "stream_object_decrypted",
    "StorageCryptoError",
    "EncryptionKeyError",
    "UnsupportedAlgorithmError",
    "DecryptionError",
]
