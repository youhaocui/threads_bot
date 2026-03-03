#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.py
從中央氣象署 OPML / RSS 讀取天氣與警特報，處理天氣分區發文、警報逐筆發文（含長浪判斷與圖片）、地震處理、發文紀錄與錯誤處理。
部署時請以環境變數或 config.json 提供實際 API 設定（THREADS_API_ENDPOINT / THREADS_ACCESS_TOKEN）。
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

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ---------- Config ----------
OPML_PATH = os.getenv("OPML_PATH", "https://www.cwa.gov.tw/rss/channel.opml")
FORCE_WARNINGS_RSS = os.getenv("FORCE_WARNINGS_RSS", "https://www.cwa.gov.tw/rss/Data/cwa_warning.xml")
RECORD_FILE = os.getenv("RECORD_FILE", "posted_records.json")
GREETING_STATE_FILE = os.getenv("GREETING_STATE_FILE", "greeting_state.json")
POST_INTERVAL_SECONDS = int(os.getenv("POST_INTERVAL_SECONDS", "180"))
MAX_SINGLE_POST_CHARS = int(os.getenv("MAX_SINGLE_POST_CHARS", "500"))
USER_TIMEZONE = timezone(timedelta(hours=8))

# Threads API config (set these in your environment)
# NOTE: Do NOT set THREADS_API_ENDPOINT to a custom value like /posts; we build endpoints from USER_ID.
THREADS_ACCESS_TOKEN = os.getenv("THREADS_ACCESS_TOKEN", "")
THREADS_USER_ID = os.getenv("THREADS_USER_ID", "")

# 長浪圖片（可用環境變數覆蓋）
SURGE_IMAGE_URL = os.getenv("SURGE_IMAGE_URL", "https://www.cwa.gov.tw/Data/warning/Surge_Swell/Swell_MapTaiwan02.png?v=2026030319-2")

# ---------- Region mapping ----------
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

# ---------- Persistence helpers ----------
def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def save_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.error("保存 JSON 失敗: %s", e)

posted_records = load_json(RECORD_FILE, {"warnings": {}, "posts": {}})
greeting_state = load_json(GREETING_STATE_FILE, {"morning": 0, "noon": 0, "night": 0, "surge": 0})

# ---------- OPML parsing ----------
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

# ---------- RSS fetch ----------
def fetch_rss_xml(url, timeout=10):
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return ET.fromstring(resp.content)

def get_items_from_rss(root):
    channel = root.find("channel")
    if channel is None:
        return []
    return channel.findall("item")

# ---------- description segmentation ----------
SEGMENT_HEADERS = ["今日白天", "今晚明晨", "明日白天", "今日", "今晚", "明日"]

def split_description_into_segments(description_html):
    text = unescape(re.sub(r"<[^>]+>", "", description_html)).strip()
    segments = {}
    pattern = r"(今日白天|今晚明晨|明日白天|今日|今晚|明日)"
    parts = re.split(pattern, text)
    if len(parts) <= 1:
        segments["full"] = text
        return segments
    i = 0
    while i < len(parts):
        if parts[i] == "":
            i += 1
            continue
        if re.match(pattern, parts[i]):
            header = parts[i]
            content = parts[i+1] if i+1 < len(parts) else ""
            segments[header] = content.strip()
            i += 2
        else:
            segments.setdefault("pre", "")
            segments["pre"] += parts[i].strip()
            i += 1
    return segments

def choose_segment_by_time(segments, now=None):
    if now is None:
        now = datetime.now(USER_TIMEZONE)
    hour = now.hour
    is_day = 6 <= hour < 18
    if is_day:
        for key in ["今晚明晨", "今晚", "明日白天", "明日", "full"]:
            if key in segments:
                return key, segments[key]
    else:
        for key in ["明日白天", "明日", "今晚明晨", "今晚", "full"]:
            if key in segments:
                return key, segments[key]
    return "full", segments.get("full", "")

# ---------- extract temp and rain ----------
TEMP_RAIN_PATTERN = re.compile(
    r"(?P<city>[\u4e00-\u9fff\w\s\(\)]+)[：:]\s*(?P<temp_low>\d{1,2})\s*[-~]\s*(?P<temp_high>\d{1,2})\s*°?C.*?降雨機率[：:]\s*(?P<rain>\d{1,3})\s*%?"
)

