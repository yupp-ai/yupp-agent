import os
from typing import Literal, get_args

# Any third-party dependencies that are added here, should also be installed during the
# "Generate gcloud deploy secrets string" step in the deploy-v2-deploy-only.yml workflow.
import yaml
from pydantic import BaseModel, field_validator

Environment = Literal["local", "staging", "production"]
Service = Literal[
    "agent-harness-service",
    "backend",
    "cronjob",
    "mcp-server",
    "partner-payments-server",
    "webhooks-service",
    "risk-service",
    "slack-agent-gateway",
    "slack-interaction-server",
    "streamlit-server",
    "discord-service",
]

SECRET_ENV_VAR_MAP_FILE = "data/secret-env-var-map.yml"


class EnvVar(BaseModel):
    name: str
    services: list[Service]
    secret_names: dict[Environment, str]
    version: str = "latest"  # Default, and across all environments for now.

    @field_validator("name")
    def validate_name(cls, v: str) -> str:
        assert v.replace("_", "").isalnum() and not v[0].isdigit(), (
            f"NAME {v} must be a valid environment variable name"
        )
        assert v.isupper(), f"NAME {v} must be uppercase"
        return v

    @field_validator("secret_names")
    def validate_secret_names(cls, v: dict[Environment, str]) -> dict[Environment, str]:
        for environment in v:
            if environment not in get_args(Environment):
                raise ValueError(f"environment {environment} must be one of {get_args(Environment)}")
        return v

    def to_secret_key_value_pair(self, environment: Environment) -> str:
        return f"{self.name}={self.secret_names[environment]}:{self.version}"


class SecretEnvVarMap(BaseModel):
    env_vars: list[EnvVar]

    def to_secret_string_for_service(self, environment: Environment, service_name: Service) -> str:
        return ",".join(
            [
                env_var.to_secret_key_value_pair(environment)
                for env_var in self.env_vars
                if service_name in env_var.services and environment in env_var.secret_names
            ]
        )


def validate_and_load_secret_env_var_map() -> SecretEnvVarMap:
    with open(SECRET_ENV_VAR_MAP_FILE) as f:
        secret_env_var_map = yaml.safe_load(f)

    return SecretEnvVarMap.model_validate(secret_env_var_map)


def format_and_dump_secret_env_var_map(secret_env_var_map: SecretEnvVarMap) -> None:
    secret_env_var_map.env_vars.sort(key=lambda x: x.name)
    with open(SECRET_ENV_VAR_MAP_FILE, "w") as f:
        yaml.dump(
            secret_env_var_map.model_dump(),
            f,
            indent=2,
            sort_keys=False,
        )


def validate_env_var_is_not_set(env_var: str, secret_env_var_map: SecretEnvVarMap | None = None) -> None:
    if secret_env_var_map is None:
        secret_env_var_map = validate_and_load_secret_env_var_map()

    for env_var_struct in secret_env_var_map.env_vars:
        if env_var_struct.name == env_var:
            raise ValueError(f"Environment variable {env_var} is already set in the {SECRET_ENV_VAR_MAP_FILE} file")


def add_new_env_var(env_var: EnvVar) -> None:
    secret_env_var_map = validate_and_load_secret_env_var_map()
    validate_env_var_is_not_set(env_var.name, secret_env_var_map)
    secret_env_var_map.env_vars.append(env_var)
    format_and_dump_secret_env_var_map(secret_env_var_map)


def format_secret_env_var_map() -> None:
    secret_env_var_map = validate_and_load_secret_env_var_map()
    format_and_dump_secret_env_var_map(secret_env_var_map)


def read_env_file(env_file: str) -> dict[str, str]:
    env_vars: dict[str, str] = {}
    if not os.path.exists(env_file):
        return env_vars

    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                key, value = line.split("=", 1)
                env_vars[key] = value
        return env_vars
