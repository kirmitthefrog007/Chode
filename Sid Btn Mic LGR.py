import tkinter as tk
from PIL import Image, ImageTk, ImageEnhance
import traceback, sys, os, ctypes, requests, threading, time
from pynput import mouse, keyboard
import speech_recognition as sr
import pyttsx3

# --- DYNAMIC DIRECTORY PATHING ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def get_path(filename): 
    return os.path.join(BASE_DIR, filename)

LOG_PATH = get_path("sid_debug_log.txt")

def write_crash_log(error):
    try:
        timestamp = time.strftime("[%Y-%m-%d %H:%M:%S]")
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{timestamp} {error}\n")
    except:
        pass

def hide_console():
    try:
        hWnd = ctypes.WinDLL('kernel32').GetConsoleWindow()
        if hWnd != 0:
            ctypes.WinDLL('user32').ShowWindow(hWnd, 0)
    except Exception as e:
        write_crash_log(f"Hide console failed: {e}")

class SidCore:
    def __init__(self, root):
        self.root = root
        self.lock = threading.Lock()
        self.is_shutting_down = False
        self.deactivate_timer = None

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

                # Pre-render pulse frames to optimize animation loop
                self.pulse_frames = []
                # Scale from 0 to 5 and back to 0
                scales = [x * 0.5 for x in range(11)] + [x * 0.5 for x in range(9, 0, -1)]
                for s in scales:
                    d_size = int(35 + s)
                    self.pulse_frames.append(self.render_size(self.raw_listening, d_size, 0.7 + (s/30.0)))
                self.pulse_frame_idx = 0

                self.label = tk.Label(root, image=self.img_ready, bg="black", bd=0)
            except Exception as e:
                write_crash_log(f"Image Load Failed at {BASE_DIR}: {e}")
                self.use_fallback = True
                self.label = tk.Label(root, text="SID", fg="red", bg="black", font=("Arial", 10, "bold"))
            
            self.label.pack(expand=True)

            self.recognizer = sr.Recognizer()
            self.mic = sr.Microphone()
            write_crash_log("Calibrating microphone...")
            with self.mic as source:
                self.recognizer.adjust_for_ambient_noise(source, duration=1.0)

            self.is_active = False
            self.pressed_keys = set()
            self.tts_lock = threading.Lock()

            self.mouse_l = mouse.Listener(on_click=self.on_mouse_click)
            self.mouse_l.start()
            self.key_l = keyboard.Listener(on_press=self.on_key_press, on_release=self.on_key_release)
            self.key_l.start()

            self.label.bind("<Button-1>", self.start_move)
            self.label.bind("<B1-Motion>", self.do_move)
            self.animate()
            
        except Exception:
            write_crash_log(traceback.format_exc())

    def shutdown(self):
        with self.lock:
            if self.is_shutting_down:
                return
            self.is_shutting_down = True

        write_crash_log("Shutdown initiated.")

        if self.deactivate_timer:
            self.deactivate_timer.cancel()

        try:
            self.mouse_l.stop()
            self.key_l.stop()
        except Exception as e:
            write_crash_log(f"Error stopping listeners: {e}")

        self.root.after(0, self.root.destroy)

    def speak(self, text):
        def audio_thread():
            with self.tts_lock:
                with self.lock:
                    if self.is_shutting_down:
                        return
                try:
                    alltalk_url = "http://127.0.0.1:7851/api/tts-generate"
                    payload = {"text_input": text, "character_voice_gen": "archer.wav", "autoplay": "true", "autoplay_volume": "0.8"}
                    response = requests.post(alltalk_url, data=payload, timeout=5)
                    if response.status_code != 200: raise ConnectionError("AllTalk Offline")
                except Exception as e:
                    write_crash_log(f"AllTalk TTS unavailable: {e}. Falling back to pyttsx3.")
                    try:
                        engine = pyttsx3.init()
                        voices = engine.getProperty('voices')
                        engine.setProperty('voice', voices[0].id)
                        engine.setProperty('rate', 155)
                        engine.say(text)
                        engine.runAndWait()
                        engine.stop()
                    except Exception as ex:
                        write_crash_log(f"Audio Critical Failure (pyttsx3): {ex}")
        threading.Thread(target=audio_thread, daemon=True).start()

    def send_to_lm_studio(self, text):
        payload = {
            "model": "local-model",
            "messages": [{"role": "system", "content": "You are a helpful assistant."},
                         {"role": "user", "content": text}],
            "stream": False
        }

        for attempt in range(2):
            with self.lock:
                if self.is_shutting_down: return

            try:
                response = requests.post("http://localhost:1234/v1/chat/completions", json=payload, timeout=8)
                if response.status_code == 200:
                    self.speak(response.json()['choices'][0]['message']['content'])
                    return
                else:
                    write_crash_log(f"LM Studio returned status {response.status_code}")
            except Exception as e:
                write_crash_log(f"LM Studio Error (Attempt {attempt+1}): {e}")
                if attempt == 0: time.sleep(1)

        self.speak("LM Studio is unavailable.")

    def capture_audio(self):
        try:
            with self.mic as source:
                # No adjustment here, done at startup
                audio = self.recognizer.listen(source, timeout=7, phrase_time_limit=10)

            with self.lock:
                if self.is_shutting_down: return

            user_text = self.recognizer.recognize_google(audio)
            write_crash_log(f"Recognized: {user_text}")
            self.send_to_lm_studio(user_text)
        except sr.WaitTimeoutError:
            write_crash_log("Listening timed out (no speech detected).")
        except sr.UnknownValueError:
            write_crash_log("Speech recognition could not understand audio.")
        except Exception as e:
            write_crash_log(f"Capture Error: {e}")
        finally:
            self.deactivate()

    def activate(self):
        with self.lock:
            if self.is_shutting_down or self.is_active:
                return
            self.is_active = True
            if self.deactivate_timer:
                self.deactivate_timer.cancel()
                self.deactivate_timer = None
        threading.Thread(target=self.capture_audio, daemon=True).start()

    def deactivate(self):
        with self.lock:
            if not self.is_active or self.is_shutting_down:
                return
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
                with self.lock:
                    if self.deactivate_timer:
                        self.deactivate_timer.cancel()
                    self.deactivate_timer = threading.Timer(0.5, self.deactivate)
                    self.deactivate_timer.start()

    def on_key_press(self, key):
        try:
            k = str(key).replace("'", "")
            self.pressed_keys.add(k)
            if '-' in self.pressed_keys and '=' in self.pressed_keys:
                self.shutdown()
            if 'Key.f1' in self.pressed_keys and 'Key.f2' in self.pressed_keys:
                self.activate()
        except Exception as e:
            write_crash_log(f"Key Press Error: {e}")

    def on_key_release(self, key):
        try:
            k = str(key).replace("'", "")
            if k in self.pressed_keys:
                self.pressed_keys.remove(k)
            if k == 'Key.f1' or k == 'Key.f2':
                self.deactivate()
        except Exception as e:
            write_crash_log(f"Key Release Error: {e}")

    def render_size(self, pil_img, size, brightness=1.0):
        enh = ImageEnhance.Brightness(pil_img)
        img = enh.enhance(brightness)
        return ImageTk.PhotoImage(img.resize((size, size), Image.Resampling.LANCZOS))

    def animate(self):
        with self.lock:
            if self.is_shutting_down:
                return
            active = self.is_active

        if active:
            if not self.use_fallback:
                self.pulse_frame_idx = (self.pulse_frame_idx + 1) % len(self.pulse_frames)
                self.label.config(image=self.pulse_frames[self.pulse_frame_idx])
            else:
                self.label.config(fg="white" if time.time() % 1 > 0.5 else "red")
            self.root.after(35, self.animate)
        else:
            # Reduce CPU usage while idle by checking less frequently
            self.root.after(200, self.animate)

    def start_move(self, event): self.x, self.y = event.x, event.y
    def do_move(self, event):
        x, y = self.root.winfo_x() + (event.x - self.x), self.root.winfo_y() + (event.y - self.y)
        self.root.geometry(f"+{x}+{y}")

if __name__ == "__main__":
    root = tk.Tk()
    app = SidCore(root)
    root.mainloop()