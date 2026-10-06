"""S1 line-aware closing value: residual-table builder and transformation (frozen before execution).
Development data: 2021-2024 only. 2025 rows are dropped at load and never used.

DEFINITIONS
Outcome Y (integer):  totals -> home_points + away_points ; spreads -> home_points - away_points (home margin).
Threshold L for a wager:
  totals Over / Under at total line t          -> L = t
  spreads home at home point h                  -> L = -h   (home covers iff Y > -h)
  spreads away at away point a (= -h)           -> L = -h = a ... expressed as L = -h_home
Side type: OVER-TYPE = {Over, home spread} wins iff Y > L ; UNDER-TYPE = {Under, away spread} wins iff Y < L ; push iff Y == L.
Lines must lie on the 0.5 grid (2L integer); otherwise the observation is not evaluable (counted). NaN/inf inputs are rejected.
NCAAF development close: CFBD 'lines' field (latest state; 'opening_lines' is the only other state). Each book quote = one
'over' row + one 'under' row with the same line; a book is used only if exactly that pair exists with an identical finite on-grid line.

RESIDUAL TABLES (per mechanism M1 NFL spreads, M2 NFL totals, M3 NCAAF totals)
Residual r = Y - L_close, where L_close is the development closing threshold (spreads: L_close = nflverse spread_line,
which is the home team's expected margin, so r = home margin - spread_line; totals: r = total points - total line).
Two tables per mechanism, by closing-line type: INT (L_close integer) and HALF (L_close half-integer).
f_type(x) = count(r == x) / N_type, empirical, exact; no smoothing, interpolation, fitting or continuity correction.
Cells absent from the sample have mass 0. Support = observed residual values; no extrapolation (mass 0 outside).

TRANSFORMATION for a 2025/2026 wager at bet threshold L_b, decimal price d, with Pinnacle close at threshold L_c and
Pinnacle closing no-vig probability p_c for the SAME side type at L_c (proportional normalization of both sides):
  q(y) = f_type(L_c)(y - L_c) for integer y   (outcome mass anchored at the Pinnacle close, table chosen by L_c type)
  push_c = q(L_c) if L_c integer else 0
  At L_c:   W_c = p_c*(1-push_c) ; U_c = push_c ; Lo_c = (1-p_c)*(1-push_c)
  OVER-TYPE  (win iff Y > L):  W(L_b) = W_c + sum_{y int, L_b < y <= L_c} q(y)   if L_b < L_c
                                       W_c - sum_{y int, L_c < y <= L_b} q(y)   if L_b > L_c
  UNDER-TYPE (win iff Y < L):  W(L_b) = W_c + sum_{y int, L_c <= y < L_b} q(y)   if L_b > L_c
                                       W_c - sum_{y int, L_b <= y < L_c} q(y)   if L_b < L_c
  Push(L_b) = q(L_b) if L_b integer else 0 ;  Lose(L_b) = 1 - W(L_b) - Push(L_b)
  If L_b == L_c: W = W_c, Push = U_c, Lose = Lo_c (price-only CLV).
  Safeguard: if any of W, Push, Lose < 0 or > 1 -> clip each to [0,1], renormalize to sum 1, flag 'clipped' (kept in primary, counted).
  CLV = W*(d - 1) - Lose      (push = 0). float64 throughout; no rounding except in reports (4 dp).
"""
import json, hashlib, math
from fractions import Fraction

GRID = Fraction(1, 2)
def finite(x):
    try: return x is not None and math.isfinite(float(x))
    except (TypeError, ValueError): return False
def on_grid(x): return finite(x) and (Fraction(str(float(x))) / GRID).denominator == 1
def is_int(x): return Fraction(str(x)).denominator == 1
def key(x): return str(Fraction(str(x)))
def check_table(T):
    for t in ("INT", "HALF"):
        if T.get(t): assert abs(sum(T[t].values()) - 1.0) < 1e-9, f"{t} table does not sum to 1"
    return True   # exact residual key, e.g. '-7/2', '3'

def build_table(rows):
    """rows: iterable of (Y:int, L_close:float). Returns {'INT':{key:p}, 'HALF':{key:p}, 'n':{...}, 'excluded':k}."""
    cnt = {"INT": {}, "HALF": {}}; excluded = 0
    for Y, L in rows:
        if Y is None or not on_grid(L): excluded += 1; continue
        t = "INT" if is_int(L) else "HALF"; k = key(Fraction(int(Y)) - Fraction(str(L)))
        cnt[t][k] = cnt[t].get(k, 0) + 1
    out = {"n": {t: sum(c.values()) for t, c in cnt.items()}, "excluded": excluded}
    for t, c in cnt.items():
        n = sum(c.values()); out[t] = {k: v / n for k, v in sorted(c.items(), key=lambda kv: Fraction(kv[0]))} if n else {}
    return out

def q_factory(table, L_c):
    t = table["INT" if is_int(L_c) else "HALF"]; Lc = Fraction(str(L_c))
    return lambda y: t.get(key(Fraction(y) - Lc), 0.0)

