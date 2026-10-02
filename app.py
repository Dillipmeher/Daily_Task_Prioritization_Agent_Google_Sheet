import re
import streamlit as st
import pandas as pd
from datetime import datetime, timedelta

st.set_page_config(page_title="Procurement Task Prioritizer", page_icon="📋", layout="wide")
st.title("📋 Daily Task Prioritization Agent")
st.write("Load your task sheet and get a prioritized plan for today.")

# ---------- Header mapping (case and spaces ignored) ----------
COLUMN_MAP = {
    "date": "Date",
    "task": "Task",
    "to do list/work": "Task",
    "owner": "Owner",
    "initative": "Owner",
    "initiative": "Owner",
    "category": "Category",
    "region/brand": "Region",
    "remarks-1": "Remarks1",
    "remarks-2": "Remarks2",
    "priority": "Priority",
    "completion/pending": "Status",
    "summary": "Summary",
    "estimated_minutes": "Estimated_Minutes",
}

# ---------- Scoring rules (edit to suit your work) ----------
PRIORITY_POINTS = {"high": 40, "medium": 25, "low": 10}

# (keywords, extra points, default minutes) - first match wins
RULES = [
    (["quality", "complaint"], 10, 45),
    (["supply", "short receive", "credit note"], 10, 30),
    (["payment", "invoice", "billing", "booking"], 10, 20),
    (["price", "pricing", "comparison", "oil"], 5, 40),
    (["gst", "data update", "certificate", "fssai", "vendor code"], 5, 20),
    (["distributor", "required"], 5, 30),
]
DEFAULT_POINTS, DEFAULT_MINUTES = 0, 30

URGENT_WORDS = r"urg|ureg|asap|today|immediate"
LATER_WORDS = r"later|next week|not urgent|postpone"
QUICK_WORDS = r"less time|quick|short task|few minutes"


def task_rule(row):
    text = f"{row.get('Category', '')} {row.get('Task', '')}".lower()
    for keywords, points, minutes in RULES:
        if any(k in text for k in keywords):
            return points, minutes
    return DEFAULT_POINTS, DEFAULT_MINUTES


def age_points(age_days):
    if pd.isna(age_days):
        return 0
    if age_days >= 7:
        return 30
    if age_days >= 3:
        return 20
    if age_days >= 1:
        return 10
    return 0


def clean_status(value):
    t = str(value).strip().lower()
    if re.search(r"\b(comp\w*|done|closed|finished)\b", t):
        return "Completed"
    if "wip" in t or "progress" in t:
        return "WIP"
    return "Pending"


def clean_priority(value):
    t = str(value).strip().lower()
    if t.startswith("h"):
        return "High"
    if t.startswith("m"):
        return "Medium"
    if t.startswith("l"):
        return "Low"
    return "Medium"  # blank or unknown


# ---------- Google Sheet helpers ----------
def sheet_to_csv_url(url):
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", url)
    if not m:
        return None
    g = re.search(r"[#&?]gid=([0-9]+)", url)
    return f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=csv&gid={g.group(1) if g else '0'}"


@st.cache_data(ttl=60)
def load_sheet(csv_url):
    return pd.read_csv(csv_url)


# ---------- Load data ----------
st.subheader("📤 Step 1: Load Your Tasks")
source = st.radio("Where are your tasks?", ["Google Sheet link", "Upload CSV"], horizontal=True)
raw = None

if source == "Google Sheet link":
    sheet_url = st.text_input("Paste your Google Sheet link")
    st.caption("Sharing must be 'Anyone with the link → Viewer'. Open the right tab before copying the link.")
    if sheet_url:
        csv_url = sheet_to_csv_url(sheet_url)
        if csv_url is None:
            st.error("That doesn't look like a Google Sheets link.")
        else:
            try:
                raw = load_sheet(csv_url)
            except Exception:
                st.error("Couldn't read the sheet. Check the sharing setting and the link.")
    if st.button("🔄 Refresh from sheet"):
        st.cache_data.clear()
        st.rerun()
else:
    up = st.file_uploader("Choose your CSV file", type=["csv"])
    if up is not None:
        raw = pd.read_csv(up)


