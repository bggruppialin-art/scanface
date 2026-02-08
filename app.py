import hashlib
import io
import json
import re
import uuid
from typing import Optional, Tuple

import altair as alt
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components


st.set_page_config(page_title="FB Lead Hunter", layout="wide")

REQUIRED_COLUMNS = [
    "User Name",
    "Profile URL",
    "Profile Picture",
    "Join Status Text",
    "Friendship Status",
]

STATUS_OPTIONS = ["New", "Contacted", "Interested", "Customer", "Ignore"]
CONTACTED_STATUSES = {"Contacted", "Interested"}
DAILY_GOAL = 50

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

KEYWORD_LIST = ["bet", "money", "invest", "เสี่ยง", "ดวง"]

FRESHNESS_ORDER = {
    "🔥 Hot (Minutes)": 0,
    "🌤️ Warm (Hours)": 1,
    "❄️ Cold (Days+)": 2,
}

TABLE_ROW_HEIGHT = 78


def inject_ui_styles() -> None:
    st.markdown(
        """
        <link href="https://cdn.jsdelivr.net/npm/tailwindcss@2.2.19/dist/tailwind.min.css" rel="stylesheet">
        <style>
            .block-container {
                max-width: 100% !important;
                padding-top: 1.1rem;
                padding-bottom: 1.6rem;
            }

            [data-testid="stSidebar"] {
                border-right: 1px solid #E2E8F0;
            }

            [data-testid="stMetricValue"] {
                color: #0F172A;
                font-size: 1.95rem;
                font-weight: 700;
            }

            [data-testid="stDataFrame"] [role="gridcell"] {
                padding-left: 0.24rem !important;
                padding-right: 0.24rem !important;
            }

            [data-testid="stDataFrame"] [role="columnheader"] {
                background: #F8FAFC;
                color: #334155;
                font-weight: 600;
            }

            .tw-panel {
                border: 1px solid #E2E8F0;
                border-radius: 14px;
                padding: 14px;
                background: #FFFFFF;
                box-shadow: 0 1px 2px rgba(15, 23, 42, 0.08);
            }

            .tw-title {
                font-size: 1.9rem;
                line-height: 2.2rem;
                font-weight: 800;
                color: #0F172A;
                margin-bottom: 0.15rem;
            }

            .tw-subtitle {
                color: #475569;
                margin-bottom: 0.6rem;
            }
        </style>
        """,
        unsafe_allow_html=True,
    )


def parse_join_status(join_text: object) -> Tuple[str, float]:
    """Parse Thai natural-language text into freshness category + sortable minutes."""
    if pd.isna(join_text):
        return "❄️ Cold (Days+)", float("inf")

    text = str(join_text).strip()
    if not text:
        return "❄️ Cold (Days+)", float("inf")

    # Thai parsing logic:
    # - contains "นาที"    -> Hot
    # - contains "ชั่วโมง" -> Warm
    # - otherwise           -> Cold
    minute_match = re.search(r"(\d+)\s*นาที", text)
    if minute_match:
        return "🔥 Hot (Minutes)", float(int(minute_match.group(1)))

    hour_match = re.search(r"(\d+)\s*ชั่วโมง", text)
    if hour_match:
        return "🌤️ Warm (Hours)", float(int(hour_match.group(1)) * 60)

    day_match = re.search(r"(\d+)\s*วัน", text)
    if day_match:
        return "❄️ Cold (Days+)", float(int(day_match.group(1)) * 24 * 60)

    week_match = re.search(r"(\d+)\s*สัปดาห์", text)
    if week_match:
        return "❄️ Cold (Days+)", float(int(week_match.group(1)) * 7 * 24 * 60)

    month_match = re.search(r"(\d+)\s*เดือน", text)
    if month_match:
        return "❄️ Cold (Days+)", float(int(month_match.group(1)) * 30 * 24 * 60)

    if "เมื่อวาน" in text:
        return "❄️ Cold (Days+)", float(24 * 60)

    if "วัน" in text:
        return "❄️ Cold (Days+)", float(2 * 24 * 60)

    return "❄️ Cold (Days+)", float("inf")


def normalize_status(series: pd.Series) -> pd.Series:
    cleaned = series.fillna("New").astype(str).str.strip()
    return cleaned.where(cleaned.isin(STATUS_OPTIONS), "New")


def normalize_script_used(series: pd.Series, script_names: list[str]) -> pd.Series:
    valid_scripts = {"Unassigned", *script_names}
    cleaned = series.fillna("Unassigned").astype(str).str.strip()
    return cleaned.where(cleaned.isin(valid_scripts), "Unassigned")


