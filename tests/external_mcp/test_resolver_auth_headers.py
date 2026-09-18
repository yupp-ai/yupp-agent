"""``_auth_headers`` — how a resolved credential is rendered onto the wire.

The default is RFC 6750 (``Authorization: Bearer …``).  Servers that name a
custom ``auth_header`` get the credential verbatim on that header, because the
``Bearer`` prefix belongs to ``Authorization`` and providers matching on a
bespoke header (arti's ``X-Arti-Service-Secret``, a plain ``X-API-Key``)
compare the raw value.
"""

import pytest

from ypl.db.external_mcp import McpAuthType, McpServer
from ypl.external_mcp.resolver import _auth_headers


def _server(auth_header: str | None) -> McpServer:
    return McpServer(
        slug="arti",
        display_name="arti",
        url="http://example.invalid/mcp",
        auth_type=McpAuthType.M2M_SHARED,
        auth_header=auth_header,
    )


@pytest.mark.parametrize("auth_header", [None, "", "   ", "Authorization", "authorization"])
def test_defaults_to_bearer(auth_header: str | None) -> None:
    assert _auth_headers(_server(auth_header), "tok") == {"Authorization": "Bearer tok"}


def test_custom_header_carries_credential_verbatim() -> None:
    # No "Bearer " prefix: arti compares the shared secret byte-for-byte.
    assert _auth_headers(_server("X-Arti-Service-Secret"), "s3cret") == {"X-Arti-Service-Secret": "s3cret"}


def test_custom_header_is_trimmed() -> None:
    assert _auth_headers(_server("  X-API-Key  "), "k") == {"X-API-Key": "k"}
