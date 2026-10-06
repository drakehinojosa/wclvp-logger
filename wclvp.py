"""W-CLV-P v1.0-R1 production code. Prospective closing-line value of v1.1 wind tickets vs the Pinnacle close.
Reads v1.1 log/entries.jsonl read-only; never reads v1.1 grades or any score source. See frozen/SPEC.md."""
import os, sys, json, math, hashlib, collections, datetime as dt, zoneinfo, importlib.util, requests
ROOT = os.path.dirname(os.path.abspath(__file__)); ET = zoneinfo.ZoneInfo("America/New_York")
P = lambda s: dt.datetime.fromisoformat(s.replace("Z", "+00:00")); F = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")
WCLVP_START = P("2026-10-15T00:00:00Z")
SEASON_END_CUTOFF = P("2029-03-01T00:00:00Z")  # implements 'end of the 2028 season' (after all 2028 regular-season and postseason games)
TRANSFORM_SHA = "806f9decf7825e202af33577f25478cb9586af269635ef37b12aa28b58427d4d"
TABLES_SHA = "eeff6a1a8625dc867c955ae9f53678a46d04a0021fe70ae3c89ee26f8edacc43"
V11_RAW = "https://raw.githubusercontent.com/drakehinojosa/Wind---logger-/main"
V11_API = "https://api.github.com/repos/drakehinojosa/Wind---logger-"
SPORT = {"NFL": "americanfootball_nfl", "CFB": "americanfootball_ncaaf"}
TABLE = {"NFL": "M2_NFL_totals", "CFB": "M3_NCAAF_totals"}
W = dt.timedelta(minutes=15); FRESH = dt.timedelta(minutes=30); JIT = dt.timedelta(minutes=5)
LOOKS = [40, 80, 120]

def sha(path): return hashlib.sha256(open(path, "rb").read()).hexdigest()

def load_frozen():
    t = os.path.join(ROOT, "frozen", "s1_transform.py"); b = os.path.join(ROOT, "frozen", "s1_residual_tables.json")
    assert sha(t) == TRANSFORM_SHA, "transform hash mismatch - halt"
    assert sha(b) == TABLES_SHA, "residual table hash mismatch - halt"
    spec = importlib.util.spec_from_file_location("s1_transform", t); TR = importlib.util.module_from_spec(spec); spec.loader.exec_module(TR)
    return TR, json.load(open(b))

# ---------- append-only hash-chained logs ----------
def chain_append(rel, rec, now):
    p = os.path.join(ROOT, rel); prev = "GENESIS"
    if os.path.exists(p):
        lines = [l for l in open(p).read().split("\n") if l]
        if lines: prev = hashlib.sha256(lines[-1].encode()).hexdigest()
    rec = dict(rec, prev_hash=prev, logged_at=F(now))
    with open(p, "a") as f: f.write(json.dumps(rec, sort_keys=True) + "\n")

def chain_verify_lines(lines):
    prev = "GENESIS"
    for i, l in enumerate(lines):
        if json.loads(l)["prev_hash"] != prev: return False, i
        prev = hashlib.sha256(l.encode()).hexdigest()
    return True, len(lines)

def read_log(rel):
    p = os.path.join(ROOT, rel)
    if not os.path.exists(p): return []
    lines = [l for l in open(p).read().split("\n") if l]
    ok, i = chain_verify_lines(lines); assert ok, f"{rel} chain broken at {i} - halt"
    return [json.loads(l) for l in lines]

# ---------- v1.1 inputs (read-only) ----------
def fetch_v11_entries(get):
    lines = [l for l in get(f"{V11_RAW}/log/entries.jsonl").split("\n") if l]
    ok, i = chain_verify_lines(lines); assert ok, f"v1.1 entries chain broken at {i} - halt"
    return [(l, json.loads(l)) for l in lines]

def v11_stopped_at(lg, get_json, exists=None):
    """UTC time at which v1.1 first committed looks/<LG>_STOPPED, or None.
    Existence is checked first via the raw file URL (not API rate-limited); the commits API is queried only if the file exists."""
    if exists is not None and not exists(f"{V11_RAW}/looks/{lg}_STOPPED"): return None
    c = get_json(f"{V11_API}/commits", dict(path=f"looks/{lg}_STOPPED", per_page=100))
    return min(P(x["commit"]["committer"]["date"]) for x in c) if c else None

def is_candidate(e):
    return e.get("status") == "LOGGED" and e.get("qualified") is True

