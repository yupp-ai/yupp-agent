"""Pydantic models for the Linear ↔ AHS sync integration."""

from __future__ import annotations
from datetime import datetime

from pydantic import BaseModel, Field


class LinearProjectRef(BaseModel):
    """Reference to a Linear project synced from an AHS project."""

    linear_project_id: str = Field(..., description="Linear project UUID")
    linear_team_id: str = Field(..., description="Linear team UUID that owns the project")
    last_synced_at: datetime | None = Field(None, description="Timestamp of the last successful sync")


class LinearIssueRef(BaseModel):
    """Reference to a Linear issue synced from an AHS task."""

    linear_issue_id: str = Field(..., description="Linear issue UUID")
    linear_identifier: str = Field(..., description="Human-readable issue identifier, e.g. 'ENG-123'")
    last_synced_at: datetime | None = Field(None, description="Timestamp of the last successful sync")


class SyncResult(BaseModel):
    """Counts summarising the outcome of a sync operation."""

    created: int = Field(0, description="Number of items created in Linear")
    updated: int = Field(0, description="Number of items updated in Linear")
    skipped: int = Field(0, description="Number of items skipped (already up-to-date)")
    errors: int = Field(0, description="Number of items that failed to sync")
