# Jitsi Meet Recorder (Windows)

Автор: **Alexander Wizard**

Записывает аудио звонка Jitsi Meet в MP3. Приложение через headless Chromium
(Playwright) заходит в комнату по ссылке как участник (микрофон/камера
выключены), микширует удалённое аудио через Web Audio API + MediaRecorder и
транскодирует результат в MP3 через ffmpeg. Управление — по локальному REST API.

> ⚠️ **Только для законной записи.** Используйте для собственных встреч или
> встреч, где участники уведомлены о записи. Скрытая запись без согласия
> участников во многих странах незаконна.

## Требования
- Windows 10/11, Python 3.11+
- ffmpeg в PATH

## Установка
```powershell
cd W:\NET\Recorder
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
```

## Запуск сервера
```powershell
# опционально: токен доступа и адрес
$env:RECORDER_TOKEN = "секрет"
$env:RECORDER_HOST  = "127.0.0.1"   # 0.0.0.0 чтобы управлять из локальной сети
$env:RECORDER_PORT  = "8080"
python server.py
```

## Управление (REST)
```powershell
$H = @{ "X-Token" = "секрет" }

# начать запись
Invoke-RestMethod -Method Post http://127.0.0.1:8080/start -Headers $H `
  -ContentType application/json `
  -Body '{"url":"https://meet.jit.si/МояКомната","name":"Recorder"}'

# статус
Invoke-RestMethod http://127.0.0.1:8080/status -Headers $H

# остановить
Invoke-RestMethod -Method Post http://127.0.0.1:8080/stop -Headers $H

# список и скачивание mp3
Invoke-RestMethod http://127.0.0.1:8080/recordings -Headers $H
Invoke-WebRequest http://127.0.0.1:8080/download/ИМЯ.mp3 -Headers $H -OutFile out.mp3
```

Готовые файлы лежат в `recordings/`.

## Как это работает
- `server.py` — REST API (FastAPI), один сеанс записи за раз.
- `recorder.py` — Playwright: запуск Chromium, заход в комнату, приём аудио-чанков, транскод в MP3.
- `capture.js` — инъекция в страницу: находит все `<audio>/<video>` участников, миксует их в один поток и пишет через `MediaRecorder`.

## Ограничения
- Пишется только **входящее** (удалённое) аудио участников — то, что вы слышите
  в звонке. Голос самого бота не пишется (он замьючен).
- Если комната требует пароль/лобби, бота нужно впустить, либо добавить логику
  ввода пароля в `recorder._prejoin`.
- Флаги `config.*` в URL работают на публичном `meet.jit.si`; на приватных
  инсталляциях набор доступных параметров может отличаться.