def ensure_lead_ids(df: pd.DataFrame) -> pd.DataFrame:
    prepared = df.copy()

    if "Lead ID" not in prepared.columns:
        prepared.insert(0, "Lead ID", range(1, len(prepared) + 1))
        return prepared

    ids = pd.to_numeric(prepared["Lead ID"], errors="coerce")
    used = set()
    next_id = 1
    fixed_ids = []

    for raw_value in ids:
        if pd.notna(raw_value):
            candidate = int(raw_value)
            if candidate > 0 and candidate not in used:
                fixed_ids.append(candidate)
                used.add(candidate)
                continue

        while next_id in used:
            next_id += 1
        fixed_ids.append(next_id)
        used.add(next_id)

    prepared["Lead ID"] = fixed_ids
    return prepared


def apply_keyword_flags(df: pd.DataFrame) -> pd.DataFrame:
    prepared = df.copy()

    if "Biography" not in prepared.columns:
        prepared["Biography"] = ""

    bio_series = prepared["Biography"].fillna("").astype(str).str.lower()
    keyword_pattern = "|".join(re.escape(keyword) for keyword in KEYWORD_LIST)
    flagged_mask = bio_series.str.contains(keyword_pattern, regex=True)

    prepared["Keyword Flag"] = flagged_mask
    prepared["Lead Name"] = prepared["User Name"]
    prepared.loc[flagged_mask, "Lead Name"] = "⭐ " + prepared.loc[flagged_mask, "User Name"]
    return prepared


def preprocess_data(df: pd.DataFrame, script_names: list[str]) -> pd.DataFrame:
    prepared = df.copy()

    for col in REQUIRED_COLUMNS:
        if col not in prepared.columns:
            prepared[col] = pd.NA

    prepared = ensure_lead_ids(prepared)

    parsed = prepared["Join Status Text"].apply(parse_join_status)
    prepared["Freshness"] = parsed.map(lambda x: x[0])
    prepared["Join Minutes Ago"] = parsed.map(lambda x: x[1])

    if "Status" not in prepared.columns:
        prepared["Status"] = "New"
    prepared["Status"] = normalize_status(prepared["Status"])

    if "Notes" not in prepared.columns:
        prepared["Notes"] = ""

    if "Script Used" not in prepared.columns:
        prepared["Script Used"] = "Unassigned"
    prepared["Script Used"] = normalize_script_used(prepared["Script Used"], script_names)

    prepared["Notes"] = prepared["Notes"].fillna("").astype(str)
    prepared["Friendship Status"] = prepared["Friendship Status"].fillna("Unknown").astype(str)
    prepared["User Name"] = prepared["User Name"].fillna("(No Name)").astype(str)
    prepared["Profile URL"] = prepared["Profile URL"].fillna("").astype(str)
    prepared["Profile Picture"] = prepared["Profile Picture"].fillna("").astype(str)

    prepared = apply_keyword_flags(prepared)
    return prepared


@st.cache_data(show_spinner=False)
def load_data(file_name: str, file_bytes: bytes) -> pd.DataFrame:
    suffix = file_name.lower().split(".")[-1]

    if suffix == "csv":
        try:
            return pd.read_csv(io.BytesIO(file_bytes), encoding="utf-8")
        except Exception as utf8_error:
            try:
                return pd.read_csv(io.BytesIO(file_bytes), encoding="cp874")
            except Exception as cp874_error:
                raise ValueError(
                    f"Cannot read CSV as utf-8 ({utf8_error}) or cp874 ({cp874_error})."
                ) from cp874_error

    if suffix in {"xlsx", "xls"}:
        try:
            return pd.read_excel(io.BytesIO(file_bytes))
        except Exception as excel_error:
            raise ValueError(f"Cannot read Excel file: {excel_error}") from excel_error

    raise ValueError("Unsupported file format. Please upload .csv or .xlsx")


def sort_freshness_values(values: pd.Series) -> list[str]:
    unique_values = values.dropna().astype(str).unique().tolist()
    return sorted(unique_values, key=lambda value: FRESHNESS_ORDER.get(value, 99))


