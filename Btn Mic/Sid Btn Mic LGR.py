import tkinter as tk
from PIL import Image, ImageTk, ImageEnhance
import traceback, sys, os, ctypes, requests, threading, time, psutil, io, numpy as np
from pynput import mouse, keyboard
import speech_recognition as sr
import pyttsx3
from faster_whisper import WhisperModel

# --- FIXED DIRECTORY PATHING ---
BASE_DIR = r"C:\Users\Yoda\LM\AI\Tools"

def get_path(filename): 
    return os.path.join(BASE_DIR, filename)

LOG_PATH = get_path("sid_crash_log.txt")

def write_crash_log(error):
    try:
        with open(LOG_PATH, "a") as f:
            f.write(f"\n--- AUDIO DEBUG: 2026-02-07 ---\n{error}\n")
    except: pass

def set_high_priority():
    try:
        p = psutil.Process(os.getpid())
        p.nice(psutil.HIGH_PRIORITY_CLASS)
    except Exception as e:
        write_crash_log(f"Failed to set high priority: {e}")

def hide_console():
    hWnd = ctypes.WinDLL('kernel32').GetConsoleWindow()
    if hWnd != 0: ctypes.WinDLL('user32').ShowWindow(hWnd, 0)

class SidCore:
    def __init__(self, root):
        self.root = root
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

            # Initialize Faster-Whisper
            model_size = "base.en"
            self.whisper_model = WhisperModel(model_size, device="cuda", compute_type="float16")

            self.is_active = False
            self.pulse_scale = 0
            self.pulse_direction = 1
            self.pressed_keys = set()

            self.mouse_l = mouse.Listener(on_click=self.on_mouse_click)
            self.mouse_l.start()
            self.key_l = keyboard.Listener(on_press=self.on_key_press, on_release=self.on_key_release)
            self.key_l.start()

            self.session = requests.Session()

            self.label.bind("<Button-1>", self.start_move)
            self.label.bind("<B1-Motion>", self.do_move)
            self.animate()
            
        except Exception:
            write_crash_log(traceback.format_exc())

    def speak(self, text):
        def audio_thread():
            try:
                alltalk_url = "http://127.0.0.1:7851/api/tts-generate"
                payload = {
                    "text_input": text,
                    "character_voice_gen": "archer.wav",
                    "autoplay": "true",
                    "autoplay_volume": "0.8",
                    "deepspeed": "True"
                }
                response = self.session.post(alltalk_url, data=payload, timeout=5)
                if response.status_code != 200: raise ConnectionError("AllTalk Offline")
            except Exception:
                try:
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
                "messages": [
                    {"role": "system", "content": "You are a helpful assistant."},
                    {"role": "user", "content": text}
                ],
                "stream": False
            }
            # KEEPING PORT AT 1234 AS REQUESTED
            response = self.session.post("http://localhost:1234/v1/chat/completions", json=payload, timeout=15)
            response.raise_for_status()
            content = response.json()['choices'][0]['message']['content']
            if content:
                self.speak(content)
        except Exception as e:
            write_crash_log(f"LM Studio Comm Error: {e}")

    def capture_audio(self):
        try:
            with self.mic as source:
                self.recognizer.adjust_for_ambient_noise(source, duration=0.2)
                audio = self.recognizer.listen(source, phrase_time_limit=10)

            # Convert AudioData to NumPy array for Faster-Whisper
            raw_data = audio.get_raw_data(convert_rate=16000, convert_width=2)
            audio_np = np.frombuffer(raw_data, dtype=np.int16).astype(np.float32) / 32768.0

            segments, info = self.whisper_model.transcribe(audio_np, beam_size=1)
            user_text = " ".join([segment.text for segment in segments]).strip()

            if user_text:
                self.send_to_lm_studio(user_text)
        except Exception as e:
            write_crash_log(f"Transcription Error: {e}")

    def activate(self):
        if not self.is_active:
            self.is_active = True
            threading.Thread(target=self.capture_audio, daemon=True).start()

    def deactivate(self):
        if self.is_active:
            self.is_active = False
            if not self.use_fallback:
                self.root.after(0, lambda: self.label.config(image=self.img_ready))
            else:
                self.root.after(0, lambda: self.label.config(fg="red"))

    # --- SMALL SECTION UPDATE: 0.5s DELAY ---
    def on_mouse_click(self, x, y, button, pressed):
        if button == mouse.Button.x1:
            if pressed:
                self.activate()
            else:
                # Half-second delay before allowing the system to reset state
                threading.Timer(0.5, self.deactivate).start()

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
    set_high_priority()
    root = tk.Tk()
    app = SidCore(root)
    root.mainloop()