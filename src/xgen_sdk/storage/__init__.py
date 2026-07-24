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
    # 전역 토글/모드 (쓰기 측 — 읽기는 항상 자동 sniff)
    # 판정 우선순위: 서비스 설정 resolver(app_config) > env
    # 모드 3값: Disable / AES-256 / UDE (과거 bool 값 하위호환 해석)
    DEFAULT_ENABLED_ENV,
    MODE_DISABLE,
    MODE_AES,
    MODE_UDE,
    encryption_enabled,
    encryption_mode,
    resolve_write_algorithm,
    resolve_encrypt_flag,
    set_encryption_enabled_resolver,
    # 키 관리 (resolver: app_config > env)
    DEFAULT_KEY_ENV,
    generate_key,
    decode_key,
    load_key_from_env,
    set_encryption_key_resolver,
    # 암복호화 (복호화는 알고리즘 자동 식별)
    encrypt_bytes,
    decrypt_bytes,
    encrypt_file,
    decrypt_file,
    decrypt_stream,
    is_encrypted_data,
    is_encrypted_file,
    detect_algorithm_name,
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
from xgen_sdk.storage.audit import (
    # 감사(audit) 훅 — 모든 upload/download 를 서비스 DB(minio_logs)에 기록
    set_storage_audit_logger,
    storage_audit_context,
    audit_context_snapshot,
    emit_storage_audit,
)
# jeju 전용 UDE (KSIGN) — cipher 등록은 crypto 모듈 로드 시 이미 완료.
# 정책 resolver 는 다른 resolver 들과 같은 자리(xgen_sdk.storage)에서 쓰도록 재수출.
from xgen_sdk.jeju_bank.storage.ude import (
    DEFAULT_UDE_POLICY,
    UDE_POLICY_ENV,
    UdeAria256Cipher,
    resolve_ude_policy,
    set_ude_policy_resolver,
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
    "MODE_DISABLE",
    "MODE_AES",
    "MODE_UDE",
    "encryption_enabled",
    "encryption_mode",
    "resolve_write_algorithm",
    "resolve_encrypt_flag",
    "set_encryption_enabled_resolver",
    "DEFAULT_KEY_ENV",
    "generate_key",
    "decode_key",
    "load_key_from_env",
    "set_encryption_key_resolver",
    "encrypt_bytes",
    "decrypt_bytes",
    "encrypt_file",
    "decrypt_file",
    "decrypt_stream",
    "is_encrypted_data",
    "is_encrypted_file",
    "detect_algorithm_name",
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
    # audit
    "set_storage_audit_logger",
    "storage_audit_context",
    "audit_context_snapshot",
    "emit_storage_audit",
    # jeju 전용 UDE (KSIGN)
    "DEFAULT_UDE_POLICY",
    "UDE_POLICY_ENV",
    "UdeAria256Cipher",
    "resolve_ude_policy",
    "set_ude_policy_resolver",
]
