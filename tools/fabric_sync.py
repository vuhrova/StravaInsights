"""Export, deploy and run the Strava Insights Microsoft Fabric items.

Authentication uses the signed-in Azure CLI account (``az login``); no secrets are stored here.

Usage:
    python tools/fabric_sync.py export [--items "Strava Trends" Strava_Ingest ...]
    python tools/fabric_sync.py deploy "workspace/Strava Trends.Report" [more folders ...]
    python tools/fabric_sync.py run Refresh_Orchestration --type DataPipeline
    python tools/fabric_sync.py dax StravaInsights_SM "EVALUATE ROW(\\"x\\", [Total Workouts])"

Items are written to ``workspace/<DisplayName>.<ItemType>/`` using the same part paths that the Fabric
``getDefinition`` / ``updateDefinition`` APIs use. The definitions in the repository are a neutral template: they
reference the placeholder workspace, lakehouses, SQL endpoint, items and Key Vault listed in
``workspace/environment.json``. ``deploy`` rewrites those placeholders to the target workspace, and ``export`` rewrites
the target workspace's real IDs back to the placeholders, so no personal IDs ever end up in git.

Target workspace: ``FABRIC_WORKSPACE_ID`` environment variable, else the installation recorded by
``tools/install.py`` in ``stravainsights.local.json`` (git-ignored). Key Vault: ``STRAVA_KEY_VAULT_URL``, else the
same local file.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.fabric.microsoft.com/v1"
REPO = Path(__file__).resolve().parents[1]
ROOT = REPO / "workspace"
ENV_FILE = ROOT / "environment.json"
LOCAL_CONFIG = REPO / "stravainsights.local.json"

# Item types that support getDefinition. Lakehouses/SQL endpoints hold data, not definitions.
EXPORTABLE = {"Report", "SemanticModel", "Notebook", "DataPipeline", "DataAgent"}
# Parts never written to disk (runtime/user-local state).
SKIP_PARTS = {"definition/localSettings.json"}
TEXT_SUFFIXES = {".json", ".ipynb", ".tmdl", ".pbir", ".pbism", ".platform", ".schedules", ".py", ".md", ".txt", ""}


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


SOURCE_ENV = load_json(ENV_FILE)
LOCAL = load_json(LOCAL_CONFIG)
WORKSPACE_ID = os.environ.get("FABRIC_WORKSPACE_ID") or LOCAL.get("workspaceId") or ""
KEY_VAULT_URL = os.environ.get("STRAVA_KEY_VAULT_URL") or LOCAL.get("keyVaultUrl")

_token: str | None = None
_token_expires = 0.0


def access_token(resource: str) -> tuple[str, float]:
    """Token from the Azure CLI plus its expiry (az may return a cached token with only minutes left)."""
    data = json.loads(az("account", "get-access-token", "--resource", resource, "-o", "json"))
    expires = float(data.get("expires_on") or 0) or time.time() + 30 * 60
    return data["accessToken"], expires


def token(force: bool = False) -> str:
    global _token, _token_expires
    if force or _token is None or time.time() > _token_expires - 5 * 60:
        _token, _token_expires = access_token("https://api.fabric.microsoft.com")
    return _token


def az(*args: str) -> str:
    """Run an Azure CLI command and return stdout. On Windows az is a .cmd script run through cmd.exe, so arguments with
    cmd metacharacters are quoted."""
    cmd = ["az", *args]
    if os.name == "nt":
        line = " ".join(f'"{a}"' if any(c in a for c in ' ()&|<>^,;=') else a for a in cmd)
        result = subprocess.run(line, shell=True, capture_output=True, text=True)
    else:
        result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"az {' '.join(args[:3])} failed: {result.stderr.strip()[:1000]}")
    return result.stdout.strip()


class FabricError(SystemExit):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def call(method: str, url: str, body: dict | None = None) -> tuple[int, dict, dict]:
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(5):
        req = urllib.request.Request(url if url.startswith("http") else API + url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {token()}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req) as r:
                raw = r.read()
                return r.status, dict(r.headers), (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            text = e.read().decode(errors="replace")
            if e.code == 401 and "TokenExpired" in text and attempt < 4:
                token(force=True)
                continue
            if e.code == 429 or (e.code >= 500 and attempt < 4):
                time.sleep(int(e.headers.get("Retry-After", 10 * (attempt + 1))))
                continue
            raise FabricError(e.code, f"{method} {url} -> {e.code}: {text}")
    raise FabricError(429, f"{method} {url} -> throttled")


def lro(method: str, url: str, body: dict | None = None) -> dict:
    status, headers, payload = call(method, url, body)
    if status != 202:
        return payload
    location = headers.get("Location")
    while True:
        time.sleep(int(headers.get("Retry-After", 3)))
        status, headers, payload = call("GET", location)
        state = payload.get("status")
        if state == "Succeeded":
            try:
                return call("GET", location.rstrip("/") + "/result")[2]
            except SystemExit:
                return payload
        if state in ("Failed", "Cancelled"):
            raise SystemExit(f"Operation {state}: {json.dumps(payload, indent=2)}")


def list_items(workspace_id: str | None = None) -> list[dict]:
    ws = workspace_id or WORKSPACE_ID
    items, url = [], f"/workspaces/{ws}/items"
    while url:
        page = call("GET", url)[2]
        items += page.get("value", [])
        url = page.get("continuationUri")
    return items


# ------------------------------------------------------------------------------------------------ environment mapping


def describe(workspace_id: str, key_vault_url: str | None = None) -> dict:
    """Live description of a workspace in the environment.json format."""
    ws = call("GET", f"/workspaces/{workspace_id}")[2]
    env = {"workspace": {"id": ws["id"], "name": ws["displayName"]}, "lakehouses": {}, "items": {}}
    for lh in call("GET", f"/workspaces/{workspace_id}/lakehouses")[2].get("value", []):
        sql = (lh.get("properties") or {}).get("sqlEndpointProperties") or {}
        env["lakehouses"][lh["displayName"]] = {"id": lh["id"], "sqlEndpointId": sql.get("id")}
        if sql.get("connectionString"):
            env["sqlConnectionString"] = sql["connectionString"]
    for item in list_items(workspace_id):
        if item["type"] in EXPORTABLE:
            env["items"][f"{item['type']}/{item['displayName']}"] = item["id"]
    if key_vault_url:
        env["keyVaultUrl"] = key_vault_url
    return env


def replacements(target: dict, source: dict | None = None) -> list[tuple[str, str]]:
    """String replacements that turn references to the source environment into references to the target."""
    source = source or SOURCE_ENV
    pairs: list[tuple[str, str]] = []

    def add(old, new):
        if old and new and old != new:
            pairs.append((old, new))

    add(source["workspace"]["id"], target["workspace"]["id"])
    add(f"myorg/{source['workspace']['name']};", f"myorg/{target['workspace']['name']};")
    add(source.get("sqlConnectionString"), target.get("sqlConnectionString"))
    add(source.get("keyVaultUrl"), target.get("keyVaultUrl"))
    for name, lh in source.get("lakehouses", {}).items():
        new = target.get("lakehouses", {}).get(name)
        if new:
            add(lh.get("id"), new.get("id"))
            add(lh.get("sqlEndpointId"), new.get("sqlEndpointId"))
    for key, old_id in source.get("items", {}).items():
        add(old_id, target.get("items", {}).get(key))
    return sorted(pairs, key=lambda p: -len(p[0]))


def remap(data: bytes, path: str, pairs: list[tuple[str, str]]) -> bytes:
    if not pairs or Path(path).suffix.lower() not in TEXT_SUFFIXES:
        return data
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data
    for old, new in pairs:
        text = text.replace(old, new)
    return text.encode("utf-8")


def target_env(workspace_id: str | None = None) -> dict:
    ws = workspace_id or WORKSPACE_ID
    kv = KEY_VAULT_URL if ws == WORKSPACE_ID else None
    return describe(ws, kv)


PLACEHOLDER_PREFIX = "00000000-0000-4000-8000-"
KEY_VAULT_PATTERN = re.compile(r"https://[A-Za-z0-9-]+\.vault\.azure\.net/?")


def placeholder_id(n: int) -> str:
    return f"{PLACEHOLDER_PREFIX}{n:012x}"


def add_placeholders(live: dict) -> bool:
    """Give items / lakehouses that exist only in the live workspace a placeholder id in the template."""
    used = [int(v[-12:], 16) for v in re.findall(PLACEHOLDER_PREFIX + r"[0-9a-f]{12}", json.dumps(SOURCE_ENV))]
    next_id = max(used + [0x100]) + 1
    changed = False
    for key in sorted(set(live["items"]) - set(SOURCE_ENV.setdefault("items", {}))):
        SOURCE_ENV["items"][key] = placeholder_id(next_id)
        next_id, changed = next_id + 1, True
    for name in sorted(set(live["lakehouses"]) - set(SOURCE_ENV.setdefault("lakehouses", {}))):
        SOURCE_ENV["lakehouses"][name] = {"id": placeholder_id(next_id), "sqlEndpointId": placeholder_id(next_id + 1)}
        next_id, changed = next_id + 2, True
    return changed


def to_template(data: bytes, path: str, pairs: list[tuple[str, str]]) -> bytes:
    """Real IDs -> placeholders; any Key Vault URL -> the placeholder vault (even if the local config does not know it)."""
    data = remap(data, path, pairs)
    if Path(path).suffix.lower() in TEXT_SUFFIXES and SOURCE_ENV.get("keyVaultUrl"):
        try:
            data = KEY_VAULT_PATTERN.sub(SOURCE_ENV["keyVaultUrl"], data.decode("utf-8")).encode("utf-8")
        except UnicodeDecodeError:
            pass
    return data


# ------------------------------------------------------------------------------------------------ commands


def export(names: list[str] | None) -> None:
    live = describe(WORKSPACE_ID, KEY_VAULT_URL)
    if add_placeholders(live):
        ENV_FILE.write_text(json.dumps(SOURCE_ENV, indent=2) + "\n", encoding="utf-8")
        print(f"added placeholders for new items to {ENV_FILE.relative_to(REPO)}")
    # Turn this workspace's real IDs back into the template placeholders before anything is written to disk.
    pairs = replacements(SOURCE_ENV, live)
    for item in list_items():
        if item["type"] not in EXPORTABLE or (names and item["displayName"] not in names):
            continue
        fmt = "?format=ipynb" if item["type"] == "Notebook" else ""
        result = lro("POST", f"/workspaces/{WORKSPACE_ID}/items/{item['id']}/getDefinition{fmt}")
        folder = ROOT / f"{item['displayName']}.{item['type']}"
        for part in result["definition"]["parts"]:
            if part["path"] in SKIP_PARTS:
                continue
            target = folder / part["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(to_template(base64.b64decode(part["payload"]), part["path"], pairs))
        print(f"exported {item['type']:<14} {item['displayName']} -> {folder.relative_to(REPO)}")


def deploy(folder: Path, workspace_id: str | None = None, pairs: list[tuple[str, str]] | None = None,
           transform=None) -> str:
    """Create or update one item from its folder; returns the item id."""
    ws = workspace_id or WORKSPACE_ID
    if pairs is None:
        pairs = replacements(target_env(ws))
    name, item_type = folder.name.rsplit(".", 1)
    match = [i for i in list_items(ws) if i["displayName"] == name and i["type"] == item_type]
    parts = []
    kv_placeholder = (SOURCE_ENV.get("keyVaultUrl") or "").encode()
    for p in sorted(folder.rglob("*")):
        rel = p.relative_to(folder).as_posix()
        if not p.is_file() or rel in SKIP_PARTS:
            continue
        data = remap(p.read_bytes(), rel, pairs)
        if transform:
            data = transform(rel, data)
        if kv_placeholder and kv_placeholder in data:
            raise SystemExit(f"{folder.name}/{rel} needs your Key Vault, which is unknown: run tools/install.py once, "
                             "or set STRAVA_KEY_VAULT_URL=https://<vault-name>.vault.azure.net/")
        parts.append({"path": rel, "payload": base64.b64encode(data).decode(), "payloadType": "InlineBase64"})
    definition: dict = {"parts": parts}
    if item_type == "Notebook":
        definition["format"] = "ipynb"
    if match:
        url = f"/workspaces/{ws}/items/{match[0]['id']}/updateDefinition"
        if any(p["path"] == ".platform" for p in parts):
            url += "?updateMetadata=True"
        lro("POST", url, {"definition": definition})
        print(f"updated {item_type} {name}")
        return match[0]["id"]
    created = lro("POST", f"/workspaces/{ws}/items", {"displayName": name, "type": item_type, "definition": definition})
    item_id = created.get("id") or next(i["id"] for i in list_items(ws) if i["displayName"] == name and i["type"] == item_type)
    print(f"created {item_type} {name}")
    return item_id


def run(name: str, item_type: str, workspace_id: str | None = None, parameters: dict | None = None,
        timeout_minutes: int = 240, attach_running: bool = False) -> dict:
    """Run a notebook or pipeline on demand and wait for it to finish (or wait for a run already in progress)."""
    ws = workspace_id or WORKSPACE_ID
    item = next(i for i in list_items(ws) if i["displayName"] == name and i["type"] == item_type)
    job_type = {"Notebook": "RunNotebook", "DataPipeline": "Pipeline"}[item_type]
    jobs_url = f"/workspaces/{ws}/items/{item['id']}/jobs/instances"
    running = [j for j in call("GET", jobs_url)[2].get("value", []) if j.get("status") in ("NotStarted", "InProgress")]
    if attach_running and running:
        location = f"{API}{jobs_url}/{running[0]['id']}"
        print(f"{name}: attaching to the run started {running[0].get('startTimeUtc')}")
    else:
        body = None
        if parameters:
            body = {"executionData": {"parameters": {k: {"value": v, "type": "string" if isinstance(v, str) else
                                                          "bool" if isinstance(v, bool) else "int"} for k, v in parameters.items()}}}
        _, headers, _ = call("POST", f"{jobs_url}?jobType={job_type}", body)
        location = headers["Location"]
    started, last = time.time(), None
    while True:
        time.sleep(20)
        state = call("GET", location)[2]
        if state.get("status") != last:
            last = state.get("status")
            print(f"{name}: {last} ({int((time.time() - started) / 60)} min)")
        if state.get("status") in ("Completed", "Failed", "Cancelled", "Deduped"):
            if state.get("failureReason"):
                print(json.dumps(state["failureReason"], indent=2))
            if state["status"] != "Completed":
                raise SystemExit(f"{name} {state['status']}")
            return state
        if time.time() - started > timeout_minutes * 60:
            raise SystemExit(f"{name} still running after {timeout_minutes} min - check the Monitor hub")


def dax(model_name: str, query: str, workspace_id: str | None = None) -> list[dict]:
    """Run a DAX query against a semantic model in the workspace (Power BI executeQueries API)."""
    ws = workspace_id or WORKSPACE_ID
    item = next(i for i in list_items(ws) if i["displayName"] == model_name and i["type"] == "SemanticModel")
    pbi_token = access_token("https://analysis.windows.net/powerbi/api")[0]
    req = urllib.request.Request(
        f"https://api.powerbi.com/v1.0/myorg/groups/{ws}/datasets/{item['id']}/executeQueries",
        data=json.dumps({"queries": [{"query": query}], "serializerSettings": {"includeNulls": True}}).encode(),
        method="POST")
    req.add_header("Authorization", f"Bearer {pbi_token}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as r:
            rows = json.loads(r.read())["results"][0]["tables"][0]["rows"]
    except urllib.error.HTTPError as e:
        raise SystemExit(f"DAX failed {e.code}: {e.read().decode(errors='replace')[:2000]}")
    for row in rows:
        print(json.dumps(row, default=str))
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export")
    e.add_argument("--items", nargs="*")
    d = sub.add_parser("deploy")
    d.add_argument("folders", nargs="+", type=Path)
    r = sub.add_parser("run")
    r.add_argument("name")
    r.add_argument("--type", default="Notebook", choices=["Notebook", "DataPipeline"])
    q = sub.add_parser("dax")
    q.add_argument("model")
    q.add_argument("query")
    args = parser.parse_args()
    if not WORKSPACE_ID:
        sys.exit("No target workspace: run tools/install.py once (it records your workspace in the git-ignored "
                 "stravainsights.local.json), or set FABRIC_WORKSPACE_ID.")
    if args.cmd == "export":
        export(args.items)
    elif args.cmd == "run":
        run(args.name, args.type)
    elif args.cmd == "dax":
        query = Path(args.query[1:]).read_text(encoding="utf-8") if args.query.startswith("@") else args.query
        dax(args.model, query)
    else:
        for f in args.folders:
            # Recompute the mapping per folder: items created by an earlier folder (e.g. a new notebook) are then
            # resolved in the definitions that reference them (e.g. the pipeline).
            deploy(f.resolve())


if __name__ == "__main__":
    sys.exit(main())
