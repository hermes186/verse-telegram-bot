import os
import json
import logging
from typing import Dict, List

try:
    from mem0 import Memory
except ImportError:
    Memory = None

from .plugin import Plugin

class Mem0MemoryPlugin(Plugin):
    """
    A plugin to handle core memory/personalization facts using Mem0.
    """

    def __init__(self):
        db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'mem0_db')
        os.makedirs(db_path, exist_ok=True)
        
        mem0_api_key = os.getenv("MEM0_API_KEY") or os.getenv("OPENAI_API_KEY")
        mem0_model = os.getenv("MEM0_MODEL") or os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        mem0_base_url = os.getenv("MEM0_BASE_URL") or os.getenv("OPENAI_BASE_URL")
        
        config = {
            "vector_store": {
                "provider": "qdrant",
                "config": {
                    "path": db_path,
                }
            },
            "llm": {
                "provider": "openai",
                "config": {
                    "model": mem0_model,
                    "openai_base_url": mem0_base_url,
                    "api_key": mem0_api_key,
                }
            }
        }
        
        # Embedder must use global OPENAI settings, not MEM0 settings (which might be OpenRouter)
        openai_api_key = os.getenv("OPENAI_API_KEY")
        openai_base_url = os.getenv("OPENAI_BASE_URL")
        
        config["embedder"] = {
            "provider": "openai",
            "config": {
                "model": "jinaai/jina-embeddings-v2-base-en",
            }
        }
        if openai_base_url:
            config["embedder"]["config"]["openai_base_url"] = openai_base_url
        if openai_api_key:
            config["embedder"]["config"]["api_key"] = openai_api_key

        self.memory = Memory.from_config(config)

    def get_source_name(self) -> str:
        return "Mem0Memory"

    def get_spec(self) -> List[Dict]:
        return []

    def get_user_memory(self, chat_id: int) -> str:
        """
        Retrieves recent or all core memories for a user to inject into system prompt.
        """
        try:
            chat_id_str = str(chat_id)
            memories = self.memory.get_all(user_id=chat_id_str)
            if not memories:
                return ""
                
            # Format depends on Mem0 version
            if isinstance(memories, dict) and 'memories' in memories:
                memories_list = memories['memories']
            elif isinstance(memories, list):
                memories_list = memories
            else:
                memories_list = []

            facts = []
            for m in memories_list:
                if isinstance(m, dict) and 'memory' in m:
                    facts.append(m['memory'])
                elif isinstance(m, str):
                    facts.append(m)
            
            if facts:
                return "\n".join([f"- {fact}" for fact in facts])
        except Exception as e:
            logging.error(f"Error fetching mem0 memories: {e}")
            
        return ""

    async def add_memory_async(self, chat_id: int, messages: list):
        """
        Non-blocking background memory extraction.
        Filters out short messages or commands.
        """
        if not messages:
            return

        # Check if the user message is substantial enough to warrant memory extraction
        # Assuming messages format: [{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}]
        user_messages = [m['content'] for m in messages if m.get('role') == 'user']
        if not user_messages:
            return
            
        last_user_msg = user_messages[-1].strip()
        
        # Filter commands
        if last_user_msg.startswith('/'):
            return
            
        # Filter very short messages that usually don't contain facts
        if len(last_user_msg) < 6 and last_user_msg.lower() not in ['yes', 'no', 'ok', 'okay']:
            # But what if they say "我20岁"? It's 4 chars.
            # Let's use a very small length check or just rely on mem0 to drop it.
            if len(last_user_msg) < 3:
                return
        
        chat_id_str = str(chat_id)
        try:
            import asyncio
            # Use to_thread to run synchronous Mem0 SDK in background
            await asyncio.to_thread(self.memory.add, messages, user_id=chat_id_str)
        except Exception as e:
            logging.error(f"Error in background mem0 extraction: {e}")

    async def execute(self, function_name, helper, **kwargs) -> Dict:
        return {}
