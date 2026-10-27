"""closed 모델(OpenAI/Claude/Gemini/Grok)의 모델별 최저가 추적.

입력 : output/latest.json            (crawl.py --ci 가 만든 전체 스냅샷)
출력 : output/lowest_latest.json     모델별 현재 최저가 판매처 + 공식가 + 상위 3개
       output/lowest_changes.jsonl   최저가/판매처가 바뀐 이벤트 누적 (한 줄 = 이벤트 1건)

비교 기준 (같은 조건끼리만 비교)
  - USD, 100만 토큰당, 입력·출력 가격이 모두 있는 행
  - tier = standard(또는 미표기)  -> batch/flex/priority/finetune 제외
  - 기본(짧은) 컨텍스트 구간, 텍스트 모델  -> image/tts/realtime/embedding 등 제외
  - 환율 환산은 하지 않음 -> KRW 등 비 USD 사이트는 자동 제외
  - 최저가 = 가중 평균가 (입력*3 + 출력*1) / 4  (INPUT_WEIGHT 로 조정)

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

OUT = Path(__file__).parent / "output"
INPUT_WEIGHT, OUTPUT_WEIGHT = 3, 1
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
    s = re.sub(r"(?<![\d.])(\d)-(\d)(?=$|-)", r"\1.\2", s)    # v4-1 / k2-5 / glm-5-3 -> v4.1 / k2.5 / glm-5.3
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
    return out


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


def diff(old, new, at):
    ev = []
    for cid, n in new.items():
        o = old.get(cid)
        nl = n["lowest"]
        if o is None:
            ev.append({"at": at, "model": cid, "event": "new_model", "new": nl})
            continue
        ol = o["lowest"]
        if abs(ol["blended"] - nl["blended"]) > 1e-9 or ol["seller"] != nl["seller"]:
            kind = "seller_change" if ol["seller"] != nl["seller"] else "price_change"
            ev.append({"at": at, "model": cid, "event": kind, "old": ol, "new": nl,
                       "blended_change_pct": round((nl["blended"] / ol["blended"] - 1) * 100, 2)})
    for cid, o in old.items():
        if cid not in new:
            ev.append({"at": at, "model": cid, "event": "gone", "old": o["lowest"]})
    return ev


def main():
    latest = json.loads((OUT / "latest.json").read_text(encoding="utf-8"))
    now = datetime.fromisoformat(latest["collected_at"])
    new = build(latest["models"], now)
    lp = OUT / "lowest_latest.json"
    old = json.loads(lp.read_text(encoding="utf-8"))["models"] if lp.exists() else None
    events = diff(old, new, latest["collected_at"]) if old is not None else []
    payload = {"collected_at": latest["collected_at"],
               "weights": {"input": INPUT_WEIGHT, "output": OUTPUT_WEIGHT},
               "model_count": len(new), "models": new}
    lp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    if events:
        with (OUT / "lowest_changes.jsonl").open("a", encoding="utf-8") as f:
            for e in events:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
    print(f"최저가 추적: 모델 {len(new)}개, 이번 변화 {len(events)}건" + (" (최초 기준선)" if old is None else ""))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
