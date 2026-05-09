# My Alerts Digest

Google AlertsのRSSフィードを毎日自動取得し、Gemini APIで日本語要約してGitHub Pagesで公開するシステムです。費用は**完全無料**。

## 構成

```
Google Alerts (RSS) → GitHub Actions → Gemini API → GitHub Pages
```

## セットアップ手順

### 1. GitHubリポジトリを作成

GitHubで新しいパブリックリポジトリを作成し、このディレクトリの内容を push します。

```bash
git init
git add .
git commit -m "initial setup"
git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPO.git
git push -u origin main
```

### 2. Gemini APIキーを取得

1. https://aistudio.google.com/app/apikey を開く
2. 「Create API key」→ キーをコピー（クレジットカード登録不要）

### 3. GitHub Secrets にキーを登録

リポジトリの **Settings → Secrets and variables → Actions → New repository secret**:

- Name: `GEMINI_API_KEY`
- Value: （取得したキー）

### 4. GitHub Pages を有効化

リポジトリの **Settings → Pages**:
- Source: `Deploy from a branch`
- Branch: `main` / folder: `/docs`
- 「Save」

### 5. Google Alerts を RSS に切り替え

1. https://www.google.com/alerts を開く
2. 監視したいキーワードでアラートを作成
3. 作成したアラートの「オプション」→「配信先」→「RSSフィード」に変更
4. アラート右端の RSS アイコンを右クリックしてURLをコピー

### 6. フィードを登録

`https://YOUR_USERNAME.github.io/YOUR_REPO/admin.html` を開いて:

1. GitHubユーザー名・リポジトリ名・Personal Access Token を入力して「設定を保存」
   - PAT作成: https://github.com/settings/tokens/new?scopes=repo
2. コピーしたRSS URLをフィードとして追加

### 7. 動作確認

リポジトリの **Actions** タブ → `Daily Alerts Digest` → `Run workflow` で手動実行。

まとめ読みサイト: `https://YOUR_USERNAME.github.io/YOUR_REPO/`

---

## admin.html のパスワードを変更する場合

```bash
# 新しいパスワードのSHA-256ハッシュを取得
echo -n 'YOUR_NEW_PASSWORD' | shasum -a 256
```

取得したハッシュを `docs/admin.html` の `PW_HASH` 変数に貼り替えてください。

---

## コスト

| コンポーネント | 費用 |
|---|---|
| Google Alerts | 無料 |
| GitHub Actions | 無料（パブリックリポジトリ） |
| Gemini API (gemini-2.0-flash-lite) | 無料（1,500 req/日） |
| GitHub Pages | 無料 |
| **合計** | **¥0** |
