import os
import time

import httpx
import pandas as pd
import plotly.express as px
import streamlit as st
from pydantic import SecretStr

st.set_page_config(page_title="Lending Data Desk", page_icon="◈", layout="wide")
API = os.environ.get("EDQ_API_URL", "http://127.0.0.1:8000")
UI_AUTH_MODE = os.environ.get("EDQ_UI_AUTH_MODE", "development")
if UI_AUTH_MODE not in ("development", "oidc_proxy", "ldaps"):
    st.error("The sign-in configuration is invalid. Contact support.")
    st.stop()
DEV = UI_AUTH_MODE == "development"
request_auth = None
st.markdown(
    """<style>
.stApp {background: #f5f8fa;} h1,h2,h3 {color:#102d49;}
div[data-testid="stMetric"] {background:white; padding:1rem; border-radius:8px;}
</style>""",
    unsafe_allow_html=True,
)
st.title("Lending Data Desk")
st.caption("Ask a question. Explore approved data. Inspect the evidence.")

with st.sidebar:
    st.header("Your workspace")
    if DEV:
        st.warning("DEVELOPMENT AUTH · SYNTHETIC DATA")
        username = st.selectbox(
            "Demo identity",
            ["alice", "bob", "charlie"],
            help="Alice: approved lending scope. Bob: UAE / AED only. Charlie: no asset access.",
        )
        headers = {"X-Dev-User": username}
    elif UI_AUTH_MODE == "ldaps":
        if not API.startswith("https://"):
            st.error("Directory sign-in requires an HTTPS data service.")
            st.stop()
        ttl = min(1800, max(60, int(os.environ.get("EDQ_UI_LDAPS_SESSION_SECONDS", "900"))))
        if "directory_password" in st.session_state and time.monotonic() >= st.session_state.get(
            "directory_expiry", 0
        ):
            st.session_state.clear()
        if "directory_password" not in st.session_state:
            st.caption("Sign in with your approved directory account.")
            with st.form("directory_signin", clear_on_submit=True):
                directory_user = st.text_input("Username")
                directory_password = st.text_input("Password", type="password")
                sign_in = st.form_submit_button("Sign in")
            if sign_in:
                try:
                    response = httpx.get(
                        API + "/v1/me",
                        auth=httpx.BasicAuth(directory_user, directory_password),
                        timeout=15,
                        verify=os.environ.get("EDQ_API_CA_BUNDLE") or True,
                    )
                    if response.status_code == 200:
                        st.session_state.directory_username = directory_user
                        st.session_state.directory_password = SecretStr(directory_password)
                        st.session_state.directory_expiry = time.monotonic() + ttl
                        st.rerun()
                    st.error("Sign-in was denied or temporarily unavailable. Contact support if it persists.")
                except httpx.HTTPError:
                    st.error("The sign-in service is unavailable.")
            st.stop()
        username = st.session_state.directory_username
        request_auth = httpx.BasicAuth(username, st.session_state.directory_password.get_secret_value())
        headers = {}
        st.caption("Directory sign-in · " + username)
        if st.button("Sign out"):
            st.session_state.clear()
            st.rerun()
    else:
        # oauth2-proxy (or equivalent) must authenticate WebSocket handshakes,
        # strip client headers, and inject the verified access token. API verifies it independently.
        token = st.context.headers.get("X-Forwarded-Access-Token", "")
        if not token:
            st.error(
                "Enterprise sign-in is required. Open this application through the approved sign-in route."
            )
            st.stop()
        headers = {"Authorization": "Bearer " + token}
        username = "enterprise"


def api(method, path, body=None):
    try:
        r = httpx.request(
            method,
            API + path,
            headers=headers,
            auth=request_auth,
            json=body,
            timeout=15,
            verify=os.environ.get("EDQ_API_CA_BUNDLE") or True,
        )
        if r.status_code == 401 and UI_AUTH_MODE == "ldaps":
            st.session_state.clear()
            st.rerun()
        if not r.is_success:
            payload = r.json()
            raise ValueError(payload.get("error", {}).get("message", "The request could not complete."))
        return r.json()
    except (httpx.HTTPError, ValueError) as exc:
        st.error(
            str(exc) if isinstance(exc, ValueError) else "The data service is unavailable. Try again later."
        )
        st.stop()


me = api("GET", "/v1/me")
identity_key = me["username"]
if st.session_state.get("identity") != identity_key:
    for key in list(st.session_state):
        if key not in ("identity", "directory_username", "directory_password", "directory_expiry"):
            del st.session_state[key]
    st.session_state.identity = identity_key
if me["synthetic"]:
    st.info(f"Synthetic corporate lending demonstration · {me['executor']} execution · No live bank records")

