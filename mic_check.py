# mic_check.py - full system check for Acoustic Sentinel
#
# 1. close sentinel.py, the Arduino Serial Monitor and anything else using the Arduino port
# 2. run:  python mic_check.py
#    options: --skip-telegram   --skip-camera   --skip-buzzer
# takes about 40 seconds. read the PASS / WARN / FAIL list at the end.
import argparse
import os
import statistics
import sys
import time

import requests
import serial
import yaml
from serial.tools import list_ports

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

results = []


def add(status, name, detail="", fix=""):
    results.append(status)
    tag = {"PASS": "[ OK ]", "WARN": "[WARN]", "FAIL": "[FAIL]"}[status]
    print(f"{tag} {name}" + (f" - {detail}" if detail else ""))
    if fix and status != "PASS":
        print(f"       -> {fix}")


def pctl(vals, p):
    q = sorted(vals)
    return q[min(len(q) - 1, int(len(q) * p))]


# --------------------------------------------------------------------------------------
# serial port with automatic reconnect (USB cables sometimes drop for a moment)

class Port:
    def __init__(self, port, baud):
        self.port, self.baud, self.ser, self.reconnects = port, baud, None, 0

    def open(self):
        self.ser = serial.Serial(self.port, self.baud, timeout=1)
        time.sleep(2)                      # opening the port resets an Uno
        self.ser.reset_input_buffer()

    def readline(self):
        for _ in range(4):
            try:
                return self.ser.readline().decode("ascii", errors="ignore").strip()
            except (serial.SerialException, OSError):
                self.reconnects += 1
                try:
                    self.ser.close()
                except Exception:
                    pass
                time.sleep(2)
                try:
                    self.open()
                except serial.SerialException:
                    pass
        raise RuntimeError("lost the USB connection to the Arduino (try another cable / USB port)")

    def write(self, data):
        try:
            self.ser.write(data)
        except Exception:
            pass


def collect(p, seconds):
    rows, notes = [], []
    t0 = time.monotonic()
    while time.monotonic() - t0 < seconds:
        line = p.readline()
        if line.startswith("#"):
            notes.append(line)
            continue
        parts = line.split(",")
        if len(parts) == 14:
            try:
                rows.append([float(x) for x in parts])
            except ValueError:
                pass
    return rows, notes, time.monotonic() - t0


# --------------------------------------------------------------------------------------

def check_port(cfg):
    port, baud = cfg["serial"]["port"], cfg["serial"]["baud"]
    ports = [x.device for x in list_ports.comports()]
    print(f"      ports found on this computer: {', '.join(ports) or 'none'}")
    if port not in ports:
        add("FAIL", f"Arduino port {port}", "not found",
            f"plug in the Arduino, then set serial.port in config.yaml to one of: {', '.join(ports) or '(none found)'}")
        return None
    p = Port(port, baud)
    try:
        p.open()
    except serial.SerialException as e:
        busy = "denied" in str(e).lower() or "13" in str(e)
        add("FAIL", f"Open {port}", "access denied / busy" if busy else str(e),
            "close sentinel.py, the Arduino Serial Monitor/Plotter and any other program using the port, then retry")
        return None
    add("PASS", f"Open {port}", f"{baud} baud")
    return p


def check_firmware(p):
    lines, t0 = [], time.monotonic()
    while time.monotonic() - t0 < 4:
        l = p.readline()
        if l:
            lines.append(l)
    data = [l for l in lines if not l.startswith("#")]
    if not data:
        add("FAIL", "Arduino data", "nothing received",
            "upload arduino_sketch.ino, make sure Board = Arduino Uno, and serial.baud is 250000 in config.yaml")
        return False
    cols = [len(l.split(",")) for l in data]
    mode = max(set(cols), key=cols.count)
    if mode == 14:
        add("PASS", "Firmware", "arduino_sketch.ino v2 is running (14 columns)")
        return True
    if mode == 12:
        add("FAIL", "Firmware", "OLD sketch (12 columns)", "re-upload the new arduino_sketch.ino")
    elif mode == 1 and data[0].replace(".", "").isdigit():
        add("FAIL", "Firmware", "the small test sketch is still on the board", "upload arduino_sketch.ino")
    else:
        add("FAIL", "Firmware", f"unreadable data ({data[0][:30]!r})",
            "baud mismatch: the sketch uses 250000, check serial.baud in config.yaml")
    return False


