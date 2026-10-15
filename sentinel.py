


import csv
import json
import math
import os
import shutil
import subprocess
import threading
import time
from collections import deque
from datetime import datetime

import requests
import serial
import yaml

HERE=os.path.dirname(os.path.abspath(__file__))
STATUS_PATH=os.path.join(HERE, "status.json")
EVENTS_PATH=os.path.join(HERE, "events.csv")
PROTOS_PATH=os.path.join(HERE, "prototypes.json")
COMMAND_PATH=os.path.join(HERE, "command.json")
VIDEOS_DIR=os.path.join(HERE, "videos")

NB=8
# serial rows are 16 ms long
WINDOW_S=128/8000.0
REFERENCE_SENSITIVITY=2.5
BAND_HZ=[62.5*math.sqrt(lo*hi) for lo, hi in
           [(1, 2), (3, 4), (5, 7), (8, 11), (12, 17), (18, 26), (27, 40), (41, 63)]]

EVENT_FIELDS=["timestamp", "mode", "type", "confidence", "score", "rise_db", "duration_s",
                "rms", "peak", "video"]

DEFAULTS={
    "serial": {"port": "COM7", "baud": 250000},
    "mode": {
        "current": "security",
        "security": {
            "sensitivity_multiplier": 1.0,
        },
        "machine": {
            "drift_warn_db": 5.0,
            "harmonic_warn_tonal": 0.72,
            "sensitivity_multiplier": 0.9,
        },
    },
    "telegram": {"token": "PASTE_BOT_TOKEN_HERE", "chat_id": "PASTE_CHAT_ID_HERE",
                 "cooldown_seconds": 5, "send_clips": True},
    "camera": {"enabled": False, "phone_url": "http://10.180.63.44:8080", "stream_path": "/video",
               "username": "", "password": "", "clip_seconds": 15, "pre_seconds": 3,
               "fps": 10, "cooldown_seconds": 20},
    "detection": {"z_threshold": 3.5, "min_rise_db": 6.0, "min_rms": 0.15, "warmup_seconds": 10.0,
                  "adapt_seconds": 30.0, "end_hold_seconds": 0.5, "max_event_seconds": 2.0,
                  "accept_as_normal_seconds": 30.0, "novelty": 0.42, "learn_rate": 0.06},
    "retention_days": 180,
}


def deep_merge(base, override):
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            deep_merge(base[k], v)
        else:
            base[k]=v
    return base


def load_config(path=os.path.join(HERE, "config.yaml")):
    cfg=json.loads(json.dumps(DEFAULTS))
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            user=yaml.safe_load(f) or {}
        deep_merge(cfg, user)
        if "z_threshold" in user:
            cfg["detection"]["z_threshold"]=float(user["z_threshold"])
    return cfg





class Frame:
    __slots__=("ms", "rms", "zcr", "peak", "bands", "domhz", "tonal", "level", "band_db")

    def __init__(self, ms, rms, zcr, peak, bands, domhz, tonal):
        self.ms, self.rms, self.zcr, self.peak=ms, rms, zcr, peak
        self.bands, self.domhz, self.tonal=bands, domhz, tonal
        self.level=20*math.log10(rms+0.3)
        self.band_db=[20*math.log10(b+0.05) for b in bands]


_warned_old_firmware=False
_old_fw_count=0


def parse_line(line):
    global _warned_old_firmware, _old_fw_count
    if not line or line.startswith("#"):
        return None
    parts=line.split(",")
    if len(parts)==4+NB:
        _old_fw_count+=1
        if _old_fw_count>=10 and not _warned_old_firmware:
            _warned_old_firmware=True
            print("[sentinel] the arduino is running the OLD firmware (12 columns). "
                  "re-upload arduino_sketch.ino - nothing will be detected until you do.")
        return None
    if len(parts)!=6+NB:
        return None
    _old_fw_count=0
    try:
        vals=[float(p) for p in parts]
    except ValueError:
        return None
    return Frame(vals[0], vals[1], vals[2], vals[3], vals[4:4+NB], vals[4+NB], vals[5+NB])





