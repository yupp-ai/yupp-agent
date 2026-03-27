import os

from google.auth import default
from google.auth.exceptions import DefaultCredentialsError
from google.cloud import bigquery

from ypl.backend.config import settings
from ypl.structured_logger import get_logger

logger = get_logger()
_bq_client: bigquery.Client | None = None


def get_bigquery_client() -> bigquery.Client:
    """Get or create BigQuery client."""
    global _bq_client
    if _bq_client is None:
        # Log which credentials are being used
        try:
            credentials, project = default()  # type: ignore[no-untyped-call]
            if hasattr(credentials, "service_account_email"):
                logger.info(f"Using service account: {credentials.service_account_email}")
            elif hasattr(credentials, "_service_account_email"):
                logger.info(f"Using service account: {credentials._service_account_email}")
            elif hasattr(credentials, "token"):
                # Try to get email from user credentials
                try:
                    from google.auth.transport.requests import Request

                    credentials.refresh(Request())  # type: ignore[no-untyped-call]
                    if hasattr(credentials, "id_token"):
                        import jwt

                        token_info = jwt.decode(credentials.id_token, options={"verify_signature": False})
                        email = token_info.get("email", "unknown")
                        logger.info(f"Using user credentials: {email}")
                    else:
                        logger.info("Using user credentials (gcloud auth)")
                except Exception:
                    logger.info("Using user credentials (gcloud auth)")
            else:
                logger.info(f"Using credentials type: {type(credentials).__name__}")

            # Check for GOOGLE_APPLICATION_CREDENTIALS env var
            if os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
                logger.info(f"GOOGLE_APPLICATION_CREDENTIALS set to: {os.getenv('GOOGLE_APPLICATION_CREDENTIALS')}")
        except DefaultCredentialsError as e:
            logger.error(f"Could not get default credentials: {e}")
            logger.error("Please run: gcloud auth application-default login")
            raise
        except Exception as e:
            logger.warning(f"Could not determine credentials type: {e}")
        _bq_client = bigquery.Client(project=settings.GCP_PROJECT_ID)
    return _bq_client
