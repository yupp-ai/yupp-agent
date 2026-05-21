FROM python:3.12-slim

WORKDIR /app

ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    GRPC_DNS_RESOLVER=native

# Install poetry
RUN pip install --no-cache-dir poetry==1.8.5 && \
    poetry config virtualenvs.create false

# Dependency layer — cached until pyproject.toml / poetry.lock change.
COPY pyproject.toml poetry.lock README.md /app/
RUN set -e && \
    apt-get update && \
    # `cmake g++ make` are build-only and purged below.
    # `graphviz` is kept in the runtime image — it provides the `dot` binary
    # used by SAG's rich-content rendering pipeline (DOT fence → PNG).
    apt-get install -y --no-install-recommends cmake g++ git make graphviz && \
    poetry install --no-root --without dev --no-interaction --no-ansi --compile && \
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

RUN python -m compileall /app -q -f && \
    python -c "import ypl; print('Package installed successfully')"

# Make every entrypoint shell script executable so operators can point
# SCRIPT_TO_RUN at whichever one they want without chmod-ing by hand.
RUN find /app/ypl -type f -name "*entrypoint.sh" -exec chmod +x {} +

EXPOSE 8090

# Default: the monolith (AHS + SAG + MCP in one process). Override
# SCRIPT_TO_RUN (e.g. to streamlit_server_entrypoint.sh) for other services.
ENV SCRIPT_TO_RUN=/app/ypl/mono_server/entrypoint.sh

# Shell form so the env var is expanded at container start.
ENTRYPOINT exec $SCRIPT_TO_RUN
