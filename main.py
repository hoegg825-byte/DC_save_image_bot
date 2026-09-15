import os
import io
import re
import sqlite3
import asyncio
import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv

# Google Drive API 相關套件
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload

# ================= 載入環境變數 =================
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
FOLDER_ID = os.getenv('GDRIVE_FOLDER_ID')

if not TOKEN or not FOLDER_ID:
    raise ValueError("請確認 .env 檔案中已設定 DISCORD_TOKEN 與 GDRIVE_FOLDER_ID！")

# ================= 本地資料庫初始化 (SQLite) =================
conn = sqlite3.connect('images.db', check_same_thread=False)
cursor = conn.cursor()
cursor.execute('''
    CREATE TABLE IF NOT EXISTS images (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE,
        drive_id TEXT
    )
''')
conn.commit()

# ================= Google Drive OAuth 2.0 初始化 =================
SCOPES = ['https://www.googleapis.com/auth/drive']

def init_drive_service():
    """使用個人 OAuth 憑證登入 Google Drive"""
    creds = None
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)
    
    # 若凭證已過期，使用 refresh_token 自動在背景換發新憑證 (雲端可正常執行)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open('token.json', 'w') as token:
            token.write(creds.to_json())
    elif not creds or not creds.valid:
        # 若在無瀏覽器的伺服器環境中且無 token.json
        if not os.path.exists('token.json'):
            raise FileNotFoundError(
                "❌ 伺服器上找不到 'token.json'！\n"
                "因為雲端無桌面瀏覽器，請先在個人電腦執行取得 'token.json' 後，手動上傳至伺服器根目錄。"
            )
        flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
        creds = flow.run_local_server(port=0)
        with open('token.json', 'w') as token:
            token.write(creds.to_json())

    return build('drive', 'v3', credentials=creds)

print("[系統] 正在驗證並初始化 Google Drive 服務...")
drive_service = init_drive_service()
print("[系統] Google Drive 驗證成功！")

# ================= Google Drive 核心函式 (執行緒隔離) =================
def upload_to_drive_sync(file_bytes, filename, mimetype):
    file_metadata = {
        'name': filename,
        'parents': [FOLDER_ID]
    }
    media = MediaIoBaseUpload(io.BytesIO(file_bytes), mimetype=mimetype, resumable=True)
    file = drive_service.files().create(
        body=file_metadata,
        media_body=media,
        fields='id',
        supportsAllDrives=True
    ).execute()
    return file.get('id')

def download_from_drive_sync(drive_id):
    request = drive_service.files().get_media(fileId=drive_id, supportsAllDrives=True)
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        status, done = downloader.next_chunk()
    fh.seek(0)
    return fh

def delete_from_drive_sync(drive_id):
    try:
        drive_service.files().delete(fileId=drive_id, supportsAllDrives=True).execute()
    except Exception as e:
        print(f"[GDrive 警告] 刪除檔案失敗 (ID: {drive_id}): {e}")

# ================= Discord Bot 初始化 =================
class MemeBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True  # 讀取訊息內容必需
        super().__init__(command_prefix='!', intents=intents)

    async def setup_hook(self):
        GUILD_ID = 1443936725385744464
        guild = discord.Object(id=GUILD_ID)

        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        print(f"[系統] 已針對伺服器 {GUILD_ID} 強制即時同步斜線指令！")

bot = MemeBot()

@bot.event
async def on_ready():
    print(f"[系統] 機器人登入成功：{bot.user} (ID: {bot.user.id})")

