from app.contracts import ChartSpec, QueryResult
from app.errors import AppError


def chart_options(result: QueryResult) -> list[str]:
    cols = result.columns
    numeric = [
        c.name
        for c in cols
        if any(t in c.data_type.lower() for t in ("decimal", "double", "float", "int", "numeric"))
        and c.name != "months_observed"
    ]
    times = [c.name for c in cols if "date" in c.data_type.lower() or "timestamp" in c.data_type.lower()]
    categories = [c.name for c in cols if c.name not in numeric + times and c.name != "months_observed"]
    options = ["table"]
    if not result.rows or result.truncated:
        return options
    if len(cols) == 1 and len(result.rows) == 1 and len(numeric) == 1:
        return ["kpi", "table"]
    if len(numeric) == 1 and times and len(categories) <= 1:
        if not categories or len({r[[c.name for c in cols].index(categories[0])] for r in result.rows}) <= 12:
            options = ["line", "bar", "table"]
    elif len(numeric) == 1 and 1 <= len(categories) <= 2:
        if all(
            len({r[[c.name for c in cols].index(name)] for r in result.rows}) <= 20 for name in categories
        ):
            options = ["bar", "table"]
    elif len(numeric) == 2 and not times and not categories:
        options = ["scatter", "table"]
    return options


def choose_chart(result: QueryResult, requested="auto") -> ChartSpec:
    options = chart_options(result)
    if requested != "auto" and requested not in options:
        if requested == "line" and "period" in [c.name for c in result.columns]:
            raise AppError(
                "chart_unsuitable",
                "This comparison is a period average. Ask for monthly exposure to show a trend.",
                422,
            )
        raise AppError("chart_unsuitable", "That chart type is not suitable for this returned result.", 422)
    kind = options[0] if requested == "auto" else requested
    cols = result.columns
    numbers = [
        c.name
        for c in cols
        if any(t in c.data_type.lower() for t in ("decimal", "double", "float", "int", "numeric"))
        and c.name != "months_observed"
    ]
    names = [c.name for c in cols]
    time_col = next(
        (c.name for c in cols if "date" in c.data_type.lower() or "timestamp" in c.data_type.lower()), None
    )
    category = [n for n in names if n not in numbers and n != time_col and n != "months_observed"]
    spec = ChartSpec(
        chart_type=kind,
        title="Authorized lending query results",
        labels={c.name: c.name.replace("_", " ").title() + (f" ({c.unit})" if c.unit else "") for c in cols},
    )
    if kind in ("line", "bar"):
        spec.x = time_col or category[0]
        spec.y = numbers[0]
        spec.group = category[0] if time_col and category else category[1] if len(category) > 1 else None
    elif kind == "scatter":
        spec.x, spec.y = numbers
    elif kind == "kpi":
        spec.y = numbers[0]
    if any(v is not None and v not in names for v in (spec.x, spec.y, spec.group)):
        raise AppError("chart_invalid", "Chart references an unavailable result column.", 422)
    return spec
