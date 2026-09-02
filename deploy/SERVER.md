# Деплой на сервер (полная версия)

**Важно:** нельзя копировать только `sender.py` / `config.py` — app.py упадёт с exit 1.
Нужен **полный pull** всей ветки.

## Быстрый путь

```bash
cd /opt/new-soft
bash deploy/server-update.sh
```

Ветка по умолчанию: `cursor/min-members-story-rr-c290`

## Вручную

```bash
BACKUP=/tmp/bak-$(date +%F-%H%M)
mkdir -p "$BACKUP"
cp -a /opt/new-soft/{stories.txt,bots.txt,domains.txt,.env} "$BACKUP/"

cd /opt/new-soft
git fetch origin cursor/min-members-story-rr-c290
git reset --hard origin/cursor/min-members-story-rr-c290

cp -a "$BACKUP"/* /opt/new-soft/
```

Перезапуск — **только через панель**.

## Проверка старта

```bash
cd /opt/new-soft
python3 -c "from sender import Spammer; print('ok')"
tail -30 app.log
```

## Что в этой версии

- Stories → ЛС + группы, группы первыми
- `GROUP_MIN_MEMBERS=0` — не отсекать мелкие группы (10 ломало охват)
- `STORY_CONFIRM_IN_CHAT=0` / `STORY_CONFIRM_GROUPS=0` — get_messages после send ломал группы
- Round-robin stories, перемешивание групп каждый круг
- Join в канал **только fallback** (если без подписки story не видна)
- Успех только с `message id` + проверка `get_messages` в чате (нет ложных ✅)
- Мёртвые сессии (`key is not registered`) — меньше спама в логах

## .env — минимум для stories

```
STORY_JOIN_CHANNEL=1
STORY_CONFIRM_IN_CHAT=1
STORY_VERIFY_SESSION=1
STORY_WARM_MODE=full
LOG_SUCCESS_GROUPS=1
TARGET_GROUPS_FIRST=1
GROUP_MIN_MEMBERS=10
```

Шаблон: `deploy/env.production`
