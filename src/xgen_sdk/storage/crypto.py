"""
xgen_sdk.storage.crypto — MinIO 업로드/다운로드 파일 암복호화 계층

MinIO 업로드 **이전** 암호화, 다운로드 **이후** 복호화를 담당하는 공통 헬퍼.
서버측(SSE)이 아닌 클라이언트측 암호화 — MinIO 에는 암호문만 저장된다.

설계 원칙:
    - 추상 클래스(FileCipher) + 레지스트리 기반 확장 구조.
      새 알고리즘은 FileCipher 서브클래스 작성 → register_cipher() 한 번이면 끝.
    - 암호문은 자기서술적(self-describing) 엔벨로프 포맷 —
      헤더에 알고리즘 id 가 기록되므로 복호화는 알고리즘을 몰라도 자동 식별된다.
      (알고리즘이 늘어나도 과거 암호문은 영원히 복호화 가능)
    - 스트리밍 처리(64KB 청크) — 수 GB 파일도 메모리 O(1).
    - 기본 알고리즘: AES-256-GCM (인증 암호화 — 기밀성 + 무결성/변조 감지).

엔벨로프 포맷 (v1):
    ┌────────┬─────────┬────────┬───────────────┬────────────┬────────────┬─────────┐
    │ MAGIC  │ version │ alg_id │ alg_header_len│ alg_header │ ciphertext │ trailer │
    │ "XSE1" │  1B     │  1B    │  2B (BE)      │  가변       │  가변       │ 알고리즘별│
    └────────┴─────────┴────────┴───────────────┴────────────┴────────────┴─────────┘
    - AES-256-GCM: alg_header = nonce(12B), trailer = auth tag(16B)
    - 전체 헤더(MAGIC~alg_header)는 AAD 로 인증에 바인딩 — 헤더 변조도 복호화 실패

키 관리:
    - 32바이트 대칭키. 환경변수 XGEN_STORAGE_ENCRYPTION_KEY (base64 또는 hex).
    - generate_key() 로 신규 키 생성(base64). 키는 배포 환경 시크릿으로 주입할 것.
    - 키/평문은 어떤 경우에도 로그에 남기지 않는다.

의존성:
    pycryptodome — minio 패키지의 필수 전이 의존성이므로 SDK 소비자 환경에
    이미 존재한다 (신규 설치 불필요 — 폐쇄망 안전).

사용 예:
    from xgen_sdk.storage import (
        get_minio_client, upload_file_encrypted, download_file_decrypted,
        encrypt_bytes, decrypt_bytes, generate_key,
    )

    client = get_minio_client()
    # 업로드 이전 암호화 (키는 env XGEN_STORAGE_ENCRYPTION_KEY)
    upload_file_encrypted(client, "/tmp/a.pdf", "docs/a.pdf", bucket_name="file-storage")
    # 다운로드 시 복호화 (알고리즘 자동 식별)
    download_file_decrypted(client, "docs/a.pdf", "/tmp/a.pdf", bucket_name="file-storage")
"""
from __future__ import annotations

import base64
import binascii
import logging
import os
import tempfile
from abc import ABC, abstractmethod
from io import BytesIO
from typing import BinaryIO, ClassVar, Dict, Optional, Tuple, Type

from Crypto.Cipher import AES
from Crypto.Random import get_random_bytes

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────
# 엔벨로프 포맷 상수
# ──────────────────────────────────────────────────────────────────────
MAGIC = b"XSE1"                     # Xgen Storage Encryption
ENVELOPE_VERSION = 1
_FIXED_HEADER_LEN = 8               # MAGIC(4) + version(1) + alg_id(1) + alg_header_len(2)
_MAX_ALG_HEADER_LEN = 1024          # 방어적 상한 — 손상/악성 헤더로 인한 과대 읽기 방지
_CHUNK_SIZE = 64 * 1024

DEFAULT_ALGORITHM = "aes256-gcm"
DEFAULT_KEY_ENV = "XGEN_STORAGE_ENCRYPTION_KEY"
DEFAULT_ENABLED_ENV = "XGEN_STORAGE_ENCRYPTION_ENABLED"
KEY_LEN = 32                        # AES-256


# ── 토글 resolver (app_config 연동 지점) ─────────────────────────────
# 서비스가 자체 설정 시스템(xgen-core persistent_configs / ConfigClient 등)으로
# 토글을 제어하도록 등록하는 훅. 등록되면 resolver 판단이 env 보다 우선하며,
# resolver 가 None(판단 불가/미설정)을 반환하거나 예외를 던지면 env 로 fallback.
# resolver 는 encryption_enabled() 호출마다 평가되므로 admin UI 에서 설정을
# 바꾸면 재시작 없이 즉시 반영된다 (설정 조회 비용은 resolver 구현이 책임).
_ENABLED_RESOLVER = None