# ================= 1. 保存功能：回覆訊息標註 @機器人 "檔名" =================
@bot.event
async def on_message(message):
    if message.author.bot:
        return

    # 檢查是否標註機器人
    if bot.user in message.mentions:
        print(f"\n[除錯] 偵測到提及機器人 | 來自: {message.author} | 內容: '{message.content}'")

        # 必須是「回覆 (Reply)」某則訊息
        if message.reference is None:
            await message.reply("傻逼你沒有回復任何訊息")
            return

        # 提取引號中的檔案名稱 (支援英文半形 ""、中文全形 “”、直角引號 「」)
        pattern = r'["“”「](.+?)["“”」]'
        match = re.search(pattern, message.content)
        if not match:
            await message.reply("是不是跟你說了檔案名稱要用雙引號包起來，例如：`@機器人 \"檔案名稱\"`， 傻逼")
            return

        base_name = match.group(1).strip()
        if not base_name:
            await message.reply("檔案名稱不能是空的，傻逼。")
            return

        # 抓取被回覆的原始訊息
        try:
            replied_msg = await message.channel.fetch_message(message.reference.message_id)
        except discord.NotFound:
            await message.reply("找不到原始訊息。")
            return
        except Exception as e:
            await message.reply(f"錯誤：\n{e}")
            return

        # 檢查圖片附件
        images = [att for att in replied_msg.attachments if att.content_type and att.content_type.startswith('image/')]

        if len(images) == 0:
            await message.reply("這訊息裡面沒有圖片，傻逼。")
            return
        elif len(images) > 1:
            await message.reply("這訊息包含超過一張圖片，老子存不了。")
            return

        image_att = images[0]
        await message.add_reaction("⏳")

        # 處理名稱重複問題：同名時自動遞增 "名稱(2)", "名稱(3)"
        final_name = base_name
        counter = 2
        while True:
            cursor.execute("SELECT id FROM images WHERE name = ?", (final_name,))
            if cursor.fetchone() is None:
                break
            final_name = f"{base_name}({counter})"
            counter += 1

        try:
            # 讀取圖片進入記憶體
            file_bytes = await image_att.read()
            mimetype = image_att.content_type

            # 上傳至 Google Drive (在背景執行緒中處理，避免卡住 Discord 事件迴圈)
            drive_id = await asyncio.to_thread(upload_to_drive_sync, file_bytes, final_name, mimetype)

            # 存入本地 SQLite 資料庫
            cursor.execute("INSERT INTO images (name, drive_id) VALUES (?, ?)", (final_name, drive_id))
            conn.commit()

            print(f"[成功] 圖片已保存：{final_name} -> GDrive ID: {drive_id}")
            await message.reply(f"圖片 `{final_name}` 成功保存")
        except Exception as e:
            print(f"保存失敗：\n{e}")
            await message.reply(f"儲存失敗：\n{e}")
        finally:
            await message.remove_reaction("⏳", bot.user)

# ================= 自動補全邏輯 (Autocomplete) =================
async def image_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    print(f"[除錯-Autocomplete] 收到查詢字串: '{current}'")
    try:
        with sqlite3.connect('images.db') as db:
            cur = db.cursor()
            # 若為空則帶出最新 25 筆，若有打字則前後加模糊比對符號
            if current and current.strip():
                cur.execute(
                    "SELECT name FROM images WHERE name LIKE ? ORDER BY id DESC LIMIT 25",
                    (f"%{current.strip()}%",)
                )
            else:
                cur.execute("SELECT name FROM images ORDER BY id DESC LIMIT 25")
            
            rows = cur.fetchall()

        choices = [
            app_commands.Choice(name=str(row[0])[:100], value=str(row[0])[:100])
            for row in rows
        ]
        print(f"[除錯-Autocomplete] 成功回傳 {len(choices)} 個選項")
        return choices
    except Exception as e:
        print(f"[Autocomplete 異常例外]: {e}")
        return []

