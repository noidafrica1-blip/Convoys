"""
CONVOY MONITOR — ETA LOGISTICS & SECURITY LTD
Enhanced Streamlit fleet-monitoring application.

Run:
    py -m pip install -r requirements.txt
    py -m streamlit run convoy_monitor.py

SQLite is the default durable store. Set CONVOY_DB_PATH to a persistent path
before deployment. GPS provider integration is intentionally adapter-based:
configure the provider endpoint/credentials in the deployment environment and
implement fetch_provider_positions() for the provider's documented API.
"""
from __future__ import annotations

import io
import json
import math
import os
import sqlite3
import html
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
import streamlit as st

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9
    from backports.zoneinfo import ZoneInfo

try:
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
except ImportError:
    Font = PatternFill = Alignment = Border = Side = None
    get_column_letter = None

APP_NAME = "CONVOY MONITOR"
COMPANY = "ETA LOGISTICS & SECURITY LTD"
DB_PATH = Path(os.environ.get("CONVOY_DB_PATH", "convoy_monitor.db"))
DEFAULT_LIMIT_KM = 10.0
WARNING_KM = 8.0
STOP_SPEED_KMH = 1.0
STOP_RADIUS_KM = 0.05
STOP_MINUTES = 15
GPS_STALE_MINUTES = 15

GPS_COLUMNS = [
    "convoy_id", "registration", "tour_no", "latitude", "longitude",
    "is_reference", "gps_time", "speed_kmh"
]

CHECKPOINTS = ["LOADING", "KAZUNGULA", "GRB", "OFFLOADING"]
MILESTONE_TYPES = {
    "LOADING": ["ARRIVAL", "DEPARTURE"],
    "KAZUNGULA": ["ARRIVAL", "CROSSING", "DEPARTURE"],
    "GRB": ["ARRIVAL", "CROSSING", "DEPARTURE"],
    "OFFLOADING": ["ARRIVAL", "DEPARTURE"],
}
REPORT_COLUMNS = [
    "Convoy ID", "Horse and trailer registrations", "Client", "Commodity",
    "Current location",
    "Loading arrival", "Loading departure", "Loading total time",
    "Kazungula arrival", "Kazungula crossing", "Kazungula departure", "Kazungula total time",
    "GRB arrival", "GRB crossing", "GRB departure", "GRB total time",
    "Offloading arrival", "Offloading departure", "Offloading total time",
]

st.set_page_config(page_title=APP_NAME, page_icon="🚛", layout="wide")
st.markdown("""
<style>
.block-container{padding-top:1.1rem;padding-bottom:2rem}
[data-testid="stMetric"]{background:#f4f7fb;border:1px solid #e3e9f1;padding:12px;border-radius:12px}
.convoy-card{padding:16px;border-radius:13px;border-left:8px solid;margin-bottom:12px}
.small-note{font-size:0.85rem;color:#64748b}
</style>
""", unsafe_allow_html=True)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat(timespec="seconds")


def parse_utc(value, naive_timezone="Africa/Lusaka"):
    if pd.isna(value):
        return None
    try:
        ts = pd.Timestamp(value)
        if ts.tzinfo is None:
            ts = ts.tz_localize(ZoneInfo(naive_timezone), ambiguous="NaT", nonexistent="NaT")
        if pd.isna(ts):
            return None
        return ts.tz_convert("UTC").to_pydatetime()
    except (TypeError, ValueError, OverflowError):
        return None


