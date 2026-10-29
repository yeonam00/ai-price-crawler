"""closed 모델(OpenAI/Claude/Gemini/Grok)의 모델별 최저가 추적.

입력 : output/latest.json            (crawl.py --ci 가 만든 전체 스냅샷)
출력 : output/lowest_latest.json     모델별 현재 최저가 판매처 + 공식가 + 상위 3개
       output/lowest_series.json     모델별 최저가 시계열 (값이 바뀔 때만 기록) + 판정 상태
       output/price_changes.jsonl    '확정'된 최저가 변화만 누적 (한 줄 = 이벤트 1건). 판정 규칙은 changes.py
       output/lowest_changes.jsonl   (구버전) 수집 때마다 단순 비교한 로그. 판매처 목록이 잠깐 출렁여도 변화로 잡혀
                                     가짜가 많아서 더 이상 쓰지 않음 (2026-10-29 이전 이력만 남아 있음)

비교 기준 (같은 조건끼리만 비교)
  - USD, 100만 토큰당, 입력·출력 가격이 모두 있는 행
  - tier = standard(또는 미표기)  -> batch/flex/priority/finetune 제외
  - 기본(짧은) 컨텍스트 구간, 텍스트 모델  -> image/tts/realtime/embedding 등 제외
  - 환율 환산은 하지 않음 -> KRW 등 비 USD 사이트는 자동 제외
  - 최저가 = 가중 평균가 (입력*3 + 출력*1) / 4 -> 기준 시나리오 SCENARIO 참고

중국 오픈 웨이트 모델(DeepSeek, Qwen, GLM, Kimi, MiniMax, MiMo) 추가 조건 (canonical_china)
  - 스냅샷 날짜까지 같은 버전끼리만 비교 (v4-flash-0731 != v4-flash). 날짜 표기는 MMDD 로 통일
  - 속도 등급(fast/highspeed/ultraspeed), 파생 모델(uncensored, RL 튜닝 등), 저정밀 양자화(fp4/int4/int8/AWQ),
    별칭(deepseek-chat, *-latest)은 제외. fp8/bf16 은 같은 모델로 봄
  - DeepSeek 은 피크 요금이 정가, Qwen 공식가는 Alibaba International 배포 기준 (parsers.py)
"""
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

from changes import CONFIRM_HOURS, detect

OUT = Path(__file__).parent / "output"
# 기준 시나리오: 판매처마다 입력/출력 단가 구조가 달라(입력만 싼 곳, 출력만 싼 곳) 어떤 사용 패턴을 가정하느냐에 따라
# 최저가 판매처가 바뀜 -> 하나로 고정해서 모든 비교·변화 판정에 같은 값을 씀. 바꾸면 과거 이력도 이 값으로 다시 계산됨
#   입력 3 : 출력 1, 캐시 미사용(캐시 단가는 판매처 절반 정도만 공개해 비교가 불공정), 기본 컨텍스트 구간, standard 요금
# 입출력 비율 민감도(2026-10-29, 중국 모델 123개): 1:1 -> 6개, 10:1 -> 5개, 30:1 -> 7개 모델만 최저가 판매처가 달라짐
SCENARIO = {"name": "3:1 입력 중심, 캐시 없음", "input": 3, "output": 1}
INPUT_WEIGHT, OUTPUT_WEIGHT = SCENARIO["input"], SCENARIO["output"]
# 중국 모델 이상 행 방어 (수집 오류·단위 오류가 최저가를 흔드는 것을 막음)
CN_MIN_OUT_IN = 0.5      # 출력 단가가 입력 단가의 절반 미만이면 오류로 봄 (예: streamlake MiniMax-M3 입력 512.0 / 출력 2.4)
CN_MIN_VS_MEDIAN = 0.02  # 같은 모델 다른 판매처 입력 단가 중앙값의 2% 미만이면 단위 오류·프로모션으로 보고 제외
CN_MIN_PEERS = 3         # 중앙값 비교는 판매처가 이 수 이상일 때만
MIN_PLAUSIBLE = 0.005   # USD/1M 입력 토큰. 이보다 싼 값은 단위 오류로 간주
MAX_AGE = timedelta(hours=48)   # 사이트 수집이 계속 실패해 오래 묵은 가격은 비교에서 제외
OK_CTX = {None, "short"}
COUNT_LITELLM = False   # litellm 은 사람이 관리하는 카탈로그라 오래되거나 틀린 가격이 섞임 -> 기본 제외

