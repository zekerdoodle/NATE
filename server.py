import logging
import os
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Path as PathParam
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from chat_engine import ChatEngine
from chat_manager import ChatManager

# Configure logging
log_file = Path(__file__).parent / "logs" / "web_ui.log"
log_file.parent.mkdir(parents=True, exist_ok=True)

# Create a specific handler for Web UI related logs
web_file_handler = logging.FileHandler(log_file, encoding="utf-8")
web_file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))

# Attach handler to relevant loggers
web_loggers = [
    logging.getLogger(__name__),
    logging.getLogger("chat_engine"),
    logging.getLogger("chat_manager"),
    logging.getLogger("uvicorn"),
    logging.getLogger("uvicorn.access"),
    logging.getLogger("uvicorn.error"),
]

for log in web_loggers:
    log.addHandler(web_file_handler)
    log.setLevel(logging.INFO)

# Ensure basic logging is set up if running standalone
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO)

logger = logging.getLogger(__name__)

app = FastAPI(title="Nate Chat Interface")

# Mount static files
static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")

# Mount docs directory
docs_dir = Path(__file__).parent / "docs"
app.mount("/docs", StaticFiles(directory=docs_dir), name="docs")

# Mount dev_docs directory
dev_docs_dir = Path(__file__).parent / "dev_docs"
app.mount("/dev_docs", StaticFiles(directory=dev_docs_dir), name="dev_docs")

# Initialize Chat Engine & Manager
chat_engine: Optional[ChatEngine] = None
chat_manager: Optional[ChatManager] = None

@app.on_event("startup")
async def startup_event():
    global chat_engine, chat_manager
    repo_root = Path(__file__).parent
    chat_engine = ChatEngine(repo_root, system_instructions_path="config/web_ui_system_instructions.md")
    chat_manager = ChatManager(repo_root / ".chat_history.json")
    logger.info("Chat Engine and Manager initialized")

class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None

class ChatResponse(BaseModel):
    response: str
    citations: List[str] = []
    session_id: str

class SessionCreate(BaseModel):
    title: str = "New Chat"

class SessionUpdate(BaseModel):
    title: str

@app.get("/")
async def read_root():
    return FileResponse(static_dir / "index.html")

@app.get("/api/sessions")
async def list_sessions():
    if not chat_manager:
        raise HTTPException(status_code=500, detail="Chat manager not initialized")
    return chat_manager.list_sessions()

@app.post("/api/sessions")
async def create_session(session: SessionCreate):
    if not chat_manager:
        raise HTTPException(status_code=500, detail="Chat manager not initialized")
    return chat_manager.create_session(session.title)

@app.get("/api/sessions/{session_id}")
async def get_session(session_id: str):
    if not chat_manager:
        raise HTTPException(status_code=500, detail="Chat manager not initialized")
    session = chat_manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return session

@app.put("/api/sessions/{session_id}")
async def update_session(session_id: str, update: SessionUpdate):
    if not chat_manager:
        raise HTTPException(status_code=500, detail="Chat manager not initialized")
    session = chat_manager.update_session_title(session_id, update.title)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return session

@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str):
    if not chat_manager:
        raise HTTPException(status_code=500, detail="Chat manager not initialized")
    success = chat_manager.delete_session(session_id)
    if not success:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"status": "success"}

@app.post("/api/chat", response_model=ChatResponse)
async def chat_endpoint(request: ChatRequest):
    if not chat_engine or not chat_manager:
        raise HTTPException(status_code=500, detail="Components not initialized")
    
    session_id = request.session_id
    history = []
    
    if session_id:
        session = chat_manager.get_session(session_id)
        if session:
            history = session.get("history", [])
        else:
            # If session ID provided but not found, create new? Or error?
            # Let's create a new one for robustness if it's missing
            session = chat_manager.create_session("New Chat")
            session_id = session["id"]
    else:
        session = chat_manager.create_session("New Chat")
        session_id = session["id"]

    # Save user message
    chat_manager.add_message(session_id, "user", request.message)
    
    try:
        # Convert history format if needed. ChatManager stores {"role":..., "content":..., "timestamp":...}
        # ChatEngine expects [{"role":..., "content":...}]
        engine_history = [{"role": m["role"], "content": m["content"]} for m in history]
        
        response_text, citations = await chat_engine.process_message(request.message, engine_history)
        
        # Save assistant message
        chat_manager.add_message(session_id, "assistant", response_text)
        
        # Auto-update title if it's the first message and title is default
        if len(history) == 0 and session.get("title") == "New Chat":
             # Simple heuristic: use first few words of user message
             new_title = (request.message[:30] + '...') if len(request.message) > 30 else request.message
             chat_manager.update_session_title(session_id, new_title)

        return ChatResponse(response=response_text, citations=citations, session_id=session_id)
    except Exception as e:
        logger.exception("Error processing chat message")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
