# traffic_tester 設計仕様書

NW機器・FWのルーティングおよびポリシー試験用トラフィックテストツール。  
TCP / HTTP / HTTPS / UDP の各プロトコルについてサーバ・クライアントを提供する。

---

## 目次

1. [ツール概要](#1-ツール概要)
2. [ディレクトリ構成](#2-ディレクトリ構成)
3. [セットアップ](#3-セットアップ)
4. [共通仕様](#4-共通仕様)
   - 4.1 [CLIオプション](#41-cliオプション全プロトコル共通)
   - 4.2 [ログフォーマット](#42-ログフォーマット)
   - 4.3 [ログファイル命名規則](#43-ログファイル命名規則)
   - 4.4 [event_type 一覧](#44-event_type-一覧)
   - 4.5 [転送データ](#45-転送データ)
   - 4.6 [commonモジュール](#46-commonモジュール)
5. [TCP](#5-tcp)
6. [HTTP](#6-http)
7. [HTTPS](#7-https)
8. [UDP](#8-udp)
9. [プロトコル比較](#9-プロトコル比較)
10. [将来拡張](#10-将来拡張)

---

## 1. ツール概要

### 目的

- NW機器（ルータ・スイッチ）のルーティング動作確認
- FWのポリシー（許可・拒否）動作確認
- 障害試験時のトラフィック通過確認と記録

### 設計方針

- 実ファイル不要（サーバがダミーデータを動的生成、クライアントはNullに破棄）
- 接続・切断・タイムアウトのタイムスタンプをCSVでログ記録
- 送受信データ量（bps/bytes）を指定秒間隔でログ出力
- 接続ごとにログファイルを自動生成（サーバ側・クライアント側それぞれ）
- Windows / Linux 両対応（Python 3.11以上）
- 停止は Ctrl+C
- サーバ、クライアントともに障害試験を想定した、再接続機能を備えること
  * TCP、HTTP、HTTPSはTCPのセッションを再利用すること。

---

## 2. ディレクトリ構成

```
traffic_tester/
├── pyproject.toml          # uv プロジェクト定義
├── .python-version         # Python バージョン固定（3.11）
├── SPEC.md                 # 本仕様書
├── common/
│   ├── __init__.py
│   ├── logger.py           # CSVログクラス
│   ├── stats.py            # bps計算・統計集計
│   ├── dummy.py            # ダミーデータジェネレータ
│   └── ip_utils.py         # 送信元IP取得・socket共通処理
├── tcp/
│   ├── __init__.py
│   ├── server.py
│   └── client.py
├── http/
│   ├── __init__.py
│   ├── server.py
│   └── client.py
├── https/
│   ├── __init__.py
│   ├── cert_utils.py       # 自己署名証明書動的生成
│   ├── server.py
│   └── client.py
└── udp/
    ├── __init__.py
    ├── frame.py            # フレーム定義・pack/unpack・定数
    ├── server.py
    └── client.py
```

---

## 3. セットアップ

```bash
cd traffic_tester
uv sync
```

### 依存パッケージ

| パッケージ | 用途 | 区分 |
|---|---|---|
| `cryptography` | HTTPS自己署名証明書の動的生成 | 本体依存 |
| `rich` | 将来の標準出力高度化（現時点では未使用） | dev依存 |

---

## 4. 共通仕様
- ソースコードには、デバッグ、リファクタリング等が容易になるようコメントを記述する。
  * Docstringにはgoogle styleのコメントを記述する。
  * コメントは日本語で記述する。
  * コードの可読性を重視する。
  * 長い関数や複雑なロジックにはインラインで適切なコメントを追加する。
  * 重要な処理やアルゴリズムにはコメントを追加する。
  * コードの意図や目的を明確にするコメントを追加する。
  * コードの変更履歴やTODO事項を記録するコメントを追加する。
  * コードのバグ修正や改善点を記録するコメントを追加する。
  * コードのテストやデバッグ用のコメントを追加する。
  * コードの将来の拡張性や保守性を考慮したコメントを追加する。
  * コードの依存関係や相互作用を説明するコメントを追加する。
  * コードのエラーハンドリングや例外処理を説明するコメントを追加する。
  * コードのパフォーマンスや最適化についてのコメントを追加する。
  * コードのセキュリティやアクセス制御についてのコメントを追加する。
  * コードのドキュメントや使用方法についてのコメントを追加する。
  * コードのテストやデバッグ用のコメントを追加する。
  * コードの将来の拡張性や保守性を考慮したコメントを追加する。
- ディレクトリ構成を変更した方が良い場合は、本ドキュメントを変更してから実装する。
- 変更時はgitコミットし、変更履歴はgitのコミットメッセージに記録する。

### 4.1 CLIオプション（全プロトコル共通）

#### サーバ共通

| オプション | デフォルト | 説明 |
|---|---|---|
| `port`（positional） | 必須 | Listenポート番号 |
| `--bind ADDR` | `0.0.0.0` | バインドアドレス |
| `--timeout SEC` | `30` | クライアントタイムアウト秒 |
| `--interval SEC` | `1` | 統計ログ出力間隔（秒） |
| `--blocksize N` | `65536` | 1回の送受信バイト数 |
| `--logdir DIR` | `./log_traffic` | ログ出力ディレクトリ |

#### クライアント共通

| オプション | デフォルト | 説明 |
|---|---|---|
| `host`（positional） | 必須 | 接続先サーバホスト名またはIPアドレス |
| `port`（positional） | 必須 | 接続先ポート番号 |
| `--timeout SEC` | `30` | 接続・通信タイムアウト秒 |
| `--interval SEC` | `1` | 統計ログ出力間隔（秒） |
| `--duration SEC` | `0` | 送信継続時間（秒、0=無制限） |
| `--blocksize N` | `65536` | 1回の送受信バイト数 |
| `--mode MODE` | `download` | `download` / `upload` / `both` |
| `--logdir DIR` | `./log_traffic` | ログ出力ディレクトリ |

#### --mode の意味

| mode | クライアントの動作 | サーバの動作 |
|---|---|---|
| `download` | データを受信・破棄 | ダミーデータを送信 |
| `upload` | ダミーデータを送信 | データを受信・破棄 |
| `both` | 送受信を同時に実施 | 送受信を同時に実施 |

> **注意:** サーバの `--mode` はサーバ視点での動作を指定する。クライアントの `--mode` と一致させること。

### 4.2 ログフォーマット

#### TCP / HTTP / HTTPS（現行フォーマット）

UDPは固有フィールド3列(`pkt_seq`, `pkt_loss`, `pkt_ooo`)を追加。TCP/HTTP/HTTPSは該当列を空欄で出力し互換を維持する。
```
datetime,event_type,proto,server_ip,server_port,client_ip,client_port,elapsed_sec,bytes_sent,bytes_recv,bps_sent,bps_recv,message,pkt_seq,pkt_loss,pkt_ooo
```

#### UDP（拡張フォーマット）

```
datetime,event_type,proto,server_ip,server_port,client_ip,client_port,elapsed_sec,bytes_sent,bytes_recv,bps_sent,bps_recv,message,pkt_seq,pkt_loss,pkt_ooo
```


#### フィールド定義

| フィールド | 型 | 説明 |
|---|---|---|
| `datetime` | 文字列 | ローカル時刻（ミリ秒まで）`YYYY-MM-DD HH:MM:SS.mmm` |
| `event_type` | 文字列 | イベント種別（後述） |
| `proto` | 文字列 | `TCP` / `HTTP` / `HTTPS` / `UDP` |
| `server_ip` | 文字列 | サーバIPアドレス |
| `server_port` | 整数 | サーバポート番号 |
| `client_ip` | 文字列 | クライアントIPアドレス |
| `client_port` | 整数 | クライアントエフェメラルポート番号 |
| `elapsed_sec` | 小数 | 接続確立からの経過秒（小数点3桁） |
| `bytes_sent` | 整数 | セッション累計送信バイト数 |
| `bytes_recv` | 整数 | セッション累計受信バイト数 |
| `bps_sent` | 整数 | 直前interval期間の送信bps |
| `bps_recv` | 整数 | 直前interval期間の受信bps |
| `message` | 文字列 | イベント詳細メッセージ |
| `pkt_seq` | 整数/空 | **UDP専用** 該当行のシーケンス番号 |
| `pkt_loss` | 整数/空 | **UDP専用** セッション累計欠落パケット数 |
| `pkt_ooo` | 整数/空 | **UDP専用** セッション累計順序外れパケット数 |

### 4.3 ログファイル命名規則

```
{logdir}/yyyymmdd_HHMMSS_{role}_{proto}_{server_port}_{client_ip}_{server_ip}.log
```

| 要素 | 内容 |
|---|---|
| `yyyymmdd_HHMMSS` | 接続確立時刻 |
| `role` | `client` または `server` |
| `proto` | `TCP` / `HTTP` / `HTTPS` / `UDP` |
| `server_port` | サーバポート番号 |
| `client_ip` | クライアントIPアドレス（IPv6は `:` を `-` に置換） |
| `server_ip` | サーバIPアドレス |

**例:**

```
20260403_100000_client_TCP_5001_192.168.1.100_192.168.1.1.log
20260403_100000_server_TCP_5001_192.168.1.100_192.168.1.1.log
```

1回の接続につき、クライアント側・サーバ側それぞれ1ファイルずつ生成される。

### 4.4 event_type 一覧

#### TCP / HTTP / HTTPS

| event_type | 発生タイミング | 出力タイミング |
|---|---|---|
| `CONNECT` | 接続確立時 | 即時 |
| `DATA` | interval秒経過ごとのbps統計 | interval |
| `DISCONNECT` | 正常切断時 | 即時 |
| `TIMEOUT` | タイムアウト発生時 | 即時 |
| `ERROR` | 例外・エラー発生時 | 即時 |
| `RETRY` | 再接続関連イベント | 即時 |

**再接続関連イベントのmessage例:**
- `Send error: [Errno 104] Connection reset by peer`
- `Retrying connection in 1.0s... (1/3)`
- `Reconnected successfully`
- `Reconnection failed: [Errno 111] Connection refused`
- `Max retries reached, giving up`

#### UDP（追加）

| event_type | 発生タイミング | 出力タイミング |
|---|---|---|
| `CONNECT` | SYNACK受信（クライアント）/ SYN受信（サーバ） | 即時 |
| `DATA` | interval秒経過ごとのbps統計 | interval |
| `LOSS` | シーケンスgap検出時 | 即時 |
| `OUT_OF_ORDER` | 期待より大きいseqを受信した時 | 即時 |
| `LATE_ARRIVAL` | 期待より小さいseqを受信した時（遅延到着） | 即時 |
| `DISCONNECT` | FIN/FINACK完了 | 即時 |
| `TIMEOUT` | SYNハンドシェイク失敗 / セッションタイムアウト | 即時 |
| `ERROR` | 例外・不正フレーム受信 | 即時 |

### 4.5 転送データ

- **サーバ:** `os.urandom(4)` で生成した1MiBのプールから繰り返しチャンクを切り出して送信（実ファイル不要）
- **クライアント:** 受信データはすべてNullに破棄（ディスク書き込みなし）
- **upload時のクライアント:** 同様のダミーデータを生成して送信

### 4.6 commonモジュール

| モジュール | 役割 |
|---|---|
| `logger.py` | CSVログクラス。接続ごとにファイルを生成し、stdout同時出力 |
| `stats.py` | スレッドセーフなbps計算・累計バイト集計 |
| `dummy.py` | 1MiBプールからダミーデータを高速生成する `DummyReader` |
| `ip_utils.py` | UDPソケットのconnectトリックで送信元IPを取得、keepalive設定など |

---

### 4.7.1 クライアント動作仕様
 
#### 4.7.1.1 接続確立
 
1. **DNS解決**: `resolve_host()` でサーバ名をIPアドレスに解決
2. **ソケット作成**: 最適化されたTCPソケットを生成
3. **接続**: サーバにTCP接続を確立
4. **ロギング**: CONNECTイベントを記録
 
#### 4.7.1.2 データ転送
 
1. **送信ループ**: 指定ブロックサイズでダミーデータを送信（upload/bothモード）
2. **受信ループ**: 指定ブロックサイズでデータを受信・破棄（download/bothモード）
3. **統計レポート**: interval秒ごとにDATAイベントを記録
4. **エラーハンドリング**: 接続エラー時に再接続を試行
 
#### 4.7.1.3 接続終了
 
1. **正常終了**: 指定時間経過後、FINハンドシェイクで接続を終了
2. **異常終了**: 最大再試行回数到達時、エラーとして記録
3. **リソース解放**: ソケット、スレッド、ログファイルを適切に解放

## 5. TCP

### 概要

標準ライブラリ `socketserver.ThreadingTCPServer` によるマルチスレッドTCPサーバ。  
クライアントごとに独立スレッドで処理する。

### CLIオプション（TCP固有）

サーバの `--mode` はサーバ視点での転送方向を指定する。

```bash
# サーバ（downloadモード: クライアントへ送信）
python tcp/server.py <port> [--bind ADDR] [--timeout SEC] [--interval SEC]
                            [--blocksize N] [--mode download|upload|both]
                            [--logdir DIR]

# クライアント
python tcp/client.py <host> <port> [--timeout SEC] [--interval SEC]
                                   [--duration SEC] [--blocksize N]
                                   [--mode download|upload|both] [--logdir DIR]
```

### 実行例

```bash
# downloadモード（サーバ→クライアント）
python tcp/server.py 5001
python tcp/client.py 192.168.1.1 5001 --duration 30

# uploadモード（クライアント→サーバ）
python tcp/server.py 5001 --mode upload
python tcp/client.py 192.168.1.1 5001 --mode upload --duration 30

# 双方向
python tcp/server.py 5001 --mode both
python tcp/client.py 192.168.1.1 5001 --mode both --duration 30
```

### ログ出力例

```
datetime,event_type,proto,server_ip,server_port,client_ip,client_port,elapsed_sec,bytes_sent,bytes_recv,bps_sent,bps_recv,message,pkt_seq,pkt_loss,pkt_ooo
2026-04-03 10:00:00.123,CONNECT,TCP,192.168.1.1,5001,192.168.1.100,54321,0.000,0,0,0,0,Connected to 192.168.1.1:5001,,,
2026-04-03 10:00:01.124,DATA,TCP,192.168.1.1,5001,192.168.1.100,54321,1.001,0,67108864,0,536870912,interval 1.0s,,,
2026-04-03 10:00:02.125,DATA,TCP,192.168.1.1,5001,192.168.1.100,54321,2.002,0,134217728,0,536870912,interval 1.0s,,,
2026-04-03 10:00:30.135,DISCONNECT,TCP,192.168.1.1,5001,192.168.1.100,54321,30.012,0,2013265920,0,0,Session ended,,,
```


---

## 6. HTTP

### 概要

標準ライブラリ `http.server` + `socketserver.ThreadingMixIn` によるマルチスレッドHTTPサーバ。  
データ転送はHTTP/1.1のchunked transfer encodingで実施する。

### エンドポイント

| メソッド | パス | 動作 |
|---|---|---|
| `GET` | `/download` | サーバがchunkedでダミーデータをストリーム送信 |
| `POST` | `/upload` | サーバがリクエストボディをchunked読み取りして破棄（B案実装） |

### CLIオプション（HTTP固有）

HTTPサーバに `--mode` オプションはない。クライアントの `--mode` でエンドポイントが切り替わる。

```bash
# サーバ
python http/server.py <port> [--bind ADDR] [--timeout SEC] [--interval SEC]
                             [--blocksize N] [--logdir DIR]

# クライアント
python http/client.py <host> <port> [--timeout SEC] [--interval SEC]
                                    [--duration SEC] [--blocksize N]
                                    [--mode download|upload|both] [--logdir DIR]
```

### 実行例

```bash
# downloadモード（GET /download）
python http/server.py 8080
python http/client.py 192.168.1.1 8080 --mode download --duration 30

# uploadモード（POST /upload、chunked送信）
python http/server.py 8080
python http/client.py 192.168.1.1 8080 --mode upload --duration 30

# 双方向（GET + POST を同時実行）
python http/server.py 8080
python http/client.py 192.168.1.1 8080 --mode both --duration 30
```

### 正常切断の処理

クライアントが `--duration` 経過またはCtrl+Cで終了する際、chunked転送中のソケットを単純にcloseするとサーバ側にTCP RST（`ECONNRESET`）が記録される。これを避けるため以下の手順で接続を終了する。

- **download終了時:** 受信データを最大2秒間drain（読み捨て）してからソケットをclose
  * 接続をソケットレベルで shutdown(SHUT_WR) して FIN を送り、サーバに終了を通知してからクローズ処理
- **upload終了時:** `0\r\n\r\n`（chunked終端）を必ず送信してサーバの200レスポンスを受け取ってからclose
  * 接続をソケットレベルで shutdown(SHUT_WR) して FIN を送り、サーバに終了を通知してからクローズ処理
    finallyで必ず送るよう保護する


---

## 7. HTTPS

### 概要

HTTPと同一実装をSSLコンテキストでラップしたもの。  
証明書は起動時に自動生成（`cryptography` ライブラリ）または既存ファイルを指定可能。

### CLIオプション（HTTPS固有）

```bash
# サーバ（自己署名証明書を自動生成）
python https/server.py <port> [--bind ADDR] [--timeout SEC] [--interval SEC]
                              [--blocksize N] [--cert FILE] [--key FILE]
                              [--logdir DIR]

# クライアント
python https/client.py <host> <port> [--timeout SEC] [--interval SEC]
                                     [--duration SEC] [--blocksize N]
                                     [--mode download|upload|both]
                                     [--verify] [--logdir DIR]
```

### HTTPS固有オプション

| オプション | 対象 | デフォルト | 説明 |
|---|---|---|---|
| `--cert FILE` | server | なし（自動生成） | PEM証明書ファイルパス |
| `--key FILE` | server | なし（自動生成） | PEM秘密鍵ファイルパス |
| `--verify` | client | 無効 | TLS証明書検証を有効化（デフォルトは検証無効） |

### 証明書の扱い

- `--cert` / `--key` の両方が指定された場合: 指定ファイルを使用
- いずれかが省略された場合: `cryptography` ライブラリで自己署名証明書を起動時に動的生成し、サーバ停止時に自動削除
- クライアントはデフォルトで証明書検証を無効（`verify=False`）にする。FW試験用途では自己署名証明書が一般的なため

### SSL EOF の扱い

クライアント終了時にSSL `close_notify` を送らずに切断した場合（`verify=False` 環境では一般的）、サーバ側で `SSL EOF` が発生するが、これは `DISCONNECT` として記録する（`ERROR` 扱いにしない）。

### 実行例

```bash
# 自己署名証明書で起動
python https/server.py 8443
python https/client.py 192.168.1.1 8443 --mode download --duration 30

# 既存証明書を使用
python https/server.py 8443 --cert server.pem --key server.key
python https/client.py 192.168.1.1 8443 --mode download --verify
```

---

## 8. UDP

### 概要

UDPはセッションレスなため、アプリケーション層で擬似セッション管理を実装する。  
シーケンス番号による欠落・順序外れ検出を行い、NW品質の観測に特化する（再送は実装しない）。

### 8.1 フレームフォーマット

```
 0        1        2        3        4        5        6        7        8
 +--------+--------+--------+--------+--------+--------+--------+--------+--------+
 |  type  |          session_id (4 bytes)              |      seq_no (4 bytes)    |
 +--------+--------+--------+--------+--------+--------+--------+--------+--------+
 |  payload (0 〜 blocksize bytes) ...
 +--------+ ...

合計ヘッダサイズ: 9 bytes
エンコード: big-endian（struct format: !B4sI）
```

#### typeフィールド

| 値 | 定数名 | 方向 | 説明 |
|---|---|---|---|
| `0x01` | `SYN` | クライアント→サーバ | セッション開始要求 |
| `0x02` | `SYNACK` | サーバ→クライアント | セッション開始受理 |
| `0x03` | `DATA` | 双方向 | データ転送 |
| `0x04` | `FIN` | クライアント→サーバ | セッション終了通知 |
| `0x05` | `FINACK` | サーバ→クライアント | セッション終了受理 |

#### SYNフレームのpayload（モード指定）

| 値 | モード | 説明 |
|---|---|---|
| `0x01` | `download` | サーバ→クライアント方向にDATAを送信 |
| `0x02` | `upload` | クライアント→サーバ方向にDATAを送信 |
| `0x03` | `both` | 双方向にDATAを送信 |

#### session_id

- クライアントが接続時に `os.urandom(4)` で生成する4バイトのランダムID
- 全フレームに含める（NAT環境でのセッション識別に対応）

#### seq_no

- 送信側が1からインクリメント（32bit符号なし整数）
- SYN/SYNACK/FIN/FINACKは `seq_no=0` 固定
- DATAフレームのみ有効なseq_noを持つ

#### blocksize

- `--blocksize` でpayloadサイズを指定（デフォルト: **1400 bytes**）
- ジャンボフレーム試験など目的に応じて変更可能（利用者責任とする）

### 8.2 ハンドシェイクシーケンス

#### 接続

```
Client                                      Server
  |                                            |
  |---- SYN(session_id, seq=0, mode) --------->|  session_idをキーにセッション登録
  |<--- SYNACK(session_id, seq=0) -------------|
  |                                            |
  リトライ間隔: timeout / 3 秒
  最大3回リトライ → 失敗時 TIMEOUT ログ出力して終了
```

#### データ転送（downloadモード例）

```
Client                                      Server
  |                                            |
  |<-- DATA(session_id, seq=1, payload) -------|
  |<-- DATA(session_id, seq=2, payload) -------|
  |<-- DATA(session_id, seq=4, payload) -------|  ← seq=3が抜けている場合:
  |                                            |    LOSS ログ即時出力（seq=3 missing）
  |                                            |    OUT_OF_ORDER ログ即時出力（got=4）
  |<-- DATA(session_id, seq=3, payload) -------|  ← 遅延到着:
  |                                            |    LATE_ARRIVAL ログ即時出力
```

#### 切断

```
Client                                      Server
  |                                            |
  |---- FIN(session_id, seq=0) --------------->|  セッション削除
  |<--- FINACK(session_id, seq=0) -------------|
  |                                            |
  最大3回リトライ → 失敗時もログ記録して終了（強制終了扱い）
```

#### サーバ側タイムアウト

`--timeout` 秒間無通信のセッションをバックグラウンドスレッドが検出し、`TIMEOUT` ログを出力してセッションを削除する。

### 8.3 シーケンス番号管理とgap検出

#### 受信側状態変数

```
rx_next_expected: 次に期待するseq番号（初期値=1）
pkt_loss_total:   セッション累計欠落パケット数
pkt_ooo_total:    セッション累計順序外れパケット数（LATE_ARRIVALを含む）
```

#### 判定ロジック

```
(A) recv_seq == rx_next_expected   → 正常受信
                                     rx_next_expected += 1

(B) recv_seq > rx_next_expected    → gap検出
                                     LOSSログ即時出力（missing seq一覧）
                                     OUT_OF_ORDERログ即時出力
                                     pkt_loss_total += (recv_seq - rx_next_expected)
                                     pkt_ooo_total += 1
                                     rx_next_expected = recv_seq + 1

(C) recv_seq < rx_next_expected    → 遅延到着
                                     LATE_ARRIVALログ即時出力
                                     rx_next_expectedは更新しない
                                     pkt_loss_totalはデクリメントしない
```

#### bothモードのseqカウンタ（独立）

```
クライアント:
  tx_seq:           クライアント→サーバ方向（upload側送信）
  rx_next_expected: サーバ→クライアント方向（download側受信）

サーバ（セッションごとに独立）:
  tx_seq:           サーバ→クライアント方向（download側送信）
  rx_next_expected: クライアント→サーバ方向（upload側受信）
```

### 8.4 CLIオプション（UDP）

```bash
# サーバ
python udp/server.py <port> [--bind ADDR] [--timeout SEC] [--interval SEC]
                            [--logdir DIR]

# クライアント
python udp/client.py <host> <port> [--timeout SEC] [--interval SEC]
                                   [--duration SEC] [--blocksize N]
                                   [--mode download|upload|both] [--logdir DIR]
```

### 8.5 サーバ内部設計

#### セッション管理

1つのUDPソケットで複数クライアントを処理する。`session_id` をキーにした辞書でセッション状態を管理する。

```python
sessions: dict[bytes, SessionState] = {}

@dataclass
class SessionState:
    client_addr:      tuple                    # (ip, port)
    mode:             int                      # MODE_DOWNLOAD / UPLOAD / BOTH
    tx_seq:           int                      # サーバ送信seqカウンタ
    rx_next_expected: int                      # 受信期待seq
    pkt_loss_total:   int                      # 累計欠落数
    pkt_ooo_total:    int                      # 累計順序外れ数
    logger:           TrafficLogger
    stats:            StatsTracker
    last_recv_time:   float                    # タイムアウト判定用
    send_thread:      threading.Thread | None  # bothモード送信スレッド
    stop_event:       threading.Event
```

#### スレッド構成

| スレッド | 役割 |
|---|---|
| メインスレッド | 受信ループ（全セッション共通） |
| 送信スレッド（セッションごと） | download / both モードのDATA送信 |
| タイムアウト監視スレッド | 無通信セッションの自動削除 |
| intervalレポートスレッド（セッションごと） | bps統計のDATA行出力 |

### 8.6 ファイルログ出力例（downloadモード、クライアント側）

```
datetime,event_type,proto,server_ip,server_port,client_ip,client_port,elapsed_sec,bytes_sent,bytes_recv,bps_sent,bps_recv,message,pkt_seq,pkt_loss,pkt_ooo
2026-04-03 10:00:00.000,CONNECT,UDP,192.168.1.1,5005,192.168.1.100,54321,0,0,0,0,0,Connected session_id=a1b2c3d4 mode=download,,0,0
2026-04-03 10:00:01.000,DATA,UDP,192.168.1.1,5005,192.168.1.100,54321,1,0,1400000,0,11200000,interval 1.0s,,0,0
2026-04-03 10:00:01.523,LOSS,UDP,192.168.1.1,5005,192.168.1.100,54321,1.523,0,1450400,0,0,LOSS: seq 101-102 missing,2,0,
2026-04-03 10:00:01.523,OUT_OF_ORDER,UDP,192.168.1.1,5005,192.168.1.100,54321,1.523,0,1451800,0,0,OUT_OF_ORDER: expected=101 got=103,2,1,103
2026-04-03 10:00:01.524,LATE_ARRIVAL,UDP,192.168.1.1,5005,192.168.1.100,54321,1.524,0,1453200,0,0,LATE_ARRIVAL: seq=101,2,2,101
2026-04-03 10:00:02.000,DATA,UDP,192.168.1.1,5005,192.168.1.100,54321,2,0,2800000,0,10800000,interval 1.0s,,2,2
2026-04-03 10:00:30.000,DISCONNECT,UDP,192.168.1.1,5005,192.168.1.100,54321,30,0,42000000,0,0,Session ended pkt_sent=30000 pkt_recv=29998 loss=2 ooo=2,,2,2

```

---

## 9. プロトコル比較

### オプション対応表

| オプション | TCP | HTTP | HTTPS | UDP |
|---|---|---|---|---|
| `--bind` | ✅ server | ✅ server | ✅ server | ✅ server |
| `--timeout` | ✅ 両方 | ✅ 両方 | ✅ 両方 | ✅ 両方 |
| `--interval` | ✅ 両方 | ✅ 両方 | ✅ 両方 | ✅ 両方 |
| `--duration` | ✅ client | ✅ client | ✅ client | ✅ client |
| `--blocksize` | ✅ 両方（default: 65536） | ✅ 両方（default: 65536） | ✅ 両方（default: 65536） | ✅ client（default: **1400**） |
| `--mode` | ✅ 両方 | ✅ client のみ | ✅ client のみ | ✅ client のみ |
| `--logdir` | ✅ 両方 | ✅ 両方 | ✅ 両方 | ✅ 両方 |
| `--cert` / `--key` | — | — | ✅ server | — |
| `--verify` | — | — | ✅ client | — |

### event_type 対応表

| event_type | TCP | HTTP | HTTPS | UDP |
|---|---|---|---|---|
| `CONNECT` | ✅ | ✅ | ✅ | ✅ |
| `DATA` | ✅ | ✅ | ✅ | ✅ |
| `DISCONNECT` | ✅ | ✅ | ✅ | ✅ |
| `TIMEOUT` | ✅ | ✅ | ✅ | ✅ |
| `ERROR` | ✅ | ✅ | ✅ | ✅ |
| `LOSS` | — | — | — | ✅ |
| `OUT_OF_ORDER` | — | — | — | ✅ |
| `LATE_ARRIVAL` | — | — | — | ✅ |

### ログCSVフィールド対応表

| フィールド | TCP | HTTP | HTTPS | UDP |
|---|---|---|---|---|
| `datetime` 〜 `bps_recv` | ✅ | ✅ | ✅ | ✅ |
| `pkt_seq` | 空欄 | 空欄 | 空欄 | ✅ |
| `pkt_loss` | 空欄 | 空欄 | 空欄 | ✅ |
| `pkt_ooo` | 空欄 | 空欄 | 空欄 | ✅ |
| `message` | ✅ | ✅ | ✅ | ✅ |

---

## 10. 将来拡張

| 項目 | 内容 |
|---|---|
| `rich` 対応 | 標準出力のカラー・テーブル形式リアルタイム表示 |
| 再接続オプション | `--reconnect N` による自動再接続 |
| UDP NACK再送 | 欠落パケットの再送要求（現在は観測のみ） |
| IPv6対応 | `AF_INET6` ソケット対応（現在はIPv4のみ） |
| 複数宛先並列試験 | `--targets host1,host2,...` による同時試験 |
| ログサマリ出力 | セッション終了時の平均bps・loss率などのサマリ表示 |
| サーバ証明書チェック | `--verify` によるサーバ証明書の検証、証明書情報表示 |
