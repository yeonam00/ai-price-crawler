"""가격 추적 대시보드 생성 (output/dashboard.html).

입력 : output/history/prices_*.json.gz   가격이 바뀐 실행마다 1개 (persist.py)
       output/last_run.json              마지막 실행 시각·사이트 성공 현황
출력 : output/dashboard.html             데이터를 내장한 단일 HTML (오프라인으로 열림, Chart.js 만 CDN)

비교 조건은 track.py 와 동일 (USD, 100만 토큰당, standard tier, 기본 컨텍스트, 텍스트, 가중 평균 입력3:출력1).

변화 판정 규칙(확정/일시적/관찰 중)은 changes.py 참고 (track.py 의 변화 로그와 공용).
"""
import gzip
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import track
from changes import CONFIRM_HOURS, MIN_PCT, detect

ROOT = Path(__file__).parent
OUT = ROOT / "output"
TEMPLATE = ROOT / "dashboard_template.html"
# 수집 방식을 바꾼 판매처는 그 이전 이력과 새 값이 같은 기준이 아니라 가짜 '가격 변화'로 잡힘 -> 바꾼 시점 이후만 사용
#   2026-10-27: Eden AI 대표가 -> 연결 업체별 가격(압축 모델 제외), 중국 제조사 공식 페이지 파서 수정,
#               OpenRouter 중국 모델 목록가 -> 업체별 가격(저정밀 양자화 제외)
#   키 = (판매처, 모델 그룹 또는 None=전체)
_CHANGED = datetime.fromisoformat("2026-10-27T12:00:00+00:00")
SELLER_SINCE = {(s, None): _CHANGED for s in ("edenai", "alibaba_qwen", "deepseek", "zai", "minimax", "xiaomi_mimo")}
SELLER_SINCE[("openrouter", "china")] = _CHANGED
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
                since = SELLER_SINCE.get((seller, None)) or SELLER_SINCE.get((seller, track.group(cid)))
                if since and t < since:
                    continue
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

        # 최저가 시계열 + 판매처가 새로 잡히거나 빠져 최저가가 바뀐 시점(가격 변화가 아니므로 기준가 재설정)
        lowest_pts, rebase_at, prev = [], [], None     # prev = (최저가, 판매처, 그때 수집된 판매처 집합)
        for i, t in enumerate(times):
            present = {s for s, r in sellers.items() if r[i]}
            if not present:
                continue
            v, s = min((sellers[x][i][2], x) for x in present)
            lowest_pts.append((t, v))
            if prev and abs(v / prev[0] - 1) * 100 >= MIN_PCT:
                entered, exited = s not in prev[2], prev[1] not in present
                if entered or exited:
                    rebase_at.append(t)
                if entered and v < prev[0]:
                    # 새로 확인된 더 싼 판매처: 시장 가격 변화는 아니지만 조달 후보로 의미 있는 정보
                    alerts.append({"model": cid, "family": fam, "scope": "lowest", "seller": s, "status": "new_seller",
                                   "affects_lowest": True, "at": t, "old": prev[0], "new": v,
                                   "still_lowest": False})
            prev = (v, s, present)
        for e in detect(lowest_pts, now, rebase_at):
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
        for a in alerts:   # '새 판매처' 알림은 그 판매처가 지금도 최저가일 때만 보여줌 (지난 정보로 목록이 쌓이지 않게)
            if a["model"] == cid and a["status"] == "new_seller":
                a["still_lowest"] = bool(lo) and a["seller"] == lo["seller"]
        first = lowest_pts[0][1] if lowest_pts else None
        models.append({
            "id": cid, "family": fam, "group": track.group(cid), "active": bool(cur),
            "offers": len(cur), "lowest": lo, "direct": direct,
            "ranking": [{k: x.get(k) for k in ("seller", "via", "input", "output", "blended", "cache_read", "listed_as", "region")}
                        for x in cur],
            "change_since_first_pct": round((lowest_pts[-1][1] / first - 1) * 100, 2) if first else None,
            "scheduled": scheduled_for(latest_models, cid, lo, direct),
            "sellers": sellers,
        })

    # 반영 검토 대상은 모델 최저가를 바꾼 변화뿐: 최저가가 아닌 판매처만 움직인 변화는 제외
    alerts = [a for a in alerts if a.get("affects_lowest")
              and (a["status"] != "new_seller" or a["still_lowest"])]
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
                  "weights": {"input": track.INPUT_WEIGHT, "output": track.OUTPUT_WEIGHT}, "scenario": track.SCENARIO["name"]},
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
