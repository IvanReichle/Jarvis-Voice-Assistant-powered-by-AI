"""
jarvis.py — Asistente de voz con:
  · config.json para rutas de apps y ajustes
  · Wake word siempre activo con pvporcupine ("jarvis")
  · Memoria larga persistente en jarvis_memoria.json
  · Fallback: si pvporcupine no está instalado, usa detección por texto como antes

Instalación de dependencias:
    pip install pvporcupine sounddevice numpy groq elevenlabs pygame requests

Wake word (gratis, personal):
    1. Crea cuenta en https://picovoice.ai/  (gratis)
    2. Copia tu Access Key y ponla en PICOVOICE_ACCESS_KEY como variable de entorno
       o directamente en jarvis_config.json → "wake_word" → "picovoice_access_key"
"""

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
import tempfile
from collections import deque
from datetime import datetime
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
PICOVOICE_ACCESS_KEY = (
    os.environ.get("PICOVOICE_ACCESS_KEY")
    or CFG.get("wake_word", {}).get("picovoice_access_key", "")
)

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

JARVIS_SYSTEM_PROMPT = (
    "Eres JARVIS, asistente de IA personal. Sé directo, eficiente e inteligente. "
    "Humor seco e ironía ligera cuando encaje, nunca grosero. "
    "Responde SIEMPRE en español, en máximo 2 o 3 frases. "
    "Llama al usuario 'Maestro' de forma natural cuando encaje. "
    "Prioriza ser útil y preciso. Usa el historial para mantener coherencia.\n\n"
    "{memoria}"
)

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


threading.Thread(
    target=lambda: HTTPServer(("localhost", 5050), SSEHandler).serve_forever(),
    daemon=True,
).start()

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


def escuchar() -> str:
    """Graba hasta que hay silencio o timeout. Devuelve texto transcrito."""
    _gui_estado("escuchando")
    print("🎙️  Escuchando...")
    chunk_samples      = int(FS * CHUNK_MS / 1000)
    max_silencio_ch    = int(SILENCIO_SEG * 1000 / CHUNK_MS)
    max_total_ch       = int(MAX_GRAB_SEG  * 1000 / CHUNK_MS)
    max_espera_ch      = int(MAX_ESPERA_SEG * 1000 / CHUNK_MS)

    frames, hablando, silencio_ch, espera_ch = [], False, 0, 0

    with sd.InputStream(samplerate=FS, channels=1, dtype="int16",
                        blocksize=chunk_samples) as stream:
        umbral = _calibrar_umbral(stream, chunk_samples)
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
    )


def hablar(texto: str) -> bool:
    """Sintetiza y reproduce audio. Devuelve True si el usuario interrumpió (barge-in)."""
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

    _barge_in = threading.Event()

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
        while pygame.mixer.music.get_busy():
            pygame.time.Clock().tick(10)
        pygame.mixer.music.unload()
    finally:
        notify("idle")
        _gui_estado("idle")
        os.remove(tmp_path)

    return _barge_in.is_set()

# ──────────────────────────────────────────────
#  TOOLS (function calling)
# ──────────────────────────────────────────────

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "abrir_app",
            "description": "Abre una aplicación instalada en el PC del usuario.",
            "parameters": {
                "type": "object",
                "properties": {
                    "app": {
                        "type": "string",
                        "enum": ["discord", "steam", "spotify", "chrome", "vscode", "riot"],
                        "description": "Nombre de la aplicación a abrir"
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
            "description": "Abre una URL (YouTube, Twitch, Reddit...) o hace una búsqueda en Google.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "URL del sitio (youtube.com, twitch.tv...) o términos de búsqueda"
                    },
                    "es_busqueda": {
                        "type": "boolean",
                        "description": "True para buscar en Google, False para abrir la URL directamente"
                    }
                },
                "required": ["query", "es_busqueda"]
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
        app  = args.get("app", "")
        ruta = APP_PATHS.get(app, "")
        if not ruta:
            return f"No tengo configurada la ruta de {app}."
        try:
            subprocess.Popen(ruta, shell=True)
            return f"{app} abierto correctamente."
        except Exception as e:
            return f"Error al abrir {app}: {e}"

    elif nombre == "abrir_web":
        query       = args.get("query", "")
        es_busqueda = args.get("es_busqueda", True)
        if es_busqueda:
            webbrowser.open(f"https://www.google.com/search?q={query}")
            return f"Búsqueda '{query}' abierta en Google."
        else:
            url = query if query.startswith("http") else f"https://{query}"
            webbrowser.open(url)
            return f"Abierto: {url}"

    elif nombre == "controlar_volumen":
        accion = args.get("accion", "")
        if accion == "subir":
            subir_volumen(); return "Volumen subido."
        elif accion == "bajar":
            bajar_volumen(); return "Volumen bajado."
        elif accion == "silenciar":
            silenciar();     return "Sistema silenciado."

    elif nombre == "controlar_sistema":
        accion = args.get("accion", "")
        if accion == "apagar":
            os.system("shutdown /s /t 5")
            return "Apagando el PC en 5 segundos."
        elif accion == "reiniciar":
            os.system("shutdown /r /t 5")
            return "Reiniciando en 5 segundos."

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
    sistema  = JARVIS_SYSTEM_PROMPT.format(memoria=_resumen_memoria())
    messages = [
        {"role": "system", "content": sistema},
        *list(historial_reciente),
        {"role": "user", "content": pregunta},
    ]
    resp = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=messages,
        tools=TOOLS,
        tool_choice="auto",
    )
    msg = resp.choices[0].message

    if msg.tool_calls:
        tc       = msg.tool_calls[0]
        fn_name  = tc.function.name
        fn_args  = json.loads(tc.function.arguments)
        resultado = _ejecutar_tool(fn_name, fn_args)
        print(f"[Tool] {fn_name}({fn_args}) → {resultado}")
        _gui_log("tool", f"{fn_name}({fn_args}) → {resultado}")

        # Segunda llamada: Jarvis comenta lo que acaba de hacer
        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": fn_name, "arguments": tc.function.arguments}
                }
            ]
        })
        messages.append({
            "role": "tool",
            "tool_call_id": tc.id,
            "content": resultado
        })
        resp2 = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=messages,
        )
        texto = resp2.choices[0].message.content or resultado
    else:
        texto = msg.content or ""

    registrar_conversacion(pregunta, texto)
    return texto

