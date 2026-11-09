import calendar
import re
from datetime import date

import sqlglot
from sqlglot import exp

from app.auth import Grant
from app.contracts import AnalyticalState, MetadataContext, Metric
from app.errors import AppError

IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def shift_month(day: date, offset: int) -> date:
    y, m = divmod(day.year * 12 + day.month - 1 + offset, 12)
    return date(y, m + 1, min(day.day, calendar.monthrange(y, m + 1)[1]))


def approved_metric(context: MetadataContext, state: AnalyticalState) -> Metric:
    matches = [m for m in context.metrics if m.id == state.metric_id]
    if len(matches) != 1:
        raise AppError(
            "clarification_required",
            "The exposure definition is missing or ambiguous in authorized metadata.",
            422,
        )
    return matches[0]


def prepare_state(state: AnalyticalState, context: MetadataContext, grant: Grant) -> AnalyticalState:
    state = state.model_copy(deep=True)
    metric = approved_metric(context, state)
    if state.currency not in grant.currencies:
        raise AppError("access_denied", "The requested currency is not authorized.", 403)
    if grant.country:
        if state.country and state.country != grant.country:
            raise AppError("access_denied", "The requested country is outside your authorized scope.", 403)
        state.country = grant.country
    state.end_date = metric.as_of
    state.start_date = (
        metric.as_of
        if state.grain == "latest"
        else shift_month(metric.as_of.replace(day=1), -(state.months - 1))
    )
    if state.comparison:
        state.grain = "period_average"
    if len(set(state.dimensions)) != len(state.dimensions):
        raise AppError("invalid_plan", "Duplicate grouping dimensions are not supported.")
    state.metadata_refs = [a.id for a in context.assets] + [metric.glossary_ref, metric.join_id]
    state.units = state.currency
    state.last_sql = None
    state.last_query_id = None
    state.pending_clarification = None
    return state


def compile_sql(state: AnalyticalState, context: MetadataContext) -> tuple[str, dict]:
    """Reviewed semantic compiler. LLM SQL must match this exact AST before execution.

    The compiler is intentionally a bounded semantic layer, not a general SQL author.
    No multi-date snapshot sum, currency conversion, guessed join or free expression.
    """
    metric = approved_metric(context, state)
    assets = {a.id: a for a in context.assets}
    joins = [j for j in context.joins if j.id == metric.join_id]
    if len(joins) != 1:
        raise AppError("clarification_required", "An approved facility relationship is required.", 422)
    join = joins[0]
    if join.left_asset != metric.source_asset:
        raise AppError("invalid_metadata", "Metric and join contract do not agree.", 422)
    try:
        left, right = assets[join.left_asset], assets[join.right_asset]
        for a in (left, right):
            if len(a.fqn.split(".")) != 3 or not all(IDENTIFIER.fullmatch(x) for x in a.fqn.split(".")):
                raise ValueError("Invalid fully qualified name")
        for name in [
            metric.value_column,
            metric.snapshot_column,
            metric.currency_column,
            join.left_column,
            join.right_column,
            *state.dimensions,
        ]:
            if not IDENTIFIER.fullmatch(name):
                raise ValueError("Invalid identifier")
        left_columns = {c.name for c in left.columns}
        right_columns = {c.name for c in right.columns}
        if (
            not {metric.value_column, metric.snapshot_column, metric.currency_column, join.left_column}
            <= left_columns
        ):
            raise ValueError("Missing metric column")
        if (
            not {join.right_column, *state.dimensions, *(["country"] if state.country else [])}
            <= right_columns
        ):
            raise ValueError("Missing dimension or join column")
    except (KeyError, ValueError) as exc:
        raise AppError(
            "clarification_required", "Required authorized columns or relationships are missing.", 422
        ) from exc
    dims = [f"f.{d}" for d in state.dimensions]
    group = ", ".join([f"s.{metric.snapshot_column}", *dims])
    select_dims = ", " + ", ".join(dims) if dims else ""
    params = {
        "currency": state.currency,
        "start": state.start_date.isoformat(),
        "end": state.end_date.isoformat(),
        "minimum_group": metric.minimum_group_size,
    }
    earliest = shift_month(state.start_date, -state.months) if state.comparison else state.start_date
    params["earliest"] = earliest.isoformat()
    country_clause = " AND f.country = :country" if state.country else ""
    if state.country:
        params["country"] = state.country
    monthly = f"""SELECT s.{metric.snapshot_column} AS snapshot_date{select_dims},
        SUM(s.{metric.value_column}) AS exposure
        FROM {left.fqn} AS s JOIN {right.fqn} AS f ON s.{join.left_column} = f.{join.right_column}
        WHERE s.{metric.snapshot_column} >= CAST(:earliest AS DATE)
          AND s.{metric.snapshot_column} <= CAST(:end AS DATE)
          AND s.{metric.currency_column} = :currency {country_clause}
        GROUP BY {group}
        HAVING COUNT(DISTINCT s.{join.left_column}) >= :minimum_group"""
    dim_names = ", ".join(state.dimensions)
    if state.comparison:
        params["months"] = state.months
        label = f"CASE WHEN snapshot_date >= CAST(:start AS DATE) THEN 'Current {state.months} months' ELSE 'Previous {state.months} months' END"
        prefix = dim_names + ", " if dim_names else ""
        group_by = prefix + label
        sql = f"""WITH monthly AS ({monthly}) SELECT {prefix}{label} AS period,
            AVG(exposure) AS average_month_end_exposure, COUNT(*) AS months_observed
            FROM monthly GROUP BY {group_by} HAVING COUNT(*) = :months ORDER BY {prefix}period"""
    else:
        suffix = ", " + dim_names if dim_names else ""
        sql = f"WITH monthly AS ({monthly}) SELECT snapshot_date{suffix}, exposure FROM monthly ORDER BY snapshot_date{suffix}"
        if state.grain == "latest":
            prefix = dim_names + ", " if dim_names else ""
            order = " ORDER BY " + dim_names if dim_names else ""
            sql = f"WITH monthly AS ({monthly}) SELECT {prefix}exposure FROM monthly{order}"
    # Only values become AST literals. Table names above came from an authorized, identifier-checked contract.
    tree = sqlglot.parse_one(sql, read="trino")
    used = {n.name for n in tree.find_all(exp.Placeholder)}
    return tree.sql(dialect="trino"), {k: v for k, v in params.items() if k in used}


def bind_parameters(tree, parameters):
    used = {n.name for n in tree.find_all(exp.Placeholder)}
    if set(parameters) != used:
        raise AppError("invalid_parameters", "SQL parameters do not match the validated query.")
    return tree.transform(lambda n: exp.convert(parameters[n.name]) if isinstance(n, exp.Placeholder) else n)