class Baseline:
    SD_FLOOR_LEVEL=1.5
    SD_FLOOR_BAND=2.5

    def __init__(self, warmup_s=10.0, adapt_s=30.0):
        self.warmup_s=max(3.0, float(warmup_s))
        self.warmup_n=max(30, int(self.warmup_s/WINDOW_S))
        self.alpha=WINDOW_S/max(adapt_s, 1.0)
        self.reset()

    def reset(self):
        self.n=0
        self.warmup_samples=[]
        self.mean=[0.0]*(1+NB)
        self.m2=[1.0]*(1+NB)
        self.initial_level=None
        self.avg_tonal=0.15

    @property
    def ready(self):
        return self.n>=self.warmup_n

    @property
    def progress(self):
        return min(1.0, self.n/float(self.warmup_n))

    def level_sd(self):
        return self._sd(0)

    def _sd(self, i):
        floor=self.SD_FLOOR_LEVEL if i==0 else self.SD_FLOOR_BAND
        return max(math.sqrt(max(self.m2[i], 0.0)), floor)

    def _finalize_warmup(self):
        samples=sorted(self.warmup_samples, key=lambda x: x[0])
        keep_n=max(10, int(len(samples)*0.90))
        kept=samples[:keep_n]
        cols=len(kept[0])
        for c in range(cols):
            vals=[row[c] for row in kept]
            mu=sum(vals)/len(vals)
            var=sum((v-mu)**2 for v in vals)/max(1, len(vals)-1)
            self.mean[c]=mu
            self.m2[c]=var
        self.initial_level=self.mean[0]
        self.warmup_samples=[]
        print(f"[sentinel] Calibration complete! Normal baseline = {self.mean[0]:.1f} dB")

    def update(self, f):
        x=[f.level]+f.band_db
        self.n+=1
        if self.n<=self.warmup_n:
            self.warmup_samples.append(x)
            for i, v in enumerate(x):
                self.mean[i]+=(v-self.mean[i])/self.n
            self.avg_tonal+=(f.tonal-self.avg_tonal)/self.n
            if self.n==self.warmup_n:
                self._finalize_warmup()
        else:
            a=self.alpha
            for i, v in enumerate(x):
                d=v-self.mean[i]
                self.mean[i]+=a*d
                self.m2[i]=(1-a)*(self.m2[i]+a*d*d)
            self.avg_tonal=(1-a)*self.avg_tonal+a*f.tonal

    def score(self, f):
        if not self.ready:
            return 0.0, 0.0, 0.0, 0.0
        lz=(f.level-self.mean[0])/self._sd(0)
        lrise=f.level-self.mean[0]
        bz, brise=0.0, 0.0
        for i, v in enumerate(f.band_db, start=1):
            z=(v-self.mean[i])/self._sd(i)
            if z>bz:
                bz, brise=z, v-self.mean[i]
        return lz, lrise, bz, brise

    def band_power(self):
        return [10**(m/10.0) for m in self.mean[1:]]

    @property
    def level(self):
        return self.mean[0]

    @property
    def drift_db(self):
        if self.initial_level is None:
            return 0.0
        return self.mean[0]-self.initial_level





class Event:
    def __init__(self, t0, wall0):
        self.t0=t0
        self.wall0=wall0
        self.frames=[]
        self.last_active=t0
        self.last_ms=None
        self.max_z=0.0
        self.alerted=False
        self.result=None
        self.clip=None


def event_features(ev, baseline):
    frames=[f for f in ev.frames if f.ms<=ev.last_ms] or ev.frames
    bg=baseline.band_power()
    spec=[0.0]*NB
    weights=[]
    for f in frames:
        ex=[max(b*b-p, 0.0) for b, p in zip(f.bands, bg)]
        for k in range(NB):
            spec[k]+=ex[k]
        weights.append(sum(ex))
    tot=sum(spec)
    if tot<=0:
        spec, tot=[1.0]*NB, float(NB)

    centroid=sum(s*hz for s, hz in zip(spec, BAND_HZ))/tot
    shares=[s/tot for s in spec if s>0]
    spread=sum(p*math.log(p) for p in shares)/math.log(NB)

    wsum=sum(weights) or 1.0
    tonal=sum(w*f.tonal for w, f in zip(weights, frames))/wsum
    mean_zcr=sum(w*f.zcr for w, f in zip(weights, frames))/wsum

    duration=(ev.last_ms-frames[0].ms)/1000.0+WINDOW_S
    levels=[f.level for f in frames]
    top=max(levels)
    sustain=sum(1 for v in levels if v>=top-10.0)/len(levels)

    i=next(k for k, v in enumerate(levels) if v>=top-1.0)
    after=levels[i+1:i+4]
    after+=[baseline.level]*(3-len(after))
    decay_db=levels[i]-sum(after)/3.0

    max_peak=max(f.peak for f in frames)
    max_rms=max(max(f.rms for f in frames), 0.5)
    crest=max_peak/max_rms
    low_ratio=sum(spec[0:3])/tot
    high_ratio=sum(spec[5:8])/tot

    raw={
        "centroid_hz": round(centroid),
        "spread": round(spread, 2),
        "tonal": round(tonal, 2),
        "duration_s": round(duration, 2),
        "sustain": round(sustain, 2),
        "decay_db": round(decay_db, 1),
        "zcr": round(mean_zcr, 3),
        "crest": round(crest, 2),
        "low_ratio": round(low_ratio, 2),
        "high_ratio": round(high_ratio, 2),
        "rise_db": round(top-baseline.level, 1),
    }

    vec=[
        min(max(math.log2(max(centroid, 62.5)/62.5)/6.0, 0.0), 1.0),
        min(max(spread, 0.0), 1.0),
        min(max(tonal, 0.0), 1.0),
        min(max(math.log10(max(duration, 0.016)/0.016)/math.log10(4.0/0.016), 0.0), 1.0),
        min(max(sustain, 0.0), 1.0),
        min(max(decay_db/20.0, 0.0), 1.0),
        min(max(mean_zcr/0.55, 0.0), 1.0),
        min(max((crest-1.4)/6.0, 0.0), 1.0),
        min(max(low_ratio, 0.0), 1.0),
        min(max(high_ratio, 0.0), 1.0),
    ]
    return vec, raw


