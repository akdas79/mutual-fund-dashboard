#!/usr/bin/env python3
"""Mutual Fund Portfolio Dashboard — upload any CAMS/KFintech CAS PDF"""

import os
import re
import tempfile
import requests
from datetime import datetime, date, timedelta
from collections import defaultdict
from flask import Flask, render_template, jsonify, request

import pdfplumber

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024  # 20 MB max upload

# ── In-memory state ───────────────────────────────────────────────────────────
_portfolio: dict | None = None   # parsed + computed portfolio
_nav_cache: dict        = {}     # mfapi scheme_code -> nav history list
_mfapi_cache: dict      = {}     # isin -> scheme_code (searched dynamically)
_yf_cache: dict         = {}     # Yahoo Finance ticker -> price history

# ── Known fund metadata (extend as needed) ────────────────────────────────────
KNOWN_META = {
    "INF209K01S38": {"cat":"Debt",   "sub":"Corporate Bond",  "er":0.35, "mfapi":119533},
    "INF846K01K35": {"cat":"Equity", "sub":"Small Cap",        "er":0.52, "mfapi":125354},
    "INF194K015G8": {"cat":"Debt",   "sub":"Banking & PSU",    "er":0.28, "mfapi":121279},
    "INF194K01Q29": {"cat":"Debt",   "sub":"Gilt",             "er":0.38, "mfapi":118464},
    "INF760K01EL8": {"cat":"ELSS",   "sub":"ELSS",             "er":0.68, "mfapi":118285},
    "INF179K01WA6": {"cat":"Hybrid", "sub":"Balanced Advantage","er":0.78,"mfapi":118968},
    "INF179K01XO5": {"cat":"Equity", "sub":"Mid Cap",          "er":0.82, "mfapi":118988},
    "INF769K01DM9": {"cat":"ELSS",   "sub":"ELSS",             "er":0.55, "mfapi":135781},
    "INF247L01445": {"cat":"Equity", "sub":"Mid Cap",          "er":0.63, "mfapi":127042},
    "INF204K01XI3": {"cat":"Equity", "sub":"Large Cap",        "er":0.72, "mfapi":118632},
    "INF879O01027": {"cat":"Equity", "sub":"Flexi Cap",        "er":0.58, "mfapi":122639},
    "INF966L01820": {"cat":"Liquid", "sub":"Liquid",           "er":0.18, "mfapi":120837},
    # common additional funds
    "INF109K01Z13": {"cat":"Equity", "sub":"Large Cap",        "er":0.50, "mfapi":120716},
    "INF740K01397": {"cat":"Equity", "sub":"Small Cap",        "er":0.42, "mfapi":125497},
    "INF917K01EG4": {"cat":"Equity", "sub":"Mid Cap",          "er":0.45, "mfapi":135800},
    "INF200K01RD8": {"cat":"ELSS",   "sub":"ELSS",             "er":0.55, "mfapi":118598},
    "INF277K01Z60": {"cat":"Liquid", "sub":"Liquid",           "er":0.15, "mfapi":119062},
}

CAT_COLOR = {
    "Equity":"#3b82f6","ELSS":"#a855f7","Debt":"#22c55e",
    "Hybrid":"#f59e0b","Liquid":"#06b6d4","Other":"#64748b",
}

HORIZON_MAP = {
    "Liquid":"emergency","Debt":"short","Hybrid":"medium",
    "ELSS":"medium","Equity":"long","Other":"long",
}

# ── Category detector (for unknown ISINs) ─────────────────────────────────────
def detect_meta(fund_name: str) -> dict:
    n = fund_name.upper()
    if any(k in n for k in ["LIQUID","OVERNIGHT","MONEY MARKET"]):
        return {"cat":"Liquid","sub":"Liquid","er":0.15}
    if any(k in n for k in ["GILT","GSEC","G-SEC","GOVERNMENT SECURITIES"]):
        return {"cat":"Debt","sub":"Gilt","er":0.35}
    if any(k in n for k in ["CORPORATE BOND","BANKING AND PSU","PSU DEBT",
                              "BANKING & PSU","CREDIT RISK","SHORT TERM",
                              "ULTRA SHORT","LOW DURATION","FLOATING RATE",
                              "DYNAMIC BOND","INCOME FUND","BOND FUND"]):
        return {"cat":"Debt","sub":"Debt","er":0.30}
    if any(k in n for k in ["ELSS","TAX SAVER","TAX SAVING","80C"]):
        return {"cat":"ELSS","sub":"ELSS","er":0.55}
    if any(k in n for k in ["BALANCED ADVANTAGE","BAF","HYBRID","EQUITY SAVINGS",
                              "ARBITRAGE","MULTI ASSET","AGGRESSIVE HYBRID"]):
        return {"cat":"Hybrid","sub":"Hybrid","er":0.65}
    if "SMALL CAP" in n:
        return {"cat":"Equity","sub":"Small Cap","er":0.55}
    if any(k in n for k in ["MID CAP","MIDCAP"]):
        return {"cat":"Equity","sub":"Mid Cap","er":0.55}
    if any(k in n for k in ["LARGE CAP","LARGECAP","LARGE AND MID","LARGE & MID"]):
        return {"cat":"Equity","sub":"Large Cap","er":0.55}
    return {"cat":"Equity","sub":"Flexi Cap","er":0.60}

