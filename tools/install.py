"""Install (or upgrade) Strava Insights into your own Microsoft Fabric workspace.

    python tools/install.py --workspace "Strava Insights" --key-vault <vault-name> --resource-group <rg> --location <region>

What it does (every step is idempotent - run it again to upgrade after ``git pull``):

1. Finds or creates the Fabric workspace on your capacity.
2. Creates the lakehouses Bronze (with schemas), Strava_Silver and Strava_Gold.
3. Finds or creates the Azure Key Vault and gives your account the "Key Vault Secrets Officer" role.
4. Connects Strava: asks once for your Strava API app's Client ID / Secret and opens Strava's consent page
   (see tools/strava_auth.py). Credentials are stored only in Key Vault.
5. Deploys the notebooks and the Refresh_Orchestration pipeline with a daily schedule, rewriting every
   workspace / lakehouse / item / Key Vault reference to your environment.
6. Runs the pipeline once (first load), then deploys the semantic model, the report and the data agent.

Prerequisites: Python 3.10+, Azure CLI signed in (``az login``) with an account that can use a Fabric capacity
(F2 or larger, or a Fabric trial) and an Azure subscription for the Key Vault. See docs/setup.md.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fabric_sync as fs  # noqa: E402
import strava_auth  # noqa: E402

LAKEHOUSES = [("Bronze", True), ("Strava_Silver", False), ("Strava_Gold", False)]
PIPELINE = "Refresh_Orchestration"
MODEL = "StravaInsights_SM"
REPORT = "Strava Insights"
AGENT = "Strava insights agent"
SECRETS_OFFICER = "Key Vault Secrets Officer"


def step(n: int, text: str) -> None:
    print(f"\n[{n}/7] {text}")


def default_timezone() -> str:
    if os.name == "nt":
        try:
            return subprocess.run(["tzutil", "/g"], capture_output=True, text=True, check=True).stdout.strip() or "UTC"
        except (OSError, subprocess.CalledProcessError):
            pass
    return "UTC"


# ------------------------------------------------------------------------------------------------ Fabric workspace


def find_workspace(name: str) -> dict | None:
    url = "/workspaces"
    while url:
        page = fs.call("GET", url)[2]
        for ws in page.get("value", []):
            if ws["displayName"].lower() == name.lower() and ws.get("type") == "Workspace":
                return ws
        url = page.get("continuationUri")
    return None


def pick_capacity(wanted: str | None) -> dict:
    caps = [c for c in fs.call("GET", "/capacities")[2].get("value", []) if c.get("state") == "Active"]
    if wanted:
        match = [c for c in caps if wanted.lower() in (c["id"].lower(), c["displayName"].lower())]
        if not match:
            raise SystemExit(f"Capacity '{wanted}' not found or not active. Available: "
                             + ", ".join(f"{c['displayName']} ({c['sku']})" for c in caps))
        return match[0]
    if len(caps) == 1:
        return caps[0]
    if not caps:
        raise SystemExit("No active Fabric capacity is available to your account. Start a Fabric trial "
                         "(https://app.fabric.microsoft.com -> Account manager -> Start trial) or ask your admin for an F SKU.")
    raise SystemExit("Several capacities are available - choose one with --capacity: "
                     + ", ".join(f"'{c['displayName']}' ({c['sku']})" for c in caps))


def ensure_workspace(name: str, capacity: str | None) -> dict:
    ws = find_workspace(name)
    if ws is None:
        cap = pick_capacity(capacity)
        ws = fs.call("POST", "/workspaces", {"displayName": name, "capacityId": cap["id"],
                                              "description": "Strava Insights - training and longevity analytics"})[2]
        print(f"  created workspace '{name}' on capacity {cap['displayName']} ({cap['sku']})")
    else:
        print(f"  using workspace '{ws['displayName']}' ({ws['id']})")
        if not ws.get("capacityId"):
            cap = pick_capacity(capacity)
            fs.call("POST", f"/workspaces/{ws['id']}/assignToCapacity", {"capacityId": cap["id"]})
            print(f"  assigned capacity {cap['displayName']}")
    return ws


def ensure_lakehouses(ws_id: str) -> None:
    existing = {lh["displayName"]: lh for lh in fs.call("GET", f"/workspaces/{ws_id}/lakehouses")[2].get("value", [])}
    for name, schemas in LAKEHOUSES:
        if name in existing:
            print(f"  lakehouse {name}: exists")
            continue
        body = {"displayName": name}
        if schemas:
            body["creationPayload"] = {"enableSchemas": True}
        fs.lro("POST", f"/workspaces/{ws_id}/lakehouses", body)
        print(f"  lakehouse {name}: created")
    deadline = time.time() + 15 * 60
    while True:
        lakehouses = fs.call("GET", f"/workspaces/{ws_id}/lakehouses")[2].get("value", [])
        pending = [lh["displayName"] for lh in lakehouses if lh["displayName"] in dict(LAKEHOUSES)
                   and ((lh.get("properties") or {}).get("sqlEndpointProperties") or {}).get("provisioningStatus") != "Success"]
        if not pending:
            return
        if time.time() > deadline:
            raise SystemExit(f"SQL endpoints still provisioning for {pending} - run the installer again in a few minutes.")
        print(f"  waiting for SQL endpoints: {', '.join(pending)}")
        time.sleep(20)


# ------------------------------------------------------------------------------------------------ Key Vault


def ensure_key_vault(name: str, resource_group: str | None, location: str | None) -> str:
    try:
        vault = json.loads(fs.az("keyvault", "show", "--name", name, "-o", "json"))
        print(f"  using Key Vault {name}")
    except RuntimeError:
        if not resource_group or not location:
            raise SystemExit(f"Key Vault '{name}' not found. Pass --resource-group and --location to create it, "
                             "or use an existing vault you can manage.")
        fs.az("group", "create", "--name", resource_group, "--location", location, "-o", "none")
        vault = json.loads(fs.az("keyvault", "create", "--name", name, "--resource-group", resource_group,
                                 "--location", location, "--enable-rbac-authorization", "true",
                                 "--enable-purge-protection", "true", "-o", "json"))
        print(f"  created Key Vault {name} in {resource_group} ({location})")
    props = vault.get("properties", vault)
    user = fs.az("ad", "signed-in-user", "show", "--query", "id", "-o", "tsv")
    try:  # access may already come from a parent scope or a group - test the data plane before assigning anything
        strava_auth.KeyVault(props["vaultUri"]).get("strava-client-id")
        print("  your account can read and write secrets")
        return props["vaultUri"]
    except SystemExit:
        pass
    if props.get("enableRbacAuthorization"):
        assigned = json.loads(fs.az("role", "assignment", "list", "--assignee", user, "--scope", vault["id"],
                                    "--role", SECRETS_OFFICER, "--include-inherited", "--include-groups", "-o", "json"))
        if not assigned:
            try:
                fs.az("role", "assignment", "create", "--assignee-object-id", user, "--assignee-principal-type", "User",
                      "--role", SECRETS_OFFICER, "--scope", vault["id"], "-o", "none")
                print(f"  granted '{SECRETS_OFFICER}' to your account (can take a few minutes to apply)")
            except RuntimeError as exc:
                raise SystemExit(f"Could not assign '{SECRETS_OFFICER}' on the vault ({exc}). Ask the vault owner to grant "
                                 "it to your account, then run the installer again.")
    else:
        fs.az("keyvault", "set-policy", "--name", name, "--object-id", user, "--secret-permissions", "get", "list", "set", "-o", "none")
        print("  access policy for your account set (get / list / set secrets)")
    return props["vaultUri"]


def wait_for_vault_access(vault: strava_auth.KeyVault) -> None:
    for attempt in range(20):
        try:
            vault.get("strava-client-id")
            return
        except SystemExit as exc:
            if "denied" not in str(exc) or attempt == 19:
                raise
            print("  waiting for the Key Vault role assignment to apply ...")
            time.sleep(30)


# ------------------------------------------------------------------------------------------------ deployment


def item_folders(item_type: str) -> list[Path]:
    return sorted(p for p in fs.ROOT.glob(f"*.{item_type}") if p.is_dir())


def schedule_transform(time_of_day: str, timezone: str):
    def transform(rel: str, data: bytes) -> bytes:
        if rel != ".schedules":
            return data
        doc = json.loads(data)
        for s in doc.get("schedules", []):
            s["enabled"] = True
            s["configuration"].update({
                "type": "Daily", "times": [time_of_day], "localTimeZoneId": timezone,
                "startDateTime": dt.date.today().isoformat() + "T00:00:00",
                "endDateTime": (dt.date.today() + dt.timedelta(days=365 * 5)).isoformat() + "T23:59:00",
            })
        return json.dumps(doc, indent=2).encode()
    return transform


def deploy(folder: Path, ws_id: str, key_vault_url: str, transform=None) -> str:
    target = fs.describe(ws_id, key_vault_url)
    return fs.deploy(folder, ws_id, fs.replacements(target), transform)


def gold_tables(ws_id: str) -> set[str]:
    lh = next(l for l in fs.call("GET", f"/workspaces/{ws_id}/lakehouses")[2]["value"] if l["displayName"] == "Strava_Gold")
    try:
        return {t["name"] for t in fs.call("GET", f"/workspaces/{ws_id}/lakehouses/{lh['id']}/tables")[2].get("data", [])}
    except SystemExit:
        return set()


def main() -> None:
    previous = fs.LOCAL
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workspace", default=previous.get("workspaceName", "Strava Insights"), help="Fabric workspace name")
    parser.add_argument("--capacity", help="Fabric capacity name or id (needed when you have several)")
    parser.add_argument("--key-vault", default=previous.get("keyVaultName"), help="Azure Key Vault name (globally unique)")
    parser.add_argument("--resource-group", help="resource group for a new Key Vault")
    parser.add_argument("--location", help="Azure region for a new Key Vault, e.g. westeurope")
    parser.add_argument("--schedule", default=previous.get("schedule", "05:00"), help="daily refresh time HH:MM (default 05:00)")
    parser.add_argument("--timezone", default=previous.get("timezone") or default_timezone(),
                        help="Windows time zone id for the schedule, e.g. 'Central Europe Standard Time'")
    parser.add_argument("--reconnect-strava", action="store_true", help="re-run the Strava consent even if connected")
    parser.add_argument("--skip-first-run", action="store_true", help="do not run the pipeline now")
    args = parser.parse_args()

    if sys.version_info < (3, 10):
        sys.exit("Python 3.10 or later is required.")
    if not args.key_vault:
        sys.exit("Pass --key-vault <name> (an existing vault, or a new globally unique name with --resource-group and --location).")
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", args.schedule):
        sys.exit("--schedule must be HH:MM, e.g. 05:00")
    try:
        account = json.loads(fs.az("account", "show", "-o", "json"))
    except (RuntimeError, OSError):
        sys.exit("Azure CLI not found or not signed in. Install it (https://aka.ms/azure-cli) and run: az login")
    print(f"Signed in as {account['user']['name']} (tenant {account['tenantId']})")

    step(1, "Fabric workspace")
    ws = ensure_workspace(args.workspace, args.capacity)
    ws_id = ws["id"]
    step(2, "Lakehouses")
    ensure_lakehouses(ws_id)

    step(3, "Azure Key Vault")
    vault_url = ensure_key_vault(args.key_vault, args.resource_group, args.location)
    vault = strava_auth.KeyVault(vault_url)
    wait_for_vault_access(vault)

    config = {"workspaceId": ws_id, "workspaceName": ws["displayName"], "keyVaultName": args.key_vault,
              "keyVaultUrl": vault_url, "schedule": args.schedule, "timezone": args.timezone}
    fs.LOCAL_CONFIG.write_text(json.dumps({**previous, **config}, indent=2) + "\n", encoding="utf-8")

    step(4, "Strava connection")
    strava_auth.connect(vault, reauthorize=args.reconnect_strava)

    step(5, "Notebooks and pipeline")
    for folder in item_folders("Notebook"):
        deploy(folder, ws_id, vault_url)
    deploy(fs.ROOT / f"{PIPELINE}.DataPipeline", ws_id, vault_url, schedule_transform(args.schedule, args.timezone))
    print(f"  daily refresh at {args.schedule} ({args.timezone})")

    step(6, "First data load")
    first_run_ok = None
    if args.skip_first_run:
        print("  skipped (--skip-first-run)")
    elif {"FactWorkout", "DataStatus"} <= gold_tables(ws_id):
        print("  data already loaded - the daily schedule keeps it current")
    else:
        print("  running Refresh_Orchestration: loads your Strava history (first run ~20-60 min; detailed data for long "
              "histories completes over the next daily runs because of Strava's API limits)")
        try:
            fs.run(PIPELINE, "DataPipeline", ws_id, timeout_minutes=300, attach_running=True)
            first_run_ok = True
        except SystemExit as exc:
            first_run_ok = False
            print(f"  first run did not complete: {exc}. Open the workspace -> Monitor to see the failing step; "
                  "the report is deployed anyway.")

    step(7, "Semantic model, report and data agent")
    if not {"FactWorkout", "DataStatus"} <= gold_tables(ws_id):
        print("  Gold tables do not exist yet - run the pipeline, then run this installer again to deploy the report.")
        report_id = None
    else:
        deploy(fs.ROOT / f"{MODEL}.SemanticModel", ws_id, vault_url)
        fs.run("Refresh_Semantic_Models", "Notebook", ws_id)
        report_id = deploy(fs.ROOT / f"{REPORT}.Report", ws_id, vault_url)
        try:
            deploy(fs.ROOT / f"{AGENT}.DataAgent", ws_id, vault_url)
        except SystemExit as exc:
            print(f"  data agent skipped ({str(exc)[:200]}). It needs a paid F2+ capacity and the Fabric data agent "
                  "tenant setting; the report works without it.")

    print("\nDone.")
    print(f"  Workspace: https://app.fabric.microsoft.com/groups/{ws_id}")
    if report_id:
        print(f"  Report:    https://app.powerbi.com/groups/{ws_id}/reports/{report_id}")
    if first_run_ok is False:
        print("  The first pipeline run failed - see docs/setup.md#troubleshooting.")
    print("  Settings saved to stravainsights.local.json (no secrets). Re-run this command after 'git pull' to upgrade.")


if __name__ == "__main__":
    main()
