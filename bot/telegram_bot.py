from __future__ import annotations

import asyncio
import logging
import os
import io
import json
from typing import Any

from uuid import uuid4
from telegram import BotCommandScopeAllGroupChats, Update, constants
from telegram import InlineKeyboardMarkup, InlineKeyboardButton, InlineQueryResultArticle
from telegram import InputTextMessageContent, BotCommand
from telegram.error import RetryAfter, TimedOut, BadRequest
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, \
    filters, InlineQueryHandler, CallbackQueryHandler, Application, ContextTypes, CallbackContext

from pydub import AudioSegment
from PIL import Image

from utils import is_group_chat, get_thread_id, message_text, wrap_with_indicator, split_into_chunks, \
    edit_message_with_retry, get_stream_cutoff_values, is_allowed, get_remaining_budget, is_admin, is_within_budget, \
    get_reply_to_message_id, add_chat_request_to_usage_tracker, error_handler, is_direct_result, handle_direct_result, \
    cleanup_intermediate_files, parse_image_args, resolve_image_size
from openai_helper import OpenAIHelper, localized_text
from usage_tracker import UsageTracker
from document_parser import parse_document
from video_helper import VideoIntelligenceHelper
try:
    from media_group import MediaGroupCollector
except ImportError:
    from bot.media_group import MediaGroupCollector


