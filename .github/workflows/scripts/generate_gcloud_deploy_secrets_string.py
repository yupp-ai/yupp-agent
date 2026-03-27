#!/usr/bin/env python3

import sys
from typing import cast, get_args

# Any third-party dependencies that are added here, should also be installed during the
# "Generate gcloud deploy secrets string" step in the .github/actions/generate-gcloud-deploy-secrets-string action.
from scripts.secret_env_var_map import Environment, Service, validate_and_load_secret_env_var_map


def print_usage_and_exit() -> None:
    print("Usage: generate_gcloud_deploy_secrets_string.py <environment> <service-name>")
    print("   OR: generate_gcloud_deploy_secrets_string.py validate-yaml")
    sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print_usage_and_exit()

    environment = sys.argv[1]
    service_name = sys.argv[2]
    assert environment in [
        "staging",
        "production",
    ], f"<environment> {environment} must be one of {'staging', 'production'}"
    assert service_name in get_args(Service), f"<service_name> {service_name} must be one of {get_args(Service)}"

    secret_env_var_map = validate_and_load_secret_env_var_map()

    secrets_string = secret_env_var_map.to_secret_string_for_service(
        environment=cast(Environment, environment), service_name=cast(Service, service_name)
    )
    print(secrets_string)
