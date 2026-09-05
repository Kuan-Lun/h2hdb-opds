# AGENTS.md

## 政策來源

- 本檔是此 repository 的唯一代理開發政策來源。
- 其他代理入口只能要求完整閱讀本檔，不得複製另一份政策。
- 可執行規則以 repository 內的 scripts 與設定檔為準。

## 溝通

- 最終回覆一律使用繁體中文。
- 程式碼、識別字、命令、檔名與 commit message 可使用英文。
- 不得為了承載回覆而新增 Markdown 文件。
- 移除 compatibility path、改變公開行為或採用例外時，必須在對話及
  最終回覆中明確說明。

## 設計與修改原則

- 不預設存在最小修改或向後相容要求。
- 在任務範圍內選擇架構、可讀性與可測試性最好的完整結果。
- 綜合考慮 SOLID、KISS、YAGNI、內聚性與低耦合。
- 必要的局部重構可直接納入任務。
- 若會實質擴大任務範圍、改變原要求未涵蓋的公開行為，或引入資料遷移，
  必須先取得使用者同意。
- 任務直接涉及的 legacy compatibility code 應移除，不保留 shim；不全面
  清理與任務無關的 legacy code。
- generated output 不得直接修改；必須修改 generator 或 source 後重新產生。

## 工作樹與 Git

- 唯讀分析不建立 branch。
- 凡會修改 tracked files 的任務，使用
  `scripts/detect-primary-branch.sh` 判定 primary，並建立專用 task branch。
- 不得 stash、reset、clean、覆寫或混入既有使用者修改。
- 工作樹不乾淨時，從 committed primary 建立獨立 worktree。
- task branch 可包含多個邏輯 Conventional Commits。避免巨大 commit；小而
  內聚的任務仍可只有一個 commit。
- 任務完成後執行 `scripts/git-flow-merge.sh`。該腳本負責完整 gate、
  `--no-ff` merge、安全移除 task worktree，以及以 `git branch -d`
  刪除已合併的本機 branch。
- primary 在任務期間可以推進；整合只要求 primary 與 task branch 有共同
  ancestor，不要求 task branch 仍直接基於目前 primary tip。
- merge conflict 或 gate failure 時必須 abort merge 並保留 task branch。
- merge 後收到的任何 follow-up 都建立新的 task branch。
- 本機 task branch、commit、`--no-ff` merge 與 `branch -d` 已獲預先
  授權。
- fetch、pull、push、remote branch、tag、release、publish、deploy 與任何
  force 操作仍須逐次明確授權。
- 不得使用 `--no-verify`。

## 提交格式

- 所有非 merge commit 必須符合 Conventional Commits。
- Breaking change 使用 `type!:` 或 `BREAKING CHANGE:` footer。
- project version 更新使用獨立 commit：
  `chore(release): bump version to X.Y.Z`。

## 版本政策

- `pyproject.toml` 的 `[project].version` 是唯一 project version source。
- project version 固定使用 `X.Y.Z`。
- 1.0 前，`Y` 是 compatibility lane，`Z` 是同一 lane 內的相容 release
  counter。相容修正或功能遞增 `Z`；breaking change 遞增 `Y` 並將
  `Z` 歸零。
- 1.0 後使用標準 Semantic Versioning。
- 整個 task branch 只在整合前更新一次 project version。
- shipped runtime 或 deployment surface 有變更時，至少需要相容升版。
- Breaking API、CLI、config、schema、protocol、資料格式或 Python/platform
  support 變更必須提高 compatibility lane 或 major。
- tests、一般文件、IDE、hooks、CI 與 dev-only tooling 單獨變更時不升版。
- 未分類路徑必須明確判定 impact，不得靜默當作 `none`。
- `Version-Impact: none` 必須附具體理由，並在最終回覆揭露。
- project version 變更必須觸發完整 direct dependency audit。
- `scripts/check-version.py` 以 staged merge candidate 判定 release surface，
  並強制 task-level `X.Y.Z` 只升版一次；pre-merge gate 不得略過。
