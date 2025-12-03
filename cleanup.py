from google.adk.agents.callback_context import CallbackContext
from google.adk.tools.mcp_tool import MCPToolset
from typing import Optional
from google.genai import types

async def cleanup_mcp_after_agent(
    callback_context: CallbackContext
) -> Optional[types.Content]:
    """
    This is the ONLY way to properly close MCPToolset in sub-agents.
    """
    # Get the specific agent instance
    agent = callback_context.agent
    
    # Access that agent's specific tools
    if hasattr(agent, 'tools') and agent.tools:
        for tool in agent.tools:
            # Check if it's an MCPToolset instance
            if isinstance(tool, MCPToolset):
                # Call close() on THIS SPECIFIC INSTANCE
                await tool.close()
                print(f"Closed MCPToolset for agent: {agent.name}")
    
    return None
