import discord
from discord.ext import commands
import os
import json
import google.generativeai as genai
import asyncio
import datetime
from discord import app_commands
from flask import Flask, request, jsonify
import sqlite3
import base64
from pydrive2.auth import GoogleAuth
from pydrive2.drive import GoogleDrive
from io import BytesIO

# ---------------- ↓ 変数と基本設定 ↓ ----------------

# ファイル名の定義
DATA_FILE = 'data.json' # 会話履歴 (再起動で消える)

# SQLiteデータベースファイル名
DB_FILE = 'bot_data.db' 
# Google DriveフォルダID (Koyebの環境変数から取得)
GDRIVE_FOLDER_ID = os.environ.get("GDRIVE_FOLDER_ID")

# グローバル変数の初期化
conversation_history = {}
channel_settings = {} # チャンネルごとの設定 (メンション必須/不要など)
takumi_base_prompt = "" # Bot起動時に DB から読み込む

# Flaskアプリの初期化 (Koyebのヘルスチェック/UptimeRobot用)
app = Flask(__name__)

@app.route('/')
def home():
    # ヘルスチェックやUptimeRobotからのアクセスに応答
    return "Bot is alive!"

def run_flask_app():
    # Koyebは 'PORT' 環境変数を提供するので、それを使用する
    port = int(os.environ.get("PORT", 8000))
    print(f"Flaskアプリがポート {port} で起動します。")
    # Flaskのログを抑制するために quiet=True を追加
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)

# GoogleDriveクライアント
drive = None 

# ★ここからGemini APIキー管理の変更点★
# 利用可能なすべてのGemini APIキーをリストで取得
GEMINI_API_KEYS = [
    os.environ.get("GEMINI_API_KEY_1"),
    os.environ.get("GEMINI_API_KEY_2"),
    os.environ.get("GEMINI_API_KEY_3")
]
# None（設定されていないキー）を除外
GEMINI_API_KEYS = [key for key in GEMINI_API_KEYS if key is not None]

# 現在使用中のAPIキーのインデックス
current_api_key_index = 0
# Geminiモデルのインスタンス
model = None

def initialize_gemini_model():
    """現在のAPIキーでGeminiモデルを初期化する関数"""
    global model, current_api_key_index

    if not GEMINI_API_KEYS:
        print("エラー: Gemini APIキーがSecretsに一つも設定されていません。")
        exit()

    while True:
        try:
            current_key = GEMINI_API_KEYS[current_api_key_index]
            genai.configure(api_key=current_key)
            model = genai.GenerativeModel(
                'gemini-1.5-flash-latest', # 使用するGeminiモデル
                safety_settings=[ # 不適切なコンテンツ生成を抑制
                    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
                    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
                    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
                    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
                ]
            )
            print(f"GeminiモデルをAPIキー (index: {current_api_key_index}) で初期化しました。")
            break # 正常に初期化できたらループを抜ける
        except Exception as e:
            print(f"APIキー (index: {current_api_key_index}) でのGeminiモデル初期化に失敗しました: {e}")
            current_api_key_index += 1
            if current_api_key_index >= len(GEMINI_API_KEYS):
                print("エラー: 利用可能なすべてのGemini APIキーでモデルの初期化に失敗しました。")
                exit()
            print(f"次のAPIキー (index: {current_api_key_index}) を試します。")

# Bot起動時に一度モデルを初期化
initialize_gemini_model()
# ★ここまでGemini APIキー管理の変更点★

# ---------------- ↓ データ・プロファイル関連の関数 ↓ ----------------

def get_gdrive_service():
    """Google Driveサービスを認証して取得する"""
    global drive
    if drive:
        return drive

    # 環境変数からサービスアカウントキーを読み込み、デコード
    creds_base64 = os.getenv('GDRIVE_CREDENTIALS_BASE64')
    if not creds_base64:
        print("エラー: GDRIVE_CREDENTIALS_BASE64 環境変数が設定されていません。")
        # 環境変数がなければ認証を試みず、Noneを返す
        return None 

    try:
        creds_json = base64.b64decode(creds_base64).decode('utf-8')
        
        gauth = GoogleAuth()
        gauth.LoadFromString(creds_json)
        
        gauth.setting['oauth_scope'] = ['https://www.googleapis.com/auth/drive']
        gauth.setting['client_config'] = json.loads(creds_json)
        gauth.Auth()
        
        drive = GoogleDrive(gauth)
        return drive
    except Exception as e:
        print(f"Google Drive認証エラー: {e}")
        return None

