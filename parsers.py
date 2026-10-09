"""사이트별 전용 파서.

각 파서: parse_<id>(data, site) -> list[record]
    data = {"tables": [{kind, heading, tab, rows}], "text": str, "title": str}
    record = {model, pricing{field: price}, unit, tier, section, meta, [context_tier, modality, model_id, model_provider]}

price = {"raw", "value", "currency", ["variants": [{"value","label","unit"}]]}
    value    = 칸에 적힌 첫 번째(대표) 가격. 프로모션이면 '현재 적용 중인' 가격이 먼저 오는 사이트가 많음.
    variants = 한 칸에 여러 가격이 있을 때(모달리티별, 프롬프트 길이별, 기간별) 전부.
"""
import re

from crawl_common import classify_header, detect_unit

_PRICE_RE = re.compile(r"(?:(US\$|\$|€|£|¥|￥|₩|RMB|CNY|KRW)\s*(\d[\d,]*(?:\.\d+)?)"
                       r"|(\d[\d,]*(?:\.\d+)?)\s*(USD|KRW|원|CNY|RMB|EUR|달러))")
_BARE_RE = re.compile(r"^\s*(\d[\d,]*(?:\.\d+)?)\s*$")
_SYMS = {"$": "USD", "US$": "USD", "€": "EUR", "£": "GBP", "¥": "CNY", "￥": "CNY", "₩": "KRW",
         "RMB": "CNY", "CNY": "CNY", "KRW": "KRW", "USD": "USD", "EUR": "EUR", "원": "KRW", "달러": "USD"}
_EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿-]")
_NONE = {"", "-", "—", "–", "--", "n/a", "na", "not available", "not available**", "unavailable", "사용 불가"}


def clean(s):
    return re.sub(r"\s+", " ", _EMOJI.sub("", s or "")).strip()


def _variant_unit(s):
    s = s.lower()
    if re.search(r"per\s*\d*\s*k?\s*image|/\s*(image|img)", s):
        return "per_image"
    if re.search(r"/\s*min|per\s*minute", s):
        return "per_minute"
    if re.search(r"/\s*s(ec)?\b|per\s*(\d+\s*)?s(ec|econd)?s?\b|per second", s):
        return "per_second"
    if re.search(r"per hour|/\s*hour|per session-hour", s):
        return "per_hour"
    return None


def make_price(text, cur="USD"):
    """칸 텍스트 -> price dict (없으면 None). 여러 가격이면 variants 포함."""
    t = clean(text)
    if t.lower() in _NONE:
        return None
    if t.lower() in {"free", "free of charge", "무료"}:
        return {"raw": t, "value": 0.0, "currency": cur}
    ms = list(_PRICE_RE.finditer(t))
    if not ms:
        b = _BARE_RE.match(t)
        return {"raw": t, "value": float(b.group(1).replace(",", "")), "currency": cur} if b else None
    variants = []
    for i, m in enumerate(ms):
        end = ms[i + 1].start() if i + 1 < len(ms) else len(t)
        after = t[m.end():end].strip(" ,.;")
        before = t[ms[i - 1].end():m.start()].strip(" ,.;") if i else ""
        label = after or before
        v = {"value": float((m.group(2) or m.group(3)).replace(",", "")), "label": label[:120] or None}
        u = _variant_unit(after[:30])
        if u:
            v["unit"] = u
        variants.append(v)
    out = {"raw": t[:400], "value": variants[0]["value"], "currency": _SYMS.get(ms[0].group(1) or ms[0].group(4), cur)}
    if len(variants) > 1:
        out["variants"] = variants
    return out


def split_model(name):
    """'Claude Fable 5.1For demanding...' -> ('Claude Fable 5.1', 'For demanding...')"""
    n = clean(name)
    if "PROMO" in n:
        i = n.index("PROMO")
        return n[:i].strip(), n[i:]
    m = re.search(r"(?<=[a-z0-9])(Long context|Text, |Text \u2192)", n)  # 'grok-4.3Long context ≥ 200k'
    if m and m.start() > 2:
        return n[:m.start()].strip(), n[m.start():]
    m = re.match(r"^(.*?\d)(?=[A-Z][a-z]{2,})", n)
    if m and re.search(r"\d[A-Z][a-z]", n) and len(n) > len(m.group(1)) + 8:
        return m.group(1), n[len(m.group(1)):]
    return n, None


