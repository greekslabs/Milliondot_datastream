from django.urls import path
from api.v1.market_data.consumers import StockLTPConsumer

websocket_urlpatterns = [
    path("ws/ltp/", StockLTPConsumer.as_asgi()),
]