def set_encryption_enabled_resolver(resolver) -> None:
    """저장소 암호화 토글의 외부 설정 resolver 등록.

    Args:
        resolver: 인자 없는 callable. 반환 계약:
            - True/False  → 그 값으로 결정 (env 무시)
            - None        → 판단 불가 — env XGEN_STORAGE_ENCRYPTION_ENABLED fallback
            - str/int 도 허용 ("true"/"false"/"1"/"0" 등 — SDK 가 강제변환)
            예외 발생 시 warning 로그 후 env fallback (실행을 깨뜨리지 않는다).
        None 을 전달하면 등록 해제.

    사용 예 (서비스 부트 시):
        from xgen_sdk.storage import set_encryption_enabled_resolver
        set_encryption_enabled_resolver(
            lambda: config_composer.get_config_by_name(
                "XGEN_STORAGE_ENCRYPTION_ENABLED").value
        )
    """
    global _ENABLED_RESOLVER
    if resolver is not None and not callable(resolver):
        raise TypeError("resolver 는 callable 또는 None 이어야 합니다.")
    _ENABLED_RESOLVER = resolver


def _coerce_flag(v) -> Optional[bool]:
    """resolver/설정 값의 관대한 bool 변환. 판단 불가 타입/빈 문자열 → None."""
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if not s:
            return None
        return s in {"1", "true", "yes", "on"}
    return None


def encryption_enabled() -> bool:
    """저장소 암호화 전역 토글.

    판정 우선순위:
        1. set_encryption_enabled_resolver() 로 등록된 서비스 설정
           (xgen-core app_config: XGEN_STORAGE_ENCRYPTION_ENABLED — admin UI 제어)
        2. env XGEN_STORAGE_ENCRYPTION_ENABLED (resolver 미등록/판단불가 시)

    - 기본 **off** — 토글을 켜기 전까지 모든 동작은 기존과 100% 동일.
    - 이 토글은 **쓰기(암호화) 측에만** 적용된다. 읽기(복호화)는 토글과 무관하게
      객체의 매직 헤더를 보고 항상 자동 판별 — 암호화 도입 전/후 객체가 혼재해도
      안전하다 (safe rollout: 모든 서비스가 복호화 가능 상태로 먼저 배포된 뒤
      설정을 켠다).
    """
    if _ENABLED_RESOLVER is not None:
        try:
            resolved = _coerce_flag(_ENABLED_RESOLVER())
        except Exception as e:  # pylint: disable=broad-except
            logger.warning("[storage.crypto] 토글 resolver 평가 실패 — env fallback: %s", e)
            resolved = None
        if resolved is not None:
            return resolved
    return _coerce_flag(os.getenv(DEFAULT_ENABLED_ENV)) or False


def resolve_encrypt_flag(explicit: Optional[bool]) -> bool:
    """호출부 명시값 우선, None 이면 전역 토글."""
    return encryption_enabled() if explicit is None else bool(explicit)


# ──────────────────────────────────────────────────────────────────────
# 예외
# ──────────────────────────────────────────────────────────────────────
class StorageCryptoError(Exception):
    """storage 암복호화 계층의 공통 베이스 예외."""


class EncryptionKeyError(StorageCryptoError):
    """키 미설정/형식 오류/길이 오류."""


class UnsupportedAlgorithmError(StorageCryptoError):
    """레지스트리에 없는 알고리즘 id/이름."""


class DecryptionError(StorageCryptoError):
    """복호화 실패 — 헤더 손상, 데이터 변조, 키 불일치, 절단 등."""


# ──────────────────────────────────────────────────────────────────────
# 키 유틸
# ──────────────────────────────────────────────────────────────────────
def generate_key() -> str:
    """신규 32바이트 키를 생성해 base64 문자열로 반환.

    반환값을 배포 환경의 XGEN_STORAGE_ENCRYPTION_KEY 시크릿으로 설정한다.
    """
    return base64.b64encode(get_random_bytes(KEY_LEN)).decode("ascii")


def decode_key(value: str) -> bytes:
    """base64 또는 hex 문자열을 32바이트 키로 디코드.

    판정 순서: base64 디코드 결과가 32B → 채택, 아니면 64자 hex → 채택.
    둘 다 아니면 EncryptionKeyError (raw 문자열은 모호성 때문에 허용하지 않음).
    """
    if not value or not value.strip():
        raise EncryptionKeyError("암호화 키가 비어 있습니다.")
    value = value.strip()

    try:
        decoded = base64.b64decode(value, validate=True)
        if len(decoded) == KEY_LEN:
            return decoded
    except (binascii.Error, ValueError):
        pass

    try:
        decoded = bytes.fromhex(value)
        if len(decoded) == KEY_LEN:
            return decoded
    except ValueError:
        pass

    raise EncryptionKeyError(
        f"암호화 키 형식 오류 — {KEY_LEN}바이트의 base64 또는 hex({KEY_LEN * 2}자) 문자열이어야 합니다. "
        "generate_key() 로 새 키를 만들 수 있습니다."
    )


def load_key_from_env(env_var: str = DEFAULT_KEY_ENV) -> Optional[bytes]:
    """환경변수에서 키를 로드. 미설정 시 None (fallback 을 위해 예외 아님)."""
    raw = os.getenv(env_var)
    if not raw:
        return None
    return decode_key(raw)


# ── 키 resolver (app_config 연동 지점) ───────────────────────────────
# 서비스가 자체 설정 시스템(xgen-core persistent_configs)으로 키를 공급하도록
# 등록하는 훅. 등록되면 env 보다 우선. resolver 는 base64/hex 문자열 또는
# 32바이트 bytes 또는 None(미설정) 을 반환할 수 있다.
_KEY_RESOLVER = None


