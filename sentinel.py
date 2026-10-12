# acoustic sentinel - serial reader, anomaly detector, telegram + phone camera alerts
# pip install pyserial requests pyyaml
# run: python sentinel.py
# reads config.yaml, writes status.json (for dashboard.py) and events.csv (alert log)
import csv
import json
import math
import os
import re
import threading
import time
from collections import deque
from datetime import datetime
from urllib.parse import urljoin

import requests
import serial
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = yaml.safe_load(open(os.path.join(HERE, "config.yaml"), encoding="utf-8"))
STATUS_PATH = os.path.join(HERE, "status.json")
EVENTS_PATH = os.path.join(HERE, "events.csv")
VIDEOS_DIR = os.path.join(HERE, "videos")
os.makedirs(VIDEOS_DIR, exist_ok=True)

NB = 8  # number of goertzel bands, must match the sketch

_camera_lock = threading.Lock()
_camera_busy = False
_last_clip_end = 0.0


def parse_line(line):
    # row: ms,rms,zcr,peak,b1..b8 - anything starting with # is a device note, skip it
    if not line or line.startswith("#"):
        return None
    parts = line.split(",")
    if len(parts) != 4 + NB:
        return None
    try:
        vals = [float(p) for p in parts]
    except ValueError:
        return None
    ms, rms, zcr, peak = vals[0], vals[1], vals[2], vals[3]
    bands = vals[4:]
    return ms, rms, zcr, peak, bands


class ShapeClassifier:
    # online prototype classifier (lvq / art style), no training step.
    # each anomaly becomes a vector: its 8-band energy shape (normalized so
    # loudness doesn't matter) plus zcr and a scaled crest factor. that vector
    # is matched against a few prototypes by cosine similarity - closest wins,
    # and the winner gets nudged a little toward what was actually heard, so
    # labels tune themselves to this room over time. anything that doesn't
    # look like a known prototype spawns a new one on the spot (type_1,
    # type_2, ...), so the set of labels grows as it listens.

    SEEDS = {
        "impact":      [0.125] * 8 + [0.05, 0.80],
        "tonal_alarm": [0.02, 0.02, 0.02, 0.02, 0.02, 0.70, 0.14, 0.06] + [0.20, 0.25],
        "voice":       [0.05, 0.15, 0.25, 0.25, 0.15, 0.10, 0.03, 0.02] + [0.18, 0.30],
        "machine_hum": [0.60, 0.20, 0.10, 0.05, 0.025, 0.015, 0.005, 0.005] + [0.05, 0.15],
    }

    def __init__(self, learn_rate=0.05, novelty=0.75):
        self.learn_rate = learn_rate
        self.novelty = novelty  # below this cosine similarity, call it a new type
        self.protos = {k: list(v) for k, v in self.SEEDS.items()}
        self.next_id = 1

    @staticmethod
    def _vector(zcr, peak, rms, bands):
        total = sum(bands) + 1e-6
        shape = [b / total for b in bands]
        crest = min((peak / (rms + 1e-6)) / 10.0, 1.0)
        return shape + [min(zcr, 1.0), crest]

    @staticmethod
    def _cosine(a, b):
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a)) or 1e-6
        nb = math.sqrt(sum(x * x for x in b)) or 1e-6
        return dot / (na * nb)

    def classify(self, zcr, peak, rms, bands):
        v = self._vector(zcr, peak, rms, bands)
        best_label, best_sim = None, -1.0
        for label, proto in self.protos.items():
            sim = self._cosine(v, proto)
            if sim > best_sim:
                best_label, best_sim = label, sim

        if best_sim < self.novelty:
            label = f"type_{self.next_id}"
            self.next_id += 1
            self.protos[label] = v
            return label

        proto = self.protos[best_label]
        self.protos[best_label] = [p + self.learn_rate * (x - p) for p, x in zip(proto, v)]
        return best_label


