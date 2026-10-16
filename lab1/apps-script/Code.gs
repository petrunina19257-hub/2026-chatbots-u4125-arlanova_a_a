// Task Hunter — Telegram-бот для задач на Google Apps Script.
// Скрипт привязан к Google Таблице. Триггер по времени запускает pollTelegram,
// функция забирает новые сообщения из Telegram и записывает команды /task в таблицу.

const TELEGRAM_TOKEN = 'XXX'; // настоящий токен от @BotFather не публикуется
const SPREADSHEET_ID = 'XXX'; // ID таблицы из её ссылки, не публикуется

function pollTelegram() {
  const props = PropertiesService.getScriptProperties();
  const lastUpdateId = Number(props.getProperty('last_update_id') || '0');

  const url = 'https://api.telegram.org/bot' + TELEGRAM_TOKEN + '/getUpdates?timeout=0&offset=' + (lastUpdateId + 1);
  const response = UrlFetchApp.fetch(url);
  const data = JSON.parse(response.getContentText());

  if (!data.ok || !data.result.length) {
    return;
  }

  let maxUpdateId = lastUpdateId;

  data.result.forEach(function(update) {
    maxUpdateId = Math.max(maxUpdateId, update.update_id);

    const message = update.message;
    if (!message || !message.text) {
      return;
    }

    const text = message.text.trim();

    // Берем только команды задачи:
    // /task текст
    // /task@botname текст
    // / task текст
    const taskMatch = text.match(/^\/\s*task(?:@\w+)?\s+([\s\S]+)/i);

    if (!taskMatch) {
      return;
    }

    const taskText = taskMatch[1].trim();

    if (!taskText) {
      return;
    }

    writeTask(message, taskText);
  });

  props.setProperty('last_update_id', String(maxUpdateId));
}

function writeTask(message, taskText) {
  const user = message.from || {};
  const sheet = SpreadsheetApp.openById(SPREADSHEET_ID).getSheets()[0];

  sheet.appendRow([
    new Date(),
    message.chat.title || message.chat.first_name || '',
    [user.first_name, user.last_name].filter(Boolean).join(' '),
    user.username ? '@' + user.username : '',
    taskText,
    'new'
  ]);
}

function resetTelegramOffset() {
  PropertiesService.getScriptProperties().deleteProperty('last_update_id');
}
