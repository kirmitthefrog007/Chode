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
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    from comtypes import CLSCTX_ALL
except ImportError:
    AudioUtilities = None

# --- CONFIGURATION ---
BASE_DIR = r"C:\Users\Yoda\LM\AI\Tools"
ALLTALK_URL = "http://127.0.0.1:7851/api/tts-generate"
LM_STUDIO_URL = "http://localhost:1234/v1/chat/completions"
WHISPER_MODEL_SIZE = "base.en"

def get_path(filename): 
    return os.path.join(BASE_DIR, filename)

LOG_PATH = get_path("sid_crash_log.txt")

def write_crash_log(error):
    try:
        with open(LOG_PATH, "a") as f:
            f.write(f"\n--- DEBUG {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n{error}\n")
    except: pass

def set_high_priority():
    try:
        p = psutil.Process(os.getpid())
        if os.name == 'nt':
            p.nice(psutil.HIGH_PRIORITY_CLASS)
        else:
            p.nice(-10)
    except Exception as e:
        write_crash_log(f"Priority Error: {e}")

def hide_console():
    if os.name == 'nt':
        hWnd = ctypes.WinDLL('kernel32').GetConsoleWindow()
        if hWnd != 0: ctypes.WinDLL('user32').ShowWindow(hWnd, 0)

class AudioDucker:
    def __init__(self):
        self.original_volumes = {}
        self.enabled = AudioUtilities is not None
        if self.enabled:
            try:
                import pythoncom
                pythoncom.CoInitialize()
            except Exception as e:
                write_crash_log(f"Ducking Init COM Error: {e}")
                self.enabled = False

    def duck(self, level=0.1):
        if not self.enabled: return
        try:
            sessions = AudioUtilities.GetAllSessions()
            for session in sessions:
                if session.Process and session.Process.name().lower() != "python.exe":
                    volume = session.SimpleAudioVolume
                    self.original_volumes[session.Process.pid] = volume.GetMasterVolume()
                    volume.SetMasterVolume(level, None)
        except Exception as e:
            write_crash_log(f"Duck Error: {e}")

    def unduck(self):
        if not self.enabled: return
        try:
            sessions = AudioUtilities.GetAllSessions()
            for session in sessions:
                if session.Process and session.Process.pid in self.original_volumes:
                    volume = session.SimpleAudioVolume
                    volume.SetMasterVolume(self.original_volumes[session.Process.pid], None)
            self.original_volumes.clear()
        except Exception as e:
            write_crash_log(f"Unduck Error: {e}")

