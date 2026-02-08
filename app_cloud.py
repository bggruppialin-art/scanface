from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import altair as alt
import gspread
import pandas as pd
import streamlit as st
from gspread.exceptions import WorksheetNotFound
from gspread.utils import rowcol_to_a1


st.set_page_config(page_title="FB Lead Hunter Cloud", layout="wide")

DEFAULT_USERS = {
    "Admin": {"password": "admin123", "role": "Admin"},
    "Sale1": {"password": "sale123", "role": "Sales"},
    "Sale2": {"password": "sale123", "role": "Sales"},
}

LEAD_REQUIRED_COLUMNS = [
    "Lead ID",
    "User Name",
    "Profile URL",
    "Profile Picture",
    "Join Status Text",
    "Friendship Status",
    "Biography",
    "Status",
    "Assigned_To",
    "Script_Used",
    "Notes",
    "Updated_At",
    "Last_Updated_By",
    "Freshness",
]

STATUS_OPTIONS = ["New", "Contacted", "Interested", "Customer", "Ignore"]
SCRIPT_OPTIONS = ["Unassigned", "Script A: Friendly", "Script B: Direct"]


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def get_sheet_setting(name: str, default: str) -> str:
    try:
        return str(st.secrets["sheets"][name])
    except Exception:
        return default


def parse_freshness(text: object) -> str:
    raw = "" if pd.isna(text) else str(text)
    if "นาที" in raw:
        return "🔥 Hot (Minutes)"
    if "ชั่วโมง" in raw:
        return "🌤️ Warm (Hours)"
    return "❄️ Cold (Days+)"


def parse_dt(series: pd.Series) -> pd.Series:
    parsed = pd.to_datetime(series, errors="coerce", utc=True)
    return parsed


@st.cache_resource(show_spinner=False)
def get_gspread_client() -> gspread.Client:
    creds = dict(st.secrets["gcp_service_account"])
    return gspread.service_account_from_dict(creds)


@st.cache_resource(show_spinner=False)
def get_spreadsheet() -> gspread.Spreadsheet:
    spreadsheet_id = st.secrets["sheets"]["spreadsheet_id"]
    return get_gspread_client().open_by_key(spreadsheet_id)


def get_worksheet(name: str, required: bool = True):
    spreadsheet = get_spreadsheet()
    try:
        return spreadsheet.worksheet(name)
    except WorksheetNotFound:
        if required:
            raise
        return None


def read_ws_dataframe(ws: gspread.Worksheet) -> Tuple[pd.DataFrame, List[str]]:
    values = ws.get_all_values()
    if not values:
        return pd.DataFrame(), []

    headers = values[0]
    rows = values[1:]

    normalized_rows: List[List[str]] = []
    for row in rows:
        row = row[: len(headers)]
        if len(row) < len(headers):
            row = row + [""] * (len(headers) - len(row))
        normalized_rows.append(row)

    df = pd.DataFrame(normalized_rows, columns=headers)
    df["_row"] = range(2, len(df) + 2)
    return df, headers


def ensure_lead_schema(ws: gspread.Worksheet) -> List[str]:
    values = ws.get_all_values()
    headers = values[0] if values else []

    if not headers:
        headers = LEAD_REQUIRED_COLUMNS.copy()
        end = rowcol_to_a1(1, len(headers))
        ws.update(f"A1:{end}", [headers], value_input_option="USER_ENTERED")
        st.cache_data.clear()
        return headers

    missing = [col for col in LEAD_REQUIRED_COLUMNS if col not in headers]
    if missing:
        headers = headers + missing
        end = rowcol_to_a1(1, len(headers))
        ws.update(f"A1:{end}", [headers], value_input_option="USER_ENTERED")
        st.cache_data.clear()

    return headers


@st.cache_data(ttl=15, show_spinner=False)
def load_leads() -> pd.DataFrame:
    ws_name = get_sheet_setting("leads_worksheet", "Leads")
    ws = get_worksheet(ws_name, required=True)
    ensure_lead_schema(ws)
    df, _ = read_ws_dataframe(ws)

    if df.empty:
        for col in LEAD_REQUIRED_COLUMNS:
            df[col] = pd.Series(dtype="object")
        df["_row"] = pd.Series(dtype="int")
        return df

    for col in LEAD_REQUIRED_COLUMNS:
        if col not in df.columns:
            df[col] = ""

    df = df.fillna("")
    df["Status"] = df["Status"].astype(str).where(df["Status"].isin(STATUS_OPTIONS), "New")
    df["Assigned_To"] = df["Assigned_To"].astype(str)
    df["Freshness"] = df["Freshness"].astype(str)

    missing_freshness = df["Freshness"].str.strip().eq("")
    df.loc[missing_freshness, "Freshness"] = df.loc[missing_freshness, "Join Status Text"].apply(parse_freshness)

    empty_lead_id = df["Lead ID"].astype(str).str.strip().eq("")
    df.loc[empty_lead_id, "Lead ID"] = df.loc[empty_lead_id, "_row"].astype(str)

    return df


