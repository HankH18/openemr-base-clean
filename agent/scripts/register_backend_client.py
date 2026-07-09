#!/usr/bin/env python3
"""Register a SMART Backend Services client in OpenEMR (client_credentials).

Generates an RSA keypair, registers a confidential client via OpenEMR's dynamic
client-registration endpoint with the public JWK + system/*.read scopes, and
prints the client_id plus the SQL to ENABLE it (new clients are disabled and
default to the ``user`` role; Backend Services needs ``system`` + enabled).

Usage (local dev stack over the self-signed https port):
    python scripts/register_backend_client.py \
        --base-url https://localhost:9300 --out-key secrets/backend-key.pem --insecure

Usage (deployed droplet — use the public https URL once TLS is in front):
    python scripts/register_backend_client.py \
        --base-url https://copilot.example.com --out-key secrets/backend-key.pem

Then enable the client (dev stack shown; adapt the mariadb invocation for deploy):
    docker compose exec -T -e MYSQL_PWD=<root_pw> mysql mariadb -uroot openemr \
      -e "UPDATE oauth_clients SET is_enabled=1, client_role='system' WHERE client_id='<CLIENT_ID>';"

The private key is written to --out-key (keep it OUT of git; agent/secrets/ is
gitignored). Put the client_id in the agent config (COPILOT_BACKEND_SERVICES_CLIENT_ID)
and point COPILOT_BACKEND_SERVICES_PRIVATE_KEY_PATH at the key.
"""

from __future__ import annotations

import argparse
import json
import secrets
from pathlib import Path

import httpx
from authlib.jose import JsonWebKey  # type: ignore[import-untyped]
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

_DEFAULT_SCOPES = " ".join(
    f"system/{r}.read"
    for r in (
        "Patient",
        "Observation",
        "MedicationRequest",
        "Condition",
        "AllergyIntolerance",
        "Encounter",
        "DiagnosticReport",
    )
)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base-url", required=True, help="OpenEMR base, e.g. https://localhost:9300")
    ap.add_argument(
        "--out-key", default="secrets/backend-key.pem", help="where to write the private PEM"
    )
    ap.add_argument(
        "--scopes", default=_DEFAULT_SCOPES, help="space-separated system/*.read scopes"
    )
    ap.add_argument("--site", default="default", help="OpenEMR site (default: default)")
    ap.add_argument(
        "--insecure", action="store_true", help="skip TLS verify (self-signed dev cert)"
    )
    args = ap.parse_args()

    key_path = Path(args.out_key)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    jwk = JsonWebKey.import_key(key.public_key(), {"kty": "RSA"}).as_dict()
    jwk.update({"kid": secrets.token_hex(8), "use": "sig", "alg": "RS384"})

    body = {
        "application_type": "private",
        "client_name": "AgentForge Rounds Poller",
        "token_endpoint_auth_method": "private_key_jwt",
        "grant_types": ["client_credentials"],
        # Unused by client_credentials but OpenEMR's registration validates it.
        "redirect_uris": [f"{args.base_url}/oauth2/callback"],
        "jwks": {"keys": [jwk]},
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
    print(f"private key : {key_path}")
    print(f"CLIENT_ID   : {client_id}")
    print(f"scopes      : {args.scopes}")
    print("\nNext: ENABLE the client (it is registered but disabled + role=user):")
    print(
        "  UPDATE oauth_clients SET is_enabled=1, client_role='system' "
        f"WHERE client_id='{client_id}';"
    )
    Path(key_path.parent / "backend-client.json").write_text(json.dumps(data, indent=2))


if __name__ == "__main__":
    main()