def extract_city_weather_from_text(text):
    results = {}
    for m in TEMP_RAIN_PATTERN.finditer(text):
        city = m.group("city").strip()
        low = m.group("temp_low")
        high = m.group("temp_high")
        rain = m.group("rain")
        results[city] = {"temp": f"{low}-{high}°C", "rain": f"{rain}%"}
    if not results:
        alt_pattern = re.compile(
            r"(?P<city>[\u4e00-\u9fff\w\s\(\)]+)\s+(?P<temp_low>\d{1,2})\s*[~\-]\s*(?P<temp_high>\d{1,2})\s*°?C.*?降雨機率\s*(?P<rain>\d{1,3})\s*%?"
        )
        for m in alt_pattern.finditer(text):
            city = m.group("city").strip()
            low = m.group("temp_low")
            high = m.group("temp_high")
            rain = m.group("rain")
            results[city] = {"temp": f"{low}-{high}°C", "rain": f"{rain}%"}
    return results

# ---------- build region messages ----------
def build_region_messages(city_weather_map, update_time):
    region_msgs = {}
    for region, cities in REGION_MAP.items():
        lines = []
        for city in cities:
            if city in city_weather_map:
                w = city_weather_map[city]
                lines.append(f"{city}：{w['temp']}，降雨機率：{w['rain']}")
        if lines:
            header = f"🌤 今日{region}地區天氣 ({update_time})"
            region_msgs[region] = header + "\n" + "\n".join(lines)
    return region_msgs

# ---------- greeting pick ----------
def pick_greeting(kind=None, now=None):
    if now is None:
        now = datetime.now(USER_TIMEZONE)
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

# ---------- Threads API helpers (文 + 圖片 正確流程) ----------
def _api_headers():
    return {
        "Authorization": f"Bearer {THREADS_ACCESS_TOKEN}"
    }

def _upload_image_return_media_id(image_url):
    """
    上傳圖片到 threads_media，回傳 media container id。
    若失敗回傳 None。
    """
    if not THREADS_USER_ID or not THREADS_ACCESS_TOKEN:
        logging.debug("No THREADS credentials for media upload.")
        return None
    try:
        payload = {
            "media_type": "IMAGE",
            "image_url": image_url
        }
        resp = requests.post(
            f"https://graph.threads.net/v1.0/{THREADS_USER_ID}/threads_media",
            json=payload,
            headers=_api_headers(),
            timeout=20
        )
        resp.raise_for_status()
        data = resp.json()
        media_id = data.get("id") or data.get("media_id")
        logging.debug("Uploaded media %s -> id=%s", image_url, media_id)
        return media_id
    except Exception as e:
        logging.error("upload_media error: %s", e)
        return None

def _create_thread_container(text, media_ids=None):
    """
    建立貼文 container（尚未 publish），回傳 container id。
    """
    if not THREADS_USER_ID or not THREADS_ACCESS_TOKEN:
        logging.debug("No THREADS credentials for create container.")
        return None
    try:
        payload = {"text": text}
        if media_ids:
            payload["media_ids"] = media_ids
        resp = requests.post(
            f"https://graph.threads.net/v1.0/{THREADS_USER_ID}/threads",
            json=payload,
            headers=_api_headers(),
            timeout=20
        )
        resp.raise_for_status()
        data = resp.json()
        container_id = data.get("id") or data.get("container_id")
        logging.debug("Created thread container id=%s", container_id)
        return container_id
    except Exception as e:
        logging.error("create_container error: %s", e)
        return None

def _publish_container(container_id):
    """
    發布 container，回傳最終貼文 id 或 None。
    """
    if not container_id:
        return None
    try:
        resp = requests.post(
            f"https://graph.threads.net/v1.0/{container_id}",
            headers=_api_headers(),
            timeout=20
        )
        resp.raise_for_status()
        data = resp.json()
        post_id = data.get("id") or data.get("post_id")
        logging.debug("Published container %s -> post_id=%s", container_id, post_id)
        return post_id
    except Exception as e:
        logging.error("publish_container error: %s", e)
        return None