def mfapi_search(fund_name: str) -> int | None:
    """Search MFAPI by fund name, preferring Direct Growth schemes."""
    query = re.sub(r'\s*[-–]\s*(Direct|Regular|Growth|IDCW|Dividend|Payout|Reinvest|Plan|Option|Demat)\b.*',
                   '', fund_name, flags=re.I).strip()[:60]
    try:
        r = requests.get(f"https://api.mfapi.in/mf/search?q={requests.utils.quote(query)}", timeout=10)
        results = r.json()
        for res in results[:15]:
            sname = res["schemeName"].upper()
            if "DIRECT" in sname and "GROWTH" in sname:
                return res["schemeCode"]
        if results:
            return results[0]["schemeCode"]
    except Exception:
        pass
    return None

def get_mfapi(isin: str, fund_name: str) -> int | None:
    if isin in _mfapi_cache:
        return _mfapi_cache[isin]
    known = KNOWN_META.get(isin, {}).get("mfapi")
    if known:
        _mfapi_cache[isin] = known
        return known
    code = mfapi_search(fund_name)
    _mfapi_cache[isin] = code
    return code

# ── Helpers ───────────────────────────────────────────────────────────────────
def to_float(s: str) -> float:
    s = str(s).strip().replace(",", "")
    if s.startswith("(") and s.endswith(")"):
        return -float(s[1:-1])
    return float(s)

def parse_date(s: str) -> date | None:
    for fmt in ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            pass
    return None

# ── XIRR ─────────────────────────────────────────────────────────────────────
def xirr(cashflows: list, dates: list) -> float | None:
    if len(cashflows) < 2:
        return None
    t0 = min(dates)

    def npv(r):
        return sum(cf / (1 + r) ** ((d - t0).days / 365.0)
                   for cf, d in zip(cashflows, dates))

    def dnpv(r):
        return sum(-((d - t0).days / 365.0) * cf / (1 + r) ** ((d - t0).days / 365.0 + 1)
                   for cf, d in zip(cashflows, dates))

    rate = 0.15
    for _ in range(300):
        f, df = npv(rate), dnpv(rate)
        if df == 0:
            break
        new_rate = rate - f / df
        if abs(new_rate - rate) < 1e-7:
            return new_rate
        rate = max(-0.9999, min(new_rate, 50.0))
    return rate

# ── CAS Parser ────────────────────────────────────────────────────────────────
NUMBER_RE          = re.compile(r"[\(]?[\d,]+\.?\d*[\)]?")
KFINTECH_ISIN_RE   = re.compile(r'\b(INF[A-Z0-9]{9})\b', re.IGNORECASE)
KFINTECH_PREFIX_RE = re.compile(r'^[A-Z0-9]{3,10}-', re.IGNORECASE)

AMC_NAMES = {
    "PPFAS Mutual Fund","Aditya Birla Sun Life Mutual Fund","Bandhan Mutual Fund",
    "HDFC Mutual Fund","MOTILAL OSWAL MUTUAL FUND","Mirae Asset Mutual Fund",
    "Quant MF","AXIS Mutual Fund","Canara Robeco Mutual Fund","Nippon India Mutual Fund",
    "SBI Mutual Fund","ICICI Prudential Mutual Fund","Kotak Mahindra Mutual Fund",
    "DSP Mutual Fund","Franklin Templeton Mutual Fund","UTI Mutual Fund",
    "Tata Mutual Fund","Invesco Mutual Fund","Edelweiss Mutual Fund",
    "Navi Mutual Fund","Groww Mutual Fund","360 ONE Mutual Fund",
    "WhiteOak Capital Mutual Fund","Bajaj Finserv Mutual Fund","Helios Mutual Fund",
    "Samco Mutual Fund","Trust Mutual Fund","JM Financial Mutual Fund",
    "Mahindra Manulife Mutual Fund","LIC Mutual Fund","PGIM India Mutual Fund",
    "Baroda BNP Paribas Mutual Fund","Union Mutual Fund","ITI Mutual Fund",
    "Quantum Mutual Fund","HSBC Mutual Fund","Shriram Mutual Fund",
}

def _extract_txn(line: str):
    m = re.match(r"^(\d{2}-[A-Za-z]{3}-\d{4})\s+", line)
    if not m:
        return None
    if "*** Stamp Duty ***" in line or "*** STT Paid ***" in line:
        return None
    nums = NUMBER_RE.findall(line[m.end():])
    if len(nums) < 4:
        return None
    try:
        amount       = to_float(nums[-4])
        unit_balance = abs(to_float(nums[-1]))   # running unit total after transaction
    except Exception:
        return None
    return (parse_date(m.group(1)), amount < 0, abs(amount), unit_balance)

CLOSING_RE = re.compile(
    r"Closing Unit Balance:\s*([\d,]+\.?\d*)\s+NAV on [^:]+:\s*INR\s*([\d,.]+)"
    r"\s+Total Cost Value:\s*([\d,]+\.?\d*)\s+Market Value on [^:]+:\s*INR\s*([\d,]+\.?\d*)"
)

