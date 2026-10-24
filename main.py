"""Sarkari Saathi: government scheme navigator powered by the TinyFish Agent API."""
import asyncio, json, os, sqlite3, time
from pathlib import Path
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import hashlib
from urllib.parse import urlparse
from helpers import parse_text, score, pick_gov, unwrap_result, clean_result

load_dotenv()
API_KEY = os.getenv("TINYFISH_API_KEY", "")
AGENT_URL = os.getenv("TINYFISH_AGENT_URL", "https://agent.tinyfish.ai/v1/automation/run-sse")
BASE = Path(__file__).parent
SCHEMES = {s["id"]: s for s in json.loads((BASE / "schemes.json").read_text())}
LIVE = {}  # schemes discovered by live TinyFish Search
SEARCH_URL = "https://api.search.tinyfish.ai"
MAX_RUNS_PER_HOUR = int(os.getenv("MAX_RUNS_PER_HOUR", "15"))  # hard credit guard
CACHE_TTL = int(os.getenv("CACHE_TTL_HOURS", "24")) * 3600
RUN_LOG = []
SCHEMA = {"type": "object", "properties": {
    "scheme_name": {"type": "string"}, "eligible": {"type": "boolean"}, "confidence": {"type": "string"}, "summary": {"type": "string"},
    "benefit": {"type": "string"}, "reasons": {"type": "array", "items": {"type": "string"}},
    "not_eligible_because": {"type": "array", "items": {"type": "string"}},
    "documents_required": {"type": "array", "items": {"type": "string"}},
    "how_to_apply": {"type": "array", "items": {"type": "string"}}, "deadline": {"type": "string"},
    "apply_url": {"type": "string"}, "helpline": {"type": "string"}, "evidence": {"type": "string"}}}
PREFILL_SCHEMA = {**SCHEMA, "properties": {**SCHEMA["properties"],
    "fields_filled": {"type": "array", "items": {"type": "string"}},
    "fields_left_blank": {"type": "array", "items": {"type": "string"}},
    "stopped_at": {"type": "string"}, "form_url": {"type": "string"}}}
SEM = asyncio.Semaphore(2)  # max parallel live agent runs
DB = sqlite3.connect(BASE / "runs.db", check_same_thread=False)
DB.execute("CREATE TABLE IF NOT EXISTS cache(k TEXT PRIMARY KEY, ts REAL, result TEXT)")
DB.execute("CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY, ts REAL, scheme_id TEXT, name TEXT, eligible INTEGER, result TEXT)")


def save_run(scheme, data):
    DB.execute("INSERT INTO runs(ts,scheme_id,name,eligible,result) VALUES(?,?,?,?,?)",
               (time.time(), scheme["id"], scheme["name"], int(bool(data.get("eligible"))), json.dumps(data)))
    DB.commit()

app = FastAPI(title="Sarkari Saathi")
from assist import router as assist_router
app.include_router(assist_router)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")


class Profile(BaseModel):
    age: int
    state: str
    category: str
    income: int
    occupation: str
    gender: str = "male"
    full_name: str = ""
    dob: str = ""
    mobile: str = ""
    district: str = ""
    course: str = ""


class VerifyRequest(BaseModel):
    scheme_id: str
    profile: Profile
    mode: str = "quick"   # quick = read-only and cheap; prefill = also fills the form (more credits)
    force: bool = False   # skip the cache


def prefilter(p: Profile):
    """Cheap local filter + match score, so Agent credits go only to likely matches."""
    out = []
    for s in SCHEMES.values():
        r = score(s, p.model_dump())
        if r:
            out.append({**{k: s[k] for k in ("id", "name", "benefit", "url", "docs")}, "form_url": s.get("form_url"), **r})
    return sorted(out, key=lambda x: -x["match"])


def sse(obj):
    return f"data: {json.dumps(obj)}\n\n"


