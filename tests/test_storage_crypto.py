"""
xgen_sdk.storage.crypto 단위 테스트.

pytest 로 실행하거나 (pytest tests/test_storage_crypto.py),
python 으로 직접 실행할 수 있다 (python tests/test_storage_crypto.py).

minio 패키지가 없는 환경(개발 PC 등)을 위해 import 전에 minio 를 스텁으로
대체한다 — crypto 모듈 자체는 pycryptodome 만 필요하며, MinIO 결합 헬퍼는
upload_file/download_file 을 파일 복사로 흉내내는 가짜 스토리지로 검증한다.
"""
from __future__ import annotations

import io
import os
import shutil
import sys
import tempfile
import types

# ── minio 스텁 (설치 안 된 환경 대비 — 설치돼 있으면 실물 사용) ──
try:
    import minio  # noqa: F401
except ImportError:
    _minio = types.ModuleType("minio")

    class _Minio:  # noqa: D401
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

# src 레이아웃 대응 (repo 루트에서 직접 실행 시)
_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from xgen_sdk.storage.crypto import (  # noqa: E402
    DEFAULT_KEY_ENV,
    Aes256GcmCipher,
    DecryptionError,
    EncryptionKeyError,
    FileCipher,
    MAGIC,
    StorageCryptoError,
    UnsupportedAlgorithmError,
    decode_key,
    decrypt_bytes,
    decrypt_file,
    download_file_decrypted,
    encrypt_bytes,
    encrypt_file,
    generate_key,
    get_cipher,
    is_encrypted_data,
    is_encrypted_file,
    load_key_from_env,
    register_cipher,
    upload_file_encrypted,
)

KEY = bytes(range(32))          # 테스트 고정 키
KEY2 = bytes(range(1, 33))      # 다른 키


# ──────────────────────────────────────────────────────────────────────
# 1. 라운드트립 — 청크 경계 포함 다양한 크기
# ──────────────────────────────────────────────────────────────────────
def test_roundtrip_various_sizes():
    chunk = 64 * 1024
    for size in (0, 1, 15, 16, 17, 1024, chunk - 1, chunk, chunk + 1, chunk * 3 + 7):
        data = os.urandom(size)
        enc = encrypt_bytes(data, key=KEY)
        assert enc[:4] == MAGIC
        assert is_encrypted_data(enc)
        dec = decrypt_bytes(enc, key=KEY)
        assert dec == data, f"size={size} 라운드트립 불일치"


def test_nonce_uniqueness_and_ciphertext_differs():
    data = b"same plaintext"
    e1 = encrypt_bytes(data, key=KEY)
    e2 = encrypt_bytes(data, key=KEY)
    assert e1 != e2, "동일 평문 2회 암호화가 같은 암호문 — nonce 재사용 의심"
    assert decrypt_bytes(e1, key=KEY) == data
    assert decrypt_bytes(e2, key=KEY) == data


# ──────────────────────────────────────────────────────────────────────
# 2. 파일 라운드트립 (스트리밍) + 원자성
# ──────────────────────────────────────────────────────────────────────
def test_file_roundtrip_streaming():
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "plain.bin")
        enc = os.path.join(d, "enc.bin")
        dec = os.path.join(d, "dec.bin")
        payload = os.urandom(3 * 1024 * 1024 + 123)  # 다중 청크
        with open(src, "wb") as f:
            f.write(payload)

        encrypt_file(src, enc, key=KEY)
        assert is_encrypted_file(enc)
        assert not is_encrypted_file(src)

        decrypt_file(enc, dec, key=KEY)
        with open(dec, "rb") as f:
            assert f.read() == payload


def test_empty_file_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        src, enc, dec = (os.path.join(d, n) for n in ("s", "e", "d"))
        open(src, "wb").close()
        encrypt_file(src, enc, key=KEY)
        decrypt_file(enc, dec, key=KEY)
        assert os.path.getsize(dec) == 0


