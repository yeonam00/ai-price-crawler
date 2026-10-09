"""AI 모델 API 가격 크롤러.

사용법:
    python crawl.py                      # 전체 사이트
    python crawl.py openrouter openai    # 일부 사이트(id)만
    python crawl.py --headed             # 브라우저 창 보이게

출력 (output/ 폴더):
    prices_<YYYYMMDD_HHMMSS>.json  - 정규화된 모델별 가격 (collected_at, provider 포함)
    raw_<YYYYMMDD_HHMMSS>.json     - 파싱 못한/원본 표 + 페이지 텍스트 일부 (정규화 개선용)
"""
import asyncio
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import httpx
from playwright.async_api import async_playwright

from parsers import PARSERS
from sites import SITES

OUT_DIR = Path(__file__).parent / "output"
DUMP_DIR = OUT_DIR / "dump" if "--dump" in sys.argv else None  # 파서 개발용 원본 추출 데이터
CONCURRENCY = 4
PAGE_TIMEOUT_MS = 45_000
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

PRICE_FIELDS = ["input", "output", "cache_read", "cache_write", "cache_write_5m", "cache_write_1h",
                "batch_input", "batch_output", "per_request", "per_image", "per_second", "per_minute"]


from crawl_common import classify_header, detect_unit, parse_price, tier_from  # noqa: E402


def normalize_table(tbl, site):
    """표 하나 -> 모델 레코드 리스트. 모델 열/가격 열이 없으면 []"""
    rows = [r for r in tbl["rows"] if any(c.strip() for c in r)]
    if len(rows) < 2:
        return [], False
    hdr_idx = next((i for i, r in enumerate(rows[:3]) if sum(1 for c in r if c.strip()) >= 2), 0)
    headers = rows[hdr_idx]
    ctx = tbl.get("heading", "")
    roles = [classify_header(h, ctx) for h in headers]
    if "model" not in roles:
        # 첫 열을 모델명으로 간주 (가격 열이 있을 때만)
        if any(r and (r in PRICE_FIELDS or r == "price_generic") for r in roles):
            roles[0] = "model"
        else:
            return [], False
    if not any(r in PRICE_FIELDS or r == "price_generic" for r in roles if r):
        return [], False
    unit_hint = detect_unit(ctx + " " + " ".join(headers))
    out = []
    for r in rows[hdr_idx + 1:]:
        if len(r) < 2:
            continue
        rec_roles = dict(zip(roles, r)) if False else None  # 중복 역할 대비해 인덱스로 처리
        model = None
        prices, meta = {}, {}
        for i, role in enumerate(roles):
            if i >= len(r) or not role:
                continue
            cell = r[i].strip()
            if role == "model":
                model = model or cell
            elif role.startswith("meta:"):
                if cell:
                    meta[role[5:]] = cell
            else:
                key = "input" if role == "price_generic" else role
                if key not in prices:
                    p = parse_price(cell, site["currency"])
                    if p:
                        prices[key] = p
        if not model or not prices or all(p["value"] is None for p in prices.values()):
            continue
        out.append({
            "model": model,
            "pricing": prices,
            "unit": unit_hint or detect_unit(" ".join(p["raw"] for p in prices.values())),
            "tier": tier_from(ctx),
            "section": ctx or None,
            "meta": meta or None,
        })
    return out, bool(out)


# ───────────────────────── API 어댑터 ─────────────────────────
def _per_m(v):
    try:
        return round(float(v) * 1_000_000, 6) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _p(value, cur="USD"):
    return None if value is None else {"raw": str(value), "value": value, "currency": cur}


