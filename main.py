#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.py
從中央氣象署 OPML / RSS 讀取天氣與警特報，處理天氣分區發文、警報逐筆發文（含長浪判斷與圖片）、地震處理、發文紀錄與錯誤處理。
部署時請以環境變數或 config.json 提供實際 API 設定（THREADS_API_ENDPOINT / THREADS_API_TOKEN）。
"""

import os
import re
import json
import time
import requests
import xml.etree.ElementTree as ET
from html import unescape
from datetime import datetime, timezone, timedelta

# ---------- Config ----------
# 預設使用中央氣象署 OPML 主檔（可被環境變數覆蓋）
OPML_PATH = os.getenv("OPML_PATH", "https://www.cwa.gov.tw/rss/channel.opml")
# 直接指定警特報 RSS（若你要強制直接抓 RSS，可改這個值）
FORCE_WARNINGS_RSS = os.getenv("FORCE_WARNINGS_RSS", "https://www.cwa.gov.tw/rss/Data/cwa_warning.xml")
RECORD_FILE = os.getenv("RECORD_FILE", "posted_records.json")
GREETING_STATE_FILE = os.getenv("GREETING_STATE_FILE", "greeting_state.json")
POST_INTERVAL_SECONDS = int(os.getenv("POST_INTERVAL_SECONDS", "180"))
MAX_SINGLE_POST_CHARS = int(os.getenv("MAX_SINGLE_POST_CHARS", "500"))
USER_TIMEZONE = timezone(timedelta(hours=8))
THREADS_API_ENDPOINT = os.getenv("THREADS_API_ENDPOINT", "")
THREADS_API_TOKEN = os.getenv("THREADS_API_TOKEN", "")

# 長浪圖片（若 RSS item 判定為長浪，會附上此圖）
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
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

posted_records = load_json(RECORD_FILE, {"warnings": {}, "posts": {}})
greeting_state = load_json(GREETING_STATE_FILE, {"morning": 0, "noon": 0, "night": 0, "surge": 0})

# ---------- OPML parsing (support local file or URL) ----------
def load_opml(opml_path_or_url):
    """
    支援本地檔案路徑或 URL。
    回傳 dict: { '今明天氣預報': {city: xmlUrl, ...}, '警報、特報': { ... } }
    會處理一層或兩層 outline 嵌套。
    """
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
# 更寬鬆的 regex，容忍中間雜訊
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

# ---------- greeting pick (supports different keys) ----------
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

# ---------- post to API placeholder ----------
def post_to_api(content, attachments=None):
    """
    請在部署時替換為實際發文實作（Threads/Telegram/Discord 等）。
    回傳格式：{"ok": True, "id": "post-id"} 或 {"ok": False, "error": "..."}
    目前為 mock 回傳，方便測試。
    """
    try:
        # 若要實作真實 API，請在此加入 requests.post 並帶入授權 header
        # 範例（Threads API 假設）：
        # headers = {"Authorization": f"Bearer {THREADS_API_TOKEN}"}
        # resp = requests.post(THREADS_API_ENDPOINT, json={"content": content}, headers=headers, timeout=10)
        # resp.raise_for_status()
        # return {"ok": True, "id": resp.json().get("id")}
        return {"ok": True, "id": f"mock-{int(time.time())}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

# ---------- warnings feed processing (with surge detection) ----------
def process_warnings_feed(warnings_url):
    try:
        root = fetch_rss_xml(warnings_url)
    except Exception as e:
        print(f"[WARN] 下載警報 RSS 失敗: {e}")
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

            # 判斷是否為長浪/海象相關警報（可擴充關鍵字）
            desc_plain = unescape(re.sub(r"<[^>]+>", "", description)).strip()
            is_surge = any(k in title for k in ["長浪", "湧浪", "海象"]) or any(k in desc_plain for k in ["長浪", "湧浪", "海象"])

            image_url = None
            if is_surge:
                image_url = SURGE_IMAGE_URL
                greeting = pick_greeting(kind="surge")
            else:
                # 一般警報抓 RSS 裡的圖片或 enclosure
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
                print(f"[INFO] 已發佈警報: {title}")
            else:
                print(f"[ERROR] 發文失敗，但已更新紀錄: {title} / error: {resp.get('error')}")
        except Exception as e:
            print(f"[ERROR] 處理某筆警報時發生例外: {e}")
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
        print(f"[INFO] 已發佈地震: {title}")
    else:
        print(f"[ERROR] 發佈地震失敗，但已更新紀錄: {title}")

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
            # 若需要 debug description，取消下一行註解
            # print(f"[DEBUG] {city} description:\n{description}\n---")
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
                    print(f"[WARN] 無法從 {city} 的 RSS 解析出溫度/降雨資訊")
        except Exception as e:
            print(f"[ERROR] 下載或解析 {city} RSS 失敗: {e}")
            continue
    region_msgs = build_region_messages(city_weather_map, update_time_str)
    if not region_msgs:
        print("[WARN] 未取得任何區域天氣資料")
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
            print("[INFO] 已發佈合併天氣貼文")
        else:
            print("[ERROR] 發佈合併天氣貼文失敗，但已更新紀錄")
    else:
        for region, msg in region_msgs.items():
            content = f"{greeting}\n\n{msg}\n\n詳細報告：https://www.cwa.gov.tw/..."
            resp = post_to_api(content)
            key = f"weather_{region}||{datetime.now(USER_TIMEZONE).isoformat()}"
            posted_records["posts"][key] = {"posted_at": datetime.now(USER_TIMEZONE).isoformat(), "post_id": resp.get("id") if resp.get("ok") else None, "error": resp.get("error") if not resp.get("ok") else None}
            save_json(RECORD_FILE, posted_records)
            if resp.get("ok"):
                print(f"[INFO] 已發佈 {region} 天氣貼文")
            else:
                print(f"[ERROR] 發佈 {region} 天氣貼文失敗，但已更新紀錄")
            time.sleep(POST_INTERVAL_SECONDS)

# ---------- main ----------
def main():
    # 1) 天氣 pipeline（使用 OPML 的 今明天氣預報）
    try:
        run_weather_pipeline(OPML_PATH)
    except Exception as e:
        print(f"[ERROR] 天氣 pipeline 發生未捕捉例外: {e}")

    # 2) 警特報 pipeline（直接使用 FORCE_WARNINGS_RSS，或從 OPML 取出）
    warnings_feed = None

    # 優先使用強制指定的 RSS（環境變數 FORCE_WARNINGS_RSS）
    if FORCE_WARNINGS_RSS:
        warnings_feed = FORCE_WARNINGS_RSS

    # 若未強制指定，嘗試從 OPML 取出
    if not warnings_feed:
        try:
            opml = load_opml(OPML_PATH)
            warnings_map = opml.get("警報、特報", {})
            # 取第一個 value（通常是 "警報、特報": "https://.../cwa_warning.xml"）
            warnings_feed = next(iter(warnings_map.values()), None)
        except Exception as e:
            print(f"[WARN] 嘗試從 OPML 取得警報 RSS 失敗: {e}")
            warnings_feed = None

    if warnings_feed:
        try:
            process_warnings_feed(warnings_feed)
        except Exception as e:
            print(f"[ERROR] 警特報 pipeline 發生未捕捉例外: {e}")
    else:
        print("[WARN] OPML 中找不到 警報、特報 RSS")

if __name__ == "__main__":
    main()