def set_encryption_key_resolver(resolver) -> None:
    """암호화 키의 외부 설정 resolver 등록.

    Args:
        resolver: 인자 없는 callable. 반환 계약:
            - 32바이트 bytes            → 그대로 사용
            - base64/hex 문자열(32B)     → decode 후 사용
            - None/빈 문자열             → 미설정 — env(XGEN_STORAGE_ENCRYPTION_KEY) fallback
            예외 발생 시 warning 로그 후 env fallback.
        None 을 전달하면 등록 해제.

    사용 예 (서비스 부트 시):
        set_encryption_key_resolver(
            lambda: config_composer.get_config_by_name(
                "XGEN_STORAGE_ENCRYPTION_KEY").value
        )
    """
    global _KEY_RESOLVER
    if resolver is not None and not callable(resolver):
        raise TypeError("resolver 는 callable 또는 None 이어야 합니다.")
    _KEY_RESOLVER = resolver


def _coerce_key(value) -> Optional[bytes]:
    """resolver/설정 값을 32바이트 키로 정규화. 미설정(None/빈) → None."""
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        if len(value) == KEY_LEN:
            return bytes(value)
        raise EncryptionKeyError(f"키 bytes 길이 오류: {len(value)} (필요 {KEY_LEN}).")
    if isinstance(value, str):
        if not value.strip():
            return None
        return decode_key(value)
    raise EncryptionKeyError(f"키 타입 오류: {type(value).__name__}")


def _resolve_key(key: Optional[bytes]) -> bytes:
    """키 결정. 우선순위: 명시 인자 > resolver(app_config) > env.

    어느 경로에서도 키를 얻지 못하면 EncryptionKeyError (평문 조용히 업로드 방지).
    """
    if key is not None:
        if not isinstance(key, (bytes, bytearray)) or len(key) != KEY_LEN:
            raise EncryptionKeyError(
                f"키는 {KEY_LEN}바이트 bytes 여야 합니다 "
                f"(len={len(key) if isinstance(key, (bytes, bytearray)) else 'N/A'})."
            )
        return bytes(key)

    # resolver (서비스 app_config)
    if _KEY_RESOLVER is not None:
        try:
            resolved = _coerce_key(_KEY_RESOLVER())
        except EncryptionKeyError:
            raise
        except Exception as e:  # pylint: disable=broad-except
            logger.warning("[storage.crypto] 키 resolver 평가 실패 — env fallback: %s", e)
            resolved = None
        if resolved is not None:
            return resolved

    # env fallback
    env_key = load_key_from_env()
    if env_key is not None:
        return env_key

    raise EncryptionKeyError(
        "암호화 키가 없습니다 — app_config(XGEN_STORAGE_ENCRYPTION_KEY) 설정 또는 "
        f"환경변수 {DEFAULT_KEY_ENV} 를 주입하세요. generate_key() 로 키를 만들 수 있습니다."
    )


# ──────────────────────────────────────────────────────────────────────
# 엔벨로프 인코딩/디코딩
# ──────────────────────────────────────────────────────────────────────
def _build_envelope(algorithm_id: int, alg_header: bytes) -> bytes:
    """엔벨로프 헤더 직렬화. 반환값 전체가 AAD 로 쓰인다."""
    if not (0 < algorithm_id < 256):
        raise StorageCryptoError(f"algorithm_id 는 1~255 여야 합니다: {algorithm_id}")
    if len(alg_header) > _MAX_ALG_HEADER_LEN:
        raise StorageCryptoError(f"alg_header 가 상한({_MAX_ALG_HEADER_LEN}B)을 초과합니다.")
    return (
        MAGIC
        + bytes([ENVELOPE_VERSION])
        + bytes([algorithm_id])
        + len(alg_header).to_bytes(2, "big")
        + alg_header
    )


def _read_envelope(reader: BinaryIO) -> Tuple[int, bytes, bytes]:
    """reader 에서 엔벨로프 헤더를 읽어 (alg_id, alg_header, aad) 반환.

    aad 는 읽은 헤더 원문 전체 — 복호화 시 인증에 재사용된다.
    """
    fixed = reader.read(_FIXED_HEADER_LEN)
    if len(fixed) != _FIXED_HEADER_LEN:
        raise DecryptionError("암호문 헤더가 불완전합니다 (파일 절단 또는 비암호화 데이터).")
    if fixed[:4] != MAGIC:
        raise DecryptionError(
            "암호화 매직 헤더가 없습니다 — 이 데이터는 xgen 스토리지 암호화 포맷이 아닙니다."
        )
    version = fixed[4]
    if version != ENVELOPE_VERSION:
        raise DecryptionError(f"지원하지 않는 엔벨로프 버전입니다: {version}")
    alg_id = fixed[5]
    header_len = int.from_bytes(fixed[6:8], "big")
    if header_len > _MAX_ALG_HEADER_LEN:
        raise DecryptionError(f"alg_header 길이가 비정상입니다: {header_len}")
    alg_header = reader.read(header_len)
    if len(alg_header) != header_len:
        raise DecryptionError("암호문 헤더가 불완전합니다 (alg_header 절단).")
    return alg_id, alg_header, fixed + alg_header


