import re
STATES = ["Andhra Pradesh","Assam","Bihar","Chhattisgarh","Delhi","Gujarat","Haryana","Himachal Pradesh","Jharkhand","Karnataka","Kerala","Madhya Pradesh","Maharashtra","Odisha","Punjab","Rajasthan","Tamil Nadu","Telangana","Uttar Pradesh","Uttarakhand","West Bengal"]
OCC = {"student": ["student", "college", "छात्र", "विद्यार्थी", "पढ़"], "farmer": ["farmer", "kisan", "किसान", "खेती"],
       "vendor": ["vendor", "thela", "street", "ठेला", "रेहड़ी"], "artisan": ["artisan", "carpenter", "potter", "tailor", "कारीगर", "बढ़ई"],
       "small business": ["business", "shop", "dukan", "दुकान", "व्यापार"]}


def parse_text(t):
    """Turn a free-text / voice description into profile fields (rule-based)."""
    low, out = t.lower(), {}
    m = re.search(r"(\d{1,2})\s*(?:years|yrs|yr|साल|वर्ष|year)", low) or re.search(r"age\D{0,6}(\d{1,2})", low)
    if m: out["age"] = int(m.group(1))
    m = re.search(r"(\d+(?:\.\d+)?)\s*(lakh|lakhs|lac|लाख)", low)
    if m: out["income"] = int(float(m.group(1)) * 100000)
    else:
        m = re.search(r"income\D{0,15}(\d[\d,]{4,})", low)
        if m: out["income"] = int(m.group(1).replace(",", ""))
    for k, words in OCC.items():
        if any(w in low for w in words): out["occupation"] = k; break
    for c in ["SC", "ST", "OBC"]:
        if re.search(rf"\b{c.lower()}\b", low): out["category"] = c; break
    else:
        if "general" in low: out["category"] = "General"
    if re.search(r"female|woman|girl|lady|mahila|महिला|लड़की", low): out["gender"] = "female"
    elif re.search(r"\bmale\b|\bman\b|\bboy\b|लड़का|पुरुष", low): out["gender"] = "male"
    for s in STATES:
        if s.lower() in low: out["state"] = s; break
    return out


def score(s, p):
    """Return None if clearly ineligible, else {match, reasons}."""
    why = []
    occ = s["occupations"]
    if "any" not in occ:
        if p["occupation"] not in occ: return None
        why.append("Occupation: " + p["occupation"])
    if s.get("gender"):
        if p["gender"] != s["gender"]: return None
        why.append("Gender criterion met")
    if s.get("any_of"):
        if p["category"] not in s["any_of"] and p["gender"] not in s["any_of"]: return None
        why.append("Category or gender criterion met")
    if s.get("max_income"):
        if p["income"] > s["max_income"]: return None
        why.append("Income under Rs %s" % format(s["max_income"], ","))
    if s.get("min_age"):
        if p["age"] < s["min_age"]: return None
        why.append("Age %d+" % s["min_age"])
    return {"match": min(98, 60 + 10 * len(why)), "reasons": why}


GENERIC = re.compile(r"schemes\s*/\s*programmes|directory|^home\b|search result", re.I)
BLOCKED = {"igod.gov.in"}


def pick_gov(results, known_urls=(), limit=8):
    """Keep official (.gov.in / .nic.in) search hits, de-duplicated, skipping already-known catalog sites."""
    seen, out = set(), []
    known_hosts = {re.sub(r"^https?://(www\.)?", "", u).split("/")[0] for u in known_urls}
    for r in results:
        url = r.get("url", "")
        host = re.sub(r"^https?://(www\.)?", "", url).split("/")[0]
        if not (host.endswith(".gov.in") or host.endswith(".nic.in")) or host in known_hosts or host in BLOCKED or GENERIC.search(r.get("title", "")):
            continue
        key = url.split("#")[0].rstrip("/")
        if key in seen: continue
        seen.add(key)
        out.append({"title": r.get("title") or host, "snippet": r.get("snippet", ""), "url": url})
        if len(out) >= limit: break
    return out


def unwrap_result(evt):
    """Extract the final answer from a TinyFish COMPLETE event (bare lists/text arrive under result.result)."""
    import json
    res = evt.get("result")
    if res is None: return None
    if isinstance(res, dict) and list(res.keys()) == ["result"]: res = res["result"]
    if isinstance(res, str):
        try: res = json.loads(res)
        except ValueError: res = {"summary": res}
    if isinstance(res, list): res = {"items": res}
    return res


JUNK = re.compile(r"^[a-z0-9]+(_[a-z0-9]+){3,}$")
LIST_KEYS = ["reasons", "not_eligible_because", "documents_required", "missing_docs", "how_to_apply", "fields_filled", "fields_left_blank"]


def clean_result(res):
    """Drop leaked field-name junk, trim long text, cap list sizes so the UI stays readable."""
    if not isinstance(res, dict): return res
    for k in LIST_KEYS:
        v = res.get(k)
        if isinstance(v, list):
            items = [str(x).strip() for x in v if isinstance(x, (str, int, float))]
            items = [x[:200] for x in items if x and not JUNK.match(x.replace(" ", "_").lower()) and "_" not in x.split(" ")[0][:40]]
            res[k] = items[:8]
    for k, v in list(res.items()):
        if isinstance(v, str): res[k] = v[:300]
    return res
