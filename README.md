# Sarkari Saathi (prototype)

Describe yourself, see matching schemes, and let a TinyFish web agent verify eligibility on the live portal and pre-fill the form. The agent is told never to click final Submit.

## Run
```
pip install -r requirements.txt
cp .env.example .env        # add TINYFISH_API_KEY (leave empty for demo mode)
uvicorn main:app --reload
```
Open http://localhost:8000

## How it uses TinyFish
- `main.py` -> `live_stream()` calls the TinyFish **Agent API** (`run-sse`) with `{url, goal}` and streams progress events to the browser.
- The goal text lives in `schemes.json`, one template per portal. Add a scheme = add one JSON entry.
- Without an API key the app runs in **demo mode** with simulated events, so you can build the UI first.

## Before the demo
1. Check the request and event field names against the TinyFish docs (`extract_result` in `main.py` is defensive, but confirm).
2. Run each portal once live and tune its goal text.
3. Record a backup video of a successful run.

## Next steps
Add TinyFish Search/Fetch for scheme discovery, a Hindi UI, WhatsApp input, and persistent run history.

## v2 additions
- Describe yourself in free text or by voice (Chrome mic, Hindi or English); the form fills itself
- 8 schemes with match scores and "why it matched" chips (rules in `helpers.py`, schemes in `schemes.json`)
- "Verify all" runs several agent checks in parallel (max 2 live at a time) with a progress bar
- Retry once on failed agent runs; every result is saved to `runs.db` (SQLite) and shown under Recent checks
- Document checklist (remembered in your browser), Print my plan, WhatsApp share
- Hindi / English interface toggle

## v3: real live results
- **Live discovery:** with an API key, `/api/match` calls the TinyFish **Search API** (`GET https://api.search.tinyfish.ai`) and keeps official `.gov.in` / `.nic.in` pages, so the list is different for every profile and is not pre-saved.
- **Proof of liveness:** each verify shows timestamped agent steps, the TinyFish run id, run time and the portal URL. Compare the run id with your runs on agent.tinyfish.ai.
- **Honest demo mode:** without a key, a red SIMULATED banner is shown and results are labelled as fake.

## v4: richer results, lower cost
- **Quick check (default):** read-only, max 75 s and 4 page visits, no form fill. **Pre-fill form** is opt-in (max 180 s).
- **Richer answer** via `output_schema`: eligibility + confidence, summary, benefit, reasons, why not, documents, how-to-apply steps, deadline, helpline, official link, source evidence. Falls back to simpler requests if the API rejects a field.
- **Cache:** same scheme + profile + mode is answered from SQLite for 24 h at 0 credits (Re-run live to refresh).
- **Credit guard:** `MAX_RUNS_PER_HOUR` (default 15); the header shows runs left. "Verify all" checks only the top 3 matches.

## v5: watch the form being filled
- **Fill the form for me (live)** runs the agent in prefill mode and embeds TinyFish's live browser view (`STREAMING_URL` event) so you can watch it type into the portal form.
- Optional applicant details (name, DOB, mobile, district) are used to fill fields. The agent never invents values, never enters Aadhaar/OTP/passwords, and stops before OTP, login or final Submit.
- The result lists fields filled, fields left for you, and where it stopped. Prefill always runs live (never cached).

## v6: Fill with me (100%)
Assisted filling on a TinyFish **Browser API** session driven by Playwright. Needs the Browser API enabled on your TinyFish plan.
- Live screenshot of the portal, click-through, and typing from the app.
- Side box lists the fields on the current page, auto-filled from your profile. Aadhaar, OTP, captcha, password and bank fields are never auto-filled; the citizen types them and they go straight to the portal (not stored).
- Captcha image is shown in the side box. Page buttons are listed; the citizen presses each one, including final submit.
- Only https .gov.in / .nic.in pages are allowed.
