import pandas as pd
import re
import streamlit as st
import folium
from streamlit_folium import st_folium
from geopy.distance import geodesic
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
from datetime import datetime
import requests

# ---------------------------------------------------------
# 0. 服務水準 (LOS) 計算邏輯與顏色映射
# ---------------------------------------------------------
# 文字顏色與背景顏色的搭配 (讓配色更舒適易讀)
LOS_STYLE_CONFIG = {
    'A': {'bg': '#d4edda', 'color': '#155724'},  # 深綠字 淺綠底
    'B': '#91cf60',                              # 或直接純文字色
    'C': '#d9ef8b',
    'D': '#fee08b',
    'E': '#fc8d59',
    'F': '#d73027',
    'N/A': '#808080'
}

LOS_TEXT_COLORS = {
    'A': '#1a9850',
    'B': '#469b2a',
    'C': '#8fa810',
    'D': '#d99b00',
    'E': '#d9531e',
    'F': '#d73027',
    'N/A': '#808080'
}

def get_los_grade(ratio):
    if pd.isna(ratio):
        return "N/A"
    if ratio >= 0.80:
        return 'A'
    elif ratio >= 0.60:
        return 'B'
    elif ratio >= 0.50:
        return 'C'
    elif ratio >= 0.40:
        return 'D'
    elif ratio >= 0.20:
        return 'E'
    else:
        return 'F'

def style_los_column(val):
    """Pandas Styler: 為 LOS 欄位加上文字顏色與加粗"""
    color = LOS_TEXT_COLORS.get(str(val).strip().upper(), '#808080')
    return f'color: {color}; font-weight: bold;'

def calculate_weighted_los(df_group):
    df_sorted = df_group.sort_values('里程點').copy()
    df_sorted['區段長度'] = df_sorted['里程點'].diff().fillna(df_sorted['里程點'])
    df_sorted['區段長度'] = df_sorted['區段長度'].apply(lambda x: x if x > 0 else 1.0)
    
    total_distance = df_sorted['區段長度'].sum()
    
    if total_distance == 0:
        weighted_speed = df_sorted['速度'].mean()
        weighted_limit = df_sorted['速限'].mean()
    else:
        weighted_speed = (df_sorted['速度'] * df_sorted['區段長度']).sum() / total_distance
        weighted_limit = (df_sorted['速限'] * df_sorted['區段長度']).sum() / total_distance
        
    ratio = weighted_speed / weighted_limit if weighted_limit > 0 else 0
    los = get_los_grade(ratio)
    
    return pd.Series({
        '總里程(m)': round(total_distance, 2),
        '加權平均速度(km/h)': round(weighted_speed, 2),
        '加權平均速限(km/h)': round(weighted_limit, 1),
        'V/VL比值': round(ratio, 4),
        '全段LOS': los
    })

