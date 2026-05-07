import importlib
import importlib.util
import json
import os
import pathlib
import platform
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from tkinter import filedialog
from tkinter import messagebox
from tkinter import simpledialog
from tkinter import ttk
from typing import Callable, Optional


APP_DIR = pathlib.Path.home() / ".kokoro_gui"
VENV_DIR = APP_DIR / ".venv"
CONFIG_FILE = APP_DIR / "config.json"

VOICES = ["af_heart", "af_bella", "af_nicole", "bm_george", "bm_daniel"]
LANG_MAP = {"af_": "a", "bm_": "b"}
OS_OPTIONS = ["mac", "linux", "windows"]
AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".ogg"}
DEFAULT_LIBRARY_ROOT = str(pathlib.Path.home() / "KokoroLibrary")
GENERATE_SAVE_NONE_LABEL = "None"
SECTION_ORDER_FILENAME = ".kokoro_section_order.json"


def detect_os():
    sysname = platform.system().lower()
    if "darwin" in sysname:
        return "mac"
    if "windows" in sysname:
        return "windows"
    return "linux"


def venv_python(target_os):
    if target_os == "windows":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def venv_pip(target_os):
    if target_os == "windows":
        return VENV_DIR / "Scripts" / "pip.exe"
    return VENV_DIR / "bin" / "pip"


def default_output_path(fmt):
    return str(pathlib.Path.home() / "Desktop" / f"reading.{fmt}")


def load_config():
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_config(data):
    APP_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(data, indent=2))


def run(cmd, check=True):
    return subprocess.run(cmd, text=True, capture_output=True, check=check)


def stream_run(cmd, log, check=True):
    log(f"$ {' '.join(str(c) for c in cmd)}")
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert process.stdout is not None
    for line in process.stdout:
        line = line.rstrip()
        if line:
            log(line)
    process.wait()
    if check and process.returncode != 0:
        raise subprocess.CalledProcessError(process.returncode, cmd)
    return process.returncode


def ensure_ffmpeg(target_os, log):
    if shutil.which("ffmpeg"):
        log("ffmpeg already installed.")
        return
    if target_os == "mac":
        if shutil.which("brew") is None:
            raise RuntimeError("Homebrew not found. Install from https://brew.sh first.")
        log("Installing ffmpeg via brew...")
        stream_run(["brew", "install", "ffmpeg"], log)
        return
    if target_os == "linux":
        if shutil.which("apt-get"):
            log("Installing ffmpeg via apt (will prompt for sudo)...")
            stream_run(["sudo", "apt-get", "update"], log)
            stream_run(["sudo", "apt-get", "install", "-y", "ffmpeg"], log)
            return
        if shutil.which("dnf"):
            stream_run(["sudo", "dnf", "install", "-y", "ffmpeg"], log)
            return
        if shutil.which("pacman"):
            stream_run(["sudo", "pacman", "-S", "--noconfirm", "ffmpeg"], log)
            return
        raise RuntimeError("ffmpeg not found and no supported package manager.")
    if target_os == "windows":
        if shutil.which("winget"):
            log("Installing ffmpeg via winget...")
            stream_run(["winget", "install", "-e", "--id", "Gyan.FFmpeg"], log)
            return
        raise RuntimeError(
            "ffmpeg not found. Install from https://ffmpeg.org/download.html "
            "and ensure ffmpeg is on PATH."
        )


def ensure_pygame_host(log):
    """PlaybackEngine runs in sys.executable (the GUI process), not ~/.kokoro_gui/.venv."""
    try:
        import pygame  # noqa: F401

        log("pygame OK (GUI interpreter, for Play/Pause).")
        return
    except ImportError:
        pass
    log(
        "Installing pygame for the Python running this window (Play/Pause lives here, "
        "not only the Kokoro venv). This may take a moment…"
    )
    attempts = [
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--user",
            "--break-system-packages",
            "pygame",
        ],
        [sys.executable, "-m", "pip", "install", "--break-system-packages", "pygame"],
        [sys.executable, "-m", "pip", "install", "--user", "pygame"],
        [sys.executable, "-m", "pip", "install", "pygame"],
    ]
    last_err = ""
    for cmd in attempts:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            log("pygame installed for GUI.")
            return
        tail = (result.stderr or result.stdout or "").strip().splitlines()
        last_err = tail[-1] if tail else ""
        if last_err:
            log(last_err)
    raise RuntimeError(
        "Could not pip install pygame into this Python (PEP 668 may block it). "
        "Try one of:\n"
        f"  {sys.executable} -m pip install --user pygame\n"
        f"  sudo apt install python3-pygame\n"
        f"  Or run this app with a venv: python -m venv .venv && .venv/bin/python kokoro_gui.py"
    )


def ensure_env(target_os, log):
    APP_DIR.mkdir(parents=True, exist_ok=True)
    ensure_ffmpeg(target_os, log)
    py_bin = venv_python(target_os)
    pip_bin = venv_pip(target_os)
    if not py_bin.exists():
        log("Creating Python virtual environment...")
        stream_run([sys.executable, "-m", "venv", str(VENV_DIR)], log)
    else:
        log("Python virtual environment already exists.")
    log("Upgrading pip / wheel...")
    stream_run([str(pip_bin), "install", "--upgrade", "pip", "wheel"], log)
    log(
        "Installing packages in Kokoro venv (kokoro, soundfile, numpy, misaki, pypdf)…"
    )
    stream_run(
        [
            str(pip_bin),
            "install",
            "kokoro",
            "soundfile",
            "numpy",
            "misaki",
            "pypdf",
        ],
        log,
    )
    ensure_pygame_host(log)
    log("Environment ready.")


def synth_to_wav(target_os, text, voice, out_wav, log):
    lang = "a"
    for prefix, mapped_lang in LANG_MAP.items():
        if voice.startswith(prefix):
            lang = mapped_lang
            break
    code = (
        "import numpy as np, soundfile as sf\n"
        "from kokoro import KPipeline\n"
        f"pipe = KPipeline(lang_code={lang!r})\n"
        "parts = []\n"
        f"for _, _, audio in pipe({text!r}, voice={voice!r}, speed=1):\n"
        "    parts.append(audio)\n"
        "audio = np.concatenate(parts)\n"
        f"sf.write({str(out_wav)!r}, audio, 24000)\n"
        "print('ok')\n"
    )
    log(f"Synthesizing with voice={voice} lang={lang}...")
    stream_run([str(venv_python(target_os)), "-c", code], log)
    log(f"WAV saved: {out_wav}")


