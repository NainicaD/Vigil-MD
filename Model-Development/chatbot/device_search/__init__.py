"""Device search shared by the website and the chatbot (SQLite index built from BigQuery)."""
from .search import DeviceIndex, Hit, Resolution

__all__ = ["DeviceIndex", "Hit", "Resolution"]