class ShapeClassifier:


    SEEDS={
        "impact":        [0.62, 0.65, 0.35, 0.35, 0.30, 0.55, 0.38, 0.72, 0.35, 0.40],
        "tonal_alarm":   [0.84, 0.16, 0.94, 0.85, 0.80, 0.02, 0.55, 0.20, 0.05, 0.85],
        "voice":         [0.58, 0.76, 0.52, 0.74, 0.72, 0.06, 0.30, 0.38, 0.28, 0.32],
        "abnormal_sound": [0.57, 0.57, 0.61, 0.87, 0.90, 0.04, 0.44, 0.30, 0.43, 0.45],
    }
    WEIGHTS=[1.2, 0.8, 1.5, 2.0, 1.8, 2.0, 1.4, 1.5, 1.3, 1.3]
    def __init__(self, learn_rate=0.06, novelty=0.42, path=None):
        self.learn_rate=learn_rate
        self.novelty=novelty
        self.path=PROTOS_PATH if path is None else path
        self.protos={k: list(v) for k, v in self.SEEDS.items()}
        self.counts={k: 0 for k in self.SEEDS}
        self.next_id=1
        self._load()

    def _load(self):
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                data=json.load(f)
            if data.get("version")==3:
                self.protos={k: [float(x) for x in data["protos"][k]]
                             for k in self.SEEDS if k in data["protos"]}
                if not self.protos:
                    self.protos={k: list(v) for k, v in self.SEEDS.items()}
                self.counts={k: int(data.get("counts", {}).get(k, 0)) for k in self.protos}
                self.next_id=1
        except (OSError, ValueError, KeyError, TypeError):
            print("[classifier] prototypes.json unreadable or older version, upgrading to v3 seeds")

    def _save(self):
        if not self.path:
            return
        tmp=self.path+".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"version": 3, "next_id": self.next_id, "counts": self.counts,
                           "protos": {k: [round(x, 4) for x in v] for k, v in self.protos.items()}}, f, indent=1)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def reset_seeds(self):
        self.protos={k: list(v) for k, v in self.SEEDS.items()}
        self.counts={k: 0 for k in self.SEEDS}
        self.next_id=1
        self._save()

    def distance(self, a, b):
        return math.sqrt(sum(w*(x-y)**2 for w, x, y in zip(self.WEIGHTS, a, b))/sum(self.WEIGHTS))

    def classify(self, v):
        ranked=sorted((self.distance(v, p), k) for k, p in self.protos.items())
        best_d, best=ranked[0]
        second_d=ranked[1][0] if len(ranked)>1 else best_d+0.5
        margin=(second_d-best_d)/max(second_d, 1e-4)
        closeness=max(0.0, 1.0-(best_d/(1.5*self.novelty)))
        conf=max(0.15, min(0.99, 0.55*closeness+0.45*min(1.0, margin*1.8)))
        if best_d<0.65*self.novelty:
            p=self.protos[best]
            self.protos[best]=[x+self.learn_rate*(y-x) for x, y in zip(p, v)]
        self.counts[best]=self.counts.get(best, 0)+1
        self._save()
        return best, round(conf, 2)

    def teach(self, vec, correct_label):
        if not vec or len(vec)!=len(self.WEIGHTS):
            return
        if correct_label not in self.SEEDS:
            return
        p=self.protos[correct_label]
        lr=min(0.35, self.learn_rate*3.5)
        self.protos[correct_label]=[x+lr*(y-x) for x, y in zip(p, vec)]
        self.counts[correct_label]=self.counts.get(correct_label, 0)+1
        self._save()
        print(f"[classifier] Learned supervised correction -> {correct_label}")


