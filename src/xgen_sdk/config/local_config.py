"""
Local Config Manager

Redis 연결 실패 시 사용하는 메모리 기반 설정 관리 시스템
RedisConfigManager와 동일한 인터페이스를 제공하여 완벽하게 대체 가능

분산 환경 지원 (2026-02-04):
- 파일 기반 저장 제거 (멀티 Pod에서 파일 불일치 문제 방지)
- 메모리 + DB 조합으로 동작 (앱 재시작 시 DB에서 복원)
- 단독 앱 배포 시에도 정상 동작 (메모리 전용 모드)
"""
import logging
from typing import Dict, Any, Optional, List

logger = logging.getLogger(__name__)


class LocalConfigManager:
    """
    로컬 메모리 기반 설정 관리자
    Redis 연결 실패 시 fallback으로 사용됨
    RedisConfigManager와 동일한 인터페이스 제공
    """

    def __init__(self, db_manager=None, persist_path: Optional[str] = None):
        """
        LocalConfigManager 초기화

        Args:
            db_manager: DB 매니저 인스턴스 (선택적)
            persist_path: 더 이상 사용되지 않음 (하위 호환성을 위해 유지)
        """
        # 메모리 내 설정 저장소
        self._configs: Dict[str, Dict[str, Any]] = {}

        # 카테고리별 인덱스
        self._category_index: Dict[str, set] = {}

        # Config 키 Prefix (Redis 호환)
        self.config_prefix = "config"

        # DB Manager (선택적)
        self.db_manager = db_manager

        # 연결 상태 (항상 True - 로컬이므로)
        self._connection_available = True

        # DB에서 설정 로드 (분산 환경 지원 - 파일 대신 DB 사용)
        self._load_from_db()

        logger.info(f"✅ Local Config Manager 초기화 완료 (메모리 기반, fallback 모드)")
        if self.db_manager:
            logger.info(f"   DB 연동 활성화 - 설정 변경 시 DB에 저장됨")
        else:
            logger.info(f"   메모리 전용 모드 - 앱 재시작 시 설정 유실됨")

    def _load_from_db(self):
        """
        DB에서 설정 로드 (분산 환경 지원)

        DB Manager가 있고, configs 테이블에서 설정을 조회할 수 있는 경우
        저장된 설정을 메모리로 복원합니다.
        """
        if not self.db_manager:
            logger.debug("   DB Manager가 없어 설정 로드 스킵")
            return

        try:
            # AppDatabaseManager의 get_all_configs 메서드 사용
            if hasattr(self.db_manager, 'get_all_configs'):
                configs = self.db_manager.get_all_configs()
                if configs:
                    for config in configs:
                        env_name = config.get('env_name', '')
                        if not env_name:
                            continue

                        config_data = {
                            'value': config.get('config_value'),
                            'type': config.get('data_type', 'string'),
                            'category': config.get('category', config.get('config_path', '').split('.')[0]),
                            'path': config.get('config_path', env_name),
                            'env_name': env_name
                        }

                        self._configs[env_name] = config_data

                        # 카테고리 인덱스 업데이트
                        category = config_data['category']
                        if category:
                            if category not in self._category_index:
                                self._category_index[category] = set()
                            self._category_index[category].add(env_name)

                    logger.info(f"   DB에서 설정 {len(self._configs)}개 로드됨")
            else:
                logger.debug("   DB Manager에 get_all_configs 메서드 없음")

        except Exception as e:
            logger.warning(f"   DB에서 설정 로드 실패: {e}")
            self._configs = {}
            self._category_index = {}

    # ========== Config 값 CRUD ==========

    def health_check(self) -> bool:
        """연결 상태 확인 (로컬은 항상 True)"""
        return True

    def set_config(self, config_path: str, config_value: Any,
                   data_type: str = "string", category: Optional[str] = None,
                   env_name: Optional[str] = None) -> bool:
        """
        설정 값 저장

        Args:
            config_path: 설정 경로 (예: "openai.api_key", "vast.vllm.port")
            config_value: 설정 값
            data_type: 데이터 타입 (string, int, float, bool, list, dict)
            category: 설정 카테고리 (예: "openai", "vast")
            env_name: 환경 변수 이름 (예: "OPENAI_API_KEY")

        Returns:
            bool: 성공 여부
        """
        try:
            # 카테고리 자동 추출 (config_path의 첫 번째 부분)
            if not category:
                category = config_path.split('.')[0]

            # env_name이 없으면 config_path 사용
            final_env_name = env_name or config_path

            # 설정 값과 메타데이터 저장
            config_data = {
                'value': config_value,
                'type': data_type,
                'category': category,
                'path': config_path,
                'env_name': final_env_name
            }

            # 메모리에 저장
            self._configs[final_env_name] = config_data

            # 카테고리별 인덱스 업데이트
            if category not in self._category_index:
                self._category_index[category] = set()
            self._category_index[category].add(final_env_name)

            # DB에도 저장 (DB가 있는 경우) - 분산 환경에서 앱 재시작 시 복원 가능
            if self.db_manager:
                try:
                    if hasattr(self.db_manager, 'update_config'):
                        db_success = self.db_manager.update_config(
                            env_name=final_env_name,
                            config_path=config_path,
                            config_value=config_value,
                            data_type=data_type,
                        )
                        if db_success:
                            logger.debug(f"[DB] Config 저장 완료: {final_env_name}")
                except Exception as db_error:
                    logger.error(f"DB 저장 실패: {config_path} - {db_error}")

            logger.debug(f"[Local] Config 저장 완료: {final_env_name} = {config_value}")
            return True

        except Exception as e:
            logger.error(f"Config 저장 실패: {config_path} - {str(e)}")
            return False

    def get_config_value(self, env_name: str, default: Any = None) -> Any:
        """
        설정 값만 조회 (env_name 기준)

        Args:
            env_name: 환경 변수 이름
            default: 기본값

        Returns:
            설정 값 또는 기본값
        """
        try:
            if env_name in self._configs:
                return self._configs[env_name].get('value', default)
            return default

        except Exception as e:
            logger.error(f"Config 조회 실패: {env_name} - {str(e)}")
            return default

    def get_config(self, env_name: str) -> Optional[Dict[str, Any]]:
        """
        설정 값과 메타데이터 조회 (env_name 기준)

        Args:
            env_name: 환경 변수 이름

        Returns:
            설정 데이터 (value, type, category, path, env_name)
        """
        try:
            return self._configs.get(env_name)

        except Exception as e:
            logger.error(f"Config 조회 실패: {env_name} - {str(e)}")
            return None

    def delete_config(self, env_name: str) -> bool:
        """
        설정 삭제 (env_name 기준)

        Args:
            env_name: 환경 변수 이름

        Returns:
            bool: 성공 여부
        """
        try:
            if env_name not in self._configs:
                logger.warning(f"삭제할 Config를 찾을 수 없음: {env_name}")
                return False

            config_data = self._configs[env_name]
            category = config_data.get('category')

            # 메모리에서 삭제
            del self._configs[env_name]

            # 카테고리 인덱스에서도 제거
            if category and category in self._category_index:
                self._category_index[category].discard(env_name)

            # DB에서도 삭제 (DB가 있는 경우)
            if self.db_manager and hasattr(self.db_manager, 'delete_config'):
                try:
                    self.db_manager.delete_config(env_name)
                except Exception as db_error:
                    logger.warning(f"DB에서 설정 삭제 실패: {env_name} - {db_error}")

            logger.debug(f"Config 삭제 완료: {env_name}")
            return True

        except Exception as e:
            logger.error(f"Config 삭제 실패: {env_name} - {str(e)}")
            return False

    def get_category_configs(self, category: str) -> List[Dict[str, Any]]:
        """
        특정 카테고리의 모든 설정 조회 (리스트 형태)

        Args:
            category: 카테고리 이름

        Returns:
            설정 리스트
        """
        try:
            env_names = self._category_index.get(category, set())

            configs = []
            for env_name in env_names:
                config = self.get_config(env_name)
                if config:
                    configs.append(config)

            return configs

        except Exception as e:
            logger.error(f"카테고리 Config 조회 실패: {category} - {str(e)}")
            return []

    def get_category_configs_nested(self, category: str) -> Dict[str, Any]:
        """
        특정 카테고리의 모든 설정 조회 (중첩 딕셔너리 형태)

        Args:
            category: 카테고리 이름

        Returns:
            중첩된 딕셔너리 형태의 설정
        """
        try:
            configs = self.get_category_configs(category)
            result = {}

            for config in configs:
                path = config['path']
                value = config['value']

                # 경로를 '.'로 분리하여 중첩 딕셔너리 생성
                keys = path.split('.')
                current = result

                for key in keys[:-1]:
                    if key not in current:
                        current[key] = {}
                    current = current[key]

                current[keys[-1]] = value

            return result

        except Exception as e:
            logger.error(f"카테고리 중첩 Config 조회 실패: {category} - {str(e)}")
            return {}

    def get_all_configs(self) -> List[Dict[str, Any]]:
        """
        모든 설정 조회

        Returns:
            모든 설정 리스트
        """
        try:
            return list(self._configs.values())

        except Exception as e:
            logger.error(f"전체 Config 조회 실패: {str(e)}")
            return []

    def clear_category(self, category: str) -> bool:
        """
        특정 카테고리의 모든 설정 삭제

        Args:
            category: 카테고리 이름

        Returns:
            bool: 성공 여부
        """
        try:
            env_names = list(self._category_index.get(category, set()))

            # 각 설정 삭제
            for env_name in env_names:
                self.delete_config(env_name)

            # 카테고리 인덱스도 삭제
            if category in self._category_index:
                del self._category_index[category]

            logger.info(f"카테고리 '{category}' 전체 삭제 완료")
            return True

        except Exception as e:
            logger.error(f"카테고리 삭제 실패: {category} - {str(e)}")
            return False

    def exists(self, env_name: str) -> bool:
        """
        설정 존재 여부 확인 (env_name 기준)

        Args:
            env_name: 환경 변수 이름

        Returns:
            bool: 존재 여부
        """
        return env_name in self._configs

    def get_all_categories(self) -> List[str]:
        """
        모든 카테고리 목록 조회

        Returns:
            카테고리 목록
        """
        return sorted(list(self._category_index.keys()))

    # ========== ConfigComposer 호환성 메서드 ==========

    def get_config_by_name(self, config_name: str) -> Any:
        """
        이름으로 특정 설정 가져오기 (ConfigComposer 호환)

        Args:
            config_name: 설정 이름

        Returns:
            설정 값

        Raises:
            KeyError: 설정이 존재하지 않는 경우
        """
        # 1. env_name으로 직접 검색 시도
        value = self.get_config_value(config_name)
        if value is not None:
            return value

        # 2. 모든 config에서 검색
        for config in self._configs.values():
            if config.get('env_name') == config_name or config['path'] == config_name:
                return config['value']

            # path의 마지막 부분이 config_name과 일치하는 경우
            path_parts = config['path'].split('.')
            if path_parts[-1] == config_name:
                return config['value']

        raise KeyError(f"Configuration '{config_name}' not found")

    def get_config_by_category_name(self, category_name: str) -> Dict[str, Any]:
        """
        카테고리 이름으로 특정 설정 그룹 가져오기 (ConfigComposer 호환)

        Args:
            category_name: 카테고리 이름

        Returns:
            해당 카테고리의 모든 설정

        Raises:
            KeyError: 카테고리가 존재하지 않는 경우
        """
        configs = self.get_category_configs_nested(category_name)

        if not configs:
            raise KeyError(f"Configuration category '{category_name}' not found")

        return configs

    def update_config_by_name(self, config_name: str, new_value: Any) -> None:
        """
        이름으로 특정 설정 업데이트 (ConfigComposer 호환)

        Args:
            config_name: 설정 이름
            new_value: 새로운 값

        Raises:
            KeyError: 설정이 존재하지 않는 경우
        """
        # 1. env_name으로 직접 검색 시도
        if self.exists(config_name):
            config_data = self.get_config(config_name)
            self.set_config(
                config_path=config_data.get('path', config_name),
                config_value=new_value,
                data_type=config_data.get('type', 'string'),
                category=config_data.get('category'),
                env_name=config_data.get('env_name')
            )
            logger.info(f"Config 업데이트 완료: {config_name} = {new_value}")
            return

        # 2. 모든 config에서 검색하여 업데이트
        for config in self._configs.values():
            if config.get('env_name') == config_name or config['path'] == config_name:
                self.set_config(
                    config_path=config['path'],
                    config_value=new_value,
                    data_type=config['type'],
                    category=config['category'],
                    env_name=config.get('env_name')
                )
                logger.info(f"Config 업데이트 완료: {config_name} = {new_value}")
                return

            path_parts = config['path'].split('.')
            if path_parts[-1] == config_name:
                self.set_config(
                    config_path=config['path'],
                    config_value=new_value,
                    data_type=config['type'],
                    category=config['category'],
                    env_name=config.get('env_name')
                )
                logger.info(f"Config 업데이트 완료: {config['path']} = {new_value}")
                return

        raise KeyError(f"Configuration '{config_name}' not found")

    def get_all_config(self, **kwargs) -> Dict[str, Any]:
        """
        모든 설정을 카테고리별로 구조화하여 반환 (ConfigComposer 호환)

        Returns:
            Dict: 카테고리별 설정
        """
        result = {}

        for category in self.get_all_categories():
            result[category] = self.get_category_configs_nested(category)

        result["all_configs"] = self.get_all_configs()

        return result

    def get_config_summary(self) -> Dict[str, Any]:
        """
        모든 설정의 요약 정보 반환 (ConfigComposer 호환)

        Returns:
            Dict: 설정 요약 정보
        """
        try:
            all_configs = self.get_all_configs()
            categories = self.get_all_categories()

            categories_summary = {}
            for category in categories:
                category_configs = self.get_category_configs(category)
                categories_summary[category] = {
                    "count": len(category_configs),
                    "configs": [
                        {
                            "path": cfg["path"],
                            "type": cfg["type"],
                            "has_value": cfg["value"] is not None
                        }
                        for cfg in category_configs
                    ]
                }

            return {
                "total_configs": len(all_configs),
                "discovered_categories": categories,
                "categories": categories_summary,
                "persistent_summary": self.export_config_summary()
            }

        except Exception as e:
            logger.error(f"Config 요약 정보 조회 실패: {str(e)}")
            return {
                "total_configs": 0,
                "discovered_categories": [],
                "categories": {},
                "persistent_summary": {},
                "error": str(e)
            }

    def update_config(self, config_name: str, new_value: Any) -> Dict[str, Any]:
        """
        설정값을 업데이트하고 결과를 반환하는 통합 메서드 (ConfigComposer 호환)

        Args:
            config_name: 설정 이름
            new_value: 새로운 값

        Returns:
            Dict: 업데이트 결과 정보
        """
        try:
            old_value = self.get_config_by_name(config_name)
            self.update_config_by_name(config_name, new_value)

            return {
                "config_name": config_name,
                "old_value": old_value,
                "new_value": new_value,
                "success": True
            }

        except KeyError:
            raise KeyError(f"Config '{config_name}' not found")
        except Exception as e:
            raise ValueError(f"Failed to update config '{config_name}': {str(e)}")

    def refresh_all(self) -> None:
        """모든 설정을 DB에서 다시 로드 (ConfigComposer 호환)"""
        self._configs = {}
        self._category_index = {}
        self._load_from_db()
        logger.info("=== Local configs refreshed from DB ===")

    def export_config_summary(self) -> Dict[str, Any]:
        """
        모든 설정의 요약 정보 반환 (PersistentConfig 형태)

        Returns:
            Dict: 설정 요약 정보
        """
        all_configs = self.get_all_configs()

        return {
            "total_configs": len(all_configs),
            "storage_type": "memory" if not self.db_manager else "memory+db",
            "configs": [
                {
                    "env_name": config.get("env_name", ""),
                    "config_path": config.get("path", ""),
                    "current_value": config.get("value"),
                    "default_value": config.get("env_value"),
                    "is_saved": config.get("value") is not None,
                    "data_type": config.get("type", "string"),
                    "category": config.get("category")
                }
                for config in all_configs
            ]
        }

    def get_registry_statistics(self) -> Dict[str, Any]:
        """
        레지스트리 통계 정보 반환

        Returns:
            Dict: 통계 정보
        """
        all_configs = self.get_all_configs()
        config_paths = [config['path'] for config in all_configs]
        env_names = [config['env_name'] for config in all_configs]

        # 중복 검사
        duplicate_paths = []
        duplicate_names = []

        seen_paths = set()
        seen_names = set()

        for path in config_paths:
            if path in seen_paths:
                duplicate_paths.append(path)
            seen_paths.add(path)

        for name in env_names:
            if name in seen_names:
                duplicate_names.append(name)
            seen_names.add(name)

        return {
            "total_configs": len(all_configs),
            "unique_config_paths": len(set(config_paths)),
            "unique_env_names": len(set(env_names)),
            "duplicate_config_paths": duplicate_paths,
            "duplicate_env_names": duplicate_names,
            "has_duplicates": len(duplicate_paths) > 0 or len(duplicate_names) > 0,
            "categories": self.get_all_categories(),
            "storage_type": "memory" if not self.db_manager else "memory+db"
        }

    def save_all(self) -> None:
        """모든 설정을 DB에 저장 (ConfigComposer 호환)"""
        if not self.db_manager:
            logger.warning("=== DB Manager가 없어 저장 스킵 (메모리 전용 모드) ===")
            return

        try:
            if hasattr(self.db_manager, 'update_config'):
                for env_name, config_data in self._configs.items():
                    self.db_manager.update_config(
                        env_name=env_name,
                        config_path=config_data.get('path', env_name),
                        config_value=config_data.get('value'),
                        data_type=config_data.get('type', 'string'),
                    )
                logger.info(f"=== Local configs saved to DB ({len(self._configs)}개) ===")
            else:
                logger.warning("=== DB Manager에 update_config 메서드 없음 ===")
        except Exception as e:
            logger.error(f"=== DB 저장 실패: {e} ===")

    def validate_critical_configs(self) -> Dict[str, Any]:
        """
        중요한 설정들이 올바르게 설정되었는지 검증 (ConfigComposer 호환)

        Returns:
            Dict: 검증 결과
        """
        validation_results = {
            "valid": True,
            "warnings": [],
            "errors": []
        }

        try:
            # 포트 번호 검증
            try:
                port = self.get_config_by_name("PORT")
                if port is not None:
                    port_int = int(port)
                    if not (1 <= port_int <= 65535):
                        validation_results["errors"].append(f"Invalid port number: {port}")
                        validation_results["valid"] = False
            except (KeyError, ValueError):
                pass

            # API 키 존재 여부 확인
            try:
                api_key = self.get_config_by_name("OPENAI_API_KEY")
                if not api_key or api_key.strip() == "":
                    validation_results["warnings"].append("OpenAI API Key is not set")
            except KeyError:
                pass

            # 데이터베이스 연결 정보 확인
            try:
                db_host = self.get_config_by_name("DATABASE_HOST")
                if not db_host:
                    validation_results["warnings"].append("Database host is not set")
            except KeyError:
                pass

        except Exception as e:
            logger.error(f"Config 검증 실패: {str(e)}")
            validation_results["errors"].append(f"Validation error: {str(e)}")
            validation_results["valid"] = False

        return validation_results


def create_config_manager(db_manager=None):
    """
    Redis 또는 Local Config Manager 생성 (팩토리 함수)

    Redis 연결이 가능하면 RedisConfigManager를 반환하고,
    연결 실패 시 LocalConfigManager를 fallback으로 반환합니다.

    Args:
        db_manager: DB 매니저 인스턴스 (선택적)

    Returns:
        RedisConfigManager 또는 LocalConfigManager 인스턴스
    """
    from xgen_sdk.config.redis_config import RedisConfigManager

    # Redis 연결 시도
    redis_manager = RedisConfigManager(db_manager=db_manager)

    if redis_manager._connection_available:
        logger.info("🔴 Redis 연결 성공 - RedisConfigManager 사용")
        return redis_manager
    else:
        logger.warning("🟡 Redis 연결 실패 - LocalConfigManager로 fallback")
        return LocalConfigManager(db_manager=db_manager)
