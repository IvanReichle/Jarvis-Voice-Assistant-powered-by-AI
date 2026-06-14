"""
jarvis.py — Asistente de voz con:
  · config.json para rutas de apps y ajustes
  · Wake word local con openWakeWord (sin API key, sin cuenta)
  · Memoria larga persistente en jarvis_memoria.json
  · Fallback: si openWakeWord no disponible, usa detección por texto (Whisper)

Instalación de dependencias:
    pip install -r requirements.txt
    # o individualmente:
    # pip install openwakeword sounddevice numpy groq elevenlabs pygame requests python-dotenv pyttsx3
"""

import sys

# Forzar UTF-8 en stdout/stderr para que los emojis de los logs (🎙️, ✅, ⚠️…)
# no provoquen UnicodeEncodeError cuando la salida está redirigida, se ejecuta
# con pythonw.exe o la consola usa una code page heredada (cp1252/cp850).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from dotenv import load_dotenv
load_dotenv()

import webbrowser
import sounddevice as sd
import numpy as np
import io
import wave
import os
import json
import ctypes
import pygame
import threading
import time
import subprocess
import re
import tempfile
from collections import deque
from datetime import datetime
from urllib.parse import quote_plus
from groq import Groq
from elevenlabs.client import ElevenLabs
from elevenlabs import VoiceSettings
from http.server import HTTPServer, BaseHTTPRequestHandler
import math
import queue as _queue_module
import tkinter as tk
try:
    from PIL import Image, ImageTk
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False

# ──────────────────────────────────────────────
#  CONFIG
# ──────────────────────────────────────────────

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "jarvis_config.json")
MEMORIA_PATH = os.path.join(BASE_DIR, "jarvis_memoria.json")


def _cargar_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        raise SystemExit(f"No encontré {CONFIG_PATH}. Créalo o cópialo del repositorio.")
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


CFG = _cargar_config()

# ──────────────────────────────────────────────
#  CREDENCIALES
# ──────────────────────────────────────────────

GROQ_API_KEY        = os.environ.get("GROQ_API_KEY")
ELEVENLABS_API_KEY  = os.environ.get("ELEVENLABS_API_KEY")
# openWakeWord no necesita API key

if not GROQ_API_KEY or not ELEVENLABS_API_KEY:
    raise SystemExit("Define GROQ_API_KEY y ELEVENLABS_API_KEY como variables de entorno.")

APP_PATHS       = CFG.get("app_paths", {})
VOICE_CFG       = CFG.get("voice", {})
MEMORY_CFG      = CFG.get("memory", {})
ACTIVACIONES_FB = CFG.get("wake_word", {}).get(
    "activaciones_fallback",
    ["jarvis", "harvees", "harvis", "jarbes", "harvey"]
)

ELEVENLABS_VOICE_ID = VOICE_CFG.get("elevenlabs_voice_id", "onwK4e9ZLuTAKqWW03F9")
print(f"🎙️  Voice ID cargado: {ELEVENLABS_VOICE_ID}")
HISTORIAL_MAX       = MEMORY_CFG.get("historial_reciente", 10)
MEMORIA_LARGA_MAX   = MEMORY_CFG.get("memoria_larga_max", 100)

# ──────────────────────────────────────────────
#  MEMORIA LARGA PERSISTENTE
# ──────────────────────────────────────────────

