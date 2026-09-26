"""
RPi Monitor - backend de métricas para Raspberry Pi + Docker.

Recolecta en segundo plano (cada INTERVAL segundos):
  * Métricas del host: CPU, RAM, swap, disco, temperatura, red, carga, uptime.
  * Métricas por contenedor: estado, CPU %, RAM usada, % de la RAM total, red, puertos.
Y las expone en /api/metrics y /api/history para el dashboard.
"""
import os
import time
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import psutil
from flask import Flask, jsonify, send_from_directory

try:
    import docker
except ImportError:  # permite probar el host sin el SDK de Docker
    docker = None

# ---------------------------------------------------------------- config ---
HOST_PROC = os.getenv("HOST_PROC", "/host/proc")
HOST_SYS = os.getenv("HOST_SYS", "/host/sys")
HOST_ROOT = os.getenv("HOST_ROOT", "/host/root")
INTERVAL = float(os.getenv("INTERVAL", "5"))
HISTORY = int(os.getenv("HISTORY", "360"))  # 360 * 5 s = 30 min

# Leer /proc y /sys del HOST (montados en el contenedor), no los del contenedor
if os.path.isdir(HOST_PROC):
    psutil.PROCFS_PATH = HOST_PROC
else:
    HOST_PROC = "/proc"
if not os.path.isdir(HOST_SYS):
    HOST_SYS = "/sys"
if not os.path.isdir(HOST_ROOT):
    HOST_ROOT = "/"

app = Flask(__name__, static_folder="static", static_url_path="")

_state = {"host": {}, "containers": [], "summary": {}, "updated": 0, "error": None}
_history = deque(maxlen=HISTORY)
_lock = threading.Lock()
_pool = ThreadPoolExecutor(max_workers=8)
_docker = None
_prev_net = None


# ------------------------------------------------------------- helpers ---
def _read(path, default=None):
    try:
        with open(path) as f:
            return f.read().strip().strip("\x00")
    except OSError:
        return default


def docker_client():
    global _docker
    if _docker is None and docker is not None:
        _docker = docker.from_env(timeout=10)
    return _docker


def read_temp():
    base = os.path.join(HOST_SYS, "class/thermal")
    try:
        for zone in sorted(os.listdir(base)):
            if zone.startswith("thermal_zone"):
                raw = _read(os.path.join(base, zone, "temp"))
                if raw:
                    return round(int(raw) / 1000, 1)
    except (OSError, ValueError):
        pass
    return None


def read_throttled():
    """Estado de throttling del firmware de la Pi (bajo voltaje / temperatura)."""
    raw = _read(os.path.join(HOST_SYS, "devices/platform/soc/soc:firmware/get_throttled"))
    if raw is None:
        return None
    try:
        v = int(raw, 16)
    except ValueError:
        return None
    return {
        "raw": raw,
        "under_voltage": bool(v & 0x1),
        "throttled": bool(v & 0x4),
        "soft_temp_limit": bool(v & 0x8),
        "under_voltage_occurred": bool(v & 0x10000),
        "throttling_occurred": bool(v & 0x40000),
    }


MODEL = (
    _read(os.path.join(HOST_PROC, "device-tree/model"))
    or _read(os.path.join(HOST_SYS, "firmware/devicetree/base/model"))
    or "Desconocido"
)
HOSTNAME = _read(os.path.join(HOST_ROOT, "etc/hostname")) or os.uname().nodename


def net_rates():
    """Bytes/s de subida y bajada en interfaces físicas (ignora lo, veth, docker, br-)."""
    global _prev_net
    rx = tx = 0
    for name, c in psutil.net_io_counters(pernic=True).items():
        if name == "lo" or name.startswith(("veth", "docker", "br-")):
            continue
        rx += c.bytes_recv
        tx += c.bytes_sent
    now = time.time()
    rate = (0.0, 0.0)
    if _prev_net:
        dt = now - _prev_net[0]
        if dt > 0:
            rate = (max(rx - _prev_net[1], 0) / dt, max(tx - _prev_net[2], 0) / dt)
    _prev_net = (now, rx, tx)
    return rate


def host_metrics():
    vm = psutil.virtual_memory()
    sw = psutil.swap_memory()
    du = psutil.disk_usage(HOST_ROOT)
    rx, tx = net_rates()
    return {
        "hostname": HOSTNAME,
        "model": MODEL,
        "cpu_percent": psutil.cpu_percent(),
        "cpu_per_core": psutil.cpu_percent(percpu=True),
        "cpu_count": psutil.cpu_count(),
        "load": [round(x, 2) for x in psutil.getloadavg()],
        "mem_total": vm.total,
        "mem_used": vm.total - vm.available,
        "mem_available": vm.available,
        "mem_percent": vm.percent,
        "swap_total": sw.total,
        "swap_used": sw.used,
        "swap_percent": sw.percent,
        "disk_total": du.total,
        "disk_used": du.used,
        "disk_percent": du.percent,
        "temp": read_temp(),
        "throttled": read_throttled(),
        "net_rx_rate": rx,
        "net_tx_rate": tx,
        "uptime": int(time.time() - psutil.boot_time()),
    }


def _parse_started(s):
    if not s or s.startswith("0001"):
        return None
    try:
        dt = datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        return int(time.time() - dt.timestamp())
    except ValueError:
        return None


