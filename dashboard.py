# acoustic sentinel - barebones dashboard
# pip install streamlit pandas
# run: streamlit run dashboard.py  (needs sentinel.py running alongside it)
import json
import os
import time

import pandas as pd
import streamlit as st

HERE = os.path.dirname(os.path.abspath(__file__))
STATUS_PATH = os.path.join(HERE, "status.json")
EVENTS_PATH = os.path.join(HERE, "events.csv")
VIDEOS_DIR = os.path.join(HERE, "videos")

st.set_page_config(page_title="Acoustic Sentinel", layout="wide")
st.title("Acoustic Sentinel")


@st.fragment(run_every=1)
def live():
    try:
        s = json.load(open(STATUS_PATH, encoding="utf-8"))
        online = time.time() - s["updated"] < 3
    except Exception:
        s, online = None, False

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Status", "LIVE" if online else "OFFLINE")
    c2.metric("Last sound", s["type"] if online else "-")
    c3.metric("Loudness (rms)", f'{s["rms"]:.0f}' if online else "-")
    c4.metric("Anomaly score", f'{s["score"]:.1f}' if online else "-")

    if not online:
        st.info("sentinel.py is not running.")
        return
    if s.get("recording"):
        st.warning("Recording a clip on the phone camera...")
    if s.get("history"):
        df = pd.DataFrame(s["history"], columns=["t", "rms", "peak"])
        df["sec ago"] = (df["t"] - df["t"].max()).round(1)
        st.caption("Loudness, last ~40 seconds")
        st.line_chart(df.set_index("sec ago")[["rms", "peak"]], height=240)


live()

st.subheader("Alert log")


# events table and clip picker live in one fragment so both stay in sync on
# every refresh - splitting them broke the clip list on auto-refresh before
@st.fragment(run_every=3)
def events_and_clips():
    try:
        df = pd.read_csv(EVENTS_PATH)
    except Exception:
        st.write("No alerts yet.")
        return
    st.dataframe(df.sort_values("timestamp", ascending=False),
                 use_container_width=True, hide_index=True, height=360)

    st.subheader("Clips")
    with_video = df[df["video"].notna() & (df["video"].astype(str) != "")]
    if with_video.empty:
        st.write("No clips yet.")
        return
    rows = with_video.sort_values("timestamp", ascending=False).reset_index(drop=True)
    names = [f"{r.timestamp}  -  {r.type}" for r in rows.itertuples()]
    i = st.selectbox("Alert", range(len(rows)), format_func=lambda k: names[k])
    path = os.path.join(VIDEOS_DIR, rows.loc[i, "video"])
    if os.path.exists(path):
        st.video(path)
    else:
        st.info("Clip not ready yet (still recording, or the phone didn't finish uploading).")


events_and_clips()
