"""Email delivery application service using SMTP with HTML and plain-text fallbacks."""

import asyncio
import logging
import smtplib
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from typing import Any

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)


class EmailDeliveryError(Exception):
    """Raised when outbound email dispatch fails."""

    pass


class EmailService:
    """Outbound email delivery service supporting SMTP, STARTTLS, and SSL."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        use_mock_transport: bool = False,
    ) -> None:
        """Initialize EmailService with configuration and optional mock transport for testing."""
        self.settings = settings or get_settings()
        self.use_mock_transport = use_mock_transport
        self.sent_messages: list[dict[str, Any]] = []

    def build_verification_url(self, raw_token: str) -> str:
        """Construct public frontend verification link with token in URI fragment.

        Placing the token in the URI fragment (#token=...) ensures that browsers
        never transmit the raw token in HTTP request lines to the frontend web server,
        CDN, or reverse proxy, and prevents the token from leaking into server access logs,
        Referer headers to third-party assets, or external telemetry.
        """
        base_url = self.settings.FRONTEND_BASE_URL.rstrip("/")
        return f"{base_url}/verify-email#token={raw_token}"

    def create_verification_message(
        self,
        to_email: str,
        verification_url: str,
        expires_in_minutes: int,
    ) -> MIMEMultipart:
        """Assemble multipart MIME email with both HTML and plain-text representations."""
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"Verify your email - {self.settings.APP_NAME}"
        msg["From"] = formataddr(
            (
                str(Header(self.settings.SMTP_FROM_NAME, "utf-8")),
                self.settings.SMTP_FROM_EMAIL,
            )
        )
        msg["To"] = to_email

        plain_text = (
            f"Hello,\n\n"
            f"Thank you for registering with {self.settings.APP_NAME}.\n\n"
            f"Please verify your email address ({to_email}) by visiting the link below:\n"
            f"{verification_url}\n\n"
            f"This link will expire in {expires_in_minutes} minutes.\n\n"
            f"If you did not request this verification, you can safely ignore this email.\n\n"
            f"Best regards,\n"
            f"The {self.settings.APP_NAME} Team\n"
        )

        html_content = (
            "<!DOCTYPE html>\n"
            "<html>\n"
            "<head><meta charset='utf-8'><title>Verify your email</title></head>\n"
            "<body style='font-family: sans-serif; line-height: 1.6; color: #1f2937; margin: 0; "
            "padding: 20px;'>\n"
            "  <div style='max-width: 600px; margin: 0 auto; background: #ffffff; padding: 24px; "
            "border: 1px solid #e5e7eb; border-radius: 8px;'>\n"
            "    <h2 style='color: #111827; margin-top: 0;'>Verify your email address</h2>\n"
            "    <p>Hello,</p>\n"
            f"    <p>Thank you for registering with <strong>{self.settings.APP_NAME}</strong>. "
            f"Please confirm that <strong>{to_email}</strong> is your email address:</p>\n"
            "    <p style='margin: 24px 0;'>\n"
            f"      <a href='{verification_url}' style='background-color: #2563eb; color: #ffffff; "
            "padding: 12px 24px; text-decoration: none; border-radius: 6px; font-weight: 600; "
            "display: inline-block;'>\n"
            "        Verify Email\n"
            "      </a>\n"
            "    </p>\n"
            "    <p style='font-size: 0.9em; color: #4b5563;'>Or copy this link into browser:</p>\n"
            f"    <p style='word-break: break-all; font-size: 0.85em; color: #2563eb;'>"
            f"<a href='{verification_url}'>{verification_url}</a></p>\n"
            f"    <p style='font-size: 0.85em; color: #6b7280;'>"
            f"This link will expire in {expires_in_minutes} minutes.</p>\n"
            "    <hr style='border: none; border-top: 1px solid #e5e7eb; margin: 24px 0;' />\n"
            "    <p style='font-size: 0.8em; color: #9ca3af; margin-bottom: 0;'>\n"
            "      If you did not request this email, you can safely ignore it.\n"
            "    </p>\n"
            "  </div>\n"
            "</body>\n"
            "</html>"
        )

        part_text = MIMEText(plain_text, "plain", "utf-8")
        part_html = MIMEText(html_content, "html", "utf-8")

        msg.attach(part_text)
        msg.attach(part_html)
        return msg

    def _send_smtp_sync(self, msg: MIMEMultipart, to_email: str) -> None:
        """Execute synchronous SMTP transmission."""
        if self.settings.SMTP_USE_SSL:
            server: smtplib.SMTP = smtplib.SMTP_SSL(
                self.settings.SMTP_HOST,
                self.settings.SMTP_PORT,
                timeout=10.0,
            )
        else:
            server = smtplib.SMTP(
                self.settings.SMTP_HOST,
                self.settings.SMTP_PORT,
                timeout=10.0,
            )

        try:
            if not self.settings.SMTP_USE_SSL and self.settings.SMTP_USE_TLS:
                server.starttls()

            if self.settings.SMTP_USERNAME and self.settings.SMTP_PASSWORD:
                server.login(self.settings.SMTP_USERNAME, self.settings.SMTP_PASSWORD)

            server.send_message(msg)
        finally:
            try:
                server.quit()
            except Exception:
                pass

    async def send_verification_email(
        self,
        to_email: str,
        raw_token: str,
        expires_in_minutes: int | None = None,
    ) -> bool:
        """Deliver an email verification message containing the single-use token link.

        Returns True if sent successfully, or False if delivery failed.
        Never raises unhandled SMTP exceptions or leaks raw token data to callers.
        """
        expiry_minutes = (
            expires_in_minutes
            if expires_in_minutes is not None
            else self.settings.EMAIL_VERIFICATION_TOKEN_EXPIRE_MINUTES
        )
        verification_url = self.build_verification_url(raw_token)
        msg = self.create_verification_message(
            to_email=to_email,
            verification_url=verification_url,
            expires_in_minutes=expiry_minutes,
        )

        if self.use_mock_transport:
            self.sent_messages.append(
                {
                    "to_email": to_email,
                    "verification_url": verification_url,
                    "expires_in_minutes": expiry_minutes,
                    "msg": msg,
                }
            )
            logger.info("Mock email dispatched to recipient: %s", to_email)
            return True

        try:
            await asyncio.to_thread(self._send_smtp_sync, msg, to_email)
            logger.info("Verification email successfully delivered to recipient: %s", to_email)
            return True
        except Exception as exc:
            logger.warning(
                "Failed to deliver verification email to %s via SMTP (%s: %s). "
                "Account created; user may request resend.",
                to_email,
                type(exc).__name__,
                exc,
            )
            return False


# Global default service instance
_default_email_service: EmailService | None = None


def get_email_service() -> EmailService:
    """Retrieve or create default EmailService instance."""
    global _default_email_service
    if _default_email_service is None:
        _default_email_service = EmailService()
    return _default_email_service


def set_email_service(service: EmailService | None) -> None:
    """Override or reset default EmailService instance (primarily for testing)."""
    global _default_email_service
    _default_email_service = service
