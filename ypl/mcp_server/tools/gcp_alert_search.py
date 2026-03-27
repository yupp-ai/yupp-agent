"""GCP Alert Search Tool.

Fetch alert (incident) metadata from Google Cloud Monitoring.
"""

import re
from typing import Any

import google.auth
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from ypl.backend.config import settings
from ypl.structured_logger import get_logger

logger = get_logger()

# Pattern to validate alert ID format (alphanumeric with dots, dashes, underscores)
_ALERT_ID_PATTERN = re.compile(r"^[a-zA-Z0-9._-]+$")
# Pattern to extract alert ID from GCP console URL
_ALERT_URL_PATTERN = re.compile(r"console\.cloud\.google\.com/monitoring/alerting/alerts/([a-zA-Z0-9._-]+)")


def extract_alert_id(alert_url: str) -> str:
    """Extract alert ID from GCP alert console URL."""
    match = _ALERT_URL_PATTERN.search(alert_url)
    if not match:
        raise ValueError(
            "Could not extract alert ID from URL. "
            "Expected format: https://console.cloud.google.com/monitoring/alerting/alerts/ALERT_ID"
        )
    return match.group(1)


async def get_gcp_alert_metadata(alert_input: str) -> dict[str, Any]:
    """Fetch GCP alert (incident) metadata using Monitoring REST API.

    Args:
        alert_input: Either a full GCP alert console URL (e.g.,
            https://console.cloud.google.com/monitoring/alerting/alerts/ALERT_ID)
            or just an alert ID (e.g., "0.o3gwwfx7rf2c"). Uses settings.GCP_PROJECT_ID as the project.

    Returns:
        Dictionary containing:
        - success: Whether the request succeeded
        - alert_id: The extracted alert ID
        - project_id: The project ID used (from settings.GCP_PROJECT_ID)
        - metadata: The raw alert metadata from GCP API
        - error: Error message if success is False
    """
    try:
        # Detect if input is a URL (starts with http) or just an alert ID
        if alert_input.startswith(("http://", "https://")):
            alert_id = extract_alert_id(alert_input)
        else:
            # Assume it's just an alert ID
            if not _ALERT_ID_PATTERN.match(alert_input):
                return {
                    "success": False,
                    "error": (
                        f"Invalid alert ID format: {alert_input}. Expected alphanumeric with dots, dashes, underscores."
                    ),
                    "alert_input": alert_input,
                }
            alert_id = alert_input

        # Always use settings.GCP_PROJECT_ID
        project_id = settings.GCP_PROJECT_ID
        if not project_id:
            return {
                "success": False,
                "error": ("GCP_PROJECT_ID not configured. Please configure GCP_PROJECT_ID in settings."),
                "alert_input": alert_input,
            }

        logger.info("Fetching GCP alert metadata", alert_id=alert_id, project_id=project_id)

        # Use application default credentials
        credentials, _ = google.auth.default(  # type: ignore[no-untyped-call]
            scopes=["https://www.googleapis.com/auth/monitoring.read"]
        )
        service = build("monitoring", "v3", credentials=credentials, cache_discovery=False)

        alert_name = f"projects/{project_id}/alerts/{alert_id}"
        alert = service.projects().alerts().get(name=alert_name).execute()

        logger.info("GCP alert metadata fetched successfully", alert_id=alert_id)

        return {
            "success": True,
            "alert_id": alert_id,
            "project_id": project_id,
            "metadata": alert,
        }
    except ValueError as e:
        logger.warning("Invalid alert input", alert_input=alert_input, error=str(e))
        return {
            "success": False,
            "error": str(e),
            "alert_input": alert_input,
        }
    except HttpError as e:
        if e.resp.status == 404:
            logger.warning("GCP alert not found", alert_input=alert_input, error=str(e))
            return {
                "success": False,
                "error": f"Alert not found: {alert_input}",
                "alert_input": alert_input,
            }
        logger.warning("GCP API error fetching alert", alert_input=alert_input, error=str(e))
        return {
            "success": False,
            "error": f"GCP API error: {str(e)}",
            "alert_input": alert_input,
        }
    except Exception as exc:
        logger.warning("Error fetching GCP alert", alert_input=alert_input, error=str(exc))
        return {
            "success": False,
            "error": f"Exception fetching alert: {exc}",
            "alert_input": alert_input,
        }