FAMILIES = {"claude": "anthropic", "gpt": "openai", "chatgpt": "openai", "o": "openai",
            "gemini": "google", "grok": "xai"}
# 기본 텍스트 모델이 아닌 변형 -> 제외 (가격 구조가 달라 같은 선상에서 비교 불가)
_EXCLUDE = re.compile(
    r"image|imagine|tts|transcribe|diarize|realtime|audio|embed|live|whisper|search|computer|"
    r"deep-research|customtools|robotics|translate|moderation|rerank|speech|video|veo|lyria|"
    r"fast|ultrafast|priority|batch|flex|latest|oss|safeguard|cyber|tuning|ft-|finetune|nano-banana|omni|"
    r"think|disc|beta|experimental|long-context|daybreak|-(high|low|medium)$",
    re.I)
_VENDOR_PREFIX = re.compile(r"^(?:[a-z]{2,}\.)*?(?:anthropic|openai|google|xai|meta|amazon)\.")


def canonical(raw):
    """소스별로 제각각인 모델 식별자 -> 'claude-opus-5.5' 같은 공통 id. 대상 아니면 None."""
    s = raw.lower().strip().lstrip("~")
    s = re.sub(r"\([^)]*\)", " ", s)                       # (Public Extended Access ...)
    s = re.split(r"starting |through | – | us/eu|data zone", s)[0]  # 'FlashStarting January 1, 2027' 등 꼬리
    segs = [x for x in re.split(r"/", s) if x]
    pick = None
    for seg in reversed(segs):
        seg = _VENDOR_PREFIX.sub("", seg.strip())
        seg = re.sub(r"^(us|eu|global|apac|jp)\.", "", seg)
        if re.match(r"(claude|gpt|chatgpt|gemini|grok|o[134](?![a-z0-9]))", seg):
            pick = seg
            break
    if not pick:
        return None
    s = pick.replace("_", "-")
    s = re.sub(r"^(claude|gpt|gemini|grok)(?=\d)", r"\1-", s)   # gmicloud 'grok4.6' -> 'grok-4.6'
    if ":" in s:                                            # ':batch' ':free' 등 변형은 제외, ':0' 버전만 허용
        head, tail = s.split(":", 1)
        if not tail.isdigit():
            return None
        s = head
    s = re.sub(r"@.*$", "", s)
    s = re.sub(r"\s+", "-", s.strip())
    s = re.sub(r"-(us|eu|global|regional|datazone)$", "", s)
    s = re.sub(r"-v\d+$", "", s)
    s = re.sub(r"-20\d\d-\d{4}$", "", s)                  # azure: gpt-4o-2024-0513
    s = re.sub(r"-\d{2}-20\d\d$", "", s)                  # gemini: ...-09-2025
    s = re.sub(r"-(0[1-9]|1[0-2])[0-3]\d(?=-|$)", "", s)    # MMDD 스냅샷: gpt-4-0613, grok-4.20-0309-reasoning
    s = re.sub(r"^gpt-35", "gpt-3.5", s)
    s = re.sub(r"-(20\d{6}|20\d\d-\d\d-\d\d)$", "", s)       # 날짜 스냅샷은 기본 모델로 합침
    s = re.sub(r"-(\d{8})$", "", s)
    if _EXCLUDE.search(s):
        return None
    if s.startswith("claude"):
        s = re.sub(r"claude-(\d+(?:[.-]\d+)?)-(opus|sonnet|haiku)", r"claude-\2-\1", s)  # 구형 표기 통일
    if s.startswith(("claude", "grok", "gemini")):
        s = re.sub(r"(\d)-(\d)", r"\1.\2", s)               # claude-opus-4-5 / grok-4-20 -> 4.5 / 4.20
    s = re.sub(r"-+", "-", s).strip("-")
    if not s or s in FAMILIES or not re.search(r"\d", s):    # 'claude' 같은 별칭/세대 불명 제외
        return None
    return s


