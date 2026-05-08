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

import os, json, tempfile, math, time
from datetime import datetime

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

JST = pytz.timezone("Asia/Tokyo")

import requests
from flask import Flask, request, abort

from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, AudioMessage, LocationMessage,
    TextSendMessage, QuickReply, QuickReplyButton, LocationAction,
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
OPENAI_KEY  = os.environ.get("OPENAI_API_KEY", "")  # 音声認識用（任意）
WEATHER_KEY = os.environ.get("WEATHER_API_KEY", "")
SHEET_NAME  = os.environ.get("SHEET_NAME", "農作業記録")

line_bot_api  = LineBotApi(LINE_TOKEN)
handler       = WebhookHandler(LINE_SECRET)
claude        = anthropic.Anthropic(api_key=CLAUDE_KEY)
openai_client = OpenAI(api_key=OPENAI_KEY) if OPENAI_KEY else None

# ======================================================
# 圃場マスタ（Googleスプレッドシート「圃場マスタ」シートから読み込む）
# ======================================================
FIELD_DETECT_RADIUS_M = 300   # この距離（m）以内なら圃場と判定
FIELDS_CACHE_TTL      = 300   # 5分キャッシュ
_fields_cache      = []
_fields_cache_time = 0.0


def get_gspread_client():
    """Google Sheets クライアントを返す（共通処理）。"""
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds_b64 = os.environ.get("GOOGLE_CREDENTIALS_JSON", "")
    if creds_b64:
        creds_dict = json.loads(base64.b64decode(creds_b64).decode())
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    else:
        creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
    return gspread.authorize(creds)


def load_fields(force: bool = False) -> list:
    """スプレッドシートの「圃場マスタ」シートから圃場一覧を読み込む（5分キャッシュ）。"""
    global _fields_cache, _fields_cache_time
    if not force and _fields_cache and (time.time() - _fields_cache_time) < FIELDS_CACHE_TTL:
        return _fields_cache
    try:
        sheet  = get_gspread_client().open(SHEET_NAME).worksheet("圃場マスタ")
        rows   = sheet.get_all_records()
        fields = []
        for row in rows:
            try:
                fields.append({
                    "name":   str(row.get("圃場名", "")).strip(),
                    "group":  str(row.get("グループ", "")).strip(),
                    "lat":    float(row.get("緯度", 0)),
                    "lon":    float(row.get("経度", 0)),
                    "area_a": row.get("面積(a)", ""),
                })
            except (ValueError, TypeError):
                continue
        _fields_cache      = fields
        _fields_cache_time = time.time()
        return fields
    except Exception:
        return _fields_cache  # エラー時は古いキャッシュを返す


# ユーザーごとの入力途中データを一時保持（サーバー再起動でリセットされます）
_pending = {}

# ユーザーごとの作業開始時刻（「作業開始」コマンドでセット）
_work_start: dict[str, datetime] = {}


# ======================================================
# 作業状態シート（開始・完了をGoogleSheetsに永続保存）
# ======================================================

def get_status_sheet():
    """「作業状態」シートを返す（なければ自動作成）。"""
    ss = get_gspread_client().open(SHEET_NAME)
    try:
        return ss.worksheet("作業状態")
    except Exception:
        ws = ss.add_worksheet(title="作業状態", rows=1000, cols=4)
        ws.append_row(["日付", "作業者ID", "開始時刻", "ステータス"])
        return ws


def save_work_start_to_sheet(uid: str, start_dt: datetime):
    """「開始中」行をシートに追記する。"""
    try:
        ws = get_status_sheet()
        ws.append_row([
            start_dt.strftime("%Y-%m-%d"),
            uid,
            start_dt.strftime("%H:%M"),
            "開始中",
        ])
    except Exception as e:
        print(f"[作業状態] 書き込みエラー: {e}")


def mark_work_status(uid: str, status: str):
    """今日の最新「開始中」行を指定ステータスに更新する。"""
    try:
        ws   = get_status_sheet()
        today = datetime.now(JST).strftime("%Y-%m-%d")
        rows  = ws.get_all_values()
        for i in range(len(rows) - 1, -1, -1):
            row = rows[i]
            if len(row) >= 4 and row[0] == today and row[1] == uid and row[3] == "開始中":
                ws.update_cell(i + 1, 4, status)  # 1-indexed
                break
    except Exception as e:
        print(f"[作業状態] 更新エラー: {e}")


# ======================================================
# 18時の未完了通知
# ======================================================

