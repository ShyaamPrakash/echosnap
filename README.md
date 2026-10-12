# Acoustic Sentinel

A sound-based anomaly detector built around an Arduino Uno and a MAX4466 electret mic.

The Uno samples audio continuously, extracts loudness, zero-crossing rate, peak amplitude, and 8-band frequency energy from each window, and streams it to a laptop over serial. The laptop side watches for anything that breaks from the room's recent "normal," figures out what it sounds like, logs it, texts you on Telegram, and — if a phone's set up for it — tells the phone to record a clip.

There's no training step and no recorded dataset. The detector builds its own idea of "normal" as it listens, and the part that labels sounds grows new categories on its own the first time it hears something it doesn't recognize.

## What's in here

| File | Role |
|---|---|
| `arduino_sketch.ino` | Firmware. Samples the mic, computes features per window, streams them over serial. |
| `sentinel.py` | Reads the serial stream, detects anomalies, classifies the sound, fires Telegram and the phone camera, logs everything. |
| `dashboard.py` | Streamlit dashboard — live status, loudness chart, alert log, clip playback. |
| `config.yaml` | Every setting lives here: serial port, Telegram credentials, camera, detection threshold. |

Four files. That's the whole project.

## How it works

1. **Uno** — the ADC free-runs on A0, an interrupt fills one of two alternating buffers, and once a 128-sample window is ready the main loop computes RMS, zero-crossing rate, peak amplitude, and 8 Goertzel band energies, then prints one CSV row per window at 250000 baud.
2. **Baseline** — `sentinel.py` keeps a running mean and standard deviation of those features. A window that lands several standard deviations outside that baseline counts as an anomaly. The baseline only updates on windows that still look normal, so a long anomaly can't drag it toward itself.
3. **Classifier** — anomalies get compared, by spectral shape, against a small set of prototype sounds. The closest match wins and gets nudged slightly closer to what was actually heard. Anything that doesn't resemble a known prototype becomes a brand new type on the spot.
4. **Response** — the event gets logged to `events.csv`, a Telegram message goes out, and, if the camera isn't already busy or still cooling down, the phone starts recording a short clip.
5. **Dashboard** — `dashboard.py` only reads what `sentinel.py` writes. Close it, reopen it, or run it from a different machine pointed at the same folder, and detection keeps running undisturbed either way.
6. **Cleanup** — once a day (and once at startup), `sentinel.py` deletes clips and event log rows older than `retention_days` (180 by default, six months), so `videos/` and `events.csv` don't grow forever unattended.

Full setup instructions, including wiring and the Telegram bot walkthrough, are in `SETUP.md`.

## Serial format

```
ms,rms,zcr,peak,b1,b2,b3,b4,b5,b6,b7,b8
```

One row per processed window. Anything else the firmware wants to report — like a dropped-sample count — goes out on a line starting with `#`, so the row format never has to change.

## Known limitations

- The classifier has no memory of true sound classes beyond its four seed prototypes — it's reacting to "different from normal" and guessing a label from signal shape, so it's noticeably rougher than a properly trained model. That's the trade-off for needing zero training data.
- Learned prototypes live in memory only. Restarting `sentinel.py` resets them back to the seeds.
- Camera integration assumes a phone running the IP Webcam Android app on the same wifi network. There's no local-webcam fallback in this version.

## Why it's built this way

This started as a fuller system — a trained classifier, a separate recording/training pipeline, a heavier dashboard. It got rebuilt into this smaller form on purpose, for a college project where the firmware is the point and the laptop side just needs to be honest about what it's doing. Four files, each doing one clearly named thing, felt easier to explain in front of an examiner than a scattered folder of helper scripts.