def _cargar_memoria() -> list:
    if os.path.exists(MEMORIA_PATH):
        try:
            with open(MEMORIA_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return []


def _guardar_memoria(memoria: list):
    try:
        with open(MEMORIA_PATH, "w", encoding="utf-8") as f:
            json.dump(memoria[-MEMORIA_LARGA_MAX:], f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[Memoria] Error al guardar: {e}")


# Historial en RAM (últimas N interacciones para el contexto de Groq)
historial_reciente: deque = deque(maxlen=HISTORIAL_MAX * 2)

# Memoria larga en disco (todas las conversaciones)
memoria_larga: list = _cargar_memoria()


def registrar_conversacion(usuario: str, asistente: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    historial_reciente.append({"role": "user",      "content": usuario})
    historial_reciente.append({"role": "assistant", "content": asistente})
    memoria_larga.append({"ts": ts, "user": usuario, "jarvis": asistente})
    _guardar_memoria(memoria_larga)


def _resumen_memoria() -> str:
    """Genera un mini-resumen de las últimas conversaciones para el system prompt."""
    if not memoria_larga:
        return ""
    ultimas = memoria_larga[-10:]
    lineas = [f"[{e['ts']}] Tú: {e['user']} → Jarvis: {e['jarvis']}" for e in ultimas]
    return "Conversaciones recientes:\n" + "\n".join(lineas)

# ──────────────────────────────────────────────
#  CLIENTES IA
# ──────────────────────────────────────────────

groq_client  = Groq(api_key=GROQ_API_KEY)
eleven_client = ElevenLabs(api_key=ELEVENLABS_API_KEY)

KEYEVENTF_KEYUP = 0x0002

def _build_system_prompt() -> str:
    """Construye el system prompt incluyendo el perfil del usuario si está en config."""
    perfil = CFG.get("perfil_usuario", {})
    perfil_txt = ""
    if perfil:
        partes = []
        if perfil.get("nombre"):
            partes.append(f"El usuario se llama {perfil['nombre']}.")
        if perfil.get("juegos"):
            partes.append(f"Sus juegos favoritos: {', '.join(perfil['juegos'])}.")
        if perfil.get("preferencias"):
            partes.append(f"Preferencias: {', '.join(perfil['preferencias'])}.")
        if perfil.get("notas"):
            partes.append(perfil["notas"])
        if partes:
            perfil_txt = "\nPerfil del usuario:\n" + "\n".join(partes)
    return (
        "Eres JARVIS, asistente de IA personal. Sé directo, eficiente e inteligente. "
        "Humor seco e ironía ligera cuando encaje, nunca grosero. "
        "Responde SIEMPRE en español, en máximo 2 o 3 frases. "
        "Llama al usuario 'Maestro' de forma natural cuando encaje. "
        "Prioriza ser útil y preciso. Usa el historial para mantener coherencia."
        + perfil_txt + "\n\n{memoria}"
    )

JARVIS_SYSTEM_PROMPT = _build_system_prompt()

# ──────────────────────────────────────────────
#  SSE (interfaz web)
# ──────────────────────────────────────────────

sse_clients = []
_sse_lock   = threading.Lock()


class SSEHandler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass

    def do_GET(self):
        if self.path == "/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            with _sse_lock:
                sse_clients.append(self)
            try:
                while True:
                    time.sleep(1)
            except Exception:
                with _sse_lock:
                    if self in sse_clients:
                        sse_clients.remove(self)


def notify(state: str):
    with _sse_lock:
        clientes = list(sse_clients)
    dead = []
    for c in clientes:
        try:
            c.wfile.write(f"data: {state}\n\n".encode())
            c.wfile.flush()
        except Exception:
            dead.append(c)
    if dead:
        with _sse_lock:
            for d in dead:
                if d in sse_clients:
                    sse_clients.remove(d)


def _iniciar_sse_server():
    try:
        HTTPServer(("localhost", 5050), SSEHandler).serve_forever()
    except Exception as e:
        print(f"⚠️  Servidor SSE no disponible (puerto 5050): {e}")


threading.Thread(target=_iniciar_sse_server, daemon=True).start()

# ──────────────────────────────────────────────
#  GUI QUEUE
# ──────────────────────────────────────────────
_gui_queue = _queue_module.Queue()

def _gui_log(tipo: str, texto: str):
    _gui_queue.put(("log", tipo, texto))

def _gui_estado(estado: str):
    _gui_queue.put(("estado", estado))

# ──────────────────────────────────────────────
#  VOLUMEN
# ──────────────────────────────────────────────

def _pulsar_tecla(codigo, veces=5):
    for _ in range(veces):
        ctypes.windll.user32.keybd_event(codigo, 0, 0, 0)
        ctypes.windll.user32.keybd_event(codigo, 0, KEYEVENTF_KEYUP, 0)


def subir_volumen():   _pulsar_tecla(0xAF)
def bajar_volumen():   _pulsar_tecla(0xAE)
def silenciar():       _pulsar_tecla(0xAD, veces=1)

# ──────────────────────────────────────────────
#  AUDIO
# ──────────────────────────────────────────────

FS              = 16000
CHUNK_MS        = 50
SILENCIO_SEG    = 0.9
MAX_GRAB_SEG    = 30
MAX_ESPERA_SEG  = 45


def _rms(chunk):
    return float(np.sqrt(np.mean((chunk.astype(np.float32) / 32768.0) ** 2)))


def _calibrar_umbral(stream, chunk_samples, muestras=4):
    niveles = [_rms(stream.read(chunk_samples)[0].flatten()) for _ in range(muestras)]
    return max(float(np.mean(niveles)) * 2.5, 0.006)


def escuchar(max_espera: float = MAX_ESPERA_SEG, rapida: bool = False) -> str:
    """Graba hasta que hay silencio o timeout. Devuelve texto transcrito.
    rapida=True: calibración de 1 muestra (50 ms) para capturar comandos inmediatos."""
    _gui_estado("escuchando")
    print("🎙️  Escuchando...")
    chunk_samples      = int(FS * CHUNK_MS / 1000)
    max_silencio_ch    = int(SILENCIO_SEG * 1000 / CHUNK_MS)
    max_total_ch       = int(MAX_GRAB_SEG  * 1000 / CHUNK_MS)
    max_espera_ch      = int(max_espera * 1000 / CHUNK_MS)

    frames, hablando, silencio_ch, espera_ch = [], False, 0, 0

    _oww_pausado.set()   # pausar OWW para evitar dos streams de mic simultáneos
    try:
        with sd.InputStream(samplerate=FS, channels=1, dtype="int16",
                            blocksize=chunk_samples) as stream:
            umbral = _calibrar_umbral(stream, chunk_samples, muestras=1 if rapida else 4)
            while True:
                chunk = stream.read(chunk_samples)[0].flatten()
                nivel = _rms(chunk)
                if not hablando:
                    if nivel > umbral:
                        hablando, silencio_ch = True, 0
                        frames.append(chunk)
                    else:
                        espera_ch += 1
                        if espera_ch > max_espera_ch:
                            return ""
                else:
                    frames.append(chunk)
                    if nivel < umbral:
                        silencio_ch += 1
                        if silencio_ch >= max_silencio_ch:
                            break
                    else:
                        silencio_ch = 0
                    if len(frames) > max_total_ch:
                        break
    finally:
        _oww_pausado.clear()  # reanudar OWW

    if not frames:
        return ""

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1); wf.setsampwidth(2)
        wf.setframerate(FS); wf.writeframes(np.concatenate(frames).tobytes())
    buf.seek(0)

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp_path = tmp.name
        tmp.write(buf.read())
    try:
        with open(tmp_path, "rb") as f:
            tr = groq_client.audio.transcriptions.create(
                file=("audio.wav", f),
                model="whisper-large-v3-turbo",
                language="es",
                prompt="Jarvis, abre Steam, reproduce Spotify, busca en Google, pon música, dime la hora, abre Chrome, qué hora es, abre Discord, sube el volumen, baja el volumen",
            )
    finally:
        os.remove(tmp_path)

    texto = tr.text.strip()
    # Filtrar alucinaciones comunes de Whisper (capta audio del sistema/altavoces)
    _ALUCINACIONES = {
        "gracias por ver el video", "suscríbete", "like y suscríbete",
        "no olvides suscribirte", "gracias por ver", "hasta la próxima",
        "no te olvides de suscribirte", "amara.org",
        "subtítulos por la comunidad de amara.org",
        "like, suscríbete y activa la campanita",
    }
    texto_norm = texto.lower().rstrip(".!?¡¿ ")
    es_activacion = any(p in texto_norm for p in ACTIVACIONES_FB)
    if not es_activacion and (texto_norm in _ALUCINACIONES or len(texto.split()) < 2):
        print(f"[Whisper] Ignorado (ruido/alucinación): {texto!r}")
        return ""
    print(f"👤 Tú: {texto}")
    _gui_log("user", texto)
    return texto


def _build_voice_settings() -> VoiceSettings | None:
    vs = VOICE_CFG.get("voice_settings")
    if not vs:
        return None
    return VoiceSettings(
        stability=float(vs.get("stability", 0.5)),
        similarity_boost=float(vs.get("similarity_boost", 0.75)),
        style=float(vs.get("style", 0.0)),
        use_speaker_boost=bool(vs.get("use_speaker_boost", True)),
        speed=float(vs.get("speed", 1.0)),
    )


def _hablar_local(texto: str):
    """Fallback TTS offline usando SAPI de Windows (pyttsx3). Sin calidad de ElevenLabs pero funciona sin internet."""
    try:
        import pyttsx3
        engine = pyttsx3.init()
        engine.setProperty("rate", 175)
        for voz in engine.getProperty("voices"):
            if "spanish" in voz.name.lower() or "es" in voz.id.lower():
                engine.setProperty("voice", voz.id)
                break
        engine.say(texto)
        engine.runAndWait()
    except Exception as e:
        print(f"[TTS local] Error: {e}")


def hablar(texto: str) -> bool:
    """Sintetiza y reproduce audio. Devuelve True si el usuario interrumpió (barge-in)."""
    if not texto or not texto.strip():
        return False
    print(f"🤖 Jarvis: {texto}")
    _gui_log("jarvis", texto)

    model_id   = VOICE_CFG.get("model_id", "eleven_multilingual_v2")
    lang_code  = VOICE_CFG.get("language_code", "es")
    v_settings = _build_voice_settings()
    use_stream = VOICE_CFG.get("streaming", True)

    kwargs = dict(
        text=texto,
        voice_id=ELEVENLABS_VOICE_ID,
        model_id=model_id,
    )
    if "turbo" in model_id or "flash" in model_id:
        kwargs["language_code"] = lang_code
    if v_settings:
        kwargs["voice_settings"] = v_settings

    try:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            tmp_path = tmp.name
            if use_stream:
                audio_iter = eleven_client.text_to_speech.stream(**kwargs)
                for chunk in audio_iter:
                    if chunk:
                        tmp.write(chunk)
            else:
                audio_iter = eleven_client.text_to_speech.convert(**kwargs)
                for chunk in audio_iter:
                    tmp.write(chunk)
    except Exception as e:
        print(f"⚠️  ElevenLabs falló ({e}) — usando TTS local")
        _gui_log("system", "⚠️ ElevenLabs no disponible — voz local activada")
        _hablar_local(texto)
        return False

    _barge_in = threading.Event()
    _oww_pausado.set()   # liberar mic para barge-in (OWW no lo necesita mientras hablamos)

    def _monitor_barge_in():
        """Para la reproducción si el usuario empieza a hablar durante el audio."""
        _chunk = int(FS * 50 / 1000)  # 50 ms
        time.sleep(0.4)               # deja que empiece el audio antes de calibrar
        try:
            with sd.InputStream(samplerate=FS, channels=1, dtype="int16",
                                 blocksize=_chunk) as stream:
                # Calibrar umbral con nivel actual (altavoces + ambiente)
                refs = []
                for _ in range(8):
                    if not pygame.mixer.music.get_busy():
                        return
                    refs.append(_rms(stream.read(_chunk)[0].flatten()))
                umbral = max(float(np.mean(refs)) * 2.5, 0.025)
                votos = 0
                while pygame.mixer.music.get_busy():
                    nivel = _rms(stream.read(_chunk)[0].flatten())
                    if nivel > umbral:
                        votos += 1
                        if votos >= 4:          # ~200 ms sostenido
                            pygame.mixer.music.stop()
                            _barge_in.set()
                            return
                    else:
                        votos = max(0, votos - 1)
        except Exception as e:
            print(f"[barge-in] Error: {e}")

    threading.Thread(target=_monitor_barge_in, daemon=True).start()

    try:
        pygame.mixer.music.load(tmp_path)
        notify("speaking")
        _gui_estado("hablando")
        pygame.mixer.music.play()
        _clock = pygame.time.Clock()
        while pygame.mixer.music.get_busy():
            _clock.tick(10)
    finally:
        pygame.mixer.music.unload()   # siempre liberar mixer
        notify("idle")
        _gui_estado("idle")
        _oww_pausado.clear()          # reanudar OWW
        try:
            os.remove(tmp_path)
        except Exception:
            pass

    return _barge_in.is_set()

# ──────────────────────────────────────────────
#  TOOLS (function calling)
# ──────────────────────────────────────────────

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "abrir_app",
            "description": "Abre cualquier aplicación instalada en el PC. Usa el nombre común (steam, discord, chrome, spotify, notepad, vscode, etc.).",
            "parameters": {
                "type": "object",
                "properties": {
                    "app": {
                        "type": "string",
                        "description": "Nombre común de la aplicación a abrir (ej: steam, discord, chrome, spotify, notepad, calculadora, etc.)"
                    }
                },
                "required": ["app"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "abrir_web",
            "description": "Abre una URL (YouTube, Twitch, Reddit, store.steampowered.com...) o busca en Google. Pasa la URL o los términos de búsqueda en 'query'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "URL completa (https://...) o dominio (youtube.com) o términos de búsqueda en Google"
                    }
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "controlar_volumen",
            "description": "Sube, baja o silencia el volumen del sistema.",
            "parameters": {
                "type": "object",
                "properties": {
                    "accion": {
                        "type": "string",
                        "enum": ["subir", "bajar", "silenciar"],
                        "description": "Acción de volumen"
                    }
                },
                "required": ["accion"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "controlar_sistema",
            "description": "Apaga o reinicia el PC del usuario.",
            "parameters": {
                "type": "object",
                "properties": {
                    "accion": {
                        "type": "string",
                        "enum": ["apagar", "reiniciar"],
                        "description": "Acción del sistema"
                    }
                },
                "required": ["accion"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "anotar_nota",
            "description": "Guarda una nota, recordatorio o dato importante en la memoria de Jarvis.",
            "parameters": {
                "type": "object",
                "properties": {
                    "nota": {
                        "type": "string",
                        "description": "Contenido de la nota a guardar"
                    }
                },
                "required": ["nota"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "obtener_hora_fecha",
            "description": "Devuelve la hora y fecha actual del sistema.",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
]


def _ejecutar_tool(nombre: str, args: dict) -> str:
    """Ejecuta la herramienta elegida por el LLM y devuelve el resultado en texto."""

    if nombre == "abrir_app":
        app       = args.get("app", "").lower().strip()
        app_alias = {
            "calculadora": "calc", "notepad": "notepad", "bloc de notas": "notepad",
            "explorador": "explorer", "paint": "mspaint", "cmd": "cmd",
            "powershell": "powershell", "task manager": "taskmgr",
            # Steam — abrir secciones dentro de la app con protocolo steam://
            "tienda steam": "steam://store",
            "steam store": "steam://store",
            "steam tienda": "steam://store",
            "tienda de steam": "steam://store",
            "biblioteca steam": "steam://open/games",
            "steam biblioteca": "steam://open/games",
        }
        # Steam protocol URIs → abrirlas directamente con os.startfile
        if app in app_alias:
            mapped = app_alias[app]
            if mapped.startswith("steam://"):
                try:
                    os.startfile(mapped)
                    return f"Abriendo {app} en Steam."
                except Exception as e:
                    return f"Error al abrir {app}: {e}"
            app = mapped
        else:
            app = app_alias.get(app, app)

        # Discord: buscar Discord.exe real en AppData/Local (Update.exe es launcher viejo)
        if app == "discord":
            import glob as _g
            _discord_patterns = [
                os.path.join(os.environ.get("LOCALAPPDATA", ""), "Discord", "app-*", "Discord.exe"),
                os.path.join(os.environ.get("LOCALAPPDATA", ""), "Discord", "Discord.exe"),
            ]
            for pat in _discord_patterns:
                hits = _g.glob(pat)
                if hits:
                    hits.sort(reverse=True)  # versión más nueva primero
                    try:
                        subprocess.Popen([hits[0]], shell=False)
                        return "Discord abierto."
                    except Exception as e:
                        return f"Error al abrir Discord: {e}"

        def _buscar_exe(nombre_app: str) -> str:
            """Busca el exe de la app: config → registro → Start Menu → glob en discos."""
            import glob as _glob

            # 1) Config (caché rápido)
            ruta_cfg = APP_PATHS.get(nombre_app, "")
            if ruta_cfg:
                m = re.match(r'^(.*?\.exe)', ruta_cfg, re.IGNORECASE)
                exe_cfg = m.group(1).strip() if m else ruta_cfg
                if os.path.exists(exe_cfg):
                    return ruta_cfg

            # 2) Apps de Windows integradas (no necesitan ruta)
            _WIN_BUILTIN = {
                "notepad", "calc", "mspaint", "explorer", "cmd",
                "powershell", "taskmgr", "regedit", "wordpad",
            }
            if nombre_app in _WIN_BUILTIN:
                return nombre_app  # os.startfile/ShellExecute lo resuelve

            # 3) Registro de Windows — Uninstall keys (funciona para casi todo)
            try:
                import winreg
                _UNINSTALL_HIVES = [
                    (winreg.HKEY_LOCAL_MACHINE,
                     r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
                    (winreg.HKEY_LOCAL_MACHINE,
                     r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
                    (winreg.HKEY_CURRENT_USER,
                     r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
                ]
                # Claves especiales por app
                _REG_ESPECIAL = {
                    "steam":   [(winreg.HKEY_LOCAL_MACHINE,
                                 r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath", "Steam.exe"),
                                (winreg.HKEY_LOCAL_MACHINE,
                                 r"SOFTWARE\Valve\Steam", "InstallPath", "Steam.exe"),
                                (winreg.HKEY_CURRENT_USER,
                                 r"SOFTWARE\Valve\Steam", "SteamPath", "Steam.exe")],
                    "chrome":  [(winreg.HKEY_LOCAL_MACHINE,
                                 r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe",
                                 "", "chrome.exe"),
                                (winreg.HKEY_LOCAL_MACHINE,
                                 r"SOFTWARE\Google\Chrome\Application",
                                 "Version", None)],
                }
                if nombre_app in _REG_ESPECIAL:
                    for hive, key, val, exe_name in _REG_ESPECIAL[nombre_app]:
                        try:
                            with winreg.OpenKey(hive, key) as k:
                                install = winreg.QueryValueEx(k, val)[0]
                                if exe_name:
                                    candidate = os.path.join(install, exe_name)
                                    if os.path.exists(candidate):
                                        print(f"[App] {nombre_app} → {candidate}")
                                        APP_PATHS[nombre_app] = candidate
                                        return candidate
                        except Exception:
                            continue

                # Buscar en todas las claves Uninstall por DisplayName ~ nombre_app
                for hive, key_path in _UNINSTALL_HIVES:
                    try:
                        with winreg.OpenKey(hive, key_path) as k:
                            for i in range(winreg.QueryInfoKey(k)[0]):
                                try:
                                    sub = winreg.EnumKey(k, i)
                                    with winreg.OpenKey(k, sub) as sk:
                                        try:
                                            name = winreg.QueryValueEx(sk, "DisplayName")[0].lower()
                                            if nombre_app in name or name.startswith(nombre_app):
                                                try:
                                                    icon = winreg.QueryValueEx(sk, "DisplayIcon")[0]
                                                    exe = re.split(r'[",]', icon)[0].strip()
                                                    if exe.endswith(".exe") and os.path.exists(exe):
                                                        print(f"[App] {nombre_app} (reg) → {exe}")
                                                        APP_PATHS[nombre_app] = exe
                                                        return exe
                                                except Exception:
                                                    pass
                                                try:
                                                    loc = winreg.QueryValueEx(sk, "InstallLocation")[0]
                                                    if loc:
                                                        for f in _glob.glob(os.path.join(loc, "*.exe")):
                                                            bn = os.path.basename(f).lower().replace(".exe","")
                                                            if nombre_app in bn or bn.startswith(nombre_app[:4]):
                                                                print(f"[App] {nombre_app} (loc) → {f}")
                                                                APP_PATHS[nombre_app] = f
                                                                return f
                                                except Exception:
                                                    pass
                                        except Exception:
                                            pass
                                except Exception:
                                    continue
                    except Exception:
                        continue
            except ImportError:
                pass

            # 4) Start Menu shortcuts — más completo que el registro
            _start_menus = [
                os.path.join(os.environ.get("ProgramData","C:/ProgramData"),
                             "Microsoft","Windows","Start Menu","Programs"),
                os.path.join(os.environ.get("APPDATA",""),
                             "Microsoft","Windows","Start Menu","Programs"),
            ]
            for sm in _start_menus:
                for lnk in _glob.glob(os.path.join(sm, "**", "*.lnk"), recursive=True):
                    if nombre_app in os.path.basename(lnk).lower():
                        print(f"[App] {nombre_app} shortcut → {lnk}")
                        APP_PATHS[nombre_app] = lnk
                        return lnk

            # 5) Glob en discos — último recurso
            _UNIDADES = ["C:/", "D:/", "E:/", "F:/"]
            _DIRS_BUSQUEDA = [
                "Program Files/*/{exe}",
                "Program Files (x86)/*/{exe}",
                "Programs/*/{exe}",
                "Games/*/{exe}",
                "{app}/{exe}",
                "Users/*/AppData/Local/{app_cap}/{exe}",
                "Users/*/AppData/Local/{app_cap}/app-*/{exe}",
                "Users/*/AppData/Roaming/{app_cap}/{exe}",
            ]
            exe_names = [
                f"{nombre_app}.exe",
                f"{nombre_app.capitalize()}.exe",
                f"{nombre_app.upper()}.exe",
            ]
            for unidad in _UNIDADES:
                for patron_tmpl in _DIRS_BUSQUEDA:
                    for exe_n in exe_names:
                        patron = patron_tmpl.format(
                            exe=exe_n,
                            app=nombre_app,
                            app_cap=nombre_app.capitalize()
                        )
                        hits = _glob.glob(os.path.join(unidad, patron), recursive=False)
                        if hits:
                            found = hits[0]
                            print(f"[App] {nombre_app} (glob) → {found}")
                            APP_PATHS[nombre_app] = found
                            return found

            return ""  # no encontrado

        ruta_real = _buscar_exe(app)
        if not ruta_real:
            return f"No encontré '{app}' instalado. Dime la ruta exacta y te la configuro."

        try:
            m = re.match(r'^(.*?\.exe)\s*(.*)$', ruta_real, re.IGNORECASE)
            if m:
                exe  = m.group(1).strip()
                rest = (m.group(2) or '').strip()
                if rest:
                    subprocess.Popen([exe] + rest.split(), shell=False)
                else:
                    os.startfile(exe)
            elif ruta_real.endswith(".lnk"):
                os.startfile(ruta_real)
            elif "." not in os.path.basename(ruta_real):
                subprocess.Popen(ruta_real, shell=True)
            else:
                os.startfile(ruta_real)
            return f"{app} abierto correctamente."
        except Exception as e:
            return f"Error al abrir {app}: {e}"

    elif nombre == "abrir_web":
        query = args.get("query", "").strip()
        # Mapear dominios Steam a protocolo steam:// (abre dentro de la app)
        _STEAM_MAP = {
            "store.steampowered.com": "steam://store",
            "steampowered.com/store": "steam://store",
            "store steam": "steam://store",
            "steam store": "steam://store",
        }
        query_lower = query.lower().rstrip("/")
        for patron, steam_uri in _STEAM_MAP.items():
            if patron in query_lower:
                try:
                    os.startfile(steam_uri)
                    return "Tienda de Steam abierta en la aplicación."
                except Exception as e:
                    return f"Error al abrir Steam: {e}"
        # Inferir si es URL o búsqueda — sin depender de es_busqueda
        es_url = (query.startswith("http://") or query.startswith("https://")
                  or query.startswith("steam://")
                  or ("." in query and " " not in query and len(query.split(".")) >= 2))
        if es_url:
            if query.startswith("steam://"):
                try:
                    os.startfile(query)
                    return f"Abierto en Steam: {query}"
                except Exception as e:
                    return f"Error: {e}"
            url = query if query.startswith("http") else f"https://{query}"
            webbrowser.open(url)
            return f"Abierto: {url}"
        else:
            webbrowser.open(f"https://www.google.com/search?q={quote_plus(query)}")
            return f"Búsqueda '{query}' abierta en Google."

    elif nombre == "controlar_volumen":
        accion = args.get("accion", "")
        if accion == "subir":
            subir_volumen(); return "Volumen subido."
        elif accion == "bajar":
            bajar_volumen(); return "Volumen bajado."
        elif accion == "silenciar":
            silenciar();     return "Sistema silenciado."
        return f"Acción de volumen desconocida: {accion}"

    elif nombre == "controlar_sistema":
        accion = args.get("accion", "")
        if accion == "apagar":
            os.system("shutdown /s /t 5")
            return "Apagando el PC en 5 segundos."
        elif accion == "reiniciar":
            os.system("shutdown /r /t 5")
            return "Reiniciando en 5 segundos."
        return f"Acción de sistema desconocida: {accion}"

    elif nombre == "anotar_nota":
        nota = args.get("nota", "")
        memoria_larga.append({
            "ts":     datetime.now().strftime("%Y-%m-%d %H:%M"),
            "user":   f"[NOTA] {nota}",
            "jarvis": "Anotado."
        })
        _guardar_memoria(memoria_larga)
        return f"Nota guardada: {nota}"

    elif nombre == "obtener_hora_fecha":
        return datetime.now().strftime("Son las %H:%M del %A %d de %B de %Y.")

    return f"Herramienta desconocida: {nombre}"


# ──────────────────────────────────────────────
#  IA
# ──────────────────────────────────────────────

def preguntar_a_groq(pregunta: str) -> str:
    sistema  = JARVIS_SYSTEM_PROMPT.replace("{memoria}", _resumen_memoria())
    messages = [
        {"role": "system", "content": sistema},
        *list(historial_reciente),
        {"role": "user", "content": pregunta},
    ]
    # Llama 4 Scout tiene tool calling nativo JSON; 70b genera XML legacy y falla
    _TOOL_MODEL  = "meta-llama/llama-4-scout-17b-16e-instruct"
    _CHAT_MODEL  = "llama-3.3-70b-versatile"
    resp = groq_client.chat.completions.create(
        model=_TOOL_MODEL,
        messages=messages,
        tools=TOOLS,
        tool_choice="auto",
    )
    msg = resp.choices[0].message

    if msg.tool_calls:
        # Ejecutar TODOS los tool_calls (el LLM puede pedir varios a la vez)
        resultados = []
        tool_calls_msg = []
        tool_results_msgs = []
        for tc in msg.tool_calls:
            fn_name  = tc.function.name
            fn_args  = json.loads(tc.function.arguments)
            resultado = _ejecutar_tool(fn_name, fn_args)
            print(f"[Tool] {fn_name}({fn_args}) → {resultado}")
            _gui_log("tool", f"{fn_name}({fn_args}) → {resultado}")
            resultados.append(resultado)
            tool_calls_msg.append({
                "id": tc.id,
                "type": "function",
                "function": {"name": fn_name, "arguments": tc.function.arguments}
            })
            tool_results_msgs.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": resultado
            })

        resultado = " | ".join(resultados)

        # Segunda llamada: Jarvis comenta lo que acaba de hacer
        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": tool_calls_msg
        })
        messages.extend(tool_results_msgs)
        try:
            resp2 = groq_client.chat.completions.create(
                model=_CHAT_MODEL,
                messages=messages,
            )
            texto = resp2.choices[0].message.content or resultado
        except Exception as _e2:
            print(f"[Groq] Narración falló ({_e2}), usando resultado directo.")
            texto = resultado
    else:
        texto = (msg.content or "").strip()
        if not texto:
            # Tool model no generó respuesta — fallback a chat model sin tools
            try:
                resp_chat = groq_client.chat.completions.create(
                    model=_CHAT_MODEL,
                    messages=messages,
                )
                texto = (resp_chat.choices[0].message.content or "").strip()
            except Exception as _e3:
                print(f"[Groq] Chat fallback falló: {_e3}")
                texto = "Disculpe, Maestro, no pude procesar eso."

    if not texto:
        texto = "Entendido, Maestro."

    registrar_conversacion(pregunta, texto)
    return texto

# ──────────────────────────────────────────────
#  COMANDOS
# ──────────────────────────────────────────────

def ejecutar_comando(texto: str):
    """El LLM decide qué hacer mediante function calling — sin keywords hardcodeados."""
    respuesta = preguntar_a_groq(texto)
    hablar(respuesta)

# ──────────────────────────────────────────────
#  WAKE WORD — openWakeWord (local, sin API key)
# ──────────────────────────────────────────────

_wake_event  = threading.Event()   # se activa cuando se detecta "jarvis"
_oww_pausado = threading.Event()   # pausa el loop OWW mientras escuchar() graba
_USE_OWW     = False

# Modo conversación libre: escucha y responde sin wake word
_free_talk   = threading.Event()
if CFG.get("conversacion", {}).get("free_talk", True):
    _free_talk.set()

# Frases de voz para alternar de modo y para cerrar (multi-palabra: pasan el
# filtro anti-ruido de escuchar() y evitan activaciones accidentales)
_FRASES_FREE_ON  = (
    "modo libre", "modo conversación", "modo conversacion",
    "conversación libre", "conversacion libre", "free talk",
    "escúchame siempre", "escuchame siempre", "modo charla",
)
_FRASES_FREE_OFF = (
    "modo normal", "modo manual", "modo wake", "modo wake word",
    "deja de escucharme", "solo cuando te llame", "desactiva conversación",
    "desactiva conversacion", "modo silencioso",
)
_FRASES_SALIR = (
    "adiós jarvis", "adios jarvis", "hasta luego jarvis", "cierra el programa",
    "salir del programa", "apágate jarvis", "apagate jarvis", "apagar jarvis",
)


def _detectar_toggle_modo(texto: str):
    """Devuelve 'on'/'off' si la frase pide cambiar de modo, o None."""
    tl = texto.lower()
    if any(f in tl for f in _FRASES_FREE_ON):
        return "on"
    if any(f in tl for f in _FRASES_FREE_OFF):
        return "off"
    return None

# Modelos a probar en orden de preferencia
_OWW_MODELS  = ["hey_jarvis", "jarvis", "hey_mycroft", "alexa"]
_OWW_SCORE   = 0.35   # umbral de confianza (0-1)

def _iniciar_openwakeword():
    """Arranca openWakeWord en hilo daemon. Sin cuenta, sin API key."""
    global _USE_OWW
    try:
        import openwakeword
        from openwakeword.model import Model

        # Descargar modelos en segundo plano (no bloquea el arranque)
        def _descargar():
            try:
                print("⏳ Descargando modelos de wake word en segundo plano...")
                openwakeword.utils.download_models()
                print("✅ Modelos descargados.")
            except Exception as e:
                print(f"⚠️  No se pudieron descargar modelos: {e}")
        threading.Thread(target=_descargar, daemon=True).start()

        # Intentar cargar modelo — suprimir spam de onnxruntime en stdout/stderr
        import sys as _sys, io as _io
        _devnull = _io.StringIO()

        oww = None
        modelo_activo = None
        for nombre in _OWW_MODELS:
            try:
                _old_out, _old_err = _sys.stdout, _sys.stderr
                _sys.stdout = _sys.stderr = _devnull
                try:
                    oww = Model(wakeword_models=[nombre], inference_framework="onnx")
                finally:
                    _sys.stdout, _sys.stderr = _old_out, _old_err
                modelo_activo = nombre
                break
            except Exception:
                _sys.stdout, _sys.stderr = _old_out, _old_err
                continue

        if oww is None:
            try:
                _old_out, _old_err = _sys.stdout, _sys.stderr
                _sys.stdout = _sys.stderr = _devnull
                try:
                    oww = Model(inference_framework="onnx")
                finally:
                    _sys.stdout, _sys.stderr = _old_out, _old_err
                modelos_disp = list(oww.models.keys())
                modelo_activo = modelos_disp[0] if modelos_disp else None
            except Exception as e:
                _sys.stdout, _sys.stderr = _old_out, _old_err
                print(f"⚠️  openWakeWord sin modelos disponibles: {e} → fallback texto.")
                return

        if modelo_activo is None:
            print("⚠️  No se encontró ningún modelo de wake word → fallback texto.")
            return

        _USE_OWW = True
        FRAME = 1280   # 80 ms @ 16 kHz (tamaño estándar de openWakeWord)
        frase  = "hey jarvis" if "jarvis" in modelo_activo else modelo_activo.replace("_", " ")
        print(f"✅ Wake word activo — di '{frase}' para activarme (modelo: {modelo_activo})")

        def _loop():
            with sd.InputStream(samplerate=16000, channels=1, dtype="int16",
                                 blocksize=FRAME) as stream:
                while True:
                    if _oww_pausado.is_set():
                        time.sleep(0.05)
                        continue
                    chunk, _ = stream.read(FRAME)
                    audio = chunk.flatten()
                    pred  = oww.predict(audio)
                    score = pred.get(modelo_activo, 0.0)
                    if score >= _OWW_SCORE:
                        print(f"🟢 Wake word detectado! (score={score:.2f})")
                        _wake_event.set()
                        # pequeña pausa para evitar detecciones dobles
                        time.sleep(0.8)
                        oww.reset()   # limpia el buffer interno

        threading.Thread(target=_loop, daemon=True, name="oww-loop").start()

    except ImportError:
        print("⚠️  openWakeWord no instalado → fallback texto.")
        print("   pip install openwakeword")
    except Exception as e:
        print(f"⚠️  Error openWakeWord: {e} → fallback texto.")


def _esperar_wake_word():
    """
    Espera a que se diga 'jarvis'. Devuelve:
      - False : no detectado
      - True  : wake word detectado sin comando extra
      - str   : wake word + comando en la misma frase ("Jarvis abre YouTube")
    """
    if _USE_OWW:
        _wake_event.wait()
        _wake_event.clear()
        return True
    else:
        texto = escuchar()
        if not texto:
            return False
        texto_lower = texto.lower()
        for p in ACTIVACIONES_FB:
            if p in texto_lower:
                idx = texto_lower.find(p)
                resto = texto[idx + len(p):].strip().lstrip(",").strip()
                if len(resto) > 3:
                    return resto   # hay comando tras el wake word
                return True
        return False

# ──────────────────────────────────────────────
#  LOOP PRINCIPAL
# ──────────────────────────────────────────────

def _main_loop():
    """Loop principal de Jarvis — corre en hilo daemon."""
    pygame.mixer.init()
    # Arrancar OWW en background para que hablar() no espere la carga del modelo
    threading.Thread(target=_iniciar_openwakeword, daemon=True, name="oww-init").start()
    _gui_log("system", "J.A.R.V.I.S Online — All systems nominal.")
    _hora = datetime.now().strftime("%H:%M")
    _modo = ("Modo conversación libre activo, ya puede hablarme."
             if _free_talk.is_set() else "Diga 'hey Jarvis' para activarme.")
    hablar(f"Online. Son las {_hora}. Todos los sistemas operativos, Maestro. {_modo}")

    while True:
        try:
            # ── Capturar comando según el modo activo ──
            if _free_talk.is_set():
                # Modo libre: escuchar continuamente sin wake word
                _gui_estado("escuchando")
                comando = escuchar()
                if not comando:
                    continue
            else:
                activado = _esperar_wake_word()
                if not activado:
                    _gui_estado("idle")
                    continue
                if isinstance(activado, str):
                    comando = activado
                else:
                    # Capturar comando dicho justo tras el wake word
                    _gui_estado("escuchando")
                    comando = escuchar(max_espera=2.0, rapida=True)
                    if not comando:
                        hablar("Dígame, Maestro.")
                        time.sleep(0.15)
                        _gui_estado("escuchando")
                        comando = escuchar()
                    if not comando:
                        _gui_estado("idle")
                        continue

            comando_lower = comando.lower()

            # ── Cambio de modo por voz ──
            toggle = _detectar_toggle_modo(comando)
            if toggle == "on":
                if _free_talk.is_set():
                    hablar("El modo conversación libre ya está activo, Maestro.")
                else:
                    _free_talk.set()
                    hablar("Modo conversación libre activado. Ya puede hablarme sin llamarme.")
                _gui_estado("idle")
                continue
            if toggle == "off":
                if not _free_talk.is_set():
                    hablar("Ya estoy en modo normal, Maestro.")
                else:
                    _free_talk.clear()
                    hablar("Modo normal. Dígame 'hey Jarvis' cuando me necesite.")
                _gui_estado("idle")
                continue

            # ── Cerrar (requiere frase explícita para no apagarse por error) ──
            if any(f in comando_lower for f in _FRASES_SALIR):
                hablar("Hasta luego, Maestro. Ha sido un placer.")
                break

            _gui_estado("procesando")
            ejecutar_comando(comando)
            _gui_estado("idle")

            # En modo wake-word: ventana corta de seguimiento sin repetir el wake word
            if not _free_talk.is_set():
                time.sleep(0.15)
                seguimiento = escuchar(max_espera=2.5)
                if seguimiento:
                    _gui_estado("procesando")
                    ejecutar_comando(seguimiento)
                    _gui_estado("idle")

        except KeyboardInterrupt:
            hablar("Cerrando, Maestro.")
            break
        except Exception as e:
            print(f"⚠️  Error recuperable: {e}")
            _gui_log("system", f"⚠️ Error: {e}")
            _gui_estado("idle")


# ──────────────────────────────────────────────
#  GUI TKINTER
# ──────────────────────────────────────────────

class JarvisApp:

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("J.A.R.V.I.S")
        self.root.configure(bg="#000508")
        self._bg_photo = None
        self.root.state("zoomed")
        self.root.update()
        self.SW = self.root.winfo_screenwidth()
        self.SH = self.root.winfo_screenheight()
        self._cx = self.SW // 2
        self._cy = int(self.SH * 0.50)
        # Ángulos de animación
        self._a1 = 0.0       # anillo exterior: CW lento
        self._a2 = 180.0     # anillo 2: CCW
        self._a3 = 90.0      # anillo 3: CW (rojo)
        self._a4 = 45.0      # anillo 4: CCW rápido
        self._a5 = 0.0       # anillo interior: muy rápido CW
        self._a_spoke = 0.0  # spokes radiales
        self._a_orbit = 0.0  # targeting brackets
        self._scan = 0.0
        self._pulse = 0.0
        self._frame = 0
        self._title_id = None
        self._setup_ui()

    def _setup_ui(self):
        SW, SH = self.SW, self.SH
        cx, cy = self._cx, self._cy

        self.canvas = tk.Canvas(
            self.root, width=SW, height=SH,
            bg="#000508", highlightthickness=0, bd=0
        )
        self.canvas.place(x=0, y=0)

        # ── Imagen de fondo ──
        bg_path = os.path.join(BASE_DIR, "Jarvis Fondo.png")
        if os.path.exists(bg_path):
            try:
                if _PIL_AVAILABLE:
                    img = Image.open(bg_path).resize((SW, SH), Image.LANCZOS)
                    self._bg_photo = ImageTk.PhotoImage(img)
                else:
                    raw = tk.PhotoImage(file=bg_path)
                    w, h = raw.width(), raw.height()
                    f = max(1, min(w // SW, h // SH))
                    self._bg_photo = raw.subsample(f, f) if f > 1 else raw
                self.canvas.create_image(0, 0, image=self._bg_photo, anchor="nw")
            except Exception as e:
                print(f"[GUI] Fondo no cargado: {e}")

        # ── Overlay semitransparente en zona del log ──
        log_top = int(SH * 0.42)
        self.canvas.create_rectangle(
            0, log_top, SW, SH,
            fill="#020810", stipple="gray75", outline=""
        )

        # ── Círculos de referencia estáticos (guías) ──
        for r_frac, col in [(0.400, "#050e18"), (0.310, "#050e18"), (0.195, "#050e18")]:
            r = int(SH * r_frac)
            self.canvas.create_oval(cx-r, cy-r, cx+r, cy+r, outline=col, width=1)

        # ── Cruceta de ejes HUD ──
        ax_len = int(SH * 0.30)
        self.canvas.create_line(cx - ax_len, cy, cx + ax_len, cy,
                                fill="#081828", width=1, dash=(4, 10))
        self.canvas.create_line(cx, cy - ax_len, cx, cy + ax_len,
                                fill="#081828", width=1, dash=(4, 10))

        # ── Retículo hexagonal interior (estático) ──
        r_hex = int(SH * 0.068)
        for i in range(6):
            a1h = math.radians(i * 60)
            a2h = math.radians((i + 1) * 60)
            x1h = cx + r_hex * math.cos(a1h)
            y1h = cy + r_hex * math.sin(a1h)
            x2h = cx + r_hex * math.cos(a2h)
            y2h = cy + r_hex * math.sin(a2h)
            self.canvas.create_line(x1h, y1h, x2h, y2h, fill="#003344", width=1)

        # ── Título ──
        self._title_id = self.canvas.create_text(
            SW // 2, int(SH * 0.060),
            text="J.A.R.V.I.S",
            font=("Segoe UI", 34, "bold"), fill="#00b4d8", anchor="center"
        )
        self.canvas.create_text(
            SW // 2, int(SH * 0.094),
            text="JUST A RATHER VERY INTELLIGENT SYSTEM",
            font=("Segoe UI", 8), fill="#0d4a60", anchor="center"
        )
        # Línea divisoria con marcadores
        lx1, lx2 = int(SW * 0.32), int(SW * 0.68)
        ly_sep = int(SH * 0.114)
        self.canvas.create_line(lx1, ly_sep, lx2, ly_sep, fill="#00b4d8", width=1)
        for xm in [lx1, lx2]:
            self.canvas.create_rectangle(xm-3, ly_sep-3, xm+3, ly_sep+3,
                                         fill="#00b4d8", outline="")

        # ── Corner brackets ──
        self._draw_corner_brackets()

        # ── Log panel ──
        lw = int(SW * 0.68)
        lh = int(SH * 0.46)
        lx = (SW - lw) // 2
        ly = int(SH * 0.44)
        log_frame = tk.Frame(self.root, bg="#040d1a", bd=0,
                             highlightthickness=1, highlightbackground="#00b4d8")
        log_frame.place(x=lx, y=ly, width=lw, height=lh)

        self.log = tk.Text(
            log_frame, bg="#040d1a", fg="#e8e8e8",
            font=("Consolas", 11), bd=0, relief="flat",
            wrap=tk.WORD, state=tk.DISABLED, highlightthickness=0,
            insertbackground="#00b4d8", padx=12, pady=8, spacing1=3, spacing3=3,
        )
        sb = tk.Scrollbar(log_frame, orient="vertical", command=self.log.yview,
                          bg="#0d1525", troughcolor="#030b18", activebackground="#00b4d8")
        self.log.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.log.tag_config("user",   foreground="#e8e8e8")
        self.log.tag_config("jarvis", foreground="#00cfff",
                            font=("Consolas", 11, "bold"))
        self.log.tag_config("tool",   foreground="#7ec8e3",
                            font=("Consolas", 10))
        self.log.tag_config("system", foreground="#4a6fa5",
                            font=("Consolas", 10, "italic"))

        self.status_lbl = tk.Label(
            self.root, text="● INITIALIZING...",
            font=("Segoe UI", 10, "bold"), fg="#00b4d8",
            bg="#020810", anchor="w", padx=16,
        )
        self.status_lbl.place(x=0, y=SH - 42, width=SW, height=42)

        self._animate()

    def _draw_corner_brackets(self):
        SW, SH = self.SW, self.SH
        s, m, w, c = 40, 18, 2, "#00b4d8"
        bl = SH - 58
        coords = [
            (m, m, m+s, m), (m, m, m, m+s),
            (SW-m, m, SW-m-s, m), (SW-m, m, SW-m, m+s),
            (m, bl, m+s, bl), (m, bl, m, bl-s),
            (SW-m, bl, SW-m-s, bl), (SW-m, bl, SW-m, bl-s),
        ]
        for x1, y1, x2, y2 in coords:
            self.canvas.create_line(x1, y1, x2, y2, fill=c, width=w)

    def _animate(self):
        SW, SH = self.SW, self.SH
        cx, cy = self._cx, self._cy
        self.canvas.delete("anim")

        self._frame += 1
        log_top = int(SH * 0.44)

        # ── Avanzar ángulos ──
        self._a1 = (self._a1 + 0.18) % 360
        self._a2 = (self._a2 - 0.40) % 360
        self._a3 = (self._a3 + 0.65) % 360
        self._a4 = (self._a4 - 1.30) % 360
        self._a5 = (self._a5 + 2.20) % 360
        self._a_spoke = (self._a_spoke + 0.08) % 360
        self._a_orbit = (self._a_orbit + 0.30) % 360
        self._scan = (self._scan + 1.8) % (log_top - int(SH * 0.02))
        self._pulse = (self._pulse + 0.05) % (6.283185)

        # ── Radios ──
        r1 = int(SH * 0.355)
        r2 = int(SH * 0.295)
        r3 = int(SH * 0.235)
        r4 = int(SH * 0.168)
        r5 = int(SH * 0.095)
        r6 = int(SH * 0.042)

        # ── GLOW CENTRAL ──
        for rg, col, ww in [
            (r6+18, "#002535", 14),
            (r6+8,  "#004055", 8),
            (r6,    "#00b4d8", 2),
            (r6-8,  "#60d8f0", 1),
        ]:
            self.canvas.create_oval(cx-rg, cy-rg, cx+rg, cy+rg,
                                    outline=col, width=ww, tags="anim")

        # ── ANILLO 1: Exterior (CW lento, cian, segmentado) ──
        for s_off, ext, lw in [(0, 115, 2), (135, 55, 1), (220, 90, 2), (330, 18, 3)]:
            self.canvas.create_arc(
                cx-r1, cy-r1, cx+r1, cy+r1,
                start=self._a1+s_off, extent=ext,
                style="arc", outline="#00b4d8", width=lw, tags="anim"
            )
        # Leading edge brillante
        self.canvas.create_arc(
            cx-r1, cy-r1, cx+r1, cy+r1,
            start=self._a1+328, extent=3,
            style="arc", outline="#80e8ff", width=3, tags="anim"
        )
        # 36 ticks en anillo exterior
        for i in range(36):
            ang = math.radians(self._a1 + i * 10)
            if i % 9 == 0:
                r_in, col, tlw = r1-24, "#00cfff", 2
            elif i % 3 == 0:
                r_in, col, tlw = r1-12, "#1a7a9a", 1
            else:
                r_in, col, tlw = r1-5,  "#0d2a35", 1
            self.canvas.create_line(
                cx + r1*math.cos(ang), cy + r1*math.sin(ang),
                cx + r_in*math.cos(ang), cy + r_in*math.sin(ang),
                fill=col, width=tlw, tags="anim"
            )

        # ── ANILLO 2: Segundo (CCW, azul) ──
        for s_off, ext in [(0, 75), (95, 38), (155, 105), (285, 50)]:
            self.canvas.create_arc(
                cx-r2, cy-r2, cx+r2, cy+r2,
                start=self._a2+s_off, extent=ext,
                style="arc", outline="#0088bb", width=2, tags="anim"
            )
        # Punto brillante líder
        self.canvas.create_arc(
            cx-r2, cy-r2, cx+r2, cy+r2,
            start=self._a2+358, extent=2,
            style="arc", outline="#80e8ff", width=3, tags="anim"
        )
        # Puntos de datos en anillo 2
        for i in range(16):
            ang = math.radians(self._a2 + i * 22.5)
            rl = r2+7 if i % 4 == 0 else r2+3
            xn = cx + rl * math.cos(ang)
            yn = cy + rl * math.sin(ang)
            col = "#00cfff" if i % 4 == 0 else "#0a3040"
            self.canvas.create_oval(xn-2, yn-2, xn+2, yn+2,
                                    fill=col, outline="", tags="anim")

        # ── ANILLO 3: Reactor (CW, rojo) ──
        for s_off, ext, col in [
            (0, 55, "#cc2a20"), (70, 28, "#ff5040"),
            (125, 65, "#cc2a20"), (215, 42, "#ff5040"), (280, 60, "#cc2a20")
        ]:
            self.canvas.create_arc(
                cx-r3, cy-r3, cx+r3, cy+r3,
                start=self._a3+s_off, extent=ext,
                style="arc", outline=col, width=2, tags="anim"
            )
        # 6 puntos marcadores rojos
        for i in range(6):
            ang = math.radians(self._a3 + i * 60)
            xd = cx + r3 * math.cos(ang)
            yd = cy + r3 * math.sin(ang)
            self.canvas.create_oval(xd-4, yd-4, xd+4, yd+4,
                                    fill="#ff3b30", outline="#ff8070", tags="anim")

        # ── ANILLO 4: Datos interior (CCW rápido, teal) ──
        for s_off, ext in [(0, 38), (50, 18), (85, 45), (155, 22), (195, 55), (268, 30), (318, 14)]:
            self.canvas.create_arc(
                cx-r4, cy-r4, cx+r4, cy+r4,
                start=self._a4+s_off, extent=ext,
                style="arc", outline="#2a9a8a", width=1, tags="anim"
            )
        # Punto blanco en anillo 4
        self.canvas.create_arc(
            cx-r4, cy-r4, cx+r4, cy+r4,
            start=self._a4+1, extent=2,
            style="arc", outline="#ffffff", width=2, tags="anim"
        )

        # ── ANILLO 5: Arc Reactor (muy rápido, verde) ──
        for offset in (0, 120, 240):
            self.canvas.create_arc(
                cx-r5, cy-r5, cx+r5, cy+r5,
                start=self._a5+offset, extent=78,
                style="arc", outline="#00e676", width=2, tags="anim"
            )
        # Leading edge brillante
        self.canvas.create_arc(
            cx-r5, cy-r5, cx+r5, cy+r5,
            start=self._a5, extent=3,
            style="arc", outline="#aaffcc", width=3, tags="anim"
        )

        # ── SPOKES RADIALES (8, rotación lenta) ──
        for i in range(8):
            ang = math.radians(self._a_spoke + i * 45)
            rs = r6 + 22
            re = r5 - 8
            col = "#00cfff" if i % 4 == 0 else "#061e2e"
            self.canvas.create_line(
                cx + rs*math.cos(ang), cy + rs*math.sin(ang),
                cx + re*math.cos(ang), cy + re*math.sin(ang),
                fill=col, width=1, tags="anim"
            )

        # ── TARGETING BRACKETS ORBITALES (4 brackets) ──
        r_orb = (r3 + r4) // 2
        for i in range(4):
            ang = math.radians(self._a_orbit + i * 90)
            ox = cx + r_orb * math.cos(ang)
            oy = cy + r_orb * math.sin(ang)
            s = 9
            for dx, dy in [(-1, -1), (1, -1), (-1, 1), (1, 1)]:
                self.canvas.create_line(ox+dx*s, oy+dy*s, ox+dx*s, oy,
                                        fill="#00cfff", width=1, tags="anim")
                self.canvas.create_line(ox+dx*s, oy+dy*s, ox, oy+dy*s,
                                        fill="#00cfff", width=1, tags="anim")

        # ── SCAN LINE ──
        sy = int(SH * 0.02) + int(self._scan)
        self.canvas.create_line(int(SW*0.02), sy, int(SW*0.98), sy,
                                fill="#0d2535", width=4, tags="anim")
        self.canvas.create_line(int(SW*0.02), sy, int(SW*0.98), sy,
                                fill="#00b4d8", width=1, tags="anim")

        # ── PANELES DE DATOS EN ESQUINAS ──
        now_str = time.strftime("%H:%M:%S")
        tl = f"SYSTEM STATUS\n{'─'*14}\nPOWER  : 100%\nSHIELD : ACTIVE\nAI CORE: ONLINE\nTIME   : {now_str}"
        self.canvas.create_text(
            55, 140, text=tl,
            font=("Consolas", 9), fill="#1a7a9a",
            anchor="nw", tags="anim", justify="left"
        )
        tr = f"DIAGNOSTICS\n{'─'*14}\nNETWORK: SECURE\nMEMORY : OPTIMAL\nTHREAT : NONE\nFRAME  : {self._frame:06d}"
        self.canvas.create_text(
            SW - 55, 140, text=tr,
            font=("Consolas", 9), fill="#1a7a9a",
            anchor="ne", tags="anim", justify="right"
        )

        # ── PULSO EN TÍTULO ──
        pb = int(150 + 105 * abs(math.sin(self._pulse)))
        pc = min(pb + 55, 255)
        title_col = f"#00{pb:02x}{pc:02x}"
        if self._title_id:
            self.canvas.itemconfig(self._title_id, fill=title_col)

        self.root.after(30, self._animate)

    def _add_log(self, tipo: str, texto: str):
        prefijos = {
            "user":   "YOU    ▸  ",
            "jarvis": "JARVIS ▸  ",
            "tool":   "SYS    ▸  ",
            "system": "  ─── ",
        }
        prefijo = prefijos.get(tipo, "")
        self.log.configure(state=tk.NORMAL)
        self.log.insert(tk.END, f"{prefijo}{texto}\n", tipo)
        self.log.configure(state=tk.DISABLED)
        self.log.see(tk.END)

    def _set_estado(self, estado: str):
        estados = {
            "idle":       ("● STANDBY",       "#00b4d8"),
            "escuchando": ("● LISTENING...",  "#00e676"),
            "hablando":   ("● SPEAKING...",   "#ff6b35"),
            "procesando": ("● PROCESSING...", "#ffd700"),
        }
        label, color = estados.get(estado, ("● STANDBY", "#00b4d8"))
        self.status_lbl.configure(text=label, fg=color)

    def _poll_queue(self):
        try:
            while True:
                item = _gui_queue.get_nowait()
                if item[0] == "log":
                    self._add_log(item[1], item[2])
                elif item[0] == "estado":
                    self._set_estado(item[1])
        except _queue_module.Empty:
            pass
        except Exception as _e_poll:
            print(f"[GUI] Error en poll: {_e_poll}")
        self.root.after(50, self._poll_queue)

    def run(self):
        t = threading.Thread(target=_main_loop, daemon=True)
        t.start()
        self.root.after(50, self._poll_queue)
        self.root.mainloop()


if __name__ == "__main__":
    JarvisApp().run()