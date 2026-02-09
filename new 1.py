import subprocess
import os
import time
import psutil
import webbrowser
import threading
import tkinter as tk
import ctypes
import socket
import traceback

# --- CONFIGURATION ---
def get_git_bash():
    """Attempts to find Git Bash in common installation paths."""
    standard_paths = [
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
        os.path.expanduser(r"~\AppData\Local\Programs\Git\bin\bash.exe")
    ]
    for path in standard_paths:
        if os.path.exists(path):
            return path
    return "bash.exe" # Fallback to system PATH

PATHS = {
    "GIT_BASH": get_git_bash(),
    "ALLTALK": r"C:\AllTalkTTS\start_alltalk.sh",
    "LM_STUDIO": r"C:\Users\Yoda\AppData\Local\Programs\LM Studio\LM Studio.exe",
    "SILLY_TAVERN": r"C:\Users\Yoda\SillyTavern\Start.bat"
}

URL_ALLTALK = "http://127.0.0.1:7851?__theme=dark"
ALLTALK_PORT = 7851
DESKTOP = os.path.join(os.path.expanduser("~"), "Desktop")
LOG_PATH = os.path.join(DESKTOP, "LGR_BOOT_DIAGNOSTICS.txt")
ALLTALK_LOG = os.path.join(DESKTOP, "ALLTALK_OUTPUT.log")

# --- THEME ---
BG_COLOR, PATTERN_COLOR, BORDER_COLOR = "#0D0221", "#261447", "#00FFC8"
TEXT_MAIN, TEXT_ACCENT = "#FF00FF", "#00D9FF"

def log_event(source, message, is_error=False):
    timestamp = time.strftime("%H:%M:%S")
    tag = "[ERROR]" if is_error else "[INFO]"
    try:
        with open(LOG_PATH, "a") as f:
            f.write(f"{timestamp} {tag.ljust(7)} | {source.ljust(10)} | {message}\n")
    except: pass

class RetroBootGUI:
    def __init__(self):
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.geometry("262x120")
        self.root.attributes("-topmost", True)
        self.root.configure(bg=BG_COLOR)
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.root.geometry(f"+{(sw-262)//2}+{(sh-120)//2}")
        
        self.container = tk.Frame(self.root, bg=BG_COLOR, highlightbackground=BORDER_COLOR, highlightthickness=2)
        self.container.place(relx=0.05, rely=0.05, relwidth=0.9, relheight=0.9)
        
        tk.Label(self.container, text="NEON BOOT v2.2", font=("Courier", 10, "bold"), fg=BORDER_COLOR, bg=BG_COLOR).pack(pady=5)
        self.items = {
            "ALLTALK": self.create_label("Voice: STANDBY"),
            "LM_STUDIO": self.create_label("LLM: STANDBY"),
            "SILLY": self.create_label("Silly: STANDBY"),
            "STATUS": self.create_label("Net: STANDBY")
        }

    def create_label(self, text):
        lbl = tk.Label(self.container, text=text, font=("Courier", 8, "bold"), fg=TEXT_MAIN, bg=BG_COLOR)
        lbl.pack(anchor="center")
        return lbl

    def update_status(self, key, text, color=TEXT_MAIN):
        self.items[key].config(text=text, fg=color)
        self.root.update()

def kill_processes():
    targets = ["node.exe", "python.exe"]
    curr_pid = os.getpid()
    for proc in psutil.process_iter(['pid', 'name']):
        try:
            if proc.info['name'] in targets and proc.info['pid'] != curr_pid:
                proc.kill()
        except: continue

def wait_for_port(port, timeout=90): # Increased timeout for model loading
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1.0):
                return True
        except:
            time.sleep(2)
    return False

def boot_logic(gui):
    with open(LOG_PATH, "w") as f: f.write(f"--- DIAGNOSTIC RUN: {time.ctime()} ---\n")
    
    try:
        kill_processes()
        
        # 1. Start AllTalk with explicit Shell Login
        gui.update_status("ALLTALK", "Voice: STARTING...", TEXT_ACCENT)
        log_event("ALLTALK", f"Using Bash: {PATHS['GIT_BASH']}")
        
        if not os.path.exists(PATHS["ALLTALK"]):
            log_event("ALLTALK", f"Script missing: {PATHS['ALLTALK']}", True)
            gui.update_status("ALLTALK", "ERR: SH MISSING", BORDER_COLOR)
        else:
            # Use --login to ensure the .sh script sees the environment variables
            # Convert path to use forward slashes for Bash compatibility
            bash_script_path = PATHS["ALLTALK"].replace("\\", "/")
            # Redirecting stdout/stderr to a file to prevent buffer-fill hangs and allow debugging
            out_file = open(ALLTALK_LOG, "w")
            proc = subprocess.Popen(
                [PATHS["GIT_BASH"], "--login", "-c", f'"{bash_script_path}"'],
                cwd=os.path.dirname(PATHS["ALLTALK"]),
                creationflags=subprocess.CREATE_NO_WINDOW,
                stdout=out_file,
                stderr=out_file,
                text=True,
                bufsize=1 # Line buffered
            )
            log_event("ALLTALK", "Subprocess spawned")

        # 2. Start LM Studio
        if os.path.exists(PATHS["LM_STUDIO"]):
            os.startfile(PATHS["LM_STUDIO"])
            gui.update_status("LM_STUDIO", "LLM: ACTIVE")
        else:
            log_event("LM_STUDIO", "Path not found", True)

        # 3. Start SillyTavern
        if os.path.exists(PATHS["SILLY_TAVERN"]):
            subprocess.Popen([PATHS["SILLY_TAVERN"]], 
                             cwd=os.path.dirname(PATHS["SILLY_TAVERN"]), 
                             creationflags=subprocess.CREATE_NEW_CONSOLE, shell=True)
            gui.update_status("SILLY", "Silly: ACTIVE")

        # 4. Network Verification
        gui.update_status("STATUS", "Net: SCANNING...", TEXT_ACCENT)
        if wait_for_port(ALLTALK_PORT):
            gui.update_status("ALLTALK", "Voice: READY")
            webbrowser.open_new_tab(URL_ALLTALK)
            gui.update_status("STATUS", "Net: ONLINE")
        else:
            gui.update_status("ALLTALK", "Voice: TIMEOUT", BORDER_COLOR)
            log_event("NET", f"Port {ALLTALK_PORT} failed to open within 90s.", True)

    except Exception as e:
        log_event("CRASH", traceback.format_exc(), True)
        gui.update_status("STATUS", "CRITICAL ERROR", "red")
    
    time.sleep(3)
    gui.root.withdraw()

if __name__ == "__main__":
    hWnd = ctypes.WinDLL('kernel32').GetConsoleWindow()
    if hWnd: ctypes.WinDLL('user32').ShowWindow(hWnd, 0)
    gui = RetroBootGUI()
    threading.Thread(target=boot_logic, args=(gui,), daemon=True).start()
    gui.root.mainloop()