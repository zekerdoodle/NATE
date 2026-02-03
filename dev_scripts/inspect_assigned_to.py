import os
import sys
import json
import logging
from pathlib import Path
from dotenv import load_dotenv

# Add the repo root to sys.path
repo_root = Path(__file__).resolve().parent.parent
sys.path.append(str(repo_root))

from ticket_listener import NinjaOneClient

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("fetch_users")

def main():
    load_dotenv(repo_root / "api_keys.env")
    
    client_id = os.getenv("NinjaOne_ClientID")
    client_secret = os.getenv("NinjaOne_ClientSecret")
    base_url = os.getenv("NinjaOne_BaseURL", "https://app.ninjarmm.com")
    refresh_token = os.getenv("NINJA_REFRESH_TOKEN")
    
    if not all([client_id, client_secret, refresh_token]):
        logger.error("Missing credentials.")
        return

    client = NinjaOneClient(
        base_url=base_url,
        client_id=client_id,
        client_secret=client_secret,
        refresh_token=refresh_token
    )
    
    logger.info("Authenticating...")
    client.authenticate()
    
    logger.info("Fetching users...")
    # Try to find an endpoint for users. Usually /v2/users or similar.
    # Based on common NinjaOne API patterns.
    url = f"{client.base_url}/v2/users" 
    # Note: The exact endpoint might differ. If this fails, we might need to look at the spec again or try /v2/system/users
    
    # Let's try to list tickets and see the assignedTo structure from a real ticket
    logger.info("Fetching recent tickets to inspect assignedTo structure...")
    boards = client.get_boards()
    if not boards:
        logger.error("No boards found.")
        return

    board_id = boards[0]['id']
    tickets = client.run_board(board_id, page_size=10)
    
    for ticket in tickets:
        t_id = ticket.get('id')
        full_ticket = client.get_ticket_with_logs(t_id)
        assigned_to = full_ticket.get('assignedTo')
        if assigned_to:
            print(f"Ticket {t_id} assignedTo: {json.dumps(assigned_to, indent=2)}")
            
            # Check the assignee details
            first = assigned_to.get('firstName')
            last = assigned_to.get('lastName')
            name = assigned_to.get('name')
            print(f"  -> firstName: {first}, lastName: {last}, name: {name}")

if __name__ == "__main__":
    main()
