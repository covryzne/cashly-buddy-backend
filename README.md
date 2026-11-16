# cashly-buddy-backend

Latest local workflow: [LOCAL_DEVELOPMENT.md](docs/LOCAL_DEVELOPMENT.md) (ADR-0068).
Supabase CLI/config lives only in this repository; Supabase runs in Docker while
FE/BE are served separately. Local email/password accounts activate immediately;
Google/SMTP are unused in this local flow. Existing private env files were preserved.

Task 01/02: FastAPI foundation, branch-selected configuration, SQLAlchemy sessions,
and an initial Alembic migration for private core storage. Google-first Auth now
implements POST `/api/v1/auth/bootstrap`, GET `/api/v1/me` and bounded GET
`/api/v1/expenses`. Tokens are validated through Supabase Auth; application
ownership remains server-side. Local email/password routes in Next.js are also
implemented (ADR-0068); hosted email/SMTP and finance writes remain deferred.
Remote Supabase has not been
inspected or migrated by the agent. Design source: `../specs/README.md`, ADR-0062,
ADR-0063, ADR-0064 and ADR-0066. Operator setup:
`../cashly-buddy-frontend/GOOGLE_OAUTH_SETUP.md`. `DEV_SAMPLE_DATA=true`
seeds fictional expenses once per account in dev only, during authenticated bootstrap.

## Conda quickstart (CMD)

Use your existing `cashly` Conda environment; no nested venv is needed. From the
backend root (if currently in its `src` folder, run `cd ..` first):

```cmd
conda activate cashly
python -m pip install -r requirements.txt
python -m pip install --no-deps -e .
python -m cashly dev
```

`requirements.txt` delegates to `requirements.lock` so there is one set of pins.
Do not overwrite an existing filled env file with an example.

## Local setup (PowerShell)

Python 3.12 is the tested version. From this backend repository:

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.lock
.venv/Scripts/python.exe -m pip install --no-deps -e .
Copy-Item .env.dev.example .env.dev
# Edit .env.dev locally; connection fields may stay empty for Task 01.
.venv/Scripts/python.exe -m cashly check
.venv/Scripts/python.exe -m cashly dev
```

The lock includes runtime and development tools with transitive pins; the build
backend is pinned in `pyproject.toml`. No global Python packages are changed.
Default local URL: `http://127.0.0.1:8000/api/v1/health`; Swagger: `/docs`.
Do not commit filled env files. The agent must not read private env files; only
examples and synthetic temporary test files may be inspected during this task.

## Environment selection

| Source | Priority / mapping |
|---|---|
| `--app-env dev\|staging\|prod` | Highest priority |
| `CASHLY_ENV` | Explicit process/deployment selection |
| CI branch metadata | GitHub, GitLab, Vercel, Azure; PR head before target |
| Local Git branch | `dev/develop/development` and feature branches → dev; `stage/staging` → staging; `main/master/prod/production` → prod |

File source loads exactly `.env.dev`, `.env.staging` or `.env.prod` from the
backend root, independent of the current working directory. A missing selected
file fails; no cross-environment fallback. Detached HEAD, tags and no-Git
deployments require explicit environment selection. Restart after changing env
files or branches; no automatic branch checkout or hot switching of credentials.

Process variables override file values. Selector variables inside a file cannot
change the selected environment/source. `${...}` interpolation is disabled.
Legacy `.env`, `.env.local`, `.env.development*`, `.env.production*` and
`.env.staging.local` files are rejected in file mode without reading them.
CLI startup/check prints only environment/source/filename, never loaded values.

```powershell
# Explicit environment, requires its corresponding file:
.venv/Scripts/python.exe -m cashly start --app-env staging
# Deployment-injected variables, does not open any dotenv files:
.venv/Scripts/python.exe -m cashly start --app-env prod --env-source system
# Health-only check without private files or credentials:
.venv/Scripts/python.exe -m cashly check --app-env dev --env-source system
```

