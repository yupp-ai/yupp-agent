#! /usr/bin/env python3

"""
This script is used to manage secrets in Google Cloud Secret Manager.

It can be used to add a new secret or update the value of an existing secret.
It can also be used to pull secrets from GCP Secret Manager to a local .env file.

Example usage:

```
python -m scripts.gcpsecrets add OPENAI_API_KEY --project yupp-llms
```

It will prompt you to enter the secret value, and create the local, staging and production secrets with the same value.

If you want to mark the secret as sensitive (e.g. if it deals with payments), you can use the `--sensitive` flag.
This is not enforced right now, but it's a good idea to use it.

If you want to restrict the environments to add the secret to, you can use the `--env` or `-e` flag.

Example that adds the secret only to staging and production:
```
python -m scripts.gcpsecrets add OPENAI_API_KEY --project yupp-llms -e staging -e production
```

If you want to create separate secrets with different values for each environment, you can use the
`--per-environment-secret` flag. This will prompt you to enter a separate value for each environment.

Example that creates separate secrets for each environment:
```
python -m scripts.gcpsecrets add OPENAI_API_KEY --project yupp-llms --per-environment-secret
```

To update the value of an existing secret, you can use the `update` command. It will prompt you to enter the new secret
value.

Example that updates the secret only for production:

```
python -m scripts.gcpsecrets update-value OPENAI_API_KEY --project yupp-llms --env production
```

To update with separate values for each environment, you can use the `--per-environment-secret` flag:

```
python -m scripts.gcpsecrets update-value OPENAI_API_KEY --project yupp-llms \
    -e staging -e production --per-environment-secret
```

To dry run the command, you can use the `--dry-run` flag.

To pull secrets from GCP Secret Manager to a local .env file, you can use the `pull-local-secrets` command.

Example which updates your .env.local file with missing secrets (ignoring the --env-file flag updates the default .env
file):

```
python -m scripts.gcpsecrets pull-local-secrets --env-file .env.local --only-missing
```
"""

import os
from datetime import UTC, datetime
from getpass import getpass
from typing import Literal, cast, get_args

import click
import google.auth
import google.auth.exceptions
from google.cloud import secretmanager
from scripts.secret_env_var_map import (
    EnvVar,
    Service,
    add_new_env_var,
    format_secret_env_var_map,
    read_env_file,
    validate_and_load_secret_env_var_map,
    validate_env_var_is_not_set,
)

Environment = Literal["local", "staging", "production"]

# The service name, used as a prefix for all secrets to name space for that service.
# Secrets would look like
# * ym-<secret_name>-<environment>
# * sensitive-ym-<secret_name>-<environment>
SECRET_SERVICE_NAMESPACE = "ym"
# Default service name for now.
SERVICE_NAME = "yupp-mind"

# Create the Secret Manager client
client = secretmanager.SecretManagerServiceClient()


def create_secret(
    project_id: str,
    secret_id: str,
    secret_value: str,
    labels: dict[str, str],
    annotations: dict[str, str],
) -> None:
    """
    Upload a secret to Google Cloud Secret Manager.

    Args:
        project_id: Your Google Cloud project ID
        secret_id: The ID for the secret to create
        secret_value: The secret value to store
        labels: The labels to add to the secret
        annotations: The annotations to add to the secret
    """
    # Build the resource name of the parent project
    parent = f"projects/{project_id}"

    try:
        client.create_secret(
            request={
                "parent": parent,
                "secret_id": secret_id,
                "secret": {
                    "replication": {"automatic": {}},
                    "labels": labels,
                    "annotations": annotations,
                },
            }
        )
        print(f"Created secret: {secret_id}")
    except google.api_core.exceptions.AlreadyExists as e:
        print(f"Secret {secret_id} already exists")
        raise click.Abort() from e

    # Convert the secret value to bytes
    secret_value_bytes = secret_value.encode("UTF-8")

    response = client.add_secret_version(
        request={
            "parent": f"{parent}/secrets/{secret_id}",
            "payload": {"data": secret_value_bytes},
        }
    )

    print(f"Added secret version: {response.name}")


def update_secret_value(
    project_id: str,
    secret_id: str,
    secret_value: str,
) -> None:
    """
    Update a secret value in Google Cloud Secret Manager.
    """
    parent = f"projects/{project_id}"

    secret_value_bytes = secret_value.encode("UTF-8")

    response = client.add_secret_version(
        request={
            "parent": f"{parent}/secrets/{secret_id}",
            "payload": {"data": secret_value_bytes},
        }
    )

    print(f"Updated secret value: {response.name}")