# ---------------------------------------------------------
# 1. 向量化高速 ETL 清洗函式
# ---------------------------------------------------------
def process_single_excel(uploaded_file, target_keyword="harmonic avg"):
    xls = pd.ExcelFile(uploaded_file, engine='openpyxl')
    target_sheets = [s for s in xls.sheet_names if target_keyword.lower() in s.lower()]
    
    if not target_sheets:
        return None

    all_dfs = []
    for sheet in target_sheets:
        match = re.search(r'Route\s*(\d+)', sheet, re.IGNORECASE)
        route_label = f"Route {match.group(1)}" if match else sheet.split('-')[0].strip()
        
        df_raw = pd.read_excel(xls, sheet_name=sheet, header=None)
        
        a1_text = str(df_raw.iloc[0, 0]) if not df_raw.empty else ""
        a1_parts = a1_text.split('-')
        direction_label = a1_parts[3].strip().upper() if len(a1_parts) >= 4 else "N/A"
        
        row_date = df_raw.iloc[3].ffill() 
        row_header = df_raw.iloc[4]
        df_data = df_raw.iloc[5:].copy()
        
        dist_col_idx = 2
        limit_col_idx = None
        for c_idx, val in enumerate(row_header):
            if pd.notna(val) and 'speed limit' in str(val).lower():
                limit_col_idx = c_idx
                break
        
        valid_cols = []
        for c_idx in range(3, df_raw.shape[1]):
            col_str = str(row_header.iloc[c_idx])
            if 'Speed(kph)' in col_str and 'Speed Limit' not in col_str:
                date_raw = row_date.iloc[c_idx]
                date_val = str(date_raw).split(' ')[0] if pd.notna(date_raw) else "未知日期"
                time_clean = col_str.replace('Speed(kph)', '').strip().split('-')[0].strip()
                valid_cols.append((c_idx, date_val, time_clean))
                
        if not valid_cols:
            continue
            
        mileage_s = pd.to_numeric(df_data.iloc[:, dist_col_idx], errors='coerce')
        limit_s = pd.to_numeric(df_data.iloc[:, limit_col_idx], errors='coerce') if limit_col_idx else 50.0
        
        sheet_records = []
        for c_idx, date_val, start_time in valid_cols:
            speed_s = pd.to_numeric(df_data.iloc[:, c_idx], errors='coerce')
            
            sub_df = pd.DataFrame({
                '路線': route_label,
                '方向': direction_label,
                '里程點': mileage_s,
                '日期': date_val,
                '原始開始時間': start_time,
                '速度': speed_s,
                '速限': limit_s.fillna(50.0)
            }).dropna(subset=['里程點', '速度'])
            
            sheet_records.append(sub_df)
            
        if sheet_records:
            df_sheet = pd.concat(sheet_records, ignore_index=True)
            df_sheet['時間'] = pd.to_datetime(df_sheet['原始開始時間'], format='%H:%M', errors='coerce').dt.strftime('%H:00')
            
            df_hourly = df_sheet.groupby(['路線', '方向', '里程點', '日期', '時間'], as_index=False).agg({
                '速度': 'mean',
                '速限': 'first'
            })
            df_hourly['速度'] = df_hourly['速度'].round(4)
            all_dfs.append(df_hourly)
            
    return pd.concat(all_dfs, ignore_index=True) if all_dfs else None

def load_multiple_tomtom_data(uploaded_files, target_keyword="harmonic avg"):
    combined_dfs = []
    for f in uploaded_files:
        df = process_single_excel(f, target_keyword)
        if df is not None:
            combined_dfs.append(df)
    if combined_dfs:
        res = pd.concat(combined_dfs, ignore_index=True)
        return res.drop_duplicates(subset=['路線', '方向', '里程點', '日期', '時間'])
    return None

# ---------------------------------------------------------
# 2. 起終點經緯度表 Clean & Merge 函式
# ---------------------------------------------------------
def merge_geo_data(df_speed, geo_files):
    geo_dfs = []
    for gfile in geo_files:
        df_geo = pd.read_excel(gfile, engine='openpyxl')
        df_geo.columns = [str(c).strip() for c in df_geo.columns]
        
        if '路線' in df_geo.columns:
            df_geo['路線'] = df_geo['路線'].astype(str).str.strip()
            df_geo['路線'] = df_geo['路線'].apply(
                lambda x: f"Route {re.search(r'\d+', x).group()}" if re.search(r'\d+', x) else x
            )
            
        if '方向' in df_geo.columns:
            df_geo['方向'] = df_geo['方向'].astype(str).str.strip().str.upper()
            
        geo_dfs.append(df_geo)
        
    if not geo_dfs:
        return df_speed

    full_geo = pd.concat(geo_dfs, ignore_index=True).drop_duplicates()

    if '方向' in full_geo.columns:
        merged_df = pd.merge(df_speed, full_geo, on=['路線', '方向'], how='left')
    else:
        st.warning("⚠️ 經緯度檔案中缺少 '方向' 欄位！")
        merged_df = pd.merge(df_speed, full_geo, on='路線', how='left')

    merged_df['V/VL比值'] = (merged_df['速度'] / merged_df['速限']).round(4)
    merged_df['單點LOS'] = merged_df['V/VL比值'].apply(get_los_grade)
    
    return merged_df