def container_stats(c, host_total):
    attrs = c.attrs
    state = attrs.get("State", {})
    labels = attrs.get("Config", {}).get("Labels") or {}

    ports = []
    for cport, binds in (attrs.get("NetworkSettings", {}).get("Ports") or {}).items():
        for b in binds or []:
            hp = b.get("HostPort")
            if hp and not any(p["host"] == hp and p["container"] == cport for p in ports):
                ports.append({"host": hp, "container": cport})

    # Se considera "web" si publica un puerto TCP o tiene la etiqueta monitor.web=true.
    # Con monitor.web=false se excluye (útil para bases de datos con puerto expuesto).
    web_label = labels.get("monitor.web", "").lower()
    if web_label in ("1", "true", "yes"):
        is_web = True
    elif web_label in ("0", "false", "no"):
        is_web = False
    else:
        is_web = any(p["container"].endswith("/tcp") for p in ports)

    info = {
        "id": c.short_id,
        "name": c.name,
        "image": attrs.get("Config", {}).get("Image", ""),
        "status": c.status,
        "health": (state.get("Health") or {}).get("Status"),
        "restarts": attrs.get("RestartCount", 0),
        "uptime": _parse_started(state.get("StartedAt")) if c.status == "running" else None,
        "ports": ports,
        "url": labels.get("monitor.url"),
        "is_web": is_web,
        "cpu_percent": 0.0,
        "mem_used": 0,
        "mem_limit": 0,
        "mem_percent_host": 0.0,
        "net_rx": 0,
        "net_tx": 0,
    }
    if c.status != "running":
        return info

    try:
        s = c.stats(stream=False)
    except Exception:
        return info

    # --- memoria (descuenta la caché de páginas, igual que `docker stats`)
    mem = s.get("memory_stats") or {}
    st = mem.get("stats") or {}
    cache = st.get("inactive_file", st.get("total_inactive_file", 0))
    used = max(mem.get("usage", 0) - cache, 0)
    limit = mem.get("limit") or host_total
    info["mem_used"] = used
    info["mem_limit"] = min(limit, host_total)
    info["mem_percent_host"] = round(used / host_total * 100, 2) if host_total else 0

    # --- CPU (100 % = un núcleo completo, como `docker stats`)
    cpu = s.get("cpu_stats") or {}
    pre = s.get("precpu_stats") or {}
    cpu_delta = (cpu.get("cpu_usage") or {}).get("total_usage", 0) - (pre.get("cpu_usage") or {}).get("total_usage", 0)
    sys_delta = cpu.get("system_cpu_usage", 0) - pre.get("system_cpu_usage", 0)
    ncpu = cpu.get("online_cpus") or len((cpu.get("cpu_usage") or {}).get("percpu_usage") or [1])
    if sys_delta > 0 and cpu_delta > 0:
        info["cpu_percent"] = round(cpu_delta / sys_delta * ncpu * 100, 2)

    # --- red
    for n in (s.get("networks") or {}).values():
        info["net_rx"] += n.get("rx_bytes", 0)
        info["net_tx"] += n.get("tx_bytes", 0)
    return info


# ----------------------------------------------------------- collector ---
def collect_once():
    host = host_metrics()
    containers, err = [], None
    try:
        cl = docker_client()
        if cl is None:
            raise RuntimeError("SDK de Docker no instalado")
        cs = cl.containers.list(all=True)
        futs = [_pool.submit(container_stats, c, host["mem_total"]) for c in cs]
        containers = [f.result() for f in futs]
    except Exception as e:  # noqa: BLE001
        err = f"No se pudo leer Docker: {e}"

    containers.sort(key=lambda x: (x["status"] != "running", -x["mem_used"]))
    running = [c for c in containers if c["status"] == "running"]
    cont_mem = sum(c["mem_used"] for c in running)
    summary = {
        "containers_total": len(containers),
        "containers_running": len(running),
        "containers_stopped": len(containers) - len(running),
        "webs_running": sum(1 for c in running if c["is_web"]),
        "webs_total": sum(1 for c in containers if c["is_web"]),
        "unhealthy": sum(1 for c in containers if c["health"] == "unhealthy"),
        "containers_mem": cont_mem,
        "containers_mem_percent": round(cont_mem / host["mem_total"] * 100, 2) if host["mem_total"] else 0,
        "containers_cpu": round(sum(c["cpu_percent"] for c in running), 2),
    }
    now = time.time()
    with _lock:
        _state.update(host=host, containers=containers, summary=summary, updated=now, error=err)
        _history.append({
            "t": int(now),
            "cpu": host["cpu_percent"],
            "mem": host["mem_percent"],
            "temp": host["temp"],
            "rx": host["net_rx_rate"],
            "tx": host["net_tx_rate"],
        })


def collector_loop():
    psutil.cpu_percent()
    psutil.cpu_percent(percpu=True)
    while True:
        t0 = time.time()
        try:
            collect_once()
        except Exception as e:  # noqa: BLE001
            with _lock:
                _state["error"] = f"Error de recolección: {e}"
        time.sleep(max(INTERVAL - (time.time() - t0), 0.5))


threading.Thread(target=collector_loop, daemon=True, name="collector").start()


# ---------------------------------------------------------------- rutas ---
@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/metrics")
def api_metrics():
    with _lock:
        return jsonify({**_state, "interval": INTERVAL})


@app.get("/api/history")
def api_history():
    with _lock:
        return jsonify(list(_history))


@app.get("/healthz")
def healthz():
    ok = time.time() - _state["updated"] < INTERVAL * 4
    return ("ok", 200) if ok else ("stale", 503)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8088")))
