# 資料字典

**下載資料前，先查`select * from v_data_dictionary`**——這是資料庫自己的說明文件，每張表、每個欄位的中文意思都在裡面(100%覆蓋，沒有欄位缺說明)，不用先讀這份文件才知道怎麼用。Supabase Table Editor滑鼠移到欄位上也看得到同樣的說明。這份`DATA_DICTIONARY.md`補的是`COMMENT`放不下的東西：欄位之間的關聯、已知限制、方法論的來龍去脈。

## 表結構

```
factset_revisions (主表，一篇文章一列)
  └─ factset_estimates (子表，僅EPS事件有，一個revision可能對多列，多年度/多指標)

ticker_industry_official (官方產業/市場別，獨立參照表)
industry_alias           (舊產業文字對照，補ticker_industry_official涵蓋不到的)
concept_map              (概念股分類，獨立參照表，many-to-many)

stock_prices             (個股每日收盤價，策略回測用，見下方stock_prices/taiex_index說明)
taiex_index              (台股加權指數每日收盤，策略回測的交易日曆基準)

v_revisions_normalized   (view，把上面幾張表JOIN好，網站/研究都從這個view讀)
v_unified_target_events  (view，正式的目標價方向/幅度分析從這個view讀，見下方第5點)
v_eps_only_target_events (view，只用EPS內嵌目標價串成的序列，跟v_unified_target_events
                           同一套邏輯，差在資料來源範圍，用來做三方比較/驗證EPS通道本身準不準)
v_data_dictionary        (view，整個資料庫的自我說明文件，下載資料前先查這個)
get_strategy_price_bundle() (RPC function，把stock_prices/taiex_index打包成strategy.html
                           要的{dates,tickers}精簡格式，一次呼叫取代287k列分頁抓取)
```

## factset_revisions（主表）

一列 = 一篇鉅亨網「Factset最新調查」快訊。`event_type`決定這篇是目標價修正還是EPS修正，兩種事件共用同一張表，各自只有對應的欄位有值。

| 欄位 | 說明 |
|---|---|
| `direction` | UP/DOWN，已交叉驗證跟`target_change_pct`正負號100%一致 |
| `old_target`/`new_target` | 目標價修正前後（僅`TARGET_PRICE`） |
| `target_change_pct` | `= (new_target/old_target-1)*100`，公式已驗證正確 |
| `old_eps`/`new_eps`/`eps_year` | EPS修正前後跟對應財年（僅`EPS`） |
| `analyst_count` | **該篇文章當下的快照人數，不是累計**——同一股票同一天可能有好幾篇文章、人數逐次往上更新（見下方已知限制①） |
| `industry_name` | 原始文字，**不要直接用**，cnyes三年間寫法改了好幾次，正式分析一律用`v_revisions_normalized.industry_canonical` |
| `price_5d_pct`/`industry_5d_pct`/`market_5d_pct` | 原文自帶的事件前5日報酬快照，**跟正式event study算法不同口徑**，不能拿來跟`event_study_full.py`算出來的CAR比較（見下方已知限制②） |
| `concept` | 抓取當下的原始文字，**不是**正式概念分類，正式分類看`concept_map`表 |
| `source_url` | 每篇文章唯一，`daily_update.py`用它做upsert去重 |

## factset_estimates（子表）

`revision_id` → `factset_revisions.id`。只有`event_type='EPS'`的revision才會有對應列，一個revision可能對多列（不同`fiscal_year`×`metric`組合，例如同時有2025/2026的EPS估值）。

## ticker_industry_official（權威產業/市場別）

來源：證交所+櫃買中心官方ISIN公開資料，**不是**從新聞文字解析出來的。`market_type`='上市'/'上櫃'，決定抓股價時用`.TW`還是`.TWO`後綴（用官方值，不要用try/except猜）。

## concept_map（概念股分類）

兩種來源、兩個獨立維度，`dimension`欄位區分：

**`dimension = 'theme'`（主題曝險，來源：官方ETF成分股權重揭露）**
- 半導體(00904台新)、IC設計(00947台新)、AI(00962台新)、智能車供應鏈(00901永豐)，見`fetch_concept_etf.py`的`ETF_CONFIG`
- 有`weight`(持股權重%)，`source`是ETF代號
- 查過資安(00875成分股是全球公司、幾乎不含台股)、核能/鈾礦(台股無對應掛牌公司)，沒有品質相當的官方ETF來源，暫不做

