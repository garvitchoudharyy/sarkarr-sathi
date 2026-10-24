"""Assisted form filling: a TinyFish remote browser that WE control, with the citizen in the loop.
The page is shown as a live screenshot; fields are auto-filled from the profile; OTP, Aadhaar and captcha are typed by the citizen
in a side box and go straight to the page (never stored or logged); every button, including final submit, is pressed by the citizen."""
import base64, os, re, uuid
from urllib.parse import urlparse
import httpx
from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel

router = APIRouter(prefix="/api/assist")
SESS = {}
BROWSER_URL = "https://api.browser.tinyfish.ai"
SKIP = re.compile(r"aadhaar|aadhar|otp|captcha|password|\bpin\b|bank|account|ifsc|father|mother|guardian|spouse|school|institution|college", re.I)
RULES = [("mobile", r"mobile|phone"), ("dob", r"birth|dob"), ("district", r"district"), ("state", r"\bstate\b"), ("gender", r"gender|\bsex\b"),
         ("category", r"category|caste"), ("income", r"income"), ("age", r"\bage\b"), ("full_name", r"name")]
SCAN_JS = """() => {
 const vis=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.display!=='none'};
 const cap=[...document.images].find(im=>vis(im)&&/captcha/i.test(im.id+im.src+im.alt+im.className));
 document.querySelectorAll('[data-ss-cap]').forEach(e=>e.removeAttribute('data-ss-cap')); if(cap)cap.setAttribute('data-ss-cap','1');
 let i=0;const fields=[],buttons=[];
 document.querySelectorAll('input,select,textarea,button,a,[role=button]').forEach(e=>{
  if(!vis(e))return;const tag=e.tagName.toLowerCase(),type=(e.type||'').toLowerCase();
  if(['hidden','image'].includes(type))return;e.setAttribute('data-ss',i);
  if(tag==='button'||tag==='a'||['submit','button','reset'].includes(type)||e.getAttribute('role')==='button'){
    const text=(e.innerText||e.value||'').trim().slice(0,40);if(text)buttons.push({i,text});i++;return}
  const lab=(e.labels&&e.labels[0]?e.labels[0].innerText:'')||e.getAttribute('aria-label')||e.placeholder||e.name||e.id||'';
  const o={i,tag,type,label:lab.trim().slice(0,80),name:e.name||e.id||'',value:e.value||'',checked:!!e.checked};
  if(tag==='select')o.options=[...e.options].map(x=>x.text.trim()).slice(0,80);
  fields.push(o);i++;});
 return {fields,buttons:buttons.slice(0,40)};}"""


def suggest(f, p):
    """Suggest a value from the profile. Never for Aadhaar, OTP, captcha, passwords or bank fields."""
    text = f.get("label", "") + " " + f.get("name", "")
    if f.get("type") in ("checkbox", "radio", "password", "file") or SKIP.search(text) or f.get("value"):
        return ""
    for k, rx in RULES:
        if re.search(rx, text, re.I):
            v = str(p.get(k, "") or "")
            return v.capitalize() if k == "gender" else v
    return ""


def ok_url(u):
    h = urlparse(u)
    return h.scheme == "https" and bool(h.hostname) and (h.hostname.endswith(".gov.in") or h.hostname.endswith(".nic.in"))


def cur(sid):
    s = SESS.get(sid)
    if not s: raise HTTPException(404, "Session expired. Start again.")
    ctx = s["br"].contexts[0]
    return ctx.pages[-1]


class Start(BaseModel):
    url: str
    profile: dict = {}


class XY(BaseModel):
    x: float
    y: float


class El(BaseModel):
    i: int


class Fill(BaseModel):
    values: dict


class Txt(BaseModel):
    text: str = ""
    key: str = ""


class Go(BaseModel):
    url: str


