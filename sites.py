"""스키마.xlsx 기반 크롤링 대상 사이트 목록.

adapter: 공개 JSON API가 있는 사이트는 API로 수집(정확/빠름), 없으면 None -> Playwright 렌더링 + 표 파싱.
currency: 사이트가 기본으로 쓰는 통화 힌트 (표 안에서 기호가 감지되면 그 값이 우선).
"""

# (id, name, category, url, note, adapter, currency)
_ROWS = [
    # --- 라우터 ---
    ("pleum", "pleum router", "router", "https://router.pleum.ai/models", "원화 가격제공", "pleum", "KRW"),
    ("gabia_aihub", "가비아 ai hub", "router", "https://aihub.gabiacloud.com/model-catalog", "원화 가격제공", None, "KRW"),
    ("bizrouter", "biz router", "router", "https://bizrouter.ai/models", "원화 가격제공", "bizrouter", "KRW"),
    ("openrouter", "openrouter", "router", "https://openrouter.ai/models", "", "openrouter", "USD"),
    ("vercel", "vercel", "router", "https://vercel.com/ai-gateway/models", "", "vercel", "USD"),
    ("litellm", "lite llm", "router", "https://models.litellm.ai/", "", "litellm", "USD"),
    ("cloudflare", "cloudflare", "router", "https://developers.cloudflare.com/workers-ai/platform/pricing/", "모델 카탈로그(/ai/models/)에는 가격이 없어 Workers AI 가격 페이지로 대체", None, "USD"),
    ("requesty", "requesty", "router", "https://www.requesty.ai/models", "", None, "USD"),
    ("edenai", "eden ai", "router", "https://app.edenai.run/models", "", "edenai", "USD"),
    # --- 영상/이미지 위주 라우터 ---
    ("fal", "fal", "media_router", "https://fal.ai/pricing", "영상, 이미지위주 라우터", "fal", "USD"),
    ("replicate", "replicate", "media_router", "https://replicate.com/pricing", "영상, 이미지위주 라우터", None, "USD"),
    # --- 클라우드 ---
    ("azure", "azure", "cloud", "https://azure.microsoft.com/en-us/pricing/details/azure-openai/", "시트 URL은 Azure 가격 개요 페이지(표 없음) -> OpenAI 서비스 가격 상세 페이지로 대체", None, "USD"),
    ("bedrock", "bedrock", "cloud", "https://aws.amazon.com/bedrock/pricing/", "시트 URL은 /ko/(한국어) -> 파싱을 위해 영어 페이지 사용", None, "USD"),
    ("google_cloud", "google cloud", "cloud", "https://cloud.google.com/gemini-enterprise-agent-platform/generative-ai/pricing", "", None, "USD"),
    # --- 제조사 공식 ---
    ("openai", "openai", "official", "https://developers.openai.com/api/docs/pricing", "", None, "USD"),
    ("anthropic", "claude", "official", "https://platform.claude.com/docs/en/about-claude/pricing", "", None, "USD"),
    ("google", "google", "official", "https://ai.google.dev/gemini-api/docs/pricing", "시트 URL은 hl=ko(한국어) -> 파싱을 위해 영어 페이지 사용", None, "USD"),
    ("deepseek", "deepseek", "official", "https://api-docs.deepseek.com/quick_start/pricing/", "", None, "USD"),
    ("xai", "grok", "official", "https://docs.x.ai/developers/pricing", "시트 URL(/models)은 대표 모델 카드만 있어 /pricing 페이지로 대체", None, "USD"),
    ("zai", "Z-ai", "official", "https://docs.z.ai/guides/overview/pricing", "", None, "USD"),
    ("xiaomi_mimo", "xiaomi-mimo", "official", "https://mimo.mi.com/docs/en-US/price/pay-as-you-go", "", None, "USD"),
    ("alibaba_qwen", "알리바바-qwen", "official", "https://www.alibabacloud.com/help/en/model-studio/model-pricing", "", None, "USD"),
    # --- 영상/이미지/음성 모델 제조사 ---
    ("kling", "kling-kuaishou", "official_media", "https://kling.ai/document-api/pricing/base/video", "", None, "USD"),
    ("bfl_flux", "flux-blackforestlabs", "official_media", "https://bfl.ai/pricing", "", None, "USD"),
    ("minimax", "minimax", "official_media", "https://platform.minimax.io/docs/pricing/overview", "", None, "USD"),
    ("seedance", "seedance", "official_media", "https://docs.byteplus.com/ko/docs/modelark/model-pricing", "", None, "USD"),
    ("elevenlabs", "elevenlabs", "official_media", "https://elevenlabs.io/pricing/api", "", None, "USD"),
    ("ideogram", "ideogram", "official_media", "https://ideogram.ai/pricing/?pricing_tab=api", "", None, "USD"),
    # --- 기타 추론 호스팅 ---
    ("relace", "relace", "other", "https://docs.relace.ai/docs/open-models/quickstart", "", None, "USD"),
    ("inference_net", "inference.net", "other", "https://inference.net/models/", "시트에 'open inference'와 중복", "inference_net", "USD"),
    ("sailresearch", "sail research", "other", "https://docs.sailresearch.com/pricing", "", None, "USD"),
    ("decart", "decart", "other", "https://cogito.decart.ai/models", "", None, "USD"),
    ("morph", "morph", "other", "https://www.morphllm.com/models", "", None, "USD"),
    ("dekallm", "deka llm", "other", "https://www.cloudeka.id/en/products/deka-llm-en/", "", None, "USD"),
    ("deepinfra", "deepinfra", "other", "https://deepinfra.com/pricing", "", None, "USD"),
    ("streamlake", "streamlake", "other", "https://www.streamlake.ai/document/DOC/mgrnm4xm362hvp5wyce", "", None, "USD"),
    ("atlascloud", "atlas cloud", "other", "https://www.atlascloud.ai/pricing", "시트의 URL이 deepinfra와 동일(오타 추정) -> atlascloud.ai/pricing으로 대체 (/models 에는 단가 표 없음)", None, "USD"),
    ("digitalocean", "digital ocean", "other", "https://docs.digitalocean.com/products/inference/details/pricing/", "", None, "USD"),
    ("gmicloud", "gmicloud", "other", "https://console.gmicloud.ai/?group=LLM", "", "gmicloud", "USD"),
    ("coreweave", "coreweave", "other", "https://coreweave.com/forge-pricing/inference-agents", "", None, "USD"),
    ("nextbit", "nextBit", "other", "https://www.nextbit256.com/inference/models-api", "", "nextbit", "USD"),
    ("phala", "Phala", "other", "https://phala.com/models", "", "phala", "USD"),
    ("novita", "Novita ai", "other", "https://novita.ai/pricing", "", None, "USD"),
    ("makora", "Makora", "other", "https://www.makora.com/#models", "", "makora", "USD"),
    ("baidu_qianfan", "Baidu Qianfan", "other", "https://cloud.baidu.com/doc/qianfan/s/wmh4sv6ya", "", None, "CNY"),
    ("baseten", "Baseten", "other", "https://www.baseten.co/pricing/", "모델 라이브러리(/library/)에는 가격이 없어 /pricing/ 으로 대체", None, "USD"),
    ("together", "Together", "other", "https://www.together.ai/pricing", "", None, "USD"),
    ("siliconflow", "SiliconFlow", "other", "https://www.siliconflow.com/pricing", "", None, "USD"),
    ("modal", "Modal", "other", "https://modal.com/library", "", None, "USD"),
    ("parasail", "parasail", "other", "https://www.parasail.io/pricing", "모델 목록(/models)에는 가격이 없어 /pricing 으로 대체", None, "USD"),
    ("fireworks", "fireworks", "other", "https://fireworks.ai/models", "", None, "USD"),
    ("venice", "venice", "other", "https://docs.venice.ai/models/overview", "", "venice", "USD"),
    ("friendli", "Friendli", "other", "https://friendli.ai/pricing", "모델 목록(/models)에는 가격이 없어 /pricing 으로 대체", None, "USD"),
]

SITES = [
    dict(id=r[0], name=r[1], category=r[2], url=r[3], note=r[4], adapter=r[5], currency=r[6])
    for r in _ROWS
]



