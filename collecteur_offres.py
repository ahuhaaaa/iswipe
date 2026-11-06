#!/usr/bin/env python3
"""Collecteur d'offres finance juniors (France) via les API publiques des plateformes de recrutement.

Principe (identique à celui de juniorjobfinance.com, déduit de ses liens) :
  1. une liste d'employeurs de la finance, chacun relié à sa plateforme (Greenhouse, Lever, SmartRecruiters, Workday) ;
  2. on lit toutes leurs offres via l'API publique de la plateforme ;
  3. on garde : France + métier finance + niveau junior ;
  4. on classe : type, domaine, date de début, durée, niveau ; on regroupe les villes d'une même offre ;
  5. on écrit offers.json, lu par l'application ISwipe.

Usage : python3 collecteur_offres.py [offers.json]      (aucune dépendance externe)
À lancer chaque jour (cron, GitHub Actions…) pour garder les offres à jour.
"""
import json, re, sys, os, base64, hashlib, smtplib, datetime as dt, urllib.request, urllib.parse
from email.message import EmailMessage
from html import unescape

# (nom, domaine du site, structure, plateforme, identifiant)
# Workday : "tenant|wdN|site" — SmartRecruiters / Greenhouse / Lever : identifiant de l'entreprise.
COMPANIES = [
 ("Point72", "point72.com", "Société de gestion", "greenhouse", "point72"),
 ("Qube Research & Technologies", "qube-rt.com", "Société de gestion", "greenhouse", "quberesearchandtechnologies"),
 ("Mirakl", "mirakl.com", "Entreprise", "greenhouse", "mirakl"),
 ("Qonto", "qonto.com", "Fintech", "lever", "qonto"),
 ("Veepee", "veepee.com", "Entreprise", "lever", "veepee"),
 ("Sia Partners", "sia-partners.com", "Conseil", "smart", "Sia"),
 ("Accor", "accor.com", "Entreprise", "smart", "AccorCorpo"),
 ("Forvis Mazars", "forvismazars.com", "Big Four", "smart", "MAZARS"),
 ("Wavestone", "wavestone.com", "Conseil", "smart", "Wavestone1"),
 ("Coface", "coface.com", "Assurance", "smart", "Coface"),
 ("Sycomore AM", "sycomore-am.com", "Société de gestion", "smart", "SycomoreAssetManagement"),
 ("Deutsche Bank", "db.com", "BFI", "workday", "db|wd3|DBWebsite"),
 ("Rothschild & Co", "rothschildandco.com", "Banque d’affaires", "workday", "rothschildandco|wd3|RothschildAndCo_Lateral"),
 ("Ardian", "ardian.com", "Fonds", "workday", "ardian|wd103|ArdianCareers"),
 ("Deloitte", "deloitte.com", "Big Four", "workday", "fina|wd103|DeloitteRecrute"),
 ("PwC", "pwc.fr", "Big Four", "workday", "pwc|wd3|Global_Campus_Careers"),
 ("ING", "ing.com", "BFI", "workday", "ing|wd3|ICSFRADIR"),
 ("Euronext", "euronext.com", "Fintech", "workday", "hrhub|wd3|Euronext_Career_Page"),
 ("Banque de France", "banque-france.fr", "Régulateur", "workday", "bdf|wd103|recrutement-banque-de-France"),
 ("Houlihan Lokey", "hl.com", "Banque d’affaires", "workday", "hl|wd1|Campus"),
 ("Air Liquide", "airliquide.com", "Entreprise", "workday", "airliquidehr|wd3|AirLiquideExternalCareer"),
]

HERE = os.path.dirname(os.path.abspath(__file__))
_x = os.path.join(HERE, "employeurs.json")  # employeurs supplémentaires : [["Nom","domaine.com","Structure","workday","tenant|wd3|site"], ...]
if os.path.exists(_x): COMPANIES += [tuple(e) for e in json.load(open(_x, encoding="utf-8"))]

