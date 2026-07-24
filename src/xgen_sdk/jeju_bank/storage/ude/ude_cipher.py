"""
xgen_sdk.jeju_bank.storage.ude.ude_cipher — KSIGN UDE 저장소 cipher (jeju 전용)

동봉된 PythonAPIForUDE/ 는 KSIGN 제공 원본의 **완전 사본**이다.
    수정 1건(보고됨): sample/SDBAPIForPythonFile.py `_raise_if_failed` 의
    `self.encoding` → `self.charset` — 존재하지 않는 속성 참조로 실패 경로에서
    AttributeError 가 나던 벤더 결함 교정. 그 외 전 파일 바이트 동일.

설계:
    - XSE1 엔벨로프(algorithm_id=2) 안에 KSIGN UDE 암호문(파일 포맷 그대로)을 담는다.
      → 복호화 자동 판별, AES-256-GCM 객체와 혼재 안전, 기존 storage 헬퍼 전부 재사용.
    - alg_header 에 **암호화 당시 정책명**(utf-8)을 기록한다. 정책 설정이 나중에
      바뀌어도 과거 객체는 기록된 정책으로 복호화된다.
    - 키는 KSIGN UDE 에이전트가 정책 단위로 관리 — SDK 의 키 resolve 불필요
      (requires_key=False). XGEN_STORAGE_ENCRYPTION_KEY 는 AES-256 전용.
    - 벤더 API 는 파일 경로 기반(EncryptFile/DecryptFile) — temp 파일 왕복으로
      FileCipher 의 스트림 계약에 맞춘다. AAD 바인딩은 벤더 API 가 지원하지 않아
      적용되지 않는다 (무결성은 UDE 포맷 자체 검증에 위임).
    - .so 로드는 최초 사용 시 lazy. 리눅스 x86-64 전용 — 그 외 환경에서 import 는
      안전하고, 사용 시점에 명확한 StorageCryptoError 를 낸다.
    - 벤더 라이브러리의 스레드 안전성이 보증되지 않아 프로세스 전역 Lock 으로
      호출을 직렬화한다.

경로/정책 결정:
    - SDB_HOME: env 가 있으면 그것을 존중(운영 별도 배치), 없으면 동봉 사본 경로를
      사용한다 (.so 로드 전에 env 로 주입 — 네이티브 lib 가 conf/license 탐색에 씀).
    - 정책명: set_ude_policy_resolver(app_config) > env XGEN_STORAGE_UDE_POLICY
      > 기본값 ARIA_256_UDE.
"""
from __future__ import annotations

import importlib.util
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import BinaryIO, ClassVar, Optional

from xgen_sdk.storage.crypto import (
    DecryptionError,
    FileCipher,
    StorageCryptoError,
)

logger = logging.getLogger(__name__)

_CHUNK_SIZE = 64 * 1024

# 동봉된 KSIGN 원본 사본 (완전 복사)
_BUNDLED_HOME = Path(__file__).resolve().parent / "PythonAPIForUDE"
_VENDOR_WRAPPER = _BUNDLED_HOME / "sample" / "SDBAPIForPythonFile.py"

UDE_HOME_ENV = "SDB_HOME"
UDE_POLICY_ENV = "XGEN_STORAGE_UDE_POLICY"
DEFAULT_UDE_POLICY = "ARIA_256_UDE"

# 벤더 호출 직렬화 (스레드 안전성 미보증) + 싱글턴 보호
_API_LOCK = threading.Lock()
_API = None  # SDBAPI 싱글턴 (프로세스당 1회 로드)


# ── 정책 resolver (app_config 연동 지점 — crypto 의 토글/키 resolver 와 동일 패턴) ──
_POLICY_RESOLVER = None


def set_ude_policy_resolver(resolver) -> None:
    """UDE 암호화 정책명의 외부 설정 resolver 등록.

    resolver: 인자 없는 callable — 정책명 문자열 또는 None(미설정) 반환.
    예외/None 이면 env XGEN_STORAGE_UDE_POLICY > 기본값 fallback.
    None 전달 시 등록 해제.
    """
    global _POLICY_RESOLVER
    if resolver is not None and not callable(resolver):
        raise TypeError("resolver 는 callable 또는 None 이어야 합니다.")
    _POLICY_RESOLVER = resolver


def resolve_ude_policy() -> str:
    """쓰기(암호화)에 사용할 UDE 정책명. resolver > env > 기본값."""
    if _POLICY_RESOLVER is not None:
        try:
            v = _POLICY_RESOLVER()
            if v and str(v).strip():
                return str(v).strip()
        except Exception as e:  # pylint: disable=broad-except
            logger.warning("[ude] 정책 resolver 평가 실패 — env fallback: %s", e)
    env = os.getenv(UDE_POLICY_ENV)
    if env and env.strip():
        return env.strip()
    return DEFAULT_UDE_POLICY


def ude_home() -> Path:
    """UDE 홈 디렉토리 — env SDB_HOME 우선, 없으면 동봉 사본."""
    env = os.getenv(UDE_HOME_ENV)
    if env and env.strip():
        return Path(env.strip())
    return _BUNDLED_HOME


