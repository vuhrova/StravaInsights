# Reading the dashboard

The report tells one story: **am I moving enough, consistently, with a solid aerobic base — and is my fitness going
up?** Pick a **Period** first (top right, synced across pages: Last 4 weeks, 3 / 6 / 12 months, This year, Last 2
years). Every number and chart on every page uses only that period; comparisons with the *previous period* use the
same number of days just before it.

## 1. Longevity — the overview (start here)

Read the **Your period at a glance** strip from left to right; the KPI rows and charts below are the evidence.

| Step | Question | Look at | Good sign |
|---|---|---|---|
| 1 · Movement | Do I move enough? | Active minutes / day, WHO minutes / week, *Weekly active minutes vs. WHO guideline* | Bars above the 150 min line most weeks; 300+ adds little extra longevity benefit |
| 2 · Consistency | Is it a habit or bursts? | Sessions / week, Active days / week, *Sessions per week by sport* | Even bars, 4+ active days, **2 strength sessions** a week |
| 3 · Aerobic base | Do I train easy enough? | Zone 2 hours / week, *Zone 2 endurance* | ≈ 3 h of easy Zone 2 a week — the base for VO2max and healthy ageing |
| 4 · Fitness | Is fitness going up? | Running / cycling VO2max, FTP, *VO2max per sport & FTP* | Rising lines month to month; FTP up vs. the previous period |
| 5 · Focus next | What should I change? | The *Focus next* card | Picks the first gap in order: movement → strength → Zone 2 |

Then check the *Training mix* donut: a longevity routine mixes endurance (cycling / running), strength and some
walking.

## 2. Cycling and Running — the details

Both pages share one layout, read top to bottom:

1. **Overview** — how much (rides/runs, km, hours, elevation) and how hard on average.
2. **Performance KPIs** — cycling: FTP in the period, FTP vs. the previous period, best 5-min power, fitness (CTL)
   and form at the end of the period; running: running VO2max (VDOT), best 5K / 10K, longest run, cadence, km/week.
3. **Volume & intensity** — weekly distance with speed/pace, heart-rate zones (mostly Z1–Z2 with some Z4–Z5 is a
   healthy "polarised" mix), distance bands.
4. **Performance trends** — *Power curve* / *Best efforts*: orange (selected period) taller or faster than grey
   (previous period) means you got fitter. *Power vs. heart rate* / *Pace vs. heart rate*: more watts or faster pace at
   the same heart rate = better aerobic efficiency. *Fitness, fatigue & form*: CTL rising slowly = building; form
   (TSB) far below −30 = overreaching, above +5 = fresh (good before an event).
5. **Recent sessions** — what actually happened, and which weekdays carry your training.

## 3. Activity — one session

Pick a sport and an activity (newest in the period is preselected). Read the plain-language summary first, then
speed & heart rate per section (heart rate drifting up at the same speed = fatigue or heat), HR zones and splits.

## 4. Can I trust the numbers?

The line at the bottom of every page says how current the data is (*Data as of … · Synced … · 24/24 data checks
passed*). Every check run after each refresh is stored with its result, observed and expected value in the
`DataQualityCheck` table of the Strava_Gold lakehouse. *Failed* means something is wrong with the data (the refresh run is marked failed too); *Warning* explains a gap
(e.g. an unmapped sport type); *History download* shows how much detailed data Strava still has to send.

## Notes on the estimates

* **VO2max** is estimated per sport and labelled as such. *Running* uses the Jack Daniels VDOT formula on your fastest
  best effort (1.5 km – marathon) in the period — a race or hard run shows your real level, easy runs underestimate
  it. *Cycling* uses the ACSM formula on best 5-min power and needs body weight (reconnect Strava with
  `python tools/strava_auth.py` and keep *profile* ticked). A device value (Garmin, Apple Watch) replaces both once
  connected.
* **Training load** uses one scale for every sport (TSS: one hour at threshold ≈ 100) — power rides use TSS, other
  workouts a heart-rate load scaled to the same units — so fitness, fatigue and form compare rides, runs and gym
  sessions fairly.
* **Steps** only count runs and walks until a step-counting device is connected, so treat them as a minimum.
