import os
import sys
import threading
import time
import queue
import logging
import requests
import numpy as np
import psutil
import tkinter as tk
from pynput import mouse
from faster_whisper import WhisperModel
try:
    from pycaw.pycaw import AudioUtilities, ISimpleAudioVolume
    PYCAW_AVAILABLE = True
except ImportError:
    PYCAW_AVAILABLE = False

# --- CONFIGURATION ---
CONFIG = {
    "MODEL_SIZE": "base", # or "small", "medium"
    "DEVICE": "cuda",     # "cuda" for GPU
    "COMPUTE_TYPE": "float16",
    "LM_STUDIO_URL": "http://localhost:1234/v1/chat/completions",
    "ALLTALK_URL": "http://127.0.0.1:7851/api/tts-generate",
    "VOICE": "archer.wav",
    "PTT_BUTTON": mouse.Button.x1,
    "LOG_FILE": "sid_bridge.log",
    "DUCK_VOLUME": 0.1,
    "VAD_MIN_SILENCE_MS": 250
}

# --- LOGGING SETUP ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(CONFIG["LOG_FILE"]),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# --- HIGH PRIORITY ---
def set_high_priority():
    try:
        p = psutil.Process(os.getpid())
        if sys.platform == 'win32':
            p.nice(psutil.HIGH_PRIORITY_CLASS)
        else:
            p.nice(-10)
        logger.info("Process priority set to HIGH")
    except Exception as e:
        logger.error(f"Failed to set high priority: {e}")

# --- DLL LOADING HELPER (Windows) ---
def load_cuda_dlls():
    if sys.platform == 'win32':
        # Add common CUDA paths to DLL search path if they exist
        cuda_paths = [
            r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.0\bin",
            r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.1\bin",
            r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.2\bin",
            r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.3\bin",
            r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.4\bin",
            r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.5\bin",
            r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.6\bin",
            os.path.join(os.environ.get("USERPROFILE", ""), "AppData", "Local", "Programs", "Python", "Python310", "Lib", "site-packages", "nvidia", "cublas", "bin"),
        ]
        for path in cuda_paths:
            if os.path.exists(path):
                try:
                    os.add_dll_directory(path)
                    logger.info(f"Added DLL directory: {path}")
                except Exception as e:
                    logger.warning(f"Failed to add DLL directory {path}: {e}")

# --- AUDIO DUCKING ---
class AudioController:
    _original_volumes = {}

    @classmethod
    def duck(cls, level):
        if not PYCAW_AVAILABLE: return
        try:
            sessions = AudioUtilities.GetAllSessions()
            for session in sessions:
                if session.Process and session.Process.name().lower() != "python.exe":
                    volume = session._ctl.QueryInterface(ISimpleAudioVolume)
                    # Store original volume if not already stored
                    pid = session.ProcessId
                    if pid not in cls._original_volumes:
                        cls._original_volumes[pid] = volume.GetMasterVolume()
                    volume.SetMasterVolume(level, None)
        except Exception as e:
            logger.error(f"Audio ducking error: {e}")

    @classmethod
    def restore(cls):
        if not PYCAW_AVAILABLE: return
        try:
            sessions = AudioUtilities.GetAllSessions()
            for session in sessions:
                if session.Process and session.ProcessId in cls._original_volumes:
                    volume = session._ctl.QueryInterface(ISimpleAudioVolume)
                    volume.SetMasterVolume(cls._original_volumes[session.ProcessId], None)
            cls._original_volumes.clear()
        except Exception as e:
            logger.error(f"Audio restoration error: {e}")