def fix_model(name):
    return split_model(name)[0]


def header_rows(rows):
    """(그룹헤더 or None, 컬럼헤더, 데이터행들). 그룹헤더 = 인접 동일 셀이 있고 다음 행에 가격이 없는 첫 행."""
    rows = [r for r in rows if any(c.strip() for c in r)]
    if len(rows) < 2:
        return None, None, []
    first, second = rows[0], rows[1]
    dup = any(a and a == b for a, b in zip(first, first[1:]))
    if dup and len(first) == len(second) and not any(_PRICE_RE.search(c) for c in second):
        return first, second, rows[2:]
    return None, first, rows[1:]


FIELD_BY_WORDS = [
    (r"5\s*m\b.*writ|writ.*5\s*m\b|5.?minute.*(cache|writ)", "cache_write_5m"),
    (r"1\s*h\b.*writ|writ.*1\s*h\b|1.?hour.*(cache|writ)", "cache_write_1h"),
]


def hclass(group, col):
    """그룹+컬럼 헤더 -> (field, tier_override, context_tier)"""
    g, c = (group or "").lower(), (col or "").lower()
    if re.search(r"(^|\b)models?$|^model name$|^inference option$|^모델명?$|^provider model", c):
        return "model", None, None
    if re.search(r"supervised|\bsft\b", c):
        return "finetune_sft", None, None
    if re.search(r"preference|\bdpo\b", c):
        return "finetune_dpo", None, None
    if re.search(r"minimum charge", c):
        return "minimum_charge", None, None
    if re.search(r"per mp|megapixel", c):
        return "per_megapixel", None, None
    ctx_tier = "short" if "short" in g else "long" if "long" in g else None
    tier = "batch" if "batch" in g or "batch" in c else None
    g_clean = re.sub(r"short|long|context|batch|tokens?", "", g)
    full = f"{g_clean} {c}".strip()
    for pat, f in FIELD_BY_WORDS:
        if re.search(pat, full):
            return f, tier, ctx_tier
    if re.search(r"\b(writes?|hits?|refresh)", full) and "cache" not in full:
        full = "cache " + full
    f = classify_header(full.replace("batch", ""), "")
    return f, tier, ctx_tier


def split_cached(p):
    """variants 중 라벨에 'cache'가 있는 값을 분리 -> (본 가격, 캐시 가격|None)"""
    vs = p.get("variants")
    if not vs:
        return p, None
    cached = [v for v in vs if "cache" in (v.get("label") or "").lower()]
    rest = [v for v in vs if v not in cached]
    if not cached or not rest:
        return p, None
    main = dict(p, value=rest[0]["value"])
    main.pop("variants", None)
    if len(rest) > 1:
        main["variants"] = rest
    return main, {"raw": p["raw"], "value": cached[0]["value"], "currency": p["currency"]}


