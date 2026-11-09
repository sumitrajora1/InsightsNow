from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Identity(StrictModel):
    owner: str
    username: str
    roles: list[str]
    token: str = Field(default="", exclude=True, repr=False)
    password: SecretStr = Field(default_factory=lambda: SecretStr(""), exclude=True, repr=False)


class Column(StrictModel):
    name: str
    data_type: str
    description: str = ""
    sensitivity: str = "internal"


class Asset(StrictModel):
    id: str
    fqn: str
    columns: list[Column]
    description: str = ""
    owner: str = ""
    source_url: str = ""
    lineage: list[str] = []
    version: str = ""
    sensitivity: str = "internal"


class JoinDefinition(StrictModel):
    id: str
    left_asset: str
    left_column: str
    right_asset: str
    right_column: str
    cardinality: Literal["many_to_one"]


class Metric(StrictModel):
    id: str
    name: str
    definition: str
    source_asset: str
    value_column: str
    snapshot_column: str
    currency_column: str
    default_currency: str
    units: str
    grain: str
    as_of: date
    join_id: str
    formula: Literal["sum_per_snapshot"]
    period_comparison: Literal["average_month_end"]
    glossary_ref: str
    minimum_group_size: int = Field(default=1, ge=1)


class MetadataContext(StrictModel):
    assets: list[Asset]
    metrics: list[Metric]
    joins: list[JoinDefinition]
    available_definitions: dict[str, str] = {}
    synthetic: bool
    capabilities: list[str] = []


class AnalyticalState(StrictModel):
    subject: str = "Corporate lending"
    metric_id: str = "month_end_exposure"
    dimensions: list[Literal["industry", "country"]] = ["industry"]
    country: str | None = None
    currency: str = "AED"
    start_date: date | None = None
    end_date: date | None = None
    months: int = Field(default=6, ge=1, le=24)
    grain: Literal["monthly", "period_average", "latest"] = "monthly"
    comparison: bool = False
    chart_preference: Literal["auto", "line", "bar", "table"] = "auto"
    units: str = "AED"
    metadata_refs: list[str] = []
    last_sql: str | None = None
    last_query_id: str | None = None
    pending_clarification: Literal["period"] | None = None


class QueryPlan(StrictModel):
    interpreted_question: str = Field(max_length=1000)
    kind: Literal["query", "definition", "clarification"]
    clarification: str | None = Field(default=None, max_length=500)
    state: AnalyticalState
    metadata_refs: list[str]
    assumptions: list[str] = Field(default_factory=list, max_length=6)
    sql: str | None = Field(default=None, max_length=16000)
    parameters: dict[str, str | int | float | None] = {}


class Question(StrictModel):
    question: str = Field(min_length=1, max_length=1000)


class NewSession(StrictModel):
    title: str = Field(default="New lending conversation", max_length=100)


class ResultColumn(StrictModel):
    name: str
    data_type: str
    unit: str | None = None


class ChartSpec(StrictModel):
    chart_type: Literal["kpi", "bar", "line", "scatter", "table"]
    x: str | None = None
    y: str | None = None
    group: str | None = None
    title: str = Field(max_length=160)
    labels: dict[str, str] = {}


class ChartChange(StrictModel):
    chart_type: Literal["kpi", "bar", "line", "scatter", "table"]


class QueryResult(StrictModel):
    columns: list[ResultColumn]
    rows: list[list[Any]]
    row_count: int
    truncated: bool
    query_id: str
    duration_ms: float
    synthetic: bool
    execution_backend: str
    chart: ChartSpec | None = None
    allowed_charts: list[str] = []


class QueryResponse(StrictModel):
    id: str
    session_id: str
    correlation_id: str
    status: str
    stage: str
    question: str
    explanation: str | None = None
    clarification: str | None = None
    error: dict[str, str] | None = None
    plan: QueryPlan | None = None
    result: QueryResult | None = None
    metadata: MetadataContext | None = None