`dev` enables reload and is rejected for staging/prod. `start` runs without
reload. System source can also be selected with `CASHLY_ENV_SOURCE=system`.
`HOST`, `PORT`, `LOG_LEVEL` and `CORS_ORIGINS` configure the process. Origins are
a JSON array of exact origins without paths, wildcards or trailing slashes;
staging/prod origins require HTTPS. Empty origins permit no browser cross-origin
access. Bind to `0.0.0.0` explicitly if the hosting environment requires it.

## Database/auth configuration

All three `.env.*.example` files are safe to copy and fill privately. The dev
example documents placeholder connection formats. The liveness-only app can run
without a URL. When configured, the app creates a lazy engine/session factory and
disposes it at shutdown; it does not connect, migrate or seed at startup.

- `DATABASE_URL`: SQLAlchemy/psycopg session-pooler URL, port 5432, TLS required.
- `MIGRATION_DATABASE_URL`: separate migration role; direct URL when reachable,
  session-pooler URL for IPv4 fallback. Never transaction pooler for Alembic.
- `SUPABASE_URL`: expected project URL, required for remote migration target
  matching. `SUPABASE_PUBLISHABLE_KEY` is used to validate bearer tokens via Supabase Auth.
  No service-role key is required for these database tools.

Copy actual host/user from Supabase Connect; percent-encode passwords. Only use
the isolated Cashly dev project. Plain `postgresql://` or `postgres://` URLs are
normalized to the installed psycopg driver. Remote connections require TLS; port
6543 is rejected because this setup targets direct/session connections.

`DB_POOL_SIZE=2`, `DB_MAX_OVERFLOW=0`, `DB_POOL_TIMEOUT=10` seconds and
`DB_CONNECT_TIMEOUT=10` seconds are bounded defaults. Size the total budget across
all workers. Migration engines use NullPool, independently from the runtime pool.

## Task 02: inspect SQL, verify connection, apply migration (CMD)

```cmd
python -m cashly db-sql > migration-preview.sql
python -m cashly db-check
python -m cashly db-upgrade
python -m cashly db-check
```

`db-sql` is an offline preview and does not read env files or connect anywhere.
Do not commit the generated preview. The other commands use the selected env
file/system source without printing credentials. Fill the two DB URLs and
`SUPABASE_URL` privately first. Choose the empty, isolated dev project; the
launcher checks that both URLs match that project and permits online migrations
only in dev. A correctly labeled live project cannot be detected by this guard,
so verify the actual project in Supabase Dashboard before applying.

`db-check` is read-only and prints only `connected`, `tls`, `privileged_role` and
`schema_current` booleans. Before migration, `schema_current` can be false; after
upgrade it must be true. An owner/BYPASSRLS credential can report
`privileged_role=true`; that connection does not establish RLS isolation.
`tls` comes from the application's libpq connection, describing application →
pooler (or application → PostgreSQL for direct connections). It does not describe
the separate pooler → PostgreSQL hop; server-side `pg_stat_ssl` cannot establish
the client hop's TLS status through a pooler.

The first migration creates `cashly.accounts`, `profiles`, `account_auth_links`,
`expense_categories`, `expenses`, `expense_items`, `idempotency_records`, plus
private Alembic version bookkeeping. It references existing `auth.users` without
creating/modifying it. Only nine category rows are seeded; no accounts, expenses,
Auth identities or personal bot history are imported. Subsequent upgrade-to-head
is a no-op. Schema/version bootstrap and upgrades share one transaction with an
advisory lock to serialize migrations. Alembic's autogenerated comparisons inspect
only Cashly app objects, excluding Supabase/Auth and unrelated schemas.

Normal Alembic tooling is also configured: `alembic current`, `alembic check`,
`alembic revision --autogenerate -m "description"`. Review generated migrations,
especially RLS/functions/grants. Use the safe `db-upgrade` wrapper to apply them;
raw online Alembic also enforces the dev target guard but may print driver errors
locally. Schema bootstrap occurs only during online Alembic operations.

## Runtime permissions and rollback

