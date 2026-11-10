"""
Alfred v4 - voice assistant with a neon-ring HUD.

Install (PyCharm Terminal):
    pip install anthropic SpeechRecognition sounddevice numpy edge-tts pyttsx3

Needs the ANTHROPIC_API_KEY environment variable (Run > Edit Configurations).
Say "Alfred, ..." or type in the box at the bottom of the window.
"""

import asyncio
import ctypes
import datetime
import math
import os
import queue
import random
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import webbrowser
from collections import deque

import anthropic
import edge_tts
import numpy as np
import sounddevice as sd
import speech_recognition as sr

# ---------------- Settings you can tweak ----------------
MODEL = "claude-sonnet-5-5"
VOICE = "en-GB-RyanNeural"      # try "en-GB-ThomasNeural"
VOICE_RATE = "-8%"
VOICE_PITCH = "-4Hz"
SAMPLE_RATE = 16000
WAKE_WORDS = ("alfred", "al fred", "alford", "alfredo")

SYSTEM_PROMPT = (
    "You are Alfred, a refined, loyal, dry-witted British butler and personal assistant "
    "running on the user's laptop. Address the user politely, occasionally as 'sir'. "
    "Your replies are spoken aloud, so keep them to 1-3 sentences, with no markdown or lists."
)

STATE = {"threshold": 300.0}    # auto-calibrated at startup

# A leftover ANTHROPIC_AUTH_TOKEN on the system makes the SDK send a "bearer token"
# instead of your API key, so remove it and pass the key explicitly.
os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip().strip('"').strip("'")
os.environ.pop("ANTHROPIC_API_KEY", None)
client_apikey = anthropic.Anthropic(api_key=API_KEY)      # sent as x-api-key
client_bearer = anthropic.Anthropic(auth_token=API_KEY)   # sent as Authorization: Bearer
recognizer = sr.Recognizer()
history = []
lock = threading.Lock()
warned_voice = False

BG = "#000000"
RED = "#ff3b3b"


def mix(c1, c2, f):
    """Blend colour c1 towards c2 by fraction f (0..1)."""
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(int(a[i] + (b[i] - a[i]) * f) for i in range(3))