@st.cache_data(ttl=60, show_spinner=False)
def load_users() -> Dict[str, Dict[str, str]]:
    ws_name = get_sheet_setting("users_worksheet", "Users")
    ws = get_worksheet(ws_name, required=False)
    if ws is None:
        return DEFAULT_USERS.copy()

    df, _ = read_ws_dataframe(ws)
    if df.empty:
        return DEFAULT_USERS.copy()

    expected = {"Username", "Password", "Role"}
    if not expected.issubset(set(df.columns)):
        return DEFAULT_USERS.copy()

    users: Dict[str, Dict[str, str]] = {}
    for _, row in df.iterrows():
        username = str(row.get("Username", "")).strip()
        password = str(row.get("Password", "")).strip()
        role = str(row.get("Role", "Sales")).strip() or "Sales"
        if username and password:
            users[username] = {"password": password, "role": role}

    return users if users else DEFAULT_USERS.copy()


def get_leads_ws_and_map() -> Tuple[gspread.Worksheet, Dict[str, int]]:
    ws_name = get_sheet_setting("leads_worksheet", "Leads")
    ws = get_worksheet(ws_name, required=True)
    headers = ensure_lead_schema(ws)
    col_map = {name: idx + 1 for idx, name in enumerate(headers)}
    return ws, col_map


def batch_update_cells(ws: gspread.Worksheet, updates: List[Dict[str, List[List[str]]]]) -> None:
    if not updates:
        return
    ws.batch_update(updates, value_input_option="USER_ENTERED")
    st.cache_data.clear()


def request_leads_for_user(current_user: str, count: int = 10) -> int:
    ws, col_map = get_leads_ws_and_map()
    grabbed_rows: List[int] = []
    remaining = count

    for _ in range(3):
        if remaining <= 0:
            break

        latest = load_leads()
        candidates = latest[
            latest["Status"].eq("New")
            & latest["Assigned_To"].astype(str).str.strip().eq("")
        ].copy()

        if candidates.empty:
            break

        chunk = candidates.head(remaining)
        now = now_utc_iso()

        updates: List[Dict[str, List[List[str]]]] = []
        for _, row in chunk.iterrows():
            row_no = int(row["_row"])
            updates.append({
                "range": rowcol_to_a1(row_no, col_map["Assigned_To"]),
                "values": [[current_user]],
            })
            updates.append({
                "range": rowcol_to_a1(row_no, col_map["Updated_At"]),
                "values": [[now]],
            })
            updates.append({
                "range": rowcol_to_a1(row_no, col_map["Last_Updated_By"]),
                "values": [[current_user]],
            })

        batch_update_cells(ws, updates)

        verify = load_leads()
        claimed = verify[
            verify["_row"].isin(chunk["_row"])
            & verify["Assigned_To"].astype(str).eq(current_user)
        ]["_row"].astype(int).tolist()

        grabbed_rows.extend(claimed)
        grabbed_rows = list(dict.fromkeys(grabbed_rows))
        remaining = count - len(grabbed_rows)

    st.session_state["requested_rows"] = grabbed_rows
    return len(grabbed_rows)


def save_sales_updates(current_user: str, original_df: pd.DataFrame, edited_df: pd.DataFrame) -> int:
    ws, col_map = get_leads_ws_and_map()

    editable_cols = ["Status", "Notes", "Script_Used"]
    updates: List[Dict[str, List[List[str]]]] = []
    changed_rows: set = set()

    for row_no in edited_df.index.tolist():
        if row_no not in original_df.index:
            continue

        for col in editable_cols:
            old_val = str(original_df.at[row_no, col]) if col in original_df.columns else ""
            new_val = str(edited_df.at[row_no, col]) if col in edited_df.columns else ""
            if new_val != old_val:
                updates.append({
                    "range": rowcol_to_a1(int(row_no), col_map[col]),
                    "values": [[new_val]],
                })
                changed_rows.add(int(row_no))

    now = now_utc_iso()
    for row_no in changed_rows:
        updates.append({
            "range": rowcol_to_a1(row_no, col_map["Updated_At"]),
            "values": [[now]],
        })
        updates.append({
            "range": rowcol_to_a1(row_no, col_map["Last_Updated_By"]),
            "values": [[current_user]],
        })

    batch_update_cells(ws, updates)
    return len(changed_rows)


