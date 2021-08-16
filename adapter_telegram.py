import time
import uuid
import threading
import os
import logging
from functools import lru_cache

from telegram.error import NetworkError, Unauthorized, TelegramError # pylint: disable=import-error
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton, Update # pylint: disable=import-error
from telegram.ext import Updater, CommandHandler, MessageHandler, CallbackQueryHandler, Filters, CallbackContext # pylint: disable=import-error

import pika # pylint: disable=import-error

import entities.message_pb2 as messages
import entities.event_pb2 as events

source = "Telegram"

class AdapterTelegram:
    def __init__(self, hostname, APIKey, username, password):

        self.connection_parameters = pika.ConnectionParameters(
                        heartbeat=30,
                        blocked_connection_timeout=300, 
                        host=hostname
                        )
        self.connection = pika.BlockingConnection(self.connection_parameters)
        self.channel = self.connection.channel()
        result = self.channel.queue_declare(queue='Telegram', exclusive=True)
        self.callback_queue = result.method.queue
        self.channel.basic_consume(
            queue=self.callback_queue,
            on_message_callback=self.on_response,
            auto_ack=True)

        self.updater = Updater(APIKey)
        self.bot = self.updater.bot
        self.updater.dispatcher.add_handler(CommandHandler('start', self.start_command))
        self.updater.dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command, self.echo))
        self.updater.dispatcher.add_handler(CallbackQueryHandler(self.callback_echo))
        self.updater.dispatcher.add_error_handler(self.error_handler)



    def start_command(self, update, context) -> None:
        logging.info(f'Start message from {update.message.chat.id}')
        response = self.call('start', update.message.chat.id)
        self.send_response(update.message.chat.id, response)
        logging.info(f'Send response to {update.message.chat.id}')

    def error_handler(self, update, context) -> None:
        #Добавить разделение по типам ошибки
        #При некоторых ошибках падать и открывать connection заново
        logging.warning(f'Error while sendind message', exc_info=context.error)
        if update is not None:
            if update.message is not None:
                if update.message.chat is not None:
                    self.bot.send_message(update.message.chat.id, "Во время обработки вашего сообщения произошла ошибка, пожалуйста, попробуйте позже.")
        if (isinstance(context.error, pika.exceptions.ChannelWrongStateError)) or (isinstance(context.error, pika.exceptions.StreamLostError)):
            logging.error(f'Channel is down')
            try:
                self.connection = pika.BlockingConnection(self.connection_parameters)
                self.channel = self.connection.channel()
                result = self.channel.queue_declare(queue='Telegram', exclusive=True)
                self.callback_queue = result.method.queue
                self.channel.basic_consume(
                    queue=self.callback_queue,
                    on_message_callback=self.on_response,
                    auto_ack=True)
            except Exception as exc:
                logging.error(exc)
                sys.exit(404)


    def prepare_inline_keyboard(self, keyboard):
        if keyboard.hasRowWidth():
            row_width = int(keyboard.row_width)
        else:
            row_width = 1
        result = []
        keys = keyboard.keys
        for i in range(len(keys)//row_width + 1):
            row_keys = keys[i*row_width:(i+1)*row_width]
            row = []
            for key in row_keys:
                if key.hasURL():
                    row.append(InlineKeyboardButton(key.text, url = key.url))
                elif key.hasCallback_data():
                    row.append(InlineKeyboardButton(key.text, callback_data = key.callback_data))
                else:
                    row.append(InlineKeyboardButton(key.text, callback_data = key.text))
            result.append(row)
        return result

    def prepare_reply_keyboard(self, keyboard):
        if keyboard.hasRowWidth():
            row_width = int(keyboard.row_width)
        else:
            row_width = 1
        result = []
        keys = keyboard.keys
        for i in range(len(keys)//row_width + 1):
            row_keys = keys[i*row_width:(i+1)*row_width]
            row = []
            for key in row_keys:
                row.append(key.find('text').text)
            result.append(row)
        return result

    def send_response(self, chat_id, response):
        if response.hasKeyboard():
            keyboard = response.keyboard
            if keyboard.type == 'InlineKeyboard':
                    reply_markup = InlineKeyboardMarkup(self.prepare_inline_keyboard(keyboard))
            elif keyboard.attrib.type == 'ReplyKeyboard':
                    reply_markup = ReplyKeyboardMarkup(self.prepare_reply_keyboard(keyboard))
            else:
                reply_markup = InlineKeyboardMarkup(self.prepare_inline_keyboard(keyboard))

            if response.hasText():
                self.bot.send_message(chat_id, response.text, reply_markup=reply_markup)
            else:
                self.bot.send_message(chat_id, response.name, reply_markup=reply_markup)
        else:
            self.bot.send_message(chat_id, response.text)
        for picture in response.pictures:
            self.bot.send_photo(chat_id, picture.URL)
        if response.hasLocation():
            self.bot.send_location(chat_id, float(response.location.latitude), float(response.find.location.longitude))



    def echo(self, update, context) -> None:
        logging.info(f'Got message from {update.message.chat.id}')
        response = self.call(update.message.text, update.message.chat.id)
        self.send_response(update.message.chat.id, response)
        logging.info(f'Send response to {update.message.chat.id}')

    def callback_echo(self, update, context) -> None:
        query = update.callback_query
        logging.info(f'Got callback message from {query.message.chat.id}')
        query.answer()
        response = self.call(query.data, query.message.chat.id)
        self.send_response(query.message.chat.id, response)
        logging.info(f'Send response to {query.message.chat.id}')

    def call(self, message, chat_id):
        self.response = None
        self.corr_id = str(uuid.uuid4())
        message_entity = messages.Message()
        message_entity.source = source
        message_entity.userId = str(chat_id)
        message_entity.text = message
        event = events.Event()
        event.UUID = uuid.uuid4()
        event.time.GetCurrentTime()
        event.payload.Pack(message_entity)
        self.channel.basic_publish(
            exchange='messages',
            routing_key='message',
            properties=pika.BasicProperties(
                reply_to=self.callback_queue,
                correlation_id=self.corr_id,
            ),
            body=event.SerializeToString()
            )
        logging.info(f'Sent message event to rabbitMQ, UUID:{self.corr_id}')
        while self.response is None:
            self.connection.process_data_events()
        return self.response

    def on_response(self, ch, method, props, body):
        if self.corr_id == props.correlation_id:
            event = events.Event()
            event.ParseFromString(body)
            response = messages.Response()
            event.payload.Unpack(response)
            self.response = response

    def telegram_start(self):
        self.updater.start_polling()

    def start(self) -> None:
        self.telegram_start()
        while (True):
            time.sleep(5)
            self.connection.process_data_events()


def main():
    logging.basicConfig(format='%(asctime)s:%(levelname)s:%(message)s', encoding='utf-8', level=logging.INFO)
    username = os.environ.get('USER')
    password = os.environ.get('PASS')
    APIKey = os.environ.get('API_KEY')
    hostname = os.environ.get('RABBIT_HOSTNAME')
    if hostname is None:
        hostname = 'localhost'
    for _ in range(5):
        try:
            adp = AdapterTelegram(hostname, APIKey, username, password)
            adp.start()
        except pika.exceptions.AMQPConnectionError as exc:
            logging.warning("Failed to connect to RabbitMQ")
            time.sleep(30)
        except Exception as exc:
            logging.error(exc.__cause__, exc_info=exc)
            break


if __name__ == '__main__':
    main()
    print("finished", flush=True)
