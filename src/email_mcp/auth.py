"""One-time ProtonMail authentication command.

Performs SRP authentication against the ProtonMail API and saves the
resulting session (access_token, refresh_token, uid, key_salts) to a JSON file.

Usage:
    email-mcp-auth

Reads credentials from env vars (EMAIL_MCP_IMAP_USERNAME and
EMAIL_MCP_PROTON_PASSWORD). Intended to be run via `op run`:

    op run --env-file=.env.auth -- email-mcp-auth
"""

from __future__ import annotations

import base64
import getpass
import json
import sys

import httpx

from email_mcp.config import Settings
from email_mcp.srp import SRPUser, extract_modulus

_API_BASE = "https://mail.proton.me/api"
_APP_VERSION = "linux-bridge@3.22.0"


def _headers() -> dict[str, str]:
    return {"x-pm-appversion": _APP_VERSION}


def main() -> None:
    settings = Settings()

    # Prompt for everything interactively — env vars are optional
    username = settings.imap_username
    if not username:
        username = input("ProtonMail username: ").strip()
    password = settings.proton_password
    if not password:
        password = getpass.getpass("ProtonMail password: ")

    print(f"Authenticating as {username} ...", file=sys.stderr)

    with httpx.Client(base_url=_API_BASE, headers=_headers(), timeout=30.0) as client:
        # Step 1: get SRP challenge
        info = client.post("/auth/info", json={"Username": username}).json()
        if info.get("Code") != 1000:
            print(f"ERROR: /auth/info failed: {info.get('Error', info)}", file=sys.stderr)
            sys.exit(1)

        modulus = extract_modulus(info["Modulus"])
        salt = base64.b64decode(info["Salt"])
        server_ephemeral = base64.b64decode(info["ServerEphemeral"])
        version = info["Version"]
        srp_session = info["SRPSession"]

        # Step 2: SRP exchange
        user = SRPUser(password, modulus)
        client_ephemeral = user.get_challenge()
        client_proof = user.process_challenge(salt, server_ephemeral, version)
        if client_proof is None:
            print("ERROR: SRP safety check failed", file=sys.stderr)
            sys.exit(1)

        auth = client.post("/auth", json={
            "Username": username,
            "ClientEphemeral": base64.b64encode(client_ephemeral).decode(),
            "ClientProof": base64.b64encode(client_proof).decode(),
            "SRPSession": srp_session,
        }).json()

        if auth.get("Code") != 1000:
            print(f"ERROR: Authentication failed: {auth.get('Error', auth)}", file=sys.stderr)
            sys.exit(1)

        uid = auth["UID"]
        access_token = auth["AccessToken"]
        refresh_token = auth["RefreshToken"]
        scope = auth.get("Scope", "")

        # Verify server proof
        server_proof = base64.b64decode(auth.get("ServerProof", ""))
        if server_proof:
            user.verify_session(server_proof)
            if not user.authenticated():
                print("WARNING: Server proof verification failed", file=sys.stderr)

        print(f"Scope after SRP: {scope}", file=sys.stderr)

        # Step 3: 2FA if required
        # "twofactor" in scope means 2FA is required but not yet provided
        if "twofactor" in scope.lower():
            code = input("Enter your 2FA code: ").strip()
            tfa = client.post(
                "/auth/2fa",
                json={"TwoFactorCode": code},
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "x-pm-uid": uid,
                    **_headers(),
                },
            ).json()
            if tfa.get("Code") != 1000:
                print(f"ERROR: 2FA failed: {tfa.get('Error', tfa)}", file=sys.stderr)
                sys.exit(1)
            scope = tfa.get("Scope", scope)
            print(f"Scope after 2FA: {scope}", file=sys.stderr)

        # Step 4: Fetch key salts and derive mailbox passphrase
        key_salts = {}
        mailbox_passphrase = ""
        try:
            salts_resp = client.get(
                "/core/v4/keys/salts",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "x-pm-uid": uid,
                    **_headers(),
                },
            ).json()
            if salts_resp.get("Code") == 1000:
                for ks in salts_resp.get("KeySalts", []):
                    key_salts[ks["ID"]] = ks.get("KeySalt")
                print(f"Fetched {len(key_salts)} key salts", file=sys.stderr)
            else:
                print(
                    f"WARNING: Could not fetch key salts: "
                    f"{salts_resp.get('Error', salts_resp)}",
                    file=sys.stderr,
                )
        except Exception as e:
            print(f"WARNING: Key salts fetch failed: {e}", file=sys.stderr)

        # Step 5: Fetch user key and derive passphrase
        if key_salts:
            try:
                user_resp = client.get(
                    "/core/v4/users",
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "x-pm-uid": uid,
                        **_headers(),
                    },
                ).json()
                user_key = user_resp["User"]["Keys"][0]
                key_salt = key_salts.get(user_key["ID"])
                if key_salt:
                    from email_mcp.crypto import derive_mailbox_passphrase

                    mailbox_passphrase = derive_mailbox_passphrase(
                        password, key_salt
                    )
                    print("Mailbox passphrase derived and cached.", file=sys.stderr)
                else:
                    print(
                        "WARNING: No key salt for primary key — "
                        "using raw password as passphrase.",
                        file=sys.stderr,
                    )
                    mailbox_passphrase = password
            except Exception as e:
                print(f"WARNING: Passphrase derivation failed: {e}", file=sys.stderr)

    session = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "uid": uid,
        "key_salts": key_salts,
        "mailbox_passphrase": mailbox_passphrase,
    }

    out_path = settings.proton_session_file
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(session, indent=2))

    print(f"Session saved to {out_path}", file=sys.stderr)
    if mailbox_passphrase:
        print(
            "Mailbox passphrase cached in session file. "
            "PASSWORD IS NO LONGER NEEDED AT RUNTIME.",
            file=sys.stderr,
        )
