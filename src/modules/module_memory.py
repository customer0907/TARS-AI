"""
module_memory.py

Memory Management Module for TARS-AI.

Handles long-term and short-term memory. 
Ensures contextual and historical knowledge during interactions.
"""
# === Standard Libraries ===
import os
import re
import json
import requests
from typing import List
from datetime import datetime
from hyperdb import HyperDB
import numpy as np

# === Custom Modules ===
from modules.module_hyperdb import *
from modules.module_config import load_config
from modules.module_messageQue import queue_message

CONFIG = load_config()

# Casual chat / bot-opinion turns are not stored or retrieved — only user facts persist.
_EPHEMERAL_CHAT_PATTERNS = [
    r'너[가는]?',
    r'넌\b',
    r'니가\b',
    r'추천',
    r'알려\s*줘',
    r'말해\s*줘',
    r'생각해\s*봐',
    r'챔피언',
    r'골\s*챔',
    r'롤\b',
    r'리그\s*오브',
    r'게임\s*(좀|하나|추천|뭐|어때)',
    r'뭐\s*(가\s*)?(재밌|좋)',
    r'누구\s*좋아',
    r'다른\s*(사람|챔|거|건)',
    r'^왜\b',
    r'왜\s*\?',
    r'왜\s*좋',
    r'왜\s*그',
]

_USER_FACT_PATTERNS = [
    r'(?:^|[.\s])(?:나|내|저|우리)\s*(?:는|가|의)?\s*.+(?:좋아|싫어|선호|먹|마셔|기억)',
    r'기억해\s*줘',
    r'잊지\s*마',
    r'내\s+(?:이름|생일|나이|직업|취미|주소|번호|전화)',
]

_SESSION_CONTEXT_PATTERNS = [
    r'무슨\s*얘기',
    r'뭐\s*얘기',
    r'이야기\s*하고\s*있',
    r'방금\s*얘기',
    r'아까\s*얘기',
    r'지금\s*.*얘기',
]