def http(url, body=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
        headers={"User-Agent": "Mozilla/5.0", "Content-Type": "application/json", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.load(r)

def strip(h): return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", unescape(h or ""))).strip()

# ---------- lecteurs de plateformes ----------
def greenhouse(tok):
    for j in http(f"https://boards-api.greenhouse.io/v1/boards/{tok}/jobs?content=true")["jobs"]:
        yield dict(title=j["title"], url=j["absolute_url"], loc=(j.get("location") or {}).get("name", ""),
                   posted=(j.get("first_published") or j["updated_at"])[:10], text=strip(j.get("content")))

def lever(tok):
    for j in http(f"https://api.lever.co/v0/postings/{tok}?mode=json"):
        c = j.get("categories") or {}
        yield dict(title=j["text"], url=j["hostedUrl"], loc=c.get("location") or "", emp=c.get("commitment") or "",
                   posted=dt.date.fromtimestamp(j["createdAt"] / 1000).isoformat(), text=j.get("descriptionPlain", ""))

def smart(tok):
    off = 0
    while True:
        d = http(f"https://api.smartrecruiters.com/v1/companies/{tok}/postings?limit=100&offset={off}&country=fr")
        for j in d["content"]:
            yield dict(title=j["name"], url=f"https://jobs.smartrecruiters.com/{tok}/{j['id']}",
                       loc=(j.get("location") or {}).get("city", ""), posted=j["releasedDate"][:10],
                       emp=(j.get("typeOfEmployment") or {}).get("label", ""),
                       detail=f"https://api.smartrecruiters.com/v1/companies/{tok}/postings/{j['id']}")
        off += 100
        if off >= d.get("totalFound", 0): break

def workday(spec):
    t, wd, site = spec.split("|"); base = f"https://{t}.{wd}.myworkdayjobs.com"
    for off in range(0, 600, 20):
        ps = http(f"{base}/wday/cxs/{t}/{site}/jobs", {"appliedFacets": {}, "limit": 20, "offset": off, "searchText": ""}).get("jobPostings", [])
        if not ps: break
        for j in ps:
            n = re.search(r"(\d+)", j.get("postedOn", "")); today = dt.date.today()
            posted = today - dt.timedelta(days=int(n.group(1))) if n else today
            yield dict(title=j["title"], url=f"{base}/en-US/{site}{j['externalPath']}", loc=j.get("locationsText", ""),
                       posted=posted.isoformat(), detail=f"{base}/wday/cxs/{t}/{site}{j['externalPath']}")

FETCH = dict(greenhouse=greenhouse, lever=lever, smart=smart, workday=workday)

def describe(o):
    if o.get("text") or not o.get("detail"): return o.get("text", "")
    try:
        d = http(o["detail"])
        if "jobAd" in d: return strip(" ".join(s.get("text", "") for s in d["jobAd"]["sections"].values()))
        return strip(d["jobPostingInfo"].get("jobDescription", ""))
    except Exception: return ""

# ---------- filtres et classification ----------
FR = re.compile(r"france|paris|lyon|marseille|bordeaux|nantes|lille|toulouse|rennes|strasbourg|nice|montpellier|puteaux|défense|levallois|neuilly|issy|boulogne|nanterre|courbevoie|saint-denis|montrouge|\d+ locations?|\bFR\b", re.I)
FIN = re.compile(r"financ|comptab|accounting|audit|contr[ôo]l|tr[ée]sor|treasury|m&a|merger|acquisition|analyst|analyste|risk|risque|compliance|conformit|quant|bank|banq|asset|portfolio|portefeuille|trading|markets|march[ée]s|equity|credit|cr[ée]dit|fund|invest|actuar|consolidation|fp&a|lcb|kyc|tax|fiscal|esg|valuation|advisory", re.I)
SENIOR = re.compile(r"\b(senior|sr|manager|director|directeur|head|lead|vp|vice president|principal)\b", re.I)
JUNIOR = re.compile(r"junior|débutant|jeune diplômé|première expérience|graduate|entry.level|new grad", re.I)
FAMILIES = [("M&A", r"m&a|merger|acquisition|corporate finance|transaction services|valuation|évaluation|advisory"),
 ("Capital-investissement", r"private equity|capital-invest|venture|buyout|infrastructure invest"),
 ("Financements", r"coverage|financement|lending|\bdcm\b|debt|corporate banking|origination"),
 ("Marchés", r"trading|sales|marchés|markets|structur|derivat|rates|\bfx\b|securities|titrisation"),
 ("Gestion d’actifs", r"asset management|gestion d.actifs|portfolio|portefeuille|gérant|esg"),
 ("Risques & Conformité", r"risk|risque|compliance|conformité|lcb|kyc|\baml\b|fraud"),
 ("Audit", r"audit|contrôle interne|internal control"),
 ("Comptabilité", r"comptab|accounting|consolidation|fiscal|\btax"),
 ("Contrôle de gestion & Trésorerie", r"contrôle de gestion|fp&a|trésor|treasury|controller|business planning|financial analyst"),
 ("Data & Quant", r"quant|data|machine learning|actuar")]
MOIS = "janvier février mars avril mai juin juillet août septembre octobre novembre décembre".split()
EN = "january february march april may june july august september october november december".split()
LAB = "janv. févr. mars avr. mai juin juil. août sept. oct. nov. déc.".split()

def kind(title, emp):
    s = f"{title} {emp}".lower()
    if re.search(r"alternan|apprenti|work.?study|contrat pro", s): return "Alternance"
    if re.search(r"\bvie\b|v\.i\.e", s): return "VIE"
    if re.search(r"\bstage|stagiaire|\bintern(ship)?\b|trainee|césure", s): return "Stage"
    if re.search(r"\bcdd\b|fixed.term|temporary", s): return "CDD"
    return "CDI"

def family(title):
    for name, rx in FAMILIES:
        if re.search(rx, title, re.I): return name
    return "Autres métiers de la finance"

def start(title, text):
    rx = re.compile(r"(janvier|février|fevrier|mars|avril|mai|juin|juillet|août|aout|septembre|octobre|novembre|décembre|decembre|" + "|".join(EN) + r")\s+(20\d\d)", re.I)
    m = rx.search(title) or rx.search(text[:3000])
    if not m: return "", ""
    w = m.group(1).lower().replace("fevrier", "février").replace("aout", "août").replace("decembre", "décembre")
    i = (MOIS.index(w) if w in MOIS else EN.index(w))
    return f"{m.group(2)}-{i+1:02d}", f"{LAB[i].capitalize()} {m.group(2)}"

def duration(title, text):
    s = f"{title} {text[:3000]}"
    m = re.search(r"(\d+)\s*(?:à|-|to)\s*(\d+)\s*(?:mois|months)", s, re.I)
    if m and 1 <= int(m.group(1)) <= 36: return int(m.group(1)), f"{m.group(1)} à {m.group(2)} mois"
    m = re.search(r"(\d+)\s*(?:mois|months)", s, re.I)
    if m and 1 <= int(m.group(1)) <= 36: return int(m.group(1)), f"{m.group(1)} mois"
    return None, ""

def level(t, title, text):
    if t in ("Stage", "Alternance", "VIE"): return ""
    s = f"{title} {text[:3000]}"
    if JUNIOR.search(s): return "Junior / débutant accepté"
    ys = [int(y) for y in re.findall(r"(\d+)\s*\+?\s*(?:ans|years)", s, re.I) if int(y) < 15]
    if ys and min(ys) <= 3: return f"{min(ys)} an(s) d’expérience"
    return None  # pas junior -> écarté

def extras(title, text):
    s = f"{title} {text[:4000]}"
    m = re.search(r"(\d[\d\s.,]{2,6})\s*(?:€|euros?)\s*(?:brut|net)?\s*(?:/|par)\s*mois", s, re.I)
    return (re.sub(r"\s+", " ", m.group(1)).strip() + " €/mois") if m else "", bool(re.search(r"télétravail|remote|hybrid", s, re.I))

def notify(new):
    """Alertes Telegram / e-mail pour les nouvelles offres correspondant à alertes.json (secrets via variables d'environnement)."""
    p = os.path.join(HERE, "alertes.json"); cfg = json.load(open(p, encoding="utf-8")) if os.path.exists(p) else {}
    ok = lambda o: (not cfg.get("types") or o["t"] in cfg["types"]) and (not cfg.get("domaines") or o["f"] in cfg["domaines"]) \
        and (not cfg.get("villes") or any(l[0] in cfg["villes"] for l in o["loc"])) and (not cfg.get("mots") or any(w.lower() in o["p"].lower() for w in cfg["mots"]))
    hits = [o for o in new if ok(o)]
    if not hits: return
    txt = f"ISwipe : {len(hits)} nouvelle(s) offre(s)\n\n" + "\n".join(f"• {o['c']} — {o['p']} ({o['t']}, {o['loc'][0][0]})\n  {o['loc'][0][1]}" for o in hits[:25])
    tok, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if tok and chat:
        try: urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/sendMessage", data=urllib.parse.urlencode({"chat_id": chat, "text": txt[:4000]}).encode(), timeout=20)
        except Exception as e: print("! Telegram :", e, file=sys.stderr)
    host, to = os.getenv("SMTP_HOST"), os.getenv("MAIL_TO")
    if host and to:
        try:
            m = EmailMessage(); m["Subject"] = f"ISwipe : {len(hits)} nouvelle(s) offre(s)"; m["From"] = os.getenv("SMTP_USER", to); m["To"] = to; m.set_content(txt)
            with smtplib.SMTP(host, int(os.getenv("SMTP_PORT", "587")), timeout=20) as s:
                s.starttls(); s.login(os.getenv("SMTP_USER", ""), os.getenv("SMTP_PASS", "")); s.send_message(m)
        except Exception as e: print("! E-mail :", e, file=sys.stderr)

def logo(domain):
    """Télécharge l'icône du site (Google, puis DuckDuckGo en secours) ; signale les icônes absentes."""
    for url in (f"https://www.google.com/s2/favicons?domain={domain}&sz=128", f"https://icons.duckduckgo.com/ip3/{domain}.ico"):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                data = r.read()
            if len(data) > 700:  # en dessous : icône par défaut (globe) -> on tente la source suivante
                return "data:image/png;base64," + base64.b64encode(data).decode()
        except Exception: pass
    print(f"  ! logo introuvable pour {domain} (initiales affichées)", file=sys.stderr)
    return ""

def main(out="offers.json", prev=None):
    prev = prev or out
    groups, logos = {}, {}
    for name, dom, struct, ats, ident in COMPANIES:
        n = 0
        try:
            for o in FETCH[ats](ident):
                t = o["title"]
                if not FIN.search(t) or SENIOR.search(t) or not FR.search(o["loc"] or "France"): continue
                text = describe(o); k = kind(t, o.get("emp", "")); lv = level(k, t, text)
                if lv is None: continue
                key = (name, re.sub(r"\W+", " ", t.lower()).strip())
                city = re.split(r"[,\-–(]", o["loc"] or "France")[0].strip() or "France"
                if key in groups: groups[key]["loc"].append([city, o["url"]]); continue
                st, stl = start(t, text); d, dl = duration(t, text); g, tt = extras(t, text)
                groups[key] = dict(id=hashlib.sha1("|".join(key).encode()).hexdigest()[:10], t=k, p=t, c=name, f=family(t), s=struct,
                                   loc=[[city, o["url"]]], st=st, stl=stl, d=d, dl=dl, lv=lv, po=o["posted"], g=g, tt=tt)
                n += 1
        except Exception as e:
            print(f"! {name} ({ats}) : {e}", file=sys.stderr)
        if n: logos[name] = logo(dom)
        print(f"{name}: {n} offre(s)")
    offers = sorted(groups.values(), key=lambda o: o["po"], reverse=True)
    try: old = {o["id"] for o in json.load(open(prev, encoding="utf-8"))["offers"]}
    except Exception: old = None
    if old is not None: notify([o for o in offers if o["id"] not in old])
    json.dump(dict(generated=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), offers=offers, logos=logos, doms={c[0]: c[1] for c in COMPANIES}), open(out, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"\n{len(offers)} offres écrites dans {out}")

if __name__ == "__main__":
    main(*sys.argv[1:3])
