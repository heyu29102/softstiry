import asyncio
import zipfile

import httpx

import config
from panel_control import count_sessions, rm, temp_path
from panel_import import archive_has_session, import_package

api_monitor = {"on": False, "etag": None}


async def api_loop(bot):
    backoff = config.API_CHECK_INTERVAL
    async with httpx.AsyncClient(timeout=30.0) as client:
        while True:
            try:
                if not api_monitor["on"]:
                    await asyncio.sleep(config.API_CHECK_INTERVAL)
                    backoff = config.API_CHECK_INTERVAL
                    continue
                if not config.API_TOKEN:
                    api_monitor["on"] = False
                    for a in config.ADMIN_IDS:
                        try:
                            await bot.send_message(a, "⚠️ API_TOKEN пуст — мониторинг выключен.")
                        except Exception:
                            pass
                    await asyncio.sleep(config.API_CHECK_INTERVAL)
                    continue
                headers = {"Authorization": f"Bearer {config.API_TOKEN}"}
                if api_monitor["etag"]:
                    headers["If-None-Match"] = api_monitor["etag"]
                params = {"user_id": config.TARGET_USER_ID, "set_loaded": "true"}
                async with client.stream("GET", f"{config.API_BASE_URL}/api/sessions",
                                         params=params, headers=headers) as resp:
                    if resp.status_code in (204, 304, 404):
                        pass
                    elif resp.status_code == 401:
                        api_monitor["on"] = False
                        for a in config.ADMIN_IDS:
                            await bot.send_message(a, "⚠️ API 401 — мониторинг выключен.")
                    elif resp.status_code == 200:
                        tmp = temp_path(".zip")
                        consumed = False
                        try:
                            with tmp.open("wb") as out:
                                async for chunk in resp.aiter_bytes(65536):
                                    out.write(chunk)
                            etag = resp.headers.get("ETag")
                            if etag:
                                api_monitor["etag"] = etag
                            if tmp.stat().st_size >= 22 and zipfile.is_zipfile(str(tmp)) and archive_has_session(tmp):
                                consumed = True
                                res = await import_package(f"api_{config.TARGET_USER_ID}.zip", tmp)
                                if res["added"] + res["updated"] > 0:
                                    routed = ""
                                    if res.get("routed_added") or res.get("routed_updated"):
                                        tag = "intl" if config.SESSION_ROUTE_ROLE in ("ru", "cis", "rushki") else "RU"
                                        routed = (
                                            f" | {tag}: +{res.get('routed_added', 0)} "
                                            f"/ обн. {res.get('routed_updated', 0)}"
                                        )
                                    for a in config.ADMIN_IDS:
                                        await bot.send_message(
                                            a,
                                            f"🌐 API: +{res['added']} сесс. / обновлено {res['updated']}. "
                                            f"Всего сессий: {count_sessions()}{routed}",
                                        )
                        finally:
                            if not consumed:
                                rm(tmp)
                    else:
                        print(f"API: HTTP {resp.status_code}")
                backoff = config.API_CHECK_INTERVAL
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print("api_loop:", e)
                backoff = min(backoff * 2, 300)
            await asyncio.sleep(backoff)
