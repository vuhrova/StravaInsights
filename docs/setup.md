# Set up Strava Insights for yourself

Strava Insights turns your Strava history into a daily-refreshed Power BI report (longevity habits, cycling, running,
single-activity analysis) and an optional AI data agent, all inside **your own** Microsoft Fabric
workspace. Your data never leaves your Microsoft tenant, and your Strava credentials are kept only in your Azure Key
Vault.

Setup takes about 15 minutes of your time plus the first data load (20–60 minutes, unattended).

## What you need

| Requirement | Why | Notes |
|---|---|---|
| **Microsoft Fabric capacity** | Runs the notebooks, pipeline, lakehouses and report | A free 60-day [Fabric trial](https://learn.microsoft.com/fabric/fundamentals/fabric-trial) or any paid F SKU (F2 is enough). Your admin must allow *Users can create Fabric items*. |
| **Power BI license to view the report** | Viewing reports on capacities below F64 | Power BI Pro, PPU or the Fabric trial. On F64 and larger, free users can view. |
| **Azure subscription** | One Key Vault for the Strava credentials | Costs cents per month. You need to be able to create a vault, or have *Key Vault Secrets Officer* on an existing one. |
| **Strava account** | Your data | Free or subscription, with at least one recorded activity. Without a subscription, Strava's *Relative Effort* is missing, but training load is still computed from power and heart rate. |
| **A computer with** Python 3.10+, [Azure CLI](https://aka.ms/azure-cli) and git | Runs the installer | Windows, macOS or Linux. The installer uses only the Python standard library. |

Optional: the **data agent** (ask questions about your training in plain language) needs a paid F2+ capacity and the
Fabric data agent / Copilot tenant settings. The installer skips it if they are not available; everything else works
without it.

## 1. Create your Strava API application (2 minutes)

1. Go to <https://www.strava.com/settings/api> and create an application:
   * **Application name**: e.g. *My Strava Insights*
   * **Category**: *Data Importer* (any category works)
   * **Website**: `http://localhost`
   * **Authorization Callback Domain**: `localhost`
2. Keep the page open. You will need the **Client ID** and **Client Secret** in step 3.

Only you can authorize your own app, and it reads only your data. Strava allows an app about 1,000 read requests a
day, which is why a long history (detailed laps, best efforts, heart-rate and power streams) fills in over a few daily
runs. Your activity list and all summary numbers are complete after the first run.

## 2. Get the code and sign in to Azure

```powershell
git clone https://github.com/vuhrova/StravaInsights.git
cd StravaInsights
az login                      # add --tenant <your-tenant> if you have several
```

## 3. Run the installer

```powershell
python tools/install.py --workspace "Strava Insights" --key-vault <unique-vault-name> --resource-group rg-strava-insights --location westeurope
```

* `--key-vault`: a globally unique name (3–24 letters, digits and dashes). If the vault does not exist, it is
  created in `--resource-group` / `--location`. You can also pass an existing vault.
* `--capacity "<name>"`: only needed when you have access to more than one Fabric capacity.
* `--schedule 05:00 --timezone "Central Europe Standard Time"`: when the daily refresh runs (Windows time zone names;
  the installer uses your computer's time zone on Windows and UTC elsewhere).

What happens:

1. Your Fabric workspace is created on your capacity (or reused).
2. Three lakehouses are created: **Bronze** (raw data), **Strava_Silver** (clean data) and **Strava_Gold** (report data).
3. The Key Vault is created and your account gets the *Key Vault Secrets Officer* role.
4. **Strava**: paste the Client ID and Client Secret when asked. Your browser opens Strava's consent page. Keep all
   boxes ticked (private activities, profile with body weight and heart-rate zones) and click **Authorize**. The
   credentials and the refresh token go straight into Key Vault.
5. The notebooks and the **Refresh_Orchestration** pipeline are deployed, and the daily schedule is set.
6. The pipeline runs once to load your history. You can watch it in the workspace under **Monitor**.
7. The semantic model, the **Strava Trends** report and the data agent are deployed.

At the end the installer prints the link to your report. Your settings (workspace, vault, schedule, no secrets) are
saved in `stravainsights.local.json` so the next run needs no arguments. The file is git-ignored, so it is never
committed or pushed to a fork.

## 4. Use the report

| Page | What it answers |
|---|---|
| **Longevity** | Movement, consistency, aerobic base and fitness markers against WHO guidelines, as a five-step story |
| **Cycling** / **Running** | Volume, intensity, performance (FTP, W/kg, VO2max, best efforts), fitness and form |
| **Activity** | One session: pacing, heart rate, terrain, splits and plain-language insights |

Every page has a **Period** selector (last 4 weeks to last 2 years) and, at the bottom, a status line:
*Data as of … · Synced … · 24/24 data checks passed*. See [reading-the-dashboard.md](reading-the-dashboard.md) for how to
read the numbers and [longevity-metrics.md](longevity-metrics.md) for every formula.

## Keeping it running

| Task | How |
|---|---|
| Daily refresh | Automatic (Refresh_Orchestration schedule). Change it with `python tools/install.py --schedule 06:30`. |
| Update to a new version | `git pull`, then `python tools/install.py`. Your data stays; definitions are updated in place. |
| Reconnect Strava (password change, revoked access, missing permissions) | `python tools/strava_auth.py` |
| Get notified when a refresh fails | In the workspace open **Refresh_Orchestration → Schedule**, or create an alert on the pipeline's job events in **Real-Time hub → Fabric events → Job events** (Activator), e.g. an e-mail or Teams message when a run fails. |
| Pause costs | Pause the Fabric capacity in the Azure portal. Scheduled refreshes are skipped while it is paused. |

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `No active Fabric capacity is available` | Start a Fabric trial or ask your admin to give you access to an F capacity, then run the installer again. |
| `Access to Key Vault … denied` | The role assignment is still propagating (wait 5 minutes and re-run), or you cannot assign roles on the vault: ask its owner to grant you *Key Vault Secrets Officer*. |
| Pipeline fails in **Bronze_Ingest** with `Strava token refresh failed: HTTP 400/401` | The Strava authorization was revoked or the app secret changed. Run `python tools/strava_auth.py` (add `--new-credentials` if you reset the Client Secret on Strava). |
| Pipeline fails in **Bronze_Ingest** with `Cannot read secret` | The person who owns the pipeline schedule needs *Key Vault Secrets Officer* on the vault. The schedule runs as the account that last saved it. |
| Ingestion status `partial` | Normal while the history is downloading: Strava's daily limit was reached. The next daily run continues. The `History download` row of the `DataQualityCheck` table (Strava_Gold lakehouse) shows what is left. |
| Run marked failed with `DataQualityFailed` | A data check failed. The report is still refreshed: the status line at the bottom of each report page names the problem; the `DataQualityCheck` table in the Strava_Gold lakehouse shows every check with its observed and expected value. Most failures clear on the next run; persistent ones are a bug, please open an issue with the check name. |
| Browser shows *redirect_uri invalid* during Strava authorization | The app's *Authorization Callback Domain* must be `localhost`. |
| `SQL endpoints still provisioning` | New lakehouses can take a few minutes. Run the installer again. |
| Data agent skipped | Needs a paid F2+ capacity and the Fabric data agent tenant setting. Optional. |

## Privacy and security

* All data is stored in your Fabric workspace in your Microsoft tenant. Nothing is sent anywhere else.
* GPS coordinates are not stored, only time series of speed, heart rate, power, cadence, altitude and temperature.
* Strava credentials live only in your Key Vault. Notebooks read them at run time and never print or store them. A
  data check fails the refresh if a token table ever appears in a lakehouse.
* To remove everything: delete the Fabric workspace, delete the Key Vault (or the three `strava-*` secrets), and
  revoke the app at <https://www.strava.com/settings/apps>.
