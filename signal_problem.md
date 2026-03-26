# OCP Sidecar Signal Relay — Complete Flow

## Architecture

```
UI ──POST /continue──► Your Existing API Pod (ocp.api/continue)
                              │
                              │ stores signal in DB/memory
                              │
Sidecar (in job pod) ──GET /signal──► polls your API
    │
    │ got "continue" signal
    │ writes to shared volume
    ▼
/signals/continue
    │
    │ main job reads it, unblocks
    ▼
Main Container (backend job)
```

---

## 1. Main Job — `main_app/app.py`

```python
import os
import time

SIGNALS_DIR = "/signals"


def write_status(msg):
    """Write current status so sidecar/API can report it."""
    with open(os.path.join(SIGNALS_DIR, "status"), "w") as f:
        f.write(msg)


def wait_for_signal(step: str = "continue") -> str:
    """Block until the signal file appears, then read and delete it."""
    signal_path = os.path.join(SIGNALS_DIR, step)

    # Clean up any old signal
    if os.path.exists(signal_path):
        os.remove(signal_path)

    # Tell the world we're waiting
    write_status(f"waiting:{step}")
    print(f"⏸️  Waiting for signal: {step}")

    # Poll until file appears
    while not os.path.exists(signal_path):
        time.sleep(0.5)

    # Read the payload
    with open(signal_path, "r") as f:
        payload = f.read().strip()

    # Clean up
    os.remove(signal_path)
    write_status("running")
    print(f"▶️  Got signal: {step}, payload: {payload}")

    return payload


def main():
    print("Step 1: Processing data...")
    write_status("running:step1")
    time.sleep(5)  # simulate work
    print("Step 1 done.")

    # ⏸️ PAUSE — waits here until UI sends "continue"
    wait_for_signal("continue")

    print("Step 2: Processing more data...")
    write_status("running:step2")
    time.sleep(5)
    print("Step 2 done.")

    # ⏸️ PAUSE — waits for user approval
    user_input = wait_for_signal("approve")
    print(f"User said: {user_input}")

    print("Step 3: Final step...")
    write_status("running:step3")
    time.sleep(5)

    write_status("done")
    print("All done!")


if __name__ == "__main__":
    main()
```

### `main_app/Dockerfile`

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY app.py .
CMD ["python", "app.py"]
```

---

## 2. Sidecar Poller — `sidecar/poller.py`

```python
import os
import time
import requests

# Your existing API route on OCP
API_URL = os.getenv("API_URL", "https://ocp.api.apps.my-cluster.com")
SIGNALS_DIR = os.getenv("SIGNALS_DIR", "/signals")
JOB_ID = os.getenv("JOB_ID", "job-123")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "2"))


def read_status() -> str:
    """Read the main job's current status from the shared volume."""
    status_path = os.path.join(SIGNALS_DIR, "status")
    if os.path.exists(status_path):
        with open(status_path, "r") as f:
            return f.read().strip()
    return "unknown"


def report_status():
    """Report main job's status back to the API so UI can poll it."""
    status = read_status()
    try:
        requests.post(
            f"{API_URL}/status/{JOB_ID}",
            json={"status": status},
            timeout=5
        )
    except Exception as e:
        print(f"Status report failed: {e}")