class MemoryManager:
    """
    Handles memory operations (long-term and short-term) for TARS-AI.
    """
    def __init__(self, config, char_name, char_greeting):
        self.config = config
        self.char_name = char_name
        self.char_greeting = char_greeting
        self.memory_db_path = os.path.abspath(os.path.join(os.path.join("..", "memory"), f"{self.char_name}.pickle.gz"))
        
        # Load RAG configuration from dictionary
        rag_config = self.config.get('RAG', {})  # Get RAG section or empty dict if not exists
        self.rag_strategy = rag_config.get('strategy', 'naive')  # Default to 'naive' if not specified
        self.vector_weight = float(rag_config.get('vector_weight', 0.5))  # Default to 0.5 if not specified
        self.top_k = int(rag_config.get('top_k', 5))  # Default to 5 if not specified
        
        # Initialize HyperDB with the RAG strategy
        self.hyper_db = HyperDB(rag_strategy=self.rag_strategy)
        self.long_mem_use = True
        self.initial_memory_path =  os.path.abspath(os.path.join(os.path.join("..", "memory", "initial_memory.json")))
        
        self.session_turns = []
        self.init_dynamic_memory()
        self.load_initial_memory(self.initial_memory_path)

    def init_dynamic_memory(self):
        """
        Initialize dynamic memory from the database file.
        """
        if os.path.exists(self.memory_db_path):
            queue_message(f"LOAD: Found existing memory: {self.char_name}.pickle.gz")
            loaded_successfully = self.hyper_db.load(self.memory_db_path)
            if not loaded_successfully or self.hyper_db.vectors is None:
                queue_message(f"LOAD: Memory load failed. Initializing new memory.")
                self.hyper_db.vectors = np.empty((0, 0), dtype=np.float32)
            else:
                queue_message(f"LOAD: Memory loaded successfully")
        else:
            queue_message(f"LOAD: No memory DB found. Creating new one: {self.memory_db_path}")
            if self.char_greeting and self.char_greeting.strip():
                self.hyper_db.add_document({"text": f'{self.char_name}: {self.char_greeting}'})
            self.hyper_db.save(self.memory_db_path)

    def _is_ephemeral_chat(self, user_input: str) -> bool:
        text = (user_input or "").strip()
        if not text:
            return True
        for pattern in _EPHEMERAL_CHAT_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                return True
        return False

    def _is_user_fact(self, user_input: str) -> bool:
        text = (user_input or "").strip()
        if not text:
            return False
        user_name = self.config.get('CHAR', {}).get('user_name', '')
        if user_name and user_name in text and re.search(r'좋아|싫어|생일|이름|기억', text):
            return True
        for pattern in _USER_FACT_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                return True
        return False

    def _should_persist_memory(self, user_input: str, bot_response: str) -> bool:
        if self._is_ephemeral_chat(user_input):
            return False
        return self._is_user_fact(user_input)

    def _is_retrievable_document(self, document: dict) -> bool:
        user_input = document.get('user_input', '')
        if not user_input:
            return True
        return self._should_persist_memory(user_input, document.get('bot_response', ''))

    def _is_session_context_query(self, user_input: str) -> bool:
        text = (user_input or "").strip()
        if not text:
            return False
        for pattern in _SESSION_CONTEXT_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                return True
        return False

    def add_session_turn(self, user_input: str, bot_response: str):
        user_input = (user_input or "").strip()
        bot_response = (bot_response or "").strip()
        if not user_input or not bot_response:
            return
        self.session_turns.append((user_input, bot_response))

    def write_longterm_memory(self, user_input: str, bot_response: str):
        """
        Save user input and bot response to long-term memory.

        Parameters:
        - user_input (str): The user's input.
        - bot_response (str): The bot's response.
        """
        if not self._should_persist_memory(user_input, bot_response):
            return

        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        document = {
            "timestamp": current_time,
            "user_input": user_input,
            "bot_response": bot_response,
        }
        self.hyper_db.add_document(document)
        self.hyper_db.save(self.memory_db_path)

    def get_related_memories(self, query: str) -> str:
        """
        Retrieve memories related to a given query from the HyperDB.

        Parameters:
        - query (str): The input query.

        Returns:
        - str: Relevant memories or a fallback message.
        """
        try:
            results = self.hyper_db.query(
                query, 
                top_k=self.top_k, 
                return_similarities=False
            )
            
            if results:
                memory_list = self.hyper_db.dict()

                for memory in results:
                    if not self._is_retrievable_document(memory):
                        continue

                    start_index = next((i for i, d in enumerate(memory_list) if d['document'] == memory), None)
                    if start_index is None:
                        continue

                    prev_count = 1
                    post_count = 1
                    start = max(start_index - prev_count, 0)
                    end = min(start_index + post_count + 1, len(memory_list))

                    result = [
                        memory_list[i]['document']
                        for i in range(start, end)
                        if self._is_retrievable_document(memory_list[i]['document'])
                    ]
                    if result:
                        return result

                return f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] WARN: No user-fact memories found for the query."
            else:
                return f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] WARN: No memories found for the query."
        except Exception as e:
            #queue_message(f"ERROR: Error retrieving related memories: {e}")
            return "Error retrieving related memories."
    
    def get_longterm_memory(self, user_input: str) -> str:
        """
        Retrieve long-term memory relevant to a user input.

        Parameters:
        - user_input (str): The user input.

        Returns:
        - str: Relevant memory or a fallback message.
        """
        try:
            if self._is_session_context_query(user_input):
                return "No relevant memories found."
            if self.long_mem_use:
                # Fetch related memories
                past = self.get_related_memories(user_input)
                return str(past) if past else "No relevant memories found."
            return f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] WARN: Long-term memory is disabled."
        except Exception as e:
            queue_message(f"ERROR: Error retrieving long-term memory: {e}")
            return "Error retrieving long-term memory."

    def get_shortterm_memories_recent(self, max_entries: int) -> List[str]:
        """
        Retrieve the most recent short-term memories.

        Parameters:
        - max_entries (int): Number of recent memories to retrieve.

        Returns:
        - List[str]: List of recent memory documents.
        """
        # Get the memory dictionary
        memory_dict = self.hyper_db.dict()
        return [entry['document'] for entry in memory_dict[-max_entries:]]  # Retrieve the most recent entries
    
    def get_shortterm_memories_tokenlimit(self, token_limit: int) -> str:
        """
        Retrieve short-term memories constrained by a token limit.

        Parameters:
        - token_limit (int): Maximum token limit.

        Returns:
        - str: Concatenated memories formatted for output.
        """
        accumulated_documents = []
        accumulated_length = 0

        def try_add_turn(user_input, bot_response):
            nonlocal accumulated_length
            if not user_input or not bot_response:
                return False
            text_str = f"user_input: {user_input}\nbot_response: {bot_response}"
            text_length = self.token_count(text_str)['length']
            if accumulated_length + text_length > token_limit:
                return False
            accumulated_documents.append((user_input, bot_response))
            accumulated_length += text_length
            return True

        for user_input, bot_response in reversed(self.session_turns):
            if not try_add_turn(user_input, bot_response):
                break

        for entry in reversed(self.hyper_db.dict()):
            user_input = entry['document'].get('user_input', "")
            bot_response = entry['document'].get('bot_response', "")

            if not user_input or not bot_response:
                continue

            if not self._is_retrievable_document(entry['document']):
                continue

            if not try_add_turn(user_input, bot_response):
                break

        formatted_output = '\n'.join(
            [f"{{user}}: {ui}\n{{char}}: {br}" for ui, br in reversed(accumulated_documents)]
        )
        return formatted_output

    def write_tool_used(self, toolused: str):
        """
        Record the use of a tool in long-term memory.

        Parameters:
        - toolused (str): Description of the tool used.
        """
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        document = {
            "timestamp": current_time,
            "bot_response": toolused
        }
        self.hyper_db.add_document(document)
        self.hyper_db.save(self.memory_db_path)

    def load_initial_memory(self, json_file_path: str):
        """
        Load memories from a JSON file and inject them into the memory database.

        Parameters:
        - json_file_path (str): Path to the JSON file.
        """
        if os.path.exists(json_file_path):
            queue_message(f"LOAD: Injecting memories from JSON.")
            with open(json_file_path, 'r') as file:
                memories = json.load(file)

            for memory in memories:
                time = memory.get("time", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                user_input = memory.get("userinput", "")
                bot_response = memory.get("botresponse", "")
                self.write_longterm_memory(user_input, bot_response)

            os.rename(json_file_path, os.path.splitext(json_file_path)[0] + ".loaded")

    def token_count(self, text: str) -> dict:
        """
        Calculate the number of tokens in a given text.

        Parameters:
        - text (str): Input text.

        Returns:
        - dict: Dictionary with token count.
        """
        llm_backend = self.config['LLM']['llm_backend']
        # Cache for already logged fallback warnings
        if not hasattr(self, '_fallback_warning_logged'):
            self._fallback_warning_logged = False

        # Support both openai and deepinfra using tiktoken
        if llm_backend in ["openai", "deepinfra"]:
            try:
                import tiktoken
                override_encoding_model = self.config['LLM'].get('override_encoding_model', "cl100k_base")

                # for deepinfra's models, we can directly use override_encoding_model
                if llm_backend == "deepinfra":
                    enc = tiktoken.get_encoding(override_encoding_model)
                else:
                    # for openai, try model-specific encoding first
                    openai_model = self.config['LLM'].get('openai_model', None)
                    try:
                        enc = tiktoken.encoding_for_model(openai_model)
                    except KeyError:
                        if not self._fallback_warning_logged:
                            queue_message(f"INFO: Automatic mapping failed '{openai_model}'. Using '{override_encoding_model}'.")
                            self._fallback_warning_logged = True
                        enc = tiktoken.get_encoding(override_encoding_model)

                length = {"length": len(enc.encode(text))}
                return length

            except Exception as e:
                if not hasattr(self, '_token_error_logged'):
                    queue_message(f"ERROR: Failed to calculate tokens using tiktoken: {e}")
                    self._token_error_logged = True
                return {"length": 0}

        elif llm_backend in ["ooba", "tabby"]:
            # Handle token counting for other backends via API
            url = f"{self.config['LLM']['base_url']}/v1/internal/token-count" if llm_backend == "ooba" else f"{self.config['LLM']['base_url']}/v1/token/encode"
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.config['LLM']['api_key']}"
            }
            data = {
                "text": text
            }

            try:
                response = requests.post(url, headers=headers, json=data)
                response.raise_for_status()
                return response.json()
            except requests.exceptions.RequestException as e:
                queue_message(f"ERROR: Request to {llm_backend} token count API failed: {e}")
                return {"length": 0}

        else:
            queue_message(f"ERROR: Unsupported LLM backend: {llm_backend}")
            return {"length": 0}