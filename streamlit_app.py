import numpy as np
import pandas as pd
import streamlit as st
from math import radians, sin, cos, asin, sqrt
from datetime import datetime

# 1. Page configuration
st.set_page_config(
    page_title="CONVOY MONITOR", 
    page_icon="🚛", 
    layout="wide", 
    initial_sidebar_state="collapsed"
)

REQUIRED_COLUMNS = [
    "convoy_id", "registration", "tour_no", "latitude",
    "longitude", "is_reference", "gps_time", "speed_kmh",
]

st.markdown("""
<style>
.block-container {padding-top: 1.3rem; padding-bottom: 2rem;}
[data-testid="stMetric"] {background:#f5f7fa; border:1px solid #e4e9f0; padding:14px; border-radius:10px;}
</style>
""", unsafe_allow_html=True)

# Optional: You can add an image here later if you upload one to GitHub!
# st.image("logo.png", width=150)

st.title("🚛 CONVOY MONITOR")
st.caption("ETA SECURITY & LOGISTICS LTD  |  Intelligent Fleet Monitoring & Control")
st.info("DEMO MODE — positions are simulated unless you upload a GPS CSV. "
        "This prototype is not connected to a live GPS provider.")


def utc_now_naive():
    """Current UTC time as a tz-naive timestamp (matches tz-naive GPS times)."""
    return pd.Timestamp.now(tz="UTC").tz_convert(None)


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0088
    p1, p2 = radians(float(lat1)), radians(float(lat2))
    dlat = radians(float(lat2) - float(lat1))
    dlon = radians(float(lon2) - float(lon1))
    a = sin(dlat / 2) ** 2 + cos(p1) * cos(p2) * sin(dlon / 2) ** 2
    return 2 * r * asin(sqrt(a))


def sample_data():
    now = utc_now_naive()

    def ts(minutes_ago):
        return (now - pd.Timedelta(minutes=minutes_ago)).strftime("%Y-%m-%d %H:%M:%S")

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