def wav_to_mp3(wav_path, mp3_path, log):
    log("Converting WAV to MP3 with ffmpeg...")
    encode = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-nostats",
            "-loglevel",
            "error",
            "-i",
            str(wav_path),
            str(mp3_path),
        ],
        text=True,
        capture_output=True,
    )
    if encode.returncode != 0:
        err = (encode.stderr or encode.stdout or "ffmpeg failed").strip()
        raise subprocess.CalledProcessError(encode.returncode, encode.args, stderr=err)
    log(f"MP3 saved: {mp3_path}")


def ffmpeg_to_temp_wav(src_path):
    fd, wav_path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    r = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-nostats",
            "-loglevel",
            "error",
            "-i",
            str(src_path),
            str(wav_path),
        ],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        try:
            os.unlink(wav_path)
        except OSError:
            pass
        raise RuntimeError(
            (r.stderr or r.stdout or "ffmpeg decode failed").strip()
        )
    return wav_path


def extract_text_from_file(target_os, file_path):
    path = pathlib.Path(file_path)
    if path.suffix.lower() == ".txt":
        return path.read_text(encoding="utf-8", errors="ignore")
    if path.suffix.lower() == ".pdf":
        code = (
            "from pypdf import PdfReader\n"
            f"reader = PdfReader({str(path)!r})\n"
            "text = []\n"
            "for page in reader.pages:\n"
            "    text.append(page.extract_text() or '')\n"
            "print('\\n'.join(text))\n"
        )
        result = run([str(venv_python(target_os)), "-c", code])
        return result.stdout
    raise RuntimeError("Unsupported file type. Use .txt or .pdf.")


def sanitize_filename(name, max_len=80):
    name = name.strip().replace("\n", " ")
    name = re.sub(r"[^\w\s\-.]", "", name)
    name = name.strip(". ") or "clip"
    if len(name) > max_len:
        name = name[:max_len].rstrip(". ")
    return name or "clip"


def section_order_manifest_path(section_dir: pathlib.Path) -> pathlib.Path:
    return section_dir / SECTION_ORDER_FILENAME


def write_section_manifest(section_dir: pathlib.Path, basenames: list[str]) -> None:
    section_dir.mkdir(parents=True, exist_ok=True)
    mp = section_order_manifest_path(section_dir)
    txt = json.dumps({"tracks": list(basenames)}, indent=2)
    fd, tmp = tempfile.mkstemp(prefix=".kokoro_ord_", suffix=".json", dir=section_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(txt)
        os.replace(tmp, mp)
    except Exception:
        try:
            pathlib.Path(tmp).unlink(missing_ok=True)
        except OSError:
            pass
        raise


def ordered_audio_paths(section_dir: pathlib.Path) -> list[pathlib.Path]:
    if not section_dir.is_dir():
        return []
    by_name: dict[str, pathlib.Path] = {}
    try:
        for p in section_dir.iterdir():
            if p.is_file() and p.suffix.lower() in AUDIO_EXTS:
                by_name[p.name] = p
    except OSError:
        return []
    if not by_name:
        return []
    mp = section_order_manifest_path(section_dir)
    if not mp.is_file():
        return sorted(by_name.values(), key=lambda x: x.name.lower())
    seq_s: list[str] = []
    try:
        raw = json.loads(mp.read_text(encoding="utf-8"))
        lst = raw.get("tracks") if isinstance(raw, dict) else None
        if isinstance(lst, list):
            seq_s = [str(x) for x in lst]
    except Exception:
        seq_s = []
    out: list[pathlib.Path] = []
    seen: set[str] = set()
    for bn in seq_s:
        if bn in by_name and bn not in seen:
            out.append(by_name[bn])
            seen.add(bn)
    tail = sorted(
        (by_name[b] for b in by_name if b not in seen),
        key=lambda x: x.name.lower(),
    )
    out.extend(tail)
    return out


def manifest_remove_basenames(section_dir: pathlib.Path, remove: set[str]) -> None:
    mp = section_order_manifest_path(section_dir)
    if not mp.is_file():
        return
    try:
        raw = json.loads(mp.read_text(encoding="utf-8"))
        lst = raw.get("tracks") if isinstance(raw, dict) else None
        if not isinstance(lst, list):
            return
        keep = [str(x) for x in lst if str(x) not in remove]
        write_section_manifest(section_dir, keep)
    except Exception:
        pass


def ffmpeg_concat_to_mp3(input_paths: list[pathlib.Path], out_mp3: pathlib.Path, log):
    n = len(input_paths)
    if n < 2:
        raise ValueError("concat needs at least 2 inputs")
    log(f"Merging {n} tracks with ffmpeg…")
    cmd: list[str] = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-nostats",
        "-loglevel",
        "error",
    ]
    for p in input_paths:
        cmd.extend(["-i", str(p)])
    norm_chunks = []
    concat_in = ""
    for i in range(n):
        norm_chunks.append(
            f"[{i}:a]aresample=24000,channel_layouts=mono[a{i}]"
        )
        concat_in += f"[a{i}]"
    fc = ";".join(norm_chunks) + f";{concat_in}concat=n={n}:v=0:a=1[out]"
    cmd.extend(
        [
            "-filter_complex",
            fc,
            "-map",
            "[out]",
            "-c:a",
            "libmp3lame",
            "-q:a",
            "4",
            str(out_mp3),
        ]
    )
    r = subprocess.run(cmd, text=True, capture_output=True)
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "ffmpeg concat failed").strip()
        raise subprocess.CalledProcessError(r.returncode, cmd, stderr=err)
    log(f"Merged MP3: {out_mp3}")


