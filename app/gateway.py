import aiohttp
import uuid
import time
from typing import Dict, List, Optional, Any
from collections import defaultdict
from itertools import cycle
from datetime import datetime, timezone

from app.config import host_manager
from app.models import HostStatus


class OpenAIGateway:
    """OpenAI-compatible API gateway with routing and load balancing"""
    
    def __init__(self):
        # Map of model alias to list of instance info dictionaries
        self.model_to_hosts: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        # Round-robin iterators for each model (used as tiebreaker)
        self.model_iterators: Dict[str, Any] = {}  # cycle objects don't have good type hints
        # Track active requests per instance (instance_id -> count)
        self.active_requests: Dict[str, int] = defaultdict(int)
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
                
                async with self.session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as response:
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
                        
            except Exception:
                host_manager.update_host_status(host.id, HostStatus.OFFLINE)
        
        # Create round-robin iterators for each model
        for alias, instances in self.model_to_hosts.items():
            if instances:
                self.model_iterators[alias] = cycle(instances)
    
    async def get_available_models(self) -> List[Dict[str, Any]]:
        """Get list of all available models with full metadata from llama.cpp servers"""
        await self.refresh_model_registry()
        await self._ensure_session()
        
        if not self.session:
            return []
        
        models_dict = {}  # Use dict to deduplicate by model ID
        
        # Query each instance directly to get full model info
        for alias, instances in self.model_to_hosts.items():
            if not instances:
                continue
            
            # Get model info from the first available instance
            instance = instances[0]
            try:
                url = f"{instance['url']}/v1/models"
                headers = {"Authorization": f"Bearer {instance['api_key']}"}
                
                async with self.session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as response:
                    if response.status == 200:
                        data = await response.json()
                        # llama.cpp returns both "models" and "data" arrays
                        # Use the "data" array which has the OpenAI-compatible format
                        if 'data' in data and data['data']:
                            for model in data['data']:
                                # Use the model ID as key to avoid duplicates
                                model_id = model.get('id', alias)
                                if model_id not in models_dict:
                                    models_dict[model_id] = model
            except Exception:
                # Fallback: create minimal model info
                if alias not in models_dict:
                    models_dict[alias] = {
                        "id": alias,
                        "object": "model",
                        "created": int(datetime.now(timezone.utc).timestamp()),
                        "owned_by": "solar"
                    }
        
        return list(models_dict.values())
    
    def _get_next_instance(self, model: str) -> Optional[Dict[str, Any]]:
        """Get next instance for a model using intelligent load balancing
        
        Strategy:
        1. Prefer instances with no active requests (free)
        2. If all busy, choose the least busy one
        3. Use round-robin as tiebreaker among equally busy instances
        """
        if model not in self.model_to_hosts or not self.model_to_hosts[model]:
            return None
        
        available_instances = self.model_to_hosts[model]
        
        # Find the instance with minimum active requests
        min_requests = float('inf')
        best_instances = []
        
        for instance in available_instances:
            instance_key = f"{instance['host_id']}-{instance['instance_id']}"
            active = self.active_requests.get(instance_key, 0)
            
            if active < min_requests:
                min_requests = active
                best_instances = [instance]
            elif active == min_requests:
                best_instances.append(instance)
        
        # If we have multiple instances with same load, use round-robin as tiebreaker
        if len(best_instances) == 1:
            return best_instances[0]
        
        # Use round-robin among the least busy instances
        if model in self.model_iterators:
            # Find the next instance in round-robin that's also in best_instances
            for _ in range(len(available_instances)):
                candidate = next(self.model_iterators[model])
                if candidate in best_instances:
                    return candidate
        
        # Fallback: return first best instance
        return best_instances[0]
    
    async def route_request(self, model: str, endpoint: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """Route a request to the appropriate instance"""
        # Generate unique request ID and start time
        request_id = str(uuid.uuid4())
        start_time = time.time()
        
        # Import here to avoid circular dependency
        from app.routes.websockets import broadcast_routing_event
        
        # Emit request start event
        await broadcast_routing_event({
            "type": "request_start",
            "data": {
                "request_id": request_id,
                "model": model,
                "endpoint": endpoint,
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
        })
        
        await self._ensure_session()
        
        if not self.session:
            raise RuntimeError("Failed to create aiohttp session")
        
        # Refresh registry to get latest instances
        await self.refresh_model_registry()
        
        # Get instance for this model
        instance = self._get_next_instance(model)
        if not instance:
            error_msg = f"Model '{model}' not found or no instances available"
            # Emit error event
            await broadcast_routing_event({
                "type": "request_error",
                "data": {
                    "request_id": request_id,
                    "model": model,
                    "error_message": error_msg,
                    "timestamp": datetime.now(timezone.utc).isoformat()
                }
            })
            raise ValueError(error_msg)
        
        # Track this request as active
        instance_key = f"{instance['host_id']}-{instance['instance_id']}"
        self.active_requests[instance_key] += 1
        
        try:
            # Emit routing event (instance selected)
            await broadcast_routing_event({
                "type": "request_routed",
                "data": {
                    "request_id": request_id,
                    "model": model,
                    "host_id": instance['host_id'],
                    "instance_id": instance['instance_id'],
                    "instance_url": instance['url'],
                    "timestamp": datetime.now(timezone.utc).isoformat()
                }
            })
            
            # Forward request to instance
            url = f"{instance['url']}{endpoint}"
            headers = {
                "Authorization": f"Bearer {instance['api_key']}",
                "Content-Type": "application/json"
            }
            
            async with self.session.post(url, json=data, headers=headers) as response:
                if response.status == 200:
                    result = await response.json()
                    duration = time.time() - start_time
                    
                    # Emit success event
                    await broadcast_routing_event({
                        "type": "request_success",
                        "data": {
                            "request_id": request_id,
                            "model": model,
                            "host_id": instance['host_id'],
                            "instance_id": instance['instance_id'],
                            "duration": duration,
                            "timestamp": datetime.now(timezone.utc).isoformat()
                        }
                    })
                    
                    return result
                else:
                    error_text = await response.text()
                    duration = time.time() - start_time
                    
                    # Emit error event
                    await broadcast_routing_event({
                        "type": "request_error",
                        "data": {
                            "request_id": request_id,
                            "model": model,
                            "host_id": instance['host_id'],
                            "instance_id": instance['instance_id'],
                            "error_message": f"Request failed: {response.status} - {error_text}",
                            "duration": duration,
                            "timestamp": datetime.now(timezone.utc).isoformat()
                        }
                    })
                    raise Exception(f"Request failed: {response.status} - {error_text}")
        except Exception as e:
            duration = time.time() - start_time
            
            # Emit error event
            await broadcast_routing_event({
                "type": "request_error",
                "data": {
                    "request_id": request_id,
                    "model": model,
                    "host_id": instance.get('host_id') if instance else None,
                    "instance_id": instance.get('instance_id') if instance else None,
                    "error_message": str(e),
                    "duration": duration,
                    "timestamp": datetime.now(timezone.utc).isoformat()
                }
            })
            raise Exception(f"Failed to route request: {str(e)}")
        finally:
            # Always decrement active requests count
            self.active_requests[instance_key] = max(0, self.active_requests[instance_key] - 1)
    
    async def stream_request(self, model: str, endpoint: str, data: Dict[str, Any]):
        """Stream a request to the appropriate instance"""
        # Generate unique request ID and start time
        request_id = str(uuid.uuid4())
        start_time = time.time()
        
        # Import here to avoid circular dependency
        from app.routes.websockets import broadcast_routing_event
        
        # Emit request start event
        await broadcast_routing_event({
            "type": "request_start",
            "data": {
                "request_id": request_id,
                "model": model,
                "endpoint": endpoint,
                "stream": True,
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
        })
        
        await self._ensure_session()
        
        if not self.session:
            raise RuntimeError("Failed to create aiohttp session")
        
        # Refresh registry to get latest instances
        await self.refresh_model_registry()
        
        # Get instance for this model
        instance = self._get_next_instance(model)
        if not instance:
            error_msg = f"Model '{model}' not found or no instances available"
            # Emit error event
            await broadcast_routing_event({
                "type": "request_error",
                "data": {
                    "request_id": request_id,
                    "model": model,
                    "error_message": error_msg,
                    "timestamp": datetime.now(timezone.utc).isoformat()
                }
            })
            raise ValueError(error_msg)
        
        # Track this request as active
        instance_key = f"{instance['host_id']}-{instance['instance_id']}"
        self.active_requests[instance_key] += 1
        
        try:
            # Emit routing event (instance selected)
            await broadcast_routing_event({
                "type": "request_routed",
                "data": {
                    "request_id": request_id,
                    "model": model,
                    "host_id": instance['host_id'],
                    "instance_id": instance['instance_id'],
                    "instance_url": instance['url'],
                    "timestamp": datetime.now(timezone.utc).isoformat()
                }
            })
            
            # Forward request to instance
            url = f"{instance['url']}{endpoint}"
            headers = {
                "Authorization": f"Bearer {instance['api_key']}",
                "Content-Type": "application/json"
            }
            
            async with self.session.post(url, json=data, headers=headers) as response:
                if response.status == 200:
                    async for line in response.content:
                        yield line
                    
                    # Emit success event after stream completes
                    duration = time.time() - start_time
                    await broadcast_routing_event({
                        "type": "request_success",
                        "data": {
                            "request_id": request_id,
                            "model": model,
                            "host_id": instance['host_id'],
                            "instance_id": instance['instance_id'],
                            "duration": duration,
                            "timestamp": datetime.now(timezone.utc).isoformat()
                        }
                    })
                else:
                    error_text = await response.text()
                    duration = time.time() - start_time
                    
                    # Emit error event
                    await broadcast_routing_event({
                        "type": "request_error",
                        "data": {
                            "request_id": request_id,
                            "model": model,
                            "host_id": instance['host_id'],
                            "instance_id": instance['instance_id'],
                            "error_message": f"Request failed: {response.status} - {error_text}",
                            "duration": duration,
                            "timestamp": datetime.now(timezone.utc).isoformat()
                        }
                    })
                    raise Exception(f"Request failed: {response.status} - {error_text}")
        except Exception as e:
            duration = time.time() - start_time
            
            # Emit error event
            await broadcast_routing_event({
                "type": "request_error",
                "data": {
                    "request_id": request_id,
                    "model": model,
                    "host_id": instance.get('host_id') if instance else None,
                    "instance_id": instance.get('instance_id') if instance else None,
                    "error_message": str(e),
                    "duration": duration,
                    "timestamp": datetime.now(timezone.utc).isoformat()
                }
            })
            raise
        finally:
            # Always decrement active requests count
            self.active_requests[instance_key] = max(0, self.active_requests[instance_key] - 1)


# Global gateway instance
gateway = OpenAIGateway()

