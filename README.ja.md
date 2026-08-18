<!-- English version: README.md -->
# daicho

長期間動き続けるアシスタントのための、追記専用の記憶台帳。

会話ログとノートを渡すと、書き換えのできないエピソードとして記帳し、任意の
言語モデルで人物・場所・機材と日付つきの約束を収穫し、確信が持てないものは
レビュー待ち行列に置いて人間の判断を待つ。検索は日本語でも英語でも同じ全文
索引で引ける。

依存パッケージなし。Python 3.10以上と標準ライブラリだけで動く（ログは
`json`、索引は`sqlite3`のFTS5、モデル呼び出しは`subprocess`）。

```bash
pip install daicho
daicho init
daicho ingest
daicho --llm-cmd "your-model-cli --quiet" extract
daicho review list
daicho reindex
daicho search "会場はどこにするって決めたっけ"
```

ライブラリとして使う場合:

```python
from daicho import Config, ingest, extract, search

cfg = Config.load("/srv/memory").ensure_dirs()
ingest.run(cfg)                      # セッション -> エピソード
extract.run(cfg)                     # エピソード -> エンティティ・コミットメント（staging）
search.build_index(cfg)
hits = search.search(cfg, "秋の清掃の集合場所")
```

`python examples/quickstart.py` を実行すると、架空データとモックモデルで
一連の流れをネットワークなしで確認できる。

## この形にした理由

アシスタントの記憶は、複数のプロセスが同時に書き込むファイル群と、監査の
できないベクトルストアになりがち。そこから予測できる形で4つの壊れ方が起きる。
以下の規約はそれぞれを1つずつ潰すためにある。

### 1. 正本は追記専用のイベントログ

全ての記録は `events/YYYY-MM.jsonl` に1行1イベントで追記される。エンティティ
台帳と検索索引は**導出物**で、消しても再構築できる。

```bash
rm -rf registry/ index/
daicho rebuild-registry && daicho reindex
```

導出物にしか存在しない事実を作ると、DBの破損がそのまま記憶の喪失になる。
来歴・巻き戻し・復旧はすべてこの性質から出てくる。

### 2. 全イベントに writer を必須にする

`type` / `at` / `writer` が無いイベントはAPIの入口で弾く。複数セッション・
夜間ワーカー・サブエージェントが同じ記憶を触る構成では、後になって必ず
「これは誰が、どのエピソードを根拠に主張したのか」を問うことになる。
それは後から復元できない。

### 3. カーソルは毎回再計算する。ファイルに持たない

「どのターンまで取り込んだか」はイベントログ自身から毎回導出する
（`events.session_cursors`）。カーソルファイルは第二の正本で、腐ると冪等性
ごと壊れる。症状は静かなデータ欠落か、無限の再取り込みのどちらか。
エピソードの同一性も内容から導出する（`セッションID + ターン範囲 + 内容
ハッシュ`）ので、ログを圧縮アーカイブへ退避しても再取り込みは起きない。

### 4. 抽出は必ずstagingで止まる

会話ログを読む言語モデルは推測器で、外したときの損害が非対称になる。人物の
取り違え（「同じ姓の2人のどちらか」）は、それ以降に書かれるもの全部を静かに
汚染する。

人間を通さずに台帳へ入れる規則はひとつだけ:

> 既存のどの表記とも衝突せず、**かつ**異なるエピソードN件以上に出現している
> （既定3、`auto_confirm_min_episodes`）。

それ以外は全て提案になる。既存名と重なる名前、確信度の低い抽出、リマインダー
が見当たらない日付つき記述など。

```
$ daicho review list
prop_1762…_01b776de  entity 山田 (person) -- collides with the existing entity '山田花子'
prop_1762…_3d07c83e  commitment 9月までに許可証を更新する -- dated statement with no matching reminder

$ daicho review approve prop_1762…_01b776de       # 別人として登録
$ daicho review approve prop_1762…_01b776de --merge-into person:yamada_hanako
$ daicho review reject  prop_1762…_3d07c83e --note "カレンダーに入っている"
```

別エンティティとして承認すると、双方向に `confusable_with` が記録される。
同じ紛らわしさを二度議論しないで済む。

待ち行列自体もイベントログの投影（`proposal_opened` / `proposal_deferred` /
`proposal_resolved`）なので、判断の履歴が追え、導出ファイルを全部失っても
待ち行列は残る。

## 失敗の規律

静かに失敗する記憶ワーカーは、止まるワーカーより悪い。動いている顔をしたまま
学習だけが止まる。

* LLM呼び出しは `llm_retries` 回（既定2）まで再試行する。それでも失敗した
  バッチは**未走査のまま**残す。やっていない仕事に「済んだ印」を書かない。
  次の実行が再試行する。
* 連続 `max_consecutive_failures` バッチ（既定5）失敗したら、設定された通知
  コマンドを叩いて**非ゼロ終了**する。無言の無限リトライは耐障害性ではない。
* `ingest` は毎回、取り込み済みセッションの原本が消えていないか、月次アーカイブ
  に歯抜けが無いかを確認する。どちらも非ゼロ終了で、誰も読まない警告にはしない。
* 提案は1実行あたり `max_proposals_per_run`（既定10、系統ごと）で頭打ちにする。
  溢れた分は持ち越して次回の冒頭で捌く。うるさい夜が人を埋めることも、
  提案が消えることもない。

## 検索

`daicho search` は記憶への**唯一の読み出し経路**として使う想定。取り出し
経路が複数あって少しずつロジックが違うと、答えが「たまたまどの経路を通ったか」
で変わる。

