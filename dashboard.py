"""가격 추적 대시보드 생성 (output/dashboard.html).

입력 : output/history/prices_*.json.gz   가격이 바뀐 실행마다 1개 (persist.py)
       output/last_run.json              마지막 실행 시각·사이트 성공 현황
출력 : output/dashboard.html             데이터를 내장한 단일 HTML (오프라인으로 열림, Chart.js 만 CDN)

비교 조건은 track.py 와 동일 (USD, 100만 토큰당, standard tier, 기본 컨텍스트, 텍스트, 가중 평균 입력3:출력1).

반영할 변화 판정 (판매처별·모델 최저가별 시계열에 동일 적용)
  - 기준가 = 마지막으로 '확정'된 가격 (직전 수집값이 아니라 마지막 알림값 기준 -> 작은 변화의 누적도 잡힘)
  - 기준가 대비 MIN_PCT 이상 벗어난 상태가 CONFIRM_HOURS 이상 유지되면 '확정' -> 알림, 기준가 갱신
  - 그 전에 기준가 근처로 돌아오면 '일시적' -> 알림하지 않고 기록만
  - 아직 CONFIRM_HOURS 가 지나지 않았으면 '관찰 중'
"""
import gzip
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import track

ROOT = Path(__file__).parent
OUT = ROOT / "output"
TEMPLATE = ROOT / "dashboard_template.html"
MIN_PCT = 1.0                       # 이보다 작은 변동은 같은 가격으로 봄
CONFIRM_HOURS = 24                  # 이 시간 이상 유지돼야 반영할 변화로 확정
_SCHEDULED = re.compile(r"\$([\d.,]+)[^$]*?starting\s+([A-Z][a-z]+ \d{1,2}, \d{4})")
_UNTIL = re.compile(r"through\s+([A-Z][a-z]+ \d{1,2}, \d{4})")


def _num(p, key):
    v = (p.get(key) or {}).get("value")
    return round(v, 9) if v else None


def drop_mislabeled_batch(models):
    """같은 판매처·모델·섹션에 standard 행의 batch 가격과 똑같은 'standard' 행이 또 있으면 제거.

    2026-10-14 ~ parsers.py 수정 전 이력: Google 가격 페이지에 신설된 'Contributor' 탭(batch 와 같은 할인가)이
    standard 로 태깅돼 실제의 절반 가격이 최저가로 잡힘. 수정 후 데이터는 tier='contributor' 로 들어와 영향 없음.
    """
    batch = set()
    for m in models:
        p = m.get("pricing") or {}
        bi, bo = _num(p, "batch_input"), _num(p, "batch_output")
        if bi and bo:
            batch.add((m["provider"]["id"], m["model"], m.get("section"), bi, bo))
    if not batch:
        return models
    return [m for m in models
            if (m["provider"]["id"], m["model"], m.get("section"),
                _num(m["pricing"], "input"), _num(m["pricing"], "output")) not in batch]


def _snapshot(d):
    models = drop_mislabeled_batch(d["models"])
    now = datetime.fromisoformat(d["collected_at"])
    return now, models, track.offers(models, now)


def load_snapshots(extra=None):
    """이력 스냅샷 + (선택) 로컬에서 `python crawl.py` 로 만든 전체 수집본을 가장 최근 스냅샷으로 덧붙임."""
    snaps = []
    for f in sorted((OUT / "history").glob("prices_*.json.gz")):
        with gzip.open(f, "rt", encoding="utf-8") as fh:
            snaps.append(_snapshot(json.load(fh)))
    if extra:
        snaps.append(_snapshot(extra))
        snaps.sort(key=lambda s: s[0])
    return snaps


def best_per_seller(offs):
    best = {}
    for o in offs:
        if o["seller"] not in best or o["blended"] < best[o["seller"]]["blended"]:
            best[o["seller"]] = o
    return best