class Baseline:
    # running mean/std per feature (ewma), no training step - it adapts as it listens.
    # only updates while the signal looks normal, so a long anomaly can't drag
    # the baseline toward itself.
    def __init__(self, dim, alpha=0.02, z_threshold=4.0, warmup=50):
        self.alpha = alpha
        self.z_threshold = z_threshold
        self.warmup = warmup
        self.n = 0
        self.mean = [0.0] * dim
        self.var = [1.0] * dim

    def score(self, x):
        self.n += 1
        z = 0.0
        for i, v in enumerate(x):
            sd = self.var[i] ** 0.5
            z = max(z, abs(v - self.mean[i]) / (sd + 1e-6))
        if self.n <= self.warmup or z < self.z_threshold:
            for i, v in enumerate(x):
                d = v - self.mean[i]
                self.mean[i] += self.alpha * d
                self.var[i] = (1 - self.alpha) * (self.var[i] + self.alpha * d * d)
        return 0.0 if self.n <= self.warmup else z


def is_camera_busy():
    return _camera_busy


def record_phone_clip(name, cfg):
    # talks to the "ip webcam" android app over wifi: start, wait, stop, then
    # pull the finished file down into videos/. runs on its own thread so a
    # slow phone or network never blocks the serial read loop.
    global _camera_busy, _last_clip_end
    cam = cfg["camera"]
    base = cam["phone_url"].rstrip("/")
    out = os.path.join(VIDEOS_DIR, name)
    try:
        requests.get(base + cam["start_path"], timeout=5).raise_for_status()
        time.sleep(float(cam["clip_seconds"]))
        requests.get(base + cam["stop_path"], timeout=10)
        time.sleep(2)  # give the phone a moment to finish writing the file
        list_url = base + cam["list_path"]
        links = re.findall(r'href="([^"]+\.(?:mp4|mkv|webm|mov))"',
                            requests.get(list_url, timeout=10).text, re.I)
        if not links:
            print("[camera] no video found on phone")
            return
        url = urljoin(list_url, sorted(links)[-1])  # newest file name sorts last
        with requests.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(out, "wb") as f:
                for chunk in r.iter_content(1 << 16):
                    f.write(chunk)
        print("[camera] saved", name)
    except requests.RequestException as e:
        print("[camera] failed:", type(e).__name__)
    finally:
        with _camera_lock:
            _camera_busy = False
            _last_clip_end = time.monotonic()


def start_clip(kind, cfg):
    # returns the filename right away (for logging) and starts the actual
    # recording in the background. returns "" if the camera is busy or still
    # in its own cooldown - it's one shared resource, so this is global, not
    # per sound type.
    global _camera_busy
    with _camera_lock:
        if _camera_busy or time.monotonic() - _last_clip_end < float(cfg["camera"]["cooldown_seconds"]):
            return ""
        _camera_busy = True
    name = f"{datetime.now():%Y%m%d_%H%M%S}_{kind}.mp4"
    threading.Thread(target=record_phone_clip, args=(name, cfg), daemon=True).start()
    return name


