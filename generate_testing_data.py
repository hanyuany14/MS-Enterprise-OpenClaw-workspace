import pandas as pd
import numpy as np
from datetime import datetime, timedelta

# 設定 12 個門市
stores = ['台北總店', '板橋門市', '基隆門市', '桃園門市', '新竹門市', '台中門市', 
          '彰化門市', '嘉義門市', '台南門市', '高雄門市', '屏東門市', '宜蘭門市']

# 設定 12 個完整的週 (從週一開始)
start_date = datetime(2026, 2, 22) # 2026-02-22 是週日
weeks = 12

# 設定初始值
base_orders_per_store_day = 28 # 第一週平均每店每天 28 單
base_revenue_per_order = 1200  # 平均客單價 1200 元

data = []

for w in range(weeks):
    # 【關鍵修正】：讓「訂單數量」每週穩定成長約 8%
    order_growth_factor = 1.08 ** w 
    
    for d in range(7): # 每天
        current_date = start_date + timedelta(weeks=w, days=d)
        
        # 週末人潮多 20%
        day_weight = 1.2 if current_date.weekday() >= 5 else 1.0
        
        for store in stores:
            # 算出當天當店的「目標單量」，並加入微小隨機值 (±2筆) 避免假得太明顯
            target_orders = int(base_orders_per_store_day * order_growth_factor * day_weight)
            num_orders = max(1, target_orders + np.random.randint(-2, 3))
            
            # 依據算出的單量，一筆一筆建立訂單
            for i in range(num_orders):
                order_id = f"LB-{current_date.strftime('%Y%m%d')}-{store[:2]}-{i+1:03d}"
                
                # 每筆訂單的結帳金額也帶一點隨機浮動 (90%~110%)
                revenue = base_revenue_per_order * np.random.uniform(0.9, 1.1)
                
                data.append({
                    "OrderID": order_id,
                    "OrderDate": current_date.strftime('%Y-%m-%d'),
                    "StoreName": store,
                    "Revenue": round(revenue, 2)
                })

df = pd.DataFrame(data)
df.to_csv('lobster_perfect_order_growth.csv', index=False, encoding='utf-8-sig')
print(f"✅ 成功產生 {len(df)} 筆完美成長曲線數據！")