**`dimension = 'value_chain'`（產業鏈位置，來源：證券櫃檯買賣中心/臺灣證券交易所官方「產業價值鏈資訊平台」https://ic.tpex.org.tw）**
- 2026-07-24新增：改用這個官方平台做**全量分類**，不是ETF局部覆蓋——212/216檔追蹤股票都查得到資料(4檔平台上查無：6456 GIS-KY、6768志強-KY、6781 AES-KY、6890來億-KY，都是KY股，可能是海外公司未在此平台登記)，共780筆(industry, position)紀錄，一檔可能對應到好幾個產業鏈位置(例如台達電對應68個位置，因為業務橫跨半導體/電腦周邊/雲端運算/AI/電動車等，是真實的多角化，不是資料錯誤)
- 這是公司自行向交易所申報、公開對外揭露的官方分類，不是AI主觀判斷、也不是爬蟲網站的群眾標籤；不強制湊滿1~3個，公司填幾個就存幾個
- 沒有`weight`(這個平台不提供權重數字，只有「有/沒有」這個產業鏈位置)
- `source`欄位格式：`產業價值鏈資訊平台(位置:{子分類})，證券櫃檯買賣中心/臺灣證券交易所官方資料`——`concept`存的是28大類的上層產業別(例如「印刷電路板」「金融」「資通訊安全」)，細部位置存在source裡
- 抓取腳本：`fetch_industry_chain.py`，原始資料存在`factset_data/industry_chain_map.json`
- **這個維度取代了原本用IC設計(00947 ETF)代表value_chain的做法**——IC設計那49筆(ETF來源)還留著沒刪，跟這780筆(官方平台來源)共存，是同一dimension、不同source，可以並存不衝突

## stock_prices / taiex_index（策略回測用股價與大盤）

2026-07-28新增：把原本只存在本機的股價快照（`factset_data/prices_full.json`／`taiex_full.json`，本機檔案、不進git）搬進資料庫，原因是本機檔案沒辦法自動更新、也沒辦法讓別人接手（見下方版本記錄）。

- `stock_prices`：`(ticker, date)`複合主鍵，`close`收盤價。來源yfinance，`.TW`/`.TWO`後綴依`ticker_industry_official.market_type`決定
- `taiex_index`：`date`主鍵，`close`。來源yfinance `^TWII`，同時也是策略回測的交易日曆基準（哪些日子算交易日，看這張表有沒有資料，不是看股票本身）
- **股票清單不是寫死的名單**：`update_stock_prices.py`每次執行都重新查`SELECT DISTINCT ticker FROM factset_revisions`，抓「歷史上出現過目標價/EPS修正新聞的所有ticker」，這樣清單永遠跟事件資料同步，不用手動維護
- **更新方式**：GitHub Actions排程`.github/workflows/daily_prices.yml`，每天台灣時間15:00自動跑`update_stock_prices.py --days 10`（增量、有重疊也沒關係，upsert會覆蓋），用`SUPABASE_SERVICE_ROLE_KEY`這個repo secret寫入，完全不依賴任何人的本機電腦
- **一次性全量回補**：2026-07-28執行`update_stock_prices.py --full`，回補2021-01-01至今，206檔追蹤股票中204檔成功（`2888`、`6288`兩檔查無資料，疑似已下市/更名，`3008`大立光第一次抓時遇到yfinance暫時性錯誤，已手動補回），共269,509筆股價、1,347筆TAIEX
- **給前端的存取方式**：不要直接對`stock_prices`分頁查詢（287k列超過單次5000列上限），一律呼叫RPC `get_strategy_price_bundle()`，資料庫端已經把287k列打包成`{dates:[...], taiex_close:[...], tickers:{ticker:[...]}}`一次回傳，`strategy.html`就是這樣讀的

## 已知限制（做分析前務必知道）

1. **同日重複發稿**：cnyes會對同一檔股票同一天發好幾篇文章，隨分析師陸續更新覆蓋人數（`analyst_count`從3路更新到7這種情況）。DB故意保留全部（貼近真實新聞流），但做統計分析時要先去重，否則會讓那天被單一股票灌水。已在`build_event_dataset.py`的`dedup_same_day()`處理（保留當天`analyst_count`最高的一筆）。

2. **兩套「事件前5天報酬」不能混用**：`factset_revisions.price_5d_pct`是原文自帶的快照口徑；`event_study_full.py`算出來的`car_pre_5`是市場模型扣beta後的異常報酬。兩者計算基準完全不同，只是欄位名字看起來像，**不要拿來互相對照或加總**。

3. **PostgREST查詢有上限**：目前設定`pgrst.db_max_rows=5000`（2026-07-22前是1000，曾造成事件研究資料被截斷，見下方版本記錄）。單次查詢超過這個數字會被靜默截斷、不會報錯，抓大量資料一定要用分頁（參考`build_event_dataset.py`的`fetch_all()`）。