def run(
    operation: Literal["create", "update"],
    env_var: str,
    project: str,
    secret_value: str | dict[Environment, str],
    dry_run: bool = True,
    sensitive: bool = False,
    environments: list[Environment] | None = None,
) -> dict[Environment, str]:
    secret_base_name = f"{SECRET_SERVICE_NAMESPACE}-{env_var.lower().replace('_', '-')}"
    labels = {"service": SERVICE_NAME}
    if sensitive:
        labels["sensitive"] = "true"
        secret_base_name = f"sensitive-{secret_base_name}"

    modified_secret_ids: dict[Environment, str] = {}
    target_environments = environments or ["local", "staging", "production"]

    for environment in target_environments:
        secret_id = f"{secret_base_name}-{environment}"
        labels["environment"] = environment
        annotations = labels.copy()
        annotations["env_variable_name"] = env_var

        if isinstance(secret_value, dict):
            env_secret_value = secret_value.get(environment)
            if env_secret_value is None:
                click.echo(f"Warning: No secret value provided for environment {environment}, skipping")
                continue
        else:
            env_secret_value = secret_value

        if dry_run:
            assert env_secret_value, f"Secret value for {env_var} in {environment} is empty"
            click.echo(f"Would upload secret {secret_id} for environment {environment}")
        else:
            if operation == "create":
                create_secret(
                    project_id=project,
                    secret_id=secret_id,
                    secret_value=env_secret_value,
                    labels=labels,
                    annotations=annotations,
                )
                modified_secret_ids[environment] = secret_id
            elif operation == "update":
                update_secret_value(
                    project_id=project,
                    secret_id=secret_id,
                    secret_value=env_secret_value,
                )
                modified_secret_ids[environment] = secret_id
    return modified_secret_ids


def check_credentials(require_user_credentials: bool = False) -> Literal["service_account", "user"]:
    """
    Check if application default credentials are available, valid, and optionally enforce user credentials.

    Args:
        require_user_credentials: If True, raises an error if service account credentials are detected.

    Returns:
        Literal["service_account", "user"]: The type of credentials being used.

    Raises:
        click.Abort: If credentials are missing, expired, or if service account is used when user
            credentials are required.
    """
    click.echo("Checking Google Cloud authentication...")
    try:
        # Get the credentials that will actually be used by the Secret Manager client
        credentials, project_id = google.auth.default()  # type: ignore[no-untyped-call]

        # Check if it's a service account
        is_service_account = hasattr(credentials, "service_account_email")
        if is_service_account:
            service_account_email = credentials.service_account_email
            if require_user_credentials:
                click.echo(
                    f"Error: Using service account credentials ({service_account_email}).",
                    err=True,
                )
                click.echo(
                    "Please use user credentials instead. Run 'gcloud auth application-default login'.",
                    err=True,
                )
                raise click.Abort()
            click.echo(f"Using service account: {service_account_email}")

        # Verify credentials can be refreshed (this catches expired credentials)
        if hasattr(credentials, "refresh"):
            try:
                from google.auth.transport.requests import Request

                credentials.refresh(Request())  # type: ignore[no-untyped-call]
            except google.auth.exceptions.RefreshError as e:
                error_msg = str(e).lower()
                if "reauthentication" in error_msg or "expired" in error_msg or "invalid_grant" in error_msg:
                    click.echo("Error: Credentials have expired and need to be refreshed.", err=True)
                    click.echo("Please run 'gcloud auth application-default login' to reauthenticate.", err=True)
                    raise click.Abort() from e
                # Re-raise other refresh errors
                raise
            except Exception as e:
                error_msg = str(e).lower()
                if "reauthentication" in error_msg or "gcloud auth application-default login" in error_msg:
                    click.echo("Error: Credentials have expired and need to be refreshed.", err=True)
                    click.echo("Please run 'gcloud auth application-default login' to reauthenticate.", err=True)
                    raise click.Abort() from e
                # For other errors, let them pass (might be service account keys that don't need refresh)

        if not is_service_account:
            click.echo(f"Authentication successful (project: {project_id or 'default'})")
        return "service_account" if is_service_account else "user"
    except google.auth.exceptions.DefaultCredentialsError as e:
        click.echo("Error: No Google Cloud credentials found.", err=True)
        click.echo(
            "Please run 'gcloud auth application-default login' to set up application default credentials.",
            err=True,
        )
        raise click.Abort() from e
    except click.Abort:
        # Re-raise Abort exceptions
        raise
    except Exception as e:
        click.echo(f"Error: Authentication failed - {e}", err=True)
        click.echo(
            "Please run 'gcloud auth application-default login' to set up application default credentials.",
            err=True,
        )
        raise click.Abort() from e