def parse_wide(tbl, site, tier=None, section=None, default_unit=None, modality=None):
    """모델이 행, 가격 종류가 열인 표(그룹 헤더 지원). 모델명이 빈 행은 직전 모델을 이어받음(모달리티 행)."""
    groups, cols, data = header_rows(tbl["rows"])
    if not cols:
        return []
    groups = groups or [""] * len(cols)
    specs = [hclass(g, c) for g, c in zip(groups, cols)]
    model_idx = next((i for i, s in enumerate(specs) if s[0] == "model"), None)
    if model_idx is None:
        return []
    table_tier = tier
    colstr = " ".join(cols)
    if re.search(r"per (minute|second|image|request|hour)|/\s*(min|s)\b", colstr, re.I):
        unit = detect_unit(colstr)
    elif default_unit:
        unit = default_unit
    elif re.search(r"1m\b|1 million|1,000,000|백만", colstr, re.I):
        unit = "per_1m_tokens"
    elif re.search(r"1,?000 (input |output )?tokens", colstr, re.I):
        unit = "per_1k_tokens"
    else:
        unit = None
    out, prev_model = [], None
    for r in data:
        r = list(r)
        if len(r) == len(cols) - 1 and model_idx == 0 and prev_model:
            r = [prev_model] + r
        if len(r) < len(cols) - 1:
            continue
        meta = {}
        model, model_note = split_model(r[model_idx]) if model_idx < len(r) else ("", None)
        if not model:
            continue
        if model_note:
            meta["model_note"] = model_note[:200]
        prev_model = r[model_idx]
        multi_ctx = {}
        for i, (f, t_over, ctx) in enumerate(specs):
            if i >= len(r) or i == model_idx or not f:
                continue
            cell = r[i]
            if f.startswith("meta:"):
                if clean(cell):
                    k = f[5:]
                    if k in {"modality", "use_case", "category", "training"}:
                        meta[k] = clean(cell)
                    else:
                        meta[k] = clean(cell)
                continue
            p = make_price(cell, site["currency"])
            if not p:
                continue
            key = f
            if t_over == "batch" and table_tier != "batch" and f in ("input", "output", "cache_read", "cache_write"):
                key = "batch_" + f
            if key == "input":  # "$0.30$0.06 (cached)" -> input + cache_read
                p, cached = split_cached(p)
                if cached:
                    multi_ctx.setdefault(ctx, {}).setdefault("cache_read", cached)
            if f == "price_generic":
                u = detect_unit(cell) or detect_unit(cols[i])
                key = {"per_minute": "per_minute", "per_second": "per_second", "per_image": "per_image",
                       "per_request": "per_request"}.get(u, "price")
            multi_ctx.setdefault(ctx, {}).setdefault(key, p)
        rec_unit = unit
        if rec_unit is None:
            first = next((ps["output"] for ps in multi_ctx.values() if "output" in ps), None) or \
                next((pp for ps in multi_ctx.values() for pp in ps.values()), None)
            rec_unit = _variant_unit(first["raw"]) if first else None
        meta_modality = modality or meta.pop("modality", None)
        provider = meta.pop("provider", None)
        for ctx, prices in multi_ctx.items():
            if not prices:
                continue
            rec = {"model": model, "pricing": prices, "unit": rec_unit, "tier": table_tier,
                   "section": section or tbl.get("heading") or None, "context_tier": ctx,
                   "modality": meta_modality, "meta": dict(meta) or None}
            if provider:
                rec["model_provider"] = provider
            out.append(rec)
    return out


def fold_batch(records):
    """tier=batch 레코드를 같은 (모델, context_tier, modality)의 표준 레코드에 batch_* 로 합침."""
    key = lambda r: (r["model"], r.get("context_tier"), r.get("modality"))
    std = {}
    for r in records:  # 같은 모델이 여러 행(예: 프롬프트 길이별)이면 등장 순서대로 짝지음
        if r.get("tier") in (None, "standard"):
            std.setdefault(key(r), []).append(r)
    seen, out = {}, []
    for r in records:
        if r.get("tier") == "batch":
            k = key(r)
            i = seen.get(k, 0)
            seen[k] = i + 1
            if i < len(std.get(k, [])):
                for f, pr in r["pricing"].items():
                    if "storage" not in f:
                        std[k][i]["pricing"][f"batch_{f}"] = pr
                continue
        out.append(r)
    return out


# ───────────────────────── Anthropic (claude) ─────────────────────────
def parse_anthropic(data, site):
    recs = []
    for t in data["tables"]:
        h = (t["heading"] or "").lower()
        if h.startswith(("model pricing", "fast mode", "batch processing")):
            tier = "batch" if h.startswith("batch") else "fast" if h.startswith("fast") else "standard"
            recs += parse_wide(t, site, tier=tier, default_unit="per_1m_tokens")
    for r in recs:  # '$0.10 / MTok for prompts up to 100,000 tokens' 류 조건은 variants/raw에 유지
        r["section"] = clean(re.sub(r"\s*$", "", r["section"] or ""))
    return fold_batch(recs)


# ───────────────────────── OpenAI ─────────────────────────
_TIER_WORDS = r"(?:Standard|Batch|Flex|Priority|Fast|Ultrafast)"