def poll_and_relay():
    """Poll the API for signals and relay them to the shared volume."""
    print(f"Sidecar started. Polling {API_URL} every {POLL_INTERVAL}s for job {JOB_ID}")

    while True:
        try:
            # 1. Report current status to API
            report_status()

            # 2. Check if there's a signal from the UI
            resp = requests.get(
                f"{API_URL}/signal/{JOB_ID}",
                timeout=5
            )
            data = resp.json()

            # 3. If signal found, write it to the shared volume
            if data.get("action"):
                action = data["action"]
                payload = data.get("payload", "go")
                signal_path = os.path.join(SIGNALS_DIR, action)

                with open(signal_path, "w") as f:
                    f.write(payload)

                print(f"✅ Relayed signal: {action} (payload: {payload})")

        except Exception as e:
            print(f"Poll error: {e}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    poll_and_relay()
```

### `sidecar/Dockerfile`

```dockerfile
FROM python:3.11-slim
RUN pip install requests
WORKDIR /app
COPY poller.py .
CMD ["python", "poller.py"]
```

---

## 3. Your Existing API — Add These Endpoints

Add these to your existing FastAPI app running on another pod:

```python
from fastapi import FastAPI
from pydantic import BaseModel
from typing import Optional

app = FastAPI()

# ── In-memory signal store (swap with Redis/DB for production) ──
pending_signals = {}   # job_id -> {"action": "continue", "payload": "go"}
job_statuses = {}      # job_id -> "waiting:continue"


class SignalRequest(BaseModel):
    action: str         # "continue" or "approve"
    payload: str = ""   # optional data


class StatusUpdate(BaseModel):
    status: str


# ── UI calls these ──

@app.post("/continue/{job_id}")
async def send_continue(job_id: str, req: SignalRequest):
    """UI sends a signal (continue, approve, etc.)"""
    pending_signals[job_id] = {"action": req.action, "payload": req.payload}
    return {"status": "queued", "job_id": job_id, "action": req.action}


@app.get("/job-status/{job_id}")
async def get_job_status(job_id: str):
    """UI polls this to show current job state."""
    return {"job_id": job_id, "status": job_statuses.get(job_id, "unknown")}


# ── Sidecar calls these ──

@app.get("/signal/{job_id}")
async def get_signal(job_id: str):
    """Sidecar polls this. Returns and removes pending signal."""
    signal = pending_signals.pop(job_id, None)
    if signal:
        return signal
    return {"action": None}


@app.post("/status/{job_id}")
async def update_status(job_id: str, req: StatusUpdate):
    """Sidecar reports main job's status here."""
    job_statuses[job_id] = req.status
    return {"status": "updated"}
```

---

## 4. OCP Deployment YAML — `deployment.yaml`

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: my-job
  labels:
    app: my-job
spec:
  replicas: 1
  selector:
    matchLabels:
      app: my-job
  template:
    metadata:
      labels:
        app: my-job
    spec:
      # ── Shared volume ──
      volumes:
      - name: signals
        emptyDir: {}

      containers:
      # ── MAIN JOB: your backend process ──
      - name: main-job
        image: image-registry.openshift-image-registry.svc:5000/my-namespace/main-job:latest
        volumeMounts:
        - name: signals
          mountPath: /signals

      # ── SIDECAR: polls your API, relays signals ──
      - name: signal-sidecar
        image: image-registry.openshift-image-registry.svc:5000/my-namespace/signal-sidecar:latest
        env:
        - name: API_URL
          value: "https://ocp.api.apps.my-cluster.com"   # ← your existing API route
        - name: JOB_ID
          value: "job-123"
        - name: POLL_INTERVAL
          value: "2"
        volumeMounts:
        - name: signals
          mountPath: /signals
        resources:
          requests:
            memory: "64Mi"
            cpu: "50m"
          limits:
            memory: "128Mi"
            cpu: "100m"
```

---

## 5. UI — `ui/index.html`

```html
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Job Control Panel</title>
    <style>
        body { font-family: Arial, sans-serif; max-width: 600px; margin: 40px auto; padding: 20px; }
        .status { padding: 12px; border-radius: 6px; margin: 16px 0; font-size: 18px; }
        .status.waiting { background: #fff3cd; color: #856404; }
        .status.running { background: #d4edda; color: #155724; }
        .status.done { background: #cce5ff; color: #004085; }
        .status.unknown { background: #e2e3e5; color: #383d41; }
        button { padding: 12px 24px; font-size: 16px; cursor: pointer; border: none; border-radius: 6px; margin: 8px 4px; }
        .btn-continue { background: #28a745; color: white; }
        .btn-approve { background: #007bff; color: white; }
        button:hover { opacity: 0.9; }
        button:disabled { opacity: 0.5; cursor: not-allowed; }
        .hidden { display: none; }
    </style>
</head>
<body>
    <h2>Job Control Panel</h2>

    <label for="jobId">Job ID:</label>
    <input type="text" id="jobId" value="job-123" />

    <div class="status unknown" id="statusBox">Status: checking...</div>

    <div id="controls">
        <button class="btn-continue hidden" id="continueBtn" onclick="sendSignal('continue')">
            ▶ Continue
        </button>
        <button class="btn-approve hidden" id="approveBtn" onclick="sendSignal('approve', 'approved')">
            ✅ Approve
        </button>
    </div>

    <h3>Log</h3>
    <pre id="log"></pre>

    <script>
        // ← Replace with your actual API route
        const API_URL = "https://ocp.api.apps.my-cluster.com";

        function getJobId() {
            return document.getElementById("jobId").value;
        }

        function log(msg) {
            const el = document.getElementById("log");
            const time = new Date().toLocaleTimeString();
            el.textContent += `[${time}] ${msg}\n`;
            el.scrollTop = el.scrollHeight;
        }

        // Poll job status every 2 seconds
        setInterval(async () => {
            try {
                const res = await fetch(`${API_URL}/job-status/${getJobId()}`);
                const data = await res.json();
                const status = data.status || "unknown";

                const box = document.getElementById("statusBox");
                box.textContent = `Status: ${status}`;

                // Update styling
                box.className = "status";
                if (status.startsWith("waiting")) box.classList.add("waiting");
                else if (status.startsWith("running")) box.classList.add("running");
                else if (status === "done") box.classList.add("done");
                else box.classList.add("unknown");

                // Show/hide buttons based on what the job is waiting for
                document.getElementById("continueBtn").classList.toggle("hidden", status !== "waiting:continue");
                document.getElementById("approveBtn").classList.toggle("hidden", status !== "waiting:approve");

            } catch (e) {
                console.error("Status poll failed:", e);
            }
        }, 2000);

        async function sendSignal(action, payload = "") {
            try {
                const res = await fetch(`${API_URL}/continue/${getJobId()}`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ action, payload })
                });
                const data = await res.json();
                log(`Sent signal: ${action} → ${data.status}`);
            } catch (e) {
                log(`Error sending signal: ${e.message}`);
            }
        }
    </script>
</body>
</html>
```

---

## 6. Full Flow Diagram

```
┌──── OCP Cluster ─────────────────────────────────────────────┐
│                                                               │
│  ┌── Job Pod ──────────────────────────────────┐              │
│  │                                              │              │
│  │  ┌─ Main Job ────────┐  ┌─ Sidecar ───────┐ │              │
│  │  │                    │  │                  │ │              │
│  │  │  1. runs step 1   │  │  polls API       │ │              │
│  │  │  2. writes status │  │  every 2s        │ │              │
│  │  │  3. waits for     │  │                  │ │              │
│  │  │     /signals/cont │  │  GET /signal     │ │              │
│  │  │         ▲         │  │       │          │ │              │
│  │  │         │         │  │  if found:       │ │              │
│  │  │         └─────────┼──┼── write file     │ │              │
│  │  │   (shared volume) │  │                  │ │              │
│  │  └───────────────────┘  └────────┬─────────┘ │              │
│  │                                  │            │              │
│  │  volumes:                        │ HTTPS      │              │
│  │    - signals (emptyDir)          │            │              │
│  └──────────────────────────────────┼────────────┘              │
│                                     │                           │
│  ┌── API Pod ───────────────────────▼─────────┐                 │
│  │                                             │                │
│  │  FastAPI (your existing API)                │                │
│  │                                             │                │
│  │  POST /continue/{job_id}  ← UI sends       │◄── Route ◄── UI│
│  │  GET  /signal/{job_id}    ← sidecar polls   │                │
│  │  GET  /job-status/{job_id} ← UI polls       │                │
│  │  POST /status/{job_id}    ← sidecar reports │                │
│  │                                             │                │
│  └─────────────────────────────────────────────┘                │
└─────────────────────────────────────────────────────────────────┘
```

---

## Quick Commands

```bash
# Build images
docker build -t main-job:latest ./main_app
docker build -t signal-sidecar:latest ./sidecar

# Push to OCP internal registry
oc project my-namespace
docker tag main-job:latest image-registry.openshift-image-registry.svc:5000/my-namespace/main-job:latest
docker tag signal-sidecar:latest image-registry.openshift-image-registry.svc:5000/my-namespace/signal-sidecar:latest
docker push image-registry.openshift-image-registry.svc:5000/my-namespace/main-job:latest
docker push image-registry.openshift-image-registry.svc:5000/my-namespace/signal-sidecar:latest

# Deploy
oc apply -f deployment.yaml

# Check logs
oc logs <pod-name> -c main-job
oc logs <pod-name> -c signal-sidecar
```