# ──────────────────────────────────────────────
#  COMANDOS
# ──────────────────────────────────────────────

def abrir_app(clave: str, nombre: str):
    ruta = APP_PATHS.get(clave, "")
    if not ruta:
        hablar(f"No tengo configurada la ruta de {nombre} en config, Maestro.")
        return
    try:
        os.startfile(ruta)
        hablar(f"Abriendo {nombre}, Maestro.")
    except Exception as e:
        print(f"[abrir_app] {e}")
        hablar(f"No pude abrir {nombre}. Revisa la ruta en jarvis_config.json.")


def ejecutar_comando(texto: str):
    """El LLM decide qué hacer mediante function calling — sin keywords hardcodeados."""
    respuesta = preguntar_a_groq(texto)
    hablar(respuesta)

# ──────────────────────────────────────────────
#  WAKE WORD — pvporcupine (siempre activo)
# ──────────────────────────────────────────────

_wake_event = threading.Event()   # se activa cuando se detecta "jarvis"
_USE_PORCUPINE = False

def _iniciar_porcupine():
    global _USE_PORCUPINE
    if not PICOVOICE_ACCESS_KEY:
        print("⚠️  PICOVOICE_ACCESS_KEY no configurada → usando detección por texto (fallback).")
        print("   Obtén tu clave gratis en https://picovoice.ai/")
        return

    try:
        import pvporcupine
        import struct

        porcupine = pvporcupine.create(
            access_key=PICOVOICE_ACCESS_KEY,
            keywords=["jarvis"],
        )
        _USE_PORCUPINE = True
        print("✅ Wake word pvporcupine activo — di 'Jarvis' para activarme.")

        def _loop():
            chunk_size = porcupine.frame_length
            with sd.RawInputStream(
                samplerate=porcupine.sample_rate,
                channels=1,
                dtype="int16",
                blocksize=chunk_size,
            ) as stream:
                while True:
                    pcm_raw, _ = stream.read(chunk_size)
                    pcm = struct.unpack_from(f"{chunk_size}h", bytes(pcm_raw))
                    if porcupine.process(pcm) >= 0:
                        print("🟢 Wake word detectado!")
                        _wake_event.set()

        threading.Thread(target=_loop, daemon=True).start()

    except ImportError:
        print("⚠️  pvporcupine no instalado (pip install pvporcupine) → fallback por texto.")
    except Exception as e:
        print(f"⚠️  Error al iniciar porcupine: {e} → fallback por texto.")


def _esperar_wake_word():
    """
    Espera a que se diga 'jarvis'. Devuelve:
      - False : no detectado
      - True  : wake word detectado sin comando extra
      - str   : wake word + comando en la misma frase ("Jarvis abre YouTube")
    """
    if _USE_PORCUPINE:
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
#  ARRANQUE
# ──────────────────────────────────────────────

# ──────────────────────────────────────────────
#  LOOP PRINCIPAL
# ──────────────────────────────────────────────

def _main_loop():
    """Loop principal de Jarvis — corre en hilo daemon."""
    pygame.mixer.init()
    _iniciar_porcupine()
    _gui_log("system", "J.A.R.V.I.S Online — All systems nominal.")
    hablar("Online. Todos los sistemas operativos, Maestro.")

    while True:
        try:
            activado = _esperar_wake_word()
            if not activado:
                _gui_estado("idle")
                continue

            if isinstance(activado, str):
                comando = activado
                _gui_estado("procesando")  # comando ya capturado, ir directo
            else:
                hablar("Dígame, Maestro.")
                time.sleep(0.15)  # deja que hardware de audio se estabilice
                _gui_estado("escuchando")
                comando = escuchar()
                if not comando:
                    _gui_estado("idle")
                    continue

            if "adiós" in comando.lower() or "salir" in comando.lower():
                hablar("Hasta luego, Maestro. Ha sido un placer.")
                break

            _gui_estado("procesando")
            ejecutar_comando(comando)
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
        except Exception:
            pass
        self.root.after(50, self._poll_queue)

    def run(self):
        t = threading.Thread(target=_main_loop, daemon=True)
        t.start()
        self.root.after(50, self._poll_queue)
        self.root.mainloop()


if __name__ == "__main__":
    JarvisApp().run()