def check_signal(rows_q, rows_t, secs_q, notes):
    rate = len(rows_q) / max(secs_q, 0.1)
    if rate >= 56:
        add("PASS", "Data rate", f"{rate:.0f} windows/s (expected about 62)")
    else:
        add("FAIL", "Data rate", f"{rate:.0f} windows/s (expected about 62)",
            "Arduino is dropping windows: close other programs using USB, use a direct USB port, re-upload the sketch")
    over = cpu = None
    for n in notes:
        for part in n.lstrip("# ").split():
            try:
                if part.startswith("overruns="):
                    over = int(part.split("=")[1])
                if part.startswith("cpu="):
                    cpu = int(part.split("=")[1].rstrip("%"))
            except ValueError:
                pass
    if over is not None:
        if over > 0:
            add("WARN", "Arduino overruns", f"{over} dropped windows (cpu {cpu}%)", "re-upload the sketch; avoid USB hubs")
        else:
            add("PASS", "Arduino load", f"no dropped windows (cpu {cpu}%)")

    rq = [r[1] for r in rows_q]
    rt = [r[1] for r in rows_t]
    pk_q = max(r[3] for r in rows_q)
    pk_t = max(r[3] for r in rows_t)
    q_avg, q90, t90, t_max = statistics.mean(rq), pctl(rq, 0.9), pctl(rt, 0.9), max(rt)
    print(f"      quiet: rms avg {q_avg:.2f}, 90th% {q90:.2f}, peak {pk_q:.0f}   |   sound: rms 90th% {t90:.2f}, max {t_max:.1f}, peak {pk_t:.0f}")

    if max(pk_q, pk_t) >= 450:
        add("FAIL", "Clipping", f"peak {max(pk_q, pk_t):.0f} (limit is about 500)",
            "turn the MAX4466 gain screw DOWN; a clipped mic reads quieter when you talk")
    else:
        add("PASS", "Clipping", f"none (highest peak {max(pk_q, pk_t):.0f})")

    if q_avg > 15:
        add("FAIL", "Quiet-room noise", f"rms {q_avg:.1f} is far too high (want 0.3 to 8)",
            "bad GND/5V wire, gain too high, or electrical noise: re-seat wires, turn gain down, unplug the laptop charger")
    elif q_avg < 0.3:
        add("WARN", "Quiet-room level", f"rms {q_avg:.2f} is very low", "turn the gain screw up a little")
    else:
        add("PASS", "Quiet-room level", f"rms {q_avg:.2f}")

    if t90 >= 1.5 * q90 and t_max >= 2 * q_avg:
        add("PASS", "Mic reacts to sound", f"sound is {t90 / max(q90, 0.01):.1f}x louder than quiet")
    else:
        add("FAIL", "Mic reacts to sound", "clapping/talking is not clearly louder than quiet",
            "gain too low or too high, mic faulty, or OUT not on A0. Re-run after adjusting the gain screw")


def check_detection(rows_q, rows_t):
    try:
        import sentinel as S
    except Exception as e:
        add("FAIL", "Load sentinel.py", str(e), "run this from the same folder as sentinel.py")
        return
    scfg = S.load_config()
    det = S.Detector(dict(scfg["detection"]), S.ShapeClassifier(path=""))
    mode = scfg["mode"]["current"]
    det.sensitivity_mult = float(scfg["mode"].get(mode, {}).get("sensitivity_multiplier", 1.0))
    quiet_onsets = sound_onsets = 0
    n_q = len(rows_q)
    for i, v in enumerate(rows_q + rows_t):
        f = S.Frame(v[0], v[1], v[2], v[3], v[4:12], v[12], v[13])
        for kind, ev in det.feed(f):
            if kind == "onset":
                if i < n_q:
                    quiet_onsets += 1
                else:
                    sound_onsets += 1
    if not det.baseline.ready:
        add("WARN", "Detector learning", f"only {det.baseline.n} windows, needs {det.baseline.warmup_n}",
            "data rate too low to finish calibration")
        return
    add("PASS", "Detector learning",
        f"normal level {det.baseline.level:+.1f} dB, alert line +{det.effective_rise_db:.1f} dB above it")
    if sound_onsets >= 1:
        add("PASS", "Detector catches your sounds", f"{sound_onsets} alert(s) triggered by your clap/talk")
    else:
        add("FAIL", "Detector catches your sounds", "no alert triggered",
            "make the sound louder/closer, or raise the Sensitivity slider; fix any mic problems listed above first")
    if quiet_onsets == 0:
        add("PASS", "No false alarms in quiet")
    else:
        add("WARN", "False alarms in quiet", f"{quiet_onsets} alert(s) while you were quiet",
            "room noise is varying; lower the Sensitivity slider or find the noise source")


def check_telegram(cfg):
    tg = cfg.get("telegram", {})
    tok, chat = str(tg.get("token", "")).strip(), str(tg.get("chat_id", "")).strip()
    if not tok or not chat or "PASTE" in tok + chat:
        add("FAIL", "Telegram config", "token or chat_id not set", "fill in telegram.token and telegram.chat_id in config.yaml")
        return
    try:
        r = requests.get(f"https://api.telegram.org/bot{tok}/getMe", timeout=10)
        if r.status_code in (401, 404):
            add("FAIL", "Telegram token", "rejected (revoked or mistyped)",
                "copy a fresh token from BotFather into config.yaml")
            return
        r.raise_for_status()
        r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage", timeout=15,
                          data={"chat_id": chat, "text": "Acoustic Sentinel system check: Telegram works."})
        if r.status_code == 200:
            add("PASS", "Telegram", "test message sent - check your phone")
        else:
            add("FAIL", "Telegram send", f"{r.status_code} {r.text[:80]}",
                "open your bot in Telegram and press Start, and check chat_id")
    except requests.RequestException as e:
        add("FAIL", "Telegram network", type(e).__name__, "check your internet connection / firewall")