def is_encrypted_data(data: bytes) -> bool:
    """바이트열이 xgen 스토리지 암호화 포맷인지 (매직 헤더) 판별."""
    return len(data) >= len(MAGIC) and data[: len(MAGIC)] == MAGIC


def is_encrypted_file(path: str) -> bool:
    """파일이 xgen 스토리지 암호화 포맷인지 판별. 읽기 실패 시 False."""
    try:
        with open(path, "rb") as f:
            return is_encrypted_data(f.read(len(MAGIC)))
    except OSError:
        return False


# ──────────────────────────────────────────────────────────────────────
# 추상 클래스
# ──────────────────────────────────────────────────────────────────────
class FileCipher(ABC):
    """파일/바이트 암복호화 알고리즘의 추상 계약.

    서브클래스 구현 요건:
        - algorithm_id  : 1~255 고유 정수. 엔벨로프에 1바이트로 기록되어
                          복호화 시 알고리즘 자동 식별에 쓰인다. **한 번 배정하면
                          영구 불변** — 재사용/변경 시 과거 암호문을 못 읽는다.
        - algorithm_name: 사람 친화적 고유 이름 (예: "aes256-gcm").
        - _new_alg_header(): 암호화 1회마다 새로 생성되는 알고리즘 헤더
                          (예: nonce). 같은 값을 재사용하면 안 되는 값은
                          반드시 여기서 매 호출 무작위 생성할 것.
        - _encrypt_body()/_decrypt_body(): 본문 스트리밍 암복호화.
                          aad(엔벨로프 헤더 원문)를 인증에 바인딩해야 하며,
                          무결성 검증 실패 시 DecryptionError 를 raise 한다.

    등록: 모듈 하단처럼 register_cipher(YourCipher) 호출 한 번.
    """

    #: 엔벨로프에 기록되는 알고리즘 식별자 (1~255, 영구 불변)
    algorithm_id: ClassVar[int]
    #: 알고리즘 이름 (get_cipher(name=...) 로 선택)
    algorithm_name: ClassVar[str]

    @classmethod
    def from_key(cls, key: bytes) -> "FileCipher":
        """키 1개로 인스턴스를 만드는 표준 팩토리.

        키 외 파라미터가 필요한 알고리즘은 이 classmethod 를 오버라이드한다
        (레지스트리 기반 자동 복호화가 이 계약으로 인스턴스를 만든다).
        """
        return cls(key)  # type: ignore[call-arg]

    # ── 서브클래스 구현 지점 ─────────────────────────────────────────
    @abstractmethod
    def _new_alg_header(self) -> bytes:
        """암호화 1회분 알고리즘 헤더 생성 (예: 무작위 nonce)."""

    @abstractmethod
    def _encrypt_body(self, reader: BinaryIO, writer: BinaryIO, alg_header: bytes, aad: bytes) -> None:
        """reader 평문 → writer 에 ciphertext(+trailer) 스트리밍 기록."""

    @abstractmethod
    def _decrypt_body(self, reader: BinaryIO, writer: BinaryIO, alg_header: bytes, aad: bytes) -> None:
        """reader ciphertext(+trailer) → writer 에 평문 스트리밍 기록.

        무결성 검증 실패 시 반드시 DecryptionError. (주의: 스트리밍 특성상
        검증 전에 평문 일부가 writer 에 쓰일 수 있으므로, 최종 파일 반영은
        상위(decrypt_file)가 temp+rename 으로 처리한다.)
        """

    # ── 공통 제공 API (서브클래스 오버라이드 불필요) ──────────────────
    def encrypt_stream(self, reader: BinaryIO, writer: BinaryIO) -> None:
        """reader 평문 전체를 암호화해 writer 에 기록 (엔벨로프 포함)."""
        alg_header = self._new_alg_header()
        envelope = _build_envelope(self.algorithm_id, alg_header)
        writer.write(envelope)
        self._encrypt_body(reader, writer, alg_header, envelope)

    def decrypt_stream(self, reader: BinaryIO, writer: BinaryIO) -> None:
        """이 인스턴스의 알고리즘으로 암호화된 스트림을 복호화.

        다른 알고리즘의 암호문이면 DecryptionError — 알고리즘을 모르면
        모듈 함수 decrypt_stream()/decrypt_file() (자동 식별) 을 쓸 것.
        """
        alg_id, alg_header, aad = _read_envelope(reader)
        if alg_id != self.algorithm_id:
            raise DecryptionError(
                f"알고리즘 불일치 — 데이터는 id={alg_id}, 이 cipher 는 "
                f"{self.algorithm_name}(id={self.algorithm_id}). "
                "decrypt_file()/decrypt_bytes() (자동 식별) 을 사용하세요."
            )
        self._decrypt_body(reader, writer, alg_header, aad)

    def encrypt_bytes(self, data: bytes) -> bytes:
        """바이트열 암호화 (소용량 편의 API — 대용량 파일은 encrypt_file)."""
        out = BytesIO()
        self.encrypt_stream(BytesIO(data), out)
        return out.getvalue()

    def decrypt_bytes(self, data: bytes) -> bytes:
        """바이트열 복호화 (이 인스턴스의 알고리즘 한정)."""
        out = BytesIO()
        self.decrypt_stream(BytesIO(data), out)
        return out.getvalue()