def post_to_api(content, attachments=None):
    """
    正確的發文流程（支援文字 + 多張圖片）：
    1) 逐張上傳圖片到 threads_media，取得 media_ids（失敗則跳過該張）
    2) 建立貼文 container（text + media_ids）
    3) publish container
    回傳 dict: {"ok": bool, "id": post_id or container_id or None, "error": str or None}
    """
    # If no credentials, fallback to mock
    if not THREADS_ACCESS_TOKEN or not THREADS_USER_ID:
        logging.info("THREADS credentials not set — using mock post.")
        return {"ok": True, "id": f"mock-{int(time.time())}"}

    media_ids = []
    if attachments:
        for url in attachments:
            if not url:
                continue
            media_id = _upload_image_return_media_id(url)
            if media_id:
                media_ids.append(media_id)
            else:
                logging.warning("某張媒體上傳失敗，將跳過: %s", url)

    # 建立 container
    container_id = _create_thread_container(content, media_ids if media_ids else None)
    if not container_id:
        # 若 container 建立失敗，嘗試只發文字（fallback）
        logging.warning("建立 container 失敗，嘗試以純文字發文")
        container_id = _create_thread_container(content, None)
        if not container_id:
            return {"ok": False, "error": "create_container_failed"}

    # publish
    post_id = _publish_container(container_id)
    if not post_id:
        return {"ok": False, "error": "publish_failed", "id": container_id}

    return {"ok": True, "id": post_id}

# ---------- warnings feed processing (with surge detection) ----------
def process_warnings_feed(warnings_url):
    try:
        root = fetch_rss_xml(warnings_url)
    except Exception as e:
        logging.warning("下載警報 RSS 失敗: %s", e)
        return
    items = get_items_from_rss(root)
    for item in items:
        try:
            title = item.findtext("title", "").strip()
            pubDate = item.findtext("pubDate", "").strip()
            link = item.findtext("link", "").strip()
            description = item.findtext("description", "") or ""
            key = f"{title}||{pubDate}"
            if key in posted_records["warnings"]:
                continue

            desc_plain = unescape(re.sub(r"<[^>]+>", "", description)).strip()
            is_surge = any(k in title for k in ["長浪", "湧浪", "海象"]) or any(k in desc_plain for k in ["長浪", "湧浪", "海象"])

            image_url = None
            if is_surge:
                image_url = SURGE_IMAGE_URL
                greeting = pick_greeting(kind="surge")
            else:
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

            posted_records["warnings"][key] = {
                "posted_at": datetime.now(USER_TIMEZONE).isoformat(),
                "post_id": resp.get("id") if resp.get("ok") else None,
                "error": resp.get("error") if not resp.get("ok") else None
            }
            save_json(RECORD_FILE, posted_records)

            if resp.get("ok"):
                logging.info("已發佈警報: %s", title)
            else:
                logging.error("發文失敗，但已更新紀錄: %s / error: %s", title, resp.get("error"))
        except Exception as e:
            logging.error("處理某筆警報時發生例外: %s", e)
            continue

# ---------- earthquake item processing ----------
def process_earthquake_item(item):
    title = item.findtext("title", "").strip()
    pubDate = item.findtext("pubDate", "").strip()
    link = item.findtext("link", "").strip()
    description = item.findtext("description", "") or ""
    image_url = None
    m = re.search(r'ReportImageURI[:=]\s*(https?://[^\s<]+)', description)
    if m:
        image_url = m.group(1)
    else:
        m2 = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', description)
        if m2:
            image_url = m2.group(1)
    if image_url:
        ts = int(time.time())
        sep = "&" if "?" in image_url else "?"
        image_url = f"{image_url}{sep}v={ts}"
    plain = unescape(re.sub(r"<[^>]+>", "", description)).strip()
    if len(plain) <= MAX_SINGLE_POST_CHARS:
        content = f"🌏 {title}\n\n{plain}\n\n官方連結：{link}"
    else:
        mmax = re.search(r"最大震度[:：]?\s*([^\s，。]+)", plain)
        if mmax:
            max_info = mmax.group(1)
        else:
            max_info = title
        content = f"🌏 {title}\n\n最大震度：{max_info}\n\n詳情請見官方連結：{link}"
    attachments = [image_url] if image_url else None
    resp = post_to_api(content, attachments=attachments)
    key = f"{title}||{pubDate}"
    posted_records["warnings"][key] = {"posted_at": datetime.now(USER_TIMEZONE).isoformat(), "post_id": resp.get("id") if resp.get("ok") else None, "error": resp.get("error") if not resp.get("ok") else None}
    save_json(RECORD_FILE, posted_records)
    if resp.get("ok"):
        logging.info("已發佈地震: %s", title)
    else:
        logging.error("發佈地震失敗，但已更新紀錄: %s", title)