def test_decrypt_file_atomicity_on_tamper():
    """변조 파일 복호화 실패 시 dest 는 생성/덮어쓰기되지 않아야 한다."""
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "p.bin")
        enc = os.path.join(d, "e.bin")
        dst = os.path.join(d, "out.bin")
        with open(src, "wb") as f:
            f.write(os.urandom(200_000))
        encrypt_file(src, enc, key=KEY)

        # 본문 중간 1바이트 변조
        with open(enc, "r+b") as f:
            f.seek(100_000)
            b = f.read(1)
            f.seek(100_000)
            f.write(bytes([b[0] ^ 0xFF]))

        # 기존 dest 내용이 보존되는지도 확인
        with open(dst, "wb") as f:
            f.write(b"KEEP")
        try:
            decrypt_file(enc, dst, key=KEY)
            raise AssertionError("변조 파일 복호화가 성공해버림")
        except DecryptionError:
            pass
        with open(dst, "rb") as f:
            assert f.read() == b"KEEP", "실패한 복호화가 dest 를 오염시킴"
        # temp 파일 잔존 없어야 함
        leftovers = [n for n in os.listdir(d) if n.startswith(".xse_")]
        assert not leftovers, f"temp 잔존: {leftovers}"


def test_inplace_rejected():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "x.bin")
        with open(p, "wb") as f:
            f.write(b"data")
        for fn in (encrypt_file, decrypt_file):
            try:
                fn(p, p, key=KEY)
                raise AssertionError("in-place 가 허용됨")
            except StorageCryptoError:
                pass


# ──────────────────────────────────────────────────────────────────────
# 3. 무결성 / 키 오류
# ──────────────────────────────────────────────────────────────────────
def test_wrong_key_fails():
    enc = encrypt_bytes(b"secret", key=KEY)
    try:
        decrypt_bytes(enc, key=KEY2)
        raise AssertionError("잘못된 키로 복호화 성공")
    except DecryptionError:
        pass