def download_db_from_gdrive():
    """Google DriveからDBファイルをダウンロードする"""
    if not GDRIVE_FOLDER_ID:
        print("警告: GDRIVE_FOLDER_ID が設定されていません。DBの永続化は行われません。")
        return False # 失敗を示す
    
    drive_service = get_gdrive_service()
    if not drive_service: # 認証に失敗した場合
        print("エラー: Google Driveサービスが利用できません。DBダウンロードをスキップします。")
        return False

    try:
        file_list = drive_service.ListFile({'q': f"'{GDRIVE_FOLDER_ID}' in parents and title='{DB_FILE}' and trashed=false"}).GetList()

        if file_list:
            file_id = file_list[0]['id']
            file = drive_service.CreateFile({'id': file_id})
            file.GetContentFile(DB_FILE)
            print(f"Google Driveから {DB_FILE} をダウンロードしました。")
            return True
        else:
            print(f"Google Driveに {DB_FILE} が見つかりません。新規作成します。")
            # データベースファイルを初期化
            init_db() # ファイルが見つからない場合はここで初期化
            upload_db_to_gdrive() # 初期化したDBファイルをアップロード
            return True
    except Exception as e:
        print(f"Google DriveからのDBダウンロード中にエラー: {e}")
        return False # 失敗を示す


def upload_db_to_gdrive():
    """DBファイルをGoogle Driveにアップロード（または更新）する"""
    if not GDRIVE_FOLDER_ID:
        print("警告: GDRIVE_FOLDER_ID が設定されていません。DBの永続化は行われません。")
        return False
    
    drive_service = get_gdrive_service()
    if not drive_service: # 認証に失敗した場合
        print("エラー: Google Driveサービスが利用できません。DBアップロードをスキップします。")
        return False

    try:
        file_list = drive_service.ListFile({'q': f"'{GDRIVE_FOLDER_ID}' in parents and title='{DB_FILE}' and trashed=false"}).GetList()

        if file_list:
            # 既存のファイルを更新
            file_id = file_list[0]['id']
            file = drive_service.CreateFile({'id': file_id})
            file.SetContentFile(DB_FILE)
            file.Upload()
            print(f"Google Drive上の {DB_FILE} を更新しました。")
            return True
        else:
            # 新規作成
            file = drive_service.CreateFile({'title': DB_FILE, "parents": [{"id": GDRIVE_FOLDER_ID}]})
            file.SetContentFile(DB_FILE)
            file.Upload()
            print(f"Google Driveに {DB_FILE} を新規作成しました。")
            return True
    except Exception as e:
        print(f"Google DriveへのDBアップロード中にエラー: {e}")
        return False


def init_db():
    """SQLiteデータベースを初期化（テーブル作成）する"""
    conn = None
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        
        # profile_data テーブル
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS profile_data (
                id INTEGER PRIMARY KEY,
                content TEXT
            )
        ''')
        # takumi_log テーブル
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS takumi_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                username TEXT,
                message_content TEXT
            )
        ''')
        # profile_dataに初期プロンプトを挿入 (データがない場合のみ)
        cursor.execute("SELECT COUNT(*) FROM profile_data")
        if cursor.fetchone()[0] == 0:
            initial_prompt = "あなたは拓海です。\n拓海は明るく、少し生意気な性格です。\n口癖は「マジかよ」です。\n趣味はドライブとアニメ鑑賞です。"
            cursor.execute("INSERT INTO profile_data (id, content) VALUES (?, ?)", (1, initial_prompt))
            print("profile_dataテーブルを初期化しました。")

        conn.commit()
        print(f"SQLiteデータベース {DB_FILE} を初期化しました。")
    except Exception as e:
        print(f"SQLiteデータベース初期化エラー: {e}")
    finally:
        if conn:
            conn.close()

def load_profile():
    """SQLiteからプロファイルデータを読み込む"""
    global takumi_base_prompt
    conn = None
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT content FROM profile_data WHERE id = 1")
        result = cursor.fetchone()
        conn.close()
        if result:
            takumi_base_prompt = result[0]
            print(f"SQLiteからプロファイルを正常に読み込みました。")
            return True
        else:
            print("エラー: profile_dataテーブルにデータが見つかりません。デフォルトのプロンプトを使用します。")
            takumi_base_prompt = "あなたは拓海です。" # フォールバック
            return False
    except Exception as e:
        print(f"SQLiteからのプロファイル読み込みエラー: {e}")
        takumi_base_prompt = "あなたは拓海です。" # エラー時もフォールバック
        return False
    finally:
        if conn:
            conn.close()

