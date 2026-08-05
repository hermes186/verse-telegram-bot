import os
import json
import logging
from typing import Dict, List

from mem0 import Memory
from .plugin import Plugin

class Mem0MemoryPlugin(Plugin):
    """
    A plugin to handle core memory/personalization facts using Mem0.
    """

    def __init__(self):
        db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'mem0_db')
        os.makedirs(db_path, exist_ok=True)
        base_url = os.getenv("OPENAI_BASE_URL")
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
                    "model": os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
                    "openai_base_url": base_url,
                }
            },
            "embedder": {
                "provider": "openai",
                "config": {
                    "model": "jinaai/jina-embeddings-v2-base-en",
                    "openai_base_url": base_url,
                }
            }
        }
        self.memory = Memory.from_config(config)

    def get_source_name(self) -> str:
        return "Mem0Memory"

    def get_spec(self) -> List[Dict]:
        return [
            {
                "name": "save_memory",
                "description": "Save important information, facts, or preferences about the user into long-term memory. Use this when the user reveals a preference, fact, or instruction you should remember across sessions.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "fact": {
                            "type": "string",
                            "description": "The specific fact or preference to remember. E.g. 'User prefers Python over Java', 'User lives in New York'."
                        }
                    },
                    "required": ["fact"]
                }
            },
            {
                "name": "search_memory",
                "description": "Search the user's long-term memory for relevant past information or preferences. Use this when you need to recall something the user might have told you previously.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "The question or topic to search for in the user's memory."
                        }
                    },
                    "required": ["query"]
                }
            }
        ]

    def get_user_memory(self, chat_id: int) -> str:
        """
        Retrieves recent or all core memories for a user to inject into system prompt.
        """
        try:
            chat_id_str = str(chat_id)
            memories = self.memory.get_all(filters={'user_id': chat_id_str})
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

    async def execute(self, function_name, helper, **kwargs) -> Dict:
        chat_id = kwargs.get('chat_id')
        if chat_id is None:
            return {"error": "chat_id is required for memory operations"}
            
        chat_id_str = str(chat_id)

        if function_name == "save_memory":
            fact = kwargs.get('fact')
            if not fact:
                return {"error": "Missing 'fact' parameter"}
            
            try:
                self.memory.add(fact, user_id=chat_id_str)
                return {"status": "success", "message": f"Fact saved to long-term memory successfully."}
            except Exception as e:
                logging.error(f"Error saving to mem0: {e}")
                return {"error": f"Failed to save memory: {str(e)}"}

        elif function_name == "search_memory":
            query = kwargs.get('query')
            if not query:
                return {"error": "Missing 'query' parameter"}
            
            try:
                results = self.memory.search(query, filters={'user_id': chat_id_str})
                if isinstance(results, dict) and 'memories' in results:
                    results = results['memories']
                elif not isinstance(results, list):
                    results = []
                
                facts = []
                for m in results:
                    if isinstance(m, dict) and 'memory' in m:
                        facts.append(m['memory'])
                    elif isinstance(m, str):
                        facts.append(m)
                
                if facts:
                    return {"status": "success", "memories": facts}
                else:
                    return {"status": "success", "message": "No relevant memories found."}
            except Exception as e:
                logging.error(f"Error searching mem0: {e}")
                return {"error": f"Failed to search memory: {str(e)}"}
        
        return {"error": f"Unknown function {function_name}"}
