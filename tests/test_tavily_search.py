import os
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch

ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
BOT_DIR = os.path.join(ROOT_DIR, "bot")
if BOT_DIR not in sys.path:
    sys.path.insert(0, BOT_DIR)

if "telegram" not in sys.modules:
    telegram_module = types.ModuleType("telegram")
    telegram_module.Message = object
    telegram_module.MessageEntity = object
    telegram_module.Update = object
    telegram_module.ChatMember = types.SimpleNamespace(OWNER="owner", ADMINISTRATOR="administrator", MEMBER="member")
    telegram_module.constants = types.SimpleNamespace(
        ChatType=types.SimpleNamespace(GROUP="group", SUPERGROUP="supergroup"),
        ParseMode=types.SimpleNamespace(MARKDOWN="Markdown"),
    )
    telegram_module.error = types.SimpleNamespace(BadRequest=Exception)
    sys.modules["telegram"] = telegram_module

if "telegram.ext" not in sys.modules:
    telegram_ext_module = types.ModuleType("telegram.ext")
    telegram_ext_module.CallbackContext = object
    telegram_ext_module.ContextTypes = types.SimpleNamespace(DEFAULT_TYPE=object)
    sys.modules["telegram.ext"] = telegram_ext_module

from plugins.tavily_search import TavilySearchPlugin
from utils import handle_direct_result


class TavilySearchPluginTests(unittest.TestCase):
    def test_format_response_limits_sources_and_can_hide_them(self):
        response = {
            "query": "latest Gemini release",
            "answer": "Google announced Gemini 2.5 Flash Lite.",
            "results": [
                {
                    "title": "Google Blog",
                    "url": "https://blog.google/products/gemini/",
                    "content": "Official announcement.",
                    "score": 0.99,
                },
                {
                    "title": "The Verge",
                    "url": "https://www.theverge.com/",
                    "content": "Coverage from The Verge.",
                    "score": 0.86,
                },
                {
                    "title": "Wikipedia",
                    "url": "https://en.wikipedia.org/",
                    "content": "Background info.",
                    "score": 0.30,
                },
            ],
        }

        with patch.dict(os.environ, {
            "TAVILY_SHOW_SOURCES": "true",
            "TAVILY_MAX_SOURCES": "2",
        }, clear=False):
            plugin = TavilySearchPlugin()
            formatted = plugin.format_response(response)

        self.assertIn("Tavily 搜索", formatted)
        self.assertIn("Google Blog", formatted)
        self.assertIn("The Verge", formatted)
        self.assertNotIn("Wikipedia", formatted)
        self.assertIn("来源", formatted)
        self.assertIn("blog.google", formatted)

        with patch.dict(os.environ, {
            "TAVILY_SHOW_SOURCES": "false",
            "TAVILY_MAX_SOURCES": "2",
        }, clear=False):
            plugin = TavilySearchPlugin()
            hidden = plugin.format_response(response)

        self.assertIn("Google announced Gemini 2.5 Flash Lite.", hidden)
        self.assertNotIn("来源", hidden)
        self.assertNotIn("Google Blog", hidden)


class DirectResultTextTests(unittest.IsolatedAsyncioTestCase):
    async def test_handle_direct_result_text_sends_telegram_message(self):
        reply_text = AsyncMock()
        update = type("Update", (), {})()
        message = type("Message", (), {})()
        message.chat_id = 123
        message.message_id = 456
        message.is_topic_message = False
        message.reply_text = reply_text
        update.effective_message = message
        update.message = message
        update.effective_chat = None

        await handle_direct_result(
            {"enable_quoting": False},
            update,
            {
                "direct_result": {
                    "kind": "text",
                    "format": "markdown",
                    "value": "hello **world**",
                }
            },
        )

        reply_text.assert_awaited_once()
        kwargs = reply_text.await_args.kwargs
        self.assertEqual(kwargs["text"], "hello **world**")


if __name__ == "__main__":
    unittest.main()