def render_copy_button(text: str, key: str, label: str = "Copy to Clipboard") -> None:
    safe_key = re.sub(r"[^a-zA-Z0-9_-]", "_", key)
    safe_text = json.dumps(text or "")
    safe_label = json.dumps(label)

    components.html(
        f"""
        <button id="{safe_key}" style="width:100%;padding:8px 12px;border:1px solid #CBD5E1;border-radius:8px;background:#fff;cursor:pointer;font-weight:600;">
            {label}
        </button>
        <script>
            const btn = document.getElementById("{safe_key}");
            btn.onclick = async () => {{
                const original = {safe_label};
                try {{
                    await navigator.clipboard.writeText({safe_text});
                    btn.innerText = "Copied!";
                }} catch (err) {{
                    btn.innerText = "Copy failed";
                }}
                setTimeout(() => {{
                    btn.innerText = original;
                }}, 1200);
            }};
        </script>
        """,
        height=48,
    )


def render_launch_tabs(urls: list[str], nonce: str) -> None:
    payload = json.dumps(urls)
    components.html(
        f"""
        <script>
            const urls = {payload};
            urls.forEach((url, index) => {{
                setTimeout(() => window.open(url, '_blank'), index * 140);
            }});
        </script>
        """,
        height=0,
        key=f"launch_tabs_{nonce}",
    )


def script_manager_sidebar() -> Tuple[str, str, list[str]]:
    if "scripts" not in st.session_state:
        st.session_state["scripts"] = SCRIPT_LIBRARY.copy()

    st.sidebar.markdown("---")
    st.sidebar.subheader("Sales Scripts")

    script_names = list(st.session_state["scripts"].keys())
    selected_script_name = st.sidebar.radio(
        "Choose script",
        options=script_names,
        key="script_choice",
    )

    script_key = f"script_text_{selected_script_name}"
    if script_key not in st.session_state:
        st.session_state[script_key] = st.session_state["scripts"][selected_script_name]

    script_text = st.sidebar.text_area(
        "Selected script",
        key=script_key,
        height=160,
    )
    st.session_state["scripts"][selected_script_name] = script_text

    render_copy_button(script_text, key=f"sidebar_copy_{selected_script_name}")
    return selected_script_name, script_text, script_names


def save_data(df: pd.DataFrame, output_path: str = "updated_leads.csv") -> None:
    df.to_csv(output_path, index=False, encoding="utf-8-sig")


def init_working_df(uploaded_file, script_names: list[str]) -> Optional[pd.DataFrame]:
    file_bytes = uploaded_file.getvalue()
    file_token = hashlib.md5(file_bytes).hexdigest()

    if st.session_state.get("active_file_token") == file_token and "working_df" in st.session_state:
        return preprocess_data(st.session_state["working_df"], script_names)

    try:
        raw_df = load_data(uploaded_file.name, file_bytes)
    except Exception as error:
        st.error(f"Failed to read file: {error}")
        return None

    missing_columns = [col for col in REQUIRED_COLUMNS if col not in raw_df.columns]
    if missing_columns:
        st.error(
            "Missing required columns: "
            + ", ".join(missing_columns)
            + "\n\nExpected columns: "
            + ", ".join(REQUIRED_COLUMNS)
        )
        return None

    prepared = preprocess_data(raw_df, script_names)
    st.session_state["working_df"] = prepared
    st.session_state["active_file_token"] = file_token
    return prepared


def choose_selected_lead(filtered_df: pd.DataFrame, table_event) -> Optional[int]:
    selected_rows = []
    if hasattr(table_event, "selection"):
        selected_rows = table_event.selection.rows

    if selected_rows:
        selected_idx = selected_rows[0]
        if 0 <= selected_idx < len(filtered_df):
            st.session_state["selected_lead_id"] = int(filtered_df.iloc[selected_idx]["Lead ID"])

    filtered_ids = filtered_df["Lead ID"].tolist()
    if not filtered_ids:
        return None

    if "selected_lead_id" not in st.session_state:
        st.session_state["selected_lead_id"] = int(filtered_ids[0])
    elif st.session_state["selected_lead_id"] not in filtered_ids:
        st.session_state["selected_lead_id"] = int(filtered_ids[0])

    return int(st.session_state["selected_lead_id"])


def purge_cold_leads(df: pd.DataFrame) -> Tuple[pd.DataFrame, int]:
    cold_mask = df["Freshness"].isin(["❄️ Cold (Days+)", "Cold (Days+)"])
    removable_status = df["Status"].isin(["New", "Ignore"])
    purge_mask = cold_mask & removable_status

    removed_count = int(purge_mask.sum())
    if removed_count == 0:
        return df.copy(), 0

    cleaned_df = df.loc[~purge_mask].copy()
    return cleaned_df, removed_count


