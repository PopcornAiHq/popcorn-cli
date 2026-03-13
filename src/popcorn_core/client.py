"""Popcorn API client with auth and workspace injection."""

from __future__ import annotations

import time
from typing import Any

import httpx
import jwt

from .auth import discover_oidc
from .config import Profile, load_config, save_config
from .errors import APIError, AuthError


class APIClient:
    """Synchronous HTTP client with auth + workspace injection."""

    def __init__(self, profile: Profile, timeout: float = 30.0) -> None:
        self.profile = profile
        self._client = httpx.Client(timeout=timeout)

    def _token(self) -> str:
        """Return a valid token, refreshing if needed."""
        now = int(time.time())
        if self.profile.expires_at > 0 and self.profile.expires_at < now:
            self._refresh_token()
        return self.profile.id_token

    def _refresh_token(self) -> None:
        """Attempt to refresh the token via Clerk."""
        if not self.profile.refresh_token:
            raise AuthError("Token expired. Run: popcorn auth login")
        if not self.profile.clerk_client_id:
            raise AuthError("No client ID configured. Run: popcorn auth login")

        try:
            oidc = discover_oidc(self.profile.clerk_issuer)
            resp = httpx.post(
                oidc["token_endpoint"],
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self.profile.refresh_token,
                    "client_id": self.profile.clerk_client_id,
                },
                timeout=10.0,
            )
            if resp.status_code != 200:
                raise AuthError("Token refresh failed. Run: popcorn auth login")

            data = resp.json()
            self.profile.id_token = data.get("id_token", self.profile.id_token)
            self.profile.access_token = data.get("access_token", self.profile.access_token)
            if "refresh_token" in data:
                self.profile.refresh_token = data["refresh_token"]

            claims = jwt.decode(self.profile.id_token, options={"verify_signature": False})
            self.profile.expires_at = claims.get("exp", 0)

            # Persist refreshed tokens
            cfg = load_config()
            cfg.profiles[cfg.default_profile] = self.profile
            save_config(cfg)
        except AuthError:
            raise
        except httpx.HTTPError as e:
            raise AuthError(f"Token refresh failed (network: {e}). Run: popcorn auth login") from e
        except Exception as e:
            raise AuthError(
                f"Token refresh failed ({type(e).__name__}: {e}). Run: popcorn auth login"
            ) from e

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token()}",
            "Content-Type": "application/json",
        }

    def _inject_workspace(self, params: dict[str, Any]) -> dict[str, Any]:
        if "workspace_id" not in params and self.profile.workspace_id:
            params["workspace_id"] = self.profile.workspace_id
        return params

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        params = self._inject_workspace(params or {})
        return self._request("GET", path, params=params)

    def post(
        self,
        path: str,
        data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        params = self._inject_workspace(params or {})
        return self._request("POST", path, json_data=data, params=params)

    def patch(
        self,
        path: str,
        data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        params = self._inject_workspace(params or {})
        return self._request("PATCH", path, json_data=data, params=params)

    def delete(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        params = self._inject_workspace(params or {})
        return self._request("DELETE", path, params=params, json_data=data)

    def put(
        self,
        path: str,
        data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        params = self._inject_workspace(params or {})
        return self._request("PUT", path, json_data=data, params=params)

    def request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Generic request — used by `popcorn api` escape hatch."""
        params = self._inject_workspace(params or {})
        return self._request(method.upper(), path, params=params, json_data=data)

    def _do_request(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None,
        json_data: dict[str, Any] | None,
    ) -> httpx.Response:
        """Execute a single HTTP request, translating httpx errors to APIError."""
        try:
            return self._client.request(
                method, url, headers=self._headers(), params=params, json=json_data
            )
        except httpx.ConnectError as e:
            raise APIError(f"Cannot connect to {self.profile.api_url}") from e
        except httpx.TimeoutException as e:
            raise APIError(f"Request timed out: {method} {url}") from e
        except httpx.HTTPError as e:
            raise APIError(f"Network error ({type(e).__name__}): {e}") from e

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.profile.api_url}{path}"
        resp = self._do_request(method, url, params, json_data)

        # Auto-retry on 401
        if resp.status_code == 401:
            self._refresh_token()
            resp = self._do_request(method, url, params, json_data)
            if resp.status_code == 401:
                raise AuthError("Session expired (refresh did not help). Run: popcorn auth login")

        if resp.status_code >= 400:
            try:
                body = resp.json()
            except (ValueError, Exception):
                msg = resp.text[:200]
            else:
                # Try common API error response shapes
                detail = body.get("detail", body.get("message", body.get("error", "")))
                if isinstance(detail, dict):
                    msg = str(
                        detail.get(
                            "detail", detail.get("error", detail.get("message", str(detail)))
                        )
                    )
                elif isinstance(detail, list):
                    # Pydantic 422 validation errors
                    parts = []
                    for err in detail:
                        if isinstance(err, dict):
                            loc = ".".join(str(x) for x in err.get("loc", []))
                            parts.append(f"{loc}: {err.get('msg', '')}")
                    msg = "; ".join(parts) if parts else str(detail)
                else:
                    msg = str(detail)
            raise APIError(
                msg or f"HTTP {resp.status_code}",
                status_code=resp.status_code,
                body=resp.text,
            )

        try:
            return resp.json()  # type: ignore[no-any-return]
        except (ValueError, Exception) as e:
            raise APIError(
                f"Invalid JSON in response from {path}",
                status_code=resp.status_code,
                body=resp.text[:500],
            ) from e
