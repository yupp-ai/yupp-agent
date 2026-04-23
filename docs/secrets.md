# Secrets Management

yupp-agent does not bundle a runtime secret-manager client. It reads every
secret from **environment variables** (or the `.env` file they come from),
loaded into pydantic-settings at process startup.

Rotating a secret therefore means:

1. Put the new value somewhere your deploy pipeline can see it.
2. Render it into `.env` (or export it as an env var) on the host.
3. Restart the service.

This makes yupp-agent cloud-agnostic: whatever secret store you prefer, you
only have to wire it up *once* in your boot script — everything downstream
is just env vars.

## Where secrets live inside the app

| Secret | Where the app reads it | How it gets there |
|---|---|---|
| Per-agent Slack `bot_token`, `signing_secret` | `slack_agents` row (encrypted) — see below | Written by BotFather's OAuth flow, or manually `UPDATE` |
| BotFather refresh token | `slack_oauth_tokens` row (encrypted) | Seeded once from `SLACK_BOT_FATHER_APP_CONFIG_REFRESH_TOKEN`, then rotated in place |
| Anthropic / OpenAI / Slack webhook / Postgres / … | `.env` → `settings.<FIELD>` | Your boot step (see recipes) |

The DB-resident Slack secrets are encrypted at rest with
`SLACK_AGENT_GW_ENCRYPTION_KEY` (a Fernet key). If a `slack_agents` row's
encrypted columns are null (e.g. the row was inserted manually rather than
via BotFather), the gateway falls back to `SLACK_AGENT_GATEWAY_<BOT_NAME>_BOT_TOKEN`
/ `_SIGNING_SECRET` env vars — handy when you already have a
Vault/SSM/k8s-secret flow and don't want to copy the token into the DB.

## Recipes for populating `.env`

### Plain `.env` on a VM

```bash
# /data/ahs/.env — maintained by hand, chmod 600, owned by the service user.
ANTHROPIC_API_KEY=sk-ant-...
POSTGRES_CONNECTION_AGENTDB={"user":"yupp","password":"...","host":"127.0.0.1:5432","database":"yadb"}
SLACK_AGENT_GW_ENCRYPTION_KEY=...
...
```

systemd picks it up automatically via `EnvironmentFile=/data/ahs/.env`
in `deploy/systemd/ahs-mono.service`.

### GCP Secret Manager → `.env`

```bash
# /opt/yupp-agent/refresh-env.sh — run before starting the service,
# e.g. from ExecStartPre= in the systemd unit.
#
# Assumes each GCP secret is named the same as its env var
# (use `gcloud secrets list` to confirm).

: > /data/ahs/.env
chmod 600 /data/ahs/.env
for key in ANTHROPIC_API_KEY POSTGRES_CONNECTION_AGENTDB SLACK_AGENT_GW_ENCRYPTION_KEY ...; do
  value=$(gcloud secrets versions access latest --secret="$key")
  printf '%s=%s\n' "$key" "$value" >> /data/ahs/.env
done
```

### AWS SSM Parameter Store → `.env`

```bash
# Assumes /yupp-agent/<KEY> SecureString parameters, one per env var.
aws ssm get-parameters-by-path \
    --path /yupp-agent/ \
    --with-decryption \
    --recursive \
  | jq -r '.Parameters[] | "\(.Name | sub("^/yupp-agent/"; ""))=\(.Value)"' \
  > /data/ahs/.env
chmod 600 /data/ahs/.env
```

### HashiCorp Vault KV v2 → `.env`

```bash
# Assumes secret/yupp-agent is a KV v2 map of env var names to values.
vault kv get -format=json secret/yupp-agent \
  | jq -r '.data.data | to_entries[] | "\(.key)=\(.value)"' \
  > /data/ahs/.env
chmod 600 /data/ahs/.env
```

Or use the [Vault Agent](https://developer.hashicorp.com/vault/docs/agent-and-proxy/agent/template)
to render a template file directly and skip the shell loop.

### Kubernetes `Secret` → env vars

Just mount the Secret with `envFrom:`:

```yaml
env:
  - name: SLACK_AGENT_GW_ENCRYPTION_KEY
    valueFrom:
      secretKeyRef: { name: yupp-agent, key: SLACK_AGENT_GW_ENCRYPTION_KEY }
envFrom:
  - secretRef: { name: yupp-agent }
```

No `.env` rendering needed; pydantic-settings picks the values straight out
of the process environment.

## Rotating `SLACK_AGENT_GW_ENCRYPTION_KEY`

The Fernet key used for per-agent tokens is **not** rotatable in place today.
To rotate:

1. Decrypt all `slack_agents.bot_token_encrypted` / `signing_secret_encrypted`
   rows with the old key.
2. Re-encrypt with the new key.
3. Swap the env var and restart.

Small deployments can do this with an ad-hoc script; a one-off migration
command can be added when this becomes common enough to warrant it.

## What this does NOT do

- **Runtime secret refresh.** All secrets are read once at startup. Restart
  the process to pick up a new value.
- **Secret versioning inside the app.** Use your secret store's versioning;
  yupp-agent only sees the resolved value.
- **Audit logging of secret reads.** Same — happens one level up in your
  secret store.
