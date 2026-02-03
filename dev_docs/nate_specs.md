# NATE (NinjaOne Automated Ticketing Entity)  # 

- $ = Review/edit later

## When to prompt NATE
- When a new ticket comes in
- When a ticket that is assigned to Nate is updated

## Prompt Construction
- Date / Time
- System instructions: See 'System Instructions' 
- Ticket Information: Ticket subject, comment(s), requester, number, etc
- Relevant information: Requester's employee information, technician information, recent tickets from requester (within last week)
- Tool schemas

## Available Tools
- **search**: Accepts a search query. Returns relevant document snips, ticket information, or other knowledge base information. Also accepts a specific database (documents, tickets, or non-standard) Always returns references that can be easily inputted to read_file
- **update_ticket**: Allows Nate to interact with NinjaOne tickets, including the following parameters: private_comment, public_comment, ticket_status, assignee, location, organization 
- **read_file**: Accepts a relative file path. Optionally accepts line numbers. Returns the contents of the requested file. 
- **native_web_search**: Enable the models' native web search functionality (OpenAI/Grok have their own search tools) for up-to-date knowledge sourcing

## Data Sources
- Tickets: Run a periodic (daily?) ticket pull. Store in a machine readable format. Embed the ticket description + subject. Store embeddings for search. 
- Documentation: A folder location with documentation intentionally placed by IT team including PDFs, TXTs, MD files, etc. 
- Employee Information: A file containing employee name, manager, email address, devices, location, job title
- Technician Information: A file containing technician name, specialties (what types of tickets to assign to them), presence (hours of operation, noting vacations, etc)
- NATE's Documentation: A folder containing NATE's custom made documentation

## File Structure
- model_call.py: Makes the call to the OpenAI API
- docs/: Contains all documentation from data sources
    - tickets/: Contains ticket information, pulled from ninja periodically
    - it_docs/: Contains IT Documentation like how-tos and SOPs
    - emp_info/: Contains a file with employee name, manager, email address, devices, location, job title
    - tech_info/: Contains a file with technician name, specialties (what types of tickets to assign to them), presence (hours of operation, noting vacations, etc)
    - nate_docs/: A folder containing NATE's custom made documentation
    - tech_notes/: A folder containing a file. This text file is simply some advice written by the techs. These are 'corrections' or 'rules' that need to be taken seriously.
- ticket_listener.py: Awaits tickets to come in. Passes raw ticket data to ticket_parser.py
- ticket_parser.py: Parses ticket information and formats cleanly to inject in prompt, and into tickets/. Strips signatures / irrelevant pictures
- embed.py: Embeds clean data from docs/ for semantic retreival
- tools/: Contains tool modules
    - tool_schemas.py: extracts clean tool schemas from each tool file in tools/ dir to send to the model provider
    - search.py: accepts a semantic search query, data source (defaults to all), title (filename or ticket number)
    - update_ticket.py: allows NATE to interact with NinjaOne tickets, including the following parameters: private_comment, public_comment, ticket_status, assignee, location, organization 

## Notes
- We likely need an intelligent model... GPT-5 Low, Grok-4-fast, etc.
- Model provider *needs* to accept image input. When a ticket comes through with an image, that image needs to be sent through to the model provider inline alongside the rest of the ticket information. 
- Will need a way to easily exclude NATE from seeing tickets
- 'Private mode': When enabled, anything set as a public note is diverted to a private_comment + status diverts from 'Resolved' to 'Open' (NOTHING changes between what Nate receives or does, only our interpretations of his outputs)


---

```
## System Instructions

# Role
You are *Nate*, a member of the IT Team at your company. 

# Job Description
You will be prompted with helpdesk tickets. Some of which are new, and some of which you (or others) have worked on before. Your job is to assign certain attributes to the ticket, and then take the next steps. 

## Assigning Attributes: 
Each ticket you receive is required to have *tags*, *organization*, and *location* assigned. 
- *Tags*: Assign appropriate tags from the provided list. We're required to assign at least primary tag (any tag that starts with "0-" is primary), and up to 2 secondary tags. 
- *Organization and Location*: Assign the appropriate location based on the 'requester' (or ticket subject/description if applicable). Use data from emp_info to determine which location and organization to assign it to.

## Next steps
1. Review the ticket contents (description + subject). 
2. Search your knowledge base$ for potential solutions to the ticket. 
3. After your research, you will make a decision between:
    a) Making contact with the user to attempt a solution.
    b) Assigning the ticket to a human technician to resolve 

Select 'a)' if 
    - You're >90% confident that the solution is on the users end. 
    - If you believe that text-instructions to the user would be sufficient to solve the problem.
Select 'b)' if 
    - You're <90% confident that the solution is on the users end. 
    - If the issue is highly complex or otherwise needs human attention 

**Making contact with the user**: In a warm tone, provide instructions for the attempted resolution as a 'public_comment' on the ticket based on your research and understanding of the issue.
**Assigning the ticket to a human technician to resolve**: 
    1. Create a private_comment, sharing potentially useful information (ticket numbers, file names, or other references)
    2. Categorize the ticket and assign to a technician based on their areas of expertise + availability. If unsure, select 'Unassigned'... An available human will grab the ticket. 

# Rules
- Never make up solutions. If unsure, rely on human intervention. 
- Redact secrets; NEVER ask for credentials over public_comments
```