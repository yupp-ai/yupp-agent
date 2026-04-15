#!/usr/bin/env bash
# Start the full agent platform locally from source (no Docker build needed).
#
# Prerequisites:
#   - Postgres reachable (default: localhost:5432)
#   - Redis reachable (default: localhost:6379)
#   - .env file in the repo root with required secrets
#   - Poetry environment set up (poetry install)
#
# Usage:
#   ./scripts/run_local.sh                                    # Start monolith on :8090
#   ./scripts/run_local.sh --e2e                              # Use .env.e2e instead of .env
#   ./scripts/run_local.sh --env-file=.env.staging            # Use a custom env file
#   ./scripts/run_local.sh --streamlit                        # Also start Streamlit on :8501
#   ./scripts/run_local.sh --no-interactive                   # Never prompt, fail on error
#   ./scripts/run_local.sh --pg-host=mydb --pg-port=5433      # Custom Postgres
#   ./scripts/run_local.sh --redis-host=myredis --redis-port=6380  # Custom Redis
#
# Port map:
#   8090 — Monolith (AHS + MCP + SAG gateways)
#   8501 — Streamlit dashboards (optional)
#
# Press Ctrl+C to stop all services.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

# ---------------------------------------------------------------------------
# Activate conda environment (required for poetry to find the right Python)
# ---------------------------------------------------------------------------
if [ -z "${CONDA_DEFAULT_ENV:-}" ] || [ "$CONDA_DEFAULT_ENV" != "ys-dev" ]; then
    CONDA_SH=""
    # Check common conda.sh locations
    for candidate in \
        "${CONDA_PREFIX:-}/etc/profile.d/conda.sh" \
        "/opt/homebrew/Caskroom/miniforge/base/etc/profile.d/conda.sh" \
        "$HOME/miniforge3/etc/profile.d/conda.sh" \
        "$HOME/miniconda3/etc/profile.d/conda.sh" \
        "$HOME/anaconda3/etc/profile.d/conda.sh" \
        "/usr/local/Caskroom/miniforge/base/etc/profile.d/conda.sh"; do
        if [ -f "$candidate" ]; then
            CONDA_SH="$candidate"
            break
        fi
    done

    if [ -n "$CONDA_SH" ]; then
        # shellcheck disable=SC1090
        source "$CONDA_SH"
        conda activate ys-dev
    elif command -v conda &>/dev/null; then
        eval "$(conda shell.bash hook)"
        conda activate ys-dev
    else
        echo "ERROR: conda not found and ys-dev environment not active."
        echo "  Run: conda activate ys-dev"
        exit 1
    fi
fi

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# ---------------------------------------------------------------------------
# Parse arguments
# ---------------------------------------------------------------------------
STREAMLIT=false
NO_INTERACTIVE=false
ENV_FILE=".env"
BACKGROUND=false
PG_HOST=localhost
PG_PORT=5432
REDIS_HOST=localhost
REDIS_PORT=6379

for arg in "$@"; do
    case "$arg" in
        --streamlit) STREAMLIT=true ;;
        --no-interactive) NO_INTERACTIVE=true ;;
        --e2e) ENV_FILE=".env.e2e" ;;
        --env-file=*) ENV_FILE="${arg#*=}" ;;
        --background) BACKGROUND=true ;;
        --pg-host=*) PG_HOST="${arg#*=}" ;;
        --pg-port=*) PG_PORT="${arg#*=}" ;;
        --redis-host=*) REDIS_HOST="${arg#*=}" ;;
        --redis-port=*) REDIS_PORT="${arg#*=}" ;;
        --help|-h)
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --streamlit          Also start Streamlit dashboards on port 8501"
            echo "  --e2e                Shorthand for --env-file=.env.e2e"
            echo "  --env-file=PATH      Use a custom env file instead of .env (default: .env)"
            echo "  --background         Run monolith in background (logs to /tmp/monolith_local.log)"
            echo "  --no-interactive     Never prompt or auto-start services; fail on error"
            echo "  --pg-host=HOST       Postgres host (default: localhost)"
            echo "  --pg-port=PORT       Postgres port (default: 5432)"
            echo "  --redis-host=HOST    Redis host (default: localhost)"
            echo "  --redis-port=PORT    Redis port (default: 6379)"
            exit 0
            ;;
        *)
            echo "Unknown argument: $arg (use --help for usage)"
            exit 1
            ;;
    esac
