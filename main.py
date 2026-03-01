import os
import time
import re
import requests
import xml.etree.ElementTree as ET
from dotenv import load_dotenv

# 加載 .env 檔案
load_dotenv()

# 配置區 (從 .env 讀取)
META_USER_ACCESS_TOKEN = os.getenv("THREADS_ACCESS_TOKEN")
THREADS_USER_ID = os.getenv("THREADS_USER_ID")
LAST_ALERT_FILE = "last_alert.txt"

# 警報圖片編號對照表
ALERT_MAP = {
    "颱風": "W21", "雨": "W26", "強風": "W25", "低溫": "W28",
    "高溫": "W30", "濃霧": "W23", "長浪": "W29", "雷雨": "W27", "地震": "W64"
}

def clean_text(s: str) -> str:
    """徹底壓平文字，將換行符號替換為空格，確保不出現 \n\n"""
    if not s: return ""
    s = s.replace("\\n", " ").replace("\n", " ").replace("\r", " ")
    return re.sub(r"\s+", " ", s).strip()

def get_image_url(title, description):
    """根據警報類型決定圖片網址 (支援地震動態 ID)"""
    # 1. 地震報告：抓取 EC 開頭的動態編號圖
    if "地震" in title:
        match = re.search(r'EC\d{13,}', description)
        if match:
            eq_id = match.group(0)
            v_time = time.strftime("%Y%m%d%H%M%S")
            return f"https://www.cwa.gov.tw/Data/earthquake/img/{eq_id}_H.png?v={v_time}"
        return "https://www.cwa.gov.tw/Data/warning/W64_C.png"

    # 2. 一般天氣警報：比對關鍵字
    for keyword, code in ALERT_MAP.items():
        if keyword in title:
            return f"https://www.cwa.gov.tw/Data/warning/{code}_C.png"
    
    # 預設回傳強風特報圖
    return "https://www.cwa.gov.tw/Data/warning/W25_C.png"

def post_to_threads(text, image_url):
    """Threads API 兩階段發布邏輯 (Container -> Publish)"""
    base_url = "https://graph.threads.net/v1.0"
    auth = {"access_token": META_USER_ACCESS_TOKEN}
    
    try:
        # 第一階段：建立媒體容器
        payload = {
            "text": text,
            "media_type": "IMAGE",
            "image_url": image_url
        }
        res = requests.post(f"{base_url}/{THREADS_USER_ID}/threads", params=auth, data=payload, timeout=20)
        
        if res.status_code == 400:
            print("\n⚠️ 偵測到 Token 已過期或無效！請更新 .env 檔案中的 THREADS_ACCESS_TOKEN。\n")
            return False
            
        res.raise_for_status()
        creation_id = res.json().get("id")

        # 第二階段：正式發布
        requests.post(f"{base_url}/{THREADS_USER_ID}/threads_publish", 
                      params=auth, data={"creation_id": creation_id}, timeout=20)
        
        print(f"[{time.strftime('%H:%M:%S')}] ✅ 成功發布新貼文！")
        return True
    except Exception as e:
        print(f"❌ Threads API 錯誤：{e}")
        return False

def monitor():
    """監控氣象署 RSS 並執行發布邏輯"""
    rss_url = "https://www.cwa.gov.tw/rss/Data/cwa_warning.xml"
    try:
        resp = requests.get(rss_url, timeout=15)
        resp.encoding = 'utf-8'
        root = ET.fromstring(resp.content)
        item = root.find(".//item")
        
        if item is not None:
            title = item.findtext("title")
            desc_raw = item.findtext("description")
            link = item.findtext("link")
            
            # 文字清洗與圖片準備
            c_title = clean_text(title)
            c_desc = clean_text(desc_raw)
            img_url = get_image_url(c_title, desc_raw)
            
            # 組合最終訊息：標題 + 內容 + 網址 (全部黏在一起)
            full_msg = f"⚠️ {c_title} {c_desc} {link}"

            # 讀取舊紀錄防止重複發文
            last_msg = ""
            if os.path.exists(LAST_ALERT_FILE):
                with open(LAST_ALERT_FILE, "r", encoding="utf-8") as f:
                    last_msg = f.read().strip()
            
            if full_msg != last_msg:
                print(f"[{time.strftime('%H:%M:%S')}] 偵測到新變動：{c_title}")
                if post_to_threads(full_msg, img_url):
                    # 只有發送成功才更新紀錄檔
                    with open(LAST_ALERT_FILE, "w", encoding="utf-8") as f:
                        f.write(full_msg)
            else:
                print(f"[{time.strftime('%H:%M:%S')}] 監控中...目前無新警報")
                
    except Exception as e:
        print(f"RSS 解析錯誤：{e}")

if __name__ == "__main__":
    if not META_USER_ACCESS_TOKEN:
        print("❌ 錯誤：找不到 Token。請確認 .env 檔案已正確設定。")
    else:
        print("------------------------------------------")
        print("🚀 台灣氣象機器人 (全警報支援版) 啟動中...")
        print("文字規則：壓平排版，不使用 \\n\\n")
        print("圖片規則：動態對應 W21-W64 或地震圖")
        print("------------------------------------------")
        while True:
            monitor()
            time.sleep(600) # 每 10 分鐘檢查一次