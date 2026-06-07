import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import anthropic
import re
import datetime

st.set_page_config(page_title="Ozon Анализатор метрик", page_icon="📊", layout="wide")
st.title("📊 Ozon — Анализатор метрик артикула")
st.caption("Загрузи экспорт метрик из Ozon и получи анализ: что идёт в минус и что делать")

# ── Константы ──────────────────────────────────────────────────────────────

METRICS_WHERE_HIGHER_IS_WORSE = [
    "cpo", "стоимость привлечения", "расход", "дрр",
    "cpm", "стоимость 1000", "оборот товар", "хватит на",
]
METRICS_WHERE_HIGHER_IS_WORSE_RANK = ["позиция"]

PRICE_METRICS = [
    "цена продавца до скидки мп",
    "цена покупателя после скидки мп",
    "% скидки мп для покупателя",
]

FUNNEL_METRICS = {
    "Показы":                          ["показы"],
    "Клики":                           ["клики"],
    "Корзины":                         ["корзин"],
    "Заказы":                          ["заказы"],
}
CONVERSION_METRICS = [
    "ctr",
    "конверсия в корзину из клика",
    "конверсия в заказ из корзины",
]

TRAFFIC_METRICS = {
    "Клики органические": ["клики органические"],
    "Клики рекламные":    ["клики рекламные"],
}

# ── Утилиты ────────────────────────────────────────────────────────────────

def clean_value(val):
    if pd.isna(val):
        return None
    s = str(val).strip()
    if s in ("—", "-", "", "nan"):
        return None
    s = re.sub(r"[%\s\xa0 ]", "", s)
    s = s.replace(",", ".")
    s = re.sub(r"[^\d.\-]", "", s)
    try:
        return float(s)
    except ValueError:
        return None


def parse_excel(file) -> pd.DataFrame:
    df = pd.read_excel(file, header=0)
    new_cols = []
    for c in df.columns:
        # Datetime / Timestamp → форматируем как дд.мм
        if isinstance(c, (datetime.datetime, pd.Timestamp)):
            new_cols.append(c.strftime("%d.%m"))
        # Excel serial number (целое/float) → конвертируем в дату
        elif isinstance(c, (int, float)) and not isinstance(c, bool) and 40000 < c < 60000:
            try:
                dt = pd.Timestamp("1899-12-30") + pd.Timedelta(days=int(c))
                new_cols.append(dt.strftime("%d.%m"))
            except Exception:
                new_cols.append(str(c).strip())
        else:
            new_cols.append(str(c).strip())
    df.columns = new_cols
    return df


def get_row(df: pd.DataFrame, keywords: list[str]) -> pd.Series | None:
    """Находит первую строку, в названии которой есть все ключевые слова."""
    for _, row in df.iterrows():
        name = str(row.iloc[0]).strip().lower()
        if all(kw.lower() in name for kw in keywords):
            return row
    return None


def day_values(row: pd.Series, date_cols) -> list[tuple]:
    """Возвращает [(date, value), ...] newest-first, только не-None."""
    result = []
    for dc in date_cols:
        v = clean_value(row[dc])
        if v is not None:
            result.append((dc, v))
    return result


def chronological(dv: list[tuple]) -> list[tuple]:
    return list(reversed(dv))


# ── Анализ трендов ─────────────────────────────────────────────────────────

def find_breakpoint(dv: list[tuple], find_recent_trough: bool):
    """
    dv: newest-first.
    find_recent_trough=True  → ищем ближайший локальный минимум (откуда начался текущий рост)
    find_recent_trough=False → ищем ближайший локальный максимум (откуда началось текущее падение)
    Возвращает (дата_точки, кол_во_дней_с_тех_пор) или (None, None)
    """
    if len(dv) < 4:
        return None, None
    chron_dates = [d for d, _ in reversed(dv)]
    chron_vals  = [v for _, v in reversed(dv)]
    n = len(chron_vals)

    # 2-дневное сглаживание
    smoothed = [
        (chron_vals[i] + chron_vals[i + 1]) / 2 if i + 1 < n else chron_vals[i]
        for i in range(n)
    ]

    best_idx = None
    # Ищем ПРАВЕЕ (ближе к сегодняшнему дню) — от конца к началу
    for i in range(n - 2, 0, -1):
        if find_recent_trough:
            if smoothed[i] <= smoothed[i - 1] and smoothed[i] <= smoothed[i + 1]:
                best_idx = i
                break
        else:
            if smoothed[i] >= smoothed[i - 1] and smoothed[i] >= smoothed[i + 1]:
                best_idx = i
                break

    # Фолбэк: глобальный min/max если нет локального
    if best_idx is None:
        best_idx = smoothed.index(min(smoothed) if find_recent_trough else max(smoothed))

    days_since = n - best_idx      # кол-во дней включая точку разворота
    if days_since <= 0:
        return None, None
    return chron_dates[best_idx], days_since


