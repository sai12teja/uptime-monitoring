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

# The actual startup command. Runs app.py exactly like the __main__ block
# does today on Windows/waitress -- except sys.platform inside this
# container is "linux", so requirements.txt's platform marker means
# gunicorn is what actually got installed, not waitress. gunicorn is
# started directly here rather than via app.py's own __main__, matching
# how the systemd version of this deployment (DEPLOY_HOSTINGER.md) already
# runs it -- one process, one worker (see that doc for why: the login
# rate-limiter and the background check scheduler are both in-memory
# per-process state, so a second worker would double-check every site and
# silently halve the brute-force lockout protection).
CMD ["gunicorn", "--workers", "1", "--threads", "4", "--bind", "0.0.0.0:8050", "--timeout", "60", "app:server"]