@dataclass
class PlaybackEngine:
    on_log: Callable[[str], None]
    on_state: Callable[[str], None]

    def __post_init__(self):
        self._q: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._ready = threading.Event()
        self._failed = threading.Event()
        self._fail_msg = ""
        self._thread.start()

    def _worker(self):
        try:
            os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
            old_cwd = os.getcwd()
            try:
                import pygame

                os.chdir(APP_DIR)

                pygame.mixer.pre_init(44100, -16, 2, 4096)
                pygame.mixer.init()
                pygame.init()
                if not pygame.display.get_init():
                    try:
                        pygame.display.set_mode((1, 1))
                    except Exception:
                        pass

                music_done = pygame.USEREVENT + 42
                try:
                    pygame.mixer.music.set_endevent(music_done)
                except Exception:
                    music_done = -1

                poll_prev_busy = False

                playlist: list[pathlib.Path] = []
                pos = -1
                paused = False
                temp_pcm: Optional[str] = None
                current_play_path: Optional[str] = None

                def scrub_temp():
                    nonlocal temp_pcm
                    if temp_pcm:
                        try:
                            os.unlink(temp_pcm)
                        except OSError:
                            pass
                        temp_pcm = None

                def load_and_play(orig: pathlib.Path) -> None:
                    nonlocal temp_pcm, current_play_path, poll_prev_busy
                    scrub_temp()
                    p = pathlib.Path(orig).expanduser().resolve()
                    if not p.is_file():
                        self.on_log(f"Playback: missing file {p}")
                        self.on_state("idle")
                        return
                    suf = p.suffix.lower()
                    play_path = str(p)
                    if suf == ".wav":
                        try:
                            pygame.mixer.music.load(play_path)
                        except Exception:
                            play_path = ffmpeg_to_temp_wav(p)
                            temp_pcm = play_path
                            pygame.mixer.music.load(play_path)
                    else:
                        try:
                            pygame.mixer.music.load(play_path)
                        except Exception:
                            play_path = ffmpeg_to_temp_wav(p)
                            temp_pcm = play_path
                            pygame.mixer.music.load(play_path)
                    current_play_path = play_path
                    pygame.mixer.music.play()
                    self.on_log(f"Playing: {p.name}")
                    self.on_state("playing")
                    poll_prev_busy = pygame.mixer.music.get_busy()

                def hard_stop_music():
                    nonlocal playlist, pos, paused, current_play_path, poll_prev_busy
                    pygame.mixer.music.stop()
                    scrub_temp()
                    playlist = []
                    pos = -1
                    paused = False
                    current_play_path = None
                    poll_prev_busy = False
                    pygame.event.clear()

                def consume_track_end():
                    nonlocal playlist, pos, current_play_path, poll_prev_busy
                    scrub_temp()
                    if playlist and pos + 1 < len(playlist):
                        pos += 1
                        load_and_play(playlist[pos])
                        return
                    if playlist:
                        self.on_log("Playlist finished.")
                    playlist = []
                    pos = -1
                    current_play_path = None
                    poll_prev_busy = False
                    self.on_state("idle")

                try:
                    self._ready.set()
                    while True:
                        try:
                            item = self._q.get(timeout=0.06)
                            kind = item[0]
                            if kind == "stop":
                                hard_stop_music()
                                self.on_state("idle")
                            elif kind == "pause":
                                if pygame.mixer.music.get_busy():
                                    pygame.mixer.music.pause()
                                    paused = True
                                    self.on_state("paused")
                                elif paused:
                                    pygame.mixer.music.unpause()
                                    paused = False
                                    self.on_state("playing")
                            elif kind == "resume":
                                if paused:
                                    pygame.mixer.music.unpause()
                                    paused = False
                                    self.on_state("playing")
                            elif kind == "play_one":
                                path = pathlib.Path(item[1])
                                hard_stop_music()
                                playlist = [path]
                                pos = 0
                                paused = False
                                load_and_play(path)
                            elif kind == "playlist":
                                paths = [pathlib.Path(p) for p in item[1]]
                                paths = [
                                    p.expanduser().resolve()
                                    for p in paths
                                    if p.expanduser().is_file()
                                ]
                                hard_stop_music()
                                if not paths:
                                    self.on_log("Playlist empty.")
                                    self.on_state("idle")
                                    continue
                                playlist = paths
                                pos = 0
                                paused = False
                                load_and_play(playlist[pos])
                            elif kind == "next":
                                if pygame.mixer.music.get_busy() or paused:
                                    pygame.mixer.music.stop()
                                    pygame.event.clear()
                                    pygame.event.pump()
                                    scrub_temp()
                                if playlist and pos + 1 < len(playlist):
                                    pos += 1
                                    paused = False
                                    load_and_play(playlist[pos])
                                else:
                                    scrub_temp()
                                    self.on_log("Playlist end.")
                                    self.on_state("idle")
                            elif kind == "shutdown":
                                hard_stop_music()
                                break
                            continue
                        except queue.Empty:
                            pass

                        if music_done >= 0:
                            for ev in pygame.event.get():
                                if ev.type != music_done:
                                    continue
                                if paused:
                                    continue
                                consume_track_end()
                        else:
                            pygame.event.pump()
                            busy_now = pygame.mixer.music.get_busy()
                            if (
                                not paused
                                and playlist
                                and 0 <= pos < len(playlist)
                                and poll_prev_busy
                                and not busy_now
                            ):
                                consume_track_end()
                                busy_now = pygame.mixer.music.get_busy()
                            poll_prev_busy = busy_now
                finally:
                    hard_stop_music()
                    pygame.quit()
            finally:
                try:
                    os.chdir(old_cwd)
                except OSError:
                    pass
        except Exception as exc:
            self._fail_msg = str(exc)
            self._failed.set()
            self.on_log(f"Playback engine failed: {exc}")

    def wait_ready(self, timeout=5.0) -> bool:
        if self._failed.is_set():
            return False
        return self._ready.wait(timeout)

    def last_error(self) -> str:
        return self._fail_msg

    def stop(self):
        self._q.put(("stop",))

    def pause_toggle(self):
        self._q.put(("pause",))

    def resume(self):
        self._q.put(("resume",))

    def play_one(self, path):
        self._q.put(("play_one", pathlib.Path(path)))

    def load_playlist(self, paths):
        self._q.put(("playlist", list(paths)))

    def next_track(self):
        self._q.put(("next",))

    def shutdown(self):
        self._q.put(("shutdown",))


