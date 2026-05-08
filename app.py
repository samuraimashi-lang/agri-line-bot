"""
農作業記録 LINE Bot
================================
農家さんがLINEで音声または文字を送ると
AIが自動で整理してGoogleスプレッドシートに保存します。

機能：
  ① 音声・テキストで作業内容を受け取る
  ② GPS位置情報から圃場を自動判定
  ③ 天気を自動取得（位置情報から）
  ④ AIが作業内容を整形・不足項目を質問
  ⑤ 「OK」で Google Sheets に自動保存
"""

import os, json, tempfile, math
from datetime import datetime

import requests
from flask import Flask, request, abort

from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, AudioMessage, LocationMessage,
    TextSendMessage,
)

import anthropic
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from openai import OpenAI
import base64, tempfile

# ======================================================
# アプリ初期化
# ======================================================
app = Flask(__name__)

LINE_TOKEN  = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
LINE_SECRET = os.environ["LINE_CHANNEL_SECRET"]
CLAUDE_KEY  = os.environ["ANTHROPIC_API_KEY"]
OPENAI_KEY  = os.environ["OPENAI_API_KEY"]
WEATHER_KEY = os.environ.get("WEATHER_API_KEY", "")
SHEET_NAME  = os.environ.get("SHEET_NAME", "農作業記録")

line_bot_api  = LineBotApi(LINE_TOKEN)
handler       = WebhookHandler(LINE_SECRET)
claude        = anthropic.Anthropic(api_key=CLAUDE_KEY)
openai_client = OpenAI(api_key=OPENAI_KEY)

# ======================================================
# ★ 圃場マスタ（実際の座標に変更してください）
# ======================================================
FIELDS = [
    {"name": "荒谷①",  "group": "天童", "lat": 38.3500, "lon": 140.3800, "area_a": 17},
    {"name": "神町",    "group": "東根", "lat": 38.4300, "lon": 140.4200, "area_a": 48},
    {"name": "大谷",    "group": "天童", "lat": 38.3600, "lon": 140.3900, "area_a": 30},
    {"name": "蟹沢",   "group": "東根", "lat": 38.4100, "lon": 140.4100, "area_a": 20},
]
FIELD_DETECT_RADIUS_M = 300   # この距離（m）以内なら圃場と判定

# ユーザーごとの入力途中データを一時保持（サーバー再起動でリセットされます）
_pending = {}


# ======================================================
# 補助関数
# ======================================================

def detect_field(lat: float, lon: float) -> dict | None:
    """GPS座標から最寄り圃場を返す。範囲外はNone。"""
    best, best_dist = None, float("inf")
    for f in FIELDS:
        dlat = (lat - f["lat"]) * 111_000
        dlon = (lon - f["lon"]) * 111_000 * math.cos(math.radians(lat))
        dist = math.sqrt(dlat**2 + dlon**2)
        if dist < best_dist:
            best_dist, best = dist, f
    return best if best_dist <= FIELD_DETECT_RADIUS_M else None


def get_weather(lat: float = 38.38, lon: float = 140.40) -> str:
    """OpenWeatherMap から現在の天気を取得する。"""
    if not WEATHER_KEY:
        return "天気未取得"
    try:
        url = (
            "https://api.openweathermap.org/data/2.5/weather"
            f"?lat={lat}&lon={lon}&appid={WEATHER_KEY}&lang=ja&units=metric"
        )
        d = requests.get(url, timeout=5).json()
        desc = d["weather"][0]["description"]
        temp = round(d["main"]["temp"])
        return f"{desc}・{temp}℃"
    except Exception:
        return "天気取得失敗"


def transcribe_audio(audio_bytes: bytes) -> str:
    """OpenAI Whisper で音声をテキストに変換する。"""
    with tempfile.NamedTemporaryFile(suffix=".m4a", delete=False) as f:
        f.write(audio_bytes)
        tmp = f.name
    try:
        with open(tmp, "rb") as af:
            result = openai_client.audio.transcriptions.create(
                model="whisper-1", file=af, language="ja"
            )
        return result.text
    finally:
        os.unlink(tmp)