@click.group()
def cli() -> None:
    """CLI for managing secrets in Google Cloud Secret Manager."""


@cli.command()
@click.argument("env_var")
@click.option("--project", type=str, default="yupp-llms", help="The project to upload the secret to")
@click.option(
    "-e",
    "--env",
    type=click.Choice(get_args(Environment)),
    multiple=True,
    help=(
        "The environments to add the secret to. Use `-e local -e staging` to add the secret to local and staging."
        "If unset, the secret will be added to all the environments."
    ),
)
@click.option("--dry-run", is_flag=True, help="Dry run the command")
@click.option("--sensitive", is_flag=True, help="Mark the secret as sensitive (e.g. if it deals with payments)")
@click.option(
    "-s",
    "--service",
    type=click.Choice(get_args(Service)),
    multiple=True,
    default=["backend"],
    help="The services to attribute the secret to",
)
@click.option(
    "--per-environment-secret",
    is_flag=True,
    help="Create a separate secret for each environment. Accepts separate values for each environment.",
)
def add(
    env_var: str,
    project: str,
    env: list[Environment],
    dry_run: bool,
    sensitive: bool,
    service: list[Service],
    per_environment_secret: bool,
) -> None:
    """Add a new secret to Google Cloud Secret Manager."""
    check_credentials(require_user_credentials=True)

    click.echo(f"Adding secret for {env_var}")
    try:
        validate_env_var_is_not_set(env_var)
    except ValueError as e:
        click.echo(e)
        raise click.Abort() from e

    target_environments = env if env else ["local", "staging", "production"]

    if per_environment_secret:
        secret_value_dict: dict[Environment, str] = {}
        for environment in target_environments:
            env_secret = getpass(f"Enter value for {env_var} ({environment}): ")
            if not env_secret:
                click.echo(f"Warning: Empty value provided for {environment}, skipping this environment")
                continue
            secret_value_dict[environment] = env_secret
        if not secret_value_dict:
            click.echo("Error: No secret values provided for any environment")
            raise click.Abort()
        secret_value: str | dict[Environment, str] = secret_value_dict
    else:
        secret_value = getpass(f"Enter value for {env_var}: ")

    modified_secret_ids = run(
        operation="create",
        env_var=env_var,
        project=project,
        secret_value=secret_value,
        dry_run=dry_run,
        environments=env,
        sensitive=sensitive,
    )
    add_new_env_var(EnvVar(name=env_var, services=service, secret_names=modified_secret_ids))
    click.echo("Secret added successfully!")


@cli.command()
@click.argument("env_var")
@click.option("--project", type=str, default="yupp-llms", help="The project to upload the secret to")
@click.option(
    "-e",
    "--env",
    type=click.Choice(get_args(Environment)),
    multiple=True,
    required=True,
    help=(
        "The environments to update the secret in. Use `-e local -e staging` to update the secret in local and staging."
    ),
)
@click.option("--dry-run", is_flag=True, help="Dry run the command")
@click.option("--sensitive", is_flag=True, help="Mark the secret as sensitive (e.g. if it deals with payments)")
@click.option(
    "--per-environment-secret",
    is_flag=True,
    help="Update with separate values for each environment. Accepts separate values for each environment.",
)
def update_value(
    env_var: str,
    project: str,
    env: list[Environment],
    dry_run: bool,
    sensitive: bool,
    per_environment_secret: bool,
) -> None:
    """Update the value of an existing secret in Google Cloud Secret Manager."""
    check_credentials(require_user_credentials=True)

    click.echo(f"Updating secret for {env_var}")

    if per_environment_secret:
        secret_value_dict: dict[Environment, str] = {}
        for environment in env:
            env_secret = getpass(f"Enter new value for {env_var} ({environment}): ")
            if not env_secret:
                click.echo(f"Warning: Empty value provided for {environment}, skipping this environment")
                continue
            secret_value_dict[environment] = env_secret
        if not secret_value_dict:
            click.echo("Error: No secret values provided for any environment")
            raise click.Abort()
        secret_value: str | dict[Environment, str] = secret_value_dict
    else:
        secret_value = getpass(f"Enter new value for {env_var}: ")

    run(
        operation="update",
        env_var=env_var,
        project=project,
        secret_value=secret_value,
        dry_run=dry_run,
        environments=env,
        sensitive=sensitive,
    )
    click.echo("Secret value updated successfully!")