class ChatGPTTelegramBot:
    """
    Class representing a ChatGPT Telegram Bot.
    """

    def __init__(self, config: dict, openai: OpenAIHelper):
        """
        Initializes the bot with the given configuration and GPT bot object.
        :param config: A dictionary containing the bot configuration
        :param openai: OpenAIHelper object
        """
        self.config = config
        self.openai = openai
        self.video_helper = VideoIntelligenceHelper()
        self.media_group_collector = MediaGroupCollector(debounce_seconds=0.8, on_complete=self._handle_media_group_image)
        bot_language = self.config['bot_language']
        self.commands = [
            BotCommand(command='start', description=localized_text('help_description', bot_language)),
            BotCommand(command='reset', description=localized_text('reset_description', bot_language)),
            BotCommand(command='model', description=localized_text('model_description', bot_language)),
            BotCommand(command='stats', description=localized_text('stats_description', bot_language))
        ]
        # If imaging is enabled, add the "image" command to the list
        if self.config.get('enable_image_generation', False):
            self.commands.append(BotCommand(command='image', description=localized_text('image_description', bot_language)))

        if self.config.get('enable_tts_generation', False):
            self.commands.append(BotCommand(command='tts', description=localized_text('tts_description', bot_language)))
            self.commands.append(BotCommand(command='voice', description=localized_text('voice_description', bot_language)))

        # Add search command
        self.commands.append(BotCommand(command='search', description=localized_text('search_description', bot_language)))

        self.group_commands = self.commands
        self.disallowed_message = localized_text('disallowed', bot_language)
        self.budget_limit_message = localized_text('budget_limit', bot_language)
        self.usage = {}
        self.inline_queries_cache = {}
        # Per-chat semaphores to prevent concurrent vision API calls for the same chat.
        # When a user sends an album, Telegram delivers each photo as a separate update;
        # without this guard all N photos fire simultaneous API requests, saturating the
        # event loop and freezing the bot.
        self._vision_semaphores: dict[int, asyncio.Semaphore] = {}

    async def help(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        """
        Shows the help menu.
        """
        commands = self.group_commands if is_group_chat(update) else self.commands
        commands_description = [f'/{command.command} - {command.description}' for command in commands]
        bot_language = self.config['bot_language']
        help_text = (
                localized_text('help_text', bot_language)[0] +
                '\n\n' +
                '\n'.join(commands_description) +
                '\n\n' +
                localized_text('help_text', bot_language)[1] +
                '\n\n' +
                localized_text('help_text', bot_language)[2]
        )
        await update.message.reply_text(help_text, disable_web_page_preview=True)

    async def stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Returns token usage statistics for current day and month.
        """
        if not await is_allowed(self.config, update, context):
            logging.warning(f'User {update.message.from_user.name} (id: {update.message.from_user.id}) '
                            'is not allowed to request their usage statistics')
            await self.send_disallowed_message(update, context)
            return

        logging.info(f'User {update.message.from_user.name} (id: {update.message.from_user.id}) '
                     'requested their usage statistics')

        user_id = update.message.from_user.id
        if user_id not in self.usage:
            self.usage[user_id] = UsageTracker(user_id, update.message.from_user.name)

        tokens_today, tokens_month = self.usage[user_id].get_current_token_usage()
        images_today, images_month = self.usage[user_id].get_current_image_count()
        (transcribe_minutes_today, transcribe_seconds_today, transcribe_minutes_month,
         transcribe_seconds_month) = self.usage[user_id].get_current_transcription_duration()
        vision_today, vision_month = self.usage[user_id].get_current_vision_tokens()
        characters_today, characters_month = self.usage[user_id].get_current_tts_usage()
        current_cost = self.usage[user_id].get_current_cost()

        chat_id = update.effective_chat.id
        chat_messages, chat_token_length = self.openai.get_conversation_stats(chat_id)
        remaining_budget = get_remaining_budget(self.config, self.usage, update)
        bot_language = self.config['bot_language']
        
        text_current_conversation = (
            f"*{localized_text('stats_conversation', bot_language)[0]}*:\n"
            f"{chat_messages} {localized_text('stats_conversation', bot_language)[1]}\n"
            f"{chat_token_length} {localized_text('stats_conversation', bot_language)[2]}\n"
            "----------------------------\n"
        )
        
        # Check if image generation is enabled and, if so, generate the image statistics for today
        text_today_images = ""
        if self.config.get('enable_image_generation', False):
            text_today_images = f"{images_today} {localized_text('stats_images', bot_language)}\n"

        text_today_vision = ""
        if self.config.get('enable_vision', False):
            text_today_vision = f"{vision_today} {localized_text('stats_vision', bot_language)}\n"

        text_today_tts = ""
        if self.config.get('enable_tts_generation', False):
            text_today_tts = f"{characters_today} {localized_text('stats_tts', bot_language)}\n"
        
        text_today = (
            f"*{localized_text('usage_today', bot_language)}:*\n"
            f"{tokens_today} {localized_text('stats_tokens', bot_language)}\n"
            f"{text_today_images}"  # Include the image statistics for today if applicable
            f"{text_today_vision}"
            f"{text_today_tts}"
            f"{transcribe_minutes_today} {localized_text('stats_transcribe', bot_language)[0]} "
            f"{transcribe_seconds_today} {localized_text('stats_transcribe', bot_language)[1]}\n"
            f"{localized_text('stats_total', bot_language)}{current_cost['cost_today']:.2f}\n"
            "----------------------------\n"
        )
        
        text_month_images = ""
        if self.config.get('enable_image_generation', False):
            text_month_images = f"{images_month} {localized_text('stats_images', bot_language)}\n"

        text_month_vision = ""
        if self.config.get('enable_vision', False):
            text_month_vision = f"{vision_month} {localized_text('stats_vision', bot_language)}\n"

        text_month_tts = ""
        if self.config.get('enable_tts_generation', False):
            text_month_tts = f"{characters_month} {localized_text('stats_tts', bot_language)}\n"
        
        # Check if image generation is enabled and, if so, generate the image statistics for the month
        text_month = (
            f"*{localized_text('usage_month', bot_language)}:*\n"
            f"{tokens_month} {localized_text('stats_tokens', bot_language)}\n"
            f"{text_month_images}"  # Include the image statistics for the month if applicable
            f"{text_month_vision}"
            f"{text_month_tts}"
            f"{transcribe_minutes_month} {localized_text('stats_transcribe', bot_language)[0]} "
            f"{transcribe_seconds_month} {localized_text('stats_transcribe', bot_language)[1]}\n"
            f"{localized_text('stats_total', bot_language)}{current_cost['cost_month']:.2f}"
        )

        # text_budget filled with conditional content
        text_budget = "\n\n"
        budget_period = self.config['budget_period']
        if remaining_budget < float('inf'):
            text_budget += (
                f"{localized_text('stats_budget', bot_language)}"
                f"{localized_text(budget_period, bot_language)}: "
                f"${remaining_budget:.2f}.\n"
            )
        # No longer works as of July 21st 2023, as OpenAI has removed the billing API
        # add OpenAI account information for admin request
        #     text_budget += (
        #         f"{localized_text('stats_openai', bot_language)}"
        #         f"{self.openai.get_billing_current_month():.2f}"
        #     )

        usage_text = text_current_conversation + text_today + text_month + text_budget
        await update.message.reply_text(usage_text, parse_mode=constants.ParseMode.MARKDOWN)



    async def reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Resets the conversation.
        """
        if not await is_allowed(self.config, update, context):
            logging.warning(f'User {update.message.from_user.name} (id: {update.message.from_user.id}) '
                            'is not allowed to reset the conversation')
            await self.send_disallowed_message(update, context)
            return

        logging.info(f'Resetting the conversation for user {update.message.from_user.name} '
                     f'(id: {update.message.from_user.id})...')

        chat_id = update.effective_chat.id
        reset_content = message_text(update.message)
        self.openai.reset_chat_history(chat_id=chat_id, content=reset_content)
        await update.effective_message.reply_text(
            message_thread_id=get_thread_id(update),
            text=localized_text('reset_done', self.config['bot_language'])
        )

    def _extract_photo_or_image(self, msg: Any) -> Any:
        if not msg:
            return None
        photo = getattr(msg, 'photo', None)
        if isinstance(photo, (list, tuple)) and len(photo) > 0:
            return photo[-1]
        elif photo is not None and isinstance(getattr(photo, 'file_id', None), str):
            return photo

        doc = getattr(msg, 'document', None)
        if doc is not None:
            file_id = getattr(doc, 'file_id', None)
            if isinstance(file_id, str):
                mime = getattr(doc, 'mime_type', '')
                mime = mime.lower() if isinstance(mime, str) else ''
                file_name = getattr(doc, 'file_name', '')
                file_name = file_name.lower() if isinstance(file_name, str) else ''
                if mime.startswith('image/') or file_name.endswith(('.png', '.jpg', '.jpeg', '.webp', '.bmp')):
                    return doc
            return None

        att = getattr(msg, 'effective_attachment', None)
        if att is not None:
            target = att[-1] if isinstance(att, (list, tuple)) and len(att) > 0 else att
            if target is not None:
                file_id = getattr(target, 'file_id', None)
                if isinstance(file_id, str):
                    mime = getattr(target, 'mime_type', '')
                    mime = mime.lower() if isinstance(mime, str) else ''
                    file_name = getattr(target, 'file_name', '')
                    file_name = file_name.lower() if isinstance(file_name, str) else ''
                    if mime or file_name:
                        if mime.startswith('image/') or file_name.endswith(('.png', '.jpg', '.jpeg', '.webp', '.bmp')):
                            return target
                        return None
                    return target

        return None

    async def _download_photo_as_png(self, bot: Any, photo_or_doc: Any) -> io.BytesIO:
        """
        Downloads a photo or document attachment from Telegram and converts it to a PNG BytesIO.
        """
        file_id = None
        if isinstance(photo_or_doc, str):
            file_id = photo_or_doc
        elif isinstance(photo_or_doc, (list, tuple)) and len(photo_or_doc) > 0:
            file_id = getattr(photo_or_doc[-1], 'file_id', None)
        elif hasattr(photo_or_doc, 'file_id'):
            file_id = photo_or_doc.file_id
        elif hasattr(photo_or_doc, 'photo') and photo_or_doc.photo:
            p = photo_or_doc.photo
            file_id = p[-1].file_id if isinstance(p, (list, tuple)) else getattr(p, 'file_id', None)
        elif hasattr(photo_or_doc, 'document') and photo_or_doc.document:
            file_id = getattr(photo_or_doc.document, 'file_id', None)
        elif hasattr(photo_or_doc, 'effective_attachment') and photo_or_doc.effective_attachment:
            att = photo_or_doc.effective_attachment
            if isinstance(att, (list, tuple)) and len(att) > 0:
                file_id = getattr(att[-1], 'file_id', None)
            else:
                file_id = getattr(att, 'file_id', None)

        if not file_id:
            raise ValueError("No file_id found for photo or image document")

        # Resolve bot object if a context-like object is passed
        actual_bot = bot
        if hasattr(bot, 'bot') and not hasattr(bot, 'get_file'):
            actual_bot = bot.bot

        media_file = await actual_bot.get_file(file_id)
        byte_array = await media_file.download_as_bytearray()
        temp_file = io.BytesIO(byte_array)
        with Image.open(temp_file) as original_image:
            temp_file_png = io.BytesIO()
            original_image.save(temp_file_png, format='PNG')
            temp_file_png.seek(0)
            return temp_file_png

    async def _handle_media_group_image(self, group: dict[str, Any]):
        """
        Handles image generation for a collected media group (album) when caption starts with /image.
        """
        caption = group.get('caption') or ''
        if not caption.lower().startswith('/image'):
            return

        if not self.config.get('enable_image_generation', False):
            return

        primary_message = group.get('primary_message')
        if not primary_message:
            return

        tokens = caption.split(None, 1)
        image_query = tokens[1].strip() if len(tokens) > 1 else ''

        cleaned_prompt, raw_ar = parse_image_args(image_query)
        target_size = resolve_image_size(
            raw_ar,
            model=self.config.get('image_model', 'gpt-image-2'),
            default_size=self.config.get('image_size', '1024x1024')
        )

        if cleaned_prompt == '':
            try:
                await primary_message.reply_text(
                    text=localized_text('image_no_prompt', self.config['bot_language'])
                )
            except Exception as e:
                logging.exception(e)
            return

        # Get bot instance from update or message
        bot = None
        updates = group.get('updates', [])
        if updates and hasattr(updates[0], 'get_bot'):
            try:
                bot = updates[0].get_bot()
            except Exception:
                pass
        if not bot and updates and hasattr(updates[0], '_bot'):
            bot = updates[0]._bot
        if not bot and hasattr(primary_message, 'get_bot'):
            try:
                bot = primary_message.get_bot()
            except Exception:
                pass
        if not bot and hasattr(primary_message, '_bot'):
            bot = primary_message._bot

        reference_images: list[io.BytesIO] = []
        try:
            for msg in group.get('messages', []):
                att = self._extract_photo_or_image(msg)
                if att:
                    img_buf = await self._download_photo_as_png(bot, att)
                    reference_images.append(img_buf)
        except Exception as e:
            logging.exception(f"Failed to download album reference images: {e}")

        try:
            await primary_message.reply_chat_action(action=constants.ChatAction.UPLOAD_PHOTO)
        except Exception:
            pass

        try:
            image_url, image_size = await self.openai.generate_image(
                prompt=cleaned_prompt,
                reference_images=reference_images if reference_images else None,
                size=target_size
            )
            if self.config.get('image_receive_mode', 'photo') == 'photo':
                await primary_message.reply_photo(photo=image_url)
            elif self.config.get('image_receive_mode', 'photo') == 'document':
                await primary_message.reply_document(document=image_url)
            else:
                await primary_message.reply_photo(photo=image_url)

            user_id = primary_message.from_user.id
            if user_id not in self.usage:
                self.usage[user_id] = UsageTracker(user_id, primary_message.from_user.name)
            self.usage[user_id].add_image_request(image_size, self.config['image_prices'])
            if str(user_id) not in self.config['allowed_user_ids'].split(',') and 'guests' in self.usage:
                self.usage["guests"].add_image_request(image_size, self.config['image_prices'])

        except Exception as e:
            logging.exception(e)
            try:
                await primary_message.reply_text(
                    text=f"{localized_text('image_fail', self.config['bot_language'])}: {str(e)}",
                    parse_mode=constants.ParseMode.MARKDOWN
                )
            except Exception:
                try:
                    await primary_message.reply_text(
                        text=f"{localized_text('image_fail', self.config['bot_language'])}: {str(e)}"
                    )
                except Exception:
                    pass

    async def image(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Generates an image for the given prompt using DALL·E APIs, optionally using reference images.
        """
        if not self.config['enable_image_generation'] \
                or not await self.check_allowed_and_within_budget(update, context):
            return

        raw_text = ''
        if update.message:
            if isinstance(getattr(update.message, 'text', None), str):
                raw_text = update.message.text
            elif isinstance(getattr(update.message, 'caption', None), str):
                raw_text = update.message.caption
            elif getattr(update.message, 'text', None) is not None and isinstance(update.message.text, str):
                raw_text = str(update.message.text)
            elif getattr(update.message, 'caption', None) is not None and isinstance(update.message.caption, str):
                raw_text = str(update.message.caption)

        image_query = ''
        if raw_text:
            tokens = raw_text.split(None, 1)
            if tokens and tokens[0].lower().startswith('/image'):
                image_query = tokens[1].strip() if len(tokens) > 1 else ''
            else:
                try:
                    image_query = message_text(update.message)
                except Exception:
                    image_query = raw_text.strip()

        # Collect reference images
        reference_images: list[io.BytesIO] = []
        try:
            # 1. Check if replying to a photo/document or album
            reply_msg = getattr(update.message, 'reply_to_message', None) if update.message else None
            if reply_msg:
                reply_mg_id = getattr(reply_msg, 'media_group_id', None)
                if reply_mg_id:
                    cached_group = self.media_group_collector.get_cached_group(reply_mg_id)
                    if cached_group and cached_group.get('messages'):
                        for m in cached_group['messages']:
                            att = self._extract_photo_or_image(m)
                            if att:
                                img_buf = await self._download_photo_as_png(context.bot, att)
                                reference_images.append(img_buf)
                    else:
                        att = self._extract_photo_or_image(reply_msg)
                        if att:
                            img_buf = await self._download_photo_as_png(context.bot, att)
                            reference_images.append(img_buf)
                else:
                    att = self._extract_photo_or_image(reply_msg)
                    if att:
                        img_buf = await self._download_photo_as_png(context.bot, att)
                        reference_images.append(img_buf)

            # 2. Check if current message itself has photo/document
            if update.message:
                current_att = self._extract_photo_or_image(update.message)
                if current_att:
                    img_buf = await self._download_photo_as_png(context.bot, current_att)
                    reference_images.append(img_buf)
        except Exception as e:
            logging.exception(f"Failed to process reference images: {e}")

        cleaned_prompt, raw_ar = parse_image_args(image_query)
        target_size = resolve_image_size(
            raw_ar,
            model=self.config.get('image_model', 'gpt-image-2'),
            default_size=self.config.get('image_size', '1024x1024')
        )

        if cleaned_prompt == '':
            await update.effective_message.reply_text(
                message_thread_id=get_thread_id(update),
                text=localized_text('image_no_prompt', self.config['bot_language'])
            )
            return

        logging.info(f'New image generation request received from user {update.message.from_user.name} '
                     f'(id: {update.message.from_user.id}) - size: {target_size}')

        async def _generate():
            try:
                image_url, image_size = await self.openai.generate_image(
                    prompt=cleaned_prompt,
                    reference_images=reference_images if reference_images else None,
                    size=target_size
                )
                if self.config['image_receive_mode'] == 'photo':
                    await update.effective_message.reply_photo(
                        reply_to_message_id=get_reply_to_message_id(self.config, update),
                        photo=image_url
                    )
                elif self.config['image_receive_mode'] == 'document':
                    await update.effective_message.reply_document(
                        reply_to_message_id=get_reply_to_message_id(self.config, update),
                        document=image_url
                    )
                else:
                    raise Exception(f"env variable IMAGE_RECEIVE_MODE has invalid value {self.config['image_receive_mode']}")
                # add image request to users usage tracker
                user_id = update.message.from_user.id
                if user_id not in self.usage:
                    self.usage[user_id] = UsageTracker(user_id, update.message.from_user.name)
                self.usage[user_id].add_image_request(image_size, self.config['image_prices'])
                # add guest chat request to guest usage tracker
                if str(user_id) not in self.config['allowed_user_ids'].split(',') and 'guests' in self.usage:
                    self.usage["guests"].add_image_request(image_size, self.config['image_prices'])

            except Exception as e:
                logging.exception(e)
                await update.effective_message.reply_text(
                    message_thread_id=get_thread_id(update),
                    reply_to_message_id=get_reply_to_message_id(self.config, update),
                    text=f"{localized_text('image_fail', self.config['bot_language'])}: {str(e)}",
                    parse_mode=constants.ParseMode.MARKDOWN
                )

        await wrap_with_indicator(update, context, _generate, constants.ChatAction.UPLOAD_PHOTO)

    async def tts(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Generates an speech for the given input using TTS APIs
        """
        if not self.config['enable_tts_generation'] \
                or not await self.check_allowed_and_within_budget(update, context):
            return

        tts_query = message_text(update.message)
        if tts_query == '':
            await update.effective_message.reply_text(
                message_thread_id=get_thread_id(update),
                text=localized_text('tts_no_prompt', self.config['bot_language'])
            )
            return

        logging.info(f'New speech generation request received from user {update.message.from_user.name} '
                     f'(id: {update.message.from_user.id})')

        async def _generate():
            try:
                speech_file, text_length = await self.openai.generate_speech(text=tts_query, chat_id=update.effective_chat.id)

                await update.effective_message.reply_voice(
                    reply_to_message_id=get_reply_to_message_id(self.config, update),
                    voice=speech_file
                )
                speech_file.close()
                # add image request to users usage tracker
                user_id = update.message.from_user.id
                self.usage[user_id].add_tts_request(text_length, self.config['tts_model'], self.config['tts_prices'])
                # add guest chat request to guest usage tracker
                if str(user_id) not in self.config['allowed_user_ids'].split(',') and 'guests' in self.usage:
                    self.usage["guests"].add_tts_request(text_length, self.config['tts_model'], self.config['tts_prices'])

            except Exception as e:
                logging.exception(e)
                # Avoid Markdown parse errors by not using Markdown mode for raw exception strings
                await update.effective_message.reply_text(
                    message_thread_id=get_thread_id(update),
                    reply_to_message_id=get_reply_to_message_id(self.config, update),
                    text=f"⚠️ {localized_text('tts_fail', self.config['bot_language'])}: {str(e)}"
                )

        await wrap_with_indicator(update, context, _generate, constants.ChatAction.RECORD_VOICE)

    async def transcribe(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Transcribe audio messages.
        """
        if not self.config['enable_transcription'] or not await self.check_allowed_and_within_budget(update, context):
            return

        if is_group_chat(update) and self.config['ignore_group_transcriptions']:
            logging.info('Transcription coming from group chat, ignoring...')
            return

        chat_id = update.effective_chat.id
        filename = update.message.effective_attachment.file_unique_id

        async def _execute():
            filename_mp3 = f'{filename}.mp3'
            bot_language = self.config['bot_language']
            
            is_video = bool(update.message.video or update.message.video_note or (update.message.document and update.message.document.mime_type and update.message.document.mime_type.startswith('video')))
            
            try:
                media_file = await context.bot.get_file(update.message.effective_attachment.file_id, read_timeout=300)
                await media_file.download_to_drive(filename, read_timeout=300)
            except Exception as e:
                logging.exception(e)
                await update.effective_message.reply_text(
                    message_thread_id=get_thread_id(update),
                    reply_to_message_id=get_reply_to_message_id(self.config, update),
                    text=(
                        f"{localized_text('media_download_fail', bot_language)[0]}: "
                        f"{str(e)}. {localized_text('media_download_fail', bot_language)[1]}"
                    ),
                    parse_mode=constants.ParseMode.MARKDOWN
                )
                return

            if is_video:
                if not self.video_helper.is_enabled():
                    await update.effective_message.reply_text(
                        message_thread_id=get_thread_id(update),
                        reply_to_message_id=get_reply_to_message_id(self.config, update),
                        text="视频分析功能未启用，请在配置中检查 Google Cloud Video Intelligence 设置。"
                    )
                    if os.path.exists(filename):
                        os.remove(filename)
                    return

                user_id = update.message.from_user.id
                if user_id not in self.usage:
                    self.usage[user_id] = UsageTracker(user_id, update.message.from_user.name)
                
                try:
                    with open(filename, 'rb') as f:
                        video_bytes = f.read()
                    
                    logging.info(f"Analyzing video for user {user_id}")
                    
                    msg = await update.effective_message.reply_text(
                        message_thread_id=get_thread_id(update),
                        text="⏳ Analyzing video using Google Cloud Video Intelligence...",
                        reply_to_message_id=get_reply_to_message_id(self.config, update)
                    )
                    
                    video_summary = await asyncio.to_thread(self.video_helper.annotate_video_file, video_bytes)
                    
                    caption = update.message.caption or "Please describe this video."
                    prompt = f"The user has uploaded a video. Here is the video intelligence analysis:\n\n{video_summary}\n\nUser's prompt: {caption}"
                    
                    response, total_tokens = await self.openai.get_chat_response(chat_id=chat_id, query=prompt)
                    
                    self.usage[user_id].add_chat_tokens(total_tokens, self.config['token_price'])
                    allowed_user_ids = self.config['allowed_user_ids'].split(',')
                    if str(user_id) not in allowed_user_ids and 'guests' in self.usage:
                        self.usage["guests"].add_chat_tokens(total_tokens, self.config['token_price'])
                        
                    await msg.edit_text(response, parse_mode=constants.ParseMode.MARKDOWN)
                except Exception as e:
                    logging.exception(e)
                    await update.effective_message.reply_text(
                        message_thread_id=get_thread_id(update),
                        reply_to_message_id=get_reply_to_message_id(self.config, update),
                        text=f"Failed to analyze video: {str(e)}"
                    )
                finally:
                    if os.path.exists(filename):
                        os.remove(filename)
                return

            try:
                audio_track = AudioSegment.from_file(filename)
                audio_track.export(filename_mp3, format="mp3")
                logging.info(f'New transcribe request received from user {update.message.from_user.name} '
                             f'(id: {update.message.from_user.id})')

            except Exception as e:
                logging.exception(e)
                await update.effective_message.reply_text(
                    message_thread_id=get_thread_id(update),
                    reply_to_message_id=get_reply_to_message_id(self.config, update),
                    text=localized_text('media_type_fail', bot_language)
                )
                if os.path.exists(filename):
                    os.remove(filename)
                return

            user_id = update.message.from_user.id
            if user_id not in self.usage:
                self.usage[user_id] = UsageTracker(user_id, update.message.from_user.name)

            try:
                transcript = await self.openai.transcribe(filename_mp3)

                transcription_price = self.config['transcription_price']
                self.usage[user_id].add_transcription_seconds(audio_track.duration_seconds, transcription_price)

                allowed_user_ids = self.config['allowed_user_ids'].split(',')
                if str(user_id) not in allowed_user_ids and 'guests' in self.usage:
                    self.usage["guests"].add_transcription_seconds(audio_track.duration_seconds, transcription_price)

                # check if transcript starts with any of the prefixes
                response_to_transcription = any(transcript.lower().startswith(prefix.lower()) if prefix else False
                                                for prefix in self.config['voice_reply_prompts'])

                if self.config['voice_reply_transcript'] and not response_to_transcription:

                    # Split into chunks of 4096 characters (Telegram's message limit)
                    transcript_output = f"_{localized_text('transcript', bot_language)}:_\n\"{transcript}\""
                    chunks = split_into_chunks(transcript_output)

                    for index, transcript_chunk in enumerate(chunks):
                        await update.effective_message.reply_text(
                            message_thread_id=get_thread_id(update),
                            reply_to_message_id=get_reply_to_message_id(self.config, update) if index == 0 else None,
                            text=transcript_chunk,
                            parse_mode=constants.ParseMode.MARKDOWN
                        )
                else:
                    # Get the response of the transcript
                    response, total_tokens = await self.openai.get_chat_response(chat_id=chat_id, query=transcript)

                    self.usage[user_id].add_chat_tokens(total_tokens, self.config['token_price'])
                    if str(user_id) not in allowed_user_ids and 'guests' in self.usage:
                        self.usage["guests"].add_chat_tokens(total_tokens, self.config['token_price'])

                    # Split into chunks of 4096 characters (Telegram's message limit)
                    transcript_output = (
                        f"_{localized_text('transcript', bot_language)}:_\n\"{transcript}\"\n\n"
                        f"_{localized_text('answer', bot_language)}:_\n{response}"
                    )
                    chunks = split_into_chunks(transcript_output)

                    for index, transcript_chunk in enumerate(chunks):
                        await update.effective_message.reply_text(
                            message_thread_id=get_thread_id(update),
                            reply_to_message_id=get_reply_to_message_id(self.config, update) if index == 0 else None,
                            text=transcript_chunk,
                            parse_mode=constants.ParseMode.MARKDOWN
                        )

            except Exception as e:
                logging.exception(e)
                await update.effective_message.reply_text(
                    message_thread_id=get_thread_id(update),
                    reply_to_message_id=get_reply_to_message_id(self.config, update),
                    text=f"{localized_text('transcribe_fail', bot_language)}: {str(e)}",
                    parse_mode=constants.ParseMode.MARKDOWN
                )
            finally:
                if os.path.exists(filename_mp3):
                    os.remove(filename_mp3)
                if os.path.exists(filename):
                    os.remove(filename)

        await wrap_with_indicator(update, context, _execute, constants.ChatAction.TYPING)

    async def vision(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Interpret image using vision model or generate image if caption is /image.
        """
        if not await self.check_allowed_and_within_budget(update, context):
            return

        caption = update.message.caption if update.message else None
        media_group_id = getattr(update.message, 'media_group_id', None) if update.message else None

        # Check if caption starts with /image (case-insensitive)
        if caption and caption.lower().startswith('/image'):
            if not self.config.get('enable_image_generation', False):
                return
            if media_group_id:
                await self.media_group_collector.add_update(update)
                return
            else:
                return await self.image(update, context)

        # If caption does NOT start with /image, but message has media_group_id that is already active
        if media_group_id and media_group_id in self.media_group_collector.active_groups:
            await self.media_group_collector.add_update(update)
            return

        if not self.config['enable_vision']:
            return

        chat_id = update.effective_chat.id
        prompt = update.message.caption

        if is_group_chat(update):
            if self.config['ignore_group_vision']:
                logging.info('Vision coming from group chat, ignoring...')
                return
            else:
                trigger_keyword = self.config['group_trigger_keyword']
                if (prompt is None and trigger_keyword != '') or \
                   (prompt is not None and not prompt.lower().startswith(trigger_keyword.lower())):
                    logging.info('Vision coming from group chat with wrong keyword, ignoring...')
                    return
        
        image = update.message.effective_attachment[-1]
        

        async def _execute():
            bot_language = self.config['bot_language']

            # Acquire per-chat semaphore to prevent concurrent vision API calls.
            # Telegram albums deliver each photo as a separate update in rapid succession;
            # without this guard all N photos fire simultaneous API requests.
            sem = self._vision_semaphores.setdefault(chat_id, asyncio.Semaphore(1))
            if sem.locked():
                await update.effective_message.reply_text(
                    message_thread_id=get_thread_id(update),
                    text='⏳ 正在处理上一张图片，请稍候...'
                )
                return

            async with sem:
                try:
                    media_file = await context.bot.get_file(image.file_id)
                    temp_file = io.BytesIO(await media_file.download_as_bytearray())
                except Exception as e:
                    logging.exception(e)
                    await update.effective_message.reply_text(
                        message_thread_id=get_thread_id(update),
                        reply_to_message_id=get_reply_to_message_id(self.config, update),
                        text=(
                            f"{localized_text('media_download_fail', bot_language)[0]}: "
                            f"{str(e)}. {localized_text('media_download_fail', bot_language)[1]}"
                        ),
                        parse_mode=constants.ParseMode.MARKDOWN
                    )
                    return

                # convert jpg from telegram to png as understood by openai

                temp_file_png = io.BytesIO()

                try:
                    original_image = Image.open(temp_file)

                    original_image.save(temp_file_png, format='PNG')
                    logging.info(f'New vision request received from user {update.message.from_user.name} '
                                 f'(id: {update.message.from_user.id})')

                except Exception as e:
                    logging.exception(e)
                    await update.effective_message.reply_text(
                        message_thread_id=get_thread_id(update),
                        reply_to_message_id=get_reply_to_message_id(self.config, update),
                        text=localized_text('media_type_fail', bot_language)
                    )

                user_id = update.message.from_user.id
                if user_id not in self.usage:
                    self.usage[user_id] = UsageTracker(user_id, update.message.from_user.name)

                try:
                    if self.config['stream']:
                        stream_response = self.openai.interpret_image_stream(chat_id=chat_id, fileobj=temp_file_png, prompt=prompt)
                        i = 0
                        prev = ''
                        sent_message = None
                        backoff = 0
                        stream_chunk = 0

                        async for content, tokens in stream_response:
                            if is_direct_result(content):
                                return await handle_direct_result(self.config, update, content)

                            if len(content.strip()) == 0:
                                continue

                            stream_chunks = split_into_chunks(content)
                            if len(stream_chunks) > 1:
                                content = stream_chunks[-1]
                                if stream_chunk != len(stream_chunks) - 1:
                                    stream_chunk += 1
                                    try:
                                        await edit_message_with_retry(context, chat_id, str(sent_message.message_id),
                                                                      stream_chunks[-2])
                                    except Exception:
                                        pass
                                    try:
                                        sent_message = await update.effective_message.reply_text(
                                            message_thread_id=get_thread_id(update),
                                            text=content if len(content) > 0 else "..."
                                        )
                                    except Exception:
                                        pass
                                    continue

                            cutoff = get_stream_cutoff_values(update, content)
                            cutoff += backoff

                            if i == 0:
                                try:
                                    if sent_message is not None:
                                        await context.bot.delete_message(chat_id=sent_message.chat_id,
                                                                         message_id=sent_message.message_id)
                                    sent_message = await update.effective_message.reply_text(
                                        message_thread_id=get_thread_id(update),
                                        reply_to_message_id=get_reply_to_message_id(self.config, update),
                                        text=content,
                                    )
                                except Exception:
                                    continue

                            elif abs(len(content) - len(prev)) > cutoff or tokens != 'not_finished':
                                prev = content

                                try:
                                    use_markdown = tokens != 'not_finished'
                                    await edit_message_with_retry(context, chat_id, str(sent_message.message_id),
                                                                  text=content, markdown=use_markdown)

                                except RetryAfter as e:
                                    backoff += 5
                                    await asyncio.sleep(e.retry_after)
                                    continue

                                except TimedOut:
                                    backoff += 5
                                    await asyncio.sleep(0.5)
                                    continue

                                except Exception:
                                    backoff += 5
                                    continue

                                await asyncio.sleep(0.01)

                            i += 1
                            if tokens != 'not_finished':
                                total_tokens = int(tokens)

                    else:
                        interpretation, total_tokens = await self.openai.interpret_image(chat_id, temp_file_png, prompt=prompt)

                        try:
                            await update.effective_message.reply_text(
                                message_thread_id=get_thread_id(update),
                                reply_to_message_id=get_reply_to_message_id(self.config, update),
                                text=interpretation,
                                parse_mode=constants.ParseMode.MARKDOWN
                            )
                        except BadRequest:
                            await update.effective_message.reply_text(
                                message_thread_id=get_thread_id(update),
                                reply_to_message_id=get_reply_to_message_id(self.config, update),
                                text=interpretation
                            )
                except Exception as e:
                    logging.exception(e)
                    await update.effective_message.reply_text(
                        message_thread_id=get_thread_id(update),
                        reply_to_message_id=get_reply_to_message_id(self.config, update),
                        text="不支持的文件类型！"
                    )
                    return
                vision_token_price = self.config['vision_token_price']
                self.usage[user_id].add_vision_tokens(total_tokens, vision_token_price)

                allowed_user_ids = self.config['allowed_user_ids'].split(',')
                if str(user_id) not in allowed_user_ids and 'guests' in self.usage:
                    self.usage["guests"].add_vision_tokens(total_tokens, vision_token_price)

        await wrap_with_indicator(update, context, _execute, constants.ChatAction.TYPING)

    async def prompt(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        React to incoming messages and respond accordingly.
        """
        if update.edited_message or not update.message or update.message.via_bot:
            return

        if not await self.check_allowed_and_within_budget(update, context):
            return

        logging.info(
            f'New message received from user {update.message.from_user.name} (id: {update.message.from_user.id})')
        chat_id = update.effective_chat.id
        user_id = update.message.from_user.id
        
        # message_text extracts from text or caption
        prompt = message_text(update.message)
        
        # Intercept document attachments
        if update.message.document:
            file_id = update.message.document.file_id
            file_name = update.message.document.file_name
            _, ext = os.path.splitext(file_name.lower())
            supported_exts = ['.pdf', '.docx', '.xlsx', '.pptx', '.txt']
            if ext in supported_exts:
                await update.effective_message.reply_chat_action(
                    action=constants.ChatAction.TYPING,
                    message_thread_id=get_thread_id(update)
                )
                try:
                    file_obj = await context.bot.get_file(file_id, read_timeout=300)
                    os.makedirs("scratch", exist_ok=True)
                    local_path = os.path.join("scratch", f"{uuid4()}_{file_name}")
                    await file_obj.download_to_drive(local_path, read_timeout=300)
                    
                    # Calculate dynamic max chars based on model context size to avoid blowing up the API
                    # Reserve 1000 tokens for the user prompt and system messages
                    max_allowed_tokens = self.openai.get_max_model_tokens(chat_id) - self.config.get('max_tokens', 1000) - 1000
                    # Rough estimation: assume 1 token = 2 chars for safety. 
                    max_chars = max(5000, max_allowed_tokens * 2) 
                    
                    extracted_text = parse_document(local_path, file_name, max_chars)
                    
                    if os.path.exists(local_path):
                        os.remove(local_path)
                        
                    if extracted_text:
                        if not prompt:
                            prompt = "请提取并总结这份文档的主要内容"
                        prompt = f"{prompt}\n\n[附带文档内容开始: {file_name}]\n{extracted_text}\n[附带文档内容结束]"
                    else:
                        await update.effective_message.reply_text(
                            "无法从文件中提取到任何文本内容（文件可能为空、仅包含图片，或格式不受支持）。",
                            message_thread_id=get_thread_id(update)
                        )
                        return
                except Exception as e:
                    logging.error(f"Error handling document: {e}")
                    
        if not prompt:
            return

        if is_group_chat(update):
            trigger_keyword = self.config['group_trigger_keyword']

            # Fix latent bug: update.message.text might be None for media messages
            msg_text = update.message.text or ""
            if prompt.lower().startswith(trigger_keyword.lower()) or msg_text.lower().startswith('/chat'):
                if prompt.lower().startswith(trigger_keyword.lower()):
                    prompt = prompt[len(trigger_keyword):].strip()

                if update.message.reply_to_message and \
                        update.message.reply_to_message.text and \
                        update.message.reply_to_message.from_user.id != context.bot.id:
                    prompt = f'"{update.message.reply_to_message.text}" {prompt}'
            else:
                if update.message.reply_to_message and update.message.reply_to_message.from_user.id == context.bot.id:
                    logging.info('Message is a reply to the bot, allowing...')
                else:
                    logging.warning('Message does not start with trigger keyword, ignoring...')
                    return

        try:
            total_tokens = 0

            if self.config['stream']:
                await update.effective_message.reply_chat_action(
                    action=constants.ChatAction.TYPING,
                    message_thread_id=get_thread_id(update)
                )

                stream_response = self.openai.get_chat_response_stream(chat_id=chat_id, query=prompt)
                i = 0
                prev = ''
                sent_message = None
                backoff = 0
                stream_chunk = 0

                async for content, tokens in stream_response:
                    if is_direct_result(content):
                        return await handle_direct_result(self.config, update, content)

                    if len(content.strip()) == 0:
                        continue

                    stream_chunks = split_into_chunks(content)
                    if len(stream_chunks) > 1:
                        content = stream_chunks[-1]
                        if stream_chunk != len(stream_chunks) - 1:
                            stream_chunk += 1
                            try:
                                await edit_message_with_retry(context, chat_id, str(sent_message.message_id),
                                                              stream_chunks[-2])
                            except Exception:
                                pass
                            try:
                                sent_message = await update.effective_message.reply_text(
                                    message_thread_id=get_thread_id(update),
                                    text=content if len(content) > 0 else "..."
                                )
                            except Exception:
                                pass
                            continue

                    cutoff = get_stream_cutoff_values(update, content)
                    cutoff += backoff

                    if i == 0:
                        try:
                            if sent_message is not None:
                                await context.bot.delete_message(chat_id=sent_message.chat_id,
                                                                 message_id=sent_message.message_id)
                            sent_message = await update.effective_message.reply_text(
                                message_thread_id=get_thread_id(update),
                                reply_to_message_id=get_reply_to_message_id(self.config, update),
                                text=content,
                            )
                        except Exception:
                            continue

                    elif abs(len(content) - len(prev)) > cutoff or tokens != 'not_finished':
                        prev = content

                        try:
                            use_markdown = tokens != 'not_finished'
                            await edit_message_with_retry(context, chat_id, str(sent_message.message_id),
                                                          text=content, markdown=use_markdown)

                        except RetryAfter as e:
                            backoff += 5
                            await asyncio.sleep(e.retry_after)
                            continue

                        except TimedOut:
                            backoff += 5
                            await asyncio.sleep(0.5)
                            continue

                        except Exception:
                            backoff += 5
                            continue

                        await asyncio.sleep(0.01)

                    i += 1
                    if tokens != 'not_finished':
                        total_tokens = int(tokens)
                        
                        import re
                        image_pattern = re.compile(r'!\[([^\]]*)\]\(([^)]+)\)')
                        images = image_pattern.findall(content)
                        if images:
                            clean_content = image_pattern.sub('', content).strip()
                            if clean_content:
                                await edit_message_with_retry(context, chat_id, str(sent_message.message_id),
                                                              text=clean_content, markdown=True)
                            else:
                                try:
                                    await context.bot.delete_message(chat_id=chat_id, message_id=sent_message.message_id)
                                except Exception:
                                    pass
                                    
                            for alt, url in images:
                                try:
                                    await update.effective_message.reply_photo(
                                        message_thread_id=get_thread_id(update),
                                        photo=url,
                                        caption=alt[:1024] if alt else None
                                    )
                                except Exception as e:
                                    logging.error(f"Failed to send photo {url}: {e}")

            else:
                async def _reply():
                    nonlocal total_tokens
                    response, total_tokens = await self.openai.get_chat_response(chat_id=chat_id, query=prompt)

                    if is_direct_result(response):
                        return await handle_direct_result(self.config, update, response)

                    import re
                    image_pattern = re.compile(r'!\[([^\]]*)\]\(([^)]+)\)')
                    images = image_pattern.findall(response)
                    if images:
                        response = image_pattern.sub('', response).strip()

                    if response:
                        # Split into chunks of 4096 characters (Telegram's message limit)
                        chunks = split_into_chunks(response)

                        for index, chunk in enumerate(chunks):
                            try:
                                await update.effective_message.reply_text(
                                    message_thread_id=get_thread_id(update),
                                    reply_to_message_id=get_reply_to_message_id(self.config,
                                                                                update) if index == 0 else None,
                                    text=chunk,
                                    parse_mode=constants.ParseMode.MARKDOWN
                                )
                            except Exception:
                                try:
                                    await update.effective_message.reply_text(
                                        message_thread_id=get_thread_id(update),
                                        reply_to_message_id=get_reply_to_message_id(self.config,
                                                                                    update) if index == 0 else None,
                                        text=chunk
                                    )
                                except Exception as e:
                                    raise e

                    for alt, url in images:
                        try:
                            await update.effective_message.reply_photo(
                                message_thread_id=get_thread_id(update),
                                photo=url,
                                caption=alt[:1024] if alt else None
                            )
                        except Exception as e:
                            logging.error(f"Failed to send photo {url}: {e}")

                await wrap_with_indicator(update, context, _reply, constants.ChatAction.TYPING)

            add_chat_request_to_usage_tracker(self.usage, self.config, user_id, total_tokens)

        except Exception as e:
            logging.exception(e)
            await update.effective_message.reply_text(
                message_thread_id=get_thread_id(update),
                reply_to_message_id=get_reply_to_message_id(self.config, update),
                text=f"{localized_text('chat_fail', self.config['bot_language'])} {str(e)}"
            )

    async def inline_query(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        Handle the inline query. This is run when you type: @botusername <query>
        """
        query = update.inline_query.query
        if len(query) < 3:
            return
        if not await self.check_allowed_and_within_budget(update, context, is_inline=True):
            return

        callback_data_suffix = "gpt:"
        result_id = str(uuid4())
        self.inline_queries_cache[result_id] = query
        callback_data = f'{callback_data_suffix}{result_id}'

        await self.send_inline_query_result(update, result_id, message_content=query, callback_data=callback_data)

    async def send_inline_query_result(self, update: Update, result_id, message_content, callback_data=""):
        """
        Send inline query result
        """
        try:
            reply_markup = None
            bot_language = self.config['bot_language']
            if callback_data:
                reply_markup = InlineKeyboardMarkup([[
                    InlineKeyboardButton(text=f'🤖 {localized_text("answer_with_chatgpt", bot_language)}',
                                         callback_data=callback_data)
                ]])

            inline_query_result = InlineQueryResultArticle(
                id=result_id,
                title=localized_text("ask_chatgpt", bot_language),
                input_message_content=InputTextMessageContent(message_content),
                description=message_content,
                thumbnail_url='https://x0.at/yaF8.jpg',
                reply_markup=reply_markup
            )

            await update.inline_query.answer([inline_query_result], cache_time=0)
        except Exception as e:
            logging.error(f'An error occurred while generating the result card for inline query {e}')


    async def search_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Command handler for /search to explicitly perform a web search.
        """
        if not await self.check_allowed_and_within_budget(update, context):
            return

        query = ' '.join(context.args)
        if not query:
            await update.message.reply_text(
                message_thread_id=get_thread_id(update),
                text="请提供搜索关键词，例如：/search 今天的天气"
            )
            return

        await update.effective_message.reply_chat_action(
            action=constants.ChatAction.TYPING,
            message_thread_id=get_thread_id(update)
        )

        plugin = self.openai.plugin_manager.get_plugin('TavilySearchPlugin')
        if not plugin:
            await update.message.reply_text(
                message_thread_id=get_thread_id(update),
                text="未启用搜索功能（TavilySearchPlugin）"
            )
            return

        try:
            result = await plugin.execute("tavily_search", self.openai, query=query)
            if isinstance(result, dict) and "error" in result:
                await update.message.reply_text(
                    message_thread_id=get_thread_id(update),
                    text=f"搜索失败: {result['error']}"
                )
                return

            synth_prompt = (
                f"用户使用 /search 命令请求搜索：{query}\n\n"
                f"互联网实时检索到的相关结果如下：\n{json.dumps(result, ensure_ascii=False, indent=2) if isinstance(result, dict) else result}\n\n"
                f"请结合上述检索结果，针对用户的问题进行整合、总结并回答。\n"
                f"要求：\n"
                f"1. 仔细梳理信息，给出连贯、有逻辑且条理清晰的回答，不要机械罗列或一股脑抛出原始数据。\n"
                f"2. 在回答的最后附上参考来源，格式为：\n📌 **参考来源：**\n1. [文章标题](URL)\n2. [文章标题](URL)"
            )

            # Delegate synthesis to prompt handler
            with update.message._unfrozen() as message:
                message.text = synth_prompt
            await self.prompt(update, context)

        except Exception as e:
            logging.exception(e)
            await update.message.reply_text(
                message_thread_id=get_thread_id(update),
                text=f"搜索过程发生错误: {str(e)}"
            )

    async def model_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Command handler for /model to switch models.
        """
        if not await is_allowed(self.config, update, context):
            await self.send_disallowed_message(update, context)
            return

        chat_id = update.effective_chat.id

        if context.args and len(context.args) > 0:
            target_model = context.args[0].strip()
            new_m = self.openai.set_chat_model(chat_id, target_model)
            if target_model not in self.openai.models:
                self.openai.models.append(target_model)
            text = (
                f"✅ *已成功切换模型*\n\n"
                f"生效模型: `{new_m}`"
            )
            await update.effective_message.reply_text(
                text=text,
                parse_mode=constants.ParseMode.MARKDOWN
            )
            return

        models = self.openai.get_models()
        current_m = self.openai.get_chat_model(chat_id)

        bot_language = self.config['bot_language']

        if not models:
            await update.effective_message.reply_text(
                text=localized_text('model_not_configured', bot_language),
                parse_mode=constants.ParseMode.MARKDOWN
            )
            return

        keyboard = []
        for mname in models:
            label = f"✅ {mname}" if mname == current_m else mname
            keyboard.append([InlineKeyboardButton(label, callback_data=f"select_model:{mname}")])

        reply_markup = InlineKeyboardMarkup(keyboard)
        
        model_title = localized_text('model_title', bot_language)
        model_current = localized_text('model_current', bot_language)
        model_select = localized_text('model_select', bot_language)
        
        text = (
            f"{model_title}\n\n"
            f"{model_current} `{current_m}`\n\n"
            f"{model_select}"
        )
        await update.effective_message.reply_text(
            text=text,
            reply_markup=reply_markup,
            parse_mode=constants.ParseMode.MARKDOWN
        )

    async def handle_select_model(self, update: Update, context: CallbackContext):
        query = update.callback_query
        await query.answer()
        chat_id = update.effective_chat.id
        model_name = query.data.split('select_model:')[1]
        bot_language = self.config['bot_language']

        new_m = self.openai.set_chat_model(chat_id, model_name)
        model_switched_title = localized_text('model_switched_title', bot_language)
        model_switched_current = localized_text('model_switched_current', bot_language)

        text = (
            f"{model_switched_title}\n\n"
            f"{model_switched_current} `{new_m}`"
        )
        await query.edit_message_text(text=text, parse_mode=constants.ParseMode.MARKDOWN)

    async def voice_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Handle the /voice command
        """
        if not await is_allowed(self.config, update, context):
            await self.send_disallowed_message(update, context)
            return
            
        if not self.config.get('enable_tts_generation', False):
            return

        chat_id = update.effective_chat.id
        voices = self.openai.get_tts_voices()
        current_voice = self.openai.get_tts_voice(chat_id)

        bot_language = self.config['bot_language']

        if not voices:
            await update.effective_message.reply_text(
                text=localized_text('voice_not_configured', bot_language),
                parse_mode=constants.ParseMode.MARKDOWN
            )
            return

        keyboard = []
        # voices is a dict: {voice_id: display_name}
        for vid, vname in voices.items():
            label = f"✅ {vname}" if vid == current_voice else vname
            keyboard.append([InlineKeyboardButton(label, callback_data=f"select_voice:{vid}")])

        reply_markup = InlineKeyboardMarkup(keyboard)
        
        voice_title = localized_text('voice_title', bot_language)
        voice_current = localized_text('voice_current', bot_language)
        voice_select = localized_text('voice_select', bot_language)
        
        current_name = voices.get(current_voice, current_voice)
        text = (
            f"{voice_title}\n\n"
            f"{voice_current} `{current_name}`\n\n"
            f"{voice_select}"
        )
        await update.effective_message.reply_text(
            text=text,
            reply_markup=reply_markup,
            parse_mode=constants.ParseMode.MARKDOWN
        )

    async def handle_select_voice(self, update: Update, context: CallbackContext):
        query = update.callback_query
        await query.answer()
        chat_id = update.effective_chat.id
        voice_id = query.data.split('select_voice:')[1]
        bot_language = self.config['bot_language']

        new_v = self.openai.set_tts_voice(chat_id, voice_id)
        voice_switched_title = localized_text('voice_switched_title', bot_language)
        voice_switched_current = localized_text('voice_switched_current', bot_language)
        
        voices = self.openai.get_tts_voices()
        new_name = voices.get(new_v, new_v)

        text = (
            f"{voice_switched_title}\n\n"
            f"{voice_switched_current} `{new_name}`"
        )
        await query.edit_message_text(text=text, parse_mode=constants.ParseMode.MARKDOWN)

    async def handle_callback_inline_query(self, update: Update, context: CallbackContext):
        """
        Handle the callback query from the inline query result or provider/model button selections
        """
        callback_data = update.callback_query.data
        if callback_data.startswith('select_model:'):
            await self.handle_select_model(update, context)
            return
        if callback_data.startswith('select_voice:'):
            await self.handle_select_voice(update, context)
            return
        user_id = update.callback_query.from_user.id
        inline_message_id = update.callback_query.inline_message_id
        name = update.callback_query.from_user.name
        callback_data_suffix = "gpt:"
        query = ""
        bot_language = self.config['bot_language']
        answer_tr = localized_text("answer", bot_language)
        loading_tr = localized_text("loading", bot_language)

        try:
            if callback_data.startswith(callback_data_suffix):
                await update.callback_query.answer()
                unique_id = callback_data.split(':')[1]
                total_tokens = 0

                # Retrieve the prompt from the cache
                query = self.inline_queries_cache.get(unique_id)
                if query:
                    self.inline_queries_cache.pop(unique_id)
                else:
                    error_message = (
                        f'{localized_text("error", bot_language)}. '
                        f'{localized_text("try_again", bot_language)}'
                    )
                    await edit_message_with_retry(context, chat_id=None, message_id=inline_message_id,
                                                  text=f'{query}\n\n_{answer_tr}:_\n{error_message}',
                                                  is_inline=True)
                    return

                unavailable_message = localized_text("function_unavailable_in_inline_mode", bot_language)
                if self.config['stream']:
                    stream_response = self.openai.get_chat_response_stream(chat_id=user_id, query=query)
                    i = 0
                    prev = ''
                    backoff = 0
                    async for content, tokens in stream_response:
                        if is_direct_result(content):
                            cleanup_intermediate_files(content)
                            await edit_message_with_retry(context, chat_id=None,
                                                          message_id=inline_message_id,
                                                          text=f'{query}\n\n_{answer_tr}:_\n{unavailable_message}',
                                                          is_inline=True)
                            return

                        if len(content.strip()) == 0:
                            continue

                        cutoff = get_stream_cutoff_values(update, content)
                        cutoff += backoff

                        if i == 0:
                            try:
                                await edit_message_with_retry(context, chat_id=None,
                                                              message_id=inline_message_id,
                                                              text=f'{query}\n\n{answer_tr}:\n{content}',
                                                              is_inline=True)
                            except Exception:
                                continue

                        elif abs(len(content) - len(prev)) > cutoff or tokens != 'not_finished':
                            prev = content
                            try:
                                use_markdown = tokens != 'not_finished'
                                divider = '_' if use_markdown else ''
                                text = f'{query}\n\n{divider}{answer_tr}:{divider}\n{content}'

                                # We only want to send the first 4096 characters. No chunking allowed in inline mode.
                                text = text[:4096]

                                await edit_message_with_retry(context, chat_id=None, message_id=inline_message_id,
                                                              text=text, markdown=use_markdown, is_inline=True)

                            except RetryAfter as e:
                                backoff += 5
                                await asyncio.sleep(e.retry_after)
                                continue
                            except TimedOut:
                                backoff += 5
                                await asyncio.sleep(0.5)
                                continue
                            except Exception:
                                backoff += 5
                                continue

                            await asyncio.sleep(0.01)

                        i += 1
                        if tokens != 'not_finished':
                            total_tokens = int(tokens)

                else:
                    async def _send_inline_query_response():
                        nonlocal total_tokens
                        # Edit the current message to indicate that the answer is being processed
                        await context.bot.edit_message_text(inline_message_id=inline_message_id,
                                                            text=f'{query}\n\n_{answer_tr}:_\n{loading_tr}',
                                                            parse_mode=constants.ParseMode.MARKDOWN)

                        logging.info(f'Generating response for inline query by {name}')
                        response, total_tokens = await self.openai.get_chat_response(chat_id=user_id, query=query)

                        if is_direct_result(response):
                            cleanup_intermediate_files(response)
                            await edit_message_with_retry(context, chat_id=None,
                                                          message_id=inline_message_id,
                                                          text=f'{query}\n\n_{answer_tr}:_\n{unavailable_message}',
                                                          is_inline=True)
                            return

                        text_content = f'{query}\n\n_{answer_tr}:_\n{response}'

                        # We only want to send the first 4096 characters. No chunking allowed in inline mode.
                        text_content = text_content[:4096]

                        # Edit the original message with the generated content
                        await edit_message_with_retry(context, chat_id=None, message_id=inline_message_id,
                                                      text=text_content, is_inline=True)

                    await wrap_with_indicator(update, context, _send_inline_query_response,
                                              constants.ChatAction.TYPING, is_inline=True)

                add_chat_request_to_usage_tracker(self.usage, self.config, user_id, total_tokens)

        except Exception as e:
            logging.error(f'Failed to respond to an inline query via button callback: {e}')
            logging.exception(e)
            localized_answer = localized_text('chat_fail', self.config['bot_language'])
            await edit_message_with_retry(context, chat_id=None, message_id=inline_message_id,
                                          text=f"{query}\n\n_{answer_tr}:_\n{localized_answer} {str(e)}",
                                          is_inline=True)

    async def check_allowed_and_within_budget(self, update: Update, context: ContextTypes.DEFAULT_TYPE,
                                              is_inline=False) -> bool:
        """
        Checks if the user is allowed to use the bot and if they are within their budget
        :param update: Telegram update object
        :param context: Telegram context object
        :param is_inline: Boolean flag for inline queries
        :return: Boolean indicating if the user is allowed to use the bot
        """
        name = update.inline_query.from_user.name if is_inline else update.message.from_user.name
        user_id = update.inline_query.from_user.id if is_inline else update.message.from_user.id

        if not await is_allowed(self.config, update, context, is_inline=is_inline):
            logging.warning(f'User {name} (id: {user_id}) is not allowed to use the bot')
            await self.send_disallowed_message(update, context, is_inline)
            return False
        if not is_within_budget(self.config, self.usage, update, is_inline=is_inline):
            logging.warning(f'User {name} (id: {user_id}) reached their usage limit')
            await self.send_budget_reached_message(update, context, is_inline)
            return False

        return True

    async def send_disallowed_message(self, update: Update, _: ContextTypes.DEFAULT_TYPE, is_inline=False):
        """
        Sends the disallowed message to the user.
        """
        if not is_inline:
            await update.effective_message.reply_text(
                message_thread_id=get_thread_id(update),
                text=self.disallowed_message,
                disable_web_page_preview=True
            )
        else:
            result_id = str(uuid4())
            await self.send_inline_query_result(update, result_id, message_content=self.disallowed_message)

    async def send_budget_reached_message(self, update: Update, _: ContextTypes.DEFAULT_TYPE, is_inline=False):
        """
        Sends the budget reached message to the user.
        """
        if not is_inline:
            await update.effective_message.reply_text(
                message_thread_id=get_thread_id(update),
                text=self.budget_limit_message
            )
        else:
            result_id = str(uuid4())
            await self.send_inline_query_result(update, result_id, message_content=self.budget_limit_message)

    async def post_init(self, application: Application) -> None:
        """
        Post initialization hook for the bot.
        """
        pass

    def run(self):
        """
        Runs the bot indefinitely until the user presses Ctrl+C
        """
        application = ApplicationBuilder() \
            .token(self.config['token']) \
            .proxy(self.config['proxy']) \
            .get_updates_proxy(self.config['proxy']) \
            .post_init(self.post_init) \
            .concurrent_updates(True) \
            .build()

        application.add_handler(CommandHandler('reset', self.reset))
        application.add_handler(CommandHandler('search', self.search_command))
        application.add_handler(CommandHandler('model', self.model_command))
        application.add_handler(CommandHandler('voice', self.voice_command))
        application.add_handler(CommandHandler('image', self.image))
        application.add_handler(CommandHandler('tts', self.tts))
        application.add_handler(CommandHandler('start', self.help))
        application.add_handler(CommandHandler('stats', self.stats))
        application.add_handler(MessageHandler(
            filters.PHOTO | filters.Document.IMAGE,
            self.vision))
        application.add_handler(MessageHandler(
            filters.AUDIO | filters.VOICE | filters.Document.AUDIO |
            filters.VIDEO | filters.VIDEO_NOTE | filters.Document.VIDEO,
            self.transcribe))
        application.add_handler(MessageHandler((filters.TEXT | filters.Document.ALL) & (~filters.COMMAND), self.prompt))
        application.add_handler(InlineQueryHandler(self.inline_query, chat_types=[
            constants.ChatType.GROUP, constants.ChatType.SUPERGROUP, constants.ChatType.PRIVATE
        ]))
        application.add_handler(CallbackQueryHandler(self.handle_callback_inline_query))

        application.add_error_handler(error_handler)

        application.run_polling()
