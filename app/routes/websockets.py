from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from typing import List
import asyncio
import aiohttp

from app.config import host_manager


router = APIRouter(tags=["websockets"])

# Connection manager for status updates
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        disconnected = []
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception:
                disconnected.append(connection)
        
        # Clean up disconnected clients
        for conn in disconnected:
            self.disconnect(conn)

manager = ConnectionManager()
routing_manager = ConnectionManager()

async def broadcast_host_status(status_update: dict):
    """Broadcast host status updates to all connected clients"""
    await manager.broadcast({
        "type": "host_status",
        "data": status_update
    })

async def broadcast_routing_event(event_data: dict):
    """Broadcast routing events to all connected clients"""
    await routing_manager.broadcast(event_data)


@router.websocket("/status")
async def status_updates(websocket: WebSocket):
    """WebSocket endpoint for real-time host status updates"""
    await manager.connect(websocket)
    
    try:
        # Send current status of all hosts immediately
        hosts = host_manager.get_all_hosts()
        await websocket.send_json({
            "type": "initial_status",
            "data": [{
                "host_id": host.id,
                "name": host.name,
                "status": host.status.value,
                "url": host.url,
                "last_seen": host.last_seen.isoformat() if host.last_seen else None
            } for host in hosts]
        })
        
        # Keep connection alive and wait for disconnect
        while True:
            # Receive ping/pong to keep connection alive
            try:
                message = await asyncio.wait_for(websocket.receive_text(), timeout=30)
                if message == "ping":
                    await websocket.send_text("pong")
            except asyncio.TimeoutError:
                # No message received, send a keepalive
                await websocket.send_json({"type": "keepalive"})
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:
        print(f"WebSocket error: {e}")
        manager.disconnect(websocket)


@router.websocket("/routing")
async def routing_updates(websocket: WebSocket):
    """WebSocket endpoint for real-time routing events"""
    await routing_manager.connect(websocket)
    
    try:
        # Keep connection alive and wait for disconnect
        while True:
            # Receive ping/pong to keep connection alive
            try:
                message = await asyncio.wait_for(websocket.receive_text(), timeout=30)
                if message == "ping":
                    await websocket.send_text("pong")
            except asyncio.TimeoutError:
                # No message received, send a keepalive
                await websocket.send_json({"type": "keepalive"})
    except WebSocketDisconnect:
        routing_manager.disconnect(websocket)
    except Exception as e:
        print(f"Routing WebSocket error: {e}")
        routing_manager.disconnect(websocket)


@router.websocket("/logs/{host_id}/{instance_id}")
async def proxy_instance_logs(websocket: WebSocket, host_id: str, instance_id: str):
    """Proxy WebSocket connection to solar-host for instance logs"""
    await websocket.accept()
    
    # Get the host
    host = host_manager.get_host(host_id)
    if not host:
        await websocket.send_json({"error": f"Host {host_id} not found"})
        await websocket.close()
        return
    
    # Build WebSocket URL to solar-host
    ws_url = f"{host.url.replace('http://', 'ws://').replace('https://', 'wss://')}/instances/{instance_id}/logs"
    
    try:
        # Connect to solar-host WebSocket
        async with aiohttp.ClientSession() as session:
            headers = {"X-API-Key": host.api_key}
            async with session.ws_connect(ws_url, headers=headers) as host_ws:
                # Create bidirectional proxy
                async def forward_to_client():
                    """Forward messages from solar-host to client"""
                    try:
                        async for msg in host_ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                await websocket.send_text(msg.data)
                            elif msg.type == aiohttp.WSMsgType.ERROR:
                                break
                    except Exception:
                        pass
                
                async def forward_to_host():
                    """Forward messages from client to solar-host"""
                    try:
                        while True:
                            data = await websocket.receive_text()
                            await host_ws.send_str(data)
                    except WebSocketDisconnect:
                        pass
                    except Exception:
                        pass
                
                # Run both directions concurrently
                await asyncio.gather(
                    forward_to_client(),
                    forward_to_host(),
                    return_exceptions=True
                )
                
    except aiohttp.ClientError as e:
        await websocket.send_json({"error": f"Failed to connect to solar-host: {str(e)}"})
    except Exception as e:
        await websocket.send_json({"error": f"Proxy error: {str(e)}"})
    finally:
        try:
            await websocket.close()
        except Exception:
            pass