# ──────────────────────────────────────────────────────────────────────
# AES-256-GCM (기본 알고리즘)
# ──────────────────────────────────────────────────────────────────────
class Aes256GcmCipher(FileCipher):
    """AES-256-GCM 인증 암호화.

    - nonce 12B: 암호화 1회마다 os 급 CSPRNG 로 무작위 생성 (재사용 금지 원칙)
    - tag 16B: 본문 + 엔벨로프 헤더(AAD) 전체에 대한 인증 — 1비트 변조도 감지
    - pycryptodome 의 GCM 은 증분 encrypt/decrypt 를 지원 → 스트리밍 O(1) 메모리
    """

    algorithm_id: ClassVar[int] = 1
    algorithm_name: ClassVar[str] = "aes256-gcm"

    _NONCE_LEN = 12
    _TAG_LEN = 16

    def __init__(self, key: bytes):
        if not isinstance(key, (bytes, bytearray)) or len(key) != KEY_LEN:
            raise EncryptionKeyError(f"AES-256 키는 {KEY_LEN}바이트여야 합니다.")
        self._key = bytes(key)

    def _new_alg_header(self) -> bytes:
        return get_random_bytes(self._NONCE_LEN)

    def _encrypt_body(self, reader: BinaryIO, writer: BinaryIO, alg_header: bytes, aad: bytes) -> None:
        cipher = AES.new(self._key, AES.MODE_GCM, nonce=alg_header, mac_len=self._TAG_LEN)
        cipher.update(aad)
        while True:
            chunk = reader.read(_CHUNK_SIZE)
            if not chunk:
                break
            writer.write(cipher.encrypt(chunk))
        writer.write(cipher.digest())

    def _decrypt_body(self, reader: BinaryIO, writer: BinaryIO, alg_header: bytes, aad: bytes) -> None:
        if len(alg_header) != self._NONCE_LEN:
            raise DecryptionError(f"AES-GCM nonce 길이 오류: {len(alg_header)}")
        cipher = AES.new(self._key, AES.MODE_GCM, nonce=alg_header, mac_len=self._TAG_LEN)
        cipher.update(aad)

        # trailer(tag 16B) hold-back 스트리밍 — 마지막 16B 는 본문이 아니라 태그다.
        tail = b""
        while True:
            chunk = reader.read(_CHUNK_SIZE)
            if not chunk:
                break
            tail += chunk
            if len(tail) > self._TAG_LEN:
                writer.write(cipher.decrypt(tail[: -self._TAG_LEN]))
                tail = tail[-self._TAG_LEN:]
        if len(tail) != self._TAG_LEN:
            raise DecryptionError("암호문이 절단되었습니다 (인증 태그 불완전).")
        try:
            cipher.verify(tail)
        except ValueError as e:
            raise DecryptionError(
                "복호화 무결성 검증 실패 — 키 불일치 또는 데이터 변조."
            ) from e


# ──────────────────────────────────────────────────────────────────────
# 레지스트리 (확장 지점)
# ──────────────────────────────────────────────────────────────────────
_REGISTRY_BY_ID: Dict[int, Type[FileCipher]] = {}
_REGISTRY_BY_NAME: Dict[str, Type[FileCipher]] = {}


def register_cipher(cls: Type[FileCipher]) -> Type[FileCipher]:
    """FileCipher 서브클래스를 레지스트리에 등록 (데코레이터로도 사용 가능).

    algorithm_id / algorithm_name 이 기존 등록과 충돌하면 StorageCryptoError.
    같은 클래스 재등록은 idempotent (모듈 재로드 안전).
    """
    alg_id = getattr(cls, "algorithm_id", None)
    name = getattr(cls, "algorithm_name", None)
    if not isinstance(alg_id, int) or not (0 < alg_id < 256):
        raise StorageCryptoError(f"{cls.__name__}: algorithm_id(1~255) 필수.")
    if not name or not isinstance(name, str):
        raise StorageCryptoError(f"{cls.__name__}: algorithm_name 필수.")

    existing = _REGISTRY_BY_ID.get(alg_id)
    if existing is not None and existing is not cls:
        raise StorageCryptoError(
            f"algorithm_id={alg_id} 충돌: {existing.__name__} 가 이미 사용 중."
        )
    existing_by_name = _REGISTRY_BY_NAME.get(name)
    if existing_by_name is not None and existing_by_name is not cls:
        raise StorageCryptoError(
            f"algorithm_name='{name}' 충돌: {existing_by_name.__name__} 가 이미 사용 중."
        )

    _REGISTRY_BY_ID[alg_id] = cls
    _REGISTRY_BY_NAME[name] = cls
    return cls


def get_cipher(algorithm: str = DEFAULT_ALGORITHM, key: Optional[bytes] = None) -> FileCipher:
    """알고리즘 이름으로 cipher 인스턴스를 생성. key 생략 시 환경변수 로드."""
    cls = _REGISTRY_BY_NAME.get(algorithm)
    if cls is None:
        raise UnsupportedAlgorithmError(
            f"알 수 없는 알고리즘: '{algorithm}' (등록됨: {sorted(_REGISTRY_BY_NAME)})"
        )
    return cls.from_key(_resolve_key(key))


