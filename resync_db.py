import os
import sqlite3
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# 載入環境設定
load_dotenv()
FOLDER_ID = os.getenv('GDRIVE_FOLDER_ID')
SCOPES = ['https://www.googleapis.com/auth/drive']

if not os.path.exists('token.json'):
    print("❌ 找不到 token.json，無法連線 Google Drive！")
    exit(1)

# 初始化 Google Drive 服務
creds = Credentials.from_authorized_user_file('token.json', SCOPES)
drive_service = build('drive', 'v3', credentials=creds)

# 重新建立 SQLite 資料表
conn = sqlite3.connect('images.db')
cursor = conn.cursor()
cursor.execute('''
    CREATE TABLE IF NOT EXISTS images (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE,
        drive_id TEXT
    )
''')
conn.commit()

print("🔍 正在從 Google Drive 撈取所有圖片清單...")

# 抓取指定資料夾內所有未被丟進垃圾桶的檔案
query = f"'{FOLDER_ID}' in parents and trashed = false"
page_token = None
restored_count = 0

while True:
    response = drive_service.files().list(
        q=query,
        spaces='drive',
        fields='nextPageToken, files(id, name)',
        pageToken=page_token,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True
    ).execute()

    files = response.get('files', [])
    for file in files:
        drive_id = file.get('id')
        name = file.get('name')

        try:
            # 寫入或忽略重複
            cursor.execute(
                "INSERT OR IGNORE INTO images (name, drive_id) VALUES (?, ?)",
                (name, drive_id)
            )
            restored_count += 1
            print(f"  └ 已同步: {name} (ID: {drive_id})")
        except Exception as e:
            print(f"  └ 寫入失敗 {name}: {e}")

    conn.commit()
    page_token = response.get('nextPageToken', None)
    if page_token is None:
        break

conn.close()
print(f"\n✅ 同步完成！共恢復了 {restored_count} 筆資料至 images.db。")
