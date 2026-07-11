# -*- coding: utf-8 -*-
"""
SK Hynix Analyst Radar
======================
증권사 투자의견/목표주가 상향·하향 추세 트래킹 시스템.

데이터 소스 : 한경 컨센서스 (consensus.hankyung.com) - 기업 리포트 리스트
산출물      : (1) 텔레그램 정기 브리핑  (2) dashboard.html  (3) state.json

인증정보는 환경변수(GitHub Actions Secrets) -> config.json 폴백 순으로 로드.
의존성 최소화 원칙: requests 만 사용 (표준 라이브러리 외).
"""
import os
import re
import sys
import json
import time
import html as htmllib
import statistics
from datetime import datetime, date, timedelta
from collections import defaultdict, Counter

import requests

# ------------------------------------------------------------------ config ---
STOCK_NAME = "SK하이닉스"
STOCK_CODE = "000660"
LOOKBACK_DAYS = 730          # 수집 대상 기간(일)
MOMENTUM_SHORT = 30          # 단기 모멘텀 창(일)
MOMENTUM_LONG = 90           # 중기 모멘텀 창(일)
ACTIVE_DAYS = 180            # 유효 커버리지 기준(일) - 이보다 오래된 리포트는 휴면 처리
BASE_URL = "http://consensus.hankyung.com/analysis/list"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0 Safari/537.36"),
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
    "Referer": "http://consensus.hankyung.com/",
}
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "state.json")
DASHBOARD_PATH = os.path.join(HERE, "dashboard.html")
SUMMARY_PATH = os.path.join(HERE, "summaries.json")   # report_idx -> 정형 팩트 캐시
ENRICH = os.environ.get("ENRICH", "1") != "0"         # 원문 PDF 수치 추출 on/off
MAX_ENRICH = int(os.environ.get("MAX_ENRICH", "120")) # 실행당 신규 PDF 최대 조회 수
PDF_TIMEOUT = 20
# GitHub Pages 대시보드 URL (텔레그램 브리핑 링크). 배포 계정/레포에 맞게 환경변수로 재정의 가능
DASHBOARD_URL = os.environ.get(
    "DASHBOARD_URL", "https://jinhae8971.github.io/skhynix-analyst-radar/")

# 추적 종목 (한경=국내 증권사 리포트 / yahoo=미국 애널리스트 컨센서스)
SYMBOLS = [
    {"key": "sk", "name": "SK하이닉스", "code": "000660",
     "source": "hankyung", "currency": "KRW", "src_label": "한경 컨센서스"},
    {"key": "ss", "name": "삼성전자", "code": "005930",
     "source": "hankyung", "currency": "KRW", "src_label": "한경 컨센서스"},
    {"key": "mu", "name": "마이크론", "ticker": "MU", "code": "MU",
     "source": "yahoo", "currency": "USD", "src_label": "Yahoo Finance · 美 애널리스트"},
]


def _money(v, currency="KRW"):
    if not v:
        return "-"
    if currency == "USD":
        return f"${v:,.0f}"
    return f"₩{round(v/10000):,}만"


def load_config():
    cfg = {
        "telegram_token":   os.environ.get("TELEGRAM_TOKEN", ""),
        "telegram_chat_id": os.environ.get("TELEGRAM_CHAT_ID", ""),
    }
    p = os.path.join(HERE, "config.json")
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8") as f:
            for k, v in json.load(f).items():
                if not cfg.get(k):
                    cfg[k] = v
    return cfg


# ----------------------------------------------------------------- scraping ---
def _get(params, retries=3):
    last = None
    for i in range(retries):
        try:
            r = requests.get(BASE_URL, params=params, headers=HEADERS, timeout=25)
            r.raise_for_status()
            return r.text
        except Exception as e:                       # noqa
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"fetch failed: {last}")


def _txt(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s)).strip()


def normalize_opinion(raw):
    """증권사별 상이한 표기를 BUY / HOLD / SELL / N/A 로 표준화."""
    s = (raw or "").strip().lower()
    if (not s) or s in ("-", "n/a", "na", "투자의견없음", "없음", "0"):
        return "N/A"
    if any(k in s for k in ("buy", "매수", "outperform", "overweight",
                            "비중확대", "적극", "강력")):
        return "BUY"
    if any(k in s for k in ("hold", "중립", "neutral", "marketperform",
                            "보유", "market perform")):
        return "HOLD"
    if any(k in s for k in ("sell", "매도", "underperform", "underweight",
                            "비중축소", "축소", "reduce")):
        return "SELL"
    return "N/A"


