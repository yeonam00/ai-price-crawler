"""가격 변화 판정 (dashboard.py 와 track.py 가 같은 규칙을 쓰도록 분리).

반영할 변화 판정 (판매처별·모델 최저가별 시계열에 동일 적용)
  - 기준가 = 마지막으로 '확정'된 가격 (직전 수집값이 아니라 마지막 알림값 기준 -> 작은 변화의 누적도 잡힘)
  - 기준가 대비 MIN_PCT 이상 벗어난 상태가 CONFIRM_HOURS 이상 유지되면 '확정' -> 알림, 기준가 갱신
  - 그 전에 기준가 근처로 돌아오면 '일시적' -> 알림하지 않고 기록만
  - 아직 CONFIRM_HOURS 가 지나지 않았으면 '관찰 중'
  - 최저가 시계열에서 판매처가 새로 수집되거나 빠져 최저가가 바뀐 시점(rebase_at)은 가격 변화가 아니라
    비교 대상이 바뀐 것이므로, 그 시점부터 기준가를 새로 잡음 (수집 범위 확대·수집 방식 변경이 '확정'으로 잡히던 문제)
"""
from datetime import timedelta

MIN_PCT = 1.0                       # 이보다 작은 변동은 같은 가격으로 봄
CONFIRM_HOURS = 24                  # 이 시간 이상 유지돼야 반영할 변화로 확정


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


def detect(points, now, rebase_at=()):
    """기준가(마지막 확정값) 대비 변화를 확정/일시적/관찰 중 으로 분류.

    rebase_at: 기준가를 새로 잡을 시각들. 그 시각부터 구간을 나눠 각각 판정하고,
    다음 구간이 시작되며 끊긴 '관찰 중' 변화는 버림.
    """
    cuts = sorted(t for t in set(rebase_at) if points and t > points[0][0])
    if not cuts:
        return _detect(points, now)
    events, seg = [], []
    for t, v in points:
        if seg and cuts and t >= cuts[0]:
            events += [e for e in _detect(seg, t) if e["status"] != "watching"]
            seg = []
            while cuts and t >= cuts[0]:
                cuts.pop(0)
        seg.append((t, v))
    return events + _detect(seg, now)


def _detect(points, now):
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
