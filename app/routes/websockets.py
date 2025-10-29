from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from typing import List
import asyncio
import json

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

async def broadcast_host_status(status_update: dict):
    """Broadcast host status updates to all connected clients"""
    await manager.broadcast({
        "type": "host_status",
        "data": status_update
    })


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


@router.websocket("/logs")
async def aggregate_logs(websocket: WebSocket):
    """Aggregate logs from all instances across all hosts"""
    await websocket.accept()
    
    try:
        # Get all hosts
        hosts = host_manager.get_all_hosts()
        
        if not hosts:
            await websocket.send_json({"error": "No hosts registered"})
            await websocket.close()
            return
        
        # Create WebSocket connections to all host instances
        # This is a simplified version - in production you'd want more sophisticated aggregation
        while True:
            # Poll each host for instances and their logs
            # This is a basic implementation
            await asyncio.sleep(1)
            await websocket.send_json({
                "status": "aggregating",
                "hosts": len(hosts)
            })
                
    except WebSocketDisconnect:
        pass
    except Exception as e:
        await websocket.send_json({"error": str(e)})
    finally:
        await websocket.close()

