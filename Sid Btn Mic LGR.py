import tkinter as tk
import os
import sys
import time
import traceback
import ctypes
import requests
import threading
from PIL import Image, ImageTk, ImageEnhance
from pynput import mouse, keyboard
import speech_recognition as sr
import pyttsx3

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
            self.is_active = False
            self.permanent_kill = False
            self.pulse_scale = 0
            self.pulse_direction = 1
            self.pressed_keys = set()

            # Note: Setting suppress=True here would block ALL mouse input (including move/click)
            # instead of just the side buttons. We use suppress=False and let the
            # win32_event_filter handle selective suppression by returning False.
            self.mouse_l = mouse.Listener(
                on_click=self.on_mouse_click,
                suppress=False,
                win32_event_filter=self.win32_event_filter
            )
            self.mouse_l.start()
            self.key_l = keyboard.Listener(on_press=self.on_key_press, on_release=self.on_key_release)
            self.key_l.start()

            self.label.bind("<Button-1>", self.start_move)
            self.label.bind("<B1-Motion>", self.do_move)
            self.animate()
            
        except Exception:
            write_crash_log(traceback.format_exc())

    def speak(self, text):
        def audio_thread():
            try:
                alltalk_url = "http://127.0.0.1:7851/api/tts-generate"
                payload = {"text_input": text, "character_voice_gen": "archer.wav", "autoplay": "true", "autoplay_volume": "0.8"}
                response = requests.post(alltalk_url, data=payload, timeout=2)
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

    def win32_event_filter(self, msg, data):
        # Suppress Button.x1 (Back) and Button.x2 (Forward) to stop browser navigation interference
        # WM_XBUTTONDOWN = 523, WM_XBUTTONUP = 524
        if msg in (523, 524):
            xbutton = data.mouseData >> 16
            if xbutton in (1, 2):
                return False
        return True

    def send_to_lm_studio(self, text):
        try:
            payload = {
                "model": "local-model",
                # PERSONA REMOVED: Now using a standard assistant role
                "messages": [{"role": "system", "content": "You are a helpful assistant."},
                             {"role": "user", "content": text}],
                "stream": False
            }
            # KEEPING PORT AT 1234 AS REQUESTED
            response = requests.post("http://localhost:1234/v1/chat/completions", json=payload, timeout=15)
            if response.status_code == 200:
                self.speak(response.json()['choices'][0]['message']['content'])
        except Exception as e:
            write_crash_log(f"LM Studio Comm Error: {e}")

    def capture_audio(self):
        try:
            with self.mic as source:
                # Always-on listening with dynamic threshold to ignore background noise
                self.recognizer.adjust_for_ambient_noise(source, duration=0.5)
                self.recognizer.dynamic_energy_threshold = True

                while not self.permanent_kill:
                    try:
                        # Infinite loop: no timeout or phrase time limit
                        audio = self.recognizer.listen(source, timeout=None, phrase_time_limit=None)
                        if self.permanent_kill: break

                        user_text = self.recognizer.recognize_google(audio)
                        if user_text:
                            self.send_to_lm_studio(user_text)
                    except sr.UnknownValueError:
                        continue # Keep listening
                    except sr.RequestError:
                        break
                    except Exception:
                        break
        except Exception:
            write_crash_log(traceback.format_exc())
        finally:
            self.deactivate()

    def activate(self):
        if not self.is_active:
            self.is_active = True
            threading.Thread(target=self.capture_audio, daemon=True).start()

    def trigger_permanent_kill(self):
        self.permanent_kill = True
        self.is_active = False

        # UI updates must be on the main thread
        def final_kill():
            try:
                self.label.config(text="KILL", fg="red", image='')
                self.root.update()
                time.sleep(0.5) # Allow user to see the kill state before termination
            except: pass
            os._exit(0)

        self.root.after(0, final_kill)

    def deactivate(self):
        if self.is_active:
            self.is_active = False
            if not self.use_fallback:
                self.root.after(0, lambda: self.label.config(image=self.img_ready))
            else:
                self.root.after(0, lambda: self.label.config(fg="red"))

    def on_mouse_click(self, x, y, button, pressed):
        if button == mouse.Button.x1:
            if pressed:
                if self.is_active:
                    self.trigger_permanent_kill()
                else:
                    self.activate()

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