class Detector:
    def __init__(self, det_cfg, classifier):
        self.cfg=det_cfg
        self.base_z_on=float(det_cfg.get("z_threshold", 2.5))
        self.base_min_rise=float(det_cfg.get("min_rise_db", 6.0))
        self.min_rms=float(det_cfg.get("min_rms", 0.8))
        self.sensitivity_mult=1.0
        self.baseline=Baseline(float(det_cfg["warmup_seconds"]), float(det_cfg["adapt_seconds"]))
        self.classifier=classifier
        self.event=None
        self.last_z=0.0
        self.last_rise=0.0
        self.last_vec=None
        # short history helps catch steady sounds
        self.recent=deque(maxlen=24)
        self.hits=deque(maxlen=3)

    @property
    def z_on(self):
        return self.base_z_on*self.sensitivity_mult

    @property
    def min_rise(self):
        return self.base_min_rise*self.sensitivity_mult

    @property
    def effective_rise_db(self):

        return max(self.min_rise, self.z_on*self.baseline.level_sd())

    def _onset_hit(self, f, sc):
        """returns (hit, strong). hit = this frame looks abnormal; strong = so abnormal it needs no confirmation."""
        if not self.baseline.ready or f.rms<self.min_rms:
            return False, False
        lz, lrise, bz, brise=sc
        req_rise, req_z=self.effective_rise_db, self.z_on

        strong=lrise>=req_rise*1.6 or (bz>=req_z*2.0 and brise>=req_rise*1.6)

        hit=lrise>=req_rise

        hit=hit or (bz>=req_z and brise>=max(self.min_rise, req_rise*0.8))

        if not hit and len(self.recent)==self.recent.maxlen:
            hit=sum(self.recent)/len(self.recent)-self.baseline.level>=req_rise*0.6
        return hit, strong

    def _still_loud(self, f, sc):
        """is an ongoing sound still going? uses a short average so random noise can't keep it alive."""
        lz, lrise, bz, brise=sc
        req_rise=self.effective_rise_db
        if f.rms<self.min_rms*0.6:
            return False
        if lrise>=req_rise:
            return True
        tail=list(self.recent)[-6:]
        return sum(tail)/len(tail)-self.baseline.level>=max(req_rise*0.5, 2.0)

    def feed(self, f, wall=None):
        out=[]
        sc=self.baseline.score(f)
        self.recent.append(f.level)
        self.last_z, self.last_rise=max(sc[0], sc[2]), max(sc[1], sc[3])
        t=f.ms/1000.0

        if self.event is None:
            hit, strong=self._onset_hit(f, sc)
            self.hits.append(hit)
            if strong or sum(self.hits)>=2:
                self.hits.clear()
                ev=Event(t, wall or datetime.now())
                ev.frames.append(f)
                ev.last_active=t
                ev.last_ms=f.ms
                ev.max_z=max(self.last_z, 1.0)
                self.event=ev
                out.append(("onset", ev))
            else:
                self.baseline.update(f)
            return out

        ev=self.event
        ev.frames.append(f)
        if self._still_loud(f, sc):
            ev.last_active=t
            ev.last_ms=f.ms
            ev.max_z=max(ev.max_z, self.last_z)

        over=t-ev.last_active>=float(self.cfg["end_hold_seconds"])
        if not ev.alerted and (over or t-ev.t0>=float(self.cfg["max_event_seconds"])):
            self._classify(ev)
            out.append(("alert", ev))
        if over:
            self.event=None
            out.append(("end", ev))
        elif t-ev.t0>=float(self.cfg["accept_as_normal_seconds"]):
            self.baseline.update(f)
        if len(ev.frames)>400:
            del ev.frames[100:200]
        return out

    def _classify(self, ev):
        vec, raw=event_features(ev, self.baseline)
        self.last_vec=vec
        label, conf=self.classifier.classify(vec)
        strength_conf=min(0.99, ev.max_z/max(2.0*self.z_on, 1.0))
        conf=max(conf, round(strength_conf, 2))
        ev.result={"type": label, "confidence": conf, "score": ev.max_z, "features": raw,
                     "vec": vec, "rms": max(f.rms for f in ev.frames), "peak": max(f.peak for f in ev.frames)}
        ev.alerted=True





def find_ffmpeg():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return shutil.which("ffmpeg")


