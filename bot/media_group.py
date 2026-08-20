import asyncio
import inspect
import logging
from typing import Callable, Any
from collections import OrderedDict


class MediaGroupCollector:
    """
    Collects asynchronous Telegram media group (album) updates with debouncing.
    """
    def __init__(self, debounce_seconds: float = 0.8, on_complete: Callable[[dict[str, Any]], Any] | None = None, max_cache_size: int = 50):
        self.debounce_seconds = debounce_seconds
        self.on_complete = on_complete
        self.active_groups: dict[str, dict[str, Any]] = {}
        self.history_cache: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.max_cache_size = max_cache_size

    async def add_update(self, update: Any):
        """
        Registers an update belonging to a media group.
        """
        message = getattr(update, 'message', None) or getattr(update, 'effective_message', None)
        if not message:
            return

        media_group_id = getattr(message, 'media_group_id', None)
        if not media_group_id:
            return

        caption = getattr(message, 'caption', None)

        if media_group_id not in self.active_groups:
            self.active_groups[media_group_id] = {
                'media_group_id': media_group_id,
                'updates': [update],
                'messages': [message],
                'caption': caption,
                'primary_message': message,
                'task': None
            }
        else:
            group = self.active_groups[media_group_id]
            group['updates'].append(update)
            group['messages'].append(message)
            if caption and not group['caption']:
                group['caption'] = caption
                group['primary_message'] = message

        group = self.active_groups[media_group_id]
        if group['task'] and not group['task'].done():
            group['task'].cancel()

        group['task'] = asyncio.create_task(self._debounce_and_fire(media_group_id))

    async def _debounce_and_fire(self, media_group_id: str):
        try:
            await asyncio.sleep(self.debounce_seconds)
        except asyncio.CancelledError:
            return

        group = self.active_groups.pop(media_group_id, None)
        if not group:
            return

        # Store in LRU history cache
        self.history_cache[media_group_id] = group
        self.history_cache.move_to_end(media_group_id)
        if len(self.history_cache) > self.max_cache_size:
            self.history_cache.popitem(last=False)

        if self.on_complete:
            try:
                res = self.on_complete(group)
                if inspect.isawaitable(res):
                    await res
            except Exception as e:
                logging.exception(f"Error handling completed media group {media_group_id}: {e}")

    def get_cached_group(self, media_group_id: str) -> dict[str, Any] | None:
        """
        Retrieves a recently processed media group by its media_group_id.
        """
        if media_group_id in self.history_cache:
            self.history_cache.move_to_end(media_group_id)
            return self.history_cache[media_group_id]
        return None
