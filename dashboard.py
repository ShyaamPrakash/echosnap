# EchoSnap - interactive acoustic monitoring dashboard
# pip install -r requirements.txt
# run: streamlit run dashboard.py
import json
import os
import time
from datetime import datetime

import altair as alt
import pandas as pd
import streamlit as st

HERE = os.path.dirname(os.path.abspath(__file__))
STATUS_PATH = os.path.join(HERE, "status.json")
EVENTS_PATH = os.path.join(HERE, "events.csv")
COMMAND_PATH = os.path.join(HERE, "command.json")
VIDEOS_DIR = os.path.join(HERE, "videos")

def send_command(payload):
    tmp = COMMAND_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    os.replace(tmp, COMMAND_PATH)

def display_label(mode, label):
    label = label.strip().lower().replace(" ", "_")
    return label if label in ("impact", "voice", "others") else "others"


st.set_page_config(page_title="EchoSnap", page_icon="🔊", layout="wide")
st.markdown(
    """
    <style>
    [data-testid="stMetricLabel"] { font-size: 0.78rem; }
    [data-testid="stMetricValue"] { font-size: 1.2rem; white-space: nowrap; }
    [data-testid="stMetricDelta"] { font-size: 0.7rem; }
    </style>
    """,
    unsafe_allow_html=True,
)
st.title("EchoSnap")

with st.sidebar:
    st.header("Control Center")
    mode_choice = st.radio(
        "Operating Mode",
        options=["security", "machine", "general"],
        format_func=lambda m: {
            "security": "Security Mode",
            "machine": "Machine Mode",
            "general": "Combined Mode",
        }[m],
    )
    if st.button("Apply Operating Mode", use_container_width=True):
        send_command({"action": "set_mode", "mode": mode_choice})
        st.success(f"Requested switch to {mode_choice.upper()} mode.")

    st.divider()
    st.subheader("Calibration")
    if st.button("Recalibrate", use_container_width=True):
        send_command({"action": "recalibrate"})

    sens_percent = st.slider("Sensitivity", 5, 100, 100, 5,
                             format="%d%%",
                             help="100% is the normal committed 2.5 detector setting. Lower values require a larger sound before alerting.")
    if st.button("Apply Sensitivity", use_container_width=True):
        send_command({"action": "set_sensitivity", "value": sens_percent / 100.0})
        st.success(f"Sensitivity set to {sens_percent}%")

    st.divider()
    st.subheader("Classifier")
    st.caption("If the last sound was misclassified, teach the prototype classifier the exact label:")
    teach_label = st.selectbox(
        "Correct Sound Label",
        ["impact", "voice", "others"],
    )
    if st.button("Teach Last Event Label", use_container_width=True):
        send_command({"action": "teach", "label": teach_label})
        st.success(f"Classifier updated: {teach_label}")

    if st.button("Reset Prototypes to Defaults", use_container_width=True):
        send_command({"action": "reset_prototypes"})
        st.info("Classifier prototypes reset.")


