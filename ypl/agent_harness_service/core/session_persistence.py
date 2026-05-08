"""Cloud persistence for agent session workspace directories.

After each user/assistant turn, syncs the session's ``attachments/`` and
``history/`` directories to a remote object store in an rsync-like fashion:
only new or modified files are uploaded.

Backend selection
-----------------

The active backend is chosen by ``settings.SESSION_PERSISTENCE_BACKEND``:

- ``""``    → fall through to the legacy ``ENVIRONMENT``-based default
              (off in ``local|test|selfhosted``, gcs in ``staging|production``).
- ``"off"`` → no-op (returns 0). Overrides the ENVIRONMENT default — useful
              for staging/production tenants that opt out of session sync.
- ``"gcs"`` → :func:`~ypl.agent_harness_service.core.gcs_sync.sync_dir_to_gcs`
              against ``AHS_GCS_SESSION_*``.
- ``"s3"``  → :func:`~ypl.agent_harness_service.core.session_persistence_s3.sync_dir_to_s3`
              against ``AHS_S3_SESSION_*``.

The single ``SESSION_PERSISTENCE_BACKEND`` knob fully overrides the
``ENVIRONMENT`` gate when set explicitly. When unset the previous behaviour
is preserved for backwards compatibility.
"""

from __future__ import annotations
import os

from ypl.agent_harness_service.common.constants import (
    AHS_GCS_SESSION_BUCKET,
    AHS_GCS_SESSION_PREFIX,
    AHS_S3_SESSION_BUCKET,
    AHS_S3_SESSION_PREFIX,
    AHS_SESSIONS_DIR,
)
from ypl.agent_harness_service.core.gcs_sync import sync_dir_to_gcs
from ypl.agent_harness_service.core.session_persistence_s3 import sync_dir_to_s3
from ypl.backend.config import is_gcp_free_environment, settings
from ypl.structured_logger import get_logger

logger = get_logger()

# Subdirectories inside the session workspace that should be synced.
_PERSIST_DIRS = ("attachments", "history")

# Allowed values for SESSION_PERSISTENCE_BACKEND. Empty string means
# "fall through to the ENVIRONMENT-based default" — see _resolve_backend.
_VALID_BACKENDS = frozenset({"", "off", "gcs", "s3"})


def _resolve_backend() -> str:
    """Return the effective session-persistence backend.

    Honours an explicit ``SESSION_PERSISTENCE_BACKEND`` value (single
    knob — overrides the ENVIRONMENT gate) or falls back to the legacy
    ENVIRONMENT-based default when unset.

    Returns:
        One of ``"off"``, ``"gcs"``, or ``"s3"``.
    """
    raw = (settings.SESSION_PERSISTENCE_BACKEND or "").strip().lower()
    if raw not in _VALID_BACKENDS:
        logger.warning(
            "Unknown SESSION_PERSISTENCE_BACKEND; ignoring and using ENVIRONMENT default",
            value=raw,
            valid=sorted(_VALID_BACKENDS),
        )
        raw = ""

    if raw:
        return raw

    # Backwards-compat default: off in GCP-free environments, gcs elsewhere.
    if is_gcp_free_environment(settings.ENVIRONMENT):
        return "off"
    return "gcs"


async def sync_session(session_id: str) -> int:
    """Sync the session's ``attachments/`` and ``history/`` directories.

    Dispatches to the configured backend (see module docstring). Uploads
    only files that are new or modified since the last sync (determined
    via a local manifest file). Uses a per-session lock to prevent
    concurrent uploads for the same session.

    Target layout (GCS)::

        gs://{AHS_GCS_SESSION_BUCKET}/{AHS_GCS_SESSION_PREFIX}/{session_id}/...

    Target layout (S3)::

        s3://{AHS_S3_SESSION_BUCKET}/{AHS_S3_SESSION_PREFIX}/{session_id}/...

    Args:
        session_id: The agent session UUID string.

    Returns:
        Number of files uploaded. Returns ``0`` when persistence is disabled.
    """
    backend = _resolve_backend()
    if backend == "off":
        return 0

    session_dir = os.path.join(AHS_SESSIONS_DIR, session_id)

    if backend == "gcs":
        return await sync_dir_to_gcs(
            base_dir=session_dir,
            subdirs=_PERSIST_DIRS,
            bucket=AHS_GCS_SESSION_BUCKET,
            gcs_prefix=f"{AHS_GCS_SESSION_PREFIX}/{session_id}",
            lock_key=f"session:{session_id}",
            skip_symlinks=True,
            skip_hardlinks=True,
            log_label=f"session {session_id} files",
        )

    if backend == "s3":
        return await sync_dir_to_s3(
            base_dir=session_dir,
            subdirs=_PERSIST_DIRS,
            bucket=AHS_S3_SESSION_BUCKET,
            s3_prefix=f"{AHS_S3_SESSION_PREFIX}/{session_id}",
            lock_key=f"session:{session_id}",
            region=settings.S3_REGION or None,
            endpoint_url=settings.S3_ENDPOINT_URL or None,
            skip_symlinks=True,
            skip_hardlinks=True,
            log_label=f"session {session_id} files",
        )

    # Should be unreachable thanks to _resolve_backend's normalisation.
    logger.warning("No-op for unrecognised resolved backend", backend=backend)
    return 0


async def sync_session_to_gcs(session_id: str) -> int:
    """Backwards-compatible alias for :func:`sync_session`.

    Earlier versions of AHS only supported a GCS backend, so callers used
    this name. New code should call :func:`sync_session` instead.
    """
    return await sync_session(session_id)


# TODO: implement restore_session() to download attachments/ and history/
# from the configured backend back into a session workspace for session
# resumption. Should mirror sync_session's dispatch logic.