def runs_of(points, now):
    """[(t, v)] -> 같은 값이 이어진 구간 [(시작, 끝, v)]. 끝 = 다음 값이 관측된 시각(마지막은 now)."""
    runs = []
    for t, v in points:
        if runs and abs(v / runs[-1][2] - 1) * 100 < 1e-6:
            continue
        runs.append([t, None, v])
    for i, r in enumerate(runs):
        r[1] = runs[i + 1][0] if i + 1 < len(runs) else now
    return runs


def detect(points, now):
    """기준가(마지막 확정값) 대비 변화를 확정/일시적/관찰 중 으로 분류."""
    runs = runs_of(points, now)
    if not runs:
        return []
    events, base, pend = [], runs[0][2], None
    hold = timedelta(hours=CONFIRM_HOURS)
    for start, end, v in runs[1:]:
        dev = (v / base - 1) * 100
        if abs(dev) < MIN_PCT:
            if pend:
                events.append({**pend, "status": "transient", "until": start})
            pend = None
            continue
        if pend and (dev > 0) != (pend["new"] > base):
            events.append({**pend, "status": "transient", "until": start})
            pend = None
        if not pend:
            pend = {"at": start, "old": base, "new": v}
        pend["new"] = v
        if end - pend["at"] >= hold:
            events.append({**pend, "status": "confirmed", "confirmed_at": pend["at"] + hold})
            base, pend = v, None
    if pend:
        events.append({**pend, "status": "watching", "hours": round((now - pend["at"]).total_seconds() / 3600, 1)})
    return events


def scheduled_changes(models, cid, seller):
    """'$0.75 through December 31, 2026. $1.50 starting January 1, 2027.' 같은 예정 변경 문구 추출."""
    out = []
    for m in models:
        if track.canonical(m.get("model_id") or m["model"]) != cid or track.seller_of(m) != seller:
            continue
        if m.get("tier") not in (None, "standard"):
            continue
        for key in ("input", "output"):
            raw = ((m.get("pricing") or {}).get(key) or {}).get("raw") or ""
            hit = _SCHEDULED.search(raw)
            if not hit:
                continue
            until = _UNTIL.search(raw)
            out.append({"field": key, "price": float(hit.group(1).replace(",", "")),
                        "from": datetime.strptime(hit.group(2), "%B %d, %Y").date().isoformat(),
                        "current_until": datetime.strptime(until.group(1), "%B %d, %Y").date().isoformat() if until else None})
        if out:
            break
    return out


def scheduled_for(models, cid, lowest, direct):
    """최저가 판매처, 없으면 제조사 직판 페이지의 예정 변경. 최저가가 재판매처로 넘어가도 공식 할인 종료 공지를 놓치지 않게 함."""
    for offer in (lowest, direct):
        if not offer:
            continue
        found = scheduled_changes(models, cid, offer["seller"])
        if found:
            cur = {k: offer[k] for k in ("seller", "input", "output", "blended")}
            return [{**s, "current": cur} for s in found]
    return []


def iso(t):
    return t.isoformat() if isinstance(t, datetime) else t


