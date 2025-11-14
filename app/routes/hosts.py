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
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=5)
                ) as response:
                    if response.status != 200:
                        raise HTTPException(
                            status_code=400,
                            detail=f"Cannot connect to host at {data.url}",
                        )
            except Exception as e:
                raise HTTPException(
                    status_code=400, detail=f"Cannot connect to host: {str(e)}"
                )

        # Create host
        host_id = str(uuid.uuid4())
        host = Host(id=host_id, name=data.name, url=data.url, api_key=data.api_key)

        # Check if we can reach the instances endpoint with the API key
        try:
            async with aiohttp.ClientSession() as session:
                url = f"{data.url}/instances"
                headers = {"X-API-Key": data.api_key}
                async with session.get(
                    url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)
                ) as response:
                    if response.status == 200:
                        from app.models import HostStatus

                        host.status = HostStatus.ONLINE
                        from datetime import datetime, timezone

                        host.last_seen = datetime.now(timezone.utc)
        except Exception as e:
            print(f"Warning: Could not verify host API key: {e}")
            # Still register the host, but it will stay offline

        host_manager.add_host(host)

        return HostResponse(
            host=host, message=f"Host '{data.name}' registered successfully"
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

    return HostResponse(host=host, message=f"Host '{host.name}' removed successfully")


@router.post("/{host_id}/refresh", response_model=HostResponse)
async def refresh_host_status(host_id: str):
    """Manually refresh a host's status and connectivity"""
    host = host_manager.get_host(host_id)
    if not host:
        raise HTTPException(status_code=404, detail="Host not found")

    try:
        # Check health endpoint
        async with aiohttp.ClientSession() as session:
            url = f"{host.url}/health"
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=5)
            ) as response:
                if response.status != 200:
                    from app.models import HostStatus

                    host_manager.update_host_status(host_id, HostStatus.ERROR)
                    raise HTTPException(
                        status_code=400,
                        detail=f"Health check failed with status {response.status}",
                    )

            # Check instances endpoint with API key
            url = f"{host.url}/instances"
            headers = {"X-API-Key": host.api_key}
            async with session.get(
                url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)
            ) as response:
                if response.status == 200:
                    from app.models import HostStatus

                    host_manager.update_host_status(host_id, HostStatus.ONLINE)
                    host = host_manager.get_host(host_id)  # Get updated host
                    if not host:
                        raise HTTPException(
                            status_code=404, detail="Host not found after update"
                        )
                    return HostResponse(
                        host=host,
                        message=f"Host '{host.name}' is online and responding",
                    )
                else:
                    from app.models import HostStatus

                    host_manager.update_host_status(host_id, HostStatus.ERROR)
                    raise HTTPException(
                        status_code=400,
                        detail=f"API authentication failed with status {response.status}",
                    )
    except HTTPException:
        raise
    except Exception as e:
        from app.models import HostStatus

        host_manager.update_host_status(host_id, HostStatus.OFFLINE)
        raise HTTPException(
            status_code=500, detail=f"Failed to connect to host: {str(e)}"
        )


