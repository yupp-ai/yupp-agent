## Streamlit server — agent guide

This directory hosts the operational dashboards (login, console,
dashboards, admin pages). The pages share `from ypl.db.all_models
import *` and `from ypl.db.rbac import …` — i.e. they import the **same
SQLModel `MetaData` singleton** that the rest of the platform binds tables
to. That fact creates one trap that has bitten us multiple times.

### The "Table 'X' is already defined" trap

Symptom (reported in Slack, on every page):

```
sqlalchemy.exc.InvalidRequestError:
  Table 'agents' is already defined for this MetaData instance.
  Specify 'extend_existing=True' to redefine options and columns on an
  existing Table object.
```

…with the traceback going through `ypl/db/all_models.py` → one of the
`ypl/db/*.py` model modules → `class X(BaseModel, table=True)`.

Don't be misled by the error message — there is no duplicate
`__tablename__` and you should NOT add `extend_existing=True`. The real
cause is **Streamlit's `LocalSourcesWatcher`**.

### Why it happens

Streamlit's source-file watcher is on by default
(`server.fileWatcherType="auto"`). When any watched module's `.py` file
changes mtime — which happens on every deploy as `git pull` rewrites
files, and can also happen from a stray `touch`/rsync/IDE save — the
watcher executes (verbatim, from
`streamlit/watcher/local_sources_watcher.py`):

```python
# Workaround: Delete all watched modules so we can guarantee changes to
# the updated module are reflected on reload.
for wm in self._watched_modules.values():
    if wm.module_name is not None and wm.module_name in sys.modules:
        del sys.modules[wm.module_name]
```

The next request re-runs `app.py` (or any page script). Python sees
`ypl.db.agent_harness` is no longer in `sys.modules` and re-executes the
module top-to-bottom. `class Agent(BaseModel, table=True): …` then tries
to register `agents` against `SQLModel.metadata` — but `SQLModel.metadata`
is module-level state on the `sqlmodel` package itself, which the watcher
did *not* purge, so the table is still there. `MetaData.__new__` rejects
the redefinition. Every page that imports `ypl.db.*` now 500s until the
streamlit process is restarted.

### The fix (already in place)

Both entrypoint scripts pass `--server.fileWatcherType=none`:

- `streamlit_server_entrypoint.sh` (Cloud Run shape, legacy)
- `selfhosted_entrypoint.sh` (the bare-metal monolith, lit.example.com)

Production never needs hot-reload — deploys restart the service. Local dev
keeps the default (`auto`) because `scripts/run_local.sh` and direct
`streamlit run …` invocations don't pass the flag. If you add a new
production-shaped entrypoint, **disable the watcher there too**.

Two regression tests guard this:

- `tests/streamlit_server/test_entrypoint_disables_file_watcher.py` fails
  if either production entrypoint loses the
  `--server.fileWatcherType=none` flag.
- `tests/streamlit_server/test_pages_smoke.py` runs every
  `pages/*.py` script through `streamlit.testing.v1.AppTest` and asserts
  no SQLAlchemy `InvalidRequestError` is raised (catches the
  duplicate-import-path variant of the same bug), and separately
  reproduces the watcher's `del sys.modules[…]` purge to prove the
  underlying SQLModel behaviour we're guarding against still exists.

### Investigating "this happened again"

If you see `Table 'X' is already defined` on the lit.example.com pages,
walk the checklist:

1. **Did someone reintroduce the watcher?** `grep
   fileWatcherType ypl/streamlit_server/*entrypoint.sh` — both scripts
   should show `none`. The regression test would catch this in CI; if it's
   green, look elsewhere.
2. **Is the running process actually using the current entrypoint?**
   `systemctl cat ahs-streamlit` (selfhosted) and `ps -ef | grep
   streamlit` — confirm `--server.fileWatcherType` is in the cmdline of
   the live process. After editing the entrypoint, you must `systemctl
   restart ahs-streamlit` (the entrypoint is read at process start).
3. **Did someone add explicit `importlib.reload(ypl…)` or
   `del sys.modules['ypl…']`?** `grep -r "importlib.reload\|del
   sys.modules" ypl/streamlit_server/`. Don't — use `st.cache_resource`
   to invalidate state instead.
4. **Did someone add a second `__tablename__` for the same table?**
   `grep -rn "__tablename__" ypl/db/` — same name twice (across model
   files) is a real conflict that no flag will hide.
5. **Did someone introduce a second import path for the same module?**
   e.g. running with both `/opt/yupp-agent` and `/opt/yupp-agent/ypl` in
   `PYTHONPATH` makes `import ypl.db.rbac` and `import db.rbac` both
   resolve, registering the tables twice. `printenv PYTHONPATH`,
   `cat .env | grep PYTHONPATH`, and inspect the systemd unit's
   `WorkingDirectory`/`Environment=` lines.

### Don't reach for `extend_existing=True`

It looks tempting (the error message even suggests it), but it has real
cost:

- It silences legitimate "two models claim the same table" bugs — the
  loser silently rebinds columns onto the winner's mapper.
- It does nothing about the Relationship/Mapper-level state that *also*
  duplicates on reload — you'd just trade one error for a subtler one.
- It pollutes every model file in the repo for a Streamlit-only quirk.

Disable the watcher instead.

### Adding new pages

- Page modules go in `pages/`, page-shared utilities in this directory.
- Don't import models you don't need — every `from ypl.db.foo import …`
  registers more tables onto the metadata and grows the blast radius of
  any future watcher slip-up.
- Auth gating: `from ypl.streamlit_server.auth import require_auth` (or
  `auth_required`) at the top of the page.
- Database access from a page must go through
  `get_async_session_read_replica()` — never construct an engine directly.