class PhoneCamera:
    def __init__(self, cam_cfg, videos_dir=VIDEOS_DIR, on_saved=None):
        self.cfg=cam_cfg
        self.enabled=bool(cam_cfg.get("enabled", False)) and bool(cam_cfg.get("phone_url"))
        self.url=str(cam_cfg.get("phone_url", "")).rstrip("/")+str(cam_cfg.get("stream_path", "/video"))
        user, pw=cam_cfg.get("username") or "", cam_cfg.get("password") or ""
        self.auth=(user, pw) if user else None
        self.pre_s=float(cam_cfg.get("pre_seconds", 3))
        self.clip_s=float(cam_cfg.get("clip_seconds", 15))
        self.fps=max(1.0, float(cam_cfg.get("fps", 10)))
        self.cooldown=float(cam_cfg.get("cooldown_seconds", 20))
        self.videos_dir=videos_dir
        os.makedirs(self.videos_dir, exist_ok=True)
        self.on_saved=on_saved
        self.ffmpeg=find_ffmpeg()
        self.lock=threading.Lock()
        self.ring=deque()
        self.job=None
        self.last_clip_end=0
        self.connected=False
        self.error=""
        self.frames_seen=0
        self._stop=threading.Event()
        if self.enabled:
            if not self.ffmpeg:
                self.error="ffmpeg not found - run: pip install imageio-ffmpeg"
                print("[camera]", self.error)
            threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        backoff=1.0
        while not self._stop.is_set():
            before=self.frames_seen
            try:
                with requests.get(self.url, stream=True, timeout=(5, 10), auth=self.auth) as r:
                    if r.status_code==401:
                        raise requests.HTTPError("401 - set camera.username / password in config.yaml")
                    r.raise_for_status()
                    self._read_mjpeg(r)
                raise requests.ConnectionError("stream ended")
            except requests.RequestException as e:
                detail=str(e) if isinstance(e, requests.HTTPError) else type(e).__name__
                msg=f"can't reach {self.url} ({detail})"
                if self.connected or msg!=self.error:
                    print("[camera]", msg)
                self.connected, self.error=False, msg
                if self.frames_seen-before>50:
                    backoff=1.0
                self._stop.wait(backoff)
                backoff=min(backoff*2, 15.0)

    def _read_mjpeg(self, r):
        buf=b""
        last_kept=0.0
        for chunk in r.iter_content(chunk_size=16384):
            if self._stop.is_set():
                return
            buf+=chunk
            while True:
                start=buf.find(b"\xff\xd8")
                if start<0:
                    buf=buf[-1:]
                    break
                end=buf.find(b"\xff\xd9", start+2)
                if end<0:
                    buf=buf[start:]
                    if len(buf)>8_000_000:
                        buf=b""
                    break
                jpg, buf=buf[start:end+2], buf[end+2:]
                now=time.monotonic()
                if now-last_kept<1.0/self.fps-0.005:
                    continue
                last_kept=now
                self.frames_seen+=1
                if not self.connected:
                    print(f"[camera] receiving video from {self.url}")
                    self.connected, self.error=True, ""
                self._add_frame(now, jpg)

    def _add_frame(self, now, jpg):
        done=None
        with self.lock:
            self.ring.append((now, jpg))
            while self.ring and now-self.ring[0][0]>self.pre_s:
                self.ring.popleft()
            job=self.job
            if job is not None:
                job["frames"].append((now, jpg))
                if now>=job["end"]:
                    done, self.job=job, None
                    self.last_clip_end=now
        if done:
            threading.Thread(target=self._finish, args=(done,), daemon=True).start()

    @property
    def busy(self):
        return self.job is not None

    def start_clip(self):
        if not self.enabled:
            return None
        now=time.monotonic()
        with self.lock:
            if self.job is not None:
                return None
            job={"start": now-self.pre_s, "end": now+self.clip_s,
                   "frames": [fr for fr in self.ring if fr[0]>=now-self.pre_s],
                   "name": None, "caption": "", "named": threading.Event()}
            self.job=job
        threading.Thread(target=self._watchdog, args=(job,), daemon=True).start()
        return job

    def _watchdog(self, job):
        time.sleep(self.clip_s+5)
        with self.lock:
            if self.job is not job:
                return
            self.job=None
            self.last_clip_end=time.monotonic()
        self._finish(job)

    def _finish(self, job):
        job["named"].wait(timeout=30)
        name=job["name"] or f"{datetime.now():%Y%m%d_%H%M%S}_sound.mp4"
        out=os.path.join(self.videos_dir, name)
        frames=job["frames"]
        if len(frames)<2:
            self._fail(out, f"no video from the phone ({self.error or 'stream gave no frames'})")
            return
        if not self.ffmpeg:
            self._fail(out, "ffmpeg not found - run: pip install imageio-ffmpeg")
            return
        try:
            self._encode(frames, out)
        except (OSError, subprocess.SubprocessError, RuntimeError) as e:
            self._fail(out, f"encoding failed: {e}")
            return
        secs=frames[-1][0]-frames[0][0]
        print(f"[camera] saved {name} ({secs:.0f} s, {len(frames)} frames)")
        if self.on_saved:
            self.on_saved(out, job.get("caption", ""))

    def _encode(self, frames, out):
        t0, t1=frames[0][0], frames[-1][0]
        n_out=max(2, int((t1-t0)*self.fps)+1)
        tmp=out+".part"
        cmd=[self.ffmpeg, "-y", "-loglevel", "error", "-f", "image2pipe", "-c:v", "mjpeg",
               "-framerate", f"{self.fps:g}", "-i", "-",
               "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-c:v", "libx264", "-preset", "veryfast",
               "-crf", "26", "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-f", "mp4", tmp]
        p=subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        j=0
        try:
            for i in range(n_out):
                t=t0+i/self.fps
                while j+1<len(frames) and frames[j+1][0]<=t:
                    j+=1
                p.stdin.write(frames[j][1])
            p.stdin.close()
        except BrokenPipeError:
            pass
        err=p.stderr.read().decode(errors="ignore").strip()
        if p.wait()!=0 or not os.path.exists(tmp):
            raise RuntimeError(err.splitlines()[-1] if err else "ffmpeg failed")
        os.replace(tmp, out)

    def _fail(self, out, reason):
        print(f"[camera] clip {os.path.basename(out)} failed: {reason}")
        try:
            with open(out+".failed", "w", encoding="utf-8") as f:
                f.write(reason)
        except OSError:
            pass

    def status(self):
        if not self.enabled:
            return {"state": "off"}
        if self.busy:
            state="recording"
        else:
            state="ready" if self.connected else "unreachable"
        return {"state": state, "error": self.error, "url": self.url}





class Telegram:
    def __init__(self, tg_cfg, on_sent=None):
        self.tok=str(tg_cfg.get("token", "")).strip()
        self.chat=str(tg_cfg.get("chat_id", "")).strip()
        self.send_clips=bool(tg_cfg.get("send_clips", True))
        self.on_sent=on_sent
        self.ok=bool(self.tok and self.chat) and "PASTE" not in (self.tok+self.chat)
        if not self.ok:
            print("[telegram] token/chat_id not set in config.yaml - alerts will only be logged locally")
        else:
            print(f"[telegram] configured and ready (chat_id={self.chat})")

    def _post(self, method,**kw):
        if not self.ok:
            return False
        try:
            r=requests.post(f"https://api.telegram.org/bot{self.tok}/{method}", timeout=60,**kw)
            if r.status_code!=200:
                print(f"[telegram] {method} failed: {r.status_code} {r.text[:120]}")
                return False
            else:
                print(f"[telegram] sent {method} successfully")
                if self.on_sent:
                    self.on_sent()
                return True
        except requests.RequestException as e:
            print(f"[telegram] {method} failed: {type(e).__name__}: {e}")
            return False

    def message(self, text):
        threading.Thread(target=self._post, args=("sendMessage",),
                         kwargs={"data": {"chat_id": self.chat, "text": text}}, daemon=True).start()

    def video(self, path, caption):
        if not self.send_clips:
            return
        try:
            size=os.path.getsize(path)
        except OSError as e:
            print(f"[telegram] video unavailable: {e}")
            return
        if size>49*1024*1024:
            print(f"[telegram] video skipped because it is too large ({size/1024/1024:.1f} MB)")
            return
        try:
            with open(path, "rb") as f:
                self._post("sendVideo", data={"chat_id": self.chat, "caption": caption,
                                              "supports_streaming": "true"},
                           files={"video": (os.path.basename(path), f, "video/mp4")})
        except OSError as e:
            print(f"[telegram] could not open video: {e}")


