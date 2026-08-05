import os
import random
from itertools import islice
from typing import Dict

from duckduckgo_search import DDGS

from .plugin import Plugin


class WebImageEmbedPlugin(Plugin):
    """
    A plugin to search for an image on the web and return its URL for embedding in markdown.
    """
    def __init__(self):
        self.safesearch = os.getenv('DUCKDUCKGO_SAFESEARCH', 'moderate')

    def get_source_name(self) -> str:
        return "Web Image Embed"

    def get_spec(self) -> [Dict]:
        return [{
            "name": "embed_web_image",
            "description": "Search the web for an image (e.g. photos, illustrations) and get its URL. You must use the returned URL to insert the image into your conversational response using Markdown link syntax: [Image description](URL)",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The search query for the image"},
                    "type": {
                        "type": "string",
                        "enum": ["photo", "gif"],
                        "description": "The type of image to search for. Default to `photo` if not specified",
                    }
                },
                "required": ["query"],
            },
        }]

    async def execute(self, function_name, helper, **kwargs) -> Dict:
        with DDGS() as ddgs:
            image_type = kwargs.get('type', 'photo')
            ddgs_images_gen = ddgs.images(
                kwargs['query'],
                safesearch=self.safesearch,
                type_image=image_type,
            )
            results = list(islice(ddgs_images_gen, 10))
            if not results or len(results) == 0:
                return {"result": "No image found for the query."}

            # Shuffle the results to avoid always returning the same image
            random.shuffle(results)
            image_url = results[0]['image']
            
            return {
                "result": f"Image found successfully. URL: {image_url} . Please embed this URL directly into your response using Markdown format: [Image description]({image_url})"
            }
