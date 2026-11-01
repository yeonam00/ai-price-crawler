"""클로드·제미나이·챗GPT(OpenAI) 공식 페이지에서 가격 변경·지원 종료 예고 수집.

출력 : output/notices.json
       {"collected_at", "sources": [{id, url, ok, count, error}], "notices": [...]}
       notice = {vendor, kind, date, label, model, text, replacement, source, url, first_seen}
         kind  : price(가격 변경·할인 종료) / retire(지원 종료)
         model : track.canonical 로 통일한 id. 대시보드 추적 모델이 아니면 그대로 표시만 함

읽는 방법
  - 지원 종료 페이지: 날짜·모델·대체 모델 표. 'Not sooner than', 'at earliest' 처럼 확정되지 않은 날짜는 제외
  - 가격표·변경 이력: 오늘 이후 날짜와 가격/종료 표현이 함께 있는 문장을 원문 그대로 보관
  - Google 가격표는 칸별 가격이 있어 dashboard.py 의 예정 변경(scheduled_changes)이 맡음 -> 여기서는 읽지 않음
  - 페이지 수집에 실패하면 그 페이지에서 나온 기존 예고를 유지
"""
import asyncio
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path

from playwright.async_api import async_playwright

import track
from crawl import _EXTRACT_JS, PAGE_TIMEOUT_MS, UA

OUT = Path(__file__).parent / "output" / "notices.json"
# (id, 제조사, 읽는 방법, url)
SOURCES = [
    ("anthropic_deprecations", "anthropic", "table", "https://platform.claude.com/docs/en/about-claude/model-deprecations"),
    ("anthropic_pricing", "anthropic", "text", "https://platform.claude.com/docs/en/about-claude/pricing"),
    ("anthropic_releases", "anthropic", "text", "https://platform.claude.com/docs/en/release-notes/overview"),
    ("google_deprecations", "google", "table", "https://ai.google.dev/gemini-api/docs/deprecations"),
    ("google_changelog", "google", "text", "https://ai.google.dev/gemini-api/docs/changelog"),
    ("openai_deprecations", "openai", "table", "https://developers.openai.com/api/docs/deprecations"),
    ("openai_pricing", "openai", "text", "https://developers.openai.com/api/docs/pricing"),
    ("openai_changelog", "openai", "text", "https://developers.openai.com/api/docs/changelog"),
]

_MON = "January|February|March|April|May|June|July|August|September|October|November|December|" \
       "Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept|Sep|Oct|Nov|Dec"
_DATE = re.compile(rf"\b(?:({_MON})\.? (\d{{1,2}})(?:st|nd|rd|th)?,? (20\d\d)|(20\d\d)[-‑](\d\d)[-‑](\d\d))\b")
_TENTATIVE = re.compile(r"not sooner|at earliest|earliest|tentative|to be announced|no shutdown", re.I)
_PRICE = re.compile(r"\bpric|promotional|introductory|discount|uplift", re.I)
_RETIRE = re.compile(r"deprecat|retire|shut ?down|removal|removed from|sunset", re.I)
_MODEL = re.compile(r"GPT-\d[\w.]*(?: [A-Z][a-z]+)*(?: for [A-Z][a-z]+)?|Gemini \d[\d.]*(?: [A-Z][\w-]*)*|"
                    r"Claude (?:Opus|Sonnet|Haiku|Fable|Mythos) \d[\d.]*|(?:gpt|gemini|claude)-[a-z0-9][\w.-]*")


def to_date(m):
    try:
        if m.group(4):
            return date(int(m.group(4)), int(m.group(5)), int(m.group(6)))
        return datetime.strptime(f"{m.group(1)[:3]} {m.group(2)} {m.group(3)}", "%b %d %Y").date()
    except ValueError:
        return None


def clean(s):
    return re.sub(r"\s+", " ", s or "").strip()


def cid_of(name):
    return track.canonical(name.split(" | ")[0].replace(" for ", " ")) if name else None


def _col(head, keys, skip=("price", "tentative")):
    low = [h.lower() for h in head]
    return next((i for i, h in enumerate(low) if any(k in h for k in keys) and not any(s in h for s in skip)), None)


def from_tables(data, src, vendor, url, today):
    """날짜·모델·대체 모델 표. 'Tentative retirement date' 열은 확정 일정이 아니라 _col 에서 제외됨."""
    out = []
    for t in data["tables"]:
        head = t["rows"][0] if t["rows"] else []
        cd = _col(head, ("retirement date", "shutdown date"))
        cm = _col(head, ("model", "system", "agent"))
        cr = _col(head, ("replacement", "substitute"), skip=())
        if cd is None or cm is None:
            continue
        for r in t["rows"][1:]:
            if max(cd, cm) >= len(r) or _TENTATIVE.search(r[cd]):
                continue
            m = _DATE.search(r[cd])
            d = to_date(m) if m else None
            if not d or d < today:
                continue
            label = clean(r[cm])
            out.append({"vendor": vendor, "kind": "retire", "date": d.isoformat(), "label": label,
                        "model": cid_of(label), "text": None, "source": src, "url": url,
                        "replacement": clean(r[cr]) if cr is not None and cr < len(r) else None})
    return out


