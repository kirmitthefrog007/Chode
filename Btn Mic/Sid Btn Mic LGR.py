import tkinter as tk
from PIL import Image, ImageTk, ImageEnhance
import traceback, sys, os, ctypes, requests, threading, time, io
from pynput import mouse, keyboard
import speech_recognition as sr
import pyttsx3
import psutil

try:
    from faster_whisper import WhisperModel
except ImportError:
    WhisperModel = None

try:
    from pycaw.pycaw import AudioUtilities
    import pythoncom
except ImportError:
    AudioUtilities = None

# --- FIXED DIRECTORY PATHING ---
BASE_DIR = r"C:\Users\Yoda\LM\AI\Tools"

def get_path(filename): 
    return os.path.join(BASE_DIR, filename)

LOG_PATH = get_path("sid_crash_log.txt")

def write_crash_log(error):
    try:
        with open(LOG_PATH, "a") as f:
            f.write(f"\n--- AUDIO DEBUG: {time.strftime('%Y-%m-%d')} ---\n{error}\n")
    except: pass

def hide_console():
    hWnd = ctypes.WinDLL('kernel32').GetConsoleWindow()
    if hWnd != 0: ctypes.WinDLL('user32').ShowWindow(hWnd, 0)

def set_high_priority():
    try:
        p = psutil.Process(os.getpid())
        if os.name == 'nt':
            p.nice(psutil.HIGH_PRIORITY_CLASS)
    except: pass

