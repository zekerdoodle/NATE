# NATE - AI-Powered IT Ticket Routing System

**NATE** (NinjaOne Automated Ticketing Entity) is a production LLM-powered system that automates IT support ticket triage, routing, and initial response generation.

## Impact

- **Reduced average ticket response time from 2 hours to under 5 minutes**
- Handles ~200 tickets/day with semantic search across internal documentation
- Frees IT staff to focus on complex issues instead of routine triage

## How It Works

NATE integrates with NinjaOne (IT management platform) and uses GPT-4 with custom tool calling to:

1. **Automated Ticket Processing**
   - Monitors incoming tickets in real-time
   - Searches internal knowledge base using semantic embeddings (FAISS + Nomic embeddings)
   - Categorizes and routes tickets to appropriate technicians based on expertise
   - Drafts initial responses with relevant documentation links

2. **Interactive Assistant**
   - Web chat interface for technicians to query tickets and documentation
   - Context-aware responses that pull in relevant ticket history

## Architecture

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│   NinjaOne API  │────▶│   Ticket Worker  │────▶│   LLM (GPT-4)   │
└─────────────────┘     │   + Listener     │     │   + Tool Use    │
                        └──────────────────┘     └────────┬────────┘
                                                          │
                        ┌──────────────────┐              │
                        │  FAISS + SQLite  │◀─────────────┘
                        │  (Semantic Search)│
                        └──────────────────┘
```

**Key Components:**
- `app.py` - Main entry point, orchestrates polling and web server
- `ticket_worker.py` - Background automation that processes tickets
- `model_call.py` - LLM invocation with structured tool calling
- `embed.py` - Semantic search using FAISS vector index
- `tools/` - Custom tool implementations (search, update ticket, etc.)

## Tech Stack

- **Python 3.10+**
- **OpenAI GPT-4** with function calling
- **FAISS** for vector similarity search
- **Nomic embeddings** (local, CPU-friendly)
- **SQLite** for metadata storage
- **NinjaOne API** for ticket management

## Setup

1. Copy `api_keys.env.example` to `api_keys.env` and add credentials
2. Install dependencies: `pip install -r requirements.txt`
3. Initialize search index: `python embed.py sync`
4. Run: `python app.py`

### CLI Options

```bash
python app.py [options]
  --poll-interval <sec>    Polling frequency (default: 60)
  --disable-automation     Run web UI only
  --test-mode              Safe mode for development
  --verbose                Debug logging
```

## Project Structure

```
├── app.py                 # Main entry point
├── ticket_listener.py     # NinjaOne API client
├── ticket_worker.py       # Background ticket processor
├── model_call.py          # LLM orchestration
├── embed.py               # Semantic search engine
├── config/                # System prompts and settings
├── tools/                 # LLM tool implementations
├── static/                # Web UI (HTML/CSS/JS)
└── tests/                 # Test suite
```

## License

MIT
