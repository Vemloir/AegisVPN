import os

# Let modules that import src.config (mainbot, deps, handlers, main) load under
# pytest without a real environment.
os.environ.setdefault("SUPPORT_BOT_TOKEN", "test:token")
os.environ.setdefault("ADMIN_IDS", "[1]")
