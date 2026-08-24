"""HTTP control-plane surfaces for SyndCrawler server deployments."""

from syndcrawler.control_plane.api import create_app, create_app_from_env

__all__ = ["create_app", "create_app_from_env"]
