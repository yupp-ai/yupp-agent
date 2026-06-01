"""Curated catalog of well-known external MCP servers.

Hardcoding the boring fields (URL, authorize_url, token_url, scopes,
extra_authorize_params) for popular providers means an admin only has to
paste their own OAuth client_id/secret + tick the roles allowed to use it.

Each entry is **just data** — the actual registry rows still live in the
``mcp_servers`` table.  Admin clicks "Add from catalog" → we build an
``McpServer`` row from the catalog entry + user-supplied secrets.

Add a new entry below when you onboard another well-known MCP.  Custom
MCPs (internal services, niche providers) still use the manual "Custom"
form on the admin page.
"""

from __future__ import annotations
from dataclasses import dataclass, field

from ypl.db.external_mcp import McpAuthType, McpTransport


@dataclass(frozen=True)
class CatalogEntry:
    """A pre-configured external MCP, ready to register with the admin's
    OAuth client_id/secret pasted in."""

    slug: str
    display_name: str
    description: str
    url: str
    transport: McpTransport
    auth_type: McpAuthType
    # OAuth metadata.  Empty when auth_type is not OAUTH_OBO.
    authorize_url: str = ""
    token_url: str = ""
    scopes: list[str] = field(default_factory=list)
    extra_authorize_params: dict[str, str] = field(default_factory=dict)
    # Operator hint shown in the UI — typically "create + enable the API in
    # your GCP project" or "go to <provider settings> to mint a key".
    setup_hint: str = ""


# Google's first-party hosted MCP servers.  All three live under
# *mcp.googleapis.com, all use standard Google OAuth 2.0, and all
# require the corresponding *.googleapis.com API to be enabled in
# the GCP project that owns the OAuth client.
_GOOGLE_OAUTH_AUTHORIZE = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_OAUTH_TOKEN = "https://oauth2.googleapis.com/token"
_GOOGLE_OAUTH_EXTRA = {
    # ``offline`` makes Google issue a refresh token (otherwise the
    # access token dies after ~1h and Connect must be re-clicked).
    "access_type": "offline",
    # ``prompt=consent`` forces the consent screen even on re-authorize,
    # which is the *only* reliable way to re-issue a refresh token when
    # a previous one has been revoked.
    "prompt": "consent",
}


CATALOG: list[CatalogEntry] = [
    CatalogEntry(
        slug="gmail",
        display_name="Gmail",
        description="Read / search / send mail on the user's behalf via Google's hosted Gmail MCP.",
        url="https://gmailmcp.googleapis.com/mcp/v1",
        transport=McpTransport.STREAMABLE_HTTP,
        auth_type=McpAuthType.OAUTH_OBO,
        authorize_url=_GOOGLE_OAUTH_AUTHORIZE,
        token_url=_GOOGLE_OAUTH_TOKEN,
        scopes=["https://www.googleapis.com/auth/gmail.modify"],
        extra_authorize_params=_GOOGLE_OAUTH_EXTRA,
        setup_hint=(
            "Enable the Gmail API in your GCP project "
            "(https://console.cloud.google.com/apis/library/gmail.googleapis.com), "
            "then paste your existing Web-Application OAuth client_id + secret. "
            "Add https://ahs.<your-apex>/mcp_oauth/callback to that client's "
            "Authorized redirect URIs."
        ),
    ),
    CatalogEntry(
        slug="gdrive",
        display_name="Google Drive",
        description="Read / search files on the user's Drive via Google's hosted Drive MCP.",
        url="https://drivemcp.googleapis.com/mcp/v1",
        transport=McpTransport.STREAMABLE_HTTP,
        auth_type=McpAuthType.OAUTH_OBO,
        authorize_url=_GOOGLE_OAUTH_AUTHORIZE,
        token_url=_GOOGLE_OAUTH_TOKEN,
        scopes=["https://www.googleapis.com/auth/drive.readonly"],
        extra_authorize_params=_GOOGLE_OAUTH_EXTRA,
        setup_hint=(
            "Enable the Google Drive API "
            "(https://console.cloud.google.com/apis/library/drive.googleapis.com). "
            "Same OAuth client + same callback as Gmail — re-paste the client_id/secret."
        ),
    ),
    CatalogEntry(
        slug="gcal",
        display_name="Google Calendar",
        description="Read / write events on the user's primary calendar via Google's hosted Calendar MCP.",
        url="https://calendarmcp.googleapis.com/mcp/v1",
        transport=McpTransport.STREAMABLE_HTTP,
        auth_type=McpAuthType.OAUTH_OBO,
        authorize_url=_GOOGLE_OAUTH_AUTHORIZE,
        token_url=_GOOGLE_OAUTH_TOKEN,
        scopes=["https://www.googleapis.com/auth/calendar"],
        extra_authorize_params=_GOOGLE_OAUTH_EXTRA,
        setup_hint=(
            "Enable the Calendar API "
            "(https://console.cloud.google.com/apis/library/calendar-json.googleapis.com). "
            "Same OAuth client + same callback as Gmail."
        ),
    ),
]


def get(slug: str) -> CatalogEntry | None:
    return next((e for e in CATALOG if e.slug == slug), None)
