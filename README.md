# Nate

Nate (NinjaOne Automated Ticketing Entity) is an AI-powered IT support assistant designed to integrate with NinjaOne. It automates ticket triage, provides responses, and offers a chat interface for technicians.

## What Nate Does

Nate operates as a member of the IT team, performing two primary functions:

1.  **Automated Ticket Handling**: Nate monitors the NinjaOne ticketing board. When new tickets arrive or existing ones are updated, Nate analyzes them, searches the internal knowledge base (documentation, past tickets, technician info), and takes action. Actions can include:
    *   Categorizing and tagging tickets.
    *   Assigning tickets to the appropriate technician based on expertise and availability.
    *   Drafting and posting public comments to users with potential solutions.
    *   Posting private internal notes for technicians.

2.  **Interactive Chat Assistant**: Nate provides a web-based chat interface where technicians can ask questions, get help with troubleshooting, or query specific ticket details.

## Modes of Operation

Nate runs as a single application but serves two distinct "personalities" based on the context:

### 1. Ninja Nate (Background Automation)
*   **Role**: Autonomous IT Technician.
*   **Trigger**: Runs automatically in the background when tickets are polled from NinjaOne.
*   **Configuration**: Uses `config/system_instructions.md`.
*   **Behavior**: Strictly follows the IT department's protocols for triage, assignment, and user communication. It acts *on* tickets directly.

### 2. UI Nate (Web Interface)
*   **Role**: Helpful Chat Assistant.
*   **Trigger**: Accessed via the Web UI (default: `http://localhost:8000`).
*   **Configuration**: Uses `config/web_ui_system_instructions.md`.
*   **Behavior**: Designed to assist the human user. It has access to the same tools and knowledge base as Ninja Nate but follows a different set of instructions tailored for conversation and assistance rather than autonomous ticket management.
*   **Context**: You can reference tickets in chat (e.g., "What is happening with ticket #1234?") to pull their context into the conversation.

Both modes share the same underlying intelligence (LLM), toolset (Search, Read File, Update Ticket), and database.

## How to Run

### Prerequisites
1.  Ensure you have a valid `api_keys.env` file with necessary credentials (OpenAI, NinjaOne).
2.  Install dependencies: `pip install -r requirements.txt`.
3.  Initialize the embedding database (if running for the first time): `python embed.py sync`.

### Main Application
The primary entry point runs both the background ticket listener/worker and the web server.

```bash
python app.py [options]
```

#### Available CLI Switches
*   `--poll-interval <seconds>`: Seconds between ticket polling runs (default: 60).
*   `--page-size <number>`: Number of tickets to request per board call (default: 200).
*   `--automation-interval <seconds>`: Seconds between automation worker runs (default: 45).
*   `--disable-automation`: Disable the background automation worker (useful if you only want the Web UI or Listener).
*   `--run-once`: Process a single ticket poll run and automation cycle, then exit.
*   `--reset-state`: Reset persisted poll state before starting (re-processes recent tickets).
*   `--test-mode`: **Safety Feature**. Only processes tickets assigned to a specific technician created "today". Useful for development.
*   `--verbose`: Enable debug logging.

### Manual Model Invocation
You can manually invoke Nate's logic on a specific ticket file or ID for testing purposes.

```bash
python model_call.py <ticket_id_or_path> [options]
```

#### Options
*   `--config <path>`: Path to model configuration file (default: `config/nate_model_config.json`).
*   `--dry-run`: Build the prompt and tools but skip the actual API call to the LLM.
*   `--max-output-tokens <number>`: Limit the response length.
*   `--verbose`: Enable debug logging.

### Embedding Management
To manage the semantic search index:

```bash
python embed.py sync
```
