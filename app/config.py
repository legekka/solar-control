import asyncio
import json
from pathlib import Path
from typing import Dict, Optional
from pydantic_settings import BaseSettings

from app.models import Host, HostStatus


class Settings(BaseSettings):
    """Application settings"""

    api_key: str = "change-me-please"
    host: str = "0.0.0.0"
    port: int = 8000
    hosts_file: str = "data/hosts.json"  # Store in data directory
    # Gateway routing/health tuning
    registry_refresh_interval_s: float = 2.0
    health_check_interval_s: float = 1.0
    health_ttl_s: float = 3.0
    health_cooldown_s: float = 5.0
    route_connect_timeout_s: float = 0.5
    route_max_attempts: int = 3
    route_retry_delay_s: float = 0.15
    # Health probe mode (default: TCP connect only)
    health_probe_use_http: bool = False
    health_probe_http_path: str = "/v1/models"
    # Gateway event logging
    gateway_log_dir: str = "data/gateway-logs"
    gateway_log_retention_days: int = 365
    # Host manager save debounce
    host_save_debounce_s: float = 2.0

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"  # Ignore extra env vars (e.g., SOLAR_CONTROL_URL meant for solar-host)


settings = Settings()


class HostManager:
    """Manages registered solar-hosts with debounced async saves."""

    def __init__(self, hosts_file: Optional[str] = None):
        self.hosts_file = Path(hosts_file or settings.hosts_file)
        self.hosts: Dict[str, Host] = {}
        self._save_pending = False
        self._save_task: Optional[asyncio.Task] = None
        self._save_lock = asyncio.Lock()
        self.load()

    def load(self):
        """Load hosts from disk (synchronous, called once at startup)"""
        if self.hosts_file.exists():
            try:
                with open(self.hosts_file, "r") as f:
                    data = json.load(f)
                    for host_data in data.get("hosts", []):
                        host = Host(**host_data)
                        self.hosts[host.id] = host
            except Exception as e:
                print(f"Error loading hosts: {e}")
                self.hosts = {}
        else:
            self.hosts = {}

    def _sync_save(self):
        """Synchronous save to disk (runs in thread pool)"""
        try:
            data = {
                "hosts": [host.model_dump(mode="json") for host in self.hosts.values()]
            }
            self.hosts_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.hosts_file, "w") as f:
                json.dump(data, f, indent=2, default=str)
        except Exception as e:
            print(f"Error saving hosts: {e}")

    def save(self):
        """Schedule a debounced async save.
        
        This is safe to call from sync or async context - it schedules
        the actual save to happen after a debounce delay.
        """
        self._save_pending = True
        # Try to schedule async save if we're in an event loop
        try:
            loop = asyncio.get_running_loop()
            # Cancel any pending save task
            if self._save_task and not self._save_task.done():
                self._save_task.cancel()
            # Schedule new debounced save
            self._save_task = loop.create_task(self._debounced_save())
        except RuntimeError:
            # No running event loop (startup), do sync save
            self._sync_save()
            self._save_pending = False

    async def _debounced_save(self):
        """Debounced save - waits for debounce period then saves."""
        try:
            await asyncio.sleep(settings.host_save_debounce_s)
            if self._save_pending:
                async with self._save_lock:
                    self._save_pending = False
                    await asyncio.to_thread(self._sync_save)
        except asyncio.CancelledError:
            # Save was cancelled (new save scheduled), that's fine
            pass

    async def flush_save(self):
        """Force an immediate save, useful before shutdown."""
        if self._save_task and not self._save_task.done():
            self._save_task.cancel()
            try:
                await self._save_task
            except asyncio.CancelledError:
                pass
        async with self._save_lock:
            self._save_pending = False
            await asyncio.to_thread(self._sync_save)

    def add_host(self, host: Host):
        """Add a new host"""
        self.hosts[host.id] = host
        self.save()

    def remove_host(self, host_id: str):
        """Remove a host"""
        if host_id in self.hosts:
            del self.hosts[host_id]
            self.save()

    def get_host(self, host_id: str) -> Optional[Host]:
        """Get a host by ID"""
        return self.hosts.get(host_id)

    def get_all_hosts(self) -> list[Host]:
        """Get all hosts"""
        return list(self.hosts.values())

    def update_host_status(self, host_id: str, status: HostStatus):
        """Update host status (debounced save)"""
        if host_id in self.hosts:
            self.hosts[host_id].status = status
            if status == HostStatus.ONLINE:
                from datetime import datetime, timezone

                self.hosts[host_id].last_seen = datetime.now(timezone.utc)
            self.save()


# Global host manager instance
host_manager = HostManager()