def parse_kfintech(pdf_path: str, pdf_pass: str) -> list:
    """
    Parse a KFintech CAS PDF (mfs.kfintech.com).
    pdfplumber emits each table row as a single line:
      FolioNo ISIN SchemeCode-SchemeName CostValue Units NavDate NavPrice MarketValue KFINTECH
    Strategy: anchor on the NAV date (DD-MON-YYYY) to split before/after fields.
    """
    with pdfplumber.open(pdf_path, password=pdf_pass) as pdf:
        full_text = "\n".join(p.extract_text() or "" for p in pdf.pages)

    NAV_DATE_RE = re.compile(r'\b(\d{2}-[A-Z]{3}-\d{4})\b', re.IGNORECASE)
    funds = []

    for line in full_text.splitlines():
        line = line.strip()
        m_isin = KFINTECH_ISIN_RE.search(line)
        if not m_isin:
            continue

        isin       = m_isin.group(1).upper()
        after_isin = line[m_isin.end():].strip()

        # NAV date is the reliable anchor between the two halves
        m_date = NAV_DATE_RE.search(after_isin)
        if not m_date:
            continue

        before_date = after_isin[:m_date.start()].strip()   # SchemeCode-Name CostVal Units
        after_date  = after_isin[m_date.end():].strip()     # NavPrice MarketValue KFINTECH

        # All numbers in before_date — last 2 are cost_value and units
        pre_nums = [(m.start(), m.group()) for m in re.finditer(r'[\d,]+\.?\d*', before_date)]
        if len(pre_nums) < 2:
            continue

        cost_str, units_str = pre_nums[-2][1], pre_nums[-1][1]
        scheme_raw          = before_date[:pre_nums[-2][0]].strip()
        # Strip leading scheme-code prefix like "128CFDGG-"
        scheme_name = KFINTECH_PREFIX_RE.sub('', scheme_raw, count=1).strip()

        # First two numbers after date are nav_price and market_value
        post_nums = re.findall(r'[\d,]+\.?\d*', after_date)
        if len(post_nums) < 2:
            continue

        try:
            cost_val     = to_float(cost_str)
            units        = to_float(units_str)
            nav_price    = to_float(post_nums[0])
            market_value = to_float(post_nums[1])
        except Exception:
            continue

        if not scheme_name or market_value <= 0:
            continue

        meta = KNOWN_META.get(isin) or detect_meta(scheme_name)
        cat  = meta["cat"]
        funds.append({
            "name":           scheme_name,
            "short_name":     scheme_name[:40],
            "amc":            "",
            "isin":           isin,
            "category":       cat,
            "sub_category":   meta.get("sub", ""),
            "horizon":        HORIZON_MAP.get(cat, "long"),
            "er":             meta.get("er", 0.55),
            "mfapi":          meta.get("mfapi"),
            "plan":           "Direct" if "DIRECT" in scheme_name.upper() else "Regular",
            "transactions":   [],
            "units":          units,
            "nav":            nav_price,
            "cost":           cost_val,
            "market_value":   market_value,
            "first_buy":      None,
            "elss_lock_end":  None,
            "elss_is_locked": False,
        })

    return [f for f in funds if f["market_value"] > 0]

def parse_cas(pdf_path: str, pdf_pass: str) -> list:
    with pdfplumber.open(pdf_path, password=pdf_pass) as pdf:
        full_text = "\n".join(p.extract_text() or "" for p in pdf.pages)

    lines = [l.strip() for l in full_text.splitlines() if l.strip()]
    funds, current_amc, current_fund = [], None, None
    amc_upper = {a.upper() for a in AMC_NAMES}

    for line in lines:
        if line in AMC_NAMES or line.upper() in amc_upper:
            current_amc = line
            continue

        isin_m = re.search(r"ISIN:\s*(INF\w+)", line)
        if isin_m:
            isin = isin_m.group(1)
            name_m = re.match(r"^[A-Z0-9]+-(.+?)(?:\s*\(Demat\))?\s*-\s*ISIN:", line)
            scheme_name = name_m.group(1).strip() if name_m else isin
            known = KNOWN_META.get(isin)
            meta = known if known else detect_meta(scheme_name)
            cat = meta["cat"]
            current_fund = {
                "name":         scheme_name,
                "short_name":   scheme_name[:40],
                "amc":          current_amc or "",
                "isin":         isin,
                "category":     cat,
                "sub_category": meta.get("sub", ""),
                "horizon":      HORIZON_MAP.get(cat, "long"),
                "er":           meta.get("er", 0.55),
                "mfapi":        meta.get("mfapi"),   # may be None for unknown
                "plan":         "Direct" if "DIRECT" in scheme_name.upper() else "Regular",
                "transactions": [],
                "units": 0.0, "nav": 0.0, "cost": 0.0, "market_value": 0.0,
            }
            funds.append(current_fund)
            continue

        if current_fund is None:
            continue

        cl_m = CLOSING_RE.search(line)
        if cl_m:
            current_fund["units"]        = to_float(cl_m.group(1))
            current_fund["nav"]          = to_float(cl_m.group(2))
            current_fund["cost"]         = to_float(cl_m.group(3))
            current_fund["market_value"] = to_float(cl_m.group(4))
            current_fund = None
            continue

        txn = _extract_txn(line)
        if txn:
            d, is_redeem, amt, unit_bal = txn
            if d:
                current_fund["transactions"].append({
                    "date": d, "is_redemption": is_redeem,
                    "amount": amt, "unit_balance": unit_bal,
                })

    # Drop funds with zero market value (data artefacts)
    return [f for f in funds if f["market_value"] > 0]