def normalize(df):
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError("Missing required columns: " + ", ".join(missing))

    df = df.copy()
    for c in ["latitude", "longitude", "speed_kmh"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    df["is_reference"] = (
        df["is_reference"].astype(str).str.strip().str.lower()
        .isin(["true", "1", "yes", "y"])
    )

    df["gps_time"] = (
        pd.to_datetime(df["gps_time"], errors="coerce", utc=True)
        .dt.tz_convert(None)
    )

    df = df.dropna(subset=["latitude", "longitude", "gps_time"])
    df = df[df.latitude.between(-90, 90) & df.longitude.between(-180, 180)]
    df["speed_kmh"] = df["speed_kmh"].fillna(0)
    return df


with st.sidebar:
    st.header("Monitoring settings")
    limit_km = st.number_input(
        "Maximum distance from reference truck (km)",
        min_value=0.5, max_value=100.0, value=10.0, step=0.5,
    )
    stale_mins = st.number_input(
        "GPS considered stale after (minutes)",
        min_value=1, max_value=240, value=15, step=1,
    )
    st.divider()
    st.subheader("GPS data source")
    uploaded = st.file_uploader("Upload GPS CSV export", type=["csv"])

if uploaded is not None:
    try:
        data = normalize(pd.read_csv(uploaded))
        data_source = "Uploaded GPS CSV"
    except Exception as e:
        st.error(f"Could not read GPS file: {e}")
        data = sample_data()
        data_source = "Simulated sample data"
else:
    data = sample_data()
    data_source = "Simulated sample data"

results, issues = [], []
now = utc_now_naive()

for convoy, group in data.groupby("convoy_id", dropna=False):
    refs = group[group["is_reference"]]
    if len(refs) != 1:
        issues.append(f"{convoy}: expected exactly one reference truck; found {len(refs)}.")
        for _, row in group.iterrows():
            item = row.to_dict()
            item.update(distance_km=np.nan, status="REFERENCE SETUP ERROR", gps_age_min=np.nan)
            results.append(item)
        continue

    ref = refs.iloc[0]
    for _, row in group.iterrows():
        dist = haversine_km(ref.latitude, ref.longitude, row.latitude, row.longitude)
        age = (now - row.gps_time).total_seconds() / 60
        
        if age < -5:
            status = "GPS TIME CHECK"
        elif age > stale_mins:
            status = "STALE GPS"
        elif dist > limit_km:
            status = "OUTSIDE LIMIT"
        elif float(row.speed_kmh) < 1:
            status = "PARKED / STOPPED"
        else:
            status = "WITHIN CONVOY"
            
        item = row.to_dict()
        item.update(distance_km=dist, status=status, gps_age_min=max(0, age))
        results.append(item)

out = pd.DataFrame(results)
if out.empty:
    st.warning("No valid GPS records found.")
    st.stop()

total = len(out)
outside = int((out.status == "OUTSIDE LIMIT").sum())
stale = int((out.status == "STALE GPS").sum())
setup_errors = int((out.status == "REFERENCE SETUP ERROR").sum())
active_convoys = out.convoy_id.nunique()

c1, c2, c3, c4 = st.columns(4)
c1.metric("Convoys monitored", active_convoys)
c2.metric("Trucks monitored", total)
c3.metric(f"Outside {limit_km:g} km limit", outside)
c4.metric("GPS warnings", stale + setup_errors)

st.caption(
    f"Data source: {data_source}  •  "
    f"Last dashboard refresh: {datetime.now().strftime('%d %b %Y %H:%M:%S')} (local)"
)

for issue in issues:
    st.error(issue)

st.subheader("Current convoy location and status")
convoys = sorted(out.convoy_id.dropna().astype(str).unique())
selected = st.selectbox("Select convoy", convoys)
view = out[out.convoy_id.astype(str) == selected].copy()
ref_row = view[view.is_reference]

if len(ref_row) == 1:
    ref = ref_row.iloc[0]
    a, b, c = st.columns(3)
    a.metric("Reference truck", ref.registration)
    b.metric("Reference GPS position", f"{ref.latitude:.5f}, {ref.longitude:.5f}")
    c.metric("Convoy reference status",
             "CENTRE ACTIVE" if ref.status not in ["STALE GPS", "GPS TIME CHECK"] else ref.status)

    map_df = view[["latitude", "longitude"]].copy()
    map_df["marker_size"] = np.where(view["is_reference"], 600, 150)
    st.map(map_df, latitude="latitude", longitude="longitude",
           size="marker_size", use_container_width=True)
    st.caption("Larger marker = designated reference truck (convoy centre).")
else:
    st.warning("This convoy needs exactly one reference truck before its centre can be calculated.")

def status_label(s):
    return {
        "WITHIN CONVOY": "🟢 WITHIN CONVOY",
        "OUTSIDE LIMIT": "🔴 OUTSIDE LIMIT",
        "STALE GPS": "🟠 STALE GPS",
        "PARKED / STOPPED": "🟡 PARKED / STOPPED",
    }.get(s, "⚠️ " + str(s))

display = view[[
    "registration", "tour_no", "is_reference", "latitude", "longitude",
    "distance_km", "speed_kmh", "gps_age_min", "status",
]].copy()
display["distance_km"] = display.distance_km.round(2)
display["gps_age_min"] = display.gps_age_min.round(1)
display["status"] = display.status.map(status_label)
display = display.rename(columns={
    "registration": "Truck", "tour_no": "Tour", "is_reference": "Reference truck",
    "latitude": "Latitude", "longitude": "Longitude",
    "distance_km": "Distance from centre (km)", "speed_kmh": "Speed (km/h)",
    "gps_age_min": "GPS age (min)", "status": "Status",
})
st.dataframe(display, use_container_width=True, hide_index=True)

st.subheader("All convoy summaries")
summary = []
for convoy, group in out.groupby("convoy_id"):
    ref = group[group.is_reference]
    centre = (f"{ref.iloc[0].latitude:.5f}, {ref.iloc[0].longitude:.5f}"
              if len(ref) == 1 else "Reference setup error")
    summary.append({
        "Convoy": convoy,
        "Trucks": len(group),
        "Reference truck": ref.iloc[0].registration if len(ref) == 1 else "Not set",
        "Current centre (lat, lon)": centre,
        "Within limit": int((group.status == "WITHIN CONVOY").sum()),
        "Outside limit": int((group.status == "OUTSIDE LIMIT").sum()),
        "GPS warnings": int((group.status == "STALE GPS").sum()),
        "Overall status": "ATTENTION REQUIRED" if group.status.isin(
            ["OUTSIDE LIMIT", "STALE GPS", "GPS TIME CHECK", "REFERENCE SETUP ERROR"]
        ).any() else "NORMAL",
    })
st.dataframe(pd.DataFrame(summary), use_container_width=True, hide_index=True)

st.download_button(
    "Download processed convoy report (CSV)",
    out.to_csv(index=False).encode("utf-8"),
    "convoy_monitor_report.csv",
    "text/csv",
)
