import asyncio
from playwright.async_api import async_playwright, BrowserContext
from typing import Dict
import uuid

class ContextManager:
    def __init__(self):
        # Store contexts mapped to session IDs
        self._contexts: Dict[str, BrowserContext] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
    
    def create_session_id(self) -> str:
        """Generate unique session ID for each agent"""
        return str(uuid.uuid4())
    
    async def store_context(self, session_id: str, context: BrowserContext):
        """Store context with session binding"""
        self._contexts[session_id] = context
        self._locks[session_id] = asyncio.Lock()
    
    async def get_context(self, session_id: str) -> BrowserContext:
        """Retrieve context only with valid session ID"""
        if session_id not in self._contexts:
            raise ValueError(f"No context found for session {session_id}")
        return self._contexts[session_id]
    
    async def cleanup_session(self, session_id: str):
        """Clean up context and remove from registry"""
        if session_id in self._contexts:
            await self._contexts[session_id].close()
            del self._contexts[session_id]
            del self._locks[session_id]