# ── Metrics ───────────────────────────────────────────────────────────────────
def compute_metrics(funds: list) -> dict:
    today   = date.today()
    total_cost  = sum(f["cost"]         for f in funds)
    total_value = sum(f["market_value"] for f in funds)
    total_gain  = total_value - total_cost
    abs_ret     = (total_gain / total_cost * 100) if total_cost else 0

    all_cfs, all_dates = [], []

    for f in funds:
        gain  = f["market_value"] - f["cost"]
        abs_r = (gain / f["cost"] * 100) if f["cost"] else 0
        f["gain"]       = round(gain, 2)
        f["abs_return"] = round(abs_r, 2)
        f["allocation"] = round(f["market_value"] / total_value * 100, 2) if total_value else 0

        cfs, dts = [], []
        first_buy = None
        for t in f["transactions"]:
            d   = t["date"]
            amt = t["amount"]
            cf  = amt if t["is_redemption"] else -amt
            cfs.append(cf); dts.append(d)
            all_cfs.append(cf); all_dates.append(d)
            if not t["is_redemption"] and (first_buy is None or d < first_buy):
                first_buy = d

        f["first_buy"] = str(first_buy) if first_buy else None

        # ELSS lock-in: compute from first purchase date
        if f["category"] == "ELSS" and first_buy:
            lock_end = date(first_buy.year + 3, first_buy.month, first_buy.day)
            f["elss_lock_end"]  = str(lock_end)
            f["elss_is_locked"] = today < lock_end
        else:
            f["elss_lock_end"]  = None
            f["elss_is_locked"] = False

        cfs.append(f["market_value"]); dts.append(today)
        all_cfs.append(f["market_value"]); all_dates.append(today)

        f["xirr"] = None
        if len(cfs) >= 2:
            try:
                r = xirr(cfs, dts)
                f["xirr"] = round(r * 100, 2) if r is not None else None
            except Exception:
                pass

    port_xirr = None
    if len(all_cfs) >= 2:
        try:
            r = xirr(all_cfs, all_dates)
            port_xirr = round(r * 100, 2) if r is not None else None
        except Exception:
            pass

    cat_breakdown = defaultdict(lambda: {"value": 0.0, "cost": 0.0, "count": 0})
    for f in funds:
        c = f["category"]
        cat_breakdown[c]["value"] += f["market_value"]
        cat_breakdown[c]["cost"]  += f["cost"]
        cat_breakdown[c]["count"] += 1

    goal_map = defaultdict(float)
    for f in funds:
        goal_map[f["horizon"]] += f["market_value"]

    er_drag = sum(f["market_value"] * f["er"] / 100 for f in funds)

    # Health score
    eq_pct    = (cat_breakdown["Equity"]["value"] + cat_breakdown["ELSS"]["value"]) / total_value * 100 if total_value else 0
    max_alloc = max((f["allocation"] for f in funds), default=0)
    n_funds   = len(funds)
    direct_n  = sum(1 for f in funds if "Direct" in f["plan"])

    sp = {}
    if port_xirr is not None:
        sp["alpha_vs_bench"] = min(30, max(0, int(15 + (port_xirr - 12.0) * 0.8)))
    else:
        sp["alpha_vs_bench"] = 15
    sp["fund_diversification"] = 20 if 8 <= n_funds <= 15 else (14 if 5 <= n_funds <= 20 else 8)
    sp["direct_plan_usage"]    = min(20, int(direct_n / n_funds * 20)) if n_funds else 0
    sp["concentration_risk"]   = 15 if max_alloc < 25 else (10 if max_alloc < 40 else 5)
    sp["category_balance"]     = 10 if 45 <= eq_pct <= 75 else (6 if 30 <= eq_pct <= 85 else 3)

    HORIZON_LABELS = {
        "emergency": "Liquid · Emergency (0–6mo)",
        "short":     "Debt · Short Term (1–3yr)",
        "medium":    "ELSS/Hybrid · Medium Term (3–7yr)",
        "long":      "Equity · Long Term (7yr+)",
    }

    return {
        "total_cost":       round(total_cost,  2),
        "total_value":      round(total_value, 2),
        "total_gain":       round(total_gain,  2),
        "abs_return":       round(abs_ret,      2),
        "portfolio_xirr":   port_xirr,
        "num_funds":        n_funds,
        "num_amcs":         len({f["amc"] for f in funds}),
        "direct_value":     sum(f["market_value"] for f in funds if "Direct" in f["plan"]),
        "cat_breakdown":    {k: dict(v) for k, v in cat_breakdown.items()},
        "goal_map":         dict(goal_map),
        "horizon_labels":   HORIZON_LABELS,
        "expense_drag_yr":  round(er_drag, 2),
        "expense_drag_pct": round(er_drag / total_value * 100, 4) if total_value else 0,
        "health_score":     sum(sp.values()),
        "score_parts":      sp,
    }