- 升版後執行 `scripts/audit-dependencies.py --review-note "<相容性結論>"`，
  並將 `.release/dependency-audit.json` 納入 task branch。receipt 必須符合
  candidate version 與完整 dependency manifest。

## 依賴與環境

- repository 必須能從單一乾淨 checkout 重建，不得依賴固定 sibling clone
  路徑。
- 明確跨 repository 任務可使用傳入的 wheel、Git URL/ref 或 repository
  path；sibling discovery 只能是選擇性的效能優化。
- Python registry dependencies 原則上使用 `>=` lower bound；合理 upper
  bound 與 `!=` 可以保留，但必須有相容性依據。
- 精確版本只允許經驗證且有文件理由的特殊契約。
- dependency audit 必須涵蓋 build、runtime、optional 與 development direct
  dependencies，並搜尋現有 upper bound 之外的候選版本。
- 有新版時必須檢查 release notes、驗證相容性並嘗試修正問題。
- `uv.lock` 不得成為環境重建或驗證的輸入；`scripts/rebuild-env.sh` 可
  使用 `uv venv` 與 `uv pip`，但不得使用會依賴 project lockfile 的
  同步流程。
- Node tooling 使用 `npm install --package-lock=false`，不得產生或提交
  `package-lock.json`。
- 不得依賴 system-wide lint、format、type-check 或 Markdown 工具。
- `requires-python` 使用 `>=3.14`；只有經驗證的壞版本可使用 `!=`。

## 品質工具

- `pyproject.toml` 是 Ruff 與 mypy 的唯一規則來源。
- 使用 Ruff lint 與 Ruff formatter，不使用 Black。
- Ruff 使用適合專案的嚴格規則集，不從 `ALL` 出發；每個停用規則必須
  記錄理由。
- mypy 使用標準 `strict = true`。不得保留 `mypy.ini`。
- module 例外使用精確 TOML overrides。
- `type: ignore` 必須指定 error code 並附理由。
- `noqa` 必須指定 rule code 並附理由。
- Markdown 使用 repository-local `markdownlint-cli2`。
- VS Code 使用相同設定與 repository-local environment；CLI gate 是最終
  權威，IDE diagnostics 為即時輔助。

## 檢查分層

- `scripts/format.sh`：明確執行會修改檔案的 formatter 或 fixer。
- `scripts/check-fast.sh`：離線、唯讀的 Ruff、format check、mypy 與
  markdownlint；每次非 merge commit 執行。
- `scripts/check-full.sh`：fast gate、完整測試、build、wheel smoke 及本
  repository 的特殊檢查；整合候選只跑一次。
- dependency audit 可連網，但 hooks 只驗證本機 receipt，不在 commit
  過程連網。
- GitHub Actions 只呼叫相同 scripts，並保留 trusted publishing、平台特有
  或本機無法可靠重現的檢查。
- 不使用 Claude、Codex 或其他 provider-specific Stop hooks 重複檢查。

## 測試與例外

- runtime 行為變更必須新增或更新測試；bug fix 必須有 regression test。
- 新功能涵蓋正常、邊界與錯誤路徑。
- 數值測試固定隨機種子；容許誤差需有依據。
- flaky test 視為失敗，不得以重跑掩蓋。
- 不設定跨 repository 的統一 coverage 百分比。
- live account、network、production 或 destructive probe 不得進入 hooks、
  一般 pytest 或自動 merge gate。
- `skip` 或 `xfail` 必須有理由；`xfail` 原則上使用 `strict=True`。
- 不得為通過檢查而全域放寬工具設定。

## 完成回報

最終回覆必須包含：

- 實作及公開行為變化。
- 移除的 compatibility path。
- project version 與 dependency audit 結果。
- commits 與完整檢查結果。
- primary branch 與 merge commit。
- branch/worktree 是否已清除。
- 是否仍未 push、publish 或 deploy。

## Repository-specific policy

`h2hdb-opds` 是 H2HDB catalog 的 OPDS 1.2 與 2.0 HTTP service，發佈名稱為
`h2hdb-opds`，公開 import package 為 `h2hdb_opds`。