def launch_top_uncontacted(df: pd.DataFrame, selected_script_name: str) -> Tuple[pd.DataFrame, list[str]]:
    candidates = df[
        (df["Status"] == "New")
        & df["Profile URL"].astype(str).str.strip().ne("")
    ].copy()

    if candidates.empty:
        return df.copy(), []

    top5 = candidates.sort_values(by="Join Minutes Ago", ascending=True).head(5)
    top_ids = top5["Lead ID"].tolist()
    urls = top5["Profile URL"].astype(str).tolist()

    updated_df = df.copy()
    update_mask = updated_df["Lead ID"].isin(top_ids)
    updated_df.loc[update_mask, "Status"] = "Contacted"
    updated_df.loc[update_mask, "Script Used"] = selected_script_name

    return updated_df, urls


def build_script_effectiveness(df: pd.DataFrame) -> pd.DataFrame:
    scoped = df[df["Script Used"].astype(str) != "Unassigned"].copy()
    if scoped.empty:
        return pd.DataFrame(columns=["Script Used", "Total Tagged", "Interested", "Success Rate %"])

    summary = (
        scoped.groupby("Script Used", dropna=False)
        .agg(
            **{
                "Total Tagged": ("Lead ID", "count"),
                "Interested": ("Status", lambda s: int((s == "Interested").sum())),
            }
        )
        .reset_index()
    )

    summary["Success Rate %"] = (
        (summary["Interested"] / summary["Total Tagged"].replace(0, pd.NA)) * 100
    ).fillna(0).round(2)

    return summary.sort_values(by="Interested", ascending=False)


def render_analytics_tab(df: pd.DataFrame) -> None:
    st.markdown('<div class="tw-panel">', unsafe_allow_html=True)
    st.subheader("War Room Analytics")

    total = len(df)
    new_count = int((df["Status"] == "New").sum())
    interested_count = int((df["Status"] == "Interested").sum())
    contacted_count = int(df["Status"].isin(CONTACTED_STATUSES).sum())

    conversion_denom = new_count + interested_count
    conversion_rate = (interested_count / conversion_denom * 100) if conversion_denom else 0.0

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Leads", f"{total:,}")
    c2.metric("Interested", f"{interested_count:,}")
    c3.metric("Contacted + Interested", f"{contacted_count:,}")
    c4.metric("Conversion (New -> Interested)", f"{conversion_rate:.1f}%")

    chart_col1, chart_col2 = st.columns(2)

    with chart_col1:
        st.markdown("**Lead Freshness Distribution**")
        freshness_dist = (
            df.groupby("Freshness", dropna=False)
            .size()
            .reset_index(name="Count")
        )

        if freshness_dist.empty:
            st.info("No data available for freshness chart.")
        else:
            pie_chart = (
                alt.Chart(freshness_dist)
                .mark_arc(innerRadius=45)
                .encode(
                    theta=alt.Theta(field="Count", type="quantitative"),
                    color=alt.Color(field="Freshness", type="nominal"),
                    tooltip=["Freshness", "Count"],
                )
                .properties(height=300)
            )
            st.altair_chart(pie_chart, use_container_width=True)

    with chart_col2:
        st.markdown("**Script Effectiveness (Interested Outcomes)**")
        script_perf = build_script_effectiveness(df)

        if script_perf.empty:
            st.info("Tag scripts in Action Panel to unlock script effectiveness analytics.")
        else:
            bar_chart = (
                alt.Chart(script_perf)
                .mark_bar()
                .encode(
                    x=alt.X("Script Used:N", sort="-y", title="Script"),
                    y=alt.Y("Interested:Q", title="Interested Users"),
                    tooltip=["Script Used", "Total Tagged", "Interested", "Success Rate %"],
                    color=alt.Color("Script Used:N", legend=None),
                )
                .properties(height=300)
            )
            st.altair_chart(bar_chart, use_container_width=True)
            st.dataframe(script_perf, use_container_width=True, hide_index=True)

    st.markdown("</div>", unsafe_allow_html=True)


