import asyncio
import logging
import uuid
from datetime import UTC, datetime
from functools import cache
from typing import Any, NamedTuple

import httpx
import resend
from more_itertools import chunked
from pydantic import BaseModel
from sqlalchemy import func, select, tuple_  # noqa: TID251 - use sqlmodel
from sqlmodel import Session, col
from ypl.backend.config import settings
from ypl.backend.db import get_async_session, get_async_session_read_replica, get_engine, get_engine_read_replica
from ypl.backend.email.campaigns.templates.utils import html_to_plaintext, load_html_wrapper
from ypl.backend.email.constants import (
    BRAND_NAME,
    DISCORD_LINK,
    HEAD_OVER_TO_YUPP_LINK,
    INVITE_FRIEND_BONUS_CREDITS,
    PROFILE_LINK,
    REPLY_TO_ADDRESS,
    SIGNATURE,
    UNSUBSCRIBE_LINK,
    YUPP_LINK,
)
from ypl.backend.email.email_types import EmailConfig, EmailContent, FromEmailAddressType
from ypl.backend.llm.db_helpers import get_active_model_count, get_active_model_count_sync
from ypl.backend.utils.json import json_dumps
from ypl.backend.utils.reward_utils import get_referral_bonus_referrer_credits_formatted
from ypl.db.emails import EmailLogs
from ypl.db.users import User, UserRole


async def _get_test_account_emails(emails: list[str]) -> set[str]:
    """Get the set of emails that belong to users with TEST role.

    Args:
        emails: List of email addresses to check

    Returns:
        Set of email addresses that belong to users with TEST role
    """
    if not emails:
        return set[str]()

    emails_lower = [email.lower() for email in emails]

    async with get_async_session_read_replica() as session:
        result = await session.exec(
            select(User.email, User.role).where(  # type: ignore[call-overload]
                func.lower(col(User.email)).in_(emails_lower)
            )
        )
        return {user_email.lower() for user_email, user_role in result if UserRole.TEST in user_role}


async def _is_test_account_email(email: str) -> bool:
    """Check if an email address belongs to a user with TEST role.

    Args:
        email: Email address to check

    Returns:
        True if the email belongs to a user with TEST role, False otherwise
    """
    test_account_emails = await _get_test_account_emails([email])
    return email.lower() in test_account_emails


