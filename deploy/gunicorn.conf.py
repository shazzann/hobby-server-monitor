# Gunicorn settings for hsm-api. One process with a few threads: the API does
# short SQLite reads/writes and never waits on LXD, so more processes would
# mostly cost memory.
import os

bind = f"{os.environ.get('BACKEND_HOST', '127.0.0.1')}:{os.environ.get('BACKEND_PORT', '8000')}"
workers = 1
worker_class = "gthread"
threads = 4
timeout = 30
graceful_timeout = 10
max_requests = 0
accesslog = None          # request paths are logged by the reverse proxy if needed; no query strings here
errorlog = "-"
loglevel = "info"
umask = 0o007             # SQLite WAL/SHM files must stay group-writable for the hsm group
# Gunicorn >= 25.1 opens a runtime control socket in $XDG_RUNTIME_DIR or ~/.gunicorn by default.
# The hsm-api service user has neither, and we do not want an extra local control surface.
control_socket_disable = True