# ── MFAPI ────────────────────────────────────────────────────────────────────
def fetch_nav_history(scheme_code: int) -> list:
    if scheme_code in _nav_cache:
        return _nav_cache[scheme_code]
    try:
        r = requests.get(f"https://api.mfapi.in/mf/{scheme_code}", timeout=15)
        if r.status_code == 200:
            data = r.json().get("data", [])
            _nav_cache[scheme_code] = data
            return data
    except Exception:
        pass
    return []

def period_return(nav_history: list, current_nav: float, days: int) -> float | None:
    target = date.today() - timedelta(days=days)
    for entry in nav_history:
        try:
            d = datetime.strptime(entry["date"], "%d-%m-%Y").date()
        except Exception:
            continue
        if d <= target:
            pnav = float(entry["nav"])
            if pnav <= 0:
                continue
            pct = (current_nav / pnav - 1) * 100
            if days > 365:
                pct = ((current_nav / pnav) ** (365.0 / days) - 1) * 100
            return round(pct, 2)
    return None

# ── Flask routes ──────────────────────────────────────────────────────────────
@app.route("/")
def index():
    resp = render_template("index.html")
    from flask import make_response
    r = make_response(resp)
    r.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return r

@app.route("/api/status")
def api_status():
    if _portfolio is None:
        return jsonify({"loaded": False})
    m = _portfolio["metrics"]
    return jsonify({
        "loaded":      True,
        "num_funds":   m["num_funds"],
        "total_value": m["total_value"],
        "pdf_name":    _portfolio.get("pdf_name", ""),
    })

@app.route("/upload", methods=["POST"])
def upload():
    global _portfolio, _nav_cache
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    f = request.files["file"]
    if not f.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Please upload a PDF file"}), 400
    password = request.form.get("password", "")

    suffix = ".pdf"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        f.save(tmp.name)
        tmp_path = tmp.name

    source = request.form.get("source", "cams").lower()
    PASS_HINTS = {
        "cams":     "No funds found. CAMS password is your PAN number in UPPERCASE (e.g. ABCDE1234F).",
        "kfintech": "No funds found. KFintech password is usually your mobile number or the custom password you set on the portal.",
    }
    WRONG_PASS_HINTS = {
        "cams":     "Wrong password. CAMS statements use your PAN number in UPPERCASE.",
        "kfintech": "Wrong password. KFintech statements use your registered mobile number or portal password.",
    }
    try:
        funds = parse_kfintech(tmp_path, password) if source == "kfintech" else parse_cas(tmp_path, password)
        if not funds:
            return jsonify({"error": PASS_HINTS.get(source, PASS_HINTS["cams"])}), 400
        metrics = compute_metrics(funds)
        _nav_cache = {}
        _portfolio = {"funds": funds, "metrics": metrics, "pdf_name": f.filename, "source": source}
        return jsonify({
            "success":     True,
            "num_funds":   len(funds),
            "total_value": metrics["total_value"],
            "investor":    funds[0]["amc"] if funds else "",
        })
    except Exception as e:
        msg = str(e)
        if "PDFPasswordIncorrect" in msg or "password" in msg.lower():
            return jsonify({"error": WRONG_PASS_HINTS.get(source, WRONG_PASS_HINTS["cams"])}), 400
        return jsonify({"error": f"Failed to parse PDF: {msg}"}), 400
    finally:
        os.unlink(tmp_path)

def _require_portfolio():
    if _portfolio is None:
        return jsonify({"error": "No portfolio loaded"}), 404
    return None

@app.route("/api/overview")
def api_overview():
    err = _require_portfolio()
    if err: return err
    m   = _portfolio["metrics"]
    cat = m["cat_breakdown"]
    return jsonify({
        "total_value":      m["total_value"],
        "total_cost":       m["total_cost"],
        "total_gain":       m["total_gain"],
        "abs_return":       m["abs_return"],
        "portfolio_xirr":   m["portfolio_xirr"],
        "num_funds":        m["num_funds"],
        "num_amcs":         m["num_amcs"],
        "direct_value":     m["direct_value"],
        "expense_drag_yr":  m["expense_drag_yr"],
        "expense_drag_pct": m["expense_drag_pct"],
        "health_score":     m["health_score"],
        "cat_breakdown": [
            {
                "category": k,
                "value":    round(v["value"], 2),
                "cost":     round(v["cost"],  2),
                "pct":      round(v["value"] / m["total_value"] * 100, 2) if m["total_value"] else 0,
                "color":    CAT_COLOR.get(k, "#64748b"),
            }
            for k, v in cat.items()
        ],
        "goal_map": [
            {
                "horizon": h,
                "label":   m["horizon_labels"].get(h, h),
                "value":   round(v, 2),
                "pct":     round(v / m["total_value"] * 100, 2) if m["total_value"] else 0,
            }
            for h, v in m["goal_map"].items()
        ],
    })