done

# Are we using default localhost settings? (determines if we can auto-start docker-compose)
IS_DEFAULT_CONFIG=false
if [ "$PG_HOST" = "localhost" ] && [ "$PG_PORT" = "5432" ] && [ "$REDIS_HOST" = "localhost" ] && [ "$REDIS_PORT" = "6379" ]; then
    IS_DEFAULT_CONFIG=true
fi

# Detect docker compose command (v2 plugin vs v1 standalone)
DOCKER_COMPOSE=""
if docker compose version &>/dev/null 2>&1; then
    DOCKER_COMPOSE="docker compose"
elif command -v docker-compose &>/dev/null; then
    DOCKER_COMPOSE="docker-compose"
fi

# Track background PIDs for cleanup
PIDS=()
cleanup() {
    echo ""
    echo -e "${YELLOW}Shutting down...${NC}"
    if [ ${#PIDS[@]} -gt 0 ]; then
        for pid in "${PIDS[@]}"; do
            kill "$pid" 2>/dev/null || true
        done
    fi
    wait 2>/dev/null || true
    echo -e "${GREEN}All services stopped.${NC}"
}
trap cleanup EXIT INT TERM

# ---------------------------------------------------------------------------
# Step 1: Check Postgres and Redis
# ---------------------------------------------------------------------------
echo -e "${YELLOW}[1/4] Checking dependencies...${NC}"

PG_OK=false
REDIS_OK=false

pg_isready -h "$PG_HOST" -p "$PG_PORT" -q 2>/dev/null && PG_OK=true
if [ "$PG_OK" = true ]; then
    echo -e "  Postgres ($PG_HOST:$PG_PORT) ... ${GREEN}running${NC}"
else
    echo -e "  Postgres ($PG_HOST:$PG_PORT) ... ${RED}not reachable${NC}"
fi

redis-cli -h "$REDIS_HOST" -p "$REDIS_PORT" ping &>/dev/null && REDIS_OK=true
if [ "$REDIS_OK" = true ]; then
    echo -e "  Redis ($REDIS_HOST:$REDIS_PORT)    ... ${GREEN}running${NC}"
else
    echo -e "  Redis ($REDIS_HOST:$REDIS_PORT)    ... ${RED}not reachable${NC}"
fi

# If something's down, decide what to do
if [ "$PG_OK" = false ] || [ "$REDIS_OK" = false ]; then
    if [ "$NO_INTERACTIVE" = true ]; then
        echo ""
        echo -e "${RED}Required services not reachable. Exiting (--no-interactive mode).${NC}"
        [ "$PG_OK" = false ] && echo "  - Postgres not reachable at $PG_HOST:$PG_PORT"
        [ "$REDIS_OK" = false ] && echo "  - Redis not reachable at $REDIS_HOST:$REDIS_PORT"
        echo ""
        echo "Start them with: docker compose up -d"
        exit 1
    elif [ "$IS_DEFAULT_CONFIG" = true ] && [ -n "$DOCKER_COMPOSE" ]; then
        echo ""
        echo -e "  ${YELLOW}Starting Postgres and Redis via $DOCKER_COMPOSE...${NC}"
        $DOCKER_COMPOSE up -d 2>&1 | sed 's/^/  /'

        # Wait for services to be ready (up to 30s)
        echo -e "  Waiting for services..."
        for i in $(seq 1 30); do
            pg_isready -h localhost -p 5432 -q 2>/dev/null && PG_OK=true || true
            redis-cli -h localhost -p 6379 ping &>/dev/null && REDIS_OK=true || true
            if [ "$PG_OK" = true ] && [ "$REDIS_OK" = true ]; then
                echo -e "  Postgres (localhost:5432) ... ${GREEN}running${NC}"
                echo -e "  Redis (localhost:6379)    ... ${GREEN}running${NC}"
                break
            fi
            if [ "$i" -eq 30 ]; then
                echo -e "  ${RED}Timed out waiting for services to start.${NC}"
                echo "  Check: $DOCKER_COMPOSE logs"
                exit 1
            fi
            sleep 1
        done
    elif [ "$IS_DEFAULT_CONFIG" = true ]; then
        echo ""
        echo -e "${RED}Services not running and docker compose not found.${NC}"
        echo "  Install Docker, or start Postgres/Redis manually:"
        echo "    brew services start postgresql@16"
        echo "    brew services start redis"
        exit 1
    else
        echo ""
        echo -e "${RED}Required services not reachable at custom addresses.${NC}"
        [ "$PG_OK" = false ] && echo "  - Postgres not reachable at $PG_HOST:$PG_PORT"
        [ "$REDIS_OK" = false ] && echo "  - Redis not reachable at $REDIS_HOST:$REDIS_PORT"
        echo ""
        echo "  Verify your --pg-host/--pg-port and --redis-host/--redis-port settings."
        exit 1
    fi
fi

# ---------------------------------------------------------------------------
# Step 2: Check .env and required variables
# ---------------------------------------------------------------------------
echo -e "${YELLOW}[2/4] Checking .env...${NC}"

if [ ! -f .env ]; then
    echo -e "  .env file ... ${YELLOW}not found, creating with local defaults${NC}"
    touch .env
fi

# Ensure required variables are present (append if missing)
REQUIRED_VARS_ADDED=false

if ! grep -q "^ENVIRONMENT=" .env 2>/dev/null; then
    echo 'ENVIRONMENT=local' >> .env
    REQUIRED_VARS_ADDED=true
fi

if ! grep -q "^DEFAULT_DB=" .env 2>/dev/null; then
    echo 'DEFAULT_DB=agentdb' >> .env
    REQUIRED_VARS_ADDED=true
fi

if ! grep -q "^POSTGRES_CONNECTION_AGENTDB=" .env 2>/dev/null; then
    echo "POSTGRES_CONNECTION_AGENTDB={\"user\":\"postgres\",\"password\":\"postgres\",\"host\":\"$PG_HOST:$PG_PORT\",\"database\":\"yupp_agent\"}" >> .env
    REQUIRED_VARS_ADDED=true
fi

if ! grep -q "^REDIS_URL=" .env 2>/dev/null; then
    echo "REDIS_URL=redis://$REDIS_HOST:$REDIS_PORT/1" >> .env
    REQUIRED_VARS_ADDED=true
fi

if ! grep -q "^AGENT_HARNESS_SERVICE_API_KEY=" .env 2>/dev/null; then
    echo 'AGENT_HARNESS_SERVICE_API_KEY=test-local-key' >> .env
    REQUIRED_VARS_ADDED=true
fi

if [ "$REQUIRED_VARS_ADDED" = true ]; then
    echo -e "  .env file ... ${YELLOW}added missing local defaults${NC}"
    echo -e "  (edit .env to add API keys for full functionality)"
else
    echo -e "  .env file ... ${GREEN}found${NC}"
fi

# ---------------------------------------------------------------------------
# Step 3: Run migrations
# ---------------------------------------------------------------------------
echo -e "${YELLOW}[3/4] Running database migrations...${NC}"

if poetry run alembic -c alembic.ini upgrade head 2>&1; then
    echo -e "  Migrations ... ${GREEN}done${NC}"
else
    echo -e "  Migrations ... ${RED}failed${NC}"
    echo ""
    echo "  To fix, try one of:"
    if [ -n "$DOCKER_COMPOSE" ]; then
        echo "    a) Reset database:     $DOCKER_COMPOSE down -v && $DOCKER_COMPOSE up -d && $0"
    else
        echo "    a) Reset database:     docker compose down -v && docker compose up -d && $0"
    fi
    echo "    b) Manual downgrade:   poetry run alembic downgrade base && poetry run alembic upgrade head"
    echo "    c) Check migration:    poetry run alembic history --verbose"
    exit 1
fi

# ---------------------------------------------------------------------------
# Step 4: Start services
# ---------------------------------------------------------------------------
echo -e "${YELLOW}[4/4] Starting services...${NC}"

# Source the env file — defaults to .env, overridden by --env-file or --e2e
if [ "$ENV_FILE" != ".env" ]; then
    # Resolve to absolute path so Python can find it regardless of CWD
    case "$ENV_FILE" in
        /*) ENV_PATH="$ENV_FILE" ;;
        *)  ENV_PATH="$REPO_ROOT/$ENV_FILE" ;;
    esac
    if [ -f "$ENV_PATH" ]; then
        echo -e "  ${YELLOW}Loading env file: $ENV_PATH${NC}"
        set -a
        # shellcheck disable=SC1090
        source "$ENV_PATH"
        set +a
        export DOTENV_PATH="$ENV_PATH"
    else
        echo -e "  ${RED}$ENV_FILE not found (resolved to $ENV_PATH)${NC}"
        exit 1
    fi
fi

# Export env vars for local development (don't override if already set by .env.e2e)
export ENVIRONMENT="${ENVIRONMENT:-local}"
export SANDBOX_ENABLED="${SANDBOX_ENABLED:-false}"
export USE_GOOGLE_CLOUD_LOGGING="${USE_GOOGLE_CLOUD_LOGGING:-false}"
export DISABLE_WRITE_GOOGLE_CLOUD_METRICS="${DISABLE_WRITE_GOOGLE_CLOUD_METRICS:-true}"
export PYTHONPATH="$REPO_ROOT"

# Start Streamlit in background if requested
if [ "$STREAMLIT" = true ]; then
    echo -e "  Starting Streamlit on port 8501..."
    poetry run streamlit run ypl/streamlit_server/app.py \
        --server.port 8501 \
        --server.headless true \
        &>/tmp/streamlit_local.log &
    PIDS+=($!)
    echo -e "  Streamlit ... ${GREEN}started${NC} (logs: /tmp/streamlit_local.log)"
fi

MONOLITH_LOG="/tmp/monolith_local.log"

echo -e "  Starting monolith on port 8090..."
echo ""
echo -e "  ${GREEN}AHS + MCP + SAG running at http://localhost:8090${NC}"
echo -e "  Health check: ${GREEN}http://localhost:8090/health${NC}"
echo -e "  API docs:     ${GREEN}http://localhost:8090/docs${NC}"
if [ "$STREAMLIT" = true ]; then
    echo -e "  Streamlit:    ${GREEN}http://localhost:8501${NC}"
fi
echo -e "  Logs:         ${GREEN}$MONOLITH_LOG${NC}"

if [ "$BACKGROUND" = true ]; then
    echo ""
    echo -e "  ${YELLOW}Running in background mode...${NC}"

    # Start in background, tee output to log file
    poetry run uvicorn ypl.mono_server.server:app \
        --host 0.0.0.0 \
        --port 8090 \
        --log-level info \
        > "$MONOLITH_LOG" 2>&1 &
    MONOLITH_PID=$!
    echo "$MONOLITH_PID" > /tmp/monolith_local.pid

    # Wait for health check
    for i in $(seq 1 30); do
        if curl -sf http://localhost:8090/health > /dev/null 2>&1; then
            echo -e "  ${GREEN}Monolith started (PID $MONOLITH_PID)${NC}"
            echo -e "  To stop: kill $MONOLITH_PID (or: kill \$(cat /tmp/monolith_local.pid))"
            echo -e "  To view logs: tail -f $MONOLITH_LOG"
            exit 0
        fi
        if ! kill -0 "$MONOLITH_PID" 2>/dev/null; then
            echo -e "  ${RED}Monolith crashed during startup. Check logs:${NC}"
            tail -20 "$MONOLITH_LOG"
            exit 1
        fi
        sleep 1
    done
    echo -e "  ${RED}Monolith did not become healthy within 30s. Check logs:${NC}"
    tail -20 "$MONOLITH_LOG"
    exit 1
else
    echo ""
    echo -e "  Press Ctrl+C to stop."
    echo ""

    # Start monolith in foreground, tee to log file
    poetry run uvicorn ypl.mono_server.server:app \
        --host 0.0.0.0 \
        --port 8090 \
        --log-level info \
        2>&1 | tee "$MONOLITH_LOG"
fi
