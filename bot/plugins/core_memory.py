import os
import json
from typing import Dict, List

from .plugin import Plugin

class CoreMemoryPlugin(Plugin):
    """
    A plugin to handle core memory/personalization facts for users.
    """

    def __init__(self):
        self.memory_file = os.path.join(os.path.dirname(__file__), '..', '..', 'user_memories.json')
        self._load_memory()

    def _load_memory(self):
        if os.path.exists(self.memory_file):
            try:
                with open(self.memory_file, 'r', encoding='utf-8') as f:
                    self.memory = json.load(f)
            except Exception:
                self.memory = {}
        else:
            self.memory = {}

    def _save_memory(self):
        try:
            temp_file = self.memory_file + '.tmp'
            with open(temp_file, 'w', encoding='utf-8') as f:
                json.dump(self.memory, f, ensure_ascii=False, indent=4)
            os.replace(temp_file, self.memory_file)
        except Exception as e:
            print(f"Error saving core memory: {e}")

    def get_source_name(self) -> str:
        return "CoreMemory"

    def get_spec(self) -> List[Dict]:
        return [
            {
                "name": "core_memory_append",
                "description": "Append a new fact about the user to their core memory. Use this when the user reveals a preference, fact, or instruction you should remember across sessions.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "fact": {
                            "type": "string",
                            "description": "The specific fact or preference to remember. Should be concise and written from the bot's perspective, e.g. 'User prefers Python over Java', 'User lives in New York'."
                        }
                    },
                    "required": ["fact"]
                }
            },
            {
                "name": "core_memory_replace",
                "description": "Replace an existing core memory fact with a new one. Use this when the user corrects you or changes a preference.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "old_fact": {
                            "type": "string",
                            "description": "The exact wording of the old fact that should be removed."
                        },
                        "new_fact": {
                            "type": "string",
                            "description": "The new updated fact."
                        }
                    },
                    "required": ["old_fact", "new_fact"]
                }
            }
        ]

    def get_user_memory(self, chat_id: int) -> str:
        chat_id_str = str(chat_id)
        if chat_id_str in self.memory and self.memory[chat_id_str]:
            facts = self.memory[chat_id_str]
            return "\n".join([f"- {fact}" for fact in facts])
        return ""

    async def execute(self, function_name, helper, **kwargs) -> Dict:
        chat_id = kwargs.get('chat_id')
        if chat_id is None:
            return {"error": "chat_id is required for core memory"}
            
        chat_id_str = str(chat_id)
        if chat_id_str not in self.memory:
            self.memory[chat_id_str] = []

        if function_name == "core_memory_append":
            fact = kwargs.get('fact')
            if not fact:
                return {"error": "Missing 'fact' parameter"}
            
            if fact not in self.memory[chat_id_str]:
                self.memory[chat_id_str].append(fact)
                self._save_memory()
                return {"status": "success", "message": f"Fact appended: {fact}"}
            else:
                return {"status": "success", "message": "Fact already exists in memory"}

        elif function_name == "core_memory_replace":
            old_fact = kwargs.get('old_fact')
            new_fact = kwargs.get('new_fact')
            if not old_fact or not new_fact:
                return {"error": "Missing 'old_fact' or 'new_fact' parameter"}
            
            if old_fact in self.memory[chat_id_str]:
                idx = self.memory[chat_id_str].index(old_fact)
                self.memory[chat_id_str][idx] = new_fact
                self._save_memory()
                return {"status": "success", "message": f"Replaced '{old_fact}' with '{new_fact}'"}
            else:
                # If old fact not exactly matched, just append new
                self.memory[chat_id_str].append(new_fact)
                self._save_memory()
                return {"status": "success", "message": f"Old fact not found, but appended new fact: {new_fact}"}
        
        return {"error": f"Unknown function {function_name}"}
