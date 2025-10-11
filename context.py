import asyncio
import time
import logging
import json
import os
from pathlib import Path
from typing import Dict, Optional, List
from dataclasses import dataclass
from enum import Enum
from playwright.async_api import BrowserContext, Browser

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
    
    def to_filename(self) -> str:
        """Convert to safe filename for storage state"""
        return f"{self.agent_name}_{self.session_id}_{self.environment.value}.json"


@dataclass
class SessionMetadata:
    """Metadata for a browser context session"""
    context: BrowserContext
    storage_state_path: Path
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
    Thread-safe browser context manager with persistent storage state.
    Supports isolation by agent_name, session_id, and environment.
    """
    
    def __init__(
        self, 
        default_ttl: int = 1800, 
        cleanup_interval: int = 300,
        storage_dir: str = "playwright_storage"
    ):
        """
        Initialize context manager.
        
        Args:
            default_ttl: Default session time-to-live in seconds (default: 30 minutes)
            cleanup_interval: Background cleanup check interval in seconds (default: 5 minutes)
            storage_dir: Directory to store browser state files
        """
        self._sessions: Dict[SessionKey, SessionMetadata] = {}
        self._locks: Dict[SessionKey, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()
        self._default_ttl = default_ttl
        self._cleanup_interval = cleanup_interval
        self._cleanup_task: Optional[asyncio.Task] = None
        self._is_running = False
        
        # Setup storage directory
        self._storage_dir = Path(storage_dir)
        self._storage_dir.mkdir(parents=True, exist_ok=True)
        
        logger.info(
            f"ContextManager initialized with TTL={default_ttl}s, "
            f"cleanup_interval={cleanup_interval}s, storage_dir={storage_dir}"
        )
    
    def _create_session_key(self, agent_name: str, session_id: str, environment: str) -> SessionKey:
        """Create and validate session key"""
        env = Environment.from_string(environment)
        return SessionKey(
            agent_name=agent_name.strip(),
            session_id=session_id.strip(),
            environment=env
        )
    
    def _get_storage_path(self, key: SessionKey) -> Path:
        """Get storage state file path for a session"""
        return self._storage_dir / key.to_filename()
    
    async def _get_lock(self, key: SessionKey) -> asyncio.Lock:
        """Get or create lock for a session key"""
        async with self._global_lock:
            if key not in self._locks:
                self._locks[key] = asyncio.Lock()
            return self._locks[key]
    
    async def _save_storage_state(self, context: BrowserContext, storage_path: Path):
        """Save browser context storage state to file"""
        try:
            storage_state = await context.storage_state()
            storage_path.write_text(json.dumps(storage_state, indent=2))
            logger.debug(f"Storage state saved to: {storage_path}")
        except Exception as e:
            logger.error(f"Failed to save storage state to {storage_path}: {e}")
            raise
    
    async def store_context(
        self,
        agent_name: str,
        session_id: str,
        environment: str,
        context: BrowserContext,
        ttl: Optional[int] = None,
        save_state: bool = True
    ) -> SessionKey:
        """
        Store browser context with composite key binding and save storage state.
        
        Args:
            agent_name: Name of the agent (e.g., "shopping_bot_1")
            session_id: Unique session identifier (e.g., UUID)
            environment: Target environment ("dev", "uat", "prod")
            context: Playwright BrowserContext instance
            ttl: Optional custom TTL in seconds
            save_state: Whether to save storage state to disk (default: True)
            
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
            storage_path = self._get_storage_path(key)
            
            # Save storage state to disk
            if save_state:
                await self._save_storage_state(context, storage_path)
            
            metadata = SessionMetadata(
                context=context,
                storage_state_path=storage_path,
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
                if auto_refresh:
                    # Revive the session by refreshing TTL
                    metadata.refresh_expiry(self._default_ttl)
                    logger.warning(f"Session was expired but auto-refreshed: {key}")
                else:
                    # Strict mode - close expired sessions
                    logger.warning(f"Session expired (auto_refresh=False): {key}")
                    await self._cleanup_session_internal(key)
                    raise ValueError(f"Session expired: {key}")
            else:
                # Session still valid - refresh if enabled
                if auto_refresh:
                    metadata.refresh_expiry(self._default_ttl)
                    logger.debug(f"Session refreshed: {key} (access_count={metadata.access_count})")
                else:
                    metadata.last_accessed = time.time()
                    metadata.access_count += 1
            
            return metadata.context
    
    async def get_storage_state(
        self,
        agent_name: str,
        session_id: str,
        environment: str
    ) -> Dict:
        """
        Get storage state for a session (cookies, localStorage, etc.)
        
        Args:
            agent_name: Name of the agent
            session_id: Session identifier
            environment: Target environment
            
        Returns:
            Dict: Storage state dictionary
            
        Raises:
            ValueError: If session not found or storage state file doesn't exist
        """
        key = self._create_session_key(agent_name, session_id, environment)
        lock = await self._get_lock(key)
        
        async with lock:
            if key not in self._sessions:
                raise ValueError(f"Session not found: {key}")
            
            metadata = self._sessions[key]
            storage_path = metadata.storage_state_path
            
            if not storage_path.exists():
                raise ValueError(f"Storage state file not found: {storage_path}")
            
            try:
                storage_state = json.loads(storage_path.read_text())
                logger.debug(f"Storage state loaded from: {storage_path}")
                return storage_state
            except Exception as e:
                logger.error(f"Failed to load storage state from {storage_path}: {e}")
                raise
    
    async def restore_context_from_state(
        self,
        agent_name: str,
        session_id: str,
        environment: str,
        browser: Browser
    ) -> BrowserContext:
        """
        Restore a browser context from saved storage state.
        Useful for recreating sessions after restart.
        
        Args:
            agent_name: Name of the agent
            session_id: Session identifier
            environment: Target environment
            browser: Playwright Browser instance to create context from
            
        Returns:
            BrowserContext: New context with restored state
            
        Raises:
            ValueError: If storage state file doesn't exist
        """
        key = self._create_session_key(agent_name, session_id, environment)
        storage_path = self._get_storage_path(key)
        
        if not storage_path.exists():
            raise ValueError(
                f"No saved storage state found for {key}. "
                f"File not found: {storage_path}"
            )
        
        try:
            storage_state = json.loads(storage_path.read_text())
            context = await browser.new_context(storage_state=storage_state)
            
            logger.info(f"Context restored from storage state: {key}")
            
            # Store the restored context
            await self.store_context(
                agent_name, 
                session_id, 
                environment, 
                context,
                save_state=False  # Already have the state file
            )
            
            return context
            
        except Exception as e:
            logger.error(f"Failed to restore context from {storage_path}: {e}")
            raise
    
    async def update_storage_state(
        self,
        agent_name: str,
        session_id: str,
        environment: str
    ):
        """
        Update the saved storage state for an active session.
        Call this after important actions (login, checkout, etc.)
        
        Args:
            agent_name: Name of the agent
            session_id: Session identifier
            environment: Target environment
        """
        key = self._create_session_key(agent_name, session_id, environment)
        lock = await self._get_lock(key)
        
        async with lock:
            if key not in self._sessions:
                raise ValueError(f"Session not found: {key}")
            
            metadata = self._sessions[key]
            await self._save_storage_state(metadata.context, metadata.storage_state_path)
            logger.info(f"Storage state updated: {key}")
    
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
        environment: str,
        keep_storage_state: bool = False
    ):
        """
        Manually cleanup a specific session.
        
        Args:
            agent_name: Name of the agent
            session_id: Session identifier
            environment: Target environment
            keep_storage_state: Whether to keep the storage state file (default: False)
        """
        key = self._create_session_key(agent_name, session_id, environment)
        lock = await self._get_lock(key)
        
        async with lock:
            await self._cleanup_session_internal(key, keep_storage_state)
    
    async def _cleanup_session_internal(self, key: SessionKey, keep_storage_state: bool = False):
        """Internal cleanup without lock acquisition"""
        if key in self._sessions:
            metadata = self._sessions[key]
            
            try:
                # Close browser context
                await metadata.context.close()
                logger.info(f"Context closed: {key}")
            except Exception as e:
                logger.error(f"Error closing context {key}: {e}")
            
            try:
                # Delete storage state file unless explicitly kept
                if not keep_storage_state and metadata.storage_state_path.exists():
                    metadata.storage_state_path.unlink()
                    logger.info(f"Storage state deleted: {metadata.storage_state_path}")
            except Exception as e:
                logger.error(f"Error deleting storage state {metadata.storage_state_path}: {e}")
            
            finally:
                del self._sessions[key]
                if key in self._locks:
                    del self._locks[key]
    
    async def cleanup_agent_sessions(
        self, 
        agent_name: str, 
        environment: Optional[str] = None,
        keep_storage_state: bool = False
    ):
        """
        Cleanup all sessions for a specific agent (optionally filtered by environment).
        
        Args:
            agent_name: Name of the agent
            environment: Optional environment filter
            keep_storage_state: Whether to keep storage state files (default: False)
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
                await self._cleanup_session_internal(key, keep_storage_state)
    
    async def _cleanup_expired_sessions(self):
        """Background task to cleanup expired sessions"""
        grace_period = 60  # 1 minute grace period
        
        while self._is_running:
            try:
                await asyncio.sleep(self._cleanup_interval)
                
                current_time = time.time()
                
                # Only cleanup sessions expired beyond grace period
                expired_keys = [
                    key for key, metadata in self._sessions.items()
                    if current_time > metadata.expires_at + grace_period
                ]
                
                if expired_keys:
                    logger.info(f"Found {len(expired_keys)} expired sessions (beyond grace period)")
                    
                    for key in expired_keys:
                        lock = await self._get_lock(key)
                        async with lock:
                            # Double-check expiration with grace period
                            if key in self._sessions:
                                metadata = self._sessions[key]
                                if current_time > metadata.expires_at + grace_period:
                                    logger.info(f"Background cleanup: {key}")
                                    await self._cleanup_session_internal(key, keep_storage_state=False)
                
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
    
    async def stop(self, keep_storage_states: bool = False):
        """
        Stop the context manager and cleanup all sessions.
        
        Args:
            keep_storage_states: Whether to keep storage state files (default: False)
        """
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
                await self._cleanup_session_internal(key, keep_storage_states)
        
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
                'is_expired': metadata.is_expired(),
                'storage_state_path': str(metadata.storage_state_path),
                'storage_state_exists': metadata.storage_state_path.exists()
            })
        
        return sessions_info
    
    def list_saved_storage_states(self) -> List[Dict]:
        """
        List all saved storage state files (including orphaned ones).
        
        Returns:
            List of storage state file information
        """
        storage_files = []
        
        for file_path in self._storage_dir.glob("*.json"):
            try:
                # Parse filename to extract session info
                filename = file_path.stem  # Remove .json extension
                parts = filename.split('_')
                
                if len(parts) >= 3:
                    agent_name = parts[0]
                    session_id = '_'.join(parts[1:-1])  # Handle UUIDs with underscores
                    environment = parts[-1]
                    
                    storage_files.append({
                        'agent_name': agent_name,
                        'session_id': session_id,
                        'environment': environment,
                        'file_path': str(file_path),
                        'file_size': file_path.stat().st_size,
                        'modified_time': file_path.stat().st_mtime
                    })
            except Exception as e:
                logger.warning(f"Could not parse storage file {file_path}: {e}")
        
        return storage_files
    
    async def cleanup_orphaned_storage_states(self):
        """
        Remove storage state files that don't have active sessions.
        Useful for cleaning up after crashes or improper shutdowns.
        """
        active_storage_paths = {
            metadata.storage_state_path 
            for metadata in self._sessions.values()
        }
        
        orphaned_count = 0
        for file_path in self._storage_dir.glob("*.json"):
            if file_path not in active_storage_paths:
                try:
                    file_path.unlink()
                    orphaned_count += 1
                    logger.info(f"Removed orphaned storage state: {file_path}")
                except Exception as e:
                    logger.error(f"Failed to remove orphaned storage state {file_path}: {e}")
        
        logger.info(f"Cleaned up {orphaned_count} orphaned storage state files")
        return orphaned_count
    
    @property
    def active_session_count(self) -> int:
        """Get count of active sessions"""
        return len(self._sessions)
    
    @property
    def storage_directory(self) -> Path:
        """Get storage directory path"""
        return self._storage_dir