def compute_trends(df: pd.DataFrame):
    col_metric = df.columns[0]
    date_cols  = df.columns[2:]
    results = []
    for _, row in df.iterrows():
        metric_name = str(row[col_metric]).strip()
        dv = day_values(row, date_cols)
        if len(dv) < 2:
            continue
        recent_avg = sum(v for _, v in dv[:3]) / min(3, len(dv))
        older_avg  = sum(v for _, v in dv[3:10]) / max(len(dv[3:10]), 1)
        change_pct = (recent_avg - older_avg) / abs(older_avg) * 100 if older_avg else 0.0

        name_lower     = metric_name.lower()
        is_higher_worse = any(kw in name_lower for kw in METRICS_WHERE_HIGHER_IS_WORSE)
        is_rank        = any(kw in name_lower for kw in METRICS_WHERE_HIGHER_IS_WORSE_RANK)

        if is_higher_worse or is_rank:
            negative = change_pct > 5
            positive = change_pct < -5
        else:
            negative = change_pct < -5
            positive = change_pct > 5

        bp_date, days_dec = find_breakpoint(dv, is_higher_worse or is_rank)
        results.append({
            "metric": metric_name,
            "recent_avg": round(recent_avg, 2),
            "older_avg":  round(older_avg, 2),
            "change_pct": round(change_pct, 1),
            "negative": negative,
            "positive": positive,
            "breakpoint_date": bp_date,
            "days_declining":  days_dec,
        })
    return results


# ── Блок цен ───────────────────────────────────────────────────────────────

def build_price_table(row: pd.Series, date_cols, is_pct: bool) -> pd.DataFrame:
    chron = chronological(day_values(row, date_cols))
    rows = []
    for i, (dt, val) in enumerate(chron):
        if i == 0:
            delta_pct = 0.0
            delta_rub = 0.0
            delta_str = "—"
        else:
            prev_v = chron[i - 1][1]
            delta_rub = val - prev_v
            delta_pct = (delta_rub / abs(prev_v) * 100) if prev_v else 0.0
            delta_str = f"{delta_pct:+.1f}%"
        rows.append({
            "Дата":       dt,
            "Значение":   f"{val:.0f}" + ("%" if is_pct else " ₽"),
            "Изм. %":     delta_str,
            "_pct":       delta_pct,
            "_rub":       abs(delta_rub),
        })
    return pd.DataFrame(rows)


def style_price_table(df_raw: pd.DataFrame, threshold_rub: float = 150.0):
    display = df_raw[["Дата", "Значение", "Изм. %"]].copy()

    def row_bg(row):
        idx = row.name
        pct  = df_raw.at[idx, "_pct"]
        rub  = df_raw.at[idx, "_rub"]
        if rub < threshold_rub:
            return [""] * 3
        color = "rgba(180,40,40,0.25)" if pct > 0 else "rgba(40,160,40,0.18)"
        return [f"background-color: {color}"] * 3

    def pct_color(val):
        if val == "—":
            return ""
        try:
            v = float(val.replace("%", "").replace("+", ""))
            return "color: #e05050; font-weight:600" if v > 0 else "color: #50c878; font-weight:600"
        except Exception:
            return ""

    styled = display.style.apply(row_bg, axis=1).map(pct_color, subset=["Изм. %"])
    return styled


def find_price_changes(df: pd.DataFrame, date_cols, threshold_pct: float = 5.0) -> list[dict]:
    events = []
    for kw in PRICE_METRICS:
        row = get_row(df, [kw])
        if row is None:
            continue
        is_pct = "%" in str(row.iloc[0])
        chron = chronological(day_values(row, date_cols))
        for i in range(1, len(chron)):
            prev_d, prev_v = chron[i - 1]
            curr_d, curr_v = chron[i]
            if not prev_v:
                continue
            chg = (curr_v - prev_v) / abs(prev_v) * 100
            if abs(chg) >= threshold_pct:
                events.append({
                    "date":       curr_d,
                    "metric":     str(row.iloc[0]).strip(),
                    "old_val":    round(prev_v, 2),
                    "new_val":    round(curr_v, 2),
                    "change_pct": round(chg, 1),
                    "is_pct":     is_pct,
                })
    return events


