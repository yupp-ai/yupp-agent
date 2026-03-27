FROM gcr.io/yupp-llms/agent-base-py312:latest

# set work directory
WORKDIR /app

# Environment variables
ENV PYTHONPATH=/app \
    GRPC_DNS_RESOLVER=native \
    PYTHONUNBUFFERED=1

# Copy dependency files (needed for poetry build)
COPY ./pyproject.toml ./poetry.lock* ./README.md /app/

# Copy application code and data
COPY ./ypl/ /app/ypl/
COPY ./scripts/ /app/scripts/
COPY ./data/ /app/data/
# Skills are registered as MCP resources by ypl/mcp_server/mcp_tools.py
COPY ./.agents/skills/ /app/.agents/skills/

# Copy .env file too, which is generated earlier in the build process,
# and has some build variables that get logged on server startup.
COPY .env /app/.env

# Sync dependencies (updates only if poetry.lock changed since base build)
# Then build and install the package
RUN set -e && \
    # Install build dependencies if needed (some packages may need rebuilding)
    apt-get update && \
    apt-get install -y --no-install-recommends \
        cmake \
        g++ \
        make && \
    # Sync dependencies to match poetry.lock exactly
    echo "Syncing dependencies with poetry.lock..." && \
    poetry install --no-root --without dev --compile && \
    # Build and install the package (without dependencies, already handled above)
    echo "Building package..." && \
    poetry build && \
    pip install --no-cache-dir --no-deps dist/*.whl && \
    # Remove build dependencies to reduce image size
    apt-get purge -y --auto-remove cmake g++ make && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* && \
    rm -rf dist/ build/ ~/.cache /root/.cache /tmp/*


RUN python -m compileall /app -q -f

# Verify installation
RUN python -c "import ypl; print('Package installed successfully')"

# Set executable permissions for all entrypoint scripts
RUN find /app/ypl -type f -name "*entrypoint.sh" -exec chmod +x {} +

EXPOSE 8080

# Default: AHS entrypoint (overridden per-service in deploy)
ENV SCRIPT_TO_RUN=/app/ypl/agent_harness_service/entrypoint.sh

# Use shell form to allow for environment variable expansion
ENTRYPOINT exec $SCRIPT_TO_RUN