The migration enables RLS and revokes schema/table access from PUBLIC, `anon` and
`authenticated`. `sql/runtime_role.sql` is an explicit administrator provisioning
step for a NOLOGIN `cashly_runtime` group with only required grants. Create its
LOGIN member/password privately; it must not own tables or have BYPASSRLS. The
script does not create a login or store passwords and is not run on Supabase by
the agent. Reapply reviewed grants when future migrations introduce new tables.

RLS uses transaction-local `cashly.account_id` and `cashly.auth_user_id`, set via
`Database.transaction(...)` after trusted server identity resolution. The auth
link lookup uses verified Auth identity before account context exists. These
settings are not authentication: future API code must verify JWTs and scope every
repository query. Never expose this DB role or app schema to the browser.

Rollback, **only on disposable development or after a reviewed backup**:

```cmd
alembic downgrade base
```

This drops the core application tables and their data. It keeps the schema and
Alembic bookkeeping, never drops Auth/unrelated objects or global roles, and uses
no CASCADE. Downgrade then upgrade has been tested on isolated PostgreSQL. This
does not restore deleted data; retain a database backup when preservation matters.

## HTTP behavior

Terminal logging: `LOG_FORMAT=auto` (default) shows a readable colored line on an
interactive terminal, and preserves JSON when redirected to files/pipelines.
`LOG_FORMAT=pretty` or `json` selects a format explicitly. `LOG_COLOR=auto` respects
NO_COLOR; `always`/`never` override automatic coloring. Request status uses green
for 2xx, cyan for 3xx, yellow for 4xx and red for 5xx. Human-readable time uses WIB;
JSON timestamps remain UTC. Restart the backend after changing logging settings.
No env changes are necessary for the default interactive behavior.

Account storage: `auth.users.email` is the canonical email, with password hashes
managed by Supabase. `cashly.account_auth_links.auth_user_id` references that user;
`cashly.profiles` stores display name/timezone/theme, not a second email copy.

- `GET /api/v1/health` is public process liveness: `{"data":{"status":"ok","service":"cashly-api"}}`.
  It does not claim database, OAuth, email, OCR or WhatsApp readiness.
- Request logs contain method, route template, status, duration and a generated
  request UUID. No raw path/query/body, auth headers or exception messages.
  API responses expose `X-Request-ID`; error bodies contain the same UUID.
- 404/405/422/500 use `{"error":{"code","message","details","request_id"}}`.
  Validation details include only locations/types, never supplied input values.
- CORS preflight is handled by framework middleware. CORS is a browser policy,
  not authentication. Credentialed CORS remains off while cookie/BFF design is
  awaiting approval; bearer headers are allowed for later integration.
- `/docs` and `/openapi.json` are dev-only. Generated OpenAPI describes only
  implemented routes; `../specs/openapi.yaml` remains the broader design draft.
- Launcher disables raw Uvicorn access logs and proxy-header trust. Hosting,
  trusted reverse proxy configuration and TLS termination remain future work.

## Layout

```text
src/cashly/
  api/            HTTP routing
  schemas/        Typed request/response models
  core/           Environment, settings, errors, logs, middleware
  services/       Future business use cases
  repositories/   Future account-scoped database adapters
  db/             Engine/session factory and core ORM models
  main.py         Application factory
  cli.py          Environment-aware launcher
tests/            Isolated environment and HTTP behavior tests
```

## Verification

```powershell
.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider
.venv/Scripts/python.exe -m ruff check .
.venv/Scripts/python.exe -m ruff format --check .
.venv/Scripts/python.exe -m pip check
```

Tests inject typed settings and use synthetic env files inside `.pytest-tmp/`,
never private developer files. Pytest clears that test-owned directory on each
run. When `pg_ctl`/`initdb` are on PATH, integration tests start and stop their own
temporary local PostgreSQL server on a random loopback port; otherwise they skip
with a reason. Auth tables/roles in that cluster are test stubs, not a live
Supabase instance. No remote service calls are made. Before implementing disputed feature
behavior, review `../specs/BACKEND_DESIGN_REVIEW.md` with the user.