async def _send_batch_async(batch_params: list[resend.Emails.SendParams]) -> Any:
    """
    Send batch emails asynchronously using httpx instead of the synchronous resend library.
    This provides better performance by avoiding asyncio.to_thread overhead.
    """
    url = settings.RESEND_BATCH_EMAIL_URL
    headers = {
        "Authorization": f"Bearer {settings.RESEND_API_KEY}",
        "Content-Type": "application/json",
        "User-Agent": "resend-python:async",
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(url, json=batch_params, headers=headers)
        response.raise_for_status()
        return response.json()


class BatchEmailRecipient(BaseModel):
    email: str
    name: str | None = None
    params: dict[str, str] | None = None


class BatchEmailResult(NamedTuple):
    successful_emails: list[str]
    failed_emails: list[str]


@cache
def _get_email_campaigns() -> dict[str, EmailContent]:
    from ypl.backend.email.campaigns.templates.email_templates import (
        ACCOUNT_DELETION_REQUEST_SUCCESSFUL_EMAIL_CONTENT,
        BOOSTER_CREDITS_EMAIL_CONTENT,
        BULK_ACTION_RESULTS_EMAIL_CONTENT,
        BULK_TRANSLATE_RESULTS_EMAIL_CONTENT,
        CASHOUT_DISABLED_EMAIL_CONTENT,
        CASHOUT_ENABLED_EMAIL_CONTENT,
        CASHOUT_FAILED_EMAIL_CONTENT,
        CASHOUT_PROCESSING_EMAIL_CONTENT,
        CASHOUT_SUCCESSFUL_EMAIL_CONTENT,
        DATA_TAKEOUT_READY_EMAIL_CONTENT,
        DAY_N_INACTIVE_EMAIL_CONTENT,
        DCO_SUPPORT_TICKET_CLOSED_EMAIL_CONTENT,
        DCO_SUPPORT_TICKET_FALSE_POSITIVE_EMAIL_CONTENT,
        DEACTIVATION_EMAIL_CONTENT,
        DISABLED_CASHOUT_EMAIL_CONTENT,
        DU_SUPPORT_TICKET_CLOSED_EMAIL_CONTENT,
        FIRST_PREF_BONUS_EMAIL_CONTENT,
        MCP_TOKEN_DENIED_EMAIL_CONTENT,
        MCP_TOKEN_EMAIL_CONTENT,
        MONTHLY_SUMMARY_EMAIL_CONTENT,
        OLD_ACCOUNT_INACTIVE_EMAIL_CONTENT,
        OTP_VERIFICATION_EMAIL_CONTENT,
        OUT_OF_WAITLIST_EMAIL_CONTENT,
        REACTIVATION_EMAIL_CONTENT,
        REFERRAL_EXPERIMENT_A_CONTENT,
        REFERRAL_EXPERIMENT_B_CONTENT,
        REFERRAL_EXPERIMENT_C_CONTENT,
        REFFERAL_BONUS_EMAIL_CONTENT,
        SCRATCHCARD_BUG_EMAIL_CONTENT,
        SIC_AVAILABILITY_EMAIL_CONTENT,
        SIGN_UP_EMAIL_CONTENT,
        WEEK_1_CHECKIN_EMAIL_CONTENT,
        WEEK_1_INACTIVE_EMAIL_CONTENT,
        YOUR_FRIEND_JOINED_EMAIL_CONTENT,
    )

    return {
        "signup": SIGN_UP_EMAIL_CONTENT,
        "sic_availability": SIC_AVAILABILITY_EMAIL_CONTENT,
        "referral_bonus": REFFERAL_BONUS_EMAIL_CONTENT,
        "referred_user": FIRST_PREF_BONUS_EMAIL_CONTENT,  # deprecated, use first_pref_bonus instead
        "first_pref_bonus": FIRST_PREF_BONUS_EMAIL_CONTENT,
        "your_friend_joined": YOUR_FRIEND_JOINED_EMAIL_CONTENT,
        "week_1_checkin": WEEK_1_CHECKIN_EMAIL_CONTENT,
        "week_1_inactive": WEEK_1_INACTIVE_EMAIL_CONTENT,
        "day_n_inactive": DAY_N_INACTIVE_EMAIL_CONTENT,
        # "monthly_summary" is the campaign name we use for email
        # templates. On the EmailLogs table, we store the campaign name
        # as "monthly_summary_{year}_{month}_{day}".
        "monthly_summary": MONTHLY_SUMMARY_EMAIL_CONTENT,
        "old_account_inactive": OLD_ACCOUNT_INACTIVE_EMAIL_CONTENT,
        "disabled_cashout": DISABLED_CASHOUT_EMAIL_CONTENT,
        "account_deletion_request_successful": ACCOUNT_DELETION_REQUEST_SUCCESSFUL_EMAIL_CONTENT,
        "deactivation": DEACTIVATION_EMAIL_CONTENT,
        "reactivation": REACTIVATION_EMAIL_CONTENT,
        "otp_verification": OTP_VERIFICATION_EMAIL_CONTENT,
        "cashout_successful": CASHOUT_SUCCESSFUL_EMAIL_CONTENT,
        "cashout_failed": CASHOUT_FAILED_EMAIL_CONTENT,
        "cashout_processing": CASHOUT_PROCESSING_EMAIL_CONTENT,
        "out_of_waitlist": OUT_OF_WAITLIST_EMAIL_CONTENT,
        "scratchcard_bug": SCRATCHCARD_BUG_EMAIL_CONTENT,
        "bulk_action_results": BULK_ACTION_RESULTS_EMAIL_CONTENT,
        "bulk_translate_results": BULK_TRANSLATE_RESULTS_EMAIL_CONTENT,
        "booster_credits": BOOSTER_CREDITS_EMAIL_CONTENT,
        "du_support_ticket_closed": DU_SUPPORT_TICKET_CLOSED_EMAIL_CONTENT,
        "dco_support_ticket_closed": DCO_SUPPORT_TICKET_CLOSED_EMAIL_CONTENT,
        "dco_support_ticket_false_positive": DCO_SUPPORT_TICKET_FALSE_POSITIVE_EMAIL_CONTENT,
        "cashout_enabled": CASHOUT_ENABLED_EMAIL_CONTENT,
        "cashout_disabled": CASHOUT_DISABLED_EMAIL_CONTENT,
        "referral_experiment_a": REFERRAL_EXPERIMENT_A_CONTENT,
        "referral_experiment_b": REFERRAL_EXPERIMENT_B_CONTENT,
        "referral_experiment_c": REFERRAL_EXPERIMENT_C_CONTENT,
        "mcp_token": MCP_TOKEN_EMAIL_CONTENT,
        "mcp_token_denied": MCP_TOKEN_DENIED_EMAIL_CONTENT,
        "data_takeout_ready": DATA_TAKEOUT_READY_EMAIL_CONTENT,
    }


async def _get_user_id_from_email(email: str) -> str | None:
    """Get user ID from email address."""
    async with get_async_session() as session:
        result = await session.exec(select(User).where(col(User.email) == email))  # type: ignore[call-overload]
        user = result.scalar_one_or_none()
        return user.user_id if user else None


def _enrich_template_params(template_params: dict[str, Any], ai_model_count: int) -> dict[str, Any]:
    """Merge standard template variables into the caller-supplied params."""
    if ai_model_count < 100:
        formatted_count = str(ai_model_count)
    else:
        formatted_count = f"{(ai_model_count // 100) * 100}+"

    return {
        **template_params,
        "brand_name": BRAND_NAME,
        "signature": SIGNATURE,
        "credits": template_params.get("credits", INVITE_FRIEND_BONUS_CREDITS),
        "referral_bonus_referrer_credits": get_referral_bonus_referrer_credits_formatted(),
        "yupp_link": YUPP_LINK,
        "head_over_to_yupp_link": HEAD_OVER_TO_YUPP_LINK,
        "discord_link": DISCORD_LINK,
        "profile_link": PROFILE_LINK,
        "current_date": datetime.now(UTC).strftime("%B %d, %Y").upper(),
        "ai_model_count_formatted": formatted_count,
    }


def _render_email_content(campaign: str, enriched_params: dict[str, Any]) -> EmailContent:
    """Render the final EmailContent from pre-enriched template params."""
    campaign_data = _get_email_campaigns()[campaign]
    try:
        subject = campaign_data.subject.format(**enriched_params)
        preview_text = campaign_data.preview.format(**enriched_params) if campaign_data.preview else None
        body_html = (
            load_html_wrapper()
            .replace(
                "{{content}}",
                campaign_data.body_html.format(**enriched_params) if campaign_data.body_html else "",
            )
            .replace(
                "{{unsubscribe_link}}",
                UNSUBSCRIBE_LINK.format(**enriched_params) if "unsubscribe_link" in enriched_params else "",
            )
            .replace("{{current_year}}", datetime.now(UTC).strftime("%Y"))
        )
        return EmailContent(
            subject=subject,
            preview=preview_text,
            body_html=body_html,
        )
    except KeyError as e:
        raise ValueError(f"Missing required parameter: {e}") from e


async def _prepare_email_content(campaign: str, template_params: dict[str, Any]) -> EmailContent:
    """Prepare email content for a campaign.

    Returns:
        EmailContent object with subject, preview, body, and body_html
    """
    if campaign not in _get_email_campaigns():
        raise ValueError(f"Campaign '{campaign}' not found")

    try:
        ai_model_count = await get_active_model_count()
    except Exception as e:
        logging.error("Failed to get active model count, defaulting to 900", exc_info=e)
        ai_model_count = 900

    return _render_email_content(campaign, _enrich_template_params(template_params, ai_model_count))


async def _log_emails_to_db(email_configs: list[EmailConfig]) -> list[uuid.UUID]:
    """Log sent emails to the database.

    Args:
        email_configs: List of email configurations (campaign, to_address, template_params)
    Returns:
        List of email log IDs if the email was sent successfully, otherwise an empty list.
    """
    email_logs = [
        EmailLogs(
            email_sent_to=config.to_address,
            campaign_name=f"{config.campaign}_{datetime.now(UTC).strftime('%Y_%m:02d')}"
            if config.campaign == "monthly_summary"
            else config.campaign,
        )
        for config in email_configs
    ]
    try:
        async with get_async_session() as session, session.begin():
            session.add_all(email_logs)
            await session.commit()
            return [email_log.email_log_id for email_log in email_logs]
    except Exception as e:
        logging.error("Failed to log emails to database", exc_info=e)
        return []


def _create_email_params(
    from_address: FromEmailAddressType,
    to_address: str,
    email_title: str,
    email_body: str,
    email_body_html: str | None,
) -> resend.Emails.SendParams:
    # TODO(w): pass this in as a param
    # user_id = await _get_user_id_from_email(to_address)

    """Create email parameters for Resend API."""
    params: resend.Emails.SendParams = {
        "from": from_address,
        "to": [to_address],
        "reply_to": REPLY_TO_ADDRESS,
        "subject": email_title,
        "text": email_body,
    }
    if email_body_html:
        params["html"] = email_body_html
    # TODO(w): Enable unsub link once UI is ready
    # if user_id:
    #     params["headers"] = {"List-Unsubscribe": f"https://yupp.ai/unsubscribe/{user_id}"}
    return params


async def send_email_async(email_config: EmailConfig, print_only: bool = False) -> uuid.UUID | None:
    """Send an email to a single recipient using the specified campaign template."""

    # In staging environments, skip sending emails to end-to-end test accounts, as those emails will always bounce
    # and hurt our delivery rate
    if settings.ENVIRONMENT != "production":
        if await _is_test_account_email(email_config.to_address):
            logging.info(
                f"Skipping email to test account {email_config.to_address} for campaign {email_config.campaign} "
                f"(environment: {settings.ENVIRONMENT})"
            )
            return None

    email_content = await _prepare_email_content(email_config.campaign, email_config.template_params)
    email_plaintext = html_to_plaintext(email_content.body_html)
    resend_params = _create_email_params(
        email_config.from_address,
        email_config.to_address,
        _get_subject(email_content.subject),
        email_plaintext,
        email_content.body_html,
    )

    if settings.RESEND_API_KEY and not print_only:
        resend.api_key = settings.RESEND_API_KEY
        await asyncio.to_thread(resend.Emails.send, resend_params)
        email_log_id = await _log_emails_to_db([email_config])
        logging.info(f"Email sent for {email_config.campaign}")
        return email_log_id[0] if email_log_id else None
    log_dict = {
        "message": "No Resend API key for this environment, logging the content instead",
        "resend_params": resend_params,
    }
    logging.info(json_dumps(log_dict))
    return None


def _is_test_account_email_sync(email: str) -> bool:
    """Check if an email address belongs to a user with TEST role (synchronous version)."""
    with Session(get_engine_read_replica()) as session:
        result = session.exec(
            select(User.email, User.role).where(  # type: ignore[call-overload]
                func.lower(col(User.email)) == email.lower()
            )
        )
        row = result.first()
        return row is not None and UserRole.TEST in row[1]


def _prepare_email_content_sync(campaign: str, template_params: dict[str, Any]) -> EmailContent:
    """Prepare email content for a campaign (synchronous version)."""
    if campaign not in _get_email_campaigns():
        raise ValueError(f"Campaign '{campaign}' not found")

    try:
        ai_model_count = get_active_model_count_sync()
    except Exception as e:
        logging.error("Failed to get active model count, defaulting to 900", exc_info=e)
        ai_model_count = 900

    return _render_email_content(campaign, _enrich_template_params(template_params, ai_model_count))


def _log_emails_to_db_sync(email_configs: list[EmailConfig]) -> list[uuid.UUID]:
    """Log sent emails to the database (synchronous version)."""
    email_logs = [
        EmailLogs(
            email_sent_to=config.to_address,
            campaign_name=f"{config.campaign}_{datetime.now(UTC).strftime('%Y_%m:02d')}"
            if config.campaign == "monthly_summary"
            else config.campaign,
        )
        for config in email_configs
    ]
    try:
        with Session(get_engine()) as session:
            session.add_all(email_logs)
            session.commit()
            return [email_log.email_log_id for email_log in email_logs]
    except Exception as e:
        logging.error("Failed to log emails to database", exc_info=e)
        return []


def send_email_sync(email_config: EmailConfig, print_only: bool = False) -> uuid.UUID | None:
    """Send an email to a single recipient using the specified campaign template (synchronous version).

    Mirrors send_email_async but uses only synchronous DB and HTTP calls, making it safe
    to call from synchronous contexts (e.g. cron jobs) without asyncio event loop conflicts.
    """
    # In staging environments, skip sending emails to end-to-end test accounts, as those emails will always bounce
    # and hurt our delivery rate
    if settings.ENVIRONMENT != "production":
        if _is_test_account_email_sync(email_config.to_address):
            logging.info(
                f"Skipping email to test account {email_config.to_address} for campaign {email_config.campaign} "
                f"(environment: {settings.ENVIRONMENT})"
            )
            return None

    email_content = _prepare_email_content_sync(email_config.campaign, email_config.template_params)
    email_plaintext = html_to_plaintext(email_content.body_html)
    resend_params = _create_email_params(
        email_config.from_address,
        email_config.to_address,
        _get_subject(email_content.subject),
        email_plaintext,
        email_content.body_html,
    )

    if settings.RESEND_API_KEY and not print_only:
        resend.api_key = settings.RESEND_API_KEY
        resend.Emails.send(resend_params)
        email_log_ids = _log_emails_to_db_sync([email_config])
        logging.info(f"Email sent for {email_config.campaign}")
        return email_log_ids[0] if email_log_ids else None

    log_dict = {
        "message": "No Resend API key for this environment, logging the content instead",
        "resend_params": resend_params,
    }
    logging.info(json_dumps(log_dict))
    return None


async def batch_send_emails_async(
    email_configs: list[EmailConfig],
    only_once: bool = False,
    sleep_between_chunks: int = 1,
) -> BatchEmailResult:
    """Send emails to multiple recipients with different campaigns in batch.

    Args:
        email_configs: List of email configurations (campaign, to_address, template_params)
        only_once: If True, only send emails to recipients who haven't received this campaign before
        sleep_between_chunks: Time to sleep between chunks in seconds

    Returns:
        BatchEmailResult containing lists of successful and failed email addresses
    """
    batch_params = []
    filtered_configs = []

    # In staging environments, skip sending emails to end-to-end test accounts, as those emails will always bounce
    # and hurt our delivery rate.
    if settings.ENVIRONMENT != "production":
        all_emails = [config.to_address for config in email_configs]
        test_account_emails_set = await _get_test_account_emails(all_emails)
        if test_account_emails_set:
            test_account_emails_list = [email for email in all_emails if email.lower() in test_account_emails_set]
            test_account_preview = f"{test_account_emails_list[:5]}{'...' if len(test_account_emails_list) > 5 else ''}"
            logging.info(
                f"Skipping {len(test_account_emails_list)} test account emails in non-production environment "
                f"({settings.ENVIRONMENT}): {test_account_preview}"
            )
        email_configs = [config for config in email_configs if config.to_address.lower() not in test_account_emails_set]

    # If only_once is True, check which emails have already been sent
    if only_once:
        async with get_async_session_read_replica() as session:
            # Get all email addresses and campaigns we need to check
            email_campaign_pairs = [(config.to_address, config.campaign) for config in email_configs]

            # Do a single query to get all existing email logs
            query = select(EmailLogs.email_sent_to, EmailLogs.campaign_name).where(  # type: ignore
                tuple_(EmailLogs.email_sent_to, EmailLogs.campaign_name).in_(email_campaign_pairs)  # type: ignore
            )
            result = await session.execute(query)
            existing_logs = {(row.email_sent_to, row.campaign_name) for row in result}

            # Filter configs based on the query results
            filtered_configs = [
                config for config in email_configs if (config.to_address, config.campaign) not in existing_logs
            ]

            # Log skipped emails
            for config in email_configs:
                if (config.to_address, config.campaign) in existing_logs:
                    logging.info(f"Skipping {config.to_address} for {config.campaign} because email was already sent")
    else:
        filtered_configs = email_configs

    # Prepare email parameters for each recipient
    for email_config in filtered_configs:
        email_content = await _prepare_email_content(email_config.campaign, email_config.template_params)
        email_plaintext = html_to_plaintext(email_content.body_html)
        batch_params.append(
            _create_email_params(
                email_config.from_address,
                email_config.to_address,
                _get_subject(email_content.subject),
                email_plaintext,
                email_content.body_html,
            )
        )

    if settings.RESEND_API_KEY:
        resend.api_key = settings.RESEND_API_KEY
        successful_emails: list[str] = []
        failed_emails: list[str] = []
        # Split into chunks of 100 and send with sleep between chunks
        chunk_size = 100
        total_chunks = (len(batch_params) + chunk_size - 1) // chunk_size
        for chunk_num, (chunk, chunk_configs) in enumerate(
            zip(chunked(batch_params, chunk_size), chunked(filtered_configs, chunk_size), strict=True), 1
        ):
            logging.info(f"Sending chunk {chunk_num} of {total_chunks}")
            try:
                await _send_batch_async(chunk)
                await _log_emails_to_db(chunk_configs)
                # If batch send succeeded, all emails in the chunk are considered successful
                successful_emails.extend(config.to_address for config in chunk_configs)
            except Exception as e:
                logging.error(
                    json_dumps({"message": "Failed to send chunk", "chunk_num": str(chunk_num)}),
                    exc_info=e,
                )
                # If batch send failed, all emails in the chunk are considered failed
                failed_emails.extend(config.to_address for config in chunk_configs)

            # Sleep for 1 second between chunks if there are more chunks to send
            if chunk_num < total_chunks:
                await asyncio.sleep(sleep_between_chunks)

        logging.info(f"Email count sent in batch: {len(filtered_configs)}")
        return BatchEmailResult(successful_emails=successful_emails, failed_emails=failed_emails)
    log_dict = {
        "message": "No Resend API key for this environment, logging the content instead",
        "batch_params": batch_params,
    }
    logging.info(json_dumps(log_dict))
    return BatchEmailResult(successful_emails=[], failed_emails=[])


def _get_subject(default_subject: str) -> str:
    return default_subject if settings.ENVIRONMENT == "production" else f"[{settings.ENVIRONMENT}] {default_subject}"


async def create_email_configs_from_recipients(
    campaign: str,
    recipients: list[BatchEmailRecipient],
    from_address: FromEmailAddressType,
) -> tuple[list[EmailConfig], list[str]]:
    """Create email configurations from a list of recipients.

    Args:
        campaign: The email campaign to use
        recipients: List of BatchEmailRecipient objects
        from_address: The email address to send from

    Returns:
        Tuple of (email_configs, failed_emails)
    """
    email_configs = []
    failed_emails = []

    for recipient in recipients:
        template_params = recipient.params.copy() if recipient.params else {}

        if recipient.name:
            template_params["email_recipient_name"] = recipient.name

        try:
            # Get email subject, preview, and body.
            await _prepare_email_content(campaign, template_params)

            email_config = EmailConfig(
                campaign=campaign,
                to_address=recipient.email,
                template_params=template_params,
                from_address=from_address,
            )
            email_configs.append(email_config)
            logging.info(f"Created email config for {recipient.email} with campaign {campaign}")
        except ValueError as e:
            logging.error(
                json_dumps({"message": "Failed to create email config", "recipient_email": recipient.email}),
                exc_info=e,
            )
            failed_emails.append(recipient.email)
            continue
        except KeyError as e:
            logging.error(
                json_dumps({"message": "Missing required template parameter", "recipient_email": recipient.email}),
                exc_info=e,
            )
            failed_emails.append(recipient.email)
            continue
        except Exception as e:
            logging.error(
                json_dumps({"message": "Failed to create email config", "recipient_email": recipient.email}),
                exc_info=e,
            )
            failed_emails.append(recipient.email)
            continue

    return email_configs, failed_emails
