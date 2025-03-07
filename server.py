from fastapi import FastAPI, WebSocket, HTTPException, Query
from typing import Optional
from starlette.websockets import WebSocketState
from fastapi.middleware.cors import CORSMiddleware
import psutil
import json
import asyncio
from datetime import datetime, UTC
import subprocess
import os
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, EmailStr
from passlib.context import CryptContext
from collections import defaultdict
from contextlib import asynccontextmanager

# -----------------------
# Configuración inicial de FastAPI y CORS
# -----------------------

# Intervalos de actualización para cada endpoint de WebSocket (ahora mutable)
update_interval = {
    "cpu": 3,
    "memory": 5,
    "network": 2,
    "processes": 5,
    "disk": 5
}

# -----------------------
# Configuración de MongoDB para usuarios y métricas
# -----------------------

MONGO_URI = "mongodb+srv://team:SIANSHYO@cluster0.ccwlf.mongodb.net/contactos_db?retryWrites=true&w=majority"
client = AsyncIOMotorClient(MONGO_URI)
db = client.monitoring_db
db2 = client.usuarios_db
users_collection = db2.users
metrics_collections = {
    "cpu": db.cpu_metrics,
    "memory": db.memory_metrics,
    "network": db.network_metrics,
    "processes": db.process_metrics,
    "disk": db.disk_metrics
}

metrics_queue = defaultdict(list)

# -----------------------
# Funciones auxiliares para seguridad y manejo de contraseñas
# -----------------------
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def hash_password(password: str) -> str:
    return pwd_context.hash(password)

def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)

# -----------------------
# Tarea en segundo plano para guardar métricas en MongoDB
# -----------------------
async def save_metrics_task():
    """Guarda las métricas acumuladas cada 10 segundos en sus respectivas colecciones."""
    while True:
        await asyncio.sleep(10)
        for metric_type, data_list in metrics_queue.items():
            if data_list:
                collection = metrics_collections.get(metric_type)
                if collection is not None:
                    try:
                        await collection.insert_many(data_list)
                        metrics_queue[metric_type].clear()
                    except Exception as e:
                        print(f"Error guardando {metric_type}: {str(e)}")

# -----------------------
# Manejo del ciclo de vida de la aplicación con lifespan
# -----------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(save_metrics_task())
    yield

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# -----------------------
# Función auxiliar para obtener la temperatura de la CPU (en sistemas Linux)
# -----------------------
async def get_cpu_temperature():
    try:
        temps = []
        thermal_zones = [f for f in os.listdir('/sys/class/thermal') if f.startswith('thermal_zone')]
        for zone in thermal_zones:
            try:
                with open(f'/sys/class/thermal/{zone}/temp', 'r') as f:
                    temp = float(f.read()) / 1000
                    temps.append(temp)
            except Exception:
                continue
        return max(temps) if temps else None
    except Exception:
        return None

# -----------------------
# Manejador genérico para WebSockets que envía métricas y las acumula para MongoDB
# -----------------------
async def websocket_handler(websocket: WebSocket, endpoint: str, data_func):
    await websocket.accept()
    try:
        while websocket.client_state == WebSocketState.CONNECTED:
            data = await data_func()
            await websocket.send_text(json.dumps(data, default=lambda o: o.isoformat() if isinstance(o, datetime) else None))
            metrics_data = data.copy()
            metrics_data.pop("timestamp", None)
            metrics_queue[endpoint].append(metrics_data)
            await asyncio.sleep(update_interval[endpoint])  # Usar intervalo dinámico
    except (ConnectionResetError, RuntimeError) as e:
        print(f"Error en el endpoint {endpoint}: {str(e)}")
    finally:
        if websocket.application_state == WebSocketState.CONNECTED:
            await websocket.close()

# -----------------------
# Endpoints WebSocket para métricas
# -----------------------
@app.websocket("/ws/cpu")
async def websocket_cpu(websocket: WebSocket):
    async def get_cpu_data():
        cpu_times = psutil.cpu_times_percent(percpu=False)
        return {
            "usage": psutil.cpu_percent(interval=1),
            "frequency": psutil.cpu_freq().current if hasattr(psutil, 'cpu_freq') else None,
            "temperature": await get_cpu_temperature(),
            "cores": psutil.cpu_count(logical=False),
            "logical_cores": psutil.cpu_count(logical=True),
            "times": {
                "user": cpu_times.user,
                "system": cpu_times.system,
                "idle": cpu_times.idle,
                "iowait": getattr(cpu_times, 'iowait', 0)
            },
            "timestamp": datetime.utcnow()
        }
    await websocket_handler(websocket, "cpu", get_cpu_data)