**trigramではなくbigram。** FTS5には空白で区切らない言語向けとしてよく勧め
られる `trigram` トークナイザがあるが、3文字未満のクエリは1つもtrigramを作れ
ないので構造的に0件になる。そして日本語で実際に検索される語（姓・地名・日常
の名詞）は2文字のものが非常に多い。そこでこのライブラリは、索引時にCJKの連続を
重なりつきのbigramへ分解し、クエリにも同じ変換をかけて標準の `unicode61` で
引く。ASCII語はそのまま小文字化して残す。この差は
`tests/test_search.py` で実際に示している。

ほかに知っておくとよい性質:

* **0件を返せる。** 最上位スコアの `min_ratio` 未満のヒットは捨てる。`top_k`
  を埋めにいくと、無関係なテキストが根拠として引用される。
* **別名でクエリを広げる。** 2文字の通称単独では一般文書に埋もれるが、台帳が
  同じ `OR` 節に珍しい正規表記を足すと順位が自然に正しくなる。`terms` を明示
  したときは呼び出し側の選択を信頼して広げない。
* **整理済みノートは生ログより上に来る。** 種別ごとの重み（`kind_weights`）で
  調整できる。ログは件数が多く、放っておくと上位を占める。
* **セッション途中のヒットには最終ターンを添える。** スニペット検索は自己訂正を
  構造的に見えなくする。20分後の訂正はクエリに一致せず、自信のある誤りだけが
  一致する。結論が後で覆っている可能性を読み手に見せる。
* **結果は `<retrieved_memory>` で包み、参考情報だと明記する。** 記憶には過去の
  モデル出力やWeb由来のテキストが混ざる。モデルに戻すとき、それを命令として
  読ませない。

## データ形式

セッションはターンのJSON配列:

```json
[{"role": "user", "content": "...", "timestamp": "2031-05-04T09:12:00+09:00"}]
```

必須は `role` と `content` だけ。他の形式（ベンダーのエクスポート、DB、チャット
基盤のAPI）はアダプタでこの形にして `sessions/` へ書く。下流は元の形式を知らない。

ノートは `notes/` 配下のMarkdown。追加のコーパスは `extra_sources` で足せる
（コードは触らない）。

## ディレクトリ構成

```
$DAICHO_HOME/
  events/YYYY-MM.jsonl      追記専用ログ            <- 正本
  sessions/*.json           会話ログ (+ archive/YYYY-MM/*.json.gz)
  notes/*.md                Markdownノート
  registry/*.json           エンティティ台帳        <- 導出物
  index/fts.db              全文索引                <- 導出物
  config.json               任意の設定上書き
```

ホームディレクトリは `Config.load(path)` → `$DAICHO_HOME` → `~/.daicho`
の順で決まる。マシンやユーザに固定された場所は無い。

## 設定

ホーム直下の `config.json`、または `Config.load` のキーワード引数:

| キー | 既定 | 意味 |
|---|---|---|
| `llm_cmd` | `$DAICHO_LLM_CMD` | stdinにプロンプト、stdoutにJSONを出すシェルコマンド |
| `notify_cmd` | `$DAICHO_NOTIFY_CMD` | 実行を諦めたときにstdin経由でメッセージを渡す先 |
| `auto_confirm_min_episodes` | 3 | 自動確定に必要な異なるエピソード数 |
| `max_proposals_per_run` | 10 | 1実行の提案上限（系統ごと）。超過分は持ち越し |
| `max_consecutive_failures` | 5 | 通知して非ゼロ終了するまでの連続失敗バッチ数 |
| `llm_retries` / `llm_timeout_sec` | 2 / 180 | 呼び出しごとの再試行とタイムアウト |
| `max_episodes_per_run` / `episodes_per_batch` | 60 / 10 | 1実行・1プロンプトあたりの処理量 |
| `reminders_file` | — | ホスト側で登録済みのリマインダー一覧（JSON） |
| `extra_sources` | — | `[{"kind": "profile", "path": "people", "glob": "**/*.md"}]` |
| `kind_weights` | `config.py` 参照 | 種別ごとの関連度の重み |
| `generic_aliases` | `config.py` 参照 | 別名展開に使わない一般語 |

## モデルの差し替え

モデルとの境界はシェルコマンド1本、**stdinにプロンプト・stdoutにJSON**だけ。
これを満たせば何でも使える。特定ベンダーのSDKはどこからも読み込まない。

```bash
daicho --llm-cmd "claude -p --model sonnet" extract
daicho --llm-cmd "llm -m gpt-4o-mini" extract
daicho --llm-cmd "ollama run qwen2.5" extract
daicho --llm-cmd "python my_wrapper.py" extract
export DAICHO_LLM_CMD="curl -s -XPOST … | jq -r .output"
```

stdoutの最初のJSONオブジェクトだけを読むので、進捗行を出すラッパーでも問題ない。
依存なしのスタブは `examples/mock_llm.py` にある。

## 定期実行

`ingest` は安価で決定的なので頻繁に回してよい（数分間隔）。`extract` はモデル
呼び出しの費用がかかるので夜間1回が無難。**ユニットは分けること**。抽出が
失敗しても、記帳そのものは止まってはいけない。

```cron
*/15 * * * *  daicho ingest              >> /var/log/daicho.log 2>&1
35 3    * * *  daicho extract && daicho reindex
```

## テスト

```bash
pip install -e ".[test]"
pytest
```

テストは全て一時ディレクトリとモックモデルで完結する。実際のホームには触れず、
ネットワークも不要。

## ライセンス

Apache-2.0
