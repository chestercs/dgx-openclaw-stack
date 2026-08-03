# MeshCore <-> OpenClaw bridge — pure-Python, no GPU, no host deps.
# The companion radio is passed through as a device node (see the
# openclaw-meshcore-bridge service in the root docker-compose.yml);
# everything else (meshcore serial protocol, gateway WS client) is pip-land.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bridge.py .

# Runtime identity comes from the compose file (user: "1000:1000" +
# group_add: dialout): uid 1000 owns the ro-mounted OpenClaw config volume
# (openclaw.json is 600 and cap_drop: [ALL] removes CAP_DAC_OVERRIDE, so
# even root couldn't read it), and dialout covers /dev/meshcore.
CMD ["python", "-u", "bridge.py"]