# ---------------- Interface ----------------
class UI:
    ACCENT = {"idle": "#7fe9ff", "listening": "#8ff0ff", "thinking": "#ffc46b", "speaking": "#ffffff"}
    W, H = 820, 500
    CX, CY = 410, 245
    PLACEHOLDER = "Type a command and press Enter"

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("ALFRED")
        self.root.geometry("820x800")
        self.root.minsize(700, 660)
        self.root.configure(bg=BG)

        self.canvas = tk.Canvas(self.root, width=self.W, height=self.H, bg=BG, highlightthickness=0)
        self.canvas.pack()

        self.box = tk.Text(self.root, height=8, bg=BG, fg="#d8d8de", font=("Segoe UI", 11), bd=0,
                           padx=28, pady=6, wrap="word", state="disabled", highlightthickness=0)
        self.box.tag_config("you", foreground="#ff8f8f")
        self.box.tag_config("alfred", foreground="#f3f3f6")
        self.box.tag_config("sys", foreground="#5b5b66")
        self.box.pack(fill="both", expand=True)

        line = tk.Frame(self.root, bg="#b02a2a")          # red underline under the input
        line.pack(fill="x", padx=28, pady=(6, 20))
        self.entry = tk.Entry(line, bg="#0c0c10", fg="#6c6c78", insertbackground=RED,
                              font=("Segoe UI", 12), bd=0, relief="flat")
        self.entry.pack(fill="x", pady=(0, 2), ipady=9)
        self.entry.insert(0, self.PLACEHOLDER)
        self.entry.bind("<FocusIn>", self._clear_placeholder)
        self.entry.bind("<Return>", self.submit)

        rnd = random.Random(7)   # faint orbiting streaks around the rings
        self.streaks = [(rnd.uniform(100, 228), rnd.uniform(0, 360), rnd.uniform(18, 120),
                         rnd.uniform(-16, 16), rnd.uniform(0.2, 0.65)) for _ in range(46)]

        self.q = queue.Queue()
        self.state = "idle"
        self.level = 0.0
        self.smooth = 0.0
        self.t = 0.0
        self.animate()

    def _clear_placeholder(self, _event):
        if self.entry.get() == self.PLACEHOLDER:
            self.entry.delete(0, "end")
            self.entry.config(fg="#ffffff")

    def submit(self, _event):
        text = self.entry.get().strip().lower()
        self.entry.delete(0, "end")
        if text and text != self.PLACEHOLDER.lower():
            threading.Thread(target=handle_typed, args=(text,), daemon=True).start()

    def set_state(self, state):
        self.q.put(("state", state))

    def log(self, text):
        self.q.put(("log", text))

    # wobbly closed curve, like a ring of light
    def blob(self, radius, amp, t, seeds, speed):
        pts = []
        n = 48
        for i in range(n + 1):
            th = 2 * math.pi * i / n
            w = (math.sin(2 * th + seeds[0] + t * 0.9 * speed) * 0.55
                 + math.sin(3 * th + seeds[1] - t * 0.7 * speed) * 0.30
                 + math.sin(5 * th + seeds[2] + t * 1.3 * speed) * 0.15)
            r = radius + amp * w
            pts += [self.CX + r * math.cos(th), self.CY + r * math.sin(th)]
        return pts

    def neon(self, pts, color, core=2.5):
        c = self.canvas
        for width, f in ((18, 0.05), (12, 0.11), (7, 0.24), (4, 0.5)):
            c.create_line(*pts, fill=mix(BG, color, f), width=width, smooth=True, splinesteps=6,
                          capstyle="round", joinstyle="round", tags="dyn")
        c.create_line(*pts, fill=mix(color, "#ffffff", 0.4), width=core, smooth=True, splinesteps=6,
                      capstyle="round", joinstyle="round", tags="dyn")

    def animate(self):
        while not self.q.empty():
            kind, val = self.q.get()
            if kind == "state":
                self.state = val
            else:
                tag = "you" if val.startswith("You:") else "alfred" if val.startswith("Alfred:") else "sys"
                self.box.config(state="normal")
                self.box.insert("end", val + "\n", tag)
                self.box.see("end")
                self.box.config(state="disabled")

        self.t += 0.033
        t = self.t
        self.smooth += (self.level - self.smooth) * 0.25
        mic = min(1.0, self.smooth / 1800)
        energy = {
            "idle": 0.18 + 0.05 * math.sin(t * 1.2),
            "listening": 0.25 + 0.9 * mic,
            "thinking": 0.55,
            "speaking": 0.75 + 0.25 * math.sin(t * 7.3) * math.sin(t * 3.1),
        }[self.state]
        speed = 0.6 + energy
        accent = self.ACCENT[self.state]
        c = self.canvas
        cx, cy = self.CX, self.CY
        c.delete("dyn")

        # dark red glow behind the rings, with a hollow centre
        breathe = 1 + 0.025 * math.sin(t * 1.5) + 0.02 * energy
        for i in range(16):
            r = (250 - i * 10) * breathe
            c.create_oval(cx - r, cy - r, cx + r, cy + r, fill=mix(BG, "#3a0508", 0.1 + i * 0.055),
                          outline="", tags="dyn")
        c.create_oval(cx - 92, cy - 92, cx + 92, cy + 92, fill=BG, outline=mix(BG, RED, 0.3), tags="dyn")

        # faint orbiting streaks
        for radius, start, extent, spd, bright in self.streaks:
            a = start + t * spd * speed * 6
            c.create_arc(cx - radius, cy - radius, cx + radius, cy + radius, start=a, extent=extent,
                         style="arc", outline=mix(BG, RED, bright * (0.45 + 0.55 * min(1.0, energy))),
                         width=1, tags="dyn")

        # the three rings
        self.neon(self.blob(148, 14 + 6 * energy, t, (1.0, 2.1, 0.3), 0.7 * speed), "#0a5cb0", core=2)
        self.neon(self.blob(138, 12 + 12 * energy, t, (0.2, 1.4, 2.6), 0.9 * speed), "#ff4545")
        self.neon(self.blob(158, 16 + 16 * energy, t, (2.3, 0.5, 1.7), 1.15 * speed), accent)

        # text
        c.create_text(cx, 24, text="A   L   F   R   E   D", fill="#b3b3bd", font=("Segoe UI", 11), tags="dyn")
        c.create_text(cx, cy, text="  ".join(self.state.upper()), fill=mix(BG, "#ffffff", 0.55),
                      font=("Segoe UI", 9), tags="dyn")

        # mic level line
        y, bw = self.H - 20, 360
        lvl = min(1.0, self.level / 3000)
        thr = min(1.0, STATE["threshold"] / 3000)
        loud = self.level > STATE["threshold"]
        c.create_line(cx - bw / 2, y, cx + bw / 2, y, fill="#1a1a20", width=2, tags="dyn")
        half = bw / 2 * lvl
        c.create_line(cx - half, y, cx + half, y, fill="#ff4d4d" if loud else "#5a2a2e", width=3, tags="dyn")
        for s in (-1, 1):
            c.create_line(cx + s * bw / 2 * thr, y - 5, cx + s * bw / 2 * thr, y + 5, fill="#8a3a3a", tags="dyn")
        c.create_text(cx, y + 14, text="MIC", fill="#44444e", font=("Segoe UI", 7), tags="dyn")

        self.root.after(33, self.animate)