# ── Конверсии: отдельные графики и таблица ────────────────────────────────

def build_conversion_charts(df: pd.DataFrame, date_cols) -> list[tuple]:
    """Возвращает [(metric_name, fig), ...] — отдельный график на каждую метрику."""
    charts = []
    for kw in CONVERSION_METRICS:
        row = get_row(df, [kw])
        if row is None:
            continue
        name  = str(row.iloc[0]).strip()
        chron = chronological(day_values(row, date_cols))
        if not chron:
            continue
        dates_c = [d for d, _ in chron]
        vals_c  = [v for _, v in chron]

        # Цвет линии: зелёный если последние 2 дня лучше предыдущих
        recent = sum(vals_c[:3]) / min(3, len(vals_c))
        older  = sum(vals_c[3:10]) / max(len(vals_c[3:10]), 1) if len(vals_c) > 3 else recent
        color  = "#50c878" if recent >= older else "#e05050"

        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=dates_c, y=vals_c,
            mode="lines+markers",
            name=name,
            line=dict(color=color, width=2),
            marker=dict(size=5),
            connectgaps=True,
        ))
        fig.update_layout(
            title=dict(text=name, font=dict(size=13, color="#e0e0e0")),
            height=200,
            margin=dict(l=10, r=10, t=35, b=10),
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            font=dict(color="#e0e0e0"),
            xaxis=dict(gridcolor="rgba(255,255,255,0.07)", showticklabels=True, zeroline=False, type="category"),
            yaxis=dict(gridcolor="rgba(255,255,255,0.07)", zeroline=False),
            showlegend=False,
        )
        charts.append((name, fig))
    return charts


def build_conversion_table(df: pd.DataFrame, date_cols):
    """Таблица конверсий по дням с подсветкой: зелёный = лучше, красный = хуже вчера."""
    rows_data = {}
    for kw in CONVERSION_METRICS:
        row = get_row(df, [kw])
        if row is None:
            continue
        name  = str(row.iloc[0]).strip()
        chron = chronological(day_values(row, date_cols))
        rows_data[name] = {dt: val for dt, val in chron}

    if not rows_data:
        return None

    all_dates = []
    for vals in rows_data.values():
        for d in vals:
            if d not in all_dates:
                all_dates.append(d)

    result = []
    for dt in all_dates:
        r = {"Дата": dt}
        for name, vals in rows_data.items():
            v = vals.get(dt)
            r[name] = round(v, 1) if v is not None else None
        result.append(r)
    table_df = pd.DataFrame(result)

    metric_cols = [c for c in table_df.columns if c != "Дата"]

    def color_col(col_series):
        styles = [""] * len(col_series)
        for i in range(1, len(col_series)):
            curr = col_series.iloc[i]
            prev = col_series.iloc[i - 1]
            if curr is None or prev is None:
                continue
            try:
                if float(curr) > float(prev):
                    styles[i] = "color: #50c878; font-weight: 600"
                elif float(curr) < float(prev):
                    styles[i] = "color: #e05050; font-weight: 600"
            except Exception:
                pass
        return styles

    styled = table_df.style
    for col in metric_cols:
        styled = styled.apply(color_col, subset=[col])
    return styled


# ── Трафик ─────────────────────────────────────────────────────────────────

