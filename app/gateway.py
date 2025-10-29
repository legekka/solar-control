import aiohttp
from typing import Dict, List, Optional, Any
from collections import defaultdict
from itertools import cycle

from app.config import host_manager
from app.models import HostStatus, ModelInfo


class OpenAIGateway:
    """OpenAI-compatible API gateway with routing and load balancing"""
    
    def __init__(self):
        # Map of model alias to list of (host_id, instance_info)
        self.model_to_hosts: Dict[str, List[tuple]] = defaultdict(list)
        # Round-robin iterators for each model
        self.model_iterators: Dict[str, cycle] = {}
        self.session: Optional[aiohttp.ClientSession] = None
    
    async def _ensure_session(self):
        """Ensure aiohttp session exists"""
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
    
    async def close(self):
        """Close aiohttp session"""
        if self.session and not self.session.closed:
            await self.session.close()
    
    async def refresh_model_registry(self):
        """Refresh the model registry from all hosts"""
        await self._ensure_session()
        
        # Clear existing mappings
        self.model_to_hosts.clear()
        self.model_iterators.clear()
        
        # Query each host for its instances
        for host in host_manager.get_all_hosts():
            try:
                # Get instances from host
                url = f"{host.url}/instances"
                headers = {"X-API-Key": host.api_key}
                
                async with self.session.get(url, headers=headers, timeout=5) as response:
                    if response.status == 200:
                        instances = await response.json()
                        
                        # Update host status
                        host_manager.update_host_status(host.id, HostStatus.ONLINE)
                        
                        # Add running instances to registry
                        for instance in instances:
                            if instance.get('status') == 'running':
                                alias = instance['config']['alias']
                                port = instance.get('port')
                                
                                # Build instance URL
                                host_base = host.url.rsplit(':', 1)[0]  # Remove port
                                instance_url = f"{host_base}:{port}"
                                instance_api_key = instance['config']['api_key']
                                
                                self.model_to_hosts[alias].append({
                                    'host_id': host.id,
                                    'instance_id': instance['id'],
                                    'url': instance_url,
                                    'api_key': instance_api_key
                                })
                    else:
                        host_manager.update_host_status(host.id, HostStatus.ERROR)
                        
            except Exception as e:
                print(f"Error querying host {host.name}: {e}")
                host_manager.update_host_status(host.id, HostStatus.OFFLINE)
        
        # Create round-robin iterators for each model
        for alias, instances in self.model_to_hosts.items():
            if instances:
                self.model_iterators[alias] = cycle(instances)
    
    async def get_available_models(self) -> List[ModelInfo]:
        """Get list of all available models"""
        await self.refresh_model_registry()
        
        models = []
        for alias in self.model_to_hosts.keys():
            models.append(ModelInfo(id=alias, owned_by="solar"))
        
        return models
    
    def _get_next_instance(self, model: str) -> Optional[Dict[str, Any]]:
        """Get next instance for a model using round-robin"""
        if model not in self.model_iterators:
            return None
        
        # Get next instance from round-robin
        return next(self.model_iterators[model])
    
    async def route_request(self, model: str, endpoint: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """Route a request to the appropriate instance"""
        await self._ensure_session()
        
        # Refresh registry to get latest instances
        await self.refresh_model_registry()
        
        # Get instance for this model
        instance = self._get_next_instance(model)
        if not instance:
            raise ValueError(f"Model '{model}' not found or no instances available")
        
        # Forward request to instance
        url = f"{instance['url']}{endpoint}"
        headers = {
            "X-API-Key": instance['api_key'],
            "Content-Type": "application/json"
        }
        
        try:
            async with self.session.post(url, json=data, headers=headers) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    error_text = await response.text()
                    raise Exception(f"Request failed: {response.status} - {error_text}")
        except Exception as e:
            raise Exception(f"Failed to route request: {str(e)}")
    
    async def stream_request(self, model: str, endpoint: str, data: Dict[str, Any]):
        """Stream a request to the appropriate instance"""
        await self._ensure_session()
        
        # Refresh registry to get latest instances
        await self.refresh_model_registry()
        
        # Get instance for this model
        instance = self._get_next_instance(model)
        if not instance:
            raise ValueError(f"Model '{model}' not found or no instances available")
        
        # Forward request to instance
        url = f"{instance['url']}{endpoint}"
        headers = {
            "X-API-Key": instance['api_key'],
            "Content-Type": "application/json"
        }
        
        async with self.session.post(url, json=data, headers=headers) as response:
            if response.status == 200:
                async for line in response.content:
                    yield line
            else:
                error_text = await response.text()
                raise Exception(f"Request failed: {response.status} - {error_text}")


# Global gateway instance
gateway = OpenAIGateway()

