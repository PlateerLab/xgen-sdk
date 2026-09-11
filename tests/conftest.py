"""결재 레지스트리는 **모듈 전역**이다 — 테스트끼리 새지 않게 되돌린다.

등록은 프로세스 수명 동안 유지되는 것이 정상이다(부팅 때 한 번 꽂고 계속
쓴다). 그래서 테스트가 즉석에서 등록한 행위나, "이 행위에 훅을 꽂으면" 을
확인한 흔적이 다음 테스트까지 따라간다 — 그러면 **혼자 돌릴 때와 같이 돌릴
때 결과가 달라지는** 검사가 생긴다. 그건 신호가 아니라 잡음이다.
"""
import copy

import pytest

from xgen_sdk.approval import registry


@pytest.fixture(autouse=True)
def _isolate_action_registry():
    saved = copy.copy(registry._ACTIONS)
    try:
        yield
    finally:
        registry._ACTIONS.clear()
        registry._ACTIONS.update(saved)
