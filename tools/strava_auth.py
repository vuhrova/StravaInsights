"""Connect Strava: stores your Strava API app credentials and a refresh token in Azure Key Vault.

The pipeline reads them at run time; nothing is written to the repository, a lakehouse or the console.

    python tools/strava_auth.py --key-vault <vault-name>          # first time: asks for Client ID and Client Secret
    python tools/strava_auth.py --key-vault <vault-name>          # later: re-authorize (e.g. after revoking access)

Before the first run, create a Strava API application at https://www.strava.com/settings/api and set its
"Authorization Callback Domain" to ``localhost``. This script opens Strava's consent page in your browser, receives the
redirect on http://localhost:<port>/exchange_token and exchanges the one-time code for a refresh token.
"""

from __future__ import annotations

import argparse
import getpass
import http.server
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

SCOPES = "read,read_all,profile:read_all,activity:read_all"
SECRET_NAMES = ("strava-client-id", "strava-client-secret", "strava-refresh-token")


def az(*args: str) -> str:
    cmd = ["az", *args]
    if os.name == "nt":  # az is a .cmd script run through cmd.exe: quote arguments with metacharacters
        line = " ".join(f'"{a}"' if any(c in a for c in ' ()&|<>^,;=') else a for a in cmd)
        result = subprocess.run(line, shell=True, capture_output=True, text=True)
    else:
        result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"az {' '.join(args[:3])} failed: {result.stderr.strip()[:800]}")
    return result.stdout.strip()


