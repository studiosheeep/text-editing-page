# iPad Editor Bridge (Blenderアドオン)

iPad Safari のローポリ頂点融解エディタと Blender を LAN で繋ぐアドオン。
iPad で編集したメッシュをボタン一発で Blender の選択オブジェクトに反映できる。

## インストール

1. このフォルダ (`blender_addon/`) を zip 化する
   - macOS: Finder で右クリック → 圧縮
   - Windows: 右クリック → 送る → 圧縮 (zip形式)
   - CLI: `cd .. && zip -r ipad_editor_bridge.zip blender_addon`
2. Blender を起動 → Edit → Preferences → Add-ons → Install...
3. 作った zip を選択
4. アドオンリストで「Import-Export: iPad Editor Bridge」にチェック

## 使い方

1. **PC (Blender側)**
   - 3Dビューポートの右サイドバー (`N`キー) → 「iPad Bridge」タブ
   - 「サーバー開始」をクリック
   - 表示された URL (例: `http://192.168.1.50:8765/`) を控える
2. **iPad (Safari)**
   - Safariで上記URLを開く
   - Bridge検出時、上部ツールバーに `Blender→` と `→Blender` が追加表示される
3. **同期**
   - Blenderで対象メッシュをアクティブにする (クリックして選択)
   - iPadで `Blender→` をタップ → 現在のメッシュを取得
   - 編集 (頂点タップ → 融解 → …)
   - `→Blender` をタップ → Blenderのアクティブメッシュを差し替え

## 前提

- iPadとPCが同じLAN (Wi-Fi) に接続されていること
- OS のファイアウォールでポート `8765` (デフォルト) を許可すること
  - macOS: システム設定 → ネットワーク → ファイアウォール → Blender を許可
  - Windows: 初回起動時にプロンプトが出るので「許可」

## エンドポイント (デバッグ用)

- `GET  /`               エディタHTML
- `GET  /bridge/status`  `{"app": "ipad-editor-bridge", "version": "1.0"}`
- `GET  /bridge/current` アクティブメッシュを OBJ (ワールド座標) で返す
- `POST /bridge/apply`   本文の OBJ をアクティブメッシュに反映 (なければ新規作成)

## 注意

- Blender→iPad 送信時は **ワールド座標** に変換して送る
- iPad→Blender 反映時は対象オブジェクトの逆変換を掛けてローカル座標に戻す
  (つまり位置/回転/スケールは既存オブジェクトの値を維持)
- OBJ の面インデックスのみ扱い、UV/マテリアル/シェイプキー等は破棄される
- ローポリ (300頂点以下) 想定。大きいメッシュではネットワーク送信に時間がかかる