4. **`concept`欄位 vs `concept_map`表**：`factset_revisions.concept`是抓取當下的原始文字，跟`concept_map`表是完全不同的東西，命名容易搞混，正式分類一律用`concept_map`。

5. **EPS快訊裡也有目標價，只看`event_type='TARGET_PRICE'`會漏掉65%的獨立資訊**：EPS快訊的`new_target`欄位100%有值(是文章內附帶的「預估目標價」)，但沒有對應的`old_target`。檢查發現這些EPS內嵌目標價，有65%前後7天內完全沒有TARGET_PRICE報告可對照——不是重複資料，是被忽略的獨立資訊(例如台積電2023-04~2024-04整年只有EPS快訊、沒有TARGET_PRICE報告，但目標價其實持續在動)。只看TARGET_PRICE事件的目標價序列也證實跳動過快(相鄰變動中位數3.57%，納入EPS內嵌目標價後降到1.40%)。
   **正式的目標價方向/幅度分析一律查`v_unified_target_events`這個view**，不要直接用`factset_revisions`原始的`old_target`欄位(EPS事件該欄位100%是null，用了會安靜漏掉65%資訊，不會報錯，只會默默拿到不完整結果)。這個view把去重+串接的邏輯寫在資料庫端(SQL window function)，不是外部腳本重算的——**設計成「查這個view就是對的」，不用先讀文件才知道要怎麼處理**，`build_unified_target_series.py`只是分頁抓取這個view存檔，不重複算邏輯。

## 「反轉（跑票）」——已測試、已證實無預測力、已從網站移除（2026-07-22）

曾定義：同方向修正連續`streak_len`次、跨度`span_days`天站穩後，被材料性(`|target_change_pct|≥門檻`)夠大的反向修正打破，算一次反轉事件，用意是測「多數分析師還沒轉向時，最早出現的裂縫是否具有領先性」。

**驗證方法**：`reversal_signal_calibration.py`，用完整2023-2026資料，對streak_min∈{2,3,4,5}×span_min∈{15,30,45,60}×materiality∈{1%,3%,5%}共48組門檻組合，各自跑date-cluster event study，檢查事件後5/10/20天CAR。

**結果**：48組全部不顯著(事件後窗口)。唯一出現的顯著數字(streak≥5組，`car_pre_5` t=-2.83)是**事件前**而非事件後——代表就算是站穩趨勢後才反轉的案例，價格變動也是在反轉被記錄「之前」就已經顯著發生，反轉本身沒有逃脫「分析師整體是落後指標」這個更早已驗證的規律。網格結果存在`factset_data/reversal_calibration_grid.csv`。

**結論**：反轉徽章/排序選項已從`index.html`移除，網站footer改為明講「已測試、無預測力」，避免展示一個驗證是雜訊的東西。此定義+驗證過程保留在這裡跟`reversal_signal_calibration.py`作為研究紀錄，不代表這條路線可以再直接拿來用。

## 目標價三方來源比較（純TARGET_PRICE / 純EPS內嵌 / 統一序列，2026-07-23）

用`event_study_full.py`把三種目標價資料來源分開跑，確認EPS內嵌目標價是不是真訊號、合併是否合理：

| | 純TARGET_PRICE | 純EPS內嵌目標價 | 統一序列 |
|---|---|---|---|
| 事件數 | 2,989 | 4,100 | 5,650 |
| 落後指標(pre5) | UP t=10.93 / DOWN t=-5.47 | UP t=7.86 / DOWN t=-4.29 | UP t=10.50 / DOWN t=-4.88 |
| 調降延續力(post20) | **t=-2.75(顯著)** | t=-0.29(無效果) | t=-1.76(不顯著) |

**發現**：EPS內嵌目標價本身是真訊號(單獨測落後指標依然顯著)，合併成統一序列不會稀釋核心的落後指標結論。但「調降後延續下跌」這個效應幾乎完全來自TARGET_PRICE事件，EPS內嵌目標價完全沒有這個效應——merge後被稀釋成不顯著。提示「是否為專門發布的目標價報告」可能是有意義的區分變數，之後設計策略時值得保留、不要在merge時抹平這個差異。

## 版本記錄（影響資料解讀的重大變更）

