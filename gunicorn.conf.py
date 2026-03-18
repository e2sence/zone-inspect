# gunicorn.conf.py — Production config for PCB Inspect
import multiprocessing
import os

# Server socket — only localhost, nginx proxies from outside
bind = f"127.0.0.1:{os.environ.get('PORT', '5001')}"

# Worker processes
# Single worker + threads: all threads share the same process memory,
# so in-memory dicts (sessions, mobile_tokens) are always consistent.
# PyTorch releases the GIL during inference → threads can serve other requests.
workers = 1
threads = 4
worker_class = "gthread"
timeout = 120  # ML inference can take time on large images
graceful_timeout = 30

# Security
limit_request_line = 8190
limit_request_fields = 100

# Logging
accesslog = None   # disable access log (too noisy)
errorlog = "-"    # stderr
loglevel = "warning"

# Process naming
proc_name = "pcb-inspect"

# Preload app to share model memory across workers (copy-on-write)
preload_app = True
