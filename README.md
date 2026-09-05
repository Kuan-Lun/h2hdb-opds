# h2hdb-opds

`h2hdb-opds` 讓你在支援 OPDS 的閱讀器中，瀏覽、搜尋及下載已由 H2HDB
發佈的漫畫書庫。支援 OPDS 1.2 與 OPDS 2.0，提供封面、縮圖、CBZ 下載，
以及供 OPDS-PSE 閱讀器使用的逐頁閱讀功能。

已有書庫網址時，直接從下方加入閱讀器即可。要在自己的主機上提供服務，
請見[自行架設](#自行架設)。本服務需要已由 H2HDB 與 ingest 準備好的書庫，
不會直接掃描漫畫資料夾或匯入檔案。

## 加入閱讀器

1. 在閱讀器中新增 OPDS 書庫或目錄。
2. 填入下列其中一個網址，將 `https://books.example.net` 換成你的服務位址。
3. 若書庫有啟用登入，填入管理者提供的帳號與密碼。

| 閱讀器支援的功能 | 書庫網址 |
| --- | --- |
| OPDS 1.2；需要 OPDS-PSE 逐頁閱讀時使用此網址 | `https://books.example.net/opds/v1.2/catalog` |
| OPDS 2.0 | `https://books.example.net/opds/v2` |

兩個網址提供相同書庫與搜尋條件。逐頁閱讀需要閱讀器本身支援 OPDS-PSE；
OPDS 2.0 提供封面、縮圖及 CBZ 下載。實際按鈕名稱與篩選介面依閱讀器而異。

## 瀏覽與下載

進入書庫後，可以看到三個入口：

| 畫面上的名稱 | 內容 |
| --- | --- |
| `All Publications` | 全部可下載的作品，可翻頁、搜尋及篩選 |
| `Recently Uploaded` | 依來源上傳時間排列的最新 128 部作品 |
| `Recently Downloaded` | 依來源下載時間排列的最新 128 部作品 |

「下載時間」指資料匯入時記錄的來源下載時間，不是你透過閱讀器下載 CBZ 的時間。
兩個最近動態入口最多各列出 128 部，不提供下一頁；要找更早的作品，請使用搜尋。

在作品頁選擇下載，即可取得 CBZ；支援 OPDS-PSE 的閱讀器也能逐頁讀取。
下載支援續傳，但能否使用仍取決於閱讀器。
伺服器不保存個人書架、已讀狀態或閱讀進度；這些功能由閱讀器管理。

全部作品與搜尋結果有下一頁時，使用閱讀器的翻頁功能即可。
書庫重新發佈後，原先開啟的作品或下一頁連結可能失效，重新進入書庫即可取得新連結。

## 如何搜尋

在閱讀器的書庫搜尋框中直接輸入下列內容，不需要自行填寫網址參數。
一般關鍵字與欄位條件可以混用，條件之間以空白分隔，**所有條件都必須符合（AND）**。
目前不支援 `AND`、`OR`、`NOT` 或括號運算子；AND 比對只由條件間的空白表示。

### 先試這幾個例子

| 想找什麼 | 搜尋框輸入 |
| --- | --- |
| 同時含有兩個關鍵字 | `不知火 chinese` |
| 標題含「不知火」的作品 | `title:不知火` |
| 指定 GID 的作品 | `gid:1834943` |
| 帶有中文標籤的作品 | `language:chinese` |
| 同時帶有語言與作者標籤 | `language:chinese artist:alice` |
| 標題含「不知火」、帶有中文標籤，且為 40 到 200 頁 | `title:不知火 language:chinese pages:40..200` |
| 在指定日期區間下載的作品 | `downloaded:2026-09-01..2026-09-05` |

範例中的標題、GID 與標籤需要換成你書庫內的資料。
只輸入欄位條件也能搜尋，例如 `pages:40..200`，不必另外加關鍵字。

### 關鍵字、標題與標籤

一般關鍵字會比對顯示標題、來源標題、貢獻者名稱與標籤值，
不搜尋簡介或 CBZ 檔名。純數字仍視為文字：`1834943` 是關鍵字，
`gid:1834943` 才是精確指定作品。GID 必須是沒有前置零的正整數。

| 語法 | 用法與範例 |
| --- | --- |
| `title:文字` | 只比對顯示標題與來源標題，例如 `title:不知火` |
| `title:"多個 單字"` | 把多個字詞放進同一個標題條件，例如 `title:"Alpha Gallery"` |
| `命名空間:值` | 精確比對來源標籤，例如 `artist:alice` |
| `命名空間:"含空白的值"` | 例如 `artist:"a  b"`，中間的兩個空白必須一致 |
| `"命名空間":值` | 指定保留字或含特殊字元的命名空間，例如 `"title":foo`、`"名:稱":"a  b"` |

`title:`、`gid:`、`uploaded:`、`downloaded:` 與 `pages:` 是小寫保留欄位；
其他命名空間均視為來源標籤，包括 Unicode 名稱與書庫中尚未出現的名稱。
例如 `language:chinese` 是來源標籤，`unknown:value` 是合法條件，沒有符合資料時回傳空結果。
多個 `title:` 條件或多個不同標籤都是 AND；
相同標籤重複出現只計一次。`gid:`、`uploaded:`、`downloaded:` 與 `pages:`
各只能出現一次。

雙引號只把含空白的內容視為同一個值，**不代表連續片語搜尋**。
例如 `title:"Alpha Gallery"` 表示標題要同時含有這兩個詞，不要求相鄰或固定順序。
一般文字會經 Unicode 正規化與大小寫摺疊後比對字詞，
不提供模糊搜尋、詞幹搜尋或依相關性排序。

標籤的命名空間和值都必須精確一致，包含大小寫與空白，不會自動修整。
命名空間也可以加雙引號，例如 `"artist":"alice"`；加引號後一律視為標籤命名空間，
所以 `"title":foo` 比對名為 `title` 的標籤，而 `title:foo` 比對標題。
雙引號內可用 `\"` 表示引號、`\\` 表示反斜線。
如果冒號是要搜尋的文字而非欄位語法，把整個詞加上雙引號，例如 `"re:zero"`
或 `"language:chinese"`；後者是一般文字，並非標籤條件。
舊 `tag:命名空間:值` 語法已移除，未加引號的 `tag:` 一律回傳 422。
要比對命名空間本身名為 `tag` 的來源標籤，使用 `"tag":值`。

### 日期與頁數

| 搜尋條件 | 意義 |
| --- | --- |
| `uploaded:2026-09-05` | 來源上傳日期為這一天 |
| `downloaded:2026-09-01..2026-09-05` | 來源下載日期介於這兩天，包含起訖兩天 |
| `uploaded:2026-09-01..` | 從這一天起上傳，包含當天 |
| `downloaded:..2026-09-05` | 截至這一天下載，包含當天 |
| `pages:143` | 恰好 143 頁 |
| `pages:40..200` | 40 到 200 頁，包含 40 與 200 |
| `pages:40..` | 至少 40 頁 |
| `pages:..200` | 最多 200 頁 |

日期格式固定為 `YYYY-MM-DD`，一律依 **UTC 日期** 判斷，不是閱讀器的本地日期。
例如 UTC 的 `2026-09-05` 對應臺灣時間 9 月 5 日 08:00 起，
至 9 月 6 日 08:00 前。`uploaded:` 與 `downloaded:` 都支援表中的日期寫法。

頁數依書庫已發佈的下載檔案實際頁數判斷，不使用來源網頁宣稱的頁數。
可指定的數字範圍是 0 到 4096；區間起點不能大於終點。

### 搭配篩選與處理搜尋錯誤

閱讀器若有顯示篩選選單，可以再依語言、標籤或貢獻者縮小結果。
選取標籤篩選會替換目前所有標籤條件；若要同時限定多個標籤，
請在搜尋框輸入多個 `命名空間:值`。選擇該組的 `All` 會清除該組篩選，
`More` 則會列出更多可選值。

篩選旁的數量會保留搜尋與其他組條件，但不套用同組已選條件，
方便查看改選其他值後的結果數量。

遇到搜尋錯誤（HTTP 422），先檢查：

- 搜尋不能空白；`title:` 這類欄位後面必須有值。
- 使用半形冒號 `:`、雙引號 `"` 及兩個句點 `..`，引號必須成對。
- 舊 `tag:命名空間:值` 已移除，請直接輸入 `命名空間:值`，例如 `language:chinese`。
- 日期必須存在，日期與頁數區間不能倒置。
- 不要重複指定 GID、同一種日期或頁數欄位，請合併成一個條件。
- 查詢最多 32 個條件、16 個不同標籤；一般文字與標題合計最多 16 個搜尋字詞。

一般文字與標題各有 1024 bytes 的正規化 UTF-8 上限；
標籤命名空間和值分別最多 128 與 1024 UTF-8 bytes。
完整搜尋字串最多 128 KiB，閱讀器或反向代理可能有更低的網址長度限制。
若查詢太長，請減少條件。合法查詢沒有符合的作品時會回傳空結果，不是搜尋錯誤。

### 直接使用 HTTP 搜尋

手動呼叫服務時，OPDS 1.2 使用 `q`，OPDS 2.0 使用 `query`：

```text
GET /opds/v1.2/search?q=alice
GET /opds/v2/search?query=alice
```

OPDS 2.0 不接受 `q`，使用時會回傳 422。閱讀器會透過服務提供的搜尋連結
使用對應參數；OPDS 1.2 的 OpenSearch 搜尋框也支援上述所有欄位語法。

以下以本機服務為例，用 `curl --data-urlencode` 處理中文、空白與引號：

```bash
curl --get 'http://127.0.0.1:8000/opds/v1.2/search' \
  --data-urlencode 'q=title:不知火 language:chinese pages:40..200'

curl --get 'http://127.0.0.1:8000/opds/v2/search' \
  --data-urlencode 'query=title:"Alpha Gallery" downloaded:2026-09-01..' \
  --data-urlencode 'limit=20'
```

連到啟用登入的 HTTPS 服務時，加上 `--user reader` 並依提示輸入密碼。
也可以加上以下參數；實際值可從服務提供的篩選連結取得：

| 參數 | 用法 |
| --- | --- |
| `language=值` | 精確比對作品語言；與 `language:chinese` 這類來源標籤是不同條件 |
| `tag=值` 和 `tag_namespace=命名空間` | 必須成對；與搜尋框內的所有標籤一起作 AND 比對 |
| `contributor=名稱` 和 `role=角色` | 必須成對；角色可為 `artist`、`author`、`cosplayer`、`group`、`illustrator`、`uploader` |
| `limit=數字` | 每次取得 1 到 128 筆，且不得超過管理者設定的上限 |

這些參數也能用於 `/opds/v1.2/publications` 與 `/opds/v2/publications`，
不必提供搜尋字串。篩選值不會自動去除空白或正規化，請保留原值。
HTTP 的 `tag` 與 `tag_namespace` 成對參數繼續支援；移除的是搜尋字串內的 `tag:` 前綴。
回應中的搜尋、翻頁與篩選連結會使用標準化後的 `命名空間:值` 搜尋語法。
翻頁時直接使用回應中的 `next` 連結；不要自行修改 `cursor` 或 `revision`。
最近動態入口不接受 `limit`、`cursor` 或 `offset`。

## 自行架設

### 準備環境與書庫

需要 Python 3.14 以上版本，以及支援 POSIX 檔案鎖的環境，例如 Linux 或 macOS。
目前使用的 H2HDB 相容版本範圍為 `>=0.32.0,<0.33.0`。
啟動前，請先由 H2HDB 與 ingest 完成資料庫初始化及書庫發佈，準備：

- 符合該版本 epoch 3／schema version 3、已標記為 `READY` 的資料庫。
- ingest 產生的完整 `current` 目錄，包含 `acquisitions` 與 `artwork`。
- 同一書庫旁的 `.h2hdb-coordination` 目錄，內含既有的 `publication.lock`。

OPDS 以唯讀方式開啟資料庫及書庫，不會建立或升級 schema，也不會補建 coordination
檔案。只有已發佈且有可下載檔案的書庫內容會出現在閱讀器。
舊版或不相符的資料庫需由 H2HDB／ingest 另建新資料庫並重新發佈；
目前只接受 `managed-filesystem-v2` 儲存格式，不讀取舊的 `hash-v1` 書庫。

取得本專案原始碼後，在專案目錄執行以下命令安裝：

```bash
python3.14 -m venv .venv
.venv/bin/python -m pip install .
```

### 先在本機啟動

建立 `opds.json`，把下列資料庫與書庫路徑換成實際的**絕對路徑**：

```json
{
  "library_root": "/srv/h2hdb/comics/current",
  "coordination_root": "/srv/h2hdb/comics/.h2hdb-coordination",
  "public_base_url": "http://127.0.0.1:8000",
  "core": {
    "database": {
      "sql_type": "sqlite",
      "database": "/srv/h2hdb/catalog.sqlite3",
      "access_mode": "read-only"
    }
  },
  "server": {
    "host": "127.0.0.1",
    "port": 8000
  },
  "title": "我的漫畫書庫"
}
```

此範例使用 SQLite；若現有 H2HDB 使用 MariaDB，請將 `core.database`
換成該書庫的 MariaDB 連線設定。書庫路徑及其中的檔案不能使用符號連結。

啟動並在另一個終端機確認服務狀態：

```bash
.venv/bin/h2hdb-opds --config opds.json
```

```bash
curl http://127.0.0.1:8000/health
```

成功時會取得 `{"status":"ok"}`。同一台主機的閱讀器可加入
`http://127.0.0.1:8000/opds/v1.2/catalog`。
這份設定只供本機連線，沒有啟用登入；手機或其他電腦需要下一節的對外設定。

要確認服務使用的套件版本，可另外查詢：

```bash
curl http://127.0.0.1:8000/version
```

`GET /version` 回傳 JSON，`service` 固定為 `h2hdb-opds`，`version` 是已安裝套件的
distribution metadata 版本。這個請求不需要登入、不讀取 catalog，書庫 activation
期間仍可查詢；回應帶有 `Cache-Control: no-store`。`/openapi.json` 的 `info.version`
也使用相同套件版本。

在已安裝開發依賴的 editable 環境中，修改 `pyproject.toml` 的 project version 後，
須重新安裝套件 metadata，再重新啟動服務：

```bash
uv pip install --python .venv/bin/python --no-deps --no-build-isolation --editable .
```

版本回應只表示已安裝套件宣告的版本，不提供 Git commit 識別，也不能證明重建已完成。

### 提供給其他裝置並啟用登入

`public_base_url` 必須是閱讀器實際可連到的位址，因為封面、下載及翻頁連結
都會由這個設定產生。它不會自動依瀏覽器或閱讀器送來的主機名稱切換。

以下示範同一台主機上已有 HTTPS 反向代理，代理將請求轉送到
`127.0.0.1:8000` 時，要替換或加入 `opds.json` 的欄位：

```json
{
  "public_base_url": "https://books.example.net",
  "server": {
    "host": "127.0.0.1",
    "port": 8000,
    "trusted_proxy_ips": ["127.0.0.1"]
  },
  "auth": {
    "username": "reader",
    "password": "${H2HDB_OPDS_AUTH_PASSWORD}",
    "realm": "My Catalog"
  }
}
```

這是局部設定，需保留前一節的 `library_root`、`coordination_root` 與 `core`。
先在啟動服務的環境中設定 `H2HDB_OPDS_AUTH_PASSWORD`，再重新啟動服務。
JSON 字串若完整寫成 `${環境變數名稱}`，載入時會以該變數的值替換；
變數未設定時會停止啟動。資料庫密碼也可以使用相同方式。

反向代理必須傳送 `X-Forwarded-Proto: https`，且 `trusted_proxy_ips`
只填實際代理的來源 IP 或網段。代理若不在同一台主機，還需調整監聽位址
與網路存取設定，讓代理能連到服務。
也可在 `server` 同時設定 `tls_certificate` 與 `tls_private_key` 的檔案路徑，
直接由服務提供 HTTPS。Basic 登入必須使用 HTTPS。

帳號與密碼必須一起設定；省略兩者即為不需登入的書庫。
每頁預設 50 筆，可用頂層 `default_page_size` 調整，
並以 `maximum_page_size` 設定上限；兩者須介於 1 到 128，預設值不可大於上限。

### 容器掛載

若放在容器內執行，請將 ingest 的整個 `current` 與旁邊的 coordination 目錄
分別掛載為唯讀。下例的容器路徑對應本機設定範例：

```yaml
volumes:
  - /volume1/h2hdb/comics/current:/srv/h2hdb/comics/current:ro
  - /volume1/h2hdb/comics/.h2hdb-coordination:/srv/h2hdb/comics/.h2hdb-coordination:ro
```

這只是書庫掛載片段，還需提供容器可讀取的設定檔與資料庫。
OPDS 同時需要 `acquisitions` 與 `artwork`，不能只掛載漫畫檔案的子目錄。
不要掛載書庫的上層目錄或 ingest 私有的 `.h2hdb-state`；
暫存、復原日誌與隔離檔案不需要提供給閱讀器服務。

## 疑難排解

| 現象 | 處理方式 |
| --- | --- |
| 其他裝置連不上，或封面與下載指向 `127.0.0.1` | 確認 `public_base_url` 是裝置可達的網址，並檢查反向代理與監聽設定 |
| 登入失敗（401） | 確認閱讀器填入的帳號、密碼與服務設定一致 |
| 要求 HTTPS（426），或啟用登入後無法啟動 | 確認 HTTPS 公開網址、本機 TLS 或受信任代理設定，以及代理的 `X-Forwarded-Proto` |
| 舊作品或下一頁連結出現 404 | 書庫版本可能已更新，回到書庫入口重新開啟 |
| 搜尋出現 422 | 檢查搜尋語法；手動呼叫 OPDS 2.0 時使用 `query`，成對的篩選參數不可缺漏 |
| 暫時無法使用（503） | 書庫可能正在切換發佈版本，稍後重試；持續發生時請由管理者檢查 ingest 狀態、檔案與資料庫是否一致 |
| 下載或逐頁閱讀出現 416 | 用戶端要求的下載範圍無效；服務一次只支援一個有效的 byte range，可嘗試重新下載 |
| 書庫能開啟但沒有作品 | 確認 ingest 已發佈可下載檔案；只有中繼資料的書庫會顯示為空 |
| 搜尋結果為空 | 先減少條件，確認 GID、標籤大小寫與空白、UTC 日期及頁數是否符合 |
| 啟動時找不到目錄或 `publication.lock` | 確認路徑、掛載與讀取權限，並先由 ingest 完成書庫準備 |

書庫切換期間 `/health` 仍會回報服務存活，並不代表作品此時可讀取。
若 503 是因為未完成的 `ACTIVATING` 狀態，應由 ingest 處理復原，
不要自行刪除 marker 或鎖定檔案。

## 授權

本專案採用 GNU General Public License v3.0，詳見 [LICENSE](LICENSE)。
隨附協定 schema 的來源與授權記錄於
[verification/opds](verification/opds)。