def send_evening_reminders():
    """毎日18時JST に、作業未完了者へ名指しでLINE通知を送る。"""
    try:
        ws    = get_status_sheet()
        today = datetime.now(JST).strftime("%Y-%m-%d")
        rows  = ws.get_all_values()

        # 今日「開始中」のままのユーザーIDを収集
        pending_uids = set()
        for row in rows:
            if len(row) >= 4 and row[0] == today and row[3] == "開始中":
                pending_uids.add(row[1])

        if not pending_uids:
            return  # 全員完了済み

        for uid in pending_uids:
            try:
                # LINE表示名を取得して名指しメッセージを送る
                profile = line_bot_api.get_profile(uid)
                name    = profile.display_name
                msg = (
                    f"⏰ {name}さん、18時になりました。\n\n"
                    "本日の作業記録がまだ未完了です。\n\n"
                    "📍 場所が変わっている場合は、先に\n"
                    "「位置情報」を送ってください。\n"
                    "（＋ボタン → 位置情報 → 現在地）\n\n"
                    "その後、作業内容を送ってください。\n\n"
                    "今日の記録が不要な場合は\n"
                    "「キャンセル」と送ってください。"
                )
                line_bot_api.push_message(uid, TextSendMessage(text=msg))
            except Exception as e:
                print(f"[18時通知] {uid} への送信エラー: {e}")

    except Exception as e:
        print(f"[18時通知] 全体エラー: {e}")


# ======================================================
# 補助関数
# ======================================================

def detect_field(lat: float, lon: float) -> dict | None:
    """GPS座標から最寄り圃場を返す。範囲外はNone。"""
    best, best_dist = None, float("inf")
    for f in load_fields():
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
    if not openai_client:
        raise RuntimeError("OPENAI_API_KEY が設定されていません")
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


