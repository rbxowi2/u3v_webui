# 相機校正使用說明

> 適用套件版本：LensCalibrate 2.3.2 · StereoCalibrate 1.0.1 · StereoRectify 1.1.0 · StereoMatch & Depth 1.0.2

---

## 整體流程概覽

```
單目校正 (左相機)  ──┐
                     ├──► 雙目外參校正 ──► 極線矯正 ──► 視差/深度
單目校正 (右相機)  ──┘
```

| 步驟 | 套件 | 輸出檔案 |
|------|------|---------|
| 1 | LensCalibrate（左） | `captures/calibration/<cam_L>.json` |
| 2 | LensCalibrate（右） | `captures/calibration/<cam_R>.json` |
| 3 | StereoCalibrate | `captures/stereo/<cam_L>__<cam_R>.json` |
| 4 | StereoRectify | `captures/rectify/<cam_L>__<cam_R>.json` |
| 5 | StereoMatch & Depth | `captures/depth/…ply`（可選）|

下游套件依賴上游存檔。若任一階段重新校正並存檔，其後的所有步驟都必須重做。

---

## 第一部分：單目鏡頭校正（LensCalibrate）

### 1.1 校正板

使用標準棋盤格校正板。設定說明：

| 欄位 | 說明 |
|------|------|
| Board cols | 橫向**內角點**數（格數 − 1）。實際 9 列格 → 填 8 |
| Board rows | 縱向**內角點**數（格數 − 1）。實際 6 行格 → 填 5 |
| Square size | 每格邊長（單位：mm）。量測實際印出尺寸填入 |

> **注意**：cols / rows 若填錯，系統無法偵測角點，所有拍攝都會被拒絕。

### 1.2 鏡頭類型選擇

| 類型 | 說明 |
|------|------|
| Normal | 標準鏡頭（針孔模型），校正一階段完成 |
| Fisheye | 魚眼鏡頭（等距投影模型），需兩個階段 |

校正開始後無法切換。若選錯需關閉校正（Cancel）重新開始。

---

### 1.3 Normal 鏡頭校正流程

1. 在側邊欄選 **Normal**，確認 Board 設定正確。
2. 點 **Start** 開始校正工作階段。
3. 開啟自動拍攝（**Auto**）或手動點 **Capture**。
4. 將校正板在畫面中移動，涵蓋不同位置、角度、距離。
5. 累積至少 **3 張**接受的照片後可點 **Save** 存檔。RMS 越低越好，建議低於 **1.0 px**。
6. 存檔後工作階段自動結束。

---

### 1.4 Fisheye 鏡頭校正流程（兩階段）

Fisheye 分兩階段是為了讓 K 矩陣先在無失真干擾下收斂，再讓 D 自由求解。

#### 第一階段：求 K（D 凍結為零）

1. 選 **Fisheye**，確認 Board 設定。
2. 點 **Start**，系統進入第一階段（D 固定為 `[0, 0, 0, 0]`，只解 K）。
3. 開啟 Auto 或手動拍攝，板子在畫面中大範圍移動。
4. RMS 穩定後（不再明顯下降），點 **Enter Stage 2**。

> Stage 1 的目標是讓 K（焦距 fx/fy、主點 cx/cy）收斂到正確範圍，不需要完美的 RMS。

#### 第二階段：求 K + D（D 自由解）

1. 進入 Stage 2 後，HUD 會顯示 Stage 1 的 K 與 `D = [0, 0, 0, 0]` 作為起始值。
2. 重新開始拍攝，板子同樣需要涵蓋各區域，邊緣尤其重要。
3. 每張照片被接受後 K 和 D 都會即時更新。
4. RMS 穩定後點 **Save**。

> Stage 2 計算初始猜測：K₀ = Stage 1 結果，D₀ 永遠從 `[0, 0, 0, 0]` 開始。

#### 直接跳到第二階段（Skip to Stage 2）

若已有存檔校正且只想重新調整 D：

1. 點 **Skip to Stage 2**，系統以存檔的 K 作為 K₀ 直接進入 Stage 2。
2. HUD 顯示存檔的 K 與存檔的 D（僅供參考；計算 D 仍從零開始）。
3. 正常拍攝後 Save。

> 「Skip to Stage 2」適合鏡頭結構不變、只是環境溫度變化造成輕微 D 漂移的情況。若相機或鏡頭有任何異動，請從 Stage 1 重做。

---

### 1.5 K / D 數值說明

| 符號 | 含義 |
|------|------|
| K | 相機內參矩陣 3×3（fx, fy, cx, cy）|
| D | 失真係數：Normal = 5 個；Fisheye = 4 個（k1~k4）|
| RMS | 重投影誤差（單位：px）|

Fisheye 各階段 D 來源：

| 時間點 | D 顯示值（HUD）| D 計算起始值 |
|--------|--------------|------------|
| Stage 1 中 | 每次拍攝後更新（始終為零，因 D 被凍結）| N/A（凍結）|
| Enter Stage 2 | Stage 1 最終 D（= 零）| `[0, 0, 0, 0]` |
| Skip to Stage 2 | 存檔的 D（可能非零）| `[0, 0, 0, 0]` |
| Stage 2 中 | 每次拍攝後即時更新 | 上次結果 |

