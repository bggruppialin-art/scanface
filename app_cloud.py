
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import altair as alt
import gspread
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
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
SCRIPT_LIBRARY = {
    "Script A: Friendly": (
        "สวัสดีครับ เห็นว่าเพิ่งเข้ากลุ่มมา ยินดีต้อนรับนะครับ "
        "ถ้าสนใจแนวทางเริ่มต้น/เครื่องมือที่ช่วยให้ทำงานไวขึ้น ผมแชร์ให้ได้ครับ"
    ),
    "Script B: Direct": (
        "สวัสดีครับ ผมมีวิธีช่วยให้คุณได้ผลลัพธ์เร็วขึ้นจากสิ่งที่กำลังทำอยู่ "
        "ถ้าสนใจ เดี๋ยวส่งรายละเอียดสั้น ๆ ให้ดูได้เลยครับ"
    ),
}
SCRIPT_OPTIONS = ["Unassigned", *SCRIPT_LIBRARY.keys()]
CONTACTED_STATUSES = {"Contacted", "Interested"}
KEYWORDS = ["bet", "money", "invest", "เสี่ยง", "ดวง"]
DAILY_GOAL = 50

FRESHNESS_ORDER = {
    "🔥 Hot (Minutes)": 0,
    "🌤️ Warm (Hours)": 1,
    "❄️ Cold (Days+)": 2,
}


def clean_credential(value: object) -> str:
    return str(value).replace("\u00a0", " ").strip()


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_dt(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, errors="coerce", utc=True)


def parse_join_minutes(text: object) -> float:
    if pd.isna(text):
        return float("inf")

    raw = str(text).strip()
    if not raw:
        return float("inf")

    minute_match = re.search(r"(\d+)\s*นาที", raw)
    if minute_match:
        return float(int(minute_match.group(1)))

    hour_match = re.search(r"(\d+)\s*ชั่วโมง", raw)
    if hour_match:
        return float(int(hour_match.group(1)) * 60)

    day_match = re.search(r"(\d+)\s*วัน", raw)
    if day_match:
        return float(int(day_match.group(1)) * 24 * 60)

    week_match = re.search(r"(\d+)\s*สัปดาห์", raw)
    if week_match:
        return float(int(week_match.group(1)) * 7 * 24 * 60)

    month_match = re.search(r"(\d+)\s*เดือน", raw)
    if month_match:
        return float(int(month_match.group(1)) * 30 * 24 * 60)

    if "เมื่อวาน" in raw:
        return float(24 * 60)

    if "วัน" in raw:
        return float(2 * 24 * 60)

    return float("inf")


def parse_freshness(text: object) -> str:
    raw = "" if pd.isna(text) else str(text)
    if "นาที" in raw:
        return "🔥 Hot (Minutes)"
    if "ชั่วโมง" in raw:
        return "🌤️ Warm (Hours)"
    return "❄️ Cold (Days+)"


def get_sheet_setting(name: str, default: str) -> str:
    try:
        return str(st.secrets["sheets"][name])
    except Exception:
        return default


@st.cache_resource(show_spinner=False)
def get_gspread_client() -> gspread.Client:
    if "gcp_service_account_json" in st.secrets:
        raw_json = st.secrets["gcp_service_account_json"]
        creds = json.loads(raw_json) if isinstance(raw_json, str) else dict(raw_json)
    else:
        creds = dict(st.secrets["gcp_service_account"])

    if "private_key" in creds:
        private_key = str(creds["private_key"]).strip()
        if private_key.startswith('"') and private_key.endswith('"'):
            private_key = private_key[1:-1]
        private_key = private_key.replace("\\n", "\n").replace("\r\n", "\n")
        creds["private_key"] = private_key

    return gspread.service_account_from_dict(creds)


@st.cache_resource(show_spinner=False)
def get_spreadsheet() -> gspread.Spreadsheet:
    spreadsheet_id = str(st.secrets["sheets"]["spreadsheet_id"])
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

    padded_rows: List[List[str]] = []
    for row in rows:
        row = row[: len(headers)]
        if len(row) < len(headers):
            row = row + [""] * (len(headers) - len(row))
        padded_rows.append(row)

    df = pd.DataFrame(padded_rows, columns=headers)
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