# ---------- Plan builder ----------
def build_plan(df, as_of, available_minutes, start_time):
    df = df.copy()

    # Dates are dd/mm/yy
    d = pd.to_datetime(df["Date"], format="%d/%m/%y", errors="coerce")
    fallback = pd.to_datetime(df["Date"], dayfirst=True, errors="coerce")
    df["Date_parsed"] = d.fillna(fallback)
    df["Age_Days"] = (pd.Timestamp(as_of) - df["Date_parsed"]).dt.days

    rules = df.apply(task_rule, axis=1, result_type="expand")
    df["Type_Points"] = rules[0]
    df["Priority_Points"] = df["Priority"].str.lower().map(PRIORITY_POINTS)
    df["Age_Points"] = df["Age_Days"].apply(age_points)
    df["WIP_Bonus"] = (df["State"] == "WIP").astype(int) * 5

    summary = df["Summary"].astype(str).str.lower()
    urgent = summary.str.contains(URGENT_WORDS, regex=True)
    later = summary.str.contains(LATER_WORDS, regex=True) & ~urgent
    quick = summary.str.contains(QUICK_WORDS, regex=True)
    df["Note_Points"] = urgent.astype(int) * 25 - later.astype(int) * 20

    # Minutes: your Estimated_Minutes column if present, else quick note, else guess by type
    est = rules[1].astype(float)
    est[quick] = 15
    if "Estimated_Minutes" in df.columns:
        est = pd.to_numeric(df["Estimated_Minutes"], errors="coerce").fillna(est)
    df["Est_Min"] = est.astype(int)

    df["Score"] = (df["Priority_Points"] + df["Type_Points"] + df["Age_Points"]
                   + df["WIP_Bonus"] + df["Note_Points"])

    # Plain-language explanation for each row
    why = []
    for i, r in df.iterrows():
        parts = [f"{r['Priority']} priority"]
        if pd.notna(r["Age_Days"]) and r["Age_Days"] >= 3:
            parts.append(f"{int(r['Age_Days'])} days old")
        if r["State"] == "WIP":
            parts.append("already in progress")
        if urgent[i]:
            parts.append("marked urgent in Summary")
        if later[i]:
            parts.append("marked 'later' in Summary")
        if quick[i]:
            parts.append("quick task")
        why.append(", ".join(parts))
    df["Why"] = why

    df = df.sort_values(["Score", "Est_Min"], ascending=[False, True]).reset_index(drop=True)
    df.insert(0, "Rank", df.index + 1)

    used, current = 0, start_time
    plan_col, starts, ends = [], [], []
    for _, r in df.iterrows():
        mins = int(r["Est_Min"])
        if used + mins <= available_minutes:
            end = current + timedelta(minutes=mins)
            plan_col.append("✅ Do Today")
            starts.append(current.strftime("%I:%M %p"))
            ends.append(end.strftime("%I:%M %p"))
            current, used = end, used + mins
        else:
            plan_col.append("⏭️ Defer")
            starts.append("-")
            ends.append("-")
    df["Plan"], df["Start"], df["End"] = plan_col, starts, ends
    df["Date"] = df["Date_parsed"].dt.strftime("%d/%m/%Y").fillna("invalid date")
    return df, used


# ---------- App flow ----------
if raw is not None:
    raw.columns = [COLUMN_MAP.get(str(c).strip().lower(), str(c).strip()) for c in raw.columns]
    missing = [c for c in ["Date", "Task"] if c not in raw.columns]
    if missing:
        st.error(f"Missing columns: {', '.join(missing)}")
        st.stop()

    for col in ["Owner", "Category", "Region", "Remarks1", "Priority", "Status", "Summary"]:
        if col not in raw.columns:
            raw[col] = ""
    raw = raw.dropna(subset=["Task"]).fillna("")
    raw = raw[raw["Task"].astype(str).str.strip() != ""]

    raw["State"] = raw["Status"].apply(clean_status)
    raw["Priority"] = raw["Priority"].apply(clean_priority)

    done_count = int((raw["State"] == "Completed").sum())
    pending = raw[raw["State"] != "Completed"].copy()
    st.success(f"✅ Loaded {len(raw)} tasks: {len(pending)} open, {done_count} completed (excluded).")

    st.subheader("⚙️ Step 2: Set Your Day")
    c1, c2, c3 = st.columns(3)
    with c1:
        hours = st.slider("Available working hours", 1.0, 12.0, 6.0, 0.5)
    with c2:
        start = st.time_input("Start time", value=datetime.strptime("09:00", "%H:%M").time())
    with c3:
        as_of = st.date_input("Plan as of date", value=datetime.today())

    owners = sorted({o.strip() for o in pending["Owner"].astype(str) if o.strip()})
    chosen = st.multiselect("Filter by owner / vendor (leave empty for all)", owners)
    if chosen:
        pending = pending[pending["Owner"].astype(str).str.strip().isin(chosen)]

    if pending.empty:
        st.info("No open tasks to plan 🎉")
        st.stop()

    plan, used = build_plan(pending, as_of, int(hours * 60), datetime.combine(datetime.today(), start))

    bad = int((plan["Date"] == "invalid date").sum())
    if bad:
        st.warning(f"{bad} task(s) have an invalid date. Please fix them in the sheet (format dd/mm/yy).")

    today_tasks = plan[plan["Plan"] == "✅ Do Today"]
    deferred = plan[plan["Plan"] == "⏭️ Defer"]

    st.subheader("🎯 Your Prioritized Plan")
    m1, m2, m3 = st.columns(3)
    m1.metric("Tasks Today", len(today_tasks))
    m2.metric("Deferred", len(deferred))
    m3.metric("Time Planned", f"{used} / {int(hours * 60)} min")

    show = ["Rank", "Task", "Owner", "Category", "Date", "Age_Days", "State", "Priority",
            "Score", "Est_Min", "Plan", "Start", "End", "Why", "Remarks1", "Summary"]
    st.dataframe(plan[show], use_container_width=True, hide_index=True)

    if len(today_tasks):
        st.subheader("🗓️ Today's Schedule")
        for _, r in today_tasks.iterrows():
            who = f" · {r['Owner']}" if str(r["Owner"]).strip() else ""
            st.write(f"**{r['Start']} – {r['End']}** → {r['Task']}  ·  _{r['Priority']}_{who}")

    if len(deferred):
        st.warning("Did not fit today: " + "; ".join(deferred["Task"].tolist()))

    st.download_button("⬇️ Download Plan", plan[show].to_csv(index=False),
                       file_name="prioritized_plan.csv", mime="text/csv")
else:
    st.info("👆 Paste a Google Sheet link or upload a CSV to get started.")