def recycle_stale_assignments(hours: int = 24) -> int:
    ws, col_map = get_leads_ws_and_map()
    leads = load_leads()
    if leads.empty:
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    updated_ts = parse_dt(leads["Updated_At"])

    stale_mask = (
        leads["Assigned_To"].astype(str).str.strip().ne("")
        & ((updated_ts.isna()) | (updated_ts < cutoff))
        & (~leads["Status"].isin(["Interested", "Customer"]))
    )

    stale_rows = leads[stale_mask]
    if stale_rows.empty:
        return 0

    now = now_utc_iso()
    updates: List[Dict[str, List[List[str]]]] = []
    for _, row in stale_rows.iterrows():
        row_no = int(row["_row"])
        updates.append({"range": rowcol_to_a1(row_no, col_map["Assigned_To"]), "values": [[""]]})
        updates.append({"range": rowcol_to_a1(row_no, col_map["Status"]), "values": [["New"]]})
        updates.append({"range": rowcol_to_a1(row_no, col_map["Updated_At"]), "values": [[now]]})
        updates.append({"range": rowcol_to_a1(row_no, col_map["Last_Updated_By"]), "values": [["Admin"]]})

    batch_update_cells(ws, updates)
    return len(stale_rows)


def render_login(users: Dict[str, Dict[str, str]]) -> None:
    st.title("FB Lead Hunter Cloud")
    st.caption("Online Multi-User CRM on Google Sheets")

    user_options = sorted(users.keys())

    with st.form("login_form", clear_on_submit=False):
        username = st.selectbox("Username", user_options)
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Login", use_container_width=True)

    if submitted:
        account = users.get(username)
        if account and password == account["password"]:
            st.session_state["current_user"] = username
            st.session_state["current_role"] = account.get("role", "Sales")
            st.success("Login successful")
            st.rerun()
        else:
            st.error("Invalid username or password")