def parse_price(raw):
    d = re.sub(r"[^\d]", "", raw or "")
    if not d:
        return None
    v = int(d)
    return v if v > 0 else None


def fetch_reports(name, code, lookback_days=LOOKBACK_DAYS,
                  page_size=100, max_pages=6):
    """한경 컨센서스에서 종목 리포트 전체 수집. (pagenum=페이지크기, now_page=페이지번호)"""
    edate = date.today()
    sdate = edate - timedelta(days=lookback_days)
    records, seen_first = [], None
    for page in range(1, max_pages + 1):
        params = {
            "skinType": "business",
            "sdate": sdate.strftime("%Y-%m-%d"),
            "edate": edate.strftime("%Y-%m-%d"),
            "search_value": "",
            "search_text": name,
            "pagenum": str(page_size),
            "now_page": str(page),
            "order_type": "",
        }
        html = _get(params)
        m = re.search(r"<tbody>(.*?)</tbody>", html, re.S)
        if not m:
            break
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", m.group(1), re.S)
        page_recs = []
        for row in rows:
            cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
            if len(cells) < 6:
                continue
            # 종목코드로 1차 필터: 제목에 해당 코드가 없으면 타 종목 리포트 → 제외
            if code not in cells[1]:
                continue
            anchor = re.search(r"<a[^>]*>(.*?)</a>", cells[1], re.S)   # 앵커 텍스트만 = 중복 제거
            title = _txt(anchor.group(1)) if anchor else _txt(cells[1])
            idx = re.search(r"report_idx=(\d+)", cells[1])
            page_recs.append({
                "date": _txt(cells[0]),
                "title": title[:60],
                "target": parse_price(_txt(cells[2])),
                "opinion_raw": _txt(cells[3]),
                "opinion": normalize_opinion(_txt(cells[3])),
                "author": _txt(cells[4]),
                "source": _txt(cells[5]),
                "report_idx": idx.group(1) if idx else "",
            })
        if not page_recs:
            break
        first_key = (page_recs[0]["date"], page_recs[0]["report_idx"])
        if first_key == seen_first:        # 마지막 페이지 반복 방지
            break
        seen_first = first_key
        records.extend(page_recs)
        if len(page_recs) < page_size:
            break

    # 중복 제거 (동일 date/source/author/target/opinion)
    uniq, keyset = [], set()
    for r in records:
        k = (r["date"], r["source"], r["author"], r["target"], r["opinion"])
        if k in keyset:
            continue
        keyset.add(k)
        uniq.append(r)
    uniq.sort(key=lambda x: x["date"])     # 오름차순(과거->현재)
    return uniq


_US_BUY = ("buy", "strong buy", "outperform", "overweight", "positive",
           "accumulate", "add", "market outperform", "sector outperform",
           "conviction buy", "top pick", "long")
_US_HOLD = ("hold", "neutral", "equal-weight", "equalweight", "equal weight",
            "market perform", "sector perform", "in-line", "inline", "perform",
            "sector weight", "peer perform")
_US_SELL = ("sell", "underperform", "underweight", "reduce", "negative",
            "market underperform", "sector underperform")


def _normalize_us(grade):
    g = (grade or "").strip().lower()
    if not g:
        return "N/A"
    if any(g == b or g.startswith(b) for b in _US_BUY):
        return "BUY"
    if any(g == h or g.startswith(h) for h in _US_HOLD):
        return "HOLD"
    if any(g == s or g.startswith(s) for s in _US_SELL):
        return "SELL"
    return "N/A"