register_cipher(Aes256GcmCipher)


# ──────────────────────────────────────────────────────────────────────
# 모듈 레벨 편의 함수 (복호화는 알고리즘 자동 식별)
# ──────────────────────────────────────────────────────────────────────
def encrypt_bytes(data: bytes, key: Optional[bytes] = None, algorithm: str = DEFAULT_ALGORITHM) -> bytes:
    """바이트열 암호화 (기본 AES-256-GCM)."""
    return get_cipher(algorithm, key).encrypt_bytes(data)


def decrypt_bytes(data: bytes, key: Optional[bytes] = None) -> bytes:
    """바이트열 복호화 — 엔벨로프 헤더로 알고리즘 자동 식별."""
    out = BytesIO()
    decrypt_stream(BytesIO(data), out, key=key)
    return out.getvalue()


def decrypt_stream(reader: BinaryIO, writer: BinaryIO, key: Optional[bytes] = None) -> None:
    """스트림 복호화 — 엔벨로프 헤더로 알고리즘 자동 식별."""
    alg_id, alg_header, aad = _read_envelope(reader)
    cls = _REGISTRY_BY_ID.get(alg_id)
    if cls is None:
        raise UnsupportedAlgorithmError(
            f"알 수 없는 algorithm_id={alg_id} — SDK 버전이 낮거나 커스텀 cipher 미등록."
        )
    cipher = cls.from_key(_resolve_key(key))
    cipher._decrypt_body(reader, writer, alg_header, aad)  # pylint: disable=protected-access


def encrypt_file(
    source_path: str,
    dest_path: str,
    key: Optional[bytes] = None,
    algorithm: str = DEFAULT_ALGORITHM,
) -> None:
    """파일 암호화 (스트리밍 — 대용량 안전). source == dest 는 금지."""
    if os.path.abspath(source_path) == os.path.abspath(dest_path):
        raise StorageCryptoError("source 와 dest 가 같은 파일입니다 (in-place 암호화 미지원).")
    cipher = get_cipher(algorithm, key)
    with open(source_path, "rb") as r, open(dest_path, "wb") as w:
        cipher.encrypt_stream(r, w)


