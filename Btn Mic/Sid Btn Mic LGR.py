import tkinter as tk
from PIL import Image, ImageTk, ImageEnhance, ImageOps, ImageFilter
import traceback, sys, os, ctypes, threading, time, logging, queue
from logging.handlers import RotatingFileHandler
from pynput import mouse, keyboard
import speech_recognition as sr
import pyttsx3
import httpx
from enum import Enum, auto

# Attempt to import pythoncom for Windows TTS thread safety
try:
    if sys.platform == "win32":
        import pythoncom
    else:
        pythoncom = None
except ImportError:
    pythoncom = None

# --- PORTABLE PATHING ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def get_path(filename):
    return os.path.join(BASE_DIR, filename)

# --- CENTRALIZED CONFIGURATION ---
CONFIG = {
    "URLS": {
        "LM_STUDIO": "http://localhost:1234/v1/chat/completions",
        "ALLTALK": "http://127.0.0.1:7851/api/tts-generate"
    },
    "ASSETS": {
        "READY_IMG": "ready.png",
        "LISTENING_IMG": "listening.png",
        "LOG_FILE": "sid_debug_log.txt"
    },
    "UI": {
        "GEOMETRY": "40x40+100+100",
        "BG_COLOR": "black",
        "TRANSPARENT_COLOR": "black",
        "BASE_SIZE": 40,
        "PULSE_SPEED_MS": 35,
        "PULSE_MAX": 5,
        "ANIMATION_BRIGHTNESS_BASE": 0.7,
        "ANIMATION_BRIGHTNESS_FACTOR": 30.0
    },
    "AUDIO": {
        "PAUSE_THRESHOLD": 0.5,
        "NON_SPEAKING_DURATION": 0.3,
        "PHRASE_TIME_LIMIT": 10,
        "TTS_RATE": 155,
        "ALLTALK_VOICE": "archer.wav",
        "ALLTALK_VOLUME": "0.8"
    },
    "CONTROLS": {
        "ACTIVATION_KEYS": {'Key.f1', 'Key.f2'},
        "KILL_KEYS": {'-', '='},
        "DEACTIVATION_DELAY_MS": 500,
        "DEAD_ZONE_MS": 600
    }
}

# --- LOGGING SETUP ---
def setup_logging():
    logger = logging.getLogger("SidAssistant")
    logger.setLevel(logging.DEBUG)
    log_path = get_path(CONFIG["ASSETS"]["LOG_FILE"])
    handler = RotatingFileHandler(log_path, maxBytes=1024*1024, backupCount=5)
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    return logger

logger = setup_logging()

class AppState(Enum):
    IDLE = auto()
    LISTENING = auto()
    PROCESSING = auto()
    RESPONDING = auto()

def hide_console():
    try:
        if sys.platform == "win32":
            hWnd = ctypes.WinDLL('kernel32').GetConsoleWindow()
            if hWnd != 0:
                ctypes.WinDLL('user32').ShowWindow(hWnd, 0)
    except Exception as e:
        logger.error(f"Failed to hide console: {e}")

