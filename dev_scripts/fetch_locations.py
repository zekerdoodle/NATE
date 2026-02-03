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
logger = logging.getLogger("fetch_locations")

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
    
    logger.info("Fetching organizations...")
    url = f"{client.base_url}/v2/organizations"
    params = {"pageSize": 100}
    # Access the protected _headers method or construct manually
    headers = client._headers()
    response = client.session.get(url, headers=headers, params=params, timeout=30)
    
    if response.status_code != 200:
        logger.error(f"Failed to fetch organizations: {response.status_code} {response.text}")
        return

    orgs = response.json()
    
    target_org_names = {"Automation", "Corporate", "Division C", "Service"}
    
    mapping = {}
    
    for org in orgs:
        name = org.get("name")
        
        if name in target_org_names:
            org_id = org.get("id")
            
            # Fetch locations for this org
            loc_url = f"{client.base_url}/v2/organization/{org_id}/locations"
            loc_resp = client.session.get(loc_url, headers=headers, timeout=30)
            locations = []
            if loc_resp.status_code == 200:
                locations = loc_resp.json()
            
            loc_map = {}
            for loc in locations:
                loc_map[loc["name"]] = loc["id"]
                
            mapping[name] = {
                "id": org_id,
                "locations": loc_map
            }
            logger.info(f"Found {name} (ID: {org_id}) with {len(locations)} locations.")
            
    # Save to config
    output_path = repo_root / "config" / "organization_map.json"
    with open(output_path, "w") as f:
        json.dump(mapping, f, indent=2)
        
    logger.info(f"Saved mapping to {output_path}")
    
    # Print for user verification
    print(json.dumps(mapping, indent=2))

if __name__ == "__main__":
    main()