- **2026-07-22**：修正事件研究資料被PostgREST 1000筆上限截斷的問題（實際只涵蓋2026上半年，非完整2023-2026），重建後部分結論改變（調降延續力減弱約70%、勝率盈虧比從不利轉為接近持平、電子零組件業調升效應未能複現）。詳見`event_study_full.py`/`event_study_eps.py`的輸出。
- **2026-07-22**：`concept_map`的`tier`(1/2階層)欄位改為`dimension`(theme/value_chain獨立維度)，因為階層假設被台達電(2308)案例證偽。
- **2026-07-22**：「反轉（跑票）」訊號經48組門檻校準測試後證實無事件後預測力(唯一顯著數字在事件前，屬落後性質)，從網站移除，詳見上方段落。
- **2026-07-23**：溫度計算/event study改用「統一目標價序列」(納入EPS內嵌目標價)，樣本從2,989筆增為5,650筆。分析師落後指標更穩健成立，但原本唯一的「調降延續下跌」訊號(post20 t=-2.75顯著)在更完整樣本下**不再顯著**(t=-1.76)——完整資料下事件後20天兩個方向都沒有可交易的延續效應，比先前認知更弱。
- **2026-07-23**：發現「事件後波動度變化」訊號(見RESEARCH_HANDOFF.md第7節)，調升後波動放大、調降後波動縮小，通過date-cluster+隨機基準+均值回歸排除三層檢驗，是目前唯一通過完整驗證的post-event發現。
- **2026-07-24**：產業基準改市值加權(原等權重)+樣本數<3檔退化成大盤(原<1檔即退化)。市值用`shares_outstanding.json`(yfinance目前股數，視為近似固定)×每日股價估算，權重用前一天市值避免內生性。實測四種組合(等權重/市值加權 × 門檻1/3)結果差異都在誤差範圍內(<0.5個百分點)，市值加權+門檻3方法論上更站得住腳，採用為新標準：波動度結論更新為調升+4.9%(t=5.41)、調降-8.9%(t=-9.42)。
- **2026-07-24**：修復`factset_estimates`(多年度EPS/營收估值明細，個股detail頁「今年/明年/後年」表格的資料來源)歷史缺口——2023~2025年共5,198篇EPS文章、29,534列資料，當初回補時只寫了主表`factset_revisions`，子表從未寫入。根因非爬蟲抓不到(原始JSON`backfill_2023_2025_dedup.json`裡資料本來就有)，是回補流程漏了寫子表這一步。已直接從本機JSON補寫，現在2023-2026全部EPS事件的多年度估值明細100%完整。網站個股頁面因為只抓近一年資料(`EPS_FETCH_DAYS=365`)不會顯示這麼舊的事件，此修復主要供資料庫直接查詢/研究使用。
- **2026-07-21**：`industry_name`原始文字解析改為`ticker_industry_official`官方對照表，`v_revisions_normalized.industry_canonical`為正式產業欄位。
- **2026-07-24**：`concept_map`新增「AI」(00962台新臺灣AI優息動能ETF，29檔)、「智能車供應鏈」(00901永豐台灣智能車供應鏈ETF，42檔)兩組主題分類，方法論與既有半導體/IC設計一致(官方ETF成分股權重揭露)。查過資安(00875成分股是全球公司、幾乎不含台股)、核能/鈾礦(台股無對應掛牌公司)，兩者都沒有品質相當的官方來源可用，暫不做，原因記在`fetch_concept_etf.py`。
- **2026-07-24**：`concept_map`新增`dimension='value_chain'`的全量資料源——證券櫃檯買賣中心/臺灣證券交易所官方「產業價值鏈資訊平台」(ic.tpex.org.tw)，212/216檔追蹤股票查得到官方申報的產業鏈位置，共780筆，一次補齊過去ETF方法覆蓋不到的產業(金融10檔金控、傳產塑化鋼鐵、航運航空、PCB/被動元件/連接器供應鏈等)。抓取腳本`fetch_industry_chain.py`，是目前`concept_map`裡覆蓋率最高、最接近「全部205檔都做到」的分類來源。
- **2026-07-28**：股價/大盤資料從本機快照(`factset_data/prices_full.json`等，本機檔案、不進git)搬進資料庫`stock_prices`/`taiex_index`兩張表，理由：本機檔案沒辦法自動更新，也沒辦法讓別人不靠原本那台電腦接手。新增`update_stock_prices.py`+GitHub Actions排程`daily_prices.yml`(每天台灣時間15:00自動增量更新，用repo secret的service_role key寫入，不依賴任何人的本機)，以及RPC函式`get_strategy_price_bundle()`給前端一次拉回287k列資料。`strategy.html`的股價來源已改讀這個RPC，`factset_data/strategy_prices.json`/`strategy_taiex.json`兩個靜態快照檔已停用並從git移除。
