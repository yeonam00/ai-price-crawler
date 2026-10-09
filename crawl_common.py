"""크롤러/파서 공용: 가격 문자열 파싱, 헤더 분류."""
import re


# ───────────────────────── 가격 파싱 ─────────────────────────
_CUR = {"$": "USD", "US$": "USD", "USD": "USD", "€": "EUR", "£": "GBP", "¥": "CNY", "￥": "CNY",
        "RMB": "CNY", "CNY": "CNY", "₩": "KRW", "KRW": "KRW", "원": "KRW"}
_NUM = re.compile(r"(US\$|USD|RMB|CNY|KRW|[$€£¥￥₩])?\s*(\d[\d,]*(?:\.\d+)?)\s*(원)?", re.I)


def parse_price(text, default_cur):
    """'$3.00 / MTok' -> {'raw','value','currency'}. 숫자가 없으면 None."""
    if text is None:
        return None
    text = str(text).strip()
    if not text or text.lower() in {"-", "—", "n/a", "na", "free", "무료"}:
        return {"raw": text, "value": 0.0 if text.lower() in {"free", "무료"} else None, "currency": default_cur} if text else None
    m = _NUM.search(text)
    if not m:
        return {"raw": text, "value": None, "currency": default_cur}
    cur = m.group(1) or ("원" if m.group(3) else None)
    return {"raw": text, "value": float(m.group(2).replace(",", "")),
            "currency": _CUR.get(cur.upper() if cur and cur.isalpha() else cur, default_cur)}


def detect_unit(text):
    t = (text or "").lower()
    if re.search(r"1\s*m\b|million|/\s*mtok|per\s*m\b|백만|百万", t):
        return "per_1m_tokens"
    if re.search(r"1\s*k\b|thousand|/\s*ktok|천", t):
        return "per_1k_tokens"
    if re.search(r"second|/\s*s\b|초", t):
        return "per_second"
    if re.search(r"image|이미지|/\s*img", t):
        return "per_image"
    if re.search(r"minute|/\s*min|분", t):
        return "per_minute"
    if re.search(r"request|요청|call", t):
        return "per_request"
    return None


# ───────────────────────── 표 헤더 → 필드 매핑 ─────────────────────────
def classify_header(h, ctx=""):
    s = h.lower().strip()
    if not s:
        return None
    c = (ctx or "").lower()
    batch = "batch" in s or "배치" in s
    if re.search(r"^(model|models|name|model name|model id|모델|모델명|이름|api model|서비스)\b|model$", s) and "price" not in s:
        return "model"
    if re.search(r"context|컨텍스트|max.*(token|output)|window|modal|capab|description|설명|release|date|parameters|size", s):
        return "meta:" + re.sub(r"\W+", "_", s).strip("_")
    cache = re.search(r"cache|캐시|cached", s)
    if cache:
        if re.search(r"5\s*m|5분", s):
            return "cache_write_5m"
        if re.search(r"1\s*h|1시간", s):
            return "cache_write_1h"
        if re.search(r"read|hit|refresh|input|읽기|적중|조회", s) and not re.search(r"write|creation|miss", s):
            return "cache_read"
        if re.search(r"write|creation|miss|쓰기|생성|storage|저장", s):
            return "cache_write"
        return "cache_read"
    if re.search(r"output|completion|출력|response|generation", s):
        return "batch_output" if batch or "batch" in c else "output"
    if re.search(r"input|prompt|입력|request tokens", s):
        return "batch_input" if batch or "batch" in c else "input"
    if re.search(r"per image|/\s*image|이미지당", s):
        return "per_image"
    if re.search(r"per second|/\s*s\b|초당", s):
        return "per_second"
    if re.search(r"per minute|/\s*min|분당", s):
        return "per_minute"
    if re.search(r"per request|request|요청당|/\s*call", s):
        return "per_request"
    if re.search(r"price|pricing|cost|가격|요금|비용|rate", s):
        return "price_generic"
    return "meta:" + re.sub(r"\W+", "_", s).strip("_")[:40]


def tier_from(text):
    t = (text or "").lower()
    for key in ("batch", "flex", "priority", "scale", "standard", "realtime", "fine-tun", "finetun", "embedding"):
        if key in t:
            return key
    return None


