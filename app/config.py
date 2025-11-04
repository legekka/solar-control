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
    # Health probe mode (default: TCP connect only)
    health_probe_use_http: bool = False
    health_probe_http_path: str = "/v1/models"
    # Gateway event logging
    gateway_log_dir: str = "data/gateway-logs"
    gateway_log_retention_days: int = 365
    
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()


class HostManager:
    """Manages registered solar-hosts"""
    
    def __init__(self, hosts_file: Optional[str] = None):
        self.hosts_file = Path(hosts_file or settings.hosts_file)
        self.hosts: Dict[str, Host] = {}
        self.load()
    
    def load(self):
        """Load hosts from disk"""
        if self.hosts_file.exists():
            try:
                with open(self.hosts_file, 'r') as f:
                    data = json.load(f)
                    for host_data in data.get('hosts', []):
                        host = Host(**host_data)
                        self.hosts[host.id] = host
            except Exception as e:
                print(f"Error loading hosts: {e}")
                self.hosts = {}
        else:
            self.hosts = {}
    
    def save(self):
        """Save hosts to disk"""
        try:
            data = {
                'hosts': [
                    host.model_dump(mode='json')
                    for host in self.hosts.values()
                ]
            }
            self.hosts_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.hosts_file, 'w') as f:
                json.dump(data, f, indent=2, default=str)
        except Exception as e:
            print(f"Error saving hosts: {e}")
    
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
        """Update host status"""
        if host_id in self.hosts:
            self.hosts[host_id].status = status
            if status == HostStatus.ONLINE:
                from datetime import datetime, timezone
                self.hosts[host_id].last_seen = datetime.now(timezone.utc)
            self.save()


# Global host manager instance
host_manager = HostManager()

