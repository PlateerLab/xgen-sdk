"""
xgen_sdk.jeju_bank.storage.ude (KSIGN UDE cipher) 단위 테스트.

네이티브 .so 는 리눅스 x86-64 전용이라 여기서는 로드하지 않는다 — SDBAPI 싱글턴을
가짜 구현으로 대체해 엔벨로프/레지스트리/모드 플럼빙 계약을 검증한다.
실제 에이전트 연동은 jeju 환경 통합 체크리스트로 검증한다.

실행: python tests/test_jeju_ude_cipher.py (또는 pytest)
"""
from __future__ import annotations

import contextlib
import os
import sys
import types

# ── minio 스텁 (설치 안 된 환경 대비) — test_storage_crypto.py 와 동일 패턴 ──
try:
    import minio  # noqa: F401
except ImportError:
    _minio = types.ModuleType("minio")

    class _Minio:
        def __init__(self, *a, **k):
            pass

    _minio.Minio = _Minio
    _err = types.ModuleType("minio.error")

    class _S3Error(Exception):
        code = ""

    _err.S3Error = _S3Error
    _minio.error = _err
    sys.modules["minio"] = _minio
    sys.modules["minio.error"] = _err

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from xgen_sdk.storage.crypto import (  # noqa: E402
    DEFAULT_ENABLED_ENV,
    DEFAULT_KEY_ENV,
    DecryptionError,
    MODE_AES,
    MODE_DISABLE,
    MODE_UDE,
    StorageCryptoError,
    _coerce_mode,
    decrypt_bytes,
    detect_algorithm_name,
    encrypt_bytes,
    encrypt_bytes_if_enabled,
    encryption_enabled,
    encryption_mode,
    generate_key,
    is_encrypted_data,
    resolve_write_algorithm,
    set_encryption_enabled_resolver,
    set_encryption_key_resolver,
)
from xgen_sdk.jeju_bank.storage.ude import (  # noqa: E402
    UdeAria256Cipher,
    set_ude_policy_resolver,
)
from xgen_sdk.jeju_bank.storage.ude import ude_cipher as _ude_mod  # noqa: E402


@contextlib.contextmanager
def _env(**kv):
    """환경변수 임시 설정 (None → 제거)."""
    old = {}
    for k, v in kv.items():
        old[k] = os.environ.get(k)
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class _FakeSdbApi:
    """벤더 SDBAPI 의 가짜 구현 — 파일 계약(경로 in/out, 1=성공)을 흉내낸다.

    'KSFENC' + policy + NUL + XOR(0x5A) 본문 — 정책 불일치/절단을 감지할 수 있어
    복호화가 기록된 정책으로 수행되는지 검증 가능.
    """

    def __init__(self, fail=False):
        self.fail = fail
        self.enc_calls: list[str] = []
        self.dec_calls: list[str] = []

    @staticmethod
    def _xor(data: bytes) -> bytes:
        return bytes(b ^ 0x5A for b in data)

    def EncryptFile(self, policy, src, dst):  # noqa: N802 — 벤더 계약
        self.enc_calls.append(policy)
        if self.fail:
            return 0
        with open(src, "rb") as f:
            plain = f.read()
        with open(dst, "wb") as f:
            f.write(b"KSFENC" + policy.encode("utf-8") + b"\x00" + self._xor(plain))
        return 1

    def DecryptFile(self, policy, src, dst):  # noqa: N802 — 벤더 계약
        self.dec_calls.append(policy)
        if self.fail:
            return 0
        with open(src, "rb") as f:
            blob = f.read()
        prefix = b"KSFENC" + policy.encode("utf-8") + b"\x00"
        if not blob.startswith(prefix):
            return 0  # 정책 불일치/손상 — 벤더 실패 계약
        with open(dst, "wb") as f:
            f.write(self._xor(blob[len(prefix):]))
        return 1

    def GetLastErrorMsg(self):  # noqa: N802
        return "fake vendor error"


