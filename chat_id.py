"""Вспомогательный поиск своего TG_CHAT_ID: напишите созданному боту /start."""
import os
import requests
from dotenv import load_dotenv

load_dotenv()
token = os.getenv('TG_BOT_TOKEN', '').strip()
if not token:
    raise SystemExit('Добавьте TG_BOT_TOKEN в .env')
response = requests.get(f'https://api.telegram.org/bot{token}/getUpdates', timeout=20).json()
if not response.get('ok'):
    raise SystemExit(f"Telegram: {response.get('description', 'ошибка')}")
chats = {}
for item in response.get('result', []):
    obj = item.get('message') or item.get('edited_message') or {}
    chat = obj.get('chat') or {}
    if chat.get('type') == 'private':
        chats[chat['id']] = (chat.get('first_name') or '') + ' ' + (chat.get('last_name') or '')
if not chats:
    print('Пока нет личных сообщений. Напишите /start своему Telegram-боту и запустите этот файл снова.')
else:
    print('Личные чаты, которые писали боту (выберите СВОЙ):')
    for chat_id, name in chats.items():
        print(f'TG_CHAT_ID={chat_id}    {name.strip()}')
