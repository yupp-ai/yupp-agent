FROM python:3.12-slim

WORKDIR /app

ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    GRPC_DNS_RESOLVER=native

# Install poetry
RUN pip install --no-cache-dir poetry==1.8.5 && \
    poetry config virtualenvs.create false

# Dependency layer — cached until pyproject.toml / poetry.lock change.
COPY pyproject.toml poetry.lock README.md alembic.ini /app/
RUN set -e && \
    apt-get update && \
    apt-get install -y --no-install-recommends cmake g++ git make && \
    poetry install --no-root --without dev --no-interaction --no-ansi --compile && \
    # Poetry itself has no runtime role; drop it (and its now-orphaned deps,
    # notably the keyring 24.x it pins which clashes with the project's
    # keyring 25.x and makes `pip check` further down fail).
    pip uninstall -y poetry poetry-core poetry-plugin-export && \
    apt-get purge -y --auto-remove cmake g++ make && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* ~/.cache /root/.cache /tmp/* /var/tmp/* && \
    find /usr/local/lib/python3.12/site-packages -type d -name "tests" -exec rm -rf {} + 2>/dev/null || true

# Application code + data. ``.env`` is injected at runtime (docker-compose
# ``env_file:`` / systemd ``EnvironmentFile=``) — never bake it into the image.
COPY ./ypl/ /app/ypl/
COPY ./scripts/ /app/scripts/
COPY ./data/ /app/data/
# Skills are registered as MCP resources by ypl/mcp_server/mcp_tools.py
COPY ./.agents/skills/ /app/.agents/skills/

# Sub-app: artifact-viewer.  Path B (mono-image) — install into the same
# system Python.  Bare-metal VMs keep their separate .venv (see
# apps/artifact-viewer/README.md).  ``pip check`` runs at the end to catch
# any unsatisfied dep / version conflict between the main Poetry env and
# the sub-app's pyproject.
COPY ./apps/artifact-viewer/ /app/apps/artifact-viewer/
RUN pip install --no-cache-dir -e /app/apps/artifact-viewer/ && \
    pip check

RUN python -m compileall /app/ypl /app/apps -q -f && \
    python -c "import ypl; print('Package installed successfully')" && \
    python -c "import artifact_viewer; print('Artifact viewer installed successfully')"

# Make every entrypoint shell script executable so operators can point
# SCRIPT_TO_RUN at whichever one they want without chmod-ing by hand.
RUN find /app/ypl /app/apps -type f -name "*entrypoint.sh" -exec chmod +x {} +

EXPOSE 8090 8095

# Default: the monolith (AHS + SAG + MCP in one process). Override
# SCRIPT_TO_RUN (e.g. to streamlit_server_entrypoint.sh or
# apps/artifact-viewer/entrypoint.sh) for other services.
ENV SCRIPT_TO_RUN=/app/ypl/mono_server/entrypoint.sh

# Shell form so the env var is expanded at container start.
ENTRYPOINT exec $SCRIPT_TO_RUN
