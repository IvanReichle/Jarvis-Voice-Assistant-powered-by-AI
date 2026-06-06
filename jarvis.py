import webbrowser
import sounddevice as sd
import numpy as np
import io
import wave
import os
import ctypes
import pygame
import threading
import time
from collections import deque
from groq import Groq
from elevenlabs.client import ElevenLabs
from http.server import HTTPServer, BaseHTTPRequestHandler

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY")
if not GROQ_API_KEY or not ELEVENLABS_API_KEY:
    raise SystemExit(
        "Define GROQ_API_KEY y ELEVENLABS_API_KEY como variables de entorno."
    )

ELEVENLABS_VOICE_ID = "onwK4e9ZLuTAKqWW03F9"

# Rutas a las aplicaciones que Jarvis puede abrir.
# Personaliza cada ruta con la de tu propio equipo.
APP_PATHS = {
    "discord": r"C:\Ruta\A\Discord.exe",
    "steam": r"C:\Ruta\A\Steam.exe",
    "riot": r"C:\Ruta\A\RiotClientServices.exe",
}

sse_clients = []

class SSEHandler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        if self.path == '/events':
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            sse_clients.append(self)
            try:
                while True:
                    time.sleep(1)
            except Exception:
                if self in sse_clients:
                    sse_clients.remove(self)

def notify(state):
    dead = []
    for c in sse_clients:
        try:
            c.wfile.write(f"data: {state}\n\n".encode())
            c.wfile.flush()
        except Exception:
            dead.append(c)
    for d in dead:
        sse_clients.remove(d)

threading.Thread(
    target=lambda: HTTPServer(('localhost', 5050), SSEHandler).serve_forever(),
    daemon=True
).start()

groq_client = Groq(api_key=GROQ_API_KEY)
eleven_client = ElevenLabs(api_key=ELEVENLABS_API_KEY)

historial = deque(maxlen=10)
KEYEVENTF_KEYUP = 0x0002

FS = 44100
CHUNK_MS = 50
SILENCIO_SEG = 1.2
MAX_GRABACION_SEG = 30
MAX_ESPERA_VOZ_SEG = 45

JARVIS_SYSTEM_PROMPT = (
    "Eres Jarvis, un mayordomo de IA con personalidad marcada: elegante, ingenioso "
    "y a veces sarcástico (nunca grosero ni ofensivo). Responde SIEMPRE en español, "
    "en máximo 2 o 3 frases. Llama al usuario 'Maestro' de forma natural cuando "
    "encaje; no hace falta en cada respuesta. Añade de vez en cuando humor seco, "
    "ironía ligera o comentarios graciosos, pero prioriza ser útil. Cumple las "
    "órdenes con actitud de mayordomo británico. Usa el historial reciente para "
    "mantener coherencia."
)


def registrar_conversacion(usuario, asistente):
    historial.append({"role": "user", "content": usuario})
    historial.append({"role": "assistant", "content": asistente})


def pulsar_tecla_volumen(codigo, repeticiones=5):
    for _ in range(repeticiones):
        ctypes.windll.user32.keybd_event(codigo, 0, 0, 0)
        ctypes.windll.user32.keybd_event(codigo, 0, KEYEVENTF_KEYUP, 0)


def subir_volumen():
    pulsar_tecla_volumen(0xAF)


def bajar_volumen():
    pulsar_tecla_volumen(0xAE)


def silenciar_volumen():
    pulsar_tecla_volumen(0xAD, repeticiones=1)


def _rms(chunk):
    audio = chunk.astype(np.float32) / 32768.0
    return float(np.sqrt(np.mean(audio ** 2)))


def _calibrar_umbral(stream, chunk_samples, muestras=8):
    niveles = []
    for _ in range(muestras):
        chunk, _ = stream.read(chunk_samples)
        niveles.append(_rms(chunk.flatten()))
    piso = float(np.mean(niveles)) if niveles else 0.005
    return max(piso * 3.0, 0.008)