# ───────── 중국 오픈 웨이트 모델 (같은 모델을 여러 호스팅 업체가 재판매) ─────────
# 값 = 제조사 공식 판매처 site id (direct 판정용). Kimi(Moonshot)는 공식 페이지가 수집 대상에 없음
CHINA_FAMILIES = {"deepseek": "deepseek", "qwen": "alibaba_qwen", "glm": "zai", "kimi": "moonshot",
                  "minimax": "minimax", "mimo": "xiaomi_mimo"}
_CN_KEY = re.compile(r"deepseek|qwen|glm|kimi|minimax|mimo")
# 같은 조건 비교에서 빼는 변형: 속도 등급, 파생(튜닝) 모델, 저정밀 양자화, 별칭, 배치·무료·캐시 할인 행, 비텍스트
_CN_EXCLUDE = re.compile(
    r"\bfast\b|-fast|highspeed|ultraspeed|lightning|express|uncensored|heretic|abliterated|caveman|honey|ponytail|"
    r"-rl\b|fp4|int4|int8|awq|gptq|nvfp4|w4a|latest|e2ee|:free|:batch|batch|context cach|"
    r"embed|rerank|ocr|asr|tts|image|audio|omni|realtime|guard|search")
_CN_ALIAS = {"deepseek-chat", "deepseek-reasoner", "deepseek-flash", "deepseek-pro"}
_LOW_PRECISION = re.compile(r"fp4|int4|int8|awq|gptq|q4|w4")


def canonical_china(raw):
    """'deepseek-ai/DeepSeek-V4.1-Flash-1017', 'deepseek:v4-1-flash', 'Moonshot AI Kimi K2.6' -> 'deepseek-v4.1-flash-1017' 등.

    스냅샷 날짜(0731, 1017 등)는 다른 체크포인트이므로 지우지 않고 MMDD 로만 통일함.
    """
    s = raw.lower().strip().lstrip("~")
    if _CN_EXCLUDE.search(s):
        return None
    s = s.replace("(", "-").replace(")", "")
    seg = next((x for x in reversed(re.split(r"/", s)) if _CN_KEY.search(x)), None)
    if not seg:
        return None
    seg = seg[_CN_KEY.search(seg).start():]                  # 'moonshot ai kimi k2.6' -> 'kimi k2.6'
    s = re.sub(r"[\s_:]+", "-", seg.strip())
    s = re.sub(r"-(instruct|chat-v|ga)(?=-|$)", "", s)
    s = re.sub(r"-(fp8|bf16|fp16)(?=-|$)", "", s)             # 공식 배포 정밀도는 같은 모델로 봄
    s = re.sub(r"-(20\d\d)-?(\d\d)-?(\d\d)(?=-|$)", r"-\2\3", s)   # -2026-06-08 / -20260608 -> -0608
    s = re.sub(r"-2[4-9](\d{4})(?=-|$)", r"-\1", s)          # -260731 -> -0731
    s = re.sub(r"-(0[1-9]|1[0-2])-([0-3]\d)(?=-|$)", r"-\1\2", s)  # -02-23 -> -0223
    s = re.sub(r"^qwen-(?=\d)", "qwen", s)                    # qwen-3-14b -> qwen3-14b
    s = re.sub(r"^minimax-m(\d)(\d)(?=-|$)", r"minimax-m\1.\2", s)   # minimax-m25 -> minimax-m2.5
    s = re.sub(r"(?<![\d.])(\d)-(\d)(?=t?(?:$|-))", r"\1.\2", s)  # v4-1 / k2-5 / glm-5-3 / 2-4t -> v4.1 / k2.5 / glm-5.3 / 2.4t
    s = re.sub(r"^(deepseek|kimi|minimax|mimo|glm)(?=[a-z]?\d)", r"\1-", s)   # glm5.3 -> glm-5.3
    s = re.sub(r"-+", "-", s).strip("-")
    if s in _CN_ALIAS or not re.search(r"\d", s) or (s.startswith("deepseek") and "turbo" in s):
        return None
    return s