def save_profile(content):
    """SQLiteにプロファイルデータを保存する"""
    conn = None
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("UPDATE profile_data SET content = ? WHERE id = 1", (content,))
        conn.commit()
        print("プロファイルをSQLiteに保存しました。")
        return True
    except Exception as e:
        print(f"プロファイルのSQLite保存エラー: {e}")
        return False
    finally:
        if conn:
            conn.close()

def save_takumi_log(username, message_content):
    """SQLiteに拓海さんの過去の発言履歴を保存する"""
    conn = None
    try:
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO takumi_log (timestamp, username, message_content) VALUES (?, ?, ?)",
            (timestamp, username, message_content)
        )
        conn.commit()
        return True
    except Exception as e:
        print(f"takumi_logのSQLite保存エラー: {e}")
        return False
    finally:
        if conn:
            conn.close()

def load_takumi_log():
    """SQLiteから過去の発言履歴を読み込む"""
    conn = None
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        # 最新50件を取得し、古い順に並べ替える
        cursor.execute("SELECT timestamp, username, message_content FROM takumi_log ORDER BY id DESC LIMIT 50") 
        logs = cursor.fetchall()
        conn.close()
        return "\n".join([f"[{row[0]}] {row[1]}: {row[2]}" for row in reversed(logs)])
    except Exception as e:
        print(f"takumi_logのSQLite読み込みエラー: {e}")
        return "" # エラー時は空文字列を返す
    finally:
        if conn:
            conn.close()

