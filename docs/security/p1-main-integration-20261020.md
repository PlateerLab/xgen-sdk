# SDK 최신 main · P1 통합 검증 — 2026-10-20

## 비교 기준과 통합 범위

- 최신 main: `7c892abca29813dd1d3e605443ff7a47fe586d48` (PR #75, 2.17.2).
- 검증한 P1: `308a7f3cb0b853143b0e451e11c98a16e9ec8f93` (PR #76).
- #76의 대상은 이미 #74를 main에 병합한 뒤 남아 있던 P0 브랜치였다. 따라서 #76의 P0 통합만으로는 main에 P1이 들어가지 않았다.
- 새 브랜치 `fix/p1-main-integration-20261020`을 최신 원격 main에서 만들고 원래 P1 커밋을 merge 방식으로 통합했다. 과거 커밋과 소비자가 참조하는 SHA를 재작성하지 않았다.
- 충돌은 `pyproject.toml`과 `src/xgen_sdk/__init__.py`의 버전 표기 두 곳이다. 두 값을 2.18.0으로 맞췄다.
- main의 Redis 설정·일시 장애·재접속 처리와 회귀 테스트는 그대로 유지했다. Redis가 설정돼 있는데 기동 순간 연결되지 않는 경우 메모리 설정에 고정되는 문제를 다시 만들지 않는다.
- P1의 엄격한 정책 오류 계약, 단일 snapshot 권한 조회 및 안전한 오류 처리를 그대로 보존했다. 다른 서비스의 P0 브랜치와 MR은 변경하지 않는다.

## 검증

| 검증 | 결과 |
| --- | --- |
| main 대비 `config/local_config.py`, `test_config_resilience.py` | byte 동일, diff 0 |
| 원래 P1 대비 `auth`, `control_policy`, `test_p1_security_contract.py` | byte 동일, diff 0 |
| 로컬 P0/P1 보안 + Redis 복원력 회귀 | 90 PASS |
| 오프라인 wheel 빌드 | PASS, 2.18.0 |
| Linux의 실제 설치 wheel, source overlay 없음 | 139 PASS / 기존 선택 의존성 4 SKIP |
| 설치 metadata·패키지 `__version__` | 모두 2.18.0; site-packages import 확인 |
| 충돌 표식·diff 공백 검사 | PASS |

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/test_p1_security_contract.py tests/test_principal_auth.py \
  tests/test_require_login.py tests/test_document_capability.py \
  tests/test_deployment_policy.py tests/test_config_resilience.py
uv build --wheel --offline --out-dir <private-wheel-dir>
# Linux xgen-security-python-tests:local, --network none.
# /tests와 wheel만 read-only mount; PYTHONPATH 비움, SDK 소스는 mount하지 않음.
python -m pip install --no-deps --no-index --force-reinstall <wheel>
python -m pytest -q -p no:cacheprovider -p pytest_asyncio.plugin \
  /tests/test_p1_security_contract.py /tests/test_principal_auth.py \
  /tests/test_require_login.py /tests/test_document_capability.py \
  /tests/test_deployment_policy.py /tests/test_control_policy.py \
  /tests/test_config_resilience.py
```

검증 wheel SHA256: `9fd308f76ae6dc4d8dfd7baf47542bd86a42bed8270947864e0a1b59dc9e9473`.
main 2.17.2의 설정 복구 코드가 포함되어 이전 P1 후보 wheel과 digest가 다르다. `.venv`에 build/pip 실행 모듈이 없어 초기 빌드 명령은 실행되지 않았으며, 기존 uv 캐시를 이용한 오프라인 빌드로 검증했다. 제품 코드나 의존성 선언은 이를 위해 변경하지 않았다.

원격 workflow는 release published/workflow_dispatch에서만 패키지를 발행한다. PR/main push용 CI가 정의돼 있지 않으므로 자동 CI 통과를 주장하지 않는다. 위 실제 검증 결과를 PR에 기록한다. release/tag 생성과 PyPI 발행은 이번 요청 범위에 포함하지 않는다. 최종 Head와 main 병합 SHA는 새 main 대상 PR의 기록을 따른다.
