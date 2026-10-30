# OmniTune - Personal Offline Music Server
# Copyright (C) 2026-Present  DarThunder
# SPDX-License-Identifier: AGPL-3.0-or-later.

import asyncio
from contextlib import asynccontextmanager
import shutil
import socket
import subprocess
from fastapi import FastAPI
import uvicorn

from core.configs import PORT
from core.database import Base, engine, init_fts
from routers import media, users

RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RESET = "\033[0m"

def check_warp_status() -> None:
    if not shutil.which("warp-cli"):
        print(f"{YELLOW}[i] Cloudflare WARP no está instalado (opcional).{RESET}", flush=True)
        return

    try:
        res = subprocess.run(
            ["warp-cli", "status"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        output = res.stdout.lower()

        if "disconnected" not in output and "connected" in output:
            print(f"{GREEN}[✓] Cloudflare WARP conectado.{RESET}", flush=True)
        else:
            print(
                f"{YELLOW}[i] WARP está instalado pero desconectado. "
                f"Puedes activarlo con 'warp-cli connect' si lo deseas.{RESET}",
                flush=True,
            )
    except Exception as e:
        print(f"{RED}[!] No se pudo consultar el estado de WARP: {e}{RESET}", flush=True)

@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    init_fts(engine)
    check_warp_status()
    yield

app = FastAPI(
    title="OmniTune",
    description="Personal offline music server",
    lifespan=lifespan,
)

@app.get("/health")
def live():
    return {"message": "Hello pim"}

@app.get("/debug/tasks")
async def debug_tasks():
    tasks = asyncio.all_tasks()
    return {"total": len(tasks), "tasks": [str(t) for t in tasks]}

app.include_router(media.router)
app.include_router(users.router)

class KeepaliveServer(uvicorn.Server):
    async def startup(self, sockets=None):
        await super().startup(sockets)
        for server in self.servers:
            for sock in server.sockets:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                keepidle_opt = getattr(
                    socket, "TCP_KEEPIDLE", getattr(socket, "TCP_KEEPALIVE", 4)
                )
                sock.setsockopt(socket.IPPROTO_TCP, keepidle_opt, 120)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 30)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 5)

if __name__ == "__main__":
    config = uvicorn.Config(
        app=app,
        host="0.0.0.0",
        port=PORT,
        reload=False,
        timeout_graceful_shutdown=5,
    )
    server = KeepaliveServer(config)
    asyncio.run(server.serve())