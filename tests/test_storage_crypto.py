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

        del os.environ[DEFAULT_KEY_ENV]
        try:
            load_key_from_env()
            raise AssertionError("env 미설정인데 키 로드 성공")
        except EncryptionKeyError:
            pass
    finally:
        if prev is not None:
            os.environ[DEFAULT_KEY_ENV] = prev
        else:
            os.environ.pop(DEFAULT_KEY_ENV, None)


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
