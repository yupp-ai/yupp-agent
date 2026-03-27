"""Streamlit Server for Analytics and What-If Analysis."""

from __future__ import annotations
from collections.abc import Iterable
from datetime import datetime

import pandas as pd
import streamlit as st
from google.cloud import bigquery

from ypl.streamlit_server.utils import get_bq_client

SVG_ROUTING_CATEGORIES = ("SVG Generation", "SVG Edit")
HTML_ROUTING_CATEGORIES = ("HTML Generation", "HTML Edit")


def _normalize_emails(emails: Iterable[str]) -> list[str]:
    normalized: list[str] = []
    seen = set()
    for email in emails:
        cleaned = email.strip().lower()
        if not cleaned or "@" not in cleaned:
            continue
        if cleaned in seen:
            continue
        seen.add(cleaned)
        normalized.append(cleaned)
    return normalized


def parse_email_input(text_input: str | None, uploaded_text: str | None) -> list[str]:
    raw_chunks: list[str] = []
    if text_input:
        raw_chunks.append(text_input)
    if uploaded_text:
        raw_chunks.append(uploaded_text)
    if not raw_chunks:
        return []

    combined = "\n".join(raw_chunks)
    tokens = list(combined.replace(",", " ").split())
    return _normalize_emails(tokens)


def build_suspicious_summary(rows: list[dict[str, object]], extra_notes: str | None = None) -> str:
    if not rows:
        return ""

    grouped: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        email = str(row.get("Email") or "unknown")
        grouped.setdefault(email, []).append(row)

    lines: list[str] = []
    for email, prompts in grouped.items():
        lines.append(f"{email} has {len(prompts)} suspicious prompts:")
        for prompt in prompts:
            created_at = prompt.get("Created At") or "Unknown time"
            category = prompt.get("Category") or "Unknown category"
            routing = prompt.get("Routing Categories") or "Unknown routing"
            excerpt = prompt.get("Prompt Excerpt") or "(No prompt text)"
            link = prompt.get("Chat Link") or ""
            lines.append(f"- {created_at} | {category} | {routing} | {excerpt} | {link}")
        lines.append("")

    if extra_notes:
        lines.append("Additional notes:")
        lines.append(extra_notes)

    return "\n".join(lines).strip()