def render_sales_view(current_user: str) -> None:
    st.subheader("Sales Workspace")

    c1, c2 = st.columns([1, 1])
    if c1.button("📥 Request 10 Leads", use_container_width=True):
        claimed = request_leads_for_user(current_user, 10)
        if claimed:
            st.success(f"Assigned {claimed} leads to {current_user}.")
        else:
            st.warning("No available New leads to assign.")
        st.rerun()

    if c2.button("Refresh", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

    leads = load_leads()
    my_leads = leads[leads["Assigned_To"].astype(str).str.strip().eq(current_user)].copy()

    if my_leads.empty:
        st.info("You currently have no assigned leads. Click 'Request 10 Leads'.")
        return

    my_leads["Profile Link"] = my_leads["Profile URL"]

    editor_cols = [
        "_row",
        "User Name",
        "Profile Picture",
        "Join Status Text",
        "Freshness",
        "Friendship Status",
        "Status",
        "Script_Used",
        "Notes",
        "Profile Link",
    ]

    editor_df = my_leads[editor_cols].set_index("_row")
    original_df = editor_df.copy()

    with st.form("sales_update_form"):
        edited_df = st.data_editor(
            editor_df,
            use_container_width=True,
            hide_index=True,
            row_height=78,
            column_config={
                "User Name": st.column_config.TextColumn("User Name", disabled=True),
                "Profile Picture": st.column_config.ImageColumn("Profile", width=92),
                "Join Status Text": st.column_config.TextColumn("Join Status", disabled=True),
                "Freshness": st.column_config.TextColumn("Freshness", disabled=True),
                "Friendship Status": st.column_config.TextColumn("Friendship", disabled=True),
                "Status": st.column_config.SelectboxColumn("Status", options=STATUS_OPTIONS),
                "Script_Used": st.column_config.SelectboxColumn("Script Used", options=SCRIPT_OPTIONS),
                "Notes": st.column_config.TextColumn("Notes"),
                "Profile Link": st.column_config.LinkColumn("Profile", display_text="Open Profile"),
            },
            disabled=[
                "User Name",
                "Profile Picture",
                "Join Status Text",
                "Freshness",
                "Friendship Status",
                "Profile Link",
            ],
            key=f"sales_editor_{current_user}",
        )
        submitted = st.form_submit_button("💾 Save My Updates", use_container_width=True)

    if submitted:
        changed = save_sales_updates(current_user, original_df, edited_df)
        if changed:
            st.success(f"Saved updates for {changed} leads.")
        else:
            st.info("No changes detected.")
        st.rerun()


def render_admin_dashboard() -> None:
    st.subheader("Admin Dashboard")

    leads = load_leads()
    if leads.empty:
        st.info("No leads found in worksheet 'Leads'.")
        return

    processed_mask = leads["Status"].ne("New")
    updated_ts = parse_dt(leads["Updated_At"])
    today = datetime.now(timezone.utc).date()

    processed_today = int((processed_mask & (updated_ts.dt.date == today)).sum())
    processed_total = int(processed_mask.sum())

    m1, m2 = st.columns(2)
    m1.metric("Total Leads Processed (Today)", processed_today)
    m2.metric("Total Leads Processed (All Time)", processed_total)

    contacted_mask = leads["Status"].isin(["Contacted", "Interested", "Customer"])
    actor = leads["Last_Updated_By"].astype(str).str.strip()
    actor = actor.where(actor.ne(""), leads["Assigned_To"].astype(str).str.strip())
    actor = actor.where(actor.ne(""), "Unknown")

    leaderboard_df = pd.DataFrame({"User": actor, "Contacted": contacted_mask.astype(int)})
    leaderboard_df = (
        leaderboard_df.groupby("User", as_index=False)["Contacted"].sum().sort_values("Contacted", ascending=False)
    )

    status_breakdown = (
        leads[leads["Status"].isin(["Interested", "Ignore"])]
        .groupby("Status", as_index=False)
        .size()
        .rename(columns={"size": "Count"})
    )

    c1, c2 = st.columns(2)

    with c1:
        st.markdown("**Leaderboard: Leads Contacted by User**")
        if leaderboard_df.empty:
            st.info("No contacted activity yet.")
        else:
            bar_chart = (
                alt.Chart(leaderboard_df)
                .mark_bar()
                .encode(
                    x=alt.X("User:N", sort="-y"),
                    y=alt.Y("Contacted:Q"),
                    color=alt.Color("User:N", legend=None),
                    tooltip=["User", "Contacted"],
                )
                .properties(height=320)
            )
            st.altair_chart(bar_chart, use_container_width=True)

    with c2:
        st.markdown("**Status Breakdown: Interested vs Ignore**")
        if status_breakdown.empty:
            st.info("No Interested/Ignore data yet.")
        else:
            pie_chart = (
                alt.Chart(status_breakdown)
                .mark_arc(innerRadius=45)
                .encode(
                    theta=alt.Theta("Count:Q"),
                    color=alt.Color("Status:N"),
                    tooltip=["Status", "Count"],
                )
                .properties(height=320)
            )
            st.altair_chart(pie_chart, use_container_width=True)

    with st.form("admin_recycle_form"):
        st.markdown("---")
        hours = st.number_input("Recycle threshold (hours)", min_value=1, max_value=168, value=24)
        recycle_submit = st.form_submit_button("♻️ Unassign stale leads", use_container_width=True)

    if recycle_submit:
        recycled = recycle_stale_assignments(int(hours))
        if recycled:
            st.success(f"Unassigned and recycled {recycled} stale leads.")
        else:
            st.info("No stale leads to recycle.")
        st.rerun()


def main() -> None:
    st.title("FB Lead Hunter - Cloud CRM")

    try:
        users = load_users()
    except Exception as error:
        st.error(f"Cannot load users: {error}")
        st.stop()

    if "current_user" not in st.session_state:
        render_login(users)
        st.stop()

    current_user = st.session_state["current_user"]
    current_role = st.session_state.get("current_role", "Sales")
    is_admin = str(current_role).lower() == "admin" or current_user == "Admin"

    with st.sidebar:
        st.write(f"**User:** {current_user}")
        st.write(f"**Role:** {current_role}")
        if st.button("Logout", use_container_width=True):
            for key in ["current_user", "current_role", "requested_rows"]:
                if key in st.session_state:
                    del st.session_state[key]
            st.rerun()

    if is_admin:
        tab_sales, tab_admin = st.tabs(["Sales View", "Boss View"])
        with tab_sales:
            render_sales_view(current_user)
        with tab_admin:
            render_admin_dashboard()
    else:
        render_sales_view(current_user)


if __name__ == "__main__":
    main()