def check_camera(cfg):
    cam = cfg.get("camera", {})
    if not cam.get("enabled"):
        add("PASS", "Camera", "disabled in config (no clips)")
        return
    url = str(cam.get("phone_url", "")).rstrip("/") + str(cam.get("stream_path", "/video"))
    auth = (cam["username"], cam.get("password", "")) if cam.get("username") else None
    try:
        with requests.get(url, stream=True, timeout=(5, 8), auth=auth) as r:
            r.raise_for_status()
            data, t0 = b"", time.time()
            for chunk in r.iter_content(16384):
                data += chunk
                if b"\xff\xd8" in data or time.time() - t0 > 6 or len(data) > 300000:
                    break
        if b"\xff\xd8" in data:
            add("PASS", "Phone camera", f"video stream received from {url}")
        else:
            add("FAIL", "Phone camera", "connected but no video frames", "start the stream in the IP Webcam app")
    except requests.RequestException as e:
        add("FAIL", "Phone camera", f"can't reach {url} ({type(e).__name__})",
            "phone and laptop on the same Wi-Fi, IP Webcam running, phone_url correct in config.yaml")
    try:
        import sentinel as S
        if S.find_ffmpeg():
            add("PASS", "ffmpeg (for clips)", "found")
        else:
            add("FAIL", "ffmpeg (for clips)", "not found", "pip install imageio-ffmpeg")
    except Exception:
        pass


def check_voice():
    try:
        import pyttsx3  # noqa: F401
        add("PASS", "Voice alarm", "pyttsx3 installed")
    except Exception:
        add("WARN", "Voice alarm", "pyttsx3 not installed (Windows PowerShell voice will be used instead)",
            "pip install pyttsx3")


def check_buzzer(p):
    print("\n      Buzzer test: the Arduino will pulse pin 8 + LED 13 for 2.5 seconds.")
    ans = input("      Press Enter to test, or type s to skip: ").strip().lower()
    if ans.startswith("s"):
        return
    p.write(b"A")
    time.sleep(3)
    ok = input("      Did the buzzer / LED on pin 8 activate? (y/n): ").strip().lower()
    if ok.startswith("y"):
        add("PASS", "Hardware buzzer", "works")
    else:
        add("WARN", "Hardware buzzer", "no response",
            "buzzer + to pin 8, - to GND; or set serial.hardware_buzzer: false in config.yaml")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-telegram", action="store_true")
    ap.add_argument("--skip-camera", action="store_true")
    ap.add_argument("--skip-buzzer", action="store_true")
    a = ap.parse_args()

    try:
        with open(os.path.join(HERE, "config.yaml"), encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except OSError:
        sys.exit("config.yaml not found next to mic_check.py")
    cfg.setdefault("serial", {}).setdefault("port", "COM7")
    cfg["serial"].setdefault("baud", 250000)

    print("\n=== Acoustic Sentinel system check ===\n")
    p = check_port(cfg)
    if p and check_firmware(p):
        print("\nStep 1/2: stay COMPLETELY QUIET for 14 seconds (the detector learns your room)...")
        rows_q, notes_q, secs_q = collect(p, 14)
        print("\nStep 2/2: for 10 seconds - CLAP 3 times, then TALK normally about 20 cm from the mic...")
        rows_t, notes_t, _ = collect(p, 10)
        if len(rows_q) < 100 or len(rows_t) < 50:
            add("FAIL", "Mic data", f"too little data ({len(rows_q)} + {len(rows_t)} windows)", "check cable/port and rerun")
        else:
            print()
            check_signal(rows_q, rows_t, secs_q, notes_q + notes_t)
            check_detection(rows_q, rows_t)
        if not a.skip_buzzer and cfg["serial"].get("hardware_buzzer", True):
            check_buzzer(p)
        if p.reconnects:
            add("WARN", "USB connection", f"dropped {p.reconnects} time(s) during the test",
                "use a different USB cable / port")
        try:
            p.ser.close()
        except Exception:
            pass
    print()
    if not a.skip_telegram:
        check_telegram(cfg)
    if not a.skip_camera:
        check_camera(cfg)
    check_voice()

    f, w = results.count("FAIL"), results.count("WARN")
    print("\n=== RESULT ===")
    if f:
        print(f"{f} problem(s) to fix (marked [FAIL] above). Fix the first one and run again.")
    elif w:
        print(f"Ready, with {w} warning(s). You can run sentinel.py.")
    else:
        print("ALL GOOD. Run sentinel.py and the dashboard.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nstopped")
    except RuntimeError as e:
        print(f"\n[FAIL] {e}")