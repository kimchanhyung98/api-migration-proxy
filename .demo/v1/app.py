"""v1 합성 백엔드의 ASGI 진입점."""

from backend import create_app

app = create_app("v1")
