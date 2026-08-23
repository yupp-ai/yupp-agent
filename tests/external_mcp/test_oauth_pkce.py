"""PKCE plumbing for the external-MCP OAuth client.

arti requires ``code_challenge`` on ``/oauth/authorize`` and verifies the
``code_verifier`` at token exchange; these tests pin the S256 relationship and
that the verifier round-trips through the signed state.
"""
import base64
import hashlib

from ypl.external_mcp import oauth_client


def test_generate_pkce_is_s256_of_verifier() -> None:
    verifier, challenge = oauth_client.generate_pkce()
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expected
    assert "=" not in challenge  # base64url, unpadded


def test_generate_pkce_is_random() -> None:
    assert oauth_client.generate_pkce()[0] != oauth_client.generate_pkce()[0]


def test_authorize_url_includes_challenge_only_when_given() -> None:
    base = dict(authorize_url="https://a.example/auth", client_id="cid",
                redirect_uri="https://x/cb", scopes=["s"], state="st")
    with_ch = oauth_client.build_authorize_url(**base, code_challenge="CHAL")
    assert "code_challenge=CHAL" in with_ch and "code_challenge_method=S256" in with_ch
    without = oauth_client.build_authorize_url(**base)
    assert "code_challenge" not in without


def test_state_round_trips_the_verifier() -> None:
    st = oauth_client.sign_state("user-1", "arti-obo", code_verifier="the-verifier")
    claims = oauth_client.verify_state(st)
    assert claims["cv"] == "the-verifier"
    assert claims["sub"] == "user-1" and claims["slug"] == "arti-obo"
