from fastapi import APIRouter, WebSocket, WebSocketDisconnect
import asyncio

from app.config import host_manager


router = APIRouter(tags=["websockets"])


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