---

### 1.6 拍攝技巧

- 板子需填滿畫面面積的 **1/3 以上**，太遠角點偵測困難。
- 涵蓋畫面各象限（左上、右上、左下、右下、中心）。
- 不同傾斜角度（左傾、右傾、前俯、後仰）有助 D 收斂。
- 環境光線穩定、板子本身不反光。
- Auto 模式每次拍攝間隔 **1.5 秒**，若背景不穩定建議關閉 Auto 手動選擇。
- 每次照片被拒絕（RMS 未改善）不計數，不影響資料。

### 1.7 重拍與移除

- **Reset**：清空所有已拍照片，回到 Stage 1 重頭開始（存檔不受影響）。
- **Remove**：移除最後一張接受的照片，並立刻重算 RMS。
- Reset 和 Remove 都會完全清除記憶體中的拍攝資料，不保留任何快取。

### 1.8 注意事項

- 同一組 cols/rows/square\_size 貫穿整個工作階段；中途改變無效。
- 最多保留 **30 張**有效照片（超過自動丟棄最舊的）。
- 主程式不需重啟，但建議每次正式校正前先 Reset 清空上一輪資料。
- 校正過程中拍攝越多越慢的現象已修正（v2.3.1），30 張上限確保效能穩定。

---

## 第二部分：雙目相機校正（StereoCalibrate）

### 2.1 前置條件

**必須先完成兩個相機各自的單目校正並存檔**，StereoCalibrate 會讀取：

- `captures/calibration/<cam_L>.json`
- `captures/calibration/<cam_R>.json`

若單目校正檔不存在，或 Lens type 與 StereoCalibrate 設定不符，校正會失敗。

### 2.2 設定

| 欄位 | 說明 |
|------|------|
| Left camera | 選擇左相機（與日後 StereoMatch 的 Left 對應）|
| Right camera | 選擇右相機 |
| Lens type | 與單目校正相同（normal / fisheye）|
| Board cols / rows / square size | 必須與單目校正時**完全相同** |

> Board 設定不一致會導致偵測到不同的世界座標系，R / T 計算錯誤。

### 2.3 校正流程

1. 設定好 L/R 相機與 Board 參數，點 **Calibrate** 開啟校正 Modal。
2. 將校正板同時在兩個相機畫面中顯示清楚。
3. 系統偵測到兩個相機都看到棋盤格時自動拍攝（Auto Cooldown = **2 秒**）。
4. 至少需 **4 張**才開始計算，**5 張**才能 Save。
5. RMS 穩定後點 **Save**。

### 2.4 拍攝技巧

- 板子必須**同時**出現在左右兩個畫面中，且都清楚可辨。
- 移動時要慢，讓兩台相機都能偵測到同一個靜止位置。
- 涵蓋不同深度（遠近）和位置，有助外參 R / T 精確。
- 建議至少拍 **15～20 張**以上以提升穩健性。
- 若某張被拒絕（RMS 未改善），板子移到新位置重試即可。

### 2.5 注意事項

- StereoCalibrate 使用單目校正的 K、D 作為固定內參，只解 R、T（外參）。
- 同一臺電腦重啟主程式後記憶體清空，但**存檔不受影響**。
- 最多保留 **30 張**有效照片。
- 校正過程中若任一相機斷線，工作階段自動結束；需重新開始。

---

## 第三部分：極線矯正（StereoRectify）

### 3.1 前置條件

需要 StereoCalibrate 存檔：

- `captures/stereo/<cam_L>__<cam_R>.json`

### 3.2 流程

1. 在側邊欄選擇 L / R 相機組合，點 **Rectify** 開啟 Modal。
2. 系統自動讀取 StereoCalibrate 存檔，計算矯正映射並顯示即時預覽。
3. 預覽中可看見水平 **Epipolar lines**（綠色橫線），確認左右畫面的特徵點落在同一水平線上。
4. 調整 **Alpha** 值：
   - `0`：裁切到只保留有效（無黑邊）區域，縮小視野
   - `1`：保留完整畫面，邊緣可能有黑色區域
   - 建議從 `1` 開始確認矯正品質，確認無問題再依需要裁切
5. 確認 Epipolar lines 對齊後點 **Save**。

### 3.3 輸出

存檔 `captures/rectify/<cam_L>__<cam_R>.json` 包含 R1、R2、P1、P2、Q 矩陣與 ROI。StereoMatch & Depth 會讀取此檔案重算 remap maps。

### 3.4 注意事項

- StereoRectify 是**單次計算**，不需要拍攝流程。
- 若 Epipolar lines 明顯不水平或偏移很大，表示 StereoCalibrate 品質不佳，需重做雙目校正。
- StereoCalibrate 更新後，StereoRectify 必須重做並重新 Save。

---

## 第四部分：視差與深度（StereoMatch & Depth）

### 4.1 前置條件

需要以下兩份存檔同時存在：

- `captures/stereo/<cam_L>__<cam_R>.json`（StereoCalibrate）
- `captures/rectify/<cam_L>__<cam_R>.json`（StereoRectify）

