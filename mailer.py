"""Send outbound email via Microsoft Graph (OAuth2 device-code sign-in).

Switched from SMTP AUTH because this org's M365 tenant rejects legacy SMTP
login — see .env section 7 for the Azure AD app registration this needs.

One-time setup, run from a terminal (not through the server, since a
device-code login needs a human to visit a URL and type a code):

    python mailer.py --login

That signs in as janak@stradit.com and caches the resulting token in
.msal_token_cache.json. Every send after that — including from the
frontend's Send Mail button — reuses and silently refreshes it, with no
further interactive login until the cached refresh token itself expires.

Standalone send:
    python mailer.py "someone@example.com" "Subject line" "Body text"
"""
import argparse
import os

import msal
import requests

from config import GRAPH_CLIENT_ID, GRAPH_TENANT_ID

AUTHORITY = f"https://login.microsoftonline.com/{GRAPH_TENANT_ID}"
SCOPES = ["Mail.Send"]
GRAPH_SEND_URL = "https://graph.microsoft.com/v1.0/me/sendMail"
TOKEN_CACHE_PATH = os.path.join(os.path.dirname(__file__), ".msal_token_cache.json")


class MailerError(RuntimeError):
    """Raised when a mail can't be sent, with a message safe to show a user."""


def _load_cache() -> msal.SerializableTokenCache:
    cache = msal.SerializableTokenCache()
    if os.path.exists(TOKEN_CACHE_PATH):
        with open(TOKEN_CACHE_PATH, encoding="utf-8") as fh:
            cache.deserialize(fh.read())
    return cache


def _save_cache(cache: msal.SerializableTokenCache) -> None:
    if cache.has_state_changed:
        with open(TOKEN_CACHE_PATH, "w", encoding="utf-8") as fh:
            fh.write(cache.serialize())


def _app(cache: msal.SerializableTokenCache) -> msal.PublicClientApplication:
    if not GRAPH_CLIENT_ID:
        raise MailerError("GRAPH_CLIENT_ID is not set in .env — see section 7")
    return msal.PublicClientApplication(
        GRAPH_CLIENT_ID, authority=AUTHORITY, token_cache=cache
    )


def _get_token(interactive: bool = False) -> str:
    """Return a valid Graph access token, refreshing silently from the cache.

    Never starts a device-code login unless `interactive=True` — the
    server-side send path (the frontend's button) must fail fast with a
    clear message instead of hanging, since nobody can type a device code
    into a background HTTP request.
    """
    cache = _load_cache()
    app = _app(cache)

    accounts = app.get_accounts()
    result = app.acquire_token_silent(SCOPES, account=accounts[0]) if accounts else None

    if not result:
        if not interactive:
            raise MailerError(
                "Not signed in to Microsoft yet (or the cached session expired). "
                "Run `python mailer.py --login` from a terminal once, then "
                "Send Mail will work from the app."
            )
        flow = app.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            raise MailerError(
                f"Could not start device login: {flow.get('error_description', flow)}"
            )
        print(flow["message"])
        result = app.acquire_token_by_device_flow(flow)

    _save_cache(cache)

    if not result or "access_token" not in result:
        detail = (result or {}).get("error_description", "no token returned")
        raise MailerError(f"Microsoft sign-in failed: {detail}")
    return result["access_token"]


def send_email(to: str, subject: str, body: str) -> None:
    """Send a plain-text email as the signed-in Microsoft account.

    Raises MailerError (never msal's or requests' own exceptions) so
    callers — the CLI here and the /api/send-email handler in main.py —
    can show a readable message without knowing Graph internals.
    """
    to = (to or "").strip()
    if not to:
        raise MailerError("Recipient email is required")

    token = _get_token(interactive=False)

    message = {
        "message": {
            "subject": subject or "(no subject)",
            "body": {"contentType": "Text", "content": body or ""},
            "toRecipients": [{"emailAddress": {"address": to}}],
        },
        "saveToSentItems": True,
    }

    try:
        resp = requests.post(
            GRAPH_SEND_URL,
            headers={"Authorization": f"Bearer {token}"},
            json=message,
            timeout=30,
        )
    except requests.RequestException as e:
        raise MailerError(f"Could not reach Microsoft Graph: {e}") from e

    if resp.status_code != 202:
        try:
            detail = resp.json().get("error", {}).get("message", resp.text)
        except ValueError:
            detail = resp.text
        raise MailerError(f"Graph sendMail failed ({resp.status_code}): {detail}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Send an email via Microsoft Graph.")
    parser.add_argument("to", nargs="?", help="Recipient email address")
    parser.add_argument("subject", nargs="?", help="Email subject")
    parser.add_argument("body", nargs="?", help="Email body (plain text)")
    parser.add_argument(
        "--login", action="store_true", help="Interactive one-time device-code sign-in"
    )
    args = parser.parse_args()

    try:
        if args.login:
            _get_token(interactive=True)
            print("Signed in and cached. Send Mail will now work without further login.")
        else:
            if not (args.to and args.subject is not None and args.body is not None):
                parser.error("to, subject, and body are required unless using --login")
            send_email(args.to, args.subject, args.body)
            print(f"Sent to {args.to}")
    except MailerError as e:
        print(f"Error: {e}")
        raise SystemExit(1)