def log_event(ts, mode, label, res, video=""):
    try:
        if os.path.exists(EVENTS_PATH):
            with open(EVENTS_PATH, newline="", encoding="utf-8") as f:
                header=next(csv.reader(f), [])
            if header!=EVENT_FIELDS:
                os.replace(EVENTS_PATH, os.path.join(HERE, f"events_old_{datetime.now():%Y%m%d_%H%M%S}.csv"))
        new=not os.path.exists(EVENTS_PATH)
        with open(EVENTS_PATH, "a", newline="", encoding="utf-8") as f:
            w=csv.writer(f)
            if new:
                w.writerow(EVENT_FIELDS)
            w.writerow([ts.strftime("%Y-%m-%d %H:%M:%S"), mode, label, res["confidence"],
                        round(res["score"], 1), res["features"]["rise_db"], res["features"]["duration_s"],
                        round(res["rms"], 1), round(res["peak"], 1), video])
    except OSError as e:
        print(f"[sentinel] could not write to events.csv: {e}")


def purge_old_data(retention_days):
    cutoff_ts=time.time()-retention_days*86400
    cutoff_dt=datetime.fromtimestamp(cutoff_ts)

    removed_clips=0
    if os.path.exists(VIDEOS_DIR):
        for fn in os.listdir(VIDEOS_DIR):
            path=os.path.join(VIDEOS_DIR, fn)
            try:
                if os.path.isfile(path) and os.path.getmtime(path)<cutoff_ts:
                    os.remove(path)
                    removed_clips+=1
            except OSError:
                pass

    removed_rows=0
    if os.path.exists(EVENTS_PATH):
        try:
            with open(EVENTS_PATH, newline="", encoding="utf-8") as f:
                reader=csv.DictReader(f)
                fieldnames=reader.fieldnames
                kept=[]
                for row in reader:
                    try:
                        ts=datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M:%S")
                    except (KeyError, ValueError, TypeError):
                        kept.append(row)
                        continue
                    if ts>=cutoff_dt:
                        kept.append(row)
                    else:
                        removed_rows+=1
            if fieldnames and removed_rows:
                with open(EVENTS_PATH, "w", newline="", encoding="utf-8") as f:
                    w=csv.DictWriter(f, fieldnames=fieldnames)
                    w.writeheader()
                    w.writerows(kept)
        except OSError:
            pass

    if removed_clips or removed_rows:
        print(f"[cleanup] removed {removed_clips} clip(s), {removed_rows} old event row(s)")


def write_status(data):
    tmp=STATUS_PATH+".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, STATUS_PATH)
    except (PermissionError, OSError):
        pass


def open_serial(port, baud):
    try:
        ser=serial.Serial(port, baud, timeout=1)
    except serial.SerialException as e:
        from serial.tools import list_ports
        ports=[p.device for p in list_ports.comports()]
        raise SystemExit(f"[sentinel] can't open {port}: {e}\n"
                         f"           ports on this computer: {', '.join(ports) or 'none'}\n"
                         f"           (close the arduino serial monitor, and check serial.port in config.yaml)")
    time.sleep(2)
    ser.reset_input_buffer()
    try:
        ser.readline()
    except Exception:
        pass
    return ser





