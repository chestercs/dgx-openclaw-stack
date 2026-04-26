# Sanitizing proxy for the vLLM gemma4 streaming tool-call bug
# (vllm-project/vllm#38946). Slim Python base — no GPU, no model weights;
# the proxy is a thin httpx forwarder.
FROM python:3.12-slim

# tini for clean PID 1 + sigterm propagation under docker compose down.
RUN apt-get update && apt-get install -y --no-install-recommends tini && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app.py /app/app.py

EXPOSE 8004

ENTRYPOINT ["tini", "--"]
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8004", "--no-access-log"]
