import pytest
from unittest.mock import AsyncMock, patch, MagicMock
import asyncio

from bot.plugins.mem0_memory import Mem0MemoryPlugin

@pytest.fixture
def memory_plugin():
    with patch('bot.plugins.mem0_memory.Memory.from_config') as mock_memory:
        plugin = Mem0MemoryPlugin()
        plugin.memory = MagicMock()
        plugin.memory.add = MagicMock()
        return plugin

@pytest.mark.asyncio
async def test_add_memory_async_filters_short_messages(memory_plugin):
    # Test short message
    messages = [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "你好！有什么我可以帮你的？"}
    ]
    await memory_plugin.add_memory_async(12345, messages)
    memory_plugin.memory.add.assert_not_called()

@pytest.mark.asyncio
async def test_add_memory_async_filters_commands(memory_plugin):
    # Test command message
    messages = [
        {"role": "user", "content": "/reset"},
        {"role": "assistant", "content": "Memory reset."}
    ]
    await memory_plugin.add_memory_async(12345, messages)
    memory_plugin.memory.add.assert_not_called()

@pytest.mark.asyncio
@patch('asyncio.to_thread')
async def test_add_memory_async_adds_long_messages(mock_to_thread, memory_plugin):
    # Make to_thread execute the function synchronously
    async def mock_execute(func, *args, **kwargs):
        return func(*args, **kwargs)
    mock_to_thread.side_effect = mock_execute

    # Test valid message
    messages = [
        {"role": "user", "content": "我非常喜欢在周末去公园打篮球，记得下次提醒我带水。"},
        {"role": "assistant", "content": "没问题，周末我会提醒您带水的。"}
    ]
    await memory_plugin.add_memory_async(12345, messages)
    
    # Assert that it was called with the correct parameters
    memory_plugin.memory.add.assert_called_once_with(messages, user_id="12345")