### Ownership boundary

本 repository 擁有 FastAPI/ASGI integration、OPDS models/serialization、
navigation、bounded discovery/facets/recent windows、publication seek
pagination、presentation/media、authentication、acquisition、Range 與
conditional HTTP response。兩版 search/facet 只能使用 core 公開的
revision-scoped bounded authority，不得自行掃描 database、建立第二套 index
或從 transient joins 推導結果。

`h2hdb` core 獨占 connector、transaction、schema/migration、durable queue、
coordination fencing 與 catalog repository。只能使用 core 公開介面，不得
import connector/repository internals，不得建立或 migrate schema。database
access 必須強制 read-only，只能公開已 publication 的 catalog revision。
production startup 使用 `open_database()` 執行精確 epoch-3 `READY` audit；
caller 注入的 `CatalogReader` 視為已初始化 boundary，直接使用。任何 startup
path 均不得呼叫 `migrate()`。

### HTTP and filesystem invariants

- `config.py` 擁有 frozen OPDS/server/authentication/core config models；
  `app.py` 負責 lifespan、exception mapping 與 composition；
  `catalog_service.py` 負責兩版共用的 bounded revision-pinned reads；
  `search.py` 負責 bounded DSL parse/canonical render，`discovery.py` 負責
  protocol-neutral exact filter/query mapping；
  `opds12.py`/`atom.py` 與 `opds2.py`/`serialization.py` 分別負責協定 routes
  與 serialization；`auth.py` 負責 Basic auth 與 OPDS 2 authentication
  document；`acquisition.py` 負責 sealed extent responses；`media.py` 負責
  version-neutral page/thumbnail routes；`publication.py` 負責 URI identifier 與
  acquisition relation policy。
- OPDS 1.2 與 2 root 必須同樣提供 `All Publications`、
  `Recently Uploaded`、`Recently Downloaded` 三個 navigation item。OPDS 1.2
  直接列出三個 entry；OPDS 2 以 `Browse` 與 `Recent Activity` 兩個 `groups`
  分組，且不得另外重複頂層 `navigation`。All 與 search 使用 core discovery
  seek cursor，page limit 為 1..128；recent 使用 core
  authoritative order 的固定完整 top-128 window，不接受 limit、cursor 或
  offset，也不產生 next/first/crawlable。OPDS 1.2 feed/entry 必須滿足 Atom 與
  OPDS RNC；OPDS 2 feed 使用 `application/opds+json`，standalone publication
  使用 `application/opds-publication+json`。
- OPDS 1.2 search 使用 `q` 與 OpenSearch `{searchTerms}`；OPDS 2 依規格只使用
  `query`，舊 `q` 必須回 422，不保留 alias。`tag` 與 `tag_namespace`、
  `contributor` 與 `role` 分別必須成對。Facet filter bytes 必須 exact
  round-trip，不得 trim、Unicode normalize 或 collapse whitespace。Facet values
  必須 bounded paged，超過第一個 window 時提供 followable next/More link。
- 兩版 search 共用 implicit AND DSL：bare text、`title:`、`gid:`、`namespace:value`、
  `uploaded:`、`downloaded:` 與 `pages:`。AND 只由空白隱含，不支援 `AND`、`OR`、
  `NOT` 或括號運算子。純數字保持 text；title 只比對 display/source title authority。
  未 quoted 的 `title`、`gid`、`uploaded`、`downloaded`、`pages` 是 reserved typed
  fields；其他 namespace 包括 Unicode 與未知名稱均為 exact subject filter，合法
  未知 namespace 無匹配時回 empty result。`language:chinese` 保持 subject 語義，
  HTTP `language=` 是獨立的作品語言 filter。
- Quoted namespace 一律為 subject namespace，可表達 reserved names、colon 或空白，
  例如 `"title":foo`、`"名:稱":"a  b"`。Quoted value 只負責值分組與 escaping，
  不宣稱 phrase matching；整詞 `"language:chinese"` 保持 bare text。未 quoted 的
  `tag:` 一律回 422，移除舊 `tag:namespace:value` path，不保留 alias；名為 `tag` 的
  namespace 使用 `"tag":value`。HTTP `tag`/`tag_namespace` pair 維持支援。
  Malformed syntax、absent、blank 或無有效條件的 query 回 422；合法 filter-only query
  不要求 free-text lexeme。
