import sys
from pathlib import Path
import logging

# Add repo root to path
repo_root = Path(__file__).resolve().parent.parent
sys.path.append(str(repo_root))

from tools import get_ticket

logging.basicConfig(level=logging.INFO)

def main():
    ticket_id = 35146
    print(f"Attempting to fetch ticket {ticket_id}...")
    try:
        result = get_ticket.run({"ticket_id": ticket_id}, repo_root=repo_root)
        print("Success!")
        print(f"Subject: {result.get('subject')}")
        print(f"Status: {result.get('status')}")
        print(f"Log entries: {result.get('parsed_log_entry_count')}")
    except Exception as e:
        print(f"Failed: {e}")

if __name__ == "__main__":
    main()
