# 🤖 Jarvis — Asistente de Voz con IA

Asistente de voz personal para Windows construido en Python. Escucha, entiende, responde con voz sintetizada y controla tu PC por completo.

## ✨ Características

- 🎙️ **Modo conversación libre** — habla directamente sin necesitar wake word
- 🔊 **Wake word local** — detección offline con openWakeWord (sin API key)
- 🧠 **IA con Claude** (Anthropic) — respuestas inteligentes y contextuales
- 🗣️ **Voz sintetizada en tiempo real** con ElevenLabs TTS (streaming)
- 👂 **Transcripción ultrarrápida** con Groq Whisper STT
- 💾 **Memoria persistente** — recuerda conversaciones anteriores
- 🖥️ **Control del PC por voz** — abre apps, busca en Google, cierra programas
- ⚙️ **Configurable** — perfil de usuario, rutas de apps, ajustes de voz en JSON

## 🔄 Modos de escucha

| Modo | Cómo activarlo | Comportamiento |
|------|---------------|----------------|
| **Libre** (por defecto) | Al arrancar / di *"modo libre"* | Escucha continua, habla directamente |
| **Normal** | Di *"modo normal"* | Espera el wake word antes de cada comando |

## 🗣️ Comandos de voz de ejemplo

- *"Abre Discord"* / *"Abre Chrome"* / *"Abre Steam"*
- *"Busca en Google [lo que sea]"*
- *"¿Qué tiempo hace?"*
- *"Modo normal"* / *"Modo libre"*
- *"Adiós Jarvis"* — cierra el programa

## 🛠️ Tecnologías

- **Python 3.11+**
- **Groq Whisper** — transcripción de voz (STT)
- **Claude AI** (Anthropic) — motor de razonamiento
- **ElevenLabs** — síntesis de voz (TTS) en streaming
- **openWakeWord** — detección de wake word offline
- **SoundDevice** — captura de audio
- **Pygame** — reproducción de audio
- **Tkinter + Pillow** — interfaz visual (HUD)

## 🚀 Instalación

### 1. Clona el repositorio
```bash
git clone https://github.com/IvanReichle/Jarvis-Voice-Assistant-powered-by-AI.git
cd Jarvis-Voice-Assistant-powered-by-AI
```

### 2. Crea el entorno virtual
```bash
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### 3. Instala dependencias
```bash
pip install -r requirements.txt
```

### 4. Configura tus credenciales

Crea un archivo `.env` en la raíz con tus API keys:
```
GROQ_API_KEY=tu_api_key_de_groq
ELEVENLABS_API_KEY=tu_api_key_de_elevenlabs
ANTHROPIC_API_KEY=tu_api_key_de_anthropic
```

### 5. Configura tu perfil

Copia el archivo de ejemplo y personalízalo:
```bash
copy jarvis_config.example.json jarvis_config.json
```

Edita `jarvis_config.json` con:
- Tu nombre
- Rutas de tus apps
- Tu Voice ID de ElevenLabs
- Ajustes de voz

### 6. Arranca Jarvis
```bash
python jarvis.py
```

## ⚙️ Configuración (`jarvis_config.json`)

```json
{
    "perfil_usuario": {
        "nombre": "TuNombre",
        "notas": "Descripción de tu perfil para que Jarvis te conozca"
    },
    "app_paths": {
        "discord": "C:\\Users\\TU_USUARIO\\AppData\\Local\\Discord\\...",
        "chrome": "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"
    },
    "voice": {
        "elevenlabs_voice_id": "TU_VOICE_ID",
        "model_id": "eleven_turbo_v2_5",
        "speed": 1.1
    },
    "conversacion": {
        "free_talk": true
    }
}
```

## 📦 Estructura del proyecto

```
Jarvis-Voice-Assistant-powered-by-AI/
├── jarvis.py                   # Código principal
├── jarvis_config.example.json  # Plantilla de configuración
├── requirements.txt            # Dependencias
├── .env                        # API keys (no subir a Git)
├── jarvis_config.json          # Tu config personal (no subir a Git)
└── jarvis_memoria.json         # Memoria de conversaciones (no subir a Git)
```

## 🔑 API Keys necesarias

| Servicio | Para qué | Plan gratuito |
|---------|---------|--------------|
| [Groq](https://console.groq.com) | Transcripción de voz (Whisper) | ✅ Sí |
| [Anthropic](https://console.anthropic.com) | IA (Claude) | ✅ Créditos iniciales |
| [ElevenLabs](https://elevenlabs.io) | Voz sintetizada (TTS) | ✅ 10k chars/mes |