# ---------- weather pipeline ----------
def run_weather_pipeline(opml_path):
    opml = load_opml(opml_path)
    forecast_map = opml.get("今明天氣預報", {})
    city_weather_map = {}
    update_time_str = datetime.now(USER_TIMEZONE).strftime("%m月%d日 %H:%M 更新")
    for city, url in forecast_map.items():
        try:
            root = fetch_rss_xml(url)
            items = get_items_from_rss(root)
            if not items:
                continue
            item = items[0]
            description = item.findtext("description", "") or ""
            segments = split_description_into_segments(description)
            seg_key, seg_text = choose_segment_by_time(segments)
            extracted = extract_city_weather_from_text(seg_text)
            if not extracted:
                extracted = extract_city_weather_from_text(description)
            if city in extracted:
                city_weather_map[city] = extracted[city]
            else:
                if extracted:
                    first_city = next(iter(extracted))
                    city_weather_map[city] = extracted[first_city]
                else:
                    logging.warning("無法從 %s 的 RSS 解析出溫度/降雨資訊", city)
        except Exception as e:
            logging.error("下載或解析 %s RSS 失敗: %s", city, e)
            continue
    region_msgs = build_region_messages(city_weather_map, update_time_str)
    if not region_msgs:
        logging.warning("未取得任何區域天氣資料")
        return
    combined_text = "\n\n".join(region_msgs.values())
    greeting = pick_greeting()
    if len(combined_text) <= MAX_SINGLE_POST_CHARS:
        content = f"{greeting}\n\n{combined_text}\n\n詳細報告：https://www.cwa.gov.tw/..."
        resp = post_to_api(content)
        key = f"weather_all||{datetime.now(USER_TIMEZONE).isoformat()}"
        posted_records["posts"][key] = {"posted_at": datetime.now(USER_TIMEZONE).isoformat(), "post_id": resp.get("id") if resp.get("ok") else None, "error": resp.get("error") if not resp.get("ok") else None}
        save_json(RECORD_FILE, posted_records)
        if resp.get("ok"):
            logging.info("已發佈合併天氣貼文")
        else:
            logging.error("發佈合併天氣貼文失敗，但已更新紀錄")
    else:
        for region, msg in region_msgs.items():
            content = f"{greeting}\n\n{msg}\n\n詳細報告：https://www.cwa.gov.tw/..."
            resp = post_to_api(content)
            key = f"weather_{region}||{datetime.now(USER_TIMEZONE).isoformat()}"
            posted_records["posts"][key] = {"posted_at": datetime.now(USER_TIMEZONE).isoformat(), "post_id": resp.get("id") if resp.get("ok") else None, "error": resp.get("error") if not resp.get("ok") else None}
            save_json(RECORD_FILE, posted_records)
            if resp.get("ok"):
                logging.info("已發佈 %s 天氣貼文", region)
            else:
                logging.error("發佈 %s 天氣貼文失敗，但已更新紀錄", region)
            time.sleep(POST_INTERVAL_SECONDS)

# ---------- main ----------
def main():
    # 1) 天氣 pipeline
    try:
        run_weather_pipeline(OPML_PATH)
    except Exception as e:
        logging.error("天氣 pipeline 發生未捕捉例外: %s", e)

    # 2) 警特報 pipeline（優先使用 FORCE_WARNINGS_RSS）
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

if __name__ == "__main__":
    main()
