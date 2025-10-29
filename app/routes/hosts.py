from fastapi import APIRouter, HTTPException
from typing import List
import uuid
import aiohttp

from app.models import Host, HostCreate, HostResponse
from app.config import host_manager


router = APIRouter(prefix="/hosts", tags=["hosts"])


@router.post("", response_model=HostResponse)
async def register_host(data: HostCreate):
    """Register a new solar-host"""
    try:
        # Test connection to host
        async with aiohttp.ClientSession() as session:
            try:
                url = f"{data.url}/health"
                async with session.get(url, timeout=5) as response:
                    if response.status != 200:
                        raise HTTPException(
                            status_code=400,
                            detail=f"Cannot connect to host at {data.url}"
                        )
            except Exception as e:
                raise HTTPException(
                    status_code=400,
                    detail=f"Cannot connect to host: {str(e)}"
                )
        
        # Create host
        host_id = str(uuid.uuid4())
        host = Host(
            id=host_id,
            name=data.name,
            url=data.url,
            api_key=data.api_key
        )
        
        host_manager.add_host(host)
        
        return HostResponse(
            host=host,
            message=f"Host '{data.name}' registered successfully"
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("", response_model=List[Host])
async def list_hosts():
    """List all registered hosts"""
    return host_manager.get_all_hosts()


@router.get("/{host_id}", response_model=Host)
async def get_host(host_id: str):
    """Get host details"""
    host = host_manager.get_host(host_id)
    if not host:
        raise HTTPException(status_code=404, detail="Host not found")
    return host


@router.delete("/{host_id}", response_model=HostResponse)
async def remove_host(host_id: str):
    """Remove a registered host"""
    host = host_manager.get_host(host_id)
    if not host:
        raise HTTPException(status_code=404, detail="Host not found")
    
    host_manager.remove_host(host_id)
    
    return HostResponse(
        host=host,
        message=f"Host '{host.name}' removed successfully"
    )


@router.get("/{host_id}/instances")
async def get_host_instances(host_id: str):
    """Get instances from a specific host"""
    host = host_manager.get_host(host_id)
    if not host:
        raise HTTPException(status_code=404, detail="Host not found")
    
    try:
        async with aiohttp.ClientSession() as session:
            url = f"{host.url}/instances"
            headers = {"X-API-Key": host.api_key}
            
            async with session.get(url, headers=headers, timeout=10) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    raise HTTPException(
                        status_code=response.status,
                        detail="Failed to get instances from host"
                    )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{host_id}/instances/{instance_id}/start")
async def start_instance(host_id: str, instance_id: str):
    """Start an instance on a host"""
    host = host_manager.get_host(host_id)
    if not host:
        raise HTTPException(status_code=404, detail="Host not found")
    
    try:
        async with aiohttp.ClientSession() as session:
            url = f"{host.url}/instances/{instance_id}/start"
            headers = {"X-API-Key": host.api_key}
            
            async with session.post(url, headers=headers, timeout=30) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    raise HTTPException(
                        status_code=response.status,
                        detail="Failed to start instance"
                    )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{host_id}/instances/{instance_id}/stop")
async def stop_instance(host_id: str, instance_id: str):
    """Stop an instance on a host"""
    host = host_manager.get_host(host_id)
    if not host:
        raise HTTPException(status_code=404, detail="Host not found")
    
    try:
        async with aiohttp.ClientSession() as session:
            url = f"{host.url}/instances/{instance_id}/stop"
            headers = {"X-API-Key": host.api_key}
            
            async with session.post(url, headers=headers, timeout=30) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    raise HTTPException(
                        status_code=response.status,
                        detail="Failed to stop instance"
                    )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{host_id}/instances/{instance_id}/restart")
async def restart_instance(host_id: str, instance_id: str):
    """Restart an instance on a host"""
    host = host_manager.get_host(host_id)
    if not host:
        raise HTTPException(status_code=404, detail="Host not found")
    
    try:
        async with aiohttp.ClientSession() as session:
            url = f"{host.url}/instances/{instance_id}/restart"
            headers = {"X-API-Key": host.api_key}
            
            async with session.post(url, headers=headers, timeout=60) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    raise HTTPException(
                        status_code=response.status,
                        detail="Failed to restart instance"
                    )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