def _load_vendor_sdbapi_class():
    """동봉 사본의 SDBAPIForPythonFile.py 에서 SDBAPI 클래스를 로드.

    사본 폴더를 패키지화(__init__.py 추가)하지 않기 위해 파일 경로 import 를 쓴다 —
    벤더 폴더는 수정 1건 외 원본 그대로 유지된다.
    """
    home = ude_home()
    wrapper = home / "sample" / "SDBAPIForPythonFile.py"
    if not wrapper.exists():
        # 외부 SDB_HOME 에 래퍼가 없으면 동봉 사본의 래퍼로 폴백 (lib/conf 는 SDB_HOME 기준)
        wrapper = _VENDOR_WRAPPER
    if not wrapper.exists():
        raise StorageCryptoError(f"UDE 래퍼를 찾을 수 없습니다: {wrapper}")
    spec = importlib.util.spec_from_file_location(
        "xgen_sdk.jeju_bank.storage.ude._vendor_sdbapi", str(wrapper),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module.SDBAPI


def _get_api():
    """SDBAPI 프로세스 싱글턴. 최초 호출 시 .so 로드 (호출부는 _API_LOCK 보유 필수 아님)."""
    global _API
    if _API is not None:
        return _API
    with _API_LOCK:
        if _API is not None:
            return _API
        home = ude_home()
        # 네이티브 lib 가 conf/license 탐색에 SDB_HOME 을 쓴다 — 로드 전에 보장.
        os.environ.setdefault(UDE_HOME_ENV, str(home))
        try:
            sdbapi_cls = _load_vendor_sdbapi_class()
            api = sdbapi_cls(charset="utf-8")
        except OSError as e:
            raise StorageCryptoError(
                f"UDE 네이티브 라이브러리 로드 실패 (SDB_HOME={home}, 리눅스 x86-64 전용): {e}"
            ) from e
        try:
            # 홈을 명시 지정 (env 미반영 런타임 대비 이중 보장 — 실패해도 치명 아님)
            api.SetAgentHome(str(home))
        except Exception as e:  # pylint: disable=broad-except
            logger.warning("[ude] SetAgentHome 실패 (SDB_HOME env 로 동작 지속): %s", e)
        logger.info("[ude] SDBAPI 로드 완료: home=%s", home)
        _API = api
        return _API


def _pump(reader: BinaryIO, writer: BinaryIO) -> None:
    while True:
        chunk = reader.read(_CHUNK_SIZE)
        if not chunk:
            break
        writer.write(chunk)


class UdeAria256Cipher(FileCipher):
    """KSIGN UDE(정책 기반, 기본 ARIA-256) 파일 암호화 — XSE1 엔벨로프 alg_id=2.

    키 없음(에이전트 관리) — from_key 는 key 를 무시한다.
    """

    algorithm_id: ClassVar[int] = 2
    algorithm_name: ClassVar[str] = "ude-aria256"
    requires_key: ClassVar[bool] = False

    _MAX_POLICY_LEN = 256

    def __init__(self, key: Optional[bytes] = None):  # noqa: ARG002 — 계약 호환
        pass

    @classmethod
    def from_key(cls, key: Optional[bytes]) -> "UdeAria256Cipher":
        return cls()

    def _new_alg_header(self) -> bytes:
        policy = resolve_ude_policy()
        header = policy.encode("utf-8")
        if not header or len(header) > self._MAX_POLICY_LEN:
            raise StorageCryptoError(f"UDE 정책명이 비정상입니다: {policy!r}")
        return header

    @staticmethod
    def _policy_from_header(alg_header: bytes) -> str:
        try:
            policy = alg_header.decode("utf-8").strip()
        except UnicodeDecodeError as e:
            raise DecryptionError("UDE 엔벨로프의 정책명 헤더가 손상되었습니다.") from e
        if not policy:
            raise DecryptionError("UDE 엔벨로프에 정책명이 없습니다.")
        return policy

    def _encrypt_body(self, reader: BinaryIO, writer: BinaryIO, alg_header: bytes, aad: bytes) -> None:
        policy = self._policy_from_header(alg_header)
        api = _get_api()
        tmp_dir = tempfile.mkdtemp(prefix=".xse_ude_enc_")
        plain_path = os.path.join(tmp_dir, "p.bin")
        enc_path = os.path.join(tmp_dir, "e.bin")
        try:
            with open(plain_path, "wb") as f:
                _pump(reader, f)
            with _API_LOCK:
                ok = api.EncryptFile(policy, plain_path, enc_path)
            if ok != 1:
                raise StorageCryptoError(
                    f"UDE 암호화 실패 (policy={policy}): {api.GetLastErrorMsg() or 'unknown error'}"
                )
            with open(enc_path, "rb") as f:
                _pump(f, writer)
        finally:
            for p in (plain_path, enc_path):
                try:
                    os.unlink(p)
                except OSError:
                    pass
            try:
                os.rmdir(tmp_dir)
            except OSError:
                pass

    def _decrypt_body(self, reader: BinaryIO, writer: BinaryIO, alg_header: bytes, aad: bytes) -> None:
        policy = self._policy_from_header(alg_header)
        api = _get_api()
        tmp_dir = tempfile.mkdtemp(prefix=".xse_ude_dec_")
        enc_path = os.path.join(tmp_dir, "e.bin")
        dec_path = os.path.join(tmp_dir, "d.bin")
        try:
            with open(enc_path, "wb") as f:
                _pump(reader, f)
            with _API_LOCK:
                ok = api.DecryptFile(policy, enc_path, dec_path)
            if ok != 1:
                raise DecryptionError(
                    f"UDE 복호화 실패 (policy={policy}): {api.GetLastErrorMsg() or 'unknown error'}"
                )
            with open(dec_path, "rb") as f:
                _pump(f, writer)
        finally:
            for p in (enc_path, dec_path):
                try:
                    os.unlink(p)
                except OSError:
                    pass
            try:
                os.rmdir(tmp_dir)
            except OSError:
                pass