class InputController:
    def __init__(self, on_activate, on_deactivate, on_exit, start_dead_zone_cmd):
        self.on_activate = on_activate
        self.on_deactivate = on_deactivate
        self.on_exit = on_exit
        self.start_dead_zone_cmd = start_dead_zone_cmd
        self.pressed_keys = set()
        self.dead_zone_active = False
        self.is_mouse_activating = False
        self.mouse_listener = None
        self.key_listener = None
        self._lock = threading.Lock()

    def start(self):
        # win32_event_filter for mouse suppression and dead zone handling
        def win32_event_filter(msg, data):
            # WM_XBUTTONDOWN = 0x020B (523), WM_XBUTTONUP = 0x020C (524)
            if msg in (523, 524):
                try:
                    mouse_data = data.contents.mouseData
                    xbutton = (mouse_data >> 16) & 0xFFFF
                    if xbutton in (1, 2):
                        if msg == 524: # WM_XBUTTONUP
                            with self._lock:
                                if self.is_mouse_activating:
                                    self.is_mouse_activating = False
                                    # Trigger deactivation and dead zone immediately
                                    self.start_dead_zone_cmd()
                                    return False # Suppress this release

                                if self.dead_zone_active:
                                    return False # Suppress during dead zone

                        elif msg == 523: # WM_XBUTTONDOWN
                            with self._lock:
                                if self.dead_zone_active:
                                    return False
                                self.is_mouse_activating = True
                                self.on_activate()
                                return False # Suppress the initial press too to be safe
                except Exception as e:
                    logger.error(f"Error in win32_event_filter: {e}")
            return True

        listener_kwargs = {
            'on_click': self.on_mouse_click,
            'suppress': False
        }
        if sys.platform == "win32":
            listener_kwargs['win32_event_filter'] = win32_event_filter

        self.mouse_listener = mouse.Listener(**listener_kwargs)
        self.key_listener = keyboard.Listener(
            on_press=self.on_key_press,
            on_release=self.on_key_release
        )
        self.mouse_listener.start()
        self.key_listener.start()
        logger.info("Input listeners started.")

    def stop(self):
        if self.mouse_listener:
            self.mouse_listener.stop()
        if self.key_listener:
            self.key_listener.stop()
        logger.info("Input listeners stopped.")

    def set_dead_zone(self, active):
        with self._lock:
            self.dead_zone_active = active
            if active:
                logger.debug("Dead zone activated.")
            else:
                logger.debug("Dead zone deactivated.")

    def on_mouse_click(self, x, y, button, pressed):
        # This will still be called for other buttons (Left, Right)
        # But X1/X2 are handled and suppressed in the filter on Windows
        pass

    def on_key_press(self, key):
        try:
            k = str(key).replace("'", "")
            self.pressed_keys.add(k)

            # Kill sequence check
            if all(rk in self.pressed_keys for rk in CONFIG["CONTROLS"]["KILL_KEYS"]):
                logger.info("Kill sequence detected.")
                self.on_exit()
                return

            # Activation keys check
            if any(ak in self.pressed_keys for ak in CONFIG["CONTROLS"]["ACTIVATION_KEYS"]):
                self.on_activate()
        except Exception as e:
            logger.error(f"Key press error: {e}")

    def on_key_release(self, key):
        try:
            k = str(key).replace("'", "")
            if k in self.pressed_keys:
                self.pressed_keys.remove(k)
            
            if k in CONFIG["CONTROLS"]["ACTIVATION_KEYS"]:
                self.on_deactivate()
        except Exception as e:
            logger.error(f"Key release error: {e}")

