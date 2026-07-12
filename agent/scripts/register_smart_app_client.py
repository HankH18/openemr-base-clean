#!/usr/bin/env python3
"""Register the CONFIDENTIAL SMART authorization_code (login) client in OpenEMR.

Sibling to ``register_backend_client.py``, but for the per-physician login client
(``PRODUCTION_GRADE_PLAN.md`` §1.8) rather than the background poller:

- ``authorization_code`` + ``refresh_token`` grants (interactive login + offline).
- ``token_endpoint_auth_method: client_secret_basic`` — the BFF holds the secret.
- PKCE (S256) — the agent sends a ``code_verifier`` on the exchange.
- ``redirect_uris`` = exactly ``${public_base_url}/v1/auth/callback`` (OpenEMR
  validates redirect URIs strictly; a scheme/host mismatch silently fails).
- ``scope`` = the ``COPILOT_SMART_SCOPES`` set (reads + api:oemr writes in one token).

Prints the resulting ``client_id`` AND ``client_secret`` plus the SQL to ENABLE
the client (new clients register disabled; a login client needs
``is_enabled=1, client_role='user'``).

Usage (deployed droplet — public https origin, once TLS is in front):
    python scripts/register_smart_app_client.py \
        --base-url https://agentforge.example.com \
        --public-base-url https://agentforge.example.com

Usage (local dev stack over the self-signed https port):
    python scripts/register_smart_app_client.py \
        --base-url https://localhost:9300 \
        --public-base-url https://localhost:9300 --insecure

Then ENABLE the client (dev stack shown; adapt the mariadb invocation for deploy):
    docker compose exec -T -e MYSQL_PWD=<root_pw> mysql mariadb -uroot openemr \
      -e "UPDATE oauth_clients SET is_enabled=1, client_role='user' WHERE client_id='<CLIENT_ID>';"

Record the credentials as secrets (never commit them):
    COPILOT_SMART_APP_CLIENT_ID=<client_id>
    COPILOT_SMART_APP_CLIENT_SECRET=<client_secret>   # secrets-manager only
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import httpx

from copilot.config import Settings

# Single source of truth: the same default scope set the running agent requests.
_DEFAULT_SCOPES = str(Settings.model_fields["smart_scopes"].default)
_CALLBACK_PATH = "/v1/auth/callback"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--base-url", required=True, help="OpenEMR base, e.g. https://agentforge.example.com"
    )
    ap.add_argument(
        "--public-base-url",
        required=True,
        help="Public origin the browser reaches the agent at (drives the redirect_uri).",
    )
    ap.add_argument("--scopes", default=_DEFAULT_SCOPES, help="space-separated SMART scopes")
    ap.add_argument("--site", default="default", help="OpenEMR site (default: default)")
    ap.add_argument(
        "--client-name", default="AgentForge Clinical Co-Pilot", help="registered client name"
    )
    ap.add_argument(
        "--out", default="secrets/smart-app-client.json", help="where to write the JSON response"
    )
    ap.add_argument(
        "--insecure", action="store_true", help="skip TLS verify (self-signed dev cert)"
    )
    args = ap.parse_args()

    redirect_uri = f"{args.public_base_url.rstrip('/')}{_CALLBACK_PATH}"
    body = {
        "application_type": "private",  # confidential client (holds a secret)
        "client_name": args.client_name,
        "token_endpoint_auth_method": "client_secret_basic",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "redirect_uris": [redirect_uri],
        "scope": args.scopes,
        "contacts": ["ops@agentforge.local"],
    }
    url = f"{args.base_url}/oauth2/{args.site}/registration"
    resp = httpx.post(
        url,
        json=body,
        timeout=30.0,
        verify=not args.insecure,
        headers={"Accept": "application/json"},
    )
    if resp.status_code not in (200, 201):
        raise SystemExit(f"registration failed: {resp.status_code} {resp.text[:500]}")

    data = resp.json()
    client_id = data["client_id"]
    # Confidential clients get a secret back; print it once for the operator to store.
    client_secret = data.get("client_secret", "<none returned — check registration>")

    print(f"redirect_uri  : {redirect_uri}")
    print(f"CLIENT_ID     : {client_id}")
    print(f"CLIENT_SECRET : {client_secret}")
    print(f"scopes        : {args.scopes}")
    print("\nNext: ENABLE the client (registered but disabled + role=user by default):")
    print(
        "  UPDATE oauth_clients SET is_enabled=1, client_role='user' "
        f"WHERE client_id='{client_id}';"
    )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(data, indent=2))
    print(f"\nfull response written to {out_path} (contains the secret — keep OUT of git)")


if __name__ == "__main__":
    main()