- 多 tag 使用 `CatalogDiscoveryQuery.subjects` tuple，全部 AND；HTTP 單 tag pair
  與 DSL tags 合併、exact 去重，不得保留 singular `subject` alias。Scalar field 不得
  重複；多個 title clause 合併 AND。Dates 使用 UTC calendar days，inclusive 上界日期
  轉成下一日 exclusive midnight；pages 使用 sealed artifact actual page count 的
  inclusive 0..4096 bounds，不從 source metadata 或 request-time scanning 推導。
- 完整 DSL transport 以 128 KiB UTF-8 bytes 與最多 32 clauses bounded；core 的
  text/title 各 1024 canonical-NFD UTF-8 bytes、合計 16 field-scoped lexemes、16 exact
  tags 及每個 tag namespace/value 128/1024 UTF-8 bytes bounds 仍須維持。Transport
  budget 必須容納合法 typed query 與 facet replacement 的完整 quote expansion。
  Self/first/next/facet/More links 由 typed query 重新產生 canonical `namespace:value`
  DSL，不保存 raw caller syntax 作為 authority。Subject facet clear/reselect 清除整個 tag family，
  保留其他 typed conditions；無 DSL condition 時 canonical links 使用 publications
  route，有 DSL condition 時使用 search route。
- Discovery 是 acquisition-only surface。revision `artifact_count=0` 時，即使
  `publication_count>0` 也直接產生 schema-valid empty discovery/facets/recent；
  `artifact_count=publication_count` 時每個 publication 必須有 artifact；其他
  count shape fail closed。OPDS 2 empty feed 不得輸出 `publications: []`，必須
  省略該 member 並提供非空 navigation fallback。
- Publication identifier 只接受 canonical
  `urn:h2h:gallery:<positive-int63>`；0、leading zero、非 ASCII digit、overflow 與
  arbitrary URI 必須 fail closed，且 suffix 必須等於 publication authoritative
  `gid`。Anonymous acquisition 使用 open-access relation，auth-enabled catalog
  使用 generic acquisition relation。每本 publication 只接受一個 exact
  `application/vnd.comicbook+zip` direct artifact；其他 adapter MIME fail closed。
- OPDS 1.2 publication 有 page 時必須輸出 cover、thumbnail 與 OPDS-PSE stream；
  page 0 是 cover、thumbnail 是 ingest 預生成的 320px resource、PSE page number
  是 zero-based，OPDS boundary 自己驗 0..4096 page count、JPEG resources 與
  cover/thumbnail presence shape；href 必須保留 literal `{pageNumber}`，只輸出
  `pse:count`，不輸出 `lastRead`/`maxWidth`。OPDS 2 只輸出 `images` 與正數
  `numberOfPages`，不得宣稱 non-normative PSE conformance。
- acquisition ETag 是 ingest activation 已驗證並封存的 strong SHA-256
  validator。每次 request 不得重新 hash 或複製整個 CBZ。RFC
  precondition 必須按 `If-Match` 到 `If-Modified-Since` 的順序處理。
- 只支援單一 byte range。invalid、multiple 或 unsatisfiable range 回 416
  並帶 `Content-Range`；未知 unit 忽略；`If-Range` mismatch 回完整 200；
  date comparison 必須精確。
- `library_root` 是 single-library host root 下完整 `current` 的獨立 read-only
  mount，也是唯一 public current tree；OPDS 需要 `acquisitions` 與 `artwork`
  兩個 subtree，Komga 只能 mount `current/acquisitions`。Storage object 只能透過
  core opaque `StorageObjectKey` 解析，且只接受 exact
  `managed-filesystem-v2` codec；未知 codec fail closed。open 時不得 follow
  symlink，且只接受符合 sealed enclosing-object size、合法 positive bounded
  extent 的 regular file。
