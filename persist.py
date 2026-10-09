"""CI(--ci) 저장 로직: 가격이 바뀐 실행만 latest.json / 이력(gz)을 갱신해 레포 크기를 억제.

output/
  last_run.json            매 실행: 시각, 성공/실패 현황, 변경 여부 (작은 파일)
  latest.json              가격이 바뀐 마지막 실행의 전체 결과
  history/prices_<시각>.json.gz   가격이 바뀐 실행마다 1개
  .fingerprint             마지막 저장본의 가격 해시
실패한 사이트는 직전 latest.json 의 해당 사이트 데이터를 유지(collected_at 은 옛 값 그대로 -> 오래됨이 드러남).
"""
import gzip
import hashlib
import json
from pathlib import Path


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