@st.cache_data(ttl=10, show_spinner=False)
def load_leads() -> pd.DataFrame:
    ws_name = get_sheet_setting("leads_worksheet", "Leads")
    ws = get_worksheet(ws_name, required=True)
    ensure_lead_schema(ws)
    df, _ = read_ws_dataframe(ws)

    if df.empty:
        return pd.DataFrame(columns=[*LEAD_REQUIRED_COLUMNS, "_row"])

    for col in LEAD_REQUIRED_COLUMNS:
        if col not in df.columns:
            df[col] = ""

    df = df.fillna("")
    df["Status"] = df["Status"].astype(str).where(df["Status"].isin(STATUS_OPTIONS), "New")
    df["Script_Used"] = df["Script_Used"].astype(str).where(
        df["Script_Used"].isin(SCRIPT_OPTIONS), "Unassigned"
    )
    df["Assigned_To"] = df["Assigned_To"].astype(str)

    blank_freshness = df["Freshness"].astype(str).str.strip().eq("")
    df.loc[blank_freshness, "Freshness"] = (
        df.loc[blank_freshness, "Join Status Text"].apply(parse_freshness)
    )
    df["Join_Minutes"] = df["Join Status Text"].apply(parse_join_minutes)

    blank_lead_id = df["Lead ID"].astype(str).str.strip().eq("")
    df.loc[blank_lead_id, "Lead ID"] = df.loc[blank_lead_id, "_row"].astype(str)

    bio_text = df.get("Biography", pd.Series([""] * len(df))).fillna("").astype(str).str.lower()
    keyword_pattern = "|".join(re.escape(k) for k in KEYWORDS)
    flagged = bio_text.str.contains(keyword_pattern, regex=True)
    df["Keyword_Flag"] = flagged
    df["Lead Name"] = df["User Name"].astype(str)
    df.loc[flagged, "Lead Name"] = "⭐ " + df.loc[flagged, "User Name"].astype(str)

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
        username = clean_credential(row.get("Username", ""))
        password = clean_credential(row.get("Password", ""))
        role = clean_credential(row.get("Role", "Sales")) or "Sales"
        if username and password:
            users[username] = {"password": password, "role": role}

    return users if users else DEFAULT_USERS.copy()

def get_leads_ws_and_map() -> Tuple[gspread.Worksheet, Dict[str, int]]:
    ws_name = get_sheet_setting("leads_worksheet", "Leads")
    ws = get_worksheet(ws_name, required=True)
    headers = ensure_lead_schema(ws)
    col_map = {name: i + 1 for i, name in enumerate(headers)}
    return ws, col_map


def batch_update_cells(ws: gspread.Worksheet, updates: List[Dict[str, List[List[str]]]]) -> None:
    if not updates:
        return
    ws.batch_update(updates, value_input_option="USER_ENTERED")
    st.cache_data.clear()


def update_single_lead(current_user: str, row_no: int, status: str, script_used: str, notes: str) -> None:
    ws, col_map = get_leads_ws_and_map()
    now = now_utc_iso()

    updates = [
        {"range": rowcol_to_a1(row_no, col_map["Status"]), "values": [[status]]},
        {"range": rowcol_to_a1(row_no, col_map["Script_Used"]), "values": [[script_used]]},
        {"range": rowcol_to_a1(row_no, col_map["Notes"]), "values": [[notes]]},
        {"range": rowcol_to_a1(row_no, col_map["Updated_At"]), "values": [[now]]},
        {"range": rowcol_to_a1(row_no, col_map["Last_Updated_By"]), "values": [[current_user]]},
    ]
    batch_update_cells(ws, updates)