class KeyVault:
    """Minimal Key Vault secrets client using the signed-in Azure CLI account (values never appear on a command line)."""

    def __init__(self, name_or_url: str):
        self.url = name_or_url.rstrip("/") if name_or_url.startswith("https://") else f"https://{name_or_url}.vault.azure.net"
        self._token = None
        self._expires = 0.0

    def _bearer(self, force: bool = False) -> str:
        if force or self._token is None or time.time() > self._expires - 300:
            data = json.loads(az("account", "get-access-token", "--resource", "https://vault.azure.net", "-o", "json"))
            self._token, self._expires = data["accessToken"], float(data.get("expires_on") or 0) or time.time() + 1800
        return self._token

    def _request(self, method: str, name: str, body: dict | None = None, retry: bool = True):
        req = urllib.request.Request(f"{self.url}/secrets/{name}?api-version=7.4", method=method,
                                     data=json.dumps(body).encode() if body is not None else None)
        req.add_header("Authorization", f"Bearer {self._bearer()}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            if e.code == 404 and method == "GET":
                return None
            detail = e.read().decode(errors="replace")[:400]
            if e.code == 401 and retry:
                self._bearer(force=True)
                return self._request(method, name, body, retry=False)
            if e.code == 403:
                raise SystemExit(f"Access to Key Vault {self.url} denied. Your account needs the 'Key Vault Secrets Officer' "
                                 f"role on the vault (tools/install.py assigns it).\n{detail}")
            raise SystemExit(f"Key Vault {method} {name} failed: HTTP {e.code} {detail}")

    def get(self, name: str) -> str | None:
        secret = self._request("GET", name)
        return secret.get("value") if secret else None

    def set(self, name: str, value: str) -> None:
        self._request("PUT", name, {"value": value, "contentType": "text/plain"})


def authorize(client_id: str, client_secret: str, port: int = 8765) -> dict:
    """Run the Strava OAuth authorization-code flow with a one-shot local redirect listener."""
    state = secrets.token_urlsafe(16)
    redirect = f"http://localhost:{port}/exchange_token"
    result: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server API
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if not self.path.startswith("/exchange_token"):
                self.send_response(404)
                self.end_headers()
                return
            if query.get("state", [""])[0] != state:
                result["error"] = "state mismatch"
            elif "error" in query:
                result["error"] = query["error"][0]
            else:
                result["code"] = query.get("code", [""])[0]
                result["scope"] = query.get("scope", [""])[0]
            ok = "code" in result
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(("<html><body style='font-family:Segoe UI,sans-serif;padding:40px'><h2>"
                              + ("Strava connected - you can close this tab." if ok else "Strava authorization failed.")
                              + "</h2></body></html>").encode())
            threading.Thread(target=self.server.shutdown, daemon=True).start()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("localhost", port), Handler)
    url = "https://www.strava.com/oauth/authorize?" + urllib.parse.urlencode({
        "client_id": client_id, "response_type": "code", "redirect_uri": redirect, "approval_prompt": "force",
        "scope": SCOPES, "state": state})
    print("Opening Strava in your browser. If it does not open, visit:\n  " + url)
    webbrowser.open(url)
    server.serve_forever()
    server.server_close()
    if "code" not in result:
        raise SystemExit(f"Strava authorization failed: {result.get('error', 'no code received')}")
    missing = [s for s in ("activity:read_all", "profile:read_all") if s not in result.get("scope", "")]
    if missing:
        print(f"Warning: you did not grant {', '.join(missing)}. Private activities, body weight and HR/power zones "
              "will be missing - run this script again and keep all boxes ticked.")
    body = urllib.parse.urlencode({"client_id": client_id, "client_secret": client_secret, "code": result["code"],
                                   "grant_type": "authorization_code"}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request("https://www.strava.com/oauth/token", data=body), timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise InvalidCredentials(f"Strava token exchange failed: HTTP {e.code}. Check the Client ID / Client Secret.") from None


class InvalidCredentials(SystemExit):
    pass


def ask_credentials() -> tuple[str, str]:
    print("Strava API application (https://www.strava.com/settings/api, Authorization Callback Domain: localhost)")
    client_id = input("  Client ID: ").strip()
    client_secret = getpass.getpass("  Client Secret: ").strip()
    if not client_id.isdigit() or not client_secret:
        raise SystemExit("The Client ID is a number and the Client Secret is required.")
    return client_id, client_secret


def connect(vault: KeyVault, reauthorize: bool = True, port: int = 8765, new_credentials: bool = False) -> dict:
    client_id = None if new_credentials else vault.get("strava-client-id")
    client_secret = None if new_credentials else vault.get("strava-client-secret")
    if not client_id or not client_secret:
        client_id, client_secret = ask_credentials()
    elif not reauthorize and vault.get("strava-refresh-token"):
        print("Strava is already connected (refresh token found in Key Vault).")
        return {}
    try:
        tokens = authorize(client_id, client_secret, port)
    except InvalidCredentials as exc:
        print(f"{exc} Enter the app's current values (e.g. after resetting the secret on Strava):")
        client_id, client_secret = ask_credentials()
        tokens = authorize(client_id, client_secret, port)
    # Stored only after a successful exchange, so a typo never overwrites working credentials.
    vault.set("strava-client-id", client_id)
    vault.set("strava-client-secret", client_secret)
    vault.set("strava-refresh-token", tokens["refresh_token"])
    athlete = tokens.get("athlete") or {}
    print(f"Strava connected for {athlete.get('firstname', '')} {athlete.get('lastname', '')}".rstrip()
          + f" - credentials stored in {vault.url}")
    return athlete


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--key-vault", help="Key Vault name or URL (default: the one recorded by tools/install.py)")
    parser.add_argument("--port", type=int, default=8765, help="local redirect port (default 8765)")
    parser.add_argument("--new-credentials", action="store_true",
                        help="enter a new Client ID / Client Secret (e.g. after resetting the secret on Strava)")
    args = parser.parse_args()
    vault_name = args.key_vault
    if not vault_name:
        config = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "stravainsights.local.json")
        if os.path.exists(config):
            vault_name = json.load(open(config, encoding="utf-8")).get("keyVaultUrl")
    if not vault_name:
        sys.exit("Pass --key-vault <name> (or run tools/install.py first).")
    connect(KeyVault(vault_name), reauthorize=True, port=args.port, new_credentials=args.new_credentials)


if __name__ == "__main__":
    main()
