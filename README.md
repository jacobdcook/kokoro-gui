# Kokoro TTS GUI

Small desktop app for [Kokoro](https://github.com/hexgrad/kokoro-82M) text to speech: Tkinter UI, local library folders, optional ffmpeg MP3 export, section playlists, and merge to one MP3.

Works on **macOS**, **Windows**, and **Linux** (pick your OS inside the app).

## Requirements

| Piece | Notes |
|--------|------|
| **Python** | 3.10 or newer (3.11 or 3.12 recommended) |
| **Tkinter** | Usually included with Python on macOS and Windows. On Linux you may need **`python3-tk`** (Debian/Ubuntu: `sudo apt install python3-tk`) |
| **ffmpeg** | Required for MP3 and some playback paths. The app can try to install it (Homebrew on Mac, common package managers on Linux, winget on Windows). You can also install manually and ensure `ffmpeg` is on your `PATH`. |
| **Internet** | First run downloads models and Python packages into a private venv |

## Quick start

1. Clone this repo.
2. Install pygame into the **same** Python you will use to run the app (needed for Play in the GUI):

   ```bash
   python3 -m pip install -r requirements-gui.txt
   ```

   On Linux, if pip is locked down (PEP 668), try `--user`, a project venv, or your distro pygame package (for example `sudo apt install python3-pygame` on Debian/Ubuntu).

3. Start the app from the repo folder:

   ```bash
   python3 kokoro_gui.py
   ```

4. In the app, choose **mac**, **linux**, or **windows** to match your machine, then click **Setup**. That creates `~/.kokoro_gui/.venv`, installs Kokoro and related packages, and verifies ffmpeg. It can take several minutes the first time.

5. Use **Generate** for TTS output, **Library** for sections and tracks, **Setup** again if you switch machines or need to repair the venv.

## Where data lives

| Path | Purpose |
|------|--------|
| `~/.kokoro_gui/.venv` | Kokoro and synthesis dependencies (created by Setup) |
| `~/.kokoro_gui/config.json` | Saved UI preferences |
| `~/KokoroLibrary` (default) | Library sections and audio clips (configurable in the app) |

Nothing in this repo is written with your personal paths: the app uses your home directory at runtime.

## Optional: Hugging Face token

If you see rate limit warnings when pulling models, create a token at [Hugging Face settings](https://huggingface.co/settings/tokens) and set:

```bash
export HF_TOKEN=your_token_here
```

macOS (Terminal): add the `export` line to `~/.zshrc` if you want it permanent.

## Troubleshooting

| Issue | What to try |
|-------|-------------|
| `No module named tkinter` | Install `python3-tk` (Linux) or reinstall Python with Tcl/Tk (Windows). |
| Play button does nothing | Install pygame for the Python running the app (see Quick start). |
| ffmpeg errors | Install ffmpeg and ensure it is on PATH, or allow the app to install it when prompted. |
| Very long pip logs on Setup | Normal on first run; use **Copy logs** in the UI if you need to share errors. |

## License

No license file is included; treat as source shared between friends unless the author adds one.