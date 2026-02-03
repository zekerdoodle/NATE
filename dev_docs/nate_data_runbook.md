## Nate Data Refresh Runbook

This runbook covers the daily workflows that keep Nate's ticket inputs and semantic index fresh.

### 1. Ticket ingestion (`ticket_listener.py`)
- **Command**: `python ticket_listener.py --run-once`
- **Schedule**: run every 5 minutes in production (use Task Scheduler or systemd). The `--run-once` flag is safe for cron-style loops; omit it to let the listener sleep and poll continuously.
- **Credentials**: populate `api_keys.env` with `NinjaOne_ClientID`, `NinjaOne_ClientSecret`, and optionally `NinjaOne_BaseURL`. The listener automatically loads this file.
- **State**: parsed tickets land in `docs/tickets/`. Listener progress lives in `docs/tickets/.listener_state.json`. Reset with `python ticket_listener.py --reset-state --run-once` when migrating servers.

### 2. Embedding sync (`embed.py`)
- **Command**: `python embed.py sync`
- **Schedule**: nightly (after the last ticket pull) plus ad-hoc runs whenever documentation changes.
- **NLTK data**: the chunker downloads `punkt` and `punkt_tab` automagically, but locked-down servers may need an explicit bootstrap:
  ```powershell
  python -c "import nltk;nltk.download('punkt');nltk.download('punkt_tab')"
  ```
- **Artifacts**: FAISS index + metadata under `data/` (`embeddings.sqlite`, `vector.index`, `vector_ids.json`). Back up this folder before major upgrades.

### 3. Operational checks
1. Run `python model_call.py <ticket_id> --dry-run` to validate prompt wiring for any problematic ticket.
2. Execute `python app.py --run-once --disable-automation` to smoke-test ingestion without calling the model.
3. When ready for a full loop, run `python app.py --run-once` (requires `OPENAI_API_KEY` and working NinjaOne credentials). Inspect `docs/tickets/*.automation.json` for tool-call traces and `logs/` for scheduler output.

### 4. Troubleshooting tips
- Rebuild embeddings from scratch with `python embed.py rebuild` if `tools/search.py` raises schema or FAISS alignment errors.
- If the listener logs `AuthenticationError`, rotate the NinjaOne client credentials and restart the scheduled task.
- Clear stuck automation state via `python app.py --run-once --reset-state` (this resets both listener and automation markers; use sparingly).
