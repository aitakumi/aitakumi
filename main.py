import discord
from discord.ext import commands
import os
import json
import google.generativeai as genai
from keep_alive import keep_alive
import asyncio
import datetime
from discord import app_commands

# ---------------- ↓ 変数と基本設定 ↓ ----------------

# ファイル名の定義
DATA_FILE = 'data.json'
PROFILE_FILE = 'profile.txt'
TAKUMI_LOG_FILE = "takumi_log.txt" # 拓海さんの発言ログファイル

# グローバル変数の初期化
conversation_history = {}
channel_settings = {} # チャンネルごとの設定 (メンション必須/不要など)
takumi_base_prompt = "" # Bot起動時に PROFILE_FILE から読み込む

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

def load_profile():
    """プロファイルファイル (profile.txt) を読み込む"""
    global takumi_base_prompt # 変数名をt_san_base_promptからtakumi_base_promptに変更
    try:
        with open(PROFILE_FILE, 'r', encoding='utf-8') as f:
            takumi_base_prompt = f.read()
        print(f"{PROFILE_FILE}を正常に読み込みました。")
    except FileNotFoundError:
        print(f"エラー: {PROFILE_FILE} が見つかりません。デフォルトのプロンプトを使用します。")
        takumi_base_prompt = "あなたは拓海です。" # デフォルト値
        # ファイルが存在しない場合に作成
        with open(PROFILE_FILE, 'w', encoding='utf-8') as f:
            f.write(takumi_base_prompt + "\n")
            f.write("拓海は明るく、少し生意気な性格です。\n")
            f.write("口癖は「マジかよ」です。\n")
            f.write("趣味はドライブとアニメ鑑賞です。\n")

def load_data():
    """データファイル (data.json) とプロファイルファイルを読み込む"""
    global conversation_history, channel_settings
    load_profile() # プロファイルを先に読み込む
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

# 拓海さんの過去の発言履歴を保存する関数 (takumi_log.txt用)
def save_takumi_log(username, message_content):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(TAKUMI_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{timestamp}] {username}: {message_content}\n")

# 過去の発言履歴を読み込む関数 (takumi_log.txt用)
def load_takumi_log():
    if not os.path.exists(TAKUMI_LOG_FILE):
        return ""
    with open(TAKUMI_LOG_FILE, "r", encoding="utf-8") as f:
        return f.read()

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
            with open(PROFILE_FILE, 'a', encoding='utf-8') as f:
                f.write(f"\n- {new_fact}")
            load_profile()
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
    load_data()

    try:
        await tree.sync()
        print("グローバルスラッシュコマンド同期完了！")
    except Exception as e:
        print(f"スラッシュコマンドの同期に失敗しました: {e}")

    print(f'{client.user} としてログインしました')
    print('------------------------------------')


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
        with open(PROFILE_FILE, 'a', encoding='utf-8') as f:
            f.write(f"\n- {info}")
        load_profile()
        await interaction.response.send_message(f"拓海さんのプロファイルに「{info}」を追加しました。", ephemeral=True)
        print(f"プロファイル情報が手動で追加されました: {info}")
    except Exception as e:
        await interaction.response.send_message(f"プロファイル情報の追加中にエラーが発生しました: {e}", ephemeral=True)
        print(f"プロファイル情報追加エラー: {e}")

@tree.command(name="taku_showinfo", description="拓海さんのプロファイル情報を表示します。")
async def taku_showinfo(interaction: discord.Interaction):
    """拓海さんのプロファイルファイルの内容を表示します。"""
    info = load_profile()
    await interaction.response.send_message(f"**拓海さんのプロファイル情報:**\n```\n{info}\n```", ephemeral=True)

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
        with open(TAKUMI_LOG_FILE, "a", encoding="utf-8") as f_log:
            async for msg in interaction.channel.history(limit=limit):
                if msg.author == target_user:
                    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    f_log.write(f"[{timestamp}] {msg.author.display_name}: {msg.content}\n")
                    message_count += 1
        await interaction.followup.send(f"'{username}' さんの発言履歴を {message_count} 件取得し、`{TAKUMI_LOG_FILE}` に保存しました。", ephemeral=True)
        print(f"'{username}' の発言履歴が保存されました: {message_count} 件")
    except Exception as e:
        await interaction.followup.send(f"履歴取得中にエラーが発生しました: {e}", ephemeral=True)
        print(f"履歴取得エラー: {e}")

@tree.command(name="taku_showlog", description="保存された拓海さんの発言履歴ログを表示します。")
async def taku_showlog(interaction: discord.Interaction):
    """保存された `takumi_log.txt` の内容を表示します。"""
    try:
        log_content = load_takumi_log()
    except FileNotFoundError:
        await interaction.response.send_message(f"発言履歴ログファイル `{TAKUMI_LOG_FILE}` が見つかりません。", ephemeral=True)
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

            current_takumi_log = load_takumi_log()

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


keep_alive() # keep_alive.py で定義された関数を呼び出す
try:
    token = os.environ['DISCORD_BOT_TOKEN']
    client.run(token)
except KeyError:
    print("エラー: DISCORD_BOT_TOKENが設定されていません。ReplitのSecretsを確認してください。")