class SidCore:
    def __init__(self, root):
        self.root = root
        self.state = AppState.IDLE
        self.running = True
        self._lock = threading.Lock()

        # Audio & Recognizer
        self.recognizer = sr.Recognizer()
        self.recognizer.pause_threshold = CONFIG["AUDIO"]["PAUSE_THRESHOLD"]
        self.recognizer.non_speaking_duration = CONFIG["AUDIO"]["NON_SPEAKING_DURATION"]
        self.mic = sr.Microphone()

        # TTS Queue & Threading
        self.tts_queue = queue.Queue()
        self.tts_engine = None
        self.tts_thread = threading.Thread(target=self.tts_worker, daemon=True)
        self.tts_thread.start()

        # UI & Animation
        self.frames = {state: [] for state in AppState}
        self.animation_index = 0
        self.use_fallback = False

        self.setup_ui()
        self.pre_render_animations()

        # Controller
        self.controller = InputController(
            on_activate=lambda: self.root.after(0, self.activate),
            on_deactivate=lambda: self.root.after(0, self.deactivate_request),
            on_exit=lambda: self.root.after(0, self.shutdown),
            start_dead_zone_cmd=lambda: self.root.after(0, self.deactivate_request)
        )
        self.controller.start()

        # Start animation loop
        self.animate()
        logger.info("SidCore initialized.")

    def setup_ui(self):
        hide_console()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.config(bg=CONFIG["UI"]["BG_COLOR"])
        self.root.attributes("-transparentcolor", CONFIG["UI"]["TRANSPARENT_COLOR"])
        self.root.geometry(CONFIG["UI"]["GEOMETRY"])

        try:
            self.raw_ready = Image.open(get_path(CONFIG["ASSETS"]["READY_IMG"])).convert("RGBA")
            self.raw_listening = Image.open(get_path(CONFIG["ASSETS"]["LISTENING_IMG"])).convert("RGBA")
            
            # Initial static image
            self.img_ready = self.render_frame(self.raw_ready, CONFIG["UI"]["BASE_SIZE"])
            self.label = tk.Label(self.root, image=self.img_ready, bg=CONFIG["UI"]["BG_COLOR"], bd=0)
        except Exception as e:
            logger.error(f"Image load failed: {e}")
            self.use_fallback = True
            self.label = tk.Label(self.root, text="SID", fg="red", bg="black", font=("Arial", 10, "bold"))

        self.label.pack(expand=True)
        self.label.bind("<Button-1>", self.start_move)
        self.label.bind("<B1-Motion>", self.do_move)

        self.root.protocol("WM_DELETE_WINDOW", self.shutdown)

    def render_frame(self, pil_img, size, brightness=1.0):
        try:
            enh = ImageEnhance.Brightness(pil_img)
            img = enh.enhance(brightness)
            img = ImageOps.autocontrast(img)
            img = img.filter(ImageFilter.SHARPEN)
            return ImageTk.PhotoImage(img.resize((size, size), Image.Resampling.LANCZOS))
        except Exception as e:
            logger.error(f"Frame rendering error: {e}")
            return None

    def pre_render_animations(self):
        if self.use_fallback: return
        logger.info("Pre-rendering animation frames...")

        # IDLE: Just the ready image
        self.frames[AppState.IDLE] = [self.render_frame(self.raw_ready, CONFIG["UI"]["BASE_SIZE"])]

        # LISTENING: Pulsing listening image
        for i in range(20): # 20 frame cycle
            scale = abs(i - 10) / 2.0 # 0 to 5
            size = int(CONFIG["UI"]["BASE_SIZE"] - 5 + scale)
            brightness = CONFIG["UI"]["ANIMATION_BRIGHTNESS_BASE"] + (scale / CONFIG["UI"]["ANIMATION_BRIGHTNESS_FACTOR"])
            frame = self.render_frame(self.raw_listening, size, brightness)
            if frame:
                self.frames[AppState.LISTENING].append(frame)

        # PROCESSING: Faster pulse
        for i in range(10): # 10 frame cycle for faster pulse
            scale = abs(i - 5) / 1.0 # 0 to 5
            size = int(CONFIG["UI"]["BASE_SIZE"] - 3 + scale)
            brightness = 1.0 + (scale / 20.0)
            frame = self.render_frame(self.raw_listening, size, brightness)
            if frame:
                self.frames[AppState.PROCESSING].append(frame)

        # RESPONDING: Static but bright
        self.frames[AppState.RESPONDING] = [
            self.render_frame(self.raw_listening, CONFIG["UI"]["BASE_SIZE"], 1.3),
            self.render_frame(self.raw_listening, CONFIG["UI"]["BASE_SIZE"] + 2, 1.4)
        ]

        logger.info("Animation frames cached.")

    def animate(self):
        if not self.running: return

        if not self.use_fallback:
            current_frames = self.frames.get(self.state, [])
            if current_frames:
                self.animation_index = (self.animation_index + 1) % len(current_frames)
                self.label.config(image=current_frames[self.animation_index])
        else:
            if self.state != AppState.IDLE:
                self.label.config(fg="white" if time.time() % 1 > 0.5 else "red")
            else:
                self.label.config(fg="red")

        self.root.after(CONFIG["UI"]["PULSE_SPEED_MS"], self.animate)

    def set_state(self, new_state):
        with self._lock:
            if self.state != new_state:
                logger.info(f"Transition: {self.state} -> {new_state}")
                self.state = new_state
                self.animation_index = 0

    def activate(self):
        if self.state == AppState.IDLE:
            self.set_state(AppState.LISTENING)
            threading.Thread(target=self.capture_audio, daemon=True).start()

    def deactivate_request(self):
        # 0.5s delay using root.after as requested
        self.root.after(CONFIG["CONTROLS"]["DEACTIVATION_DELAY_MS"], self.deactivate)

        # Start dead zone
        self.controller.set_dead_zone(True)
        self.root.after(CONFIG["CONTROLS"]["DEAD_ZONE_MS"], lambda: self.controller.set_dead_zone(False))

    def deactivate(self):
        if self.state == AppState.LISTENING:
            self.set_state(AppState.IDLE)

    def capture_audio(self):
        try:
            with self.mic as source:
                self.recognizer.adjust_for_ambient_noise(source, duration=0.2)
                logger.debug("Listening for audio...")
                audio = self.recognizer.listen(source, phrase_time_limit=CONFIG["AUDIO"]["PHRASE_TIME_LIMIT"])

            self.root.after(0, lambda: self.set_state(AppState.PROCESSING))
            logger.debug("Recognizing speech...")
            user_text = self.recognizer.recognize_google(audio)
            logger.info(f"User: {user_text}")

            self.send_to_lm_studio(user_text)
        except sr.UnknownValueError:
            logger.warning("Speech recognition could not understand audio.")
            self.root.after(0, lambda: self.set_state(AppState.IDLE))
        except sr.RequestError as e:
            logger.error(f"Speech recognition service error: {e}")
            self.root.after(0, lambda: self.set_state(AppState.IDLE))
        except Exception as e:
            logger.error(f"Audio capture error: {e}")
            self.root.after(0, lambda: self.set_state(AppState.IDLE))

    def send_to_lm_studio(self, text):
        def task():
            try:
                payload = {
                    "model": "local-model",
                    "messages": [
                        {"role": "system", "content": "You are a helpful assistant."},
                        {"role": "user", "content": text}
                    ],
                    "stream": False
                }
                logger.debug(f"Sending to LM Studio: {text}")
                with httpx.Client(timeout=15.0) as client:
                    response = client.post(CONFIG["URLS"]["LM_STUDIO"], json=payload)

                if response.status_code == 200:
                    reply = response.json()['choices'][0]['message']['content']
                    logger.info(f"AI: {reply}")
                    self.root.after(0, lambda: self.set_state(AppState.RESPONDING))
                    self.speak(reply)
                else:
                    logger.error(f"LM Studio returned status {response.status_code}")
                    self.root.after(0, lambda: self.set_state(AppState.IDLE))
            except Exception as e:
                logger.error(f"LM Studio API error: {e}")
                self.root.after(0, lambda: self.set_state(AppState.IDLE))

        threading.Thread(target=task, daemon=True).start()

    def speak(self, text):
        # Add to queue for the dedicated TTS worker
        self.tts_queue.put(text)

    def tts_worker(self):
        """Dedicated thread for TTS to ensure single-instance access and non-blocking UI."""
        if sys.platform == "win32" and pythoncom:
            pythoncom.CoInitialize()

        while self.running:
            try:
                text = self.tts_queue.get(timeout=0.5)
                self._do_speak(text)
                self.tts_queue.task_done()
                self.root.after(0, lambda: self.set_state(AppState.IDLE))
            except queue.Empty:
                continue
            except Exception as e:
                logger.error(f"TTS Worker Error: {e}")

    def _do_speak(self, text):
        # Try AllTalk first
        try:
            payload = {
                "text_input": text,
                "character_voice_gen": CONFIG["AUDIO"]["ALLTALK_VOICE"],
                "autoplay": "true",
                "autoplay_volume": CONFIG["AUDIO"]["ALLTALK_VOLUME"]
            }
            with httpx.Client(timeout=10.0) as client:
                response = client.post(CONFIG["URLS"]["ALLTALK"], data=payload)
            if response.status_code == 200:
                logger.debug("AllTalk TTS successful.")
                return
        except Exception as e:
            logger.warning(f"AllTalk TTS failed, falling back to local TTS: {e}")

        # Fallback to pyttsx3
        try:
            if self.tts_engine is None:
                self.tts_engine = pyttsx3.init()
                voices = self.tts_engine.getProperty('voices')
                if voices:
                    self.tts_engine.setProperty('voice', voices[0].id)
                self.tts_engine.setProperty('rate', CONFIG["AUDIO"]["TTS_RATE"])

            self.tts_engine.say(text)
            self.tts_engine.runAndWait()
        except Exception as e:
            logger.error(f"Local TTS error: {e}")

    def shutdown(self):
        logger.info("Shutting down...")
        self.running = False
        if self.controller:
            try:
                self.controller.stop()
            except Exception as e:
                logger.error(f"Error stopping controller: {e}")

        try:
            self.root.destroy()
        except Exception as e:
            logger.error(f"Error destroying root: {e}")

        logger.info("Shutdown complete.")
        sys.exit(0)

    # Window movement
    def start_move(self, event):
        self.x, self.y = event.x, event.y
    def do_move(self, event):
        try:
            x = self.root.winfo_x() + (event.x - self.x)
            y = self.root.winfo_y() + (event.y - self.y)
            self.root.geometry(f"+{x}+{y}")
        except: pass

if __name__ == "__main__":
    try:
        root = tk.Tk()
        app = SidCore(root)
        root.mainloop()
    except Exception as e:
        logger.critical(f"Unhandled exception in main: {e}\n{traceback.format_exc()}")