def test_tamper_detection_everywhere():
    data = os.urandom(1000)
    enc = bytearray(encrypt_bytes(data, key=KEY))
    # 헤더(버전/alg_id/nonce), 본문, 태그 각각 변조.
    # alg_id 바이트(pos=5) 변조는 UnsupportedAlgorithmError 로 표면화될 수 있음 —
    # 둘 다 StorageCryptoError 계열이며 "감지됨" 이 요점.
    for pos in (4, 5, 8, len(enc) // 2, len(enc) - 1):
        tampered = bytearray(enc)
        tampered[pos] ^= 0x01
        try:
            decrypt_bytes(bytes(tampered), key=KEY)
            raise AssertionError(f"pos={pos} 변조 미감지")
        except (DecryptionError, UnsupportedAlgorithmError):
            pass


def test_truncation_detected():
    enc = encrypt_bytes(b"hello world", key=KEY)
    for cut in (3, 7, len(enc) - 5):  # 헤더 절단 / 태그 절단
        try:
            decrypt_bytes(enc[:cut], key=KEY)
            raise AssertionError(f"cut={cut} 절단 미감지")
        except DecryptionError:
            pass


def test_plaintext_rejected():
    try:
        decrypt_bytes(b"just plain text, no magic here....", key=KEY)
        raise AssertionError("비암호화 데이터 복호화가 성공")
    except DecryptionError:
        pass


# ──────────────────────────────────────────────────────────────────────
# 4. 키 관리
# ──────────────────────────────────────────────────────────────────────
def test_key_generate_decode_roundtrip():
    k = generate_key()
    kb = decode_key(k)
    assert len(kb) == 32
    # hex 형식도 허용
    assert decode_key(kb.hex()) == kb


def test_key_invalid_rejected():
    for bad in ("", "   ", "short", "z" * 64, "00" * 31):
        try:
            decode_key(bad)
            raise AssertionError(f"잘못된 키 통과: {bad!r}")
        except EncryptionKeyError:
            pass


def test_env_key_loading():
    prev = os.environ.get(DEFAULT_KEY_ENV)
    try:
        os.environ[DEFAULT_KEY_ENV] = generate_key()
        k = load_key_from_env()
        assert len(k) == 32
        # key=None 경로 (env 사용) 라운드트립
        enc = encrypt_bytes(b"env-key-data")
        assert decrypt_bytes(enc) == b"env-key-data"

        # 새 계약: load_key_from_env 는 미설정 시 None (예외 아님 — fallback 용)
        del os.environ[DEFAULT_KEY_ENV]
        assert load_key_from_env() is None
        # 하지만 실제 암복호화는 키 없으면 EncryptionKeyError (fail-loud)
        try:
            encrypt_bytes(b"x")
            raise AssertionError("키 없이 암호화 성공")
        except EncryptionKeyError:
            pass
    finally:
        if prev is not None:
            os.environ[DEFAULT_KEY_ENV] = prev
        else:
            os.environ.pop(DEFAULT_KEY_ENV, None)


def test_key_resolver_precedence():
    """키 resolver(app_config) 가 env 보다 우선, None/예외는 env fallback."""
    from xgen_sdk.storage.crypto import set_encryption_key_resolver

    KEY_A = bytes(range(32))
    KEY_B = bytes(range(32, 64))
    try:
        with _env(**{DEFAULT_KEY_ENV: None}):
            # 1) resolver bytes — env 없이도 동작
            set_encryption_key_resolver(lambda: KEY_A)
            enc = encrypt_bytes(b"resolver-key")
            assert decrypt_bytes(enc) == b"resolver-key"
            # 2) resolver base64 문자열도 허용
            import base64
            set_encryption_key_resolver(lambda: base64.b64encode(KEY_B).decode())
            enc2 = encrypt_bytes(b"str-key")
            # KEY_B 로 암호화됐으니 KEY_B 로만 복호화
            assert decrypt_bytes(enc2, key=KEY_B) == b"str-key"
        # 3) resolver None + env 설정 → env fallback
        with _env(**{DEFAULT_KEY_ENV: base64_b64(KEY_A)}):
            set_encryption_key_resolver(lambda: None)
            enc3 = encrypt_bytes(b"env-fallback")
            assert decrypt_bytes(enc3, key=KEY_A) == b"env-fallback"
            # 4) resolver 예외 → env fallback
            def _boom():
                raise RuntimeError("config down")
            set_encryption_key_resolver(_boom)
            enc4 = encrypt_bytes(b"exc-fallback")
            assert decrypt_bytes(enc4, key=KEY_A) == b"exc-fallback"
        # 5) resolver None + env 없음 → EncryptionKeyError
        with _env(**{DEFAULT_KEY_ENV: None}):
            set_encryption_key_resolver(lambda: None)
            try:
                encrypt_bytes(b"nope")
                raise AssertionError("키 전무인데 암호화 성공")
            except EncryptionKeyError:
                pass
        # 6) non-callable 거부
        try:
            set_encryption_key_resolver(123)
            raise AssertionError("non-callable 통과")
        except TypeError:
            pass
    finally:
        set_encryption_key_resolver(None)


def base64_b64(b: bytes) -> str:
    import base64
    return base64.b64encode(b).decode()


# ──────────────────────────────────────────────────────────────────────
# 5. 추상 클래스 / 레지스트리 확장성
# ──────────────────────────────────────────────────────────────────────
class _XorTestCipher(FileCipher):
    """확장성 검증용 장난감 cipher (보안성 없음 — 테스트 전용)."""

    algorithm_id = 250
    algorithm_name = "xor-test"

    def __init__(self, key: bytes):
        self._k = key[0] if key else 0x5A

    def _new_alg_header(self) -> bytes:
        return b"\x00"

    def _encrypt_body(self, reader, writer, alg_header, aad):
        while True:
            chunk = reader.read(65536)
            if not chunk:
                break
            writer.write(bytes(b ^ self._k for b in chunk))

    def _decrypt_body(self, reader, writer, alg_header, aad):
        while True:
            chunk = reader.read(65536)
            if not chunk:
                break
            writer.write(bytes(b ^ self._k for b in chunk))


def test_registry_extension_and_autodetect():
    register_cipher(_XorTestCipher)
    register_cipher(_XorTestCipher)  # idempotent 재등록 허용

    c = get_cipher("xor-test", key=KEY)
    enc = c.encrypt_bytes(b"extend me")
    # 모듈 함수가 알고리즘 자동 식별 (alg_id=250 → XorTestCipher)
    assert decrypt_bytes(enc, key=KEY) == b"extend me"


def test_registry_conflicts_rejected():
    class _Dup(FileCipher):
        algorithm_id = 1  # Aes256Gcm 과 충돌
        algorithm_name = "dup-test"

        def _new_alg_header(self):
            return b""

        def _encrypt_body(self, r, w, h, a):
            pass

        def _decrypt_body(self, r, w, h, a):
            pass

    try:
        register_cipher(_Dup)
        raise AssertionError("id 충돌 등록이 허용됨")
    except StorageCryptoError:
        pass


def test_unknown_algorithm_errors():
    try:
        get_cipher("no-such-alg", key=KEY)
        raise AssertionError("미등록 알고리즘 이름 통과")
    except UnsupportedAlgorithmError:
        pass

    # 미등록 alg_id 의 암호문 → UnsupportedAlgorithmError
    fake = MAGIC + bytes([1]) + bytes([99]) + (0).to_bytes(2, "big") + b"body"
    try:
        decrypt_bytes(fake, key=KEY)
        raise AssertionError("미등록 alg_id 복호화 통과")
    except UnsupportedAlgorithmError:
        pass


def test_instance_algorithm_mismatch():
    register_cipher(_XorTestCipher)  # 실행 순서 독립 (idempotent)
    xor_enc = get_cipher("xor-test", key=KEY).encrypt_bytes(b"abc")
    aes = Aes256GcmCipher(KEY)
    try:
        aes.decrypt_bytes(xor_enc)
        raise AssertionError("알고리즘 불일치 미감지")
    except DecryptionError:
        pass


# ──────────────────────────────────────────────────────────────────────
# 6. MinIO 결합 헬퍼 (가짜 스토리지 — upload/download 를 파일 복사로 대체)
# ──────────────────────────────────────────────────────────────────────
def _install_fake_minio_store(store_dir: str):
    """xgen_sdk.storage.minio_client 의 upload/download 를 파일 복사로 monkeypatch."""
    import xgen_sdk.storage.minio_client as mc

    orig_up, orig_down = mc.upload_file, mc.download_file

    def fake_upload(client, source_path, object_name, bucket_name=None, content_type=None):
        dst = os.path.join(store_dir, object_name.replace("/", "__"))
        shutil.copyfile(source_path, dst)

    def fake_download(client, object_name, destination_path, bucket_name=None):
        src = os.path.join(store_dir, object_name.replace("/", "__"))
        shutil.copyfile(src, destination_path)

    mc.upload_file, mc.download_file = fake_upload, fake_download
    return lambda: (setattr(mc, "upload_file", orig_up), setattr(mc, "download_file", orig_down))


def test_minio_glue_encrypt_upload_then_decrypt_download():
    with tempfile.TemporaryDirectory() as d:
        store = os.path.join(d, "store")
        os.makedirs(store)
        restore = _install_fake_minio_store(store)
        try:
            src = os.path.join(d, "doc.pdf")
            payload = os.urandom(300_000)
            with open(src, "wb") as f:
                f.write(payload)

            upload_file_encrypted(None, src, "docs/doc.pdf", bucket_name="b", key=KEY)

            # 저장소의 객체는 암호문이어야 한다 (평문 노출 금지)
            stored = os.path.join(store, "docs__doc.pdf")
            assert is_encrypted_file(stored)
            with open(stored, "rb") as f:
                assert payload not in f.read(), "저장 객체에 평문이 노출됨"

            out = os.path.join(d, "restored.pdf")
            decrypted = download_file_decrypted(None, "docs/doc.pdf", out, bucket_name="b", key=KEY)
            assert decrypted is True
            with open(out, "rb") as f:
                assert f.read() == payload

            # 원본은 무변경, 임시파일 잔존 없음
            with open(src, "rb") as f:
                assert f.read() == payload
        finally:
            restore()


def test_minio_glue_plaintext_policy():
    with tempfile.TemporaryDirectory() as d:
        store = os.path.join(d, "store")
        os.makedirs(store)
        restore = _install_fake_minio_store(store)
        try:
            # 암호화 없이 저장된 (레거시) 객체
            with open(os.path.join(store, "legacy.txt"), "wb") as f:
                f.write(b"legacy plain content")

            out = os.path.join(d, "out.txt")
            # 기본: 비암호화 객체는 거부
            try:
                download_file_decrypted(None, "legacy.txt", out, bucket_name="b", key=KEY)
                raise AssertionError("비암호화 객체가 기본 정책에서 통과")
            except DecryptionError:
                pass
            assert not os.path.exists(out)

            # allow_plaintext=True → 그대로 저장, 반환 False
            decrypted = download_file_decrypted(
                None, "legacy.txt", out, bucket_name="b", key=KEY, allow_plaintext=True,
            )
            assert decrypted is False
            with open(out, "rb") as f:
                assert f.read() == b"legacy plain content"
        finally:
            restore()


# ──────────────────────────────────────────────────────────────────────
# 7. SDK 공통 경로 투명 암복호화 (upload_file/download_file + env 토글)
# ──────────────────────────────────────────────────────────────────────
from xgen_sdk.storage.crypto import (  # noqa: E402
    DEFAULT_ENABLED_ENV,
    decrypt_file_inplace,
    decrypt_bytes_if_encrypted,
    encrypt_bytes_if_enabled,
    encryption_enabled,
    get_object_bytes_decrypted,
    put_bytes_encrypted,
    stream_object_decrypted,
)
from xgen_sdk.storage.minio_client import download_file as sdk_download_file  # noqa: E402
from xgen_sdk.storage.minio_client import upload_file as sdk_upload_file  # noqa: E402


class _FakeResponse:
    def __init__(self, data: bytes):
        self._d = data

    def read(self):
        return self._d

    def close(self):
        pass

    def release_conn(self):
        pass


class _FakeMinioClient:
    """fput/fget/put/get 을 로컬 디렉토리로 흉내내는 가짜 클라이언트."""

    def __init__(self, store_dir: str):
        self._dir = store_dir

    def _path(self, bucket: str, name: str) -> str:
        return os.path.join(self._dir, f"{bucket}__{name}".replace("/", "__"))

    def fput_object(self, bucket_name, object_name, file_path, content_type=None):
        shutil.copyfile(file_path, self._path(bucket_name, object_name))

    def fget_object(self, bucket_name, object_name, file_path):
        shutil.copyfile(self._path(bucket_name, object_name), file_path)

    def put_object(self, bucket_name, object_name, data, length, content_type=None):
        with open(self._path(bucket_name, object_name), "wb") as f:
            f.write(data.read())

    def get_object(self, bucket_name, object_name):
        with open(self._path(bucket_name, object_name), "rb") as f:
            return _FakeResponse(f.read())


class _env:
    """테스트용 env 임시 설정/복원."""

    def __init__(self, **kv):
        self._kv = kv
        self._prev = {}

    def __enter__(self):
        for k, v in self._kv.items():
            self._prev[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return self

    def __exit__(self, *a):
        for k, prev in self._prev.items():
            if prev is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = prev


_KEY_B64 = __import__("base64").b64encode(KEY).decode()


def test_transparent_toggle_off_is_passthrough():
    """토글 off(기본) — upload_file/download_file 은 기존과 100% 동일 (평문)."""
    with _env(**{DEFAULT_ENABLED_ENV: None}), tempfile.TemporaryDirectory() as d:
        assert not encryption_enabled()
        c = _FakeMinioClient(d)
        src = os.path.join(d, "s.bin")
        payload = os.urandom(50_000)
        with open(src, "wb") as f:
            f.write(payload)
        sdk_upload_file(c, src, "o.bin", bucket_name="b")
        stored = c._path("b", "o.bin")
        with open(stored, "rb") as f:
            assert f.read() == payload, "토글 off 인데 객체가 변형됨"
        out = os.path.join(d, "out.bin")
        sdk_download_file(c, "o.bin", out, bucket_name="b")
        with open(out, "rb") as f:
            assert f.read() == payload


def test_transparent_toggle_on_roundtrip():
    """토글 on — 업로드는 암호문 저장, 다운로드는 자동 복호화."""
    with _env(**{DEFAULT_ENABLED_ENV: "true", DEFAULT_KEY_ENV: _KEY_B64}), \
            tempfile.TemporaryDirectory() as d:
        assert encryption_enabled()
        c = _FakeMinioClient(d)
        src = os.path.join(d, "s.bin")
        payload = os.urandom(200_000)
        with open(src, "wb") as f:
            f.write(payload)
        sdk_upload_file(c, src, "o.bin", bucket_name="b")

        stored = c._path("b", "o.bin")
        assert is_encrypted_file(stored), "토글 on 인데 객체가 평문"
        with open(stored, "rb") as f:
            assert payload not in f.read(), "저장 객체에 평문 노출"

        out = os.path.join(d, "out.bin")
        sdk_download_file(c, "o.bin", out, bucket_name="b")
        with open(out, "rb") as f:
            assert f.read() == payload, "자동 복호화 실패"

        # decrypt=False → 암호문 원문
        raw = os.path.join(d, "raw.bin")
        sdk_download_file(c, "o.bin", raw, bucket_name="b", decrypt=False)
        assert is_encrypted_file(raw)


def test_transparent_mixed_plaintext_read():
    """토글 on 이어도 평문(레거시) 객체 다운로드는 그대로 통과 — 혼재 안전."""
    with _env(**{DEFAULT_ENABLED_ENV: "true", DEFAULT_KEY_ENV: _KEY_B64}), \
            tempfile.TemporaryDirectory() as d:
        c = _FakeMinioClient(d)
        with open(c._path("b", "legacy.txt"), "wb") as f:
            f.write(b"legacy plaintext")
        out = os.path.join(d, "out.txt")
        sdk_download_file(c, "legacy.txt", out, bucket_name="b")
        with open(out, "rb") as f:
            assert f.read() == b"legacy plaintext"


def test_transparent_explicit_encrypt_overrides_toggle():
    """encrypt=True 명시 — 토글 off 여도 암호화."""
    with _env(**{DEFAULT_ENABLED_ENV: None, DEFAULT_KEY_ENV: _KEY_B64}), \
            tempfile.TemporaryDirectory() as d:
        c = _FakeMinioClient(d)
        src = os.path.join(d, "s.bin")
        with open(src, "wb") as f:
            f.write(b"force encrypt")
        sdk_upload_file(c, src, "o.bin", bucket_name="b", encrypt=True)
        assert is_encrypted_file(c._path("b", "o.bin"))
        # encrypt=False 명시 — 토글 on 이어도 평문
        with _env(**{DEFAULT_ENABLED_ENV: "1"}):
            sdk_upload_file(c, src, "p.bin", bucket_name="b", encrypt=False)
            assert not is_encrypted_file(c._path("b", "p.bin"))


def test_toggle_on_without_key_fails_loud():
    """토글 on + 키 미설정 → 평문이 조용히 올라가지 않고 EncryptionKeyError."""
    with _env(**{DEFAULT_ENABLED_ENV: "true", DEFAULT_KEY_ENV: None}), \
            tempfile.TemporaryDirectory() as d:
        c = _FakeMinioClient(d)
        src = os.path.join(d, "s.bin")
        with open(src, "wb") as f:
            f.write(b"x")
        try:
            sdk_upload_file(c, src, "o.bin", bucket_name="b")
            raise AssertionError("키 없이 암호화 업로드가 통과")
        except EncryptionKeyError:
            pass
        assert not os.path.exists(c._path("b", "o.bin")), "실패했는데 객체가 생김"


def test_enabled_resolver_precedence():
    """app_config resolver 가 env 보다 우선, None/예외는 env fallback."""
    from xgen_sdk.storage.crypto import set_encryption_enabled_resolver

    try:
        # 1) resolver True — env off 여도 on
        with _env(**{DEFAULT_ENABLED_ENV: None}):
            set_encryption_enabled_resolver(lambda: True)
            assert encryption_enabled() is True
            # 2) resolver False — env on 이어도 off (설정이 명시적으로 껐다)
            with _env(**{DEFAULT_ENABLED_ENV: "true"}):
                set_encryption_enabled_resolver(lambda: False)
                assert encryption_enabled() is False
                # 3) resolver None — env fallback (on)
                set_encryption_enabled_resolver(lambda: None)
                assert encryption_enabled() is True
                # 4) resolver 예외 — env fallback (on)
                def _boom():
                    raise RuntimeError("config down")
                set_encryption_enabled_resolver(_boom)
                assert encryption_enabled() is True
            # 5) 문자열 값 강제변환 ("false" → off, "1" → on, "" → env fallback)
            set_encryption_enabled_resolver(lambda: "false")
            assert encryption_enabled() is False
            set_encryption_enabled_resolver(lambda: "1")
            assert encryption_enabled() is True
            set_encryption_enabled_resolver(lambda: "")
            assert encryption_enabled() is False  # env 미설정 → 기본 off
        # 6) 등록 해제 → env 만
        set_encryption_enabled_resolver(None)
        with _env(**{DEFAULT_ENABLED_ENV: "yes"}):
            assert encryption_enabled() is True
        # 7) non-callable 거부
        try:
            set_encryption_enabled_resolver("notcallable")
            raise AssertionError("non-callable resolver 통과")
        except TypeError:
            pass
    finally:
        set_encryption_enabled_resolver(None)


def test_resolver_drives_transparent_upload():
    """resolver on + env off 상태에서 SDK upload_file 이 암호화하는지."""
    from xgen_sdk.storage.crypto import set_encryption_enabled_resolver

    try:
        with _env(**{DEFAULT_ENABLED_ENV: None, DEFAULT_KEY_ENV: _KEY_B64}), \
                tempfile.TemporaryDirectory() as d:
            set_encryption_enabled_resolver(lambda: True)
            c = _FakeMinioClient(d)
            src = os.path.join(d, "s.bin")
            with open(src, "wb") as f:
                f.write(b"resolver-driven")
            sdk_upload_file(c, src, "o.bin", bucket_name="b")
            assert is_encrypted_file(c._path("b", "o.bin")), "resolver on 인데 평문 업로드"
            out = os.path.join(d, "out.bin")
            sdk_download_file(c, "o.bin", out, bucket_name="b")
            with open(out, "rb") as f:
                assert f.read() == b"resolver-driven"
    finally:
        set_encryption_enabled_resolver(None)


def test_stream_object_decrypted_explicit_key():
    """stream_object_decrypted 가 명시 key 인자를 실제로 사용하는지 (env 키 없이)."""
    with _env(**{DEFAULT_ENABLED_ENV: None, DEFAULT_KEY_ENV: None}), \
            tempfile.TemporaryDirectory() as d:
        c = _FakeMinioClient(d)
        payload = os.urandom(10_000)
        # 명시 key 로 암호화해 저장
        src = os.path.join(d, "p.bin")
        enc = os.path.join(d, "e.bin")
        with open(src, "wb") as f:
            f.write(payload)
        encrypt_file(src, enc, key=KEY)
        shutil.copyfile(enc, c._path("b", "o.bin"))
        # env 키가 없어도 명시 key 로 복호화 스트림 성공해야 함
        got = b"".join(stream_object_decrypted(c, "o.bin", bucket_name="b", key=KEY))
        assert got == payload


def test_primitives_bytes_and_stream():
    """직접 구현부용 프리미티브: put/get bytes + 스트림 복호화 + inplace."""
    with _env(**{DEFAULT_ENABLED_ENV: "true", DEFAULT_KEY_ENV: _KEY_B64}), \
            tempfile.TemporaryDirectory() as d:
        c = _FakeMinioClient(d)
        payload = os.urandom(150_000)

        # put_bytes_encrypted (토글 on → 암호문 저장) / get_object_bytes_decrypted
        put_bytes_encrypted(c, payload, "img.png", bucket_name="b", content_type="image/png")
        assert is_encrypted_file(c._path("b", "img.png"))
        assert get_object_bytes_decrypted(c, "img.png", bucket_name="b") == payload

        # 평문 객체도 get_object_bytes_decrypted 는 그대로 통과
        with open(c._path("b", "plain.bin"), "wb") as f:
            f.write(b"plain")
        assert get_object_bytes_decrypted(c, "plain.bin", bucket_name="b") == b"plain"

        # stream_object_decrypted — 암호문/평문 모두 평문 청크
        got = b"".join(stream_object_decrypted(c, "img.png", bucket_name="b"))
        assert got == payload
        got2 = b"".join(stream_object_decrypted(c, "plain.bin", bucket_name="b"))
        assert got2 == b"plain"

        # encrypt_bytes_if_enabled / decrypt_bytes_if_encrypted 대칭
        enc = encrypt_bytes_if_enabled(b"abc")
        assert is_encrypted_data(enc)
        assert decrypt_bytes_if_encrypted(enc) == b"abc"
        assert decrypt_bytes_if_encrypted(b"notmagic") == b"notmagic"

        # decrypt_file_inplace — 평문 no-op / 암호문 복호화
        p = os.path.join(d, "f.bin")
        with open(p, "wb") as f:
            f.write(b"plainfile")
        assert decrypt_file_inplace(p) is False
        encrypt_file_path = os.path.join(d, "f.enc")
        encrypt_file(p, encrypt_file_path, key=KEY)
        assert decrypt_file_inplace(encrypt_file_path, key=KEY) is True
        with open(encrypt_file_path, "rb") as f:
            assert f.read() == b"plainfile"
        leftovers = [n for n in os.listdir(d) if ".xse_src_tmp" in n]
        assert not leftovers, f"inplace temp 잔존: {leftovers}"


# ──────────────────────────────────────────────────────────────────────
# 8. 감사(audit) 훅 — upload/download 이벤트 기록
# ──────────────────────────────────────────────────────────────────────
from xgen_sdk.storage.audit import (  # noqa: E402
    set_storage_audit_logger,
    storage_audit_context,
)


def test_audit_upload_download_events():
    """upload_file/download_file 이 성공 시 audit event 를 방출하는지."""
    events = []
    set_storage_audit_logger(lambda e: events.append(e))
    try:
        with _env(**{DEFAULT_ENABLED_ENV: None}), tempfile.TemporaryDirectory() as d:
            c = _FakeMinioClient(d)
            src = os.path.join(d, "s.bin")
            payload = os.urandom(1234)
            with open(src, "wb") as f:
                f.write(payload)

            # 평문 업로드 → upload event (encrypted False)
            with storage_audit_context(user_id=7, session_id="sess-1", source="test", service="unit"):
                sdk_upload_file(c, src, "o.bin", bucket_name="b")
            up = [e for e in events if e["operation"] == "upload"][-1]
            assert up["encrypted"] is False
            assert up["encryption_algorithm"] is None
            assert up["object_path"] == "b/o.bin"
            assert up["size_bytes"] == 1234
            assert up["status"] == "success"
            # 컨텍스트 병합 확인
            assert up["user_id"] == 7 and up["session_id"] == "sess-1"
            assert up["source"] == "test" and up["service"] == "unit"

            # 다운로드 → download event
            out = os.path.join(d, "out.bin")
            sdk_download_file(c, "o.bin", out, bucket_name="b")
            dn = [e for e in events if e["operation"] == "download"][-1]
            assert dn["encrypted"] is False
            assert dn["size_bytes"] == 1234
            # 컨텍스트 밖이므로 user_id 없음
            assert dn.get("user_id") is None
    finally:
        set_storage_audit_logger(None)


def test_audit_encrypted_events_report_algorithm():
    """암호화 업로드 event 는 encrypted=True + algorithm 을 보고."""
    events = []
    set_storage_audit_logger(lambda e: events.append(e))
    try:
        with _env(**{DEFAULT_ENABLED_ENV: "true", DEFAULT_KEY_ENV: _KEY_B64}), \
                tempfile.TemporaryDirectory() as d:
            c = _FakeMinioClient(d)
            src = os.path.join(d, "s.bin")
            payload = os.urandom(5000)
            with open(src, "wb") as f:
                f.write(payload)
            sdk_upload_file(c, src, "o.bin", bucket_name="b")
            up = [e for e in events if e["operation"] == "upload"][-1]
            assert up["encrypted"] is True
            assert up["encryption_algorithm"] == "aes256-gcm"
            assert up["plaintext_size_bytes"] == 5000
            assert up["size_bytes"] == 5000 + 36  # 엔벨로프 오버헤드
            # 다운로드 → 복호화 event
            out = os.path.join(d, "out.bin")
            sdk_download_file(c, "o.bin", out, bucket_name="b")
            dn = [e for e in events if e["operation"] == "download"][-1]
            assert dn["encrypted"] is True
            assert dn["encryption_algorithm"] == "aes256-gcm"
            assert dn["size_bytes"] == 5000  # 복호화된 평문 크기
    finally:
        set_storage_audit_logger(None)


def test_audit_bytes_primitives_events():
    """put_bytes_encrypted / get_object_bytes_decrypted 도 event 방출."""
    events = []
    set_storage_audit_logger(lambda e: events.append(e))
    try:
        with _env(**{DEFAULT_ENABLED_ENV: "true", DEFAULT_KEY_ENV: _KEY_B64}), \
                tempfile.TemporaryDirectory() as d:
            c = _FakeMinioClient(d)
            put_bytes_encrypted(c, b"hello-audit", "k.bin", bucket_name="b", content_type="text/plain")
            up = [e for e in events if e["operation"] == "upload"][-1]
            assert up["encrypted"] is True and up["encryption_algorithm"] == "aes256-gcm"
            got = get_object_bytes_decrypted(c, "k.bin", bucket_name="b")
            assert got == b"hello-audit"
            dn = [e for e in events if e["operation"] == "download"][-1]
            assert dn["encrypted"] is True and dn["size_bytes"] == len(b"hello-audit")
    finally:
        set_storage_audit_logger(None)


def test_audit_logger_error_never_breaks_operation():
    """로거가 예외를 던져도 storage 작업은 정상 완료."""
    def _boom(_e):
        raise RuntimeError("logger down")
    set_storage_audit_logger(_boom)
    try:
        with _env(**{DEFAULT_ENABLED_ENV: None}), tempfile.TemporaryDirectory() as d:
            c = _FakeMinioClient(d)
            src = os.path.join(d, "s.bin")
            with open(src, "wb") as f:
                f.write(b"still works")
            sdk_upload_file(c, src, "o.bin", bucket_name="b")  # 예외 나면 실패
            out = os.path.join(d, "out.bin")
            sdk_download_file(c, "o.bin", out, bucket_name="b")
            with open(out, "rb") as f:
                assert f.read() == b"still works"
    finally:
        set_storage_audit_logger(None)


def test_audit_no_logger_is_noop():
    """로거 미등록 시 완전 no-op (예외/부작용 없음)."""
    set_storage_audit_logger(None)
    with _env(**{DEFAULT_ENABLED_ENV: None}), tempfile.TemporaryDirectory() as d:
        c = _FakeMinioClient(d)
        src = os.path.join(d, "s.bin")
        with open(src, "wb") as f:
            f.write(b"x")
        sdk_upload_file(c, src, "o.bin", bucket_name="b")  # 아무 일도 없어야


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