async def adapter_openrouter(client, site):
    data = (await client.get("https://openrouter.ai/api/v1/models")).json()["data"]
    recs = []
    for m in data:
        pr = m.get("pricing") or {}
        pricing = {k: _p(_per_m(pr.get(src))) for k, src in
                   [("input", "prompt"), ("output", "completion"),
                    ("cache_read", "input_cache_read"), ("cache_write", "input_cache_write")]}
        pricing["per_request"] = _p(float(pr["request"]) if pr.get("request") not in (None, "", "0") else None)
        pricing["per_image"] = _p(float(pr["image"]) if pr.get("image") not in (None, "", "0") else None)
        recs.append({"model": m.get("name"), "model_id": m.get("id"),
                     "model_provider": (m.get("id") or "").split("/")[0] or None,
                     "pricing": {k: v for k, v in pricing.items() if v},
                     "unit": "per_1m_tokens", "tier": None, "section": None,
                     "meta": {"context_length": m.get("context_length"),
                              "modality": (m.get("architecture") or {}).get("modality")}})
    return recs


async def adapter_vercel(client, site):
    data = (await client.get("https://ai-gateway.vercel.sh/v1/models")).json()["data"]
    recs = []
    for m in data:
        pr = m.get("pricing") or {}
        pricing = {k: _p(_per_m(pr.get(src))) for k, src in
                   [("input", "input"), ("output", "output"),
                    ("cache_read", "input_cache_read"), ("cache_write", "input_cache_write")]}
        pricing["per_image"] = _p(float(pr["image"]) if pr.get("image") else None)
        recs.append({"model": m.get("name") or m.get("id"), "model_id": m.get("id"),
                     "model_provider": m.get("owned_by") or (m.get("id") or "").split("/")[0],
                     "pricing": {k: v for k, v in pricing.items() if v},
                     "unit": "per_1m_tokens", "tier": None, "section": None,
                     "meta": {"context_window": m.get("context_window"), "type": m.get("type")}})
    return recs


async def adapter_litellm(client, site):
    url = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
    data = (await client.get(url)).json()
    recs = []
    for mid, m in data.items():
        if mid == "sample_spec" or not isinstance(m, dict):
            continue
        pricing = {
            "input": _p(_per_m(m.get("input_cost_per_token"))),
            "output": _p(_per_m(m.get("output_cost_per_token"))),
            "cache_read": _p(_per_m(m.get("cache_read_input_token_cost"))),
            "cache_write": _p(_per_m(m.get("cache_creation_input_token_cost"))),
            "batch_input": _p(_per_m(m.get("input_cost_per_token_batches"))),
            "batch_output": _p(_per_m(m.get("output_cost_per_token_batches"))),
            "per_image": _p(m.get("output_cost_per_image")),
            "per_second": _p(m.get("output_cost_per_second") or m.get("output_cost_per_video_per_second")),
        }
        recs.append({"model": mid, "model_id": mid, "model_provider": m.get("litellm_provider"),
                     "pricing": {k: v for k, v in pricing.items() if v},
                     "unit": "per_1m_tokens", "tier": None, "section": None,
                     "meta": {"mode": m.get("mode"), "max_input_tokens": m.get("max_input_tokens"),
                              "max_output_tokens": m.get("max_output_tokens")}})
    return recs


ADAPTERS = {"openrouter": adapter_openrouter, "vercel": adapter_vercel, "litellm": adapter_litellm}


