"""GCS persistence for agent session workspace directories.

After each user/assistant turn, syncs the session's ``attachments/`` and
``history/`` directories to ``gs://yupp-agents/sessions/{session_id}/`` in an
rsync-like fashion: only new or modified files are uploaded.
"""

import os

from ypl.agent_harness_service.common.constants import (
    AHS_GCS_SESSION_BUCKET,
    AHS_GCS_SESSION_PREFIX,
    AHS_SESSIONS_DIR,
)
from ypl.agent_harness_service.core.gcs_sync import (
    guess_content_type,
    sync_dir_to_gcs,
    upload_file,
)
from ypl.structured_logger import get_logger

logger = get_logger()

# Subdirectories inside the session workspace that should be synced to GCS.
_PERSIST_DIRS = ("attachments", "history")

# Re-export for backward compatibility (memory_persistence imported these).
_guess_content_type = guess_content_type
_upload_file = upload_file


async def sync_session_to_gcs(session_id: str) -> int:
    """Sync session's ``attachments/`` and ``history/`` to GCS.

    Uploads only files that are new or modified since the last sync
    (determined via a local manifest file). Uses a per-session lock to
    prevent concurrent uploads for the same session.

    Target layout::

        gs://{AHS_GCS_SESSION_BUCKET}/{AHS_GCS_SESSION_PREFIX}/{session_id}/attachments/...
        gs://{AHS_GCS_SESSION_BUCKET}/{AHS_GCS_SESSION_PREFIX}/{session_id}/history/...

    Args:
        session_id: The agent session UUID string.

    Returns:
        Number of files uploaded.
    """
    if os.environ.get("ENVIRONMENT", "local") == "local":
        return 0

    session_dir = os.path.join(AHS_SESSIONS_DIR, session_id)
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


# TODO: implement restore_session_from_gcs() to download attachments/ and
# history/ from GCS back into a session workspace for session resumption.
