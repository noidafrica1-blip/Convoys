import numpy as np
import pandas as pd
import streamlit as st
from math import radians, sin, cos, asin, sqrt
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# ============================================================
# CONVOY MONITOR — corrected submission version
# ============================================================

st.set_page_config(
    page_title="CONVOY MONITOR",
    page_icon="🚛",
    layout="wide",
    initial_sidebar_state="collapsed",
)

REQUIRED_COLUMNS = [
    "convoy_id",
    "registration",
    "tour_no",
    "latitude",
    "longitude",
    "is_reference",
    "gps_time",
    "speed_kmh",
]

st.markdown(
    """
    <style>
    .block-container {padding-top: 1.3rem; padding-bottom: 2rem;}
    [data-testid="stMetric"] {
        background:#f5f7fa;
        border:1px solid #e4e9f0;
        padding:14px;
        border-radius:10px;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("🚛 CONVOY MONITOR")
st.caption(
    "ETA SECURITY & LOGISTICS LTD | Intelligent Fleet Monitoring & Control"
)

st.info(
    "DEMO MODE — sample positions are simulated unless you upload a GPS CSV. "
    "This prototype is not connected to a live GPS provider."
)


# -------------------------- Utilities --------------------------

def haversine_km(lat1, lon1, lat2, lon2):
    """Calculate the great-circle distance between two GPS points."""
    earth_radius_km = 6371.0088

    lat1 = radians(float(lat1))
    lon1 = radians(float(lon1))
    lat2 = radians(float(lat2))
    lon2 = radians(float(lon2))

    dlat = lat2 - lat1
    dlon = lon2 - lon1

    a = (
        sin(dlat / 2) ** 2
        + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    )
    a = min(1.0, max(0.0, a))  # protect sqrt/asin from floating-point drift

    return 2 * earth_radius_km * asin(sqrt(a))


def parse_gps_timestamp(value, naive_timezone):
    """
    Convert a GPS timestamp to UTC, then return a timezone-naive UTC
    Timestamp so all calculations use the same representation.

    Timestamps containing an offset/Z are treated as already timezone-aware.
    Timestamps without an offset are interpreted in the selected timezone.
    """
    if pd.isna(value):
        return pd.NaT

    try:
        timestamp = pd.Timestamp(value)

        if timestamp.tzinfo is None:
            timestamp = timestamp.tz_localize(
                ZoneInfo(naive_timezone),
                ambiguous="NaT",
                nonexistent="NaT",
            )

        if pd.isna(timestamp):
            return pd.NaT

        return timestamp.tz_convert("UTC").tz_localize(None)

    except (TypeError, ValueError, OverflowError):
        return pd.NaT


def sample_data():
    """Generate clearly labelled demonstration records."""
    now_utc = pd.Timestamp.now(tz="UTC")

    def ts(minutes_ago):
        return (
            now_utc - pd.Timedelta(minutes=minutes_ago)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

    rows = [
        ["CONVOY 01", "TMR001", "TOUR-101", -15.3875, 28.3228, True,  ts(1),  62],
        ["CONVOY 01", "TMR002", "TOUR-101", -15.3950, 28.3300, False, ts(1),  60],
        ["CONVOY 01", "TMR003", "TOUR-101", -15.3750, 28.3150, False, ts(45), 58],
        ["CONVOY 01", "TMR004", "TOUR-101", -15.4500, 28.3700, False, ts(2),  55],
        ["CONVOY 02", "TMR005", "TOUR-202", -15.4200, 28.2800, True,  ts(1),  48],
        ["CONVOY 02", "TMR006", "TOUR-202", -15.4300, 28.2900, False, ts(1),  47],
        ["CONVOY 02", "TMR007", "TOUR-202", -15.5200, 28.3500, False, ts(1),  0],
        ["CONVOY 03", "TMR008", "TOUR-303", -15.4000, 28.3000, False, ts(1),  44],
    ]

    return pd.DataFrame(rows, columns=REQUIRED_COLUMNS)


def normalize(df, naive_timezone):
    """Validate and standardize an uploaded GPS CSV."""
    missing = [column for column in REQUIRED_COLUMNS if column not in df.columns]
    if missing:
        raise ValueError(
            "Missing required CSV columns: " + ", ".join(missing)
        )

    df = df.copy()

    # Normalize text fields.
    for column in ["convoy_id", "registration", "tour_no"]:
        df[column] = df[column].astype("string").str.strip()

    # Convert numeric fields.
    for column in ["latitude", "longitude", "speed_kmh"]:
        df[column] = pd.to_numeric(df[column], errors="coerce")

    # Normalize reference-truck flags.
    reference_values = (
        df["is_reference"].astype("string").str.strip().str.lower()
    )
    df["is_reference"] = reference_values.isin(
        ["true", "1", "yes", "y"]
    )

    # Parse each timestamp consistently. Naive times use the selected timezone.
    df["gps_time"] = df["gps_time"].apply(
        lambda value: parse_gps_timestamp(value, naive_timezone)
    )

    # Reject unusable records and invalid GPS coordinates.
    df = df.dropna(
        subset=[
            "convoy_id",
            "registration",
            "tour_no",
            "latitude",
            "longitude",
            "gps_time",
        ]
    )

    df = df[
        df["convoy_id"].ne("")
        & df["registration"].ne("")
        & df["tour_no"].ne("")
        & df["latitude"].between(-90, 90)
        & df["longitude"].between(-180, 180)
        & df["speed_kmh"].ge(0)
    ].copy()

    if df.empty:
        raise ValueError(
            "The CSV contains no valid GPS records after validation."
        )

    return df.reset_index(drop=True)


def status_label(status):
    labels = {
        "WITHIN CONVOY": "🟢 WITHIN CONVOY",
        "OUTSIDE LIMIT": "🔴 OUTSIDE LIMIT",
        "STALE GPS": "🟠 STALE GPS",
        "PARKED / STOPPED": "🟡 PARKED / STOPPED",
        "GPS TIME CHECK": "⚠️ GPS TIME CHECK",
        "REFERENCE SETUP ERROR": "⚠️ REFERENCE SETUP ERROR",
    }
    return labels.get(status, "⚠️ " + str(status))


# -------------------------- Sidebar --------------------------

with st.sidebar:
    st.header("Monitoring settings")

    limit_km = st.number_input(
        "Maximum distance from reference truck (km)",
        min_value=0.5,
        max_value=100.0,
        value=10.0,
        step=0.5,
    )

    stale_mins = st.number_input(
        "GPS considered stale after (minutes)",
        min_value=1,
        max_value=240,
        value=15,
        step=1,
    )

    st.divider()
    st.subheader("GPS data source")

    naive_timezone = st.selectbox(
        "Timezone for timestamps without UTC offset",
        options=["Africa/Lusaka", "UTC"],
        index=0,
        help=(
            "Choose the timezone used by your GPS export when its timestamps "
            "do not contain Z or a UTC offset. Zambia uses Africa/Lusaka."
        ),
    )

    uploaded = st.file_uploader(
        "Upload GPS CSV export",
        type=["csv"],
        help="The CSV must contain all eight required columns.",
    )


# -------------------------- Load data --------------------------

if uploaded is not None:
    try:
        raw_data = pd.read_csv(uploaded)
        data = normalize(raw_data, naive_timezone)
        data_source = "Uploaded GPS CSV"
        st.success(
            f"GPS file loaded successfully: {len(data)} valid record(s)."
        )
    except Exception as error:
        st.error(f"GPS upload could not be processed: {error}")
        st.stop()
else:
    data = sample_data()
    # Sample data timestamps include Z and are already UTC.
    data = normalize(data, naive_timezone)
    data_source = "Simulated sample data"


# -------------------------- Process convoy records --------------------------

results = []
issues = []

# One consistent UTC clock value for the entire processing run.
now_utc_naive = pd.Timestamp.now(tz="UTC").tz_localize(None)

for convoy, group in data.groupby("convoy_id", dropna=False):
    references = group[group["is_reference"]]

    if len(references) != 1:
        message = (
            f"{convoy}: expected exactly one reference truck; "
            f"found {len(references)}."
        )
        issues.append(message)

        for _, row in group.iterrows():
            item = row.to_dict()
            item.update(
                distance_km=np.nan,
                status="REFERENCE SETUP ERROR",
                gps_age_min=np.nan,
            )
            results.append(item)

        continue

    reference_row = references.iloc[0]

    for _, row in group.iterrows():
        distance = haversine_km(
            reference_row["latitude"],
            reference_row["longitude"],
            row["latitude"],
            row["longitude"],
        )

        # Explicit Timedelta conversion fixes the reported TypeError.
        time_difference = pd.Timedelta(
            now_utc_naive - pd.Timestamp(row["gps_time"])
        )
        age_minutes = time_difference.total_seconds() / 60.0

        if age_minutes < -5:
            status = "GPS TIME CHECK"
        elif age_minutes > stale_mins:
            status = "STALE GPS"
        elif distance > limit_km:
            status = "OUTSIDE LIMIT"
        elif float(row["speed_kmh"]) < 1:
            status = "PARKED / STOPPED"
        else:
            status = "WITHIN CONVOY"

        item = row.to_dict()
        item.update(
            distance_km=distance,
            status=status,
            gps_age_min=max(0.0, age_minutes),
        )
        results.append(item)


out = pd.DataFrame(results)

if out.empty:
    st.warning("No valid GPS records are available for monitoring.")
    st.stop()


# -------------------------- Dashboard KPIs --------------------------

total_trucks = len(out)
outside_count = int((out["status"] == "OUTSIDE LIMIT").sum())
stale_count = int((out["status"] == "STALE GPS").sum())
time_warnings = int((out["status"] == "GPS TIME CHECK").sum())
setup_errors = int(
    (out["status"] == "REFERENCE SETUP ERROR").sum()
)
active_convoys = out["convoy_id"].nunique()

kpi1, kpi2, kpi3, kpi4 = st.columns(4)
kpi1.metric("Convoys monitored", active_convoys)
kpi2.metric("Trucks monitored", total_trucks)
kpi3.metric(f"Outside {limit_km:g} km limit", outside_count)
kpi4.metric(
    "GPS / setup warnings",
    stale_count + time_warnings + setup_errors,
)

st.caption(
    f"Data source: {data_source} | "
    f"Last dashboard refresh: "
    f"{datetime.now().strftime('%d %b %Y %H:%M:%S')} (server local time)"
)

for issue in issues:
    st.error(issue)


# -------------------------- Selected convoy --------------------------

st.subheader("Current convoy location and status")

convoys = sorted(out["convoy_id"].dropna().astype(str).unique())

if not convoys:
    st.warning("No convoy IDs are available.")
    st.stop()

selected_convoy = st.selectbox("Select convoy", convoys)

view = out[out["convoy_id"].astype(str) == selected_convoy].copy()
reference_view = view[view["is_reference"]]

if len(reference_view) == 1:
    reference = reference_view.iloc[0]

    col1, col2, col3 = st.columns(3)
    col1.metric("Reference truck", str(reference["registration"]))
    col2.metric(
        "Reference GPS position",
        f"{reference['latitude']:.5f}, {reference['longitude']:.5f}",
    )

    reference_status = reference["status"]
    centre_status = (
        "CENTRE ACTIVE"
        if reference_status not in ["STALE GPS", "GPS TIME CHECK"]
        else reference_status
    )
    col3.metric("Convoy reference status", centre_status)

    map_data = view[["latitude", "longitude", "is_reference"]].copy()
    map_data["marker_size"] = np.where(
        map_data["is_reference"], 600, 150
    )

    st.map(
        map_data,
        latitude="latitude",
        longitude="longitude",
        size="marker_size",
        use_container_width=True,
    )
    st.caption(
        "Larger marker = designated reference truck (convoy centre)."
    )
else:
    st.warning(
        "This convoy needs exactly one reference truck before its "
        "centre can be calculated."
    )


# -------------------------- Selected convoy table --------------------------

st.subheader("Truck-by-truck monitoring")

display_columns = [
    "registration",
    "tour_no",
    "is_reference",
    "latitude",
    "longitude",
    "distance_km",
    "speed_kmh",
    "gps_age_min",
    "status",
]

display = view[display_columns].copy()
display["distance_km"] = display["distance_km"].round(2)
display["gps_age_min"] = display["gps_age_min"].round(1)
display["status"] = display["status"].map(status_label)

display = display.rename(
    columns={
        "registration": "Truck",
        "tour_no": "Tour",
        "is_reference": "Reference truck",
        "latitude": "Latitude",
        "longitude": "Longitude",
        "distance_km": "Distance from centre (km)",
        "speed_kmh": "Speed (km/h)",
        "gps_age_min": "GPS age (min)",
        "status": "Status",
    }
)

st.dataframe(
    display,
    use_container_width=True,
    hide_index=True,
)


# -------------------------- All convoy summaries --------------------------

st.subheader("All convoy summaries")

summary_rows = []

for convoy, group in out.groupby("convoy_id", dropna=False):
    reference_group = group[group["is_reference"]]

    if len(reference_group) == 1:
        reference = reference_group.iloc[0]
        centre = (
            f"{reference['latitude']:.5f}, "
            f"{reference['longitude']:.5f}"
        )
        reference_registration = reference["registration"]
    else:
        centre = "Reference setup error"
        reference_registration = "Not set"

    attention_statuses = [
        "OUTSIDE LIMIT",
        "STALE GPS",
        "GPS TIME CHECK",
        "REFERENCE SETUP ERROR",
    ]

    overall_status = (
        "ATTENTION REQUIRED"
        if group["status"].isin(attention_statuses).any()
        else "NORMAL"
    )

    summary_rows.append(
        {
            "Convoy": convoy,
            "Trucks": len(group),
            "Reference truck": reference_registration,
            "Current centre (lat, lon)": centre,
            "Within convoy": int(
                (group["status"] == "WITHIN CONVOY").sum()
            ),
            "Outside limit": int(
                (group["status"] == "OUTSIDE LIMIT").sum()
            ),
            "Parked / stopped": int(
                (group["status"] == "PARKED / STOPPED").sum()
            ),
            "Stale GPS": int(
                (group["status"] == "STALE GPS").sum()
            ),
            "GPS time warnings": int(
                (group["status"] == "GPS TIME CHECK").sum()
            ),
            "Overall status": overall_status,
        }
    )

summary_df = pd.DataFrame(summary_rows)

st.dataframe(
    summary_df,
    use_container_width=True,
    hide_index=True,
)


# -------------------------- Download reports --------------------------

st.subheader("Export reports")

report_csv = out.to_csv(index=False).encode("utf-8")
summary_csv = summary_df.to_csv(index=False).encode("utf-8")

download_col1, download_col2 = st.columns(2)

with download_col1:
    st.download_button(
        "Download processed truck report (CSV)",
        data=report_csv,
        file_name="convoy_monitor_truck_report.csv",
        mime="text/csv",
        use_container_width=True,
    )

with download_col2:
    st.download_button(
        "Download convoy summary report (CSV)",
        data=summary_csv,
        file_name="convoy_monitor_summary_report.csv",
        mime="text/csv",
        use_container_width=True,
    )

st.caption(
    "CONVOY MONITOR | ETA SECURITY & LOGISTICS LTD | "
    "Prototype monitoring tool — verify GPS provider data before operational use."
)