def decrypt_file(source_path: str, dest_path: str, key: Optional[bytes] = None) -> None:
    """파일 복호화 (알고리즘 자동 식별, 스트리밍).

    무결성 검증이 끝나기 전에는 dest_path 에 평문이 나타나지 않는다 —
    같은 디렉토리의 temp 파일에 기록 후 검증 성공 시에만 os.replace (원자적).
    """
    if os.path.abspath(source_path) == os.path.abspath(dest_path):
        raise StorageCryptoError("source 와 dest 가 같은 파일입니다 (in-place 복호화 미지원).")
    dest_dir = os.path.dirname(os.path.abspath(dest_path)) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=".xse_dec_", dir=dest_dir)
    try:
        with open(source_path, "rb") as r, os.fdopen(fd, "wb") as w:
            decrypt_stream(r, w, key=key)
        os.replace(tmp_path, dest_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ──────────────────────────────────────────────────────────────────────
# MinIO 결합 헬퍼 — 업로드 이전 암호화 / 다운로드 시 복호화
# ──────────────────────────────────────────────────────────────────────
# minio 관련 import 는 함수 내부에서 지연 수행 — crypto 모듈 자체는 minio 설치
# 없이도 import/테스트 가능하도록 결합을 낮춘다 (순수 암복호화는 pycryptodome 만 필요).

def upload_file_encrypted(
    client,
    source_path: str,
    object_name: str,
    bucket_name: Optional[str] = None,
    key: Optional[bytes] = None,
    algorithm: str = DEFAULT_ALGORITHM,
    content_type: Optional[str] = None,
) -> None:
    """파일을 **암호화한 뒤** MinIO 에 업로드.

    원본(source_path)은 건드리지 않는다. 임시 암호문 파일은 업로드 후 즉시 삭제.
    content_type 기본값은 application/octet-stream — 저장되는 객체는 암호문이다.

    Args:
        client: xgen_sdk.storage.get_minio_client() 로 얻은 클라이언트
        bucket_name: 생략 시 DEFAULT_BUCKET_NAME (xgen_sdk.storage 와 동일 규칙)
        key: 생략 시 환경변수 XGEN_STORAGE_ENCRYPTION_KEY
    """
    from xgen_sdk.storage.minio_client import DEFAULT_BUCKET_NAME, upload_file

    if bucket_name is None:
        bucket_name = DEFAULT_BUCKET_NAME

    fd, tmp_path = tempfile.mkstemp(prefix=".xse_enc_")
    os.close(fd)
    try:
        encrypt_file(source_path, tmp_path, key=key, algorithm=algorithm)
        upload_file(
            client,
            tmp_path,
            object_name,
            bucket_name=bucket_name,
            content_type=content_type or "application/octet-stream",
        )
        logger.info(
            "[storage.crypto] 암호화 업로드 완료: %s/%s (alg=%s)",
            bucket_name, object_name, algorithm,
        )
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def download_file_decrypted(
    client,
    object_name: str,
    destination_path: str,
    bucket_name: Optional[str] = None,
    key: Optional[bytes] = None,
    allow_plaintext: bool = False,
) -> bool:
    """MinIO 객체를 다운로드해 **복호화한 뒤** destination_path 에 저장.

    알고리즘은 엔벨로프 헤더로 자동 식별. 임시 암호문 파일은 완료 후 즉시 삭제.

    Args:
        allow_plaintext: True 면 암호화되지 않은 객체(매직 헤더 없음)를
            복호화 없이 그대로 저장 — 암호화 도입 이전 데이터와의 혼재기
            마이그레이션용. False(기본)면 비암호화 객체는 DecryptionError.

    Returns:
        True  = 복호화 수행됨
        False = 비암호화 객체를 그대로 저장 (allow_plaintext=True 인 경우만)
    """
    from xgen_sdk.storage.minio_client import DEFAULT_BUCKET_NAME, download_file

    if bucket_name is None:
        bucket_name = DEFAULT_BUCKET_NAME

    fd, tmp_path = tempfile.mkstemp(prefix=".xse_dl_")
    os.close(fd)
    try:
        download_file(client, object_name, tmp_path, bucket_name=bucket_name)

        if not is_encrypted_file(tmp_path):
            if not allow_plaintext:
                raise DecryptionError(
                    f"객체 {bucket_name}/{object_name} 는 암호화 포맷이 아닙니다. "
                    "비암호화 데이터를 허용하려면 allow_plaintext=True 를 사용하세요."
                )
            # 비암호화 객체 — 그대로 반영 (temp+replace 로 부분 파일 방지)
            dest_dir = os.path.dirname(os.path.abspath(destination_path)) or "."
            fd2, tmp2 = tempfile.mkstemp(prefix=".xse_plain_", dir=dest_dir)
            os.close(fd2)
            try:
                with open(tmp_path, "rb") as src, open(tmp2, "wb") as dst:
                    while True:
                        chunk = src.read(_CHUNK_SIZE)
                        if not chunk:
                            break
                        dst.write(chunk)
                os.replace(tmp2, destination_path)
            except BaseException:
                try:
                    os.unlink(tmp2)
                except OSError:
                    pass
                raise
            logger.info(
                "[storage.crypto] 비암호화 객체 그대로 저장 (allow_plaintext): %s/%s",
                bucket_name, object_name,
            )
            return False

        decrypt_file(tmp_path, destination_path, key=key)
        logger.info(
            "[storage.crypto] 복호화 다운로드 완료: %s/%s", bucket_name, object_name,
        )
        return True
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ──────────────────────────────────────────────────────────────────────
# 직접 구현부(서비스 자체 client 호출 사이트)용 프리미티브
# ──────────────────────────────────────────────────────────────────────
# SDK 의 upload_file/download_file 을 거치지 않고 client.put_object/get_object/
# fput_object/fget_object 를 직접 쓰는 서비스 코드가 최소 diff 로 암복호화를
# 결합할 수 있게 하는 저수준 헬퍼들.
#
# 원칙 (safe rollout):
#   - 읽기: 토글과 무관하게 **항상** 매직 헤더 sniff → 암호화면 복호화, 평문이면
#     그대로. (모든 reader 가 먼저 복호화-가능 상태로 배포된 뒤 토글을 켠다)
#   - 쓰기: encryption_enabled() 토글(또는 encrypt 명시 인자)을 따른다.

def decrypt_file_inplace(path: str, key: Optional[bytes] = None) -> bool:
    """다운로드된 파일이 암호화 포맷이면 같은 경로에 복호화 반영 (원자적).

    평문 파일이면 아무 것도 하지 않는다 — 혼재기 하위 호환의 핵심 프리미티브.

    Returns:
        True = 복호화 수행됨, False = 평문(무변경)
    """
    if not is_encrypted_file(path):
        return False
    # decrypt_file 은 in-place 를 금지하므로: 원본을 .enc 임시로 비켜두고 복호화.
    enc_tmp = path + ".xse_src_tmp"
    os.replace(path, enc_tmp)
    try:
        decrypt_file(enc_tmp, path, key=key)
        return True
    except BaseException:
        # 실패 시 원본(암호문)을 되돌려 호출부가 상황을 보존/진단할 수 있게 한다.
        try:
            if not os.path.exists(path):
                os.replace(enc_tmp, path)
        except OSError:
            pass
        raise
    finally:
        try:
            os.unlink(enc_tmp)
        except OSError:
            pass


def encrypt_bytes_if_enabled(
    data: bytes,
    key: Optional[bytes] = None,
    algorithm: str = DEFAULT_ALGORITHM,
    encrypt: Optional[bool] = None,
) -> bytes:
    """토글(또는 encrypt 명시)에 따라 bytes 를 암호화. off 면 원본 그대로."""
    if not resolve_encrypt_flag(encrypt):
        return data
    return encrypt_bytes(data, key=key, algorithm=algorithm)


def decrypt_bytes_if_encrypted(data: bytes, key: Optional[bytes] = None) -> bytes:
    """bytes 가 암호화 포맷이면 복호화, 평문이면 그대로 (읽기 측 sniff 규칙)."""
    if not is_encrypted_data(data):
        return data
    return decrypt_bytes(data, key=key)


def put_bytes_encrypted(
    client,
    data: bytes,
    object_name: str,
    bucket_name: Optional[str] = None,
    content_type: str = "application/octet-stream",
    key: Optional[bytes] = None,
    algorithm: str = DEFAULT_ALGORITHM,
    encrypt: Optional[bool] = None,
) -> None:
    """bytes 를 (토글에 따라 암호화 후) client.put_object 로 업로드.

    client.put_object 직접 호출 사이트의 drop-in 대체용.
    """
    import time as _time
    from xgen_sdk.storage.minio_client import DEFAULT_BUCKET_NAME
    from xgen_sdk.storage import audit

    if bucket_name is None:
        bucket_name = DEFAULT_BUCKET_NAME
    _t0 = _time.monotonic()
    payload = encrypt_bytes_if_enabled(data, key=key, algorithm=algorithm, encrypt=encrypt)
    did_encrypt = payload is not data
    if did_encrypt:
        content_type = "application/octet-stream"  # 암호문의 정직한 타입
    try:
        client.put_object(
            bucket_name,
            object_name,
            BytesIO(payload),
            length=len(payload),
            content_type=content_type,
        )
    except Exception as e:
        audit.emit_storage_audit(
            "upload", bucket_name, object_name,
            plaintext_size_bytes=len(data), encrypted=did_encrypt,
            encryption_algorithm=(algorithm if did_encrypt else None),
            content_type=content_type, status="error", error_message=str(e),
            duration_ms=int((_time.monotonic() - _t0) * 1000),
        )
        raise
    audit.emit_storage_audit(
        "upload", bucket_name, object_name,
        size_bytes=len(payload), plaintext_size_bytes=len(data),
        encrypted=did_encrypt,
        encryption_algorithm=(algorithm if did_encrypt else None),
        content_type=content_type, status="success",
        duration_ms=int((_time.monotonic() - _t0) * 1000),
    )


def get_object_bytes_decrypted(
    client,
    object_name: str,
    bucket_name: Optional[str] = None,
    key: Optional[bytes] = None,
) -> bytes:
    """client.get_object 로 읽고, 암호화 포맷이면 복호화해 bytes 반환.

    client.get_object(...).read() 직접 호출 사이트의 drop-in 대체용.
    """
    import time as _time
    from xgen_sdk.storage.minio_client import DEFAULT_BUCKET_NAME
    from xgen_sdk.storage import audit

    if bucket_name is None:
        bucket_name = DEFAULT_BUCKET_NAME
    _t0 = _time.monotonic()
    try:
        response = client.get_object(bucket_name, object_name)
        try:
            raw = response.read()
        finally:
            try:
                response.close()
                response.release_conn()
            except Exception:  # pylint: disable=broad-except
                pass
    except Exception as e:
        audit.emit_storage_audit(
            "download", bucket_name, object_name,
            status="error", error_message=str(e),
            duration_ms=int((_time.monotonic() - _t0) * 1000),
        )
        raise
    was_encrypted = is_encrypted_data(raw)
    result = decrypt_bytes_if_encrypted(raw, key=key)
    audit.emit_storage_audit(
        "download", bucket_name, object_name,
        size_bytes=len(result), encrypted=was_encrypted,
        encryption_algorithm=(DEFAULT_ALGORITHM if was_encrypted else None),
        status="success", duration_ms=int((_time.monotonic() - _t0) * 1000),
    )
    return result


def stream_object_decrypted(
    client,
    object_name: str,
    bucket_name: Optional[str] = None,
    key: Optional[bytes] = None,
    chunk_size: int = _CHUNK_SIZE,
):
    """객체를 (필요 시 복호화하여) 평문 청크 이터레이터로 반환.

    StreamingResponse 등 HTTP 청크 응답 사이트용 — 암호화 객체를 그대로
    스트리밍하면 클라이언트가 암호문을 받게 되는 문제를 막는다.
    무결성 검증(태그)이 끝난 뒤에만 yield 를 시작한다 (temp 복호화 후 스트림).

    사용:
        for chunk in stream_object_decrypted(client, obj, bucket):
            yield chunk   # FastAPI StreamingResponse 등
    """
    from xgen_sdk.storage.minio_client import DEFAULT_BUCKET_NAME, download_file as _download

    if bucket_name is None:
        bucket_name = DEFAULT_BUCKET_NAME

    fd, tmp_path = tempfile.mkstemp(prefix=".xse_stream_")
    os.close(fd)
    try:
        # 원문을 받아 명시 key 로 복호화 (key=None 이면 env) — download_file 의
        # 내장 자동복호화는 env 키만 쓰므로, key 인자를 존중하기 위해 decrypt=False
        # 로 받고 여기서 sniff+복호화한다 (평문 객체는 no-op).
        _download(client, object_name, tmp_path, bucket_name=bucket_name, decrypt=False)
        decrypt_file_inplace(tmp_path, key=key)
        with open(tmp_path, "rb") as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                yield chunk
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
