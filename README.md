# Jarvis — Voice Assistant powered by AI

A real-time, voice-controlled desktop assistant built in Python. Jarvis listens, understands natural language, replies with a synthesized voice and a butler-like personality, and can control your computer — all driven by a chain of AI services and wrapped in a custom futuristic HUD.

> A personal project to explore real-time audio processing, speech-to-text, large language models and text-to-speech working together end to end.

---

## 🎥 Demo

> _Add a short demo video or GIF here (30–45s): say "Jarvis, open YouTube", ask a question, change the volume — show it responding by voice with the HUD reacting. This is the most impactful part of the project._

---

## ✨ Features

- **End-to-end voice conversation** — captures microphone audio, transcribes it with **Whisper**, generates a reply with an **LLM (Llama 3.3 via Groq)** and speaks it back using **ElevenLabs** text-to-speech.
- **Smart voice detection** — a custom Voice Activity Detection (VAD) system based on audio energy levels, with **automatic ambient-noise calibration** so recording starts and stops hands-free.
- **Conversation memory** — keeps recent context so replies stay coherent across turns.
- **Personality** — responds in character as an elegant, witty butler (configurable via the system prompt).
- **Command execution** — opens applications and websites, runs web searches, controls system volume, and can shut down or restart the PC by voice.
- **Real-time visual HUD** — a futuristic interface that reacts live to Jarvis's state (`speaking` / `idle`) through a lightweight HTTP server using Server-Sent Events (SSE).

---

## 🛠️ Tech Stack

| Area | Technology |
|------|------------|
| Language | Python |
| Speech-to-text | Whisper (via Groq) |
| Language model | Llama 3.3 70B (via Groq) |
| Text-to-speech | ElevenLabs |
| Audio | `sounddevice` · `numpy` · `pygame` |
| Real-time UI | HTTP server + Server-Sent Events (SSE) |

---

## 🚀 Setup

### Requirements
- Python 3.10+
- A working microphone
- A Groq API key and an ElevenLabs API key

### Installation

```bash
git clone https://github.com/IvanReichle/<your-repo>.git
cd <your-repo>

pip install sounddevice numpy pygame groq elevenlabs
```

### Configuration

The bot reads its credentials from environment variables (no secrets in the code):

| Variable | Description |
|----------|-------------|
| `GROQ_API_KEY` | Your Groq API key |
| `ELEVENLABS_API_KEY` | Your ElevenLabs API key |

To use the "open application" commands, edit the `APP_PATHS` dictionary near the top of the file with the paths to the apps on your own machine.

### Run

```bash
python jarvis.py
```

Say the wake word (**"Jarvis"**), then give a command — for example *"open YouTube"*, *"search for the weather"*, or simply ask a question and Jarvis will answer.

---

## 📚 What this project demonstrates

- Chaining multiple AI services (speech-to-text, LLM, text-to-speech) into a single real-time pipeline.
- Real-time audio capture and processing, including a custom voice-activity detector.
- Prompt design for a consistent conversational persona.
- Backend-to-frontend communication using Server-Sent Events.
- Clean separation of configuration (API keys and paths) from logic.