def haversine_km(lat1, lon1, lat2, lon2):
    vals = [lat1, lon1, lat2, lon2]
    if any(pd.isna(v) for v in vals):
        return float("nan")
    lat1, lon1, lat2, lon2 = map(math.radians, map(float, vals))
    dlat, dlon = lat2-lat1, lon2-lon1
    a = math.sin(dlat/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin(dlon/2)**2
    return 6371.0088 * 2 * math.asin(math.sqrt(min(1.0, max(0.0, a))))


def get_conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH), timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init_db():
    with get_conn() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS app_users(
            username TEXT PRIMARY KEY COLLATE NOCASE,
            password_hash TEXT NOT NULL,
            salt TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'Controller',
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS convoy_registry(
            convoy_id TEXT PRIMARY KEY, commodity TEXT NOT NULL, client_name TEXT NOT NULL,
            registrations TEXT NOT NULL, reference_registration TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'ACTIVE', manual_parked INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, offloaded_at TEXT
        );
        CREATE TABLE IF NOT EXISTS gps_history(
            id INTEGER PRIMARY KEY AUTOINCREMENT, convoy_id TEXT NOT NULL,
            registration TEXT NOT NULL, tour_no TEXT, latitude REAL NOT NULL,
            longitude REAL NOT NULL, gps_time TEXT NOT NULL, speed_kmh REAL NOT NULL,
            source TEXT NOT NULL DEFAULT 'CSV', ingested_at TEXT NOT NULL,
            UNIQUE(registration,gps_time,latitude,longitude)
        );
        CREATE INDEX IF NOT EXISTS idx_gps_reg_time ON gps_history(registration,gps_time);
        CREATE TABLE IF NOT EXISTS checkpoint_geofences(
            checkpoint TEXT PRIMARY KEY, latitude REAL NOT NULL, longitude REAL NOT NULL,
            radius_km REAL NOT NULL DEFAULT 1.0, enabled INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS convoy_events(
            id INTEGER PRIMARY KEY AUTOINCREMENT, convoy_id TEXT NOT NULL,
            checkpoint TEXT NOT NULL, event_type TEXT NOT NULL, event_time TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'MANUAL', notes TEXT,
            UNIQUE(convoy_id,checkpoint,event_type)
        );
        CREATE TABLE IF NOT EXISTS geofence_state(
            convoy_id TEXT NOT NULL, checkpoint TEXT NOT NULL,
            inside INTEGER NOT NULL DEFAULT 0, entered_at TEXT,
            PRIMARY KEY(convoy_id,checkpoint)
        );
        """)
        # Safe schema extension for older database files.
        cols = {r["name"] for r in con.execute("PRAGMA table_info(convoy_registry)")}
        if "updated_at" not in cols:
            con.execute("ALTER TABLE convoy_registry ADD COLUMN updated_at TEXT")
        con.execute("INSERT OR IGNORE INTO checkpoint_geofences(checkpoint,latitude,longitude,radius_km,enabled) VALUES('LOADING',-15.4167,28.2833,1.0,0)")
        con.execute("INSERT OR IGNORE INTO checkpoint_geofences(checkpoint,latitude,longitude,radius_km,enabled) VALUES('KAZUNGULA',-17.7833,25.2667,2.0,0)")
        con.execute("INSERT OR IGNORE INTO checkpoint_geofences(checkpoint,latitude,longitude,radius_km,enabled) VALUES('GRB',-15.4167,28.2833,1.0,0)")
        con.execute("INSERT OR IGNORE INTO checkpoint_geofences(checkpoint,latitude,longitude,radius_km,enabled) VALUES('OFFLOADING',-15.4167,28.2833,1.0,0)")


def rows(sql, params=()):
    with get_conn() as con:
        return [dict(r) for r in con.execute(sql, params).fetchall()]


def execute(sql, params=()):
    with get_conn() as con:
        cur = con.execute(sql, params)
        return cur.lastrowid


def get_convoys(active_only=False):
    sql = "SELECT * FROM convoy_registry"
    if active_only:
        sql += " WHERE status='ACTIVE'"
    sql += " ORDER BY created_at DESC"
    return rows(sql)


def create_convoy(convoy_id, commodity, client, registrations, reference):
    execute("""INSERT INTO convoy_registry
      (convoy_id,commodity,client_name,registrations,reference_registration,status,manual_parked,created_at,updated_at)
      VALUES(?,?,?,?,?,'ACTIVE',0,?,?)""",
      (convoy_id.strip(), commodity.strip(), client.strip(), json.dumps(registrations), reference, iso_now(), iso_now()))


def update_convoy(convoy_id, **fields):
    allowed = {"manual_parked", "status", "offloaded_at"}
    pairs = [(k, v) for k, v in fields.items() if k in allowed]
    if not pairs:
        return
    pairs.append(("updated_at", iso_now()))
    sql = "UPDATE convoy_registry SET " + ", ".join(f"{k}=?" for k, _ in pairs) + " WHERE convoy_id=?"
    execute(sql, [v for _, v in pairs] + [convoy_id])


def normalize_gps(frame, naive_timezone):
    missing = [c for c in GPS_COLUMNS if c not in frame.columns]
    if missing:
        raise ValueError("Missing GPS CSV columns: " + ", ".join(missing))
    df = frame.copy()
    for c in ["convoy_id", "registration", "tour_no"]:
        df[c] = df[c].astype("string").str.strip()
    for c in ["latitude", "longitude", "speed_kmh"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["is_reference"] = df["is_reference"].astype(str).str.strip().str.lower().isin(["true","1","yes","y"])
    df["gps_time"] = df["gps_time"].apply(lambda x: parse_utc(x, naive_timezone))
    df = df.dropna(subset=["convoy_id","registration","tour_no","latitude","longitude","gps_time","speed_kmh"])
    df = df[df["convoy_id"].ne("") & df["registration"].ne("") & df["tour_no"].ne("")
            & df["latitude"].between(-90,90) & df["longitude"].between(-180,180) & df["speed_kmh"].ge(0)]
    if df.empty:
        raise ValueError("No valid GPS records remain after validation.")
    return df


def save_gps_history(df, source="CSV"):
    inserted = 0
    with get_conn() as con:
        for r in df.itertuples(index=False):
            cur = con.execute("""INSERT OR IGNORE INTO gps_history
              (convoy_id,registration,tour_no,latitude,longitude,gps_time,speed_kmh,source,ingested_at)
              VALUES(?,?,?,?,?,?,?,?,?)""",
              (str(r.convoy_id),str(r.registration),str(r.tour_no),float(r.latitude),float(r.longitude),
               r.gps_time.astimezone(timezone.utc).isoformat(),float(r.speed_kmh),source,iso_now()))
            inserted += cur.rowcount
    return inserted


def latest_gps(registrations=None):
    if registrations:
        marks = ",".join("?" for _ in registrations)
        return rows(f"""SELECT g.* FROM gps_history g JOIN
          (SELECT registration,MAX(gps_time) mx FROM gps_history WHERE registration IN ({marks}) GROUP BY registration) x
          ON g.registration=x.registration AND g.gps_time=x.mx""", tuple(registrations))
    return rows("""SELECT g.* FROM gps_history g JOIN
      (SELECT registration,MAX(gps_time) mx FROM gps_history GROUP BY registration) x
      ON g.registration=x.registration AND g.gps_time=x.mx""")


def stationary_minutes(registration, now=None):
    hist = rows("SELECT gps_time,latitude,longitude,speed_kmh FROM gps_history WHERE registration=? ORDER BY gps_time DESC LIMIT 500", (registration,))
    if not hist:
        return 0.0
    latest = hist[0]
    if float(latest["speed_kmh"]) >= STOP_SPEED_KMH:
        return 0.0
    latest_time = parse_utc(latest["gps_time"])
    first = latest_time
    for r in hist:
        if float(r["speed_kmh"]) >= STOP_SPEED_KMH:
            break
        if haversine_km(latest["latitude"],latest["longitude"],r["latitude"],r["longitude"]) > STOP_RADIUS_KM:
            break
        first = parse_utc(r["gps_time"])
    now = now or utc_now()
    # Do not extrapolate beyond the last GPS observation.
    return max(0.0, (latest_time-first).total_seconds()/60.0)


def add_event(convoy_id, checkpoint, event_type, event_time=None, source="MANUAL", notes=""):
    if event_type not in MILESTONE_TYPES[checkpoint]:
        raise ValueError("Invalid milestone type for checkpoint")
    event_time = event_time or iso_now()
    execute("""INSERT INTO convoy_events(convoy_id,checkpoint,event_type,event_time,source,notes)
      VALUES(?,?,?,?,?,?) ON CONFLICT(convoy_id,checkpoint,event_type)
      DO UPDATE SET event_time=excluded.event_time,source=excluded.source,notes=excluded.notes""",
      (convoy_id,checkpoint,event_type,event_time,source,notes))


def get_events(convoy_id):
    return rows("SELECT * FROM convoy_events WHERE convoy_id=? ORDER BY event_time", (convoy_id,))


def get_geofences():
    return rows("SELECT * FROM checkpoint_geofences ORDER BY checkpoint")


def process_geofences(active_records, latest_by_reg):
    """Automatic arrival/departure detection. Crossing remains an operator-confirmed milestone."""
    for c in active_records:
        cid = c["convoy_id"]
        regs = json.loads(c["registrations"])
        fixes = [latest_by_reg[r] for r in regs if r in latest_by_reg]
        if not fixes:
            continue
        # A convoy geofence is evaluated using its designated lead/reference truck.
        lead = latest_by_reg.get(c["reference_registration"])
        if not lead:
            continue
        for fence in get_geofences():
            if not fence["enabled"]:
                continue
            distance = haversine_km(lead["latitude"],lead["longitude"],fence["latitude"],fence["longitude"])
            inside = distance <= float(fence["radius_km"])
            state = rows("SELECT * FROM geofence_state WHERE convoy_id=? AND checkpoint=?", (cid,fence["checkpoint"]))
            was_inside = bool(state[0]["inside"]) if state else False
            if inside and not was_inside:
                add_event(cid,fence["checkpoint"],"ARRIVAL",lead["gps_time"],"GPS_GEOFENCE","Automatic geofence entry")
                execute("""INSERT INTO geofence_state(convoy_id,checkpoint,inside,entered_at) VALUES(?,?,1,?)
                  ON CONFLICT(convoy_id,checkpoint) DO UPDATE SET inside=1,entered_at=excluded.entered_at""",
                  (cid,fence["checkpoint"],lead["gps_time"]))
            elif not inside and was_inside:
                # Do not overwrite a manually captured departure; event upsert records latest transition.
                add_event(cid,fence["checkpoint"],"DEPARTURE",lead["gps_time"],"GPS_GEOFENCE","Automatic geofence exit")
                execute("UPDATE geofence_state SET inside=0,entered_at=NULL WHERE convoy_id=? AND checkpoint=?",(cid,fence["checkpoint"]))


def duration_text(start, end):
    if not start or not end:
        return ""
    a, b = parse_utc(start), parse_utc(end)
    if not a or not b or b < a:
        return ""
    mins = int((b-a).total_seconds()//60)
    return f"{mins//60}h {mins%60:02d}m"


def build_transit_report(active_records, latest_by_reg):
    output = []
    for c in active_records:  # parked convoys remain ACTIVE until explicit offload confirmation
        regs = json.loads(c["registrations"])
        fixes = [latest_by_reg[r] for r in regs if r in latest_by_reg]
        lead = latest_by_reg.get(c["reference_registration"])
        location = "GPS unavailable"
        if lead:
            location = f"{float(lead['latitude']):.5f}, {float(lead['longitude']):.5f}"
        ev = {(e["checkpoint"],e["event_type"]):e["event_time"] for e in get_events(c["convoy_id"])}
        item = {
            "Convoy ID": c["convoy_id"],
            "Horse and trailer registrations": ", ".join(regs),
            "Client": c["client_name"], "Commodity": c["commodity"],
            "Current location": location,
        }
        for cp in CHECKPOINTS:
            for typ in MILESTONE_TYPES[cp]:
                label = {"LOADING":"Loading","KAZUNGULA":"Kazungula","GRB":"GRB","OFFLOADING":"Offloading"}[cp]
                item[f"{label} {typ.lower()}"] = ev.get((cp,typ),"")
            item[f"{label} total time"] = duration_text(ev.get((cp,"ARRIVAL")),ev.get((cp,"DEPARTURE")))
        output.append(item)
    df = pd.DataFrame(output)
    return df.reindex(columns=REPORT_COLUMNS)


def excel_bytes(df):
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer,index=False,sheet_name="Convoys in Transit")
        ws = writer.sheets["Convoys in Transit"]
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        ws.row_dimensions[1].height = 32
        for cell in ws[1]:
            cell.font = Font(bold=True,color="FFFFFF")
            cell.fill = PatternFill("solid",fgColor="17365D")
            cell.alignment = Alignment(wrap_text=True,vertical="center")
        for col in ws.columns:
            letter = get_column_letter(col[0].column)
            max_len = max((len(str(x.value or "")) for x in col),default=10)
            ws.column_dimensions[letter].width = min(max(max_len+2,14),38)
        for row in ws.iter_rows(min_row=2):
            for cell in row:
                cell.alignment = Alignment(vertical="top",wrap_text=True)
    buffer.seek(0)
    return buffer.getvalue()


def fetch_provider_positions():
    """Provider integration hook. Implement using the GPS vendor's official API contract."""
    raise NotImplementedError("No GPS provider API adapter has been configured.")


def status_for_truck(distance, age_min, stop_min, speed, limit_km, stale_mins=GPS_STALE_MINUTES):
    # Priority prevents a normal/stationary state hiding a separation breach or bad GPS.
    if age_min < -5:
        return "GPS TIME CHECK"
    if age_min > stale_mins:
        return "STALE GPS"
    if distance > limit_km:
        return "OUTSIDE LIMIT"
    if stop_min > STOP_MINUTES or speed < STOP_SPEED_KMH:
        return "PARKED / STOPPED" if stop_min > STOP_MINUTES else "STOPPED (UNDER 15 MIN)"
    return "WITHIN CONVOY"




def password_digest(password: str, salt: str) -> str:
    import hashlib
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 310000).hex()


def create_user_account(username: str, password: str, role: str = "Controller"):
    import secrets
    salt = secrets.token_hex(16)
    execute("INSERT INTO app_users(username,password_hash,salt,role,active,created_at) VALUES(?,?,?,?,1,?)",
            (username.strip(), password_digest(password, salt), salt, role, iso_now()))


def verify_login(username: str, password: str) -> bool:
    found = rows("SELECT * FROM app_users WHERE username=? COLLATE NOCASE AND active=1", (username.strip(),))
    if not found:
        return False
    account = found[0]
    return password_digest(password, account["salt"]) == account["password_hash"]


def get_user(username: str):
    found = rows("SELECT username,role,active FROM app_users WHERE username=? COLLATE NOCASE", (username,))
    return found[0] if found else None


def login_gate():
    account_count = rows("SELECT COUNT(*) AS n FROM app_users")[0]["n"]
    st.title("🚛 CONVOY MONITOR")
    st.caption(f"{COMPANY} | Secure multi-user access")
    if account_count == 0:
        st.subheader("Create the first administrator")
        st.info("One-time setup: create the administrator account, then sign in.")
        with st.form("first_admin_setup"):
            username = st.text_input("Administrator username")
            password = st.text_input("Password", type="password")
            confirm = st.text_input("Confirm password", type="password")
            submitted = st.form_submit_button("Create administrator", type="primary")
            if submitted:
                if len(username.strip()) < 3:
                    st.error("Username must contain at least 3 characters.")
                elif len(password) < 10:
                    st.error("Use a password with at least 10 characters.")
                elif password != confirm:
                    st.error("The passwords do not match.")
                else:
                    create_user_account(username, password, "Administrator")
                    st.success("Administrator created. Sign in below.")
                    st.rerun()
        st.stop()

    if not st.session_state.get("authenticated", False):
        with st.form("login_form"):
            st.subheader("Sign in")
            username = st.text_input("Username")
            password = st.text_input("Password", type="password")
            submitted = st.form_submit_button("Log in", type="primary")
            if submitted:
                if verify_login(username, password):
                    user = get_user(username)
                    st.session_state["authenticated"] = True
                    st.session_state["username"] = user["username"]
                    st.session_state["user_role"] = user["role"]
                    st.rerun()
                else:
                    st.error("Incorrect username/password, or account inactive.")
        st.stop()

    current = get_user(st.session_state.get("username", ""))
    if not current or not current["active"]:
        for key in ["authenticated", "username", "user_role"]:
            st.session_state.pop(key, None)
        st.error("Account inactive. Please sign in again.")
        st.stop()
    st.session_state["user_role"] = current["role"]


def render_user_admin():
    if st.session_state.get("user_role") != "Administrator":
        return
    with st.sidebar.expander("👥 User account management"):
        with st.form("add_user_form", clear_on_submit=True):
            st.markdown("**Create staff login**")
            new_name = st.text_input("New username")
            new_password = st.text_input("Temporary password", type="password")
            role = st.selectbox("Access level", ["Controller", "Viewer"])
            submitted = st.form_submit_button("Create user")
            if submitted:
                exists = rows("SELECT username FROM app_users WHERE username=? COLLATE NOCASE", (new_name.strip(),))
                if len(new_name.strip()) < 3:
                    st.error("Username must contain at least 3 characters.")
                elif len(new_password) < 10:
                    st.error("Password must contain at least 10 characters.")
                elif exists:
                    st.error("That username already exists.")
                else:
                    create_user_account(new_name, new_password, role)
                    st.success(f"Created login for {new_name.strip()}.")
                    st.rerun()
        accounts = rows("SELECT username,role,active,created_at FROM app_users ORDER BY username")
        st.dataframe(accounts, use_container_width=True, hide_index=True)
        with st.form("account_status_form"):
            target = st.selectbox("Account to activate/deactivate", [u["username"] for u in accounts])
            selected = next(u for u in accounts if u["username"] == target)
            active = st.checkbox("Account active", value=bool(selected["active"]))
            submitted = st.form_submit_button("Update account")
            if submitted:
                if target.casefold() == st.session_state["username"].casefold() and not active:
                    st.error("You cannot deactivate your own account.")
                else:
                    execute("UPDATE app_users SET active=? WHERE username=?", (int(active), target))
                    st.success("Account updated.")
                    st.rerun()


def demo_frame():
    now = utc_now()
    rows_ = [
        ["DEMO-01","DEMO-101","TOUR-101",-15.3875,28.3228,True,now-timedelta(minutes=1),62],
        ["DEMO-01","DEMO-102","TOUR-101",-15.3950,28.3300,False,now-timedelta(minutes=1),60],
        ["DEMO-01","DEMO-103","TOUR-101",-15.3750,28.3150,False,now-timedelta(minutes=1),58],
    ]
    return pd.DataFrame(rows_,columns=GPS_COLUMNS)


init_db()
login_gate()
st.title("🚛 CONVOY MONITOR")
st.caption(f"{COMPANY} | Fleet Intelligence, Convoy Control & Transit Reporting")
st.info("GPS feed: CSV upload / saved GPS history. Live provider integration requires the vendor API credentials and API specification.")

with st.sidebar:
    st.markdown(f"**Signed in:** {st.session_state['username']}")
    st.caption(f"Access: {st.session_state['user_role']}")
    render_user_admin()
    if st.button("Log out", use_container_width=True):
        for key in ["authenticated", "username", "user_role"]:
            st.session_state.pop(key, None)
        st.rerun()
    st.divider()
    st.header("Monitoring settings")
    limit_km = st.number_input("Maximum convoy separation (km)",0.5,100.0,DEFAULT_LIMIT_KM,0.5)
    warning_km = st.number_input("Warning threshold (km)",0.5,float(limit_km),min(WARNING_KM,float(limit_km)),0.5)
    stale_mins = st.number_input("Stale GPS threshold (minutes)",1,240,GPS_STALE_MINUTES,1)
    timezone_name = st.selectbox("Timezone for GPS timestamps without offset",["Africa/Lusaka","UTC"])
    uploaded = st.file_uploader("Upload GPS CSV",type=["csv"], disabled=st.session_state["user_role"]=="Viewer")
    st.caption(f"Database: {DB_PATH.resolve()}")

if uploaded is not None:
    try:
        normalized = normalize_gps(pd.read_csv(uploaded),timezone_name)
        count = save_gps_history(normalized,"CSV_UPLOAD")
        st.success(f"Imported {len(normalized)} valid GPS fixes; {count} new history records saved permanently.")
    except Exception as exc:
        st.error(f"GPS import failed: {exc}")

# The demonstration source is clearly isolated and is never silently persisted as operational GPS.
all_registry = get_convoys()
active_registry = [c for c in all_registry if c["status"]=="ACTIVE"]
assigned_regs = sorted({r for c in active_registry for r in json.loads(c["registrations"])})
latest_rows = latest_gps(assigned_regs) if assigned_regs else []
latest_by_reg = {r["registration"]:r for r in latest_rows}

st.divider()
st.header("Convoy formation and control")
with st.expander("➕ Create a convoy",expanded=not bool(active_registry)):
    with st.form("create_convoy",clear_on_submit=True):
        cid = st.text_input("Convoy ID",placeholder="e.g. ETA-026")
        commodity = st.text_input("Commodity",placeholder="Copper cathodes / copper concentrate")
        client = st.text_input("Client")
        truck_text = st.text_input("Truck registrations (comma-separated)",placeholder="ABC123, ABC124, ABC125")
        lead = st.text_input("Lead/reference truck registration")
        submit = st.form_submit_button("Create convoy",type="primary", disabled=st.session_state["user_role"]=="Viewer")
        if submit:
            regs = list(dict.fromkeys(x.strip().upper() for x in truck_text.split(",") if x.strip()))
            if not cid.strip() or not commodity.strip() or not client.strip():
                st.error("Convoy ID, commodity and client are required.")
            elif not 3 <= len(regs) <= 5:
                st.error("A convoy must have 3–5 unique truck registrations.")
            elif lead.strip().upper() not in regs:
                st.error("The lead/reference truck must be one of the assigned registrations.")
            elif any(cid.strip().casefold()==c["convoy_id"].casefold() for c in all_registry):
                st.error("That convoy ID has already been used.")
            elif set(regs) & set(assigned_regs):
                st.error("Already-assigned trucks: " + ", ".join(sorted(set(regs)&set(assigned_regs))))
            else:
                create_convoy(cid,commodity,client,regs,lead.strip().upper())
                st.success("Convoy created. Assignments will remain locked until offloading is confirmed.")
                st.rerun()

if active_registry:
    st.subheader("Active convoy controls")
    for c in active_registry:
        regs = json.loads(c["registrations"])
        a,b,d = st.columns([2,2,2])
        a.markdown(f"**{c['convoy_id']}**")
        a.caption(f"{c['client_name']} | {c['commodity']} | Lead: {c['reference_registration']}")
        parked = b.checkbox("Operator confirms parked",value=bool(c["manual_parked"]),key=f"park_{c['convoy_id']}", disabled=st.session_state["user_role"]=="Viewer")
        if int(parked)!=int(c["manual_parked"]):
            update_convoy(c["convoy_id"],manual_parked=int(parked)); st.rerun()
        if d.button("Confirm offloaded / close",key=f"off_{c['convoy_id']}", disabled=st.session_state["user_role"]=="Viewer"):
            add_event(c["convoy_id"],"OFFLOADING","DEPARTURE",source="OPERATOR",notes="Offloading confirmed; convoy closed")
            update_convoy(c["convoy_id"],status="OFFLOADED",offloaded_at=iso_now(),manual_parked=0)
            st.success(f"{c['convoy_id']} closed.")
            st.rerun()

# Refresh latest fixes after imports.
active_registry = get_convoys(active_only=True)
assigned_regs = sorted({r for c in active_registry for r in json.loads(c["registrations"])})
latest_rows = latest_gps(assigned_regs) if assigned_regs else []
latest_by_reg = {r["registration"]:r for r in latest_rows}
process_geofences(active_registry,latest_by_reg)

# Evaluate active assigned trucks against their designated lead.
monitor_rows=[]
now=utc_now()
for c in active_registry:
    regs=json.loads(c["registrations"])
    lead=latest_by_reg.get(c["reference_registration"])
    for reg in regs:
        fix=latest_by_reg.get(reg)
        if not fix:
            monitor_rows.append({"convoy_id":c["convoy_id"],"registration":reg,"latitude":None,"longitude":None,
                "distance_km":float("nan"),"speed_kmh":float("nan"),"gps_age_min":float("nan"),
                "stationary_min":0.0,"status":"GPS MISSING","gps_time":None})
            continue
        distance=haversine_km(lead["latitude"],lead["longitude"],fix["latitude"],fix["longitude"]) if lead else float("nan")
        age=(now-parse_utc(fix["gps_time"])).total_seconds()/60 if fix["gps_time"] else float("inf")
        stop=stationary_minutes(reg)
        status=status_for_truck(distance,age,stop,float(fix["speed_kmh"]),float(limit_km),int(stale_mins))
        monitor_rows.append({"convoy_id":c["convoy_id"],"registration":reg,"latitude":fix["latitude"],"longitude":fix["longitude"],
            "distance_km":distance,"speed_kmh":fix["speed_kmh"],"gps_age_min":age,"stationary_min":stop,
            "status":status,"gps_time":fix["gps_time"]})
monitor=pd.DataFrame(monitor_rows)

st.divider()
st.header("Live convoy status")
legend=st.columns(4)
for col,label,color in zip(legend,["GREEN · Normal","PINK · Approaching limit","RED · 10 km breach","GREY · Parked >15 min"],["#DCFCE7","#FCE7F3","#FEE2E2","#E5E7EB"]):
    col.markdown(f"<div style='background:{color};padding:9px;border-radius:8px;text-align:center'><b>{label}</b></div>",unsafe_allow_html=True)

for c in active_registry:
    group=monitor[monitor["convoy_id"]==c["convoy_id"]] if not monitor.empty else pd.DataFrame()
    maxdist=group["distance_km"].max() if not group.empty else float("nan")
    any_breach=(group["status"]=="OUTSIDE LIMIT").any() if not group.empty else False
    any_warning=(group["distance_km"]>=warning_km).any() if not group.empty else False
    any_park=(group["stationary_min"]>STOP_MINUTES).any() if not group.empty else False
    stale=(group["status"].isin(["STALE GPS","GPS MISSING","GPS TIME CHECK"])).any() if not group.empty else True
    if c["manual_parked"] or any_park: status="GREY"; bg="#E5E7EB"; fg="#374151"; label="PARKED / STATIONARY"
    elif any_breach: status="RED"; bg="#FEE2E2"; fg="#991B1B"; label="10 KM RULE BREACHED"
    elif any_warning or stale: status="PINK"; bg="#FCE7F3"; fg="#9D174D"; label="APPROACHING LIMIT / GPS ATTENTION"
    else: status="GREEN"; bg="#DCFCE7"; fg="#166534"; label="CONVOY OPERATING NORMALLY"
    missing=group[group["status"]=="GPS MISSING"]["registration"].tolist() if not group.empty else []
    st.markdown(f"""<div class="convoy-card" style="background:{bg};color:{fg};border-color:{fg}">
    <div style="display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap"><b style="font-size:20px">{html.escape(c['convoy_id'])}</b><b>{label}</b></div>
    <div>Client: {html.escape(c['client_name'])} | Commodity: {html.escape(c['commodity'])}</div>
    <div>Trucks: {html.escape(', '.join(json.loads(c['registrations'])))}</div>
    <div>Lead: {html.escape(c['reference_registration'])} | Furthest distance: {maxdist:.2f} km</div>
    {('<div>GPS missing: '+html.escape(', '.join(missing))+'</div>') if missing else ''}
    </div>""",unsafe_allow_html=True)

if not active_registry:
    st.info("No active convoys. Create a convoy to begin monitoring.")

st.divider()
st.header("Checkpoint milestones")
st.caption("Geofence entry/exit can automatically record arrival/departure. Kazungula and GRB crossing should be confirmed by an operator because a GPS radius alone cannot prove a completed crossing.")
if active_registry:
    chosen=st.selectbox("Convoy for milestone entry",[c["convoy_id"] for c in active_registry],key="milestone_convoy")
    cp=st.selectbox("Checkpoint",CHECKPOINTS)
    typ=st.selectbox("Milestone",MILESTONE_TYPES[cp])
    note=st.text_input("Operator note (optional)")
    if st.button("Record milestone",type="primary", disabled=st.session_state["user_role"]=="Viewer"):
        add_event(chosen,cp,typ,source="OPERATOR",notes=note)
        st.success(f"{chosen}: {cp} {typ} recorded.")
    evs=get_events(chosen)
    if evs:
        st.dataframe(pd.DataFrame(evs)[["checkpoint","event_type","event_time","source","notes"]],use_container_width=True,hide_index=True)
else:
    st.info("Milestone entry becomes available when a convoy is active.")

with st.expander("Geofence configuration"):
    st.warning("The default coordinates are disabled placeholders. Enter verified operational coordinates and enable each geofence before relying on automatic events.")
    fences=get_geofences()
    for f in fences:
        with st.form(f"fence_{f['checkpoint']}"):
            st.markdown(f"**{f['checkpoint']}**")
            x,y,z=st.columns(3)
            lat=x.number_input("Latitude",value=float(f["latitude"]),format="%.6f",key=f"lat_{f['checkpoint']}")
            lon=y.number_input("Longitude",value=float(f["longitude"]),format="%.6f",key=f"lon_{f['checkpoint']}")
            radius=z.number_input("Radius (km)",min_value=0.1,max_value=30.0,value=float(f["radius_km"]),key=f"rad_{f['checkpoint']}")
            enabled=st.checkbox("Enable geofence",value=bool(f["enabled"]),key=f"en_{f['checkpoint']}")
            if st.form_submit_button("Save geofence", disabled=st.session_state["user_role"]=="Viewer"):
                execute("UPDATE checkpoint_geofences SET latitude=?,longitude=?,radius_km=?,enabled=? WHERE checkpoint=?",
                        (lat,lon,radius,int(enabled),f["checkpoint"]))
                st.success(f"{f['checkpoint']} geofence saved."); st.rerun()

st.divider()
st.header("Find a specific truck")
st.caption("Search by full or partial horse/trailer registration. The lookup checks active assignments and offloaded convoy history.")
truck_query = st.text_input(
    "Truck registration search",
    placeholder="e.g. ABC123 or part of a registration",
    key="truck_registration_search",
).strip().casefold()

if truck_query:
    registry_matches = []
    for convoy_record in get_convoys():
        convoy_regs = json.loads(convoy_record["registrations"])
        for registration in convoy_regs:
            if truck_query in registration.casefold():
                registry_matches.append({
                    "Truck registration": registration,
                    "Convoy ID": convoy_record["convoy_id"],
                    "Client": convoy_record["client_name"],
                    "Commodity": convoy_record["commodity"],
                    "Assignment status": convoy_record["status"],
                    "Reference truck": convoy_record["reference_registration"],
                    "Manual parked": "Yes" if convoy_record["manual_parked"] else "No",
                    "Offloaded at": convoy_record["offloaded_at"] or "",
                })

    if registry_matches:
        matches_df = pd.DataFrame(registry_matches)
        st.dataframe(matches_df, use_container_width=True, hide_index=True)
        active_match_regs = set(
            matches_df.loc[matches_df["Assignment status"] == "ACTIVE", "Truck registration"]
        )
        if active_match_regs and not monitor.empty:
            live_matches = monitor[monitor["registration"].isin(active_match_regs)].copy()
            if not live_matches.empty:
                st.markdown("**Latest monitoring information**")
                st.dataframe(live_matches, use_container_width=True, hide_index=True)
            else:
                st.info("The truck is assigned to an active convoy, but no GPS fix has been received yet.")
        elif not active_match_regs:
            st.info("This truck appears only in offloaded convoy history; it is not currently assigned to an active convoy.")
    else:
        st.warning("No truck registration matching that search was found in convoy assignments.")

st.header("Truck monitoring detail")
if not monitor.empty:
    st.dataframe(monitor,use_container_width=True,hide_index=True)
    coords=monitor.dropna(subset=["latitude","longitude"])[["latitude","longitude"]]
    if not coords.empty:
        st.map(coords)
else:
    st.info("Truck-level GPS details will appear after valid GPS history is imported.")

st.header("Reports — convoys still in transit")
report=build_transit_report(active_registry,latest_by_reg)
st.caption("All ACTIVE convoys are included, including manually parked or automatically stationary convoys. A convoy leaves this report only after an operator confirms offloading.")
st.dataframe(report,use_container_width=True,hide_index=True)
try:
    data_xlsx=excel_bytes(report)
    st.download_button("⬇ Download Excel — Convoys in Transit",data=data_xlsx,
        file_name=f"convoys_in_transit_{utc_now():%Y%m%d_%H%M}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",type="primary")
except Exception as exc:
    st.error(f"Excel export unavailable: {exc}. Install openpyxl from requirements.txt.")

with st.expander("Offloaded convoy history"):
    closed=[c for c in get_convoys() if c["status"]=="OFFLOADED"]
    if closed:
        st.dataframe(pd.DataFrame([{"Convoy ID":c["convoy_id"],"Client":c["client_name"],"Commodity":c["commodity"],
            "Registrations":", ".join(json.loads(c["registrations"])),"Offloaded at":c["offloaded_at"]} for c in closed]),
            use_container_width=True,hide_index=True)
    else:
        st.write("No offloaded convoys recorded yet.")

st.caption(f"{APP_NAME} | {COMPANY} | Signed in as {st.session_state['username']} | Database: {DB_PATH.resolve()}")