def build_traffic_section(df: pd.DataFrame, date_cols):
    series = {}
    for label, kws in TRAFFIC_METRICS.items():
        row = get_row(df, kws)
        if row is None:
            continue
        chron = chronological(day_values(row, date_cols))
        series[label] = chron

    if not series:
        return None, None, None

    # Общая таблица по дням
    all_dates = []
    for chron in series.values():
        for d, _ in chron:
            if d not in all_dates:
                all_dates.append(d)

    rows = []
    for dt in all_dates:
        r = {"Дата": dt}
        for label, chron in series.items():
            val_map = {d: v for d, v in chron}
            r[label] = val_map.get(dt)
        rows.append(r)
    table_df = pd.DataFrame(rows)

    # Сравнение трендов
    summary = {}
    for label, chron in series.items():
        dv = list(reversed(chron))  # newest first
        if len(dv) < 4:
            continue
        recent = sum(v for _, v in dv[:3]) / min(3, len(dv))
        older  = sum(v for _, v in dv[3:10]) / max(len(dv[3:10]), 1)
        chg    = (recent - older) / abs(older) * 100 if older else 0.0
        currently_rising = chg > 0
        bp, days = find_breakpoint(dv, currently_rising)
        summary[label] = {"recent": round(recent), "older": round(older),
                          "change_pct": round(chg, 1), "bp": bp, "days": days,
                          "rising": currently_rising}

    # Plotly: клики + позиция в поиске на второй оси
    fig = go.Figure()
    colors = {"Клики органические": "#50c878", "Клики рекламные": "#e05050"}
    for label, chron in series.items():
        dates_c = [d for d, _ in chron]
        vals_c  = [v for _, v in chron]
        fig.add_trace(go.Scatter(
            x=dates_c, y=vals_c,
            mode="lines+markers",
            name=label,
            line=dict(color=colors.get(label, "#aaa"), width=2),
            yaxis="y1",
        ))

    # Суммарные клики (органика + реклама) — синяя линия
    if len(series) == 2:
        vals_by_date = {}
        for chron in series.values():
            for d, v in chron:
                vals_by_date[d] = vals_by_date.get(d, 0) + v
        sorted_dates = [d for d, _ in next(iter(series.values()))]  # порядок из файла
        total_vals = [vals_by_date.get(d) for d in sorted_dates]
        fig.add_trace(go.Scatter(
            x=sorted_dates, y=total_vals,
            mode="lines+markers",
            name="Клики всего",
            line=dict(color="#4a9eff", width=2, dash="dash"),
            marker=dict(size=4),
            yaxis="y1",
        ))

    # Позиция в поиске — правая ось (чем меньше = лучше, инвертируем)
    pos_row = get_row(df, ["позиция в поиске"])
    if pos_row is not None:
        pos_chron = chronological(day_values(pos_row, date_cols))
        if pos_chron:
            fig.add_trace(go.Scatter(
                x=[d for d, _ in pos_chron],
                y=[v for _, v in pos_chron],
                mode="lines+markers",
                name="Позиция в поиске",
                line=dict(color="#f5a623", width=2, dash="dot"),
                marker=dict(size=4),
                yaxis="y2",
            ))

    fig.update_layout(
        height=280,
        margin=dict(l=10, r=60, t=20, b=10),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#e0e0e0"),
        xaxis=dict(gridcolor="rgba(255,255,255,0.07)", type="category"),
        yaxis=dict(
            title="Клики",
            gridcolor="rgba(255,255,255,0.07)",
            zeroline=False,
        ),
        yaxis2=dict(
            title=dict(text="Позиция", font=dict(color="#f5a623")),
            overlaying="y",
            side="right",
            autorange="reversed",   # меньше = лучше = сверху
            showgrid=False,
            tickfont=dict(color="#f5a623"),
        ),
        legend=dict(orientation="h", y=1.12),
    )
    return table_df, summary, fig


# ── Промпт ─────────────────────────────────────────────────────────────────

def build_prompt(article, trends, date_cols, price_changes=None):
    lines = [
        f"Ты аналитик маркетплейса Ozon. Проанализируй метрики артикула '{article}'. Сравниваем последние 3 дня со средним за 7 дней до этого.",
        "",
        "Данные (среднее последних 3 дней vs среднее 7 дней до этого):",
        "",
    ]
    for t in trends:
        if t["negative"]:
            flag = " ⚠️ УХУДШИЛАСЬ"
            if t.get("breakpoint_date"):
                flag += f" (с {t['breakpoint_date']}, {t['days_declining']} дн.)"
        elif t["positive"]:
            flag = " ✅ УЛУЧШИЛАСЬ"
        else:
            flag = ""
        lines.append(f"- {t['metric']}: {t['recent_avg']} (было {t['older_avg']}, {t['change_pct']:+.1f}%){flag}")

    significant = [e for e in (price_changes or []) if abs(e["change_pct"]) >= 5]
    if significant:
        lines += ["", "ЗНАЧИМЫЕ ИЗМЕНЕНИЯ ЦЕНЫ (≥5%):"]
        for e in significant:
            unit = "%" if e["is_pct"] else " ₽"
            lines.append(f"  • {e['date']}: {e['metric']} → {e['old_val']}{unit} → {e['new_val']}{unit} ({e['change_pct']:+.1f}%)")

    lines += [
        "",
        "Задачи:",
        "1. Выдели 3-5 метрик ⚠️ УХУДШИЛАСЬ, которые вызывают наибольшее беспокойство, объясни почему.",
        "2. Для каждой — конкретная рекомендация: что проверить/изменить на карточке или в рекламе.",
        "3. ДАТЫ СЛОМА: если несколько метрик начали падать в один день — это сигнал события. Укажи дату и выдвини гипотезу.",
        "4. ЦЕНА: если даты изменений цены совпадают с датами слома других метрик — объясни механизм влияния.",
        "5. Взаимосвязи метрик (CTR упал → клики упали → заказы упали).",
        "6. Кратко про ✅ УЛУЧШИЛАСЬ — не маскирует ли что-то плохое.",
        "7. Итог: структурная проблема или временное колебание.",
        "",
        "ФОРМАТ ОТВЕТА — строго соблюдай:",
        "- Используй markdown-заголовки ## для разделов",
        "- Каждый раздел начинай с подходящего эмодзи в заголовке",
        "- Ключевые выводы и числа выделяй **жирным**",
        "- Используй маркированные списки (- пункт) внутри разделов",
        "- Критические проблемы помечай 🔴, умеренные 🟡, позитивное 🟢",
        "- Гипотезы о причинах помечай 💡",
        "- Конкретные действия помечай ✅",
        "- Между разделами оставляй пустую строку",
        "Отвечай на русском. Будь конкретным и лаконичным.",
    ]
    return "\n".join(lines)