# ---------------------------------------------------------
# 3. 地圖繪製與圖層處理
# ---------------------------------------------------------
cmap = plt.colormaps.get_cmap('RdYlGn')
def get_google_color(speed, min_s, max_s):
    norm = (speed - min_s) / (max_s - min_s + 1e-9)
    norm = max(0.0, min(1.0, norm))
    return mcolors.to_hex(cmap(norm))

@st.cache_data(show_spinner=False)
def get_path_with_distances(coords):
    distances = [0.0]
    for i in range(len(coords) - 1):
        d = geodesic((coords[i][1], coords[i][0]), (coords[i+1][1], coords[i+1][0])).meters
        distances.append(distances[-1] + d)
    return coords, distances

@st.cache_data(show_spinner=False)
def fetch_route_coords(start_lng, start_lat, end_lng, end_lat):
    url = f"http://router.project-osrm.org/route/v1/driving/{start_lng},{start_lat};{end_lng},{end_lat}?overview=full&geometries=geojson"
    try:
        res = requests.get(url, timeout=5).json()
        return res['routes'][0]['geometry']['coordinates']
    except Exception:
        return [[start_lng, start_lat], [end_lng, end_lat]]

# ---------------------------------------------------------
# 4. 地圖繪製 Fragment 區塊
# ---------------------------------------------------------
@st.fragment
def render_map_area(df):
    st.markdown("---")
    st.markdown("### 🗺️ 路線車速畫地圖上")
    
    col1, col2, col3, col4 = st.columns(4)
    
    with col1:
        all_dates = sorted([str(d) for d in df['日期'].dropna().unique().tolist()])
        selected_date = st.selectbox("選擇日期", all_dates, key="map_date")
        
    with col2:
        all_times = sorted(df['時間'].astype(str).unique().tolist())
        selected_time = st.selectbox("選擇時間", all_times, key="map_time")
        
    with col3:
        all_routes = sorted(df['路線'].unique().tolist())
        selected_routes = st.multiselect("選擇路線", all_routes, default=all_routes, key="map_routes")
    with col4:
        all_dirs = sorted([str(d) for d in df['方向'].dropna().unique().tolist()])
        selected_dirs = st.multiselect("選擇方向", all_dirs, default=all_dirs, key="map_dirs")

    filtered_df = df[
        (df['日期'].astype(str) == selected_date) & 
        (df['時間'].astype(str) == selected_time) & 
        (df['路線'].isin(selected_routes)) &
        (df['方向'].isin(selected_dirs))
    ].sort_values(['路線', '方向', '里程點'])
    
    if not filtered_df.empty:
        min_speed, max_speed = filtered_df['速度'].min(), filtered_df['速度'].max()
        
        first_row = filtered_df.dropna(subset=['起點緯度', '起點經度']).iloc[0] if not filtered_df.dropna(subset=['起點緯度', '起點經度']).empty else None
        init_center = [first_row['起點緯度'], first_row['起點經度']] if first_row is not None else [25.1336, 121.4593]
        
        m = folium.Map(location=init_center, zoom_start=13)
        folium.TileLayer("cartodbpositron", name="極簡模式").add_to(m)
        folium.TileLayer(
            tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
            attr="Esri",
            name="衛星影像模式"
        ).add_to(m)
        folium.LayerControl(position='topright').add_to(m)

        grouped = filtered_df.groupby(['路線', '方向'])
        
        for (route_name, dir_name), sub_df in grouped:
            base = sub_df.iloc[0]
            
            if pd.isna(base.get('起點經度')) or pd.isna(base.get('起點緯度')):
                continue
                
            coords = fetch_route_coords(base['起點經度'], base['起點緯度'], base['終點經度'], base['終點緯度'])
            coords, dists = get_path_with_distances(coords)
            prev_m = 0
            last_coord = None
            
            for _, row in sub_df.iterrows():
                target_m = row['里程點']
                sub_coords = [coords[i] for i, d in enumerate(dists) if prev_m <= d <= target_m]
                
                if last_coord and sub_coords: 
                    sub_coords.insert(0, last_coord)
                    
                if sub_coords:
                    line_color = get_google_color(row['速度'], min_speed, max_speed)
                    los_val = str(row.get('單點LOS', 'N/A'))
                    los_color = LOS_TEXT_COLORS.get(los_val, '#808080')
                    
                    popup_text = f"""
                    <b>路線:</b> {route_name} ({dir_name})<br>
                    <b>里程:</b> {row['里程點']} m<br>
                    <b>平均速率:</b> {row['速度']} km/h<br>
                    <b>該段速限:</b> {row.get('速限', 'N/A')} km/h<br>
                    <b>V̄/V<sub>L</sub> 比值:</b> {row.get('V/VL比值', 'N/A')}<br>
                    <b>服務水準 (LOS):</b> <b style="color:{los_color}; font-size:1.2em;">{los_val}</b>
                    """
                    
                    folium.PolyLine(
                        locations=[(lat, lon) for lon, lat in sub_coords],
                        color=line_color,
                        weight=6,
                        opacity=0.9,
                        tooltip=popup_text
                    ).add_to(m)
                    
                    last_coord = sub_coords[-1]
                prev_m = target_m

        try:
            m.fit_bounds(m.get_bounds())
        except Exception:
            pass
            
        st_folium(m, width=1000, height=600, use_container_width=True)
    else:
        st.warning("⚠️ 所選條件下查無資料！")