def family(cid):
    m = re.match(r"(claude|gpt|chatgpt|gemini|grok|o)", cid)
    if m:
        return FAMILIES[m.group(1)]
    m = _CN_KEY.match(cid)
    return CHINA_FAMILIES[m.group(0)] if m else None


def group(cid):
    return "china" if _CN_KEY.match(cid) else "closed"


def seller_of(m):
    src = m["provider"]["id"]
    if src == "litellm":
        return f"litellm:{(m.get('model_provider') or 'unknown').split('-')[0]}"
    return src


def drop_implausible(offs):
    """중국 모델 한 개(cid)의 판매 행 중 입출력 단가가 말이 안 되는 행을 제외."""
    offs = [o for o in offs if o["output"] >= o["input"] * CN_MIN_OUT_IN]
    if len(offs) >= CN_MIN_PEERS:
        ins = sorted(o["input"] for o in offs)
        med = ins[len(ins) // 2]
        offs = [o for o in offs if o["input"] >= med * CN_MIN_VS_MEDIAN]
    return offs


def offers(models, now):
    out = {}
    for m in models:
        # DeepSeek 'peak'(수정 전 이력) = 정가
        if m.get("unit") != "per_1m_tokens" or m.get("tier") not in (None, "standard", "peak"):
            continue
        if _LOW_PRECISION.search(str((m.get("meta") or {}).get("quantization") or "").lower()):
            continue                                        # 저정밀 양자화 판매분은 같은 모델로 보지 않음
        if m["provider"]["id"] == "litellm" and not COUNT_LITELLM:
            continue
        if m.get("context_tier") not in OK_CTX and not str(m.get("context_tier") or "").startswith("<="):
            continue
        mod = (m.get("modality") or "text").lower()
        if mod != "text":
            continue
        if m["provider"]["id"] == "litellm" and (m.get("meta") or {}).get("mode") not in ("chat", "responses", None):
            continue
        p = m["pricing"]
        i, o = p.get("input"), p.get("output")
        if not i or not o or i["currency"] != "USD" or o["currency"] != "USD":
            continue
        if not i["value"] or not o["value"]:                # 0원(무료 티어)/미표기 제외
            continue
        if i["value"] < MIN_PLAUSIBLE:                      # 1,000자/건당 요금표를 토큰 단가로 잘못 읽은 값 방어
            continue
        age = now - datetime.fromisoformat(m["collected_at"])
        if age > MAX_AGE:
            continue
        raw = m.get("model_id") or m["model"]
        cid = canonical(raw) or canonical_china(raw)
        if not cid:
            continue
        blended = (INPUT_WEIGHT * i["value"] + OUTPUT_WEIGHT * o["value"]) / (INPUT_WEIGHT + OUTPUT_WEIGHT)
        cr = p.get("cache_read")
        up = (m.get("meta") or {}).get("upstream")
        out.setdefault(cid, []).append({
            "via": up if isinstance(up, str) else None,   # 라우터가 실제로 연결하는 업체 (Eden AI, BizRouter)
            "seller": seller_of(m), "source": m["provider"]["id"], "listed_as": m.get("model_id") or m["model"],
            "input": i["value"], "output": o["value"], "cache_read": cr["value"] if cr else None,
            "blended": round(blended, 6), "region": (m.get("meta") or {}).get("region") or (m.get("meta") or {}).get("deployment"),
            "collected_at": m["collected_at"],
        })
    for cid in out:
        if group(cid) == "china":
            out[cid] = drop_implausible(out[cid])
    return {cid: offs for cid, offs in out.items() if offs}


def build(models, now):
    res = {}
    for cid, offs in offers(models, now).items():
        fam = family(cid)
        if not fam:
            continue
        offs.sort(key=lambda x: (x["blended"], x["source"] != fam, x["seller"]))
        direct = next((x for x in offs if x["source"] == fam), None)
        res[cid] = {"family": fam, "group": group(cid), "offers": len(offs), "lowest": offs[0], "direct": direct,
                    "next_best": offs[1:3]}
    return res


MAX_POINTS = 200   # 모델당 시계열 보관 상한 (값이 바뀔 때만 쌓이므로 보통 한참 남음)


def _t(iso):
    return datetime.fromisoformat(iso)


def update_series(state, new, now):
    """이번 수집의 최저가를 시계열 상태에 반영하고, 새로 '확정'된 변화 이벤트를 돌려줌.

    state = {cid: {"points": [[iso, blended, seller]...], "last_seen": iso, "gone": bool, "logged": [at...]}}
    같은 값이 이어지면 점을 추가하지 않음. 한두 번 수집에서만 보였다 사라지는 값은 changes.detect 가 '일시적'으로 걸러냄.
    """
    events, first_run = [], not state
    for cid, n in new.items():
        nl = n["lowest"]
        st = state.get(cid)
        if st is None:
            state[cid] = {"points": [[now.isoformat(), nl["blended"], nl["seller"]]], "last_seen": now.isoformat(),
                          "gone": False, "logged": []}
            if not first_run:
                events.append({"at": now.isoformat(), "model": cid, "event": "new_model", "new": nl})
            continue
        st["last_seen"], st["gone"] = now.isoformat(), False
        last = st["points"][-1]
        if abs(last[1] / nl["blended"] - 1) * 100 >= 1e-6:
            st["points"].append([now.isoformat(), nl["blended"], nl["seller"]])
            del st["points"][:-MAX_POINTS]
    hold = timedelta(hours=CONFIRM_HOURS)
    for cid, st in state.items():
        pts = [(_t(t), v) for t, v, _ in st["points"]]
        for e in detect(pts, now):
            at = e["at"].isoformat()
            if e["status"] != "confirmed" or at in st["logged"]:
                continue
            prev = [p for p in st["points"] if _t(p[0]) < e["at"]]
            sellers = {p[0]: p[2] for p in st["points"]}
            events.append({
                "at": at, "confirmed_at": e["confirmed_at"].isoformat(), "model": cid,
                "event": "price_change", "old": e["old"], "new": e["new"],
                "change_pct": round((e["new"] / e["old"] - 1) * 100, 2),
                "old_seller": prev[-1][2] if prev else None, "new_seller": sellers.get(at),
            })
            st["logged"].append(at)
            del st["logged"][:-MAX_POINTS]
        # 목록에서 사라진 모델도 CONFIRM_HOURS 이상 안 보일 때만 '삭제'로 확정 (사이트 일시 장애·파싱 누락 구분)
        if cid not in new and not st["gone"] and now - _t(st["last_seen"]) >= hold:
            st["gone"] = True
            events.append({"at": st["last_seen"], "confirmed_at": now.isoformat(), "model": cid, "event": "gone",
                           "old": st["points"][-1][1], "old_seller": st["points"][-1][2]})
    return events


def main():
    latest = json.loads((OUT / "latest.json").read_text(encoding="utf-8"))
    now = _t(latest["collected_at"])
    lr = OUT / "last_run.json"
    if lr.exists():                              # 변동 없는 실행은 latest.json 이 갱신되지 않으므로 실제 마지막 실행 시각을 사용
        now = max(now, _t(json.loads(lr.read_text(encoding="utf-8"))["collected_at"]))
    new = build(latest["models"], now)
    sp = OUT / "lowest_series.json"
    state = json.loads(sp.read_text(encoding="utf-8")) if sp.exists() else {}
    baseline = not state
    events = update_series(state, new, now)
    sp.write_text(json.dumps(state, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    payload = {"collected_at": latest["collected_at"],
               "scenario": SCENARIO, "weights": {"input": INPUT_WEIGHT, "output": OUTPUT_WEIGHT},
               "model_count": len(new), "models": new}
    (OUT / "lowest_latest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    if events:
        with (OUT / "price_changes.jsonl").open("a", encoding="utf-8") as f:
            for e in events:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
    print(f"최저가 추적: 모델 {len(new)}개, 확정된 변화 {len(events)}건" + (" (최초 기준선)" if baseline else ""))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
