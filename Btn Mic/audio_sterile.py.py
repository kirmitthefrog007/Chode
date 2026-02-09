import os
import sys
from mcp.server.fastmcp import FastMCP

# Initializing with a generic name to prevent persona-bleeding
mcp = FastMCP("SystemAudio")

@mcp.tool()
def play_audio_file(file_path: str) -> str:
    """
    Plays a local audio file using the system default player.
    Args:
        file_path: The full system path to the file.
    """
    # DEBUG LINE: This will show up in your terminal when the tool is used
    print(f">>> TOOL CALLED: play_audio_file with path: {file_path}")

    if not os.path.exists(file_path):
        return "Error: File path does not exist."
    
    try:
        # Using startfile for Windows 10 compatibility
        os.startfile(file_path)
        return f"File {os.path.basename(file_path)} opened successfully."
    except Exception as e:
        return f"System Error: {str(e)}"

@mcp.tool()
def check_interface() -> str:
    """Verifies that the audio tool is connected to the LLM."""
    print(">>> TOOL CALLED: check_interface")
    return "Interface Status: Connected"

if __name__ == "__main__":
    # Standard transport for LM Studio
    # This script runs via stdio; LM Studio manages the connection.
    mcp.run()