# 捕捉 Autocomplete 底層未捕獲錯誤
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    print(f"[AppCommand 全域錯誤]: {error}")
# ================= 2. 搜尋並發送功能：/搜尋 "圖片名稱" =================
@bot.tree.command(name="search", description="搜尋並發送儲存的圖片")
@app_commands.describe(圖片名稱="請輸入或選擇要搜尋的圖片名稱")
@app_commands.autocomplete(圖片名稱=image_autocomplete)
async def search_image(interaction: discord.Interaction, 圖片名稱: str):
    await interaction.response.defer()

    with sqlite3.connect('images.db') as db:
        cur = db.cursor()
        cur.execute("SELECT drive_id FROM images WHERE name = ?", (圖片名稱,))
        result = cur.fetchone()

    if not result:
        await interaction.followup.send(f"找不到名為 `{圖片名稱}` 的圖片。", ephemeral=True)
        return

    drive_id = result[0]

    try:
        fh = await asyncio.to_thread(download_from_drive_sync, drive_id)
        discord_file = discord.File(fp=fh, filename=f"{圖片名稱}.png")
        await interaction.followup.send(content=f"圖：\n", file=discord_file)
    except Exception as e:
        print(f"下載發送圖片失敗：\n{e}")
        await interaction.followup.send(f"抓取圖片時發生錯誤：\n{e}")

# ================= 3. 刪除功能：/刪除 "圖片名稱" =================
@bot.tree.command(name="delete", description="刪除已儲存的圖片")
@app_commands.describe(圖片名稱="請選擇要刪除的圖片名稱")
@app_commands.autocomplete(圖片名稱=image_autocomplete)
async def delete_image(interaction: discord.Interaction, 圖片名稱: str):
    await interaction.response.defer()

    cursor.execute("SELECT drive_id FROM images WHERE name = ?", (圖片名稱,))
    result = cursor.fetchone()

    if not result:
        await interaction.followup.send(f"找不到名為 `{圖片名稱}` 的圖片。", ephemeral=True)
        return

    drive_id = result[0]

    try:
        # 從 GDrive 刪除檔案
        await asyncio.to_thread(delete_from_drive_sync, drive_id)

        # 從本地 SQLite 移除記錄
        cursor.execute("DELETE FROM images WHERE name = ?", (圖片名稱,))
        conn.commit()

        await interaction.followup.send(f"圖片 `{圖片名稱}` 刪掉了")
    except Exception as e:
        print(f"[錯誤] 刪除失敗：\n{e}")
        await interaction.followup.send(f"刪除時發生錯誤：\n{e}")

# ================= Google Drive 全量同步函式 =================
def sync_database_from_drive_sync():
    """遍歷 Google Drive 指定資料夾，將所有圖片記錄還原至本地 SQLite"""
    query = f"'{FOLDER_ID}' in parents and trashed = false"
    page_token = None
    restored_count = 0

    # 使用獨立連線以確保執行緒安全
    with sqlite3.connect('images.db') as db:
        cur = db.cursor()
        cur.execute('''
            CREATE TABLE IF NOT EXISTS images (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE,
                drive_id TEXT
            )
        ''')
        db.commit()

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
                
                # 排除可能混入的非圖片子資料夾或空名稱
                if name and drive_id:
                    cur.execute(
                        "INSERT OR IGNORE INTO images (name, drive_id) VALUES (?, ?)",
                        (name, drive_id)
                    )
                    if cur.rowcount > 0:
                        restored_count += 1

            db.commit()
            page_token = response.get('nextPageToken', None)
            if not page_token:
                break

    return restored_count

# ================= 4. 同步指令：/同步雲端 =================
@bot.tree.command(name="同步雲端", description="從 Google Drive 還原並同步所有圖片資料至本地資料庫")
@app_commands.default_permissions(administrator=True)  # 僅限管理員使用
async def sync_drive_command(interaction: discord.Interaction):
    # 此動作需要遍歷 GDrive API，務必預先延遲回應 (defer)
    await interaction.response.defer(ephemeral=True)

    try:
        count = await asyncio.to_thread(sync_database_from_drive_sync)
        await interaction.followup.send(
            f"✅ 同步完成！已成功還原 / 補齊 `{count}` 筆圖片記錄至資料庫。",
            ephemeral=True
        )
    except Exception as e:
        print(f"[錯誤] 同步雲端資料失敗: {e}")
        await interaction.followup.send(
            f"❌ 同步失敗，發生錯誤：`{e}`",
            ephemeral=True
        )


# ================= 啟動執行 =================
if __name__ == '__main__':
    bot.run(TOKEN)