@contextlib.contextmanager
def _fake_api(fail=False):
    """SDBAPI 싱글턴을 가짜로 치환 (.so 미로드)."""
    fake = _FakeSdbApi(fail=fail)
    old = _ude_mod._API
    _ude_mod._API = fake
    try:
        yield fake
    finally:
        _ude_mod._API = old


@contextlib.contextmanager
def _no_resolvers():
    """토글/키/정책 resolver 초기화 (테스트 격리)."""
    set_encryption_enabled_resolver(None)
    set_encryption_key_resolver(None)
    set_ude_policy_resolver(None)
    try:
        yield
    finally:
        set_encryption_enabled_resolver(None)
        set_encryption_key_resolver(None)
        set_ude_policy_resolver(None)


def test_mode_coercion():
    """3값/bool 하위호환/대소문자/불명 값의 모드 정규화."""
    assert _coerce_mode(True) == MODE_AES
    assert _coerce_mode(False) == MODE_DISABLE
    assert _coerce_mode("Disable") == MODE_DISABLE
    assert _coerce_mode("disable") == MODE_DISABLE
    assert _coerce_mode("off") == MODE_DISABLE
    assert _coerce_mode("false") == MODE_DISABLE
    assert _coerce_mode("0") == MODE_DISABLE
    assert _coerce_mode("AES-256") == MODE_AES
    assert _coerce_mode("aes256-gcm") == MODE_AES
    assert _coerce_mode("true") == MODE_AES   # bool 시절 값 하위호환
    assert _coerce_mode("1") == MODE_AES
    assert _coerce_mode("UDE") == MODE_UDE
    assert _coerce_mode("ude-aria256") == MODE_UDE
    assert _coerce_mode("ARIA_256_UDE") == MODE_UDE
    assert _coerce_mode("") is None
    assert _coerce_mode("whatever") is None
    assert _coerce_mode(None) is None


def test_encryption_mode_resolver_and_env():
    """resolver 3값 판정 + 불명 값 env fallback + enabled 호환."""
    with _no_resolvers(), _env(**{DEFAULT_ENABLED_ENV: None}):
        assert encryption_mode() == MODE_DISABLE
        assert encryption_enabled() is False

        set_encryption_enabled_resolver(lambda: "UDE")
        assert encryption_mode() == MODE_UDE
        assert resolve_write_algorithm() == "ude-aria256"
        assert encryption_enabled() is True

        set_encryption_enabled_resolver(lambda: "AES-256")
        assert encryption_mode() == MODE_AES

        set_encryption_enabled_resolver(lambda: "Disable")
        assert encryption_mode() == MODE_DISABLE
        assert resolve_write_algorithm() is None

        # 불명 값 → env fallback
        set_encryption_enabled_resolver(lambda: "garbage")
        with _env(**{DEFAULT_ENABLED_ENV: "UDE"}):
            assert encryption_mode() == MODE_UDE

        # resolver 예외 → env fallback
        def _boom():
            raise RuntimeError("boom")
        set_encryption_enabled_resolver(_boom)
        with _env(**{DEFAULT_ENABLED_ENV: "AES-256"}):
            assert encryption_mode() == MODE_AES


def test_ude_roundtrip_without_key():
    """키 미설정 환경에서 UDE 왕복 (requires_key=False 계약)."""
    data = b"jeju ude payload \xea\xb0\x80" * 100
    with _no_resolvers(), _env(**{DEFAULT_KEY_ENV: None}), _fake_api() as fake:
        blob = encrypt_bytes(data, algorithm="ude-aria256")
        assert is_encrypted_data(blob)
        assert blob[5] == UdeAria256Cipher.algorithm_id  # alg_id=2
        assert data not in blob  # 본문이 평문 그대로 노출되지 않음
        out = decrypt_bytes(blob)  # 자동 판별 + 키 resolve 없이
        assert out == data
        assert fake.enc_calls and fake.dec_calls