def parse_work(text: str, weather: str, field: dict | None) -> dict:
    """Claude が作業テキストを構造化JSONに変換する。"""
    field_hint = f"{field['group']} / {field['name']}" if field else "不明"

    prompt = f"""あなたは農作業記録AIです。
農家が話した内容を以下のJSON形式に整理してください。

【農家の入力】
{text}

【自動取得済み情報】
圃場: {field_hint}
天気: {weather}
日時: {datetime.now().strftime('%Y-%m-%d %H:%M')}

【作業項目の選択肢（最も近いものを選ぶ）】
せん定 / 整枝 / 下垂誘引 / 芽傷入れ / ねぎ袋作業 /
摘蕾 / 摘花 / 摘果 / 手受粉 /
定植 / 苗木管理 / 仮植 /
防除（手散布）/ 防除（SS散布）/ 除草剤散布 / 葉面散布 /
草刈（刈払機）/ 草刈（乗用モア）/ 株周り草取 /
かん水 / 元肥 / 追肥 /
パイプ打ち / 青ポール立て / 状況確認 / その他

JSONのみ返してください（説明文は不要）:
{{
  "作業項目":   "選択肢から最も近いもの",
  "作業エリア": "エリア名・列番号など（不明なら空欄）",
  "完了数量":   "本数・列数など（不明なら空欄）",
  "気づき課題": "問題点・特記事項（なければ空欄）",
  "不足項目":   ["必須なのに不明な項目のリスト。天気・圃場は自動取得済みなので除く"]
}}"""

    msg = claude.messages.create(
        model="claude-opus-4-5",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = msg.content[0].text
    return json.loads(raw[raw.find("{") : raw.rfind("}") + 1])


def build_confirm_text(parsed: dict, weather: str, field: dict | None) -> str:
    """農家への確認メッセージを組み立てる。"""
    fn = f"{field['group']} / {field['name']}" if field else "不明（位置情報を送ってください）"
    lines = [
        "📋 以下の内容で記録します\n",
        f"🌾 圃場　　　：{fn}",
        f"🌤 天気　　　：{weather}",
        f"🔨 作業項目　：{parsed.get('作業項目','不明')}",
        f"📍 作業エリア：{parsed.get('作業エリア','（未入力）') or '（未入力）'}",
        f"📊 完了数量　：{parsed.get('完了数量','（未入力）') or '（未入力）'}",
    ]
    if parsed.get("気づき課題"):
        lines.append(f"📝 気づき　　：{parsed['気づき課題']}")
    lines.append(f"⏰ 記録時刻　：{datetime.now().strftime('%H:%M')}")

    missing = parsed.get("不足項目", [])
    if missing:
        lines += ["", "⚠️ 以下が不足しています："]
        for m in missing:
            lines.append(f"  → {m}を教えてください")
        lines.append("\n内容を追加して送り直してください。")
    else:
        lines += ["", "✅「OK」と送ると記録が完了します"]
        lines.append("修正する場合は内容を送り直してください")

    return "\n".join(lines)


def save_to_sheet(uid: str, parsed: dict, weather: str, field: dict | None):
    """Google Sheets に1行追記する。"""
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    # 環境変数からcredentials.jsonを取得（base64エンコード済み）
    creds_b64 = os.environ.get("GOOGLE_CREDENTIALS_JSON", "")
    if creds_b64:
        creds_dict = json.loads(base64.b64decode(creds_b64).decode())
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    else:
        creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
    client = gspread.authorize(creds)
    sheet  = client.open(SHEET_NAME).sheet1

    # 1行目にヘッダーがなければ追加
    if not sheet.get_all_values():
        sheet.append_row([
            "日付", "時刻", "作業者ID",
            "圃場グループ", "圃場名", "面積(a)",
            "作業項目", "作業エリア", "完了数量",
            "天気", "気づき・課題",
        ])

    now = datetime.now()
    sheet.append_row([
        now.strftime("%Y-%m-%d"),
        now.strftime("%H:%M"),
        uid,
        field["group"]  if field else "",
        field["name"]   if field else "",
        field["area_a"] if field else "",
        parsed.get("作業項目", ""),
        parsed.get("作業エリア", ""),
        parsed.get("完了数量", ""),
        weather,
        parsed.get("気づき課題", ""),
    ])


def _process_text(uid: str, text: str, reply_token: str):
    """テキスト・音声共通の処理ロジック。"""
    ctx     = _pending.get(uid, {})
    field   = ctx.get("field")
    lat     = ctx.get("lat", 38.38)
    lon     = ctx.get("lon", 140.40)
    weather = get_weather(lat, lon)
    parsed  = parse_work(text, weather, field)

    _pending[uid] = {**ctx, "parsed": parsed, "weather": weather}

    reply = build_confirm_text(parsed, weather, field)
    line_bot_api.reply_message(reply_token, TextSendMessage(text=reply))


# ======================================================
# LINE Webhook エンドポイント
# ======================================================

@app.route("/callback", methods=["POST"])
def callback():
    sig  = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, sig)
    except InvalidSignatureError:
        abort(400)
    return "OK"


