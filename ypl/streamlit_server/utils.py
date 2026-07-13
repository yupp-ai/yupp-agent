"""Utility functions for Streamlit server pages."""

from typing import cast

import streamlit as st
from google.cloud import bigquery


def get_bq_client(project: str = "your-gcp-project") -> bigquery.Client:
    """Get or create BigQuery client lazily.

    This function provides lazy initialization of BigQuery clients to improve
    page load performance. The client is stored in Streamlit's session state
    and reused across function calls.

    Args:
        project: GCP project ID. Defaults to "your-gcp-project".

    Returns:
        A BigQuery client instance.
    """
    if "bq_client" not in st.session_state:
        st.session_state.bq_client = bigquery.Client(project=project)
    return cast(bigquery.Client, st.session_state.bq_client)
