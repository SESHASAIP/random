# from tools.some_file import *
# # or your existing ADK tool registration — unchanged
# ```

# ---

# **In OCP context**

# Your pod startup sequence becomes:
# ```
# Pod starts
#    ↓
# sync_tools_from_db()  ← fetches & writes .py files
#    ↓
# Agent initializes     ← picks up tools normally from path
#    ↓
# Ready to serve


# startup.py — runs before agent initialization

import psycopg2  # or whatever your DB client is
from pathlib import Path

def sync_tools_from_db(agent_id: str):
    conn = psycopg2.connect(os.environ["DB_URL"])
    cursor = conn.cursor()
    
    # fetch the tool file content for this specific agent
    cursor.execute(
        "SELECT file_name, file_content FROM agent_tools WHERE agent_id = %s",
        (agent_id,)
    )
    rows = cursor.fetchall()
    
    tools_path = Path("project_root/tools/")
    
    for file_name, file_content in rows:
        target = tools_path / file_name  # e.g. some_file.py
        target.write_text(file_content)  # replaces whatever was there
        print(f"Synced tool: {target}")
    
    conn.close()

# Call this BEFORE agent boots
sync_tools_from_db(os.environ["AGENT_ID"])