def _tab_labels(text, heading, n_tables):
    pat = re.escape(heading) + r".{0,300}?((?:\b" + _TIER_WORDS + r"\b\s*){2,})"
    for m in re.finditer(pat, text):
        labels = [w.lower() for w in re.findall(_TIER_WORDS, m.group(1))]
        if len(labels) == n_tables:
            return labels
    return []


def parse_openai(data, site):
    recs, text = [], data["text"]
    by_heading = {}
    for t in data["tables"]:
        by_heading.setdefault(t["heading"], []).append(t)
    for heading, tabs in by_heading.items():
        labels = _tab_labels(text, heading, len(tabs))
        for i, t in enumerate(tabs):
            tier = labels[i] if len(labels) == len(tabs) else (None if len(tabs) == 1 else f"variant_{i + 1}")
            if heading == "Tools":
                for r in t["rows"][1:]:
                    if len(r) >= 3 and (p := make_price(r[-1], site["currency"])):
                        name = clean(r[1]) if clean(r[1]).lower().startswith(clean(r[0]).lower()) else clean(r[0] + " " + r[1])
                        recs.append({"model": name,
                                     "pricing": {"per_request": p},
                                     "unit": "per_1k_calls" if "1k calls" in r[-1].lower() else None,
                                     "tier": "tool", "section": heading, "meta": None})
                continue
            if heading == "Finetuning":
                tier = "finetune" if len(tabs) == 1 else f"finetune_{i + 1}"
            rs = parse_wide(t, site, tier=tier or "standard", section=heading, default_unit="per_1m_tokens")
            for r in rs:
                m = re.match(r"^(.*?)\s*with data sharing$", r["model"])
                if m:
                    r["model"] = m.group(1)
                    r["meta"] = {**(r["meta"] or {}), "data_sharing": True}
            recs += rs
    return fold_batch(recs)


# ───────────────────────── Google (Gemini API) ─────────────────────────
_G_TIER = {"standard": "standard", "batch": "batch", "flex": "flex", "priority": "priority",
           "표준": "standard", "일괄": "batch", "우선순위": "priority"}


def parse_google(data, site):
    recs = []
    for t in data["tables"]:
        heading = clean(t["heading"])
        if not heading or heading.startswith("Pricing for") or len(t["rows"]) < 2:
            continue
        head = [c.lower() for c in t["rows"][0]]
        paid = next((i for i, c in enumerate(head) if "paid" in c), None)
        if paid is None:
            continue
        unit = detect_unit(t["rows"][0][paid]) or "per_1m_tokens"
        tier = _G_TIER.get(t.get("tab", "").lower(), "standard") if t.get("tab") else "standard"
        pricing = {}
        for r in t["rows"][1:]:
            if len(r) <= paid:
                continue
            label = r[0].lower()
            cell = r[paid]
            if "storage price" in cell:  # 캐시 보관료는 별도 키로 분리
                pat = r"\$[\d.]+\s*/\s*1,000,000 tokens per hour \(storage price\)"
                ms = re.findall(pat, cell)
                if ms:
                    pricing["cache_storage_per_1m_tokens_hour"] = make_price(ms[0], site["currency"])
                    cell = re.sub(pat, " ", cell)
            cell = re.sub(r"^Same as Standard\s*", "", cell)
            if label.startswith("input"):
                key = "input"
            elif label.startswith("output"):
                key = "output"
            elif label.startswith("context caching"):
                key = "cache_read"
            elif label.startswith(("grounding", "used to improve", "image", "video", "audio")) and unit == "per_1m_tokens":
                continue
            else:
                key = {"per_request": "per_request", "per_second": "per_second"}.get(unit, re.sub(r"\W+", "_", label)[:30])
            p = make_price(cell, site["currency"])
            if p:
                pricing[key] = p
        if pricing:
            recs.append({"model": heading, "pricing": pricing, "unit": unit, "tier": tier,
                         "section": heading, "meta": None})
    return fold_batch(recs)


