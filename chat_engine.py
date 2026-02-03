import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import List, Tuple, Dict, Any, Optional

from dotenv import load_dotenv

from model_call import NateModelCaller, NateModelConfig, DEFAULT_CONFIG_PATH
from tools.tool_schemas import get_tool_schemas
from ticket_listener import NinjaOneClient

logger = logging.getLogger(__name__)

class ChatEngine:
    def __init__(self, repo_root: Path, system_instructions_path: str = "config/system_instructions.md"):
        self.repo_root = repo_root
        load_dotenv(repo_root / "api_keys.env")
        
        # Setup NinjaOne
        client_id = os.getenv("NinjaOne_ClientID")
        client_secret = os.getenv("NinjaOne_ClientSecret")
        base_url = os.getenv("NinjaOne_BaseURL", "https://app.ninjarmm.com")
        refresh_token = os.getenv("NINJA_REFRESH_TOKEN")
        
        if client_id and client_secret:
             self.ninja_client = NinjaOneClient(
                base_url=base_url,
                client_id=client_id,
                client_secret=client_secret,
                refresh_token=refresh_token
            )
        else:
            self.ninja_client = None
            logger.warning("NinjaOne credentials missing, live ticket lookup disabled")

        # Setup NateModelCaller
        config_path = repo_root / DEFAULT_CONFIG_PATH
        try:
            self.config = NateModelConfig.load(config_path)
            
            # Override system instructions if a specific path is provided
            if system_instructions_path:
                instr_path = repo_root / system_instructions_path
                if instr_path.exists():
                    with instr_path.open("r", encoding="utf-8") as f:
                        self.config.system_instructions = f.read()
                else:
                    logger.warning(f"System instructions file not found: {instr_path}, using default.")

            self.caller = NateModelCaller(repo_root, self.config)
        except Exception as e:
            logger.error(f"Failed to initialize NateModelCaller: {e}")
            raise

    async def process_message(self, message: str, history: List[Dict[str, str]]) -> Tuple[str, List[str]]:
        """
        Process a user message and return a response with citations.
        """
        context_items = []
        citations = []
        ticket_data = {}

        # 1. Detect and Fetch Ticket IDs
        ticket_ids = re.findall(r'#(\d+)', message)
        for tid in ticket_ids:
            if self.ninja_client:
                try:
                    # Run synchronous NinjaOne call in a thread
                    t_data = await asyncio.to_thread(self.ninja_client.get_ticket_details, int(tid))
                    ticket_data = t_data # Use the last found ticket as the primary context for tools
                    context_items.append(f"Ticket #{tid} Details:\n{json.dumps(t_data, indent=2)}")
                except Exception as e:
                    logger.error(f"Failed to fetch ticket {tid}: {e}")
                    context_items.append(f"Could not fetch details for Ticket #{tid}. Error: {e}")

        # 2. Construct Messages
        # NateModelCaller expects a list of dicts for messages
        messages = [{"role": "system", "content": [{"type": "input_text", "text": self.config.system_instructions}]}]
        
        # Add history
        for msg in history[-10:]: 
            # Ensure history format matches what the model expects (content as list or string)
            # The chat manager stores simple strings, so we wrap them if needed or leave as is
            # NateModelCaller handles standard OpenAI message format
            messages.append(msg)
            
        # Add context if any
        if context_items:
            context_str = "\n\n".join(context_items)
            message = f"Context Information:\n{context_str}\n\nUser Query: {message}"

        messages.append({"role": "user", "content": [{"type": "input_text", "text": message}]})

        # 3. Run Conversation via NateModelCaller
        try:
            tool_schemas = get_tool_schemas()
            
            # Run the blocking model call in a thread
            result = await asyncio.to_thread(
                self.caller.run_conversation,
                messages=messages,
                tool_schemas=tool_schemas,
                ticket_data=ticket_data
            )
            
            output_text = getattr(result.response, "output_text", None)
            if not output_text:
                 # Fallback if output_text is not directly available (depending on response type)
                 # But NateModelCaller usually returns a response object with output_text for the new API
                 # If it's a raw dictionary or other object, we might need to extract it.
                 # Based on dev_docs, result.output_text is standard for the new API.
                 pass

            return output_text or "No response generated.", citations

        except Exception as e:
            logger.error(f"Error in chat loop: {e}")
            return f"I encountered an error: {e}", []

    def _build_system_prompt(self) -> str:
        return ""

import os
