"""Env-var fallback for per-agent Slack secrets.

The canonical source for per-agent ``bot_token`` / ``signing_secret`` is the
``slack_agents`` DB row, encrypted with ``SLACK_AGENT_GW_ENCRYPTION_KEY``. When
a row's encrypted columns are null (e.g. an imported bot that was not
provisioned via BotFather), the gateway falls back to environment variables
named after the ``bot_name``:

    SLACK_AGENT_GATEWAY_<BOT_NAME_UPPER>_BOT_TOKEN
    SLACK_AGENT_GATEWAY_<BOT_NAME_UPPER>_SIGNING_SECRET

Operators who don't want DB-resident secrets (e.g. they prefer a k8s/Vault/SSM
tool that mounts env vars at boot) can leave the encrypted columns null and
populate those env vars however they like.
"""

import os

from ypl.backend.config import settings


def _build_env_var_name(bot_name: str, secret_type: str) -> str:
    """Convert ``(bot_name, secret_type)`` to the env var name.

    ``secret_type`` is one of ``"bot-token"``, ``"signing-secret"``. Dashes
    in either argument are replaced with underscores because env var names
    cannot contain dashes.
    """
    suffix = secret_type.upper().replace("-", "_")
    name = bot_name.upper().replace("-", "_")
    return f"SLACK_AGENT_GATEWAY_{name}_{suffix}"


def get_env_var_secret(bot_name: str, secret_type: str) -> str | None:
    """Read a secret from the environment.

    Checks ``os.environ`` first (so values injected at container/systemd start
    win), then falls back to ``settings`` (for values supplied via pydantic's
    ``.env`` loader).
    """
    env_var_name = _build_env_var_name(bot_name, secret_type)
    return os.environ.get(env_var_name) or getattr(settings, env_var_name, None) or None
