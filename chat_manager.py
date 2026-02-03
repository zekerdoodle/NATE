import json
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any

logger = logging.getLogger(__name__)

class ChatManager:
    def __init__(self, storage_path: Path):
        self.storage_path = storage_path
        self.sessions: Dict[str, Dict[str, Any]] = {}
        self._load_sessions()

    def _load_sessions(self):
        if self.storage_path.exists():
            try:
                with self.storage_path.open("r", encoding="utf-8") as f:
                    self.sessions = json.load(f)
            except Exception as e:
                logger.error(f"Failed to load chat history: {e}")
                self.sessions = {}
        else:
            self.sessions = {}

    def _save_sessions(self):
        try:
            with self.storage_path.open("w", encoding="utf-8") as f:
                json.dump(self.sessions, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save chat history: {e}")

    def create_session(self, title: str = "New Chat") -> Dict[str, Any]:
        session_id = str(uuid.uuid4())
        timestamp = datetime.now().isoformat()
        session = {
            "id": session_id,
            "title": title,
            "created_at": timestamp,
            "updated_at": timestamp,
            "history": []
        }
        self.sessions[session_id] = session
        self._save_sessions()
        return session

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        return self.sessions.get(session_id)

    def list_sessions(self) -> List[Dict[str, Any]]:
        # Return list sorted by updated_at desc
        summary_list = []
        for s_id, s_data in self.sessions.items():
            summary_list.append({
                "id": s_id,
                "title": s_data.get("title", "Untitled"),
                "updated_at": s_data.get("updated_at", "")
            })
        return sorted(summary_list, key=lambda x: x["updated_at"], reverse=True)

    def update_session_title(self, session_id: str, title: str) -> Optional[Dict[str, Any]]:
        if session_id in self.sessions:
            self.sessions[session_id]["title"] = title
            self.sessions[session_id]["updated_at"] = datetime.now().isoformat()
            self._save_sessions()
            return self.sessions[session_id]
        return None

    def add_message(self, session_id: str, role: str, content: str):
        if session_id in self.sessions:
            message = {"role": role, "content": content, "timestamp": datetime.now().isoformat()}
            self.sessions[session_id]["history"].append(message)
            self.sessions[session_id]["updated_at"] = datetime.now().isoformat()
            self._save_sessions()

    def delete_session(self, session_id: str) -> bool:
        if session_id in self.sessions:
            del self.sessions[session_id]
            self._save_sessions()
            return True
        return False