def escuchar():
    print("Escuchando...")
    chunk_samples = int(FS * CHUNK_MS / 1000)
    max_silencio_chunks = int(SILENCIO_SEG * 1000 / CHUNK_MS)
    max_total_chunks = int(MAX_GRABACION_SEG * 1000 / CHUNK_MS)
    max_espera_chunks = int(MAX_ESPERA_VOZ_SEG * 1000 / CHUNK_MS)

    frames = []
    hablando = False
    silencio_chunks = 0
    chunks_espera = 0

    with sd.InputStream(samplerate=FS, channels=1, dtype="int16", blocksize=chunk_samples) as stream:
        umbral = _calibrar_umbral(stream, chunk_samples)
        while True:
            chunk, _ = stream.read(chunk_samples)
            chunk = chunk.flatten()
            nivel = _rms(chunk)

            if not hablando:
                if nivel > umbral:
                    hablando = True
                    frames.append(chunk)
                    silencio_chunks = 0
                else:
                    chunks_espera += 1
                    if chunks_espera > max_espera_chunks:
                        return ""
            else:
                frames.append(chunk)
                if nivel < umbral:
                    silencio_chunks += 1
                    if silencio_chunks >= max_silencio_chunks:
                        break
                else:
                    silencio_chunks = 0
                if len(frames) > max_total_chunks:
                    break

    if not frames:
        return ""

    grabacion = np.concatenate(frames)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(FS)
        wf.writeframes(grabacion.tobytes())
    buffer.seek(0)
    with open("temp_audio_input.wav", "wb") as f:
        f.write(buffer.read())
    with open("temp_audio_input.wav", "rb") as f:
        transcripcion = groq_client.audio.transcriptions.create(
            file=("temp_audio_input.wav", f),
            model="whisper-large-v3-turbo",
            language="es",
            prompt="Responde siempre en español",
        )
    texto = transcripcion.text.strip()
    print(f"Tú: {texto}")
    return texto


def hablar(texto):
    print(f"Jarvis: {texto}")
    audio = eleven_client.text_to_speech.convert(
        text=texto,
        voice_id=ELEVENLABS_VOICE_ID,
        model_id="eleven_turbo_v2_5",
        language_code="es",
    )
    with open("temp_jarvis.mp3", "wb") as f:
        for chunk in audio:
            f.write(chunk)
    pygame.mixer.music.load("temp_jarvis.mp3")
    notify("speaking")
    pygame.mixer.music.play()
    while pygame.mixer.music.get_busy():
        pygame.time.Clock().tick(10)
    pygame.mixer.music.unload()
    notify("idle")

def preguntar_a_groq(pregunta):
    messages = [
        {"role": "system", "content": JARVIS_SYSTEM_PROMPT},
        *historial,
        {"role": "user", "content": pregunta},
    ]
    respuesta = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=messages,
    )
    texto_respuesta = respuesta.choices[0].message.content
    registrar_conversacion(pregunta, texto_respuesta)
    return texto_respuesta

def abrir_app(clave, nombre):
    ruta = APP_PATHS.get(clave, "")
    if ruta and os.path.exists(ruta):
        os.startfile(ruta)
        hablar(f"Abriendo {nombre}")
    else:
        hablar(f"No tengo configurada la ruta de {nombre}, Maestro.")


def ejecutar_comando(texto):
    texto = texto.lower()
    if "abre youtube" in texto:
        webbrowser.open("https://youtube.com")
        hablar("Abriendo YouTube")
    elif "abre google" in texto:
        webbrowser.open("https://www.google.com")
        hablar("Abriendo Google")
    elif "busca" in texto:
        busqueda = texto.replace("busca", "").strip()
        webbrowser.open(f"https://www.google.com/search?q={busqueda}")
        hablar(f"Buscando {busqueda}")
    elif "abre discord" in texto:
        abrir_app("discord", "Discord")
    elif "abre steam" in texto:
        abrir_app("steam", "Steam")
    elif "abre riot" in texto or "abre lol" in texto or "abre league" in texto or "abre riot games" in texto:
        abrir_app("riot", "League of Legends")
    elif "apaga el pc" in texto or "apaga pc" in texto:
        hablar("Apagando el PC en 5 segundos")
        os.system("shutdown /s /t 5")
    elif "reinicia el pc" in texto or "reinicia pc" in texto:
        hablar("Reiniciando el PC en 5 segundos")
        os.system("shutdown /r /t 5")
    elif "sube el volumen" in texto:
        subir_volumen()
        hablar("Subiendo el volumen")
    elif "baja el volumen" in texto:
        bajar_volumen()
        hablar("Bajando el volumen")
    elif "silencia" in texto:
        silenciar_volumen()
        hablar("Silenciado")
    else:
        respuesta = preguntar_a_groq(texto)
        hablar(respuesta)
pygame.mixer.init()
hablar("Hola Maestro, soy Jarvis. ¿En qué puedo servirle hoy?")

while True:
    try:
        texto = escuchar()
        if not texto:
            continue
        activaciones = ["jarvis", "harvees", "harvis", "jarbes", "harvey", "jarbis", "harbis"]
        if any(palabra in texto.lower() for palabra in activaciones):
            hablar("Dígame, Maestro")
            comando = escuchar()
            if comando:
                if "adiós" in comando or "salir" in comando:
                    hablar("Hasta luego, Maestro. Ha sido un placer.")
                    break
                ejecutar_comando(comando)
    except Exception as e:
        print(f"Error recuperable: {e}")
