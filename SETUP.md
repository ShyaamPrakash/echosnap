# Setup

Full walkthrough from empty breadboard to a working dashboard. Fifteen to twenty minutes if nothing fights you.

## 1. Hardware

You need:

- Arduino Uno (or any ATmega328P board with the same pinout)
- MAX4466 electret microphone amplifier module
- 3 jumper wires
- USB cable

Wiring is three wires, no breadboard needed:

| MAX4466 | Arduino Uno |
|---------|-------------|
| VCC     | 5V          |
| GND     | GND         |
| OUT     | A0          |

That's the whole circuit. The only other pin in use is the built-in LED on pin 13 — it lights up while a window is being processed, so a steady flicker means the sketch is alive.

## 2. Flash the firmware

1. Open `arduino_sketch.ino` in the Arduino IDE.
2. **Tools → Board → Arduino Uno**.
3. **Tools → Port →** pick the Uno's port.
4. Click **Upload**.
5. Open **Tools → Serial Monitor** and set the baud rate to **250000** (top-right dropdown).
6. You should see one line, `# acoustic sentinel ready`, then a stream of rows like:

   ```
   10452,38.2,0.061,120,4.1,3.8,2.2,1.9,0.8,0.6,0.3,0.2
   ```

   That's `ms,rms,zcr,peak,b1..b8`. If it looks like garbage characters instead of numbers, the baud rate isn't set to 250000.

7. **Close the Serial Monitor before the next step.** Only one program can hold the serial port at a time, and `sentinel.py` needs it.

## 3. Python environment

From the project folder:

```bash
python -m venv venv
```

Activate it:

```bash
venv\Scripts\activate        # Windows
source venv/bin/activate     # macOS / Linux
```

Install everything both scripts need:

```bash
pip install pyserial requests pyyaml streamlit pandas
```

## 4. Create a Telegram bot

1. Open Telegram, search for **@BotFather**.
2. Send `/newbot` and follow the prompts (pick a name and a username).
3. BotFather replies with a token that looks like `123456789:AAFk3gq...`. Copy it.
4. Send your new bot any message at all — it needs something to reply to before the next step works.
5. In a browser, open:

   ```
   https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
   ```

6. Find `"chat":{"id":123456789, ...}` in the response. That number is your `chat_id`.

## 5. Phone camera (optional)

Skip this section entirely if you don't want video clips — everything else still works without it.

1. Install **IP Webcam** (free, Android) from the Play Store.
2. Connect the phone to the **same wifi network** as the laptop.
3. Open the app, scroll down, tap **Start server**.
4. Note the address shown at the top, e.g. `http://192.168.43.1:8080`.
5. That address is your `camera.phone_url` in the next step.

If you skip this, leave `config.yaml`'s camera section as it is — clip recordings will just fail quietly in the background, and Telegram/logging/dashboard are unaffected.

## 6. Edit config.yaml

```yaml
serial:
  port: COM3            # from the Arduino IDE's Tools > Port menu
  baud: 250000           # leave this alone, must match the sketch

telegram:
  token: "..."            # from step 4
  chat_id: "..."           # from step 4
  cooldown_seconds: 5      # minimum gap between telegram messages, per sound type

camera:
  phone_url: "http://192.168.43.1:8080"   # from step 5
  cooldown_seconds: 20     # minimum gap between clip recordings

z_threshold: 4.0            # how many std-devs from normal counts as an anomaly
retention_days: 180          # clips and event log rows older than this get auto-deleted
```

Lower `z_threshold` to make the detector more sensitive; raise it if quiet background noise keeps triggering alerts.

## 7. Run it

Two terminals, same folder, venv activated in both.

**Terminal 1:**

```bash
python sentinel.py
```

Should print:

```
[sentinel] listening on COM3
```

**Terminal 2:**

```bash
streamlit run dashboard.py
```

Opens a browser tab with the live dashboard.

## 8. Test it

Clap near the mic, or play a tone from your phone. Within a couple of seconds you should see:

- `sentinel.py`'s terminal print `[alert] ... impact score=...` (or whatever type it decided)
- a new row in `events.csv`
- a Telegram message land on your phone
- the dashboard's "Last sound" tile update, and — if a clip was recorded — a new entry under Clips

If nothing fires at all, lower `z_threshold` to something like `3.0` and try again.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Serial Monitor shows garbage instead of numbers | baud rate isn't set to exactly 250000 |
| `sentinel.py` won't open the serial port | Serial Monitor (or another program) is still holding it |
| No Telegram messages arrive | `token`/`chat_id` still contains `PASTE_`, or the bot was never messaged first |
| Dashboard shows OFFLINE | `sentinel.py` isn't running, or it crashed — check its terminal for the error |
| Clips never show up | phone isn't on the same wifi, IP Webcam's server wasn't started, or `phone_url` is wrong |
| Everything reads as anomalous | background hum/fan noise louder than the room's quiet baseline — let it run a minute to settle, or raise `z_threshold` |
| `# overruns=N` keeps climbing in the Serial Monitor | the main loop is falling behind the sampling rate — shouldn't happen on the stock sketch, worth flagging if you've modified `process()` |
