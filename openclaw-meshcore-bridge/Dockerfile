# MeshCore <-> OpenClaw bridge — pure-Python, no GPU, no host deps.
# The companion radio is passed through as a device node (see the
# openclaw-meshcore-bridge service in the root docker-compose.yml);
# everything else (meshcore serial protocol, gateway WS client) is pip-land.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bridge.py .

# Runs as root: the passed-through /dev/meshcore node keeps its host
# ownership (root:dialout 660) inside the container and this image drops
# all capabilities at the compose level anyway (cap_drop: [ALL]).
CMD ["python", "-u", "bridge.py"]