def from_text(data, src, vendor, url, today):
    """오늘 이후 날짜와 가격/종료 표현이 같은 문장에 있으면 예고로 봄."""
    out = []
    for s in re.split(r"(?<=[.!?])\s+(?=[A-Z])", data["text"]):
        if _TENTATIVE.search(s):
            continue
        ms = [(to_date(m), m) for m in _DATE.finditer(s)]
        ms = [(d, m) for d, m in ms if d and d >= today]
        if not ms:
            continue
        kind = "price" if _PRICE.search(s) else "retire" if _RETIRE.search(s) else None
        if not kind:
            continue
        d, m = ms[0]
        name = _MODEL.search(s)
        label = name.group(0).rstrip(".") if name else None
        cut = s.find(" ", max(0, m.start() - 170)) + 1      # 단어 중간에서 자르지 않게
        text = clean(s if len(s) <= 260 else "…" + s[cut:m.end() + 60] + "…")
        out.append({"vendor": vendor, "kind": kind, "date": d.isoformat(), "label": label or text[:40],
                    "model": cid_of(label), "text": text, "source": src, "url": url, "replacement": None})
    return out


def merge(items):
    """같은 예고가 여러 페이지에 있으면 하나만 남김. 지원 종료는 표(확정 일정)를 문장보다 우선."""
    table = {(n["vendor"], n["date"], n["model"]) for n in items if n["kind"] == "retire" and not n["text"]}
    dates = {k[:2] for k in table}
    seen, out = set(), []
    for n in items:
        if n["kind"] == "retire" and n["text"]:
            if (n["vendor"], n["date"], n["model"]) in table or (not n["model"] and (n["vendor"], n["date"]) in dates):
                continue
            # 지원 종료 표에 없고 문장에만 있으면 모델이 아니라 기능 종료일 수 있음 (예: Opus 4.8 fast mode) -> 모델에 연결 안 함
            n = {**n, "model": None}
        key = (n["vendor"], n["kind"], n["date"], n["model"] or n["label"])
        if key not in seen:
            seen.add(key)
            out.append(n)
    return sorted(out, key=lambda n: (n["date"], n["vendor"], n["label"]))


async def fetch(browser, url):
    ctx = await browser.new_context(user_agent=UA, locale="en-US", viewport={"width": 1440, "height": 1000})
    page = await ctx.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
        try:
            await page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass
        await page.wait_for_timeout(1500)
        return await page.evaluate(_EXTRACT_JS)
    finally:
        await ctx.close()


async def collect(browser, source, sem, today):
    src, vendor, how, url = source
    async with sem:
        status = {"id": src, "url": url, "ok": False, "count": 0, "error": None}
        items = []
        try:
            for attempt in range(2):
                try:
                    data = await fetch(browser, url)
                    break
                except Exception:
                    if attempt == 1:
                        raise
                    await asyncio.sleep(3)
            if how == "table":
                items = from_tables(data, src, vendor, url, today)
                if not data["tables"]:          # 페이지 구조가 바뀌었으면 '예고 없음'으로 오해하지 않게 실패 처리
                    raise ValueError("표를 찾지 못함")
            else:
                items = from_text(data, src, vendor, url, today)
                if len(data["text"]) < 1000:
                    raise ValueError("본문이 비어 있음")
            status.update(ok=True, count=len(items))
        except Exception as e:
            status["error"] = f"{type(e).__name__}: {str(e)[:200]}"
        print(f"[{'OK ' if status['ok'] else 'FAIL'}] {src:<24} {status['count']:>3}건  {status['error'] or ''}", flush=True)
        return status, items


async def main():
    now = datetime.now().astimezone()
    today = now.date()
    old = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {"notices": []}
    sem = asyncio.Semaphore(4)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        results = await asyncio.gather(*(collect(browser, s, sem, today) for s in SOURCES))
        await browser.close()
    statuses = [r[0] for r in results]
    failed = {s["id"] for s in statuses if not s["ok"]}
    kept = [n for n in old["notices"] if n["source"] in failed and n["date"] >= today.isoformat()]
    notices = merge([n for r in results for n in r[1]] + kept)
    seen = {(n["kind"], n["date"], n["label"]): n.get("first_seen") for n in old["notices"]}
    for n in notices:
        n["first_seen"] = seen.get((n["kind"], n["date"], n["label"])) or now.isoformat(timespec="seconds")
    for s in statuses:
        if not s["ok"]:
            print(f"::warning title=예고 수집 실패: {s['id']}::{s['error']}")
    OUT.write_text(json.dumps({"collected_at": now.isoformat(timespec="seconds"), "sources": statuses,
                               "notices": notices}, ensure_ascii=False, indent=1), encoding="utf-8")
    kinds = {k: sum(n["kind"] == k for n in notices) for k in ("price", "retire")}
    print(f"예고: 가격 {kinds['price']}건, 지원 종료 {kinds['retire']}건 (페이지 {len(statuses) - len(failed)}/{len(statuses)} 성공)")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
