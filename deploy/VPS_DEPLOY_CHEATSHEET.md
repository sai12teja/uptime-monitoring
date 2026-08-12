# VPS Deploy Cheat Sheet — from empty folder to live app

Reusable checklist, distilled from actually deploying Rovix Uptime
Monitoring to this Hostinger VPS. Swap the project name / repo URL / port
number for whatever you're deploying next.

## 0. One-time setup (only needed the first time, skip after)

```bash
# GitHub CLI, so a PRIVATE repo can be cloned without typing a token every time
curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | sudo dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg
sudo chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | sudo tee /etc/apt/sources.list.d/github-cli.list > /dev/null
sudo apt update && sudo apt install gh -y
gh auth login          # interactive: GitHub.com -> HTTPS -> Yes -> web browser
```
(Skip entirely if the repo is public.)

## 1. Before touching the VPS: check what's already running

Never assume a clean box. Find out what's already there so nothing
conflicts (ports, disk space, an existing reverse proxy):

```bash
docker ps -a                                    # what's already running
ss -tlnp | grep -E ':(80|443|YOUR_PORT)\s'       # is your port free?
df -h /                                          # disk space
docker inspect <existing-proxy-container> --format '{{range $n,$c := .NetworkSettings.Networks}}{{$n}}{{"\n"}}{{end}}'
                                                  # how does the existing reverse proxy (if any) network?
```

## 2. Create the folder + clone

```bash
mkdir -p /home/PROJECT_NAME
cd /home/PROJECT_NAME
git clone https://github.com/USER/REPO.git .     # trailing "." avoids a nested folder
```

## 3. Configure secrets (never committed to git)

```bash
cp .env.example .env
nano .env                     # fill in real values; Ctrl+O, Enter, Ctrl+X to save
grep -c "changeme\|example.com" .env    # sanity check: should print 0
```

## 4. Build and start

```bash
docker compose build
docker compose up -d
```

## 5. Verify it's actually healthy (don't trust "Started" alone)

```bash
docker ps | grep PROJECT_NAME          # want "Up X seconds", NOT "Restarting"
docker logs PROJECT_NAME --tail 30     # read the actual startup output
```

If it's restarting, the logs will show why — read the LAST error, not the
first. Fix, then:
```bash
git pull                       # after pushing a fix from your dev machine
docker compose build
docker compose up -d
```

## 6. Confirm it's reachable from the outside

From your own computer's browser (not the VPS terminal):
```
http://<vps-ip>:<port>
```
If it doesn't load but the container looks healthy, check Hostinger's
panel-level firewall (a layer above `ufw`, separate from the container) --
that's the most common silent blocker.

## 7. (If migrating an existing database) Move it in

```bash
# On your OWN machine, in your own terminal:
scp "path/to/your.db" root@<vps-ip>:/home/PROJECT_NAME/staging.db

# Back on the VPS:
docker volume inspect PROJECT_NAME_dbvolume --format '{{.Mountpoint}}'
docker compose stop PROJECT_NAME
cp /home/PROJECT_NAME/staging.db <mountpoint-from-above>/your.db
docker compose start PROJECT_NAME
```

## The three mistakes we actually hit (worth knowing before you repeat them)

1. **Volume mounted onto a file path, not a directory.** A named Docker
   volume IS a directory. Mounting one straight onto `/app/data.db` makes
   Docker create an empty *folder* there instead of a file -- the app then
   crashes trying to open a folder as a database. Fix: mount the volume on
   a directory (`/app/data`), point the app at a file inside it.

2. **Wrong gunicorn target.** `gunicorn module:name` imports `module` and
   looks for a top-level variable called `name`. If your web framework
   exposes the real server as an *attribute* (e.g. Flask/Dash's
   `app.server`), that's not what gunicorn expects -- you may need a tiny
   `wsgi.py` that imports the app, does any startup-only setup your
   `if __name__ == "__main__":` block does (many apps skip that block
   entirely when imported as a module, silently skipping real
   initialization), and exposes the correct object.

3. **Wrong docker inspect target.** If multiple reverse-proxy-looking
   containers exist (we had both Caddy and Traefik), check which one
   actually holds the ports (`ss -tlnp`) before assuming -- don't guess
   from the container name alone.

## Reference: full explainer + Rovix-specific values

See `DEPLOY_HOSTINGER.md` in the project root for the fully worked
example with real values, and why each step matters.