@app.websocket("/ws/memory")
async def websocket_memory(websocket: WebSocket):
    async def get_memory_data():
        memory = psutil.virtual_memory()
        swap = psutil.swap_memory()
        return {
            "total": memory.total,
            "available": memory.available,
            "used": memory.used,
            "free": memory.free,
            "percent": memory.percent,
            "swap_total": swap.total,
            "swap_used": swap.used,
            "swap_free": swap.free,
            "swap_percent": swap.percent,
            "timestamp": datetime.utcnow()
        }
    await websocket_handler(websocket, "memory", get_memory_data)

@app.websocket("/ws/network")
async def websocket_network(websocket: WebSocket):
    prev_io = psutil.net_io_counters()
    prev_time = datetime.now()
    async def get_network_data():
        nonlocal prev_io, prev_time
        current_io = psutil.net_io_counters()
        current_time = datetime.now()
        time_diff = (current_time - prev_time).total_seconds()
        data = {
            "bytes_sent": current_io.bytes_sent,
            "bytes_recv": current_io.bytes_recv,
            "packets_sent": current_io.packets_sent,
            "packets_recv": current_io.packets_recv,
            "errin": current_io.errin,
            "errout": current_io.errout,
            "dropin": current_io.dropin,
            "dropout": current_io.dropout,
            "speed_sent": (current_io.bytes_sent - prev_io.bytes_sent) / time_diff,
            "speed_recv": (current_io.bytes_recv - prev_io.bytes_recv) / time_diff,
            "timestamp": datetime.utcnow()
        }
        prev_io = current_io
        prev_time = current_time
        return data
    await websocket_handler(websocket, "network", get_network_data)

@app.websocket("/ws/processes")
async def websocket_processes(websocket: WebSocket):
    async def get_processes_data():
        try:
            result = subprocess.run(['ps', 'aux'], capture_output=True, text=True)
            lines = result.stdout.strip().split('\n')[1:]
            processes = []
            for line in lines:
                parts = line.split()
                if len(parts) >= 11:
                    process = {
                        "USER": parts[0],
                        "PID": int(parts[1]),
                        "%CPU": float(parts[2]),
                        "%MEM": float(parts[3]),
                        "START": parts[8],
                        "TIME": parts[9],
                        "COMMAND": " ".join(parts[10:])
                    }
                    processes.append(process)
            return {
                "count": len(processes),
                "processes": sorted(processes, key=lambda x: x['%CPU'], reverse=True)[:50],
                "timestamp": datetime.now(UTC)
            }
        except Exception as e:
            print(f"Error al obtener procesos: {e}")
            return {
                "count": 0,
                "processes": [],
                "timestamp": datetime.now(UTC)
            }
    await websocket_handler(websocket, "processes", get_processes_data)

@app.websocket("/ws/disk")
async def websocket_disk(websocket: WebSocket):
    prev_io = psutil.disk_io_counters()
    async def get_disk_data():
        nonlocal prev_io
        partitions = psutil.disk_partitions()
        disks = []
        for partition in partitions:
            try:
                usage = psutil.disk_usage(partition.mountpoint)
                disks.append({
                    "device": partition.device,
                    "mountpoint": partition.mountpoint,
                    "total": usage.total,
                    "used": usage.used,
                    "free": usage.free,
                    "percent": usage.percent
                })
            except Exception:
                continue
        current_io = psutil.disk_io_counters()
        data = {
            "disks": disks,
            "read_count": current_io.read_count,
            "write_count": current_io.write_count,
            "read_bytes": current_io.read_bytes,
            "write_bytes": current_io.write_bytes,
            "timestamp": datetime.utcnow()
        }
        prev_io = current_io
        return data
    await websocket_handler(websocket, "disk", get_disk_data)

# -----------------------
# Endpoints HTTP para registro y login de usuarios
# -----------------------
class UserRegister(BaseModel):
    username: str
    email: EmailStr
    password: str

class UserLogin(BaseModel):
    email: EmailStr
    password: str

@app.post("/register")
async def register(user: UserRegister):
    existing_user = await users_collection.find_one({"email": user.email})
    if existing_user:
        raise HTTPException(status_code=400, detail="Email ya registrado")
    
    hashed_password = hash_password(user.password)
    user_data = {
        "username": user.username,
        "email": user.email,
        "password": hashed_password,
        "created_at": datetime.utcnow()
    }
    
    result = await users_collection.insert_one(user_data)
    return {"status": "success", "user_id": str(result.inserted_id)}