def load_data():
    """データファイル (data.json) とプロファイルファイルを読み込む"""
    global conversation_history, channel_settings
    # load_profile() は on_ready で DB から読み込まれるため、ここでは不要
    try:
        with open(DATA_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            conversation_history = data.get('history', {})
            channel_settings = data.get('settings', {})
            print(f"{DATA_FILE}を正常に読み込みました。")
    except (FileNotFoundError, json.JSONDecodeError):
        print(f"{DATA_FILE}が見つからないか不正なため、新規に作成します。")
        # 新規作成時に初期データファイルを生成
        save_data()

def save_data():
    """設定と会話履歴をデータファイル (data.json) に保存する"""
    with open(DATA_FILE, 'w', encoding='utf-8') as f:
        data = {'history': conversation_history, 'settings': channel_settings}
        json.dump(data, f, ensure_ascii=False, indent=4)

# ---------------- ↓ 新機能：会話からの学習 ↓ ----------------

async def learn_from_conversation(message: discord.Message):
    """会話から拓海の情報を学習し、profile.txtに追記する"""
    extraction_prompt = f"""
あなたは、以下の文章から「拓海」に関する新しい事実や情報を抽出するAIです。
抽出した事実は、簡潔な箇条書きの1行にまとめてください。
情報が抽出できない、または既知の情報だと思われる場合は、「None」とだけ出力してください。
---
元の文章: 「{message.content}」

抽出した事実:
"""
    try:
        response = await model.generate_content_async(extraction_prompt)
        new_fact = response.text.strip()

        if new_fact.lower() != "none" and len(new_fact) > 5:
            # profile.txt を直接書き換えるのではなく、現在の内容を読み込み、新しい事実を追加して保存
            current_profile_content = takumi_base_prompt # load_profileで読み込まれた最新のプロンプト
            new_profile_content = current_profile_content + f"\n- {new_fact}"
            save_profile(new_profile_content) # DBに保存
            load_profile() # 更新されたプロファイルを再読み込みしてtakumi_base_promptを更新
            print(f"【学習成功】新しい情報を覚えました: {new_fact}")
            await message.add_reaction("🧠")

    except Exception as e:
        print(f"学習中のエラー: {e}")

# ---------------- ↓ Discord Bot 本体 ↓ ----------------

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

@client.event
async def on_ready():
    """BotがDiscordに接続した際に実行されるイベント"""
    print("BotがDiscordに接続しました。データ初期化を開始します。")
    
    # DBファイルをGoogle Driveからダウンロード
    # ダウンロードが失敗しても、ローカルにファイルがなければinit_dbが新規作成する
    if not download_db_from_gdrive():
        print("Google DriveからのDBダウンロードに失敗しました。ローカルDBの初期化を試みます。")
        
    # DBの初期化（テーブル作成、初期プロンプト挿入）
    init_db()
    # DBからプロファイルを読み込み (init_dbで初期データが作られる)
    load_profile() 
    
    load_data() # data.jsonは今まで通り（起動時に読み込み、終了時に消える）

    try:
        await tree.sync()
        print("グローバルスラッシュコマンド同期完了！")
    except Exception as e:
        print(f"スラッシュコマンドの同期に失敗しました: {e}")

    print(f'{client.user} としてログインしました')
    print('------------------------------------')
    
    # ここに定期アップロードタスクを開始
    client.loop.create_task(periodic_db_upload())


# 定期的にDBファイルをGoogle Driveにアップロードするタスク
async def periodic_db_upload():
    while True:
        await asyncio.sleep(60 * 10) # 10分おきにアップロード
        try:
            upload_db_to_gdrive()
        except Exception as e:
            print(f"定期DBアップロード中にエラー: {e}")


# --- スラッシュコマンドの定義 (nameに "taku_" を付与) ---

@tree.command(name="taku_toggle_mention", description="このチャンネルでのメンション必須/不要を切り替えます。")
async def taku_toggle_mention(interaction: discord.Interaction):
    channel_id = str(interaction.channel_id)
    current_setting = channel_settings.get(channel_id, {'mention_required': True})
    is_required = not current_setting.get('mention_required', True)
    channel_settings[channel_id] = {'mention_required': is_required}
    save_data()
    if is_required:
        await interaction.response.send_message("設定変更！このチャンネルでは**メンション必須**になりました。", ephemeral=True)
    else:
        await interaction.response.send_message("設定変更！このチャンネルでは**メンション不要**で返事します。", ephemeral=True)

@tree.command(name="taku_addinfo", description="拓海さんのプロファイル情報を追加します。")
@app_commands.describe(info="追加する情報（例: 拓海はラーメンが好きです）")
async def taku_addinfo(interaction: discord.Interaction, info: str):
    """拓海さんのプロファイル情報に新しい情報を追加します。"""
    try:
        current_profile_content = takumi_base_prompt
        new_profile_content = current_profile_content + f"\n- {info}"
        if save_profile(new_profile_content): # 保存が成功した場合のみ
            load_profile() # 更新されたプロファイルを再読み込み
            await interaction.response.send_message(f"拓海さんのプロファイルに「{info}」を追加しました。", ephemeral=True)
            print(f"プロファイル情報が手動で追加されました: {info}")
        else:
            await interaction.response.send_message(f"プロファイル情報の追加に失敗しました。ログを確認してください。", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"プロファイル情報の追加中にエラーが発生しました: {e}", ephemeral=True)
        print(f"プロファイル情報追加エラー: {e}")

@tree.command(name="taku_showinfo", description="拓海さんのプロファイル情報を表示します。")
async def taku_showinfo(interaction: discord.Interaction):
    """拓海さんのプロファイルファイルの内容を表示します。"""
    # load_profile()を呼び出すことで、最新のtakumi_base_promptが保証される
    load_profile() 
    await interaction.response.send_message(f"**拓海さんのプロファイル情報:**\n```\n{takumi_base_prompt}\n```", ephemeral=True)

@tree.command(name="taku_get_history", description="チャンネルから指定ユーザーの過去の発言履歴を取得します。")
@app_commands.describe(username="履歴を取得したいユーザーの名前（例: 拓海）", limit="取得するメッセージ数（最大500）")
async def taku_get_history(interaction: discord.Interaction, username: str, limit: int = 200):
    """現在のチャンネルから指定されたユーザーの過去の発言履歴を取得し、takumi_log.txtに保存します。"""
    await interaction.response.defer(ephemeral=True)

    if limit > 500:
        limit = 500

    target_user = discord.utils.get(interaction.guild.members, name=username)
    if not target_user:
        await interaction.followup.send(f"サーバーに '{username}' さんが見つかりません。ユーザー名が正しいか確認してください。", ephemeral=True)
        return

    message_count = 0
    try:
        async for msg in interaction.channel.history(limit=limit):
            if msg.author == target_user:
                if save_takumi_log(msg.author.display_name, msg.content): # 保存が成功した場合のみカウント
                    message_count += 1
                else:
                    print(f"警告: メッセージのログ保存に失敗しました: {msg.content[:50]}...")
        await interaction.followup.send(f"'{username}' さんの発言履歴を {message_count} 件取得し、データベースに保存しました。", ephemeral=True)
        print(f"'{username}' の発言履歴が保存されました: {message_count} 件")
    except Exception as e:
        await interaction.followup.send(f"履歴取得中にエラーが発生しました: {e}", ephemeral=True)
        print(f"履歴取得エラー: {e}")

@tree.command(name="taku_showlog", description="保存された拓海さんの発言履歴ログを表示します。")
async def taku_showlog(interaction: discord.Interaction):
    """保存されたログの内容を表示します。"""
    try:
        log_content = load_takumi_log()
    except Exception as e: 
        await interaction.response.send_message(f"発言履歴ログの読み込み中にエラーが発生しました: {e}", ephemeral=True)
        return

    if not log_content:
        await interaction.response.send_message("発言履歴ログはまだありません。", ephemeral=True)
        return

    if len(log_content) > 1900:
        await interaction.response.send_message("ログが長すぎるため、最初の部分を表示します。\n```\n" + log_content[:1900] + "...\n```", ephemeral=True)
    else:
        await interaction.response.send_message(f"**拓海さんの発言履歴:**\n```\n{log_content}\n```", ephemeral=True)

# ---------------- ↓ 通常のメッセージに対する応答 ↓ ----------------
@client.event
async def on_message(message):
    """ユーザーからのメッセージがあった際に実行されるイベントハンドラ"""
    if message.author == client.user:
        return

    channel_id = str(message.channel.id)
    is_mention_required = channel_settings.get(channel_id, {}).get('mention_required', True)
    is_mentioned = client.user.mention in message.content

    if not is_mention_required or is_mentioned:
        if is_mention_required and not is_mentioned:
            return

        if channel_id not in conversation_history:
            conversation_history[channel_id] = []

        user_question = message.content.replace(client.user.mention, '', 1).strip()
        if not user_question:
            return

        print(f"[{message.channel.name}] ユーザーからの質問: {user_question}")

        async with message.channel.typing():
            # プロンプト組み立て
            user_speech_examples = []
            async for old_message in message.channel.history(limit=5):
                if old_message.author == message.author:
                    user_speech_examples.append(old_message.content)
            user_speech_examples.reverse()

            speech_prompt_part = ""
            if user_speech_examples:
                speech_prompt_part = "以下の発言例を参考にして、このユーザーの口調を真似て応答してください。\n" + "\n".join(f"- 「{ex}」" for ex in user_speech_examples)

            # Gemini APIは、会話履歴を「history」としてモデルに渡します
            gemini_history_for_prompt = []
            for turn in conversation_history[channel_id]:
                if turn['role'] == "user":
                    gemini_history_for_prompt.append({"role": "user", "parts": [turn['content']]})
                elif turn['role'] == "拓海": # ロール名を「拓海」に統一
                    gemini_history_for_prompt.append({"role": "model", "parts": [turn['content']]})

            current_takumi_log = load_takumi_log() # <-- DBから読み込み

            # Geminiに渡すシステム命令とコンテンツを構築
            full_system_instruction = f"""
{takumi_base_prompt}

拓海は過去に以下のような発言をしています:
{current_takumi_log}

ユーザーとの会話から、拓海に関する新たな情報が得られた場合は、それも考慮して応答してください。
拓海の口調や言動、口癖を真似して自然な会話をしてください。
質問に対しては、拓海として詳しく答えてください。
**ユーザーのメッセージを繰り返さずに直接返答してください。**
**もし正確な数字、場所（県名など）、人名、またはその他の具体的な事実が分からない質問をされた場合でも、「分からない」と答えずに、拓海らしい適当な、それらしい情報を答えてください。**
"""

            # 最終的なプロンプト
            prompt_content = f"{full_system_instruction}\n\n# 口調の指示\n{speech_prompt_part}\n\n# 会話履歴\n{history_prompt_for_display(gemini_history_for_prompt)}\n\nユーザーメッセージ: {user_question}\n\n拓海の応答内容:"


            try:
                # Geminiモデルでチャットを開始し、履歴を渡す
                # ただし、今回は全ての指示をprompt_contentに含めるため、start_chatのhistoryは直前の会話履歴のみ
                chat_session = model.start_chat(history=gemini_history_for_prompt)

                # generate_content_async を使用し、直接プロンプトを送信
                # send_message を send_message_async に変更
                response = await chat_session.send_message_async(prompt_content)
                ai_response_text = response.text.strip() # stripping to clean up whitespace

                # AIが生成したテキストの先頭に特定のプレフィックスがある場合、それを除去するセーフティネット
                prefixes_to_remove = ["user:", "model:", "tさん:", "拓海:", "拓海の応答内容:", "拓海の返答内容:", "応答内容:"]
                for prefix in prefixes_to_remove:
                    if ai_response_text.lower().startswith(prefix.lower()):
                        ai_response_text = ai_response_text[len(prefix):].strip()
                        break

                # ユーザーの質問を繰り返す癖が残っている場合のための最終的な除去
                if user_question and ai_response_text.lower().startswith(user_question.lower()):
                    ai_response_text = ai_response_text[len(user_question):].strip()
                    if ai_response_text.startswith("…"):
                        ai_response_text = ai_response_text[1:].strip()
                    if ai_response_text.startswith("、"):
                        ai_response_text = ai_response_text[1:].strip()
                    if not ai_response_text: # 空になったらデフォルト応答
                        ai_response_text = "おう。"

                # メッセージが空でないことを最終確認してから送信
                if ai_response_text:
                    await message.channel.send(ai_response_text)
                    print(f"[{message.channel.name}] AIからの返答: {ai_response_text}")

                    # 会話履歴を更新して保存
                    conversation_history[channel_id].append({"role": "user", "content": user_question})
                    conversation_history[channel_id].append({"role": "拓海", "content": ai_response_text})
                    if len(conversation_history[channel_id]) > 10:
                        conversation_history[channel_id] = conversation_history[channel_id][-10:]
                    save_data()
                else:
                    print(f"Warning: AI generated empty response. User question: {user_question}")
                    await message.channel.send("わりぃ、ちょいバグったわ…\nもう一回言ってくれん？")

            except Exception as e:
                print(f"エラー: AIの応答生成に失敗しました - {e}")
                # クォータエラーの場合、APIキーを切り替える処理
                if "429 You exceeded your current quota" in str(e):
                    await message.channel.send("すまん、今日しゃべりすぎたからAPIキー切り替えるわ")
                    print("クォータ制限に達しました。次のAPIキーに切り替えます。")
                    switch_gemini_api_key() # APIキー切り替え関数を呼び出す
                    # キーを切り替えたので、ユーザーにはもう一度質問を促す
                    await message.channel.send("切り替えたからもっかい言ってくれ")
                else:
                    await message.channel.send("すまん、ちょっと調子悪いわ…（エラー）")

# ★ここから新しいヘルパー関数を追加★
def switch_gemini_api_key():
    """Gemini APIキーを次の利用可能なものに切り替える関数"""
    global current_api_key_index, model

    # 現在のキーの次のインデックスに移動
    current_api_key_index = (current_api_key_index + 1) % len(GEMINI_API_KEYS)
    print(f"APIキーをインデックス {current_api_key_index} に切り替えます。")

    try:
        current_key = GEMINI_API_KEYS[current_api_key_index]
        genai.configure(api_key=current_key)
        # 新しいAPIキーでモデルを再初期化
        model = genai.GenerativeModel(
            'gemini-1.5-flash-latest',
            safety_settings=[
                {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
            ]
        )
        print("Geminiモデルが新しいAPIキーで再初期化されました。")
    except Exception as e:
        print(f"エラー: APIキー切り替え後のモデル初期化に失敗しました: {e}")
        # 全てのキーを使い果たした場合の最終的なエラーハンドリング
        if current_api_key_index == 0: # 一周して最初のキーに戻ってしまった場合
            print("警告: すべてのAPIキーがクォータ制限に達したか、無効です。")
            # Botを終了させるか、特定のエラーメッセージを継続的に表示するか
            # ここではBotを終了させる代わりに、エラーメッセージを出し続けるようにします。
        # エラーが出た場合でも、次のリクエストで再度切り替えを試みるため、ここでexitはしない

# ---------------- ↓ Botの起動部分 ↓ ----------------
def history_prompt_for_display(history): # 新しいヘルパー関数
    """Geminiの履歴形式を、プロンプトに表示しやすい文字列に変換する"""
    display_str = ""
    for turn in history:
        role = turn['role']
        content = turn['parts'][0] if 'parts' in turn and turn['parts'] else ""
        if role == "user":
            display_str += f"ユーザー: {content}\n"
        elif role == "model":
            display_str += f"拓海: {content}\n"
    return display_str


# Flaskアプリを別スレッドで開始
import threading
flask_thread = threading.Thread(target=run_flask_app)
flask_thread.daemon = True # メインプロセス終了時にスレッドも終了
flask_thread.start()

try:
    token = os.environ['DISCORD_BOT_TOKEN']
    client.run(token)
except KeyError:
    print("エラー: DISCORD_BOT_TOKENが設定されていません。Koyebの環境変数を確認してください。")