@st.fragment(run_every=1)
def live():
    try:
        with open(STATUS_PATH, encoding="utf-8") as f:
            s = json.load(f)
        online = time.time() - s["updated"] < 3
    except Exception:
        s, online = None, False

    if not online:
        st.error("OFFLINE — `sentinel.py` is not running. Start `python sentinel.py` in a terminal.")
        return

    mode = s.get("mode", "security")
    active_mode = "COMBINED" if mode == "general" else mode.upper()
    c1, c2, c3, c4, c5 = st.columns([0.85, 0.9, 0.9, 1.2, 0.85])
    c1.metric("Mode", active_mode)
    if s["state"] == "learning":
        state_label = "CALIBRATING"
    elif s["state"] == "event":
        state_label = str(s.get("type", "others")).upper()
    else:
        state_label = "NORMAL" if mode == "machine" else "SILENT"
    c2.metric("State", state_label)

    lvl, base = s.get("level_db"), s.get("baseline_db")
    c3.metric("Level", f"{lvl - base:+.1f} dB" if lvl is not None and base is not None else "-")

    last = s.get("last_event")
    c4.metric(
        "Last Event",
        f'{last["type"]} ({int(last.get("confidence", 0)*100)}%)' if last else "-",
        last["time"] if last else None,
        delta_color="off",
    )

    cam = s.get("camera", {})
    c5.metric("Camera", {"ready": "Ready", "recording": "Recording", "unreachable": "Offline",
                         "off": "Off"}.get(cam.get("state"), "-"))

    if s["state"] == "learning":
        prog = float(s.get("learning_progress", 0.0))
        warm_s = float(s.get("warmup_seconds", 15.0))
        st.progress(prog, text=f"Calibration ({int(prog * 100)}%) — learning the normal sound level ({warm_s:.0f}s window)...")

    if s.get("mode") == "machine":
        m = s.get("machine", {})
        m1, m2, m3 = st.columns(3)
        m1.metric("Machine Health", f"{m.get('health', 100):.1f}%", m.get("state", "Normal"), delta_color="off")
        m2.metric("Load Drift", f"{m.get('drift_db', 0.0):+.1f} dB")
        m3.metric("Tonality", f"{m.get('avg_tonal', 0.0):.2f}")

    if cam.get("state") == "unreachable":
        st.warning(f'Camera: {cam.get("error", "")}. Verify IP Webcam is running on the same Wi-Fi.')
    dev = s.get("device", {})
    if dev and int(dev.get("overruns", 0) or 0) > 0:
        st.warning(f'Arduino dropped audio windows (overruns={dev["overruns"]}, cpu={dev.get("cpu", "?")}%).')

    with st.container():
        hist = s.get("history") or []
        if len(hist) >= 2:
            cols = ["t", "loudness", "normal", "rms", "threshold", "event"]
            rows = [(list(h) + [None] * 6)[:6] for h in hist]
            df = pd.DataFrame(rows, columns=cols)
            df["event"] = df["event"].fillna(0)
            df = df.sort_values("t").reset_index(drop=True)
            df["sec_ago"] = (df["t"] - df["t"].max()).round(2)

            lo = float(df["loudness"].min()) - 3
            hi = float(df["loudness"].max()) + 3
            lines = alt.Chart(df).mark_line(strokeWidth=2, color="#1f77b4").encode(
                x=alt.X("sec_ago:Q", title="seconds ago", scale=alt.Scale(domain=[float(df["sec_ago"].min()), 0], nice=False)),
                y=alt.Y("loudness:Q", title="loudness (dB)", scale=alt.Scale(domain=[lo, hi], nice=False)),
                tooltip=[alt.Tooltip("loudness:Q", title="dB")],
            )
            marks = alt.Chart(df[df["event"] > 0]).mark_circle(color="#e5383b", size=45).encode(
                x="sec_ago:Q", y="loudness:Q",
                tooltip=[alt.Tooltip("loudness:Q", title="dB")])
            st.caption("Signal history")
            st.markdown("**Red dots indicate anomalies.**")
            st.altair_chart((lines + marks).properties(height=260), use_container_width=True)
        else:
            st.caption("Waiting for audio data...")


live()

st.subheader("Event Log")


@st.fragment(run_every=3)
def events_and_clips():
    try:
        df = pd.read_csv(EVENTS_PATH, dtype={"video": str})
    except Exception:
        st.write("No alerts logged yet.")
        return
    if df.empty:
        st.write("No alerts logged yet.")
        return
    df = df.sort_values("timestamp", ascending=False)
    df["type"] = [display_label(str(mode), str(label)) for mode, label in zip(df["mode"], df["type"])]
    counts = df["type"].value_counts()
    st.caption("  ·  ".join(f"{k}: {v}" for k, v in counts.items()))
    st.dataframe(df, hide_index=True, height=320, use_container_width=True)

    st.subheader("Recorded Video Clips")
    with_video = df[df["video"].notna() & (df["video"].astype(str).str.strip() != "")]
    if with_video.empty:
        st.write("No clips recorded yet.")
        return
    rows = with_video.reset_index(drop=True)
    names = [f"{r.timestamp}  -  [{getattr(r, 'mode', 'alert')}] {r.type}" for r in rows.itertuples()]
    i = st.selectbox("Select Alert Clip", range(len(rows)), format_func=lambda k: names[k])
    name = str(rows.loc[i, "video"])
    path = os.path.join(VIDEOS_DIR, name)
    if os.path.exists(path):
        st.video(path)
    elif os.path.exists(path + ".failed"):
        with open(path + ".failed", encoding="utf-8") as f:
            st.error(f"No clip for this alert: {f.read()}")
    else:
        try:
            age = (datetime.now() - datetime.strptime(rows.loc[i, "timestamp"], "%Y-%m-%d %H:%M:%S")).total_seconds()
        except (TypeError, ValueError):
            age = 1e9
        if age < 90:
            st.info("Still recording and encoding this clip...")
        else:
            st.warning("Clip file not found.")


events_and_clips()