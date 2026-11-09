from prometheus_client import Counter, Histogram

REQUESTS = Counter("edq_requests_total", "Query request outcomes", ["status"])
STAGES = Histogram("edq_stage_seconds", "Dependency and pipeline latency", ["stage"])
ERRORS = Counter("edq_errors_total", "Rejections and dependency errors", ["code"])
MODELS = Counter("edq_model_calls_total", "Planner calls", ["provider"])
TOKENS = Counter("edq_model_tokens_total", "External model token usage", ["direction"])