@st.cache_data(ttl=600, show_spinner=False)
def load_vendor_prompt_summary(
    emails: list[str],
    start_date: datetime,
    svg_only: bool,
    html_only: bool,
) -> pd.DataFrame:
    if not emails:
        return pd.DataFrame()

    query = """
    WITH input_emails AS (
        SELECT DISTINCT LOWER(email) AS email
        FROM UNNEST(@emails) AS email
    ),
    routing_info_first AS (
        SELECT turn_id, resolved_categories
        FROM (
            SELECT
                ri.turn_id,
                ri.resolved_categories,
                ROW_NUMBER() OVER (PARTITION BY ri.turn_id ORDER BY ri.created_at DESC) AS rn
            FROM `yupp-llms.prodyuppdb_public.routing_info` ri
        )
        WHERE rn = 1
    ),
    user_prompts AS (
        SELECT
            ie.email,
            u.user_id,
            t.turn_id,
            t.chat_id,
            t.created_at AS turn_created_at,
            um.message_id AS prompt_message_id,
            c.name AS category_name,
            JSON_VALUE_ARRAY(ri.resolved_categories) AS routing_categories
        FROM input_emails ie
        LEFT JOIN `yupp-llms.prodyuppdb_public.users` u
            ON LOWER(u.email) = ie.email
        LEFT JOIN `yupp-llms.prodyuppdb_public.turns` t
            ON t.creator_user_id = u.user_id
            AND t.deleted_at IS NULL
            AND t.created_at >= TIMESTAMP(@start_date)
        LEFT JOIN `yupp-llms.prodyuppdb_public.chat_messages` um
            ON um.turn_id = t.turn_id
            AND um.message_type = 'USER_MESSAGE'
        LEFT JOIN `yupp-llms.prodyuppdb_public.categories` c
            ON c.category_id = um.category_id
        LEFT JOIN routing_info_first ri
            ON ri.turn_id = t.turn_id
        WHERE um.message_id IS NOT NULL
            AND (@svg_only = FALSE OR EXISTS (
                SELECT 1
                FROM UNNEST(IFNULL(JSON_VALUE_ARRAY(ri.resolved_categories), [])) AS category
                WHERE category IN UNNEST(@svg_categories)
            ))
            AND (@html_only = FALSE OR EXISTS (
                SELECT 1
                FROM UNNEST(IFNULL(JSON_VALUE_ARRAY(ri.resolved_categories), [])) AS category
                WHERE category IN UNNEST(@html_categories)
            ))
    ),
    category_counts AS (
        SELECT email, category_name, COUNT(*) AS category_count
        FROM user_prompts
        WHERE category_name IS NOT NULL
        GROUP BY email, category_name
    ),
    top_categories AS (
        SELECT
            email,
            ARRAY_AGG(category_name ORDER BY category_count DESC LIMIT 1)[SAFE_OFFSET(0)] AS top_category
        FROM category_counts
        GROUP BY email
    ),
    routing_category_counts AS (
        SELECT email, routing_category, COUNT(*) AS routing_count
        FROM user_prompts, UNNEST(IFNULL(routing_categories, [])) AS routing_category
        GROUP BY email, routing_category
    ),
    top_routing_categories AS (
        SELECT
            email,
            ARRAY_AGG(routing_category ORDER BY routing_count DESC LIMIT 1)[SAFE_OFFSET(0)] AS top_routing_category
        FROM routing_category_counts
        GROUP BY email
    ),
    user_evals AS (
        SELECT
            u.user_id,
            COUNT(CASE WHEN e.eval_type = 'SELECTION' THEN 1 END) AS comparisons,
            COUNT(CASE WHEN e.eval_type = 'DOWNVOTE' THEN 1 END) AS downvotes,
            COUNT(CASE WHEN e.eval_type IN ('SELECTION', 'DOWNVOTE') THEN 1 END) AS evals
        FROM input_emails ie
        LEFT JOIN `yupp-llms.prodyuppdb_public.users` u
            ON LOWER(u.email) = ie.email
        LEFT JOIN `yupp-llms.prodyuppdb_public.evals` e
            ON e.user_id = u.user_id
            AND e.deleted_at IS NULL
            AND e.created_at >= TIMESTAMP(@start_date)
        GROUP BY u.user_id
    ),
    turns_with_evals AS (
        SELECT
            up.user_id,
            COUNT(DISTINCT up.turn_id) AS turns_with_evals
        FROM user_prompts up
        INNER JOIN `yupp-llms.prodyuppdb_public.evals` e
            ON e.turn_id = up.turn_id
            AND e.deleted_at IS NULL
            AND e.created_at >= TIMESTAMP(@start_date)
            AND e.eval_type IN ('SELECTION', 'DOWNVOTE')
        GROUP BY up.user_id
    )
    SELECT
        ie.email AS email,
        u.user_id AS user_id,
        u.country_code AS country_code,
        NULL AS roles,
        COUNT(DISTINCT up.turn_id) AS prompt_turns,
        COUNT(DISTINCT up.chat_id) AS chats,
        COUNT(up.prompt_message_id) AS prompt_messages,
        MIN(up.turn_created_at) AS first_prompt_at,
        MAX(up.turn_created_at) AS last_prompt_at,
        tc.top_category AS top_category,
        trc.top_routing_category AS top_routing_category,
        COALESCE(ue.comparisons, 0) AS comparisons,
        COALESCE(ue.downvotes, 0) AS downvotes,
        COALESCE(ue.evals, 0) AS evals,
        COALESCE(twe.turns_with_evals, 0) AS turns_with_evals,
        COUNT(DISTINCT up.turn_id) - COALESCE(twe.turns_with_evals, 0) AS turns_without_evals
    FROM input_emails ie
    LEFT JOIN `yupp-llms.prodyuppdb_public.users` u
        ON LOWER(u.email) = ie.email
    LEFT JOIN user_prompts up
        ON up.email = ie.email
    LEFT JOIN top_categories tc
        ON tc.email = ie.email
    LEFT JOIN top_routing_categories trc
        ON trc.email = ie.email
    LEFT JOIN user_evals ue
        ON ue.user_id = u.user_id
    LEFT JOIN turns_with_evals twe
        ON twe.user_id = u.user_id
    GROUP BY
        ie.email,
        u.user_id,
        u.country_code,
        roles,
        tc.top_category,
        trc.top_routing_category,
        ue.comparisons,
        ue.downvotes,
        ue.evals,
        twe.turns_with_evals
    ORDER BY prompt_messages DESC, ie.email
    """

    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ArrayQueryParameter("emails", "STRING", emails),
            bigquery.ScalarQueryParameter("start_date", "TIMESTAMP", start_date),
            bigquery.ScalarQueryParameter("svg_only", "BOOL", svg_only),
            bigquery.ArrayQueryParameter("svg_categories", "STRING", list(SVG_ROUTING_CATEGORIES)),
            bigquery.ScalarQueryParameter("html_only", "BOOL", html_only),
            bigquery.ArrayQueryParameter("html_categories", "STRING", list(HTML_ROUTING_CATEGORIES)),
        ]
    )
    return get_bq_client().query(query, job_config=job_config).to_dataframe()


