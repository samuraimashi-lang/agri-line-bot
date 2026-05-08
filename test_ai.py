"""
AI解析テストスクリプト
==============================
LINEやサーバーなしで、Claude AIの解析機能だけをテストします。
APIキーを設定した後、このファイルを実行してください。

実行方法：
  python test_ai.py
"""

import os, json
import anthropic

# ★ テスト前にここにAPIキーを貼り付けてください
os.environ["ANTHROPIC_API_KEY"] = "ここにAPIキーを貼り付け"

client = anthropic.Anthropic()

TEST_CASES = [
    "せん定終わり、北3列から5列、120本完了",
    "今日は雨の中、荒谷①で下垂誘引した。南エリア全部で約80本",
    "摘果、神町、ふじ150本。7列目に病気っぽいのがあった",
    "草刈り乗用モアで全部終わらせた",
    "SS散布、3回目、今日は全圃場やった",
]

def parse(text: str) -> dict:
    prompt = f"""あなたは農作業記録AIです。
農家が話した内容を以下のJSON形式に整理してください。

【農家の入力】
{text}

JSONのみ返してください（説明文不要）:
{{
  "作業項目":   "せん定/摘果/下垂誘引/草刈/防除などから最も近いもの",
  "作業エリア": "エリア名・列番号（不明なら空欄）",
  "完了数量":   "本数・列数など（不明なら空欄）",
  "気づき課題": "問題点・特記事項（なければ空欄）",
  "不足項目":   ["作業エリアまたは完了数量が不明な場合にその項目名"]
}}"""
    msg = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = msg.content[0].text
    return json.loads(raw[raw.find("{"):raw.rfind("}")+1])

print("=" * 50)
print("農作業記録Bot　AI解析テスト")
print("=" * 50)

for i, text in enumerate(TEST_CASES, 1):
    print(f"\n【テスト {i}】")
    print(f"入力：「{text}」")
    try:
        result = parse(text)
        print(f"✅ 作業項目：{result.get('作業項目','不明')}")
        print(f"   エリア　：{result.get('作業エリア','不明') or '（未記入）'}")
        print(f"   数量　　：{result.get('完了数量','不明') or '（未記入）'}")
        print(f"   気づき　：{result.get('気づき課題','') or '（なし）'}")
        if result.get('不足項目'):
            print(f"   ⚠️ 不足：{', '.join(result['不足項目'])}")
    except Exception as e:
        print(f"❌ エラー：{e}")

print("\n" + "=" * 50)
print("テスト完了！")
print("うまく動いていれば、次のステップに進んでください。")
print("=" * 50)
