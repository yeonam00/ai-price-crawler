"""CI(--ci) 저장 로직: 가격이 바뀐 실행만 latest.json / 이력(gz)을 갱신해 레포 크기를 억제.

output/
  last_run.json            매 실행: 시각, 성공/실패 현황, 변경 여부 (작은 파일)
  latest.json              가격이 바뀐 마지막 실행의 전체 결과
  history/prices_<시각>.json.gz   가격이 바뀐 실행마다 1개
  .fingerprint             마지막 저장본의 가격 해시
실패한 사이트는 직전 latest.json 의 해당 사이트 데이터를 유지(collected_at 은 옛 값 그대로 -> 오래됨이 드러남).
'성공'이어도 행 수가 직전 저장본의 SHRINK_RATIO 미만으로 급감하면 파싱이 일부만 된 것으로 보고 실패와 똑같이 처리
(예: google_cloud 258 -> 132 행인데 ok 로 기록돼 DeepSeek/Qwen 행이 통째로 사라진 사고). 연속 MAX_DEGRADED_RUNS 번
급감하면 실제로 줄어든 것으로 보고 새 값을 채택.
"""
import gzip
import hashlib
import json
from collections import Counter
from pathlib import Path

SHRINK_RATIO = 0.7        # 직전 저장본 대비 이 비율 미만으로 줄면 파싱 이상 의심
SHRINK_MIN_PREV = 20      # 원래 행이 적은 사이트는 변동폭이 커서 검사 제외
MAX_DEGRADED_RUNS = 4     # 연속 이 횟수까지만 직전 데이터 유지 (6시간 간격 -> 약 하루)


def _fingerprint(models):
    rows = []
    for m in models:
        r = {k: v for k, v in m.items() if not k.startswith("collected_")}
        rows.append(json.dumps(r, ensure_ascii=False, sort_keys=True))
    return hashlib.sha256("\n".join(sorted(rows)).encode("utf-8")).hexdigest()


def save_ci(payload, out_dir: Path, stamp: str):
    out_dir.mkdir(exist_ok=True)
    latest_p = out_dir / "latest.json"
    prev = json.loads(latest_p.read_text(encoding="utf-8")) if latest_p.exists() else {"models": []}

    # 행 수 급감 감지: 성공으로 찍혔어도 직전 저장본보다 크게 줄었으면 실패로 돌려 직전 데이터를 유지
    prev_counts = Counter(m["provider"]["id"] for m in prev.get("models", []))
    last_p = out_dir / "last_run.json"
    prev_streak = {}
    if last_p.exists():
        prev_streak = {s["id"]: s.get("degraded_runs", 0)
                       for s in json.loads(last_p.read_text(encoding="utf-8")).get("sites", [])}
    for s in payload["sites"]:
        was, now_n = prev_counts.get(s["id"], 0), s["model_count"]
        if s["ok"] and was >= SHRINK_MIN_PREV and now_n < was * SHRINK_RATIO:
            streak = prev_streak.get(s["id"], 0) + 1
            if streak <= MAX_DEGRADED_RUNS:
                s["ok"], s["degraded_runs"] = False, streak
                s["error"] = f"행 수 급감 {was} -> {now_n} (파싱 이상 의심), 직전 데이터 유지 ({streak}/{MAX_DEGRADED_RUNS}회)"
                payload["models"] = [m for m in payload["models"] if m["provider"]["id"] != s["id"]]
    payload["success_count"] = sum(s["ok"] for s in payload["sites"])

    # 실패 사이트는 직전 데이터 유지
    failed = {s["id"] for s in payload["sites"] if not s["ok"]}
    carried = [m for m in prev.get("models", []) if m["provider"]["id"] in failed]
    for s in payload["sites"]:
        if not s["ok"]:
            s["carried_over"] = sum(1 for m in carried if m["provider"]["id"] == s["id"])
    payload["models"] = payload["models"] + carried
    payload["model_count"] = len(payload["models"])

    fp = _fingerprint(payload["models"])
    fp_p = out_dir / ".fingerprint"
    old_fp = fp_p.read_text(encoding="utf-8").strip() if fp_p.exists() else ""
    changed = fp != old_fp
    if changed:
        text = json.dumps(payload, ensure_ascii=False, indent=1)
        latest_p.write_text(text, encoding="utf-8")
        (out_dir / "history").mkdir(exist_ok=True)
        with gzip.open(out_dir / "history" / f"prices_{stamp}.json.gz", "wt", encoding="utf-8") as f:
            f.write(text)
        fp_p.write_text(fp, encoding="utf-8")

    last = {k: payload[k] for k in ("collected_at", "site_count", "success_count", "model_count", "sites")}
    last["changed"] = changed
    (out_dir / "last_run.json").write_text(json.dumps(last, ensure_ascii=False, indent=1), encoding="utf-8")
    return changed