class SidCore:
    def __init__(self, root):
        self.root = root
        set_high_priority()
        try:
            hide_console()
            self.root.overrideredirect(True)
            self.root.attributes("-topmost", True)
            self.root.config(bg='black')
            self.root.attributes("-transparentcolor", "black")
            self.root.geometry("40x40+100+100")

            self.use_fallback = False
            try:
                self.raw_ready = Image.open(get_path("ready.png")).convert("RGBA")
                self.raw_listening = Image.open(get_path("listening.png")).convert("RGBA")
                self.img_ready = self.render_size(self.raw_ready, 40)
                self.label = tk.Label(root, image=self.img_ready, bg="black", bd=0)
            except Exception as e:
                write_crash_log(f"Image Load Failed at {BASE_DIR}: {e}")
                self.use_fallback = True
                self.label = tk.Label(root, text="SID", fg="red", bg="black", font=("Arial", 10, "bold"))
            
            self.label.pack(expand=True)

            self.recognizer = sr.Recognizer()
            self.mic = sr.Microphone()
            self.is_active = False
            self.pulse_scale = 0
            self.pulse_direction = 1
            self.pressed_keys = set()

            # --- WHISPER INIT ---
            self.whisper = None
            threading.Thread(target=self.init_whisper, daemon=True).start()

            # --- MOUSE LISTENER WITH WIN32 FILTER ---
            self.mouse_l = mouse.Listener(on_click=self.on_mouse_click, win32_event_filter=self.win32_filter)
            self.mouse_l.start()

            self.key_l = keyboard.Listener(on_press=self.on_key_press, on_release=self.on_key_release)
            self.key_l.start()

            self.label.bind("<Button-1>", self.start_move)
            self.label.bind("<B1-Motion>", self.do_move)
            self.animate()

        except Exception:
            write_crash_log(traceback.format_exc())

    def init_whisper(self):
        if WhisperModel:
            try:
                self.whisper = WhisperModel("base.en", device="cuda", compute_type="float16")
            except Exception as e:
                write_crash_log(f"Whisper CUDA Error: {e}")
                try:
                    self.whisper = WhisperModel("base.en", device="cpu", compute_type="int8")
                except Exception as e2:
                    write_crash_log(f"Whisper CPU Error: {e2}")

    def win32_filter(self, msg, data):
        # HIWORD(data.mouseData) is 1 for XBUTTON1, 2 for XBUTTON2
        button_num = data.mouseData >> 16
        if button_num == 1: # Only block the mic button (XBUTTON1)
            if msg == 0x020B: # WM_XBUTTONDOWN
                self.root.after(0, self.activate)
                return False # Consumed
            elif msg == 0x020C: # WM_XBUTTONUP
                # 0.3s delay as requested
                self.root.after(300, self.deactivate)
                return False # Consumed
        return True

    def duck_audio(self, duck=True):
        if AudioUtilities:
            try:
                pythoncom.CoInitialize()
                sessions = AudioUtilities.GetAllSessions()
                for session in sessions:
                    if session.Process and session.Process.name().lower() != "python.exe":
                        volume = session.SimpleAudioVolume
                        volume.SetMasterVolume(0.1 if duck else 1.0, None)
            except: pass

    def speak(self, text):
        def audio_thread():
            try:
                alltalk_url = "http://127.0.0.1:7851/api/tts-generate"
                # ADDED DEEPSPEED PARAMETER
                payload = {
                    "text_input": text,
                    "character_voice_gen": "archer.wav",
                    "autoplay": "true",
                    "autoplay_volume": "0.8",
                    "deepspeed": "True"
                }
                response = requests.post(alltalk_url, data=payload, timeout=15)
                if response.status_code != 200: raise ConnectionError("AllTalk Offline")
            except Exception as e:
                write_crash_log(f"TTS Error: {e}")
                try:
                    pythoncom.CoInitialize()
                    engine = pyttsx3.init()
                    voices = engine.getProperty('voices')
                    engine.setProperty('voice', voices[0].id)
                    engine.setProperty('rate', 155)
                    engine.say(text)
                    engine.runAndWait()
                    engine.stop()
                    del engine
                except Exception as e:
                    write_crash_log(f"Audio Critical Failure: {e}")
        threading.Thread(target=audio_thread, daemon=True).start()

    def send_to_lm_studio(self, text):
        try:
            payload = {
                "model": "local-model",
                "messages": [{"role": "system", "content": "You are a helpful assistant."},
                             {"role": "user", "content": text}],
                "stream": False
            }
            response = requests.post("http://localhost:1234/v1/chat/completions", json=payload, timeout=15)
            if response.status_code == 200:
                self.speak(response.json()['choices'][0]['message']['content'])
        except Exception as e:
            write_crash_log(f"LM Studio Comm Error: {e}")

    def capture_audio(self):
        try:
            with self.mic as source:
                self.recognizer.adjust_for_ambient_noise(source, duration=0.1)
                audio = self.recognizer.listen(source, phrase_time_limit=10)

            if self.whisper:
                wav_data = io.BytesIO(audio.get_wav_data())
                segments, _ = self.whisper.transcribe(wav_data, beam_size=1)
                user_text = " ".join([s.text for s in segments]).strip()
            else:
                user_text = self.recognizer.recognize_google(audio)

            if user_text:
                self.send_to_lm_studio(user_text)
        except Exception: pass

    def activate(self):
        if not self.is_active:
            self.is_active = True
            threading.Thread(target=self.duck_audio, args=(True,), daemon=True).start()
            threading.Thread(target=self.capture_audio, daemon=True).start()

    def deactivate(self):
        if self.is_active:
            self.is_active = False
            threading.Thread(target=self.duck_audio, args=(False,), daemon=True).start()
            if not self.use_fallback:
                self.root.after(0, lambda: self.label.config(image=self.img_ready))
            else:
                self.root.after(0, lambda: self.label.config(fg="red"))

    def on_mouse_click(self, x, y, button, pressed):
        # Fallback for non-windows or if filter fails, but filter should handle XBUTTON1
        pass

    def on_key_press(self, key):
        try:
            k = str(key).replace("'", "")
            self.pressed_keys.add(k)
            if '-' in self.pressed_keys and '=' in self.pressed_keys: os._exit(0)
            if 'Key.f1' in self.pressed_keys and 'Key.f2' in self.pressed_keys: self.activate()
        except: pass

    def on_key_release(self, key):
        try:
            k = str(key).replace("'", "")
            if k in self.pressed_keys: self.pressed_keys.remove(k)
            if k == 'Key.f1' or k == 'Key.f2': self.deactivate()
        except: pass

    def render_size(self, pil_img, size, brightness=1.0):
        enh = ImageEnhance.Brightness(pil_img)
        img = enh.enhance(brightness)
        return ImageTk.PhotoImage(img.resize((size, size), Image.Resampling.LANCZOS))

    def animate(self):
        if self.is_active:
            if not self.use_fallback:
                self.pulse_scale += self.pulse_direction * 0.5
                if self.pulse_scale >= 5 or self.pulse_scale <= 0: self.pulse_direction *= -1
                d_size = int(35 + self.pulse_scale)
                self.pulse_img = self.render_size(self.raw_listening, d_size, 0.7 + (self.pulse_scale/30.0))
                self.label.config(image=self.pulse_img)
            else:
                self.label.config(fg="white" if time.time() % 1 > 0.5 else "red")
        self.root.after(35, self.animate)

    def start_move(self, event): self.x, self.y = event.x, event.y
    def do_move(self, event):
        x, y = self.root.winfo_x() + (event.x - self.x), self.root.winfo_y() + (event.y - self.y)
        self.root.geometry(f"+{x}+{y}")

if __name__ == "__main__":
    root = tk.Tk()
    app = SidCore(root)
    root.mainloop()
