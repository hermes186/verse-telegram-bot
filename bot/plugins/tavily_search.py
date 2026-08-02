import os
from typing import Dict

import requests

from .plugin import Plugin


class TavilySearchPlugin(Plugin):
    """
    A plugin to search the web using Tavily.
    """

    def __init__(self):
        self.api_key = os.getenv('TAVILY_API_KEY', '')
        self.base_url = os.getenv('TAVILY_BASE_URL', 'https://api.tavily.com')
        self.show_sources = os.getenv('TAVILY_SHOW_SOURCES', 'true').lower() == 'true'
        self.max_sources = int(os.getenv('TAVILY_MAX_SOURCES', '3'))

    def get_source_name(self) -> str:
        return "Tavily"

    def get_spec(self) -> [Dict]:
        return [{
            "name": "tavily_search",
            "description": "Search the web with Tavily and return ranked results with optional AI answer.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query."
                    },
                    "search_depth": {
                        "type": "string",
                        "enum": ["basic", "advanced"],
                        "description": "Search depth. Use basic for faster/cheaper searches and advanced for deeper results.",
                    },
                    "topic": {
                        "type": "string",
                        "enum": ["general", "news"],
                        "description": "Use news for current events and general for everything else.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of results to return. Tavily supports up to 20.",
                    },
                    "include_answer": {
                        "type": "boolean",
                        "description": "Whether to include Tavily's generated answer in the response.",
                    },
                    "include_raw_content": {
                        "type": "boolean",
                        "description": "Whether to include extracted page content in the response.",
                    },
                    "include_images": {
                        "type": "boolean",
                        "description": "Whether to include image results when available.",
                    },
                    "include_favicon": {
                        "type": "boolean",
                        "description": "Whether to include favicons in results.",
                    },
                    "include_domains": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "A list of domains to include.",
                    },
                    "exclude_domains": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "A list of domains to exclude.",
                    },
                    "country": {
                        "type": "string",
                        "description": "Country code used to localize search results, if supported.",
                    },
                    "auto_parameters": {
                        "type": "boolean",
                        "description": "Let Tavily infer search parameters automatically.",
                    },
                    "exact_match": {
                        "type": "boolean",
                        "description": "Only return results containing the exact quoted phrase(s).",
                    },
                    "include_usage": {
                        "type": "boolean",
                        "description": "Whether to include Tavily credit usage in the response.",
                    },
                    "safe_search": {
                        "type": "boolean",
                        "description": "Whether to enable safe search.",
                    },
                },
                "required": ["query"],
            },
        }]

    async def execute(self, function_name, helper, **kwargs) -> Dict:
        if not self.api_key:
            return {"error": "TAVILY_API_KEY is not configured"}

        payload = {
            "query": kwargs["query"],
            "search_depth": kwargs.get("search_depth", "basic"),
            "topic": kwargs.get("topic", "general"),
            "max_results": kwargs.get("max_results", 5),
            "include_answer": kwargs.get("include_answer", True),
            "include_raw_content": kwargs.get("include_raw_content", False),
            "include_images": kwargs.get("include_images", False),
            "include_favicon": kwargs.get("include_favicon", False),
            "include_domains": kwargs.get("include_domains", []),
            "exclude_domains": kwargs.get("exclude_domains", []),
            "country": kwargs.get("country"),
            "auto_parameters": kwargs.get("auto_parameters", False),
            "exact_match": kwargs.get("exact_match", False),
            "include_usage": kwargs.get("include_usage", False),
            "safe_search": kwargs.get("safe_search", False),
        }

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        if os.getenv('TAVILY_PROJECT_ID'):
            headers["X-Project-ID"] = os.getenv('TAVILY_PROJECT_ID')

        try:
            response = requests.post(
                f"{self.base_url.rstrip('/')}/search",
                json=payload,
                headers=headers,
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()

            results = []
            for item in data.get("results", []):
                results.append({
                    "title": item.get("title"),
                    "url": item.get("url"),
                    "content": item.get("content"),
                    "score": item.get("score"),
                    "favicon": item.get("favicon"),
                })

            output = {
                "query": data.get("query", kwargs["query"]),
                "results": results,
            }

            if data.get("answer"):
                output["answer"] = data["answer"]
            if data.get("response_time"):
                output["response_time"] = data["response_time"]
            if data.get("usage"):
                output["usage"] = data["usage"]

            return {
                "direct_result": {
                    "kind": "text",
                    "format": "plain",
                    "value": self.format_response(output),
                }
            }
        except requests.HTTPError:
            try:
                detail = response.json()
            except Exception:
                detail = response.text
            return {"error": f"Tavily request failed: {detail}"}
        except Exception as e:
            return {"error": f"Tavily request failed: {str(e)}"}

    def format_response(self, data: Dict) -> str:
        lines = ["🔎 Tavily 搜索"]
        query = data.get("query")
        if query:
            lines.append(f"查询: {query}")

        answer = data.get("answer")
        if answer:
            lines.append("")
            lines.append(f"回答: {answer}")

        if self.show_sources:
            results = data.get("results", []) or []
            display_results = results[: self.max_sources] if self.max_sources > 0 else results
            if len(display_results) > 0:
                lines.append("")
                lines.append("结果:")
                for index, item in enumerate(display_results, start=1):
                    title = (item.get("title") or "未命名来源").strip()
                    url = (item.get("url") or "").strip()
                    content = (item.get("content") or "").strip()
                    if content:
                        lines.append(f"{index}. {title}")
                        if url:
                            lines.append(f"   {url}")
                        lines.append(f"   {content}")
                    else:
                        if url:
                            lines.append(f"{index}. {title} - {url}")
                        else:
                            lines.append(f"{index}. {title}")

            if len(display_results) > 0:
                lines.append("")
                lines.append("来源:")
                for index, item in enumerate(display_results, start=1):
                    title = (item.get("title") or "未命名来源").strip()
                    url = (item.get("url") or "").strip()
                    if url:
                        lines.append(f"{index}. {title} - {url}")
                    else:
                        lines.append(f"{index}. {title}")
        else:
            results = data.get("results", []) or []
            if len(results) > 0:
                lines.append("")
                lines.append(f"已检索 {len(results)} 条结果")

        response_time = data.get("response_time")
        usage = data.get("usage")
        if response_time or usage:
            lines.append("")
            meta = []
            if response_time:
                meta.append(f"耗时: {response_time}s")
            if usage and usage.get("credits") is not None:
                meta.append(f"credits: {usage['credits']}")
            if meta:
                lines.append(" | ".join(meta))

        return "\n".join(lines).strip()
