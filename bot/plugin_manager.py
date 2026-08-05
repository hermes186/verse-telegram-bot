import json

from plugins.dice import DicePlugin
from plugins.youtube_audio_extractor import YouTubeAudioExtractorPlugin
from plugins.ddg_image_search import DDGImageSearchPlugin
from plugins.spotify import SpotifyPlugin
from plugins.crypto import CryptoPlugin
from plugins.weather import WeatherPlugin
from plugins.ddg_web_search import DDGWebSearchPlugin
from plugins.wolfram_alpha import WolframAlphaPlugin
from plugins.deepl import DeeplTranslatePlugin
from plugins.worldtimeapi import WorldTimeApiPlugin
from plugins.whois_ import WhoisPlugin
from plugins.webshot import WebshotPlugin
from plugins.iplocation import IpLocationPlugin
from plugins.tavily_search import TavilySearchPlugin
from plugins.core_memory import CoreMemoryPlugin
from plugins.web_image_embed import WebImageEmbedPlugin


class PluginManager:
    """
    A class to manage the plugins and call the correct functions
    """

    def __init__(self, config):
        enabled_plugins = config.get('plugins', [])
        plugin_mapping = {
            'wolfram': WolframAlphaPlugin,
            'weather': WeatherPlugin,
            'crypto': CryptoPlugin,
            'ddg_web_search': DDGWebSearchPlugin,
            'ddg_image_search': DDGImageSearchPlugin,
            'spotify': SpotifyPlugin,
            'worldtimeapi': WorldTimeApiPlugin,
            'youtube_audio_extractor': YouTubeAudioExtractorPlugin,
            'dice': DicePlugin,
            'deepl_translate': DeeplTranslatePlugin,
            'whois': WhoisPlugin,
            'webshot': WebshotPlugin,
            'iplocation': IpLocationPlugin,
            'tavily_search': TavilySearchPlugin,
            'core_memory': CoreMemoryPlugin,
            'web_image_embed': WebImageEmbedPlugin,
        }
        self.plugins = [plugin_mapping[plugin]() for plugin in enabled_plugins if plugin in plugin_mapping]

    def get_functions_specs(self):
        """
        Return the list of function specs that can be called by the model (legacy format)
        """
        return [spec for specs in map(lambda plugin: plugin.get_spec(), self.plugins) for spec in specs]

    def get_tools_specs(self):
        """
        Return the list of tool specs in the new tools format required by OpenRouter/OpenAI.
        Wraps each function spec as {"type": "function", "function": {...}}
        """
        return [
            {"type": "function", "function": spec}
            for specs in map(lambda plugin: plugin.get_spec(), self.plugins)
            for spec in specs
        ]

    async def call_function(self, function_name, helper, arguments, chat_id=None):
        """
        Call a function based on the name and parameters provided
        """
        plugin = self.__get_plugin_by_function_name(function_name)
        if not plugin:
            return json.dumps({'error': f'Function {function_name} not found'})
        try:
            args_dict = json.loads(arguments) if arguments else {}
            if chat_id is not None:
                args_dict['chat_id'] = chat_id
        except Exception as e:
            return json.dumps({'error': f'Invalid function arguments JSON: {str(e)}'})
        return json.dumps(await plugin.execute(function_name, helper, **args_dict), default=str)

    def get_plugin(self, name: str):
        """
        Return the plugin instance by its registered name or source name.
        """
        return next((plugin for plugin in self.plugins if type(plugin).__name__ == name or plugin.get_source_name() == name), None)

    def get_plugin_source_name(self, function_name) -> str:
        """
        Return the source name of the plugin
        """
        plugin = self.__get_plugin_by_function_name(function_name)
        if not plugin:
            return ''
        return plugin.get_source_name()

    def __get_plugin_by_function_name(self, function_name):
        return next((plugin for plugin in self.plugins
                    if function_name in map(lambda spec: spec.get('name'), plugin.get_spec())), None)