@app.route("/api/holdings")
def api_holdings():
    err = _require_portfolio()
    if err: return err
    holdings = [
        {
            "name":         f["name"],
            "short_name":   f["short_name"],
            "amc":          f["amc"],
            "isin":         f["isin"],
            "category":     f["category"],
            "sub_category": f["sub_category"],
            "units":        f["units"],
            "nav":          f["nav"],
            "cost":         f["cost"],
            "market_value": f["market_value"],
            "gain":         f["gain"],
            "abs_return":   f["abs_return"],
            "xirr":         f["xirr"],
            "allocation":   f["allocation"],
            "plan":         f["plan"],
            "color":        CAT_COLOR.get(f["category"], "#64748b"),
        }
        for f in sorted(_portfolio["funds"], key=lambda x: x["market_value"], reverse=True)
    ]
    return jsonify(holdings)

@app.route("/api/performance")
def api_performance():
    err = _require_portfolio()
    if err: return err
    result = []
    for f in _portfolio["funds"]:
        sc = f.get("mfapi") or get_mfapi(f["isin"], f["name"])
        if sc:
            f["mfapi"] = sc   # cache back
        returns = {"1M": None, "3M": None, "6M": None, "1Y": None, "3Y": None}
        if sc:
            hist = fetch_nav_history(sc)
            for period, days in [("1M",30),("3M",91),("6M",182),("1Y",365),("3Y",1095)]:
                returns[period] = period_return(hist, f["nav"], days)
        result.append({
            "name":       f["short_name"],
            "full_name":  f["name"],
            "category":   f["category"],
            "isin":       f["isin"],
            "abs_return": f["abs_return"],
            "xirr":       f["xirr"],
            "color":      CAT_COLOR.get(f["category"], "#64748b"),
            "returns":    returns,
        })
    result.sort(key=lambda x: (x["returns"].get("1Y") or -999), reverse=True)
    return jsonify(result)

@app.route("/api/tax")
def api_tax():
    err = _require_portfolio()
    if err: return err
    funds        = _portfolio["funds"]
    elss_funds   = [f for f in funds if f["category"] == "ELSS"]
    equity_funds = [f for f in funds if f["category"] in ("Equity","ELSS")]
    debt_funds   = [f for f in funds if f["category"] in ("Debt","Hybrid","Liquid")]

    elss_value = sum(f["market_value"] for f in elss_funds)
    elss_cost  = sum(f["cost"]         for f in elss_funds)
    eq_gain    = sum(max(0, f["gain"]) for f in equity_funds)
    debt_gain  = sum(max(0, f["gain"]) for f in debt_funds)
    ltcg_tax   = round(max(0, eq_gain - 125000) * 0.125, 2)
    stcg_tax   = round(debt_gain * 0.30, 2)

    elss_lock_info = [
        {
            "name":      f["short_name"],
            "inv_date":  f["first_buy"],
            "lock_end":  f["elss_lock_end"],
            "is_locked": f["elss_is_locked"],
            "value":     f["market_value"],
        }
        for f in elss_funds
    ]
    return jsonify({
        "elss_value":     round(elss_value, 2),
        "elss_cost":      round(elss_cost,  2),
        "equity_gain":    round(eq_gain,    2),
        "debt_gain":      round(debt_gain,  2),
        "ltcg_tax":       ltcg_tax,
        "stcg_tax":       stcg_tax,
        "ltcg_taxable":   round(max(0, eq_gain - 125000), 2),
        "elss_lock_info": elss_lock_info,
    })

@app.route("/api/smart_insights")
def api_smart_insights():
    err = _require_portfolio()
    if err: return err
    m, funds = _portfolio["metrics"], _portfolio["funds"]
    cat = m["cat_breakdown"]

    eq_pct = (cat.get("Equity",{}).get("value",0) + cat.get("ELSS",{}).get("value",0)) / m["total_value"] * 100 if m["total_value"] else 0
    sc_pct = sum(f["market_value"] for f in funds if "Small Cap" in f["sub_category"]) / m["total_value"] * 100 if m["total_value"] else 0
    lc_pct = sum(f["market_value"] for f in funds if "Large Cap" in f["sub_category"]) / m["total_value"] * 100 if m["total_value"] else 0

    nudges = []
    if sc_pct >= 10:
        nudges.append({"icon":"📈","text": f"Small cap allocation: {sc_pct:.1f}%. Good exposure for long-term wealth creation."})
    if lc_pct < 8 and eq_pct > 40:
        nudges.append({"icon":"⚖️","text": "Consider adding a Large Cap fund for portfolio stability."})
    if m["expense_drag_pct"] < 0.5:
        nudges.append({"icon":"💰","text": f"Great expense ratio ({m['expense_drag_pct']:.2f}% drag/yr). Direct plans are paying off."})
    elif m["expense_drag_pct"] > 1.0:
        nudges.append({"icon":"⚠️","text": f"High expense ratio ({m['expense_drag_pct']:.2f}% drag/yr). Consider switching to Direct plans."})
    if eq_pct > 80:
        nudges.append({"icon":"⚠️","text": "Heavy equity concentration. Consider adding some debt for stability."})
    if cat.get("Liquid",{}).get("value",0) < 0.03 * m["total_value"]:
        nudges.append({"icon":"🏦","text": "Emergency corpus appears low. Aim for 3-6 months of expenses in a liquid fund."})
    if not nudges:
        nudges.append({"icon":"✅","text": "Portfolio looks well-balanced. Keep investing consistently."})

    order = ["emergency","short","medium","long"]
    sorted_goal = [g for h in order for g in [{"horizon":h,
        "label":  m["horizon_labels"].get(h,h),
        "value":  round(m["goal_map"].get(h,0),2),
        "pct":    round(m["goal_map"].get(h,0)/m["total_value"]*100,2) if m["total_value"] else 0
    }] if m["goal_map"].get(h,0) > 0]

    return jsonify({
        "health_score":    m["health_score"],
        "score_parts":     m["score_parts"],
        "nudges":          nudges,
        "liquid_value":    round(cat.get("Liquid",{}).get("value",0),2),
        "liquid_pct":      round(cat.get("Liquid",{}).get("value",0)/m["total_value"]*100,2) if m["total_value"] else 0,
        "expense_drag_yr": m["expense_drag_yr"],
        "expense_drag_pct":m["expense_drag_pct"],
        "elss_value":      round(cat.get("ELSS",{}).get("value",0),2),
        "goal_map":        sorted_goal,
    })