def transform(table, side_type, L_b, d, L_c, p_c):
    """side_type: 'OVER' (Over / home spread) or 'UNDER' (Under / away spread). Returns dict(W,P,L,CLV,clipped) or None."""
    if not (finite(d) and finite(p_c) and on_grid(L_b) and on_grid(L_c)): return None   # rejects NaN/inf
    if not (0.0 <= p_c <= 1.0) or d <= 1.0: return None
    q = q_factory(table, L_c); Lb, Lc = Fraction(str(L_b)), Fraction(str(L_c))
    push_c = q(Lc) if is_int(Lc) else 0.0
    W = p_c * (1 - push_c)
    lo, hi = (min(Lb, Lc), max(Lb, Lc))
    ints = range(math.floor(lo), math.ceil(hi) + 1)
    if side_type == "OVER":
        if Lb < Lc: W += sum(q(y) for y in ints if Lb < y <= Lc)
        elif Lb > Lc: W -= sum(q(y) for y in ints if Lc < y <= Lb)
    elif side_type == "UNDER":
        if Lb > Lc: W += sum(q(y) for y in ints if Lc <= y < Lb)
        elif Lb < Lc: W -= sum(q(y) for y in ints if Lb <= y < Lc)
    else: raise ValueError(side_type)
    P = (q(Lb) if is_int(Lb) else 0.0) if Lb != Lc else push_c
    Lo = 1.0 - W - P; clipped = False
    if min(W, P, Lo) < 0 or max(W, P, Lo) > 1:
        W, P, Lo = (min(max(v, 0.0), 1.0) for v in (W, P, Lo)); s = W + P + Lo
        W, P, Lo = W / s, P / s, Lo / s; clipped = True
    return dict(W=W, P=P, L=Lo, CLV=W * (d - 1) - Lo, clipped=clipped)

# ---------------- development data loaders (2021-2024 only; run only after freeze) ----------------
NFL_URL = "https://github.com/nflverse/nfldata/raw/master/data/games.csv"
CFB_LINES_URL = "https://raw.githubusercontent.com/sportsdataverse/cfbfastR-data/main/betting/csv/cfb_line_odds.csv.gz"
CFB_SCHED = "https://raw.githubusercontent.com/sportsdataverse/cfbfastR-data/main/schedules/csv/cfb_schedules_{y}.csv"
CFB_BOOK_PRIORITY = ["Bovada", "DraftKings", "William Hill (New Jersey)", "ESPN Bet", "Caesars Sportsbook (Colorado)", "Caesars (Pennsylvania)"]
# aggregators excluded: consensus, teamrankings, numberfire

def load_nfl():
    import pandas as pd
    g = pd.read_csv(NFL_URL, usecols=["season", "home_score", "away_score", "spread_line", "total_line"])
    g = g[g.season.between(2021, 2024) & g.home_score.notna() & g.away_score.notna()]
    spreads = [(int(r.home_score - r.away_score), float(r.spread_line)) for r in g.itertuples()]
    totals = [(int(r.home_score + r.away_score), float(r.total_line)) for r in g.itertuples()]
    return spreads, totals

def load_cfb():
    import pandas as pd
    L = pd.read_csv(CFB_LINES_URL, usecols=["id", "game_id", "season", "market_type", "abbr", "lines", "book"])
    L = L[L.season.between(2021, 2024) & (L.market_type == "total") & L.book.isin(CFB_BOOK_PRIORITY)]
    L["lines"] = pd.to_numeric(L["lines"], errors="coerce")
    S = pd.concat([pd.read_csv(CFB_SCHED.format(y=y), usecols=["game_id", "home_division", "away_division", "home_points", "away_points"]) for y in range(2021, 2025)])
    S = S[((S.home_division == "fbs") | (S.away_division == "fbs")) & S.home_points.notna() & S.away_points.notna()].set_index("game_id")
    # Closing total per book = the 'lines' field (latest state; 'opening_lines' is the only other state in the file).
    # Each book quote appears as exactly two rows (abbr 'over' and 'under') carrying the same line; no row ordering is used.
    # A book is usable only if it has exactly one 'over' and one 'under' row with identical, finite, on-grid lines.
    rows, counts = [], {"no_usable_book": 0, "book_rejected_shape_or_mismatch": 0, "not_fbs_or_no_score": 0}
    for gid, grp in L.groupby("game_id"):
        if gid not in S.index: counts["not_fbs_or_no_score"] += 1; continue
        close = None
        for b in CFB_BOOK_PRIORITY:
            g = grp[grp.book == b]
            if not len(g): continue
            ab = sorted(g.abbr.astype(str))
            vals = set(float(v) for v in g["lines"] if finite(v))
            if ab == ["over", "under"] and len(vals) == 1 and on_grid(next(iter(vals))):
                close = next(iter(vals)); break
            counts["book_rejected_shape_or_mismatch"] += 1
        if close is None: counts["no_usable_book"] += 1; continue
        s = S.loc[gid]; rows.append((int(s.home_points + s.away_points), close))
    return rows, counts

if __name__ == "__main__":
    import sys
    if sys.argv[1:] == ["build"]:
        sp, to = load_nfl(); cf, cfb_counts = load_cfb()
        T = {"M1_NFL_spreads": build_table(sp), "M2_NFL_totals": build_table(to), "M3_NCAAF_totals": dict(build_table(cf), exclusion_counts=cfb_counts)}
        for v in T.values(): check_table(v)
        s = json.dumps(T, sort_keys=True); open("s1_residual_tables.json", "w").write(s)
        print("tables sha256", hashlib.sha256(s.encode()).hexdigest(), {k: v["n"] for k, v in T.items()})
