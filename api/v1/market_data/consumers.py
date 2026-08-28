import json
import asyncio

from channels.generic.websocket import AsyncWebsocketConsumer
from django.core.cache import cache
from asgiref.sync import sync_to_async


class StockLTPConsumer(AsyncWebsocketConsumer):

    async def connect(self):
        await self.accept()

        self.running = True

        await self.send(
            text_data=json.dumps({
                "status": "connected"
            })
        )

        self.task = asyncio.create_task(
            self.stream_ltp()
        )

    async def disconnect(self, close_code):
        self.running = False

        if hasattr(self, "task"):
            self.task.cancel()

            try:
                await self.task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass

    async def get_tokens(self):
        """
        Run all Redis operations in threadpool.
        """

        redis_client = await sync_to_async(
            cache.client.get_client
        )()

        tokens = set()

        try:
            # ============================
            # WATCHLIST TOKENS
            # ============================
            active_tokens = await sync_to_async(
                redis_client.smembers
            )("active_tokens")

            tokens.update(
                t.decode() if isinstance(t, bytes) else str(t)
                for t in active_tokens
            )

            # ============================
            # RMS / POSITIONS TOKENS
            # ============================
            scan_keys = await sync_to_async(
                lambda: list(
                    redis_client.scan_iter("token_usage:*")
                )
            )()

            for key in scan_keys:
                key_str = (
                    key.decode()
                    if isinstance(key, bytes)
                    else str(key)
                )

                parts = key_str.split(":")

                if len(parts) >= 2:
                    tokens.add(parts[1])

        except Exception as e:
            print("TOKEN FETCH ERROR:", e)

        return tokens

    async def build_payload(self, tokens):
        payload = []

        try:
            for token in tokens:

                value = await sync_to_async(
                    cache.get
                )(f"stock:{token}:data")

                if not value:
                    continue

                try:
                    payload.append(
                        json.loads(value)
                    )
                except Exception:
                    continue

        except Exception as e:
            print("PAYLOAD BUILD ERROR:", e)

        return payload

    async def stream_ltp(self):

        try:

            while self.running:

                # ============================
                # GET TOKENS
                # ============================
                tokens = await self.get_tokens()

                # ============================
                # BUILD PAYLOAD
                # ============================
                payload = await self.build_payload(
                    tokens
                )

                # ============================
                # SEND DATA
                # ============================
                if payload:

                    try:
                        await self.send(
                            text_data=json.dumps({
                                "type": "ltp_update",
                                "data": payload
                            })
                        )
                    except Exception:
                        break

                try:
                    await asyncio.sleep(1)
                except asyncio.CancelledError:
                    break

        except asyncio.CancelledError:
            return

        except Exception as e:
            print("LTP STREAM ERROR:", e)
            return