@app.post("/login")
async def login(user: UserLogin):
    existing_user = await users_collection.find_one({"email": user.email})
    if not existing_user:
        raise HTTPException(status_code=401, detail="Credenciales inválidas")
    
    if not verify_password(user.password, existing_user["password"]):
        raise HTTPException(status_code=401, detail="Credenciales inválidas")
    
    return {"status": "success", "user_id": str(existing_user["_id"]), "name": existing_user.get("username", "Usuario")}

# -----------------------
# Nueva ruta para actualizar los intervalos desde el frontend
# -----------------------
class UpdateIntervals(BaseModel):
    cpu: Optional[int]
    memory: Optional[int]
    network: Optional[int]
    processes: Optional[int]
    disk: Optional[int]

@app.post("/update-intervals")
async def set_update_intervals(intervals: UpdateIntervals):
    global update_interval
    new_intervals = intervals.dict(exclude_unset=True)
    for key, value in new_intervals.items():
        if key in update_interval:
            update_interval[key] = value
        else:
            raise HTTPException(status_code=400, detail=f"Intervalo inválido: {key}")
    return {"status": "success", "update_interval": update_interval}

# -----------------------
# Endpoint HTTP para reporte de métricas
# -----------------------
@app.post("/report")
async def get_metrics_report(
    start_time: Optional[str] = Query(None, description="ISO datetime (e.g., 2025-03-03T00:00:00)"),
    end_time: Optional[str] = Query(None, description="ISO datetime (e.g., 2025-03-03T23:59:59)")
):
    """Genera un reporte general de métricas almacenadas en MongoDB."""
    try:
        query = {}
        if start_time and end_time:
            query["timestamp"] = {
                "$gte": datetime.fromisoformat(start_time.replace("Z", "+00:00")),
                "$lte": datetime.fromisoformat(end_time.replace("Z", "+00:00"))
            }

        report = {}
        
        cpu_docs = await metrics_collections["cpu"].find(query).to_list(None)
        if cpu_docs:
            usages = [doc["usage"] for doc in cpu_docs]
            report["cpu"] = {
                "average_usage": sum(usages) / len(usages),
                "max_usage": max(usages),
                "min_usage": min(usages),
                "sample_count": len(usages)
            }

        memory_docs = await metrics_collections["memory"].find(query).to_list(None)
        if memory_docs:
            percents = [doc["percent"] for doc in memory_docs]
            report["memory"] = {
                "average_usage_percent": sum(percents) / len(percents),
                "max_usage_percent": max(percents),
                "min_usage_percent": min(percents),
                "sample_count": len(percents)
            }

        network_docs = await metrics_collections["network"].find(query).to_list(None)
        if network_docs:
            speed_sent = [doc["speed_sent"] for doc in network_docs]
            speed_recv = [doc["speed_recv"] for doc in network_docs]
            report["network"] = {
                "average_speed_sent": sum(speed_sent) / len(speed_sent),
                "average_speed_recv": sum(speed_recv) / len(speed_recv),
                "max_speed_sent": max(speed_sent),
                "max_speed_recv": max(speed_recv),
                "sample_count": len(network_docs)
            }

        processes_docs = await metrics_collections["processes"].find(query).to_list(None)
        if processes_docs:
            total_processes = [doc["count"] for doc in processes_docs]
            cpu_usages = [sum(proc["%CPU"] for proc in doc["processes"]) for doc in processes_docs]
            report["processes"] = {
                "average_process_count": sum(total_processes) / len(total_processes),
                "max_process_count": max(total_processes),
                "average_total_cpu": sum(cpu_usages) / len(cpu_usages),
                "sample_count": len(processes_docs)
            }

        disk_docs = await metrics_collections["disk"].find(query).to_list(None)
        if disk_docs:
            disk_usage = [sum(d["percent"] for d in doc["disks"]) / len(doc["disks"]) for doc in disk_docs]
            report["disk"] = {
                "average_usage_percent": sum(disk_usage) / len(disk_usage),
                "max_usage_percent": max(disk_usage),
                "min_usage_percent": min(disk_usage),
                "sample_count": len(disk_docs)
            }

        return report
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error generando reporte: {str(e)}")

# -----------------------
# Ejecución del servidor
# -----------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)