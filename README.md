# weelect prototype

Address -> who represents you (federal, state legislature, a few mayors) + profile pages.

## Run locally
```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python scripts/fetch_data.py   # optional: refresh bulk data (already in data/)
.venv/bin/python app.py                  # http://127.0.0.1:5000
```
No keys are required to run. Optional (both free):
- `CONGRESS_API_KEY` – sign up at https://api.congress.gov/sign-up/ -> shows sponsored bills on federal profiles (5,000 req/hr).
- `OPENSTATES_API_KEY` – register at https://open.pluralpolicy.com -> exact point-based state legislator matching (`people.geo`). Without it, state legislators are matched by district name from the CC0 Open States data snapshot.

e.g. `CONGRESS_API_KEY=xxxx OPENSTATES_API_KEY=yyyy .venv/bin/python app.py`

## Notes
- Census vintage `ACS2025_Current` is used on purpose: the default `Current_Current` returns the *120th* Congress district map (starts Jan 2027), which can differ from the districts of sitting (119th) members after mid-decade redistricting.
- Data: congress-legislators (CC0), openstates/people (CC0). Photos for federal members are hotlinked from unitedstates.github.io/images (should be copied/hosted before launch).

## Recent votes (federal profiles)
- House: House Clerk XML (clerk.house.gov/evs/<year>/rollNNN.xml). Each roll call is cached forever in `cache/house/`; the "latest roll number" is re-checked every 30 minutes.
- Senate: Voteview (UCLA) bulk CSVs, re-downloaded every 12 hours into `cache/voteview/`. Used because www.senate.gov returned HTTP 403 (Akamai block) to the box this was built on; each vote links to the Senate.gov record. Voteview is also the House fallback if the Clerk is down.
- If both sources fail, the profile shows a short error instead of crashing. Voteview data can lag the official feeds by days.
- `scripts/fetch_data.py` also downloads federal photos to `static/photos/` (about 30 MB, 524 of 539 members have one).

## Production server
`.venv/bin/gunicorn -w 2 -b 0.0.0.0:8000 app:app`
