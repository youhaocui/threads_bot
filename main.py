#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Threads API 健康檢查工具
測試純文字發文、圖片上傳、文字＋圖片發文
"""

import os
import requests

TOKEN = os.getenv("THREADS_ACCESS_TOKEN")
USER = os.getenv("THREADS_USER_ID")

def check_text_post():
    print("=== 測試純文字貼文 ===")
    try:
        resp = requests.post(
            f"https://graph.threads.net/v1.0/{USER}/threads",
            json={"text": "API 健康檢查：純文字測試"},
            headers={"Authorization": f"Bearer {TOKEN}"}
        )
        print(resp.status_code, resp.text)
    except Exception as e:
        print("錯誤:", e)

def check_image_upload():
    print("\n=== 測試圖片上傳 ===")
    try:
        resp = requests.post(
            f"https://graph.threads.net/v1.0/{USER}/threads_media",
            json={
                "media_type": "IMAGE",
                "image_url": "https://example.com/test.png"  # 建議用一張簡單公開圖片
            },
            headers={"Authorization": f"Bearer {TOKEN}"}
        )
        print(resp.status_code, resp.text)
        if resp.ok:
            return resp.json().get("id")
    except Exception as e:
        print("錯誤:", e)
    return None

def check_text_with_image(media_id):
    print("\n=== 測試文字＋圖片貼文 ===")
    try:
        payload = {"text": "API 健康檢查：文字＋圖片測試"}
        if media_id:
            payload["media_ids"] = [media_id]
        resp = requests.post(
            f"https://graph.threads.net/v1.0/{USER}/threads",
            json=payload,
            headers={"Authorization": f"Bearer {TOKEN}"}
        )
        print(resp.status_code, resp.text)
    except Exception as e:
        print("錯誤:", e)

if __name__ == "__main__":
    print("開始 Threads API 健康檢查...")
    check_text_post()
    media_id = check_image_upload()
    check_text_with_image(media_id)