def fetch_yahoo_reports(ticker, lookback_days=LOOKBACK_DAYS):
    """Yahoo Finance 애널리스트 등급/목표가 변경 이력을 한경과 동일한 리포트 스키마로 변환.
    각 행 = 특정 리서치사(firm)의 등급/목표가 조정 이벤트."""
    try:
        import yfinance as yf
    except Exception as e:
        print(f"[yahoo] yfinance 미설치: {e}")
        return []
    try:
        tk = yf.Ticker(ticker)
        df = tk.upgrades_downgrades
    except Exception as e:
        print(f"[yahoo] {ticker} 이력 조회 실패: {e}")
        return []
    if df is None or len(df) == 0:
        return []
    df = df.reset_index()
    cutoff = (date.today() - timedelta(days=lookback_days))
    recs = []
    pt_dir = {"up": "상향", "down": "하향"}
    for _, row in df.iterrows():
        gd = row.get("GradeDate")
        try:
            d = gd.date() if hasattr(gd, "date") else datetime.fromisoformat(str(gd)).date()
        except Exception:
            continue
        if d < cutoff:
            continue
        firm = str(row.get("Firm") or "").strip()
        if not firm:
            continue
        to_g = str(row.get("ToGrade") or "").strip()
        fr_g = str(row.get("FromGrade") or "").strip()
        try:
            pt = float(row.get("currentPriceTarget") or 0)
        except (TypeError, ValueError):
            pt = 0
        pta = str(row.get("priceTargetAction") or "").strip().lower()
        # 리포트 논지(제목) : 등급 변경 우선, 없으면 목표가 액션
        if fr_g and to_g and fr_g != to_g:
            title = f"{fr_g} → {to_g}"
        elif "raise" in pta or pta == "up":
            title = "목표가 상향"
        elif "lower" in pta or pta == "down":
            title = "목표가 하향"
        elif "init" in pta or "announce" in pta:
            title = f"신규 커버리지 · {to_g}" if to_g else "신규 커버리지"
        else:
            title = to_g or "의견 유지"
        recs.append({
            "date": d.strftime("%Y-%m-%d"),
            "title": title[:60],
            "target": int(round(pt)) if pt and pt > 0 else None,
            "opinion_raw": to_g or "-",
            "opinion": _normalize_us(to_g),
            "author": "",
            "source": firm,
            "report_idx": "",
            "current_price": None,
            "upside": None,
            "synopsis": None,
        })
    # 중복 제거(동일 date/firm/target/opinion) 후 과거→현재
    uniq, seen = [], set()
    for r in sorted(recs, key=lambda x: x["date"]):
        k = (r["date"], r["source"], r["target"], r["opinion"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(r)
    return uniq


# ------------------------------------------------------------ enrichment ---
def _extract_pdf_facts(content):
    """원문 PDF 첫 페이지에서 정형 수치(현재주가·목표주가)만 추출.
    저작권 보호를 위해 본문 문장/문단은 저장하지 않고 숫자 팩트만 반환."""
    try:
        import io
        import pdfplumber
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            txt = pdf.pages[0].extract_text() or ""
    except Exception:
        return {}
    facts = {}
    # 쉼표 구분 숫자만 포착 -> (7/24)·(2024.07.25) 같은 날짜 괄호에 오작동하지 않음
    mc = re.search(r"(?:현재\s*주가|현재가|종가)[^\n]{0,20}?(\d{1,3}(?:,\d{3})+)", txt)
    if mc:
        try:
            facts["current_price"] = int(mc.group(1).replace(",", ""))
        except ValueError:
            pass
    mt = re.search(r"목표주가[^\n]{0,15}?(\d{1,3}(?:,\d{3})+)", txt)
    if mt:
        try:
            facts["target_pdf"] = int(mt.group(1).replace(",", ""))
        except ValueError:
            pass
    return facts


def _pdf_first_page_text(content):
    """PDF 1페이지 원문 텍스트를 반환(요약 입력용, 저장하지 않음)."""
    try:
        import io
        import pdfplumber
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            return (pdf.pages[0].extract_text() or "")[:1800]
    except Exception:
        return ""


def _summarize_thesis(text):
    """원문 1페이지를 받아 저작권 안전한(재서술) 한 줄 요약을 생성한다.
    ANTHROPIC_API_KEY가 설정된 경우에만 동작하며, 원문 문장을 인용하지 않고
    핵심 논지만 우리말로 다시 서술하도록 지시한다. 실패 시 None."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key or not text:
        return None
    prompt = (
        "다음은 한 증권사 리서치 리포트의 1페이지 원문이다. 이 리포트가 목표주가/투자의견을 "
        "그렇게 제시한 핵심 사유를 한국어 한 문장(40자 이내)으로 '요약'하라. "
        "원문 문장·표현을 그대로 베끼지 말고 완전히 새 문장으로 서술하고, 요약문만 출력하라.\n\n"
        "원문:\n" + text
    )
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": os.environ.get("SUMMARY_MODEL", "claude-haiku-4-5-20251001"),
                  "max_tokens": 80,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=30)
        if resp.status_code == 200:
            blocks = resp.json().get("content", [])
            out = "".join(b.get("text", "") for b in blocks
                          if b.get("type") == "text").strip()
            return out[:80] or None
    except Exception:
        pass
    return None


def enrich_reports(reports):
    """report_idx 캐시를 사용해 신규 리포트만 원문 PDF를 조회, 발표 시점 현재주가로
    상승여력(목표가/현재가-1)을 계산해 각 리포트에 부착한다. 스틸한 문장은 저장하지 않음."""
    if not ENRICH:
        for r in reports:
            r["current_price"], r["upside"], r["synopsis"] = None, None, None
        return
    cache = {}
    if os.path.exists(SUMMARY_PATH):
        try:
            with open(SUMMARY_PATH, "r", encoding="utf-8") as f:
                cache = json.load(f)
        except Exception:
            cache = {}
    sess = requests.Session()
    sess.headers.update(HEADERS)
    attempts = 0
    for r in reports:
        idx = r.get("report_idx")
        if idx and idx not in cache and attempts < MAX_ENRICH:
            attempts += 1
            url = f"http://consensus.hankyung.com/analysis/downpdf?report_idx={idx}"
            try:
                resp = sess.get(url, timeout=PDF_TIMEOUT)
                if resp.status_code == 200 and resp.content[:5] == b"%PDF-":
                    facts = _extract_pdf_facts(resp.content)      # 숫자 팩트
                    syn = _summarize_thesis(_pdf_first_page_text(resp.content))
                    if syn:
                        facts["synopsis"] = syn                    # 재서술 요약(선택)
                    cache[idx] = facts
                # 네트워크 실패는 캐시하지 않음 -> 다음 실행 재시도
            except Exception:
                pass
            time.sleep(0.3)
        info = cache.get(idx, {}) if idx else {}
        cp = info.get("current_price")
        r["current_price"] = cp
        r["synopsis"] = info.get("synopsis")
        r["upside"] = (round((r["target"] / cp - 1) * 100, 1)
                       if cp and r.get("target") else None)
    if attempts:
        try:
            with open(SUMMARY_PATH, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False)
        except Exception:
            pass
    print(f"[enrich] 신규 PDF 조회 {attempts}건 · 캐시 총 {len(cache)}건")


# ---------------------------------------------------------------- analytics ---
def _latest_with(lst, valid):
    for r in sorted(lst, key=lambda x: x["date"], reverse=True):
        if valid(r):
            return r
    return None


def build_analytics(reports, meta):
    by_broker = defaultdict(list)
    for r in reports:
        by_broker[r["source"]].append(r)
    for lst in by_broker.values():
        lst.sort(key=lambda x: x["date"])

    # 증권사별 최신 상태 (+ 휴면 여부)
    active_cut = (date.today() - timedelta(days=ACTIVE_DAYS)).strftime("%Y-%m-%d")
    broker_latest = {}
    for b, lst in by_broker.items():
        op_rep = _latest_with(lst, lambda r: r["opinion"] in ("BUY", "HOLD", "SELL"))
        tg_rep = _latest_with(lst, lambda r: r["target"])
        broker_latest[b] = {
            "opinion": op_rep["opinion"] if op_rep else "N/A",
            "opinion_raw": op_rep["opinion_raw"] if op_rep else "-",
            "target": tg_rep["target"] if tg_rep else None,
            "date": lst[-1]["date"],
            "title": lst[-1]["title"],
            "report_idx": lst[-1]["report_idx"],
            "stale": lst[-1]["date"] < active_cut,
        }
    active = {b: v for b, v in broker_latest.items() if not v["stale"]}

    # 리비전(직전 리포트 대비 변동) 이벤트
    RANK = {"SELL": 0, "HOLD": 1, "BUY": 2}
    revisions = []
    for b, lst in by_broker.items():
        prev_t = prev_o = None
        for r in lst:
            if prev_t and r["target"] and r["target"] != prev_t:
                revisions.append({
                    "date": r["date"], "broker": b, "kind": "target",
                    "from": prev_t, "to": r["target"],
                    "pct": round((r["target"] - prev_t) / prev_t * 100, 1),
                    "dir": "UP" if r["target"] > prev_t else "DOWN",
                    "title": r["title"], "report_idx": r["report_idx"],
                })
            if (prev_o in RANK and r["opinion"] in RANK
                    and r["opinion"] != prev_o):
                revisions.append({
                    "date": r["date"], "broker": b, "kind": "opinion",
                    "from": prev_o, "to": r["opinion"],
                    "dir": "UP" if RANK[r["opinion"]] > RANK[prev_o] else "DOWN",
                    "title": r["title"], "report_idx": r["report_idx"],
                })
            if r["target"]:
                prev_t = r["target"]
            if r["opinion"] in RANK:
                prev_o = r["opinion"]
    revisions.sort(key=lambda x: x["date"], reverse=True)

    # 컨센서스 분포 (유효 증권사의 최신 의견만)
    dist = Counter()
    for v in active.values():
        if v["opinion"] in ("BUY", "HOLD", "SELL"):
            dist[v["opinion"]] += 1

    # 목표주가 통계 (유효 증권사의 최신 목표가만)
    targets = [(b, v["target"]) for b, v in active.items() if v["target"]]
    tvals = [t for _, t in targets]
    tstats = {}
    if tvals:
        tstats = {
            "count": len(tvals),
            "mean": int(statistics.mean(tvals)),
            "median": int(statistics.median(tvals)),
            "max": max(tvals), "min": min(tvals),
            "max_broker": max(targets, key=lambda x: x[1])[0],
            "min_broker": min(targets, key=lambda x: x[1])[0],
        }

    # 모멘텀(창 내 상향/하향 집계)
    def window(days):
        cut = (date.today() - timedelta(days=days)).strftime("%Y-%m-%d")
        w = [r for r in revisions if r["date"] >= cut]
        return {
            "target_up":  sum(1 for r in w if r["kind"] == "target" and r["dir"] == "UP"),
            "target_down": sum(1 for r in w if r["kind"] == "target" and r["dir"] == "DOWN"),
            "op_up":  sum(1 for r in w if r["kind"] == "opinion" and r["dir"] == "UP"),
            "op_down": sum(1 for r in w if r["kind"] == "opinion" and r["dir"] == "DOWN"),
        }

    # 90일 이동 컨센서스 + 분산(추이 라인/밴드) — 월 단위 샘플, 롤링 윈도우로 매끄럽게
    WIN = 90
    tgt = sorted([r for r in reports if r["target"]], key=lambda r: r["date"])
    monthly_series = []
    if tgt:
        def _months(s, e):
            y, m = int(s[:4]), int(s[5:7]); ey, em = int(e[:4]), int(e[5:7])
            out = []
            while (y, m) <= (ey, em):
                out.append(f"{y:04d}-{m:02d}")
                m += 1
                if m > 12: m = 1; y += 1
            return out
        for ym in _months(tgt[0]["date"][:7], tgt[-1]["date"][:7]):
            y, m = int(ym[:4]), int(ym[5:7])
            end_d = (date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)) - timedelta(days=1)
            start_s = (end_d - timedelta(days=WIN)).strftime("%Y-%m-%d")
            end_s = end_d.strftime("%Y-%m-%d")
            w = [r["target"] for r in tgt if start_s < r["date"] <= end_s]
            if not w:
                continue
            mm = statistics.mean(w); sd = statistics.pstdev(w) if len(w) > 1 else 0
            monthly_series.append({
                "ym": ym, "avg": int(mm), "sd": int(sd),
                "lo": int(max(0, mm - sd)), "hi": int(mm + sd),
                "min": int(min(w)), "max": int(max(w)), "n": len(w),
            })

    return {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M KST"),
        "key": meta["key"],
        "stock": {"name": meta["name"], "code": meta.get("code", "")},
        "currency": meta.get("currency", "KRW"),
        "source": meta.get("src_label", ""),
        "n_reports": len(reports),
        "n_active": len(active),
        "n_stale": len(broker_latest) - len(active),
        "active_days": ACTIVE_DAYS,
        "broker_latest": broker_latest,
        "revisions": revisions,
        "distribution": dict(dist),
        "target_stats": tstats,
        "momentum_short": window(MOMENTUM_SHORT),
        "momentum_long": window(MOMENTUM_LONG),
        "monthly_series": monthly_series,
        "reports": reports,
    }


# ------------------------------------------------------------------ telegram ---
def _won(v):
    return f"₩{v:,}" if v else "-"


OP_KR = {"BUY": "매수", "HOLD": "중립", "SELL": "매도", "N/A": "의견없음"}


def format_telegram(a, meta, new_reports, first_run):
    cur = meta.get("currency", "KRW")
    def M(v):
        return _money(v, cur)
    s = a["target_stats"]
    d = a["distribution"]
    ms = a["momentum_short"]
    total = sum(d.values()) or 1
    buy = d.get("BUY", 0)
    up = ms["target_up"] + ms["op_up"]
    dn = ms["target_down"] + ms["op_down"]
    net = up - dn
    arrow = "📈" if net > 0 else ("📉" if net < 0 else "➖")

    L = [f"📡 <b>{meta['name']} 애널리스트 레이더</b>",
         f"<i>{a['generated']} · {meta.get('src_label','')}</i>"]
    if s:
        L.append(f"🎯 목표가 <b>{M(s['mean'])}</b>  ({M(s['min'])}~{M(s['max'])})")
    L.append(f"📊 매수 {buy} · 중립 {d.get('HOLD',0)} · 매도 {d.get('SELL',0)}  "
             f"(매수 {round(buy/total*100)}%)")
    L.append(f"{arrow} 최근 {MOMENTUM_SHORT}일 리비전 ▲{ms['target_up']} / ▼{ms['target_down']}"
             f"  (순 {'+' if net>=0 else ''}{net})")

    latest = a["reports"][-1] if first_run else (new_reports[0] if new_reports else None)
    if latest:
        op = OP_KR.get(latest["opinion"], latest["opinion_raw"])
        head = "📌 스냅샷" if first_run else f"🆕 신규 {len(new_reports)}건"
        L.append(f"{head} · 최신 {latest['date'][5:]} <b>{latest['source']}</b> "
                 f"{op} {M(latest['target'])}")

    url = DASHBOARD_URL.rstrip("/") + "/#" + meta["key"]
    L.append(f"🔗 <a href=\"{url}\">대시보드에서 자세히 보기 →</a>")
    return "\n".join(L)


def format_combined(items):
    """여러 종목을 하나의 메시지로 통합. items = [(meta, a, new_reports, first_run), ...]"""
    base = DASHBOARD_URL.rstrip("/")
    gen = items[0][1]["generated"] if items else datetime.now().strftime("%Y-%m-%d %H:%M KST")
    L = ["📡 <b>메모리 애널리스트 레이더</b>", f"<i>{gen} 기준</i>"]
    for meta, a, new_reports, first_run in items:
        cur = meta.get("currency", "KRW")
        def M(v, _c=cur):
            return _money(v, _c)
        s = a["target_stats"]
        d = a["distribution"]
        ms = a["momentum_short"]
        total = sum(d.values()) or 1
        buy = d.get("BUY", 0)
        srcshort = "Yahoo·美" if meta["source"] == "yahoo" else "한경"
        badge = f"🆕{len(new_reports)}" if (new_reports and not first_run) else "•"
        L.append("")
        L.append(f"{badge} <b>{meta['name']}</b> <i>· {srcshort}</i>")
        L.append(f"🎯 {M(s['mean'])} ({M(s['min'])}~{M(s['max'])})" if s else "🎯 -")
        L.append(f"📊 매수 {buy}/{total} · 30일 ▲{ms['target_up']}/▼{ms['target_down']}")
        latest = a["reports"][-1] if first_run else (new_reports[0] if new_reports else None)
        if latest:
            op = OP_KR.get(latest["opinion"], latest["opinion_raw"])
            L.append(f"   └ 최신 {latest['date'][5:]} {latest['source']} {op} {M(latest['target'])}")
        L.append(f"   🔗 <a href=\"{base}/#{meta['key']}\">{meta['name']} 탭 ›</a>")
    L.append("")
    L.append(f"🔗 <a href=\"{base}\">전체 대시보드 열기 →</a>")
    return "\n".join(L)


def send_telegram(text, token, chat_id):
    if not token or not chat_id:
        print("[telegram] token/chat_id 미설정 - 발송 건너뜀")
        print(text)
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    r = requests.post(url, json={"chat_id": chat_id, "text": text,
                                 "parse_mode": "HTML",
                                 "disable_web_page_preview": True}, timeout=20)
    if r.status_code != 200:
        print("[telegram] error:", r.status_code, r.text[:200])
        r.raise_for_status()
    print("[telegram] sent OK")
    return True


# -------------------------------------------------------------------- state ---
def report_key(r):
    return f"{r['date']}|{r['source']}|{r['author']}|{r['target']}|{r['opinion']}"


def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- dashboard ---
def render_dashboard(payload):
    with open(os.path.join(HERE, "dashboard_template.html"), "r",
              encoding="utf-8") as f:
        tmpl = f.read()
    html = tmpl.replace("/*__DATA__*/null", json.dumps(payload, ensure_ascii=False))
    with open(DASHBOARD_PATH, "w", encoding="utf-8") as f:
        f.write(html)
    with open(os.path.join(HERE, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[dashboard] wrote {DASHBOARD_PATH} + index.html ({len(html):,} bytes)")


# --------------------------------------------------------------------- main ---
def main():
    cfg = load_config()
    dashboard_only = os.environ.get("DASHBOARD_ONLY", "").lower() in ("1", "true")
    state = load_state()
    payload = {"generated": datetime.now().strftime("%Y-%m-%d %H:%M KST"),
               "symbols": [], "data": {}}
    items = []

    for meta in SYMBOLS:
        key = meta["key"]
        print(f"[{key}] {meta['name']} 수집중...")
        try:
            if meta["source"] == "yahoo":
                reports = fetch_yahoo_reports(meta["ticker"])
            else:
                reports = fetch_reports(meta["name"], meta["code"])
                enrich_reports(reports)
        except Exception as e:
            print(f"[{key}] 수집 실패: {e}")
            continue
        print(f"[{key}] {len(reports)}건 수집")
        if not reports:
            print(f"[{key}] 리포트 없음 - 스킵")
            continue

        a = build_analytics(reports, meta)
        payload["data"][key] = a
        payload["symbols"].append({
            "key": key, "name": meta["name"], "code": meta.get("code", ""),
            "currency": meta.get("currency", "KRW"),
            "source": meta.get("src_label", ""),
        })

        st = state.get(key, {})
        first_run = st.get("runs", 0) == 0
        seen = set(st.get("seen", []))
        new_reports = sorted([r for r in reports if report_key(r) not in seen],
                             key=lambda x: x["date"], reverse=True)
        print(f"[{key}] run#{st.get('runs',0)+1}  신규 {len(new_reports)}건  "
              f"(first_run={first_run})")
        items.append((meta, a, new_reports, first_run))
        state[key] = {
            "seen": [report_key(r) for r in reports],
            "runs": st.get("runs", 0) + 1,
            "last_run": datetime.now().isoformat(timespec="seconds"),
        }

    if not payload["symbols"]:
        raise SystemExit("no symbols fetched")

    render_dashboard(payload)

    # 텔레그램: 신규 리포트가 있는 종목이 하나라도 있으면 통합 메시지 1건 발송
    if dashboard_only:
        print("[telegram] DASHBOARD_ONLY - 발송 생략")
    elif any(fr or nr for (_, _, nr, fr) in items):
        send_telegram(format_combined(items), cfg["telegram_token"],
                      cfg["telegram_chat_id"])
    else:
        print("[telegram] 신규 리포트 없음 - 발송 생략")

    save_state(state)
    print("[done]")


if __name__ == "__main__":
    main()