@router.post("/start")
async def start(b: Start):
    key = os.getenv("TINYFISH_API_KEY", "")
    if not key: raise HTTPException(400, "Add TINYFISH_API_KEY to .env to use assisted fill.")
    if not ok_url(b.url): raise HTTPException(400, "Only official https .gov.in / .nic.in pages are allowed.")
    if len(SESS) >= 3: raise HTTPException(429, "Too many open sessions. Close one first.")
    async with httpx.AsyncClient(timeout=90) as c:
        r = await c.post(BROWSER_URL, headers={"X-API-Key": key}, json={"url": b.url, "timeout_seconds": 900})
    if r.status_code == 404: raise HTTPException(400, "TinyFish Browser API is not enabled on your plan. Ask TinyFish support to enable it.")
    if r.status_code >= 400: raise HTTPException(502, f"TinyFish Browser API error {r.status_code}")
    s = r.json()
    from playwright.async_api import async_playwright
    pw = await async_playwright().start()
    br = await pw.chromium.connect_over_cdp(s["cdp_url"])
    ctx = br.contexts[0] if br.contexts else await br.new_context()
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    try: await page.set_viewport_size({"width": 1100, "height": 700})
    except Exception: pass
    sid = uuid.uuid4().hex[:10]
    SESS[sid] = {"pw": pw, "br": br, "profile": b.profile}
    return {"sid": sid}


@router.get("/{sid}/shot")
async def shot(sid: str):
    img = await cur(sid).screenshot(type="jpeg", quality=60)
    return Response(img, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.get("/{sid}/fields")
async def fields(sid: str):
    page = cur(sid)
    data = await page.evaluate(SCAN_JS)
    cap = None
    loc = page.locator('[data-ss-cap="1"]')
    if await loc.count():
        try: cap = base64.b64encode(await loc.first.screenshot()).decode()
        except Exception: cap = None
    for f in data["fields"]: f["suggested"] = suggest(f, SESS[sid]["profile"])
    return {**data, "captcha": cap, "url": page.url}


@router.post("/{sid}/fill")
async def fill(sid: str, b: Fill):
    page, done = cur(sid), 0
    for i, v in b.values.items():
        loc = page.locator(f'[data-ss="{int(i)}"]').first
        try:
            tag = await loc.evaluate("e=>e.tagName.toLowerCase()")
            typ = await loc.evaluate("e=>(e.type||'').toLowerCase()")
            if tag == "select":
                try: await loc.select_option(label=str(v), timeout=4000)
                except Exception: await loc.select_option(value=str(v), timeout=4000)
            elif typ in ("checkbox", "radio"):
                if v: await loc.check(timeout=4000)
            else:
                try: await loc.fill(str(v), timeout=4000)
                except Exception:
                    await loc.click(timeout=4000); await page.keyboard.type(str(v))
            done += 1
        except Exception:
            continue
    return {"filled": done}


@router.post("/{sid}/click")
async def click(sid: str, b: XY):
    page = cur(sid)
    w, h = await page.evaluate("[innerWidth, innerHeight]")
    await page.mouse.click(b.x * w, b.y * h)
    return {"ok": True}


@router.post("/{sid}/click_el")
async def click_el(sid: str, b: El):
    await cur(sid).locator(f'[data-ss="{b.i}"]').first.click(timeout=8000)
    return {"ok": True}


@router.post("/{sid}/type")
async def type_(sid: str, b: Txt):
    page = cur(sid)
    if b.key in ("Enter", "Tab", "Backspace"): await page.keyboard.press(b.key)
    if b.text: await page.keyboard.type(b.text)
    return {"ok": True}


@router.post("/{sid}/goto")
async def goto(sid: str, b: Go):
    if not ok_url(b.url): raise HTTPException(400, "Only official https .gov.in / .nic.in pages are allowed.")
    await cur(sid).goto(b.url, timeout=30000)
    return {"ok": True}


@router.post("/{sid}/close")
async def close(sid: str):
    s = SESS.pop(sid, None)
    if s:
        try: await s["br"].close()
        finally: await s["pw"].stop()
    return {"ok": True}