async def discover(p: Profile):
    """Real web discovery: TinyFish Search API, filtered to official government domains."""
    who = "women " if p.gender == "female" else ""
    qs = [f"{who}{p.occupation} welfare scheme government of India eligibility apply online",
          f"{p.category} {p.occupation} scholarship subsidy loan scheme government India",
          f"{p.state} government scheme for {p.occupation} apply online"]
    purpose = "Find official government scheme pages a citizen can apply to"
    async def one(q):
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get(SEARCH_URL, params={"query": q, "purpose": purpose}, headers={"X-API-Key": API_KEY})
            r.raise_for_status()
            return r.json().get("results", [])
    try:
        batches = await asyncio.gather(*[one(q) for q in qs])
    except Exception as e:
        return [], f"Live search failed: {e}"
    hits = pick_gov([x for b in batches for x in b], [s["url"] for s in SCHEMES.values()])
    out = []
    for h in hits:
        sid = "live-" + hashlib.md5(h["url"].encode()).hexdigest()[:8]
        s = {"id": sid, "name": h["title"][:90], "url": h["url"], "benefit": h["snippet"][:160], "docs": [], "source": "search",
             "reasons": ["Found just now by TinyFish Search on an official .gov.in site"], "match": None}
        LIVE[sid] = s
        out.append(s)
    return out, ("" if out else "Live search returned no official government pages for this profile.")


async def mock_stream(scheme, profile, mode="quick", force=False):
    for st in ["Opening " + scheme["url"], "Reading eligibility rules", "Comparing rules with applicant profile"]:
        await asyncio.sleep(0.8)
        yield sse({"type": "progress", "message": st})
    data = {"eligible": True, "confidence": "medium", "summary": "Simulated answer for demo only.",
            "benefit": scheme["benefit"], "reasons": ["Occupation matches", "Income within limit"],
            "documents_required": scheme["docs"], "how_to_apply": ["Open the portal", "Register with Aadhaar", "Fill the form", "Upload documents"],
            "deadline": "Check portal", "apply_url": scheme["url"], "evidence": "Simulated",
            "fields_filled": ["Name = demo", "Age = %s" % profile.age, "State = %s" % profile.state] if mode == "prefill" else [], "stopped_at": "Simulated stop before OTP"}
    yield sse({"type": "result", "data": data, "simulated": True})


def cache_key(scheme, profile, mode):
    return hashlib.md5(json.dumps(["v7", scheme["id"], mode, profile.model_dump()], sort_keys=True).encode()).hexdigest()


def build_goal(scheme, profile, mode):
    host = urlparse(scheme["url"]).netloc
    base = ("Applicant profile: %s. Government scheme: '%s'. Stay on the website %s only; do not open news, press releases or other schemes' sites. "
            "Answer ONLY about this one scheme and ignore all other schemes. " % (profile.model_dump_json(), scheme["name"], host))
    if mode == "prefill":
        task = ("Find and open the application or registration form for this scheme (a registration page that needs no login is fine). "
                "Fill every field you can using ONLY values from the profile; leave other fields blank and never invent values. "
                "Never enter Aadhaar numbers, passwords or OTPs. Stop before OTP, captcha, login verification or any final Submit button. "
                "Report each filled field as 'Field name = value' in fields_filled, fields you left blank in fields_left_blank, "
                "where you stopped in stopped_at, and the form page URL in form_url. Also answer the other fields briefly.")
    else:
        task = ("READ-ONLY task: visit at most 4 pages, do not log in, do not fill or submit any form. "
                "Find eligibility rules, benefits, required documents, how to apply, deadline, helpline and the official apply link, "
                "and decide if this applicant is eligible.")
    return (base + task + " Be concise: one short sentence per item, at most 6 items per list. Only state facts you saw on the pages and write 'not found' for anything missing. "
        "If a page covers many schemes, choose the single scheme that best fits the applicant, put its name in 'scheme_name' and answer only for it. "
        "Put the page you relied on in 'evidence'.")