def build(extra=None):
    snaps = load_snapshots(extra)
    last_run = json.loads((OUT / "last_run.json").read_text(encoding="utf-8"))
    if extra and datetime.fromisoformat(extra["collected_at"]) > datetime.fromisoformat(last_run["collected_at"]):
        last_run = extra
    now = max(datetime.fromisoformat(last_run["collected_at"]), snaps[-1][0])
    times = [t for t, _, _ in snaps]
    latest_t, latest_models, latest_offs = snaps[-1]

    series = {}          # cid -> seller -> [(t, offer)]
    for t, _, offs in snaps:
        for cid, lst in offs.items():
            if not track.family(cid):
                continue
            for seller, o in best_per_seller(lst).items():
                series.setdefault(cid, {}).setdefault(seller, []).append((t, o))

    models, alerts = [], []
    for cid, by_seller in series.items():
        fam = track.family(cid)
        cur = sorted(best_per_seller(latest_offs.get(cid, [])).values(),
                     key=lambda x: (x["blended"], x["source"] != fam, x["seller"]))
        idx = {t: i for i, t in enumerate(times)}
        sellers = {}
        for seller, pts in by_seller.items():
            row = [None] * len(times)
            for t, o in pts:
                row[idx[t]] = [o["input"], o["output"], o["blended"]]
            sellers[seller] = row
            for e in detect([(t, o["blended"]) for t, o in pts], now):
                alerts.append({"model": cid, "family": fam, "scope": "seller", "seller": seller, **e})

        lowest_pts = []
        for i, t in enumerate(times):
            vals = [(r[i][2], s) for s, r in sellers.items() if r[i]]
            if vals:
                lowest_pts.append((t, min(vals)[0]))
        for e in detect(lowest_pts, now):
            # 판매처 변화가 그대로 최저가 변화인 경우 같은 알림을 두 번 내지 않고 판매처 알림에 표시만 함
            same = next((a for a in alerts if a["model"] == cid and a["scope"] == "seller" and a["at"] == e["at"]
                         and a["old"] == e["old"] and a["new"] == e["new"] and a["status"] == e["status"]), None)
            if same:
                same["affects_lowest"] = True
            else:
                alerts.append({"model": cid, "family": fam, "scope": "lowest", "seller": None,
                               "affects_lowest": True, **e})

        direct = next((x for x in cur if x["source"] == fam), None)
        lo = cur[0] if cur else None
        first = lowest_pts[0][1] if lowest_pts else None
        models.append({
            "id": cid, "family": fam, "active": bool(cur),
            "offers": len(cur), "lowest": lo, "direct": direct,
            "ranking": [{k: x[k] for k in ("seller", "input", "output", "blended", "cache_read", "listed_as", "region")}
                        for x in cur],
            "change_since_first_pct": round((lowest_pts[-1][1] / first - 1) * 100, 2) if first else None,
            "scheduled": scheduled_for(latest_models, cid, lo, direct),
            "sellers": sellers,
        })

    alerts.sort(key=lambda a: a["at"], reverse=True)
    for a in alerts:
        for k in ("at", "until", "confirmed_at"):
            if k in a:
                a[k] = iso(a[k])
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "now": iso(now),
        "times": [iso(t) for t in times],
        "rules": {"min_pct": MIN_PCT, "confirm_hours": CONFIRM_HOURS,
                  "weights": {"input": track.INPUT_WEIGHT, "output": track.OUTPUT_WEIGHT}},
        "run": {k: last_run[k] for k in ("collected_at", "site_count", "success_count")} |
               {"failed": [s["name"] for s in last_run["sites"] if not s["ok"]]},
        "models": sorted(models, key=lambda m: (not m["active"], m["family"], m["id"])),
        "alerts": alerts,
    }


def main():
    # python dashboard.py --with output/prices_<시각>.json  -> 아직 커밋 안 된 로컬 수집본까지 반영
    extra = None
    if "--with" in sys.argv:
        p = Path(sys.argv[sys.argv.index("--with") + 1])
        extra = json.loads(p.read_text(encoding="utf-8"))
        print(f"로컬 수집본 포함: {p.name} ({extra['success_count']}/{extra['site_count']} 사이트)")
    data = build(extra)
    html = TEMPLATE.read_text(encoding="utf-8").replace(
        "/*__DATA__*/null", json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/"))
    (OUT / "dashboard.html").write_text(html, encoding="utf-8")
    n = {s: sum(1 for a in data["alerts"] if a["status"] == s) for s in ("confirmed", "watching", "transient")}
    print(f"대시보드: 모델 {len(data['models'])}개, 확정 {n['confirmed']} / 관찰 중 {n['watching']} / 일시적 {n['transient']}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