### 4.2 啟用

1. 在側邊欄選擇 L / R 相機，點 **Enable** 開始運算（5 fps）。
2. 主視窗會即時疊加視差圖或深度圖到選定的相機畫面上。

### 4.3 側邊欄選項

| 選項 | 說明 |
|------|------|
| Display on | 疊加到 L 或 R 相機的主視窗 |
| View | Disp = 視差圖；Depth = 深度圖 |
| Algorithm | BM = 速度快、品質普通；SGBM = 較慢、品質佳 |
| Resolution | 1x / ½ / ¼，縮小處理解析度以節省 CPU（不存檔）|

**Resolution 說明**：縮小比例只影響內部計算，深度數值**完全正確**（K 和 Q 矩陣同步縮放，深度公式中縮放因子互相抵消）。輸出影像維持縮小後尺寸，不 upscale。

### 4.4 Setting Modal 參數

| 參數 | 說明 | 建議起始值 |
|------|------|----------|
| numDisparities | 搜尋視差範圍（16 的倍數）| 64 |
| blockSize | 匹配窗格大小（奇數）| 9 |
| P1 / P2 | SGBM 平滑懲罰（auto = 依 blockSize 自動計算）| auto |
| uniquenessRatio | 唯一性過濾（越高越嚴格）| 5 |
| speckleWindow | 斑點過濾窗口大小 | 100 |
| speckleRange | 斑點深度範圍 | 2 |
| Clip min / max | 深度有效範圍（mm）| 200 / 5000 |

調整參數後即時生效，滿意後點 **Save** 存到 `captures/match/<cam_L>__<cam_R>.json`。

### 4.5 Stats 統計欄

| 欄位 | 說明 |
|------|------|
| Disparity 平均 | 畫面平均視差值（px）|
| Disparity 有效 | 有效視差像素比例（< 50% 通常表示參數需調整）|
| Depth 有效 | 在 Clip 範圍內的深度像素比例 |
| 最近 / 中位 / 最遠 | 有效深度範圍（mm）|

### 4.6 Save PLY

點 **Save PLY** 將當前點雲存為 `captures/depth/<cam_L>__<cam_R>_<timestamp>.ply`，並自動觸發瀏覽器下載。每次點擊以當前幀的點雲為準。

### 4.7 注意事項

- 視差圖全黑：通常是 numDisparities 設太小、相機對未正確矯正、或光線不足。
- 深度全無效：檢查 Clip min/max 是否合理（單位是 mm）。
- 效能不足時優先嘗試 Resolution ½ 或 ¼，可大幅降低 CPU 負載而不影響深度精度。
- Resolution 為 session-only，每次重啟預設回 1x。

---

## 附錄 A：LensUndistort（即時矯正）

LensUndistort 套件讀取 LensCalibrate 存檔，對**即時畫面**套用去失真。

- 在側邊欄選擇相機，點 **Enable**。
- 若存檔鏡頭類型與當前設定相符，自動套用；不符則顯示錯誤。
- 此套件不需要雙目流程。

---

## 附錄 B：存檔路徑

| 套件 | 路徑 |
|------|------|
| LensCalibrate | `captures/calibration/<cam_id>.json` |
| StereoCalibrate | `captures/stereo/<cam_L>__<cam_R>.json` |
| StereoRectify | `captures/rectify/<cam_L>__<cam_R>.json` |
| StereoMatch & Depth | `captures/match/<cam_L>__<cam_R>.json` |
| PLY 點雲 | `captures/depth/<cam_L>__<cam_R>_<timestamp>.ply` |

`<cam_id>`、`<cam_L>`、`<cam_R>` 中的 `/`、`:`、空格會自動轉為 `_`。

---

## 附錄 C：常見問題

**Q：角點偵測一直失敗？**
確認 Board cols/rows 填的是**內角點數**（格數 − 1），不是格數。另外確認板子有足夠光線且無反光。

**Q：RMS 拍再多也不下降？**
試著拍不同角度（傾斜、靠近邊緣），避免所有照片都是正面平視。Normal 鏡頭 RMS < 1.0 為佳；Fisheye Stage 2 < 2.0 通常可接受。

**Q：StereoCalibrate 顯示找不到單目校正檔？**
確認左右相機各自的 LensCalibrate 已存檔，且 Lens type 選項與 StereoCalibrate 一致。

**Q：StereoMatch 視差圖有很多黑色區域？**
嘗試增加 numDisparities（需為 16 倍數），或降低 uniquenessRatio。確認 StereoRectify 已重新執行且 Epipolar lines 對齊正確。

**Q：深度數值單位是什麼？**
與校正時輸入的 Square size 單位相同。若 Square size 填 25（mm），深度單位就是 mm。

**Q：換了鏡頭或調整了焦距，需要重做哪些步驟？**
鏡頭或焦距改變後，所有步驟（LensCalibrate → StereoCalibrate → StereoRectify）都必須重做。

**Q：重啟主程式後資料會消失嗎？**
已存檔的 JSON 不受影響。只有工作階段中尚未存檔的拍攝資料會在重啟後消失。