class Sentinel:
    def __init__(self, cfg, camera=None, telegram=None, light=None):
        self.cfg=cfg
        det=cfg["detection"]
        self.classifier=ShapeClassifier(float(det["learn_rate"]), float(det["novelty"]))
        self.detector=Detector(det, self.classifier)
        self.telegram=telegram if telegram is not None else Telegram(cfg["telegram"], on_sent=light)
        self.camera=camera if camera is not None else PhoneCamera(
            cfg["camera"], on_saved=lambda path, cap:
                threading.Thread(target=self.telegram.video, args=(path, cap), daemon=True).start())
        self.tg_cooldown=float(cfg["telegram"]["cooldown_seconds"])
        self.last_telegram={}
        self.hist=deque(maxlen=200)
        self.hist_bucket=None
        self.last_event=None
        self.device={}
        self._last_hb=0.0

        self.user_sens=REFERENCE_SENSITIVITY
        self.mode=cfg.get("mode", {}).get("current", "security").lower()
        self.apply_mode_settings()
        self.machine_health=100.0
        self.machine_state="Normal"

    def apply_mode_settings(self):
        m_cfg=self.cfg.get("mode", {}).get(self.mode, {})
        mult=float(m_cfg.get("sensitivity_multiplier", 1.0))
        level=self.user_sens/REFERENCE_SENSITIVITY
        self.detector.sensitivity_mult=mult*(1.0+1.5*(1.0-level))

    def check_commands(self):
        if not os.path.exists(COMMAND_PATH):
            return
        try:
            with open(COMMAND_PATH, encoding="utf-8") as f:
                cmd=json.load(f)
            os.remove(COMMAND_PATH)
        except Exception:
            return

        action=cmd.get("action")
        if action=="set_mode":
            new_mode=str(cmd.get("mode", "security")).lower()
            if new_mode in ("security", "machine", "general"):
                self.mode=new_mode
                self.apply_mode_settings()
                print(f"[sentinel] Switched operating mode to: {self.mode.upper()}")
        elif action=="recalibrate":
            print(f"[sentinel] Recalibrating baseline for {self.detector.baseline.warmup_s:.0f}s...")
            self.detector.baseline.reset()
            print("[sentinel] Recalibrating room acoustics. Please remain quiet.")
        elif action=="teach":
            label=str(cmd.get("label", "")).strip()
            if label and self.detector.last_vec:
                self.classifier.teach(self.detector.last_vec, label)
                if self.last_event:
                    self.last_event["type"]=label
        elif action=="set_sensitivity":
            try:
                self.user_sens=REFERENCE_SENSITIVITY*min(1.0, max(0.05, float(cmd.get("value", 1.0))))
            except (TypeError, ValueError):
                self.user_sens=REFERENCE_SENSITIVITY
            self.apply_mode_settings()
            print(f"[sentinel] Sensitivity {self.user_sens/REFERENCE_SENSITIVITY*100:.0f}% "
                  f"(alert line now +{self.detector.effective_rise_db:.1f} dB)")
        elif action=="reset_prototypes":
            self.classifier.reset_seeds()
            print("[sentinel] Reset sound prototypes to factory seeds.")

    def device_note(self, line):
        for part in line.lstrip("# ").split():
            if "=" in part:
                k, v=part.split("=", 1)
                self.device[k]=v.rstrip("%")
        ov=int(self.device.get("overruns", 0) or 0)
        if ov>int(self.device.get("_ov_seen", 0)):
            print(f"[sentinel] arduino dropped audio windows (overruns={ov}) - is it running the current sketch?")
        self.device["_ov_seen"]=ov

    def _update_machine_diagnostics(self, f):
        base=self.detector.baseline
        if not base.ready:
            self.machine_health=100.0
            self.machine_state="CALIBRATING"
            return

        drift=max(0.0, base.drift_db)
        m_cfg=self.cfg.get("mode", {}).get("machine", {})
        warn_db=float(m_cfg.get("drift_warn_db", 5.0))
        warn_tonal=float(m_cfg.get("harmonic_warn_tonal", 0.72))

        high_band_energy=sum(f.band_db[5:8])/3.0-sum(base.mean[6:9])/3.0
        wear_penalty=max(0.0, high_band_energy-3.0)*3.2
        drift_penalty=(drift/max(1.0, warn_db))*25.0
        tonal_penalty=max(0.0, base.avg_tonal-warn_tonal)*60.0

        raw_health=max(0.0, min(100.0, 100.0-drift_penalty-wear_penalty-tonal_penalty))
        self.machine_health=round(0.95*self.machine_health+0.05*raw_health, 1)

        if self.machine_health>=80:
            self.machine_state="Normal"
        elif self.machine_health>=55:
            self.machine_state="Elevated"
        else:
            self.machine_state="Maintenance warning"

    def handle(self, f, wall=None):
        self._update_machine_diagnostics(f)
        for kind, ev in self.detector.feed(f, wall):
            if kind=="onset":
                print(f"[onset] Sound crossed threshold (+{self.detector.last_rise:.1f} dB, rms={f.rms:.1f}) - analysing...")
                ev.clip=self.camera.start_clip() if self.camera else None
            elif kind=="alert":
                self._alert(ev)
        self._history(f)

    def _alert(self, ev):
        res=ev.result
        raw_label, ts=res["type"], ev.wall0
        label=self._display_label(raw_label)
        ft=res["features"]
        notify=res["confidence"]>0.5

        video=""
        if ev.clip is not None:
            video=f"{ts:%Y%m%d_%H%M%S}_{self.mode}_{label}.mp4"
            ev.clip["name"]=video
            ev.clip["caption"]=f"[{self.mode.upper()}] {label} - {ts:%d %b %H:%M:%S}"
            ev.clip["named"].set()

        print(f"[alert:{self.mode}] {ts:%H:%M:%S} {label} (conf {res['confidence']:.2f}) "
              f"score={res['score']:.1f} +{ft['rise_db']:.0f}dB {ft['duration_s']:.2f}s "
              f"{ft['centroid_hz']}Hz zcr={ft['zcr']:.2f} crest={ft['crest']:.1f}"
+(f" clip={video}" if video else ""))

        log_event(ts, self.mode, label, res, video=video)
        self.last_event={
            "type": label,
            "classifier_type": raw_label,
            "mode": self.mode,
            "time": ts.strftime("%H:%M:%S"),
            "confidence": res["confidence"],
            "rise_db": ft["rise_db"],
            "features": ft,
        }

        now=time.time()
        if notify and now-self.last_telegram.get(label, 0)>=self.tg_cooldown:
            self.last_telegram[label]=now
            text=(f"[{self.mode.upper()} MODE] EchoSnap Alert: {label}\n"
                    f"Time: {ts:%d %b %H:%M:%S}\n"
                    f"Confidence: {res['confidence']*100:.0f}% | +{ft['rise_db']:.0f} dB above normal\n"
                    f"Duration: {ft['duration_s']:.2f} s | Dominant pitch: {ft['centroid_hz']} Hz")
            if video:
                text+="\nRecording video clip, uploading shortly..."
            self.telegram.message(text)

    def _display_label(self, label):
        if self.mode=="security":
            if label=="voice":
                return "voice"
            if label=="tonal_alarm":
                return "alarm"
            if label=="impact":
                return "impact"
            return "others"
        if self.mode=="machine":
            return "alarm" if label=="tonal_alarm" else "abnormal sound"
        return {
            "voice": "voice",
            "tonal_alarm": "alarm",
            "impact": "impact",
            "abnormal_sound": "abnormal sound",
        }.get(label, "others")

    def _history(self, f):
        d=self.detector
        bk=int(f.ms//200)
        thr=round(d.baseline.level+d.effective_rise_db, 1)
        in_ev=1 if d.event else 0
        if self.hist_bucket is None or self.hist_bucket[0]!=bk:
            if self.hist_bucket is not None:
                self.hist.append(self.hist_bucket[1])
            self.hist_bucket=[bk, [round(time.time(), 2), round(f.level, 1),
                                     round(d.baseline.level, 1), round(f.rms, 1), thr, in_ev]]
        else:
            row=self.hist_bucket[1]
            if f.level>row[1]:
                row[1], row[3]=round(f.level, 1), round(f.rms, 1)
            row[4]=thr
            row[5]=max(row[5], in_ev)
        self._heartbeat(f)

    def _heartbeat(self, f):
        now=time.time()
        if now-self._last_hb>=10.0:
            self._last_hb=now
            d=self.detector
            if d.baseline.ready:
                print(f"[sentinel] ok  level={f.level:+.1f} dB  normal={d.baseline.level:+.1f} dB  "
                      f"alert-at={d.baseline.level+d.effective_rise_db:+.1f} dB  rms={f.rms:.2f}")

    def status(self):
        d=self.detector
        state="learning" if not d.baseline.ready else ("event" if d.event else "listening")
        hist=list(self.hist)+([self.hist_bucket[1]] if self.hist_bucket else [])
        return {
            "updated": time.time(),
            "mode": self.mode,
            "state": state,
            "learning_progress": round(d.baseline.progress, 2),
            "warmup_seconds": d.baseline.warmup_s,
            "type": self._display_label(d.event.result["type"]) if d.event and d.event.result
                    else ("sound" if d.event else "normal"),
            "score": round(d.last_z, 2),
            "level_db": hist[-1][1] if hist else None,
            "baseline_db": round(d.baseline.level, 1),
            "threshold_db": round(d.baseline.level+d.effective_rise_db, 1),
            "machine": {
                "health": self.machine_health,
                "state": self.machine_state,
                "drift_db": round(d.baseline.drift_db, 1),
                "avg_tonal": round(d.baseline.avg_tonal, 2),
            },
            "history": hist,
            "sensitivity": self.user_sens,
            "last_event": self.last_event,
            "camera": self.camera.status() if self.camera else {"state": "off"},
            "recording": bool(self.camera and self.camera.busy),
            "device": {k: v for k, v in self.device.items() if not k.startswith("_")},
            "types": self.classifier.counts,
            "known_labels": list(self.classifier.protos.keys()),
        }


def main():
    cfg=load_config()
    os.makedirs(VIDEOS_DIR, exist_ok=True)
    ser=open_serial(cfg["serial"]["port"], cfg["serial"]["baud"])
    warmup_s=float(cfg["detection"]["warmup_seconds"])
    print(f"[sentinel] listening on {cfg['serial']['port']} in {cfg['mode']['current'].upper()} mode")
    print(f"[sentinel] extended acoustic calibration: stay quiet/normal for {warmup_s:.0f} s...")
    def flash_notification_led():
        try:
            ser.write(b"L\n")
        except serial.SerialException:
            pass

    sentinel=Sentinel(cfg, light=flash_notification_led)

    retention_days=int(cfg.get("retention_days", 180))
    purge_old_data(retention_days)
    last_purge=time.time()
    last_status=0.0
    got_rows=False
    started=time.time()

    while True:
        try:
            line=ser.readline().decode("ascii", errors="ignore").strip()
        except serial.SerialException:
            print("[sentinel] serial lost, reconnecting...")
            try:
                ser.close()
            except Exception:
                pass
            while True:
                time.sleep(2)
                try:
                    ser=serial.Serial(cfg["serial"]["port"], cfg["serial"]["baud"], timeout=1)
                    ser.reset_input_buffer()
                    ser.readline()
                    print("[sentinel] serial back")
                    break
                except serial.SerialException:
                    continue
            continue

        if line.startswith("#"):
            sentinel.device_note(line)
        else:
            f=parse_line(line)
            if f is not None:
                got_rows=True
                sentinel.handle(f)
            elif not got_rows and time.time()-started>8:
                got_rows=True
                print("[sentinel] no data rows from the arduino yet - check the sketch is uploaded "
                      "and serial.baud is 250000")

        now=time.time()
        if now-last_status>0.2:
            last_status=now
            sentinel.check_commands()
            write_status(sentinel.status())

        if now-last_purge>86400:
            last_purge=now
            purge_old_data(retention_days)


if __name__=="__main__":
    main()
