from app.contracts import AnalyticalState, MetadataContext, QueryResult
from app.planning import approved_metric


def explain(state: AnalyticalState, metadata: MetadataContext, result: QueryResult | None = None):
    """No result values sent to an external model. Never infer causality."""
    metric = approved_metric(metadata, state)
    label = "SYNTHETIC demonstration data. " if metadata.synthetic else "Authorized source data. "
    definition = f"{metric.name}: {metric.definition}"
    sources = ", ".join(a.fqn for a in metadata.assets)
    if result is None:
        return (
            label + definition + f" Approved sources: {sources}. Metadata reference: {metric.glossary_ref}."
        )
    window = f"{state.start_date} to {state.end_date}; currency {state.currency}"
    if state.comparison:
        window += f"; compared with the preceding {state.months} months using average month-end exposure"
    scope = f"; country {state.country}" if state.country else "; authorized country scope"
    if not result.rows:
        return (
            label
            + f"No reportable rows for {window}{scope}. Data may be absent, incomplete or below the approved minimum group size. "
            + definition
        )
    msg = label + f"Returned {result.row_count} rows for {window}{scope}. "
    if result.truncated:
        msg += "The result is truncated. No totals, changes or comparisons are inferred from this partial result. Narrow the question. "
    elif state.comparison:
        msg += "Only groups with the required count of complete monthly observations appear in each period. Missing groups cannot support a complete comparison. "
    else:
        msg += "Each date is a separate snapshot balance; do not sum the returned balances across dates. "
    return msg + definition + " No causal or credit-risk interpretation is inferred."
