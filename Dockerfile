# --- Rovix Uptime Monitoring: container image ---
#
# Learning notes (this is the team's first Docker deployment):
#
# Every line below is one "layer". Docker builds layers top to bottom and
# CACHES each one -- if a layer's inputs haven't changed since the last
# build, Docker reuses it instead of re-running it. That's why requirements.txt
# is copied and installed BEFORE the rest of the code: your Python code
# changes constantly, but your dependency list rarely does. This ordering
# means "docker build" after a normal code change reuses the slow pip-install
# layer and only re-runs the fast "copy code" step.

FROM python:3.12-slim

# Where the app lives INSIDE the container -- unrelated to any path on the
# VPS itself. Containers have their own private filesystem.
WORKDIR /app

# python:*-slim ships no timezone database, so TZ=Asia/Kolkata in
# docker-compose.yml would be silently ignored and alert emails would keep
# printing UTC. ~3MB, and it sits above the code COPY so it stays cached.
RUN apt-get update && apt-get install -y --no-install-recommends tzdata && rm -rf /var/lib/apt/lists/*

# Copy just the dependency list first (see caching note above).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Now copy the actual application code.
COPY . .

# Documents which port the app listens on inside the container. This does
# NOT publish the port to the internet by itself -- that mapping happens in
# docker-compose.yml (next file), same as how none of the other 25
# containers on this VPS are reachable except through the ports their
# compose files explicitly publish.
EXPOSE 8050

# The actual startup command. Points at wsgi.py, NOT app:server directly --
# gunicorn importing app.py as a plain module skips its `if __name__ ==
# "__main__":` block entirely (__name__ is "app" when imported, never
# "__main__"), which is where the session secret key, the background check
# scheduler, and the favicon backfill thread all get started. wsgi.py
# replicates exactly that setup, then exposes `server` for gunicorn.
#
# One worker, matching the systemd version of this deployment
# (DEPLOY_HOSTINGER.md) -- the login rate-limiter and the background check
# scheduler are both in-memory per-process state, so a second worker would
# double-check every site and silently halve the brute-force lockout
# protection.
CMD ["gunicorn", "--workers", "1", "--threads", "4", "--bind", "0.0.0.0:8050", "--timeout", "60", "wsgi:server"]
