import sqlglot
from sqlglot import exp
from sqlglot.errors import OptimizeError, ParseError
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, traverse_scope

from app.auth import Grant
from app.contracts import MetadataContext, QueryPlan
from app.errors import AppError
from app.planning import bind_parameters, compile_sql

ALLOWED_FUNCTIONS = {"SUM", "AVG", "COUNT", "CAST", "CASE", "IF", "AND", "OR"}


class SQLValidator:
    def __init__(self, max_rows: int):
        self.max_rows = max_rows

    def validate(
        self,
        sql: str,
        parameters: dict,
        context: MetadataContext,
        grant: Grant,
        plan: QueryPlan | None = None,
    ) -> str:
        try:
            statements = sqlglot.parse(sql, read="trino")
            if len(statements) != 1 or not isinstance(statements[0], exp.Select):
                raise ValueError("Exactly one SELECT query is permitted")
            tree = bind_parameters(statements[0], parameters)
            if tree.args.get("locks") or tree.args.get("into"):
                raise ValueError("No locks or SELECT INTO")
            if any(w.args.get("recursive") for w in tree.find_all(exp.With)):
                raise ValueError("Recursive queries are not permitted")
            denied = (
                exp.DDL,
                exp.DML,
                exp.Command,
                exp.Union,
                exp.Intersect,
                exp.Except,
                exp.Window,
                exp.Lateral,
                exp.Unnest,
            )
            if any(isinstance(n, denied) for n in tree.walk()):
                raise ValueError("Unsupported or unsafe SQL construct")
            for node in tree.find_all(exp.Func):
                if node.sql_name() not in ALLOWED_FUNCTIONS:
                    raise ValueError("Function is not approved")
            for star in tree.find_all(exp.Star):
                if not isinstance(star.parent, exp.Count):
                    raise ValueError("Wildcard projections are forbidden")
            schema = {}
            authorized = {a.fqn: a for a in context.assets if a.fqn in grant.columns}
            for scope in traverse_scope(tree):
                for _, source in scope.sources.items():
                    if isinstance(source, Scope):
                        continue
                    if not isinstance(source, exp.Table) or not isinstance(source.this, exp.Identifier):
                        raise ValueError("Table functions are forbidden")
                    fqn = ".".join(p.name for p in source.parts)
                    if fqn not in authorized:
                        raise ValueError("Table is not authorized")
            for fqn, asset in authorized.items():
                catalog, database, name = fqn.split(".")
                schema.setdefault(catalog, {}).setdefault(database, {})[name] = {
                    c.name: c.data_type for c in asset.columns if c.name in grant.columns[fqn]
                }
            # Resolves physical and CTE columns, rejects unknown / ambiguous columns even in predicates.
            qualified = qualify(
                tree.copy(),
                dialect="trino",
                schema=schema,
                validate_qualify_columns=True,
                quote_identifiers=False,
                identify=False,
            )
            if plan:
                expected_sql, expected_params = compile_sql(plan.state, context)
                expected = bind_parameters(sqlglot.parse_one(expected_sql, read="trino"), expected_params)
                if tree != expected:
                    raise ValueError("SQL does not implement the approved metric plan")
                if set(plan.metadata_refs) != set(plan.state.metadata_refs):
                    raise ValueError("Unverified metadata references")
            limit = tree.args.get("limit")
            row_bound = self.max_rows + 1  # One-row lookahead establishes truncation.
            if limit:
                literal = limit.expression
                if not isinstance(literal, exp.Literal) or literal.is_string:
                    raise ValueError("Limit must be a nonnegative integer")
                row_bound = min(row_bound, int(literal.this))
                if row_bound < 0:
                    raise ValueError("Negative limit")
            qualified = qualified.limit(row_bound)
            return qualified.sql(dialect="trino")
        except AppError:
            raise
        except (ValueError, ParseError, OptimizeError, sqlglot.errors.SchemaError) as exc:
            raise AppError(
                "sql_rejected",
                "The proposed SQL failed deterministic authorization or semantic validation.",
                422,
            ) from exc
