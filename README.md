# h2hdb-opds

`h2hdb-opds` 讓你在支援 OPDS 的閱讀器中瀏覽、搜尋及下載 H2HDB 漫畫書庫。
支援 OPDS 1.2 與 OPDS 2.0，提供封面、縮圖、CBZ 下載，以及供 OPDS-PSE
閱讀器使用的逐頁閱讀功能。

已有書庫網址時，直接依下節加入閱讀器。要在自己的主機提供服務，
請見[自行架設](#自行架設)。服務需要已由
[H2HDB](https://github.com/Kuan-Lun/h2hdb) 與
[ingest](https://github.com/Kuan-Lun/h2hdb-ingest) 準備好的書庫，
不會直接掃描漫畫資料夾或匯入檔案。

## 加入閱讀器

1. 在閱讀器中新增 OPDS 書庫或目錄。
2. 填入下列其中一個網址，把 `https://books.example.net` 換成你的服務位址。
3. 若書庫有啟用登入，填入管理者提供的帳號與密碼。

| 閱讀器支援的功能 | 書庫網址 |
| --- | --- |
| OPDS 1.2；需要 OPDS-PSE 逐頁閱讀時使用此網址 | `https://books.example.net/opds/v1.2/catalog` |
| OPDS 2.0 | `https://books.example.net/opds/v2` |

兩個入口提供相同書庫與搜尋條件。逐頁閱讀需要閱讀器支援 OPDS-PSE；
OPDS 2.0 提供封面、縮圖及 CBZ 下載。實際按鈕名稱與篩選介面依閱讀器而異。

## 瀏覽、閱讀與下載

| 畫面上的入口 | 內容 |
| --- | --- |
| `All Publications` | 全部可下載作品，可翻頁、搜尋及篩選 |
| `Recently Uploaded` | 依來源上傳時間排列的最新 128 部作品 |
| `Recently Downloaded` | 依來源下載時間排列的最新 128 部作品 |
| `Artists` | 先選作者標籤，再瀏覽作品 |
| `Groups` | 先選團隊標籤，再瀏覽作品 |
| `Parodies` | 先選原作標籤，再瀏覽二創作品 |
| `Characters` | 先選角色標籤，再瀏覽作品 |
| `Soushuuhen` | 含 `other:soushuuhen` 標籤的作品 |
| `Multi-work Series` | 含 `other:multi-work series` 標籤的作品 |
| `Uncensored` | 含 `other:uncensored` 標籤的作品 |
| `Goudoushi` | 含 `other:goudoushi` 標籤的合同本 |

作者、團隊、原作與角色目錄依各標籤最新作品的上傳時間，由新到舊排列。
標籤入口內的作品也依上傳時間排列，同時間再依標題排序。
清單有下一頁時，使用閱讀器的翻頁功能即可；預設每頁 50 筆。
首頁與標籤入口可能顯示第一本作品的封面縮圖，是否呈現取決於閱讀器。
目前首頁提供上述十一個固定入口，不支援自訂入口設定。

「下載時間」指匯入時記錄的來源下載時間，不是你透過閱讀器下載 CBZ 的時間。
兩個最近動態入口各最多列出 128 部，不提供下一頁；尋找更早的作品請使用搜尋。

在作品頁選擇下載即可取得 CBZ；支援 OPDS-PSE 的閱讀器也能逐頁讀取。
下載支援續傳，能否使用仍取決於閱讀器。
個人書架、已讀狀態與閱讀進度由閱讀器管理，伺服器不保存這些資料。

書庫更新後，舊目錄連結會重新開啟最新版第一頁，保留搜尋與篩選條件。
如果閱讀器沒有自動跳轉，或舊作品、圖片、下載連結失效，
請重新開啟書庫入口並選取作品。

## 搜尋作品

在閱讀器的書庫搜尋框輸入關鍵字或下列條件。
條件之間用空白分隔，**所有條件都必須符合**；不支援 `AND`、`OR`、`NOT`
或括號運算子。

| 想找什麼 | 搜尋框輸入 |
| --- | --- |
| 同時含有兩個關鍵字 | `不知火 chinese` |
| 標題含「不知火」 | `title:不知火` |
| 指定 GID 的作品 | `gid:1834943` |
| 帶有中文標籤 | `language:chinese` |
| 標籤值含空白 | `female:"mind control"` 或 `female:“mind control”` |
| 同時帶有語言與作者標籤 | `language:chinese artist:alice` |
| 同時帶有兩個作者標籤 | `artist:alice artist:bob` |
| 符合標題、標籤與頁數 | `title:不知火 language:chinese pages:40..200` |
| 在指定日期區間下載 | `downloaded:2026-09-01..2026-09-05` |

請把範例中的標題、GID 和標籤換成書庫內的資料。
只輸入 `pages:40..200` 這類條件也可以搜尋，不必加關鍵字。

### 關鍵字與標籤

一般關鍵字比對標題、貢獻者名稱與標籤值，不搜尋簡介或 CBZ 檔名。
文字搜尋不區分大小寫，但不提供模糊或連續片語搜尋。
`title:"Alpha Gallery"` 表示標題需同時含有這兩個詞，不要求相鄰或固定順序。
純數字仍是文字，`gid:1834943` 才會精確指定作品；GID 不能有前置零。

`命名空間:值` 精確比對來源標籤，包含大小寫與空白。
例如 `language:chinese` 是來源標籤，與閱讀器篩選選單中的作品語言可能不同。
標籤值有空白時，加上成對的 ASCII 雙引號 `"..."` 或彎雙引號 `“...”`。
iOS 與 macOS 的智慧標點會在冒號後把 `"` 換成 `”`，
因此 `female:”mind control”` 也視為有效。
ASCII 引號與彎引號不能混搭，`”...“` 反向配對無效，
單引號與中文書名號不能用來分組。

`title`、`gid`、`uploaded`、`downloaded` 和 `pages` 是小寫保留欄位。
要搜尋名稱剛好相同的標籤，請把命名空間也加引號，例如 `"title":foo`。
如果冒號是文字的一部分，請把整個詞加引號，例如 `"re:zero"`。
標籤語法直接寫 `artist:alice`，不要加 `tag:` 前綴；
命名空間本身名為 `tag` 時使用 `"tag":值`。

引號內可用 `\"` 表示 ASCII 雙引號、`\\` 表示反斜線。
標籤值本身含彎雙引號時，可用 ASCII 引號包住，例如 `artist:"a“b”"`，
以保留原始標籤內容。

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

`uploaded:` 和 `downloaded:` 都支援單日及日期區間。
日期固定使用 `YYYY-MM-DD`，依 **UTC 日期** 判斷。
例如 UTC 的 9 月 5 日，對應臺灣時間 9 月 5 日 08:00 至 9 月 6 日 08:00 前。
頁數是已發佈 CBZ 的實際頁數，可指定 0 到 4096，區間起點不能大於終點。

### 使用篩選選單

閱讀器若有顯示篩選選單，可以再依語言、標籤或貢獻者縮小結果。
選取標籤篩選會替換目前所有標籤條件；需要同時限定多個標籤時，
請在搜尋框輸入多個 `命名空間:值`。
`All` 清除該組篩選，`More` 列出更多選項。
篩選旁的數量保留搜尋及其他組條件，方便比較換選其他值後的結果數量。

搜尋沒有結果時，先減少條件，再檢查標籤大小寫、空白、日期與頁數。
出現搜尋錯誤（422）時，檢查引號是否成對、日期是否存在，
以及 GID、同一種日期或頁數欄位是否重複指定。
查詢不能空白，最多 32 個條件、16 個不同標籤；一般文字與標題合計最多 16 個字詞。

## 自行架設

### 準備環境與書庫

需要 Python 3.14 以上版本，以及支援 POSIX 檔案鎖的環境，例如 Linux 或 macOS。
此版本使用 `h2hdb>=0.41.1,<0.43.0`，對應 epoch 3／schema version 8。
Core 0.41.1 修正清理中斷時的完整稽核誤判；Core 0.42 保留相同的
catalog API 與 schema 8。HTTP、catalog 與 CBZ 格式不變。
啟動前，請先由 H2HDB 與 ingest 完成初始化及書庫發佈，準備：

- 已完成初始化、狀態為 `READY` 的相容資料庫。
- ingest 產生的完整 `current` 目錄，包含 `acquisitions` 與 `artwork`。
- 同一書庫旁的 `.h2hdb-coordination` 目錄，內含既有的 `publication.lock`。

服務以唯讀方式使用資料庫及書庫，不會補建缺少的檔案或自動升級資料庫。
只有已發佈且有可下載檔案的內容會出現在閱讀器。
目前需要 ingest 的 `managed-filesystem-v2` 書庫，不支援舊 `hash-v1` 格式。

既有 exact schema 7 資料庫需先停止所有 consumers，由管理者依
[H2HDB 的升級說明](https://github.com/Kuan-Lun/h2hdb#readme)
使用歷史 Core 0.41.2 的離線 `upgrade-source-collection-schema.py` 轉至 schema 8，
保留 catalog、來源觀測與 CBZ，不需清空資料庫或重新封裝。
Core 0.42 已移除此一次性工具，需依 Core 說明使用歷史 checkout 或既有升級包。
Schema 6 須先使用 Core 0.40.0 的 `upgrade-audit-schema.py` 轉至 schema 7。
升級完成後，須確認每個應用映像中的 Core 與 consumer 版本都相容，再啟動服務；
一次性升級容器不會更新應用映像。其他舊版或不相符資料庫由 Core 的管理工具拒絕，
應先確認其版本及可用轉換途徑，不應直接刪除資料。

### 安裝

```bash
python3.14 -m venv .venv
.venv/bin/python -m pip install h2hdb-opds
```

如果是從本專案原始碼安裝，在專案目錄將最後一行改為：

```bash
.venv/bin/python -m pip install .
```

### 先在本機啟動

建立 `opds.json`，把資料庫與書庫路徑換成實際的**絕對路徑**：

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

若現有書庫使用 MariaDB，將 `core.database` 替換為：

```json
{
  "sql_type": "mariadb",
  "host": "database.example.net",
  "port": 3306,
  "user": "h2hdb_reader",
  "password": "${H2HDB_DATABASE_PASSWORD}",
  "database": "h2h",
  "access_mode": "read-only"
}
```

MariaDB 範例需在啟動服務的環境中設定 `H2HDB_DATABASE_PASSWORD`。
書庫及 coordination 路徑不能使用符號連結。

啟動服務：

```bash
.venv/bin/h2hdb-opds --config opds.json
```

在另一個終端機確認狀態與版本：

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/version
```

健康檢查成功會回傳 `{"status":"ok"}`；版本查詢會回傳服務名稱與已安裝套件版本。
兩者不需要登入，也不代表書庫已完成完整資料稽核。
完整稽核由 ingest 管理，或由管理者明確執行 H2HDB 的 `check`。

同一台主機的閱讀器可加入 `http://127.0.0.1:8000/opds/v1.2/catalog`。
這份設定只接受本機連線且未啟用登入；其他裝置請使用下一節的對外設定。

### 提供給其他裝置並啟用登入

`public_base_url` 必須是閱讀器實際可連到的網址，因為封面、下載及翻頁連結
都由此設定產生。若服務位於網址的子路徑，也要包含該前綴。

假設已有 HTTPS 反向代理，將請求轉送到同一主機的 `127.0.0.1:8000`，
請替換或加入 `opds.json` 的下列欄位：

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
在啟動服務的環境中設定密碼後重新啟動：

```bash
export H2HDB_OPDS_AUTH_PASSWORD='replace-with-your-password'
.venv/bin/h2hdb-opds --config opds.json
```

JSON 字串只有完整寫成 `${環境變數名稱}` 才會替換，不能在字串中間插入變數。
變數未設定時會停止啟動；資料庫密碼也支援相同寫法。
帳號和密碼必須一起設定，省略兩者即為不需登入的書庫。

反向代理必須傳送 `X-Forwarded-Proto: https`。
`trusted_proxy_ips` 只填實際代理的來源 IP 或網段；代理不在同一主機時，
另需調整監聽位址與網路設定，讓代理可連到服務。
也可以在 `server` 同時設定 `tls_certificate` 與 `tls_private_key` 的檔案路徑，
直接由服務提供 HTTPS。Basic 登入必須使用 HTTPS。

每頁預設 50 筆，可用頂層 `default_page_size` 調整，並以 `maximum_page_size`
設定上限。兩者都需介於 1 到 128，預設頁面大小不可大於上限。

### 容器掛載

將 ingest 的完整 `current` 與 coordination 目錄分別掛載為唯讀。
以下片段的容器路徑對應前述設定：

```yaml
volumes:
  - /volume1/h2hdb/comics/current:/srv/h2hdb/comics/current:ro
  - /volume1/h2hdb/comics/.h2hdb-coordination:/srv/h2hdb/comics/.h2hdb-coordination:ro
```

容器還需安裝本套件，提供可讀取的設定檔及資料庫，並依部署方式設定網路。
`library_root` 必須同時包含 `acquisitions` 與 `artwork`，不能只掛載 CBZ 子目錄。
不要掛入書庫上層目錄或 ingest 私有的 `.h2hdb-state`。

## 疑難排解

| 現象 | 處理方式 |
| --- | --- |
| 其他裝置連不上，或封面、下載指向 `127.0.0.1` | 確認 `public_base_url` 是裝置可達的網址，並檢查反向代理與監聽設定 |
| 登入失敗（401） | 確認閱讀器的帳號密碼與服務設定一致 |
| 要求 HTTPS（426），或啟用登入後無法啟動 | 檢查 HTTPS 網址、TLS 或受信任代理設定，以及 `X-Forwarded-Proto` |
| 更新後翻頁回到第一頁，或收到 303 | 書庫已更新；閱讀器若未自動跳轉，重新開啟書庫入口 |
| 舊作品、下載或圖片連結出現 404 | 回到最新書庫入口重新選取作品 |
| 搜尋出現 422 | 檢查搜尋語法、引號、重複欄位及日期／頁數範圍 |
| 暫時無法使用（503，`library_activating`） | 稍後重試；持續發生時由管理者檢查 ingest 發佈與復原狀態 |
| 資料不一致（500，`library_integrity_error` 或 `catalog_integrity_error`） | 由管理者檢查掛載、權限、發佈檔案及服務記錄；單純等待未必能解決 |
| 下載出現 416 | 嘗試重新下載；服務只接受單一有效的續傳範圍 |
| 書庫能開啟但沒有作品 | 確認 ingest 已發佈可下載檔案；只有中繼資料的書庫會顯示為空 |
| 啟動時找不到目錄或 `publication.lock` | 確認絕對路徑、掛載與讀取權限，並先由 ingest 完成書庫準備 |
| 升級後仍顯示舊版 | 確認更新的是正在執行的 Python 環境，重啟服務後再查詢 `/version` |

書庫切換期間 `/health` 仍會回報服務存活，不代表作品此時可讀取。
若存在未完成的 `ACTIVATING` 狀態，應由 ingest 處理復原，
不要自行刪除標記或鎖定檔案。

## 授權

本專案採用 GNU General Public License v3.0，詳見 [LICENSE](LICENSE)。
隨附協定 schema 的來源與授權記錄於 [verification/opds](verification/opds)。
