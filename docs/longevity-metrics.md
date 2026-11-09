# Longevity metrics

Built by the `Gold_Longevity_Metrics` notebook into `Strava_Gold` and exposed in `StravaInsights_SM`
(display folders *Longevity* and *Biometrics*). Device-measured values always take precedence; until a device source
(Apple Health, Garmin, Oura, …) is connected, values are estimated from Strava workouts and labelled as estimates.

## Gold tables

| Table | Grain | Key columns |
|---|---|---|
| `FactDailyActivity` | every calendar day since the first workout (rest days = 0) | `active_minutes`, `moderate_minutes`, `vigorous_minutes`, `zone2_minutes`, `who_minutes`, `workouts`, `cardio_sessions`, `strength_sessions`, `steps`, `steps_method`, `is_active_day` |
| `FactBiometric` | day + metric + method | `metric_code`, `value`, `method`, `is_estimate` — `vo2max_ml_kg_min`, `ftp_watts`, `ftp_w_per_kg`, `body_weight_kg`, `max_heart_rate_bpm`, `resting_heart_rate_bpm`, `hrv_rmssd_ms`, … |
| `DimDate` | day | `Date`, `Week` (Monday), `Month`, `Weekday`, `Year` — related to `activities`, `FactDailyActivity`, `FactBiometric`, `fitness_daily`, `training_weekly_metrics` |
| `DimPeriod` | period + day | `Period` (Last 4 weeks, Last 3 / 6 / 12 months, This year, Last 2 years), relative to the refresh day. Bridge to `DimDate` (both-direction filter) behind the report's single-select Period slicer |

## Rules

| Metric | Definition | Guideline shown |
|---|---|---|
| Active minutes / day | Workout minutes ÷ calendar days in the period | — |
| Moderate / vigorous minutes | Time in HR zones 1–2 / 3–5. Without zone data: avg HR ≥ 77 % of max HR = vigorous. Strength & mobility = moderate | — |
| WHO minutes / week | moderate + 2 × vigorous | 150–300 (WHO 2020) |
| Zone 2 hours / week | Time in HR zone 2; without zones the whole workout if avg HR is 60–72 % of max HR | ≈ 3 h |
| Active days / week | Days with ≥ 30 active minutes | — |
| Strength sessions / week | Strength-family workouts | 2 (WHO muscle-strengthening) |
| Steps / day | Device `steps_count` when available; else running/walking workouts: cadence × 2 × minutes, or distance ÷ stride (1.10 m run, 0.75 m walk) | — |
| VO2max (running est.) | Jack Daniels VDOT from best-effort distance & time (1.5 km – marathon); best value inside the period. A race or hard run gives the truest value, easy runs underestimate | — |
| VO2max (cycling est.) | ACSM: 10.8 × best 5-min power ÷ body weight + 7 (needs body weight); best value inside the period | — |
| VO2max (`VO2max` measure) | Device value in the period if present, else the higher of the two estimates — the report shows the sport-specific measures instead | — |
| FTP | 95 % of best 20-min power inside the period, else Strava profile FTP | — |
| FTP vs previous period | FTP in the period vs. the same number of days just before it | — |
| W/kg | FTP ÷ latest known body weight up to the period end | — |
| CTL / form at period end | `fitness_daily` on the last day of the period; form bands: > 5 fresh, −10…5 neutral, −30…−10 productive, < −30 high fatigue | — |
| Training load (per workout) | TSS from power (normalized power and FTP) when available; else heart-rate load = Edwards TRIMP ÷ 2.4, so one hour at threshold ≈ 100 in both; else Strava Relative Effort. One scale for all sports feeds CTL / ATL / TSB and the weekly load | — |
| Workout intensity zone | Workout average HR ÷ the athlete's max HR (last 365 days): < 60 % easy, 60–70 % endurance, 70–80 % tempo, 80–90 % hard, ≥ 90 % max; *No heart rate* without HR data | — |
| Avg speed / heart rate / power / cadence | Speed = total distance ÷ total moving time; heart rate, power and cadence are averaged over workouts weighted by moving time | — |

All measures use only the selected period. The only look-back is body weight (latest known value), because it is
measured rarely.

## Known gaps

* **Steps**: Strava has no daily step count. Until Apple Health / Garmin is connected, steps only cover runs and walks.
* **Body weight / profile FTP / Strava zones** need the Strava `profile:read_all` scope. Re-authorize via the
  `python tools/strava_auth.py`; until then cycling VO2max and W/kg stay empty.
* **Resting HR, HRV, sleep** need a wearable source; the measures (`Resting HR`, `HRV ms`) are ready.