def save_sales_updates(current_user: str, original_df: pd.DataFrame, edited_df: pd.DataFrame) -> int:
    ws, col_map = get_leads_ws_and_map()

    editable = ["Status", "Script_Used", "Notes"]
    updates: List[Dict[str, List[List[str]]]] = []
    changed_rows: set[int] = set()

    for row_no in edited_df.index.tolist():
        if row_no not in original_df.index:
            continue

        for col in editable:
            old_val = str(original_df.at[row_no, col]) if col in original_df.columns else ""
            new_val = str(edited_df.at[row_no, col]) if col in edited_df.columns else ""
            if new_val != old_val:
                updates.append(
                    {"range": rowcol_to_a1(int(row_no), col_map[col]), "values": [[new_val]]}
                )
                changed_rows.add(int(row_no))

    now = now_utc_iso()
    for row_no in changed_rows:
        updates.append({"range": rowcol_to_a1(row_no, col_map["Updated_At"]), "values": [[now]]})
        updates.append(
            {"range": rowcol_to_a1(row_no, col_map["Last_Updated_By"]), "values": [[current_user]]}
        )

    batch_update_cells(ws, updates)
    return len(changed_rows)


def request_leads_for_user(current_user: str, count: int = 10) -> int:
    ws, col_map = get_leads_ws_and_map()
    grabbed_rows: List[int] = []

    for _ in range(3):
        if len(grabbed_rows) >= count:
            break

        latest = load_leads()
        candidates = latest[
            latest["Status"].eq("New")
            & latest["Assigned_To"].astype(str).str.strip().eq("")
        ].sort_values(by="Join_Minutes", ascending=True, na_position="last")

        if candidates.empty:
            break

        need = count - len(grabbed_rows)
        chunk = candidates.head(need)
        now = now_utc_iso()

        updates: List[Dict[str, List[List[str]]]] = []
        for _, row in chunk.iterrows():
            row_no = int(row["_row"])
            updates.extend(
                [
                    {"range": rowcol_to_a1(row_no, col_map["Assigned_To"]), "values": [[current_user]]},
                    {"range": rowcol_to_a1(row_no, col_map["Updated_At"]), "values": [[now]]},
                    {"range": rowcol_to_a1(row_no, col_map["Last_Updated_By"]), "values": [[current_user]]},
                ]
            )

        batch_update_cells(ws, updates)

        verify = load_leads()
        claimed = verify[
            verify["_row"].isin(chunk["_row"])
            & verify["Assigned_To"].astype(str).eq(current_user)
        ]["_row"].astype(int).tolist()

        grabbed_rows.extend(claimed)
        grabbed_rows = list(dict.fromkeys(grabbed_rows))

    st.session_state["requested_rows"] = grabbed_rows
    return len(grabbed_rows)


def launch_top_uncontacted(current_user: str, script_used: str, count: int = 5) -> Tuple[int, List[str]]:
    leads = load_leads()
    candidates = leads[
        leads["Assigned_To"].astype(str).eq(current_user)
        & leads["Status"].eq("New")
        & leads["Profile URL"].astype(str).str.strip().ne("")
    ].sort_values(by="Join_Minutes", ascending=True, na_position="last")

    if candidates.empty:
        return 0, []

    top = candidates.head(count)
    ws, col_map = get_leads_ws_and_map()
    now = now_utc_iso()

    updates: List[Dict[str, List[List[str]]]] = []
    for _, row in top.iterrows():
        row_no = int(row["_row"])
        updates.extend(
            [
                {"range": rowcol_to_a1(row_no, col_map["Status"]), "values": [["Contacted"]]},
                {"range": rowcol_to_a1(row_no, col_map["Script_Used"]), "values": [[script_used]]},
                {"range": rowcol_to_a1(row_no, col_map["Updated_At"]), "values": [[now]]},
                {"range": rowcol_to_a1(row_no, col_map["Last_Updated_By"]), "values": [[current_user]]},
            ]
        )

    batch_update_cells(ws, updates)
    return len(top), top["Profile URL"].astype(str).tolist()


