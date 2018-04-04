# Copyright (C) 2017 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Sample that implements a text client for the Google Assistant Service."""

import os
import logging
import json
from time import sleep

import click
from google.assistant.embedded.v1alpha2 import embedded_assistant_pb2
from google.assistant.embedded.v1alpha2 import embedded_assistant_pb2_grpc
import google.auth.transport.grpc
import google.auth.transport.requests
import google.oauth2.credentials
import telegram
from telegram.error import NetworkError
from telegram.error import TelegramError
from telegram.error import Unauthorized
from telegram.ext import Filters
from telegram.ext import MessageHandler
from telegram.ext import Updater

try:
    from . import assistant_helpers
except (SystemError, ImportError):
    import assistant_helpers


ASSISTANT_API_ENDPOINT = 'embeddedassistant.googleapis.com'
DEFAULT_GRPC_DEADLINE = 60 * 3 + 5
BOT_TOKEN = os.environ.get('BOT_TOKEN')
ALLOWED_CHAT_IDS = list(
        map(int, os.environ.get('ALLOWED_CHAT_IDS').split(','))
        )
AUTHORIZED_USER_IDS = list(
        map(int, os.environ.get('AUTHORIZED_USER_IDS').split(','))
        )
DEVICE_MODEL_ID = os.environ.get('DEVICE_MODEL_ID')
DEVICE_ID = os.environ.get('DEVICE_ID')


class SampleTextAssistant(object):
    """Sample Assistant that supports text based conversations.

    Args:
      language_code: language for the conversation.
      device_model_id: identifier of the device model.
      device_id: identifier of the registered device instance.
      channel: authorized gRPC channel for connection to the
        Google Assistant API.
      deadline_sec: gRPC deadline in seconds for Google Assistant API call.
    """

    def __init__(self, language_code, device_model_id, device_id,
                 channel, deadline_sec):
        self.language_code = language_code
        self.device_model_id = device_model_id
        self.device_id = device_id
        self.conversation_state = None
        self.assistant = embedded_assistant_pb2_grpc.EmbeddedAssistantStub(
            channel,
        )
        self.deadline = deadline_sec

    def __enter__(self):
        return self

    def __exit__(self, etype, e, traceback):
        if e:
            return False

    def assist(self, text_query):
        """Send a text request to the Assistant and playback the response."""
        def iter_assist_requests():
            dialog_state_in = embedded_assistant_pb2.DialogStateIn(
                language_code=self.language_code,
                conversation_state=b''
            )
            if self.conversation_state:
                dialog_state_in.conversation_state = self.conversation_state
            config = embedded_assistant_pb2.AssistConfig(
                audio_out_config=embedded_assistant_pb2.AudioOutConfig(
                    encoding='LINEAR16',
                    sample_rate_hertz=16000,
                    volume_percentage=0,
                ),
                dialog_state_in=dialog_state_in,
                device_config=embedded_assistant_pb2.DeviceConfig(
                    device_id=self.device_id,
                    device_model_id=self.device_model_id,
                ),
                text_query=text_query,
            )
            req = embedded_assistant_pb2.AssistRequest(config=config)
            assistant_helpers.log_assist_request_without_audio(req)
            yield req

        display_text = None
        for resp in self.assistant.Assist(iter_assist_requests(),
                                          self.deadline):
            assistant_helpers.log_assist_response_without_audio(resp)
            if resp.dialog_state_out.conversation_state:
                conversation_state = resp.dialog_state_out.conversation_state
                self.conversation_state = conversation_state
            if resp.dialog_state_out.supplemental_display_text:
                display_text = resp.dialog_state_out.supplemental_display_text
        return display_text


def assist(bot, update):
    message = update.message
    if message.chat.type == 'private':
        # If user is unauthorized, return an error.
        if message.from_user.id not in AUTHORIZED_USER_IDS:
            message.reply_text('Unauthorized')
        else:
            display_text = assistant.assist(text_query=message.text)
            message.reply_text(display_text)
    # If in a group, only reply to mentions.
    elif message.text.startswith('@%s' % bot.username):
        # Strip first word (the mention) from message text.
        message_tokens = message.text.split(' ', 1)
        if len(message_tokens) > 1:
            message_text = message_tokens[1]
            # Get response from Google Assistant API.
            display_text = assistant.assist(text_query=message_text)
            # Verify that the message is in an authorized chat or from an
            # authorized user.
            if (message.chat_id not in ALLOWED_CHAT_IDS
                    and message.from_user.id not in AUTHORIZED_USER_IDS):
                message.reply_text('Unauthorized')
                should_leave_chat = True
                # If unauthorized and no authorized users are in the chat,
                # leave the chat.
                for user_id in AUTHORIZED_USER_IDS:
                    try:
                        message.chat.get_member(user_id=user_id)
                        should_leave_chat = False
                    except TelegramError:
                        pass
                if should_leave_chat:
                    message.chat.leave()
            elif display_text is not None:
                update.message.reply_text(display_text)


@click.command()
@click.option('--api-endpoint', default=ASSISTANT_API_ENDPOINT,
              metavar='<api endpoint>', show_default=True,
              help='Address of Google Assistant API service.')
@click.option('--credentials-path',
              metavar='<credentials path>', show_default=True,
              default=os.path.join(click.get_app_dir('google-oauthlib-tool'),
                                   'credentials.json'),
              help='Path to read OAuth2 credentials.')
@click.option('--lang', show_default=True,
              metavar='<language code>',
              default='en-US',
              help='Language code of the Assistant')
@click.option('--verbose', '-v', is_flag=True, default=False,
              help='Verbose logging.')
@click.option('--grpc-deadline', default=DEFAULT_GRPC_DEADLINE,
              metavar='<grpc deadline>', show_default=True,
              help='gRPC deadline in seconds')


def main(api_endpoint, credentials_path, lang, verbose,
         grpc_deadline, *args, **kwargs):
    # Setup logging.
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO)

    # Telegram
    """Run the bot."""
    # Telegram Bot Authorization Token
    updater = Updater(BOT_TOKEN)

    # Load OAuth 2.0 credentials.
    try:
        with open(credentials_path, 'r') as f:
            credentials = google.oauth2.credentials.Credentials(token=None,
                                                                **json.load(f))
            http_request = google.auth.transport.requests.Request()
            credentials.refresh(http_request)
    except Exception as e:
        logging.error('Error loading credentials: %s', e)
        logging.error('Run google-oauthlib-tool to initialize '
                      'new OAuth 2.0 credentials.')
        return
    dispatcher = updater.dispatcher
    echo_handler = MessageHandler(Filters.text, assist)
    dispatcher.add_handler(echo_handler)		

    # Create an authorized gRPC channel.
    grpc_channel = google.auth.transport.grpc.secure_authorized_channel(
        credentials, http_request, api_endpoint)
    logging.info('Connecting to %s', api_endpoint)

    global assistant
    assistant = SampleTextAssistant(
            lang,
            DEVICE_MODEL_ID,
            DEVICE_ID,
            grpc_channel,
            grpc_deadline,
            )

    updater.start_polling()
    updater.idle()


if __name__ == '__main__':
    main()
