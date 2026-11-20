"""
SERVER HEALTH: temperatures, disk space and memory, checked every 5 minutes by the
runner. Pushes to your phone only when something is wrong (at most once an hour per
problem). SSD wear is watched by smartd, set up by linux/server-setup.sh.
"""
import shutil
import subprocess
import time

import psutil

import broker as B
import state

CPU_MAX_C, GPU_MAX_C, DISK_MIN_FREE, MEM_MAX = 90, 85, 0.10, 0.90  # mini PCs run warm; chip limit is ~100


def gpu_temp():
    """NVIDIA card (Aero) via nvidia-smi, else AMD graphics (SER9 Pro) via the amdgpu sensor."""
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10).stdout.strip()
        if out:
            return float(out.splitlines()[0])
    except Exception:
        pass
    try:
        vals = [t.current for t in (psutil.sensors_temperatures() or {}).get("amdgpu", [])]
        return max(vals) if vals else None
    except Exception:
        return None


def cpu_temp():
    try:
        temps = psutil.sensors_temperatures() or {}
    except AttributeError:  # macOS has no sensor API in psutil
        return None
    vals = [t.current for name in ("coretemp", "k10temp", "acpitz") for t in temps.get(name, [])]
    return max(vals) if vals else None


def problems() -> list:
    out = []
    c, g = cpu_temp(), gpu_temp()
    if c is not None and c >= CPU_MAX_C:
        out.append(("cpu", f"CPU at {c:.0f} °C (limit {CPU_MAX_C}). Fans may need cleaning."))
    if g is not None and g >= GPU_MAX_C:
        out.append(("gpu", f"GPU at {g:.0f} °C (limit {GPU_MAX_C}). Check the power cap and fans."))
    du = shutil.disk_usage("/")
    if du.free / du.total < DISK_MIN_FREE:
        out.append(("disk", f"Disk {du.free / 1e9:.0f} GB free ({du.free / du.total:.0%}). Clean up logs or data."))
    if psutil.virtual_memory().percent / 100 > MEM_MAX:
        out.append(("mem", f"Memory {psutil.virtual_memory().percent:.0f}% used."))
    return out


def check():
    for key, msg in problems():
        k = f"health_alert_{key}"
        if time.time() - (state.get(k) or 0) > 3600:
            state.put(k, time.time())
            B.alert(f"Server health: {msg}")


def snapshot() -> str:
    c, g = cpu_temp(), gpu_temp()
    du = shutil.disk_usage("/")
    parts = [f"CPU {c:.0f}°C" if c else None, f"GPU {g:.0f}°C" if g else None,
             f"disk {du.free / 1e9:.0f} GB free", f"RAM {psutil.virtual_memory().percent:.0f}%"]
    return "Server: " + ", ".join(p for p in parts if p)


if __name__ == "__main__":
    print(snapshot())
    print(problems() or "no problems")
