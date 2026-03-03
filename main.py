#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.py
修正版：
- 使用 Threads 簡化流程 (POST /{user_id}/threads + POST /{user_id}/threads_publish)
- 啟動時避免重複發布舊 item（比對 posted_records 並只處理近期 item）
- 只要有圖片就帶 media_type=IMAGE
- 支援中央氣象署地震 JSON API（若設定 EARTHQUAKE_JSON_URL）
- 改良天氣正則以匹配 "溫度: 15 ~ 20 降雨機率: 30%"
"""

import os
import re
import json
import time
import logging
import requests
import xml.etree.ElementTree as ET
from html import unescape
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ---------- Config (env) ----------
OPML_PATH = os.getenv("OPML_PATH", "https://www.cwa.gov.tw/rss/channel.opml")
FORCE_WARNINGS_RSS = os.getenv("FORCE_WARNINGS_RSS", "https://www.cwa.gov.tw/rss/Data/cwa_warning.xml")
RECORD_FILE = os.getenv("RECORD_FILE", "posted_records.json")
GREETING_STATE_FILE = os.getenv("GREETING_STATE_FILE", "greeting_state.json")
POST_INTERVAL_SECONDS = int(os.getenv("POST_INTERVAL_SECONDS", "3"))
MAX_SINGLE_POST_CHARS = int(os.getenv("MAX_SINGLE_POST_CHARS", "500"))
USER_TIMEZONE = timezone(timedelta(hours=8))
THREADS_ACCESS_TOKEN = os.getenv("THREADS_ACCESS_TOKEN", "")
THREADS_USER_ID = os.getenv("THREADS_USER_ID", "")
SURGE_IMAGE_URL = os.getenv("SURGE_IMAGE_URL", "https://www.cwa.gov.tw/Data/warning/Surge_Swell/Swell_MapTaiwan02.png")
EARTHQUAKE_JSON_URL = os.getenv("EARTHQUAKE_JSON_URL", "")  # optional: e.g. https://data.cwb.gov.tw/.../E-A0015-001.json
RECENT_HOURS = int(os.getenv("RECENT_HOURS", "24"))  # only process items within this many hours

# ---------- Persistence helpers ----------
def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception as e:
        logging.warning("讀取 JSON 失敗 (%s)，使用預設: %s", path, e)
        return default

def save_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.error("保存 JSON 失敗: %s", e)

posted_records = load_json(RECORD_FILE, {"warnings": {}, "posts": {}, "last_run": None})
greeting_state = load_json(GREETING_STATE_FILE, {"morning": 0, "noon": 0, "night": 0, "surge": 0})

# ---------- Utility ----------
def now_local():
    return datetime.now(USER_TIMEZONE)

def parse_rss_pubdate(pubdate_str):
    try:
        dt = parsedate_to_datetime(pubdate_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(USER_TIMEZONE)
    except Exception:
        return None

# ---------- OPML / RSS helpers ----------
def load_opml(opml_path_or_url):
    try:
        if opml_path_or_url.startswith("http://") or opml_path_or_url.startswith("https://"):
            resp = requests.get(opml_path_or_url, timeout=15)
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
        else:
            tree = ET.parse(opml_path_or_url)
            root = tree.getroot()
    except Exception as e:
        raise RuntimeError(f"載入 OPML 失敗: {e}")

    result = {}
    body = root.find("body")
    if body is None:
        return result

    for outline in body.findall("outline"):
        title = outline.attrib.get("title", "") or outline.attrib.get("text", "")
        children = {}
        for child in outline.findall("outline"):
            xmlUrl = child.attrib.get("xmlUrl")
            text = child.attrib.get("text") or child.attrib.get("title")
            if xmlUrl and text:
                children[text] = xmlUrl
            else:
                for sub in child.findall("outline"):
                    xmlUrl = sub.attrib.get("xmlUrl")
                    text = sub.attrib.get("text") or sub.attrib.get("title")
                    if xmlUrl and text:
                        children[text] = xmlUrl
        result[title] = children
    return result

def fetch_rss_xml(url, timeout=10):
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return ET.fromstring(resp.content)

def get_items_from_rss(root):
    channel = root.find("channel")
    if channel is None:
        return []
    return channel.findall("item")

# ---------- Text / parsing helpers ----------
def clean_text(s: str) -> str:
    if not s:
        return ""
    s = s.replace("\\n", " ").replace("\n", " ").replace("\r", " ")
    return re.sub(r"\s+", " ", s).strip()

# 改良正則：支援 "溫度: 15 ~ 20 降雨機率: 30%"
TEMP_RAIN_PATTERN = re.compile(
    r"(?P<city>[\u4e00-\u9fff\w\s\(\)]+).*?溫度[:：]\s*(?P<temp_low>\d{1,2})\s*[~\-]\s*(?P<temp_high>\d{1,2}).*?降雨機率[:：]\s*(?P<rain>\d{1,3})\s*%?",
    re.S
)

def extract_city_weather_from_text(text):
    results = {}
    for m in TEMP_RAIN_PATTERN.finditer(text):
        city = m.group("city").strip()
        low = m.group("temp_low")
        high = m.group("temp_high")
        rain = m.group("rain")
        results[city] = {"temp": f"{low}-{high}°C", "rain": f"{rain}%"}
    # fallback: try simpler pattern "城市 溫度: 15 ~ 20"
    if not results:
        alt = re.compile(r"(?P<city>[\u4e00-\u9fff\w\s\(\)]+)\s+溫度[:：]\s*(?P<low>\d{1,2})\s*[~\-]\s*(?P<high>\d{1,2})", re.S)
        for m in alt.finditer(text):
            city = m.group("city").strip()
            low = m.group("low")
            high = m.group("high")
            results[city] = {"temp": f"{low}-{high}°C", "rain": "N/A"}
    return results

# ---------- Greetings ----------
REGION_MAP = {
    "北部": ["臺北市", "新北市", "基隆市", "桃園市", "新竹市", "新竹縣", "宜蘭縣"],
    "中部": ["苗栗縣", "臺中市", "彰化縣", "南投縣", "雲林縣"],
    "南部": ["嘉義市", "嘉義縣", "臺南市", "高雄市", "屏東縣"],
    "東部": ["花蓮縣", "臺東縣"],
    "外島": ["澎湖縣", "金門縣", "連江縣"]
}

GREETINGS = {
    "morning": ["🌞 早安，祝你有美好的一天！", "🌞 早安，保持愉快心情！"],
    "noon": ["☀️ 午安，外出請注意天氣變化！", "☀️ 午安，祝你工作順利！"],
    "night": ["🌙 晚安，注意保暖，祝你一夜好眠！", "🌙 晚安，保持安全，平安入睡！"],
    "surge": [
        "🌊 海面湧浪強，外出活動請注意安全！",
        "🌊 長浪來襲，請遠離海邊，保護自己與家人！",
        "🌊 海象不佳，避免下水，保持警覺與平安！"
    ]
}

def pick_greeting(kind=None, now=None):
    if now is None:
        now = now_local()
    if kind is None:
        hour = now.hour
        if 6 <= hour < 12:
            key = "morning"
        elif 12 <= hour < 18:
            key = "noon"
        else:
            key = "night"
    else:
        key = kind if kind in GREETINGS else "morning"
    idx = greeting_state.get(key, 0) % len(GREETINGS[key])
    greeting = GREETINGS[key][idx]
    greeting_state[key] = idx + 1
    save_json(GREETING_STATE_FILE, greeting_state)
    return greeting

# ---------- Threads API (simplified two-step) ----------
BASE_THREADS = "https://graph.threads.net/v1.0"

def _auth_params():
    return {"access_token": THREADS_ACCESS_TOKEN}

def _create_creation(text, image_url=None):
    if not THREADS_USER_ID or not THREADS_ACCESS_TOKEN:
        logging.debug("Threads credentials not set for create_creation.")
        return None, "no_credentials"

    payload = {"text": text}
    if image_url:
        # **關鍵**：只要有圖片就一定帶 media_type
        payload["media_type"] = "IMAGE"
        payload["image_url"] = image_url

    try:
        resp = requests.post(
            f"{BASE_THREADS}/{THREADS_USER_ID}/threads",
            params=_auth_params(),
            data=payload,
            timeout=20
        )
        if not resp.ok:
            logging.error("create_creation error: %s %s", resp.status_code, resp.text)
            return None, resp.text
        data = resp.json()
        creation_id = data.get("id") or data.get("creation_id")
        logging.info("建立 creation 成功 id=%s", creation_id)
        return creation_id, None
    except Exception as e:
        logging.error("create_creation exception: %s", e)
        return None, str(e)

def _publish_creation(creation_id):
    if not THREADS_USER_ID or not THREADS_ACCESS_TOKEN:
        logging.debug("Threads credentials not set for publish_creation.")
        return None, "no_credentials"

    try:
        resp = requests.post(
            f"{BASE_THREADS}/{THREADS_USER_ID}/threads_publish",
            params=_auth_params(),
            data={"creation_id": creation_id},
            timeout=20
        )
        if not resp.ok:
            logging.error("publish_creation error: %s %s", resp.status_code, resp.text)
            return None, resp.text
        data = resp.json()
        post_id = data.get("id") or data.get("post_id")
        logging.info("publish success post_id=%s", post_id)
        return post_id, None
    except Exception as e:
        logging.error("publish_creation exception: %s", e)
        return None, str(e)

def post_to_api(content, attachments=None):
    if not THREADS_ACCESS_TOKEN or not THREADS_USER_ID:
        logging.info("THREADS credentials not set — using mock post.")
        return {"ok": True, "id": f"mock-{int(time.time())}"}

    image_url = None
    if attachments:
        for a in attachments:
            if a:
                image_url = a
                break

    creation_id, err = _create_creation(content, image_url=image_url)
    if not creation_id:
        return {"ok": False, "id": None, "error": err}

    post_id, err2 = _publish_creation(creation_id)
    if not post_id:
        return {"ok": False, "id": creation_id, "error": err2}

    return {"ok": True, "id": post_id}

# ---------- Warnings feed processing (robust) ----------
def is_recent(pub_dt):
    if not pub_dt:
        return False
    cutoff = now_local() - timedelta(hours=RECENT_HOURS)
    return pub_dt >= cutoff

def process_warnings_feed(warnings_url):
    try:
        root = fetch_rss_xml(warnings_url)
    except Exception as e:
        logging.warning("下載警報 RSS 失敗: %s", e)
        return

    items = get_items_from_rss(root)
    logging.info("警報 RSS 共 %d 筆 item，已記錄 %d 筆", len(items), len(posted_records.get("warnings", {})))
    for item in items:
        try:
            title_raw = item.findtext("title", "") or ""
            title = clean_text(title_raw)
            pubDate_raw = item.findtext("pubDate", "") or ""
            pub_dt = parse_rss_pubdate(pubDate_raw)
            link = item.findtext("link", "") or ""
            description = item.findtext("description", "") or ""
            key = f"{title}||{pubDate_raw}"

            # skip if already posted
            if key in posted_records.get("warnings", {}):
                logging.debug("已發佈，跳過: %s", title)
                continue

            # skip if too old (avoid re-posting on startup)
            if not is_recent(pub_dt):
                logging.debug("跳過舊的 item (不在最近 %d 小時): %s", RECENT_HOURS, title)
                continue

            desc_plain = unescape(re.sub(r"<[^>]+>", "", description)).strip()
            is_surge = any(k in title for k in ["長浪", "湧浪", "海象"]) or any(k in desc_plain for k in ["長浪", "湧浪", "海象"])

            image_url = None
            if is_surge:
                image_url = SURGE_IMAGE_URL
                greeting = pick_greeting(kind="surge")
            else:
                # try to find image in description or enclosure
                m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', description)
                if m:
                    image_url = m.group(1)
                enc = item.find("enclosure")
                if enc is not None and enc.attrib.get("url"):
                    image_url = enc.attrib.get("url")
                greeting = pick_greeting()

            if image_url:
                ts = int(time.time())
                sep = "&" if "?" in image_url else "?"
                image_url = f"{image_url}{sep}v={ts}"

            content = f"{greeting}\n\n🚨 {title}\n\n{desc_plain}\n\n官方連結：{link}"
            attachments = [image_url] if image_url else None

            resp = post_to_api(content, attachments=attachments)

            posted_records.setdefault("warnings", {})[key] = {
                "posted_at": now_local().isoformat(),
                "post_id": resp.get("id") if resp.get("ok") else None,
                "error": resp.get("error") if not resp.get("ok") else None
            }
            save_json(RECORD_FILE, posted_records)

            if resp.get("ok"):
                logging.info("已發佈警報: %s", title)
            else:
                logging.error("發文失敗，但已更新紀錄: %s / error: %s", title, resp.get("error"))

            time.sleep(POST_INTERVAL_SECONDS)
        except Exception as e:
            logging.error("處理某筆警報時發生例外: %s", e)
            continue

# ---------- Earthquake JSON processing (optional) ----------
def process_earthquake_json_url(json_url):
    if not json_url:
        return
    try:
        resp = requests.get(json_url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logging.warning("下載地震 JSON 失敗: %s", e)
        return

    # data structure may vary; try to find records
    records = data.get("records") or data.get("result") or data.get("Earthquake") or []
    # if top-level has Earthquake list under records->Earthquake
    if isinstance(records, dict) and "Earthquake" in records:
        records = records["Earthquake"]

    for rec in records:
        try:
            # prefer ReportContent and ReportImageURI if present
            report_content = rec.get("ReportContent") or rec.get("ReportRemark") or rec.get("ReportContent")
            report_image = rec.get("ReportImageURI") or rec.get("ReportImageURI") or rec.get("ShakemapImageURI")
            web = rec.get("Web") or rec.get("web") or ""
            eq_no = rec.get("EarthquakeNo") or rec.get("EarthquakeID") or str(time.time())
            title = rec.get("ReportType", "地震報告")
            key = f"{title}||{eq_no}"
            if key in posted_records.get("warnings", {}):
                logging.debug("地震已發佈，跳過: %s", key)
                continue

            content = f"🌏 {title}\n\n{report_content}\n\n官方連結：{web}"
            attachments = None
            if report_image:
                ts = int(time.time())
                sep = "&" if "?" in report_image else "?"
                attachments = [f"{report_image}{sep}v={ts}"]

            resp = post_to_api(content, attachments=attachments)
            posted_records.setdefault("warnings", {})[key] = {
                "posted_at": now_local().isoformat(),
                "post_id": resp.get("id") if resp.get("ok") else None,
                "error": resp.get("error") if not resp.get("ok") else None
            }
            save_json(RECORD_FILE, posted_records)

            if resp.get("ok"):
                logging.info("已發佈地震: %s", key)
            else:
                logging.error("發佈地震失敗，但已更新紀錄: %s / error: %s", key, resp.get("error"))
            time.sleep(POST_INTERVAL_SECONDS)
        except Exception as e:
            logging.error("處理地震紀錄時發生例外: %s", e)
            continue

# ---------- Weather pipeline ----------
def build_region_messages(city_weather_map, update_time):
    region_msgs = {}
    for region, cities in REGION_MAP.items():
        lines = []
        for city in cities:
            if city in city_weather_map:
                w = city_weather_map[city]
                lines.append(f"{city}：{w['temp']}，降雨機率：{w.get('rain','N/A')}")
        if lines:
            header = f"🌤 今日{region}地區天氣 ({update_time})"
            region_msgs[region] = header + "\n" + "\n".join(lines)
    return region_msgs

def run_weather_pipeline(opml_path):
    try:
        opml = load_opml(opml_path)
    except Exception as e:
        logging.error("載入 OPML 失敗: %s", e)
        return

    forecast_map = opml.get("今明天氣預報", {})
    if not forecast_map:
        logging.warning("OPML 中找不到 今明天氣預報")
        return

    city_weather_map = {}
    update_time_str = now_local().strftime("%m月%d日 %H:%M 更新")
    for city, url in forecast_map.items():
        try:
            root = fetch_rss_xml(url)
            items = get_items_from_rss(root)
            if not items:
                continue
            item = items[0]
            description = item.findtext("description", "") or ""
            segments_text = unescape(re.sub(r"<[^>]+>", "", description)).strip()
            extracted = extract_city_weather_from_text(segments_text)
            if extracted and city in extracted:
                city_weather_map[city] = extracted[city]
            else:
                # fallback: if any extracted, pick first; else use raw description as fallback
                if extracted:
                    first_city = next(iter(extracted))
                    city_weather_map[city] = extracted[first_city]
                else:
                    # fallback to raw short summary
                    plain = segments_text.strip()
                    if plain:
                        city_weather_map[city] = {"temp": "N/A", "rain": "N/A"}
                    else:
                        logging.debug("無法從 %s 的 RSS 解析出溫度/降雨資訊", city)
        except Exception as e:
            logging.error("下載或解析 %s RSS 失敗: %s", city, e)
            continue

    region_msgs = build_region_messages(city_weather_map, update_time_str)
    if not region_msgs:
        logging.warning("未取得任何區域天氣資料")
        return

    greeting = pick_greeting()
    combined_text = "\n\n".join(region_msgs.values())
    if len(combined_text) <= MAX_SINGLE_POST_CHARS:
        content = f"{greeting}\n\n{combined_text}\n\n詳細報告：https://www.cwa.gov.tw/"
        resp = post_to_api(content)
        key = f"weather_all||{now_local().isoformat()}"
        posted_records.setdefault("posts", {})[key] = {"posted_at": now_local().isoformat(), "post_id": resp.get("id") if resp.get("ok") else None, "error": resp.get("error") if not resp.get("ok") else None}
        save_json(RECORD_FILE, posted_records)
        if resp.get("ok"):
            logging.info("已發佈合併天氣貼文")
        else:
            logging.error("發佈合併天氣貼文失敗，但已更新紀錄")
    else:
        for region, msg in region_msgs.items():
            content = f"{greeting}\n\n{msg}\n\n詳細報告：https://www.cwa.gov.tw/"
            resp = post_to_api(content)
            key = f"weather_{region}||{now_local().isoformat()}"
            posted_records.setdefault("posts", {})[key] = {"posted_at": now_local().isoformat(), "post_id": resp.get("id") if resp.get("ok") else None, "error": resp.get("error") if not resp.get("ok") else None}
            save_json(RECORD_FILE, posted_records)
            if resp.get("ok"):
                logging.info("已發佈 %s 天氣貼文", region)
            else:
                logging.error("發佈 %s 天氣貼文失敗，但已更新紀錄", region)
            time.sleep(POST_INTERVAL_SECONDS)

# ---------- main ----------
def main():
    logging.info("啟動，已載入已發佈紀錄數: %d warnings, %d posts", len(posted_records.get("warnings", {})), len(posted_records.get("posts", {})))

    # 1) optional: earthquake JSON feed
    if EARTHQUAKE_JSON_URL:
        logging.info("嘗試處理地震 JSON: %s", EARTHQUAKE_JSON_URL)
        process_earthquake_json_url(EARTHQUAKE_JSON_URL)

    # 2) weather pipeline
    try:
        run_weather_pipeline(OPML_PATH)
    except Exception as e:
        logging.error("天氣 pipeline 發生未捕捉例外: %s", e)

    # 3) warnings pipeline (use FORCE_WARNINGS_RSS if set)
    warnings_feed = FORCE_WARNINGS_RSS or None
    if not warnings_feed:
        try:
            opml = load_opml(OPML_PATH)
            warnings_map = opml.get("警報、特報", {})
            warnings_feed = next(iter(warnings_map.values()), None)
        except Exception as e:
            logging.warning("嘗試從 OPML 取得警報 RSS 失敗: %s", e)
            warnings_feed = None

    if warnings_feed:
        try:
            process_warnings_feed(warnings_feed)
        except Exception as e:
            logging.error("警特報 pipeline 發生未捕捉例外: %s", e)
    else:
        logging.warning("OPML 中找不到 警報、特報 RSS")

    # update last_run
    posted_records["last_run"] = now_local().isoformat()
    save_json(RECORD_FILE, posted_records)
    logging.info("結束執行")

if __name__ == "__main__":
    main()
