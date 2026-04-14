# Zomato Voice Agent Server

A real-time voice support backend that bridges browser clients to Google's **Gemini Multimodal Live API** over WebSockets. It streams bidirectional PCM audio, dispatches tool calls against a MongoDB database, and powers an AI customer support agent that can look up orders, process refunds, file complaints, and escalate to humans — all through natural voice conversation.

<br>

## Architecture

```
Browser (Next.js Frontend)
    │
    │  WebSocket  (/ws/voice?user_id=N)
    │  ▲ PCM 16-bit audio (16 kHz)   ▼ PCM 16-bit audio (24 kHz) + JSON
    │
┌───▼────────────────────────────────────────────────┐
│              Voice Agent Server (FastAPI)           │
│                                                    │
│  ┌────────────────┐     ┌────────────────────────┐ │
│  │  WebSocket Hub │◄───►│  Gemini Live API Proxy │ │
│  │  (client I/O)  │     │  (wss://generative...) │ │
│  └────────────────┘     └──────────┬─────────────┘ │
│                                    │               │
│                         ┌──────────▼─────────────┐ │
│                         │    Tool Dispatcher      │ │
│                         │  get_user_profile       │ │
│                         │  check_order_status     │ │
│                         │  get_order_details      │ │
│                         │  initiate_refund        │ │
│                         │  file_complaint         │ │
│                         │  escalate_to_human      │ │
│                         └──────────┬─────────────┘ │
│                                    │               │
│                         ┌──────────▼─────────────┐ │
│                         │   MongoDB (Motor async) │ │
│                         └────────────────────────┘ │
└────────────────────────────────────────────────────┘
```

<br>

## Tech Stack

- **Framework:** [FastAPI](https://fastapi.tiangolo.com/) with native WebSocket support
- **ASGI Server:** [Uvicorn](https://www.uvicorn.org/)
- **AI Model:** [Gemini Multimodal Live API](https://ai.google.dev/gemini-api/docs/multimodal-live) (`gemini-3.1-flash-live-preview`)
- **Database Driver:** [Motor](https://motor.readthedocs.io/) (async MongoDB driver)
- **WebSocket Client:** [websockets](https://websockets.readthedocs.io/) (for Gemini connection)
- **Runtime:** Python 3.11+
- **Deployment:** Docker → Google Cloud Run

<br>

## Project Structure

```
voice_agent_server/
├── main.py              # FastAPI app, WebSocket handler, Gemini proxy logic
├── tools.py             # Tool implementations (MongoDB queries, refund logic)
├── requirements.txt     # Python dependencies
├── Dockerfile           # Production container image
├── .dockerignore        # Files excluded from Docker build
└── .env                 # Environment variables (not committed)
```

<br>

## Getting Started

### Prerequisites

- **Python** 3.11+
- A **Google AI API key** with access to the Gemini Multimodal Live API
- A **MongoDB Atlas** cluster (or local MongoDB instance)

### 1. Create a Virtual Environment

```bash
python -m venv venv
source venv/bin/activate
```

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure Environment Variables

Create a `.env` file in the project root (parent directory) or in `voice_agent_server/`:

```env
GEMINI_API_KEY=your-google-ai-api-key
MONGODB_URI=mongodb+srv://<username>:<password>@<cluster>.mongodb.net/?appName=<app>
```

| Variable | Description |
|---|---|
| `GEMINI_API_KEY` | Google AI API key for the Gemini Multimodal Live API |
| `MONGODB_URI` | MongoDB connection string for order, user, and ticket data |

### 4. Run the Server

```bash
uvicorn main:app --host 0.0.0.0 --port 8080
```

The server will be available at `ws://localhost:8080/ws/voice`.

<br>

## WebSocket Protocol

### Connection

```
ws://host:port/ws/voice?user_id=<int>
```

The `user_id` query parameter identifies the customer for the session. All tool calls will default to this user.

### Client → Server

- **Binary frames:** Raw 16-bit PCM audio at 16 kHz, mono. The server base64-encodes and forwards to Gemini as `realtimeInput.audio`.

### Server → Client

- **Binary frames:** Raw 16-bit PCM audio at 24 kHz from Gemini's voice response. Play directly via `AudioContext`.
- **Text frames (JSON):**

```jsonc
// Agent speech transcript
{ "type": "text", "text": "Let me check your order..." }

// Tool execution indicator
{ "type": "text", "text": "⏳ Checking your orders..." }

// Error message
{ "type": "error", "message": "Voice agent connection failed: ..." }
```

<br>

## Tools

The agent has access to six server-side tools that execute against MongoDB. Gemini decides when to call them based on conversation context.

| Tool | Description |
|---|---|
| `get_user_profile` | Fetches name, phone, email, wallet balance, total orders, lifetime spend (LTV), and customer tier |
| `check_order_status` | Lists all user orders with status, amount, timestamps, and refund eligibility |
| `get_order_details` | Detailed view of a single order including hours since delivery and refund window status |
| `initiate_refund` | Processes a refund — enforces the 2-hour delivery window and calculates amount based on LTV tier |
| `file_complaint` | Files a structured complaint (categories: `food_quality`, `late_delivery`, `missing_items`, `wrong_order`, `hygiene`, `other`) |
| `escalate_to_human` | Creates an escalation ticket and signals handoff to a human agent |

### Refund Policy

Refunds are governed by two rules:

1. **Time window:** Only orders delivered within the last **2 hours** are eligible.
2. **LTV-based amount:** The refund percentage scales with customer lifetime spend:

| Lifetime Spend | Refund Percentage |
|---|---|
| ₹10,000+ | 100% |
| ₹5,000+ | 75% |
| ₹2,000+ | 50% |
| Below ₹2,000 | 30% |

<br>

## System Prompt

The server configures Gemini with a detailed system instruction that defines the agent's personality and operational rules:

- **Tone:** Natural, empathetic, concise — like a real phone support agent
- **Primary goal:** Resolve issues while minimizing refund costs
- **Refund handling:** Always check user profile and order eligibility first; offer alternatives (complaints, re-delivery) before processing
- **Escalation:** Only when the customer is genuinely distressed or explicitly requests a human
- **Voice model:** `Kore` (Gemini prebuilt voice)

<br>

## Deployment

### Google Cloud Run (Recommended)

Build and deploy the container directly:

```bash
gcloud run deploy voice-server-agent \
  --source . \
  --region <your-region> \
  --allow-unauthenticated \
  --set-env-vars "GEMINI_API_KEY=<key>,MONGODB_URI=<uri>"
```

Cloud Run automatically sets the `PORT` environment variable. The Dockerfile reads it via `${PORT}`.

### Docker (Self-Hosted)

```bash
docker build -t voice-agent-server .
docker run -p 8080:8080 \
  -e GEMINI_API_KEY=<key> \
  -e MONGODB_URI=<uri> \
  voice-agent-server
```

### Local Development

```bash
uvicorn main:app --host 0.0.0.0 --port 8080 --reload
```

<br>

## Health Check

```bash
curl http://localhost:8080/
# → {"status": "ok", "service": "voice-agent-server"}
```

<br>

## License

This project is part of the [Zomato Live Agent](../) monorepo. See the root [LICENSE](../LICENSE) for details.