def admitted(entries, stopped):
    """Admission (irrevocable): LOGGED+qualified, K-15m >= WCLVP_START, K < season cutoff,
    and written before that league's v1.1 STOPPED commit (if any)."""
    out = []
    for line, e in entries:
        if not is_candidate(e): continue
        K = P(e["K"])
        if K - W < WCLVP_START or K >= SEASON_END_CUTOFF: continue
        s = stopped.get(e["lg"])
        if s is not None and P(e["logged_at"]) >= s: continue
        out.append(dict(event_id=e["event_id"], lg=e["lg"], K=e["K"], entry_sha=hashlib.sha256(line.encode()).hexdigest(),
                        L_b=float(e["ticket"]["total"]), under_price=e["ticket"]["under_price"], book=e["ticket"]["book"]))
    return out

def pre_wclvp_count(entries):
    return sum(1 for _, e in entries if is_candidate(e) and P(e["K"]) - W < WCLVP_START)

def dec(a):
    a = float(a); return 1 + a / 100 if a > 0 else 1 + 100 / abs(a)

def wclvp_stopped(lg): return os.path.exists(os.path.join(ROOT, "looks", f"{lg}_STOPPED"))

# ---------- capture (one sport-level call per run while any admitted window is open) ----------
def capture(now, adm, done, call):
    for lg in ("NFL", "CFB"):
        if wclvp_stopped(lg): continue
        open_ids = {a["event_id"] for a in adm if a["lg"] == lg and a["event_id"] not in done and P(a["K"]) - W <= now <= P(a["K"])}
        if not open_ids: continue
        resp, err = None, None
        for _ in range(3):
            try: resp = call(SPORT[lg]); break
            except Exception as ex: err = type(ex).__name__
        rec = dict(lg=lg, capture_time=F(resp["received_at"] if resp else now), ok=resp is not None, error=err, events={})
        if resp:
            for ev in resp["data"]:
                if ev["id"] not in open_ids: continue
                pin = [m for b in ev.get("bookmakers", []) if b["key"] == "pinnacle" for m in b["markets"] if m["key"] == "totals"]
                rec["events"][ev["id"]] = dict(commence=ev["commence_time"], totals=pin[0] if pin else None)
        chain_append("log/captures.jsonl", rec, now)

def evaluate_close(a, caps, TR, tables):
    K = P(a["K"]); win = [c for c in caps if c["lg"] == a["lg"] and K - W <= P(c["capture_time"]) <= K]
    listing_problem = False
    for c in sorted(win, key=lambda c: c["capture_time"], reverse=True):   # latest valid capture in window is authoritative
        if not c["ok"]: continue
        ev = c["events"].get(a["event_id"])
        if ev is None or abs(P(ev["commence"]) - K) > JIT: listing_problem = True; continue
        m = ev["totals"]
        if not m: continue
        ct = P(c["capture_time"]); lu = P(m.get("last_update", "1970-01-01T00:00:00Z"))
        o = {x["name"]: x for x in m["outcomes"]}
        if not (ct - FRESH <= lu <= ct) or set(o) != {"Over", "Under"} or o["Over"].get("point") != o["Under"].get("point"): continue
        dU, dO = dec(o["Under"]["price"]), dec(o["Over"]["price"]); p_c = (1 / dU) / (1 / dU + 1 / dO)
        L_c = float(o["Under"]["point"]); d = dec(a["under_price"])
        res = TR.transform(tables[TABLE[a["lg"]]], "UNDER", a["L_b"], d, L_c, p_c)
        base = dict(event_id=a["event_id"], lg=a["lg"], K=a["K"], entry_sha=a["entry_sha"], book=a["book"], L_b=a["L_b"], d=d,
                    capture_time=c["capture_time"], L_c=L_c, pin_under=o["Under"]["price"], pin_over=o["Over"]["price"], p_c=p_c)
        if res is None: return dict(base, status="INVALID-TRANSFORM")
        return dict(base, status="EVALUABLE", W=res["W"], P=res["P"], L=res["L"], clipped=res["clipped"], CLV=res["CLV"])
    return dict(event_id=a["event_id"], lg=a["lg"], K=a["K"], entry_sha=a["entry_sha"],
                status="X4-CLOSE" if listing_problem else "ERROR-NO-CLOSE")

def finalize(now, adm, caps, TR, tables):
    done = {r["event_id"] for r in read_log("log/clv.jsonl")}
    for a in sorted(adm, key=lambda a: (a["K"], a["event_id"])):
        if a["event_id"] in done or now <= P(a["K"]): continue
        chain_append("log/clv.jsonl", evaluate_close(a, caps, TR, tables), now)