def call_claude(prompt, api_key):
    client = anthropic.Anthropic(api_key=api_key)
    msg = client.messages.create(
        model="claude-opus-4-8",
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text


# ══════════════════════════════════════════════════════════════════════════
# UI
# ══════════════════════════════════════════════════════════════════════════

with st.sidebar:
    st.header("⚙️ Настройки")
    # Читаем ключ из секретов Streamlit Cloud (если задеплоено),
    # иначе показываем поле ввода
    _secret_key = st.secrets.get("ANTHROPIC_API_KEY", "") if hasattr(st, "secrets") else ""
    if _secret_key:
        api_key = _secret_key
        st.success("🔑 API-ключ подключён", icon="✅")
    else:
        api_key = st.text_input("API-ключ Claude", type="password", placeholder="sk-ant-...",
                                 help="console.anthropic.com → API Keys")
    st.divider()
    article = st.text_input("Артикул", placeholder="например: 123456789")
    if not _secret_key:
        st.divider()
        st.markdown("**Получить API-ключ:**")
        st.markdown("1. [console.anthropic.com](https://console.anthropic.com)")
        st.markdown("2. Зарегистрируйся → API Keys → Create Key")

uploaded_file = st.file_uploader("Загрузи экспорт метрик из Ozon (.xlsx)", type=["xlsx"])

if not uploaded_file:
    st.stop()

df = parse_excel(uploaded_file)
date_cols = df.columns[2:]

with st.expander("📋 Загруженные данные", expanded=False):
    st.dataframe(df, use_container_width=True)

trends       = compute_trends(df)
neg_count    = sum(1 for t in trends if t["negative"])
pos_count    = sum(1 for t in trends if t["positive"])

c1, c2, c3, c4 = st.columns(4)
c1.metric("Метрик всего", len(trends))
c2.metric("Ухудшились",   neg_count, delta=f"-{neg_count}" if neg_count else "0", delta_color="inverse")
c3.metric("Улучшились",   pos_count, delta=f"+{pos_count}" if pos_count else "0", delta_color="normal")
c4.metric("Дней в данных", len(date_cols))

# ── Таблицы ухудшились / улучшились ──────────────────────────────────────
col_neg, col_pos = st.columns(2)

with col_neg:
    st.subheader("🔴 Ухудшились")
    if neg_count:
        neg_data = sorted([t for t in trends if t["negative"]], key=lambda x: x["change_pct"])
        st.dataframe(pd.DataFrame([{
            "Метрика":          t["metric"],
            "Последние 3 дня":  t["recent_avg"],
            "7 дней до (ср.)":  t["older_avg"],
            "Изменение":        f"{t['change_pct']:+.1f}%",
            "Падает с":         t["breakpoint_date"] or "—",
            "Дней":             t["days_declining"] or "—",
        } for t in neg_data]), use_container_width=True, hide_index=True)
    else:
        st.success("Нет метрик с негативным трендом")

with col_pos:
    st.subheader("🟢 Улучшились")
    if pos_count:
        pos_data = sorted([t for t in trends if t["positive"]], key=lambda x: x["change_pct"], reverse=True)[:5]
        st.dataframe(pd.DataFrame([{
            "Метрика":         t["metric"],
            "Последние 3 дня": t["recent_avg"],
            "7 дней до (ср.)": t["older_avg"],
            "Изменение":       f"{t['change_pct']:+.1f}%",
        } for t in pos_data]), use_container_width=True, hide_index=True)
    else:
        st.info("Нет метрик с позитивным трендом")

# ── Блок: Трафик ──────────────────────────────────────────────────────────
st.divider()
st.subheader("🚦 Трафик — органика vs реклама")

traffic_table, traffic_summary, traffic_fig = build_traffic_section(df, date_cols)

if traffic_fig:
    st.plotly_chart(traffic_fig, use_container_width=True)

if traffic_summary:
    t_cols = st.columns(len(traffic_summary))
    for i, (label, s) in enumerate(traffic_summary.items()):
        with t_cols[i]:
            st.metric(
                label,
                f"{s['recent']:,}",
                delta=f"{s['change_pct']:+.1f}%",
                delta_color="normal",
            )
            if s["bp"]:
                direction = "Растёт" if s.get("rising") else "Падает"
                st.caption(f"{direction} с {s['bp']} ({s['days']} дн.)")
    if len(traffic_summary) == 2:
        items = list(traffic_summary.items())
        worse  = items[0][0] if items[0][1]["change_pct"] < items[1][1]["change_pct"] else items[1][0]
        better = items[1][0] if worse == items[0][0] else items[0][0]
        st.info(f"**{worse}** просел сильнее ({traffic_summary[worse]['change_pct']:+.1f}%) vs **{better}** ({traffic_summary[better]['change_pct']:+.1f}%)")

if traffic_table is not None:
    with st.expander("📋 Детализация по дням", expanded=False):
        st.dataframe(traffic_table, use_container_width=True, hide_index=True)

if traffic_fig is None:
    st.info("Метрики органических/рекламных кликов не найдены в файле")

# ── helper: строим summary-метрики под блоком ─────────────────────────────
def render_summary(items):
    """items = [(label, dv_newest_first, is_higher_worse), ...]"""
    cols = st.columns(len(items))
    for i, (label, dv, is_hw) in enumerate(items):
        if not dv:
            continue
        recent = sum(v for _, v in dv[:3]) / min(3, len(dv))
        older  = sum(v for _, v in dv[3:10]) / max(len(dv[3:10]), 1)
        chg    = (recent - older) / abs(older) * 100 if older else 0.0
        # Текущее направление по факту (не оценочно)
        currently_rising = chg > 0
        # растёт → ищем ближайший минимум (откуда начался рост) → find_recent_trough=True
        # падает → ищем ближайший максимум (откуда началось падение) → find_recent_trough=False
        bp, days = find_breakpoint(dv, currently_rising)
        direction = "Растёт" if currently_rising else "Падает"
        with cols[i]:
            st.metric(label, f"{round(recent, 1):,}",
                      delta=f"{chg:+.1f}%",
                      delta_color="inverse" if is_hw else "normal")
            if bp:
                st.caption(f"{direction} с {bp} ({days} дн.)")


# ── Блок: Конверсии ───────────────────────────────────────────────────────
st.divider()
st.subheader("📉 Конверсии")

conv_charts = build_conversion_charts(df, date_cols)
conv_table  = build_conversion_table(df, date_cols)

if conv_charts:
    chart_cols = st.columns(len(conv_charts))
    for i, (name, fig) in enumerate(conv_charts):
        with chart_cols[i]:
            st.plotly_chart(fig, use_container_width=True)

    # Summary под графиками
    conv_summary_items = []
    for kw in CONVERSION_METRICS:
        row = get_row(df, [kw])
        if row is None:
            continue
        name = str(row.iloc[0]).strip()
        dv   = day_values(row, date_cols)          # newest first
        conv_summary_items.append((name, dv, False))
    if conv_summary_items:
        render_summary(conv_summary_items)
else:
    st.info("Метрики конверсий не найдены")

if conv_table is not None:
    with st.expander("📋 Конверсии по дням (🟢 лучше вчера / 🔴 хуже вчера)", expanded=False):
        st.dataframe(conv_table, use_container_width=True, hide_index=True)

# ── Блок: Цена и скидки ───────────────────────────────────────────────────
st.divider()
st.subheader("💰 Цена и скидки")

price_changes = find_price_changes(df, date_cols, threshold_pct=5.0)

PRICE_RUB  = ["цена продавца до скидки мп", "цена покупателя после скидки мп"]
PRICE_PCT  = ["% скидки мп для покупателя"]

rub_rows = [(kw, get_row(df, [kw])) for kw in PRICE_RUB]
rub_rows = [(kw, r) for kw, r in rub_rows if r is not None]
pct_rows = [(kw, get_row(df, [kw])) for kw in PRICE_PCT]
pct_rows = [(kw, r) for kw, r in pct_rows if r is not None]

if rub_rows or pct_rows:
    RUB_COLORS = ["#4a9eff", "#f5a623"]   # продавец=синий, покупатель=оранжевый

    # Левый блок — совмещённый график двух рублёвых цен
    col_rub, col_pct = st.columns([2, 1])

    with col_rub:
        if rub_rows:
            combined_fig = go.Figure()
            for j, (kw, row) in enumerate(rub_rows):
                name  = str(row.iloc[0]).strip()
                chron = chronological(day_values(row, date_cols))
                color = RUB_COLORS[j % len(RUB_COLORS)]
                combined_fig.add_trace(go.Scatter(
                    x=[d for d, _ in chron], y=[v for _, v in chron],
                    mode="lines+markers", name=name,
                    line=dict(color=color, width=2), marker=dict(size=4),
                ))
            combined_fig.update_layout(
                title=dict(text="Цена продавца vs цена покупателя (₽)", font=dict(size=12, color="#e0e0e0")),
                height=220, margin=dict(l=10, r=10, t=35, b=10),
                paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                font=dict(color="#e0e0e0"),
                xaxis=dict(gridcolor="rgba(255,255,255,0.07)", zeroline=False, type="category"),
                yaxis=dict(gridcolor="rgba(255,255,255,0.07)", zeroline=False),
                legend=dict(orientation="h", y=1.15, font=dict(size=10)),
            )
            st.plotly_chart(combined_fig, use_container_width=True)

    # Правый блок — % скидки МП
    with col_pct:
        for kw, row in pct_rows:
            name  = str(row.iloc[0]).strip()
            chron = chronological(day_values(row, date_cols))
            dv    = list(reversed(chron))
            recent = sum(v for _, v in dv[:3]) / min(3, len(dv))
            older  = sum(v for _, v in dv[3:10]) / max(len(dv[3:10]), 1)
            color  = "#50c878" if recent >= older else "#e05050"
            pct_fig = go.Figure()
            pct_fig.add_trace(go.Scatter(
                x=[d for d, _ in chron], y=[v for _, v in chron],
                mode="lines+markers", line=dict(color=color, width=2), marker=dict(size=4),
                connectgaps=True,
            ))
            pct_fig.update_layout(
                title=dict(text=name, font=dict(size=12, color="#e0e0e0")),
                height=220, margin=dict(l=10, r=10, t=35, b=10),
                paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                font=dict(color="#e0e0e0"),
                xaxis=dict(gridcolor="rgba(255,255,255,0.07)", zeroline=False, type="category"),
                yaxis=dict(gridcolor="rgba(255,255,255,0.07)", zeroline=False),
                showlegend=False,
            )
            st.plotly_chart(pct_fig, use_container_width=True)

    # Summary под графиками
    price_summary_items = []
    for kw, row in rub_rows:
        price_summary_items.append((str(row.iloc[0]).strip(), day_values(row, date_cols), True))
    for kw, row in pct_rows:
        price_summary_items.append((str(row.iloc[0]).strip(), day_values(row, date_cols), False))
    if price_summary_items:
        render_summary(price_summary_items)

    # Детализация по дням — скрыта
    with st.expander("📋 Детализация по дням", expanded=False):
        for kw, row in rub_rows + pct_rows:
            name   = str(row.iloc[0]).strip()
            is_pct = "%" in name
            st.markdown(f"**{name}**")
            raw_df = build_price_table(row, date_cols, is_pct)
            st.dataframe(style_price_table(raw_df), use_container_width=True, hide_index=True,
                         column_config={"_pct": None, "_rub": None})
else:
    st.info("Ценовые метрики не найдены в файле")

# ── Блок: Эффективность рекламы ───────────────────────────────────────────
st.divider()
st.subheader("📣 Эффективность рекламы")

AD_METRICS = [
    ("Расход на рекламу",             ["расход на рекламу"],  "#e05050", True),
    ("ДРР от выручки",                ["дрр от выручки"],     "#f5a623", True),
    ("Прогноз ROMI по опер. прибыли", ["прогноз romi"],       "#4a9eff", False),
]

ad_charts  = []
ad_summary_items = []

for label, kws, color, is_hw in AD_METRICS:
    ad_row = get_row(df, kws)
    if ad_row is None:
        continue
    chron = chronological(day_values(ad_row, date_cols))
    if not chron:
        continue
    dv = list(reversed(chron))   # newest first

    afig = go.Figure()
    afig.add_trace(go.Scatter(
        x=[d for d, _ in chron], y=[v for _, v in chron],
        mode="lines+markers", name=label,
        line=dict(color=color, width=2), marker=dict(size=4),
        connectgaps=True,
    ))
    afig.update_layout(
        title=dict(text=label, font=dict(size=12, color="#e0e0e0")),
        height=200, margin=dict(l=10, r=10, t=35, b=10),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#e0e0e0"),
        xaxis=dict(gridcolor="rgba(255,255,255,0.07)", zeroline=False, type="category"),
        yaxis=dict(gridcolor="rgba(255,255,255,0.07)"),
        showlegend=False,
    )
    ad_charts.append(afig)
    ad_summary_items.append((label, dv, is_hw))

if ad_charts:
    ad_cols = st.columns(len(ad_charts))
    for i, fig in enumerate(ad_charts):
        with ad_cols[i]:
            st.plotly_chart(fig, use_container_width=True)
    if ad_summary_items:
        render_summary(ad_summary_items)
else:
    st.info("Метрики рекламы не найдены в файле")

# ── Блок: Операционная прибыль ────────────────────────────────────────────
st.divider()
st.subheader("💹 Операционная прибыль")

OP_METRICS = [
    ("Прогноз операционной прибыли",       ["прогноз операционной прибыли"], False),
    ("Прогноз операционной прибыли на ед.", ["на ед"],                       False),
]

op_charts        = []
op_summary_items = []

for label, kws, is_hw in OP_METRICS:
    op_row = get_row(df, kws)
    if op_row is None:
        continue
    chron = chronological(day_values(op_row, date_cols))
    if not chron:
        continue
    dv    = list(reversed(chron))
    vals_c = [v for _, v in chron]
    recent = sum(v for _, v in dv[:3]) / min(3, len(dv))
    older  = sum(v for _, v in dv[3:10]) / max(len(dv[3:10]), 1)
    color  = "#50c878" if recent >= older else "#e05050"

    ofig = go.Figure()
    ofig.add_trace(go.Scatter(
        x=[d for d, _ in chron], y=vals_c,
        mode="lines+markers",
        line=dict(color=color, width=2), marker=dict(size=4),
        connectgaps=True,
    ))
    ofig.update_layout(
        title=dict(text=label, font=dict(size=12, color="#e0e0e0")),
        height=200, margin=dict(l=10, r=10, t=35, b=10),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#e0e0e0"),
        xaxis=dict(gridcolor="rgba(255,255,255,0.07)", zeroline=False, type="category"),
        yaxis=dict(gridcolor="rgba(255,255,255,0.07)"),
        showlegend=False,
    )
    op_charts.append(ofig)
    op_summary_items.append((label, dv, is_hw))

if op_charts:
    op_cols = st.columns(len(op_charts))
    for i, fig in enumerate(op_charts):
        with op_cols[i]:
            st.plotly_chart(fig, use_container_width=True)
    if op_summary_items:
        render_summary(op_summary_items)
else:
    st.info("Метрики операционной прибыли не найдены в файле")

# ── Анализ Claude ──────────────────────────────────────────────────────────
st.divider()

if not api_key:
    st.warning("Введи API-ключ Claude в боковом меню чтобы получить анализ")
else:
    if st.button("🤖 Получить анализ от Claude", type="primary", use_container_width=True):
        art    = article or uploaded_file.name
        prompt = build_prompt(art, trends, date_cols, price_changes)
        with st.spinner("Claude анализирует метрики..."):
            try:
                analysis = call_claude(prompt, api_key)
                st.subheader("🧠 Анализ Claude")
                st.markdown(
                    """
                    <style>
                    [data-testid="stMarkdownContainer"] h2 {
                        font-size: 1.05em !important;
                        margin-top: 1.4em !important;
                        margin-bottom: 0.3em !important;
                        padding-bottom: 4px;
                        border-bottom: 1px solid rgba(255,255,255,0.08);
                    }
                    [data-testid="stMarkdownContainer"] li {
                        margin-bottom: 0.25em;
                        line-height: 1.6;
                    }
                    [data-testid="stMarkdownContainer"] p {
                        line-height: 1.7;
                        margin-bottom: 0.5em;
                    }
                    </style>
                    """,
                    unsafe_allow_html=True,
                )
                with st.container(border=True):
                    st.markdown(analysis)
                st.download_button("💾 Скачать анализ (txt)", data=analysis,
                                   file_name=f"analysis_{art}.txt", mime="text/plain")
            except anthropic.AuthenticationError:
                st.error("Неверный API-ключ. Проверь на console.anthropic.com")
            except Exception as e:
                st.error(f"Ошибка: {e}")
