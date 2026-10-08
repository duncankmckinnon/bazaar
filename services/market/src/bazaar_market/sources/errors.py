class SourceError(Exception):
    """A source refused or failed a request. Fetches stop here and never fall back to other data."""