# ───────────────────────── Bedrock ─────────────────────────
_B_TIERS = {"standard", "priority", "flex", "batch", "reserved"}
_B_KEEP = {"input", "output", "cache_read", "cache_write", "batch_input", "batch_output",
           "batch_cache_read", "batch_cache_write"}


def parse_bedrock(data, site):
    recs = []
    for t in data["tables"]:
        parts = [x.strip() for x in (t.get("tab") or "").split(">")]
        if not parts or not parts[0].lower().startswith("model pricing"):
            continue
        last = parts[-1].lower()
        tier = last if last in _B_TIERS else ("customization" if "customization" in last else "standard")
        first_hdr = t["rows"][0][0] if t["rows"] and t["rows"][0] else ""
        mm = re.match(r"^(.*?)\s+models?$", first_hdr, re.I)
        provider = parts[1] if len(parts) > 1 else (mm.group(1) if mm else None)
        section = " > ".join(parts[1:]) or t.get("heading")
        for r in parse_wide(t, site, tier=tier, section=section):
            if not (set(r["pricing"]) & _B_KEEP):
                continue
            r.setdefault("model_provider", provider)
            recs.append(r)
    return fold_batch(recs)


# ───────────────────────── DeepSeek (모델이 열인 전치 표) ─────────────────────────
def parse_deepseek(data, site):
    if not data["tables"]:
        return []
    rows = max(data["tables"], key=lambda t: len(t["rows"]))["rows"]
    hdr = rows[0]
    k = next((i for i, c in enumerate(hdr) if c != hdr[0]), None)
    if k is None:
        return []
    names = [re.sub(r"\s*\(\d+\)$", "", c).strip() for c in hdr[k:]]
    meta = {n: {} for n in names}
    by = {}  # (model, tier) -> pricing
    for r in rows[1:]:
        if len(r) < k + len(names):
            continue
        label, sub = r[0].upper(), r[1].upper()
        if label.startswith("PRICING"):
            kind = sub
            peak = r[2].upper() if r[2].upper() != r[1].upper() else ""
            field = ("cache_read" if "CACHE HIT" in kind else "input" if "INPUT" in kind
                     else "output" if "OUTPUT" in kind else None)
            if not field:
                continue
            tier = {"OFF-PEAK": "off_peak", "PEAK": "peak"}.get(peak, "standard")
            for n, cell in zip(names, r[k:k + len(names)]):
                p = make_price(cell, site["currency"])
                if p:
                    by.setdefault((n, tier), {})[field] = p
        elif label not in {"MODEL", "FEATURES"} or sub in {"VISION"}:
            for n, cell in zip(names, r[k:k + len(names)]):
                key = re.sub(r"\W+", "_", (r[0] if r[0] == r[1] else r[1]).lower()).strip("_")
                meta[n][key] = clean(cell)[:80]
    return [{"model": n, "pricing": pr, "unit": "per_1m_tokens", "tier": tier, "section": "Model Details",
             "meta": meta[n] or None} for (n, tier), pr in by.items()]


# ───────────────────────── Together ─────────────────────────
_T_TOKEN_TABS = {"chat", "embeddings", "rerank", "code", "moderation", "language", "vision", ""}


def parse_together(data, site):
    recs = []
    for t in data["tables"]:
        h, tab = t["heading"], (t.get("tab") or "")
        if h == "Serverless Inference":
            unit = "per_1m_tokens" if tab.lower() in _T_TOKEN_TABS else None
            recs += parse_wide(t, site, tier="standard", section=f"{h} > {tab}" if tab else h,
                               default_unit=unit, modality=tab.lower() or None)
        elif h == "Fine-Tuning":
            recs += parse_wide(t, site, tier="finetune", section=f"{h} > {tab}" if tab else h,
                               default_unit="per_1m_tokens", modality=tab.lower() or None)
    return recs