@cli.command()
def verify_credentials() -> None:
    """Verifies that gcloud authentication is working and checks credential type."""
    credential_type = check_credentials(require_user_credentials=False)
    if credential_type == "service_account":
        click.echo("You are using a service account for authentication.")
        click.echo(
            "If you're adding secrets, consider using user credentials instead by running "
            "'gcloud auth application-default login'."
        )
        if os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
            click.echo(
                "Your environment has GOOGLE_APPLICATION_CREDENTIALS set, please unset it before running this command, "
                "if you're adding secrets."
            )
    else:
        click.echo("You are using user credentials (not a service account). This is recommended for adding secrets.")


@cli.command()
def format_yaml() -> None:
    """Format the secret env var map."""
    format_secret_env_var_map()


@cli.command()
def validate_yaml() -> None:
    """Validate the secret env var map yaml file."""
    validate_and_load_secret_env_var_map()


@cli.command()
@click.option("--project", type=str, default="yupp-llms", help="The project to download the secrets from")
@click.option("--only-missing", is_flag=True, help="Only fetch secrets that are not already set in the local .env file")
@click.option("--env-file", type=str, default=".env", help="The environment file to update")
@click.option("--verbose", is_flag=True, default=False, help="Verbose output")
def pull_local_secrets(project: str, only_missing: bool, env_file: str, verbose: bool) -> None:
    """Fetch the secrets for local environment."""
    _pull_secrets(
        project, environment="local", service="all", only_missing=only_missing, env_file=env_file, verbose=verbose
    )


@cli.command()
@click.option("--project", type=str, default="yupp-llms", help="The project to download the secrets from")
@click.option(
    "--environment",
    type=click.Choice(get_args(Environment)),
    default="local",
    help="The environment to download the secrets from",
)
@click.option(
    "--service",
    type=click.Choice(get_args(Service) + ("all",)),
    default="all",
    help="The service to download the secrets for",
)
@click.option("--only-missing", is_flag=True, help="Only fetch secrets that are not already set in the local .env file")
@click.option("--env-file", type=str, default=".env", help="The environment file to update")
@click.option("--verbose", is_flag=True, default=False, help="Verbose output")
def pull_secrets(
    project: str, environment: str, service: str, only_missing: bool, env_file: str, verbose: bool
) -> None:
    """Fetch the secrets for given environment."""
    _pull_secrets(
        project,
        cast(Environment, environment),
        cast(Service | Literal["all"], service),
        only_missing,
        env_file,
        verbose,
    )


def _pull_secrets(
    project: str,
    environment: Environment,
    service: Service | Literal["all"],
    only_missing: bool,
    env_file: str,
    verbose: bool,
) -> None:
    yaml_data = validate_and_load_secret_env_var_map()
    existing_env_vars = read_env_file(env_file)
    new_env_vars = []
    for env_var in yaml_data.env_vars:
        if environment not in env_var.secret_names:
            continue
        env_name = env_var.name
        if only_missing and env_name in existing_env_vars:
            if verbose:
                click.echo(f"Secret {env_name} already exists in the {environment} .env file, skipping")
            continue
        if service != "all" and service not in env_var.services:
            continue
        secret_name = env_var.secret_names[environment]

        if verbose:
            click.echo(f"Pulling secret {env_name} for {environment} environment")
        try:
            secret_status = client.get_secret_version(name=f"projects/{project}/secrets/{secret_name}/versions/latest")
        except google.api_core.exceptions.NotFound:
            if verbose:
                click.echo(f"Secret {secret_name} not found, skipping")
            continue
        if secret_status.state == secretmanager.SecretVersion.State.DISABLED:
            if verbose:
                click.echo(f"Secret {secret_name} is disabled, skipping")
            continue
        secret_value = client.access_secret_version(name=f"projects/{project}/secrets/{secret_name}/versions/latest")
        secret_value_str = secret_value.payload.data.decode("utf-8")
        # Ignore if the secret value contains newlines, not sure if they would work.
        # Maybe test it out?
        if "\n" in secret_value_str:
            if verbose:
                click.echo(f"Secret {env_name} is a multi-line secret, skipping")
            continue
        new_env_vars.append(f"{env_name}={secret_value_str}")
    if not new_env_vars:
        click.echo("No new secrets to pull")
        return
    with open(env_file, "a") as f:
        date_str = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
        f.write(f"\n\n# -- START: Pulled from GCP Secret Manager: {date_str}\n")
        f.write("\n".join(new_env_vars))
        f.write(f"\n# -- END: Pulled from GCP Secret Manager: {date_str}\n")
    click.echo(f"Pulled {len(new_env_vars)} secrets for {environment} environment")


if __name__ == "__main__":
    cli()
