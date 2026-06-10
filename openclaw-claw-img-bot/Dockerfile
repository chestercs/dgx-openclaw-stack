# Tiny standalone Discord bot exposing the /claw-img slash command.
# Calls the comfyui_image bridge directly (no LLM, no approval gate) and
# uploads the result as a true Discord attachment. See ./README.md and
# docs/reference/image-comfyui-bridge.md.
FROM python:3.12-slim

# discord.py 2.x ships app_commands (slash) + bundles aiohttp, which we reuse
# for the bridge HTTP call. Pin the major to keep the app_commands API stable.
RUN pip install --no-cache-dir "discord.py>=2.4,<3"

WORKDIR /app
COPY bot.py /app/bot.py

# Run unprivileged (matches the rest of the stack's 1000:1000 convention).
USER 1000:1000

# -u: unbuffered stdout so `docker logs` shows lines immediately.
CMD ["python", "-u", "bot.py"]