# ───────────────────────── 가비아 AI Hub (원화) ─────────────────────────
def parse_gabia(data, site):
    recs = []
    for t in data["tables"]:
        if "모델" not in (t["heading"] or "") and "모델명" not in " ".join(t["rows"][0]):
            continue
        for r in t["rows"][1:]:
            if len(r) < 7:
                continue
            name, *tags = [x.strip() for x in r[0].split("#")]
            prices = {k: make_price(c, "KRW") for k, c in
                      zip(("input", "output", "cache_read", "cache_write"), r[3:7])}
            prices = {k: v for k, v in prices.items() if v}
            if not name or not prices:
                continue
            recs.append({"model": name, "pricing": prices, "unit": "per_1m_tokens", "tier": "standard",
                         "section": t["heading"],
                         "meta": {"max_input_tokens": r[1], "max_output_tokens": r[2], "tags": tags or None}})
    return recs


# ───────────────────────── Google Cloud (Vertex AI) : 모델/유형이 행, 컨텍스트 구간이 열 ─────────────────────────
def _mods(t):
    return {m for m in ("text", "image", "video", "audio") if m in t}


def type_field(type_text):
    """행의 'Type' 셀 -> 가격 필드명 (해당 없으면 None)"""
    t = type_text.lower()
    t = re.sub(r"\[\d+\]|\*+", "", t)
    batch = "batch" in t
    if re.search(r"tuning|training|storage", t):
        return None
    if re.search(r"5\s*m.*cache write", t):
        f = "cache_write_5m"
    elif re.search(r"1\s*h.*cache write", t):
        f = "cache_write_1h"
    elif "cache write" in t:
        f = "cache_write"
    elif re.search(r"cache hit|cached", t):
        f = "cache_read"
    elif "output" in t:
        m = _mods(t.split("output")[0] + " " + t.split("output")[-1])
        f = "output" if (not m or "text" in m or len(m) >= 3) else "output_" + "_".join(sorted(m))
    elif "input" in t:
        m = _mods(t)
        f = "input" if (not m or "text" in m or len(m) >= 3) else "input_" + "_".join(sorted(m))
    else:
        return None
    return "batch_" + f if batch and not f.startswith("batch") else f


_GC_SKIP_HEAD = re.compile(r"tuning|storage|provisioned|alphaevolve|codemender|embedding|translation|code completion", re.I)


def _ctx_label(h):
    h = h.lower().replace("=<", "<=")
    m = re.search(r"(<=|>)\s*(\d+)\s*k", h)
    return f"{m.group(1)}{m.group(2)}K" if m else None


def parse_google_cloud(data, site):
    recs = []
    for t in data["tables"]:
        rows = t["rows"]
        if len(rows) < 2 or _GC_SKIP_HEAD.search(t["heading"] or ""):
            continue
        hdr = rows[0]
        low = [h.lower() for h in hdr]
        mi = next((i for i, h in enumerate(low) if h.startswith("model") or h.endswith("model")), None)
        ti = next((i for i, h in enumerate(low) if h == "type"), None)
        ri = next((i for i, h in enumerate(low) if h == "region"), None)
        if mi is None or ti is None or any("request type" in h for h in low):
            continue
        tab = (t.get("tab") or "").strip()
        tl = tab.lower()
        if "modality" in tl:
            continue
        if tl in ("", "standard", "standard model", "token-based pricing"):
            tier, tab_region = "standard", None
        elif tl == "priority":
            tier, tab_region = "priority", None
        elif "flex" in tl or "batch" in tl:
            tier, tab_region = "flex_batch", None
        else:  # Claude 표는 탭이 리전
            tier, tab_region = "standard", tab
        pcols = []
        for i, h in enumerate(hdr):
            hl = h.lower()
            if i in (mi, ti, ri) or not re.search(r"price|token", hl) or "description" in hl:
                continue
            pcols.append((i, _ctx_label(h), "cached" in hl or "chached" in hl, "batch" in hl))
        groups, order, prev_model = {}, [], None
        for r in rows[1:]:
            if len(r) < len(hdr):
                continue
            if r[mi].strip():
                prev_model = re.sub(r"\s*\[\d+\]|\*.*$", "", r[mi]).strip()
            f = type_field(r[ti])
            if not prev_model or not f:
                continue
            region = (r[ri].strip() if ri is not None and r[ri].strip() else None) or tab_region
            for i, ctx, cached, batch in pcols:
                cell = r[i]
                if cell.strip().lower() in ("n/a", "na", "no charge", ""):
                    continue
                pr = make_price(cell, site["currency"])
                if not pr:
                    continue
                if not (re.search(r"1m|1,000,000", hdr[i], re.I) or re.search(r"1m tokens", cell, re.I)
                        or "token" in hdr[i].lower()):
                    continue
                field = "cache_read" if cached else f
                if batch and not field.startswith("batch_"):
                    field = "batch_" + field
                key = (prev_model, region, ctx)
                if key not in groups:
                    groups[key] = {}
                    order.append(key)
                groups[key].setdefault(field, pr)
        for key in order:
            model, region, ctx = key
            recs.append({"model": model, "pricing": groups[key], "unit": "per_1m_tokens", "tier": tier,
                         "section": t["heading"], "context_tier": ctx,
                         "meta": {"region": region} if region else None})
    return recs