# ---------- looks (with look-finalization watermark) ----------
def z_of(v, cl):
    n = len(v); mu = sum(v) / n; g = collections.defaultdict(float)
    for x, c in zip(v, cl): g[c] += x - mu
    G = len(g)
    if G < 2: return mu, None, None, G
    se = math.sqrt(G / (G - 1) * sum(s * s for s in g.values())) / n
    if se == 0: return mu, 0.0, (math.inf if mu > 0 else -math.inf if mu < 0 else 0.0), G
    return mu, se, mu / se, G

def looks(adm, now):
    clv = read_log("log/clv.jsonl"); term = {r["event_id"] for r in clv}
    for lg in ("NFL", "CFB"):
        if wclvp_stopped(lg): continue
        ev = sorted([r for r in clv if r["lg"] == lg and r["status"] == "EVALUABLE"], key=lambda r: (r["K"], r["event_id"]))
        for N in LOOKS:
            fn = os.path.join(ROOT, "looks", f"{lg}_{N}.json")
            if os.path.exists(fn): continue
            if len(ev) < N: break
            KN = ev[N - 1]["K"]
            if any(a["lg"] == lg and a["K"] <= KN and a["event_id"] not in term for a in adm): break   # watermark not reached
            mem = ev[:N]
            mu, se, z, G = z_of([r["CLV"] for r in mem], [P(r["K"]).astimezone(ET).date().isoformat() for r in mem])
            d = "CONTINUE"
            if z is not None and z >= 2.24: d = "CLV GATE PASSED"
            elif N == 40 and z is not None and z <= -1.28: d = "STOP: CLV NOT SUPPORTED"
            elif N in (80, 120) and mu <= 0: d = "STOP: CLV NOT SUPPORTED"
            elif N == 120: d = "STOP: CLV NOT DEMONSTRATED"
            acct = collections.Counter(r["status"] for r in clv if r["lg"] == lg and r["K"] <= KN)
            out = dict(league=lg, look=N, members=[r["event_id"] for r in mem], K_of_Nth=KN, mean_clv=mu, se_cr1=se,
                       z=(str(z) if z in (math.inf, -math.inf) else z), clusters=G, clipped=sum(bool(r["clipped"]) for r in mem),
                       accounting_through_K_of_Nth=dict(acct), decision=d, computed_at=F(now))
            json.dump(out, open(fn, "w"), indent=1, sort_keys=True)
            if d != "CONTINUE":
                open(os.path.join(ROOT, "looks", f"{lg}_STOPPED"), "w").write(
                    f"{d} at look {N}. Freeze and return for independent audit. No betting authorized.\n")
                break

# ---------- run ----------
def run(now, get, get_json, call, exists=None):
    TR, tables = load_frozen()
    entries = fetch_v11_entries(get)
    stopped = {lg: v11_stopped_at(lg, get_json, exists) for lg in ("NFL", "CFB")}
    adm = admitted(entries, stopped)
    done = {r["event_id"] for r in read_log("log/clv.jsonl")}
    capture(now, adm, done, call)
    finalize(now, adm, read_log("log/captures.jsonl"), TR, tables)
    looks(adm, now)
    json.dump(dict(last_run=F(now), admitted=len(adm), pre_wclvp=pre_wclvp_count(entries)),
              open(os.path.join(ROOT, "state", "status.json"), "w"), indent=1)

def live_io():
    key = os.environ["ODDS_API_KEY"]; tok = os.environ.get("GITHUB_TOKEN")
    H = {"Authorization": f"Bearer {tok}"} if tok else {}
    def get(u):
        r = requests.get(u, timeout=60); r.raise_for_status(); return r.text
    def get_json(u, params):
        r = requests.get(u, params=params, headers=H, timeout=60); r.raise_for_status(); return r.json()
    def call(sport):
        r = requests.get(f"https://api.the-odds-api.com/v4/sports/{sport}/odds",
                         params=dict(apiKey=key, regions="eu", markets="totals", bookmakers="pinnacle", oddsFormat="american"), timeout=60)
        r.raise_for_status()
        return dict(received_at=dt.datetime.now(dt.timezone.utc).replace(microsecond=0), data=r.json())
    def exists(u):
        r = requests.get(u, timeout=60)
        if r.status_code == 404: return False
        r.raise_for_status(); return True
    return get, get_json, call, exists

if __name__ == "__main__":
    run(dt.datetime.now(dt.timezone.utc).replace(microsecond=0), *live_io())
