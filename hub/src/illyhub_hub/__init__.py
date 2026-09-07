"""illyHub hub service: unified HEOS + Sonos + Denon control on the LAN."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("illyhub-hub")
except PackageNotFoundError:  # pragma: no cover - only when running from a raw checkout
    __version__ = "0.0.0-dev"