def purge_cold_leads_for_user(current_user: str) -> int:
    leads = load_leads()
    purge_df = leads[
        leads["Assigned_To"].astype(str).eq(current_user)
        & leads["Freshness"].isin(["❄️ Cold (Days+)", "Cold (Days+)"])
        & leads["Status"].isin(["New", "Ignore"])
    ].copy()

    if purge_df.empty:
        return 0

    ws, _ = get_leads_ws_and_map()
    row_numbers = sorted(purge_df["_row"].astype(int).tolist(), reverse=True)
    for row_no in row_numbers:
        ws.delete_rows(row_no)

    st.cache_data.clear()
    return len(row_numbers)


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
    stale = leads[stale_mask]
    if stale.empty:
        return 0

    now = now_utc_iso()
    updates: List[Dict[str, List[List[str]]]] = []
    for _, row in stale.iterrows():
        row_no = int(row["_row"])
        updates.extend(
            [
                {"range": rowcol_to_a1(row_no, col_map["Assigned_To"]), "values": [[""]]},
                {"range": rowcol_to_a1(row_no, col_map["Status"]), "values": [["New"]]},
                {"range": rowcol_to_a1(row_no, col_map["Updated_At"]), "values": [[now]]},
                {"range": rowcol_to_a1(row_no, col_map["Last_Updated_By"]), "values": [["Admin"]]},
            ]
        )

    batch_update_cells(ws, updates)
    return len(stale)


def render_copy_button(text: str, key: str, label: str = "Copy to Clipboard") -> None:
    safe_key = re.sub(r"[^a-zA-Z0-9_-]", "_", key)
    safe_text = json.dumps(text or "")

    components.html(
        f"""
        <button id=\"{safe_key}\" style=\"width:100%;padding:8px 12px;border:1px solid #CBD5E1;border-radius:8px;background:#fff;cursor:pointer;\">
            {label}
        </button>
        <script>
            const btn = document.getElementById("{safe_key}");
            btn.onclick = async () => {{
                try {{
                    await navigator.clipboard.writeText({safe_text});
                    btn.innerText = "Copied!";
                }} catch (e) {{
                    btn.innerText = "Copy failed";
                }}
                setTimeout(() => btn.innerText = {json.dumps(label)}, 1200);
            }};
        </script>
        """,
        height=48,
    )


def render_open_tabs(urls: List[str], nonce: str) -> None:
    payload = json.dumps(urls)
    components.html(
        f"""
        <div id=\"launch_{nonce}\" style=\"display:none\"></div>
        <script>
            const urls = {payload};
            urls.forEach((url, i) => setTimeout(() => window.open(url, '_blank'), i * 150));
        </script>
        """,
        height=0,
    )


def script_manager_sidebar() -> Tuple[str, str]:
    if "scripts" not in st.session_state:
        st.session_state["scripts"] = SCRIPT_LIBRARY.copy()

    st.sidebar.markdown("---")
    st.sidebar.subheader("Sales Scripts")
    script_name = st.sidebar.radio("Choose script", list(st.session_state["scripts"].keys()), key="script_choice")

    txt_key = f"script_text_{script_name}"
    if txt_key not in st.session_state:
        st.session_state[txt_key] = st.session_state["scripts"][script_name]

    script_text = st.sidebar.text_area("Selected script", key=txt_key, height=170)
    st.session_state["scripts"][script_name] = script_text

    render_copy_button(script_text, key=f"copy_sidebar_{script_name}")
    return script_name, script_text


def render_login(users: Dict[str, Dict[str, str]]) -> None:
    st.subheader("Login")
    with st.form("login_form"):
        username = st.selectbox("Username", sorted(users.keys()))
        password = st.text_input("Password", type="password")
        submit = st.form_submit_button("Login", use_container_width=True)

    if submit:
        account = users.get(username)
        if account and clean_credential(password) == clean_credential(account["password"]):
            st.session_state["current_user"] = username
            st.session_state["current_role"] = account.get("role", "Sales")
            st.success("Login successful")
            st.rerun()
        else:
            st.error("Invalid username or password")


