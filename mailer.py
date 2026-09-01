"""Send outbound email via SMTP (STARTTLS).

Configured in .env (section 7): SMTP_HOST, SMTP_PORT, SMTP_USERNAME,
SMTP_PASSWORD, and optionally SMTP_FROM (defaults to SMTP_USERNAME).

Standalone send:
    python mailer.py "someone@example.com" "Subject line" "Body text"

Verify credentials without sending anything:
    python mailer.py --test
"""
import argparse
import smtplib
import ssl
from email.message import EmailMessage

from config import SMTP_HOST, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD, SMTP_FROM


class MailerError(RuntimeError):
    """Raised when a mail can't be sent, with a message safe to show a user."""


def _require_configured() -> None:
    if not (SMTP_HOST and SMTP_USERNAME and SMTP_PASSWORD):
        raise MailerError(
            "SMTP is not configured — set SMTP_HOST, SMTP_USERNAME, and "
            "SMTP_PASSWORD in .env (see section 7)"
        )


def _connect() -> smtplib.SMTP:
    """Opens an authenticated STARTTLS connection, or raises MailerError."""
    try:
        smtp = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30)
        smtp.ehlo()
        smtp.starttls(context=ssl.create_default_context())
        smtp.ehlo()
        smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
        return smtp
    except smtplib.SMTPAuthenticationError as e:
        raise MailerError(f"SMTP authentication failed: {e}") from e
    except (smtplib.SMTPException, OSError) as e:
        raise MailerError(f"Could not connect to {SMTP_HOST}:{SMTP_PORT}: {e}") from e


def send_email(to: str, subject: str, body: str) -> None:
    """Send a plain-text email over SMTP with STARTTLS.

    Raises MailerError (never smtplib's own exceptions) so callers — the
    CLI here and the /api/send-email handler in main.py — can show a
    readable message without knowing SMTP internals.
    """
    to = (to or "").strip()
    if not to:
        raise MailerError("Recipient email is required")
    _require_configured()

    message = EmailMessage()
    message["Subject"] = subject or "(no subject)"
    message["From"] = SMTP_FROM
    message["To"] = to
    message.set_content(body or "")

    smtp = _connect()
    try:
        smtp.send_message(message)
    except smtplib.SMTPException as e:
        raise MailerError(f"Send failed: {e}") from e
    finally:
        smtp.quit()


def test_connection() -> None:
    """Verifies SMTP_HOST/PORT/USERNAME/PASSWORD authenticate, without
    sending any mail — for confirming .env is set up correctly."""
    _require_configured()
    smtp = _connect()
    smtp.quit()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Send an email via SMTP.")
    parser.add_argument("to", nargs="?", help="Recipient email address")
    parser.add_argument("subject", nargs="?", help="Email subject")
    parser.add_argument("body", nargs="?", help="Email body (plain text)")
    parser.add_argument(
        "--test", action="store_true", help="Verify SMTP credentials without sending mail"
    )
    args = parser.parse_args()

    try:
        if args.test:
            test_connection()
            print(f"Connected and authenticated to {SMTP_HOST}:{SMTP_PORT} as {SMTP_USERNAME}.")
        else:
            if not (args.to and args.subject is not None and args.body is not None):
                parser.error("to, subject, and body are required unless using --test")
            send_email(args.to, args.subject, args.body)
            print(f"Sent to {args.to}")
    except MailerError as e:
        print(f"Error: {e}")
        raise SystemExit(1)