# ───────────────────────── 브라우저 크롤링 ─────────────────────────
_EXTRACT_JS = r"""
() => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const heading = el => {
    let n = el;
    while (n && n !== document.body) {
      let p = n.previousElementSibling;
      while (p) {
        if (/^H[1-6]$/.test(p.tagName)) return clean(p.textContent);
        const hs = p.querySelectorAll('h1,h2,h3,h4,h5,h6');
        if (hs.length) return clean(hs[hs.length - 1].textContent);
        p = p.previousElementSibling;
      }
      n = n.parentElement;
    }
    return '';
  };
  const tabOf = el => {
    const out = [];
    let n = el;
    while (n && n !== document.body) {
      if (n.getAttribute && n.getAttribute('role') === 'tabpanel') {
        const ids = (n.getAttribute('aria-labelledby') || '').split(' ').filter(Boolean);
        let lab = ids.map(i => document.getElementById(i)).filter(Boolean).map(e => clean(e.textContent)).join(' ');
        if (!lab && n.id) { const t = document.querySelector('[aria-controls="' + n.id + '"]'); if (t) lab = clean(t.textContent); }
        if (lab) out.push(lab);
      }
      n = n.parentElement;
    }
    return out.reverse().join(' > ');
  };
  const out = [];
  document.querySelectorAll('table').forEach(t => {
    const rows = [];  // colspan/rowspan 을 펼친 그리드
    [...t.querySelectorAll('tr')].forEach((tr, ri) => {
      rows[ri] = rows[ri] || [];
      let ci = 0;
      [...tr.querySelectorAll('th,td')].forEach(c => {
        while (rows[ri][ci] !== undefined) ci++;
        const cs = Math.min(c.colSpan || 1, 40), rs = Math.min(c.rowSpan || 1, 40), txt = clean(c.textContent);
        for (let dr = 0; dr < rs; dr++) {
          rows[ri + dr] = rows[ri + dr] || [];
          for (let dc = 0; dc < cs; dc++) rows[ri + dr][ci + dc] = txt;
        }
        ci += cs;
      });
    });
    const grid = rows.filter(Boolean).map(r => Array.from(r, v => v === undefined ? '' : v));
    if (grid.length >= 2) out.push({kind: 'table', heading: heading(t), tab: tabOf(t), rows: grid});
  });
  document.querySelectorAll('[role=table],[role=grid],[role=treegrid]').forEach(t => {
    const rows = [...t.querySelectorAll('[role=row]')].map(r =>
      [...r.querySelectorAll('[role=columnheader],[role=cell],[role=gridcell],[role=rowheader]')].map(c => clean(c.textContent)));
    if (rows.length >= 2) out.push({kind: 'aria', heading: heading(t), tab: tabOf(t), rows});
  });
  return {tables: out, text: clean(document.body.innerText).slice(0, 80000), title: document.title};
}
"""


async def autoscroll(page, rounds=12):
    last = -1
    for _ in range(rounds):
        h = await page.evaluate("document.body.scrollHeight")
        if h == last:
            break
        last = h
        await page.mouse.wheel(0, 4000)
        await page.wait_for_timeout(500)
    await page.evaluate("window.scrollTo(0, 0)")