@app.route("/", methods=["GET"])
def health():
    return "🌾 農作業記録Bot 稼働中"


# ======================================================
# テキストメッセージ
# ======================================================
@handler.add(MessageEvent, message=TextMessage)
def on_text(event):
    uid  = event.source.user_id
    text = event.message.text.strip()

    # --- OK → 保存 ---
    if text.upper() in ["OK", "ＯＫ", "確認", "保存", "記録"]:
        ctx = _pending.get(uid, {})
        if "parsed" not in ctx:
            reply = "記録する内容がありません。\nまず作業内容を送ってください。"
        else:
            try:
                save_to_sheet(uid, ctx["parsed"], ctx["weather"], ctx.get("field"))
                _pending.pop(uid, None)
                reply = "✅ 記録しました！\nお疲れさまでした🍎"
            except Exception as e:
                reply = f"⚠️ 保存エラーが発生しました。\n管理者に連絡してください。\n({e})"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return

    # --- キャンセル ---
    if text in ["キャンセル", "取消", "やめる"]:
        _pending.pop(uid, None)
        line_bot_api.reply_message(
            event.reply_token, TextSendMessage(text="入力をキャンセルしました。")
        )
        return

    # --- ヘルプ ---
    if text in ["ヘルプ", "help", "使い方", "？"]:
        reply = (
            "【農作業記録Bot 使い方】\n\n"
            "1️⃣ まず位置情報を送ると圃場を自動判定します\n"
            "   ＋ボタン → 位置情報 → 現在地を送信\n\n"
            "2️⃣ 作業内容を音声または文字で送ってください\n"
            "   例：「せん定終わり、北3〜5列、120本」\n\n"
            "3️⃣ 内容を確認して「OK」で記録完了\n\n"
            "📝 修正 → 内容を送り直す\n"
            "❌ 取消 → 「キャンセル」と送る"
        )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return

    # --- 作業内容の解析 ---
    _process_text(uid, text, event.reply_token)


# ======================================================
# 音声メッセージ
# ======================================================
@handler.add(MessageEvent, message=AudioMessage)
def on_audio(event):
    uid = event.source.user_id

    # LINEから音声データを取得
    content = line_bot_api.get_message_content(event.message.id)
    audio_bytes = b"".join(content.iter_content())

    # Whisper で文字起こし
    try:
        text = transcribe_audio(audio_bytes)
    except Exception as e:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text="音声の認識に失敗しました。\nもう一度送るか、文字で入力してください。"
            ),
        )
        return

    # 文字起こし結果を表示してから解析
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=f"🎤 聞き取り結果：\n「{text}」\n\n処理中..."),
    )
    _process_text(uid, text, event.reply_token)


# ======================================================
# 位置情報（GPS → 圃場自動判定）
# ======================================================
@handler.add(MessageEvent, message=LocationMessage)
def on_location(event):
    uid = event.source.user_id
    lat = event.message.latitude
    lon = event.message.longitude

    field = detect_field(lat, lon)
    _pending.setdefault(uid, {})
    _pending[uid].update({"field": field, "lat": lat, "lon": lon})

    if field:
        reply = (
            f"📍 圃場を検知しました！\n"
            f"　{field['group']} / {field['name']}（{field['area_a']}a）\n\n"
            f"作業内容を音声または文字で送ってください 🎤\n"
            f"例：「せん定終わり、北3〜5列、120本」"
        )
    else:
        reply = (
            "📍 位置情報を受け取りましたが\n"
            "登録済み圃場が見つかりませんでした。\n\n"
            "作業内容に圃場名を含めて送ってください。\n"
            "例：「荒谷①でせん定、北3列完了」"
        )
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))


# ======================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
