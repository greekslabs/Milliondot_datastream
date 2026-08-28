"""
ASGI config for datastream project.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/6.0/howto/deployment/asgi/
"""

import os
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.auth import AuthMiddlewareStack
from django.core.asgi import get_asgi_application
from datastream.routing import websocket_urlpatterns  

# Set Django settings
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "datastream.settings")
os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "true"  

# ASGI application
application = ProtocolTypeRouter({
    "http": get_asgi_application(),  
    "websocket": AuthMiddlewareStack(
        URLRouter(
            websocket_urlpatterns  
        )
    ),
})