async def crawl_with_browser(browser, site):
    ctx = await browser.new_context(user_agent=UA, locale="en-US", viewport={"width": 1440, "height": 1000})
    page = await ctx.new_page()
    try:
        await page.goto(site["url"], wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
        try:
            await page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass
        await page.wait_for_timeout(1500)
        await autoscroll(page)
        data = await page.evaluate(_EXTRACT_JS)
    finally:
        await ctx.close()
    if DUMP_DIR:
        DUMP_DIR.mkdir(parents=True, exist_ok=True)
        (DUMP_DIR / f"{site['id']}.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    if site["id"] in PARSERS:
        return PARSERS[site["id"]](data, site), {"title": data["title"], "tables_found": len(data["tables"]),
                                                  "unparsed_tables": [], "page_text": data["text"][:3000]}
    models, raw_tables = [], []
    for t in data["tables"]:
        recs, ok = normalize_table(t, site)
        models.extend(recs)
        if not ok:
            raw_tables.append({"heading": t["heading"], "rows": t["rows"][:60]})
    return models, {"title": data["title"], "tables_found": len(data["tables"]),
                    "unparsed_tables": raw_tables[:30], "page_text": data["text"]}


# ───────────────────────── 실행 ─────────────────────────
def build_record(rec, site, collected_at):
    full = {k: rec["pricing"].get(k) for k in PRICE_FIELDS if rec["pricing"].get(k)}
    for k, v in rec["pricing"].items():
        if k not in PRICE_FIELDS:
            full[k] = v
    return {
        "collected_at": collected_at.isoformat(timespec="seconds"),
        "collected_date": collected_at.strftime("%Y-%m-%d"),
        "collected_time": collected_at.strftime("%H:%M:%S"),
        "provider": {
            "id": site["id"], "name": site["name"], "category": site["category"],
            "url": site["url"], "note": site["note"] or None,
        },
        "model": rec["model"],
        "model_id": rec.get("model_id"),
        "model_provider": rec.get("model_provider"),
        "pricing": full,
        "unit": rec.get("unit"),
        "tier": rec.get("tier"),
        "section": rec.get("section"),
        "context_tier": rec.get("context_tier"),
        "modality": rec.get("modality"),
        "meta": rec.get("meta"),
    }


async def run_site(site, browser, client, sem, collected_at):
    async with sem:
        status = {"id": site["id"], "name": site["name"], "url": site["url"], "method": None,
                  "ok": False, "model_count": 0, "error": None}
        raw = None
        t0 = datetime.now()
        try:
            if site["adapter"]:
                status["method"] = f"api:{site['adapter']}"
                recs = await ADAPTERS[site["adapter"]](client, site)
            else:
                status["method"] = "browser"
                for attempt in range(2):  # 일시적 연결 오류(ERR_CONNECTION_RESET 등) 1회 재시도
                    try:
                        recs, raw = await crawl_with_browser(browser, site)
                        break
                    except Exception:
                        if attempt == 1:
                            raise
                        await asyncio.sleep(3)
            status["model_count"] = len(recs)
            status["ok"] = len(recs) > 0
            if not recs:
                status["error"] = "가격 표를 찾지 못함 (raw 파일의 page_text/unparsed_tables 확인 -> 전용 파서 필요)"
        except Exception as e:  # 한 사이트 실패가 전체를 막지 않도록
            status["error"] = f"{type(e).__name__}: {str(e)[:300]}"
            recs = []
        status["seconds"] = round((datetime.now() - t0).total_seconds(), 1)
        print(f"[{'OK ' if status['ok'] else 'FAIL'}] {site['id']:<14} {status['model_count']:>5}개  {status['method']}  {status['error'] or ''}", flush=True)
        return status, [build_record(r, site, collected_at) for r in recs], raw


async def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    headed = "--headed" in sys.argv
    targets = [s for s in SITES if not args or s["id"] in args]
    if not targets:
        sys.exit(f"일치하는 사이트 id 없음. 가능: {', '.join(s['id'] for s in SITES)}")

    collected_at = datetime.now().astimezone()
    stamp = collected_at.strftime("%Y%m%d_%H%M%S")
    OUT_DIR.mkdir(exist_ok=True)
    sem = asyncio.Semaphore(CONCURRENCY)

    async with async_playwright() as pw, httpx.AsyncClient(timeout=60, follow_redirects=True, headers={"User-Agent": UA}) as client:
        browser = await pw.chromium.launch(headless=not headed)
        results = await asyncio.gather(*(run_site(s, browser, client, sem, collected_at) for s in targets))
        await browser.close()

    statuses = [r[0] for r in results]
    models = [m for r in results for m in r[1]]
    raws = {r[0]["id"]: r[2] for r in results if r[2]}

    payload = {
        "collected_at": collected_at.isoformat(timespec="seconds"),
        "site_count": len(targets),
        "success_count": sum(s["ok"] for s in statuses),
        "model_count": len(models),
        "sites": statuses,
        "models": models,
    }
    if "--ci" in sys.argv:  # 자동 실행용: 변경이 있을 때만 latest/history 갱신
        from persist import save_ci
        changed = save_ci(payload, OUT_DIR, stamp)
        print(f"\n완료(CI): 사이트 {payload['success_count']}/{payload['site_count']} 성공, "
              f"모델 {payload['model_count']}건, 가격 변경 {'있음' if changed else '없음'}")
        return
    (OUT_DIR / f"prices_{stamp}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT_DIR / f"raw_{stamp}.json").write_text(json.dumps(raws, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n완료: 사이트 {payload['success_count']}/{payload['site_count']} 성공, 모델 {len(models)}건 -> output/prices_{stamp}.json")


if __name__ == "__main__":
    asyncio.run(main())