def fill_missing(text: str, missing_items: list, current_parsed: dict) -> dict:
    """不足項目への追加入力から情報を補完する。"""
    prompt = f"""農家が不足していた情報を追加で入力しました。

【追加入力】
{text}

【現在の記録内容】
{json.dumps(current_parsed, ensure_ascii=False, indent=2)}

【まだ不足していた項目】
{', '.join(missing_items)}

追加入力の内容を現在の記録内容に反映してください。
JSONのみ返してください（説明文不要）:
{{
  "作業項目":   "既存の値、または追加入力で更新された値",
  "完了数量":   "既存の値、または追加入力で更新された値",
  "気づき課題": "既存の値、または追加入力で更新された値",
  "不足項目":   ["まだ不明な必須項目のリスト（解決済みは除く）"]
}}"""

    msg = claude.messages.create(
        model="claude-opus-4-5",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = msg.content[0].text
    return json.loads(raw[raw.find("{") : raw.rfind("}") + 1])


def build_confirm_text(parsed: dict, weather: str, field: dict | None,
                       work_start: datetime | None = None) -> str:
    """農家への確認メッセージを組み立てる。"""
    fn = f"{field['group']} / {field['name']}" if field else "不明（位置情報を送ってください）"
    now = datetime.now()
    lines = [
        "📋 以下の内容で記録します\n",
        f"🌾 圃場　　　：{fn}",
        f"🌤 天気　　　：{weather}",
        f"🔨 作業項目　：{parsed.get('作業項目','不明')}",
        f"📊 完了数量　：{parsed.get('完了数量','（未入力）') or '（未入力）'}",
    ]
    if parsed.get("気づき課題"):
        lines.append(f"📝 気づき　　：{parsed['気づき課題']}")
    lines.append(f"⏰ 記録時刻　：{now.strftime('%H:%M')}")

    # 作業開始時刻が記録されていれば作業時間を表示
    if work_start:
        elapsed_min = int((now - work_start).total_seconds() / 60)
        h, m = divmod(elapsed_min, 60)
        wt_str = f"{h}時間{m}分" if h > 0 else f"{m}分"
        lines.append(f"⏱ 作業時間　：{wt_str}（{work_start.strftime('%H:%M')}〜）")

    missing = parsed.get("不足項目", [])
    if missing:
        lines += ["", "⚠️ 以下を教えてください："]
        for m in missing:
            lines.append(f"  → {m}は？")
        lines.append("\n（不足している情報だけ送ってください）")
    else:
        lines += ["", "✅「OK」と送ると記録が完了します"]
        lines.append("修正する場合は内容を送り直してください")

    return "\n".join(lines)


def save_to_sheet(uid: str, parsed: dict, weather: str, field: dict | None):
    """Google Sheets に1行追記する（常にA列から書き込む）。"""
    client = get_gspread_client()
    sheet  = client.open(SHEET_NAME).sheet1

    # 作業時間の計算
    start_dt = _work_start.pop(uid, None)
    now = datetime.now(JST)
    if start_dt:
        elapsed_min    = int((now - start_dt).total_seconds() / 60)
        h, m           = divmod(elapsed_min, 60)
        work_time      = f"{h}時間{m}分" if h > 0 else f"{m}分"
        start_time_str = start_dt.strftime("%H:%M")
    else:
        work_time      = ""
        start_time_str = ""

    # LINE表示名を取得
    try:
        display_name = line_bot_api.get_profile(uid).display_name
    except Exception:
        display_name = ""

    # 現在の行数を取得して次の書き込み行を決定（常にA列から始まる）
    all_vals = sheet.get_all_values()
    next_row = len(all_vals) + 1

    # シートが空ならヘッダーを1行目に追加
    if next_row == 1:
        sheet.update([
            ["日付", "作業者名", "作業者ID", "開始時刻", "終了時刻", "作業時間",
             "圃場グループ", "圃場名", "面積(a)",
             "作業項目", "完了数量", "天気", "気づき・課題"]
        ], "A1")
        next_row = 2

    # データ行を書き込む
    row_data = [
        now.strftime("%Y-%m-%d"),
        display_name,
        uid,
        start_time_str,
        now.strftime("%H:%M"),
        work_time,
        field["group"]  if field else "",
        field["name"]   if field else "",
        field["area_a"] if field else "",
        parsed.get("作業項目", ""),
        parsed.get("完了数量", ""),
        weather,
        parsed.get("気づき課題", ""),
    ]
    sheet.update([row_data], f"A{next_row}")


def _process_text(uid: str, text: str, reply_token: str):
    """テキスト・音声共通の処理ロジック。"""
    ctx   = _pending.get(uid, {})
    field = ctx.get("field")
    lat   = ctx.get("lat", 38.38)
    lon   = ctx.get("lon", 140.40)

    # 不足項目への追加入力なら補完モード（前回データ＋今回入力をマージ）
    if "parsed" in ctx and ctx.get("missing"):
        weather = ctx.get("weather", get_weather(lat, lon))
        parsed  = fill_missing(text, ctx["missing"], ctx["parsed"])
    else:
        weather = get_weather(lat, lon)
        parsed  = parse_work(text, weather, field)

    missing = parsed.get("不足項目", [])
    _pending[uid] = {**ctx, "parsed": parsed, "weather": weather, "missing": missing}

    reply = build_confirm_text(parsed, weather, field, _work_start.get(uid))
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

    # --- 作業開始 → タイマースタート ---
    if text in ["作業開始", "開始"]:
        _work_start[uid] = datetime.now(JST)
        _pending.pop(uid, None)  # 前回の入力途中データをリセット
        save_work_start_to_sheet(uid, _work_start[uid])   # シートに永続保存
        now_str = _work_start[uid].strftime("%H:%M")
        reply = (
            f"⏱ 作業開始を記録しました（{now_str}）\n\n"
            "📍 まず現在地の位置情報を送ってください\n"
            "（下のボタンをタップするだけでOK）"
        )
        quick_reply = QuickReply(items=[
            QuickReplyButton(action=LocationAction(label="📍 位置情報を送る"))
        ])
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=reply, quick_reply=quick_reply)
        )
        return

    # --- OK → 保存 ---
    if text.upper() in ["OK", "ＯＫ", "確認", "保存", "記録"]:
        ctx = _pending.get(uid, {})
        if "parsed" not in ctx:
            reply = "記録する内容がありません。\nまず作業内容を送ってください。"
        else:
            try:
                save_to_sheet(uid, ctx["parsed"], ctx["weather"], ctx.get("field"))
                mark_work_status(uid, "完了")   # 作業状態シートを更新
                _pending.pop(uid, None)
                reply = "✅ 記録しました！\nお疲れさまでした🍎"
            except Exception as e:
                reply = f"⚠️ 保存エラーが発生しました。\n管理者に連絡してください。\n({e})"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return

    # --- 圃場マスタ更新 ---
    if text in ["圃場更新", "圃場リスト", "圃場一覧"]:
        fields = load_fields(force=True)
        if fields:
            lines = [f"📋 圃場マスタ（{len(fields)}件）\n"]
            for f in fields:
                lines.append(f"🌾 {f['group']} / {f['name']}（{f['area_a']}a）")
            lines.append("\nスプレッドシートの「圃場マスタ」シートを編集後\n「圃場更新」と送ると反映されます")
            reply = "\n".join(lines)
        else:
            reply = "⚠️ 圃場マスタが読み込めませんでした。\nスプレッドシートに「圃場マスタ」シートがあるか確認してください。"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return

    # --- キャンセル ---
    if text in ["キャンセル", "取消", "やめる"]:
        _pending.pop(uid, None)
        _work_start.pop(uid, None)
        mark_work_status(uid, "キャンセル")  # 作業状態シートを更新
        line_bot_api.reply_message(
            event.reply_token, TextSendMessage(text="入力をキャンセルしました。")
        )
        return

    # --- ヘルプ ---
    if text in ["ヘルプ", "help", "使い方", "？"]:
        reply = (
            "【農作業記録Bot 使い方】\n\n"
            "1️⃣ 「作業開始」と送るとタイマースタート\n\n"
            "2️⃣ 位置情報を送ると圃場を自動判定します\n"
            "   ＋ボタン → 位置情報 → 現在地を送信\n\n"
            "3️⃣ 作業内容を音声または文字で送ってください\n"
            "   例：「せん定終わり、北3〜5列、120本」\n\n"
            "4️⃣ 内容を確認して「OK」で記録完了\n"
            "   ※ 作業時間が自動で計算されます\n\n"
            "📝 修正 → 内容を送り直す\n"
            "❌ 取消 → 「キャンセル」と送る\n"
            "📋 圃場確認 → 「圃場一覧」と送る"
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
# スケジューラー起動（毎日18時JST に未完了者へ通知）
# ======================================================
_scheduler = BackgroundScheduler(timezone=JST)
_scheduler.add_job(
    send_evening_reminders,
    CronTrigger(hour=18, minute=0, timezone=JST),
)
_scheduler.start()


# ======================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