ui = UI()


# ---------------- Voice out ----------------
def play_mp3(path):
    mci = ctypes.windll.winmm.mciSendStringW
    mci("close alfred", None, 0, 0)
    mci(f'open "{path}" type mpegvideo alias alfred', None, 0, 0)
    mci("play alfred wait", None, 0, 0)
    mci("close alfred", None, 0, 0)


async def _synthesize(text, path):
    await edge_tts.Communicate(text, VOICE, rate=VOICE_RATE, pitch=VOICE_PITCH).save(path)


def speak(text):
    global warned_voice
    ui.log(f"Alfred: {text}")
    ui.set_state("speaking")
    path = os.path.join(tempfile.gettempdir(), "alfred_tts.mp3")
    try:
        asyncio.run(_synthesize(text, path))
        play_mp3(path)
    except Exception:
        if not warned_voice:
            ui.log("[online voice unavailable, using backup voice]")
            warned_voice = True
        try:
            import pyttsx3
            engine = pyttsx3.init()
            engine.setProperty("rate", 165)
            engine.say(text)
            engine.runAndWait()
        except Exception as e2:
            ui.log(f"[voice error: {e2}]")
    ui.set_state("idle")


# ---------------- Voice in ----------------
def calibrate():
    try:
        rec = sd.rec(int(SAMPLE_RATE * 1.0), samplerate=SAMPLE_RATE, channels=1, dtype="int16")
        sd.wait()
        ambient = float(np.abs(rec).mean())
        STATE["threshold"] = min(1500.0, max(120.0, ambient * 2.5))
        ui.log(f"[mic ready: background {ambient:.0f}, trigger level {STATE['threshold']:.0f}]")
    except Exception as e:
        ui.log(f"[microphone error: {e}]")


