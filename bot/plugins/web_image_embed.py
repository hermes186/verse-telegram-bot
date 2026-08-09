import os
import random
import asyncio
import functools
import requests
from typing import Dict

from .plugin import Plugin


class WebImageEmbedPlugin(Plugin):
    """
    A plugin to search for an image on the web and return its URL for embedding in markdown.
    Now using Tavily as the backend instead of DuckDuckGo.
    """
    def __init__(self):
        self.api_key = os.getenv('TAVILY_API_KEY', '')
        self.base_url = os.getenv('TAVILY_BASE_URL', 'https://api.tavily.com')

    def get_source_name(self) -> str:
        return "Web Image Embed (Tavily)"

    def get_spec(self) -> [Dict]:
        return [{
            "name": "embed_web_image",
            "description": "Search the web for an image (e.g. photos, illustrations) and get its URL. You must use the returned URL to insert the image into your conversational response using Markdown link syntax: [Image description](URL)",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The search query for the image"}
                },
                "required": ["query"],
            },
        }]

    async def execute(self, function_name, helper, **kwargs) -> Dict:
        if not self.api_key:
            return {"error": "TAVILY_API_KEY is not configured"}

        query = kwargs.get("query")
        if not query:
            return {"error": "Missing required parameter 'query'"}

        payload = {
            "query": query,
            "search_depth": "basic",
            "include_images": True,
            "max_results": 1,
            "include_answer": False
        }

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        if os.getenv('TAVILY_PROJECT_ID'):
            headers["X-Project-ID"] = os.getenv('TAVILY_PROJECT_ID')

        try:
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                functools.partial(
                    requests.post,
                    f"{self.base_url.rstrip('/')}/search",
                    json=payload,
                    headers=headers,
                    timeout=30,
                )
            )
            response.raise_for_status()
            data = response.json()

            images = data.get("images", [])
            if not images or len(images) == 0:
                return {"result": "No image found for the query."}

            # Shuffle the results to avoid always returning the same image
            random.shuffle(images)
            image_url = images[0]
            
            return {
                "result": f"Image found successfully. URL: {image_url} . Please embed this URL directly into your response using Markdown format: [Image description]({image_url})"
            }
        except Exception as e:
            return {"error": f"Tavily image search failed: {str(e)}"}