@router.post("/refresh-all")
async def refresh_all_hosts():
    """Refresh status for all registered hosts"""
    from app.models import HostStatus

    hosts = host_manager.get_all_hosts()
    results = []

    async with aiohttp.ClientSession() as session:
        for host in hosts:
            try:
                # Check health endpoint
                url = f"{host.url}/health"
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=5)
                ) as response:
                    if response.status != 200:
                        host_manager.update_host_status(host.id, HostStatus.ERROR)
                        results.append(
                            {
                                "host_id": host.id,
                                "name": host.name,
                                "status": "error",
                                "message": f"Health check failed with status {response.status}",
                            }
                        )
                        continue

                # Check instances endpoint with API key
                url = f"{host.url}/instances"
                headers = {"X-API-Key": host.api_key}
                async with session.get(
                    url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)
                ) as response:
                    if response.status == 200:
                        host_manager.update_host_status(host.id, HostStatus.ONLINE)
                        results.append(
                            {
                                "host_id": host.id,
                                "name": host.name,
                                "status": "online",
                                "message": "Connected successfully",
                            }
                        )
                    else:
                        host_manager.update_host_status(host.id, HostStatus.ERROR)
                        results.append(
                            {
                                "host_id": host.id,
                                "name": host.name,
                                "status": "error",
                                "message": f"API authentication failed with status {response.status}",
                            }
                        )
            except Exception as e:
                host_manager.update_host_status(host.id, HostStatus.OFFLINE)
                results.append(
                    {
                        "host_id": host.id,
                        "name": host.name,
                        "status": "offline",
                        "message": f"Failed to connect: {str(e)}",
                    }
                )

    return {"message": f"Refreshed {len(hosts)} hosts", "results": results}


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

            async with session.get(
                url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    raise HTTPException(
                        status_code=response.status,
                        detail="Failed to get instances from host",
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

            async with session.post(
                url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)
            ) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    raise HTTPException(
                        status_code=response.status, detail="Failed to start instance"
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

            async with session.post(
                url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)
            ) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    raise HTTPException(
                        status_code=response.status, detail="Failed to stop instance"
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

            async with session.post(
                url, headers=headers, timeout=aiohttp.ClientTimeout(total=60)
            ) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    raise HTTPException(
                        status_code=response.status, detail="Failed to restart instance"
                    )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{host_id}/instances")
async def create_instance(host_id: str, instance_data: dict):
    """Create a new instance on a host"""
    host = host_manager.get_host(host_id)
    if not host:
        raise HTTPException(status_code=404, detail="Host not found")

    try:
        async with aiohttp.ClientSession() as session:
            url = f"{host.url}/instances"
            headers = {"X-API-Key": host.api_key, "Content-Type": "application/json"}

            async with session.post(
                url,
                json=instance_data,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    error_text = await response.text()
                    raise HTTPException(
                        status_code=response.status,
                        detail=f"Failed to create instance: {error_text}",
                    )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/{host_id}/instances/{instance_id}")
async def update_instance(host_id: str, instance_id: str, instance_data: dict):
    """Update an instance configuration on a host"""
    host = host_manager.get_host(host_id)
    if not host:
        raise HTTPException(status_code=404, detail="Host not found")

    try:
        async with aiohttp.ClientSession() as session:
            url = f"{host.url}/instances/{instance_id}"
            headers = {"X-API-Key": host.api_key, "Content-Type": "application/json"}

            async with session.put(
                url,
                json=instance_data,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    error_text = await response.text()
                    raise HTTPException(
                        status_code=response.status,
                        detail=f"Failed to update instance: {error_text}",
                    )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{host_id}/instances/{instance_id}")
async def delete_instance(host_id: str, instance_id: str):
    """Delete an instance from a host"""
    host = host_manager.get_host(host_id)
    if not host:
        raise HTTPException(status_code=404, detail="Host not found")

    try:
        async with aiohttp.ClientSession() as session:
            url = f"{host.url}/instances/{instance_id}"
            headers = {"X-API-Key": host.api_key}

            async with session.delete(
                url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    error_text = await response.text()
                    raise HTTPException(
                        status_code=response.status,
                        detail=f"Failed to delete instance: {error_text}",
                    )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{host_id}/instances/{instance_id}/state")
async def get_instance_state(host_id: str, instance_id: str):
    """Proxy runtime state snapshot for a specific instance on a host"""
    host = host_manager.get_host(host_id)
    if not host:
        raise HTTPException(status_code=404, detail="Host not found")

    try:
        async with aiohttp.ClientSession() as session:
            url = f"{host.url}/instances/{instance_id}/state"
            headers = {"X-API-Key": host.api_key}
            async with session.get(
                url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)
            ) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    text = await response.text()
                    raise HTTPException(
                        status_code=response.status,
                        detail=f"Failed to get instance state: {text}",
                    )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
