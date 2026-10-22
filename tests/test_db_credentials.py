from uuid import uuid4

import pytest

from xgen_sdk.db_credentials.envelope import (
    CredentialEnvelopeError, CredentialKeyring, decrypt_envelope, encrypt_envelope,
)
from xgen_sdk.db_credentials.rows import decrypt_row_credential


@pytest.mark.parametrize("plaintext", ["synthetic-db-password", "synthetic-access-password"])
def test_shared_row_reader_roundtrips_and_rejects_cross_row_or_field(plaintext):
    ring = CredentialKeyring(active_kid="test", keys={"test": bytes(range(32))})
    binding = str(uuid4())
    encrypted = encrypt_envelope(plaintext, binding_id=binding, field_name="db_password", keyring=ring)
    row = {"credential_binding_id": binding, "credential_format": "envelope-v1", "db_password": encrypted}
    assert decrypt_row_credential(row, "db_password", keyring=ring) == plaintext
    for wrong_binding, wrong_field in [(str(uuid4()), "db_password"), (binding, "custom_password")]:
        with pytest.raises(CredentialEnvelopeError):
            decrypt_envelope(encrypted, binding_id=wrong_binding, field_name=wrong_field, keyring=ring)


def test_shared_reader_refuses_legacy_values_without_plaintext_fallback():
    with pytest.raises(CredentialEnvelopeError, match="CREDENTIAL_FORMAT_UNMIGRATED"):
        decrypt_row_credential({"db_password": "legacy-value"}, "db_password")
