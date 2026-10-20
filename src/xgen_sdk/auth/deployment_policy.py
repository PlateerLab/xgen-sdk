"""Canonical deployment-policy binding for short-lived capabilities."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any


DEPLOYMENT_POLICY_FIELDS = (
    "id",
    "user_id",
    "workflow_id",
    "published_workflow_id",
    "is_deployed",
    "is_accepted",
    "deployment_stage",
    "deploy_key",
    "require_api_key",
    "public_policy",
)


class DeploymentPolicyDigestError(ValueError):
    """The deployment row cannot be represented by the canonical policy schema."""


def _reject_constant(value: str) -> None:
    raise DeploymentPolicyDigestError(f"non-finite JSON number in public_policy: {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DeploymentPolicyDigestError("duplicate JSON member in public_policy")
        result[key] = value
    return result


def _public_policy(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(
                value,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
        except (json.JSONDecodeError, DeploymentPolicyDigestError) as exc:
            raise DeploymentPolicyDigestError("public_policy must contain valid JSON") from exc
    return value


def deployment_policy_digest(row: Mapping[str, Any]) -> str:
    """Return the SHA-256 digest of the security-relevant deployment fields.

    Extra database columns, including mutable timestamps, are ignored. A JSON
    string in ``public_policy`` is parsed before serialization so JSON and JSONB
    representations of the same policy produce the same digest.
    """

    if not isinstance(row, Mapping):
        raise DeploymentPolicyDigestError("deployment row must be a mapping")
    missing = [field for field in DEPLOYMENT_POLICY_FIELDS if field not in row]
    if missing:
        raise DeploymentPolicyDigestError(
            f"deployment row is missing required fields: {', '.join(missing)}"
        )
    canonical = {field: row[field] for field in DEPLOYMENT_POLICY_FIELDS}
    canonical["public_policy"] = _public_policy(canonical["public_policy"])
    try:
        encoded = json.dumps(
            canonical,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise DeploymentPolicyDigestError("deployment policy contains a non-JSON value") from exc
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "DEPLOYMENT_POLICY_FIELDS",
    "DeploymentPolicyDigestError",
    "deployment_policy_digest",
]
