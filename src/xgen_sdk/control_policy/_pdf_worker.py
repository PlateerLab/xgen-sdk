"""PDF 가리기 작업 프로세스(``files._PdfWorker`` 가 부모와 같은 import 경로로 띄운다).

pdfium 은 스레드에 안전하지 않아, 서비스 프로세스 안에서 다른 PDF 일(문서 색인 · 미리보기)과 겹치면 프로세스가
죽는다. 그래서 PDF 가리기는 이 프로세스에서 돈다. multiprocessing 을 쓰지 않는 이유: spawn 은 부모의 시작 모듈
(서비스 main.py 전체)을 자식에서 다시 import 하고, fork 는 부모의 스레드 · 잠금 상태를 물려받는다. 이 프로세스는
이 SDK 모듈만 불러온다.

주고받기: 표준 입력으로 ``(8바이트 길이)(pickle 된 (함수 이름, 인자))``, 표준 출력으로 같은 꼴의 결과.
부모가 사라지면(표준 입력이 닫히면) 끝난다.
"""
from __future__ import annotations

import os
import pickle
import struct
import sys

#: 부르는 쪽이 고를 수 있는 일(이 모듈 밖의 함수는 부르지 않는다).
_JOBS = ("_mask_pdf_job", "_pdf_text_job")


def _read_exact(stream, size: int) -> bytes:
    data = stream.read(size)
    if data is None or len(data) < size:
        raise EOFError
    return data


def main() -> None:
    from xgen_sdk.control_policy import files

    stdin = sys.stdin.buffer
    # 결과는 원래 표준 출력으로만 보낸다. 라이브러리가 print 해도 결과에 섞이지 않게 fd 1 은 표준 오류로 돌린다.
    out = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)
    while True:
        try:
            size = struct.unpack(">Q", _read_exact(stdin, 8))[0]
            name, args = pickle.loads(_read_exact(stdin, size))
        except EOFError:
            return
        try:
            if name not in _JOBS:
                raise ValueError(f"unknown job: {name}")
            result = ("ok", getattr(files, name)(*args))
        except files.MaskingUnsupportedError as exc:
            result = ("unsupported", (exc.extension, exc.reason))
        except BaseException as exc:  # noqa: BLE001, 부모에게 알리고 다음 일을 받는다
            result = ("error", repr(exc))
        payload = pickle.dumps(result)
        out.write(struct.pack(">Q", len(payload)) + payload)
        out.flush()


if __name__ == "__main__":
    main()