with st.sidebar:
    if st.button("＋ New conversation", width="stretch"):
        created = api("POST", "/v1/sessions", {"title": "Lending analysis"})
        st.session_state.current = created["id"]
        st.session_state.pending = None
        st.rerun()
    sessions = api("GET", "/v1/sessions")
    if not sessions:
        created = api("POST", "/v1/sessions", {"title": "Lending analysis"})
        st.session_state.current = created["id"]
        st.rerun()
    ids = [s["id"] for s in sessions]
    current = st.session_state.get("current", ids[0])
    chosen = st.selectbox(
        "Conversations",
        ids,
        index=ids.index(current) if current in ids else 0,
        format_func=lambda x: next(s["title"] for s in sessions if s["id"] == x) + " · " + x[:6],
    )
    if chosen != current:
        st.session_state.current, st.session_state.pending = chosen, None
        st.rerun()
    st.session_state.current = chosen
    st.caption("Only your conversations are visible. Start a new conversation to reset analytical context.")


def render_result(item):
    if item.get("clarification"):
        st.warning(item["clarification"])
    if item.get("error"):
        st.error(item["error"]["message"])
    if item.get("explanation"):
        st.write(item["explanation"])
    result = item.get("result")
    if result:
        names = [c["name"] for c in result["columns"]]
        df = pd.DataFrame(result["rows"], columns=names)
        for col in result["columns"]:
            if any(t in col["data_type"].lower() for t in ("decimal", "double", "int", "numeric")):
                df[col["name"]] = pd.to_numeric(df[col["name"]], errors="raise")
        tab1, tab2 = st.tabs(["Visualization", "Data table"])
        with tab1:
            kind = st.selectbox(
                "Display",
                result["allowed_charts"],
                index=result["allowed_charts"].index(result["chart"]["chart_type"]),
                key="chart_" + item["id"],
            )
            spec = api("POST", f"/v1/requests/{item['id']}/chart", {"chart_type": kind})
            if kind == "kpi":
                st.metric(spec["labels"].get(spec["y"], "Result"), df[spec["y"]].iloc[0])
            elif kind in ("bar", "line", "scatter"):
                factory = {"bar": px.bar, "line": px.line, "scatter": px.scatter}[kind]
                fig = factory(
                    df,
                    x=spec["x"],
                    y=spec["y"],
                    color=spec["group"],
                    title=spec["title"],
                    labels=spec["labels"],
                    template="plotly_white",
                )
                st.plotly_chart(fig, width="stretch", key="plot_" + item["id"])
            else:
                st.dataframe(df, hide_index=True, width="stretch", key="visual_table_" + item["id"])
        with tab2:
            st.dataframe(df, hide_index=True, width="stretch", key="data_table_" + item["id"])
        st.caption(
            f"{result['row_count']} returned rows · {result['duration_ms']:.0f} ms · Query {result['query_id']}"
        )
        if result["truncated"]:
            st.warning("Partial result: narrow the question. Charts and inferred comparisons are disabled.")
    if item.get("plan"):
        with st.expander("SQL and analytical context"):
            state = item["plan"]["state"]
            if state.get("last_sql"):
                st.code(state["last_sql"], language="sql")
            st.json({k: v for k, v in state.items() if k not in ("last_sql",)})
            st.write("Assumptions:", item["plan"]["assumptions"])
    if item.get("metadata"):
        with st.expander("Definitions and metadata sources"):
            for metric in item["metadata"]["metrics"]:
                st.write(metric["name"] + ": " + metric["definition"])
                st.caption(metric["glossary_ref"])
            for asset in item["metadata"]["assets"]:
                st.write(asset["fqn"])
                st.caption(f"Owner: {asset['owner']} · Metadata ID: {asset['id']} · {asset['source_url']}")
    with st.expander("Execution trace"):
        st.write("Correlation ID:", item["correlation_id"])
        st.write("Status:", item["status"])
        if st.button("View redacted audit events", key="audit_" + item["id"]):
            st.json(api("GET", f"/v1/requests/{item['id']}/audit"))


conversation = api("GET", f"/v1/sessions/{st.session_state.current}")
for item in conversation["history"]:
    with st.chat_message("user"):
        st.write(item["question"])
    with st.chat_message("assistant"):
        render_result(item)

pending = st.session_state.get("pending")
if pending:

    @st.fragment(run_every="1s")
    def progress():
        item = api("GET", f"/v1/requests/{pending}")
        if item["status"] in ("queued", "running"):
            st.status("Working: " + item["stage"], state="running")
            if st.button("Cancel request"):
                api("POST", f"/v1/requests/{pending}/cancel")
                st.caption("Cancellation requested. Waiting for the execution worker to stop.")
        else:
            st.session_state.pending = None
            st.rerun()

    progress()

examples = [
    "Show corporate lending exposure by industry for the last six months.",
    "Only show UAE.",
    "Compare this with the previous six months.",
    "Show this as a trend.",
    "What does exposure mean, and which data sources support this answer?",
]
with st.expander("Example questions", expanded=not conversation["history"]):
    for example in examples:
        if st.button(example, disabled=bool(pending), key="example_" + example):
            st.session_state.example_question = example
question = st.chat_input("Ask about approved corporate lending data…", disabled=bool(pending))
question = question or st.session_state.pop("example_question", None)
if question:
    item = api("POST", f"/v1/sessions/{st.session_state.current}/questions", {"question": question})
    st.session_state.pending = item["id"]
    st.rerun()