# ── Yahoo Finance ─────────────────────────────────────────────────────────────
YF_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json",
}

BENCHMARKS = {
    "nifty50":   {"ticker": "^NSEI",              "name": "Nifty 50"},
    "sensex":    {"ticker": "^BSESN",             "name": "Sensex"},
    "midcap":    {"ticker": "NIFTYMIDCAP150.NS",  "name": "Nifty Midcap 150"},
    "midcap100": {"ticker": "^NSMIDCP",           "name": "Nifty Midcap 100"},
    "smallcap":  {"ticker": "^CNXSC",             "name": "Nifty Smallcap 100"},
    "nifty500":  {"ticker": "^CRSLDX",            "name": "Nifty 500"},
    "niftybank": {"ticker": "^NSEBANK",           "name": "Nifty Bank"},
}

# sub_category / category -> benchmark key
CAT_BENCHMARK = {
    "Large Cap":      "nifty50",
    "Flexi Cap":      "nifty500",
    "Mid Cap":        "midcap",
    "Small Cap":      "smallcap",
    "ELSS":           "nifty50",
    "Balanced Advantage": "nifty50",
    "Hybrid":         "nifty50",
    "Debt":           None,
    "Liquid":         None,
    "Corporate Bond": None,
    "Banking & PSU":  None,
    "Gilt":           None,
}

def fetch_yf(ticker: str) -> list:
    """Fetch daily close prices from Yahoo Finance. Returns [{date, close}] sorted asc."""
    if ticker in _yf_cache:
        return _yf_cache[ticker]
    try:
        # Use 10-year range
        url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
               f"?interval=1d&range=10y")
        r = requests.get(url, headers=YF_HEADERS, timeout=20)
        if r.status_code != 200:
            return []
        data   = r.json()["chart"]["result"][0]
        times  = data["timestamp"]
        closes = data["indicators"]["quote"][0]["close"]
        entries = []
        for ts, c in zip(times, closes):
            if c is not None:
                entries.append({"date": datetime.fromtimestamp(ts).date(), "close": c})
        entries.sort(key=lambda x: x["date"])
        _yf_cache[ticker] = entries
        return entries
    except Exception as e:
        print(f"YF error {ticker}: {e}")
        return []

# ── Portfolio historical performance ──────────────────────────────────────────
def _nav_lookup(nav_dict: dict, target: date) -> float | None:
    """Binary-search-style lookup: most recent NAV on or before target date."""
    best = None
    for d, v in nav_dict.items():
        if d <= target:
            if best is None or d > best[0]:
                best = (d, v)
    return best[1] if best else None

def build_portfolio_history(funds: list, start_date: date | None = None) -> list:
    """
    Returns [{date, value}] weekly portfolio NAV history.
    Uses unit_balance stored per transaction + MFAPI NAV history.
    """
    today = date.today()

    # Find earliest investment
    all_dates = [t["date"] for f in funds for t in f["transactions"] if not t["is_redemption"]]
    if not all_dates:
        return []
    earliest = min(all_dates)
    start = max(earliest, start_date) if start_date else earliest

    # Pre-load NAV histories for all funds
    fund_data = []
    for f in funds:
        sc = f.get("mfapi") or get_mfapi(f["isin"], f["name"])
        if sc:
            f["mfapi"] = sc
        hist = fetch_nav_history(sc) if sc else []
        if not hist:
            continue
        nav_dict = {}
        for entry in hist:
            d = parse_date(entry["date"])
            if d:
                nav_dict[d] = float(entry["nav"])
        sorted_txns = sorted(f["transactions"], key=lambda x: x["date"])
        fund_data.append({"nav_dict": nav_dict, "transactions": sorted_txns})

    if not fund_data:
        return []

    # Weekly date range
    dates, d = [], start
    while d <= today:
        dates.append(d)
        d += timedelta(days=7)
    if dates[-1] != today:
        dates.append(today)

    result = []
    for d in dates:
        total = 0.0
        for fd in fund_data:
            # Unit balance: last transaction on or before d
            units = 0.0
            for t in fd["transactions"]:
                if t["date"] <= d:
                    units = t["unit_balance"]
                else:
                    break
            if units <= 0:
                continue
            nav = _nav_lookup(fd["nav_dict"], d)
            if nav:
                total += units * nav
        if total > 0:
            result.append({"date": d, "value": total})

    return result