def main() -> None:
    inject_ui_styles()

    st.markdown('<div class="tw-title">FB Lead Hunter - Commander Dashboard</div>', unsafe_allow_html=True)
    st.markdown('<div class="tw-subtitle">Phase 3: Speed & Intelligence</div>', unsafe_allow_html=True)

    progress_placeholder = st.empty()

    uploaded_file = st.file_uploader(
        "Upload member data (.csv or .xlsx)",
        type=["csv", "xlsx"],
    )

    selected_script_name, selected_script_text, script_names = script_manager_sidebar()

    if not uploaded_file:
        progress_placeholder.progress(0.0, text=f"Daily Goal: Contacted 0 / {DAILY_GOAL} users")
        st.info("Please upload a file to begin.")
        return

    working_df = init_working_df(uploaded_file, script_names)
    if working_df is None:
        return

    working_df = preprocess_data(working_df.copy(), script_names)

    st.sidebar.header("Filters")
    freshness_options = sort_freshness_values(working_df["Freshness"])
    selected_freshness = st.sidebar.multiselect(
        "Freshness",
        options=freshness_options,
        default=freshness_options,
    )

    friend_options = sorted(working_df["Friendship Status"].dropna().astype(str).unique().tolist())
    selected_friendship = st.sidebar.multiselect(
        "Friendship Status",
        options=friend_options,
        default=friend_options,
    )

    sort_mode = st.sidebar.selectbox(
        "Sort by Join Time",
        options=["Newest first", "Oldest first"],
        index=0,
    )

    tabs = st.tabs(["List View", "War Room Analytics"])

    with tabs[0]:
        metric_col1, metric_col2 = st.columns(2)
        metric_col1.metric("Total Leads", f"{len(working_df):,}")
        hot_count = int((working_df["Freshness"] == "🔥 Hot (Minutes)").sum())
        metric_col2.metric("Hot Leads", f"{hot_count:,}")

        action_col1, action_col2, action_col3 = st.columns(3)
        launch_clicked = action_col1.button("🚀 Launch Top 5 Uncontacted", use_container_width=True)
        purge_clicked = action_col2.button("🧹 Purge Cold Leads", use_container_width=True)
        save_clicked = action_col3.button("Save Data", type="primary", use_container_width=True)

        if launch_clicked:
            working_df, launch_urls = launch_top_uncontacted(working_df, selected_script_name)
            if launch_urls:
                st.session_state["working_df"] = preprocess_data(working_df, script_names)
                st.session_state["pending_launch"] = {
                    "urls": launch_urls,
                    "nonce": uuid.uuid4().hex,
                }
                st.success(f"Launching {len(launch_urls)} profiles and marking them as Contacted.")
            else:
                st.warning("No eligible 'New' leads with valid profile URLs.")

        if purge_clicked:
            working_df, removed_count = purge_cold_leads(working_df)
            if removed_count > 0:
                st.session_state["working_df"] = preprocess_data(working_df, script_names)
                st.success(f"Purged {removed_count} cold leads (status New/Ignore).")
            else:
                st.info("No cold leads matched purge criteria.")

        if save_clicked:
            try:
                save_data(preprocess_data(working_df, script_names), output_path="updated_leads.csv")
                st.success("Saved successfully to updated_leads.csv")
            except Exception as error:
                st.error(f"Save failed: {error}")

        if st.session_state.get("pending_launch"):
            pending = st.session_state.pop("pending_launch")
            render_launch_tabs(pending["urls"], pending["nonce"])

        working_df = preprocess_data(st.session_state.get("working_df", working_df), script_names)

        mask = (
            working_df["Freshness"].isin(selected_freshness)
            & working_df["Friendship Status"].astype(str).isin(selected_friendship)
        )

        filtered_df = working_df.loc[mask].copy()
        ascending = sort_mode == "Newest first"
        filtered_df = filtered_df.sort_values(by="Join Minutes Ago", ascending=ascending, na_position="last")

        left_col, right_col = st.columns([7, 3], gap="large")

        with left_col:
            st.markdown('<div class="tw-panel">', unsafe_allow_html=True)
            st.subheader("Lead List")

            table_columns = [
                "Lead ID",
                "Profile Picture",
                "Lead Name",
                "Join Status Text",
                "Freshness",
                "Friendship Status",
                "Status",
                "Script Used",
                "Profile URL",
            ]

            table_view = filtered_df[table_columns].copy()
            table_height = max(280, 58 + len(table_view) * TABLE_ROW_HEIGHT)

            table_event = st.dataframe(
                table_view,
                use_container_width=True,
                hide_index=True,
                row_height=TABLE_ROW_HEIGHT,
                height=table_height,
                on_select="rerun",
                selection_mode="single-row",
                key="lead_list_table",
                column_config={
                    "Lead ID": st.column_config.NumberColumn("Lead ID", width=66),
                    "Profile Picture": st.column_config.ImageColumn("Profile Picture", width=84),
                    "Lead Name": st.column_config.TextColumn("User Name", width=175),
                    "Join Status Text": st.column_config.TextColumn("Join Status Text", width=185),
                    "Freshness": st.column_config.TextColumn("Freshness", width=125),
                    "Friendship Status": st.column_config.TextColumn("Friendship Status", width=120),
                    "Status": st.column_config.TextColumn("Status", width=95),
                    "Script Used": st.column_config.TextColumn("Script Used", width=120),
                    "Profile URL": st.column_config.LinkColumn(
                        "Profile URL",
                        display_text="Open",
                        width=78,
                    ),
                },
            )
            st.caption("⭐ indicates biography contains tracked keywords.")
            st.markdown("</div>", unsafe_allow_html=True)

        selected_lead_id = choose_selected_lead(filtered_df, table_event)

        with right_col:
            st.markdown('<div class="tw-panel">', unsafe_allow_html=True)
            st.subheader("Action Panel")

            if selected_lead_id is None:
                st.info("No leads match the selected filters.")
            else:
                selected_mask = working_df["Lead ID"] == selected_lead_id
                selected_row = working_df.loc[selected_mask].iloc[0]
                selected_row_idx = working_df.index[selected_mask][0]

                if str(selected_row["Profile Picture"]).strip():
                    st.image(selected_row["Profile Picture"], width=180)

                st.write(f"**Name:** {selected_row['Lead Name']}")
                st.write(f"**Join Status:** {selected_row['Join Status Text']}")
                st.write(f"**Freshness:** {selected_row['Freshness']}")
                st.write(f"**Friendship:** {selected_row['Friendship Status']}")

                if bool(selected_row.get("Keyword Flag", False)):
                    st.warning("Keyword alert found in Biography (bet/money/invest/เสี่ยง/ดวง)")

                if str(selected_row["Profile URL"]).strip():
                    st.link_button(
                        "Open Facebook Profile",
                        selected_row["Profile URL"],
                        use_container_width=True,
                    )

                active_token = st.session_state.get("active_file_token", "file")

                status_key = f"status_{active_token}_{selected_lead_id}"
                if status_key not in st.session_state:
                    st.session_state[status_key] = selected_row["Status"]

                status_value = st.selectbox(
                    "Quick Status Update",
                    options=STATUS_OPTIONS,
                    index=STATUS_OPTIONS.index(selected_row["Status"])
                    if selected_row["Status"] in STATUS_OPTIONS
                    else 0,
                    key=status_key,
                )
                working_df.loc[selected_row_idx, "Status"] = status_value

                script_options = ["Unassigned", *script_names]
                script_tag_key = f"script_tag_{active_token}_{selected_lead_id}"
                if script_tag_key not in st.session_state:
                    st.session_state[script_tag_key] = (
                        selected_row["Script Used"]
                        if selected_row["Script Used"] in script_options
                        else "Unassigned"
                    )

                tagged_script = st.selectbox(
                    "Script Used For This Lead",
                    options=script_options,
                    index=script_options.index(selected_row["Script Used"])
                    if selected_row["Script Used"] in script_options
                    else 0,
                    key=script_tag_key,
                )
                working_df.loc[selected_row_idx, "Script Used"] = tagged_script

                st.markdown(f"**{selected_script_name}**")
                st.code(selected_script_text, language="text")
                render_copy_button(selected_script_text, key=f"panel_copy_{selected_lead_id}")

                note_key = f"note_{active_token}_{selected_lead_id}"
                if note_key not in st.session_state:
                    st.session_state[note_key] = str(selected_row["Notes"])

                note_text = st.text_area(
                    "Notes",
                    key=note_key,
                    height=130,
                    placeholder="Add notes for this lead...",
                )
                working_df.loc[selected_row_idx, "Notes"] = note_text

            st.markdown("</div>", unsafe_allow_html=True)

        working_df = preprocess_data(working_df, script_names)
        st.session_state["working_df"] = working_df

    with tabs[1]:
        analytics_df = preprocess_data(st.session_state.get("working_df", working_df), script_names)
        render_analytics_tab(analytics_df)

    contacted_count = int(st.session_state["working_df"]["Status"].isin(CONTACTED_STATUSES).sum())
    progress_value = min(contacted_count / DAILY_GOAL, 1.0)
    progress_placeholder.progress(
        progress_value,
        text=f"Daily Goal: Contacted {contacted_count} / {DAILY_GOAL} users",
    )


if __name__ == "__main__":
    main()
