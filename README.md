# We Elect prototype (weelect.org)

Address -> who represents you (federal, state legislature, plus mayors and some county leaders where verified) + profile pages.

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

## Elections panel (Nov. 3, 2026)
The results page has an "Elections & voting" panel: election date + countdown, key dates for the state (early voting, registration and mail-ballot deadlines, poll hours), offices up, statewide ballot measures, and the state's official polling-place lookup. **Every item shows its source.** Nothing is invented: if a fact has no source, it is not shown.
- `elections.py` builds the panel. Federal seats come from congress-legislators term end dates (House `end == 2027-01-03`; Senate `end <= 2027-01-03`, which also catches the OH/FL specials).
- `data/elections_2026.json` is built by `scripts/fetch_elections.py` (needs `pip install beautifulsoup4`; re-run it to refresh): Ballotpedia (hours, early voting, deadlines, statewide measures), Wikipedia (governor + state legislative seats up), plus hand-curated official state lookup URLs (link-checked 2026-10-26; AK, IA, KY, NH, OH, RI block automated checks but are listed by Ballotpedia).
- **Optional `GOOGLE_CIVIC_API_KEY`** (free; Google Cloud Console -> enable "Google Civic Information API" -> create an API key). Unlocks address-specific polling place, early-vote sites, drop boxes, and the full contest/measure list via `voterinfo` (election id `GOOGLE_CIVIC_ELECTION_ID`, default `12000` = Nov. 3, 2026 General). Data is published by the Voting Information Project typically 2-4 weeks before the election and expires after Election Day. Without the key the panel links to the state lookup instead.
- **Voting-locations map** (only when Civic returns sites): Leaflet 1.9.4 is self-hosted in `static/vendor/leaflet/`; `static/elmap.js` draws the voter (navy star) and sites (red pins P/E/D) on OpenStreetMap tiles (`tile.openstreetmap.org`, attribution shown, browser sends Referer + UA per the OSM Tile Usage Policy; muted with a CSS filter). Site coordinates come from the Civic response; sites without them are geocoded with the free Census geocoder (max 12 new lookups per request, cached in memory and `cache/site_geocode.json`). At most 60 pins are sent; the map zooms to the voter + nearest 12. No sites/coords -> the map is not rendered. If traffic grows, switch the tile URL in `elmap.js` to a provider that permits it (the OSM policy is for light use).
- No FEC key is needed (candidate lists are not shown: FEC filings include candidates who lost primaries).

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

## Branding & assets (Oct 2026)
Broadsheet look (serif type, thin rules, cream paper, muted red/navy), faint continental-US outline as page background,
and a state outline + state flag on the results dashboard (all 50 states, DC and the five territories).
- `static/style.css`, `static/logo-mark.svg`, `static/favicon.svg` – our own design (logo and wordmark are original).
- `static/states/*.svg`, `static/us-outline.svg` – built by `scripts/build_state_svgs.py` from the U.S. Census Bureau 2023 Cartographic Boundary File `cb_2023_us_state_500k` (U.S. Government work, no copyright; Census asks to be cited, which the footer/credits page do).
- `static/flags/*` – Wikimedia Commons flag files (license metadata checked via the Commons API on 2026-10-25: 55 are "Public domain", Mississippi is "Copyrighted free use" - the state says the design "can be freely used by members of the public"). No attribution required; all 56 sources are listed on `/credits` (data in `data/flag_credits.json`). Files over 60 KB after SVG optimization (scour) were converted to 480 px WebP.
- `static/fonts/` – Playfair Display and Source Serif 4 (SIL OFL 1.1, via Fontsource); see `static/fonts/FONT-LICENSES.txt`.



**Voting-locations map is opt-in:** the "Where & when to vote" card shows the text list by default; a "Show map" button (with `aria-expanded`) reveals the map. Leaflet and OSM tiles are not requested until the first click (`static/elmap.js`), and each list entry has a "show on map" link that opens that pin's popup.

## Both sides on key votes

Editorial section showing the strongest case for and against a few notable votes (`/votes`, `/votes/<id>`, plus an expandable "Both sides" on member profiles).

- **Data:** `data/counterarguments.json` — `meta` (last reviewed date, disclaimer), `sources` (id → org, title, url, type, date, checked) and `items` (summary, `rolls` pointing at real House Clerk / Senate roll numbers, `for` and `against` points, each citing source ids). `bothsides.py` loads it and looks up each member's vote on the featured rolls.
- **Rules:** same structure and length for both sides, always "for" then "against", no party colors, at least two real sources per side, no quote or number that is not in a source we opened. Anything unverifiable is left out. Update `last_reviewed` whenever an item is re-checked.
- **Adding an item:** add sources and an item to the JSON (rolls: House `chamber/year/roll`, Senate `chamber/session/roll`), re-read every cited page, and check each quote and number against the page text.
- Corrections contact on `/credits#both-sides` is generic (a Brunner Creative site); add a real address before launch.

### "What neither side is saying"
Each item in `data/counterarguments.json` has an `unsaid` list (`label`, `text`, `src[]`) shown full width under the two columns (copy in `meta.unsaid_*`). Only add a point if it concerns the bill's effects, cost, enforcement or who it affects, is not covered by the for/against arguments, is sourced to a page you opened, and states no opinion on what should be done. Add its sources to `sources` and re-check every number and quote against the source text.

## Search, sharing and privacy (Oct 2026)
- **Meta / sharing:** `templates/base.html` emits canonical, Open Graph and Twitter card tags; each page type sets its own `title` and `meta_description` blocks. The share image is `static/og-default.png` (1200x630), built from `scripts/og-image.html` (command in the file). `SITE_URL` (default `https://weelect.org`) controls canonical/OG/sitemap URLs.
- **robots.txt / sitemap.xml** are generated by `app.py`. The sitemap lists the home page, `/votes`, each vote page, `/credits` and the federal member profiles. Set `SITEMAP_INCLUDE_STATE=1` to add the ~7,700 state-legislator profiles (thinner pages; off by default). `/lookup` and `/city-interest` are disallowed.
- **Address privacy:** the address form POSTs to `/lookup`, so the address is not in the URL, access logs, history or shared links. Results are `Cache-Control: no-store` and `noindex`. An old `GET /lookup?address=...` link renders a bridge page that re-submits as POST. The site sets `Referrer-Policy: strict-origin-when-cross-origin`.
- **"Tell us your city"** (`/city-interest`): city + state only; one `CITY_INTEREST {...}` line is written to the app log. No database. TODO(Bob): move to a durable destination if you want to keep these.
- **About page placeholders:** set `CORRECTIONS_EMAIL` and `RESPONSIBLE_NAME` (Render env vars) to replace the `[TODO(Bob): ...]` text.
- The masthead date uses U.S. Eastern time (same clock as the election countdown); `tzdata` is in requirements.txt.

## Local officials data
`data/local_officials.json` (keyed by Census place / county GEOID) is built offline with `python scripts/build_local_officials.py` from free data only: Wikidata (SPARQL), openstates/people mayor records, Wikipedia infobox names for large counties, and the Census Gazetteer. Each entry is marked `confirmed` (name+title found on the cited government page), `corroborated` (two datasets agree) or `unverified` (hidden in the UI). `localdata.py` serves it; the Census Geocoder supplies the GEOIDs (places, counties, county subdivisions, school districts). No per-lookup cost.
