import os
import sys
import logging
import json
from pathlib import Path
from dotenv import load_dotenv

# Add the repo root to sys.path so we can import modules
repo_root = Path(__file__).resolve().parent.parent
sys.path.append(str(repo_root))

from ticket_listener import NinjaOneClient
from ticket_parser import TicketParser
from model_call import NateModelCaller, NateModelConfig

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("test_ticket_flow")

def main():
    # Load environment variables
    load_dotenv(repo_root / "api_keys.env")
    
    client_id = os.getenv("NinjaOne_ClientID")
    client_secret = os.getenv("NinjaOne_ClientSecret")
    base_url = os.getenv("NinjaOne_BaseURL", "https://app.ninjarmm.com")
    refresh_token = os.getenv("NINJA_REFRESH_TOKEN")
    
    if not all([client_id, client_secret, refresh_token]):
        logger.error("Missing credentials in api_keys.env")
        return

    logger.info("Initializing NinjaOne Client...")
    client = NinjaOneClient(
        base_url=base_url,
        client_id=client_id,
        client_secret=client_secret,
        refresh_token=refresh_token
    )
    
    ticket_id = 34528
    logger.info(f"Fetching ticket {ticket_id}...")
    
    try:
        detailed_ticket = client.get_ticket_with_logs(ticket_id)
    except Exception as e:
        logger.error(f"Failed to fetch ticket: {e}")
        return
        
    logger.info("Parsing and saving ticket...")
    tickets_dir = repo_root / "docs" / "tickets"
    parser = TicketParser(tickets_dir)
    
    # We need to mock the image downloader or pass the client's method
    output_path = parser.parse_and_save(
        detailed_ticket,
        board="Test Board", # Board name doesn't matter much for this test
        image_downloader=client.download_image
    )
    logger.info(f"Ticket saved to {output_path}")
    
    logger.info("Initializing Nate Model Caller...")
    config_path = repo_root / "config" / "nate_model_config.json"
    config = NateModelConfig.load(config_path)
    
    caller = NateModelCaller(repo_root, config)
    
    logger.info("Running Nate on the ticket...")
    try:
        result = caller.invoke(str(output_path))
        
        logger.info("--- Model Run Result ---")
        logger.info(f"Status: {result.status}")
        logger.info(f"Response ID: {result.response_id}")
        
        if result.tool_calls:
            logger.info(f"Tool Calls ({len(result.tool_calls)}):")
            for tc in result.tool_calls:
                logger.info(f"  - {tc.name}: {json.dumps(tc.arguments)}")
                if tc.output:
                     logger.info(f"    Output: {str(tc.output)[:200]}...")
                if tc.error:
                    logger.error(f"    Error: {tc.error}")
        else:
            logger.info("No tool calls made.")
            
        if hasattr(result.response, 'output_text'):
             logger.info(f"Output Text: {result.response.output_text}")

    except Exception as e:
        logger.exception(f"Model run failed: {e}")
    finally:
        client.close()

if __name__ == "__main__":
    main()