def purge_old_data(retention_days):
    # deletes clips and event rows older than retention_days. runs once a
    # day from the main loop, plus once at startup to catch anything already
    # stale from before.
    cutoff_ts = time.time() - retention_days * 86400
    cutoff_dt = datetime.fromtimestamp(cutoff_ts)

    removed_clips = 0
    for fn in os.listdir(VIDEOS_DIR):
        path = os.path.join(VIDEOS_DIR, fn)
        try:
            if os.path.isfile(path) and os.path.getmtime(path) < cutoff_ts:
                os.remove(path)
                removed_clips += 1
        except OSError:
            pass

    removed_rows = 0
    if os.path.exists(EVENTS_PATH):
        with open(EVENTS_PATH, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames
            kept = []
            for row in reader:
                try:
                    ts = datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M:%S")
                except (KeyError, ValueError):
                    kept.append(row)  # can't parse it, don't guess, just keep it
                    continue
                if ts >= cutoff_dt:
                    kept.append(row)
                else:
                    removed_rows += 1
        if fieldnames and removed_rows:
            with open(EVENTS_PATH, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=fieldnames)
                w.writeheader()
                w.writerows(kept)

    if removed_clips or removed_rows:
        print(f"[cleanup] removed {removed_clips} clip(s), {removed_rows} old event row(s)")


def log_event(ts, kind, score, rms, peak, video=""):
    new = not os.path.exists(EVENTS_PATH)
    with open(EVENTS_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["timestamp", "type", "score", "rms", "peak", "video"])
        w.writerow([ts.strftime("%Y-%m-%d %H:%M:%S"), kind, round(score, 2),
                    round(rms, 1), round(peak, 1), video])


def send_telegram(kind, score, rms, peak, ts, video):
    tok, chat = str(CFG["telegram"]["token"]), str(CFG["telegram"]["chat_id"])
    if "PASTE" in tok or "PASTE" in chat:
        print("[telegram] token/chat_id not set, skipping")
        return
    text = (f"acoustic sentinel alert: {kind}\n{ts:%d %b %H:%M:%S}\n"
            f"score={score:.1f}  rms={rms:.0f}  peak={peak:.0f}")
    if video:
        text += "\nrecording a video clip..."
    try:
        requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                      data={"chat_id": chat, "text": text}, timeout=10)
    except requests.RequestException as e:
        print("[telegram] failed:", type(e).__name__)


def write_status(kind, score, rms, peak, hist):
    tmp = STATUS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"updated": time.time(), "type": kind, "score": round(score, 2),
                   "rms": round(rms, 1), "peak": round(peak, 1), "history": list(hist),
                   "recording": is_camera_busy()}, f)
    os.replace(tmp, STATUS_PATH)


def main():
    ser = serial.Serial(CFG["serial"]["port"], CFG["serial"]["baud"], timeout=1)
    time.sleep(2)  # uno resets when the port opens
    ser.reset_input_buffer()
    print(f"[sentinel] listening on {CFG['serial']['port']}")

    baseline = Baseline(dim=1 + NB, z_threshold=float(CFG["z_threshold"]))
    classifier = ShapeClassifier()
    tg_cooldown = float(CFG["telegram"]["cooldown_seconds"])  # per sound type
    last_telegram = {}
    hist = deque(maxlen=200)
    last_status = 0.0
    kind, score, rms, peak = "normal", 0.0, 0.0, 0.0

    retention_days = int(CFG.get("retention_days", 180))
    purge_old_data(retention_days)  # catch anything already stale before we start
    last_purge = time.time()

    while True:
        try:
            line = ser.readline().decode("ascii", errors="ignore").strip()
        except serial.SerialException:
            print("[sentinel] serial lost, retrying")
            time.sleep(2)
            continue
        parsed = parse_line(line)
        if parsed is None:
            continue
        _, rms, zcr, peak, bands = parsed

        s = baseline.score([rms] + bands)
        anomalous = s >= float(CFG["z_threshold"])
        kind, score = (classifier.classify(zcr, peak, rms, bands), s) if anomalous else ("normal", s)

        now = time.time()
        hist.append([round(now, 2), round(rms, 1), round(peak, 1)])

        if anomalous and now - last_telegram.get(kind, 0) >= tg_cooldown:
            last_telegram[kind] = now
            ts = datetime.now()
            video = start_clip(kind, CFG)  # "" if the camera is busy or still cooling down
            print(f"[alert] {ts:%H:%M:%S} {kind} score={score:.1f} rms={rms:.0f}" +
                  (f" clip={video}" if video else ""))
            log_event(ts, kind, score, rms, peak, video)
            threading.Thread(target=send_telegram, args=(kind, score, rms, peak, ts, video),
                              daemon=True).start()

        if now - last_status > 0.2:  # don't hammer the status file every single window
            last_status = now
            write_status(kind, score, rms, peak, hist)

        if now - last_purge > 86400:  # once a day
            last_purge = now
            purge_old_data(retention_days)


if __name__ == "__main__":
    main()