- `coordination_root` 是同一 host root 下 sibling `.h2hdb-coordination` 的獨立
  read-only mount，只包含 permanent `publication.lock` 與 optional
  `ACTIVATING`。不得把 parent root 或 `.h2hdb-state` 的 staging、quarantine、
  journal、locks 暴露給 OPDS。每個 catalog/feed/publication read 都在
  nonblocking shared `flock` 下檢查 marker；acquisition 必須在同一 lock 內
  依序 pin current head、
  取得 artifact、open FD、驗證 fstat size，再重查 current head。Lock contention
  或任何 marker entry 都回 503；已開啟的 immutable inode 可在 release lock
  後直接 Range stream。
- OPDS 不得建立、修改或刪除 coordination state。正常 SIGTERM、Compose stop
  或非正常 process termination 都只靠 FD close 釋放 shared lock；health route
  在 activation 期間維持可用，避免把受控 maintenance 誤判成 process failure。
- absolute link 只能來自 canonical public base URL，不能信任 request Host。
  Basic auth 只允許 effective HTTPS：local TLS 或明確 trusted
  TLS-terminating proxy。credential comparison 使用
  `secrets.compare_digest`。
- 兩版 discovery/facets/recent 只能使用 core pinned
  `discover_publications`、`list_publication_facets` 與
  `list_recent_publications`；page/thumbnail 只能使用 core presentation APIs。
  每個 serialized publication 必須有 acquisition link；不得使用 legacy listing
  API、`OFFSET`、逐頁 `COUNT(*)`、OPDS-side sort、request-time ZIP parsing/image
  resize 或 protocol-specific durable pagination state。
- 所有 feed、publication、pagination 與 acquisition link 都攜帶 selected
  revision。省略 revision 時選 current head；明確 revision 只有等於 current
  head 才接受，其他回 404。不得將 stale revision 替換成 current data，或在
  同一 response 混用 revisions。

### Verification

- tests 使用 injected reader、temporary artifact 與 local ASGI transport，
  不得啟動 production server、連線 production database 或依賴外部網路。
- OPDS 1.2 XML tests 使用 namespace-aware semantic assertions，不使用易碎的
  whole-document snapshot；v1.2 routes/tests 必須保持可整體移除，且不得複製
  共用 CBZ、Range、coordination 或 catalog state。
- authentication、trusted proxy、canonical URL、storage-key containment、
  symlink、activation marker/lock、atomic replacement、sealed size、direct
  streaming、Range、conditional request 與 revision pinning 變更都必須新增
  regression coverage。
- `verification/opds/schemas` 是 pinned immutable upstream/generated snapshot
  closure；`sources.toml` 記錄 source commit/URL、license/notice、Trang coordinate
  與 SHA-256，`scripts/check-opds-schema-snapshots.py` 驗 exact file closure/hash。
  `opds-upstream.rng`/`atom.rng` 只能由 pinned RNC 與 pinned Trang 重新產生；
  `opds.rng` 只能由 hashed deterministic generator 加上唯一 CR/TAB typo correction，
  不得包含 PSE URI overlay 或手改。Raw upstream RNC 必須維持 byte-for-byte。
  CLI 與 pytest 必須共用 validation-only PSE helper：先在原文件驗 exact stream
  rel、`image/jpeg`、1..4096 `pse:count`、唯一 literal `{pageNumber}` 且拒絕其餘
  braces/PSE attributes，再只於 deep copy 以合法 sentinel 替代 token 後套用 strict
  runtime RNG。Release gate 不得依賴 Java、network 或 unresolved JSON Schema ref。
- `scripts/check-full.sh` 必須離線 compile unmodified/strict-runtime OPDS 1.2
  RELAX NG 與完整
  OPDS 2/Readium JSON Schema closure，並透過 pytest 驗實際 root/all/search/recent/
  facet/standalone/empty corpus及 invalid Atom、URI、empty array、unresolved-ref
  negative controls，再執行 sdist/wheel build 與 installed wheel CLI smoke。
