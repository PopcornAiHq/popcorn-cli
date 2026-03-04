"""Authentication — PKCE, OIDC discovery, OAuth callback, token exchange."""

from __future__ import annotations

import base64
import hashlib
import secrets
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import jwt

from .config import OAUTH_CALLBACK_PORT
from .errors import AuthError, PopcornError


def pkce_pair() -> tuple[str, str]:
    """Generate PKCE code_verifier and S256 code_challenge."""
    verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def discover_oidc(issuer: str) -> dict[str, str]:
    """Fetch OIDC configuration from the issuer."""
    url = f"{issuer.rstrip('/')}/.well-known/openid-configuration"
    try:
        resp = httpx.get(url, timeout=10.0)
        resp.raise_for_status()
    except httpx.ConnectError as e:
        raise AuthError(
            f"Cannot reach OIDC server at {issuer}. Check your network connection."
        ) from e
    except httpx.TimeoutException as e:
        raise AuthError(f"OIDC discovery timed out for {issuer}") from e
    except httpx.HTTPStatusError as e:
        raise AuthError(f"OIDC discovery failed: HTTP {e.response.status_code} from {url}") from e
    try:
        data = resp.json()
        return {
            "authorization_endpoint": data["authorization_endpoint"],
            "token_endpoint": data["token_endpoint"],
        }
    except (KeyError, ValueError) as e:
        raise AuthError(f"Invalid OIDC configuration from {issuer}: {e}") from e


def login_with_token(token: str) -> dict[str, Any]:
    """Validate a JWT and extract claims. Returns {email, exp, token}."""
    try:
        claims = jwt.decode(token, options={"verify_signature": False})
    except jwt.DecodeError as e:
        raise AuthError("Invalid JWT token") from e
    email = claims.get("email", "")
    if not email:
        raise AuthError("Token has no email claim")
    return {"email": email, "exp": claims.get("exp", 0), "token": token}


def exchange_code_for_tokens(
    token_endpoint: str,
    code: str,
    redirect_uri: str,
    client_id: str,
    code_verifier: str,
) -> dict[str, Any]:
    """Exchange auth code for tokens. Returns {id_token, access_token, refresh_token, email, exp}."""
    try:
        resp = httpx.post(
            token_endpoint,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "code_verifier": code_verifier,
            },
            timeout=10.0,
        )
    except httpx.ConnectError as e:
        raise AuthError(f"Cannot reach token endpoint at {token_endpoint}") from e
    except httpx.TimeoutException as e:
        raise AuthError("Token exchange timed out") from e
    except httpx.HTTPError as e:
        raise AuthError(f"Token exchange network error: {e}") from e
    if resp.status_code != 200:
        raise AuthError(f"Token exchange failed: {resp.status_code} {resp.text[:200]}")

    data = resp.json()
    id_token = data.get("id_token", "")
    if not id_token:
        raise AuthError("No id_token in token response")

    claims = jwt.decode(id_token, options={"verify_signature": False})
    return {
        "id_token": id_token,
        "access_token": data.get("access_token", ""),
        "refresh_token": data.get("refresh_token", ""),
        "email": claims.get("email", ""),
        "exp": claims.get("exp", 0),
    }


class CallbackHandler(BaseHTTPRequestHandler):
    """Captures the OAuth callback code."""

    auth_code: str | None = None
    error: str | None = None
    expected_state: str | None = None

    def do_GET(self) -> None:
        qs = parse_qs(urlparse(self.path).query)

        # Validate OAuth state parameter to prevent CSRF
        received_state = qs.get("state", [None])[0]
        if CallbackHandler.expected_state and received_state != CallbackHandler.expected_state:
            CallbackHandler.error = "state_mismatch"
            self._respond(
                "<h2>Error: state mismatch</h2><p>Possible CSRF attack. Please try again.</p>"
            )
            return

        if "code" in qs:
            CallbackHandler.auth_code = qs["code"][0]
            self._respond(
                "<h2>Authenticated!</h2><p>You can close this tab and return to the terminal.</p>"
            )
        else:
            CallbackHandler.error = qs.get("error", ["unknown"])[0]
            detail = qs.get("error_description", [""])[0]
            self._respond(f"<h2>Error: {CallbackHandler.error}</h2><p>{detail}</p>")

    def _respond(self, body: str) -> None:
        html = (
            "<!DOCTYPE html><html><head>"
            '<meta charset="utf-8">'
            "<title>Popcorn CLI</title>"
            "<style>body{font-family:system-ui;max-width:480px;"
            "margin:60px auto;text-align:center}</style>"
            f"</head><body>{body}</body></html>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(html.encode())

    def log_message(self, format: str, *args: Any) -> None:
        pass  # silence HTTP server logs


def run_callback_server() -> HTTPServer:
    """Start a localhost HTTP server on the fixed OAuth callback port."""
    try:
        server = HTTPServer(("127.0.0.1", OAUTH_CALLBACK_PORT), CallbackHandler)
    except OSError as e:
        raise PopcornError(
            f"Port {OAUTH_CALLBACK_PORT} is in use. Close the other process and try again."
        ) from e
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    return server