# ---------------------------------------------------------
# 5. Streamlit 前端介面主程式
# ---------------------------------------------------------
st.set_page_config(page_title="路段速度整理", layout="wide")
st.title("🚦 路段速度GVP分析")

col1, col2 = st.columns(2)
with col1:
    uploaded_speed_files = st.file_uploader("1. 請上傳 GVP速度表 (可一次選擇多個 Excel)", type=["xlsx"], accept_multiple_files=True, key="speed")
with col2:
    uploaded_geo_files = st.file_uploader("2. 請上傳起終點經緯度對照檔 (可多檔)", type=["xlsx"], accept_multiple_files=True, key="geo")

if uploaded_speed_files:
    with st.spinner("正在高速合併與清洗報表資料中..."):
        df_speed = load_multiple_tomtom_data(uploaded_speed_files, target_keyword="harmonic avg")
    
    if df_speed is not None:
        if uploaded_geo_files:
            df_final = merge_geo_data(df_speed, uploaded_geo_files)
            st.success(f"成功合併並載入 {len(uploaded_speed_files)} 個速度檔案！")
        else:
            df_final = df_speed
            df_final['V/VL比值'] = (df_final['速度'] / df_final['速限']).round(4)
            df_final['單點LOS'] = df_final['V/VL比值'].apply(get_los_grade)
            st.info("ℹ️ 已載入速度與速限報表。(尚未上傳經緯度對照檔)")

        # 側邊欄篩選
        st.sidebar.header("🔍 資料篩選選單")
        all_dates = sorted([str(d) for d in df_final['日期'].dropna().unique().tolist()])
        selected_date = st.sidebar.selectbox("請選擇日期：", ["全部日期"] + all_dates)
        
        all_routes = sorted(df_final['路線'].dropna().unique().tolist())
        selected_route = st.sidebar.selectbox("請選擇路線：", ["全部路線"] + all_routes)
        
        display_df = df_final.copy()
        if selected_date != "全部日期":
            display_df = display_df[display_df['日期'] == selected_date]
        if selected_route != "全部路線":
            display_df = display_df[display_df['路線'] == selected_route]
            
        # 呈現資料表（恢復為 st.dataframe，具備捲軸與可滑動分頁）
        st.subheader(f"📋 整理後之標準資料庫預覽 (共 {len(display_df)} 筆)")
        base_cols = ['路線', '方向', '里程點', '日期', '時間', '速度', '速限', 'V/VL比值', '單點LOS']
        geo_cols = [c for c in display_df.columns if c not in base_cols]
        display_df = display_df[base_cols + geo_cols]
        
        # 透過 Styler 上色，高度固定為 400px (畫面會非常乾淨)
        styled_df = display_df.style.map(style_los_column, subset=['單點LOS'])
        st.dataframe(styled_df, use_container_width=True, height=400)

        # ---------------------------------------------------------
        # 全路段距離加權 LOS 報告
        # ---------------------------------------------------------
        st.subheader("📊 路線全段「距離加權」服務水準 ")
        
        los_summary = display_df.groupby(['路線', '方向', '日期', '時間']).apply(calculate_weighted_los).reset_index()
        styled_summary = los_summary.style.map(style_los_column, subset=['全段LOS'])
        st.dataframe(styled_summary, use_container_width=True, height=300)
        
        # 呼叫地圖渲染函式
        render_map_area(df_final)





        # ---------------------------------------------------------
        # 新修正：多日期/多時間篩選與 Excel 匯出模組 (安全數字排序版)
        # ---------------------------------------------------------
        st.markdown("---")
        st.subheader("📥 多時段加權平均速度與 LOS 報表匯出")
        
        with st.expander("⚙️ 點此展開多時段篩選設定", expanded=True):
            col_ex1, col_ex2 = st.columns(2)
            
            with col_ex1:
                def natural_sort_key(s):
                    return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', str(s))]
                
                all_dates_list = sorted(df_final['日期'].dropna().unique().tolist(), key=natural_sort_key)
                selected_export_dates = st.multiselect(
                    "選擇要匯出的日期 (可多選)", 
                    options=all_dates_list, 
                    default=all_dates_list[:1] if all_dates_list else [],
                    key="export_dates"
                )
                
            with col_ex2:
                all_times_list = sorted(
                    df_final['時間'].astype(str).unique().tolist(), 
                    key=lambda x: datetime.strptime(x, '%H:%M') if ':' in x else x
                )
                selected_export_times = st.multiselect(
                    "選擇要匯出的時間 (可多選，例如 09:00, 10:00, 11:00...)", 
                    options=all_times_list,
                    default=all_times_list[:3] if len(all_times_list) >= 3 else all_times_list,
                    key="export_times"
                )

        if selected_export_dates and selected_export_times:
            export_filtered_df = df_final[
                df_final['日期'].astype(str).isin(selected_export_dates) & 
                df_final['時間'].astype(str).isin(selected_export_times)
            ]
            
            if not export_filtered_df.empty:
                export_summary = export_filtered_df.groupby(
                    ['路線', '方向', '日期', '時間']
                ).apply(calculate_weighted_los).reset_index()
                
                # 💡 安全排序法：抓出路線名稱中的數字轉成整數排（例如 Route 2 -> 2, Route 10 -> 10）
                export_summary['route_num'] = export_summary['路線'].astype(str).str.extract(r'(\d+)').astype(float).fillna(0)
                export_summary = export_summary.sort_values(
                    ['route_num', '路線', '方向', '日期', '時間']
                ).drop(columns=['route_num']).reset_index(drop=True)
                
                styled_export_summary = export_summary.style.map(style_los_column, subset=['全段LOS'])
                
                st.write(f"📊 預計匯出報表預覽 (共 {len(export_summary)} 筆資料)：")
                st.dataframe(styled_export_summary, use_container_width=True, height=400)
                
                from io import BytesIO
                output = BytesIO()
                with pd.ExcelWriter(output, engine='openpyxl') as writer:
                    export_summary.to_excel(writer, index=False, sheet_name='多時段加權速度與LOS報表')
                excel_data = output.getvalue()
                
                st.download_button(
                    label="📥 下載所選時段加權平均速度 Excel 報表",
                    data=excel_data,
                    file_name=f"多時段路段加權速度與LOS報表_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                )
            else:
                st.warning("⚠️ 在您選擇的日期與時間交叉組合中查無對應資料，請重新調整篩選條件。")
        else:
            st.info("💡 請至少選擇一個「日期」與一個「時間」以產生匯出報表。")
