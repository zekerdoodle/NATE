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
    
    logger.info("Fetching technicians...")
    url = f"{client.base_url}/v2/users"
    params = {"userType": "TECHNICIAN"}
    
    headers = client._headers()
    response = client.session.get(url, headers=headers, params=params, timeout=30)
    
    if response.status_code != 200:
        logger.error(f"Failed to fetch users: {response.status_code} {response.text}")
        return

    users = response.json()
    print(f"Found {len(users)} technicians:")
    
    for user in users:
        uid = user.get("id")
        first = user.get("firstName")
        last = user.get("lastName")
        email = user.get("email")
        print(f"ID: {uid} | Name: {first} {last} | Email: {email}")

if __name__ == "__main__":
    main()
