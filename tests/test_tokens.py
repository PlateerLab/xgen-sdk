"""토큰 추정 — 모델도 데이터 파일도 네트워크도 쓰지 않는다.

이 파일이 지키는 것은 **성질**이지 정확한 숫자가 아니다. 계수는 실제 tiktoken 과 대조해
맞췄지만(한국어·영어·혼합·코드·JSON·로그 20종, 평균 절대 오차 9%), 그 대조는 인터넷이
있는 곳에서 한 번 한 것이고 테스트가 매번 할 일이 아니다 — 폐쇄망에서 돌아야 하는 코드의
테스트가 인터넷을 요구하면 앞뒤가 맞지 않는다.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xgen_sdk.tokens import HANGUL_LEGACY, HANGUL_MODERN, estimate_tokens  # noqa: E402


class TestItNeverReachesOut:
    def test_the_module_imports_nothing_that_downloads(self):
        """tiktoken 을 되살리면 여기서 걸린다 — 그 라이브러리는 BPE 표를 인터넷에서 받는다."""
        source = (Path(__file__).resolve().parents[1] / "src/xgen_sdk/tokens.py").read_text("utf-8")
        for banned in ("tiktoken", "requests", "httpx", "urllib"):
            assert f"import {banned}" not in source, banned


class TestEmptyAndOdd:
    def test_nothing_is_zero(self):
        assert estimate_tokens("") == 0
        assert estimate_tokens(None) == 0

    def test_non_strings_are_counted_as_their_text(self):
        assert estimate_tokens(12345) == estimate_tokens("12345")
        assert estimate_tokens(["a"]) > 0

    def test_any_text_costs_at_least_one(self):
        assert estimate_tokens(" ") >= 1
        assert estimate_tokens("a") == 1


class TestItReflectsHowTokenizersWork:
    def test_korean_costs_more_per_character_than_english(self):
        """한글은 음절마다 쪼개지고 영어는 여러 글자가 뭉친다 — 같은 길이면 한글이 비싸다."""
        korean = "안녕하세요반갑습니다오늘도좋은하루"       # 17자
        english = "greetings friends have a nice day"      # 33자
        assert estimate_tokens(korean) > estimate_tokens(english)

    def test_the_legacy_tokenizer_charges_more_for_korean(self):
        korean = "회의는 오후 세시에 시작합니다"
        assert estimate_tokens(korean, hangul=HANGUL_LEGACY) > estimate_tokens(
            korean, hangul=HANGUL_MODERN)
        # 영어는 인코딩이 달라도 같다 — 차이는 한글 가중치 하나뿐이다.
        english = "the meeting starts at three in the afternoon"
        assert estimate_tokens(english, hangul=HANGUL_LEGACY) == estimate_tokens(
            english, hangul=HANGUL_MODERN)

    def test_digits_are_grouped(self):
        """BPE 는 숫자를 두세 자리씩 묶는다 — 글자마다 한 토큰이 아니다."""
        assert estimate_tokens("123456789") < 9

    def test_repeated_spaces_are_nearly_free(self):
        """첫 공백은 뒤 단어에 붙는다. 들여쓰기가 본문만큼 비싸면 코드 예산이 망가진다."""
        assert estimate_tokens("word") == estimate_tokens(" word")

    def test_it_grows_with_length(self):
        short = "배포는 승인된 정의를 연다"
        assert estimate_tokens(short * 4) > estimate_tokens(short * 2) > estimate_tokens(short)


class TestItIsInTheRightBallpark:
    """실제 tiktoken 대조에서 나온 범위 — 크게 벗어나면 계수가 망가진 것이다."""

    def test_korean_prose(self):
        text = "안녕하세요. 오늘 회의는 오후 3시에 시작합니다. 참석자는 총 12명이며, 안건은 두 가지입니다."
        # 실측(o200k) 29 토큰 / 추정 31
        assert 22 <= estimate_tokens(text) <= 40

    def test_english_prose(self):
        text = ("The deployment pipeline publishes an immutable snapshot of the approved "
                "definition so that editing the source never disturbs external users.")
        # 실측(o200k) 22 토큰 / 추정 23
        assert 16 <= estimate_tokens(text) <= 30

    def test_it_beats_the_old_length_over_four_rule(self):
        """옛 fallback 은 한국어에서 평균 31% 과소였다 — 그걸 대신하는 것이 이 함수의 목적이다."""
        korean = "형태소 분석기는 어절을 형태소 단위로 나누고, 각 형태소에 품사 태그를 붙인다."
        assert estimate_tokens(korean) > len(korean) // 4
