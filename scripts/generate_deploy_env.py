import sys
from pathlib import Path
from typing import Any, Literal, cast, get_args

# Any third-party dependencies that are added here, should also be installed in
# .github/actions/generate-gcloud-deploy-secrets-string/action.yml
import yaml
from pydantic import BaseModel, field_validator

# TODO: move these to a shared module
Environment = Literal["local", "staging", "production"]
Service = Literal[
    "agent-harness-service",
    "backend",
    "cronjob",
    "discord-service",
    "leaderboard",
    "mcp-server",
    "partner-payments-server",
    "risk-service",
    "slack-agent-gateway",
    "slack-interaction-server",
    "streamlit-server",
    "webhooks-service",
]
Format = Literal["yaml", "dotenv"]

ENV_VAR_DIR = "data/env_vars"


class EnvVariables(BaseModel):
    env_vars: dict[str, str]

    @field_validator("env_vars", mode="before")
    def validate_and_convert_values_to_strings(cls, v: dict[str, Any]) -> dict[str, str]:
        result = {}
        for key, value in v.items():
            assert key.replace("_", "").isalnum() and not key[0].isdigit(), (
                f"NAME {key} must be a valid environment variable name"
            )
            assert key.isupper(), f"NAME {key} must be uppercase"
            if isinstance(value, bool):
                # Print boolean values as lowercase strings instead of 'True' or 'False'
                result[key] = str(value).lower()
            else:
                result[key] = str(value)
        return result


def load_config(env: Environment, service_name: Service) -> dict[str, str]:
    """
    Load configuration for a given service and environment.

    Merges the following files in order:
    - base.yml
    - {env}.yml
    - {service_name}/base.yml
    - {service_name}/{env}.yml
    """
    base_path = Path(f"{ENV_VAR_DIR}/base.yml")
    base_env_path = Path(f"{ENV_VAR_DIR}/{env}.yml")
    service_base_path = Path(f"{ENV_VAR_DIR}/{service_name}/base.yml")
    service_env_path = Path(f"{ENV_VAR_DIR}/{service_name}/{env}.yml")

    config: dict[str, str] = {}
    if base_path.exists():
        with open(base_path) as f:
            config = EnvVariables.model_validate(yaml.safe_load(f)).env_vars

    if base_env_path.exists():
        with open(base_env_path) as f:
            config.update(EnvVariables.model_validate(yaml.safe_load(f) or {}).env_vars)

    if service_base_path.exists():
        with open(service_base_path) as f:
            config.update(EnvVariables.model_validate(yaml.safe_load(f) or {}).env_vars)

    if service_env_path.exists():
        with open(service_env_path) as f:
            config.update(EnvVariables.model_validate(yaml.safe_load(f) or {}).env_vars)

    return config


def print_env_yaml(config: dict[str, str]) -> None:
    """Print the environment variables as a YAML file."""
    print(yaml.dump(config, sort_keys=False))


def print_env_dotenv(config: dict[str, str]) -> None:
    """Print the environment variables as a dotenv file."""
    for key, value in config.items():
        print(f"{key}={value}")


def print_usage_and_exit() -> None:
    print("Usage: generate_deploy_env.py <environment> <service-name> [format]")
    print("  format: optional, either 'yaml' (default) or 'dotenv'")
    sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) < 3 or len(sys.argv) > 4:
        print_usage_and_exit()

    environment = sys.argv[1]
    service_name = sys.argv[2]
    format_type = sys.argv[3] if len(sys.argv) == 4 else "yaml"

    assert environment in [
        "staging",
        "production",
    ], f"<environment> {environment} must be one of {'staging', 'production'}"
    assert service_name in get_args(Service), f"<service_name> {service_name} must be one of {get_args(Service)}"
    assert format_type in get_args(Format), f"<format> {format_type} must be one of {get_args(Format)}"

    config = load_config(cast(Environment, environment), cast(Service, service_name))

    if format_type == "dotenv":
        print_env_dotenv(config)
    else:
        print_env_yaml(config)