def listen():
    chunk = int(SAMPLE_RATE * 0.1)
    frames, pre, started, silent = [], deque(maxlen=3), False, 0
    try:
        with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16") as stream:
            for _i in range(120):
                data, _overflow = stream.read(chunk)
                level = float(np.abs(data).mean())
                ui.level = level
                if level > STATE["threshold"]:
                    if not started:
                        frames.extend(pre)
                    started, silent = True, 0
                elif started:
                    silent += 1
                if started:
                    frames.append(data)
                    if silent >= 10:
                        break
                else:
                    pre.append(data)
    except Exception as e:
        ui.log(f"[microphone error: {e}]")
        time.sleep(3)
        return ""
    ui.level = 0.0
    if not frames:
        return ""
    audio = sr.AudioData(np.concatenate(frames).tobytes(), SAMPLE_RATE, 2)
    try:
        text = recognizer.recognize_google(audio).lower()
        ui.log(f"[heard: {text}]")
        return text
    except sr.UnknownValueError:
        ui.log("[heard something but couldn't make it out]")
    except sr.RequestError as e:
        ui.log(f"[speech service unreachable: {e}]")
    return ""


# ---------------- Brain ----------------
def open_app(name):
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-a", name])
    elif sys.platform.startswith("win"):
        os.startfile(name)
    else:
        subprocess.Popen([name])


def local_command(command):
    if "what time" in command or "the time" in command:
        return datetime.datetime.now().strftime("It is %H:%M, sir.")
    if command.startswith("open "):
        target = command[5:].strip()
        if target in ("youtube", "google", "github", "gmail"):
            webbrowser.open(f"https://www.{target}.com")
        else:
            open_app(target)
        return f"Opening {target}, sir."
    if command.startswith("search for "):
        query = command[len("search for "):]
        webbrowser.open(f"https://www.google.com/search?q={query.replace(' ', '+')}")
        return f"Searching for {query}."
    if command in ("quit", "exit", "goodbye", "shut down"):
        speak("Very good, sir. Goodbye.")
        os._exit(0)
    return None


def ask_ai(command):
    history.append({"role": "user", "content": command})
    if API_KEY.startswith("sk-ant-usr"):
        clients = [("bearer", client_bearer), ("api-key", client_apikey)]
    else:
        clients = [("api-key", client_apikey), ("bearer", client_bearer)]
    last_error = None
    for label, c in clients:
        try:
            response = c.messages.create(model=MODEL, max_tokens=300, system=SYSTEM_PROMPT,
                                         messages=history[-12:])
            break
        except anthropic.AuthenticationError as e:
            ui.log(f"[auth as {label} failed: {e}]")
            last_error = e
    else:
        history.pop()
        raise last_error
    reply = "".join(b.text for b in response.content if getattr(b, "type", "") == "text").strip()
    if not reply:
        reply = "I'm afraid I have nothing to say to that, sir."
    history.append({"role": "assistant", "content": reply})
    return reply


def process(command):
    with lock:
        ui.log(f"You: {command}")
        ui.set_state("thinking")
        try:
            reply = local_command(command) or ask_ai(command)
        except Exception as e:
            ui.log(f"[error: {e}]")
            reply = "I'm afraid I'm having trouble reaching my sources, sir."
        speak(reply)


def strip_wake_word(text):
    for w in WAKE_WORDS:
        if w in text:
            return True, text.split(w, 1)[1].strip(" ,.")
    return False, text


def handle_typed(text):
    _found, command = strip_wake_word(text)
    process(command or text)


def assistant_loop():
    ui.log(f"[key check: starts with '{API_KEY[:10]}', length {len(API_KEY)}]")
    try:
        ui.log(f"[microphone: {sd.query_devices(kind='input')['name']}]")
    except Exception as e:
        ui.log(f"[no microphone found: {e}]")
    speak("Alfred at your service.")
    ui.log("[calibrating mic, stay quiet for one second...]")
    ui.set_state("listening")
    calibrate()
    while True:
        ui.set_state("listening")
        heard = listen()
        if not heard:
            continue
        found, command = strip_wake_word(heard)
        if not found:
            continue
        if not command:
            with lock:
                speak("Yes, sir?")
            ui.set_state("listening")
            command = listen()
            if not command:
                continue
        process(command)


threading.Thread(target=assistant_loop, daemon=True).start()
ui.root.mainloop()