def unique_out_path(directory: pathlib.Path, base_name: str, ext: str) -> pathlib.Path:
    safe = sanitize_filename(base_name)
    cand = directory / f"{safe}{ext}"
    if not cand.exists():
        return cand
    stem = cand.stem
    for idx in range(1, 10_000):
        alt = directory / f"{stem}-{idx:03d}{ext}"
        if not alt.exists():
            return alt
    return directory / f"{safe}-{time.time_ns()}{ext}"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Kokoro TTS")
        self.geometry("780x820")
        self.minsize(720, 700)

        cfg = load_config()
        self.target_os = tk.StringVar(value=cfg.get("os", detect_os()))
        self.voice = tk.StringVar(value=cfg.get("voice", "af_heart"))
        self.format_value = tk.StringVar(value=cfg.get("format", "mp3"))
        self.out_path = tk.StringVar(
            value=cfg.get("out_path", default_output_path(self.format_value.get()))
        )
        self.input_path = tk.StringVar(value="")
        self.status = tk.StringVar(value="Pick OS, click Setup, then Generate.")
        self.library_root = tk.StringVar(
            value=cfg.get("library_root", DEFAULT_LIBRARY_ROOT)
        )
        self.last_library_dir = cfg.get(
            "last_library_dir", cfg.get("library_root", DEFAULT_LIBRARY_ROOT)
        )
        save_sec = (cfg.get("generate_save_section") or "").strip()
        if (
            not save_sec
            and cfg.get("output_mode") == "library"
            and isinstance(cfg.get("last_library_dir"), str)
        ):
            try:
                root = pathlib.Path(cfg.get("library_root", DEFAULT_LIBRARY_ROOT)).expanduser().resolve()
                last = pathlib.Path(cfg["last_library_dir"]).expanduser().resolve()
                save_sec = last.relative_to(root).parts[0] if last.is_dir() else ""
            except (ValueError, IndexError, OSError):
                save_sec = ""
        self.generate_save_section = tk.StringVar(
            value=(
                GENERATE_SAVE_NONE_LABEL
                if not save_sec
                else save_sec
            )
        )
        self.clip_name = tk.StringVar(value=cfg.get("clip_name_hint", ""))
        self._last_generated: Optional[pathlib.Path] = None
        self._playback_engine = PlaybackEngine(
            on_log=self.log,
            on_state=self._on_playback_state,
        )
        self._playback_status = tk.StringVar(value="engine…")
        self._build_ui()

    def _on_playback_state(self, state_name: str):
        def bump():
            self._playback_status.set(state_name)

        self.after(0, bump)

    def _restart_playback_engine(self):
        old = self._playback_engine
        if old._thread.is_alive():
            try:
                old.shutdown()
            except Exception:
                pass
            time.sleep(0.15)
        self._playback_engine = PlaybackEngine(
            on_log=self.log,
            on_state=self._on_playback_state,
        )

    def _playback_ready(self) -> bool:
        eng = self._playback_engine
        if eng.wait_ready(timeout=8.0) and not eng._failed.is_set():
            return True

        pygame_missing = importlib.util.find_spec("pygame") is None
        err_text = (eng.last_error() or "").lower()
        pygame_err = pygame_missing or "no module named pygame" in err_text

        if pygame_err:
            try:
                self.log(
                    "Playback uses pygame in the same Python that runs this window "
                    "(the Kokoro venv is separate). Installing pygame here…"
                )
                ensure_pygame_host(self.log)
                importlib.invalidate_caches()
            except RuntimeError as exc:
                messagebox.showwarning("Playback", str(exc))
                return False
            except Exception as exc:
                messagebox.showwarning("Playback", str(exc))
                return False

        if pygame_err or eng._failed.is_set():
            self._restart_playback_engine()

        eng2 = self._playback_engine
        if eng2.wait_ready(timeout=10.0) and not eng2._failed.is_set():
            self._playback_status.set("ready")
            return True

        messagebox.showwarning(
            "Playback",
            f"Playback unavailable: {eng2.last_error() or 'timeout'}\n"
            f"If this persists restart the app, or try: "
            f"{sys.executable} -m pip install --user --break-system-packages pygame",
        )
        return False

    def _global_play_controls(self, parent):
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=(0, 6))
        ttk.Button(row, text="Play", command=self.play_last_or_selected).pack(
            side="left", padx=(0, 6)
        )
        ttk.Button(row, text="Pause", command=self.pause_playback).pack(
            side="left", padx=(0, 6)
        )
        ttk.Button(row, text="Resume", command=self.resume_playback).pack(
            side="left", padx=(0, 6)
        )
        ttk.Button(row, text="Stop", command=self.stop_playback_engine).pack(
            side="left", padx=(0, 6)
        )
        ttk.Button(row, text="Next", command=self.next_playback_track).pack(
            side="left", padx=(0, 6)
        )
        ttk.Label(row, textvariable=self._playback_status).pack(side="left", padx=8)

    def _build_ui(self):
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True)

        generate_tab = ttk.Frame(nb, padding=12)
        library_tab = ttk.Frame(nb, padding=12)
        nb.add(generate_tab, text="Generate")
        nb.add(library_tab, text="Library")

        self._build_generate_tab(generate_tab)
        self._build_library_tab(library_tab)

    def _build_generate_tab(self, frm):
        self._global_play_controls(frm)

        ttk.Label(frm, text="Operating system").pack(anchor="w")
        os_row = ttk.Frame(frm)
        os_row.pack(fill="x", pady=(0, 6))
        for option in OS_OPTIONS:
            ttk.Radiobutton(
                os_row, text=option.capitalize(), variable=self.target_os, value=option
            ).pack(side="left", padx=(0, 12))
        ttk.Button(os_row, text="Save Preference", command=self.save_pref).pack(
            side="right"
        )

        ttk.Label(frm, text="Voice").pack(anchor="w")
        ttk.Combobox(
            frm, textvariable=self.voice, values=VOICES, state="readonly"
        ).pack(fill="x", pady=(0, 10))

        ttk.Label(frm, text="Output format").pack(anchor="w")
        fmt_box = ttk.Combobox(
            frm,
            textvariable=self.format_value,
            values=["wav", "mp3"],
            state="readonly",
        )
        fmt_box.pack(fill="x", pady=(0, 10))
        fmt_box.bind("<<ComboboxSelected>>", self._on_format_change)

        self.dest_holder = ttk.Frame(frm)
        self.dest_holder.pack(fill="x", pady=(0, 10))

        self.quick_frame = ttk.Frame(self.dest_holder)
        ttk.Label(self.quick_frame, text="Output file").pack(anchor="w")
        out_row = ttk.Frame(self.quick_frame)
        out_row.pack(fill="x")
        ttk.Entry(out_row, textvariable=self.out_path).pack(
            side="left", fill="x", expand=True
        )
        ttk.Button(out_row, text="Browse", command=self.pick_file).pack(
            side="left", padx=6
        )

        self.library_frame = ttk.Frame(self.dest_holder)
        ttk.Label(self.library_frame, text="Clip name (optional)").pack(anchor="w")
        ttk.Entry(self.library_frame, textvariable=self.clip_name).pack(
            fill="x", pady=(0, 4)
        )
        hint = (
            "Uses the section chosen in Save to (next to Generate). "
            "Filename uses this name plus a suffix if needed."
        )
        ttk.Label(self.library_frame, text=hint, wraplength=700).pack(anchor="w")

        ttk.Label(frm, text="Optional input file (.txt or .pdf)").pack(anchor="w")
        input_row = ttk.Frame(frm)
        input_row.pack(fill="x", pady=(0, 10))
        ttk.Entry(input_row, textvariable=self.input_path).pack(
            side="left", fill="x", expand=True
        )
        ttk.Button(input_row, text="Pick File", command=self.pick_input_file).pack(
            side="left", padx=6
        )

        ttk.Label(frm, text="Text").pack(anchor="w")
        self.text_box = tk.Text(frm, height=10, wrap="word")
        self.text_box.pack(fill="both", expand=True)

        btns = ttk.Frame(frm)
        btns.pack(fill="x", pady=10)
        ttk.Button(btns, text="Setup", command=self.setup_async).pack(side="left")
        ttk.Button(btns, text="Generate", command=self.gen_async).pack(
            side="left", padx=8
        )
        ttk.Label(btns, text="Save to:").pack(side="left", padx=(16, 4))
        self.save_to_combo = ttk.Combobox(
            btns,
            textvariable=self.generate_save_section,
            width=26,
            state="readonly",
        )
        self.save_to_combo.pack(side="left")
        self.save_to_combo.bind("<<ComboboxSelected>>", self._on_generate_save_selected)

        ttk.Button(btns, text="Save Copy", command=self.save_copy).pack(
            side="left", padx=8
        )
        ttk.Label(btns, textvariable=self.status, wraplength=220).pack(
            side="left", padx=10
        )

        log_hdr = ttk.Frame(frm)
        log_hdr.pack(fill="x", pady=(4, 2))
        ttk.Label(log_hdr, text="Logs").pack(side="left")
        ttk.Button(log_hdr, text="Clear logs", command=self.clear_logs).pack(side="right")
        ttk.Button(log_hdr, text="Copy logs", command=self.copy_logs).pack(
            side="right", padx=(0, 8)
        )

        self.log_box = tk.Text(frm, height=8, wrap="word", state="disabled")
        self.log_box.pack(fill="both", expand=False)
        self._refresh_generate_save_combo()
        self._sync_generate_dest_frames()

    def _library_section_basenames(self):
        root = pathlib.Path(self.library_root.get()).expanduser()
        if not root.is_dir():
            return []
        try:
            return sorted(
                p.name for p in root.iterdir() if p.is_dir()
            )
        except OSError:
            return []

    def _refresh_generate_save_combo(self):
        combo = getattr(self, "save_to_combo", None)
        if combo is None:
            return
        names = self._library_section_basenames()
        vals = [GENERATE_SAVE_NONE_LABEL] + names
        cur = (self.generate_save_section.get() or "").strip()
        if cur not in vals:
            cur = GENERATE_SAVE_NONE_LABEL
            self.generate_save_section.set(cur)
        combo.configure(values=tuple(vals))

    def _on_generate_save_selected(self, _event=None):
        self._sync_generate_dest_frames()

    def _sync_generate_dest_frames(self):
        choice = (self.generate_save_section.get() or "").strip()
        use_quick = not choice or choice == GENERATE_SAVE_NONE_LABEL
        for w in self.dest_holder.winfo_children():
            w.pack_forget()
        if use_quick:
            self.quick_frame.pack(fill="x")
        else:
            self.library_frame.pack(fill="x")

    def _build_library_tab(self, frm):
        self._global_play_controls(frm)

        root_row = ttk.Frame(frm)
        root_row.pack(fill="x", pady=(0, 8))
        ttk.Label(root_row, text="Library root").pack(side="left")
        ttk.Entry(root_row, textvariable=self.library_root, width=48).pack(
            side="left", fill="x", expand=True, padx=6
        )
        ttk.Button(root_row, text="Browse", command=self.pick_library_root).pack(
            side="left", padx=4
        )
        ttk.Button(root_row, text="Open root", command=self.open_library_root).pack(
            side="left"
        )

        panes = ttk.PanedWindow(frm, orient=tk.HORIZONTAL)
        panes.pack(fill="both", expand=True, pady=6)

        left = ttk.Frame(panes)
        right = ttk.Frame(panes)
        panes.add(left, weight=1)
        panes.add(right, weight=2)

        sec_row = ttk.Frame(left)
        sec_row.pack(fill="x")
        ttk.Label(sec_row, text="Sections").pack(side="left")
        ttk.Button(sec_row, text="Refresh", command=self.library_refresh).pack(
            side="right"
        )
        ttk.Button(sec_row, text="New section", command=self.new_section).pack(
            side="right", padx=4
        )
        ttk.Button(sec_row, text="Remove section", command=self.remove_selected_section).pack(
            side="right", padx=4
        )

        self.section_tree = ttk.Treeview(
            left, columns=("path",), show="tree", height=18, selectmode="browse"
        )
        self.section_tree.pack(fill="both", expand=True)
        self.section_tree.bind("<<TreeviewSelect>>", self._on_section_select)

        ttk.Label(right, text="Tracks").pack(anchor="w")

        tracks_row = ttk.Frame(right)
        tracks_row.pack(fill="both", expand=True)

        self.track_list = tk.Listbox(tracks_row, height=16, selectmode=tk.SINGLE)
        self.track_list.pack(side="left", fill="both", expand=True)
        self.track_list.bind("<Double-Button-1>", self._on_track_double)

        track_mv_col = ttk.Frame(tracks_row)
        track_mv_col.pack(side="right", fill="y", padx=(6, 0))
        ttk.Button(track_mv_col, text="Move up", width=10, command=self.move_track_up).pack(
            pady=(0, 4)
        )
        ttk.Button(track_mv_col, text="Move down", width=10, command=self.move_track_down).pack()

        self._track_popup_menu = tk.Menu(self, tearoff=0)
        self._track_popup_menu.add_command(label="Delete track…", command=self.delete_selected_track)

        self.track_list.bind("<Button-3>", self._track_context_menu)
        self.track_list.bind("<Control-Button-1>", self._track_context_menu)

        lib_btns = ttk.Frame(right)
        lib_btns.pack(fill="x", pady=8)
        lib_row1 = ttk.Frame(lib_btns)
        lib_row1.pack(fill="x")
        lib_row2 = ttk.Frame(lib_btns)
        lib_row2.pack(fill="x", pady=(6, 0))
        ttk.Button(
            lib_row1, text="Play selection", command=self.play_library_selection
        ).pack(side="left", padx=(0, 8))
        ttk.Button(
            lib_row1, text="Play section in order", command=self.play_section_order
        ).pack(side="left", padx=(0, 8))
        ttk.Button(lib_row2, text="Remove track", command=self.delete_selected_track).pack(
            side="left", padx=(0, 8)
        )
        ttk.Button(
            lib_row2, text="Merge to MP3…", command=self.merge_section_mp3_async
        ).pack(side="left")

        self.after(100, self.library_refresh)

    def pick_library_root(self):
        path = filedialog.askdirectory(
            initialdir=self.library_root.get() or DEFAULT_LIBRARY_ROOT
        )
        if path:
            self.library_root.set(path)
            self._refresh_generate_save_combo()

    def open_library_root(self):
        root = pathlib.Path(self.library_root.get()).expanduser()
        root.mkdir(parents=True, exist_ok=True)
        try:
            if platform.system().lower().startswith("win"):
                os.startfile(str(root))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(root)])
            else:
                subprocess.Popen(["xdg-open", str(root)])
        except Exception as exc:
            messagebox.showerror("Open folder", str(exc))

    def library_refresh(self):
        for item in self.section_tree.get_children():
            self.section_tree.delete(item)
        root = pathlib.Path(self.library_root.get()).expanduser()
        if not root.is_dir():
            try:
                root.mkdir(parents=True, exist_ok=True)
            except OSError:
                return
        for child in sorted(root.iterdir()):
            if child.is_dir():
                self.section_tree.insert(
                    "", "end", iid=str(child), text=child.name, values=(str(child),)
                )
        self.track_list.delete(0, tk.END)
        self._refresh_generate_save_combo()

    def new_section(self):
        name = simpledialog.askstring("New section", "Section folder name:")
        if not name:
            return
        safe = sanitize_filename(name, max_len=120)
        root = pathlib.Path(self.library_root.get()).expanduser()
        root.mkdir(parents=True, exist_ok=True)
        dest = root / safe
        try:
            dest.mkdir(parents=False, exist_ok=False)
        except FileExistsError:
            messagebox.showinfo("New section", "That section already exists.")
        except OSError as exc:
            messagebox.showerror("New section", str(exc))
            return
        self.library_refresh()
        self.section_tree.selection_set(str(dest))
        self.section_tree.see(str(dest))
        self._load_tracks_for_section(dest)

    def _on_section_select(self, _event=None):
        sel = self.section_tree.selection()
        if not sel:
            return
        path = pathlib.Path(sel[0])
        self._load_tracks_for_section(path)

    def _load_tracks_for_section(self, section_dir: pathlib.Path):
        self.track_list.delete(0, tk.END)
        if not section_dir.is_dir():
            return
        for p in ordered_audio_paths(section_dir):
            self.track_list.insert(tk.END, str(p))

    def _selected_section_path(self) -> Optional[pathlib.Path]:
        sel = self.section_tree.selection()
        if not sel:
            return None
        return pathlib.Path(sel[0])

    def _on_track_double(self, _event=None):
        self.play_library_selection()

    def play_library_selection(self):
        if not self._playback_ready():
            return
        idx = self.track_list.curselection()
        if not idx:
            messagebox.showinfo("Play", "Select a track.")
            return
        path = pathlib.Path(self.track_list.get(idx[0]))
        self._playback_engine.play_one(path)

    def play_section_order(self):
        if not self._playback_ready():
            return
        sec = self._selected_section_path()
        if not sec or not sec.is_dir():
            messagebox.showinfo("Playlist", "Select a section first.")
            return
        paths = [p for p in ordered_audio_paths(sec) if p.is_file()]
        if not paths:
            messagebox.showinfo("Playlist", "No audio files in this section.")
            return
        self._playback_engine.load_playlist(paths)

    def _persist_track_order_from_listbox(self):
        sec = self._selected_section_path()
        if not sec or not sec.is_dir():
            return
        names = [
            pathlib.Path(self.track_list.get(i)).name
            for i in range(self.track_list.size())
        ]
        if not names:
            return
        try:
            write_section_manifest(sec, names)
        except OSError as exc:
            messagebox.showerror("Reorder", str(exc))

    def move_track_up(self):
        sel = self.track_list.curselection()
        if not sel:
            messagebox.showinfo("Reorder", "Select a track first.")
            return
        idx = sel[0]
        if idx <= 0:
            return
        items = list(self.track_list.get(0, tk.END))
        items[idx - 1], items[idx] = items[idx], items[idx - 1]
        self.track_list.delete(0, tk.END)
        for it in items:
            self.track_list.insert(tk.END, it)
        self.track_list.selection_set(idx - 1)
        self._persist_track_order_from_listbox()

    def move_track_down(self):
        sel = self.track_list.curselection()
        if not sel:
            messagebox.showinfo("Reorder", "Select a track first.")
            return
        idx = sel[0]
        n = self.track_list.size()
        if idx >= n - 1:
            return
        items = list(self.track_list.get(0, tk.END))
        items[idx], items[idx + 1] = items[idx + 1], items[idx]
        self.track_list.delete(0, tk.END)
        for it in items:
            self.track_list.insert(tk.END, it)
        self.track_list.selection_set(idx + 1)
        self._persist_track_order_from_listbox()

    def _track_context_menu(self, event):
        idx = self.track_list.nearest(event.y)
        self.track_list.selection_clear(0, tk.END)
        self.track_list.selection_set(idx)
        self._track_popup_menu.tk_popup(event.x_root, event.y_root)

    def delete_selected_track(self):
        idx = self.track_list.curselection()
        if not idx:
            messagebox.showinfo("Delete track", "Select a track.")
            return
        sec = self._selected_section_path()
        if not sec or not sec.is_dir():
            messagebox.showinfo("Delete track", "Select a section first.")
            return
        path = pathlib.Path(self.track_list.get(idx[0])).expanduser().resolve()
        try:
            path.relative_to(sec.resolve())
        except ValueError:
            messagebox.showerror("Delete track", "Invalid path.")
            return
        if not messagebox.askyesno(
            "Delete track",
            "Permanently delete this file from disk?\n\n"
            f"{path.name}",
        ):
            return
        try:
            path.unlink()
            manifest_remove_basenames(sec, {path.name})
        except OSError as exc:
            messagebox.showerror("Delete track", str(exc))
            return
        sid = str(sec)
        self.library_refresh()
        if self.section_tree.exists(sid):
            self.section_tree.selection_set(sid)
            self.section_tree.see(sid)
        self._load_tracks_for_section(sec)

    def remove_selected_section(self):
        sec = self._selected_section_path()
        if not sec or not sec.is_dir():
            messagebox.showinfo("Remove section", "Select a section first.")
            return
        lib_root = pathlib.Path(self.library_root.get()).expanduser().resolve()
        try:
            sec.resolve().relative_to(lib_root)
        except ValueError:
            messagebox.showerror("Remove section", "Invalid section path.")
            return
        if not messagebox.askyesno(
            "Remove section",
            "Permanently delete this entire section folder and ALL files inside?\n\n"
            f"{sec}",
        ):
            return
        try:
            shutil.rmtree(sec)
        except OSError as exc:
            messagebox.showerror("Remove section", str(exc))
            return
        self.library_refresh()
        self.track_list.delete(0, tk.END)

    def merge_section_mp3_async(self):
        sec = self._selected_section_path()
        if not sec or not sec.is_dir():
            messagebox.showinfo("Merge", "Select a section first.")
            return
        paths = [p for p in ordered_audio_paths(sec) if p.is_file()]
        if not paths:
            messagebox.showinfo("Merge", "No audio files in this section.")
            return
        stem = sanitize_filename(sec.name, max_len=60) + "_full"
        dest = filedialog.asksaveasfilename(
            parent=self,
            title="Save merged MP3",
            defaultextension=".mp3",
            initialfile=f"{stem}.mp3",
            filetypes=[("MP3", "*.mp3")],
        )
        if not dest:
            return
        out = pathlib.Path(dest)
        threading.Thread(
            target=self._merge_section_worker,
            kwargs={"paths": paths, "out": out},
            daemon=True,
        ).start()

    def _merge_section_worker(self, paths: list[pathlib.Path], out: pathlib.Path):
        err_msg = ""
        try:
            self.log("Merging section to one MP3…")
            ensure_ffmpeg(self.target_os.get(), self.log)
            if len(paths) == 1:
                shutil.copy2(paths[0], out)
                self.log(f"Copied single track to: {out}")
            else:
                ffmpeg_concat_to_mp3(paths, out, self.log)
            done = str(out)
            self.after(
                0,
                lambda msg=done: messagebox.showinfo(
                    "Merge", f"Saved merged audio:\n{msg}"
                ),
            )
            self.after(0, self.library_refresh)
        except subprocess.CalledProcessError as error:
            detail = getattr(error, "stderr", None) or getattr(error, "stdout", None) or str(error)
            err_msg = (
                detail.strip().splitlines()[-1]
                if detail
                else str(error)
            )[:500]
        except Exception as exc:
            err_msg = str(exc)
        if err_msg:
            self.log(f"Merge error: {err_msg}")
            em = err_msg
            self.after(
                0,
                lambda m=em: messagebox.showerror("Merge failed", m),
            )

    def play_last_or_selected(self):
        if not self._playback_ready():
            return
        if self._last_generated and self._last_generated.is_file():
            self._playback_engine.play_one(self._last_generated)
            return
        out = pathlib.Path(self.out_path.get()).expanduser()
        if out.is_file():
            self._playback_engine.play_one(out)
            return
        messagebox.showinfo("Play", "Generate audio first or select a track on Library.")

    def pause_playback(self):
        if self._playback_ready():
            self._playback_engine.pause_toggle()

    def resume_playback(self):
        if self._playback_ready():
            self._playback_engine.resume()

    def stop_playback_engine(self):
        if self._playback_ready():
            self._playback_engine.stop()

    def next_playback_track(self):
        if self._playback_ready():
            self._playback_engine.next_track()

    def _on_format_change(self, _event=None):
        current = pathlib.Path(self.out_path.get())
        new_ext = "." + self.format_value.get()
        if current.suffix and current.suffix.lower() != new_ext:
            self.out_path.set(str(current.with_suffix(new_ext)))

    def log(self, msg):
        self.after(0, self._log_main, msg)

    def _log_main(self, msg):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", msg + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")
        first_line = msg.splitlines()[0] if msg else ""
        self.status.set(first_line[:120])

    def clear_logs(self):
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", tk.END)
        self.log_box.configure(state="disabled")
        self.status.set("")

    def copy_logs(self):
        self.log_box.configure(state="normal")
        text = self.log_box.get("1.0", tk.END).rstrip()
        self.log_box.configure(state="disabled")
        if not text:
            return
        self.clipboard_clear()
        self.clipboard_append(text)
        self.update_idletasks()
        self.status.set("Logs copied to clipboard.")

    def save_pref(self):
        cfg = load_config()
        save_sec = (self.generate_save_section.get() or "").strip()
        if save_sec == GENERATE_SAVE_NONE_LABEL:
            save_sec = ""
        cfg.update(
            {
                "os": self.target_os.get(),
                "voice": self.voice.get(),
                "format": self.format_value.get(),
                "out_path": self.out_path.get(),
                "library_root": self.library_root.get(),
                "last_library_dir": self.last_library_dir,
                "generate_save_section": save_sec,
                "output_mode": "library" if save_sec else "quick",
                "clip_name_hint": self.clip_name.get(),
            }
        )
        save_config(cfg)
        self.log(f"Preferences saved for {self.target_os.get()}.")

    def pick_file(self):
        ext = self.format_value.get()
        path = filedialog.asksaveasfilename(
            defaultextension=f".{ext}", filetypes=[(ext.upper(), f"*.{ext}")]
        )
        if path:
            self.out_path.set(path)

    def pick_input_file(self):
        path = filedialog.askopenfilename(
            filetypes=[
                ("Text or PDF", "*.txt *.pdf"),
                ("Text", "*.txt"),
                ("PDF", "*.pdf"),
            ]
        )
        if path:
            self.input_path.set(path)

    def setup_async(self):
        threading.Thread(target=self._setup, daemon=True).start()

    def _setup(self):
        try:
            self.log(f"Setting up for {self.target_os.get()}...")
            ensure_env(self.target_os.get(), self.log)
            self.log("Setup complete.")
        except subprocess.CalledProcessError as error:
            details = error.stderr or error.stdout or str(error)
            self.log(
                f"Setup error: {details.strip().splitlines()[-1] if details else error}"
            )
            messagebox.showerror("Setup failed", details or str(error))
        except Exception as error:
            self.log(f"Setup error: {error}")
            messagebox.showerror("Setup failed", str(error))

    def gen_async(self):
        fmt = self.format_value.get().lower()
        ext = "." + fmt
        sel = (self.generate_save_section.get() or "").strip()
        library_section = (
            None if not sel or sel == GENERATE_SAVE_NONE_LABEL else sel
        )
        if library_section is not None:
            lib_root = pathlib.Path(self.library_root.get()).expanduser()
            try:
                lib_root = lib_root.resolve()
            except OSError:
                pass
            lib_root.mkdir(parents=True, exist_ok=True)
            dest = lib_root / library_section
            if not dest.is_dir():
                messagebox.showerror(
                    "Save to",
                    "That section folder is missing under your library root.\n"
                    f"{dest}\n\n"
                    "Open the Library tab, create the section or Refresh.",
                )
                return
            try:
                dest.resolve().relative_to(lib_root)
            except ValueError:
                messagebox.showerror("Save to", "Invalid library section path.")
                return
            self._persist_last_library_dir(str(dest))
            threading.Thread(
                target=self._gen,
                kwargs={"library_folder": dest},
                daemon=True,
            ).start()
            return

        cur = pathlib.Path(self.out_path.get()).expanduser()
        parent = cur.parent if cur.parent.is_dir() else pathlib.Path.home()
        filetypes = [(fmt.upper(), f"*{ext}"), ("Audio", "*.wav *.mp3")]
        picked = filedialog.asksaveasfilename(
            parent=self,
            title="Choose where to save this audio",
            defaultextension=ext,
            initialdir=str(parent),
            initialfile=cur.name if cur.suffix else f"reading{ext}",
            filetypes=filetypes,
        )
        if not picked:
            self.log("Generate canceled (no save path).")
            return
        self.out_path.set(picked)
        threading.Thread(target=self._gen, daemon=True).start()

    def _default_clip_name(self, text: str) -> str:
        hint = self.clip_name.get().strip()
        if hint:
            return hint
        first = text.strip().split("\n", 1)[0].strip()[:60]
        if first:
            return first
        ip = self.input_path.get().strip()
        if ip:
            return pathlib.Path(ip).stem
        return "clip"

    def _gen(self, library_folder: Optional[pathlib.Path] = None):
        text = self.text_box.get("1.0", "end").strip()
        fmt = self.format_value.get().lower()
        ext = "." + fmt
        try:
            self.log("Generating audio...")
            ensure_env(self.target_os.get(), self.log)
            if not text:
                if self.input_path.get().strip():
                    text = extract_text_from_file(
                        self.target_os.get(), self.input_path.get().strip()
                    ).strip()
                    if not text:
                        raise RuntimeError("No text extracted from selected file.")
                    self.log("Loaded text from input file.")
                else:
                    raise RuntimeError("Paste text or select a .txt/.pdf file.")

            if library_folder is not None:
                sec_dir = library_folder.expanduser().resolve()
                sec_dir.mkdir(parents=True, exist_ok=True)
                base_name = self._default_clip_name(text)
                out = unique_out_path(sec_dir, base_name, ext)
            else:
                out = pathlib.Path(self.out_path.get()).expanduser()

            with tempfile.TemporaryDirectory() as tmp_dir:
                wav_tmp = pathlib.Path(tmp_dir) / "tmp.wav"
                synth_to_wav(
                    self.target_os.get(), text, self.voice.get(), wav_tmp, self.log
                )
                out.parent.mkdir(parents=True, exist_ok=True)
                if fmt == "wav":
                    shutil.copy2(wav_tmp, out)
                else:
                    wav_to_mp3(wav_tmp, out, self.log)

            self._last_generated = out
            self.log(f"Done: {out}")
            out_saved = str(out)
            self.after(
                0,
                lambda msg=out_saved: messagebox.showinfo("Success", f"Saved:\n{msg}"),
            )
            self.after(0, self.library_refresh)
            if library_folder is None:
                self.after(
                    0,
                    lambda p=out_saved: self.out_path.set(p),
                )
        except subprocess.CalledProcessError as error:
            details = error.stderr or error.stdout or str(error)
            err_fallback = str(error)
            self.log(f"Generate error: {error}")
            self.after(
                0,
                lambda d=details, fb=err_fallback: messagebox.showerror(
                    "Generate failed",
                    (d or fb).strip(),
                ),
            )
        except Exception as error:
            err_msg = str(error)
            self.log(f"Generate error: {error}")
            self.after(
                0,
                lambda m=err_msg: messagebox.showerror("Generate failed", m),
            )

    def _persist_last_library_dir(self, path: str):
        self.last_library_dir = path
        cfg = load_config()
        cfg["last_library_dir"] = path
        save_config(cfg)

    def save_copy(self):
        src = (
            self._last_generated
            if self._last_generated and self._last_generated.is_file()
            else pathlib.Path(self.out_path.get()).expanduser()
        )
        if not src.is_file():
            messagebox.showwarning("No audio yet", "Generate audio first.")
            return
        ext = src.suffix or ".mp3"
        dest = filedialog.asksaveasfilename(
            defaultextension=ext, filetypes=[("Audio", "*.mp3 *.wav")]
        )
        if dest:
            shutil.copy2(src, dest)
            self.log(f"Copied to: {dest}")

    def destroy(self):
        try:
            self._playback_engine.shutdown()
            time.sleep(0.08)
        except Exception:
            pass
        super().destroy()


if __name__ == "__main__":
    App().mainloop()