async def live_stream(scheme, profile, mode="quick", force=False):
    key = cache_key(scheme, profile, mode)
    if not force:
        row = DB.execute("SELECT ts,result FROM cache WHERE k=?", (key,)).fetchone()
        if row and time.time() - row[0] < CACHE_TTL:
            yield sse({"type": "progress", "message": "Served from cache: 0 credits used"})
            yield sse({"type": "result", "data": json.loads(row[1]), "cached_at": row[0], "simulated": False}); return
    now = time.time()
    RUN_LOG[:] = [t for t in RUN_LOG if now - t < 3600]
    if len(RUN_LOG) >= MAX_RUNS_PER_HOUR:
        yield sse({"type": "error", "message": f"Credit guard: {MAX_RUNS_PER_HOUR} live runs per hour reached. Try cached results or wait."}); return
    secs = 240 if mode == "prefill" else 75
    schema = PREFILL_SCHEMA if mode == "prefill" else SCHEMA
    base = {"url": scheme["url"], "goal": build_goal(scheme, profile, mode)}
    variants = [{**base, "output_schema": schema, "agent_config": {"max_duration_seconds": secs}},
                {**base, "agent_config": {"max_duration_seconds": secs}}, base]  # fall back if API rejects a field
    headers = {"X-API-Key": API_KEY, "Content-Type": "application/json"}
    async with SEM:
        failed, run_id = "no attempt", ""
        for body in variants:
            failed = None
            try:
                async with httpx.AsyncClient(timeout=None) as client:
                    async with client.stream("POST", AGENT_URL, headers=headers, json=body) as r:
                        if r.status_code in (400, 422):
                            failed = f"TinyFish rejected request fields (HTTP {r.status_code}), trying simpler request"
                            yield sse({"type": "progress", "message": failed}); continue
                        if r.status_code != 200:
                            failed = f"TinyFish returned HTTP {r.status_code}"; break
                        RUN_LOG.append(time.time())
                        async for line in r.aiter_lines():
                            if not line.startswith("data:"): continue
                            try: evt = json.loads(line[5:].strip())
                            except ValueError: continue
                            run_id = evt.get("run_id") or run_id
                            typ = evt.get("type")
                            if typ == "STREAMING_URL":
                                yield sse({"type": "stream", "url": evt.get("streaming_url")}); continue
                            if typ == "COMPLETE":
                                res = clean_result(unwrap_result(evt)) if evt.get("status") == "COMPLETED" else None
                                if res is None:
                                    failed = f"Run ended with status {evt.get('status')}"; break
                                DB.execute("INSERT OR REPLACE INTO cache VALUES(?,?,?)", (key, time.time(), json.dumps(res)))
                                save_run(scheme, res)
                                yield sse({"type": "result", "data": res, "run_id": run_id, "simulated": False}); return
                            msg = evt.get("purpose") or ("Run started" if typ == "STARTED" else str(typ))
                            yield sse({"type": "progress", "message": msg, "run_id": run_id})
                        failed = failed or "Agent finished without a result"
                        break
            except httpx.HTTPError as e:
                failed = f"Network error: {e}"; break
        yield sse({"type": "error", "message": failed})


@app.get("/")
def home():
    return FileResponse(BASE / "static" / "index.html")


@app.get("/api/mode")
def mode():
    now = time.time()
    used = len([t for t in RUN_LOG if now - t < 3600])
    return {"mode": "live" if API_KEY else "demo", "runs_left": max(0, MAX_RUNS_PER_HOUR - used)}


@app.post("/api/match")
async def match(p: Profile):
    cat = [{**s, "source": "catalog"} for s in prefilter(p)]
    if not API_KEY:
        return {"schemes": cat, "note": "Demo mode: add TINYFISH_API_KEY to discover more schemes live."}
    found, note = await discover(p)
    return {"schemes": cat + found, "note": note}


@app.post("/api/verify")
async def verify(req: VerifyRequest):
    scheme = SCHEMES.get(req.scheme_id) or LIVE[req.scheme_id]
    gen = (live_stream if API_KEY else mock_stream)(scheme, req.profile, req.mode, req.force)
    return StreamingResponse(gen, media_type="text/event-stream")


class Text(BaseModel):
    text: str


@app.post("/api/parse")
def parse(t: Text):
    return parse_text(t.text)


@app.get("/api/history")
def history():
    rows = DB.execute("SELECT ts,name,eligible FROM runs ORDER BY id DESC LIMIT 15").fetchall()
    return [{"ts": r[0], "name": r[1], "eligible": bool(r[2])} for r in rows]