@st.cache_data(ttl=600, show_spinner=False)
def load_vendor_prompt_samples(
    emails: list[str],
    start_date: datetime,
    svg_only: bool,
    html_only: bool,
    sample_per_user: int | None,
) -> pd.DataFrame:
    if not emails:
        return pd.DataFrame()

    query = """
    WITH input_emails AS (
        SELECT DISTINCT LOWER(email) AS email
        FROM UNNEST(@emails) AS email
    ),
    routing_info_first AS (
        SELECT turn_id, resolved_categories
        FROM (
            SELECT
                ri.turn_id,
                ri.resolved_categories,
                ROW_NUMBER() OVER (PARTITION BY ri.turn_id ORDER BY ri.created_at DESC) AS rn
            FROM `yupp-llms.prodyuppdb_public.routing_info` ri
        )
        WHERE rn = 1
    ),
    prompts AS (
        SELECT
            ie.email,
            u.user_id,
            u.country_code,
            t.turn_id,
            t.chat_id,
            t.sequence_id,
            t.created_at AS turn_created_at,
            um.message_id AS prompt_message_id,
            um.content AS prompt_text,
            LENGTH(um.content) AS prompt_length,
            c.name AS category_name,
            JSON_VALUE_ARRAY(ri.resolved_categories) AS routing_categories,
            tq.prompt_difficulty AS prompt_difficulty,
            tq.prompt_is_safe AS prompt_is_safe
        FROM input_emails ie
        JOIN `yupp-llms.prodyuppdb_public.users` u
            ON LOWER(u.email) = ie.email
        JOIN `yupp-llms.prodyuppdb_public.turns` t
            ON t.creator_user_id = u.user_id
            AND t.deleted_at IS NULL
            AND t.created_at >= TIMESTAMP(@start_date)
        JOIN `yupp-llms.prodyuppdb_public.chat_messages` um
            ON um.turn_id = t.turn_id
            AND um.message_type = 'USER_MESSAGE'
        LEFT JOIN `yupp-llms.prodyuppdb_public.categories` c
            ON c.category_id = um.category_id
        LEFT JOIN `yupp-llms.prodyuppdb_public.turn_qualities` tq
            ON tq.turn_id = t.turn_id
        LEFT JOIN routing_info_first ri
            ON ri.turn_id = t.turn_id
        WHERE (@svg_only = FALSE OR EXISTS (
            SELECT 1
            FROM UNNEST(IFNULL(JSON_VALUE_ARRAY(ri.resolved_categories), [])) AS category
            WHERE category IN UNNEST(@svg_categories)
        ))
        AND (@html_only = FALSE OR EXISTS (
            SELECT 1
            FROM UNNEST(IFNULL(JSON_VALUE_ARRAY(ri.resolved_categories), [])) AS category
            WHERE category IN UNNEST(@html_categories)
        ))
    ),
    ranked AS (
        SELECT
            *,
            ROW_NUMBER() OVER (PARTITION BY email ORDER BY turn_created_at DESC) AS rn
        FROM prompts
    )
    SELECT *
    FROM ranked
    WHERE (@sample_per_user IS NULL OR rn <= @sample_per_user)
    ORDER BY turn_created_at DESC
    """

    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ArrayQueryParameter("emails", "STRING", emails),
            bigquery.ScalarQueryParameter("start_date", "TIMESTAMP", start_date),
            bigquery.ScalarQueryParameter("svg_only", "BOOL", svg_only),
            bigquery.ArrayQueryParameter("svg_categories", "STRING", list(SVG_ROUTING_CATEGORIES)),
            bigquery.ScalarQueryParameter("html_only", "BOOL", html_only),
            bigquery.ArrayQueryParameter("html_categories", "STRING", list(HTML_ROUTING_CATEGORIES)),
            bigquery.ScalarQueryParameter("sample_per_user", "INT64", sample_per_user),
        ]
    )
    return get_bq_client().query(query, job_config=job_config).to_dataframe()
