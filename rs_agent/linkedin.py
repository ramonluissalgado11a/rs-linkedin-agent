"""Authorized LinkedIn integration layer.

* OAuth 2.0 authorization-code flow (3-legged). Passwords are never handled.
* Tokens are encrypted at rest (Fernet) and never logged.
* Granted scopes are detected dynamically via token introspection and stored in
  api_permissions; features degrade gracefully when a scope is missing.
* Only the Posts API (own content) and OpenID userinfo are implemented — the
  endpoints this app can legitimately be granted. No scraping, no browser automation.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import requests
from cryptography.fernet import Fernet, InvalidToken

from .config import Settings
from .db import Database, utcnow
from .errors import AgentError, ErrorCategory, classify_http_status
from .logging_setup import new_id

AUTH_URL = "https://www.linkedin.com/oauth/v2/authorization"
TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"  # nosec B105 # noqa: S105 - URL, not a secret
INTROSPECT_URL = "https://www.linkedin.com/oauth/v2/introspectToken"
USERINFO_URL = "https://api.linkedin.com/v2/userinfo"
POSTS_URL = "https://api.linkedin.com/rest/posts"
KNOWN_SCOPES = ("openid", "profile", "email", "w_member_social", "w_organization_social",
                "w_member_social_feed", "w_organization_social_feed", "r_member_social")
STATE_TTL = timedelta(minutes=10)


class TokenCipher:
    def __init__(self, key: str | None) -> None:
        if not key:
            raise AgentError("TOKEN_ENCRYPTION_KEY is not configured", ErrorCategory.VALIDATION)
        try:
            self._f = Fernet(key.encode())
        except (ValueError, TypeError) as exc:
            # Hosting dashboards (e.g. Render `generateValue`) produce random secrets that are not in
            # Fernet's urlsafe format. Accept any high-entropy secret and derive a Fernet key from it.
            if len(key) < 32:
                raise AgentError("TOKEN_ENCRYPTION_KEY must be a Fernet key or a random secret of 32+ chars",
                                 ErrorCategory.VALIDATION) from exc
            import base64
            import hashlib
            derived = hashlib.sha256(b"rs-agent-token-key:" + key.encode()).digest()
            self._f = Fernet(base64.urlsafe_b64encode(derived))

    def encrypt(self, value: str) -> str:
        return str(self._f.encrypt(value.encode()).decode())

    def decrypt(self, value: str) -> str:
        try:
            return str(self._f.decrypt(value.encode()).decode())
        except InvalidToken as exc:
            raise AgentError("stored token cannot be decrypted (key rotated?)", ErrorCategory.AUTHENTICATION) from exc


@dataclass
class TokenBundle:
    access_token: str
    expires_at: datetime | None
    refresh_token: str | None = None
    refresh_expires_at: datetime | None = None
    scopes: tuple[str, ...] = ()
    member_urn: str | None = None


class LinkedInClient:
    def __init__(self, settings: Settings, db: Database, session: requests.Session | None = None) -> None:
        self.settings = settings
        self.db = db
        self.http = session or requests.Session()

    # ---------- OAuth ----------
    def authorization_url(self) -> str:
        if not self.settings.linkedin_configured:
            raise AgentError("LinkedIn OAuth is not configured", ErrorCategory.VALIDATION)
        state = secrets.token_urlsafe(24)
        self.db.execute("INSERT INTO oauth_states (state, created_at) VALUES (?, ?)", (state, utcnow()))
        params = {
            "response_type": "code",
            "client_id": self.settings.linkedin_client_id,
            "redirect_uri": self.settings.linkedin_redirect_uri,
            "state": state,
            "scope": " ".join(self.settings.linkedin_scopes),
        }
        return f"{AUTH_URL}?{urlencode(params)}"

    def _consume_state(self, state: str) -> None:
        row = self.db.fetchone("SELECT created_at FROM oauth_states WHERE state = ?", (state,))
        self.db.execute("DELETE FROM oauth_states WHERE state = ?", (state,))
        if row is None:
            raise AgentError("invalid OAuth state", ErrorCategory.AUTHENTICATION)
        created = datetime.fromisoformat(row["created_at"])
        if datetime.now(UTC) - created > STATE_TTL:
            raise AgentError("expired OAuth state", ErrorCategory.AUTHENTICATION)

    def exchange_code(self, code: str, state: str) -> TokenBundle:
        self._consume_state(state)
        if not code or len(code) > 2048:
            raise AgentError("invalid authorization code", ErrorCategory.VALIDATION)
        data = self._token_request({
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.settings.linkedin_redirect_uri,
        })
        bundle = self._bundle_from_token_response(data)
        bundle.scopes = self.detect_scopes(bundle.access_token, fallback=bundle.scopes)
        bundle.member_urn = self.fetch_member_urn(bundle.access_token) if "openid" in bundle.scopes else None
        self.save_tokens(bundle)
        return bundle

    def refresh(self) -> TokenBundle:
        row = self._token_row()
        if not row["refresh_token_enc"]:
            raise AgentError("no refresh token; re-authorize LinkedIn", ErrorCategory.AUTHENTICATION)
        cipher = TokenCipher(self.settings.token_encryption_key)
        data = self._token_request({
            "grant_type": "refresh_token",
            "refresh_token": cipher.decrypt(row["refresh_token_enc"]),
        })
        bundle = self._bundle_from_token_response(data)
        bundle.member_urn = row["member_urn"]
        bundle.scopes = self.detect_scopes(bundle.access_token, fallback=tuple((row["scopes"] or "").split()))
        self.save_tokens(bundle)
        return bundle

    def _token_request(self, form: dict[str, Any]) -> dict[str, Any]:
        form = {**form, "client_id": self.settings.linkedin_client_id,
                "client_secret": self.settings.linkedin_client_secret}
        try:
            resp = self.http.post(TOKEN_URL, data=form, timeout=20)
        except requests.RequestException as exc:
            raise AgentError(f"LinkedIn token endpoint unreachable: {type(exc).__name__}", ErrorCategory.TEMPORARY) from exc
        if resp.status_code != 200:
            category = classify_http_status(resp.status_code)
            if resp.status_code in (400, 401):
                category = ErrorCategory.AUTHENTICATION
            raise AgentError(f"LinkedIn token request failed ({resp.status_code})", category)
        try:
            payload: dict[str, Any] = resp.json()
        except ValueError as exc:
            raise AgentError("LinkedIn token response was not JSON", ErrorCategory.PROVIDER) from exc
        if not payload.get("access_token"):
            raise AgentError("LinkedIn token response missing access_token", ErrorCategory.PROVIDER)
        return payload

    @staticmethod
    def _bundle_from_token_response(data: dict[str, Any]) -> TokenBundle:
        now = datetime.now(UTC)
        expires = now + timedelta(seconds=int(data["expires_in"])) if data.get("expires_in") else None
        rexp = now + timedelta(seconds=int(data["refresh_token_expires_in"])) if data.get("refresh_token_expires_in") else None
        scopes = tuple(str(data.get("scope", "")).replace(",", " ").split())
        return TokenBundle(data["access_token"], expires, data.get("refresh_token"), rexp, scopes)

    def detect_scopes(self, access_token: str, fallback: tuple[str, ...] = ()) -> tuple[str, ...]:
        """Ask LinkedIn which scopes this token really has; persist the result."""
        scopes = fallback
        try:
            resp = self.http.post(
                INTROSPECT_URL,
                data={"client_id": self.settings.linkedin_client_id,
                      "client_secret": self.settings.linkedin_client_secret, "token": access_token},
                timeout=20,
            )
            if resp.status_code == 200:
                body = resp.json()
                if body.get("active") is False:
                    raise AgentError("LinkedIn token is not active", ErrorCategory.AUTHENTICATION)
                if body.get("scope"):
                    scopes = tuple(str(body["scope"]).replace(",", " ").split())
        except requests.RequestException:
            pass  # keep fallback; introspection is best-effort
        self.store_permissions(scopes)
        return scopes

    def store_permissions(self, scopes: tuple[str, ...]) -> None:
        now = utcnow()
        for scope in sorted(set(KNOWN_SCOPES) | set(scopes)):
            granted = 1 if scope in scopes else 0
            existing = self.db.fetchone(
                "SELECT id FROM api_permissions WHERE provider = 'linkedin' AND scope = ?", (scope,))
            if existing:
                self.db.execute("UPDATE api_permissions SET granted = ?, checked_at = ? WHERE id = ?",
                                (granted, now, existing["id"]))
            else:
                self.db.execute(
                    "INSERT INTO api_permissions (id, provider, scope, granted, checked_at) VALUES (?, 'linkedin', ?, ?, ?)",
                    (new_id(), scope, granted, now))

    def fetch_member_urn(self, access_token: str) -> str | None:
        resp = self._request("GET", USERINFO_URL, access_token, versioned=False)
        sub = resp.get("sub")
        return f"urn:li:person:{sub}" if sub else None

    # ---------- token storage ----------
    def save_tokens(self, b: TokenBundle) -> None:
        cipher = TokenCipher(self.settings.token_encryption_key)
        args = (
            b.member_urn, cipher.encrypt(b.access_token),
            cipher.encrypt(b.refresh_token) if b.refresh_token else None,
            b.expires_at.isoformat() if b.expires_at else None,
            b.refresh_expires_at.isoformat() if b.refresh_expires_at else None,
            " ".join(b.scopes), utcnow(),
        )
        if self.db.fetchone("SELECT provider FROM oauth_tokens WHERE provider = 'linkedin'"):
            self.db.execute(
                "UPDATE oauth_tokens SET member_urn = ?, access_token_enc = ?, refresh_token_enc = ?, expires_at = ?,"
                " refresh_expires_at = ?, scopes = ?, updated_at = ? WHERE provider = 'linkedin'", args)
        else:
            self.db.execute(
                "INSERT INTO oauth_tokens (member_urn, access_token_enc, refresh_token_enc, expires_at,"
                " refresh_expires_at, scopes, updated_at, provider) VALUES (?, ?, ?, ?, ?, ?, ?, 'linkedin')", args)

    def _token_row(self) -> dict[str, Any]:
        row = self.db.fetchone("SELECT * FROM oauth_tokens WHERE provider = 'linkedin'")
        if row is None:
            raise AgentError("LinkedIn is not connected", ErrorCategory.AUTHENTICATION)
        return row

    def access_token(self) -> str:
        row = self._token_row()
        if row["expires_at"]:
            expires = datetime.fromisoformat(row["expires_at"])
            if expires - datetime.now(UTC) < timedelta(minutes=5):
                return self.refresh().access_token
        return TokenCipher(self.settings.token_encryption_key).decrypt(row["access_token_enc"])

    def status(self) -> dict[str, Any]:
        if not self.settings.linkedin_configured:
            return {"status": "DEGRADED", "detail": "OAuth app not configured"}
        row = self.db.fetchone("SELECT member_urn, expires_at, scopes FROM oauth_tokens WHERE provider = 'linkedin'")
        if row is None:
            return {"status": "DEGRADED", "detail": "not connected"}
        expired = bool(row["expires_at"] and datetime.fromisoformat(row["expires_at"]) < datetime.now(UTC))
        return {"status": "BLOCKED" if expired else "HEALTHY", "member": bool(row["member_urn"]),
                "scopes": (row["scopes"] or "").split(), "expires_at": row["expires_at"]}

    # ---------- API calls ----------
    def _request(self, method: str, url: str, access_token: str, json_body: dict[str, Any] | None = None,
                 versioned: bool = True) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
        if versioned:
            headers["Linkedin-Version"] = self.settings.linkedin_api_version
            headers["X-Restli-Protocol-Version"] = "2.0.0"
        started = time.monotonic()
        try:
            resp = self.http.request(method, url, headers=headers, json=json_body, timeout=30)
        except requests.RequestException as exc:
            raise AgentError(f"LinkedIn unreachable: {type(exc).__name__}", ErrorCategory.TEMPORARY) from exc
        latency_ms = int((time.monotonic() - started) * 1000)
        self.db.execute(
            "INSERT INTO metrics (id, name, value, labels, created_at) VALUES (?, 'linkedin_api_latency_ms', ?, ?, ?)",
            (new_id(), latency_ms, f'{{"status": {resp.status_code}}}', utcnow()),
        )
        if resp.status_code >= 400:
            retry_after = resp.headers.get("Retry-After")
            raise AgentError(
                f"LinkedIn API {method} {url.split('?')[0]} failed ({resp.status_code})",
                classify_http_status(resp.status_code),
                retry_after_s=int(retry_after) if retry_after and retry_after.isdigit() else None,
            )
        if resp.status_code == 201 or not resp.content:
            return {"_headers": dict(resp.headers)}
        try:
            body: dict[str, Any] = resp.json()
            return body
        except ValueError:
            return {}

    def create_text_post(self, text: str, visibility: str = "PUBLIC") -> str:
        """Publish own text post (w_member_social). Returns the post URN from x-restli-id."""
        if not text.strip() or len(text) > 3000:
            raise AgentError("post text must be 1–3000 characters", ErrorCategory.VALIDATION)
        row = self._token_row()
        if not row["member_urn"]:
            raise AgentError("member URN unknown; connect with openid profile scopes", ErrorCategory.PERMISSION)
        body = {
            "author": row["member_urn"],
            "commentary": text,
            "visibility": visibility,
            "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [], "thirdPartyDistributionChannels": []},
            "lifecycleState": "PUBLISHED",
            "isReshareDisabledByAuthor": False,
        }
        result = self._request("POST", POSTS_URL, self.access_token(), body)
        headers = {k.lower(): v for k, v in result.get("_headers", {}).items()}
        urn = headers.get("x-restli-id") or headers.get("x-linkedin-id")
        if not urn:
            raise AgentError("LinkedIn did not return a post id", ErrorCategory.PROVIDER)
        return str(urn)