# ── Benchmark & peer comparison API ──────────────────────────────────────────
PERIOD_DAYS = {"1m": 30, "3m": 91, "6m": 182, "1y": 365, "3y": 1095, "5y": 1825, "all": None}

@app.route("/api/benchmark")
def api_benchmark():
    err = _require_portfolio()
    if err: return err

    bench_key  = request.args.get("benchmark", "nifty50")
    period     = request.args.get("period", "all").lower()
    p_days     = PERIOD_DAYS.get(period)
    start_date = (date.today() - timedelta(days=p_days)) if p_days else None

    bench_info = BENCHMARKS.get(bench_key, BENCHMARKS["nifty50"])

    # Build portfolio history
    port_hist = build_portfolio_history(_portfolio["funds"], start_date)
    if not port_hist:
        return jsonify({"error": "Not enough portfolio data"}), 400

    actual_start = port_hist[0]["date"]

    # Fetch benchmark
    yf_data = fetch_yf(bench_info["ticker"])
    bench_hist = [e for e in yf_data if e["date"] >= actual_start]

    # Index both to 100
    port_base  = port_hist[0]["value"]
    bench_base = bench_hist[0]["close"] if bench_hist else None

    port_series  = [{"date": str(p["date"]), "value": round(p["value"] / port_base * 100, 3)}
                    for p in port_hist]
    bench_series = ([{"date": str(e["date"]), "value": round(e["close"] / bench_base * 100, 3)}
                     for e in bench_hist] if bench_base else [])

    port_ret  = round(port_series[-1]["value"]  - 100, 2) if port_series  else 0
    bench_ret = round(bench_series[-1]["value"] - 100, 2) if bench_series else 0

    return jsonify({
        "portfolio":      port_series,
        "benchmark":      bench_series,
        "benchmark_name": bench_info["name"],
        "port_return":    port_ret,
        "bench_return":   bench_ret,
        "alpha":          round(port_ret - bench_ret, 2),
        "start_date":     str(actual_start),
    })

@app.route("/api/peer_comparison")
def api_peer_comparison():
    """Compare each fund's indexed NAV vs its category benchmark."""
    err = _require_portfolio()
    if err: return err

    period  = request.args.get("period", "3y").lower()
    p_days  = PERIOD_DAYS.get(period, 1095)
    start   = date.today() - timedelta(days=p_days) if p_days else None

    funds   = _portfolio["funds"]
    result  = []

    for f in funds:
        sc = f.get("mfapi") or get_mfapi(f["isin"], f["name"])
        if sc:
            f["mfapi"] = sc
        if not sc:
            continue

        hist = fetch_nav_history(sc)
        if not hist:
            continue

        # Filter and index NAV
        nav_entries = []
        for entry in hist:
            d = parse_date(entry["date"])
            if d and (start is None or d >= start):
                nav_entries.append({"date": d, "nav": float(entry["nav"])})
        nav_entries.sort(key=lambda x: x["date"])
        if len(nav_entries) < 2:
            continue

        base = nav_entries[0]["nav"]
        nav_series = [{"date": str(e["date"]), "value": round(e["nav"] / base * 100, 3)}
                      for e in nav_entries[::3]]   # every 3rd point to reduce size

        # Benchmark for this fund's category
        bench_key = CAT_BENCHMARK.get(f["sub_category"]) or CAT_BENCHMARK.get(f["category"])
        bench_series = []
        bench_name   = None
        if bench_key:
            bi = BENCHMARKS[bench_key]
            bench_name = bi["name"]
            yf_data = fetch_yf(bi["ticker"])
            bench_filtered = [e for e in yf_data if e["date"] >= nav_entries[0]["date"]]
            if bench_filtered:
                bb = bench_filtered[0]["close"]
                bench_series = [{"date": str(e["date"]), "value": round(e["close"] / bb * 100, 3)}
                                for e in bench_filtered[::3]]

        fund_ret  = round(nav_series[-1]["value"]  - 100, 2) if nav_series  else 0
        bench_ret = round(bench_series[-1]["value"] - 100, 2) if bench_series else None

        result.append({
            "isin":        f["isin"],
            "name":        f["short_name"],
            "full_name":   f["name"],
            "category":    f["category"],
            "sub":         f["sub_category"],
            "color":       CAT_COLOR.get(f["category"], "#64748b"),
            "nav_series":  nav_series,
            "bench_series":bench_series,
            "bench_name":  bench_name,
            "fund_return": fund_ret,
            "bench_return":bench_ret,
            "alpha":       round(fund_ret - bench_ret, 2) if bench_ret is not None else None,
        })

    return jsonify(result)

if __name__ == "__main__":
    print("=" * 60)
    print("  Mutual Fund Portfolio Dashboard")
    print("=" * 60)
    print("\nOpen http://localhost:5001 — upload your CAS PDF to begin")
    app.run(debug=False, port=5001, host="0.0.0.0")
