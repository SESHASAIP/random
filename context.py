import asyncio
import time
import logging
from typing import Dict, Optional, Tuple, List
from dataclasses import dataclass
from enum import Enum
from playwright.async_api import BrowserContext

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class Environment(Enum):
    """Supported environments"""
    DEV = "dev"
    UAT = "uat"
    PROD = "prod"
    
    @classmethod
    def from_string(cls, env: str) -> 'Environment':
        """Convert string to Environment enum"""
        env_map = {e.value: e for e in cls}
        if env.lower() not in env_map:
            raise ValueError(f"Invalid environment: {env}. Must be one of {list(env_map.keys())}")
        return env_map[env.lower()]


@dataclass(frozen=True)
class SessionKey:
    """Immutable composite key for context identification"""
    agent_name: str
    session_id: str
    environment: Environment
    
    def __post_init__(self):
        """Validate fields"""
        if not self.agent_name or not self.agent_name.strip():
            raise ValueError("agent_name cannot be empty")
        if not self.session_id or not self.session_id.strip():
            raise ValueError("session_id cannot be empty")
    
    def __str__(self) -> str:
        return f"{self.agent_name}:{self.session_id}:{self.environment.value}"
    
    def __hash__(self) -> int:
        return hash((self.agent_name, self.session_id, self.environment.value))


@dataclass
class SessionMetadata:
    """Metadata for a browser context session"""
    context: BrowserContext
    created_at: float
    expires_at: float
    last_accessed: float
    access_count: int = 0
    
    def is_expired(self) -> bool:
        """Check if session has expired"""
        return time.time() > self.expires_at
    
    def refresh_expiry(self, ttl: int):
        """Update expiration time"""
        self.expires_at = time.time() + ttl
        self.last_accessed = time.time()
        self.access_count += 1