class SidCore:
    def __init__(self, root):
        self.root = root
        set_high_priority()
        hide_console()

        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.config(bg='black')
        if os.name == 'nt':
            self.root.attributes("-transparentcolor", "black")
        self.root.geometry("40x40+100+100")

        self.is_active = False
        self.suppress_until = 0
        self.last_deactivate_request = 0

        # Pre-render animation frames
        self.frames = []
        self.img_ready = None
        self.load_assets()

        self.label = tk.Label(root, image=self.img_ready, bg="black", bd=0)
        if not self.img_ready:
            self.label.config(text="SID", fg="cyan", font=("Arial", 10, "bold"))
            
        self.label.pack(expand=True)
        self.label.bind("<Button-1>", self.start_move)
        self.label.bind("<B1-Motion>", self.do_move)

        # STT Setup
        self.model = None
        threading.Thread(target=self.init_whisper, daemon=True).start()

        self.ducker = AudioDucker()
        self.recognizer = sr.Recognizer()
        self.mic = sr.Microphone()

        # Input Listeners
        self.mouse_l = mouse.Listener(on_click=self.on_mouse_click, win32_event_filter=self.win32_filter)
        self.mouse_l.start()

        self.pressed_keys = set()
        self.key_l = keyboard.Listener(on_press=self.on_key_press, on_release=self.on_key_release)
        self.key_l.start()

        self.animate_state = 0
        self.animate()

    def load_assets(self):
        try:
            ready_path = get_path("ready.png")
            listen_path = get_path("listening.png")
            if os.path.exists(ready_path) and os.path.exists(listen_path):
                self.raw_ready = Image.open(ready_path).convert("RGBA")
                self.raw_listening = Image.open(listen_path).convert("RGBA")
                self.img_ready = ImageTk.PhotoImage(self.raw_ready.resize((40, 40), Image.Resampling.LANCZOS))

                # Pre-calculate 12 frames of pulsing for smoother loop
                for i in range(12):
                    # Pulse scale 0 to 5 and back
                    scale = i if i <= 6 else 12 - i
                    size = int(35 + (scale * 0.8))
                    brightness = 0.7 + (scale / 25.0)
                    enh = ImageEnhance.Brightness(self.raw_listening)
                    img = enh.enhance(brightness)
                    self.frames.append(ImageTk.PhotoImage(img.resize((size, size), Image.Resampling.LANCZOS)))
        except Exception as e:
            write_crash_log(f"Asset Load Error: {e}")

    def init_whisper(self):
        if WhisperModel is None:
            write_crash_log("Faster-Whisper not installed")
            return
        try:
            # Try to add CUDA DLLs if they exist in common locations
            cuda_path = r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.4\bin"
            if os.path.exists(cuda_path):
                os.add_dll_directory(cuda_path)

            self.model = WhisperModel(WHISPER_MODEL_SIZE, device="cuda", compute_type="float16")
            write_crash_log("Whisper loaded with CUDA")
        except Exception as e:
            write_crash_log(f"Whisper CUDA Error (falling back to CPU): {e}")
            try:
                self.model = WhisperModel(WHISPER_MODEL_SIZE, device="cpu", compute_type="int8")
                write_crash_log("Whisper loaded with CPU")
            except Exception as e2:
                write_crash_log(f"Whisper CPU Critical Error: {e2}")

    def win32_filter(self, msg, data):
        # WM_XBUTTONDOWN = 0x020B, WM_XBUTTONUP = 0x020C
        if msg in [0x020B, 0x020C]:
            if time.time() < self.suppress_until:
                return False # Suppress navigation
        return True

    def on_mouse_click(self, x, y, button, pressed):
        if button in [mouse.Button.x1, mouse.Button.x2]:
            self.suppress_until = time.time() + 0.3
            if pressed:
                self.root.after(0, self.activate)
            else:
                self.last_deactivate_request = time.time()
                # 300ms delay to allow for quick re-clicks (stutter prevention)
                self.root.after(300, self.check_deactivate)

    def check_deactivate(self):
        # Only deactivate if another click hasn't happened since the timer started
        if time.time() - self.last_deactivate_request >= 0.25:
            self.deactivate()

    def activate(self):
        if not self.is_active:
            self.is_active = True
            threading.Thread(target=self.ducker.duck, daemon=True).start()
            threading.Thread(target=self.capture_audio, daemon=True).start()

    def deactivate(self):
        if self.is_active:
            self.is_active = False
            threading.Thread(target=self.ducker.unduck, daemon=True).start()
            self.root.after(0, lambda: self.label.config(image=self.img_ready) if self.img_ready else self.label.config(fg="cyan"))

    def capture_audio(self):
        try:
            with self.mic as source:
                self.recognizer.adjust_for_ambient_noise(source, duration=0.1)
                audio_data = self.recognizer.listen(source, phrase_time_limit=12)

            if not self.model:
                # Fallback to Google if Whisper failed to load
                user_text = self.recognizer.recognize_google(audio_data)
            else:
                wav_data = io.BytesIO(audio_data.get_wav_data())
                segments, _ = self.model.transcribe(wav_data, beam_size=1) # Beam size 1 for max speed
                user_text = " ".join([s.text for s in segments]).strip()

            if user_text:
                self.send_to_lm_studio(user_text)
        except Exception as e:
            if "recognition" not in str(e).lower():
                write_crash_log(f"STT Error: {e}")

    def send_to_lm_studio(self, text):
        try:
            payload = {
                "model": "local-model",
                "messages": [{"role": "system", "content": "You are a helpful assistant."},
                             {"role": "user", "content": text}],
                "stream": False
            }
            response = requests.post(LM_STUDIO_URL, json=payload, timeout=25)
            if response.status_code == 200:
                self.speak(response.json()['choices'][0]['message']['content'])
        except Exception as e:
            write_crash_log(f"LM Studio Error: {e}")

    def speak(self, text):
        def audio_thread():
            try:
                # AllTalk Multipart Request with deepspeed
                files = {
                    'text_input': (None, text),
                    'character_voice_gen': (None, 'archer.wav'),
                    'autoplay': (None, 'true'),
                    'deepspeed': (None, 'True'),
                    'output_file_name': (None, 'sid_response')
                }
                response = requests.post(ALLTALK_URL, files=files, timeout=15)
                if response.status_code != 200:
                    write_crash_log(f"AllTalk Status Error: {response.status_code}")
                    raise ConnectionError()
            except Exception as e:
                write_crash_log(f"AllTalk Post Error: {e}. Falling back to pyttsx3.")
                try:
                    import pythoncom
                    pythoncom.CoInitialize()
                    engine = pyttsx3.init()
                    engine.setProperty('rate', 160)
                    engine.say(text)
                    engine.runAndWait()
                except Exception as e2:
                    write_crash_log(f"Local TTS Error: {e2}")
        threading.Thread(target=audio_thread, daemon=True).start()

    def animate(self):
        if self.is_active and self.frames:
            self.animate_state = (self.animate_state + 1) % len(self.frames)
            self.label.config(image=self.frames[self.animate_state])
        elif not self.is_active and self.img_ready:
            if self.label.cget("image") != str(self.img_ready):
                self.label.config(image=self.img_ready)

        self.root.after(40, self.animate)

    def on_key_press(self, key):
        try:
            k = str(key).replace("'", "")
            self.pressed_keys.add(k)
            if '-' in self.pressed_keys and '=' in self.pressed_keys:
                self.mouse_l.stop()
                self.key_l.stop()
                self.root.quit()
                os._exit(0)
            if 'Key.f1' in self.pressed_keys: self.root.after(0, self.activate)
        except: pass

    def on_key_release(self, key):
        try:
            k = str(key).replace("'", "")
            if k in self.pressed_keys: self.pressed_keys.remove(k)
            if k == 'Key.f1': self.root.after(0, self.deactivate)
        except: pass

    def start_move(self, event):
        self.offset_x, self.offset_y = event.x, event.y
    def do_move(self, event):
        x, y = self.root.winfo_x() + (event.x - self.offset_x), self.root.winfo_y() + (event.y - self.offset_y)
        self.root.geometry(f"+{x}+{y}")

if __name__ == "__main__":
    root = tk.Tk()
    app = SidCore(root)
    root.mainloop()