# ───────────────────────── xAI (Grok) ─────────────────────────
def parse_xai(data, site):
    recs = []
    for t in data["tables"]:
        if t["heading"] == "Text API":
            recs += parse_wide(t, site, tier="standard", section=t["heading"], default_unit="per_1m_tokens")
        elif t["heading"] == "Imagine API":
            recs += parse_wide(t, site, tier="standard", section=t["heading"])
    return recs


# ───────────────────────── Azure OpenAI ─────────────────────────
_AZ_LABEL = re.compile(r"(Cached Input|Cache(?:d)? Write|Input|Output)\s*:\s*\$\s*([\d.,]+)", re.I)
_AZ_FIELD = {"cached input": "cache_read", "cache write": "cache_write", "cached write": "cache_write",
             "input": "input", "output": "output"}


def _az_prices(cell, cur):
    out = {}
    for m in _AZ_LABEL.finditer(cell):
        f = _AZ_FIELD[m.group(1).lower()]
        out.setdefault(f, {"raw": m.group(0), "value": float(m.group(2).replace(",", "").rstrip(".")), "currency": cur})
    return out


def parse_azure(data, site):
    recs = []
    for t in data["tables"]:
        rows = t["rows"]
        if len(rows) < 2 or not rows[0] or rows[0][0].strip().lower() != "model":
            continue
        hdr = [h.lower() for h in rows[0]]
        for r in rows[1:]:
            if len(r) < len(hdr):
                continue
            name = r[0].strip()
            dep = re.search(r"\s+(Global|Data ?Zone|Regional)\s*$", name, re.I)
            deployment = dep.group(1) if dep else None
            if dep:
                name = name[:dep.start()]
            ctx = None
            m = re.search(r"\((short|long) context\)|\(([<>]=?\s*\d+k)[^)]*\)", name, re.I)
            if m:
                ctx = (m.group(1) or m.group(2)).lower().replace(" ", "")
                name = (name[:m.start()] + name[m.end():]).strip()
            std = {}
            others = []
            for i in range(1, len(hdr)):
                pr = _az_prices(r[i], site["currency"])
                if not pr:
                    continue
                if "batch" in hdr[i]:
                    for f, v in pr.items():
                        std.setdefault("__batch__", {})[f"batch_{f}"] = v
                elif "flex" in hdr[i]:
                    others.append(("flex", pr))
                elif "priority" in hdr[i]:
                    others.append(("priority", pr))
                else:
                    std.update(pr)
            batch = std.pop("__batch__", {})
            base = {"model": name, "unit": "per_1m_tokens", "section": t["heading"], "context_tier": ctx,
                    "model_provider": "OpenAI", "meta": {"deployment": deployment}}
            if std or batch:
                recs.append({**base, "pricing": {**std, **batch}, "tier": "standard"})
            for tier, pr in others:
                recs.append({**base, "pricing": pr, "tier": tier})
    return recs


PARSERS = {
    "xai": parse_xai,
    "azure": parse_azure,
    "google_cloud": parse_google_cloud,
    "bedrock": parse_bedrock,
    "deepseek": parse_deepseek,
    "together": parse_together,
    "gabia_aihub": parse_gabia,
    "anthropic": parse_anthropic,
    "openai": parse_openai,
    "google": parse_google,
}