def apply_sales_filters(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    st.sidebar.markdown("---")
    st.sidebar.subheader("Filters")

    freshness_options = sorted(
        df["Freshness"].dropna().astype(str).unique().tolist(),
        key=lambda x: FRESHNESS_ORDER.get(x, 99),
    )
    friend_options = sorted(df["Friendship Status"].dropna().astype(str).unique().tolist())
    status_options = sorted(df["Status"].dropna().astype(str).unique().tolist())

    selected_fresh = st.sidebar.multiselect("Freshness", freshness_options, default=freshness_options)
    selected_friend = st.sidebar.multiselect("Friendship", friend_options, default=friend_options)
    selected_status = st.sidebar.multiselect("Status", status_options, default=status_options)
    only_keyword = st.sidebar.checkbox("Keyword alert only (⭐)", value=False)

    filtered = df[
        df["Freshness"].isin(selected_fresh)
        & df["Friendship Status"].astype(str).isin(selected_friend)
        & df["Status"].astype(str).isin(selected_status)
    ].copy()

    if only_keyword:
        filtered = filtered[filtered["Keyword_Flag"] == True].copy()  # noqa: E712

    return filtered.sort_values(by="Join_Minutes", ascending=True, na_position="last")

def render_war_room_analytics(df: pd.DataFrame, title: str) -> None:
    st.subheader(title)

    total = len(df)
    new_count = int((df["Status"] == "New").sum()) if total else 0
    interested_count = int((df["Status"] == "Interested").sum()) if total else 0
    contacted_count = int(df["Status"].isin(CONTACTED_STATUSES).sum()) if total else 0
    denom = new_count + interested_count
    conversion = (interested_count / denom * 100) if denom else 0.0

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Total Leads", f"{total:,}")
    m2.metric("Interested", f"{interested_count:,}")
    m3.metric("Contacted + Interested", f"{contacted_count:,}")
    m4.metric("Conversion (New -> Interested)", f"{conversion:.1f}%")

    c1, c2 = st.columns(2)
    with c1:
        dist = df.groupby("Freshness", dropna=False).size().reset_index(name="Count")
        if dist.empty:
            st.info("No data for Freshness chart")
        else:
            pie = (
                alt.Chart(dist)
                .mark_arc(innerRadius=45)
                .encode(
                    theta=alt.Theta("Count:Q"),
                    color=alt.Color("Freshness:N"),
                    tooltip=["Freshness", "Count"],
                )
                .properties(height=300)
            )
            st.altair_chart(pie, use_container_width=True)

    with c2:
        scoped = df[df["Script_Used"].astype(str) != "Unassigned"].copy()
        if scoped.empty:
            st.info("No script usage data yet")
        else:
            perf = (
                scoped.groupby("Script_Used", dropna=False)
                .agg(
                    total=("Lead ID", "count"),
                    interested=("Status", lambda s: int((s == "Interested").sum())),
                )
                .reset_index()
            )
            perf["success_rate"] = ((perf["interested"] / perf["total"]) * 100).round(2)

            bar = (
                alt.Chart(perf)
                .mark_bar()
                .encode(
                    x=alt.X("Script_Used:N", sort="-y"),
                    y=alt.Y("interested:Q", title="Interested"),
                    color=alt.Color("Script_Used:N", legend=None),
                    tooltip=["Script_Used", "total", "interested", "success_rate"],
                )
                .properties(height=300)
            )
            st.altair_chart(bar, use_container_width=True)
            st.dataframe(perf, use_container_width=True, hide_index=True)


def render_sales_workspace(current_user: str, selected_script_name: str, selected_script_text: str) -> None:
    st.subheader("Sales Workspace")

    top_c1, top_c2, top_c3, top_c4 = st.columns(4)

    if top_c1.button("📥 Request 10 Leads", use_container_width=True):
        claimed = request_leads_for_user(current_user, 10)
        if claimed:
            st.success(f"Assigned {claimed} leads to {current_user}")
        else:
            st.warning("No available New leads")
        st.rerun()

    if top_c2.button("🚀 Launch Top 5 Uncontacted", use_container_width=True):
        launched_count, urls = launch_top_uncontacted(current_user, selected_script_name, 5)
        if launched_count:
            st.session_state["pending_launch_urls"] = urls
            st.session_state["pending_launch_nonce"] = uuid.uuid4().hex
            st.success(f"Launched {launched_count} profiles and marked Contacted")
        else:
            st.info("No uncontacted leads with valid profile URL")
        st.rerun()

    if top_c3.button("🧹 Purge Cold Leads", use_container_width=True):
        removed = purge_cold_leads_for_user(current_user)
        if removed:
            st.success(f"Purged {removed} cold leads")
        else:
            st.info("No cold leads to purge")
        st.rerun()

    if top_c4.button("Refresh", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

    if st.session_state.get("pending_launch_urls"):
        urls = st.session_state.pop("pending_launch_urls")
        nonce = st.session_state.pop("pending_launch_nonce", uuid.uuid4().hex)
        render_open_tabs(urls, nonce)

    leads = load_leads()
    my_df = leads[leads["Assigned_To"].astype(str).eq(current_user)].copy()

    if my_df.empty:
        st.info("You have no assigned leads. Click 'Request 10 Leads'.")
        return

    filtered_df = apply_sales_filters(my_df)

    contacted_count = int(filtered_df["Status"].isin(CONTACTED_STATUSES).sum()) if not filtered_df.empty else 0
    progress_val = min(contacted_count / DAILY_GOAL, 1.0)
    st.progress(progress_val, text=f"Daily Goal: Contacted {contacted_count} / {DAILY_GOAL} users")

    m1, m2, m3 = st.columns(3)
    m1.metric("Assigned Leads", f"{len(my_df):,}")
    m2.metric("Filtered Leads", f"{len(filtered_df):,}")
    m3.metric("Interested", int((my_df["Status"] == "Interested").sum()))

    list_tab, analytics_tab = st.tabs(["List View", "War Room Analytics"])

    with list_tab:
        left, right = st.columns([7, 3], gap="large")

        with left:
            st.markdown("### Lead List")
            if filtered_df.empty:
                st.warning("No leads match current filters")
            else:
                table_cols = [
                    "_row",
                    "Profile Picture",
                    "Lead Name",
                    "Join Status Text",
                    "Freshness",
                    "Friendship Status",
                    "Status",
                    "Script_Used",
                    "Profile URL",
                ]
                table_df = filtered_df[table_cols].copy()

                event = st.dataframe(
                    table_df,
                    use_container_width=True,
                    hide_index=True,
                    row_height=80,
                    height=max(280, 58 + len(table_df) * 80),
                    on_select="rerun",
                    selection_mode="single-row",
                    key=f"lead_list_{current_user}",
                    column_config={
                        "_row": st.column_config.NumberColumn("Row", width=58),
                        "Profile Picture": st.column_config.ImageColumn("Profile", width=90),
                        "Lead Name": st.column_config.TextColumn("User Name", width=190),
                        "Join Status Text": st.column_config.TextColumn("Join Status", width=210),
                        "Freshness": st.column_config.TextColumn("Freshness", width=125),
                        "Friendship Status": st.column_config.TextColumn("Friendship", width=130),
                        "Status": st.column_config.TextColumn("Status", width=110),
                        "Script_Used": st.column_config.TextColumn("Script", width=120),
                        "Profile URL": st.column_config.LinkColumn("Profile", display_text="Open", width=85),
                    },
                )
                st.caption("⭐ means keyword found in Biography")

                rows = event.selection.rows if hasattr(event, "selection") else []
                if rows:
                    idx = rows[0]
                    if 0 <= idx < len(filtered_df):
                        st.session_state[f"selected_row_{current_user}"] = int(filtered_df.iloc[idx]["_row"])

            st.markdown("#### Bulk Editor")
            if filtered_df.empty:
                st.info("No leads to edit")
            else:
                bulk_df = filtered_df[["_row", "Lead Name", "Status", "Script_Used", "Notes"]].set_index("_row")
                original_bulk = bulk_df.copy()

                with st.form(f"bulk_form_{current_user}"):
                    edited_bulk = st.data_editor(
                        bulk_df,
                        use_container_width=True,
                        hide_index=True,
                        column_config={
                            "Lead Name": st.column_config.TextColumn("User Name", disabled=True),
                            "Status": st.column_config.SelectboxColumn("Status", options=STATUS_OPTIONS),
                            "Script_Used": st.column_config.SelectboxColumn("Script", options=SCRIPT_OPTIONS),
                            "Notes": st.column_config.TextColumn("Notes"),
                        },
                        disabled=["Lead Name"],
                        key=f"bulk_editor_{current_user}",
                    )
                    bulk_submit = st.form_submit_button("💾 Save Bulk Updates", use_container_width=True)

                if bulk_submit:
                    changed = save_sales_updates(current_user, original_bulk, edited_bulk)
                    if changed:
                        st.success(f"Saved {changed} updated leads")
                    else:
                        st.info("No changes detected")
                    st.rerun()

        with right:
            st.markdown("### Action Panel")
            if filtered_df.empty:
                st.info("No selected lead")
            else:
                selected_key = f"selected_row_{current_user}"
                filtered_rows = filtered_df["_row"].astype(int).tolist()

                if selected_key not in st.session_state or st.session_state[selected_key] not in filtered_rows:
                    st.session_state[selected_key] = filtered_rows[0]

                row_no = int(st.session_state[selected_key])
                selected = filtered_df[filtered_df["_row"].astype(int).eq(row_no)].iloc[0]

                if str(selected["Profile Picture"]).strip():
                    st.image(selected["Profile Picture"], width=190)

                st.write(f"**Name:** {selected['Lead Name']}")
                st.write(f"**Join Status:** {selected['Join Status Text']}")
                st.write(f"**Freshness:** {selected['Freshness']}")
                st.write(f"**Friendship:** {selected['Friendship Status']}")

                if bool(selected.get("Keyword_Flag", False)):
                    st.warning("Keyword alert in Biography")

                if str(selected["Profile URL"]).strip():
                    st.link_button("Open Facebook Profile", selected["Profile URL"], use_container_width=True)

                st.markdown(f"**{selected_script_name}**")
                st.code(selected_script_text, language="text")
                render_copy_button(selected_script_text, key=f"copy_panel_{current_user}_{row_no}")

                with st.form(f"action_form_{current_user}_{row_no}"):
                    status_val = st.selectbox(
                        "Quick Status Update",
                        STATUS_OPTIONS,
                        index=STATUS_OPTIONS.index(selected["Status"]) if selected["Status"] in STATUS_OPTIONS else 0,
                    )
                    script_val = st.selectbox(
                        "Script Used",
                        SCRIPT_OPTIONS,
                        index=SCRIPT_OPTIONS.index(selected["Script_Used"])
                        if selected["Script_Used"] in SCRIPT_OPTIONS
                        else 0,
                    )
                    notes_val = st.text_area("Notes", value=str(selected["Notes"]), height=140)
                    submit_action = st.form_submit_button("Save This Lead", use_container_width=True)

                if submit_action:
                    update_single_lead(current_user, row_no, status_val, script_val, notes_val)
                    st.success("Lead updated")
                    st.rerun()

    with analytics_tab:
        render_war_room_analytics(my_df, "War Room Analytics (My Leads)")

def render_admin_dashboard() -> None:
    leads = load_leads()
    st.subheader("Boss View")

    if leads.empty:
        st.info("No leads in worksheet 'Leads'")
        return

    overview_tab, analytics_tab = st.tabs(["Command Center", "War Room Analytics"])

    with overview_tab:
        processed_mask = leads["Status"].ne("New")
        updated_ts = parse_dt(leads["Updated_At"])
        today = datetime.now(timezone.utc).date()
        processed_today = int((processed_mask & (updated_ts.dt.date == today)).sum())
        processed_total = int(processed_mask.sum())

        m1, m2 = st.columns(2)
        m1.metric("Total Leads Processed (Today)", processed_today)
        m2.metric("Total Leads Processed (All Time)", processed_total)

        actor = leads["Last_Updated_By"].astype(str).str.strip()
        actor = actor.where(actor.ne(""), leads["Assigned_To"].astype(str).str.strip())
        actor = actor.where(actor.ne(""), "Unknown")
        contacted = leads["Status"].isin(["Contacted", "Interested", "Customer"]).astype(int)

        leaderboard = (
            pd.DataFrame({"User": actor, "Contacted": contacted})
            .groupby("User", as_index=False)["Contacted"]
            .sum()
            .sort_values("Contacted", ascending=False)
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
            if leaderboard.empty:
                st.info("No data")
            else:
                bar = (
                    alt.Chart(leaderboard)
                    .mark_bar()
                    .encode(
                        x=alt.X("User:N", sort="-y"),
                        y=alt.Y("Contacted:Q"),
                        color=alt.Color("User:N", legend=None),
                        tooltip=["User", "Contacted"],
                    )
                    .properties(height=320)
                )
                st.altair_chart(bar, use_container_width=True)

        with c2:
            st.markdown("**Status Breakdown: Interested vs Ignore**")
            if status_breakdown.empty:
                st.info("No data")
            else:
                pie = (
                    alt.Chart(status_breakdown)
                    .mark_arc(innerRadius=45)
                    .encode(
                        theta=alt.Theta("Count:Q"),
                        color=alt.Color("Status:N"),
                        tooltip=["Status", "Count"],
                    )
                    .properties(height=320)
                )
                st.altair_chart(pie, use_container_width=True)

        with st.form("admin_recycle_form"):
            hours = st.number_input("Unassign if inactive for (hours)", min_value=1, max_value=168, value=24)
            recycle = st.form_submit_button("♻️ Unassign stale leads", use_container_width=True)

        if recycle:
            recycled = recycle_stale_assignments(int(hours))
            if recycled:
                st.success(f"Recycled {recycled} stale leads")
            else:
                st.info("No stale leads to recycle")
            st.rerun()

    with analytics_tab:
        render_war_room_analytics(leads, "War Room Analytics (All Leads)")


def main() -> None:
    st.title("FB Lead Hunter - Cloud CRM")

    users_error: Optional[str] = None
    try:
        users = load_users()
    except Exception as error:
        users = DEFAULT_USERS.copy()
        users_error = str(error)

    if users_error:
        st.warning(
            "Google Sheets user auth is unavailable right now. "
            "Using fallback users from code. "
            f"Error: {users_error}"
        )

    if "current_user" not in st.session_state:
        render_login(users)
        st.stop()

    current_user = st.session_state["current_user"]
    current_role = st.session_state.get("current_role", "Sales")
    is_admin = current_user == "Admin" or str(current_role).lower() == "admin"

    with st.sidebar:
        st.write(f"**User:** {current_user}")
        st.write(f"**Role:** {current_role}")
        if st.button("Logout", use_container_width=True):
            for key in [
                "current_user",
                "current_role",
                "requested_rows",
                f"selected_row_{current_user}",
                "pending_launch_urls",
                "pending_launch_nonce",
            ]:
                if key in st.session_state:
                    del st.session_state[key]
            st.rerun()

    selected_script_name, selected_script_text = script_manager_sidebar()

    try:
        if is_admin:
            sales_tab, boss_tab = st.tabs(["Sales View", "Boss View"])
            with sales_tab:
                render_sales_workspace(current_user, selected_script_name, selected_script_text)
            with boss_tab:
                render_admin_dashboard()
        else:
            render_sales_workspace(current_user, selected_script_name, selected_script_text)
    except Exception as error:
        st.error(f"Google Sheets connection error: {error}")
        st.info("Please verify secrets, sheet sharing (Editor), and worksheet names Leads / Users.")


if __name__ == "__main__":
    main()
