"""ToolExecutor 구현체들"""

import asyncio
import logging
from ..interfaces import ToolExecutor, ToolRouter, ToolResult

logger = logging.getLogger("harness.strategy.tool_executor")


class SequentialToolExecutor(ToolExecutor):
    """순차 도구 실행 — 기본 전략.

    모든 도구를 순서대로 실행. 하나가 실패해도 나머지 계속 진행.
    """

    def __init__(self, timeout: float = 60.0, result_budget: int = 50_000):
        self._timeout = timeout
        self._budget = result_budget

    @property
    def name(self) -> str:
        return "sequential"

    @property
    def description(self) -> str:
        return "순차 실행 (에러 허용, 예산 관리)"

    def configure(self, config: dict) -> None:
        self._timeout = config.get("timeout", self._timeout)
        self._budget = config.get("result_budget", self._budget)

    async def execute_all(
        self,
        tool_calls: list[dict],
        router: ToolRouter,
    ) -> list[tuple[str, ToolResult]]:
        results: list[tuple[str, ToolResult]] = []
        total_chars = 0

        for tc in tool_calls:
            tool_use_id = tc.get("tool_use_id", "")
            tool_name = tc.get("tool_name", "")
            tool_input = tc.get("tool_input", {})

            try:
                result = await asyncio.wait_for(
                    router.route(tool_name, tool_input),
                    timeout=self._timeout,
                )

                # 예산 관리
                if total_chars + len(result.content) > self._budget:
                    remaining = max(0, self._budget - total_chars)
                    result = ToolResult(
                        content=result.content[:remaining] + f"\n... (축약됨, 원본 {len(result.content)}자)",
                        is_error=result.is_error,
                        metadata=result.metadata,
                    )

                total_chars += len(result.content)
                results.append((tool_use_id, result))

            except asyncio.TimeoutError:
                results.append((tool_use_id, ToolResult(
                    content=f"Tool '{tool_name}' timed out after {self._timeout}s",
                    is_error=True,
                )))
            except Exception as e:
                results.append((tool_use_id, ToolResult(
                    content=f"Tool '{tool_name}' failed: {e}",
                    is_error=True,
                )))

        return results


class ParallelToolExecutor(ToolExecutor):
    """병렬 도구 실행 — 읽기 도구만 병렬, 쓰기 도구는 순차.

    read_only 판별: 도구 메타데이터의 is_read_only 속성 또는
    도구 이름에 create/update/delete/write/send/post/put/remove 미포함.
    """

    def __init__(self, timeout: float = 60.0, result_budget: int = 50_000):
        self._timeout = timeout
        self._budget = result_budget

    @property
    def name(self) -> str:
        return "parallel"

    @property
    def description(self) -> str:
        return "읽기 도구 병렬, 쓰기 도구 순차"

    async def execute_all(
        self,
        tool_calls: list[dict],
        router: ToolRouter,
    ) -> list[tuple[str, ToolResult]]:
        # 읽기/쓰기 분류
        write_keywords = {"create", "update", "delete", "write", "send", "post", "put", "remove"}
        read_calls = []
        write_calls = []
        for tc in tool_calls:
            name_lower = tc.get("tool_name", "").lower()
            if any(kw in name_lower for kw in write_keywords):
                write_calls.append(tc)
            else:
                read_calls.append(tc)

        results: list[tuple[str, ToolResult]] = []

        # 읽기 도구 병렬 실행
        if read_calls:
            tasks = [
                self._execute_one(tc, router)
                for tc in read_calls
            ]
            read_results = await asyncio.gather(*tasks, return_exceptions=False)
            results.extend(read_results)

        # 쓰기 도구 순차 실행
        for tc in write_calls:
            r = await self._execute_one(tc, router)
            results.append(r)

        return results

    async def _execute_one(self, tc: dict, router: ToolRouter) -> tuple[str, ToolResult]:
        tool_use_id = tc.get("tool_use_id", "")
        tool_name = tc.get("tool_name", "")
        tool_input = tc.get("tool_input", {})
        try:
            result = await asyncio.wait_for(
                router.route(tool_name, tool_input),
                timeout=self._timeout,
            )
            return (tool_use_id, result)
        except asyncio.TimeoutError:
            return (tool_use_id, ToolResult(content=f"Tool '{tool_name}' timed out", is_error=True))
        except Exception as e:
            return (tool_use_id, ToolResult(content=f"Tool '{tool_name}' failed: {e}", is_error=True))
