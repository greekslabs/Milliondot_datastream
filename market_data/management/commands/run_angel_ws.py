import threading
import time
from django.core.management.base import BaseCommand

from api.v1.market_data.utils import start_service
from api.v1.market_data.utils2 import start_service2
from api.v1.market_data.utils3 import start_service3



class Command(BaseCommand):
    help = "Run Angel WebSocket batches"

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS("Starting Angel WS batch 1 & 2 & 3..."))

        threading.Thread(
            target=start_service,
            daemon=True
        ).start()

        time.sleep(8)

        threading.Thread(
            target=start_service2,
            daemon=True
        ).start()
       
        time.sleep(8)
          
        threading.Thread(
            target=start_service3,
            daemon=True
        ).start()

        # keep process alive
        while True:
            time.sleep(1)