class ContextManager:
    """
    Thread-safe browser context manager with multi-dimensional session binding.
    Supports isolation by agent_name, session_id, and environment.
    """
    
    def __init__(self, default_ttl: int = 1800, cleanup_interval: int = 300):
        """
        Initialize context manager.
        
        Args:
            default_ttl: Default session time-to-live in seconds (default: 30 minutes)
            cleanup_interval: Background cleanup check interval in seconds (default: 5 minutes)
        """
        self._sessions: Dict[SessionKey, SessionMetadata] = {}
        self._locks: Dict[SessionKey, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()
        self._default_ttl = default_ttl
        self._cleanup_interval = cleanup_interval
        self._cleanup_task: Optional[asyncio.Task] = None
        self._is_running = False
        
        logger.info(f"ContextManager initialized with TTL={default_ttl}s, cleanup_interval={cleanup_interval}s")
    
    def _create_session_key(self, agent_name: str, session_id: str, environment: str) -> SessionKey:
        """Create and validate session key"""
        env = Environment.from_string(environment)
        return SessionKey(
            agent_name=agent_name.strip(),
            session_id=session_id.strip(),
            environment=env
        )
    
    async def _get_lock(self, key: SessionKey) -> asyncio.Lock:
        """Get or create lock for a session key"""
        async with self._global_lock:
            if key not in self._locks:
                self._locks[key] = asyncio.Lock()
            return self._locks[key]
    
    async def store_context(
        self,
        agent_name: str,
        session_id: str,
        environment: str,
        context: BrowserContext,
        ttl: Optional[int] = None
    ) -> SessionKey:
        """
        Store browser context with composite key binding.
        
        Args:
            agent_name: Name of the agent (e.g., "shopping_bot_1")
            session_id: Unique session identifier (e.g., UUID)
            environment: Target environment ("dev", "uat", "prod")
            context: Playwright BrowserContext instance
            ttl: Optional custom TTL in seconds
            
        Returns:
            SessionKey: The composite key for this session
            
        Raises:
            ValueError: If session already exists or invalid parameters
        """
        key = self._create_session_key(agent_name, session_id, environment)
        lock = await self._get_lock(key)
        
        async with lock:
            if key in self._sessions:
                raise ValueError(f"Session already exists: {key}")
            
            ttl_seconds = ttl or self._default_ttl
            current_time = time.time()
            
            metadata = SessionMetadata(
                context=context,
                created_at=current_time,
                expires_at=current_time + ttl_seconds,
                last_accessed=current_time
            )
            
            self._sessions[key] = metadata
            logger.info(f"Context stored: {key} (expires in {ttl_seconds}s)")
            
            return key
    
    async def get_context(
        self,
        agent_name: str,
        session_id: str,
        environment: str,
        auto_refresh: bool = True
    ) -> BrowserContext:
        """
        Retrieve browser context by composite key.
        
        Args:
            agent_name: Name of the agent
            session_id: Session identifier
            environment: Target environment
            auto_refresh: Automatically refresh expiry on access (sliding window)
            
        Returns:
            BrowserContext: The stored browser context
            
        Raises:
            ValueError: If session not found or expired
        """
        key = self._create_session_key(agent_name, session_id, environment)
        lock = await self._get_lock(key)
        
        async with lock:
            if key not in self._sessions:
                raise ValueError(f"Session not found: {key}")
            
            metadata = self._sessions[key]
            
            # Check expiration
            if metadata.is_expired():
                logger.warning(f"Session expired: {key}")
                await self._cleanup_session_internal(key)
                raise ValueError(f"Session expired: {key}")
            
            # Auto-refresh expiry (sliding window)
            if auto_refresh:
                metadata.refresh_expiry(self._default_ttl)
                logger.debug(f"Session refreshed: {key} (access_count={metadata.access_count})")
            else:
                metadata.last_accessed = time.time()
                metadata.access_count += 1
            
            return metadata.context
    
    async def refresh_session(
        self,
        agent_name: str,
        session_id: str,
        environment: str,
        additional_seconds: Optional[int] = None
    ):
        """
        Manually extend session lifetime.
        
        Args:
            agent_name: Name of the agent
            session_id: Session identifier
            environment: Target environment
            additional_seconds: Additional seconds to extend (default: use default_ttl)
        """
        key = self._create_session_key(agent_name, session_id, environment)
        lock = await self._get_lock(key)
        
        async with lock:
            if key not in self._sessions:
                raise ValueError(f"Session not found: {key}")
            
            metadata = self._sessions[key]
            extension = additional_seconds or self._default_ttl
            metadata.refresh_expiry(extension)
            
            logger.info(f"Session manually refreshed: {key} (extended by {extension}s)")
    
    async def cleanup_session(
        self,
        agent_name: str,
        session_id: str,
        environment: str
    ):
        """
        Manually cleanup a specific session.
        
        Args:
            agent_name: Name of the agent
            session_id: Session identifier
            environment: Target environment
        """
        key = self._create_session_key(agent_name, session_id, environment)
        lock = await self._get_lock(key)
        
        async with lock:
            await self._cleanup_session_internal(key)
    
    async def _cleanup_session_internal(self, key: SessionKey):
        """Internal cleanup without lock acquisition"""
        if key in self._sessions:
            metadata = self._sessions[key]
            try:
                await metadata.context.close()
                logger.info(f"Context closed: {key}")
            except Exception as e:
                logger.error(f"Error closing context {key}: {e}")
            finally:
                del self._sessions[key]
                if key in self._locks:
                    del self._locks[key]
    
    async def cleanup_agent_sessions(self, agent_name: str, environment: Optional[str] = None):
        """
        Cleanup all sessions for a specific agent (optionally filtered by environment).
        
        Args:
            agent_name: Name of the agent
            environment: Optional environment filter
        """
        keys_to_cleanup = [
            key for key in self._sessions.keys()
            if key.agent_name == agent_name and (
                environment is None or key.environment.value == environment.lower()
            )
        ]
        
        logger.info(f"Cleaning up {len(keys_to_cleanup)} sessions for agent: {agent_name}")
        
        for key in keys_to_cleanup:
            lock = await self._get_lock(key)
            async with lock:
                await self._cleanup_session_internal(key)
    
    async def _cleanup_expired_sessions(self):
        """Background task to cleanup expired sessions"""
        while self._is_running:
            try:
                await asyncio.sleep(self._cleanup_interval)
                
                current_time = time.time()
                expired_keys = [
                    key for key, metadata in self._sessions.items()
                    if metadata.is_expired()
                ]
                
                if expired_keys:
                    logger.info(f"Found {len(expired_keys)} expired sessions to cleanup")
                    
                    for key in expired_keys:
                        lock = await self._get_lock(key)
                        async with lock:
                            # Double-check expiration (race condition protection)
                            if key in self._sessions and self._sessions[key].is_expired():
                                await self._cleanup_session_internal(key)
                
            except asyncio.CancelledError:
                logger.info("Cleanup task cancelled")
                break
            except Exception as e:
                logger.error(f"Error in cleanup task: {e}")
    
    async def start(self):
        """Start the context manager and background cleanup task"""
        if self._is_running:
            logger.warning("ContextManager already running")
            return
        
        self._is_running = True
        self._cleanup_task = asyncio.create_task(self._cleanup_expired_sessions())
        logger.info("ContextManager started with background cleanup")
    
    async def stop(self):
        """Stop the context manager and cleanup all sessions"""
        logger.info("Stopping ContextManager...")
        self._is_running = False
        
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        
        # Cleanup all remaining sessions
        keys = list(self._sessions.keys())
        for key in keys:
            lock = await self._get_lock(key)
            async with lock:
                await self._cleanup_session_internal(key)
        
        logger.info("ContextManager stopped")
    
    def get_session_info(
        self,
        agent_name: Optional[str] = None,
        environment: Optional[str] = None
    ) -> List[Dict]:
        """
        Get information about active sessions (for monitoring/debugging).
        
        Args:
            agent_name: Optional filter by agent name
            environment: Optional filter by environment
            
        Returns:
            List of session information dictionaries
        """
        current_time = time.time()
        sessions_info = []
        
        for key, metadata in self._sessions.items():
            # Apply filters
            if agent_name and key.agent_name != agent_name:
                continue
            if environment and key.environment.value != environment.lower():
                continue
            
            sessions_info.append({
                'agent_name': key.agent_name,
                'session_id': key.session_id,
                'environment': key.environment.value,
                'created_at': metadata.created_at,
                'expires_at': metadata.expires_at,
                'last_accessed': metadata.last_accessed,
                'access_count': metadata.access_count,
                'time_remaining': max(0, metadata.expires_at - current_time),
                'is_expired': metadata.is_expired()
            })
        
        return sessions_info
    
    @property
    def active_session_count(self) -> int:
        """Get count of active sessions"""
        return len(self._sessions)
