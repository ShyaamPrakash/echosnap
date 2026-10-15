# acoustic sentinel v2 - interactive multi-mode dashboard
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

BAND_NAMES = ["60-125Hz", "190-250Hz", "310-440Hz", "500-690Hz",
              "750-1kHz", "1.1-1.6kHz", "1.7-2.5kHz", "2.6-3.9kHz"]


def send_command(payload):
    tmp = COMMAND_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    os.replace(tmp, COMMAND_PATH)


st.set_page_config(page_title="Acoustic Sentinel v2", layout="wide")
st.title("Acoustic Sentinel — Multi-Mode Audio Intelligence")

with st.sidebar:
    st.header("Control Center")
    mode_choice = st.radio(
        "Operating Mode",
        options=["security", "machine", "general"],
        format_func=lambda m: {
            "security": "🛡️ Security Mode (Intrusion & Voice Alarms)",
            "machine": "⚙️ Machine Mode (Predictive Maintenance)",
            "general": "📡 General Mode (All-Around Logging)",
        }[m],
    )
    if st.button("Apply Operating Mode", use_container_width=True):
        send_command({"action": "set_mode", "mode": mode_choice})
        st.success(f"Requested switch to {mode_choice.upper()} mode.")

    st.divider()
    st.subheader("Voice Alarm & Calibration")
    col_v1, col_v2 = st.columns(2)
    if col_v1.button("🔊 Test Voice", use_container_width=True):
        send_command({"action": "test_voice"})
    if col_v2.button("🔄 Recalibrate", use_container_width=True):
        send_command({"action": "recalibrate"})

    sens = st.slider("Sensitivity", 0.25, 4.0, 1.0, 0.25,
                     help="Higher = catches quieter/smaller anomalies (and more false alarms). 1.0 = default.")
    if st.button("Apply Sensitivity", use_container_width=True):
        send_command({"action": "set_sensitivity", "value": sens})
        st.success(f"Sensitivity set to x{sens:g}")

    voice_toggle = st.toggle("Enable Spoken Voice Alarms", value=True)
    if st.button("Update Voice Setting", use_container_width=True):
        send_command({"action": "set_voice_enabled", "enabled": voice_toggle})

    st.divider()
    st.subheader("Supervised Accuracy Boost")
    st.caption("If the last sound was misclassified, teach the prototype classifier the exact label:")
    teach_label = st.selectbox(
        "Correct Sound Label",
        ["impact", "voice", "tonal_alarm", "machine_hum", "bearing_fault", "glass_shatter"],
    )
    if st.button("Teach Last Event Label", use_container_width=True):
        send_command({"action": "teach", "label": teach_label})
        st.success(f"Taught classifier: last sound = {teach_label}")

    if st.button("Reset Prototypes to Defaults", use_container_width=True):
        send_command({"action": "reset_prototypes"})
        st.info("Prototypes reset to v2 factory seeds.")


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

    active_mode = s.get("mode", "security").upper()
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Active Mode", active_mode)
    state_label = {"learning": "CALIBRATING", "event": "ALERT / HEARING SOUND"}.get(s["state"], "ARMED & LISTENING")
    c2.metric("System State", state_label)

    lvl, base = s.get("level_db"), s.get("baseline_db")
    c3.metric("Loudness vs Normal", f"{lvl - base:+.1f} dB" if lvl is not None and base is not None else "-")

    last = s.get("last_event")
    c4.metric(
        "Last Detected Event",
        f'{last["type"]} ({int(last.get("confidence", 0)*100)}%)' if last else "-",
        last["time"] if last else None,
        delta_color="off",
    )

    cam = s.get("camera", {})
    c5.metric("Camera Status", {"ready": "Ready", "recording": "RECORDING", "unreachable": "Unreachable",
                                "off": "Off"}.get(cam.get("state"), "-"))

    if s["state"] == "learning":
        prog = float(s.get("learning_progress", 0.0))
        warm_s = float(s.get("warmup_seconds", 15.0))
        st.progress(prog, text=f"Extended Acoustic Learning ({int(prog * 100)}%) — profiling room/machine baseline ({warm_s:.0f}s window with outlier rejection)...")

    voice_info = s.get("voice", {})
    if s.get("mode") == "security":
        st.info(
            f"🛡️ **SECURITY MODE ACTIVE** — Watching for forced impacts, glass shatter, human voices, and tonal alarms. "
            f"Voice Deterrent: **{'ENABLED' if voice_info.get('enabled') else 'MUTED'}** "
            + (f" | Last Spoken: *\"{voice_info.get('last_message')}\"*" if voice_info.get("last_message") else "")
        )
    elif s.get("mode") == "machine":
        m = s.get("machine", {})
        m1, m2, m3 = st.columns(3)
        m1.metric("Machine Health Index", f"{m.get('health', 100):.1f}%", m.get("state", "OPTIMAL"), delta_color="off")
        m2.metric("Baseline Thermal/Load Drift", f"{m.get('drift_db', 0.0):+.1f} dB")
        m3.metric("Harmonic Tonality Index", f"{m.get('avg_tonal', 0.0):.2f}")

    if cam.get("state") == "unreachable":
        st.warning(f'Camera: {cam.get("error", "")}. Verify IP Webcam is running on the same Wi-Fi.')
    dev = s.get("device", {})
    if dev and int(dev.get("overruns", 0) or 0) > 0:
        st.warning(f'Arduino dropped audio windows (overruns={dev["overruns"]}, cpu={dev.get("cpu", "?")}%).')

    col_chart, col_spec = st.columns([2, 1])
    with col_chart:
        hist = s.get("history") or []
        if len(hist) >= 2:
            cols = ["t", "loudness", "normal", "rms", "alert level", "event"]
            rows = [(list(h) + [None] * 6)[:6] for h in hist]
            df = pd.DataFrame(rows, columns=cols)
            if df["alert level"].isna().all():            # older sentinel.py: only a single threshold value
                df["alert level"] = s.get("threshold_db")
            df["alert level"] = df["alert level"].ffill().bfill()
            df["event"] = df["event"].fillna(0)
            df = df.sort_values("t").reset_index(drop=True)
            df["sec_ago"] = (df["t"] - df["t"].max()).round(2)

            long = df.melt(id_vars=["sec_ago"], value_vars=["loudness", "normal", "alert level"],
                           var_name="series", value_name="dB")
            lo = float(min(df["loudness"].min(), df["normal"].min())) - 3
            hi = float(max(df["loudness"].max(), df["alert level"].max())) + 3
            order = ["loudness", "normal", "alert level"]
            lines = alt.Chart(long).mark_line(strokeWidth=2).encode(
                x=alt.X("sec_ago:Q", title="seconds ago", scale=alt.Scale(domain=[float(df["sec_ago"].min()), 0], nice=False)),
                y=alt.Y("dB:Q", title="loudness (dB)", scale=alt.Scale(domain=[lo, hi], nice=False)),
                color=alt.Color("series:N", sort=order, title=None, legend=alt.Legend(orient="top", direction="horizontal"),
                                scale=alt.Scale(domain=order, range=["#1f77b4", "#8d99ae", "#e5383b"])),
                strokeDash=alt.StrokeDash("series:N", sort=order, legend=None,
                                          scale=alt.Scale(domain=order, range=[[1, 0], [1, 0], [6, 4]])),
            )
            marks = alt.Chart(df[df["event"] > 0]).mark_circle(color="#e5383b", size=45).encode(
                x="sec_ago:Q", y="loudness:Q",
                tooltip=[alt.Tooltip("loudness:Q", title="dB")])
            st.caption("Loudness vs normal vs alert line (last ~40 s). Red dots = sound being treated as an anomaly.")
            st.altair_chart((lines + marks).properties(height=260), use_container_width=True)
        else:
            st.caption("Waiting for audio data...")

    with col_spec:
        bands = s.get("bands_db")
        base_bands = s.get("baseline_bands_db")
        if bands and base_bands and len(bands) == len(BAND_NAMES) == len(base_bands):
            bdf = pd.DataFrame({
                "Band": BAND_NAMES,
                "Above normal (dB)": [round(c - b, 1) for c, b in zip(bands, base_bands)],
            })
            st.caption("Each frequency band: how far above its normal level right now")
            chart = alt.Chart(bdf).mark_bar().encode(
                x=alt.X("Band:N", sort=BAND_NAMES, title=None, axis=alt.Axis(labelAngle=-45)),
                y=alt.Y("Above normal (dB):Q"),
                color=alt.condition(alt.datum["Above normal (dB)"] >= 6, alt.value("#e5383b"), alt.value("#1f77b4")),
            ).properties(height=260)
            st.altair_chart(chart, use_container_width=True)


live()

st.subheader("Event & Voice Alarm Log")


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