# --- BRIDGE CORE ---
class SidBridge:
    def __init__(self):
        self.root = tk.Tk()
        self.setup_ui()

        load_cuda_dlls()
        set_high_priority()

        # Check for CUDA
        try:
            import torch
            if torch.cuda.is_available():
                logger.info(f"CUDA is available. GPU: {torch.cuda.get_device_name(0)}")
            else:
                logger.warning("CUDA is NOT available to Torch. Faster-Whisper might still work if it has its own CUDA access.")
        except ImportError:
            logger.info("Torch not installed, skipping CUDA hardware log check.")

        logger.info("Loading Faster-Whisper model...")
        try:
            self.stt_model = WhisperModel(CONFIG["MODEL_SIZE"], device=CONFIG["DEVICE"], compute_type=CONFIG["COMPUTE_TYPE"])
            logger.info("Faster-Whisper model loaded successfully.")
        except Exception as e:
            logger.error(f"Failed to load Faster-Whisper: {e}")
            logger.info("Falling back to CPU...")
            try:
                self.stt_model = WhisperModel(CONFIG["MODEL_SIZE"], device="cpu", compute_type="int8")
            except:
                self.stt_model = None

        self.is_listening = False
        self.is_processing = False

        self.mouse_listener = mouse.Listener(on_click=self.on_click)
        self.mouse_listener.start()

        # Initial health check
        threading.Thread(target=self.health_check, daemon=True).start()

        # Keyboard listener for Soft Kill
        from pynput import keyboard
        self.key_listener = keyboard.Listener(on_press=self.on_key_press, on_release=self.on_key_release)
        self.key_listener.start()

        self.pressed_keys = set()
        logger.info("Bridge initialized and waiting for PTT. Press '-=' to exit.")

    def setup_ui(self):
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.geometry("120x50+20+20")
        self.root.configure(bg='black')

        self.status_label = tk.Label(self.root, text="READY", fg="#00FFC8", bg="black", font=("Courier", 12, "bold"))
        self.status_label.pack(expand=True, fill='both')

        self.hint_label = tk.Label(self.root, text="PTT: MB4 | Exit: -=", fg="grey", bg="black", font=("Courier", 7))
        self.hint_label.pack()

        # Draggable
        self.status_label.bind("<Button-1>", self.start_move)
        self.status_label.bind("<B1-Motion>", self.do_move)

    def start_move(self, event): self.x, self.y = event.x, event.y
    def do_move(self, event):
        x, y = self.root.winfo_x() + (event.x - self.x), self.root.winfo_y() + (event.y - self.y)
        self.root.geometry(f"+{x}+{y}")

    def update_status(self, text, color):
        def _update():
            self.status_label.config(text=text, fg=color)
        self.root.after(0, _update)

    def on_key_press(self, key):
        try:
            k = str(key).replace("'", "")
            self.pressed_keys.add(k)
            if '-' in self.pressed_keys and '=' in self.pressed_keys:
                logger.info("Soft Kill detected. Exiting...")
                self.root.quit()
                os._exit(0)
        except: pass

    def on_key_release(self, key):
        try:
            k = str(key).replace("'", "")
            if k in self.pressed_keys: self.pressed_keys.remove(k)
        except: pass

    def on_click(self, x, y, button, pressed):
        if button == CONFIG["PTT_BUTTON"]:
            if pressed:
                self.start_listening()
            else:
                self.stop_listening()

    def start_listening(self):
        if not self.is_listening and not self.is_processing:
            logger.info("PTT Pressed: Listening...")
            self.is_listening = True
            self.update_status("LISTENING", "#FF00FF")
            AudioController.duck(CONFIG["DUCK_VOLUME"])
            threading.Thread(target=self.record_loop, daemon=True).start()

    def stop_listening(self):
        if self.is_listening:
            logger.info("PTT Released.")
            self.is_listening = False

    def record_loop(self):
        try:
            import pyaudio
            pa = pyaudio.PyAudio()

            # Match settings for faster-whisper
            stream = pa.open(format=pyaudio.paInt16,
                             channels=1,
                             rate=16000,
                             input=True,
                             frames_per_buffer=1024)

            frames = []
            while self.is_listening:
                data = stream.read(1024, exception_on_overflow=False)
                frames.append(data)

            stream.stop_stream()
            stream.close()
            pa.terminate()

            if frames:
                self.process_audio(frames)
            else:
                self.reset_state()

        except Exception as e:
            logger.error(f"Recording error: {e}")
            self.reset_state()

    def process_audio(self, frames):
        self.is_processing = True
        self.update_status("THINKING", "#00D9FF")

        try:
            audio_bytes = b''.join(frames)
            audio_np = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0

            logger.info("Transcribing...")
            segments, _ = self.stt_model.transcribe(
                audio_np,
                beam_size=1,
                vad_filter=True,
                vad_parameters=dict(min_silence_duration_ms=CONFIG["VAD_MIN_SILENCE_MS"])
            )

            transcript = "".join([s.text for s in segments]).strip()
            logger.info(f"Transcript: {transcript}")

            if transcript:
                self.query_llm(transcript)
            else:
                logger.info("No speech detected.")
                self.reset_state()

        except Exception as e:
            logger.error(f"Processing error: {e}")
            self.reset_state()

    def query_llm(self, text):
        try:
            logger.info("Sending to LM Studio...")
            payload = {
                "model": "local-model",
                "messages": [
                    {"role": "system", "content": "You are a fast, concise AI assistant."},
                    {"role": "user", "content": text}
                ],
                "stream": False
            }
            response = requests.post(CONFIG["LM_STUDIO_URL"], json=payload, timeout=30)
            if response.status_code == 200:
                answer = response.json()['choices'][0]['message']['content']
                logger.info(f"LLM Response: {answer}")
                self.generate_speech(answer)
            else:
                logger.error(f"LM Studio error: {response.status_code}")
                self.reset_state()
        except Exception as e:
            logger.error(f"LLM Query failed: {e}")
            self.reset_state()

    def generate_speech(self, text):
        self.update_status("SPEAKING", "#FF0000")
        try:
            logger.info("Sending to AllTalk...")
            # Multipart Form-Data
            files = {
                'text_input': (None, text),
                'character_voice_gen': (None, CONFIG["VOICE"]),
                'autoplay': (None, 'true'),
                'autoplay_volume': (None, '1.0'),
                'output_file': (None, 'output.wav')
            }
            response = requests.post(CONFIG["ALLTALK_URL"], files=files, timeout=60)
            if response.status_code != 200:
                logger.error(f"AllTalk error: {response.status_code} - {response.text}")
        except Exception as e:
            logger.error(f"TTS failed: {e}")

        self.reset_state()

    def reset_state(self):
        self.is_listening = False
        self.is_processing = False
        AudioController.restore()
        self.update_status("READY", "#00FFC8")

    def health_check(self):
        logger.info("Performing API health checks...")
        # LM Studio
        try:
            requests.get(CONFIG["LM_STUDIO_URL"].replace("/v1/chat/completions", ""), timeout=2)
            logger.info("LM Studio API: Online")
        except:
            logger.warning("LM Studio API: Offline or Unreachable")

        # AllTalk
        try:
            # Simple check if port is open or basic GET works
            requests.get(CONFIG["ALLTALK_URL"].split("/api")[0], timeout=2)
            logger.info("AllTalk API: Online")
        except:
            logger.warning("AllTalk API: Offline or Unreachable")

    def run(self):
        self.root.mainloop()

if __name__ == "__main__":
    bridge = SidBridge()
    bridge.run()
