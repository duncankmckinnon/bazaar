import os

# The library opens spans but never configures Logfire; only capfire tests configure it.
os.environ.setdefault("LOGFIRE_IGNORE_NO_CONFIG", "1")