def test_ude_policy_recorded_in_envelope():
    """암호화 당시 정책명이 엔벨로프에 기록되어, 이후 정책이 바뀌어도 그 정책으로 복호화."""
    with _no_resolvers(), _env(**{DEFAULT_KEY_ENV: None}), _fake_api() as fake:
        set_ude_policy_resolver(lambda: "POLICY_A")
        blob = encrypt_bytes(b"hello", algorithm="ude-aria256")
        set_ude_policy_resolver(lambda: "POLICY_B")  # 정책 변경 후에도
        assert decrypt_bytes(blob) == b"hello"
        assert fake.enc_calls == ["POLICY_A"]
        assert fake.dec_calls == ["POLICY_A"]  # 기록된 정책으로 복호화


def test_mode_ude_drives_write_helpers():
    """모드=UDE 면 토글 헬퍼가 UDE 로 암호화한다 (알고리즘 명시 없이)."""
    with _no_resolvers(), _env(**{DEFAULT_KEY_ENV: None}), _fake_api() as fake:
        set_encryption_enabled_resolver(lambda: "UDE")
        blob = encrypt_bytes_if_enabled(b"data-by-mode")
        assert is_encrypted_data(blob) and blob[5] == UdeAria256Cipher.algorithm_id
        assert fake.enc_calls  # UDE 경로 사용됨
        assert decrypt_bytes(blob) == b"data-by-mode"

        # 모드 Disable → 평문 그대로
        set_encryption_enabled_resolver(lambda: "Disable")
        assert encrypt_bytes_if_enabled(b"plain") == b"plain"


def test_aes_objects_decrypt_regardless_of_mode():
    """모드가 UDE 여도 기존 AES 객체는 키로 자동 복호화 (혼재 안전)."""
    key = generate_key()
    with _no_resolvers(), _env(**{DEFAULT_KEY_ENV: key}), _fake_api():
        aes_blob = encrypt_bytes(b"aes object", algorithm="aes256-gcm")
        set_encryption_enabled_resolver(lambda: "UDE")  # 이후 모드 전환 상황
        assert decrypt_bytes(aes_blob) == b"aes object"


def test_detect_algorithm_name():
    key = generate_key()
    with _no_resolvers(), _env(**{DEFAULT_KEY_ENV: key}), _fake_api():
        assert detect_algorithm_name(encrypt_bytes(b"a", algorithm="aes256-gcm")) == "aes256-gcm"
        assert detect_algorithm_name(encrypt_bytes(b"a", algorithm="ude-aria256")) == "ude-aria256"
        assert detect_algorithm_name(b"plain bytes") is None


def test_ude_vendor_failure_raises():
    """벤더 실패(0 반환) → 암호화는 StorageCryptoError, 복호화는 DecryptionError."""
    with _no_resolvers(), _env(**{DEFAULT_KEY_ENV: None}):
        with _fake_api():
            blob = encrypt_bytes(b"x", algorithm="ude-aria256")
        with _fake_api(fail=True):
            try:
                encrypt_bytes(b"x", algorithm="ude-aria256")
                raise AssertionError("암호화 실패가 전파되지 않음")
            except StorageCryptoError:
                pass
            try:
                decrypt_bytes(blob)
                raise AssertionError("복호화 실패가 전파되지 않음")
            except DecryptionError:
                pass


def test_vendor_copy_bugfix_present():
    """동봉 사본의 벤더 결함 교정(self.encoding→self.charset)이 유지되는지 회귀 가드."""
    wrapper = os.path.join(
        os.path.dirname(os.path.abspath(_ude_mod.__file__)),
        "PythonAPIForUDE", "sample", "SDBAPIForPythonFile.py",
    )
    with open(wrapper, encoding="utf-8") as f:
        src = f.read()
    # 실제 호출 라인 기준 검사 (교정 이력 주석의 'self.encoding' 언급은 무관)
    assert "err.decode(self.encoding" not in src, "벤더 결함(err.decode(self.encoding))이 되살아남"
    assert "err.decode(self.charset" in src


# ──────────────────────────────────────────────────────────────────────
# 러너 (pytest 없이 직접 실행 가능)
# ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    fns = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = []
    for name, fn in fns:
        try:
            fn()
            print(f"PASS  {name}")
        except Exception as e:  # noqa: BLE001
            failed.append(name)
            print(f"FAIL  {name}: {type(e).__name__}: {e}")
    print()
    print(f"{len(fns) - len(failed)}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
